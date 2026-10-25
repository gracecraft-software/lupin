"""Run and inspect delegation loops in Herdr sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis

from . import claims, run_log, slots, slots_redis

# A loop id is a repo name. `repo~2` is a second loop on the same repo on this
# machine, `repo~3` a third, and so on. There is no `repo~1`: that is `repo`.
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(~([2-9]|[1-9][0-9]))?$")
PLATFORMS = {"claude", "omp"}
AGENT_NAME = "lupin-loop"
# One Herdr session per machine. Each loop is one workspace in it.
SESSION_NAME = "lupin-loops"
STATE_DIR = Path(os.environ.get("LUPIN_LOOP_STATE_DIR", "/var/lib/delegation-loop"))
# The longest note a start request accepts. `lupin dispatch` uses this to
# decide when a brief is too long to pass as the note itself.
MAX_NOTE_CHARS = 8000
PROMPT_DOC_INLINE_LIMIT = 8000
CODE_DIR = Path(os.environ.get("LUPIN_LOOP_CODE_DIR", "/code"))
REPOS_FILE = STATE_DIR / "repos"
ORCHESTRATORS_FILE = STATE_DIR / "orchestrators.json"
LOOPS_DIR = STATE_DIR / "herdr-loops"
REPORTS_DIR = STATE_DIR / "reports"
HERDR = os.environ.get("LUPIN_HERDR_BIN", "herdr")
NIXOS_SUDO_WRAPPER = Path("/run/wrappers/bin/sudo")
DEFAULT_PROMPT = (
    "You are the delegation-loop orchestrator for this repository. Read "
    "AGENTS.md and /delegation-loop. If docs/delegation-loop.md exists, read "
    "it; otherwise continue without repo-specific delegation notes."
)
HERDR_TIMEOUT = 20.0
GIT_TIMEOUT = 60.0
CODEGRAPH_TIMEOUT = 120.0
# Files in the git directory that show an unfinished git operation.
UNFINISHED_GIT_STATES = (
    ("MERGE_HEAD", "merge"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)
SERVER_START_TIMEOUT = 30.0
PANE_READ_TIMEOUT = 60.0
SYSTEMCTL_CHECK_TIMEOUT = 3.0
SYSTEMCTL_STOP_TIMEOUT = 30.0
LEASE_TTL = 60.0
# The monitor renews the run's claims this often. A claim lasts 10 minutes.
CLAIM_RENEW_INTERVAL = 120.0
HANDOFF_GRACE_S = 600.0
HANDOFF_TEXT = (
    "Lupin will stop this loop shortly. Run /handoff now: write an entry to the "
    ".loop/loop-state.json ledger (issue number if any, repo, branch, last known "
    "status) and update the relevant GitHub issue. Lupin keeps a copy of that "
    "ledger when it stops. Lupin deletes other ignored files in the worktree, so "
    "keep anything you need in the ledger or the issue. Lupin stops this loop when "
    "you finish, or after the wait time ends."
)

def validate_orchestrator(selector: str) -> str:
    if not isinstance(selector, str):
        raise LoopError("orchestrator selector must be text")
    if selector == "claude":
        return selector
    provider, separator, model = selector.partition("/")
    if not separator or provider not in {"openai", "openai-codex", "opencode-go"}:
        raise LoopError("orchestrator must be claude, openai/MODEL, openai-codex/MODEL, or opencode-go/MODEL")
    provider, model = validate_omp_options("omp", provider, model)
    return f"{provider}/{model}"


def orchestrator_profiles() -> dict[str, list[str]]:
    if not ORCHESTRATORS_FILE.exists():
        return {}
    try:
        values = json.loads(ORCHESTRATORS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"could not read {ORCHESTRATORS_FILE}: {exc}") from exc
    if not isinstance(values, dict):
        raise LoopError(f"invalid orchestrator profiles in {ORCHESTRATORS_FILE}")
    profiles: dict[str, list[str]] = {}
    for repo, selectors in values.items():
        repo = validate_repo(repo)
        if not isinstance(selectors, list) or not selectors:
            raise LoopError(f"invalid orchestrator profile for {repo}")
        profiles[repo] = list(dict.fromkeys(validate_orchestrator(item) for item in selectors))
    return profiles


def _write_orchestrator_profiles(profiles: dict[str, list[str]]) -> None:
    ORCHESTRATORS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    content = json.dumps(profiles, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".orchestrators.", dir=ORCHESTRATORS_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, ORCHESTRATORS_FILE)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def select_orchestrator(
    candidates: list[str], snapshot: dict, *, now: float | None = None
) -> dict[str, str | float | None]:
    from . import pace, quota_cache

    now = time.time() if now is None else now
    now_ms = now * 1000
    try:
        weekly_targets = pace.weekly_daily_targets()
    except pace.PacingConfigError as exc:
        raise EnvValueError(str(exc)) from exc
    choices: list[tuple[float, int, dict[str, str | float | None]]] = []
    for index, raw in enumerate(candidates):
        selector = validate_orchestrator(raw)
        if selector == "claude":
            provider, model = "claude", None
            platform = "claude"
        else:
            platform = "omp"
            provider, _ = selector.split("/", 1)
            quota_provider = "openai" if provider == "openai-codex" else provider
            provider = quota_provider
            model = selector
        entry = snapshot.get(provider)
        if not isinstance(entry, dict) or not quota_cache.is_fresh(entry, now):
            continue
        rows = entry.get("rows", [])
        if not isinstance(rows, list):
            continue
        rows = [row for row in rows if isinstance(row, dict)]
        headroom = pace.headroom(
            rows, provider, now_ms, weekly_targets=weekly_targets
        )
        if headroom is None:
            continue
        choices.append((headroom, -index, {
            "platform": platform,
            "provider": ("openai-codex" if provider == "openai" else provider) if platform == "omp" else None,
            "model": model,
            "quota_headroom": headroom,
        }))
    if not choices:
        raise LoopError("no configured orchestrator has fresh, usable quota")
    return max(choices, key=lambda choice: (choice[0], choice[1]))[2]


class LoopError(Exception):
    """A loop action cannot continue safely."""


class EnvValueError(LoopError):
    """A `LUPIN_*` environment value is malformed."""


class HerdrError(LoopError):
    """Herdr returned an error or an unsupported response."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def validate_repo(repo: Any) -> str:
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise LoopError(f"invalid repo name {repo!r}")
    return repo


def loop_repo(loop: str) -> str:
    """The repo part of a loop id, with no check. Use it on data from other machines."""
    return loop.partition("~")[0]


def base_repo(loop: str) -> str:
    """The repo that a loop works on: `bodysmith~2` works on `bodysmith`.

    Use it for anything that belongs to the repo and not to one loop: the
    checkout, the credentials, the profile, the fleet slot.
    """
    return loop_repo(validate_repo(loop))


def validate_plain_repo(repo: Any) -> str:
    """A repo name that is not a numbered loop. Use it for what is kept per repo."""
    repo = validate_repo(repo)
    if repo != base_repo(repo):
        raise LoopError(f"{repo} is a loop, not a repo; use the repo name {base_repo(repo)}")
    return repo


def validate_platform(platform: Any) -> str:
    if platform not in PLATFORMS:
        raise LoopError(f"invalid platform {platform!r}; use claude or omp")
    return platform


def session_name(repo: str) -> str:
    validate_repo(repo)
    return SESSION_NAME


def legacy_session_name(repo: str) -> str:
    """The session name used before loops shared one session per machine."""
    repo = validate_repo(repo)
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", repo)[:38].strip(".-") or "repo"
    digest = hashlib.sha256(repo.encode()).hexdigest()[:10]
    return f"lupin-{slug}-{digest}"


def agent_name(repo: str) -> str:
    """A Herdr agent name for one repo: lowercase, digits, - and _, up to 32 characters."""
    repo = validate_repo(repo)
    slug = re.sub(r"[^a-z0-9_-]", "-", repo.lower())[:15].strip("-_") or "repo"
    digest = hashlib.sha256(repo.encode()).hexdigest()[:8]
    return f"lupin-{slug}-{digest}"


def _owns_session(repo: str, session: Any) -> bool:
    return session in (SESSION_NAME, legacy_session_name(repo))


def _agent_name_for(repo: str, session: str) -> str:
    """Loops in the shared session need their own agent name. Old loops keep AGENT_NAME."""
    return agent_name(repo) if session == SESSION_NAME else AGENT_NAME


def unit_name(repo: str, kind: str = "loop") -> str:
    return f"lupin-{kind}-{hashlib.sha256(validate_repo(repo).encode()).hexdigest()[:16]}"


def _lupin_command(*args: str) -> list[str]:
    executable = os.environ.get("LUPIN_EXECUTABLE") or str(Path(sys.argv[0]).resolve())
    return [executable, "loop", *args]


def _run(argv: list[str], *, timeout: float = HERDR_TIMEOUT, env: dict | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoopError(f"could not run {argv[0]}: {exc}") from exc
    output = proc.stdout
    if proc.stderr:
        output += proc.stderr
    return proc.returncode, output.strip()


def _sudo_argv(*args: str) -> list[str]:
    """Use NixOS's sudo wrapper when the service PATH omits it."""
    executable = (
        str(NIXOS_SUDO_WRAPPER)
        if NIXOS_SUDO_WRAPPER.is_file() and os.access(NIXOS_SUDO_WRAPPER, os.X_OK)
        else "sudo"
    )
    # The module grants NOPASSWD to a validating wrapper, never to
    # systemd-run itself, because systemd-run can change the unit user.
    wrapper = os.environ.get("LUPIN_SYSTEMD_RUN")
    if wrapper and args and args[0] == "systemd-run":
        return [executable, "-n", wrapper, *args[1:]]
    return [executable, "-n", *args]


def _herdr_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HERDR_ENV"] = "1"
    return env


def _herdr(session: str | None, *args: str, timeout: float = HERDR_TIMEOUT) -> str:
    argv = [HERDR]
    if session:
        argv.extend(["--session", session])
    argv.extend(args)
    rc, output = _run(argv, timeout=timeout, env=_herdr_env())
    if rc:
        raise HerdrError(output or f"Herdr exited with status {rc}")
    return output


def _herdr_json(session: str | None, *args: str, timeout: float = HERDR_TIMEOUT) -> dict:
    output = _herdr(session, *args, timeout=timeout)
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise HerdrError("Herdr returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise HerdrError("Herdr returned an unexpected JSON value")
    if "error" in value:
        error = value["error"]
        message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
        raise HerdrError(message)
    result = value.get("result", value)
    if not isinstance(result, dict):
        raise HerdrError("Herdr returned an unexpected result")
    return result


def herdr_sessions() -> list[dict]:
    result = _herdr_json(None, "session", "list", "--json")
    sessions = result.get("sessions", [])
    return [row for row in sessions if isinstance(row, dict)]


def _server_running(session: str) -> bool:
    try:
        result = _herdr_json(session, "status", "server", "--json")
    except LoopError:
        return False
    return result.get("running") is True or result.get("status") == "running"


def _workspaces(session: str) -> list[dict]:
    result = _herdr_json(session, "workspace", "list")
    rows = [row for row in result.get("workspaces", []) if isinstance(row, dict)]
    if any("root_pane" not in row for row in rows):
        # Some Herdr versions list workspaces without their root pane.
        panes = [
            pane for pane in _herdr_json(session, "pane", "list").get("panes", [])
            if isinstance(pane, dict)
        ]
        for row in rows:
            if "root_pane" not in row:
                pane = next((p for p in panes if p.get("workspace_id") == _workspace_id(row)), None)
                if pane:
                    row["root_pane"] = pane
    return rows


def _agents(session: str) -> list[dict]:
    result = _herdr_json(session, "agent", "list")
    rows = result.get("agents", [])
    return [row for row in rows if isinstance(row, dict)]


def _agent_for_workspace(session: str, workspace_id: str) -> dict | None:
    for agent in _agents(session):
        name = agent.get("name")
        if agent.get("workspace_id") == workspace_id and isinstance(name, str) and name.startswith("lupin-"):
            return agent
    return None


def _workspace_by_id(session: str, workspace_id: str | None) -> dict | None:
    if not workspace_id:
        return None
    return next((row for row in _workspaces(session) if _workspace_id(row) == workspace_id), None)


def _workspace_root(workspace: dict) -> dict:
    pane = workspace.get("root_pane")
    return pane if isinstance(pane, dict) else {}


def _workspace_id(workspace: dict) -> str | None:
    value = workspace.get("id") or workspace.get("workspace_id")
    if isinstance(value, str):
        return value
    return _workspace_root(workspace).get("workspace_id")


def _pane_id(workspace: dict) -> str | None:
    value = _workspace_root(workspace).get("pane_id")
    return value if isinstance(value, str) else None


def _agent_state(session: str, workspace: dict) -> str:
    workspace_id = _workspace_id(workspace)
    if workspace_id:
        agent = _agent_for_workspace(session, workspace_id)
        if agent and agent.get("agent_status") in {"idle", "working", "blocked", "done", "unknown"}:
            return agent["agent_status"]
    state = _workspace_root(workspace).get("agent_status")
    return state if state in {"idle", "working", "blocked", "done", "unknown"} else "unknown"


def _metadata_path(repo: str) -> Path:
    return LOOPS_DIR / f"{validate_repo(repo)}.json"


def _read_metadata(repo: str) -> dict | None:
    path = _metadata_path(repo)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"could not read loop state for {repo}: {exc}") from exc
    if not isinstance(value, dict) or value.get("repo") != repo:
        raise LoopError(f"invalid loop state for {repo}")
    return value


def _write_metadata(repo: str, value: dict) -> None:
    path = _metadata_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, temporary = tempfile.mkstemp(prefix=f".{repo}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _log_event(repo: str, metadata: dict | None, event: str, **fields: object) -> None:
    run_id = metadata.get("run_id") if isinstance(metadata, dict) else None
    if isinstance(run_id, str):
        run_log.append_event(STATE_DIR, repo, run_id, event, **fields)


def _log_current_event(repo: str, event: str, **fields: object) -> None:
    try:
        metadata = _read_metadata(repo)
    except Exception:
        return
    _log_event(repo, metadata, event, **fields)


def _set_loop_state(repo: str, metadata: dict, state: str, **fields: object) -> None:
    previous = metadata.get("state")
    metadata["state"] = state
    _write_metadata(repo, metadata)
    if previous != state:
        _log_event(
            repo, metadata, "state_transition",
            from_state=previous, to_state=state, **fields,
        )


def _default_repos() -> list[tuple[str, str]]:
    raw = os.environ.get("LUPIN_LOOP_DEFAULT_REPOS", "")
    return [(validate_plain_repo(item), "claude") for item in raw.split(",") if item.strip()]


def enabled_repos() -> dict[str, str]:
    if not REPOS_FILE.exists():
        defaults = _default_repos()
        write_repos(defaults)
    values: dict[str, str] = {}
    try:
        lines = REPOS_FILE.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        raise LoopError(f"could not read {REPOS_FILE}: {exc}") from exc
    for line in lines:
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        repo = validate_plain_repo(fields[0])
        platform = fields[1] if len(fields) > 1 else "claude"
        values[repo] = validate_platform(platform)
    return values


def write_repos(values: list[tuple[str, str]]) -> None:
    REPOS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    content = "".join(f"{validate_plain_repo(repo)} {validate_platform(platform)}\n" for repo, platform in values)
    fd, temporary = tempfile.mkstemp(prefix=".repos.", dir=REPOS_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, REPOS_FILE)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def enable_repo(
    repo: str,
    platform: str = "claude",
    orchestrators: list[str] | None = None,
    *,
    clear_orchestrators: bool = False,
) -> None:
    repo = validate_plain_repo(repo)
    platform = validate_platform(platform)
    if orchestrators is not None and clear_orchestrators:
        raise LoopError("use --orchestrator or --clear-orchestrators, not both")
    profiles = None
    if orchestrators is not None or clear_orchestrators:
        profiles = orchestrator_profiles()
        if clear_orchestrators:
            profiles.pop(repo, None)
        else:
            candidates = list(dict.fromkeys(validate_orchestrator(item) for item in orchestrators))
            if not candidates:
                raise LoopError("at least one orchestrator is required")
            profiles[repo] = candidates
    values = enabled_repos()
    values[repo] = platform
    write_repos(sorted(values.items()))
    if profiles is not None:
        _write_orchestrator_profiles(profiles)


def disable_repo(repo: str) -> None:
    repo = validate_plain_repo(repo)
    values = enabled_repos()
    values.pop(repo, None)
    profiles = orchestrator_profiles()
    profiles.pop(repo, None)
    write_repos(sorted(values.items()))
    if ORCHESTRATORS_FILE.exists():
        _write_orchestrator_profiles(profiles)


def repo_catalog() -> list[dict]:
    enabled = enabled_repos()
    rows = []
    if not CODE_DIR.is_dir():
        return rows
    for path in sorted(CODE_DIR.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_dir() or not REPO_RE.fullmatch(path.name) or "~" in path.name:
            continue
        has_doc = (path / "docs" / "delegation-loop.md").is_file()
        rows.append({
            "repo": path.name,
            "loopable": True,
            "has_doc": has_doc,
            "state": "enabled" if path.name in enabled else "disabled",
            "platform": enabled.get(path.name, "claude"),
        })
    return rows


def _runtime_paths(repo: str) -> dict:
    path = os.environ.get("LUPIN_LOOP_CREDENTIALS_FILE")
    if not path:
        return {}
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"could not read loop credential map: {exc}") from exc
    if not isinstance(values, dict):
        raise LoopError("loop credential map must be a JSON object")
    value = values.get(base_repo(repo))
    if value is None:
        return {}
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise LoopError(f"invalid credential path for {repo}")
    if not Path(value).is_file():
        raise LoopError(f"credential file for {repo} is not available")
    return {"gh-token": value}


def _password_source() -> str | None:
    value = os.environ.get("LUPIN_LOOP_REDIS_PASSWORD_FILE")
    if value and Path(value).is_file():
        return value
    return None


def _credential_properties(values: dict[str, str]) -> list[str]:
    return [f"--property=LoadCredential={name}:{source}" for name, source in values.items()]


def _loop_user() -> str:
    user = os.environ.get("LUPIN_LOOP_USER") or os.environ.get("USER")
    if not user:
        raise LoopError("no loop user: set LUPIN_LOOP_USER or USER")
    return user


def _service_environment() -> list[str]:
    names = (
        "HOME", "LUPIN_BACKEND", "LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT",
        "LUPIN_REDIS_USERNAME", "LUPIN_STATE_ROOT", "LUPIN_EXECUTABLE", "LUPIN_HERDR_BIN",
        "LUPIN_GH_CANONICAL_FETCHER",
        "LUPIN_LOOP_STATE_DIR", "LUPIN_LOOP_CODE_DIR", "LUPIN_LOOP_USER", "LUPIN_LOOP_GROUP",
        "LUPIN_LOOP_DEFAULT_REPOS", "LUPIN_LOOP_CLAUDE_MAX_CONCURRENT",
        "LUPIN_WEEKLY_QUOTA_DAILY_TARGETS",
        "LUPIN_LOOP_CREDENTIALS_FILE", "LUPIN_LOOP_REDIS_PASSWORD_FILE",
        "LUPIN_LOOP_PROMPT_FILE", "LUPIN_LOOP_PATH", "ANTHROPIC_BASE_URL",
        "LUPIN_SYSTEMD_RUN",
        "HEADROOM_TELEMETRY_DISABLED", "RTK_TELEMETRY_DISABLED", "CODEGRAPH_TELEMETRY",
        "DO_NOT_TRACK",
    )
    properties = []
    path = os.environ.get("LUPIN_LOOP_PATH")
    if path:
        properties.append(f"--setenv=PATH={path}")
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            properties.append(f"--setenv={name}={value}")
    return properties


def _systemd_run(
    repo: str | None,
    kind: str,
    command: list[str],
    *,
    credentials: dict[str, str] | None = None,
    claude_limits: bool = False,
) -> tuple[int, str]:
    user = _loop_user()
    group = os.environ.get("LUPIN_LOOP_GROUP", "users")
    unit = unit_name(repo, kind) if repo else f"lupin-{kind}-machine"
    argv = _sudo_argv(
        "systemd-run", f"--unit={unit}", "--collect",
        f"--uid={user}", f"--gid={group}", "--property=KillMode=control-group",
        "--property=TimeoutStopSec=20s",
    )
    if kind == "loop":
        argv.extend([
            "--property=Restart=on-failure",
            "--property=RestartSec=10s",
            "--property=StartLimitIntervalSec=5min",
            "--property=StartLimitBurst=3",
        ])
    if claude_limits:
        argv.extend(["--property=OOMPolicy=kill", "--property=MemoryMax=5G"])
    argv.extend(_service_environment())
    argv.extend(_credential_properties(credentials or {}))
    directory = CODE_DIR / base_repo(repo) if repo else CODE_DIR
    argv.extend(["--working-directory", str(directory), "--", *command])
    return _run(argv, timeout=HERDR_TIMEOUT)


def _lock_file(repo: str):
    locks = STATE_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o750)
    return (locks / f"{validate_repo(repo)}.lock").open("a+")


def _machine_lock():
    locks = STATE_DIR / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o750)
    return (locks / "herdr-server.lock").open("a+")


def _legacy_tmux_exists(repo: str) -> bool:
    rc, _ = _run(["tmux", "has-session", "-t", f"=loop-{validate_repo(repo)}:"], timeout=2.0)
    return rc == 0


def _session_info(session: str) -> dict | None:
    return next((row for row in herdr_sessions() if row.get("name") == session), None)


def _session_running(session: str) -> bool:
    info = _session_info(session)
    return bool(info and info.get("running"))


def _write_prompt(repo: str, note: str | None, resume: bool, worktree: Path) -> str:
    base_path = os.environ.get("LUPIN_LOOP_PROMPT_FILE")
    base = Path(base_path).read_text(encoding="utf-8") if base_path else DEFAULT_PROMPT
    if resume:
        base = (
            "You were cut off, not finished. Check for work in progress and continue it.\n\n"
            + base
        )
    source_doc = (CODE_DIR / base_repo(repo) / "docs" / "delegation-loop.md").absolute()
    worktree_doc = worktree / "docs" / "delegation-loop.md"
    if source_doc.is_file():
        current_doc = source_doc.read_text(encoding="utf-8")
        if not worktree_doc.is_file() or (
            worktree_doc.read_text(encoding="utf-8") != current_doc
        ):
            base += (
                f"\n\nThe current checkout has a saved docs/delegation-loop.md at {source_doc} "
                "It is missing from or different in this run's worktree. Use it instead."
            )
            if len(current_doc) <= PROMPT_DOC_INLINE_LIMIT:
                base += f"\n\nCurrent docs/delegation-loop.md:\n\n{current_doc}"
    handoff = STATE_DIR / "handoffs" / f"{validate_repo(repo)}.json"
    if handoff.is_file():
        base = (
            f"The last run kept its ledger at {handoff}. Read it before you start.\n\n"
            + base
        )
    handoff_doc = STATE_DIR / "handoffs" / f"{validate_repo(repo)}.HANDOFF.md"
    if handoff_doc.is_file():
        base = (
            f"The last run kept its HANDOFF.md at {handoff_doc}. Read it before you start.\n\n"
            + base
        )
    if repo != base_repo(repo):
        base += (
            f"\n\nYou are the loop {repo}. Other loops work on {base_repo(repo)} on this "
            "machine at the same time. Claim an issue with your own --holder before you "
            "start it, so that no two loops work the same issue.\n"
        )
    if note:
        base += f"\n\nExtra instructions for this run:\n{note}\n"
    notes = STATE_DIR / "notes"
    notes.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = notes / f"prompt-{validate_repo(repo)}-{time.time_ns()}.md"
    path.write_text(base, encoding="utf-8")
    os.chmod(path, 0o640)
    return str(path)


def validate_omp_options(
    platform: str, provider: str | None, model: str | None
) -> tuple[str | None, str | None]:
    if provider is not None and (
        not isinstance(provider, str) or provider not in {"openai", "openai-codex", "opencode-go"}
    ):
        raise LoopError("OMP provider must be openai, openai-codex, or opencode-go")
    if model is not None and (
        not isinstance(model, str)
        or not model
        or len(model) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in model)
    ):
        raise LoopError("invalid OMP model")
    if platform != "omp" and (provider is not None or model is not None):
        raise LoopError("--provider and --model require --platform omp")
    if provider == "openai":
        provider = "openai-codex"
    if model and model.startswith("openai/"):
        model = "openai-codex/" + model.split("/", 1)[1]
    return provider, model


def _git(directory: Path, *args: str) -> str:
    """Run git in `directory`. Return its output. Raise LoopError if git fails."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoopError(f"could not run git in {directory}: {exc}") from exc
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise LoopError(f"git {args[0]} failed in {directory}: {detail}")
    return proc.stdout


def _refuse_unfinished_git_work(checkout: Path) -> None:
    """Raise LoopError if the checkout has an unfinished git operation or unmerged files.

    This function only reads. It never aborts, resets, stashes, or cleans.
    """
    git_dir = Path(_git(checkout, "rev-parse", "--absolute-git-dir").strip())
    for name, label in UNFINISHED_GIT_STATES:
        marker = git_dir / name
        if marker.exists():
            raise LoopError(
                f"{checkout} has a {label} in progress ({marker}). "
                "Lupin does not change it. Finish or cancel it, then start the loop again."
            )
    entries = _git(checkout, "ls-files", "-u", "-z").split("\0")
    unmerged = sorted({entry.split("\t", 1)[1] for entry in entries if "\t" in entry})
    if unmerged:
        shown = ", ".join(unmerged[:5])
        if len(unmerged) > 5:
            shown += f" and {len(unmerged) - 5} more"
        raise LoopError(
            f"{checkout} has unmerged files: {shown}. "
            "Lupin does not change them. Resolve them, then start the loop again."
        )


def _default_base(checkout: Path) -> str:
    """Return the remote branch that a run starts from, for example `origin/main`."""
    try:
        base = _git(checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    except LoopError:
        base = ""
    if base:
        return base
    try:
        _git(checkout, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main")
    except LoopError:
        raise LoopError(
            f"cannot find the default branch of {checkout}. "
            "Run `git remote set-head origin --auto` in it."
        ) from None
    return "origin/main"


def _create_worktree(repo: str, checkout: Path) -> tuple[Path, str]:
    """Add a worktree for one run, on a new branch from the fetched default branch.

    Return the worktree path and the branch name. The worktree is under the
    loop state directory, not under CODE_DIR. Raise LoopError if the fetch fails.
    """
    _git(checkout, "fetch", "origin")
    base = _default_base(checkout)
    worktree_id = f"{_stamp()}-{time.time_ns() % 1000000:06d}"
    branch = f"lupin-loop/{worktree_id}"
    path = STATE_DIR / "worktrees" / repo / worktree_id
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    _git(checkout, "worktree", "add", "--no-track", "-b", branch, str(path), base)
    return path, branch


def _index_worktree(checkout: Path, worktree: Path) -> dict[str, str] | None:
    """Build a CodeGraph index in a new worktree. Return the log fields, or None.

    The repo ignores the index, so git does not copy it from the checkout. Do
    nothing when the checkout has no index. Skip when `codegraph` is not on PATH.
    Skip when git does not ignore the index: it would make the worktree dirty,
    and `lupin stop` keeps a dirty worktree. A failure never stops the run.
    """
    if not (checkout / ".codegraph" / "codegraph.db").is_file():
        return None
    codegraph = shutil.which("codegraph")
    if not codegraph:
        return {"status": "skipped", "reason": "codegraph is not on PATH"}
    try:
        ignored = subprocess.run(
            ["git", "-C", str(worktree), "check-ignore", "-q", ".codegraph/codegraph.db"],
            capture_output=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
        if ignored.returncode:
            return {"status": "skipped", "reason": "git does not ignore .codegraph/codegraph.db"}
        proc = subprocess.run(
            [codegraph, "init", "-y", str(worktree)],
            capture_output=True,
            text=True,
            timeout=CODEGRAPH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        reason = lines[-1][:200] if lines else f"exit code {proc.returncode}"
        return {"status": "failed", "reason": reason}
    return {"status": "indexed"}


def _resumed_worktree(repo: str, metadata: dict | None) -> tuple[Path, str]:
    """Return the worktree and branch that the last run recorded. Never create one."""
    info = metadata or {}
    recorded = info.get("worktree")
    if not recorded:
        raise LoopError(f"cannot resume {repo}: no worktree is recorded. Start it without --resume.")
    path = Path(recorded)
    if not path.is_dir():
        raise LoopError(f"cannot resume {repo}: worktree {path} is missing. Start it without --resume.")
    branch = _git(path, "branch", "--show-current").strip()
    if branch != info.get("branch"):
        raise LoopError(
            f"cannot resume {repo}: worktree {path} is on branch {branch or 'none'}, "
            f"not {info.get('branch')}."
        )
    return path, branch


def _keep_handoff(repo: str, worktree: Path) -> None:
    """Copy handoff files out of the worktree before git deletes them.

    git deletes ignored files with the worktree. A file missing from this
    worktree keeps its earlier copy.
    """
    copies = [
        (worktree / ".loop" / "loop-state.json", f"{repo}.json"),
        (worktree / "HANDOFF.md", f"{repo}.HANDOFF.md"),
    ]
    found = [(source, name) for source, name in copies if source.is_file()]
    if not found:
        return
    target = STATE_DIR / "handoffs"
    target.mkdir(parents=True, exist_ok=True, mode=0o750)
    for source, name in found:
        shutil.copyfile(source, target / name)


def _remove_worktree(repo: str, metadata: dict) -> tuple[bool, str]:
    """Remove the run's worktree, then its branch. Never use `--force`.

    Return (gone, note). `gone` is False when git keeps the worktree. The note
    names what was kept and starts with "; " so a caller can append it. Raise
    LoopError when git fails for another reason. A branch that git will not
    delete only adds a note. "worktree" leaves `metadata` only when the worktree
    is gone. "branch" leaves it only when the branch is deleted.
    """
    value = metadata.get("worktree")
    if value:
        path = Path(value)
        if path.is_dir():
            if not _git(path, "for-each-ref", "--contains", "HEAD", "refs/heads", "refs/remotes").strip():
                return False, f"; worktree kept at {path}: its commit is on no branch"
            _keep_handoff(repo, path)
            try:
                _git(CODE_DIR / base_repo(repo), "worktree", "remove", str(path))
            except LoopError as exc:
                try:
                    changed = _git(path, "status", "--porcelain").strip()
                except LoopError:
                    changed = ""
                if changed:
                    return False, f"; worktree kept at {path}: it has changes"
                raise LoopError(f"worktree kept at {path}: {exc}") from exc
        metadata.pop("worktree", None)
    branch = metadata.get("branch")
    if branch:
        try:
            _git(CODE_DIR / base_repo(repo), "branch", "-d", branch)
        except LoopError:
            return True, f"; branch {branch} kept: git branch -d refused it"
        metadata.pop("branch", None)
    return True, ""


def _clear_previous_worktree(repo: str, metadata: dict | None) -> str:
    """Remove the last run's worktree before a new run starts. Return a note.

    Raise LoopError if git keeps that worktree.
    """
    if not metadata:
        return ""
    gone, note = _remove_worktree(repo, metadata)
    if not gone:
        raise LoopError(f"cannot start {repo}{note}. Save or remove that work, then start again.")
    return note


def _finish_failed_start(repo: str, value: dict, resume: bool) -> str:
    """Record a failed start and remove what it made. Return a note for the message.

    A resume keeps the recorded worktree, so the next resume can reuse it.
    Otherwise remove the worktree and its branch. When git keeps the worktree,
    the note names its path. Metadata is written even if removal raises.
    """
    previous_state = value.get("state")
    value["state"] = "failed"
    removed_note = ""
    try:
        if not resume:
            try:
                _, removed_note = _remove_worktree(repo, value)
            except LoopError as exc:
                removed_note = f"; {exc}"
    finally:
        _write_metadata(repo, value)
        if previous_state != "failed":
            _log_event(
                repo, value, "state_transition",
                from_state=previous_state, to_state="failed",
            )
        if value["prompt_file"]:
            Path(value["prompt_file"]).unlink(missing_ok=True)
    return removed_note


def start_loop(
    repo: str,
    *,
    platform: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    note: str | None = None,
    resume: bool = False,
) -> tuple[bool, str]:
    repo = validate_repo(repo)
    source = base_repo(repo)
    selected = validate_platform(platform or enabled_repos().get(source, "claude"))
    provider, model = validate_omp_options(selected, provider, model)
    checkout = CODE_DIR / source
    if not checkout.is_dir():
        return False, f"skip {repo}: no checkout at {checkout}"
    if note is not None and (
        not isinstance(note, str) or len(note) > MAX_NOTE_CHARS or "\x00" in note
    ):
        raise LoopError("note is invalid or too long")
    session = session_name(repo)
    worker_unit = unit_name(repo)
    lock = _lock_file(repo)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return False, f"skip {repo}: another start request is active"
    try:
        rc, _ = _run(
            ["systemctl", "is-active", "--quiet", f"{worker_unit}.service"],
            timeout=3.0,
        )
        if rc == 0:
            return False, f"skip {repo}: Lupin loop is already active"
        if _legacy_tmux_exists(repo):
            return False, (
                f"skip {repo}: legacy tmux loop-{repo} is open; "
                "drain it before starting Herdr"
            )
        metadata = _read_metadata(repo)
        if metadata and metadata.get("state") in {"running", "starting", "needs_attention"}:
            if _session_running(metadata.get("session") or session):
                return False, f"skip {repo}: Herdr loop is already running; use lupin stop first"
            return False, f"skip {repo}: saved Herdr state needs review; use lupin stop before starting again"
        legacy = _session_info(legacy_session_name(repo))
        if legacy and legacy.get("running"):
            return False, f"skip {repo}: an old Herdr session is still running; use lupin stop first"
        routing_decision = "configured_platform"
        quota_headroom = None
        if platform is None and provider is None and model is None:
            candidates = orchestrator_profiles().get(source)
            if candidates:
                from . import quota_cache

                choice = select_orchestrator(candidates, quota_cache.read_snapshot(**_redis_kwargs()))
                routing_decision = "profile"
                quota_headroom = choice.get("quota_headroom")
                selected = choice["platform"]
                provider = choice["provider"]
                model = choice["model"]
        else:
            routing_decision = "explicit"
        provider, model = validate_omp_options(selected, provider, model)
        _refuse_unfinished_git_work(checkout)
        run_id = run_log.new_run_id()
        previous_state = metadata.get("state") if metadata else None
        if resume:
            worktree, branch = _resumed_worktree(repo, metadata)
            previous_note = ""
        else:
            previous_note = _clear_previous_worktree(repo, metadata)
            worktree, branch = _create_worktree(repo, checkout)
        value = {
            "run_id": run_id,
            "version": 1,
            "repo": repo,
            "platform": selected,
            "provider": provider,
            "model": model,
            "session": session,
            "state": "starting",
            "started_at": _now(),
            "workspace_id": None,
            "pane_id": None,
            "prompt_file": None,
            "resume": bool(resume),
            "worktree": str(worktree),
            "branch": branch,
        }
        try:
            prompt_file = _write_prompt(repo, note, resume, worktree)
            value["prompt_file"] = prompt_file
            _log_event(
                repo, value, "run_start",
                platform=selected, provider=provider, model=model, resume=bool(resume),
            )
            _log_event(
                repo, value, "routing_decision",
                decision=routing_decision, platform=selected, provider=provider, model=model,
                quota_headroom=quota_headroom,
            )
            if not resume:
                indexed = _index_worktree(checkout, worktree)
                if indexed:
                    _log_event(repo, value, "codegraph_index", **indexed)
            run_log.prune_runs(STATE_DIR, repo)
            _write_metadata(repo, value)
            if previous_state != "starting":
                _log_event(
                    repo, value, "state_transition",
                    from_state=previous_state, to_state="starting",
                )
            worker = _lupin_command(
                "worker", "--repo", repo, "--platform", selected, "--session", session,
                "--prompt-file", prompt_file,
            )
            if resume:
                worker.append("--resume")
            password_source = _password_source()
            worker_credentials = {"redis-password": password_source} if password_source else {}
            rc, output = _systemd_run(
                repo,
                "loop",
                worker,
                credentials=worker_credentials,
                claude_limits=selected == "claude",
            )
        except BaseException as exc:
            _log_event(
                repo, value, "error",
                error_type=type(exc).__name__, message=str(exc),
            )
            removed_note = _finish_failed_start(repo, value, resume)
            if not isinstance(exc, Exception):
                raise
            return False, (str(exc) or f"could not start loop service for {repo}") + removed_note
        if rc:
            _log_event(
                repo, value, "error",
                error_type="WorkerStartError", message=output or f"could not start loop service for {repo}",
            )
            removed_note = _finish_failed_start(repo, value, resume)
            return False, (output or f"could not start loop service for {repo}") + removed_note
        return True, (
            f"started {repo} ({selected}) in Herdr session {session}; "
            f"worktree {worktree}{previous_note}"
        )
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _load_credential(name: str) -> str | None:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return None
    path = Path(directory) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _env_int(name: str, default: int) -> int:
    """A whole number from `name`, or a clean `EnvValueError`."""
    value = os.environ.get(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise EnvValueError(f"{name} needs a whole number, got {value!r}") from None


def _redis_kwargs() -> dict:
    password = _load_credential("redis-password") or os.environ.get("LUPIN_REDIS_PASSWORD")
    return {
        "redis_host": os.environ.get("LUPIN_REDIS_HOST", "localhost"),
        "redis_port": _env_int("LUPIN_REDIS_PORT", 6379),
        "redis_username": os.environ.get("LUPIN_REDIS_USERNAME"),
        "redis_password": password,
    }


def _wait_for_server(session: str, label: str) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if _server_running(session):
            return
        time.sleep(0.25)
    raise LoopError(f"Herdr server did not start for {label} within {SERVER_START_TIMEOUT:g}s")


def _ensure_shared_server(session: str) -> None:
    """Start the machine's one Herdr server. The lock stops two starts racing."""
    lock = _machine_lock()
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if _server_running(session):
            return
        command = _lupin_command("herdr-server", "--session", session)
        rc, output = _systemd_run(None, "herdr", command)
        if rc and not _server_running(session):
            raise LoopError(output or "could not start the Herdr server")
        _wait_for_server(session, "this machine")
    finally:
        lock.close()


def _ensure_server(repo: str, session: str, platform: str) -> None:
    if _server_running(session):
        return
    if session == SESSION_NAME:
        _ensure_shared_server(session)
        return
    credentials = _runtime_paths(repo)
    command = _lupin_command("herdr-server", "--session", session)
    rc, output = _systemd_run(repo, "herdr", command, credentials=credentials, claude_limits=platform == "claude")
    if rc:
        raise LoopError(output or f"could not start Herdr server for {repo}")
    _wait_for_server(session, repo)


def _metadata_for_session(repo: str, session: str) -> dict:
    value = _read_metadata(repo) or {}
    if value.get("session") != session:
        value = {
            "version": 1,
            "repo": repo,
            "platform": "claude",
            "session": session,
            "started_at": _now(),
            "workspace_id": None,
            "pane_id": None,
            "state": "starting",
        }
    return value


def claim_holder(repo: str, metadata: dict) -> str | None:
    """The claim holder of one run: this machine, the repo, and the run branch.

    None when the run has no branch.
    """
    branch = metadata.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    return f"{socket.gethostname()}:{repo}:{branch}"


def _workspace_env(session: str, repo: str, holder: str | None = None) -> list[str]:
    """The shared server has no per-repo token, so give the repo's token to its workspace.

    The agent also gets the run's claim holder as LUPIN_CLAIM_HOLDER.
    """
    holder_env = ["--env", f"LUPIN_CLAIM_HOLDER={holder}"] if holder else []
    if session != SESSION_NAME:
        return holder_env
    path = _runtime_paths(repo).get("gh-token")
    if not path:
        return holder_env
    token = Path(path).read_text(encoding="utf-8").strip()
    return (["--env", f"GH_TOKEN={token}"] if token else []) + holder_env


def _create_workspace(session: str, directory: Path, repo: str, holder: str | None = None) -> dict:
    result = _herdr_json(
        session,
        "workspace", "create", "--cwd", str(directory), "--label", repo, "--no-focus",
        *_workspace_env(session, repo, holder),
    )
    workspace = result.get("workspace")
    root = result.get("root_pane")
    if not isinstance(workspace, dict) or not isinstance(root, dict):
        raise HerdrError("Herdr did not return a workspace and root pane")
    workspace_id = root.get("workspace_id") or workspace.get("id") or workspace.get("workspace_id")
    pane_id = root.get("pane_id")
    if not isinstance(workspace_id, str) or not isinstance(pane_id, str):
        raise HerdrError("Herdr workspace response did not include IDs")
    return {"id": workspace_id, "root_pane": root, "pane_id": pane_id}


def _find_workspace(session: str, metadata: dict) -> dict | None:
    return _workspace_by_id(session, metadata.get("workspace_id"))


def _record_workspace(repo: str, metadata: dict, workspace: dict) -> dict:
    value = dict(metadata)
    value["workspace_id"] = _workspace_id(workspace)
    value["pane_id"] = _pane_id(workspace)
    value.setdefault("started_at", _now())
    _set_loop_state(repo, value, "running")
    return value


def _wait_agent(session: str, workspace_id: str) -> int:
    while True:
        if not _server_running(session):
            return 1
        workspace = _workspace_by_id(session, workspace_id)
        if workspace is None:
            return 0
        if _agent_state(session, workspace) == "done":
            return 0
        time.sleep(2)


def _launch_agent(
    session: str, platform: str, workspace: dict, prompt_file: str, resume: bool,
    name: str = AGENT_NAME, provider: str | None = None, model: str | None = None,
) -> int:
    pane = _pane_id(workspace)
    workspace_id = _workspace_id(workspace)
    if not pane or not workspace_id:
        raise HerdrError("Herdr workspace has no pane or workspace ID")
    # Herdr rejects newlines in agent arguments, so send the prompt as one line.
    prompt = " ".join(Path(prompt_file).read_text(encoding="utf-8").split())
    argv = [
        HERDR, "--session", session, "agent", "start", name, "--kind", platform,
        "--pane", pane, "--timeout", "300000", "--",
    ]
    # Herdr passes arguments after `--` to the agent. It rejects this flag before `--`.
    if platform == "claude":
        argv.append("--dangerously-skip-permissions")
    if resume and platform == "claude":
        argv.append("--continue")
    if platform == "omp":
        if provider:
            argv.extend(["--provider", provider])
        if model:
            argv.extend(["--model", model])
    argv.append(prompt)
    rc, output = _run(argv, timeout=310.0, env=_herdr_env())
    if rc:
        try:
            kept = _keep_agent_after_timeout(session, pane, name, output)
        except LoopError as exc:
            raise HerdrError(f"{output}; could not keep the agent: {exc}") from exc
        if not kept:
            raise HerdrError(output or "Herdr could not start the agent")
    return _wait_agent(session, workspace_id)


def _keep_agent_after_timeout(session: str, pane: str, name: str, output: str) -> bool:
    """Give a started agent its name back when `agent start` times out.

    Herdr waits 5 minutes at most for the agent to be ready, then drops its
    name, and `_agent_for_workspace` needs it. An agent in the pane is alive,
    so watch it. Any other error, or no agent in the pane, is a failed start.
    """
    try:
        code = json.loads(output)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return False
    if code != "timeout" or not any(row.get("pane_id") == pane for row in _agents(session)):
        return False
    _herdr(session, "agent", "rename", pane, name)
    return True


def _wait_existing_agent(session: str, workspace_id: str) -> int:
    return _wait_agent(session, workspace_id)

def _agent_command(
    repo: str, session: str, workspace_id: str, platform: str, prompt_file: str,
    resume: bool, launch: bool, provider: str | None = None, model: str | None = None,
) -> list[str]:
    args = [
        "launch-agent" if launch else "wait-agent",
        "--repo", repo, "--platform", platform, "--session", session,
        "--workspace", workspace_id,
    ]
    if launch:
        args.extend(["--prompt-file", prompt_file])
        if provider:
            args.extend(["--provider", provider])
        if model:
            args.extend(["--model", model])
    if resume:
        args.append("--resume")
    return _lupin_command(*args)




def _run_worktree(repo: str, metadata: dict) -> Path:
    """Return the run's worktree. The agent never works in the main checkout."""
    value = metadata.get("worktree")
    if not isinstance(value, str) or not Path(value).is_dir():
        raise LoopError(f"the run for {repo} has no worktree; stop it and start again")
    return Path(value)


def _renew_claims(repo: str, holder: str, stop: threading.Event) -> None:
    """Renew the claims of `holder` each CLAIM_RENEW_INTERVAL until `stop` is set.

    An error does not stop the loop. The monitor log gets one line for each
    new error. The line does not contain the password.
    """
    reported = None
    while not stop.wait(CLAIM_RENEW_INTERVAL):
        what = f"renewing the claims of {holder}"
        try:
            claims.renew_holder_claims(holder, ttl=claims.DEFAULT_TTL, **_redis_kwargs())
        except (slots_redis.CoordinatorAuthFailed, redis.exceptions.NoPermissionError) as exc:
            line = slots_redis.refusal_message(exc, what, fleet=True)
        except slots.CoordinatorUnreachable:
            line = f"cannot reach the redis coordinator for {what}"
        except redis.exceptions.RedisError as exc:
            line = f"redis error for {what}: {exc}"
        else:
            reported = None
            continue
        if line != reported:
            _log_current_event(repo, "claim_renew_error", message=line)
            print(f"lupin: {line}", file=sys.stderr, flush=True)
            reported = line


def _monitor_loop(
    repo: str, platform: str, session: str, prompt_file: str, resume: bool
) -> int:
    server_was_running = _server_running(session)
    if not server_was_running:
        _log_current_event(repo, "herdr_server_start_requested", session=session)
    _ensure_server(repo, session, platform)
    _log_current_event(
        repo, "herdr_server_ready", session=session, started=not server_was_running,
    )
    metadata = _metadata_for_session(repo, session)
    workspace = _find_workspace(session, metadata)
    if workspace is None and metadata.get("state") in {"running", "needs_attention"}:
        _set_loop_state(repo, metadata, "needs_attention", reason="workspace_missing")
        raise LoopError(f"saved Herdr workspace for {repo} is missing; review it before starting again")
    launch = workspace is None
    holder = claim_holder(repo, metadata)
    if launch:
        workspace = _create_workspace(session, _run_worktree(repo, metadata), repo, holder)
        metadata = _record_workspace(repo, metadata, workspace)
    else:
        workspace_id = _workspace_id(workspace)
        if not workspace_id:
            raise HerdrError("saved Herdr workspace has no ID")
        if _agent_state(session, workspace) != "done" and _agent_for_workspace(session, workspace_id) is None:
            _set_loop_state(repo, metadata, "needs_attention", reason="agent_missing")
            raise LoopError(f"saved Herdr workspace for {repo} has no reported agent; stop it before starting again")
    workspace_id = _workspace_id(workspace)
    if not workspace_id:
        raise HerdrError("Herdr workspace has no ID")
    command = _agent_command(
        repo, session, workspace_id, platform, prompt_file, resume, launch,
        provider=metadata.get("provider"), model=metadata.get("model"),
    )
    max_holders = (
        _env_int("LUPIN_LOOP_CLAUDE_MAX_CONCURRENT", 1)
        if platform == "claude" else 1
    )
    wait_s = 0.0 if launch and platform == "claude" else 10800.0
    # Renew the run's claims until the workspace closes or the Herdr server
    # stops (issue #156). `hold` returns when the agent is `done`, but the
    # agent is still alive then and waits for input.
    stop_renewing = threading.Event()
    renewer = threading.Thread(
        target=_renew_claims, args=(repo, holder, stop_renewing), daemon=True
    )
    if holder:
        renewer.start()
    try:
        try:
            _log_event(
                repo, metadata, "slot_wait", slot=platform, scope="agent",
                wait_seconds=wait_s,
            )
            rc = slots.hold(
                command, slot=platform, holder=unit_name(repo), wait=wait_s,
                ttl=LEASE_TTL, max_holders=max_holders,
            )
            metadata["agent_exit_code"] = rc
            _write_metadata(repo, metadata)
            _log_event(
                repo, metadata, "slot_released", slot=platform, scope="agent",
                exit_code=rc,
            )
        except slots.SlotFull:
            _log_event(
                repo, metadata, "slot_wait_failed", slot=platform, scope="agent",
                reason="full",
            )
            if launch:
                _herdr_json(session, "workspace", "close", workspace_id)
                metadata["workspace_id"] = None
                metadata["pane_id"] = None
                removed_note = ""
                try:
                    _, removed_note = _remove_worktree(repo, metadata)
                except LoopError as exc:
                    removed_note = f"; {exc}"
                _set_loop_state(repo, metadata, "failed", reason="slot_full")
                raise LoopError(f"{platform} slot is full{removed_note}")
            raise LoopError(f"{platform} slot is full")
        if rc:
            if not _server_running(session):
                reason = "Herdr server stopped while the agent was running"
                _log_event(
                    repo, metadata, "herdr_server_stopped",
                    reason=reason, agent_exit_code=rc,
                )
                _set_loop_state(
                    repo, metadata, "needs_attention",
                    reason=reason, exit_code=rc,
                )
            else:
                _log_event(repo, metadata, "agent_exit", exit_code=rc)
                _set_loop_state(
                    repo, metadata, "needs_attention", reason="agent_exit", exit_code=rc,
                )
            raise LoopError(f"Herdr agent for {repo} stopped with status {rc}")
        while True:
            if not _server_running(session):
                _log_event(
                    repo, metadata, "herdr_server_stopped",
                    reason="Herdr server is not running",
                )
                _set_loop_state(
                    repo, metadata, "needs_attention",
                    reason="Herdr server is not running",
                )
                return 1
            if _workspace_by_id(session, workspace_id) is None:
                metadata["stopped_at"] = _now()
                _set_loop_state(repo, metadata, "stopped", reason="workspace_closed")
                return 0
            time.sleep(2)
    finally:
        stop_renewing.set()
        if holder:
            renewer.join(timeout=5)


def worker(
    repo: str, platform: str, session: str, prompt_file: str, resume: bool = False,
    recovering: bool = False,
) -> int:
    repo = validate_repo(repo)
    platform = validate_platform(platform)
    if not _owns_session(repo, session):
        raise LoopError("Herdr session name does not match repo")
    if not (CODE_DIR / base_repo(repo)).is_dir():
        raise LoopError(f"repo directory does not exist: {CODE_DIR / base_repo(repo)}")
    credential = _load_credential("redis-password")
    if credential:
        os.environ["LUPIN_REDIS_PASSWORD"] = credential
    kwargs = _redis_kwargs()
    metadata = _metadata_for_session(repo, session)
    metadata.update({"repo": repo, "platform": platform, "session": session})
    metadata["prompt_file"] = prompt_file
    if not recovering:
        _set_loop_state(repo, metadata, "starting")
    else:
        _write_metadata(repo, metadata)
    command = _lupin_command(
        "monitor", "--repo", repo, "--platform", platform, "--session", session,
        "--prompt-file", prompt_file,
    )
    if resume:
        command.append("--resume")
    _log_event(
        repo, metadata, "slot_wait", slot=f"loop-{base_repo(repo)}",
        scope="loop", wait_seconds=0,
    )
    try:
        rc = slots_redis.hold(
            command, slot=f"loop-{base_repo(repo)}", holder=unit_name(repo), wait=0.0,
            ttl=LEASE_TTL, max_holders=1, **kwargs,
        )
    except slots.SlotFull:
        _log_event(
            repo, metadata, "slot_wait_failed", slot=f"loop-{base_repo(repo)}",
            scope="loop", reason="full",
        )
        _set_loop_state(repo, metadata, "needs_attention", reason="loop_slot_full")
        raise LoopError(
            f"fleet loop slot loop-{base_repo(repo)} is full for {repo}; "
            "raise its stored max to run more loops on this repo"
        )
    _log_event(
        repo, metadata, "slot_released", slot=f"loop-{base_repo(repo)}",
        scope="loop", exit_code=rc,
    )
    if rc:
        latest = _read_metadata(repo)
        if latest and latest.get("state") == "starting":
            _set_loop_state(
                repo, latest, "failed", reason="monitor_exit", exit_code=rc,
            )
    _log_current_event(repo, "worker_exit", exit_code=rc)
    return rc


def monitor(repo: str, platform: str, session: str, prompt_file: str, resume: bool) -> int:
    _log_current_event(
        repo, "slot_acquired", slot=f"loop-{base_repo(repo)}", scope="loop",
    )
    try:
        return _monitor_loop(repo, platform, session, prompt_file, resume)
    except LoopError:
        raise
    except Exception as exc:
        raise LoopError(str(exc)) from exc

def herdr_server(session: str) -> int:
    if not re.fullmatch(r"lupin-[A-Za-z0-9._-]{1,52}", session):
        raise LoopError("invalid Lupin Herdr session name")
    credential = _load_credential("gh-token")
    env = os.environ.copy()
    if credential:
        env["GH_TOKEN"] = credential
    env.pop("HERDR_ENV", None)
    argv = [HERDR, "--session", session, "server"]
    try:
        return subprocess.run(argv, env=env, check=False).returncode
    except OSError as exc:
        raise LoopError(f"could not start Herdr server: {exc}") from exc


def launch_agent(
    repo: str, session: str, workspace_id: str, platform: str, prompt_file: str, resume: bool,
    provider: str | None = None, model: str | None = None,
) -> int:
    _log_current_event(repo, "slot_acquired", slot=platform, scope="agent")
    workspace = _workspace_by_id(session, workspace_id)
    if workspace is None:
        _log_current_event(
            repo, "agent_exit", exit_code=0, reason="workspace_missing",
        )
        return 0
    provider, model = validate_omp_options(platform, provider, model)
    rc = _launch_agent(
        session, platform, workspace, prompt_file, resume,
        _agent_name_for(repo, session), provider, model,
    )
    _log_current_event(repo, "agent_exit", exit_code=rc)
    return rc

def wait_agent(repo: str, session: str, workspace_id: str, platform: str) -> int:
    _log_current_event(repo, "slot_acquired", slot=platform, scope="agent")
    rc = _wait_existing_agent(session, workspace_id)
    _log_current_event(repo, "agent_exit", exit_code=rc)
    return rc


def _save_report(
    repo: str, session: str, workspace: dict, run_id: str | None = None
) -> Path:
    pane = _pane_id(workspace)
    if not pane:
        raise HerdrError("Herdr workspace has no pane ID")
    output = _herdr(session, "pane", "read", pane, "--source", "recent", "--lines", "100000", "--format", "text", timeout=PANE_READ_TIMEOUT)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    if run_log.valid_run_id(run_id):
        path = REPORTS_DIR / f"{validate_repo(repo)}-{run_id}.log"
    else:
        path = REPORTS_DIR / f"{validate_repo(repo)}-{_stamp()}-{time.time_ns() % 1000000:06d}.log"
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(output + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, path)
    return path


def _start_server_for_read(repo: str, metadata: dict) -> str:
    session = metadata.get("session") or session_name(repo)
    if not isinstance(session, str):
        raise LoopError("loop state has an invalid Herdr session")
    was_running = _server_running(session)
    if not was_running:
        _log_event(repo, metadata, "herdr_server_start_requested", session=session)
    _ensure_server(repo, session, metadata.get("platform", "claude"))
    _log_event(repo, metadata, "herdr_server_ready", session=session, started=not was_running)
    return session


def _request_handoff(
    repo: str, session: str, workspace: dict, grace: float
) -> tuple[str, str]:
    """Ask the loop's agent to run /handoff. Return a stop note and outcome."""
    workspace_id = _workspace_id(workspace)
    agent = _agent_for_workspace(session, workspace_id) if workspace_id else None
    if agent is None:
        return "", "no_agent"
    try:
        _herdr(
            session, "agent", "prompt", agent.get("name") or AGENT_NAME, HANDOFF_TEXT,
            "--wait", "--timeout", str(int(grace * 1000)),
            timeout=grace + HERDR_TIMEOUT,
        )
    except HerdrError as exc:
        if "timeout" in str(exc).lower():
            return f"; the agent did not finish its handoff in {int(grace)}s", "timeout"
        raise LoopError(
            f"could not ask the agent for {repo} to hand off: {exc}. "
            "Nothing was stopped. Add --force to stop it anyway."
        ) from exc
    return "", "completed"


def stop_loop(repo: str, *, force: bool = False, grace: float = HANDOFF_GRACE_S) -> str:
    repo = validate_repo(repo)
    lock = _lock_file(repo)
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        metadata = _read_metadata(repo)
        if not metadata:
            raise LoopError(f"no Lupin loop state for {repo}")
        _log_event(repo, metadata, "stop_requested", forced=force)
        session = _start_server_for_read(repo, metadata)
        workspace = _find_workspace(session, metadata)
        report = None
        handoff_note = ""
        handoff_outcome = "no_workspace"
        if workspace is not None:
            if force:
                handoff_outcome = "skipped"
            else:
                try:
                    handoff_note, handoff_outcome = _request_handoff(
                        repo, session, workspace, grace
                    )
                except LoopError as exc:
                    _log_event(
                        repo, metadata, "handoff_wait", outcome="error",
                        error_type=type(exc).__name__, message=str(exc),
                    )
                    raise
            _log_event(
                repo, metadata, "handoff_wait", outcome=handoff_outcome,
                grace_seconds=grace,
            )
            report = _save_report(repo, session, workspace, metadata.get("run_id"))
            _log_event(repo, metadata, "pane_report_saved", path=str(report))
            workspace_id = _workspace_id(workspace)
            if not workspace_id:
                raise HerdrError("Herdr workspace has no ID")
            _herdr_json(session, "workspace", "close", workspace_id)
        else:
            _log_event(
                repo, metadata, "handoff_wait", outcome=handoff_outcome,
                grace_seconds=grace,
            )
        _, state = _run(["systemctl", "is-active", f"{unit_name(repo)}.service"], timeout=SYSTEMCTL_CHECK_TIMEOUT)
        stop_exit_code = 0
        if state in {"active", "activating", "deactivating"}:
            rc, output = _run(
                _sudo_argv("systemctl", "stop", f"{unit_name(repo)}.service"),
                timeout=SYSTEMCTL_STOP_TIMEOUT,
            )
            if rc:
                _log_event(
                    repo, metadata, "worker_stop", outcome="failed",
                    exit_code=rc, message=output,
                )
                raise LoopError(output or f"could not stop Lupin worker for {repo}")
            stop_exit_code = rc
            _log_event(repo, metadata, "worker_stop", outcome="stopped", exit_code=rc)
        if session != SESSION_NAME and not _workspaces(session):
            _log_event(repo, metadata, "herdr_server_stop_requested", session=session)
            _herdr_json(None, "session", "stop", session, "--json")
            _log_event(repo, metadata, "herdr_server_stopped", session=session)
        _, worktree_note = _remove_worktree(repo, metadata)
        metadata["stopped_at"] = _now()
        metadata["workspace_id"] = None
        metadata["pane_id"] = None
        _set_loop_state(repo, metadata, "stopped", forced=force)
        _log_event(
            repo, metadata, "loop_stopped", path="force" if force else "graceful",
            forced=force, exit_code=stop_exit_code,
            agent_exit_code=metadata.get("agent_exit_code"),
            handoff_outcome=handoff_outcome,
        )
        message = (
            f"stopped {repo}"
            + (f"; report saved to {report}" if report else "")
            + handoff_note
            + worktree_note
        )
    finally:
        lock.close()
    _write_debrief(repo, metadata, forced=force)
    return message


def _write_debrief(repo: str, metadata: dict, *, forced: bool) -> None:
    """Write the debrief. The stop already happened, so a failure only prints a warning."""
    # Local import: debrief -> roadmap -> loop_runtime is a cycle.
    from . import debrief

    try:
        debrief.write_debrief(
            STATE_DIR, base_repo(repo), CODE_DIR / base_repo(repo), metadata.get("started_at"),
            forced=forced,
        )
    except Exception as exc:
        print(f"lupin loop: no debrief for {repo}: {exc}", file=sys.stderr)


def write_due_periodic_debriefs(now: datetime | None = None) -> None:
    """Write each periodic debrief that is due. A failure only prints a warning.

    The windows are on a fixed grid, so every machine sees the same window as due.
    Only one machine in the fleet writes it: the first to claim it in Redis. A machine
    that does not have the repo's checkout does not claim.
    """
    # Local import: debrief -> roadmap -> loop_runtime is a cycle.
    from . import debrief

    now = datetime.now(timezone.utc) if now is None else now
    try:
        repos = enabled_repos()
    except (LoopError, OSError) as exc:
        # enabled_repos writes the default file when it is missing. That write can raise OSError.
        print(f"lupin agent: no periodic debriefs: {exc} ({REPOS_FILE})", file=sys.stderr)
        return
    client = None  # Made when the first window is due. None means Redis is not available.
    client_tried = False
    machine = socket.gethostname()
    wrote = False
    for repo in repos:
        checkout = CODE_DIR / repo
        if not checkout.is_dir():
            continue
        for period in debrief.PERIODS:
            try:
                if not debrief.window_due(STATE_DIR, repo, period, now):
                    continue
                if not client_tried:
                    client_tried = True
                    try:
                        client = slots_redis.debrief_client(*debrief._redis_connection())
                    except (ValueError, OSError) as exc:  # A fleet config file that cannot be read.
                        print(f"lupin agent: debrief claims off: {exc}", file=sys.stderr)
                end = debrief.finished_window_end(period, now)
                if not debrief.claim_window(client, repo, period, end, machine):
                    continue  # Another machine writes this window.
                written = False
                try:
                    debrief.write_window(STATE_DIR, repo, checkout, period, now=now)
                    written = wrote = True
                finally:
                    debrief.finish_window(client, repo, period, end, machine, written=written)
            except Exception as exc:
                print(f"lupin agent: no {period} debrief for {repo}: {exc}", file=sys.stderr)
    if wrote:
        # The window is claimed for the whole fleet, so no other machine will write it. Put the text
        # in Redis now, so it is not lost if this machine goes away before the digest thread runs.
        # `limit=0`: no model call here. The digest thread writes the digest.
        try:
            from . import digest

            host, port, username, password = debrief._redis_connection()
            digest.sync(
                STATE_DIR,
                dict(redis_host=host, redis_port=port, redis_username=username, redis_password=password),
                limit=0, now=now, images=False,
            )
        except Exception as exc:
            print(f"lupin agent: could not publish the new debriefs: {exc}", file=sys.stderr)


def publish_debrief_digests() -> None:
    """Write a digest for each new debrief and put both in Redis. A failure only prints a warning."""
    # Local import: digest -> debrief -> roadmap -> loop_runtime is a cycle.
    from . import debrief, digest

    host, port, username, password = debrief._redis_connection()
    connection = dict(redis_host=host, redis_port=port, redis_username=username, redis_password=password)
    digest.sync(STATE_DIR, connection)


def peek_loop(repo: str, lines: int = 60) -> str:
    repo = validate_repo(repo)
    if not isinstance(lines, int) or lines < 1 or lines > 5000:
        raise LoopError("lines must be between 1 and 5000")
    metadata = _read_metadata(repo)
    session = metadata.get("session") if metadata else session_name(repo)
    if not metadata:
        agent = next(
            (
                row for row in _agents(session)
                if _agent_repo(row) == base_repo(repo)
                and isinstance(row.get("pane_id"), str)
                and row["pane_id"]
            ),
            None,
        )
        if agent is None:
            raise LoopError(f"no Lupin or Herdr agent found for {repo}")
        pane = agent["pane_id"]
        return _herdr(
            session, "pane", "read", pane, "--source", "recent",
            "--lines", str(lines), "--format", "text",
        )
    if not _server_running(session):
        raise LoopError(f"Herdr session for {repo} is not running")
    workspace = _find_workspace(session, metadata)
    if not workspace:
        raise LoopError(f"Herdr workspace for {repo} is not available")
    pane = _pane_id(workspace)
    if not pane:
        raise HerdrError("Herdr workspace has no pane ID")
    return _herdr(session, "pane", "read", pane, "--source", "recent", "--lines", str(lines), "--format", "text")


def send_loop(repo: str, text: str) -> None:
    """Type one line into the loop's pane and press Enter."""
    repo = validate_repo(repo)
    metadata = _read_metadata(repo)
    if not metadata:
        raise LoopError(f"no Lupin loop state for {repo}")
    session = metadata.get("session") or session_name(repo)
    if not _server_running(session):
        raise LoopError(f"Herdr session for {repo} is not running")
    workspace = _find_workspace(session, metadata)
    pane = _pane_id(workspace) if workspace else None
    if not pane:
        raise LoopError(f"Herdr pane for {repo} is not available")
    _herdr(session, "pane", "send-text", pane, text)
    _herdr(session, "pane", "send-keys", pane, "Enter")


def loop_state(repo: str) -> dict:
    repo = validate_repo(repo)
    metadata = _read_metadata(repo)
    if not metadata:
        return {"repo": repo, "backend": "herdr", "state": "stopped", "session": session_name(repo)}
    session = metadata.get("session") or session_name(repo)
    if metadata.get("state") in {"stopped", "failed"}:
        return {
            "repo": repo,
            "platform": metadata.get("platform", "claude"),
            "backend": "herdr",
            "state": metadata["state"],
            "session": session,
            "since": metadata.get("started_at"),
        }
    if not _server_running(session):
        return {
            "repo": repo, "backend": "herdr", "state": "unknown", "session": session,
            "since": metadata.get("started_at"), "error": "Herdr server is not running",
        }
    workspace = _find_workspace(session, metadata)
    if workspace is None:
        saved_state = metadata.get("state")
        if saved_state in {"running", "needs_attention"}:
            state = "needs_attention"
            _set_loop_state(repo, metadata, state, reason="workspace_missing")
        else:
            state = saved_state if saved_state == "starting" else "unknown"
        return {
            "repo": repo,
            "platform": metadata.get("platform", "claude"),
            "backend": "herdr",
            "state": state,
            "session": session,
            "workspace_id": metadata.get("workspace_id"),
            "pane_id": metadata.get("pane_id"),
            "since": metadata.get("started_at"),
        }
    workspace_id = _workspace_id(workspace)
    agent = _agent_for_workspace(session, workspace_id) if workspace_id else None
    return {
        "repo": repo,
        "platform": metadata.get("platform", "claude"),
        "backend": "herdr",
        "state": _agent_state(session, workspace),
        "session": session,
        "workspace_id": workspace_id,
        "pane_id": _pane_id(workspace),
        "since": metadata.get("started_at"),
        "agent": agent.get("name", AGENT_NAME) if agent else AGENT_NAME,
    }


def read_run_logs(repo: str, run_id: str | None = None, *, tail: int = 50) -> list[dict]:
    repo = validate_repo(repo)
    if run_id is None:
        metadata = _read_metadata(repo)
        run_id = metadata.get("run_id") if metadata else None
        if not run_log.valid_run_id(run_id):
            run_id = run_log.latest_run_id(STATE_DIR, repo)
    if not run_id:
        raise LoopError(f"no run logs for {repo}")
    try:
        return run_log.read_events(STATE_DIR, repo, run_id, tail=tail)
    except run_log.RunLogError as exc:
        raise LoopError(str(exc)) from exc


def _agent_repo(agent: dict) -> str | None:
    cwd = agent.get("cwd")
    if not isinstance(cwd, str):
        return None
    path = Path(cwd)
    try:
        relative = path.relative_to(CODE_DIR)
    except ValueError:
        try:
            remote = subprocess.run(
                ["git", "-C", cwd, "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if remote.returncode != 0 or not remote.stdout.strip():
            return None
        remote_url = remote.stdout.strip()
        remote_path = remote_url.rsplit(":", 1)[-1] if "://" not in remote_url else remote_url
        repo = Path(remote_path).name.removesuffix(".git")
    else:
        if not relative.parts:
            return None
        repo = relative.parts[0]
    try:
        return base_repo(validate_repo(repo))
    except LoopError:
        return None


def _reported_agent_state(status: object) -> str:
    if not isinstance(status, str):
        return "unknown"
    if status in {"idle", "working"}:
        return "running"
    if status == "blocked":
        return "needs_attention"
    if status == "done":
        return "done"
    return "unknown"


def local_loops() -> list[dict]:
    result = []
    if LOOPS_DIR.is_dir():
        for path in sorted(LOOPS_DIR.glob("*.json")):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            repo = metadata.get("repo")
            session = metadata.get("session")
            if (
                metadata.get("state") not in {"running", "starting", "needs_attention"}
                or not isinstance(repo, str)
                or not isinstance(session, str)
            ):
                continue
            try:
                state = loop_state(repo)
            except LoopError:
                state = {"repo": repo, "backend": "herdr", "state": "unknown", "session": session}
            if state.get("state") == "stopped":
                continue
            result.append({
                "repo": repo,
                "platform": metadata.get("platform", "claude"),
                "state": state.get("state", "unknown"),
                "since": metadata.get("started_at"),
                "backend": "herdr",
                "session": session,
                "workspace_id": state.get("workspace_id"),
                "pane_id": state.get("pane_id"),
            })

    try:
        agents = _agents(SESSION_NAME)
    except LoopError:
        agents = []
    groups: dict[str, list[dict]] = {}
    for agent in agents:
        repo = _agent_repo(agent)
        if repo:
            groups.setdefault(repo, []).append(agent)

    for repo, agents_for_repo in groups.items():
        panes = [
            {
                "workspace_id": agent.get("workspace_id"),
                "pane_id": agent.get("pane_id"),
                "agent_status": agent.get("agent_status", "unknown"),
                "agent": agent.get("agent"),
            }
            for agent in agents_for_repo
        ]
        state = max(
            (_reported_agent_state(agent.get("agent_status")) for agent in agents_for_repo),
            key=lambda item: {"needs_attention": 3, "running": 2, "done": 1, "unknown": 0}[item],
        )
        existing_rows = [row for row in result if loop_repo(row["repo"]) == repo]
        if not existing_rows:
            first = agents_for_repo[0]
            result.append({
                "repo": repo,
                "platform": first.get("agent"),
                "state": state,
                "since": first.get("started_at"),
                "backend": "herdr",
                "session": SESSION_NAME,
                "workspace_id": first.get("workspace_id"),
                "pane_id": first.get("pane_id"),
                "panes": panes,
            })
        else:
            for existing in existing_rows:
                existing["panes"] = panes
                if state != "unknown":
                    existing["state"] = state
    return result

def attach_argv(repo: str, machine: str, local_host: str, ssh_target: str | None) -> list[str]:
    repo = validate_repo(repo)
    metadata = _read_metadata(repo)
    session = metadata.get("session") if metadata else session_name(repo)
    if machine == local_host:
        return [HERDR, "--session", session]
    if not ssh_target:
        raise LoopError(f"no ssh target is configured for {machine!r}")
    return [HERDR, "--remote", ssh_target, "--session", session]


def recover() -> list[str]:
    recovered = []
    if not LOOPS_DIR.is_dir():
        return recovered
    for path in sorted(LOOPS_DIR.glob("*.json")):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            repo = validate_repo(metadata.get("repo"))
            session = metadata.get("session")
            platform = validate_platform(metadata.get("platform", "claude"))
            prompt_file = metadata.get("prompt_file")
        except (OSError, json.JSONDecodeError, LoopError):
            continue
        if metadata.get("state") not in {"running", "starting"}:
            continue
        if (
            not _owns_session(repo, session)
            or not isinstance(prompt_file, str)
            or not Path(prompt_file).is_file()
        ):
            continue
        rc, _ = _run(["systemctl", "is-active", "--quiet", f"{unit_name(repo)}.service"], timeout=3.0)
        if rc == 0:
            continue
        command = _lupin_command(
            "worker", "--repo", repo, "--platform", platform, "--session", session,
            "--prompt-file", prompt_file, "--recover",
        )
        if metadata.get("resume"):
            command.append("--resume")
        password_source = _password_source()
        creds = {"redis-password": password_source} if password_source else {}
        rc, output = _systemd_run(
            repo, "loop", command, credentials=creds, claude_limits=platform == "claude"
        )
        recovered.append(f"{repo}: {'restarted monitor' if rc == 0 else output}")
    return recovered


def _schedule_text(value: str, label: str, maximum: int = 64) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise LoopError(f"invalid {label}")
    return value


def _event_time(when: str) -> tuple[str, bool]:
    _schedule_text(when, "schedule time")
    if when.startswith("+"):
        span = when[1:]
        rc, output = _run(["systemd-analyze", "timespan", span], timeout=5.0)
        if rc:
            raise LoopError(f"invalid relative time span: {output}")
        return span, True
    rc, output = _run(["systemd-analyze", "calendar", when], timeout=5.0)
    if rc:
        raise LoopError(f"invalid calendar time: {output}")
    return when, False


def schedule_once(
    when: str,
    repos: list[str],
    platform: str | None = None,
    note: str | None = None,
    resume: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    if note is not None and (
        not isinstance(note, str) or len(note) > MAX_NOTE_CHARS or "\x00" in note
    ):
        raise LoopError("note is invalid or too long")
    selected = validate_platform(platform) if platform else None
    provider, model = validate_omp_options(selected, provider, model) if selected else (provider, model)
    if (provider is not None or model is not None) and selected != "omp":
        raise LoopError("--provider and --model require --platform omp")
    repos = [validate_plain_repo(repo) for repo in repos]
    if not repos:
        raise LoopError("at least one repo is required for a one-off run")
    when_value, relative = _event_time(when)
    user = _loop_user()
    identifier = hashlib.sha256(f"{time.time_ns()}:{os.getpid()}:{repos}".encode()).hexdigest()[:16]
    entry = {
        "id": identifier,
        "repos": repos,
        "platform": selected,
        "provider": provider,
        "model": model,
        "note": note,
        "resume": bool(resume),
        "when": when,
        "created_at": _now(),
    }
    directory = STATE_DIR / "once"
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = directory / f"{identifier}.json"
    path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o640)
    unit = f"lupin-once-{identifier}"
    command = _lupin_command("once-fire", "--id", identifier)
    group = os.environ.get("LUPIN_LOOP_GROUP", "users")
    argv = _sudo_argv(
        "systemd-run", f"--unit={unit}", "--collect",
        f"--uid={user}", f"--gid={group}", "--timer-property=AccuracySec=1s",
    )
    if relative:
        argv.append(f"--on-active={when_value}")
    else:
        argv.append(f"--on-calendar={when_value}")
    argv.extend(_service_environment())
    argv.extend(["--", *command])
    rc, output = _run(argv, timeout=HERDR_TIMEOUT)
    if rc:
        path.unlink(missing_ok=True)
        raise LoopError(output or "could not schedule one-off loop")
    return f"scheduled {', '.join(repos)} for {when}"


def once_fire(identifier: str) -> list[str]:
    if not re.fullmatch(r"[a-f0-9]{16}", identifier):
        raise LoopError("invalid one-off schedule ID")
    path = STATE_DIR / "once" / f"{identifier}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoopError(f"could not read one-off schedule: {exc}") from exc
    results = []
    for repo in entry.get("repos", []):
        try:
            ok, message = start_loop(
                repo,
                platform=entry.get("platform"),
                provider=entry.get("provider"),
                model=entry.get("model"),
                note=entry.get("note"),
                resume=entry.get("resume", False),
            )
        except LoopError as exc:
            ok = False
            message = f"could not start loop for {repo}: {exc}"
        results.append(message)
        if not ok:
            print(message, file=sys.stderr)
    path.unlink(missing_ok=True)
    return results


_TIMER_TRIGGER_RESET = (
    "OnCalendar=\n"
    "OnActiveSec=\n"
    "OnBootSec=\n"
    "OnStartupSec=\n"
    "OnUnitActiveSec=\n"
    "OnUnitInactiveSec=\n"
)


def schedule_local(args: list[str]) -> str:
    """Show or change the recurring systemd timer."""
    if not args or args == ["show"]:
        rc, output = _run(["systemctl", "list-timers", "delegation-loop.timer", "--no-pager"], timeout=10.0)
        if rc:
            raise LoopError(output or "could not read delegation-loop.timer")
        return output
    if args == ["pause"] or args == ["resume"]:
        action = "stop" if args[0] == "pause" else "start"
        rc, output = _run(_sudo_argv("systemctl", action, "delegation-loop.timer"), timeout=20.0)
        if rc:
            raise LoopError(output or f"could not {args[0]} delegation-loop.timer")
        return f"{args[0]}d delegation-loop.timer"
    state_dir = STATE_DIR
    dropin = state_dir / "timer-dropin.d" / "override.conf"
    if len(args) == 4 and args[0] == "first" and args[2] == "every":
        when, relative = _event_time(args[1])
        interval = _schedule_text(args[3], "timer interval")
        rc, output = _run(["systemd-analyze", "timespan", interval], timeout=5.0)
        if rc:
            raise LoopError(f"invalid timer interval: {output}")
        first = "OnActiveSec" if relative else "OnCalendar"
        content = (
            f"[Timer]\n{_TIMER_TRIGGER_RESET}"
            f"{first}={when}\nOnUnitActiveSec={interval}\nAccuracySec=1s\n"
        )
    elif len(args) == 2 and args[0] == "cal":
        expression = _schedule_text(args[1], "calendar expression", 256)
        rc, output = _run(["systemd-analyze", "calendar", expression], timeout=5.0)
        if rc:
            raise LoopError(f"invalid calendar expression: {output}")
        content = (
            f"[Timer]\n{_TIMER_TRIGGER_RESET}"
            f"OnCalendar={expression}\nAccuracySec=1s\n"
        )
    else:
        raise LoopError("usage: lupin schedule [show|first <when> every <interval>|cal <expression>]")
    dropin.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    dropin.write_text(content, encoding="utf-8")
    rc, output = _run(_sudo_argv("systemctl", "daemon-reload"), timeout=20.0)
    if rc:
        raise LoopError(output or "systemd daemon-reload failed")
    rc, output = _run(
        _sudo_argv("systemctl", "restart", "delegation-loop.timer"), timeout=20.0
    )
    if rc:
        raise LoopError(output or "could not restart delegation-loop.timer")
    return "updated delegation-loop.timer"


def status_rows(repos: list[str] | None = None) -> list[dict]:
    enabled = enabled_repos()
    values = {row["repo"]: row for row in local_loops()}
    selected = repos if repos is not None else list(dict.fromkeys([*enabled, *values]))
    return [
        values.get(repo, {
            "repo": repo,
            "platform": enabled.get(repo, "claude"),
            "state": "stopped",
            "backend": "herdr",
            "session": session_name(repo),
            "since": None,
        })
        for repo in selected
    ]


def _print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True))


def _log_action_error(args: Any, exc: Exception) -> None:
    repo = None
    if getattr(args, "action", None) in {"worker", "monitor", "launch-agent", "wait-agent"}:
        repo = getattr(args, "repo", None)
    elif (
        getattr(args, "action", None) == "local-action"
        and getattr(args, "local_action", None) == "stop"
        and getattr(args, "action_args", None)
    ):
        repo = args.action_args[0]
    if isinstance(repo, str):
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        _log_current_event(
            repo, "error", error_type=type(cause).__name__, message=str(cause),
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="lupin loop", add_help=True)
    sub = parser.add_subparsers(dest="action", required=True)
    worker_parser = sub.add_parser("worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--repo", required=True)
    worker_parser.add_argument("--platform", required=True)
    worker_parser.add_argument("--session", required=True)
    worker_parser.add_argument("--prompt-file", required=True)
    worker_parser.add_argument("--resume", action="store_true")
    worker_parser.add_argument("--recover", action="store_true")
    server_parser = sub.add_parser("herdr-server", help=argparse.SUPPRESS)
    server_parser.add_argument("--session", required=True)
    monitor_parser = sub.add_parser("monitor", help=argparse.SUPPRESS)
    monitor_parser.add_argument("--repo", required=True)
    monitor_parser.add_argument("--platform", required=True)
    monitor_parser.add_argument("--session", required=True)
    monitor_parser.add_argument("--prompt-file", required=True)
    monitor_parser.add_argument("--resume", action="store_true")
    launch_parser = sub.add_parser("launch-agent", help=argparse.SUPPRESS)
    launch_parser.add_argument("--repo", required=True)
    launch_parser.add_argument("--session", required=True)
    launch_parser.add_argument("--workspace", required=True)
    launch_parser.add_argument("--platform", required=True)
    launch_parser.add_argument("--prompt-file", required=True)
    launch_parser.add_argument("--provider", choices=("openai", "openai-codex", "opencode-go"))
    launch_parser.add_argument("--model")
    launch_parser.add_argument("--resume", action="store_true")
    wait_parser = sub.add_parser("wait-agent", help=argparse.SUPPRESS)
    wait_parser.add_argument("--repo", required=True)
    wait_parser.add_argument("--platform", required=True)
    wait_parser.add_argument("--session", required=True)
    wait_parser.add_argument("--workspace", required=True)
    once_parser = sub.add_parser("once-fire", help=argparse.SUPPRESS)
    once_parser.add_argument("--id", required=True)
    local_parser = sub.add_parser("local-action", help=argparse.SUPPRESS)
    local_parser.add_argument("local_action", choices=("stop", "peek", "send", "state", "schedule", "pause", "resume"))
    local_parser.add_argument("action_args", nargs=argparse.REMAINDER)
    sub.add_parser("recover", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "worker":
            return worker(
                args.repo, args.platform, args.session, args.prompt_file, args.resume, args.recover
            )
        if args.action == "herdr-server":
            return herdr_server(args.session)
        if args.action == "monitor":
            return monitor(args.repo, args.platform, args.session, args.prompt_file, args.resume)
        if args.action == "launch-agent":
            return launch_agent(
                args.repo,
                args.session,
                args.workspace,
                args.platform,
                args.prompt_file,
                args.resume,
                args.provider,
                args.model,
            )
        if args.action == "wait-agent":
            return wait_agent(args.repo, args.session, args.workspace, args.platform)
        if args.action == "once-fire":
            for item in once_fire(args.id):
                print(item)
            return 0
        if args.action == "recover":
            for item in recover():
                print(item)
            return 0
        if args.action == "local-action":
            if args.local_action == "stop":
                print(stop_loop(args.action_args[0], force="--force" in args.action_args[1:]))
            elif args.local_action == "peek":
                lines = int(args.action_args[1]) if len(args.action_args) > 1 else 60
                print(peek_loop(args.action_args[0], lines))
            elif args.local_action == "send":
                send_loop(args.action_args[0], args.action_args[1])
                print(f"sent to {args.action_args[0]}")
            elif args.local_action == "state":
                _print_json(loop_state(args.action_args[0]))
            elif args.local_action == "schedule":
                print(schedule_local(args.action_args))
            else:
                print(schedule_local([args.local_action]))
            return 0
    except (LoopError, slots.SlotFull, slots.CoordinatorUnreachable, slots_redis.CoordinatorUnreachable) as exc:
        _log_action_error(args, exc)
        print(f"lupin loop: {exc}", file=sys.stderr)
        return 2
    except (IndexError, ValueError) as exc:
        _log_action_error(args, exc)
        print(f"lupin loop: invalid arguments: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        _log_action_error(args, exc)
        raise
    print(f"lupin loop: no handler for action {args.action}", file=sys.stderr)
    return 1

"""Written debrief for one loop stop or one period.

A debrief is one markdown file. A loop stop writes one. The agent writes
periodic debriefs (`PERIODS`) on a timer. Facts come from GitHub (`gh`).
The ledger lists shared repository events. A claim marks an issue that one loop
is working on. A normal stop and standard periodic debriefs also read the
ledger and issue claims in Redis. A forced stop does not. Files stay on the
machine that wrote them.
The standard periodic debriefs use fixed windows (`window_end`). One machine
in the fleet writes each window (`claim_window`) and captures two dashboard
screenshots. The daily owner-blocked report shares the 24-hour window but uses
its own claim and file. It does not capture screenshots. A stop debrief does
not capture screenshots.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import redis

from . import claims, ledger, machines, roadmap, slots_redis
from .slots import CoordinatorUnreachable

GH_TIMEOUT = 30.0
DEBRIEF_TIME_LIMIT_S = 10.0  # Time limit for all gh calls in one stop debrief. Redis reads do not count.
# A periodic window is written in the background and nobody waits for it, so it may take longer.
# With 10 seconds, jesus ran out of time before it read the closed issues, and the digest said little.
PERIODIC_TIME_LIMIT_S = 120.0
LIST_LIMIT = "200"
COMMIT_PAGE = 100  # The most commits one GitHub API page gives.
COMMIT_PAGES_MAX = 50  # Stop after this many pages (5000 commits). More is an error, not a cut list.
PR_ISSUE_LIMIT = 1000  # `active_days` list size. A full list may be cut, so it is an error.
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
STAMP_FORMAT = "%Y%m%d-%H%M%S"
PERIODS = {
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
# Periodic windows end on a fixed grid of this clock (issue #125). Every machine computes the
# same window for the same repo, so one machine can write it for the whole fleet.
CLOCK = ZoneInfo("America/Los_Angeles")
_HOUR_STEPS = {"6h": 6, "12h": 12, "24h": 24}
WINDOW_GRACE_S = 180  # A window is written this long after it ends. A fast clock or a late GitHub search must not cut it.
WINDOW_WRITING_S = 3600  # A claim not finished in this time is free again. The writer may have crashed.
WINDOW_DONE_S = 8 * 24 * 60 * 60  # Longer than the longest window (7d), so a late machine does not repeat it.
# A backfill file covers one UTC day. Its name ends in `-day.md`.
# The daily owner-blocked report uses the 24-hour window, but has its own file.
DAY = "day"
OWNER_BLOCKED_PERIOD = "owner-blocked"
_PERIOD_NAMES = "|".join(map(re.escape, [*PERIODS, DAY, OWNER_BLOCKED_PERIOD]))
FILE_RE = re.compile(rf"^\d{{8}}-\d{{6}}(?:-(?:{_PERIOD_NAMES}))?\.md$")
# Screenshot links in a periodic debrief. `_screenshots` writes this shape only.
SHOT_PATH_RE = re.compile(
    rf"\d{{8}}-\d{{6}}(?:-(?:{_PERIOD_NAMES}))?-screenshots/[A-Za-z0-9_-]+\.png"
)
# Image link: a GitHub attachment URL or a screenshot path. Group 2 is the target.
IMAGE_LINK_RE = re.compile(rf"!\[([^\]]*)\]\((https://[^)\s]+|{SHOT_PATH_RE.pattern})\)")
GITHUB_ITEM_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"
    r"/(?:issues|pull)/\d+(?![A-Za-z0-9._/-])"
)
EVIDENCE_DIRS = ("docs/", "evidence/")
EVIDENCE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
FAILING_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"}
FAILING_STATES = {"FAILURE", "ERROR"}
CSS = ".debrief img{max-width:100%;height:auto}"
SHOT_BROWSER = "chromium"  # Headless browser command. It must be on PATH.
SHOT_WINDOW = "1200,1000"
SHOT_TIMEOUT_S = 60.0
SHOT_SERVER_WAIT_S = 10.0
# (file name, alt text, caption, dashboard path). `{query}` is the debrief's query string.
SHOTS = (
    ("debrief-list.png", "Debrief list",
     "The list of debriefs on the dashboard. Newest first.", "/debrief"),
    ("debrief.png", "Debrief page",
     "One periodic debrief, as the dashboard shows it.", "/debrief?{query}"),
)


class DebriefError(Exception):
    """A debrief cannot be written or read."""


class GhTimeout(DebriefError):
    """A gh call hit its time limit."""


def _gh(args: list[str], cwd: str | None = None, timeout: float = GH_TIMEOUT):
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=cwd, capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhTimeout(f"gh {' '.join(args[:2])} failed: {exc}") from exc
    except OSError as exc:
        raise DebriefError(f"gh {' '.join(args[:2])} failed: {exc}") from exc
    if proc.returncode != 0:
        raise DebriefError(
            f"gh {' '.join(args[:2])} exited {proc.returncode}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise DebriefError(f"gh {' '.join(args[:2])} sent bad JSON") from exc


class _TimeLimit:
    """Time left for the gh calls of one debrief. Redis reads do not use it."""

    def __init__(self, seconds: float):
        self.left = seconds

    def gh(self, args: list[str], cwd: str | None = None):
        """Run gh with the time left. Raise `GhTimeout` when none is left."""
        if self.left <= 0:
            raise GhTimeout(f"gh {' '.join(args[:2])} not run: time limit reached")
        started = time.monotonic()
        try:
            return _gh(args, cwd=cwd, timeout=min(GH_TIMEOUT, self.left))
        finally:
            self.left -= time.monotonic() - started


def _list(time_limit: _TimeLimit, args: list[str]) -> tuple[list[dict] | None, str]:
    """Return (items, reason). Items is None when the call failed. Reason says why."""
    try:
        return time_limit.gh(args + ["--limit", LIST_LIMIT]) or [], ""
    except GhTimeout:
        return None, "time limit reached"
    except DebriefError:
        return None, "gh error"


def _cut(section: str, name: str, items: list | None, reason: str) -> list[str]:
    if items is None:
        return [f"- Not collected: {reason}. {section}: {name} list."]
    if len(items) < int(LIST_LIMIT):
        return []
    return [f"- {section}: {name} list cut at {LIST_LIMIT} items. Some items may be missing."]


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        # Years 1 and 9999 can leave the datetime range when shifted to UTC.
        return None


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_time(when: datetime) -> str:
    """The UTC time and the Los Angeles time, for the Window section.

    The digest spec asks for the Los Angeles clock. The UTC value stays in
    the file, so the record is unambiguous.
    """
    local = when.astimezone(CLOCK)
    return f"{_iso(when)} ({local.strftime('%Y-%m-%d %H:%M %Z')})"


def _in_window(value, start: datetime, end: datetime, *, closed_end: bool = True) -> bool:
    """Return True when `value` is in `[start, end]`, or in `[start, end)` if `closed_end` is False."""
    when = _parse(value)
    return when is not None and start <= when and (when <= end if closed_end else when < end)


def _well_formed(event: dict) -> bool:
    """Return False when a field the debrief reads has the wrong type."""
    for key in ("next", "decisions"):
        texts = event.get(key, [])
        if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
            return False
    return True


def _failing_checks(pr: dict) -> list[str]:
    names = []
    for check in pr.get("statusCheckRollup") or []:
        if check.get("conclusion") in FAILING_CONCLUSIONS or check.get("state") in FAILING_STATES:
            names.append(check.get("name") or check.get("context") or "unnamed check")
    return names


def _attachments(text: str | None) -> list[str]:
    found = []
    for match in roadmap.IMAGE.finditer(text or ""):
        attachment = roadmap.github_attachment_id(match.group(2))
        if attachment and attachment not in found:
            found.append(attachment)
    return found


def build_markdown(
    full_name: str,
    start: datetime,
    end: datetime,
    *,
    forced: bool = False,
    events: list[dict] | None = None,
    ledger_note: str | None = None,
    claimed: set[int] | None = None,
    time_limit: _TimeLimit | None = None,
    period: str | None = None,
    past_day: bool = False,
) -> str:
    """Return the debrief for `full_name` over `[start, end]` as markdown.

    `past_day` is set for a backfill file. Its window is `[start, end)`. GitHub
    shows open PRs, labels and claims for today only, so those sections say
    "not collected". The Shipped section also lists commits.

    `events` is None when the ledger was not read. `claimed` is None when
    claims were not read. A forced stop uses GitHub facts only. A section
    whose gh call failed or timed out is marked as not collected. `period` is set for a
    periodic debrief. It replaces the Stop line.
    """
    day = start.strftime("%Y-%m-%d")
    repo = ["--repo", full_name]
    time_limit = _TimeLimit(DEBRIEF_TIME_LIMIT_S) if time_limit is None else time_limit

    def in_window(value) -> bool:
        return _in_window(value, start, end, closed_end=not past_day)

    # GitHub search dates are inclusive. A past day gets an upper bound too.
    last_day = (end - timedelta(seconds=1)).strftime("%Y-%m-%d")
    span = f"{day}..{last_day}" if past_day else f">={day}"
    window_events = (
        [] if forced or events is None
        else [e for e in events if _well_formed(e) and in_window(e.get("timestamp"))]
    )
    lines = [f"# Debrief: {full_name}", "", "## Window", f"- From: {_window_time(start)}", f"- To: {_window_time(end)}"]
    if period is None:
        lines.append("- Stop: forced, no handoff" if forced else "- Stop: normal")
    else:
        lines.append(f"- Period: {period}")
    lines.append("")

    merged_all, merged_reason = _list(time_limit, ["pr", "list", *repo, "--state", "merged",
                                "--search", f"merged:{span}",
                                "--json", "number,title,mergedAt,mergeCommit"])
    merged = [pr for pr in merged_all or [] if in_window(pr.get("mergedAt"))]
    closed_all, closed_reason = _list(time_limit, ["issue", "list", *repo, "--state", "closed",
                                "--search", f"closed:{span}",
                                "--json", "number,title,closedAt"])
    closed = [issue for issue in closed_all or [] if in_window(issue.get("closedAt"))]
    commits, commits_reason = (
        _day_commits(time_limit, full_name, start, end, {(pr.get("mergeCommit") or {}).get("oid") for pr in merged})
        if past_day else ([], "")
    )
    lines.append("## Shipped")
    for pr in merged:
        commit = (pr.get("mergeCommit") or {}).get("oid", "")[:7]
        lines.append(f"- PR #{pr['number']}: {pr['title']} (merged {pr['mergedAt']}, commit {commit})")
    for issue in closed:
        lines.append(f"- Issue #{issue['number']}: {issue['title']} (closed {issue['closedAt']})")
    for sha, subject, when in commits or []:
        lines.append(f"- Commit {sha[:7]}: {subject} ({when})")
    if (not merged and not closed and not commits and merged_all is not None
            and closed_all is not None and commits is not None):
        lines.append("- None.")
    if commits is None:
        lines.append(f"- Not collected: {commits_reason}. Shipped: commit list.")
    lines += _cut("Shipped", "merged PR", merged_all, merged_reason)
    lines += _cut("Shipped", "closed issue", closed_all, closed_reason)
    lines.append("")

    lines.append("## Follow-up tasks")
    if forced:
        lines.append("- Forced stop. Ledger not read.")
    elif events is None:
        lines.append(f"- {ledger_note or 'No ledger read.'}")
    else:
        for event in window_events:
            for text in event.get("next", []):
                prefix = f"#{event['issue']}: " if event.get("issue") else ""
                lines.append(f"- {prefix}{text}")
        if not any(event.get("next") for event in window_events):
            lines.append("- None.")
    lines.append("")

    failing = []
    if past_day:
        # GitHub keeps no history of check results or labels. Do not show today's.
        open_prs, open_reason = None, "past day, GitHub shows today only"
        blocked, blocked_reason = None, open_reason
    else:
        open_prs, open_reason = _list(time_limit, ["pr", "list", *repo, "--state", "open",
                                  "--json", "number,title,statusCheckRollup"])
        blocked, blocked_reason = _list(time_limit, ["issue", "list", *repo, "--state", "open",
                                                     "--label", "blocked", "--json", "number,title"])
    for pr in open_prs or []:
        names = _failing_checks(pr)
        if names:
            failing.append((pr, names))
    decisions = [text for event in window_events for text in event.get("decisions", [])]
    risk = []
    for pr, names in failing:
        risk.append(f"- PR #{pr['number']}: {pr['title']} (failing: {', '.join(names)})")
    for issue in blocked or []:
        risk.append(f"- Issue #{issue['number']}: {issue['title']} (labelled blocked)")
    for text in decisions:
        risk.append(f"- Decision: {text}")
    decisions_read = not forced and events is not None
    lines.append("## Risk")
    lines.append("Derived from GitHub facts and ledger decisions. Not checked against real ledger rows.")
    lines += risk or (
        ["- None."] if decisions_read and open_prs is not None and blocked is not None else []
    )
    if not decisions_read:
        why = "forced stop" if forced else (ledger_note or "no ledger read")
        lines.append(f"- Decisions not collected: {why}.")
    lines += _cut("Risk", "open PR", open_prs, open_reason)
    lines += _cut("Risk", "blocked issue", blocked, blocked_reason)
    lines.append("")

    if past_day:
        ready, ready_reason = None, "past day, GitHub shows today only"
    else:
        ready, ready_reason = _list(time_limit, ["issue", "list", *repo, "--state", "open",
                                                 "--label", "ready", "--json", "number,title"])
    lines.append("## Opportunities")
    if forced:
        lines.append("Forced stop. Claims not read.")
    elif past_day:
        lines.append("Past day. Claims are for today only, so they are not read.")
    elif claimed is None:
        lines.append("Claims not read. Listed issues may already be claimed.")
    unclaimed = [issue for issue in ready or [] if not claimed or issue["number"] not in claimed]
    for issue in unclaimed:
        lines.append(f"- Issue #{issue['number']}: {issue['title']}")
    if not unclaimed and ready is not None:
        lines.append("- None.")
    lines += _cut("Opportunities", "ready issue", ready, ready_reason)
    lines.append("")

    lines.append("## Evidence")
    seen = []
    cut = []
    complete = True
    for kind, args in (
        ("Issue", ["issue", "list", *repo, "--state", "all",
                   "--search", f"updated:>={day}",
                   "--json", "number,title,body,comments,updatedAt"]),
        ("PR", ["pr", "list", *repo, "--state", "all",
                "--search", f"updated:>={day}",
                "--json", "number,title,body,comments,updatedAt"]),
    ):
        items, reason = _list(time_limit, args)
        cut += _cut("Evidence", kind, items, reason)
        complete = complete and items is not None
        for item in items or []:
            texts = []
            if in_window(item.get("updatedAt")):
                texts.append(item.get("body"))
            texts += [c.get("body") for c in item.get("comments") or []
                      if in_window(c.get("createdAt"))]
            for text in texts:
                for attachment in _attachments(text):
                    if attachment in seen:
                        continue
                    seen.append(attachment)
                    url = f"https://github.com/user-attachments/assets/{attachment}"
                    lines.append(f"- {kind} #{item['number']}: ![{kind} {item['number']} image]({url})")
    if not seen and complete:
        lines.append("- None.")
    lines += cut
    lines.append("")
    return "\n".join(lines)


def write_debrief(
    root: Path,
    repo: str,
    checkout: Path,
    started_at: str | None,
    *,
    forced: bool = False,
) -> Path:
    """Write one debrief under `root/debriefs/<repo>/` and return its path.

    Raises `DebriefError` or a Redis error. Nothing is written then. The gh
    calls share one time limit. If a gh call fails or times out, the file marks
    that section as not collected.
    """
    if not REPO_RE.fullmatch(repo or ""):
        raise DebriefError(f"invalid repo name {repo!r}")
    start = _parse(started_at)
    if start is None:
        raise DebriefError(f"loop state for {repo} has no start time")
    if not checkout.is_dir():
        raise DebriefError(f"no checkout at {checkout}")
    return _write(root, repo, checkout, start, datetime.now(timezone.utc), forced=forced)


def _write(
    root: Path,
    repo: str,
    checkout: Path,
    start: datetime,
    end: datetime,
    *,
    forced: bool,
    period: str | None = None,
    past_day: bool = False,
) -> Path:
    """Build the debrief for `[start, end]` and write it. Return its path.

    Raises `DebriefError` or a Redis error. Nothing is written then.
    The gh calls share one time limit. If a section's gh call fails or
    times out, the file marks that section as not collected. `period` adds the
    period to the file name. A periodic file also gets screenshots.
    `past_day` writes a backfill file: window `[start, end)`, no claims, no
    screenshots, name `<end>-day.md`.
    """
    time_limit = _TimeLimit(DEBRIEF_TIME_LIMIT_S if period is None else PERIODIC_TIME_LIMIT_S)
    view = time_limit.gh(["repo", "view", "--json", "nameWithOwner"], cwd=str(checkout))
    full_name = view["nameWithOwner"]

    events = None
    claimed = None
    ledger_note = None
    if not forced:
        # Both Redis reads use one debrief client. Each Redis read uses the
        # debrief client timeouts (DEBRIEF_TIMEOUT_S).
        client = None
        try:
            client = slots_redis.debrief_client(*_redis_connection())
        except (ValueError, OSError) as exc:  # A fleet config file that cannot be read or parsed.
            ledger_note = f"Ledger unavailable: {exc}"
        try:
            if client is not None:
                events = ledger.read_events(full_name, limit=None, client=client)
        except (CoordinatorUnreachable, redis.exceptions.RedisError, ValueError, KeyError) as exc:
            ledger_note = f"Ledger unavailable: {exc.__cause__ or exc}"
        if client is not None and not past_day:  # Claims show today only.
            try:
                held = claims.claims_for([full_name], client=client)
                claimed = {
                    int(key.rsplit("#", 1)[1]) for key in held if key.startswith(f"{full_name}#")
                }
            except (CoordinatorUnreachable, redis.exceptions.RedisError, ValueError):
                claimed = None

    text = build_markdown(
        full_name, start, end, forced=forced, events=events,
        ledger_note=ledger_note, claimed=claimed, time_limit=time_limit,
        period=DAY if past_day else period, past_day=past_day,
    )
    folder = root / "debriefs" / repo
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    if past_day:
        path = folder / f"{end.strftime(STAMP_FORMAT)}-{DAY}.md"
        _store(path, text)
        return path
    if period is None:
        path = _claim(folder, end)
        try:
            _store(path, text)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    path = folder / f"{end.strftime(STAMP_FORMAT)}-{period}.md"
    _store(path, text)
    # The screenshots show the debrief page. That page must exist first.
    _store(path, text + _screenshots(root, repo, path))
    return path


def _redis_connection() -> tuple[str | None, int | None, str | None, str | None]:
    """Return (host, port, username, password) for the debrief's Redis client.

    This is the agent's own setting: `LUPIN_REDIS_HOST`, `LUPIN_REDIS_PORT` and
    `LUPIN_REDIS_USERNAME` from the environment, then the `lupin join` config.
    The password comes from the systemd credential `redis-password`, or from
    `LUPIN_REDIS_PASSWORD` (`machines.resolve_connection`). It is never logged.
    """
    port = os.environ.get("LUPIN_REDIS_PORT")
    conn = machines.resolve_connection(
        redis_host=os.environ.get("LUPIN_REDIS_HOST"),
        redis_port=int(port) if port and port.isdigit() else None,
        redis_username=os.environ.get("LUPIN_REDIS_USERNAME"),
        config_path=os.environ.get("LUPIN_FLEET_CONFIG"),
    )
    return conn["redis_host"], conn["redis_port"], conn["redis_username"], conn["redis_password"]


def _day_commits(
    time_limit: _TimeLimit, full_name: str, start: datetime, end: datetime, skip: set[str],
) -> tuple[list[tuple[str, str, str]] | None, str]:
    """Return ([(sha, subject, date)], reason) for the default branch commits in `[start, end)`.

    Commits in `skip` (merge commits of the listed PRs) are left out.
    The list is None when a call failed. It reads every page, so the list is complete.
    A short page ends the list. More than `COMMIT_PAGES_MAX` pages is a failure.
    """
    found: list = []
    for page in range(1, COMMIT_PAGES_MAX + 2):
        query = urlencode({
            "since": _iso(start), "until": _iso(end), "per_page": COMMIT_PAGE, "page": page,
        })
        try:
            items = time_limit.gh(["api", f"repos/{full_name}/commits?{query}"]) or []
        except GhTimeout:
            return None, "time limit reached"
        except DebriefError:
            return None, "gh error"
        if page > COMMIT_PAGES_MAX:
            if items:
                return None, f"more than {COMMIT_PAGES_MAX * COMMIT_PAGE} commits"
            break
        found += items
        if len(items) < COMMIT_PAGE:
            break
    commits = []
    for item in found:
        sha = item.get("sha") or ""
        info = item.get("commit") or {}
        when = (info.get("committer") or {}).get("date") or ""
        # `until` includes its own second. Drop a commit at exactly `end`.
        if sha in skip or not _in_window(when, start, end, closed_end=False):
            continue
        commits.append((sha, (info.get("message") or "").splitlines()[0] if info.get("message") else "", when))
    return commits, ""


def _claim(folder: Path, end: datetime) -> Path:
    """Reserve the file for a stop debrief. If its second is taken, use the next one.

    Two loops of one repo can stop in the same second. The second write must
    not replace the first.
    """
    for offset in range(60):
        stamp = (end + timedelta(seconds=offset)).strftime(STAMP_FORMAT)
        path = folder / f"{stamp}.md"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640))
        except FileExistsError:
            continue
        return path
    raise DebriefError(f"no free debrief file name in {folder} after {end:%Y-%m-%d %H:%M:%S}")


def _store(path: Path, text: str) -> None:
    """Write `text` to `path` in one step."""
    fd, temporary = tempfile.mkstemp(prefix=".debrief.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_env(root: Path) -> dict[str, str]:
    """Return the environment for the dashboard process.

    `sys.executable` can be a bare interpreter that cannot import `lupin`.
    The nix wrapper sets `sys.path` in this process only. `PYTHONPATH` copies
    that path to the child, so it finds `lupin` and its libraries too.
    """
    paths = [p for p in sys.path if p]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    return {**os.environ, "LUPIN_LOOP_STATE_DIR": str(root), "PYTHONPATH": os.pathsep.join(paths)}


def _serve(root: Path, port: int) -> subprocess.Popen:
    """Start the dashboard on `port`, reading debriefs from `root`. Return once it answers."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "lupin.serve", "--port", str(port)],
        env=_serve_env(root),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + SHOT_SERVER_WAIT_S
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    proc.wait()
    raise DebriefError("dashboard server did not start")


def _shot(url: str, target: Path) -> str | None:
    """Save a screenshot of `url` at `target`. Return None, or the reason it failed."""
    argv = [
        SHOT_BROWSER, "--headless", "--disable-gpu", "--hide-scrollbars",
        f"--window-size={SHOT_WINDOW}", f"--screenshot={target}", url,
    ]
    try:
        # A new session gives the browser its own process group.
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        return f"browser did not run ({exc.strerror})"
    try:
        code = proc.wait(timeout=SHOT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Kill the browser's child processes too.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return "browser timed out"
    if code or not target.is_file() or target.stat().st_size == 0:
        return f"browser exited {code} without a screenshot"
    return None


def _page_error(url: str) -> str | None:
    """Return None when `url` answers 200. Otherwise return the reason."""
    try:
        with urllib.request.urlopen(url, timeout=SHOT_SERVER_WAIT_S) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except OSError as exc:
        return f"dashboard did not answer ({exc})"
    return None if status == 200 else f"dashboard returned HTTP {status}"


def _capture(root: Path, repo: str, name: str, folder: Path) -> dict[str, str]:
    """Save the dashboard screenshots in `folder`. Return {file name: reason} for each one missing."""
    if shutil.which(SHOT_BROWSER) is None:
        return {file: f"browser command {SHOT_BROWSER!r} not found on PATH" for file, *_ in SHOTS}
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    port = _free_port()
    try:
        server = _serve(root, port)
    except (DebriefError, OSError) as exc:
        return {file: str(exc) for file, *_ in SHOTS}
    query = urlencode({"repo": repo, "file": name})
    missing = {}
    try:
        for file, _alt, _caption, path in SHOTS:
            url = f"http://127.0.0.1:{port}{path.format(query=query)}"
            # A page that is not 200 gives no screenshot of the debrief.
            reason = _page_error(url) or _shot(url, folder / file)
            if reason:
                missing[file] = reason
    finally:
        server.kill()
        server.wait()
    return missing


def _screenshots(root: Path, repo: str, path: Path) -> str:
    """Save the screenshots for the periodic debrief at `path`. Return its markdown section.

    A screenshot that is not saved is named in the section, with its reason.
    """
    stem = path.stem
    try:
        missing = _capture(root, repo, path.name, path.parent / f"{stem}-screenshots")
    except OSError as exc:
        missing = {file: f"could not save files ({exc})" for file, *_ in SHOTS}
    lines = ["", "## Screenshots"]
    for file, alt, caption, _path in SHOTS:
        if file in missing:
            lines.append(f"- Not captured: {alt} ({file}). Reason: {missing[file]}.")
        else:
            lines += ["", f"![{alt}]({stem}-screenshots/{file})", "", caption]
    return "\n".join(lines) + "\n"


def window_end(period: str, now: datetime) -> datetime:
    """Return the newest window end that is not after `now`, in UTC.

    Windows end on a fixed grid of the Los Angeles clock: every 6 hours (00, 06, 12, 18),
    every 12 hours (00, 12), every day (00), and every Monday at 00 for `7d`.
    """
    if period not in PERIODS:
        raise DebriefError(f"unknown period {period!r}")
    local = now.astimezone(CLOCK)
    if period == "7d":
        monday = local.date() - timedelta(days=local.weekday())
        boundary = datetime(monday.year, monday.month, monday.day, tzinfo=CLOCK)
    else:
        step = _HOUR_STEPS[period]
        boundary = local.replace(hour=local.hour // step * step, minute=0, second=0, microsecond=0, fold=0)
    return boundary.astimezone(timezone.utc)


def window_start(period: str, end: datetime) -> datetime:
    """Return the start of the window that ends at `end`, in UTC.

    It steps back on the wall clock, so a day with a clock change is 23 or 25 hours long.
    """
    if period not in PERIODS:
        raise DebriefError(f"unknown period {period!r}")
    back = timedelta(days=7) if period == "7d" else timedelta(hours=_HOUR_STEPS[period])
    wall = end.astimezone(CLOCK).replace(tzinfo=None) - back
    return wall.replace(tzinfo=CLOCK).astimezone(timezone.utc)


def finished_window_end(period: str, now: datetime) -> datetime:
    """The end of the newest window that has been over for `WINDOW_GRACE_S`.

    The grace keeps a machine with a clock a few minutes fast from writing a window too early.
    The first claim wins for the whole fleet, so an early write would cut the window for everyone.
    Run NTP on every machine. A larger clock error is not covered.
    """
    return window_end(period, now - timedelta(seconds=WINDOW_GRACE_S))


def window_due(root: Path, repo: str, period: str, now: datetime) -> bool:
    """Return True when this machine has no debrief for the newest finished window of `period`."""
    if not REPO_RE.fullmatch(repo or ""):
        raise DebriefError(f"invalid repo name {repo!r}")
    end = finished_window_end(period, now)
    return not (root / "debriefs" / repo / f"{end.strftime(STAMP_FORMAT)}-{period}.md").exists()


def _window_key(repo: str, period: str, end: datetime) -> str:
    return f"{slots_redis.PREFIX}debrief-window:{repo}:{period}:{end.strftime(STAMP_FORMAT)}"


def claim_window(client, repo: str, period: str, end: datetime, machine: str) -> bool:
    """Return True if this machine may write the window. Only one machine in the fleet gets True.

    The claim is a Redis key set with NX. A machine can take back its own unfinished claim.
    If Redis cannot be read, return True: a duplicate
    debrief is better than a missing one. Call `finish_window` when the write ends.
    """
    if client is None:
        return True
    key = _window_key(repo, period, end)
    mine = f"{machine}:writing"
    try:
        if slots_redis._call_with_retry(lambda: client.set(key, mine, nx=True, ex=WINDOW_WRITING_S)):
            return True
        # This machine's own claim, left by a restart or a retried SET, is still this machine's.
        return slots_redis._call_with_retry(lambda: client.get(key)) == mine
    except redis.exceptions.RedisError:
        return True


def finish_window(client, repo: str, period: str, end: datetime, machine: str, *, written: bool) -> None:
    """Mark the claim done after a write, or free it after a failed write. A Redis error is ignored."""
    if client is None:
        return
    key = _window_key(repo, period, end)
    try:
        if written:
            slots_redis._call_with_retry(lambda: client.set(key, f"{machine}:done", ex=WINDOW_DONE_S))
        else:
            slots_redis._call_with_retry(lambda: client.delete(key))
    except redis.exceptions.RedisError:
        pass


def write_window(
    root: Path, repo: str, checkout: Path, period: str, *, now: datetime | None = None,
) -> Path:
    """Write the debrief for the newest finished window of `period` and return its path.

    The window is `[window_start, window_end]` on the fixed grid. The file name is the window
    end, so every machine gives the same window the same name. Screenshots are added.
    """
    when = datetime.now(timezone.utc) if now is None else now
    end = finished_window_end(period, when)
    start = window_start(period, end)
    if not checkout.is_dir():
        raise DebriefError(f"no checkout at {checkout}")
    return _write(root, repo, checkout, start, end, forced=False, period=period)


def owner_blocked_window_due(root: Path, repo: str, now: datetime) -> bool:
    """Return True when this machine has no owner-blocked report for today's LA window."""
    if not REPO_RE.fullmatch(repo or ""):
        raise DebriefError(f"invalid repo name {repo!r}")
    end = finished_window_end("24h", now)
    name = f"{end.strftime(STAMP_FORMAT)}-{OWNER_BLOCKED_PERIOD}.md"
    return not (root / "debriefs" / repo / name).exists()


def write_owner_blocked_window(
    root: Path, repo: str, checkout: Path, *, now: datetime | None = None,
) -> Path:
    """Write the daily owner-blocked report for the newest finished LA day."""
    if not REPO_RE.fullmatch(repo or ""):
        raise DebriefError(f"invalid repo name {repo!r}")
    when = datetime.now(timezone.utc) if now is None else now
    end = finished_window_end("24h", when)
    start = window_start("24h", end)
    if not checkout.is_dir():
        raise DebriefError(f"no checkout at {checkout}")
    view = _TimeLimit(PERIODIC_TIME_LIMIT_S).gh(
        ["repo", "view", "--json", "nameWithOwner"], cwd=str(checkout)
    )
    full_name = view.get("nameWithOwner") if isinstance(view, dict) else None
    if not isinstance(full_name, str) or not full_name:
        raise DebriefError("gh did not name the repository")

    from . import owner_blocked

    host, port, username, password = _redis_connection()
    text = owner_blocked.collect_markdown(
        str(checkout),
        full_name,
        _window_time(start),
        _window_time(end),
        connection={
            "redis_host": host,
            "redis_port": port,
            "redis_username": username,
            "redis_password": password,
        },
    )
    folder = root / "debriefs" / repo
    folder.mkdir(parents=True, exist_ok=True, mode=0o750)
    path = folder / f"{end.strftime(STAMP_FORMAT)}-{OWNER_BLOCKED_PERIOD}.md"
    _store(path, text)
    return path


def write_day(root: Path, repo: str, checkout: Path, day: datetime) -> Path:
    """Write the backfill debrief for one UTC day and return its path.

    `day` must be midnight UTC. The window is `[day, day + 1 day)`. The file
    name is `<day + 1 day>-day.md`. `window_due` ignores such a file.
    The day must be over. Today is refused.
    """
    if day.tzinfo is None or day.utcoffset() != timedelta(0) or day != day.replace(
        hour=0, minute=0, second=0, microsecond=0
    ):
        raise DebriefError(f"{day!r} is not midnight UTC")
    if day + timedelta(days=1) > datetime.now(timezone.utc):
        raise DebriefError(f"{day:%Y-%m-%d} is not over yet")
    if not REPO_RE.fullmatch(repo or ""):
        raise DebriefError(f"invalid repo name {repo!r}")
    if not checkout.is_dir():
        raise DebriefError(f"no checkout at {checkout}")
    return _write(
        root, repo, checkout, day, day + timedelta(days=1), forced=False, past_day=True,
    )


def active_days(checkout: Path, first: datetime, last: datetime) -> list[datetime]:
    """Return the UTC days in `[first, last + 1 day)` with a merged PR, a closed issue or a commit.

    `first` and `last` are midnight UTC. Raises `DebriefError` if a gh call fails.
    """
    time_limit = _TimeLimit(DEBRIEF_TIME_LIMIT_S * 3)
    view = time_limit.gh(["repo", "view", "--json", "nameWithOwner"], cwd=str(checkout))
    full_name = view["nameWithOwner"]
    end = last + timedelta(days=1)
    span = f"{first:%Y-%m-%d}..{last:%Y-%m-%d}"
    repo = ["--repo", full_name]
    days = set()
    for args, key in (
        (["pr", "list", *repo, "--state", "merged", "--search", f"merged:{span}",
          "--json", "mergedAt", "--limit", str(PR_ISSUE_LIMIT + 1)], "mergedAt"),
        (["issue", "list", *repo, "--state", "closed", "--search", f"closed:{span}",
          "--json", "closedAt", "--limit", str(PR_ISSUE_LIMIT + 1)], "closedAt"),
    ):
        items = time_limit.gh(args) or []
        if len(items) > PR_ISSUE_LIMIT:
            raise DebriefError(f"{args[0]} list cut at {PR_ISSUE_LIMIT} items for {span}")
        for item in items:
            when = _parse(item.get(key))
            if when is not None and first <= when < end:
                days.add(when.replace(hour=0, minute=0, second=0, microsecond=0))
    commits, reason = _day_commits(time_limit, full_name, first, end, set())
    if commits is None:
        raise DebriefError(f"commit list not collected: {reason}")
    for _sha, _subject, when in commits:
        parsed = _parse(when)
        if parsed is not None:
            days.add(parsed.replace(hour=0, minute=0, second=0, microsecond=0))
    return sorted(days)


def _newest_first(item: tuple[str, str]) -> tuple[datetime, timedelta, str]:
    """Sort key: file name time, then period length. A stop file has no period, so it counts as zero."""
    repo, name = item
    try:
        when = datetime.strptime(name[:15], STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        when = datetime.min.replace(tzinfo=timezone.utc)  # Not a real date. Sort it last.
    return when, PERIODS.get(name[16:-3], timedelta(0)), repo


def list_debriefs(root: Path) -> list[tuple[str, str]]:
    """Return (repo, file name) for each debrief, newest first."""
    base = root / "debriefs"
    if not base.is_dir():
        return []
    found = []
    for folder in base.iterdir():
        if not REPO_RE.fullmatch(folder.name) or not folder.is_dir():
            continue
        for path in folder.iterdir():
            if FILE_RE.fullmatch(path.name) and path.is_file():
                found.append((folder.name, path.name))
    return sorted(found, key=_newest_first, reverse=True)


def read_debrief(root: Path, repo: str, name: str) -> str:
    """Return the markdown of one debrief. Raises `DebriefError` if absent."""
    if not REPO_RE.fullmatch(repo or "") or not FILE_RE.fullmatch(name or ""):
        raise DebriefError("no such debrief")
    path = root / "debriefs" / repo / name
    if not path.is_file():
        raise DebriefError("no such debrief")
    return path.read_text(encoding="utf-8")


def _image_src(target: str, repo: str) -> str | None:
    """Return the `src` for an image link. None when the link is not served."""
    if target.startswith("https://"):
        attachment = roadmap.github_attachment_id(target)
        return f"/image?id={attachment}" if attachment else None
    return "/debrief/shot?" + escape(urlencode({"repo": repo, "path": target}), quote=True)


def _escaped_text(text: str) -> str:
    escaped = escape(text)
    return GITHUB_ITEM_URL_RE.sub(
        lambda match: f"<a href='{match.group(0)}'>{match.group(0)}</a>",
        escaped,
    )


def _inline(text: str, repo: str) -> str:
    parts = []
    position = 0
    for match in IMAGE_LINK_RE.finditer(text):
        parts.append(_escaped_text(text[position:match.start()]))
        src = _image_src(match.group(2), repo)
        if src:
            alt = escape(match.group(1), quote=True)
            parts.append(f"<img src='{src}' alt='{alt}' loading=lazy>")
        else:
            parts.append(_escaped_text(match.group(0)))
        position = match.end()
    parts.append(_escaped_text(text[position:]))
    return "".join(parts)


def render_html(markdown: str, repo: str) -> str:
    """Render the debrief markdown subset as HTML.

    Headings, bullets, paragraphs, images, and GitHub issue and PR URLs.
    Images are GitHub attachments or this repo's screenshots. Other links
    and all text are escaped.
    """
    out = []
    in_list = False
    for line in markdown.splitlines():
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:], repo)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:], repo)}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:], repo)}</h2>")
        elif line.strip():
            out.append(f"<p>{_inline(line, repo)}</p>")
    if in_list:
        out.append("</ul>")
    return "".join(out)


def read_screenshot(root: Path, repo: str, rel: str) -> tuple[bytes, str] | None:
    """Return (bytes, content type) for a screenshot of `repo`'s debriefs.

    `rel` must match `SHOT_PATH_RE`. Return None when it does not match,
    when the file is absent, or when it resolves outside the repo's folder.
    """
    if not REPO_RE.fullmatch(repo or "") or not SHOT_PATH_RE.fullmatch(rel or ""):
        return None
    folder = (root / "debriefs" / repo).resolve()
    try:
        target = (folder / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(folder) or not target.is_file():
        return None
    if target.stat().st_size > MAX_EVIDENCE_BYTES:
        return None
    return target.read_bytes(), "image/png"


def read_evidence(checkout: Path, rel: str) -> tuple[bytes, str] | None:
    """Return (bytes, content type) for an image under `docs/` or `evidence/`.

    Returns None for an absolute path, a `://` or `..` path, or another extension.
    Also None when the path is outside those folders or resolves outside the checkout.
    """
    if not rel or rel.startswith("/") or "://" in rel or ".." in rel or "\\" in rel or "\0" in rel:
        return None
    content_type = EVIDENCE_TYPES.get(Path(rel).suffix.lower())
    if content_type is None or not rel.startswith(EVIDENCE_DIRS):
        return None
    try:
        root = checkout.resolve(strict=True)
        target = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(root):
        return None
    if not target.relative_to(root).as_posix().startswith(EVIDENCE_DIRS) or not target.is_file():
        return None
    if target.stat().st_size > MAX_EVIDENCE_BYTES:
        return None
    return target.read_bytes(), content_type

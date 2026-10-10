"""Run a loop action on the machine that owns the loop.

If the target is this machine, this module runs the given Lupin command.
If the target is another machine, it sends a signed command on the Redis
queue from `commands.py`. That machine's `lupin agent` runs the command.

The CLI and dashboard use this module for local and remote actions.

`attach` does not use this module. It runs `herdr --session <session>` on
this machine, or `herdr --remote <target> --session <session>` for a remote
host. Herdr uses SSH for the remote connection. See `ssh_target_for` below
for the file that maps a machine name to its SSH target.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Callable

from . import commands, machines

SSH_TARGETS_PATH = Path.home() / ".config" / "lupin" / "ssh-targets"

# A command's terminal states. Same set as `commands.py`'s `STATES`, minus
# "queued" and "running" -- those two mean the command is still in flight,
# not finished.
_TERMINAL_STATES = {"ok", "failed", "rejected", "expired"}


class AmbiguousMachine(Exception):
    """Raised when a repo's loop does not resolve to exactly one machine
    -- zero matches, or more than one. `cli.py` turns this into exit code
    5 ("use --machine")."""

    def __init__(self, repo: str, candidates: list[str]):
        self.repo = repo
        self.candidates = candidates
        super().__init__(f"{repo!r} is running on {len(candidates)} machine(s) {candidates!r} -- use --machine")


class MissingSigningKey(Exception):
    """Raised when an action targets another machine, but no signing key
    was given. `commands.enqueue` needs a signing key for every remote
    target."""

    def __init__(self, machine: str):
        self.machine = machine
        super().__init__(f"no signing key given to reach {machine!r}")


def resolve_machine_for_repo(repo: str, connection: dict) -> str:
    """Find the one machine whose heartbeat says it is running `repo`'s
    loop right now.

    This reads `machines.py`'s heartbeat `loops` field (issue #2 phase
    A). That field is the single source of truth here -- this function
    does not guess.

    Raises `AmbiguousMachine` if zero machines match, or more than one
    does. Raises `machines.CoordinatorUnreachable` if the machine
    registry itself cannot be reached -- that is a different problem
    from "no match found", so this function does not swallow it.
    """
    records = machines.machines(connection, strict=True)
    candidates = [
        record["name"]
        for record in records
        if any(loop.get("repo") == repo for loop in record.get("loops", []))
    ]
    if len(candidates) != 1:
        raise AmbiguousMachine(repo, candidates)
    return candidates[0]


def run_subprocess(argv: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run `argv` as a real subprocess. Never go through a shell.

    `serve.py`'s `run()` calls this too, with its own default timeout, so
    there is one copy of this subprocess-running code, not two that can
    drift apart.
    """
    try:
        proc = subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    out = proc.stdout
    if proc.stderr:
        out = out + ("\n" if out and not out.endswith("\n") else "") + proc.stderr
    return proc.returncode, out


def dispatch_loop_action(
    *,
    machine: str,
    local_host: str,
    local_argv: list[str],
    queue_action: str,
    queue_params: dict,
    connection: dict,
    signing_key: str | None = None,
    actor: str = "lupin",
    issuer: str | None = None,
    wait_s: float | None = None,
    run_local: Callable[[list[str]], tuple[int, str]] | None = None,
) -> dict:
    """Run one loop-control action on `machine`.

    If `machine` is this machine (`local_host`), this runs `local_argv`
    directly. It uses `run_local` if given, or `run_subprocess` otherwise.
    It returns `{"mode": "local", "returncode": int, "output": str}`.

    Otherwise, this sends `queue_action`/`queue_params` on the Redis
    queue, for `machine`'s `lupin agent` to run. It returns
    `{"mode": "queued", "id": str, "result": dict | None}`.

    `result` is `None` in two cases: `wait_s` was not given at all (this
    is fire-and-forget -- `serve.py`'s dashboard does not want to block
    an HTTP reply on a remote machine), or `wait_s` was given but no
    terminal state (`ok`/`failed`/`rejected`/`expired`) showed up before
    the deadline. `cli.py`'s own commands treat that second case as
    "sent, but the result is unknown" -- exit code 4.

    Raises `MissingSigningKey` if `machine` is not `local_host` and no
    `signing_key` was given. Raises `commands.enqueue`'s own
    `CoordinatorUnreachable` if Redis cannot be reached.
    """
    if machine == local_host:
        runner = run_local or run_subprocess
        returncode, output = runner(local_argv)
        return {"mode": "local", "returncode": returncode, "output": output}

    if not signing_key:
        raise MissingSigningKey(machine)

    cmd_id = commands.enqueue(
        machine, queue_action, queue_params,
        key=signing_key, actor=actor, issuer=issuer or local_host,
        **connection,
    )
    result = None
    if wait_s is not None and wait_s > 0:
        deadline = time.monotonic() + wait_s
        while True:
            result = commands.get_status(cmd_id, **connection)
            if result and result.get("state") in _TERMINAL_STATES:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    return {"mode": "queued", "id": cmd_id, "result": result}


def dispatch_fleet_runs(
    repos: list[str],
    records: list[dict],
    *,
    local_host: str,
    signing_keys: dict[str, str],
    connection: dict,
) -> list[dict]:
    """Queue each enabled repo on an online worker that has its checkout."""
    workers = [
        record
        for record in records
        if record.get("name") != local_host
        and record.get("state") == "online"
        and "loop.run" in record.get("actions", [])
        and record.get("name") in signing_keys
    ]
    queued = []
    assigned: dict[str, int] = {}
    for repo in sorted(repos):
        candidates = [
            worker
            for worker in workers
            if any(
                entry.get("repo") == repo and entry.get("loopable") is True
                for entry in worker.get("repos", [])
            )
            and not any(
                entry.get("repo") == repo
                for entry in worker.get("loops", [])
            )
        ]
        if not candidates:
            queued.append({
                "repo": repo,
                "queued": False,
                "message": "no idle online worker has this repo",
            })
            continue
        candidates.sort(
            key=lambda worker: (
                assigned.get(worker["name"], 0),
                -sum(
                    int(slot.get("max", 0)) - int(slot.get("used", 0))
                    for slot in (worker.get("slots") or {}).values()
                ),
                worker["name"],
            )
        )
        worker = candidates[0]
        params = {"repo": repo}
        command_id = commands.enqueue(
            worker["name"],
            "loop.run",
            params,
            key=signing_keys[worker["name"]],
            actor="lupin-fleet-scheduler",
            issuer=local_host,
            **connection,
        )
        assigned[worker["name"]] = assigned.get(worker["name"], 0) + 1
        queued.append({
            "repo": repo,
            "machine": worker["name"],
            "queued": True,
            "id": command_id,
        })
    return queued


def ssh_target_for(machine: str, path: Path | str | None = None) -> str | None:
    """Look up `machine`'s ssh target -- a `user@host` string, or an
    ssh_config alias.

    Reads it from a plain text file, one line per machine:
    `<machine> <target>`. Blank lines and `#` comments are skipped.
    Returns `None` if the file is missing, or has no line for `machine`.

    Default path: `~/.config/lupin/ssh-targets`. This file lives on the
    local machine (in practice, Nix writes it on each machine). It is not
    fleet state read from Redis. `attach` needs this mapping before it
    can even open a connection to the other machine, so the mapping
    cannot come from the far side of a connection that does not exist
    yet.
    """
    p = Path(path) if path else SSH_TARGETS_PATH
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] == machine:
            return parts[1]
    return None

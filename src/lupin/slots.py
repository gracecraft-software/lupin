"""Slot leases: a fixed number of holders per named slot, backed by files.

New code for issue #205 (part of #198's plan). A *slot* is a resource with a
fixed number of concurrent holders (bmo has 1, say). A *lease* is a hold on
a slot; it expires on its own after a TTL unless renewed. This module is the
`local` backend: one directory per slot under a state root, one file per
active holder (PID + a monotonic deadline), locked with `fcntl.flock` so two
processes on the same machine never race on the same slot.

A later sub-issue (#206, not this one) adds a `redis` backend for leases
visible across machines. This module deliberately knows nothing about any
specific caller (no `loop-claude-*` units, no `ghostbook.nix` lock files) --
just "a slot has a name and a max holder count."

Judgment call -- a slot's max: nothing local knows a slot's intended max
without being told. The first `acquire` call for a slot writes its max (from
`--max`, default 1 if omitted) into `<slot>/config.json`; every later
`acquire` for that slot reads the stored value and ignores `--max`. `status`
reads the same file. See BRIEF.md / the job report for the alternatives
considered.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import time
from pathlib import Path
from typing import Callable

from . import _lease_runtime


class SlotFull(Exception):
    """Raised by `acquire` when the slot stays at its max past `wait`."""


class CoordinatorUnreachable(Exception):
    """Reserved for a future backend (e.g. `redis`) that can lose its
    coordinator. The `local` backend's coordinator is the filesystem, which
    this module always reaches once the state root is writable, so it never
    raises this -- it exists so callers (and the CLI's exit-code mapping)
    don't change shape when a networked backend is added later.
    """


DEFAULT_TTL = 60.0


def _resolve_root(state_root: str | Path | None) -> Path:
    if state_root is not None:
        return Path(state_root).expanduser()
    env = os.environ.get("LUPIN_STATE_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".lupin" / "slots"


def _holder_path(slot_dir: Path, token: str) -> Path:
    return slot_dir / f"{token}.holder"


def _read_holder(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, path)


def _get_or_set_max(slot_dir: Path, max_holders: int | None) -> int:
    """Read `config.json`'s max, creating it from `max_holders` if this is
    the slot's first call. A later call's `--max` is ignored once the slot
    has a config -- the max is fixed at creation, not renegotiated per call.
    """
    config_path = slot_dir / "config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as handle:
            return json.load(handle)["max"]
    chosen = max_holders if max_holders is not None else 1
    _write_json_atomic(config_path, {"max": chosen})
    return chosen


def _prune_locked(slot_dir: Path) -> list[Path]:
    """Remove holder files past their deadline; return the survivors.

    Caller must hold the slot's lock.
    """
    now = time.monotonic()
    survivors = []
    for path in slot_dir.glob("*.holder"):
        data = _read_holder(path)
        if data is None or data.get("deadline", 0) < now:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        survivors.append(path)
    return survivors


def _with_slot_lock(slot_dir: Path):
    slot_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(slot_dir / ".lock", "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def acquire(
    slot: str,
    holder: str,
    *,
    wait: float = 0.0,
    ttl: float = DEFAULT_TTL,
    max_holders: int | None = None,
    state_root: str | Path | None = None,
    cancel_wait: Callable[[], bool] | None = None,
) -> str | None:
    """Acquire a lease on `slot`, blocking up to `wait` seconds (polling).

    Returns the lease id, or None when `cancel_wait` asks it to stop. Raises
    `SlotFull` if the slot is still at its max once `wait` has elapsed.
    """
    root = _resolve_root(state_root)
    slot_dir = root / slot
    deadline = time.monotonic() + wait
    poll_interval = 0.2
    while True:
        if cancel_wait is not None and cancel_wait():
            return None
        lock_file = _with_slot_lock(slot_dir)
        try:
            max_n = _get_or_set_max(slot_dir, max_holders)
            holders = _prune_locked(slot_dir)
            if len(holders) < max_n:
                token = secrets.token_hex(8)
                _write_json_atomic(
                    _holder_path(slot_dir, token),
                    {
                        "holder": holder,
                        "pid": os.getpid(),
                        "deadline": time.monotonic() + ttl,
                        "slot": slot,
                    },
                )
                return f"{slot}:{token}"
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        if time.monotonic() >= deadline:
            raise SlotFull(slot)
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


_split_lease = _lease_runtime.split_lease


def renew(lease: str, *, ttl: float = DEFAULT_TTL, state_root: str | Path | None = None) -> bool:
    """Push `lease`'s deadline out by `ttl`. Returns False if the lease is
    gone (already pruned, released, or never existed) -- the caller lost it.
    """
    slot, token = _split_lease(lease)
    root = _resolve_root(state_root)
    slot_dir = root / slot
    if not slot_dir.is_dir():
        return False
    lock_file = _with_slot_lock(slot_dir)
    try:
        path = _holder_path(slot_dir, token)
        data = _read_holder(path)
        if data is None:
            return False
        data["deadline"] = time.monotonic() + ttl
        _write_json_atomic(path, data)
        return True
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def release(lease: str, *, state_root: str | Path | None = None) -> bool:
    """Remove `lease`'s holder file. Returns False if it was already gone --
    releasing twice is not an error, the end state is what it would be
    anyway.
    """
    slot, token = _split_lease(lease)
    root = _resolve_root(state_root)
    slot_dir = root / slot
    if not slot_dir.is_dir():
        return False
    lock_file = _with_slot_lock(slot_dir)
    try:
        path = _holder_path(slot_dir, token)
        if path.exists():
            path.unlink()
            return True
        return False
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def status(*, state_root: str | Path | None = None) -> dict[str, dict]:
    """Return `{slot_name: {"holders": live_count, "max": max_or_None}}` for
    every slot directory under the state root.

    Read-only: unlike `acquire`, this does not delete expired holder files --
    it only excludes them from the live count. The next `acquire` on that
    slot does the actual pruning.
    """
    root = _resolve_root(state_root)
    if not root.is_dir():
        return {}
    now = time.monotonic()
    result: dict[str, dict] = {}
    for slot_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        live = 0
        for path in slot_dir.glob("*.holder"):
            data = _read_holder(path)
            if data is not None and data.get("deadline", 0) >= now:
                live += 1
        config_path = slot_dir / "config.json"
        max_n = None
        if config_path.exists():
            with open(config_path, encoding="utf-8") as handle:
                max_n = json.load(handle).get("max")
        result[slot_dir.name] = {"holders": live, "max": max_n}
    return result


def hold(
    command: list[str],
    *,
    lease: str | None = None,
    slot: str | None = None,
    holder: str | None = None,
    wait: float = 0.0,
    ttl: float = DEFAULT_TTL,
    max_holders: int | None = None,
    state_root: str | Path | None = None,
    before_run: Callable[[], bool] | None = None,
) -> int | None:
    """Acquire (unless `lease` is already held), run `command`, renewing the
    lease while it runs, and release on exit -- success, failure, or the
    child being killed by a signal. Returns the child's exit code, or
    128 + signal number if it was killed by one (the usual shell convention).

    If `before_run` returns False while waiting or after acquiring the lease,
    `hold` releases any lease and returns `None` without running the command.

    Release always runs: it is in a `finally`, after `proc.wait()` returns --
    which it does even when the child was killed by SIGTERM or SIGKILL, since
    that is the parent process observing the child's death, not the parent
    itself being killed. A SIGTERM sent to this process (the `hold` process)
    is caught and forwarded to the child so the same cleanup path runs, but
    only when `hold` runs on the main thread -- Python's `signal.signal` is
    main-thread-only, so a caller running `hold` on a worker thread (as the
    tests do, to be able to signal the child while `hold` blocks) skips that
    extra forwarding and relies on the `finally` below, same as it would for
    any other way the child exits. A SIGKILL sent to this process cannot be
    caught by anything, in any language, so that case is out of scope
    (nothing could run a release in that case regardless of how this
    function is written).

    The subprocess/renew-timer/signal-forwarding mechanics live in
    `_lease_runtime.run_with_lease`, shared with the `redis` backend's own
    `hold` -- only the renew/release calls underneath differ per backend.
    """
    if lease is None:
        if slot is None or holder is None:
            raise ValueError("hold needs either lease=, or slot= and holder=")
        cancel_wait = (
            (lambda: not before_run()) if before_run is not None else None
        )
        lease = acquire(
            slot, holder, wait=wait, ttl=ttl, max_holders=max_holders,
            state_root=state_root, cancel_wait=cancel_wait,
        )
        if lease is None:
            return None

    if before_run is not None and not before_run():
        release(lease, state_root=state_root)
        return None

    return _lease_runtime.run_with_lease(
        command,
        lease,
        ttl=ttl,
        renew=lambda lease_id: renew(lease_id, ttl=ttl, state_root=state_root),
        release=lambda lease_id: release(lease_id, state_root=state_root),
    )

"""The fleet machine registry: `lupin join`/`heartbeat`/`drain`/`undrain`/
`machines` (issue #7, part of #2's plan). See `docs/redis-schema.md`'s
"Fleet keys" section for the `machine:<name>` key shape this module reads
and writes -- that doc is the spec, this module is just that spec in code.

Reuses `slots_redis._client()`/`_call_with_retry()` for the Redis
connection and retry-once rule, and `slots_redis.status()` for the slot
summary -- same backend, same conventions, not reimplemented here.

Judgment call -- offline detection: the schema lists `machine:<name>` as
"with a TTL" and says a machine that misses two 30s renewals (120s) is
offline, "same convention as the bmo slot lease". The bmo lease enforces
its TTL by comparing a stored expiry to "now" when read, not by relying on
Redis to delete the key the instant it expires -- `status()` still reports
a holder whose score has passed until the next acquire/renew prunes it.
This module copies that: `OFFLINE_AFTER` (120s) is compared against the
record's own `heartbeat` field by `machines()`, so a dead machine shows up
as "offline" instead of silently vanishing. The Redis key itself gets a
much longer TTL (`RECORD_TTL`, 20x `OFFLINE_AFTER`) purely as a janitor for
machines retired long ago -- not the thing that decides online/offline.

`quota` comes from `quota.snapshot()` (issue #8), recomputed on every
write. `providers` is still a stub (`[]`) -- nothing populates it yet, but
`_write_record` carries over whatever is already there instead of
overwriting it, so a future writer's value survives the next heartbeat.

`loops` lists the live loops on this machine. Each entry includes its
repo, platform, and state from the Herdr agent API.

`join()` and `heartbeat()` receive loop state from Herdr and repo inventory
from their caller. They do not infer loop state from pane text. `drain()`
and `undrain()` keep their last known values.

`session_backend` is `"herdr"` on every machine.

`actions` is the list of queue actions this machine's `lupin agent`
accepts. It is read straight from `agent.ACTIONS`, so it always matches
what the agent actually runs.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import redis

from . import quota, slots_redis

CoordinatorUnreachable = slots_redis.CoordinatorUnreachable

PREFIX = slots_redis.PREFIX
OFFLINE_AFTER = 120.0
RECORD_TTL = int(OFFLINE_AFTER * 20)
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "lupin" / "fleet.json"
_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)

# Herdr owns local loop sessions on every machine.
SESSION_BACKEND = "herdr"


def package_version() -> str:
    """This machine's `lupin` version, for the record's `version` field and
    for comparing against another machine's. `importlib.metadata` reads it
    off the installed package's metadata (how the Nix-built `lupin` runs);
    in a dev checkout that was never `pip install`-ed (e.g. this repo's own
    test suite, run via `pytest`'s `pythonpath` instead), there is no such
    metadata, so this falls back to a fixed placeholder rather than raising.
    """
    try:
        return _pkg_version("lupin")
    except PackageNotFoundError:
        return "0.0.0+dev"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def hostname() -> str:
    return socket.gethostname()


def _record_key(name: str) -> str:
    return f"{PREFIX}machine:{name}"


def _slot_totals(slots: dict) -> tuple[int, int]:
    """(used, max) summed across every slot a `machines()` record reports.
    Shared by `place.py` (ranking candidates) and `quest.py` (picking a
    focus machine) -- one reading of a machine's free capacity, not two.
    """
    used = sum(int(entry.get("used", 0)) for entry in (slots or {}).values())
    max_ = sum(int(entry.get("max", 0)) for entry in (slots or {}).values())
    return used, max_


def _heartbeat_age(record: dict, now: float) -> float:
    """Seconds since a `machines()` record's own heartbeat. Shared the same
    way `_slot_totals` is -- `place.py` and `quest.py` both break ranking
    ties on heartbeat freshness.
    """
    stamp = record.get("heartbeat")
    if not stamp:
        return float("inf")
    try:
        return now - _parse_iso(stamp)
    except ValueError:
        return float("inf")


def load_config(config_path: str | Path | None = None) -> dict:
    """What `lupin join` last wrote: `redis_host`, `redis_port`, and
    `redis_username` if one was given. `{}` if this machine hasn't joined.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def _write_config(config: dict, config_path: str | Path | None = None) -> Path:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _load_credential(name: str) -> str | None:
    """A systemd credential, from `$CREDENTIALS_DIRECTORY/<name>`.

    Same convention `loop_runtime.worker()` already uses for the fleet's
    Redis password: the secret is loaded into the service by systemd, not
    passed in the environment or stored in the fleet config, so a process
    that wants it has to read it from here.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return None
    try:
        value = (Path(directory) / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _redis_password() -> str | None:
    """The fleet Redis password, for a caller that passes none.

    A caller that does pass one (`cli.py`'s `--redis-password`, which
    defaults to `$LUPIN_REDIS_PASSWORD`) always wins. Without this
    fallback, every path that resolves the fleet connection without a
    password -- `lupin serve` under systemd, which loads the secret as
    `LoadCredential` and sets no `LUPIN_REDIS_PASSWORD`, and
    `gh_cache`'s own `_resolve` -- connects unauthenticated and Redis
    answers `AuthenticationError`. That error subclasses `ConnectionError`,
    so it is indistinguishable from "Redis is down" and every fleet read
    reports the cache as unreachable.
    """
    return _load_credential("redis-password") or os.environ.get("LUPIN_REDIS_PASSWORD")


def resolve_connection(
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    config_path: str | Path | None = None,
) -> dict:
    """Fill in whatever `redis_host`/`redis_port`/`redis_username` a caller
    didn't pass from the config `lupin join` wrote, then a hardcoded
    default. `redis_password` is never read from the config file (`join`
    never writes it there -- see `join`'s docstring), only from the caller
    (in practice, `cli.py`'s `--redis-password`/`$LUPIN_REDIS_PASSWORD`) or,
    when the caller passes none, from `_redis_password()`.
    """
    config = load_config(config_path)
    return {
        "redis_host": redis_host or config.get("redis_host") or "localhost",
        "redis_port": redis_port or config.get("redis_port") or 6379,
        "redis_username": redis_username or config.get("redis_username"),
        "redis_password": redis_password or _redis_password(),
    }


def _slot_summary(connection: dict) -> dict:
    raw = slots_redis.status(**connection)
    return {name: {"used": info["holders"], "max": info["max"]} for name, info in raw.items()}


def _write_record(
    client, name: str, *, state: str, connection: dict,
    loops: list[dict] | None = None, repos: list[dict] | None = None,
) -> dict:
    """Keep the last `providers`, `loops`, and `repos` values when a writer does not refresh them.

    `quota` is recalculated on every write.
    `usage_detail` carries the full per-window quota rows and 7-day token
    totals -- `quota` only keeps one summarized row per provider, which is
    enough for `place`'s scoring but not for the `/usage` page's richer
    tables. Reported here, by whichever host actually has the provider
    logins, so a reader (pihome, which has none) never needs its own.
    `actions` is read live from `agent.py`'s own `ACTIONS` table (imported
    here, not at module load, to avoid a top-level import cycle -- `agent.py`
    imports this module to check `draining` state). This way the list can
    never drift from what the agent here actually supports.
    """
    from . import agent  # deferred import, dodges the cycle noted above

    existing = _read_record(client, name)
    record = {
        "version": package_version(),
        "heartbeat": _now_iso(),
        "state": state,
        "slots": _slot_summary(connection),
        "providers": existing.get("providers", []) if existing else [],
        "quota": quota.snapshot(),
        "usage_detail": {
            "quota_rows": quota.quota_usage(),
            "token_rows": quota.claude_usage() + quota.omp_usage(),
        },
        "loops": loops if loops is not None else (existing.get("loops", []) if existing else []),
        "repos": repos if repos is not None else (existing.get("repos", []) if existing else []),
        "session_backend": SESSION_BACKEND,
        "actions": sorted(agent.ACTIONS),
    }
    client.set(_record_key(name), json.dumps(record), ex=RECORD_TTL)
    return record


def _read_record(client, name: str) -> dict | None:
    raw = client.get(_record_key(name))
    return json.loads(raw) if raw is not None else None


def _readable_record(raw: str) -> dict | None:
    """The record, or None if `raw` is not a JSON object with a heartbeat that parses."""
    try:
        record = json.loads(raw)
        _parse_iso(record["heartbeat"])
    except (ValueError, TypeError, KeyError):
        return None
    return record if isinstance(record, dict) else None


def _run(op):
    """`_call_with_retry`, but a connection failure becomes
    `CoordinatorUnreachable` -- the schema's fallback table has no fallback
    for the fleet keys (unlike the `bmo` slot), so there is nothing to fall
    back to, just one exception `cli.py` already knows how to report.
    """
    try:
        return slots_redis._call_with_retry(op)
    except _REDIS_ERRORS as exc:
        raise CoordinatorUnreachable("machine registry") from exc


def join(
    coordinator: str,
    *,
    redis_username: str | None = None,
    redis_password: str | None = None,
    config_path: str | Path | None = None,
    loops: list[dict] | None = None,
    repos: list[dict] | None = None,
) -> dict:
    """Write the Redis location to the local fleet config and register this machine.

    The caller supplies this machine's live loops and local repo list. The
    password is used for this write only; it is not saved in the config.
    """

    host, _, port_str = coordinator.partition(":")
    port = int(port_str) if port_str else 6379
    config = {"redis_host": host, "redis_port": port}
    if redis_username:
        config["redis_username"] = redis_username
    path = _write_config(config, config_path)

    connection = {
        "redis_host": host,
        "redis_port": port,
        "redis_username": redis_username,
        "redis_password": redis_password,
    }
    client = slots_redis._client(host, port, redis_username, redis_password)
    name = hostname()
    record = _run(
        lambda: _write_record(
            client, name, state="online", connection=connection, loops=loops, repos=repos
        )
    )
    return {"name": name, "config_path": str(path), **record}


def heartbeat(
    connection: dict, *, loops: list[dict] | None = None, repos: list[dict] | None = None
) -> dict:
    """Refresh this machine's record and keep its state.

    `loops` lists live Herdr loops; `repos` lists local repos. The caller
    reads both values and passes them in.
    """
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    name = hostname()

    def op():
        existing = _read_record(client, name)
        state = existing["state"] if existing else "online"
        return _write_record(
            client, name, state=state, connection=connection, loops=loops, repos=repos
        )

    return _run(op)


def _set_state(connection: dict, state: str) -> dict:
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    name = hostname()
    return _run(lambda: _write_record(client, name, state=state, connection=connection))


def drain(connection: dict) -> dict:
    return _set_state(connection, "draining")


def undrain(connection: dict) -> dict:
    return _set_state(connection, "online")


def machines(
    connection: dict, skipped: list[str] | None = None, *, strict: bool = True
) -> list[dict]:
    """Every registered machine, each as:
    `{"name", "state", "version", "heartbeat", "version_mismatch", "slots",
    "providers", "quota", "usage_detail", "loops", "session_backend",
    "actions"}`.

    `state` is the record's own `online`/`draining`, overridden to
    `offline` once `OFFLINE_AFTER` seconds have passed since `heartbeat`
    with no renewal -- see this module's docstring for why that is computed
    here rather than left to Redis's key TTL.

    `slots`/`providers`/`quota` are carried straight through from the
    record (see `_write_record`) -- added for `place` (issue #9), which
    scores machines on exactly this data. Earlier callers only read
    name/state/version/heartbeat, so this is a pure addition, not a change
    to those fields.

    `loops`/`repos`/`session_backend`/`actions` are optional additions. Old
    records return empty lists or `None` for these fields.

    A record that cannot be read is not JSON, not an object, or has no
    readable `heartbeat`.

    `strict=True` (the default) raises the error for that record
    (`json.JSONDecodeError`, `KeyError`, or `AttributeError`). Acting
    callers use it. A list without the record is a partial list.

    `strict=False` leaves the record out. Its `machine:<name>` label is
    added to `skipped` when `skipped` is a list. Display callers use it.
    """
    client = slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )
    local_version = package_version()
    now = time.time()

    def op():
        keys = list(client.scan_iter(match=f"{PREFIX}machine:*"))
        result = []
        unreadable = []
        for key in keys:
            name = key[len(f"{PREFIX}machine:") :]
            raw = client.get(key)
            if raw is None:
                continue
            if strict:
                record = json.loads(raw)
            else:
                record = _readable_record(raw)
                if record is None:
                    unreadable.append(f"machine:{name}")
                    continue
            state = record.get("state", "online")
            if now - _parse_iso(record["heartbeat"]) > OFFLINE_AFTER:
                state = "offline"
            result.append(
                {
                    "name": name,
                    "state": state,
                    "version": record.get("version"),
                    "heartbeat": record.get("heartbeat"),
                    "version_mismatch": record.get("version") != local_version,
                    "slots": record.get("slots", {}),
                    "providers": record.get("providers", []),
                    "quota": record.get("quota", {}),
                    "usage_detail": record.get("usage_detail", {}),
                    "loops": record.get("loops", []),
                    "repos": record.get("repos", []),
                    "session_backend": record.get("session_backend"),
                    "actions": record.get("actions", []),
                }
            )
        return result, unreadable

    # `_run` may retry `op`, so the labels are added only after it returns.
    result, unreadable = _run(op)
    if skipped is not None:
        skipped.extend(unreadable)
    return result

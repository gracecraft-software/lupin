"""Shared cache for read-only `gh` lookups (issue #35): `place.py`,
`quest.py`, `roadmap.py`, and `roadmap_cli.py` all call `gh` for the same
kind of data -- issue state, body, labels, dependency links. Every machine
doing that independently means duplicate rate-limit pressure and different
machines seeing different snapshots of the same repo at the same moment.

Grace's correction on the first design: the fetcher is not "whichever
machine wins a lock race" -- it is one named machine, pinned by hostname.
`CANONICAL_GH_FETCHER` is that name. Every other machine reads the cache
while that fetcher is alive; on a miss it returns an honest "no data yet"
error instead of calling `gh` itself. When the pinned fetcher is not
maintaining the cache (draining, offline, or never joined), the pin has no
holder, so the next machine reads the fleet registry, sees that, and fetches
live itself -- publishing through the same shared lock, so the first
machine to get there still answers for the whole fleet. A machine never
calls `gh` on its own when Redis is down and it is not the canonical
fetcher. The `slots_redis` lock below serializes fetchers; with more than
one machine able to fetch, that lock is what keeps the "one snapshot" part
of the design true.

Risk, stated rather than papered over: `CANONICAL_GH_FETCHER` is compared
against `machines.hostname()` (`socket.gethostname()`) with a plain `==`.
If that machine's hostname is ever reported differently -- a FQDN
(`pihome.local`) instead of the short name, or changed by whoever reimages
it -- this check silently stops matching and pihome's own reads stop
fetching. The registry check in `_canonical_fetcher_live` no longer lets
that take the rest of the fleet down with it: the registry is keyed by the
name `join` wrote, so a hostname mismatch leaves pihome looking absent,
which is the same signal as "not maintaining the cache", and the other
machines fetch live instead of showing nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import redis

from . import machines, slots_redis

PREFIX = slots_redis.PREFIX
CANONICAL_GH_FETCHER = "pihome"

# A few minutes: short enough that `place`/`quest` aren't deciding off data
# that's badly stale, long enough that a burst of lookups across machines
# within that window shares one fetch instead of each paying for their own.
CACHE_TTL = 300

# The lock only guards the canonical fetcher against itself (two `lupin`
# invocations on the same machine racing), so a short wait is enough --
# contention here is rare and brief. The TTL is generous (2 min) because a
# paginated GraphQL fetch (comments, dependency links) can run several
# `gh api graphql` calls in a row; if a fetch ever runs past this, the lock
# just expires and a second fetch may start -- a harmless duplicate read,
# not a correctness problem.
LOCK_WAIT = 2.0
LOCK_TTL = 120.0

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _resolve(connection: dict | None) -> dict:
    """Fold `connection` (whatever a caller passed, if anything) into the
    fleet's configured Redis location -- `machines.resolve_connection`
    already knows how to do this (reads `~/.config/lupin/fleet.json`,
    falls back to localhost). Without this, a machine that never threaded
    a `connection` dict this deep would always miss the fleet's real Redis
    and talk to a local, empty one -- defeating the point of a shared cache.
    """
    connection = connection or {}
    return machines.resolve_connection(
        redis_host=connection.get("redis_host"),
        redis_port=connection.get("redis_port"),
        redis_username=connection.get("redis_username"),
        redis_password=connection.get("redis_password"),
    )


def _client(connection: dict):
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _canonical_fetcher_live(connection: dict) -> bool:
    """Is `CANONICAL_GH_FETCHER` maintaining the shared cache right now?

    Read from the fleet registry, not assumed. A pinned host that is
    draining, offline, or never joined leaves the cache cold forever --
    which is exactly what happened here: pihome was drained and jesus,
    the machine running `lupin serve`, refused every `gh` read. This is
    the cold-cache case the module docstring names as a risk ("the fetch
    path goes cold fleet-wide ... it would show up as every caller's cache
    staying empty"); this check is what detects it.

    Returns True when the registry cannot be read: an unreadable registry
    is not evidence the fetcher is gone, and the conservative answer keeps
    the original behaviour rather than starting a fetch it cannot justify.
    """
    try:
        records = {record["name"]: record for record in machines.machines(connection, strict=True)}
    except Exception:
        return True
    record = records.get(CANONICAL_GH_FETCHER)
    return bool(record) and record.get("state") == "online"


def _fetch_and_publish(
    owner: str,
    name: str,
    cache_key: str,
    fetch_fn: Callable[[], tuple[Any, str | None]],
    client,
    connection: dict,
) -> tuple[Any, str | None]:
    """Run `fetch_fn` under the shared `gh-fetch` lock and publish the
    result. Only one machine fetches a given repo's key at a time, so the
    first one to win answers for the whole fleet and everyone else reads
    its answer from the cache instead of paying for a second `gh` call.
    """
    key = f"{PREFIX}gh-cache:{owner}/{name}:{cache_key}"
    # A unique holder per call, not just `cache_key`: `slots_redis.acquire`
    # treats a second acquire from the *same* holder as a renew, not
    # contention (see `_ACQUIRE_SCRIPT` -- same holder means no wait at
    # all). Two real concurrent calls for the same cache_key need distinct
    # holders to actually serialize against each other; `cache_key` alone
    # would make them look like the same caller renewing its own lease.
    holder = f"{cache_key}:{uuid.uuid4().hex[:8]}"

    # No colon in the slot name: `_lease_runtime.split_lease` rebuilds
    # `(slot, holder)` from the lease string `f"{slot}:{holder}"` by
    # splitting on the *first* colon only, so a colon inside `slot` itself
    # (e.g. "gh-fetch:acme/repo") makes it parse the wrong slot and
    # `release()`/`renew()` silently act on a key that was never acquired
    # -- the real lock then never clears until LOCK_TTL expires. "/" has
    # the same job without that trap.
    lease = None
    try:
        lease = slots_redis.acquire(
            f"gh-fetch/{owner}/{name}",
            holder=holder,
            wait=LOCK_WAIT,
            ttl=LOCK_TTL,
            redis_host=connection.get("redis_host"),
            redis_port=connection.get("redis_port"),
            redis_username=connection.get("redis_username"),
            redis_password=connection.get("redis_password"),
        )
    except slots_redis.SlotFull:
        # Another fetch for this repo is already in flight -- on the
        # canonical fetcher this is another `lupin` process on the same
        # machine, in the fallback case it is another machine. Check once
        # more in case it just finished, otherwise fetch anyway. A
        # duplicate read is wasted work, not a bug.
        hit, cached, _redis_ok = _read_cache(client, key)
        if hit:
            return cached, None
    except slots_redis.CoordinatorUnreachable:
        return fetch_fn()
    else:
        # We got the lock, possibly after waiting on LOCK_WAIT -- the
        # holder we were waiting behind may have already finished and
        # published the result. Check before paying for a second live
        # fetch; without this, every caller that waits (the normal case,
        # not just SlotFull) double-fetches `gh`.
        hit, cached, _redis_ok = _read_cache(client, key)
        if hit:
            _release(lease, connection)
            return cached, None

    try:
        data, error = fetch_fn()
        if error:
            return data, error
        _write_cache(client, key, data)
        return data, None
    finally:
        if lease:
            _release(lease, connection)


def cached_gh_json(
    owner: str,
    name: str,
    cache_key: str,
    fetch_fn: Callable[[], tuple[Any, str | None]],
    *,
    connection: dict | None = None,
) -> tuple[Any, str | None]:
    """Return `fetch_fn()`'s result, either from the shared cache or from a
    live `gh` call. `CANONICAL_GH_FETCHER` always fetches on a miss. Every
    other machine reads the cache while that fetcher is alive; when it is
    not (draining, offline, or never joined -- see `_canonical_fetcher_live`),
    this machine fetches too, and publishes, so the fleet still gets one
    shared snapshot. A machine never fetches when Redis is down and it is
    not the canonical fetcher.

    `fetch_fn` takes no arguments and returns `(data, error)`, the same
    shape every `gh`-calling function in this codebase already uses --
    callers wrap their real call in a closure. A result is cached only when
    `error` is falsy; a failed fetch is never cached, so the next caller
    (which might be able to reach `gh` where this one couldn't) gets a real
    retry instead of a cached failure.
    """
    connection = _resolve(connection)
    client = _client(connection)
    key = f"{PREFIX}gh-cache:{owner}/{name}:{cache_key}"

    hit, cached, redis_ok = _read_cache(client, key)
    if hit:
        return cached, None

    hostname = machines.hostname()
    canonical = hostname == CANONICAL_GH_FETCHER
    if not canonical:
        if not redis_ok:
            return None, (
                f"the GitHub data cache is unreachable and this machine ({hostname}) "
                f"is not {CANONICAL_GH_FETCHER}, so it cannot fetch directly"
            )
        if _canonical_fetcher_live(connection):
            return None, (
                f"no cached GitHub data yet for {cache_key} ({owner}/{name}); "
                f"only {CANONICAL_GH_FETCHER} fetches live data"
            )
        # Nobody is maintaining the cache, so fall through and fetch here.

    if not redis_ok:
        # Only the canonical fetcher reaches this with Redis down: it is
        # still the authority even when it cannot publish for anyone else
        # -- answer its own caller with a live fetch rather than failing a
        # command over a cache-layer outage.
        return fetch_fn()

    return _fetch_and_publish(owner, name, cache_key, fetch_fn, client, connection)


def _release(lease, connection: dict) -> None:
    try:
        slots_redis.release(
            lease,
            redis_host=connection.get("redis_host"),
            redis_port=connection.get("redis_port"),
            redis_username=connection.get("redis_username"),
            redis_password=connection.get("redis_password"),
        )
    except slots_redis.CoordinatorUnreachable:
        pass


def _read_cache(client, key: str) -> tuple[bool, Any, bool]:
    try:
        raw = slots_redis._call_with_retry(lambda: client.get(key))
    except _REDIS_ERRORS:
        return False, None, False
    if raw is None:
        return False, None, True
    try:
        envelope = json.loads(raw)
        return True, envelope["data"], True
    except (json.JSONDecodeError, KeyError, TypeError):
        return False, None, True


def _write_cache(client, key: str, data: Any) -> None:
    try:
        slots_redis._call_with_retry(
            lambda: client.set(key, json.dumps({"data": data}), ex=CACHE_TTL)
        )
    except _REDIS_ERRORS:
        pass  # best effort -- CANONICAL_GH_FETCHER still has the live answer

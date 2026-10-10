"""GitHub-issue claims: one loop marks an issue as its own so two loops
never work the same task (issue #6, the `lupin` half of
`gracecraft/nix#212`). See `docs/redis-schema.md` for the key shape this
module implements.

A claim is a single Redis string at `claim:<owner>/<repo>#<n>`: JSON
`{"host", "session", "since"}`, with a TTL Redis enforces natively. Unlike
`slots_redis.py`'s slots (a sorted set, lazily pruned by whoever next calls
`acquire`/`renew`), a claim has exactly one holder, so `SET ... PX` and
Redis's own expiry are enough -- no pruning code needed here.

Reuses `slots_redis._client` and `slots_redis._call_with_retry` as-is (same
"2s connect timeout, one retry" rule every other Redis call in this project
follows) -- not redefined here, per issue #6.

Judgment call -- what `--holder H` means: the schema's JSON has three
fields, not one, so the single `--holder` string from the CLI becomes the
`session` field (the identity that must match for a renew or release to
succeed). `host` is filled in automatically from `socket.gethostname()`.
This keeps the CLI surface the same shape as `slots.py`/`slots_redis.py`'s
single `holder` string -- `host` is metadata the schema asks for, not a
second identity the caller has to pass.

Claims have no local fallback (`docs/redis-schema.md`'s fallback table: "the
orchestrator starts no new issue"), so every function here raises
`CoordinatorUnreachable` (imported from `slots.py`, same exception every
other backend failure uses) when Redis can't be reached -- there is no
`local` backend to fall back to, unlike the `bmo` slot.
"""

from __future__ import annotations

import json
import re
import socket
import time

import redis

from .slots import CoordinatorUnreachable
from .slots_redis import _call_with_retry, _client

PREFIX = "lupin:v1:"
DEFAULT_TTL = 600.0  # 10 minutes, per docs/redis-schema.md

_TARGET_RE = re.compile(r"^(?P<owner>[^/#]+)/(?P<repo>[^/#]+)#(?P<number>\d+)$")


class ClaimHeld(Exception):
    """Raised by `claim` when someone else already holds this issue."""

    def __init__(self, target: str, current_raw: str | None):
        detail = current_raw or "unknown holder"
        super().__init__(f"{target} is already claimed: {detail}")


def parse_target(target: str) -> str:
    """Validate `OWNER/REPO#N` and return it unchanged -- it's also the key
    suffix, since `docs/redis-schema.md`'s key is literally `claim:<that
    string>`.
    """
    if not _TARGET_RE.match(target):
        raise ValueError(f"expected OWNER/REPO#N, got {target!r}")
    return target


def _key(target: str) -> str:
    return f"{PREFIX}claim:{target}"


def _value(session: str) -> str:
    return json.dumps({"host": socket.gethostname(), "session": session, "since": time.time()})


# KEYS[1] = claim:<target>, ARGV[1] = session, ARGV[2] = value (JSON),
# ARGV[3] = ttl_ms. Claiming is idempotent for the same session (a retry
# renews instead of failing). Returns 1 (claimed or renewed) or 0 (someone
# else holds it).
_CLAIM_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if not ok or decoded.session ~= ARGV[1] then
        return 0
    end
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return 1
"""

# KEYS[1] = claim:<target>, ARGV[1] = session, ARGV[2] = value (JSON),
# ARGV[3] = ttl_ms. Unlike the claim script, never creates a new claim --
# only pushes the TTL out if `session` is the current holder. Returns 1
# (renewed) or 0 (not held by this session).
_RENEW_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
local ok, decoded = pcall(cjson.decode, current)
if not ok or decoded.session ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return 1
"""

# KEYS[1] = claim:<target>, ARGV[1] = session. Compare-and-delete: only
# removes the claim if `session` is the current holder. Returns 1
# (released) or 0 (not held by this session).
_RELEASE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
local ok, decoded = pcall(cjson.decode, current)
if not ok or decoded.session ~= ARGV[1] then
    return 0
end
redis.call('DEL', KEYS[1])
return 1
"""


def claim(
    target: str,
    holder: str,
    *,
    ttl: float = DEFAULT_TTL,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> None:
    """Atomically take `target` (an `OWNER/REPO#N` string) for `holder`.

    Idempotent for the same holder -- a retry renews rather than failing.
    Raises `ClaimHeld` if another holder already has it, or
    `CoordinatorUnreachable` if Redis can't be reached.
    """
    target = parse_target(target)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    value = _value(holder)
    try:
        result = _call_with_retry(
            lambda: client.eval(_CLAIM_SCRIPT, 1, _key(target), holder, value, int(ttl * 1000))
        )
        if not result:
            current = _call_with_retry(lambda: client.get(_key(target)))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    if not result:
        raise ClaimHeld(target, current)


def renew_claim(
    target: str,
    holder: str,
    *,
    ttl: float = DEFAULT_TTL,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    """Push `target`'s claim TTL back out. Returns False if `holder` is not
    the current holder (claim expired, released, or never theirs). Raises
    `CoordinatorUnreachable` if Redis can't be reached.
    """
    target = parse_target(target)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    value = _value(holder)
    try:
        result = _call_with_retry(
            lambda: client.eval(_RENEW_SCRIPT, 1, _key(target), holder, value, int(ttl * 1000))
        )
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    return bool(result)


def release_claim(
    target: str,
    holder: str,
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    """Compare-and-delete release. Returns False if `holder` is not the
    current holder (including "no one holds it"). Raises
    `CoordinatorUnreachable` if Redis can't be reached.
    """
    target = parse_target(target)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    try:
        result = _call_with_retry(lambda: client.eval(_RELEASE_SCRIPT, 1, _key(target), holder))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    return bool(result)


def _get_with_ttl(client, key: str) -> tuple[str | None, int]:
    """The value and the milliseconds left, read in one Redis round trip so
    the two always describe the same claim.
    """
    pipe = client.pipeline()
    pipe.get(key)
    pipe.pttl(key)
    return tuple(pipe.execute())


def _claim_object(raw: str) -> dict | None:
    """The claim, or None if `raw` is not a JSON object."""
    try:
        claim = json.loads(raw)
    except ValueError:
        return None
    return claim if isinstance(claim, dict) else None


def claims_for(
    repos: list[str],
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    with_ttl: bool = False,
    skipped: list[str] | None = None,
    strict: bool = False,
) -> dict[str, dict]:
    """Return `{"<owner>/<repo>#<n>": {"host", "session", "since"}}` for
    every currently-claimed issue in `repos` (each an `"<owner>/<repo>"`
    string, no issue number).

    With `with_ttl=True`, each value also has `"ttl"`: the seconds Redis
    still holds the claim. It is `None` if the key has no expiry.

    `strict=True` raises for a claim that is not a JSON object. Acting
    callers use it. A malformed claim raises `json.JSONDecodeError`, which
    is a `ValueError`.

    `strict=False` (the default) leaves such a claim out. Its
    `claim:<target>` label is added to `skipped` when `skipped` is a list.

    The integration point future `roadmap` (#10) and `quest` (#11) commands
    import to find out which of their issues are off-limits -- pass the
    repos you already know about, get back the claimed subset. Raises
    `CoordinatorUnreachable` if Redis can't be reached; there's no local
    fallback for claims, so a caller should treat that failure the same way
    `lupin claim` exiting 3 is treated elsewhere: start no new issue, but
    don't disturb anything already in progress.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    prefix = f"{PREFIX}claim:"
    try:
        keys = _call_with_retry(lambda: list(client.scan_iter(match=f"{prefix}*")))
        result: dict[str, dict] = {}
        for key in keys:
            target = key[len(prefix) :]
            owner_repo, _sep, _number = target.rpartition("#")
            if owner_repo not in repos:
                continue
            if with_ttl:
                raw, ttl_ms = _call_with_retry(lambda k=key: _get_with_ttl(client, k))
            else:
                raw = _call_with_retry(lambda k=key: client.get(k))
            if raw is None:
                continue
            if strict:
                claim = json.loads(raw)
                if not isinstance(claim, dict):
                    raise ValueError(f"claim:{target} is not a JSON object")
            else:
                claim = _claim_object(raw)
                if claim is None:
                    if skipped is not None:
                        skipped.append(f"claim:{target}")
                    continue
            if with_ttl:
                claim["ttl"] = ttl_ms / 1000 if ttl_ms >= 0 else None
            result[target] = claim
        return result
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("claims_for") from exc

"""GitHub-issue claims: one loop marks an issue as its own so two loops
never work the same task (issue #6, the `lupin` half of
`gracecraft/nix#212`). See `docs/redis-schema.md` for the key shape this
module implements.

This is one Redis string at `claim:<owner>/<repo>#<n>`. It stores JSON
`{"host", "session", "since", "generation"}` with a TTL. `generation` marks a
claim attempt. A same-holder claim or renewal gives it a new value when
present. Then a failed label update cannot release a newer claim. Legacy claims
without a generation stay compatible. Redis expires the claim; it needs no
pruning.

Reuses `slots_redis._client` and `slots_redis._call_with_retry` as-is (same
"2s connect timeout, one retry" rule every other Redis call in this project
follows) -- not redefined here, per issue #6.

`--holder H` is stored in `session`. `host` comes from `socket.gethostname()`.
`generation` is internal. It prevents a failed label sync from releasing a
newer claim.

Claims have no local fallback (`docs/redis-schema.md`'s fallback table: "the
orchestrator starts no new issue"), so every function here raises
`CoordinatorUnreachable` (imported from `slots.py`, same exception every
other backend failure uses) when Redis can't be reached -- there is no
`local` backend to fall back to, unlike the `bmo` slot.
A refused login raises `CoordinatorAuthFailed`. A denied command raises
`NoPermissionError` as-is.
"""

from __future__ import annotations

import json
import re
import socket
import time
import uuid

import redis

from .slots import CoordinatorUnreachable
from .slots_redis import SCAN_COUNT, _auth_failed, _call_with_retry, _client

PREFIX = "lupin:v1:"
DEFAULT_TTL = 600.0  # 10 minutes, per docs/redis-schema.md

_TARGET_RE = re.compile(r"^(?P<owner>[^/#]+)/(?P<repo>[^/#]+)#(?P<number>\d+)$")


class ClaimHeld(Exception):
    """Raised by `claim` when someone else already holds this issue."""

    def __init__(self, target: str, current_raw: str | None):
        claim = _claim_object(current_raw) if current_raw else None
        details = []
        if claim is not None:
            for key in ("host", "session"):
                value = claim.get(key)
                if isinstance(value, str):
                    details.append(f"{key}={value!r}")
            since = claim.get("since")
            if isinstance(since, (int, float)) and not isinstance(since, bool):
                details.append(f"since={since!r}")
        detail = ", ".join(details) or "unknown holder"
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


def _label_index_key(target: str) -> str:
    return f"{PREFIX}claim-index:{target}"


def _value(session: str, generation: str | None = None) -> str:
    value = {"host": socket.gethostname(), "session": session, "since": time.time()}
    if generation is not None:
        value["generation"] = generation
    return json.dumps(value)


# KEYS[1] = claim:<target>, KEYS[2] = claim-index:<target>.
# ARGV[1] = session, ARGV[2] = value (JSON), ARGV[3] = ttl_ms.
# Claiming is idempotent for the same session. Returns 1 for a new claim, 2
# when the same session already held it, or 0 when another session holds it.
_CLAIM_SCRIPT = """
local current = redis.call('GET', KEYS[1])
local same_holder = false
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if not ok or decoded.session ~= ARGV[1] then
        return 0
    end
    same_holder = true
end
redis.call('SET', KEYS[2], '1')
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
if same_holder then
    return 2
end
return 1
"""

# KEYS[1] = claim:<target>, KEYS[2] = claim-index:<target>.
# ARGV[1] = session, ARGV[2] = value (JSON), ARGV[3] = ttl_ms,
# ARGV[4] = generation. This only renews a claim held by `session`.
_RENEW_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
local ok, decoded = pcall(cjson.decode, current)
if not ok or decoded.session ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[2], '1')
local value = cjson.decode(ARGV[2])
if decoded.generation then
    value.generation = ARGV[4]
end
redis.call('SET', KEYS[1], cjson.encode(value), 'PX', ARGV[3])
return 1
"""

# KEYS[1] = claim:<target>, KEYS[2] = claim-index:<target>. Keep the index
# while a Redis claim is active.
_INDEX_IF_ACTIVE_SCRIPT = """
if not redis.call('GET', KEYS[1]) then
    return 0
end
redis.call('SET', KEYS[2], '1')
return 1
"""

# Delete the label index only when the claim key is still absent. A concurrent
# claim writes both keys in its own script.
_CLEAR_INDEX_IF_UNCLAIMED_SCRIPT = """
if redis.call('GET', KEYS[1]) then
    return 0
end
redis.call('DEL', KEYS[2])
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

# KEYS[1] = claim:<target>; ARGV[1] = session; ARGV[2] = generation.
# Release only the generation created by a failed label sync.
_RELEASE_GENERATION_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current then
    return 0
end
local ok, decoded = pcall(cjson.decode, current)
if not ok or decoded.session ~= ARGV[1] or decoded.generation ~= ARGV[2] then
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
    generation: str | None = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    """Take `target` for `holder`; return whether this created a new claim.

A same-holder retry renews the claim and advances its generation.
    Raises `ClaimHeld` if another holder already has it, or
    `CoordinatorUnreachable` if Redis can't be reached.
    """
    target = parse_target(target)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    generation = generation or uuid.uuid4().hex
    value = _value(holder, generation)
    try:
        result = _call_with_retry(
            lambda: client.eval(
                _CLAIM_SCRIPT, 2, _key(target), _label_index_key(target),
                holder, value, int(ttl * 1000)
            )
        )
        if not result:
            current = _call_with_retry(lambda: client.get(_key(target)))
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    if not result:
        raise ClaimHeld(target, current)
    return result == 1


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
    generation = uuid.uuid4().hex
    value = _value(holder)
    try:
        result = _call_with_retry(
            lambda: client.eval(
                _RENEW_SCRIPT, 2, _key(target), _label_index_key(target),
                holder, value, int(ttl * 1000), generation
            )
        )
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    return bool(result)


def renew_holder_claims(
    holder: str,
    *,
    ttl: float = DEFAULT_TTL,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> list[str]:
    """Renew each claim that `holder` holds. Return the renewed targets.

    A loop uses this to keep its own claims (issue #156). It uses only
    `SCAN` and the renew script (`EVAL`, with `GET` and `SET`). These are on
    the fleet ACL list. The script renews a claim only when `holder` holds
    it, so this never renews a claim of another holder. Errors are the same
    as for `renew_claim`.
    """
    client = _client(redis_host, redis_port, redis_username, redis_password)
    prefix = f"{PREFIX}claim:"
    value = _value(holder)
    renewed = []
    try:
        keys = _call_with_retry(
            lambda: list(client.scan_iter(match=f"{prefix}*", count=SCAN_COUNT))
        )
        for key in keys:
            generation = uuid.uuid4().hex
            if _call_with_retry(
                lambda k=key, g=generation: client.eval(
                    _RENEW_SCRIPT, 2, k, _label_index_key(k[len(prefix) :]),
                    holder, value, int(ttl * 1000), g
                )
            ):
                renewed.append(key[len(prefix) :])
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(f"claims of {holder}") from exc
    return renewed


def release_claim(
    target: str,
    holder: str,
    *,
    expected_generation: str | None = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    """Compare-and-delete release. `expected_generation` is optional. When
    set, the current claim must also have that value. Returns False if the
    claim does not match. Raises `CoordinatorUnreachable` if Redis can't be
    reached.
    """
    target = parse_target(target)
    client = _client(redis_host, redis_port, redis_username, redis_password)
    try:
        script = _RELEASE_SCRIPT
        args = (holder,)
        if expected_generation is not None:
            script = _RELEASE_GENERATION_SCRIPT
            args = (holder, expected_generation)
        result = _call_with_retry(
            lambda: client.eval(script, 1, _key(target), *args)
        )
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    return bool(result)


def _scan_claim_values(
    *,
    client: "redis.Redis | None" = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> dict[str, str | bytes]:
    """Return every active claim's exact target and stored value."""
    client = client or _client(redis_host, redis_port, redis_username, redis_password)
    prefix = f"{PREFIX}claim:"
    try:
        keys = _call_with_retry(
            lambda: list(client.scan_iter(match=f"{prefix}*", count=SCAN_COUNT))
        )
        result = {}
        for key in keys:
            raw = _call_with_retry(lambda k=key: client.get(k))
            if raw is not None:
                result[key[len(prefix) :]] = raw
        return result
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("claim scan") from exc


def _indexed_targets(
    *,
    client: "redis.Redis | None" = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> set[str]:
    client = client or _client(redis_host, redis_port, redis_username, redis_password)
    prefix = f"{PREFIX}claim-index:"
    try:
        keys = _call_with_retry(
            lambda: list(client.scan_iter(match=f"{prefix}*", count=SCAN_COUNT))
        )
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("claim label index") from exc
    return {key[len(prefix) :] for key in keys}


def _mark_label_index_if_active(
    target: str,
    *,
    client: "redis.Redis | None" = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    target = parse_target(target)
    client = client or _client(redis_host, redis_port, redis_username, redis_password)
    try:
        result = _call_with_retry(
            lambda: client.eval(
                _INDEX_IF_ACTIVE_SCRIPT, 2, _key(target), _label_index_key(target)
            )
        )
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    return bool(result)


def _mark_label_index(
    target: str,
    *,
    client: "redis.Redis | None" = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> None:
    """Keep a target indexed while a GitHub label update may be in flight."""
    target = parse_target(target)
    client = client or _client(redis_host, redis_port, redis_username, redis_password)
    try:
        _call_with_retry(lambda: client.set(_label_index_key(target), "1"))
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc



def _clear_label_index_if_unclaimed(
    target: str,
    *,
    client: "redis.Redis | None" = None,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
) -> bool:
    target = parse_target(target)
    client = client or _client(redis_host, redis_port, redis_username, redis_password)
    try:
        result = _call_with_retry(
            lambda: client.eval(
                _CLEAR_INDEX_IF_UNCLAIMED_SCRIPT, 2, _key(target), _label_index_key(target)
            )
        )
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable(target) from exc
    return bool(result)


def _claim_object(raw: str) -> dict | None:
    """The claim, or None if `raw` is not a JSON object."""
    try:
        claim = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(claim, dict):
        return None
    claim.pop("generation", None)
    return claim


def claims_for(
    repos: list[str],
    *,
    redis_host: str | None = None,
    redis_port: int | None = None,
    redis_username: str | None = None,
    redis_password: str | None = None,
    client: "redis.Redis | None" = None,
    with_ttl: bool = False,
    skipped: list[str] | None = None,
    strict: bool = False,
) -> dict[str, dict]:
    """Return `{"<owner>/<repo>#<n>": {"host", "session", "since"}}` for
    every currently-claimed issue in `repos` (each an `"<owner>/<repo>"`
    string, no issue number).

    Pass `client` to use a ready client, as the debrief path does. Without
    it, a client is made from the connection arguments.

    With `with_ttl=True`, each value also has `"ttl"`: the seconds Redis
    still holds the claim. It is `None` if the key has no expiry.

    `strict=True` raises `ValueError` for a claim that is not a JSON object.
    Acting callers use it. `strict=False` (the default) leaves such a claim
    out. Its `claim:<target>` label is added to `skipped` when `skipped` is
    a list.

    This is the integration point that future `roadmap` (#10) and `quest`
    (#11) commands import. They use it to find out which of their issues are
    off-limits. Pass the repos you already know about. The function returns
    the claimed subset. Raises `CoordinatorUnreachable` if Redis cannot be
    reached. There is no local fallback for claims. Callers must handle
    `CoordinatorUnreachable`. Start no new issue. Do not disturb anything
    already in progress.
    """
    if client is None:
        client = _client(redis_host, redis_port, redis_username, redis_password)
    prefix = f"{PREFIX}claim:"
    try:
        keys = _call_with_retry(
            lambda: list(client.scan_iter(match=f"{prefix}*", count=SCAN_COUNT))
        )
        wanted = []
        for key in keys:
            target = key[len(prefix) :]
            owner_repo, _sep, _number = target.rpartition("#")
            if owner_repo in repos:
                wanted.append(key)
        result: dict[str, dict] = {}
        if not wanted:
            return result
        # One batch for all keys. GET, not MGET: a key of the wrong type
        # raises an error, where MGET returns None. With `with_ttl`, each key
        # also gets a TTL. No MULTI: the fleet ACL does not allow it. A claim
        # that expires between GET and TTL only loses its TTL. Build
        # the batch inside the retry: execute() clears it.
        def read_claims():
            pipe = client.pipeline(transaction=False)
            for key in wanted:
                pipe.get(key)
                if with_ttl:
                    pipe.ttl(key)
            return pipe.execute(raise_on_error=True)

        replies = _call_with_retry(read_claims)
        per_key = 2 if with_ttl else 1
        for i, key in enumerate(wanted):
            raw = replies[i * per_key]
            if raw is None:
                continue  # expired between the scan and the read
            target = key[len(prefix) :]
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
                ttl_seconds = replies[i * per_key + 1]
                claim["ttl"] = float(ttl_seconds) if ttl_seconds >= 0 else None
            result[target] = claim
        return result
    except redis.exceptions.AuthenticationError as exc:
        raise _auth_failed(exc) from exc
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError) as exc:
        raise CoordinatorUnreachable("claims_for") from exc

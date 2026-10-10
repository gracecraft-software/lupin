"""Tests for the shared read-only-`gh`-lookup cache (issue #35).

Uses the real `redis-server` fixtures in `conftest.py` (`redis_port`/
`flush_redis`/`closed_port`), not a mock -- same convention as
test_slots_redis.py/test_machines.py.
"""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

import redis as redis_lib
import pytest

from lupin import gh_cache, machines

@pytest.fixture(autouse=True)
def isolated_fleet_config(monkeypatch, tmp_path):
    monkeypatch.setattr(machines, "DEFAULT_CONFIG_PATH", tmp_path / "fleet.json")


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def test_cache_hit_never_calls_fetch_fn(redis_port, flush_redis):
    key = f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open"
    _raw_client(redis_port).set(key, json.dumps({"data": [{"number": 1}]}))
    fetch = mock.Mock()

    data, error = gh_cache.cached_gh_json(
        "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
    )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_not_called()


def test_cached_none_is_a_hit_not_a_miss(redis_port, flush_redis):
    """A fetch that legitimately returns `(None, None)` -- e.g.
    `roadmap_cli._issue_state` on a deleted issue -- must still be cacheable,
    distinguishable from "nothing cached yet".
    """
    key = f"{gh_cache.PREFIX}gh-cache:acme/repo:issue-state:9"
    _raw_client(redis_port).set(key, json.dumps({"data": None}))
    fetch = mock.Mock()

    data, error = gh_cache.cached_gh_json(
        "acme", "repo", "issue-state:9", fetch, connection=_kw(redis_port)
    )

    assert data is None
    assert error is None
    fetch.assert_not_called()


def _record(name: str, state: str = "online") -> dict:
    return {"name": name, "state": state, "version": "1", "heartbeat": "1970-01-01T00:00:00Z"}


def test_non_canonical_machine_never_fetches_on_a_miss(redis_port, flush_redis):
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record(gh_cache.CANONICAL_GH_FETCHER)]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data is None
    assert "pihome" in error
    fetch.assert_not_called()


def test_non_canonical_machine_never_fetches_when_redis_is_down(closed_port):
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(closed_port)
        )

    assert data is None
    assert "jesus" in error
    fetch.assert_not_called()


@pytest.mark.parametrize("state", ["draining", "offline"])
def test_non_canonical_machine_fetches_when_the_pinned_fetcher_is_gone(
    redis_port, flush_redis, state
):
    """The pin has no holder: pihome is draining or offline, so nobody is
    maintaining the shared cache. jesus fetches and publishes instead of
    showing the whole fleet an empty roadmap -- the case that left jesus's
    own dashboard blank.
    """
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record(gh_cache.CANONICAL_GH_FETCHER, state)]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()
    raw = _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    assert json.loads(raw) == {"data": [{"number": 1}]}


def test_non_canonical_machine_fetches_when_the_pinned_fetcher_never_joined(
    redis_port, flush_redis
):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", return_value=[_record("ralpha")]):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()


def test_unreadable_registry_keeps_the_conservative_answer(redis_port, flush_redis):
    """A registry that cannot be read is not evidence the pinned fetcher is
    gone, so the miss stays a miss rather than turning into a fetch we
    cannot justify."""
    fetch = mock.Mock()
    with mock.patch.object(machines, "hostname", return_value="jesus"):
        with mock.patch.object(machines, "machines", side_effect=machines.CoordinatorUnreachable("x")):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )

    assert data is None
    assert "pihome" in error
    fetch.assert_not_called()


def test_canonical_fetcher_check_refuses_when_the_registry_is_partly_unreadable(redis_port, flush_redis):
    """The pinned fetcher's own record is corrupt. The check must read that as
    unknown and report the fetcher live, not absent."""
    _raw_client(redis_port).set(f"{machines.PREFIX}machine:{gh_cache.CANONICAL_GH_FETCHER}", "not json")

    assert gh_cache._canonical_fetcher_live(_kw(redis_port)) is True


def test_fallback_fetcher_serializes_on_the_shared_lock(redis_port, flush_redis):
    """Two machines that both fall back still fetch once: the second finds
    the first's published result after waiting on the lock."""
    from lupin import slots_redis

    held = slots_redis.acquire("gh-fetch/acme/repo", holder="other-machine", **_kw(redis_port))
    try:
        _raw_client(redis_port).set(
            f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open",
            json.dumps({"data": [{"number": 1}]}),
        )
        fetch = mock.Mock()
        with mock.patch.object(machines, "hostname", return_value="jesus"):
            with mock.patch.object(machines, "machines", return_value=[_record(gh_cache.CANONICAL_GH_FETCHER, "draining")]):
                data, error = gh_cache.cached_gh_json(
                    "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
                )
        assert data == [{"number": 1}]
        assert error is None
        fetch.assert_not_called()
    finally:
        slots_redis.release(held, **_kw(redis_port))


def test_canonical_machine_fetches_and_publishes_on_a_miss(redis_port, flush_redis):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()

    raw = _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    assert json.loads(raw) == {"data": [{"number": 1}]}


def test_canonical_machine_does_not_cache_a_failed_fetch(redis_port, flush_redis):
    fetch = mock.Mock(return_value=(None, "gh: rate limited"))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
        )

    assert error == "gh: rate limited"
    assert _raw_client(redis_port).get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open") is None


def test_canonical_machine_still_fetches_live_when_redis_is_down(closed_port):
    fetch = mock.Mock(return_value=([{"number": 1}], None))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        data, error = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch, connection=_kw(closed_port)
        )

    assert data == [{"number": 1}]
    assert error is None
    fetch.assert_called_once()


def test_canonical_machine_lock_contention_rereads_cache_instead_of_refetching(
    redis_port, flush_redis
):
    """Two `lupin` invocations on the canonical fetcher race for the same
    repo's lock. The second one should find the first's result already
    cached and skip its own fetch -- not run `gh` twice.
    """
    from lupin import slots_redis

    held = slots_redis.acquire("gh-fetch/acme/repo", holder="other-caller", **_kw(redis_port))
    try:
        _raw_client(redis_port).set(
            f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open",
            json.dumps({"data": [{"number": 1}]}),
        )
        fetch = mock.Mock()
        with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )
        assert data == [{"number": 1}]
        assert error is None
        fetch.assert_not_called()
    finally:
        slots_redis.release(held, **_kw(redis_port))


def test_canonical_machine_fetches_anyway_if_lock_busy_and_cache_still_cold(
    redis_port, flush_redis
):
    """If another holder has the lock and the cache is still empty (the
    other fetch hasn't landed yet), the canonical fetcher still answers its
    own caller rather than blocking indefinitely -- a duplicate `gh` call
    is wasted work, not a correctness problem.
    """
    from lupin import slots_redis

    held = slots_redis.acquire("gh-fetch/acme/repo", holder="other-caller", **_kw(redis_port))
    try:
        fetch = mock.Mock(return_value=([{"number": 2}], None))
        with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
            data, error = gh_cache.cached_gh_json(
                "acme", "repo", "issues:open", fetch, connection=_kw(redis_port)
            )
        assert data == [{"number": 2}]
        assert error is None
        fetch.assert_called_once()
    finally:
        slots_redis.release(held, **_kw(redis_port))


def test_concurrent_caller_that_waits_reuses_the_result_instead_of_refetching(
    redis_port, flush_redis
):
    """Real two-thread race against a real redis-server, not a held-lease
    stand-in: caller A acquires the lock and is mid-fetch (simulated by a
    brief sleep) when caller B starts and blocks inside
    `slots_redis.acquire`'s own poll loop. A finishes, writes the cache,
    and releases -- only then does B's `acquire()` return. B must re-check
    the cache at that point and reuse A's result, not call its own
    `fetch_fn`.

    The other lock-contention tests above pre-hold the lease for the whole
    test, so `acquire()` always raises `SlotFull` for them -- they never
    exercise a *successful* `acquire()` that happens after a wait, which is
    the normal (not the exceptional) case and the one the bug was in.
    """
    fetch_b = mock.Mock()
    a_holds_lock = threading.Event()

    def fetch_a():
        # Only reached once A's `acquire()` has already succeeded, so
        # setting this tells B it is safe to start -- B's own `acquire()`
        # is then guaranteed to find the lock held, not racing to get it
        # first. `cached_gh_json` does real work (a cache read, hostname
        # and redis checks) before it ever calls `acquire()`, so a plain
        # head-start sleep before starting B isn't enough to guarantee
        # ordering -- this event is.
        a_holds_lock.set()
        time.sleep(0.4)
        return [{"number": 1}], None

    results = {}

    def call_a():
        results["a"] = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch_a, connection=_kw(redis_port)
        )

    def call_b():
        a_holds_lock.wait(timeout=5)
        results["b"] = gh_cache.cached_gh_json(
            "acme", "repo", "issues:open", fetch_b, connection=_kw(redis_port)
        )

    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        thread_a = threading.Thread(target=call_a)
        thread_b = threading.Thread(target=call_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

    assert results["a"] == ([{"number": 1}], None)
    assert results["b"] == ([{"number": 1}], None)
    fetch_b.assert_not_called()


def test_different_cache_keys_for_the_same_repo_do_not_collide(redis_port, flush_redis):
    fetch_issues = mock.Mock(return_value=([{"number": 1}], None))
    fetch_deps = mock.Mock(return_value=({1: {"blockedBy": []}}, None))
    with mock.patch.object(machines, "hostname", return_value=gh_cache.CANONICAL_GH_FETCHER):
        gh_cache.cached_gh_json("acme", "repo", "issues:open", fetch_issues, connection=_kw(redis_port))
        gh_cache.cached_gh_json("acme", "repo", "dependencies", fetch_deps, connection=_kw(redis_port))

    client = _raw_client(redis_port)
    issues_raw = client.get(f"{gh_cache.PREFIX}gh-cache:acme/repo:issues:open")
    deps_raw = client.get(f"{gh_cache.PREFIX}gh-cache:acme/repo:dependencies")
    assert json.loads(issues_raw) == {"data": [{"number": 1}]}
    assert json.loads(deps_raw) == {"data": {"1": {"blockedBy": []}}}

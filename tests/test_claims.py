"""Tests for GitHub-issue claims (issue #6).

Uses the real `redis-server` fixtures in `conftest.py` -- `redis_port`,
`flush_redis`, `closed_port` -- shared with `test_slots_redis.py`.
"""

from __future__ import annotations

import time

import pytest
import redis as redis_lib

from lupin import cli, claims, slots


class _FakePipeline:
    """Queues commands and runs them inside MULTI/EXEC, as redis-py does by default."""

    def __init__(self, client):
        self._client = client
        self._queued = []

    def get(self, key):
        self._queued.append(("GET", key))
        return self

    def pttl(self, key):
        self._queued.append(("PTTL", key))
        return self

    def execute(self):
        self._client.commands.append("MULTI")
        results = []
        for name, key in self._queued:
            self._client.commands.append(name)
            results.append(self._client.values[key] if name == "GET" else 90_000)
        self._client.commands.append("EXEC")
        return results


class _FakeClient:
    """Records each command in order. Serves one scan result and its values."""

    def __init__(self, values):
        self.values = values
        self.commands = []

    def scan_iter(self, match):
        self.commands.append("SCAN")
        return iter(self.values)

    def get(self, key):
        self.commands.append("GET")
        return self.values[key]

    def pipeline(self):
        return _FakePipeline(self)


def test_claims_for_reads_only_the_value_by_default(monkeypatch):
    key = "lupin:v1:claim:gracecraft/lupin#6"
    fake = _FakeClient({key: '{"host": "h", "session": "s", "since": 1}'})
    monkeypatch.setattr(claims, "_client", lambda *a: fake)

    result = claims.claims_for(["gracecraft/lupin"])

    assert fake.commands == ["SCAN", "GET"]
    assert result == {"gracecraft/lupin#6": {"host": "h", "session": "s", "since": 1}}


def test_claims_for_reads_the_ttl_in_one_round_trip_when_asked(monkeypatch):
    key = "lupin:v1:claim:gracecraft/lupin#6"
    fake = _FakeClient({key: '{"host": "h", "session": "s", "since": 1}'})
    monkeypatch.setattr(claims, "_client", lambda *a: fake)

    result = claims.claims_for(["gracecraft/lupin"], with_ttl=True)

    assert fake.commands == ["SCAN", "MULTI", "GET", "PTTL", "EXEC"]
    assert result["gracecraft/lupin#6"]["ttl"] == 90.0


def test_claims_for_skips_a_claim_that_is_not_a_json_object(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "holder-a", **kw)
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:claim:gracecraft/lupin#8", "not json")
    raw.set("lupin:v1:claim:gracecraft/lupin#9", "[1, 2]")
    skipped = []

    result = claims.claims_for(["gracecraft/lupin"], with_ttl=True, skipped=skipped, **kw)

    assert set(result) == {"gracecraft/lupin#6"}
    assert sorted(skipped) == ["claim:gracecraft/lupin#8", "claim:gracecraft/lupin#9"]


@pytest.mark.parametrize("raw_value", ["not json", "[1, 2]"])
def test_claims_for_strict_raises_on_a_claim_that_is_not_a_json_object(redis_port, flush_redis, raw_value):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:claim:gracecraft/lupin#9", raw_value)

    with pytest.raises(ValueError):
        claims.claims_for(["gracecraft/lupin"], strict=True, **kw)


def test_claim_succeeds_and_is_visible_in_redis(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "host-a:session-1", **kw)

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    value = raw.get("lupin:v1:claim:gracecraft/lupin#6")
    assert value is not None
    assert '"session": "host-a:session-1"' in value or "session" in value


def test_claim_is_idempotent_for_the_same_holder(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "same-holder", **kw)
    # A retry by the same holder renews rather than raising ClaimHeld.
    claims.claim("gracecraft/lupin#6", "same-holder", **kw)


def test_claim_fails_when_already_held_by_someone_else(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "first-holder", **kw)
    with pytest.raises(claims.ClaimHeld):
        claims.claim("gracecraft/lupin#6", "second-holder", **kw)


def test_renew_claim_fails_for_non_holder(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "real-holder", **kw)
    assert claims.renew_claim("gracecraft/lupin#6", "impostor", **kw) is False


def test_renew_claim_succeeds_for_holder(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "real-holder", ttl=0.2, **kw)
    assert claims.renew_claim("gracecraft/lupin#6", "real-holder", ttl=10, **kw) is True
    # Still there after the original (short) TTL would have expired.
    time.sleep(0.3)
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    assert raw.get("lupin:v1:claim:gracecraft/lupin#6") is not None


def test_release_claim_fails_for_non_holder(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "real-holder", **kw)
    assert claims.release_claim("gracecraft/lupin#6", "impostor", **kw) is False
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    assert raw.get("lupin:v1:claim:gracecraft/lupin#6") is not None


def test_release_claim_succeeds_for_holder(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "real-holder", **kw)
    assert claims.release_claim("gracecraft/lupin#6", "real-holder", **kw) is True
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    assert raw.get("lupin:v1:claim:gracecraft/lupin#6") is None
    # Releasing twice is not an error -- already gone is the end state anyway.
    assert claims.release_claim("gracecraft/lupin#6", "real-holder", **kw) is False


def test_claim_expires_after_its_ttl(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "first-holder", ttl=0.05, **kw)
    time.sleep(0.2)
    # Once expired, a different holder can claim it clean.
    claims.claim("gracecraft/lupin#6", "second-holder", **kw)
    assert claims.renew_claim("gracecraft/lupin#6", "first-holder", **kw) is False


def test_claims_for_across_repos_mixed_claimed_and_unclaimed(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "holder-a", **kw)
    claims.claim("gracecraft/nix#212", "holder-b", **kw)

    result = claims.claims_for(["gracecraft/lupin", "gracecraft/nix", "gracecraft/other"], **kw)

    assert set(result) == {"gracecraft/lupin#6", "gracecraft/nix#212"}
    assert result["gracecraft/lupin#6"]["session"] == "holder-a"
    assert result["gracecraft/nix#212"]["session"] == "holder-b"


def test_claims_for_reports_seconds_left_only_when_asked(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "holder-a", ttl=100, **kw)

    plain = claims.claims_for(["gracecraft/lupin"], **kw)
    timed = claims.claims_for(["gracecraft/lupin"], with_ttl=True, **kw)

    assert "ttl" not in plain["gracecraft/lupin#6"]
    assert 90 < timed["gracecraft/lupin#6"]["ttl"] <= 100
    assert timed["gracecraft/lupin#6"]["session"] == "holder-a"


def test_claims_for_ttl_is_none_when_the_key_never_expires(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:claim:gracecraft/lupin#7", '{"host": "h", "session": "s", "since": 1}')

    result = claims.claims_for(["gracecraft/lupin"], with_ttl=True, **kw)

    assert result["gracecraft/lupin#7"]["ttl"] is None


def test_claims_for_only_returns_repos_asked_for(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "holder-a", **kw)
    claims.claim("gracecraft/nix#212", "holder-b", **kw)

    result = claims.claims_for(["gracecraft/lupin"], **kw)
    assert set(result) == {"gracecraft/lupin#6"}


def test_unreachable_redis_raises_for_every_claim_call(closed_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(slots.CoordinatorUnreachable):
        claims.claim("gracecraft/lupin#6", "a", **kw)
    with pytest.raises(slots.CoordinatorUnreachable):
        claims.renew_claim("gracecraft/lupin#6", "a", **kw)
    with pytest.raises(slots.CoordinatorUnreachable):
        claims.release_claim("gracecraft/lupin#6", "a", **kw)
    with pytest.raises(slots.CoordinatorUnreachable):
        claims.claims_for(["gracecraft/lupin"], **kw)


def test_bad_target_format_raises_value_error(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    with pytest.raises(ValueError):
        claims.claim("not-a-valid-target", "a", **kw)


def test_cli_claim_renew_release_exit_codes(redis_port, flush_redis, capsys, monkeypatch):
    # This sandbox's shell sets LUPIN_REDIS_USERNAME for the real deployment
    # Redis; clear it so the CLI's --redis-username default doesn't try to
    # auth against the test's plain (no-ACL) redis-server fixture.
    monkeypatch.delenv("LUPIN_REDIS_USERNAME", raising=False)
    monkeypatch.delenv("LUPIN_REDIS_PASSWORD", raising=False)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]

    code = cli.main(["claim", "gracecraft/lupin#6", "--holder", "real-holder", *common])
    capsys.readouterr()
    assert code == 0

    code = cli.main(["claim", "gracecraft/lupin#6", "--holder", "other-holder", *common])
    capsys.readouterr()
    assert code == 2

    code = cli.main(["renew-claim", "gracecraft/lupin#6", "--holder", "impostor", *common])
    capsys.readouterr()
    assert code == 1

    code = cli.main(["renew-claim", "gracecraft/lupin#6", "--holder", "real-holder", *common])
    capsys.readouterr()
    assert code == 0

    code = cli.main(["release-claim", "gracecraft/lupin#6", "--holder", "impostor", *common])
    capsys.readouterr()
    assert code == 1

    code = cli.main(["release-claim", "gracecraft/lupin#6", "--holder", "real-holder", *common])
    captured = capsys.readouterr()
    assert code == 0, captured.err


def test_cli_claim_unreachable_redis_exits_3(closed_port, capsys):
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(closed_port)]
    code = cli.main(["claim", "gracecraft/lupin#6", "--holder", "a", *common])
    captured = capsys.readouterr()
    assert code == 3
    assert "cannot reach the redis coordinator" in captured.err

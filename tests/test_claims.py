"""Tests for GitHub-issue claims (issue #6).

Uses the real `redis-server` fixtures in `conftest.py` -- `redis_port`,
`flush_redis`, `closed_port` -- shared with `test_slots_redis.py`.
"""

from __future__ import annotations

import json
import time

import pytest
import redis as redis_lib

from lupin import claim_labels, cli, claims, slots


@pytest.fixture(autouse=True)
def _disable_github_claim_labels(monkeypatch):
    monkeypatch.setattr(claim_labels, "add_claimed_label", lambda target: None)
    monkeypatch.setattr(claim_labels, "remove_claimed_label", lambda target: None)


class _FakePipeline:
    """Queues commands and runs them like redis-py."""

    def __init__(self, client, transaction=True):
        self._client = client
        self._transaction = transaction
        self._queued = []

    def get(self, key):
        self._queued.append(("GET", key))
        return self

    def ttl(self, key):
        self._queued.append(("TTL", key))
        return self

    def execute(self, **kwargs):
        if self._transaction:
            self._client.commands.append("MULTI")
        results = []
        for name, key in self._queued:
            self._client.commands.append(name)
            results.append(self._client.values[key] if name == "GET" else 90)
        if self._transaction:
            self._client.commands.append("EXEC")
        return results


class _FakeClient:
    """Records each command in order. Serves one scan result and its values."""

    def __init__(self, values):
        self.values = values
        self.commands = []

    def scan_iter(self, match, count=None):
        self.commands.append("SCAN")
        return iter(self.values)

    def get(self, key):
        self.commands.append("GET")
        return self.values[key]

    def pipeline(self, transaction=True):
        return _FakePipeline(self, transaction)


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

    assert fake.commands == ["SCAN", "GET", "TTL"]
    assert result["gracecraft/lupin#6"]["ttl"] == 90.0


def test_claims_for_works_when_the_acl_denies_multi(auth_redis_port, no_multi_kw):
    admin = {"redis_host": "127.0.0.1", "redis_port": auth_redis_port, "redis_password": "test-pass"}
    claims.claim("gracecraft/lupin#6", "holder-a", **admin)
    connection = {"redis_host": "127.0.0.1", "redis_port": auth_redis_port, **no_multi_kw}

    result = claims.claims_for(["gracecraft/lupin"], with_ttl=True, **connection)

    assert result["gracecraft/lupin#6"]["session"] == "holder-a"
    assert result["gracecraft/lupin#6"]["ttl"] > 0


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


def test_claim_generation_rotates_on_retry_and_renewal(
    redis_port, flush_redis
):
    target = "gracecraft/lupin#6"
    key = f"lupin:v1:claim:{target}"
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)

    claims.claim(target, "same-holder", generation="first", **kw)
    first_generation = json.loads(raw.get(key))["generation"]
    assert first_generation == "first"
    assert "generation" not in claims.claims_for(["gracecraft/lupin"], **kw)[target]

    assert claims.renew_claim(target, "same-holder", **kw) is True
    assert json.loads(raw.get(key))["generation"] != first_generation
    assert "generation" not in claims.claims_for(["gracecraft/lupin"], **kw)[target]

    assert claims.claim(target, "same-holder", generation="second", **kw) is False
    assert json.loads(raw.get(key))["generation"] == "second"

    legacy_target = "gracecraft/lupin#7"
    legacy_key = f"lupin:v1:claim:{legacy_target}"
    raw.set(
        legacy_key,
        json.dumps({"host": "old-host", "session": "legacy-holder", "since": 1}),
        ex=30,
    )
    assert claims.renew_claim(legacy_target, "legacy-holder", **kw) is True
    assert "generation" not in json.loads(raw.get(legacy_key))

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


def test_claim_held_hides_generation_and_shows_safe_holder_fields(
    redis_port, flush_redis
):
    target = "gracecraft/lupin#6"
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set(
        f"{claims.PREFIX}claim:{target}",
        json.dumps(
            {
                "host": "safe-host",
                "session": "safe-holder",
                "since": 123,
                "generation": "private-generation",
            }
        ),
    )

    with pytest.raises(claims.ClaimHeld) as exc:
        claims.claim(target, "other-holder", **kw)

    message = str(exc.value)
    assert "private-generation" not in message
    assert "host='safe-host'" in message
    assert "session='safe-holder'" in message
    assert "since=123" in message


@pytest.mark.parametrize("raw_value", ["not-json", "[1, 2]"])
def test_claim_held_uses_unknown_holder_for_malformed_values(
    redis_port, flush_redis, raw_value
):
    target = "gracecraft/lupin#6"
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set(f"{claims.PREFIX}claim:{target}", raw_value)

    with pytest.raises(claims.ClaimHeld) as exc:
        claims.claim(target, "other-holder", **kw)

    assert str(exc.value).endswith("unknown holder")


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


def test_claims_for_raises_when_a_claim_key_has_the_wrong_type(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.rpush("lupin:v1:claim:gracecraft/lupin#6", "not a claim")

    # A wrong-type key is a bug, not an empty claim. It must raise, not vanish.
    with pytest.raises(redis_lib.exceptions.ResponseError):
        claims.claims_for(["gracecraft/lupin"], **kw)


def test_claims_for_skips_a_claim_that_expired_after_the_scan(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "holder-a", **kw)
    claims.claim("gracecraft/lupin#7", "holder-b", **kw)
    real = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)

    class ExpireAfterScan:
        """Scans as usual, then deletes #6 before the read, as if its TTL ran out."""

        def __getattr__(self, name):
            return getattr(real, name)

        def scan_iter(self, *args, **kwargs):
            keys = list(real.scan_iter(*args, **kwargs))
            real.delete("lupin:v1:claim:gracecraft/lupin#6")
            return iter(keys)

    monkeypatch.setattr(claims, "_client", lambda *_args: ExpireAfterScan())

    result = claims.claims_for(["gracecraft/lupin"], **kw)

    assert set(result) == {"gracecraft/lupin#7"}


def test_claims_for_retry_reads_every_key(redis_port, flush_redis, monkeypatch):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("gracecraft/lupin#6", "holder-a", **kw)
    claims.claim("gracecraft/lupin#7", "holder-b", **kw)
    real = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    dropped = []

    class DropFirstExecute:
        """Runs the first pipeline, then drops the reply as if the link broke."""

        def __getattr__(self, name):
            return getattr(real, name)

        def pipeline(self, **kwargs):
            pipe = real.pipeline(**kwargs)

            class Pipe:
                def __getattr__(self, name):
                    return getattr(pipe, name)

                def execute(self, *args, **kwargs):
                    replies = pipe.execute(*args, **kwargs)
                    if not dropped:
                        dropped.append(True)
                        raise redis_lib.exceptions.ConnectionError("reply lost")
                    return replies

            return Pipe()

    monkeypatch.setattr(claims, "_client", lambda *_args: DropFirstExecute())

    result = claims.claims_for(["gracecraft/lupin"], **kw)

    assert dropped == [True]
    assert set(result) == {"gracecraft/lupin#6", "gracecraft/lupin#7"}


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


# Issue #156: a loop renews its own claims by holder.


def test_renew_holder_claims_renews_only_that_holders_claims(redis_port, flush_redis):
    kw = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    claims.claim("acme/repo#1", "loop-a", ttl=1.0, **kw)
    claims.claim("acme/repo#2", "loop-b", ttl=1.0, **kw)

    renewed = claims.renew_holder_claims("loop-a", ttl=60.0, **kw)

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    assert renewed == ["acme/repo#1"]
    assert raw.pttl(f"{claims.PREFIX}claim:acme/repo#1") > 50_000
    assert raw.pttl(f"{claims.PREFIX}claim:acme/repo#2") <= 1_000


def test_renew_holder_claims_works_with_only_the_fleet_acl_commands(auth_redis_port):
    """The fleet user has only the commands in docs/redis-schema.md: no MULTI, no WATCH."""
    admin = redis_lib.Redis(host="127.0.0.1", port=auth_redis_port, password="test-pass")
    admin.flushall()
    fleet = (
        "PING GET SET DEL EXPIRE PEXPIRE TTL ZADD ZREM ZSCORE ZCARD ZRANGE ZREMRANGEBYSCORE "
        "XADD XRANGE XREVRANGE SCAN EVAL EVALSHA SCRIPT|LOAD SCRIPT|EXISTS"
    ).split()
    admin.execute_command(
        "ACL", "SETUSER", "fleet", "reset", "on", ">fleet-pw", "~lupin:*",
        *[f"+{name.lower()}" for name in fleet],
    )
    kw = {
        "redis_host": "127.0.0.1", "redis_port": auth_redis_port,
        "redis_username": "fleet", "redis_password": "fleet-pw",
    }
    try:
        claims.claim("acme/repo#3", "loop-a", ttl=1.0, **kw)
        assert claims.renew_holder_claims("loop-a", ttl=60.0, **kw) == ["acme/repo#3"]
        assert admin.pttl(f"{claims.PREFIX}claim:acme/repo#3") > 50_000
    finally:
        admin.execute_command("ACL", "DELUSER", "fleet")
        admin.flushall()


def test_claim_commands_take_the_holder_from_lupin_claim_holder(
    redis_port, flush_redis, monkeypatch
):
    monkeypatch.setenv("LUPIN_CLAIM_HOLDER", "loop-a")
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]

    assert cli.main(["claim", "acme/repo#4", *common]) == 0
    held = claims.claims_for(["acme/repo"], redis_host="127.0.0.1", redis_port=redis_port)
    assert held["acme/repo#4"]["session"] == "loop-a"
    assert cli.main(["renew-claim", "acme/repo#4", *common]) == 0
    assert cli.main(["release-claim", "acme/repo#4", *common]) == 0


def test_claim_without_holder_or_lupin_claim_holder_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["claim", "acme/repo#4"])
    assert exc.value.code == 2
    assert "--holder" in capsys.readouterr().err


def test_cli_claim_label_failure_exits_one_and_rolls_back(
    redis_port, flush_redis, capsys, monkeypatch
):
    def fail(_target):
        raise claim_labels.GitHubAPIError(403, "write denied")

    monkeypatch.setattr(claim_labels, "add_claimed_label", fail)
    common = ["--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]

    code = cli.main(["claim", "acme/repo#4", "--holder", "worker", *common])
    captured = capsys.readouterr()

    assert code == 1
    assert "the Redis claim was released" in captured.err
    assert claims.claims_for(
        ["acme/repo"], redis_host="127.0.0.1", redis_port=redis_port
    ) == {}

"""Tests for `lupin place` (issue #9).

Redis-backed tests use the same real `redis-server` fixtures as
`test_machines.py` (`redis_port`/`flush_redis`/`closed_port` from
`conftest.py`), not a mock -- same rule as the rest of this backend's
tests. Machine records are written directly (bypassing `join`/`heartbeat`,
which always register *this* process's own hostname), the same approach
`test_machines.py`'s `_write_raw_record` uses, extended with the
`slots`/`quota` fields `place` actually reads.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import redis as redis_lib

from lupin import cli, machines, place, quota


_AMBIENT_ENV_VARS = (
    "LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME",
    "LUPIN_REDIS_PASSWORD", "LUPIN_FLEET_CONFIG", "LUPIN_BACKEND",
)


@pytest.fixture
def clean_fleet_env(monkeypatch):
    """Same reason `test_machines.py` needs this: a real fleet host sets
    `$LUPIN_REDIS_*` for its own Redis, which would otherwise leak into
    these tests' throwaway `redis-server` fixture and break auth.
    """
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_live_quota(monkeypatch):
    """`route()` (issue #36) reads live quota by default -- every
    `place.place()` call below would otherwise shell out to the real `omp
    usage` command on every test. None of these tests are about quota
    pacing itself (that's `test_pace.py`/`test_route.py`); patch `route()`'s
    own quota source so it behaves exactly like passing `quota_rows=[]`:
    no data, no effect on the routed model.
    """
    monkeypatch.setattr(place.route_mod.quota_mod, "quota_usage", lambda: [])


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _old_stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_machine(
    redis_port,
    name,
    *,
    state="online",
    version="0.9.2",
    heartbeat=None,
    slots=None,
    quota=None,
):
    record = {
        "version": version,
        "heartbeat": heartbeat or machines._now_iso(),
        "state": state,
        "slots": slots or {},
        "providers": [],
        "quota": quota or {},
    }
    client = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    client.set(f"{machines.PREFIX}machine:{name}", json.dumps(record))


_CLAUDE_QUOTA = {"claude": {"pct_left": 62.0, "resets_at": 1_800_000_000_000, "source": "test"}}


# --- pure helpers, no redis ---


def test_provider_for_model_maps_claude_models():
    assert place.provider_for_model("sonnet") == "claude"
    assert place.provider_for_model("opus") == "claude"
    # fable runs in Claude Code: the 2026-10-08 fetch-models snapshot lists
    # claude-fable-5-1 under the claude subscription, and the coding index
    # labels it "Claude Code | Fable 5.1". Mapping it to opencode-go gated
    # the fleet's most expensive model on the wrong account's quota.
    assert place.provider_for_model("fable") == "claude"


def test_provider_for_model_maps_subscription_prefixed_models():
    assert place.provider_for_model("opencode-go/glm-5.3") == "opencode-go"
    assert place.provider_for_model("openai/gpt-6-luna") == "openai"


def test_provider_for_model_maps_bmo_and_local_prefixes():
    assert place.provider_for_model("bmo:qwen3.8-flash-next") == "bmo"
    assert place.provider_for_model("local:deepseek-v4-flash-0731") == "local"


def test_provider_for_model_falls_back_to_the_model_name():
    assert place.provider_for_model("some-future-model") == "some-future-model"


def test_format_duration():
    assert place.format_duration(11400) == "3h 10m"
    assert place.format_duration(2700) == "45m"
    assert place.format_duration(7200) == "2h"
    assert place.format_duration(0) == "now"
    assert place.format_duration(-5) == "now"


def test_resolve_task_free_text_is_classified_as_is():
    issue, label = place._resolve_task("retry backoff")
    assert issue == {"title": "retry backoff"}
    assert label == "retry backoff"


def test_resolve_task_issue_number_uses_gh_title(monkeypatch):
    monkeypatch.setattr(
        place, "_fetch_issue", lambda number, connection=None: ({"title": "retry backoff"}, None)
    )
    issue, label = place._resolve_task("#418")
    assert issue == {"title": "retry backoff"}
    assert label == "#418 retry backoff"


def test_resolve_task_issue_number_falls_back_when_gh_fails(monkeypatch):
    monkeypatch.setattr(
        place, "_fetch_issue", lambda number, connection=None: ({}, "gh: not found")
    )
    issue, label = place._resolve_task("418")
    assert issue == {}
    assert label == "#418"


# --- quest_focus_for: the hook `place()` fills in for issue #12 ---


def test_quest_focus_for_returns_none_for_free_text():
    assert place.quest_focus_for(None, {}) is None


def test_quest_focus_for_returns_none_when_task_is_in_no_quest(monkeypatch):
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos, **kw: ([], []))
    assert place.quest_focus_for("418", {}) is None


def test_quest_focus_for_returns_none_when_quest_has_no_focus(monkeypatch):
    quests = [{"name": "session-rewrite", "tasks": [{"number": 418, "title": "t", "done": False}]}]
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos, **kw: (quests, []))
    monkeypatch.setattr(place.quest_mod, "read_focus", lambda name, **kw: None)
    assert place.quest_focus_for("418", {}) is None


def test_quest_focus_for_returns_the_quests_focus_machine(monkeypatch):
    quests = [{"name": "session-rewrite", "tasks": [{"number": 418, "title": "t", "done": False}]}]
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos, **kw: (quests, []))
    monkeypatch.setattr(
        place.quest_mod, "read_focus", lambda name, **kw: {"machine": "mac-studio"} if name == "session-rewrite" else None
    )
    assert place.quest_focus_for("418", {}) == "mac-studio"


# --- place(), against a real (throwaway) redis-server ---


def test_place_picks_the_machine_with_more_free_slots(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 2, "max": 4}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "mini-2", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    # No labels on this synthetic issue -> classify() falls back to
    # size-? -> tier2 -> "opus" (model-tiers.json's "coding" row).
    assert result["model"] == "opus"
    assert result["provider"] == "claude"
    assert result["pick"] == "mac-studio"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["mac-studio"]["result"] == "pick"
    assert by_name["mini-2"]["result"] == "fewer slots free"


def test_place_ranks_draining_below_online_and_labels_it_draining(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", state="online", slots={"bmo": {"used": 2, "max": 4}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "jesus", state="draining", slots={"bmo": {"used": 0, "max": 2}}, quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["jesus"]["result"] == "draining"


def test_place_has_no_pick_when_every_matching_machine_is_draining(redis_port, flush_redis):
    _write_machine(redis_port, "jesus", state="draining", slots={"bmo": {"used": 0, "max": 2}}, quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] is None
    assert result["candidates"][0]["result"] == "draining"


def test_place_raises_on_an_unreadable_machine_record_by_default(redis_port, flush_redis):
    """`place` callers act on the pick. A pick made from a partial list is
    not safe, so the default must raise."""
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 0, "max": 2}}, quota=_CLAUDE_QUOTA)
    redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True).set(
        f"{machines.PREFIX}machine:old-box", "not json"
    )

    with pytest.raises(json.JSONDecodeError):
        place.place("retry backoff", _kw(redis_port))


def test_place_skips_machines_running_a_different_provider(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "codex-box", quota={"openai": {"pct_left": 80.0, "resets_at": None, "source": "test"}})

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    assert len(result["candidates"]) == 1
    assert result["skipped"]["other_provider"] == 1


def test_place_skips_offline_machines(redis_port, flush_redis):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    _write_machine(
        redis_port, "ghost", quota=_CLAUDE_QUOTA,
        heartbeat="2000-01-01T00:00:00Z",
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    assert len(result["candidates"]) == 1
    assert result["skipped"]["offline"] == 1


def test_place_counts_an_offline_machine_without_a_reading_as_offline(
    redis_port, flush_redis, monkeypatch
):
    # Offline is checked before a missing reading, as on main. The machine
    # has no key and is offline: it counts as offline, not as no_key.
    monkeypatch.setattr(
        place.route_mod, "route",
        lambda *a, **kw: {"model": "opencode-go/glm-5.3", "effort": "high"},
    )
    _write_machine(
        redis_port, "ghost-no-key",
        heartbeat="2000-01-01T00:00:00Z",
        quota={"opencode-go": {
            "pct_left": None, "resets_at": None, "source": "test",
            "note": quota.NO_OPENCODE_GO_KEY,
        }},
    )
    _write_machine(
        redis_port, "opencode-box",
        quota={"opencode-go": {"pct_left": 100.0, "resets_at": None, "source": "test"}},
    )

    result = place.place("fix the retry backoff", _kw(redis_port))

    assert result["pick"] == "opencode-box"
    assert [c["name"] for c in result["candidates"]] == ["opencode-box"]
    assert result["skipped"] == {"offline": 1, "other_provider": 0, "no_key": 0, "read_failed": 0}


def test_place_breaks_ties_on_heartbeat_freshness(redis_port, flush_redis):
    fresh = machines._now_iso()
    stale = _old_stamp(90)  # older, but still under OFFLINE_AFTER (120s)
    _write_machine(redis_port, "fresher", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA, heartbeat=fresh)
    _write_machine(redis_port, "staler", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA, heartbeat=stale)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "fresher"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["staler"]["result"] == "staler heartbeat"


def test_place_prefers_the_quest_focus_machine_over_more_free_slots(redis_port, flush_redis, monkeypatch):
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 1, "max": 2}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "mini-2", slots={"bmo": {"used": 0, "max": 4}}, quota=_CLAUDE_QUOTA)
    monkeypatch.setattr(
        place, "_fetch_issue", lambda number, connection=None: ({"title": "retry backoff"}, None)
    )
    quests = [{"name": "session-rewrite", "tasks": [{"number": 418, "title": "t", "done": False}]}]
    monkeypatch.setattr(place.quest_mod, "load_quests", lambda repos, **kw: (quests, []))
    monkeypatch.setattr(place.quest_mod, "read_focus", lambda name, **kw: {"machine": "mac-studio"})

    result = place.place("#418", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    by_name = {c["name"]: c for c in result["candidates"]}
    assert by_name["mac-studio"]["quest_focus"] == "mac-studio"
    assert by_name["mini-2"]["result"] == "not quest focus"


def test_place_quota_header_prefers_a_real_reading_over_unavailable(redis_port, flush_redis):
    _write_machine(
        redis_port, "no-data", quota={"claude": {"pct_left": None, "resets_at": None, "source": "test"}}
    )
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["quota"] == _CLAUDE_QUOTA["claude"]


# --- place(): passing through route()'s wait/downgrade (issue #36) ---
#
# Quota exhaustion is route()'s decision now (its own pacing, tested in
# test_pace.py/test_route.py) -- these tests only confirm place() reports
# whatever route() decided, without re-deriving it from machine records.


def test_place_reports_a_wait_without_filtering_machines(redis_port, flush_redis, monkeypatch):
    """route() found every option blocked -- place() reports "no pick,
    wait" straight away. A blocked provider is blocked for every machine
    alike, so there is nothing to gain by still ranking them.
    """
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    monkeypatch.setattr(
        place.route_mod, "route",
        lambda *a, **kw: {"model": "opus", "effort": "high", "wait_seconds": 295.0},
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["model"] == "opus"
    assert result["provider"] == "claude"
    assert result["pick"] is None
    assert result["downgraded_from"] is None
    assert result["wait_seconds"] == 295.0
    assert result["candidates"] == []
    assert result["skipped"] == {"offline": 0, "other_provider": 0, "no_key": 0, "read_failed": 0}

def test_place_does_not_select_machine_for_unknown_quota_reset(
    redis_port, flush_redis, monkeypatch
):
    """A quota block without a reset is still a deferral, not a machine pick."""
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    monkeypatch.setattr(
        place.route_mod,
        "route",
        lambda *a, **kw: {
            "model": "opus",
            "effort": "high",
            "wait_seconds": None,
            "reason": "all allowed models are blocked by quota",
        },
    )

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] is None
    assert result["candidates"] == []
    assert result["wait_seconds"] is None
    assert result["reason"] == "all allowed models are blocked by quota"


def test_cli_place_reports_unknown_quota_reset_reason(
    redis_port, flush_redis, tmp_path, capsys, clean_fleet_env, monkeypatch
):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    monkeypatch.setattr(
        place.route_mod,
        "route",
        lambda *a, **kw: {
            "model": "opus",
            "effort": "high",
            "wait_seconds": None,
            "reason": "all allowed models are blocked by quota",
        },
    )
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    assert cli.main(["place", "retry backoff", *common]) == 2
    plain = capsys.readouterr()
    assert "all allowed models are blocked by quota" in plain.err
    assert "no online machine" not in plain.err

    assert cli.main(["place", "retry backoff", "--explain", *common]) == 2
    explain = capsys.readouterr()
    assert "all allowed models are blocked by quota" in explain.out

    assert cli.main(["place", "retry backoff", "--json", *common]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "all allowed models are blocked by quota"

def test_place_reports_routes_downgrade_and_ranks_the_new_provider(redis_port, flush_redis, monkeypatch):
    """route() already moved off a blocked pick onto another provider --
    place() reports that change and ranks machines against the new
    provider, same as any other routed provider.
    """
    _write_machine(redis_port, "claude-backup", quota=_CLAUDE_QUOTA)
    monkeypatch.setattr(
        place.route_mod, "route",
        lambda *a, **kw: {
            "model": "sonnet",
            "effort": "medium",
            "downgraded_from": {"model": "bmo:qwen3.8-flash-next", "effort": "low"},
        },
    )

    result = place.place("small bmo task", _kw(redis_port))

    assert result["model"] == "sonnet"
    assert result["provider"] == "claude"
    assert result["downgraded_from"] == {"model": "bmo:qwen3.8-flash-next", "effort": "low"}
    assert result["wait_seconds"] is None
    assert result["pick"] == "claude-backup"


def test_place_normal_pick_has_no_wait_or_downgrade_fields(redis_port, flush_redis):
    """A normal placement picks an available machine without a wait or downgrade."""
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)

    result = place.place("retry backoff", _kw(redis_port))

    assert result["pick"] == "mac-studio"
    assert result["wait_seconds"] is None
    assert result["downgraded_from"] is None
    assert result["skipped"] == {"offline": 0, "other_provider": 0, "no_key": 0, "read_failed": 0}


def test_place_skips_a_machine_that_cannot_read_the_providers_quota(
    redis_port, flush_redis, monkeypatch
):
    """`quota.snapshot()` writes an entry for every provider on every
    heartbeat, so "has an entry" proves nothing -- every machine has all
    four. A reading with a real percentage is what proves the machine has
    that provider's credentials, so an entry with none is skipped: sending
    an opencode-go model to a machine without the key cannot work.
    """
    monkeypatch.setattr(
        place.route_mod, "route",
        lambda *a, **kw: {"model": "opencode-go/glm-5.3", "effort": "high"},
    )
    _write_machine(
        redis_port, "no-opencode-key",
        quota={"opencode-go": {
            "pct_left": None, "resets_at": None, "source": "test",
            "note": quota.NO_OPENCODE_GO_KEY,
        }},
    )
    _write_machine(
        redis_port, "opencode-box",
        quota={"opencode-go": {"pct_left": 100.0, "resets_at": None, "source": "test"}},
    )

    result = place.place("fix the retry backoff", _kw(redis_port))

    assert result["model"] == "opencode-go/glm-5.3"
    assert result["provider"] == "opencode-go"
    assert result["pick"] == "opencode-box"
    assert [c["name"] for c in result["candidates"]] == ["opencode-box"]
    assert result["skipped"] == {"offline": 0, "other_provider": 0, "no_key": 1, "read_failed": 0}


def test_place_counts_a_read_failure_apart_from_a_missing_key(
    redis_port, flush_redis, monkeypatch
):
    monkeypatch.setattr(
        place.route_mod, "route",
        lambda *a, **kw: {"model": "opencode-go/glm-5.3", "effort": "high"},
    )
    _write_machine(
        redis_port, "flaky",
        quota={"opencode-go": {
            "pct_left": None, "resets_at": None, "source": "test",
            "note": "quota unavailable (URLError)",
        }},
    )

    result = place.place("fix the retry backoff", _kw(redis_port))

    assert result["provider"] == "opencode-go"
    assert result["pick"] is None
    assert result["skipped"] == {"offline": 0, "other_provider": 0, "no_key": 0, "read_failed": 1}


def test_place_picks_a_machine_for_bmo_without_a_quota_row(redis_port, flush_redis, monkeypatch):
    """`bmo` is local inference and has no cloud quota row, so a prose task
    routed to it must not be filtered out for lacking one. Only `offline`
    skips a machine here; both online machines are candidates.
    """
    monkeypatch.setattr(place.classify_mod, "classify", lambda *a, **kw: ("prose", "size-s"))
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "claude-only")

    result = place.place("write a haiku about retries", _kw(redis_port))

    assert result["model"].startswith("bmo:")
    assert result["provider"] == "bmo"
    assert result["pick"] is not None
    assert result["pick"] in {"mac-studio", "claude-only"}
    assert result["skipped"] == {"offline": 0, "other_provider": 0, "no_key": 0, "read_failed": 0}


def test_place_size_m_coding_with_readable_quota_picks_glm_on_that_machine(
    redis_port, flush_redis, monkeypatch
):
    # Issue #99 check 1, offline substitute for the live `place` run.
    # Real route(). Live quota is patched to two accounts with low use.
    # Expects glm-5.3 high, placed on the machine that reads opencode-go quota.
    reset_ms = time.time() * 1000 + 2 * 24 * 60 * 60 * 1000
    monkeypatch.setattr(place.classify_mod, "classify", lambda *a, **kw: ("coding", "size-m"))
    monkeypatch.setattr(
        place.route_mod.quota_mod, "quota_usage",
        lambda: [
            {"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS,
             "used_pct": 10, "resets_at": reset_ms},
            {"provider": "opencode-go", "duration": quota.QuotaDuration.FIVE_HOURS,
             "used_pct": 10, "resets_at": reset_ms},
        ],
    )
    _write_machine(
        redis_port, "opencode-box",
        quota={"opencode-go": {"pct_left": 90.0, "resets_at": None, "source": "test"}},
    )

    result = place.place("fix the retry backoff", _kw(redis_port))

    assert result["model"] == "opencode-go/glm-5.3"
    assert result["effort"] == "high"
    assert result["provider"] == "opencode-go"
    assert result["pick"] == "opencode-box"


def test_place_raises_coordinator_unreachable(closed_port):
    with pytest.raises(place.CoordinatorUnreachable):
        place.place("retry backoff", {"redis_host": "127.0.0.1", "redis_port": closed_port})


# --- cli wiring ---


def test_cli_place_prints_selected_machine(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", *common])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "mac-studio"


def test_cli_place_no_pick_exits_2(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", *common])
    captured = capsys.readouterr()

    assert code == 2
    assert "no online machine" in captured.err


def test_cli_place_explain_lists_candidates_and_skip_footer(
    redis_port, flush_redis, tmp_path, capsys, clean_fleet_env
):
    _write_machine(redis_port, "mac-studio", slots={"bmo": {"used": 2, "max": 4}}, quota=_CLAUDE_QUOTA)
    _write_machine(redis_port, "codex-box", quota={"openai": {"pct_left": 80.0, "resets_at": None, "source": "test"}})
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--explain", *common])
    captured = capsys.readouterr()

    assert code == 0
    assert "mac-studio" in captured.out
    assert "pick" in captured.out
    assert "1 machine(s) skipped" in captured.out
    assert "different provider" in captured.out


def test_cli_place_explain_names_no_key_and_read_failed_apart(
    redis_port, flush_redis, tmp_path, capsys, clean_fleet_env, monkeypatch
):
    monkeypatch.setattr(
        place.route_mod, "route",
        lambda *a, **kw: {"model": "sonnet", "effort": "high"},
    )
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    _write_machine(
        redis_port, "keyless",
        quota={"claude": {
            "pct_left": None, "resets_at": None, "source": "test",
            "note": quota.NO_CLAUDE_CREDENTIALS,
        }},
    )
    _write_machine(
        redis_port, "flaky",
        quota={"claude": {
            "pct_left": None, "resets_at": None, "source": "test",
            "note": "quota unavailable (URLError)",
        }},
    )
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--explain", *common])
    captured = capsys.readouterr()

    assert code == 0
    assert "2 machine(s) skipped" in captured.out
    assert "1 have no credentials for claude" in captured.out
    assert "1 cannot read claude quota" in captured.out


def test_cli_place_json_output(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    _write_machine(redis_port, "mac-studio", quota=_CLAUDE_QUOTA)
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--json", *common])
    captured = capsys.readouterr()

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["pick"] == "mac-studio"
    assert payload["provider"] == "claude"


def test_cli_place_json_no_pick_exits_2(redis_port, flush_redis, tmp_path, capsys, clean_fleet_env):
    """Same "try again later" contract as plain/--explain output (see
    the root help's exit-code section) -- --json must not silently report 0
    just because it always has a body to print.
    """
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(redis_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", "--json", *common])
    captured = capsys.readouterr()

    assert code == 2
    assert json.loads(captured.out)["pick"] is None


def test_cli_place_unreachable_redis_exits_3(closed_port, tmp_path, capsys, clean_fleet_env):
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(closed_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]

    code = cli.main(["place", "retry backoff", *common])
    captured = capsys.readouterr()

    assert code == 3
    assert "cannot reach" in captured.err

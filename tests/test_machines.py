"""Tests for the fleet machine registry (issue #7).

Uses the real `redis-server` fixtures in `conftest.py`, not a mock -- same
rule as `test_slots_redis.py`. Offline detection is tested by backdating a
record's `heartbeat` field directly in Redis, not by sleeping 120 real
seconds -- `machines()` compares that field to wall-clock time itself (see
`machines.py`'s docstring), so a record written with an old timestamp is
indistinguishable from one that really went stale that long ago.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import redis as redis_lib

from lupin import agent as agent_mod
from lupin import cli, machines


_AMBIENT_ENV_VARS = (
    "LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME",
    "LUPIN_REDIS_PASSWORD", "LUPIN_FLEET_CONFIG", "LUPIN_BACKEND",
)


@pytest.fixture(autouse=True)
def fake_quota_snapshot():
    """`_write_record` (via `join`/`heartbeat`/`drain`/`undrain`) calls
    `quota.snapshot()` and (for `usage_detail`) `quota.quota_usage()` /
    `quota.claude_usage()` / `quota.omp_usage()` for real otherwise -- on a
    box with real Claude credentials and `omp` installed, that means a live
    network call and a real subprocess on every test in this file. Fixed
    data stands in for all four.
    """
    with (
        mock.patch.object(
            machines.quota, "snapshot",
            return_value={"claude": {"pct_left": 50, "resets_at": None, "source": "test"}},
        ),
        mock.patch.object(machines.quota, "quota_usage", return_value=[]),
        mock.patch.object(machines.quota, "claude_usage", return_value=[]),
        mock.patch.object(machines.quota, "omp_usage", return_value=[]),
    ):
        yield


@pytest.fixture
def clean_fleet_env(monkeypatch):
    """`cli.py`'s fleet flags default from `$LUPIN_REDIS_*` -- a real fleet
    host (e.g. jesus) sets those for its own Redis, which would otherwise
    leak into these tests' throwaway `redis-server` fixture and break auth.
    """
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _raw_client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def _write_raw_record(redis_port, name, *, state="online", version="0.0.0+dev", heartbeat=None):
    """Write a `machine:<name>` record directly, bypassing `hostname()` --
    the public `join`/`heartbeat`/`drain` calls all register *this* test
    process's own hostname, so a test that needs more than one distinct
    machine name (the `machines()` listing test) has to write the others'
    records itself.
    """
    stamp = heartbeat or machines._now_iso()
    record = {
        "version": version,
        "heartbeat": stamp,
        "state": state,
        "slots": {},
        "providers": [],
        "quota": {},
    }
    client = _raw_client(redis_port)
    client.set(f"{machines.PREFIX}machine:{name}", json.dumps(record))


def _old_stamp(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_join_registers_machine_and_writes_config(redis_port, flush_redis, tmp_path):
    config_path = tmp_path / "fleet.json"
    result = machines.join(f"127.0.0.1:{redis_port}", config_path=config_path)

    assert result["name"] == machines.hostname()
    assert result["state"] == "online"
    assert result["version"] == machines.package_version()
    assert json.loads(config_path.read_text()) == {"redis_host": "127.0.0.1", "redis_port": redis_port}

    raw = _raw_client(redis_port)
    stored = json.loads(raw.get(f"lupin:v1:machine:{machines.hostname()}"))
    assert stored["state"] == "online"


def test_join_publishes_actions_from_agent_table(redis_port, flush_redis, tmp_path):
    # `actions` must come from `agent.ACTIONS` itself, not a hand-kept copy
    # -- this is the check that would catch the two drifting apart.
    from lupin import agent

    result = machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")

    assert result["actions"] == sorted(agent.ACTIONS)


def test_heartbeat_refreshes_fields_and_keeps_state(redis_port, flush_redis, tmp_path):
    kw = _kw(redis_port)
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    machines.drain(kw)

    before = machines._read_record(_raw_client(redis_port), machines.hostname())
    record = machines.heartbeat(kw)

    assert record["state"] == "draining"  # heartbeat does not clear drain
    assert record["heartbeat"] >= before["heartbeat"]


def test_heartbeat_fills_quota_from_quota_snapshot(redis_port, flush_redis, tmp_path):
    # `quota.snapshot()` itself is mocked (see `fake_quota_snapshot`) -- this
    # only checks that `_write_record` calls it and stores what it returns.
    kw = _kw(redis_port)
    record = machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    assert record["quota"] == {"claude": {"pct_left": 50, "resets_at": None, "source": "test"}}


def test_heartbeat_does_not_wipe_out_of_band_providers(redis_port, flush_redis, tmp_path):
    # Nothing writes `providers` yet, but `_write_record` must not clobber
    # it once something does -- same rule `heartbeat` already follows for
    # `state`. Simulate that future writer directly in Redis.
    kw = _kw(redis_port)
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    client = _raw_client(redis_port)
    key = f"{machines.PREFIX}machine:{machines.hostname()}"
    record = json.loads(client.get(key))
    record["providers"] = ["anthropic", "openai"]
    client.set(key, json.dumps(record))

    refreshed = machines.heartbeat(kw)

    assert refreshed["providers"] == ["anthropic", "openai"]


def test_join_writes_session_backend_and_actions(redis_port, flush_redis, tmp_path):
    """Every heartbeat record publishes the active backend and the
    actions in `agent.ACTIONS`."""
    record = machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    assert record["session_backend"] == "herdr"
    assert record["actions"] == sorted(agent_mod.ACTIONS)


def test_heartbeat_writes_the_loops_list_given(redis_port, flush_redis, tmp_path):
    kw = _kw(redis_port)
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    loops_payload = [{
        "repo": "widgets", "platform": "claude", "state": "running", "since": "2026-01-01T00:00:00Z",
        "backend": "herdr", "session": "widgets", "workspace_id": "workspace-1", "pane_id": "pane-1",
    }]

    record = machines.heartbeat(kw, loops=loops_payload)

    assert record["loops"] == loops_payload


def test_heartbeat_does_not_wipe_out_of_band_loops_when_not_given(redis_port, flush_redis, tmp_path):
    kw = _kw(redis_port)
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    loops_payload = [{"repo": "widgets", "platform": "claude", "state": "running", "since": None}]
    machines.heartbeat(kw, loops=loops_payload)

    # A later heartbeat that doesn't recompute loops (e.g. a caller that
    # forgot, or hasn't been updated yet) must not erase the last known list.
    refreshed = machines.heartbeat(kw)

    assert refreshed["loops"] == loops_payload


def test_machine_repo_inventory_is_updated_and_kept(redis_port, flush_redis, tmp_path):
    kw = _kw(redis_port)
    initial = [{"repo": "widgets", "enabled": False, "loopable": True}]
    updated = [{"repo": "widgets", "enabled": True, "loopable": True}]
    machines.join(
        f"127.0.0.1:{redis_port}",
        config_path=tmp_path / "fleet.json",
        repos=initial,
    )

    refreshed = machines.heartbeat(kw, repos=updated)
    machines.drain(kw)

    assert refreshed["repos"] == updated
    record = {row["name"]: row for row in machines.machines(kw)}[machines.hostname()]
    assert record["repos"] == updated


def test_drain_and_undrain_flip_state(redis_port, flush_redis, tmp_path):
    kw = _kw(redis_port)
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")

    drained = machines.drain(kw)
    assert drained["state"] == "draining"

    restored = machines.undrain(kw)
    assert restored["state"] == "online"


def test_machines_lists_multiple_including_offline(redis_port, flush_redis):
    _write_raw_record(redis_port, "mac-studio", state="online", heartbeat=machines._now_iso())
    _write_raw_record(redis_port, "jesus", state="draining", heartbeat=_old_stamp(200))

    result = {m["name"]: m for m in machines.machines(_kw(redis_port))}

    assert result["mac-studio"]["state"] == "online"
    # Past OFFLINE_AFTER (120s) with no renewal: offline, even though the
    # stored `state` field itself still says "draining".
    assert result["jesus"]["state"] == "offline"


def test_machines_surfaces_actions_field(redis_port, flush_redis, tmp_path):
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")

    result = machines.machines(_kw(redis_port))

    assert result[0]["actions"] == sorted(agent_mod.ACTIONS)


def test_machines_lists_loops_session_backend_and_actions(redis_port, flush_redis, tmp_path):
    kw = _kw(redis_port)
    machines.join(f"127.0.0.1:{redis_port}", config_path=tmp_path / "fleet.json")
    loops_payload = [{
        "repo": "widgets", "platform": "claude", "state": "running", "since": None,
        "backend": "herdr", "session": "widgets", "workspace_id": "workspace-1", "pane_id": "pane-1",
    }]

    machines.heartbeat(kw, loops=loops_payload)

    result = {m["name"]: m for m in machines.machines(kw)}
    record = result[machines.hostname()]

    assert record["loops"] == loops_payload
    assert record["session_backend"] == "herdr"
    assert record["actions"] == sorted(agent_mod.ACTIONS)


def test_machines_defaults_loops_fields_for_an_old_heartbeat_shape(redis_port, flush_redis):
    # `_write_raw_record` writes the pre-#2 record shape (no loops/
    # session_backend/actions) -- a machine still on an older `lupin` build.
    _write_raw_record(redis_port, "old-build")

    result = machines.machines(_kw(redis_port))

    assert result[0]["loops"] == []
    assert result[0]["session_backend"] is None
    assert result[0]["actions"] == []
    assert result[0]["repos"] == []


def test_machines_reports_version_mismatch_without_raising(redis_port, flush_redis):
    _write_raw_record(redis_port, "stale-build", version="9.9.9-nonexistent")

    result = machines.machines(_kw(redis_port))

    assert len(result) == 1
    assert result[0]["version_mismatch"] is True
    assert result[0]["version"] == "9.9.9-nonexistent"


def test_machines_leaves_out_unreadable_records_and_reports_them(redis_port, flush_redis):
    _write_raw_record(redis_port, "good-box")
    raw = _raw_client(redis_port)
    raw.set("lupin:v1:machine:old-box", "not json")
    raw.set("lupin:v1:machine:list-box", "[]")
    raw.set("lupin:v1:machine:ralpha", json.dumps({"name": "ralpha", "state": "online"}))
    raw.set("lupin:v1:machine:bad-time", json.dumps({"state": "online", "heartbeat": "yesterday"}))
    skipped = []

    result = machines.machines(_kw(redis_port), skipped=skipped, strict=False)

    assert [m["name"] for m in result] == ["good-box"]
    assert sorted(skipped) == [
        "machine:bad-time", "machine:list-box", "machine:old-box", "machine:ralpha",
    ]


def test_machines_raises_on_unreadable_record_by_default(redis_port, flush_redis):
    _write_raw_record(redis_port, "good-box")
    _raw_client(redis_port).set("lupin:v1:machine:old-box", "not json")

    with pytest.raises(json.JSONDecodeError):
        machines.machines(_kw(redis_port))


def test_machines_strict_raises_on_unreadable_record(redis_port, flush_redis):
    _write_raw_record(redis_port, "good-box")
    _raw_client(redis_port).set("lupin:v1:machine:old-box", "not json")

    with pytest.raises(json.JSONDecodeError):
        machines.machines(_kw(redis_port), strict=True)


def test_unreachable_redis_raises_coordinator_unreachable(closed_port):
    kw = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(machines.CoordinatorUnreachable):
        machines.heartbeat(kw)


def test_cli_join_heartbeat_drain_undrain_machines_roundtrip(
    redis_port, flush_redis, tmp_path, capsys, clean_fleet_env
):
    config_path = str(tmp_path / "fleet.json")
    common = ["--config-path", config_path, "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)]

    # `join` takes the coordinator as a positional host:port.
    repo_inventory = [{"repo": "widgets", "enabled": True, "loopable": True}]
    with (
        mock.patch.object(cli.loop_runtime, "local_loops", return_value=[]),
        mock.patch.object(cli.serve, "local_repo_inventory", return_value=repo_inventory),
    ):
        assert cli.main(["join", f"127.0.0.1:{redis_port}", "--config-path", config_path]) == 0
        capsys.readouterr()
        assert cli.main(["heartbeat", *common]) == 0
        capsys.readouterr()

    assert cli.main(["drain", *common]) == 0
    capsys.readouterr()

    code = cli.main(["machines", "--json", *common])
    captured = capsys.readouterr()
    assert code == 0
    listed = json.loads(captured.out)
    assert len(listed) == 1
    assert listed[0]["name"] == machines.hostname()
    assert listed[0]["repos"] == repo_inventory
    assert listed[0]["state"] == "draining"

    assert cli.main(["undrain", *common]) == 0
    capsys.readouterr()

    code = cli.main(["machines", *common])
    captured = capsys.readouterr()
    assert code == 0
    assert f"{machines.hostname()}: online" in captured.out


def test_cli_machines_unreachable_redis_exits_3(closed_port, tmp_path, capsys, clean_fleet_env):
    common = [
        "--redis-host", "127.0.0.1", "--redis-port", str(closed_port),
        "--config-path", str(tmp_path / "fleet.json"),
    ]
    code = cli.main(["machines", *common])
    captured = capsys.readouterr()
    assert code == 3
    assert "cannot reach" in captured.err

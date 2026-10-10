"""Tests for `lupin reconcile` (issue #14).

Redis-backed tests use the real `redis-server` fixtures in conftest.py
(`redis_port`/`flush_redis`/`closed_port`), same convention as
test_quest.py/test_claims.py/test_slots_redis.py -- not a mock.
"""

from __future__ import annotations

import json
import time

import pytest
import redis as redis_lib

from lupin import cli, claims, machines, quest, reconcile


def _kw(redis_port):
    return {"redis_host": "127.0.0.1", "redis_port": redis_port}


def _client(redis_port):
    return redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)


def _machine(name, *, state="online", used=0, max_=2, heartbeat=None):
    return {
        "name": name,
        "state": state,
        "heartbeat": heartbeat or machines._now_iso(),
        "slots": {"bmo": {"used": used, "max": max_}},
    }


def _quest(name, tasks, *, repo="repo", number=10, done_count=None):
    done_count = done_count if done_count is not None else sum(1 for t in tasks if t["done"])
    return {
        "name": name, "repo": repo, "number": number, "tasks": tasks,
        "doneCount": done_count, "total": len(tasks),
    }


_AMBIENT_ENV_VARS = (
    "LUPIN_REDIS_HOST", "LUPIN_REDIS_PORT", "LUPIN_REDIS_USERNAME",
    "LUPIN_REDIS_PASSWORD", "LUPIN_FLEET_CONFIG", "LUPIN_BACKEND",
)


@pytest.fixture
def clean_fleet_env(monkeypatch):
    for name in _AMBIENT_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# claims: release on `lost` (no heartbeat for 10m)
# --------------------------------------------------------------------------


def _write_claim(redis_port, target, session, *, age_seconds):
    client = _client(redis_port)
    value = json.dumps({"host": "mac-studio", "session": session, "since": time.time() - age_seconds})
    client.set(f"{claims.PREFIX}claim:{target}", value, px=600_000)


def test_release_lost_claims_leaves_a_fresh_claim_alone(redis_port, flush_redis):
    _write_claim(redis_port, "acme/repo#422", "api-gateway#2", age_seconds=10)

    lines = reconcile._release_lost_claims(_kw(redis_port))

    assert lines == []
    client = _client(redis_port)
    assert client.get(f"{claims.PREFIX}claim:acme/repo#422") is not None


def test_release_lost_claims_releases_a_stale_one(redis_port, flush_redis):
    _write_claim(redis_port, "acme/repo#422", "api-gateway#2", age_seconds=700)

    lines = reconcile._release_lost_claims(_kw(redis_port))

    assert lines == ["released claim #422 · lost, no heartbeat for 10m · back in the queue, branch kept"]
    client = _client(redis_port)
    assert client.get(f"{claims.PREFIX}claim:acme/repo#422") is None


def test_release_lost_claims_ignores_repos_not_enabled_locally(redis_port, flush_redis):
    # Claims are scanned fleet-wide, not filtered to this caller's own
    # `serve.enabled_repos()` -- see reconcile.py's docstring.
    _write_claim(redis_port, "other-org/other-repo#9", "loop-x", age_seconds=700)

    lines = reconcile._release_lost_claims(_kw(redis_port))

    assert lines == ["released claim #9 · lost, no heartbeat for 10m · back in the queue, branch kept"]


# --------------------------------------------------------------------------
# quest focus: done / closed / down / idle
# --------------------------------------------------------------------------


def test_release_quest_focuses_closed_when_quest_issue_vanished(redis_port, flush_redis):
    quest.write_focus("gone-quest", "mac-studio", pinned=False, **_kw(redis_port))

    lines = reconcile._release_quest_focuses([], ["repo"], _kw(redis_port))

    assert lines == ["released focus gone-quest · closed · quest issue closed"]
    assert quest.read_focus("gone-quest", **_kw(redis_port)) is None


def test_release_quest_focuses_done_when_every_task_closed(redis_port, flush_redis):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))
    done_quest = _quest("session-rewrite", [{"number": 418, "title": "t", "done": True}])

    lines = reconcile._release_quest_focuses([done_quest], ["repo"], _kw(redis_port))

    assert lines == ["released focus session-rewrite · done · all 1 tasks closed"]
    assert quest.read_focus("session-rewrite", **_kw(redis_port)) is None


def test_release_quest_focuses_moves_on_down_when_another_machine_is_ready(redis_port, flush_redis, monkeypatch):
    quest.write_focus("session-rewrite", "mac-studio", pinned=True, **_kw(redis_port))
    running = _quest("session-rewrite", [{"number": 418, "title": "t", "done": False}])
    dag = {"repos": {"repo": [{"number": 418, "blockedBy": [], "blocking": []}]}}
    monkeypatch.setattr(reconcile.roadmap, "cached_dependency_dag", lambda repos, **kw: dag)
    monkeypatch.setattr(
        reconcile.machines, "machines",
        lambda connection, **_: [_machine("mac-studio", state="offline"), _machine("mini-2", used=0, max_=4)],
    )

    lines = reconcile._release_quest_focuses([running], ["repo"], _kw(redis_port))

    assert lines == ["moved focus session-rewrite to mini-2 · mac-studio is offline"]
    moved = quest.read_focus("session-rewrite", **_kw(redis_port))
    assert moved["machine"] == "mini-2"
    assert moved["pinned"] is True  # carried over, not reset


def test_release_quest_focuses_ends_on_down_with_no_replacement(redis_port, flush_redis, monkeypatch):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))
    running = _quest("session-rewrite", [{"number": 418, "title": "t", "done": False}])
    dag = {"repos": {"repo": [{"number": 418, "blockedBy": [], "blocking": []}]}}
    monkeypatch.setattr(reconcile.roadmap, "cached_dependency_dag", lambda repos, **kw: dag)
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [_machine("mac-studio", state="offline")])

    lines = reconcile._release_quest_focuses([running], ["repo"], _kw(redis_port))

    assert lines == ["released focus session-rewrite · down · mac-studio is offline"]
    assert quest.read_focus("session-rewrite", **_kw(redis_port)) is None


def test_release_quest_focuses_raises_on_corrupt_machine_record_and_keeps_focus(
    redis_port, flush_redis, monkeypatch
):
    # A bad record stops the pass. It must not read as offline.
    quest.write_focus("session-rewrite", "bad-box", pinned=False, **_kw(redis_port))
    _client(redis_port).set(f"{machines.PREFIX}machine:bad-box", "{not json")
    running = _quest("session-rewrite", [{"number": 418, "title": "t", "done": False}])
    dag = {"repos": {"repo": [{"number": 418, "blockedBy": [], "blocking": []}]}}
    monkeypatch.setattr(reconcile.roadmap, "cached_dependency_dag", lambda repos, **kw: dag)

    with pytest.raises(json.JSONDecodeError):
        reconcile._release_quest_focuses([running], ["repo"], _kw(redis_port))

    assert quest.read_focus("session-rewrite", **_kw(redis_port))["machine"] == "bad-box"


def test_release_quest_focuses_idle_sets_timer_then_releases_after_30m(redis_port, flush_redis, monkeypatch):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))
    blocked = _quest("session-rewrite", [{"number": 418, "title": "t", "done": False}])
    dag = {"repos": {"repo": [{"number": 418, "blockedBy": [{"repo": "repo", "number": 999}], "blocking": []}]}}
    monkeypatch.setattr(reconcile.roadmap, "cached_dependency_dag", lambda repos, **kw: dag)
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [_machine("mac-studio", used=0, max_=4)])

    # First run: condition just started -- sets idle_since, releases nothing.
    lines = reconcile._release_quest_focuses([blocked], ["repo"], _kw(redis_port))
    assert lines == []
    stored = quest.read_focus("session-rewrite", **_kw(redis_port))
    assert stored["idle_since"] is not None

    # Backdate idle_since past the 30m threshold and run again.
    client = _client(redis_port)
    record = json.loads(client.get("lupin:v1:focus:session-rewrite"))
    record["idle_since"] = time.time() - reconcile.IDLE_AFTER - 1
    client.set("lupin:v1:focus:session-rewrite", json.dumps(record))

    lines = reconcile._release_quest_focuses([blocked], ["repo"], _kw(redis_port))

    assert lines == ["released focus session-rewrite · idle · no ready task and free slots for 30m"]
    assert quest.read_focus("session-rewrite", **_kw(redis_port)) is None


def test_release_quest_focuses_idle_timer_resets_once_a_task_is_ready(redis_port, flush_redis, monkeypatch):
    quest.write_focus("session-rewrite", "mac-studio", pinned=False, **_kw(redis_port))
    client = _client(redis_port)
    record = json.loads(client.get("lupin:v1:focus:session-rewrite"))
    record["idle_since"] = time.time() - 60
    client.set("lupin:v1:focus:session-rewrite", json.dumps(record))

    ready = _quest("session-rewrite", [{"number": 418, "title": "t", "done": False}])
    dag = {"repos": {"repo": [{"number": 418, "blockedBy": [], "blocking": []}]}}
    monkeypatch.setattr(reconcile.roadmap, "cached_dependency_dag", lambda repos, **kw: dag)
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [_machine("mac-studio", used=0, max_=4)])

    lines = reconcile._release_quest_focuses([ready], ["repo"], _kw(redis_port))

    assert lines == []
    stored = quest.read_focus("session-rewrite", **_kw(redis_port))
    assert stored.get("idle_since") is None


# --------------------------------------------------------------------------
# started quests: done / down
# --------------------------------------------------------------------------


def _write_started_quest(redis_port, quest_id, record):
    client = _client(redis_port)
    client.set(f"{quest.PREFIX}quest:{quest_id}", json.dumps(record))


def test_release_started_quests_done_when_every_issue_closed(redis_port, flush_redis, monkeypatch):
    _write_started_quest(
        redis_port, "q1",
        {"issues": [23], "targets": ["acme/repo#23"], "machine": "mac-studio", "state": "running"},
    )
    monkeypatch.setattr(reconcile.quest, "_locate_issue", lambda number, repos, code_dir: ("repo", "acme/repo", {"state": "CLOSED"}))

    lines = reconcile._release_started_quests(["repo"], _kw(redis_port))

    assert lines == ["quest q1 done · #23 shipped · mac-studio is free"]
    assert quest.read_quest("q1", _kw(redis_port)) is None


def test_release_started_quests_moves_on_down_when_another_machine_can_continue(redis_port, flush_redis, monkeypatch):
    _write_started_quest(
        redis_port, "q1",
        {"issues": [23], "targets": ["acme/repo#23"], "machine": "mac-studio", "state": "running"},
    )
    monkeypatch.setattr(reconcile.quest, "_locate_issue", lambda number, repos, code_dir: ("repo", "acme/repo", {"state": "OPEN"}))
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [_machine("mac-studio", state="draining")])
    monkeypatch.setattr(reconcile.place_mod, "place", lambda task, connection, **_: {"pick": "mini-2"})

    lines = reconcile._release_started_quests(["repo"], _kw(redis_port))

    assert lines == ["moved quest q1 to mini-2 · mac-studio is draining"]
    stored = quest.read_quest("q1", _kw(redis_port))
    assert stored["machine"] == "mini-2"


def test_release_started_quests_ends_on_down_with_no_replacement_and_releases_claims(redis_port, flush_redis, monkeypatch):
    _write_started_quest(
        redis_port, "q1",
        {"issues": [23], "targets": ["acme/repo#23"], "machine": "mac-studio", "state": "running"},
    )
    claims.claim("acme/repo#23", "quest:q1", **_kw(redis_port))
    monkeypatch.setattr(reconcile.quest, "_locate_issue", lambda number, repos, code_dir: ("repo", "acme/repo", {"state": "OPEN"}))
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [_machine("mac-studio", state="offline")])
    monkeypatch.setattr(reconcile.place_mod, "place", lambda task, connection, **_: {"pick": None})

    lines = reconcile._release_started_quests(["repo"], _kw(redis_port))

    assert lines == ["quest q1 down · #23 released to the queue, branch kept · mac-studio is offline"]
    assert quest.read_quest("q1", _kw(redis_port)) is None
    assert claims.claims_for(["acme/repo"], **_kw(redis_port)) == {}


def test_release_started_quests_ends_on_down_with_nothing_left_to_release(redis_port, flush_redis, monkeypatch):
    _write_started_quest(
        redis_port, "q1",
        {"issues": [23], "targets": ["acme/repo#23"], "machine": "mac-studio", "state": "running"},
    )
    monkeypatch.setattr(reconcile.quest, "_locate_issue", lambda number, repos, code_dir: ("repo", "acme/repo", {"state": "OPEN"}))
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [])
    monkeypatch.setattr(reconcile.place_mod, "place", lambda task, connection, **_: {"pick": None})

    lines = reconcile._release_started_quests(["repo"], _kw(redis_port))

    assert lines == ["quest q1 down · nothing left to release, branch kept · mac-studio is offline"]


def test_release_started_quests_online_machine_is_left_alone(redis_port, flush_redis, monkeypatch):
    _write_started_quest(
        redis_port, "q1",
        {"issues": [23], "targets": ["acme/repo#23"], "machine": "mac-studio", "state": "running"},
    )
    monkeypatch.setattr(reconcile.quest, "_locate_issue", lambda number, repos, code_dir: ("repo", "acme/repo", {"state": "OPEN"}))
    monkeypatch.setattr(reconcile.machines, "machines", lambda connection, **_: [_machine("mac-studio", state="online")])

    lines = reconcile._release_started_quests(["repo"], _kw(redis_port))

    assert lines == []
    assert quest.read_quest("q1", _kw(redis_port)) is not None


def test_release_started_quests_raises_on_corrupt_machine_record_and_keeps_quest(
    redis_port, flush_redis, monkeypatch
):
    _write_started_quest(
        redis_port, "q1",
        {"issues": [23], "targets": ["acme/repo#23"], "machine": "bad-box", "state": "running"},
    )
    _client(redis_port).set(f"{machines.PREFIX}machine:bad-box", "{not json")
    monkeypatch.setattr(reconcile.quest, "_locate_issue", lambda number, repos, code_dir: ("repo", "acme/repo", {"state": "OPEN"}))
    monkeypatch.setattr(reconcile.place_mod, "place", lambda task, connection, **_: {"pick": "mini-2"})

    with pytest.raises(json.JSONDecodeError):
        reconcile._release_started_quests(["repo"], _kw(redis_port))

    assert quest.read_quest("q1", _kw(redis_port))["machine"] == "bad-box"


# --------------------------------------------------------------------------
# reconcile(): end-to-end, real redis-server
# --------------------------------------------------------------------------


def test_reconcile_combines_every_category_in_order(redis_port, flush_redis, monkeypatch):
    _write_claim(redis_port, "acme/repo#9", "loop-x", age_seconds=700)
    monkeypatch.setattr(reconcile.quest, "load_quests", lambda repos, **kw: ([], []))

    lines, warnings = reconcile.reconcile(["repo"], _kw(redis_port))

    assert warnings == []
    assert lines == ["released claim #9 · lost, no heartbeat for 10m · back in the queue, branch kept"]


def test_reconcile_with_nothing_to_release_returns_empty(redis_port, flush_redis, monkeypatch):
    monkeypatch.setattr(reconcile.quest, "load_quests", lambda repos, **kw: ([], []))

    lines, warnings = reconcile.reconcile(["repo"], _kw(redis_port))

    assert lines == []
    assert warnings == []


def test_reconcile_raises_coordinator_unreachable(closed_port, monkeypatch):
    with pytest.raises(reconcile.CoordinatorUnreachable):
        reconcile.reconcile(["repo"], {"redis_host": "127.0.0.1", "redis_port": closed_port})


# --------------------------------------------------------------------------
# CLI wiring: `lupin reconcile`
# --------------------------------------------------------------------------


def test_cli_reconcile_prints_each_release_line(redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch):
    _write_claim(redis_port, "acme/repo#9", "loop-x", age_seconds=700)
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(cli.reconcile_mod.quest, "load_quests", lambda repos, **kw: ([], []))

    code = cli.main(["reconcile", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "released claim #9 · lost, no heartbeat for 10m · back in the queue, branch kept"


def test_cli_reconcile_with_nothing_to_release_says_so(redis_port, flush_redis, clean_fleet_env, capsys, monkeypatch):
    monkeypatch.setattr(cli.serve, "enabled_repos", lambda: ["repo"])
    monkeypatch.setattr(cli.reconcile_mod.quest, "load_quests", lambda repos, **kw: ([], []))

    code = cli.main(["reconcile", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.strip() == "Nothing to release."


def test_cli_reconcile_slot_already_held_exits_2(redis_port, flush_redis, clean_fleet_env, capsys):
    cli.slots_redis.acquire("reconcile", "other-machine", redis_host="127.0.0.1", redis_port=redis_port)

    code = cli.main(["reconcile", "--redis-host", "127.0.0.1", "--redis-port", str(redis_port)])
    captured = capsys.readouterr()

    assert code == 2
    assert "already running" in captured.err


def test_cli_reconcile_unreachable_redis_exits_3(closed_port, clean_fleet_env, capsys):
    code = cli.main(["reconcile", "--redis-host", "127.0.0.1", "--redis-port", str(closed_port)])
    captured = capsys.readouterr()

    assert code == 3
    assert "cannot reach" in captured.err

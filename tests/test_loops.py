"""Tests for `loops.py`'s shared local-or-remote dispatch (issue #2 phase
A). `dispatch_loop_action`'s local branch is pure plumbing (mocked
`run_local`, no real subprocess); its remote branch uses the real
`redis-server` fixtures in `conftest.py`, same as `test_commands.py`, so a
wrong `commands.enqueue` shape is caught here too, not just by a
call-was-made assertion.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
import redis as redis_lib

from lupin import commands, loops, machines


def _record(name, loop_repos=()):
    return {"name": name, "loops": [{"repo": r} for r in loop_repos]}


# --------------------------------------------------------------------------
# resolve_machine_for_repo
# --------------------------------------------------------------------------


def test_resolve_machine_for_repo_finds_the_one_match():
    records = [_record("jesus", ["widgets"]), _record("mini", [])]
    with mock.patch.object(machines, "machines", return_value=records):
        assert loops.resolve_machine_for_repo("widgets", {}) == "jesus"


def test_resolve_machine_for_repo_raises_on_zero_matches():
    records = [_record("jesus", []), _record("mini", [])]
    with mock.patch.object(machines, "machines", return_value=records):
        with pytest.raises(loops.AmbiguousMachine) as exc:
            loops.resolve_machine_for_repo("widgets", {})
    assert exc.value.candidates == []


def test_resolve_machine_for_repo_raises_on_multiple_matches():
    records = [_record("jesus", ["widgets"]), _record("mini", ["widgets"])]
    with mock.patch.object(machines, "machines", return_value=records):
        with pytest.raises(loops.AmbiguousMachine) as exc:
            loops.resolve_machine_for_repo("widgets", {})
    assert sorted(exc.value.candidates) == ["jesus", "mini"]


def test_resolve_machine_for_repo_lets_coordinator_unreachable_propagate():
    """A Redis outage must surface as "cannot reach the coordinator" (exit
    code 3), not get reported as "pick a different machine" (exit code 5,
    `AmbiguousMachine`) -- that advice wouldn't fix anything."""
    with mock.patch.object(machines, "machines", side_effect=machines.CoordinatorUnreachable("x")):
        with pytest.raises(machines.CoordinatorUnreachable):
            loops.resolve_machine_for_repo("widgets", {})


def test_resolve_machine_for_repo_refuses_a_partly_read_registry(redis_port, flush_redis):
    """A corrupt record must stop the pick. Skipping it would pick from a
    partial list."""
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port)
    raw.set("lupin:v1:machine:old-box", "not json")
    raw.set("lupin:v1:machine:jesus", json.dumps({
        "name": "jesus", "heartbeat": machines._now_iso(), "state": "online",
        "loops": [{"repo": "widgets"}],
    }))

    with pytest.raises(json.JSONDecodeError):
        loops.resolve_machine_for_repo(
            "widgets", {"redis_host": "127.0.0.1", "redis_port": redis_port}
        )


# --------------------------------------------------------------------------
# dispatch_loop_action -- local branch
# --------------------------------------------------------------------------


def test_dispatch_local_runs_local_argv_and_never_touches_the_queue():
    calls = []

    def fake_run_local(argv):
        calls.append(argv)
        return 0, "done"

    result = loops.dispatch_loop_action(
        machine="h", local_host="h", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
        queue_action="loop.stop", queue_params={"repo": "widgets"},
        connection={}, run_local=fake_run_local,
    )
    assert calls == [["lupin", "loop", "local-action", "stop", "widgets"]]
    assert result == {"mode": "local", "returncode": 0, "output": "done"}


def test_dispatch_local_uses_default_runner_when_none_given(monkeypatch):
    monkeypatch.setattr(loops, "run_subprocess", lambda argv: (1, "boom"))
    result = loops.dispatch_loop_action(
        machine="h", local_host="h", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
        queue_action="loop.stop", queue_params={"repo": "widgets"}, connection={},
    )
    assert result == {"mode": "local", "returncode": 1, "output": "boom"}


# --------------------------------------------------------------------------
# dispatch_loop_action -- remote branch (real redis-server)
# --------------------------------------------------------------------------


def test_dispatch_remote_enqueues_the_expected_command_shape(redis_port, flush_redis):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    result = loops.dispatch_loop_action(
        machine="jesus", local_host="pihome", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
        queue_action="loop.stop", queue_params={"repo": "widgets"},
        connection=connection, signing_key="secret", actor="lupin-dashboard",
    )
    assert result["mode"] == "queued"
    assert result["result"] is None  # no wait_s given -- fire and forget

    entries = commands.get_queue("jesus", **connection)
    assert len(entries) == 1
    assert entries[0]["action"] == "loop.stop"
    assert entries[0]["params"] == {"repo": "widgets"}


def test_dispatch_remote_without_signing_key_raises():
    with pytest.raises(loops.MissingSigningKey):
        loops.dispatch_loop_action(
            machine="jesus", local_host="pihome", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
            queue_action="loop.stop", queue_params={"repo": "widgets"}, connection={},
        )


def test_dispatch_remote_unreachable_coordinator_raises(closed_port):
    connection = {"redis_host": "127.0.0.1", "redis_port": closed_port}
    with pytest.raises(machines.CoordinatorUnreachable):
        loops.dispatch_loop_action(
            machine="jesus", local_host="pihome", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
            queue_action="loop.stop", queue_params={"repo": "widgets"},
            connection=connection, signing_key="secret",
        )


def test_dispatch_remote_waits_and_reports_ok(redis_port, flush_redis, monkeypatch):
    """`wait_s` big enough for one 0.5s poll tick -- the first status read
    is still "running", then the signed remote command finishes and reports "ok".
    """
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    call_count = {"n": 0}

    def fake_get_status(cmd_id, **kw):
        call_count["n"] += 1
        if call_count["n"] < 2:
            return {"id": cmd_id, "state": "running"}
        return {"id": cmd_id, "state": "ok", "exit_code": 0}

    monkeypatch.setattr(loops.commands, "get_status", fake_get_status)
    result = loops.dispatch_loop_action(
        machine="jesus", local_host="pihome", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
        queue_action="loop.stop", queue_params={"repo": "widgets"},
        connection=connection, signing_key="secret", wait_s=5.0,
    )
    assert result["mode"] == "queued"
    assert result["result"]["state"] == "ok"
    assert call_count["n"] == 2


def test_dispatch_remote_wait_times_out_with_unknown_result(redis_port, flush_redis, monkeypatch):
    """A `wait_s` shorter than one poll tick: the status never leaves
    "queued" before the deadline, so the caller gets that non-terminal
    state back rather than blocking forever."""
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    monkeypatch.setattr(loops.commands, "get_status", lambda cmd_id, **kw: {"id": cmd_id, "state": "queued"})

    result = loops.dispatch_loop_action(
        machine="jesus", local_host="pihome", local_argv=["lupin", "loop", "local-action", "stop", "widgets"],
        queue_action="loop.stop", queue_params={"repo": "widgets"},
        connection=connection, signing_key="secret", wait_s=0.05,
    )
    assert result["result"]["state"] == "queued"



def test_dispatch_fleet_runs_uses_each_targets_key_and_skips_unsafe_targets(redis_port, flush_redis):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    records = [
        {
            "name": "jesus",
            "state": "online",
            "actions": ["loop.run"],
            "repos": [
                {"repo": "widgets", "loopable": True},
                {"repo": "roundsmith", "loopable": True},
            ],
            "loops": [{"repo": "widgets"}],
            "slots": {"claude": {"used": 1, "max": 2}},
        },
        {
            "name": "ralpha",
            "state": "online",
            "actions": ["loop.run"],
            "repos": [
                {"repo": "widgets", "loopable": True},
                {"repo": "roundsmith", "loopable": True},
            ],
            "loops": [],
            "slots": {"claude": {"used": 1, "max": 2}},
        },
        {
            "name": "pihome",
            "state": "online",
            "actions": ["loop.run"],
            "repos": [{"repo": "not-enabled", "loopable": True}],
            "loops": [],
            "slots": {},
        },
        {
            "name": "mac",
            "state": "online",
            "actions": ["loop.run"],
            "repos": [{"repo": "not-enabled", "loopable": True}],
            "loops": [],
            "slots": {},
        },
        {
            "name": "offline",
            "state": "offline",
            "actions": ["loop.run"],
            "repos": [{"repo": "not-enabled", "loopable": True}],
            "loops": [],
            "slots": {},
        },
    ]
    keys = {"pihome": "local-key", "jesus": "jesus-key", "ralpha": "ralpha-key"}

    results = loops.dispatch_fleet_runs(
        ["widgets", "roundsmith", "not-enabled"],
        records,
        local_host="pihome",
        signing_keys=keys,
        connection=connection,
    )

    assert [(r["repo"], r.get("machine"), r["queued"]) for r in results] == [
        ("not-enabled", None, False),
        ("roundsmith", "jesus", True),
        ("widgets", "ralpha", True),
    ]
    client = commands._client(**connection)
    for result in results:
        if not result["queued"]:
            continue
        payload = json.loads(client.get(commands.cmd_key(result["id"])))
        assert payload["params"] == {"repo": result["repo"]}
        assert commands.verify(payload, keys[result["machine"]])

# --------------------------------------------------------------------------
# ssh_target_for
# --------------------------------------------------------------------------


def test_ssh_target_for_reads_a_match(tmp_path):
    path = tmp_path / "ssh-targets"
    path.write_text("# comment\n\njesus ghosta@jesus.local\nmini mini.local\n")
    assert loops.ssh_target_for("jesus", path) == "ghosta@jesus.local"
    assert loops.ssh_target_for("mini", path) == "mini.local"


def test_ssh_target_for_no_match_returns_none(tmp_path):
    path = tmp_path / "ssh-targets"
    path.write_text("jesus ghosta@jesus.local\n")
    assert loops.ssh_target_for("ralpha", path) is None


def test_ssh_target_for_missing_file_returns_none(tmp_path):
    assert loops.ssh_target_for("jesus", tmp_path / "does-not-exist") is None

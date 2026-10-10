"""`lupin reconcile` (issue #14, the last ticket in #2's fleet-CLI split):
apply the release rules in `lupin-ctl-copy.md` section 4, one shot per
call. Something else (a timer, a cron job) calls this on a schedule; this
module does not loop or sleep itself.

Shipped rules (the rest of section 4 needs an activity signal -- commit or
tool activity -- that nothing writes yet, so they stay out of this issue):

- Claims: `lost` -- no heartbeat for 10m (`claims.DEFAULT_TTL`).
- Quest focus: `done` (every task closed/merged), `closed` (the quest issue
  itself closed), `idle` (free slots and no ready task for 30m), and `down`
  (see the judgment call below).
- Started quests: `done` (every issue closed/merged), `down` (its machine
  is offline or draining -- move to the next best machine if one can take
  over, else end the quest and release its claims).

Judgment call -- `down` for quest focus: the issue's own short summary for
"Focus" lists only done/closed/idle, but section 4 of the copy doc also
lists `down` there, with the same wording as the started-quest version
("moves to the next best machine ... ends only if none can"). Implemented
here, for both: `down` needs only `machines.machines()`'s already-computed
`state`, the same data both cases already read for other checks -- nothing
about quest focus makes that check harder than it is for a started quest.

Judgment call -- claims' `lost` is an active check, not just a wait for
Redis: a claim's own TTL (`claims.DEFAULT_TTL`, 10 minutes, set again on
every renew) already deletes it with no help from here once nothing
renews it -- so most of the time Redis beats this module to it, and there
is nothing left to see. But `claim()` takes `ttl` as a caller-chosen
argument, not a fixed value; if a caller ever claims with a longer TTL,
`lost` still has to fire at 10 minutes of quiet, regardless of what TTL
that claim was given. So this module checks `since` (last write) itself
and force-releases past 10 minutes old, rather than trusting whatever TTL
the claim happens to be using. In the ordinary case (default TTL, regular
renewals) this is a no-op: Redis's own expiry already did the work, and
`claims_for` simply has nothing left to find.

Judgment call -- quest focus's `idle` dwell timer: nothing in
`focus:<quest>` (docs/redis-schema.md) tracks "how long has this been
idle." This module adds one field of its own, `idle_since`, patched
straight into the focus record's JSON (not through `quest.write_focus`,
which would also reset `since`/`pinned`). It is private bookkeeping for
this module only -- nothing else reads it.

Judgment call -- claims are scanned fleet-wide, not filtered to this
machine's own enabled repos: unlike `claims.claims_for` (built for "is one
of *my* issues claimed"), a lost claim belongs to the whole fleet, whether
or not this particular caller has that repo checked out locally. So this
module scans every `claim:*` key itself instead of calling `claims_for`.

Not done here (explicitly out of scope for issue #14, not silently
dropped):

- `stale` (claims) and `stalled` (quest focus): both need an activity
  signal this project does not compute yet.
- "Kept" lines (`lupin-ctl-copy.md`'s "focus is kept when..." list):
  reconcile only prints a line when it changes something. Explaining why a
  focus was left alone needs the same missing activity/stall signals, so
  it is not shipped either.
- `release_when` on `focus:<quest>`, and a "what releases next" column on
  `lupin machines`/`lupin status`: both section 5's own stretch goal, and
  both need their own text-generation logic beyond this issue's stated
  files (reconcile.py, cli.py, tests). Left for a later issue.

Takes the fleet-wide `reconcile` slot (max 1) before doing any release
work -- `cli.py`'s `_cmd_reconcile` does the acquire/release, the same
`slots_redis.acquire`/`release` every other fleet lock in this project
uses, not reinvented here.
"""

from __future__ import annotations

import json
import time

import redis

from . import claims, machines, place as place_mod, quest, roadmap, slots_redis
from .roadmap import CODE_DIR

CoordinatorUnreachable = slots_redis.CoordinatorUnreachable

IDLE_AFTER = 1800.0  # 30 minutes, lupin-ctl-copy.md section 4 ("idle")

_REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)


def _run(op):
    """Same convention as `machines._run`/`quest._run`: a connection
    failure becomes `CoordinatorUnreachable`, the one exception every
    caller of this module already knows how to turn into exit code 3.
    """
    try:
        return slots_redis._call_with_retry(op)
    except _REDIS_ERRORS as exc:
        raise CoordinatorUnreachable("reconcile") from exc


def _client(connection: dict):
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def _scan_claims(client) -> dict[str, dict]:
    """Every current claim, fleet-wide -- see this module's docstring for
    why this scans directly instead of calling `claims.claims_for`.
    """
    prefix = f"{claims.PREFIX}claim:"
    keys = _run(lambda: list(client.scan_iter(match=f"{prefix}*")))
    result: dict[str, dict] = {}
    for key in keys:
        raw = _run(lambda k=key: client.get(k))
        if raw is None:
            continue
        result[key[len(prefix) :]] = json.loads(raw)
    return result


def _scan_focuses(client) -> dict[str, dict]:
    prefix = f"{quest.PREFIX}focus:"
    keys = _run(lambda: list(client.scan_iter(match=f"{prefix}*")))
    result: dict[str, dict] = {}
    for key in keys:
        raw = _run(lambda k=key: client.get(k))
        if raw is None:
            continue
        result[key[len(prefix) :]] = json.loads(raw)
    return result


def _scan_started_quests(client) -> dict[str, dict]:
    prefix = f"{quest.PREFIX}quest:"
    keys = _run(lambda: list(client.scan_iter(match=f"{prefix}*")))
    result: dict[str, dict] = {}
    for key in keys:
        raw = _run(lambda k=key: client.get(k))
        if raw is None:
            continue
        result[key[len(prefix) :]] = json.loads(raw)
    return result


def _patch_focus(client, quest_name: str, **updates) -> None:
    """Merge `updates` into `focus:<quest_name>`'s JSON, leaving every other
    field (`machine`, `pinned`, `since`, `release_when`) untouched. Used
    only for the `idle_since` bookkeeping field below -- a move or a
    release goes through `quest.write_focus`/`delete_focus` instead.
    """
    key = f"{quest.PREFIX}focus:{quest_name}"

    def op():
        raw = client.get(key)
        if raw is None:
            return
        record = json.loads(raw)
        record.update(updates)
        client.set(key, json.dumps(record))

    _run(op)


def _release_lost_claims(connection: dict) -> list[str]:
    """Claims idle past `claims.DEFAULT_TTL` (10m) -- see the judgment call
    in this module's docstring for why this is an active check and not
    just a wait for Redis's own expiry.
    """
    client = _client(connection)
    now = time.time()
    lines = []
    for target, held in _scan_claims(client).items():
        age = now - held.get("since", now)
        if age < claims.DEFAULT_TTL:
            continue
        session = held.get("session")
        if not session:
            continue
        if claims.release_claim(target, session, **connection):
            number = target.rpartition("#")[2]
            lines.append(f"released claim #{number} · lost, no heartbeat for 10m · back in the queue, branch kept")
    return lines


def _machine_state_word(record: dict | None) -> str:
    return record["state"] if record else "offline"


def _release_quest_focuses(
    quests: list[dict], repos: list[str], connection: dict, *, code_dir: str = CODE_DIR
) -> list[str]:
    """`done`, `closed`, `down`, and `idle` for every active `focus:<quest>`
    (see this module's docstring for the `down` judgment call and the
    `idle_since` bookkeeping field). `quests` is `load_quests`'s result --
    passed in so a single reconcile run only reads GitHub once.
    """
    # strict: a bad record must stop the pass. Skipping it would read as offline.
    records = machines.machines(connection, strict=True)
    by_name = {record["name"]: record for record in records}
    dag_box: list[dict] = []  # fetched lazily, at most once -- most runs touch no focus at all

    def dag() -> dict:
        if not dag_box:
            dag_box.append(roadmap.cached_dependency_dag(repos, code_dir=code_dir))
        return dag_box[0]

    client = _client(connection)
    lines = []
    for name, focus in _scan_focuses(client).items():
        machine_name = focus.get("machine")

        found = quest.find_quest(quests, name)
        if found is None:
            # load_quests only returns OPEN issues -- a focus whose quest
            # has vanished from that list means the quest issue closed.
            quest.delete_focus(name, **connection)
            lines.append(f"released focus {name} · closed · quest issue closed")
            continue

        if found["total"] and found["doneCount"] == found["total"]:
            quest.delete_focus(name, **connection)
            lines.append(f"released focus {name} · done · all {found['total']} tasks closed")
            continue

        machine_record = by_name.get(machine_name)
        if machine_record is None or machine_record["state"] != "online":
            state_word = _machine_state_word(machine_record)
            ready = quest.ready_tasks(found, dag())
            new_machine = quest.pick_focus_machine(records) if ready else None
            if new_machine:
                quest.write_focus(name, new_machine, pinned=focus.get("pinned", False), **connection)
                lines.append(f"moved focus {name} to {new_machine} · {machine_name} is {state_word}")
            else:
                quest.delete_focus(name, **connection)
                lines.append(f"released focus {name} · down · {machine_name} is {state_word}")
            continue

        ready = quest.ready_tasks(found, dag())
        used, max_ = machines._slot_totals(machine_record.get("slots"))
        has_free_slots = used < max_

        if ready or not has_free_slots:
            if focus.get("idle_since") is not None:
                _patch_focus(client, name, idle_since=None)
            continue

        idle_since = focus.get("idle_since")
        now = time.time()
        if idle_since is None:
            _patch_focus(client, name, idle_since=now)
        elif now - idle_since >= IDLE_AFTER:
            quest.delete_focus(name, **connection)
            lines.append(f"released focus {name} · idle · no ready task and free slots for 30m")
    return lines


def _issue_closed(number: int, repos: list[str], code_dir: str) -> bool:
    found = quest._locate_issue(number, repos, code_dir)
    return found is not None and found[2].get("state") == "CLOSED"


def _release_started_quests(repos: list[str], connection: dict, *, code_dir: str = CODE_DIR) -> list[str]:
    """`done` and `down` for every running `quest:<id>` (see this module's
    docstring for why `down` moves to the next best machine, via
    `place.place`'s own scoring, before ending the quest outright).
    """
    client = _client(connection)
    # strict: as in `_release_quest_focuses`. A skipped record would read as offline.
    records = machines.machines(connection, strict=True)
    by_name = {record["name"]: record for record in records}

    lines = []
    for quest_id, record in _scan_started_quests(client).items():
        issues = record.get("issues", [])
        targets = record.get("targets", [])
        holder = f"quest:{quest_id}"

        all_closed = bool(issues) and all(_issue_closed(number, repos, code_dir) for number in issues)
        if all_closed:
            for number, target in zip(issues, targets):
                claims.release_claim(target, holder, **connection)
            quest._delete_quest(quest_id, connection)
            issue_list = " ".join(f"#{n}" for n in issues)
            machine_name = record.get("machine")
            lines.append(f"quest {quest_id} done · {issue_list} shipped · {machine_name} is free")
            continue

        machine_name = record.get("machine")
        machine_record = by_name.get(machine_name)
        if machine_record is not None and machine_record["state"] == "online":
            continue

        state_word = _machine_state_word(machine_record)
        pick = None
        if issues:
            placed = place_mod.place(str(issues[0]), connection)
            pick = placed.get("pick")
        if pick and pick != machine_name:
            record["machine"] = pick
            quest._write_quest(quest_id, record, connection)
            lines.append(f"moved quest {quest_id} to {pick} · {machine_name} is {state_word}")
        else:
            released = [
                number for number, target in zip(issues, targets) if claims.release_claim(target, holder, **connection)
            ]
            quest._delete_quest(quest_id, connection)
            if released:
                issue_list = " ".join(f"#{n}" for n in released)
                lines.append(
                    f"quest {quest_id} down · {issue_list} released to the queue, branch kept · "
                    f"{machine_name} is {state_word}"
                )
            else:
                lines.append(
                    f"quest {quest_id} down · nothing left to release, branch kept · {machine_name} is {state_word}"
                )
    return lines


def reconcile(repos: list[str], connection: dict, *, code_dir: str = CODE_DIR) -> tuple[list[str], list[str]]:
    """Run every shipped release rule once. Returns `(lines, warnings)`:
    `lines` is one printed line per release/move, in order (claims, quest
    focuses, started quests); `warnings` carries through any repo-read
    problems `quest.load_quests` hit along the way. Raises
    `CoordinatorUnreachable` if Redis can't be reached.
    """
    quests, warnings = quest.load_quests(repos, code_dir=code_dir)
    lines = []
    lines.extend(_release_lost_claims(connection))
    lines.extend(_release_quest_focuses(quests, repos, connection, code_dir=code_dir))
    lines.extend(_release_started_quests(repos, connection, code_dir=code_dir))
    return lines, warnings

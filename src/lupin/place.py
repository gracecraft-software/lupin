"""`lupin place <task>` (issue #9, part of #2's fleet-CLI split): which
machine should run a task already routed to a model by `route`/`classify`.

Three steps, each reusing an already-landed module rather than
reimplementing it:

1. Classify + route (`classify.classify`, `route.route`) -- unchanged logic,
   just called from here with a synthetic issue dict when `<task>` is free
   text instead of a real GitHub issue.
2. Map the routed model to a provider (`route.provider_for_model`, re-exported
   here under the same name). The table lives in `route.py` because quota
   pacing needs it too, not because it is a `place`-only concern, and
   model-tiers.json's schema is about tiers, not accounts.
3. Score every machine the registry (`machines.py`, issue #7) knows about
   that reports quota (`quota.py`, issue #8) for that provider, and rank the
   candidates.

Judgment call -- "runs the provider": `machines.py`'s `providers` field is
still an unpopulated stub (see its own docstring -- nothing writes it yet).
The thing that *is* populated, per machine, every heartbeat, is `quota`
(`quota.snapshot()`, keyed by provider). A machine with a real percentage
for that provider is the signal this repo has today that the machine can
serve it: the quota reader opens the same credentials file the agent uses,
so a machine that cannot read a provider's quota does not have that
provider's key. An entry with no percentage is therefore skipped, not
ranked -- `snapshot()` writes one entry per known provider on every
heartbeat, so "the entry exists" is true for all of them and proves
nothing. When something starts writing `providers` for real, switch to that
instead -- it is the more direct signal, this is a stand-in.

Judgment call -- ranking and the `result` column: the issue's `--explain`
example ranks by (state, quest focus, free slots, heartbeat age) and labels
every non-picked machine with *why* it lost. This module sorts candidates
by that same tuple, then reports the first dimension where a machine's
tuple differs from the picked machine's -- that reproduces the example
exactly (a draining machine always reads "draining"; an online machine
beaten only on quest focus reads "not quest focus", even if it also has
fewer free slots).

Quota is not a ranking dimension here (issue #36): every provider is one
account shared by the whole fleet, so a quota reading can never actually
differ between two machines -- ranking by it could only ever produce ties,
and the old framing ("this machine has better quota") misdescribed what
was going on. Issue #30's burn-margin ranking and issue #32's per-machine
`quota_exhausted` skip/wait/downgrade are both removed for that reason;
quota now gates the model choice directly, in `route()`'s own pacing
(`pace.py`), before this module ever sees a machine list. `place()` just
reads `route()`'s result: `wait_seconds` set means every option `route()`
tried is blocked right now (wait, no machine ranking needed);
`downgraded_from` set means `route()` already moved off a blocked pick
onto another one.

Judgment call -- quest focus: `quest_focus_for` below maps a task's issue
number to its quest (if `quest.load_quests` -- issue #11 -- finds one in
the current repo whose sub-issues include it) and reads that quest's
`focus:<quest>` machine (docs/redis-schema.md, written by `lupin quest
focus`/`release`, issue #12). Free text has no issue number, an unresolved
issue number has no quest membership to check, and an issue outside every
quest's sub-issues matches nothing -- all three return `None`, same as an
unfocused quest, so every machine ties on that dimension exactly like an
ordinary (non-quest) task.

Judgment call -- no `--repo` flag on `place`: `<task>` is just an issue
number or free text, nothing else. A numeric task is looked up with
`gh issue view <N> --json ...`, which resolves against the *current
directory's* repo the same way every other `gh` call in this codebase does
(see `roadmap.py`) -- there is nowhere else to get a repo from. If `gh`
fails (no repo, not authed, issue missing) this falls back to classifying
on the bare number alone rather than erroring out -- a placement decision
is still useful even with a generic category/size.

This module picks a machine; it does not start an agent or loop. The result
contains the selected machine name in `pick`. A task is not a repository,
so `lupin run` cannot start it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from . import classify as classify_mod
from . import gh_cache
from . import machines
from . import quest as quest_mod
from . import quota as quota_mod
from . import roadmap
from . import route as route_mod

CoordinatorUnreachable = machines.CoordinatorUnreachable

# Moved to route.py (issue #36) -- quota pacing needs this mapping now too,
# and route.py is the more fundamental module. Kept here under the same
# name since this module still needs it for an unrelated reason: labeling
# which provider a machine's quota entry is for.
provider_for_model = route_mod.provider_for_model

_CATEGORY_LABELS = {
    "coding": "code edit",
    "cad-spatial": "CAD/spatial",
    "frontend-ui": "frontend/UI",
    "translation": "translation",
    "prose": "prose",
    "general": "general",
}

_SIZE_LABELS = {
    "size-xs": "tiny",
    "size-s": "small",
    "size-m": "medium",
    "size-l": "large",
    "size-xl": "extra large",
    "size-?": "unknown size",
}

_ISSUE_NUMBER_RE = re.compile(r"^#?(\d+)$")

# Local/shared-GPU inference: these providers run on the machine itself, so
# they have no cloud quota row. The fleet's `bmo` slot limits concurrency,
# not which machine can serve the model.
_NO_QUOTA_PROVIDERS = frozenset({"bmo", "local"})


def quest_focus_for(issue_number: str | None, connection: dict) -> str | None:
    """Which machine, if any, is quest-focused for this task.

    `issue_number` is the task's bare issue number (e.g. `"418"`), or
    `None` for a free-text task -- free text has nothing to match a quest's
    sub-issues against, so it always returns `None`. The quest search is
    over the current repo only (this module's docstring already resolves
    `gh issue view` the same ambient way -- `place` takes no `--repo` flag).
    """
    if not issue_number:
        return None
    try:
        number = int(issue_number)
    except ValueError:
        return None
    repo = os.path.basename(os.getcwd())
    quests, _warnings = quest_mod.load_quests([repo], connection=connection)
    for quest in quests:
        if any(task["number"] == number for task in quest["tasks"]):
            focus = quest_mod.read_focus(quest["name"], **connection)
            return focus.get("machine") if focus else None
    return None


def _fetch_issue(number: str, connection: dict | None = None) -> tuple[dict, str | None]:
    """`gh issue view <number> --json title,body,labels` in the current
    repo (same ambient-repo convention `roadmap.py`'s `gh` calls use).
    Returns `({}, error)` on any failure -- `classify()` still produces a
    (generic) category/size from an empty issue, so a placement decision
    is still possible without a working `gh`.

    Goes through `gh_cache.cached_gh_json` (issue #35): only
    the canonical fetcher (`LUPIN_GH_CANONICAL_FETCHER`) ever runs the `gh` call below, every
    other machine reads the shared cache. A repo-identity lookup is needed
    first to key that cache -- this costs one extra `gh repo view` call on
    an uncached `place`, same ambient-repo lookup `roadmap.py` already does
    for every other cached call site.
    """
    owner, name, identity_error = roadmap._repo_identity(os.getcwd())
    if identity_error:
        return {}, identity_error

    def _fetch() -> tuple[dict, str | None]:
        argv = ["gh", "issue", "view", number, "--json", "title,body,labels"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (FileNotFoundError, OSError) as exc:
            return {}, str(exc)
        except subprocess.TimeoutExpired:
            return {}, "gh issue view timed out"
        if proc.returncode != 0:
            return {}, (proc.stderr or proc.stdout or "gh issue view failed").strip()
        try:
            return json.loads(proc.stdout), None
        except json.JSONDecodeError as exc:
            return {}, f"invalid JSON from gh: {exc}"

    data, error = gh_cache.cached_gh_json(
        owner, name, f"issue:{number}", _fetch, connection=connection
    )
    return data if data is not None else {}, error


def _resolve_task(task: str, connection: dict | None = None) -> tuple[dict, str]:
    """Return (issue dict for classify(), display label)."""
    match = _ISSUE_NUMBER_RE.match(task.strip())
    if not match:
        return {"title": task}, task
    number = match.group(1)
    issue, _error = _fetch_issue(number, connection=connection)
    title = issue.get("title") if issue else None
    label = f"#{number} {title}" if title else f"#{number}"
    return issue or {}, label


def _rank_key(record: dict, quest_focus: str | None, now: float) -> tuple:
    state_rank = 0 if record["state"] == "online" else 1
    quest_rank = 0 if quest_focus and record["name"] == quest_focus else 1
    used, max_ = machines._slot_totals(record.get("slots"))
    return (
        state_rank,
        quest_rank,
        -(max_ - used),
        machines._heartbeat_age(record, now),
    )


def _reason(record: dict, pick: dict | None, quest_focus: str | None) -> str:
    if pick is not None and record["name"] == pick["name"]:
        return "pick"
    if record["state"] != "online":
        return "draining"
    if pick is None:
        return "draining"
    if quest_focus and pick["name"] == quest_focus and record["name"] != quest_focus:
        return "not quest focus"
    used_r, max_r = machines._slot_totals(record.get("slots"))
    used_p, max_p = machines._slot_totals(pick.get("slots"))
    if (max_r - used_r) < (max_p - used_p):
        return "fewer slots free"
    return "staler heartbeat"


def format_duration(seconds: float) -> str:
    """`3h 10m`-style duration, for the `--explain` quota line. Pure
    presentation helper -- kept here (not cli.py) so it's covered the same
    way as the rest of this module's logic.
    """
    if seconds <= 0:
        return "now"
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _quota_for_provider(records: list[dict], provider: str) -> dict | None:
    """One quota reading to show in the header -- #2's design note says
    quota is "usually per-account, shared by every machine on that
    account", so one line is enough unless machines disagree (not handled
    here; see this module's docstring). Prefer full windows, then a real
    percentage, then an unavailable entry.
    """
    fallback = None
    summary = None
    for record in records:
        entry = (record.get("quota") or {}).get(provider)
        if entry is None:
            continue
        fallback = fallback or entry
        if entry.get("windows"):
            return entry
        if entry.get("pct_left") is not None:
            summary = summary or entry
    return summary or fallback


def _filter_candidates(
    records: list[dict], provider: str, quest_focus: str | None, now: float
) -> tuple[dict, list[dict], dict | None]:
    """Split `records` into skip counts, ranked candidate rows, and a pick,
    for one `provider`.

    Skip reasons:
    - `other_provider`: no quota entry at all for `provider`.
    - `no_key`: no percentage, and the note says the machine has no
      credentials for `provider`.
    - `read_failed`: no percentage, and any other note.
    - `offline`.

    Why a missing percentage disqualifies: `quota.snapshot()` writes one
    entry for every provider it knows about on every heartbeat, so "this
    machine has a quota entry" is true for all of them and proves nothing.
    A reading with a real percentage proves the opposite -- the machine has
    that provider's credentials and can read its quota. Recommending a
    machine without them would send the work to a machine that cannot make
    the call, so the heartbeat's own data decides who can serve what.

    `bmo` and `local` are the exception: they have no cloud quota row at
    all. They are local/shared-GPU inference, and the fleet `bmo` slot
    limits concurrency rather than the machine choice. For them only
    `offline` skips a machine; the quota entry is ignored.
    """
    skipped = {"offline": 0, "other_provider": 0, "no_key": 0, "read_failed": 0}
    matched = []
    for record in records:
        if provider in _NO_QUOTA_PROVIDERS:
            if record["state"] == "offline":
                skipped["offline"] += 1
            else:
                matched.append(record)
            continue
        entry = (record.get("quota") or {}).get(provider)
        if entry is None:
            skipped["other_provider"] += 1
            continue
        if record["state"] == "offline":
            skipped["offline"] += 1
            continue
        if entry.get("pct_left") is None:
            if entry.get("note") in quota_mod.NO_KEY_NOTES:
                skipped["no_key"] += 1
            else:
                skipped["read_failed"] += 1
            continue
        matched.append(record)

    ranked = sorted(matched, key=lambda r: _rank_key(r, quest_focus, now))
    online = [r for r in ranked if r["state"] == "online"]
    pick = online[0] if online else None

    candidates = [
        {
            "name": record["name"],
            "state": record["state"],
            "slots_used": machines._slot_totals(record.get("slots"))[0],
            "slots_max": machines._slot_totals(record.get("slots"))[1],
            "quest_focus": record["name"] if record["name"] == quest_focus else None,
            "version": record.get("version"),
            "result": _reason(record, pick, quest_focus),
        }
        for record in ranked
    ]
    return skipped, candidates, pick


def place(
    task: str,
    connection: dict,
    *,
    tiers: dict | None = None,
    excluded_providers: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (),
) -> dict:
    """Classify, route, and pick a machine for `task`. Raises
    `CoordinatorUnreachable` if the fleet registry can't be reached (same
    exception `machines.machines()` raises).

    `excluded_providers` prevents routing to those providers.

    Quota pacing is `route()`'s job now (issue #36 -- every provider is one
    account shared by the whole fleet, so it belongs with the model choice,
    not the machine ranking). `route()`'s result carries `wait_seconds` when
    every option it tried is currently blocked -- this skips straight to
    "no pick" without even filtering machines, since a blocked provider is
    blocked for every machine alike. A quota deferral with an unknown reset
    has `reason` but no `wait_seconds`; it also skips machine selection. It
    carries `downgraded_from` when `route()` already moved off a blocked pick
    onto another one; this just passes that through for `--explain`/`--json`
    to show.
    """
    issue, task_label = _resolve_task(task, connection)
    category, size = classify_mod.classify(issue)
    choice = route_mod.route(
        category,
        size,
        tiers=tiers,
        excluded_providers=excluded_providers,
    )
    model, effort = choice["model"], choice["effort"]
    provider = provider_for_model(model)
    wait_seconds = choice.get("wait_seconds")
    downgraded_from = choice.get("downgraded_from")
    reason = choice.get("reason")

    records = machines.machines(connection)
    issue_number_match = _ISSUE_NUMBER_RE.match(task.strip())
    quest_focus = quest_focus_for(
        issue_number_match.group(1) if issue_number_match else None, connection
    )
    now = time.time()

    if wait_seconds is not None or choice.get("reason") is not None:
        skipped, candidates, pick = (
            {"offline": 0, "other_provider": 0, "no_key": 0, "read_failed": 0},
            [],
            None,
        )
    else:
        skipped, candidates, pick = _filter_candidates(records, provider, quest_focus, now)

    quota = _quota_for_provider(records, provider)

    return {
        "task": task,
        "task_label": task_label,
        "category": category,
        "size": size,
        "category_label": _CATEGORY_LABELS.get(category, category),
        "size_label": _SIZE_LABELS.get(size, size),
        "model": model,
        "effort": effort,
        "provider": provider,
        "quota": quota,
        "candidates": candidates,
        "pick": pick["name"] if pick else None,
        "skipped": skipped,
        "wait_seconds": wait_seconds,
        "downgraded_from": downgraded_from,
        "reason": reason,
    }

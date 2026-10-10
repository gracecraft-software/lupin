"""`lupin roadmap` -- text rendering on top of `roadmap.py`'s data (issue #10).

`roadmap.py` (issue #4) is the only place that decides what counts as a
dependency and fetches it from GitHub. This file never calls GitHub's
dependency API itself -- it only reads `roadmap.py`'s output
(`cached_github`, `cached_dependency_dag`) and `claims.py`'s output
(`claims_for`) and turns them into the `ready`/`blocked`/`dag` text a model
or a person reads.

Judgment calls, since the design doc (`lupin-ctl-copy.md` section 5) gives
one illustrative example, not a full algorithm:

- An issue is "blocked" only by an *open* blocker in a repo this command
  also fetched. A blocker in a repo we didn't fetch can't be checked, so
  it's treated as not blocking (silently -- we have no evidence either
  way, so we don't guess "blocked").
- "Does not exist" (the broken-link warning) is checked with one `gh issue
  view <N> --json state` call per blocker that's missing from the open set
  -- only for blockers whose repo we did fetch. This is a small, targeted
  lookup, not a bulk fetch, and it doesn't touch how `roadmap.py` finds
  dependencies.
- `--dag` uses three states (ready/claimed/blocked), not the mockup's five
  (it also shows done/waiting). Telling "waiting" (blocked, but about to
  clear) from "blocked" (stuck) needs quest/closed-issue data this issue
  doesn't have yet -- three honest states beat five guessed ones.
- Without `--repo`, the repos are the ones this machine enables, plus the
  ones other machines enable in the machine registry (`fleet_repos`). This
  machine reads a repo only through its own checkout. A repo with no
  checkout gets a warning that names it.
- `--json` has a `claims` array: every active claim in the repos shown.
  `--dag --json` has no claims. Claim TTLs come from
  `claims.claims_for(with_ttl=True)`.
- Quest grouping (the mockup's "quest session-rewrite · 2 of 5 done" line)
  is issue #11's data. `_quest_for` is the hook: it always returns None
  today, so every node renders in one ungrouped list. #11 fills it in.
"""

from __future__ import annotations

import functools
import json
import os
import re
import time

from . import claims
from . import gh_cache
from . import machines
from . import roadmap
from .slots import CoordinatorUnreachable

GLYPHS = {"ready": "●", "claimed": "◐", "blocked": "✕"}
LEGEND = "  ".join(f"{GLYPHS[name]} {name}" for name in ("ready", "claimed", "blocked"))


def _priority_rank(priority: str) -> int:
    return int(priority[1:])


def _sort_key(node: dict):
    return (_priority_rank(node["priority"]), -node["number"])


def _are(count: int) -> str:
    return "is" if count == 1 else "are"


def _blocked_by_targets(dag_repos: dict, repo: str, number: int) -> list[tuple[str, int]]:
    for entry in dag_repos.get(repo, []):
        if entry["number"] == number:
            return [(t["repo"], t["number"]) for t in entry.get("blockedBy", [])]
    return []


def _issue_state(
    repo_path: str, number: int, owner: str, name: str, *, connection: dict | None = None
) -> str | None:
    """OPEN, CLOSED, or None (lookup failed -- most likely the issue is a
    broken link and doesn't exist at all).

    Goes through `gh_cache.cached_gh_json` (issue #35) -- only
    `gh_cache.CANONICAL_GH_FETCHER` runs `gh issue view` on a cache miss.
    `owner`/`name` are the blocker's repo identity, already known by
    `build_roadmap`'s caller -- passed in rather than re-looked-up here.
    """
    data, error = gh_cache.cached_gh_json(
        owner,
        name,
        f"issue-state:{number}",
        lambda: roadmap._run_json(
            ["gh", "issue", "view", str(number), "--json", "state"], repo_path
        ),
        connection=connection,
    )
    if error or not isinstance(data, dict):
        return None
    state = data.get("state")
    return state if isinstance(state, str) else None


def _quest_for(node: dict) -> str | None:
    """Hook for issue #11's quest grouping. Always None until #11 lands."""
    return None


def build_roadmap(
    repos: list[str],
    code_dir: str = roadmap.CODE_DIR,
    refresh: bool = False,
    claims_lookup=functools.partial(claims.claims_for, strict=True),
    connection: dict | None = None,
) -> dict:
    """Fetch open issues and the dependency DAG for `repos`, mark claimed
    issues, and compute each issue's status.

    Returns {"repos": {repo: [node, ...]}, "cycles": [...], "warnings": [...],
    "claims": [...]}.
    Each node: number, title, priority, status (ready/blocked/claimed),
    claimedBy (session string or None), blockedBy (resolved open blockers).
    Each claim: target, host, holder, age_seconds, ttl_seconds. It lists
    every active claim in `repos`, also for an issue that is no longer open.

    `connection` reaches the shared `gh` cache, which owns the fleet Redis
    location itself when it is None (see `gh_cache._resolve`).
    """
    warnings: list[str] = []
    issues_by_repo: dict[str, dict[int, dict]] = {}
    open_numbers: dict[str, set[int]] = {}
    owners: dict[str, str | None] = {}
    names: dict[str, str | None] = {}

    for repo in repos:
        path = os.path.join(code_dir, repo)
        owner, name, identity_error = roadmap._repo_identity(path)
        owners[repo] = owner
        names[repo] = name
        if identity_error:
            warnings.append(f"{repo}: {identity_error}")
        if refresh:
            issues, _comments, issue_warnings = roadmap.load_github(
                path, "open", connection=connection
            )
        else:
            issues, _comments, issue_warnings = roadmap.cached_github(
                repo, path, "open", connection=connection
            )
        warnings.extend(f"{repo}: {w}" for w in issue_warnings)
        by_number = {
            issue["number"]: issue
            for issue in issues
            if isinstance(issue, dict) and isinstance(issue.get("number"), int)
        }
        issues_by_repo[repo] = by_number
        open_numbers[repo] = set(by_number)

    dag = roadmap.cached_dependency_dag(repos, code_dir)
    for repo, messages in dag.get("warnings", {}).items():
        warnings.extend(f"{repo}: {m}" for m in messages)

    full_names = {repo: f"{owners[repo]}/{repo}" for repo in repos if owners.get(repo)}
    claimed_sessions: dict[tuple[str, int], str] = {}
    claim_rows: list[dict] = []
    now = _now()
    if full_names:
        try:
            raw_claims = claims_lookup(list(full_names.values()))
        except CoordinatorUnreachable as exc:
            raw_claims = {}
            warnings.append(f"claim data is unavailable: {exc}. Showing issues as unclaimed.")
        name_to_repo = {name: repo for repo, name in full_names.items()}
        for target, info in raw_claims.items():
            owner_repo, _sep, number_text = target.rpartition("#")
            repo = name_to_repo.get(owner_repo)
            if repo and number_text.isdigit():
                claimed_sessions[(repo, int(number_text))] = info.get("session", "")
                claim_rows.append(_claim_row(target, info, now))

    existence_cache: dict[tuple[str, int], str | None] = {}
    nodes_by_repo: dict[str, list[dict]] = {}
    for repo in repos:
        nodes = []
        for number, issue in sorted(issues_by_repo[repo].items()):
            labels = roadmap._label_names(issue)
            priority = roadmap._priority(labels)
            if priority == "P4":
                warnings.append(f"#{number} has no priority label. Sorted last.")
            active_blockers = []
            for blocker_repo, blocker_number in _blocked_by_targets(dag["repos"], repo, number):
                if blocker_repo not in open_numbers:
                    continue  # not a repo we fetched -- can't verify, not treated as blocking
                if blocker_number in open_numbers[blocker_repo]:
                    active_blockers.append((blocker_repo, blocker_number))
                    continue
                key = (blocker_repo, blocker_number)
                if key not in existence_cache:
                    existence_cache[key] = _issue_state(
                        os.path.join(code_dir, blocker_repo),
                        blocker_number,
                        owners[blocker_repo],
                        names[blocker_repo],
                    )
                if existence_cache[key] is None:
                    warnings.append(
                        f"#{number} depends on #{blocker_number}, which does not exist. Treated as unblocked."
                    )
                # CLOSED: resolved, not blocking, no warning.
            claimed_by = claimed_sessions.get((repo, number))
            if claimed_by:
                status = "claimed"
            elif active_blockers:
                status = "blocked"
            else:
                status = "ready"
            nodes.append(
                {
                    "number": number,
                    "title": issue.get("title", ""),
                    "priority": priority,
                    "status": status,
                    "claimedBy": claimed_by,
                    "blockedBy": active_blockers,
                }
            )
        nodes_by_repo[repo] = nodes

    return {
        "repos": nodes_by_repo,
        "cycles": dag.get("cycles", []),
        "warnings": warnings,
        "claims": sorted(claim_rows, key=_claim_sort_key),
    }


def _now() -> float:
    """Wall clock for claim ages. Tests patch this, not `time.time`."""
    return time.time()


def _claim_row(target: str, info: dict, now: float) -> dict:
    """One active claim for the JSON output. `holder` is the claim's
    `session` field. A missing `since` or `ttl` gives `None`."""
    since = info.get("since")
    ttl = info.get("ttl")
    return {
        "target": target,
        "host": info.get("host"),
        "holder": info.get("session"),
        "age_seconds": max(0, round(now - since)) if isinstance(since, (int, float)) else None,
        "ttl_seconds": round(ttl) if isinstance(ttl, (int, float)) else None,
    }


def _claim_sort_key(row: dict) -> tuple[str, int]:
    repo, _sep, number = row["target"].rpartition("#")
    return (repo, int(number))


_CLAIM_SESSION = re.compile(r"^(?P<name>.+)#(?P<num>\d+)$")


def _claim_display(session: str | None) -> str:
    match = _CLAIM_SESSION.match(session or "")
    if match:
        return f"{match.group('name')} #{match.group('num')}"
    return session or "another loop"


def _truncate(text: str, width: int) -> str:
    text = text.strip()
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


def _row(index: int, node: dict) -> str:
    if node["status"] == "claimed":
        status_text = f"claimed by {_claim_display(node['claimedBy'])}"
    else:
        status_text = node["status"]
    title = _truncate(node["title"], 26).ljust(26)
    return f" {index}  #{node['number']}  {node['priority']}  {title}  {status_text}"


def _repo_empty_ready_message(repo: str, claimed_count: int, blocked_count: int) -> str:
    clauses = []
    if claimed_count:
        clauses.append(f"{claimed_count} {_are(claimed_count)} claimed by other loops")
    if blocked_count:
        clauses.append(f"{blocked_count} {_are(blocked_count)} blocked")
    if not clauses:
        return f"No ready tasks in {repo}. Nothing to run."
    return f"No ready tasks in {repo}. " + ", ".join(clauses) + "."


def render_list(model: dict, limit: int, stage: str, explicit_repo: bool) -> str:
    repos = model["repos"]
    if not repos:
        return "No ready tasks in any repo. Nothing to run."

    sections = []
    any_ready = False
    for repo, nodes in repos.items():
        ready = [n for n in nodes if n["status"] == "ready"]
        claimed = [n for n in nodes if n["status"] == "claimed"]
        blocked = [n for n in nodes if n["status"] == "blocked"]
        any_ready = any_ready or bool(ready)

        if stage == "ready":
            if not ready:
                sections.append(_repo_empty_ready_message(repo, len(claimed), len(blocked)))
                continue
            shown = sorted(ready + claimed, key=_sort_key)[:limit]
        elif stage == "blocked":
            if not blocked:
                sections.append(f"No blocked tasks in {repo}.")
                continue
            shown = sorted(blocked, key=_sort_key)[:limit]
        else:  # all
            if not nodes:
                sections.append(f"No open tasks in {repo}.")
                continue
            shown = sorted(ready + claimed + blocked, key=_sort_key)[:limit]

        header = f"{repo} · {len(ready)} ready · {len(blocked)} blocked"
        lines = [header, ""]
        lines.extend(_row(idx, node) for idx, node in enumerate(shown, 1))
        sections.append("\n".join(lines))

    if stage == "ready" and not explicit_repo and not any_ready:
        return "No ready tasks in any repo. Nothing to run."
    return "\n\n".join(sections)


def _issue_json(node: dict) -> dict:
    return {
        "number": node["number"],
        "title": node["title"],
        "priority": node["priority"],
        "status": node["status"],
        "claimedBy": node["claimedBy"],
    }


def _cycles_json(cycles: list[list[dict]]) -> list[list[dict]]:
    return [[{"repo": c["repo"], "number": c["number"]} for c in cycle] for cycle in cycles]


def to_json(model: dict, limit: int, stage: str) -> dict:
    out = {
        "warnings": model["warnings"],
        "cycles": _cycles_json(model["cycles"]),
        "repos": {},
        "claims": model["claims"],
    }
    for repo, nodes in model["repos"].items():
        ready = [n for n in nodes if n["status"] == "ready"]
        claimed = [n for n in nodes if n["status"] == "claimed"]
        blocked = [n for n in nodes if n["status"] == "blocked"]
        if stage == "ready":
            shown = sorted(ready + claimed, key=_sort_key)
        elif stage == "blocked":
            shown = sorted(blocked, key=_sort_key)
        else:
            shown = sorted(ready + claimed + blocked, key=_sort_key)
        out["repos"][repo] = {
            "ready": len(ready),
            "blocked": len(blocked),
            "claimed": len(claimed),
            "issues": [_issue_json(n) for n in shown[:limit]],
        }
    return out


def _node_lookup(model: dict) -> dict[tuple[str, int], dict]:
    return {(repo, node["number"]): node for repo, nodes in model["repos"].items() for node in nodes}


def _edges_for_scope(model: dict) -> list[tuple[tuple[str, int], tuple[str, int]]]:
    edges = []
    for repo, nodes in model["repos"].items():
        for node in nodes:
            for blocker in node["blockedBy"]:
                edges.append((blocker, (repo, node["number"])))
    return edges


def _descendants(key, outgoing, memo):
    if key in memo:
        return memo[key]
    result = set()
    for child in outgoing.get(key, []):
        result.add(child)
        result |= _descendants(child, outgoing, memo)
    memo[key] = result
    return result


def _longest_path(lookup, outgoing):
    memo: dict = {}

    def best_from(key):
        if key in memo:
            return memo[key]
        best: list = []
        for child in sorted(outgoing.get(key, []), key=lambda k: k[1]):
            candidate = [key] + best_from(child)
            if len(candidate) > len(best):
                best = candidate
        memo[key] = best or [key]
        return memo[key]

    longest: list = []
    for key in sorted(lookup, key=lambda k: k[1]):
        candidate = best_from(key)
        if len(candidate) > len(longest):
            longest = candidate
    return longest


def _blocking_summary(lookup: dict, outgoing: dict) -> str | None:
    memo: dict = {}
    counts = {key: len(_descendants(key, outgoing, memo)) for key in lookup}
    best = max(counts.values(), default=0)
    if best == 0:
        return None
    top = min((key for key, count in counts.items() if count == best), key=lambda k: k[1])
    unblocks = ", ".join(f"#{k[1]}" for k in sorted(memo[top], key=lambda k: k[1]))
    line = f"Blocking the most: #{top[1]} (unblocks {unblocks})."
    path = _longest_path(lookup, outgoing)
    next_ready = next((key for key in path if lookup[key]["status"] == "ready"), None)
    if next_ready:
        line += f" Next ready on the critical path: #{next_ready[1]}"
    return line


def _node_label(key: tuple[str, int], multi_repo: bool) -> str:
    repo, number = key
    return f"{repo}#{number}" if multi_repo else f"#{number}"


def render_dag(model: dict, multi_repo: bool) -> tuple[str, int]:
    if model["cycles"]:
        lines = []
        for cycle in model["cycles"]:
            same_repo = len({c["repo"] for c in cycle}) == 1
            chain = " -> ".join(
                f"#{c['number']}" if same_repo else f"{c['repo']}#{c['number']}" for c in cycle
            )
            lines.append(f"error: dependency cycle {chain}. Fix the links in the tracker.")
        return "\n".join(lines), 1

    lookup = _node_lookup(model)
    if not lookup:
        return "No ready tasks in any repo. Nothing to run.", 0

    edges = _edges_for_scope(model)
    outgoing: dict = {}
    incoming_count: dict = {key: 0 for key in lookup}
    for source, target in edges:
        outgoing.setdefault(source, []).append(target)
        incoming_count[target] = incoming_count.get(target, 0) + 1

    # Quest grouping hook: #11 will give `_quest_for` real names. Until
    # then every node's quest is None, so this is one ungrouped group.
    groups: dict = {}
    for key, node in lookup.items():
        groups.setdefault(_quest_for(node), []).append(key)

    out_lines = []
    for _quest, keys in groups.items():
        ordered = sorted(keys, key=lambda k: (_priority_rank(lookup[k]["priority"]), -k[1]))
        for key in ordered:
            node = lookup[key]
            label = _node_label(key, multi_repo)
            glyph = GLYPHS[node["status"]]
            children = outgoing.get(key, [])
            if not children and incoming_count.get(key, 0) == 0:
                out_lines.append(f"  {label} {glyph} {node['title']}   (no dependencies)")
                continue
            out_lines.append(f"  {label} {glyph} {node['title']}")
            for child in sorted(children, key=lambda k: (_priority_rank(lookup[k]["priority"]), -k[1])):
                child_node = lookup[child]
                child_label = _node_label(child, multi_repo)
                out_lines.append(f"    └─> {child_label} {GLYPHS[child_node['status']]} {child_node['title']}")

    out_lines.append("")
    out_lines.append(LEGEND)
    summary = _blocking_summary(lookup, outgoing)
    if summary:
        out_lines.append(summary)
    return "\n".join(out_lines), 0


def dag_to_json(model: dict, multi_repo: bool) -> dict:
    if model["cycles"]:
        return {"cycles": _cycles_json(model["cycles"]), "warnings": model["warnings"]}
    lookup = _node_lookup(model)
    nodes_json = [
        {
            "repo": repo,
            "number": number,
            "title": node["title"],
            "priority": node["priority"],
            "status": node["status"],
            "claimedBy": node["claimedBy"],
            "quest": _quest_for(node),
        }
        for (repo, number), node in lookup.items()
    ]
    edges_json = [
        {"from": {"repo": s[0], "number": s[1]}, "to": {"repo": d[0], "number": d[1]}}
        for s, d in _edges_for_scope(model)
    ]
    return {"nodes": nodes_json, "edges": edges_json, "cycles": [], "warnings": model["warnings"]}


def _append_warnings(text: str, warnings: list[str]) -> str:
    if not warnings:
        return text
    lines = "\n".join(f"warning: {w}" for w in warnings)
    return f"{text}\n\n{lines}" if text else lines


def fleet_repos(
    local_repos: list[str],
    machine_records: list[dict],
    local_host: str,
    code_dir: str = roadmap.CODE_DIR,
) -> tuple[list[str], list[str]]:
    """Add the repos that other machines enable to `local_repos`.

    The machine registry gives the repos. `serve.merge_repo_inventory`
    merges them, as the dashboard does. Returns (repos, warnings).

    This machine can only read a repo that it has a checkout of. A repo
    without one is left out, and a warning names it.
    """
    from . import serve

    inventory = serve.merge_repo_inventory(
        [{"repo": name} for name in local_repos], machine_records, local_host
    )
    repos = list(local_repos)
    warnings = []
    for item in inventory:
        if item["local"] or not item.get("enabled"):
            continue
        name = item["repo"]
        path = os.path.join(code_dir, name)
        if os.path.isdir(path):
            repos.append(name)
        else:
            warnings.append(
                f"{name}: enabled on {item['machine']}, but {path} does not exist "
                "on this machine. Issues and claims are not shown."
            )
    return repos, warnings


def run(
    repo: str | None,
    limit: int,
    stage: str,
    dag: bool,
    as_json: bool,
    refresh: bool,
    code_dir: str = roadmap.CODE_DIR,
    enabled_repos=None,
    claims_lookup=functools.partial(claims.claims_for, strict=True),
    connection: dict | None = None,
    machine_records=None,
    local_host: str | None = None,
    unreadable: list[str] | None = None,
) -> tuple[str, int]:
    """Build and render `lupin roadmap`. Returns (output text, exit code).

    `connection` is the fleet Redis location, threaded from `cli.py`'s
    `--redis-*` flags. `build_roadmap` falls back to `gh_cache`'s own
    resolution when it is None, which reads the same fleet config.

    Without `repo`, the repos are the ones this machine enables. If
    `machine_records` is given, the repos that other machines enable are
    added (see `fleet_repos`). It is a function that returns the machine
    registry's records. If the registry cannot be read, a warning says so
    and only this machine's repos are shown.

    `unreadable` is the list that `machines.machines` and
    `claims.claims_for` fill with the records they skipped. If it has
    entries, a warning gives the count.

    Claims are read strictly when `claims_lookup` is not given. A corrupt
    claim then raises. A caller that passes its own lookup, with a
    `skipped` list, can skip a corrupt claim.
    """
    fleet_warnings: list[str] = []
    if repo:
        repos = [repo]
        explicit_repo = True
    else:
        if enabled_repos is None:
            from . import serve

            enabled_repos = serve.enabled_repos
        repos = enabled_repos()
        explicit_repo = False
        if machine_records is not None:
            try:
                records = machine_records()
            except CoordinatorUnreachable as exc:
                fleet_warnings.append(
                    f"{exc} is unavailable. Showing the repos that this machine enables."
                )
            else:
                repos, fleet_warnings = fleet_repos(
                    repos, records, local_host or machines.hostname(), code_dir
                )

    if repos:
        model = build_roadmap(
            repos,
            code_dir=code_dir,
            refresh=refresh,
            claims_lookup=claims_lookup,
            connection=connection,
        )
    else:
        model = {"repos": {}, "cycles": [], "warnings": [], "claims": []}
    model["warnings"] = fleet_warnings + model["warnings"]
    if unreadable:
        names = ", ".join(sorted(unreadable))
        model["warnings"].append(f"skipped {len(unreadable)} unreadable Redis record(s): {names}")
    multi_repo = len(repos) > 1

    if dag:
        text, exit_code = render_dag(model, multi_repo)
        if as_json:
            return json.dumps(dag_to_json(model, multi_repo)), exit_code
        return _append_warnings(text, model["warnings"]), exit_code

    if as_json:
        return json.dumps(to_json(model, limit, stage)), 0
    return _append_warnings(render_list(model, limit, stage, explicit_repo), model["warnings"]), 0

"""Read and render one repository's open issue roadmap."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlsplit

from . import classify
from . import gh_cache
from . import ledger as ledger_store

CODE_DIR = "/code"
GITHUB_CACHE_SECONDS = 60 * 60
GRAPHQL = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){issues(first:100,after:$cursor,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number comments(last:20){nodes{body createdAt url author{login}}}} pageInfo{hasNextPage endCursor}}}}"""
IMAGE = re.compile(r"!\[([^\]]*)\]\((https://[^)\s]+)\)")
ATTACHMENT_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
ATTACHMENT_PATH = "/user-attachments/assets/"

LEDGER_STATUS_NOTE = (
    "In-flight status comes from the last ledger action. It does not confirm that a process is running. "
    "Parallel status is an estimate. Check the live issue and worktree before dispatch."
)
QUEUE_STAGE_NOTE = (
    "Marked in flight means the ledger records a dispatch action. Eligible issues are not dispatched, "
    "held, or waiting on an open dependency. Next batch starts with the first eligible issue and can "
    "add one more only when both have known, non-overlapping files and do not touch bottleneck files. "
    "Next up holds other eligible P0/P1 issues. Blocked or held means a decision, unresolved dependency, "
    "or holding label prevents work, or the issue has an open dependency. All remaining issues are in "
    "Later queue."
)


def github_attachment_id(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(ATTACHMENT_PATH)
    ):
        return None
    attachment_id = parsed.path[len(ATTACHMENT_PATH) :]
    return attachment_id if ATTACHMENT_ID.fullmatch(attachment_id) else None


IMAGE_HOSTS = {
    "github.com",
    "images.githubusercontent.com",
    "private-user-images.githubusercontent.com",
    "user-images.githubusercontent.com",
}
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_time(value: str | None) -> str:
    parsed = _parse_time(value)
    if not parsed:
        return str(value or "Time unknown")
    return parsed.astimezone(LOS_ANGELES).strftime("%b %d, %I:%M %p %Z").replace(
        " 0", " "
    )


SOURCE_PATH = re.compile(
    r"\b(?:src|tests|scripts|site|infra|hosts|modules|docs)/[\w./-]+\.(?:ts|tsx|js|mjs|svelte|css|html|sql|py|sh|nix|rs|go|md|json|ya?ml|toml)\b"
)
BOTTLENECKS = {
    "roundsmith": {"index.html", "src/ui/shell.ts", "src/styles/components.css"},
}
_GITHUB_CACHE = {}
_CACHE_LOCK = threading.Lock()
CACHE_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "lupin",
    "cache.json",
)

# `gh repo view` answers, remembered per (checkout, origin URL) -- see
# `_repo_identity`: the URL is the cache key, `gh` stays the authority.
# A good answer is believed for hours. A failed one is re-checked soon:
# tokens expire, access gets granted, and a repo with no access should not
# look broken for half a day on the strength of one denial.
_IDENTITY_TTL = 6 * 60 * 60.0
_IDENTITY_RETRY = 5 * 60.0
_IDENTITY_CACHE: dict[tuple, tuple[float, tuple]] = {}
_IDENTITY_LOCK = threading.Lock()


def _identity_ttl(result: tuple) -> float:
    return _IDENTITY_TTL if result[0] else _IDENTITY_RETRY


def _load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(saved, dict):
        return
    now_wall = time.time()
    now_monotonic = time.monotonic()
    with _CACHE_LOCK:
        for row in saved.get("github", []):
            if isinstance(row, list) and len(row) == 4:
                repo, state, timestamp, data = row
                _GITHUB_CACHE[(repo, state)] = (
                    now_monotonic - max(0, now_wall - timestamp),
                    tuple(data),
                )
        for row in saved.get("identity", []):
            if isinstance(row, list) and len(row) == 6:
                path, url, timestamp, owner, name, warning = row
                _IDENTITY_CACHE[(path, url)] = (
                    now_monotonic - max(0, now_wall - timestamp),
                    (owner, name, warning),
                )


def _persist_cache():
    now_monotonic = time.monotonic()
    now_wall = time.time()
    # Snapshot both tables under their locks: page builds write them from
    # several threads at once, and a dict that changes size during the
    # snapshot raises.
    with _CACHE_LOCK:
        github = [
            [repo, state, now_wall - max(0, now_monotonic - timestamp), data]
            for (repo, state), (timestamp, data) in _GITHUB_CACHE.items()
        ]
    with _IDENTITY_LOCK:
        identity = [
            [path, url, now_wall - max(0, now_monotonic - timestamp), *result]
            for (path, url), (timestamp, result) in _IDENTITY_CACHE.items()
        ]
    saved = {"github": github, "identity": identity}
    try:
        directory = os.path.dirname(CACHE_FILE)
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as handle:
            json.dump(saved, handle)
            temporary_path = handle.name
        os.replace(temporary_path, CACHE_FILE)
    except OSError:
        try:
            os.unlink(temporary_path)
        except (OSError, UnboundLocalError):
            pass


_load_cache()


def _run_json(argv: list[str], cwd: str, timeout: int = 60):
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"{argv[0]} is not installed"
    except OSError as error:
        return None, f"{argv[0]} could not start: {error}"
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout} seconds"
    if result.returncode:
        error = (result.stderr or result.stdout or f"{argv[0]} failed").strip()
        error = re.sub(r"(?:gh[oprsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[redacted]", error)
        return None, error
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as error:
        return None, f"invalid JSON from {argv[0]}: {error}"


def _read_ledger(repo_path: str, *, connection: dict | None = None):
    owner, name, error = _repo_identity(repo_path)
    if error:
        return [], f"Could not identify repository for Redis ledger: {error}"
    try:
        # Roadmap status can depend on events older than the default limit.
        events = ledger_store.read_events(
            f"{owner}/{name}", limit=None, **(connection or {})
        )
        return events, None
    except ledger_store.CoordinatorUnreachable as error:
        return [], f"Repository ledger is unavailable: {error}"


# A digest is the handoff summary split into four labelled bullet lists.
DIGEST_FIELDS = (
    ("highlights", "Highlights"),
    ("evidence", "Evidence"),
    ("decisions", "Decisions"),
    ("next", "Next"),
)
DIGEST_HEADLINE_CHARS = 200


def _bullets(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    lines = []
    for item in value:
        if not isinstance(item, str):
            continue
        for part in item.splitlines():
            part = re.sub(r"\s+", " ", part).strip().lstrip("-*• ").strip()
            if part:
                lines.append(part)
    return lines


def _digest(entry: dict) -> dict | None:
    """Return a headline plus labelled bullet lists, or None when empty."""
    if not isinstance(entry, dict):
        return None
    fields = []
    for key, label in DIGEST_FIELDS:
        value = entry.get(key)
        bullets = _bullets(value)
        if bullets:
            fields.append({"key": key, "label": label, "bullets": bullets})
    headline = re.sub(r"\s+", " ", str(entry.get("summary") or "")).strip()
    if not fields and not headline:
        return None
    return {
        "headline": headline[:DIGEST_HEADLINE_CHARS]
        + ("…" if len(headline) > DIGEST_HEADLINE_CHARS else ""),
        "fields": fields,
        "timestamp": entry.get("timestamp"),
    }


def render_digest(digest: dict | None) -> str:
    if not digest:
        return ""
    parts = ["<div class='digest'>"]
    if digest["headline"]:
        parts.append(f"<p class='digest-headline'>{html.escape(digest['headline'])}</p>")
    for field in digest["fields"]:
        items = "".join(
            f"<li>{html.escape(bullet)}</li>" for bullet in field["bullets"]
        )
        parts.append(
            f"<div class='digest-field' data-digest-field='{_escape_attr(field['key'])}'>"
            f"<span class='digest-label'>{html.escape(field['label'])}</span>"
            f"<ul>{items}</ul></div>"
        )
    parts.append("</div>")
    return "".join(parts)


DIGEST_CSS = """
.digest{margin:.5rem 0;padding:.5rem .7rem;border-left:3px solid var(--line);background:var(--code);border-radius:0 6px 6px 0}
.digest-headline{margin:0 0 .35rem;font-weight:600}
.digest-field{display:grid;grid-template-columns:7.5rem minmax(0,1fr);gap:.5rem;align-items:start;margin:.2rem 0}
.digest-label{font-size:.75rem;letter-spacing:.05em;text-transform:uppercase;color:var(--dim);padding-top:.15rem}
.digest-field ul{margin:0;padding-left:1.1rem}
.digest-field li{margin:.1rem 0;overflow-wrap:anywhere}
"""


def _read_comments(
    repo_path: str, owner: str, name: str, state: str = "open",
    max_issues: int | None = None, *, connection: dict | None = None,
):
    """Return (comments, error): recent comments per open issue, paginated.

    Goes through `gh_cache.cached_gh_json` (issue #35) -- only
    `gh_cache.CANONICAL_GH_FETCHER` runs the loop below on a cache miss.
    """

    def _fetch():
        comments = {}
        cursor = None
        query = GRAPHQL.replace("states:OPEN", f"states:{state.upper()}")
        while True:
            args = [
                "gh", "api", "graphql", "-f", f"query={query}",
                "-F", f"owner={owner}", "-F", f"name={name}",
            ]
            if cursor:
                args.extend(["-F", f"cursor={cursor}"])
            data, error = _run_json(args, repo_path)
            if error:
                return comments, error
            if not isinstance(data, dict):
                return comments, "GitHub returned invalid comment data"
            errors = data.get("errors") or []
            if errors:
                messages = [
                    str(item.get("message") or "GraphQL error")
                    for item in errors
                    if isinstance(item, dict)
                ]
                return comments, "; ".join(messages) or "GraphQL error"
            response = data.get("data")
            repository = response.get("repository") if isinstance(response, dict) else None
            page = repository.get("issues") if isinstance(repository, dict) else None
            if not isinstance(page, dict):
                return comments, "GitHub returned no issue comments"
            nodes = page.get("nodes")
            page_info = page.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                return comments, "GitHub returned incomplete comment data"
            for issue in nodes:
                issue_comments = issue.get("comments") if isinstance(issue, dict) else None
                comment_nodes = (
                    issue_comments.get("nodes") if isinstance(issue_comments, dict) else None
                )
                if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
                    return comments, "GitHub returned incomplete issue comments"
                if not isinstance(comment_nodes, list):
                    return comments, "GitHub returned incomplete issue comments"
                comments[issue["number"]] = [
                    {
                        "body": str(comment.get("body") or ""),
                        "createdAt": comment.get("createdAt"),
                        "url": comment.get("url") or "",
                        "author": (
                            comment.get("author", {}).get("login", "")
                            if isinstance(comment.get("author"), dict)
                            else ""
                        ),
                    }
                    for comment in comment_nodes
                    if isinstance(comment, dict)
                ]
                if max_issues is not None and len(comments) >= max_issues:
                    return comments, None
            if not page_info.get("hasNextPage"):
                return comments, None
            cursor = page_info.get("endCursor")
            if not cursor:
                return comments, "GitHub returned incomplete pagination data"

    cache_key = f"comments:{state}" if max_issues is None else f"comments:{state}:{max_issues}"
    comments, error = gh_cache.cached_gh_json(owner, name, cache_key, _fetch, connection=connection)
    # A cache hit round-trips through JSON, which stringifies dict keys --
    # normalize back to int so a cached result matches a live fetch's shape.
    if isinstance(comments, dict):
        comments = {int(number): value for number, value in comments.items()}
    return comments, error


def _add_edge(edges: dict, issue_ids: set, source: int, target: int, kind: str) -> None:
    if source != target and source in issue_ids and target in issue_ids:
        edges[(source, target, kind)] = {"from": source, "to": target, "kind": kind}


def _label_names(issue: dict) -> list[str]:
    return sorted(
        label.get("name", "") if isinstance(label, dict) else str(label)
        for label in issue.get("labels", [])
    )


def _priority(labels: list[str]) -> str:
    for label in labels:
        match = re.fullmatch(r"(?:(?:prio|priority)/)?(P[0-3])", label, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return "P4"


def _size(labels: list[str]) -> str:
    for label in labels:
        match = re.fullmatch(r"size[-/](XS|S|M|L|XL)", label, re.IGNORECASE)
        if match:
            return f"size-{match.group(1).lower()}"
    return "size-?"


def _acyclic(issue_ids: set, edges: list[dict]) -> bool:
    outgoing = {number: [] for number in issue_ids}
    incoming = {number: 0 for number in issue_ids}
    for edge in edges:
        outgoing[edge["from"]].append(edge["to"])
        incoming[edge["to"]] += 1
    ready = [number for number, count in incoming.items() if count == 0]
    visited = 0
    while ready:
        number = ready.pop()
        visited += 1
        for target in outgoing[number]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    return visited == len(issue_ids)


def _comment_body(comment) -> str:
    return str(comment.get("body") or "") if isinstance(comment, dict) else str(comment or "")


def _comment_details(comment) -> dict:
    body = _comment_body(comment)
    if not isinstance(comment, dict):
        comment = {}
    return {
        "body": body,
        "createdAt": comment.get("createdAt"),
        "url": comment.get("url") or "",
        "author": comment.get("author") or "",
    }


def build_model(
    issues: list[dict],
    comments: dict,
    ledger: list[dict],
    warnings: list[str] | None = None,
    repo: str = "",
):
    issue_ids = {int(issue["number"]) for issue in issues}

    def inside_quote(text, position):
        line_start = text.rfind("\n", 0, position) + 1
        line = text[line_start : text.find("\n", line_start) if "\n" in text[line_start:] else len(text)]
        if line.lstrip().startswith(">"):
            return True
        prefix = text[line_start:position]
        return sum(prefix.count(quote) for quote in ('"', "“", "”")) % 2 == 1

    ledger_latest = {}
    for entry in ledger:
        if isinstance(entry, dict) and isinstance(entry.get("issue"), int):
            ledger_latest[entry["issue"]] = entry
    handoff_entry = next(
        (
            entry
            for entry in reversed(ledger)
            if isinstance(entry, dict)
            and entry.get("event") == "handoff"
        ),
        None,
    )
    handoff_status = None
    if handoff_entry:
        handoff_status = re.sub(
            r"\s+",
            " ",
            str(handoff_entry.get("status") or handoff_entry.get("event") or "handoff"),
        ).strip()
        if len(handoff_status) > 64:
            handoff_status = handoff_status[:63].rstrip() + "…"

    decision_blocks = {}
    for issue in issues:
        texts = [
            issue.get("body") or "",
            *(_comment_body(comment) for comment in comments.get(int(issue["number"]), [])),
        ]
        for text in texts:
            for match in re.finditer(r"\bblocks\s+#(\d+)\b", text, re.IGNORECASE):
                if inside_quote(text, match.start()):
                    continue
                line_start = text.rfind("\n", 0, match.start()) + 1
                prefix = text[line_start : match.start()]
                decision = re.search(r"\b(D\d+)\b[^()\n]*\([^()\n]*$", prefix)
                if decision:
                    decision_blocks[int(match.group(1))] = decision.group(1)


    edges = {}
    for issue in issues:
        number = int(issue["number"])
        issue_labels = _label_names(issue)
        texts = [
            issue.get("body") or "",
            *(_comment_body(comment) for comment in comments.get(number, [])),
        ]
        parent_refs = set()
        for text in texts:
            for match in re.finditer(r"\b(?:part of|child of|split from)\s+#(\d+)\b", text, re.IGNORECASE):
                if inside_quote(text, match.start()):
                    continue
                parent = int(match.group(1))
                parent_refs.add(parent)
                _add_edge(edges, issue_ids, parent, number, "parent")
            for match in re.finditer(r"\b(?:blocked by|waits for|requires|prerequisite(?:d)? by|depends on)\s+#(\d+)\b", text, re.IGNORECASE):
                if inside_quote(text, match.start()):
                    continue
                blocker = int(match.group(1))
                tail = text[match.end() : match.end() + 32]
                prefix = text[max(0, match.start() - 16) : match.start()]
                negated = re.search(r"\b(?:not|isn'?t|doesn'?t|didn'?t|never)\s+$", prefix, re.IGNORECASE)
                if (
                    blocker not in parent_refs
                    and not negated
                    and not re.match(r"['’]s\s+(?:comment|thread|decision)", tail, re.IGNORECASE)
                ):
                    _add_edge(edges, issue_ids, blocker, number, "depends")
            for match in re.finditer(r"\bblocks\s+#(\d+)\b", text, re.IGNORECASE):
                if inside_quote(text, match.start()):
                    continue
                line_start = text.rfind("\n", 0, match.start()) + 1
                prefix = text[line_start : match.start()]
                if not re.search(r"\bD\d+\b[^()\n]*\([^()\n]*$", prefix):
                    _add_edge(edges, issue_ids, number, int(match.group(1)), "depends")
            for match in re.finditer(r"#(\d+)\s+unblocks after\s+#(\d+)\s+lands", text, re.IGNORECASE):
                if inside_quote(text, match.start()):
                    continue
                _add_edge(edges, issue_ids, int(match.group(2)), int(match.group(1)), "depends")
            for start in re.finditer(r"\b(?:depends on|requires)\b", text, re.IGNORECASE):
                if inside_quote(text, start.start()):
                    continue
                sentence = re.split(r"[.!?\n]", text[start.start() :], maxsplit=1)[0]
                for phrase_match in re.finditer(r"[\"“]([^\"”]+)[\"”]", sentence):
                    phrase = phrase_match.group(1).casefold()
                    matches = [
                        candidate for candidate in issues
                        if int(candidate["number"]) != number
                        and phrase in candidate.get("title", "").casefold()
                    ]
                    if len(matches) == 1:
                        candidate_number = int(matches[0]["number"])
                        if not (number in decision_blocks and candidate_number in parent_refs):
                            _add_edge(edges, issue_ids, candidate_number, number, "depends")
            for line in text.splitlines():
                if not any(char in line for char in "─←→"):
                    continue
                ids = [int(match.group(1)) for match in re.finditer(r"#(\d+)", line)]
                if len(ids) < 2:
                    continue
                if "←" in line:
                    _add_edge(edges, issue_ids, ids[-1], ids[0], "depends")
                elif "+" in line[: line.rfind("─")]:
                    for prerequisite in ids[:-1]:
                        _add_edge(edges, issue_ids, prerequisite, ids[-1], "depends")
                else:
                    for index in range(1, len(ids)):
                        _add_edge(edges, issue_ids, ids[index - 1], ids[index], "depends")
            if any(label.casefold() == "epic" for label in issue_labels):
                for match in re.finditer(r"^\s*[-*]\s+#(\d+)\s+[—–-]", text, re.MULTILINE):
                    _add_edge(edges, issue_ids, number, int(match.group(1)), "parent")

    for entry in ledger:
        if not isinstance(entry, dict) or not isinstance(entry.get("issue"), int):
            continue
        children = entry.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, int):
                    _add_edge(edges, issue_ids, entry["issue"], child, "split")

    unresolved = {}
    for issue in issues:
        body = issue.get("body") or ""
        for match in re.finditer(
            r"\b(?:depends on|blocked by)\s+(?:the\s+)?([^\.\n]{1,60}?)\s+(?:issue|task)\b",
            body,
            re.IGNORECASE,
        ):
            unresolved[int(issue["number"])] = match.group(1).strip()

    nodes = []
    for issue in issues:
        number = int(issue["number"])
        labels = _label_names(issue)
        state = ledger_latest.get(number, {})
        action = state.get("event") or state.get("status")
        sprint_match = next(
            (re.fullmatch(r"sprint[-/](\d+)", label, re.IGNORECASE) for label in labels if re.fullmatch(r"sprint[-/](\d+)", label, re.IGNORECASE)),
            None,
        )
        nodes.append(
            {
                "number": number,
                "title": issue.get("title", ""),
                "body": issue.get("body") or "",
                "url": issue.get("url", ""),
                "createdAt": issue.get("createdAt"),
                "closedAt": issue.get("closedAt"),
                "updatedAt": issue.get("updatedAt"),
                "labels": labels,
                "priority": _priority(labels),
                "size": _size(labels),
                "sprint": int(sprint_match.group(1)) if sprint_match else 999,
                "loop": str(action)[:56] if action is not None else None,
                "digest": _digest(state),
                "dispatched": isinstance(action, str) and action.casefold() in {"dispatch", "dispatched", "running"},  # Ledger claims in-flight; see disclaimer below (not live-process verification).
                "decision": decision_blocks.get(number),
                "unresolvedDependency": unresolved.get(number),
                "files": sorted(set(SOURCE_PATH.findall(issue.get("body") or ""))),
                "comments": [
                    _comment_details(comment)
                    for comment in comments.get(number, [])[-20:]
                ],
            }
        )

    by_number = {node["number"]: node for node in nodes}
    open_dependencies = {edge["to"] for edge in edges.values() if edge["kind"] == "depends"}

    def held(node):
        return node["decision"] or node["unresolvedDependency"] or any(
            label.casefold() in {"owner-todo", "needs-decision", "completely-blocked-on-human", "epic"}
            for label in node["labels"]
        )

    eligible = [
        node for node in nodes
        if not node["dispatched"] and not held(node) and node["number"] not in open_dependencies
    ]
    eligible.sort(key=lambda node: (int(node["priority"][1:]), node["sprint"], -node["number"]))
    batch = []
    bottlenecks = BOTTLENECKS.get(repo, set())
    for candidate in eligible:
        if not batch:
            batch.append(candidate)
            continue
        if len(batch) == 2:
            break
        scopes_known = bool(candidate["files"]) and all(node["files"] for node in batch)
        overlaps = any(set(node["files"]) & set(candidate["files"]) for node in batch)
        paths = set(candidate["files"]) | {path for node in batch for path in node["files"]}
        if scopes_known and not overlaps and not paths & bottlenecks:
            batch.append(candidate)

    batch_ids = {node["number"] for node in batch}
    active_ids = {node["number"] for node in nodes if node["dispatched"]}
    next_up_ids = {
        node["number"] for node in eligible
        if node["number"] not in batch_ids and node["priority"] in {"P0", "P1"}
    }
    waiting_ids = {
        node["number"] for node in nodes
        if held(node) or node["number"] in open_dependencies
    }
    later_ids = {
        node["number"] for node in nodes
        if node["number"] not in active_ids | batch_ids | next_up_ids | waiting_ids
    }
    owner_blocked_ids = {
        node["number"] for node in nodes
        if any(label.casefold() == "completely-blocked-on-human" for label in node["labels"])
    }
    waiting_ids -= owner_blocked_ids
    groups = [
        ("Marked in flight", active_ids),
        ("Next batch", batch_ids),
        ("Next up", next_up_ids),
        ("Later queue", later_ids),
        ("Blocked or held", waiting_ids),
    ]
    stage_order = []
    assigned = set()
    for name, numbers in groups:
        selected = numbers - assigned
        assigned.update(selected)
        stage_order.append(
            {
                "name": name,
                "numbers": sorted(
                    selected,
                    key=lambda number: (
                        int(by_number[number]["priority"][1:]),
                        by_number[number]["sprint"],
                        -number,
                    ),
                ),
            }
        )
    edge_list = list(edges.values())
    owner_blocked_sorted = sorted(
        owner_blocked_ids,
        key=lambda number: (
            int(by_number[number]["priority"][1:]),
            by_number[number]["sprint"],
            -number,
        ),
    )
    return {
        "nodes": [by_number[number] for stage in stage_order for number in stage["numbers"]]
        + [by_number[number] for number in owner_blocked_sorted],
        "edges": edge_list,
        "stages": stage_order,
        "ownerBlocked": owner_blocked_sorted,
        "batch": [node["number"] for node in batch],
        "activeCount": len(active_ids),
        "targetDispatches": 2,
        "handoffStatus": handoff_status,
        "acyclic": _acyclic(issue_ids, edge_list),
        "warnings": list(warnings or []),
        "generatedAt": time.time(),
    }


def _repo_identity(repo_path: str):
    """Return (owner, name, warning) for the repo checked out at repo_path.

    `gh repo view` is the authority -- it follows a repo that moved owners
    -- but it is one network subprocess (~0.6 s), and a Roadmap page load
    resolves an identity per repo. The checkout's `origin` URL only changes
    when someone edits the remote, so it keys the cache: read it from git
    (~5 ms), and ask `gh` only when that URL is new or its answer is stale.
    """
    url = _git_remote_url(repo_path)
    # A checkout with no `origin` (or one that is not a repository at all)
    # cannot be identified from git alone, and `gh` answers the same denial
    # on every page load. Remember that answer too, keyed by the path, so it
    # is re-checked on the retry clock instead of once per render. Paths
    # that are not real directories are not cached.
    key = (repo_path, url or "") if os.path.isdir(repo_path) else None
    if key is None:
        return _gh_identity(repo_path)
    now = time.monotonic()
    with _IDENTITY_LOCK:
        cached = _IDENTITY_CACHE.get(key)
        if cached and now - cached[0] < _identity_ttl(cached[1]):
            return cached[1]
    result = _gh_identity(repo_path)
    with _IDENTITY_LOCK:
        _IDENTITY_CACHE[key] = (time.monotonic(), result)
    if result[0]:
        _persist_cache()
    return result


def _gh_identity(repo_path: str):
    identity, error = _run_json(["gh", "repo", "view", "--json", "owner,name"], repo_path)
    if error:
        return None, None, f"GitHub repository data is unavailable: {error}"
    if not isinstance(identity, dict):
        return None, None, "GitHub returned invalid repository data"
    owner_data = identity.get("owner")
    owner = owner_data.get("login") if isinstance(owner_data, dict) else None
    name = identity.get("name")
    if not owner or not name:
        return None, None, "GitHub returned no repository owner or name"
    return owner, name, None


def _git_remote_url(repo_path: str) -> str | None:
    """The checkout's `origin` URL, read from git alone (no network)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def load_github(repo_path: str, state: str = "open", *, connection: dict | None = None):
    warnings = []
    owner, name, error = _repo_identity(repo_path)
    issues = []
    comments = {}
    if error:
        warnings.append(error)
    else:
        issue_args = [
            "gh", "issue", "list", "--state", state,
            "--limit", "100" if state == "closed" else "1000",
            "--json", "number,title,body,labels,url,createdAt,updatedAt,closedAt",
        ]
        max_issues = None
        if state == "closed":
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
            issue_args.extend(["--search", f"closed:>={cutoff}"])
            max_issues = 100
        # gh_cache.cached_gh_json (issue #35): only CANONICAL_GH_FETCHER runs
        # this `gh issue list` call on a cache miss.
        issues, error = gh_cache.cached_gh_json(
            owner, name, f"issues:{state}",
            lambda: _run_json(issue_args, repo_path),
            connection=connection,
        )
        if error:
            warnings.append(f"GitHub issue data is unavailable: {error}")
            issues = []
        elif not isinstance(issues, list):
            warnings.append("GitHub returned invalid issue data")
            issues = []
        else:
            if max_issues is None:
                comments, error = _read_comments(
                    repo_path, owner, name, state, connection=connection
                )
            else:
                comments, error = _read_comments(
                    repo_path, owner, name, state, max_issues, connection=connection
                )
            if error:
                warnings.append(f"Recent GitHub comments are unavailable: {error}")
                comments = comments or {}
    return issues, comments, warnings


# Dependency edges come from GitHub's own issue-dependency feature --
# the `blockedBy`/`blocking` fields -- not a "Depends on #N" text search.
# Confirmed by `gh issue view --json` (lists blockedBy/blocking as real
# fields) and by GraphQL schema introspection on the Issue type. Other
# fields that looked promising don't actually carry a blocking relationship:
# `closedByPullRequestsReferences` only links an issue to the PR that closes
# it (not to another issue), and a cross-reference timeline event just means
# one issue mentioned another -- no more reliable than the text search this
# was meant to replace. A raw query (not `gh issue list --json`, which does
# not expose the linked issue's repo) confirms each blockedBy/blocking node
# carries `repository{name}`, which is what makes cross-repo edges possible.
DEPENDENCY_GRAPHQL = (
    "query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){"
    "issues(first:100,after:$cursor,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){"
    "nodes{number "
    "blockedBy(first:25){nodes{number repository{name}}} "
    "blocking(first:25){nodes{number repository{name}}}"
    "} pageInfo{hasNextPage endCursor}}}}"
)


def _dependency_targets(issue: dict, key: str) -> list[dict]:
    connection = issue.get(key)
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    if not isinstance(nodes, list):
        return []
    targets = []
    for target in nodes:
        if not isinstance(target, dict) or not isinstance(target.get("number"), int):
            continue
        repository = target.get("repository")
        repo_name = repository.get("name") if isinstance(repository, dict) else None
        if repo_name:
            targets.append({"repo": repo_name, "number": target["number"]})
    return targets


def _read_dependencies(repo_path: str, owner: str, name: str, *, connection: dict | None = None):
    """Read every open issue's blockedBy/blocking links, with pagination.

    Returns (links, error). links maps issue number -> {"blockedBy": [...],
    "blocking": [...]}, each a list of {"repo", "number"} -- the repo name
    travels with the link, so a target in another repo is still usable.

    Goes through `gh_cache.cached_gh_json` (issue #35) -- only
    `gh_cache.CANONICAL_GH_FETCHER` runs the loop below on a cache miss.
    """

    def _fetch():
        links = {}
        cursor = None
        while True:
            args = [
                "gh", "api", "graphql", "-f", f"query={DEPENDENCY_GRAPHQL}",
                "-F", f"owner={owner}", "-F", f"name={name}",
            ]
            if cursor:
                args.extend(["-F", f"cursor={cursor}"])
            data, error = _run_json(args, repo_path)
            if error:
                return links, error
            if not isinstance(data, dict):
                return links, "GitHub returned invalid dependency data"
            errors = data.get("errors") or []
            if errors:
                messages = [
                    str(item.get("message") or "GraphQL error")
                    for item in errors
                    if isinstance(item, dict)
                ]
                return links, "; ".join(messages) or "GraphQL error"
            response = data.get("data")
            repository = response.get("repository") if isinstance(response, dict) else None
            page = repository.get("issues") if isinstance(repository, dict) else None
            if not isinstance(page, dict):
                return links, "GitHub returned no issue dependency data"
            nodes = page.get("nodes")
            page_info = page.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                return links, "GitHub returned incomplete dependency data"
            for issue in nodes:
                if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
                    return links, "GitHub returned incomplete issue dependency data"
                links[issue["number"]] = {
                    "blockedBy": _dependency_targets(issue, "blockedBy"),
                    "blocking": _dependency_targets(issue, "blocking"),
                }
            if not page_info.get("hasNextPage"):
                return links, None
            cursor = page_info.get("endCursor")
            if not cursor:
                return links, "GitHub returned incomplete pagination data"

    links, error = gh_cache.cached_gh_json(owner, name, "dependencies", _fetch, connection=connection)
    # Same JSON-stringifies-int-keys fix as `_read_comments` above.
    if isinstance(links, dict):
        links = {int(number): value for number, value in links.items()}
    return links, error


def load_dependencies(repo_path: str, *, connection: dict | None = None):
    """Return (links, warnings) -- links is `_read_dependencies`'s result
    for the repo checked out at repo_path, or {} with a warning on failure.
    """
    warnings = []
    owner, name, error = _repo_identity(repo_path)
    links = {}
    if error:
        warnings.append(error)
    else:
        links, error = _read_dependencies(repo_path, owner, name, connection=connection)
        if error:
            warnings.append(f"GitHub dependency links are unavailable: {error}")
            links = links or {}
    return links, warnings


_DEPENDENCY_CACHE = {}


def cached_dependencies(repo: str, repo_path: str):
    """Cached `load_dependencies`, same lifetime as `cached_github`.

    Kept in memory only (not written to the disk cache file): the disk
    cache round-trips through JSON, which turns dict keys into strings,
    and this cache is keyed by issue number -- not worth the mismatch risk
    for data that is cheap to refetch within one `serve` run.

    Does not take a `connection` override -- `load_dependencies` resolves
    the fleet's Redis location itself (see `gh_cache.cached_gh_json`), so
    there is nothing for a caller at this level to supply.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _DEPENDENCY_CACHE.get(repo)
        if cached and now - cached[0] < GITHUB_CACHE_SECONDS:
            return cached[1]
        data = load_dependencies(repo_path)
        _DEPENDENCY_CACHE[repo] = (time.monotonic(), data)
        return data


def _dependency_cycles(edges: list[tuple[tuple[str, int], tuple[str, int]]]) -> list[list[dict]]:
    """Find cycles in the blocks graph (edge = blocker -> blocked issue).

    Walks every node once with an iterative DFS (no recursion limit) and
    reports a cycle the moment a back-edge closes one, so a bad link is
    reported instead of looping forever. Returns one list of
    {"repo", "number"} per distinct cycle; empty when the graph is a DAG.
    """
    outgoing: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for source, target in edges:
        outgoing.setdefault(source, []).append(target)
        outgoing.setdefault(target, [])
    cycles = []
    seen_signatures = set()
    state = {}  # node -> 0 unvisited, 1 in progress, 2 done
    sentinel = object()
    for start in outgoing:
        if state.get(start, 0):
            continue
        state[start] = 1
        path = [start]
        stack = [iter(outgoing[start])]
        while stack:
            target = next(stack[-1], sentinel)
            if target is sentinel:
                state[path.pop()] = 2
                stack.pop()
                continue
            if state.get(target, 0) == 1:
                cycle_nodes = path[path.index(target):] + [target]
                signature = frozenset(cycle_nodes)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append([{"repo": repo, "number": number} for repo, number in cycle_nodes])
                continue
            if state.get(target, 0) == 0:
                state[target] = 1
                path.append(target)
                stack.append(iter(outgoing[target]))
    return cycles


def build_dependency_dag(repo_links: dict[str, dict[int, dict]]) -> dict:
    """Combine each repo's blockedBy/blocking links into one cross-repo DAG.

    repo_links maps repo name -> cached_dependencies(repo, path)[0] for
    every repo you want in the graph -- call that once per repo first.

    Returns:
      {
        "repos": {repo: [{"number", "blockedBy", "blocking"}, ...]},
        "cycles": [[{"repo", "number"}, ...], ...],   # empty if acyclic
      }

    An edge always runs blocker -> blocked (the blocker must close first).
    A link that points at a repo lupin never fetched still shows up as an
    edge target; it just has no entry of its own under "repos".
    """
    edge_set = set()
    for repo, issues in repo_links.items():
        for number, links in issues.items():
            node = (repo, number)
            for target in links.get("blocking", []):
                edge_set.add((node, (target["repo"], target["number"])))
            for blocker in links.get("blockedBy", []):
                edge_set.add(((blocker["repo"], blocker["number"]), node))
    edges = list(edge_set)

    repos_out = {
        repo: [
            {
                "number": number,
                "blockedBy": list(links.get("blockedBy", [])),
                "blocking": list(links.get("blocking", [])),
            }
            for number, links in sorted(issues.items())
        ]
        for repo, issues in repo_links.items()
    }
    return {
        "repos": repos_out,
        "cycles": _dependency_cycles(edges),
    }


def cached_dependency_dag(repos: list[str], code_dir: str = CODE_DIR) -> dict:
    """Fetch (cached) dependency links for every repo in `repos` and build
    one combined DAG -- the one-call version of build_dependency_dag for a
    caller (the `--dag` CLI output, a future `/roadmap` panel) that just
    wants the combined result. Adds a "warnings" dict (repo -> messages)
    for repos whose links could not be read.
    """
    repo_links = {}
    warnings = {}
    for repo in repos:
        links, repo_warnings = cached_dependencies(repo, os.path.join(code_dir, repo))
        repo_links[repo] = links
        if repo_warnings:
            warnings[repo] = repo_warnings
    dag = build_dependency_dag(repo_links)
    dag["warnings"] = warnings
    return dag


def load_model(repo: str, repo_path: str, *, connection: dict | None = None):
    issues, comments, warnings = load_github(repo_path, connection=connection)
    ledger, ledger_error = _read_ledger(repo_path, connection=connection)
    if ledger_error:
        warnings.append(ledger_error)
    model = build_model(issues, comments, ledger, warnings, repo)
    model["repo"] = repo
    return model


# One lock per (repo, state): page builds run per-repo work on several
# threads, so a miss must not be fetched twice, and it must not hold the
# global cache lock while the (slow) fetch runs.
_FETCH_LOCKS: dict[tuple, threading.Lock] = {}


def _fetch_lock(key: tuple) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _FETCH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FETCH_LOCKS[key] = lock
        return lock


def _fetch_failed(data: tuple) -> bool:
    """Did this `load_github` result fail to read anything, and say why?

    `load_github` returns `(issues, comments, warnings)`. An empty issue
    list with a warning is an outage (bad auth, unreachable cache, no
    remote); an empty issue list with no warnings is a repo that really
    has none open. Shared by `cached_github`, which must not persist the
    first kind for an hour.
    """
    issues, _comments, warnings = data
    return not issues and bool(warnings)


def cached_github(
    repo: str,
    repo_path: str,
    state: str = "open",
    *,
    connection: dict | None = None,
):
    key = (repo, state)

    def _fresh():
        cached = _GITHUB_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < GITHUB_CACHE_SECONDS:
            return cached[1]
        return None

    data = _fresh()
    if data is not None:
        return data
    with _fetch_lock(key):
        data = _fresh()
        if data is not None:
            return data
        data = load_github(repo_path, state, connection=connection)
        if _fetch_failed(data):
            # A fetch that returned nothing *and* said why is not a
            # snapshot -- it is the outage. Caching it means one bad
            # minute (auth, a drained fetcher, a rate limit) keeps a repo
            # blank for the whole hour, which is exactly how an already
            # broken Roadmap stayed broken after its cause was fixed. A
            # repo with genuinely no issues has no warnings, so it still
            # caches normally.
            return data
        with _CACHE_LOCK:
            _GITHUB_CACHE[key] = (time.monotonic(), data)
        _persist_cache()
        return data


def cached_model(
    repo: str, repo_path: str, *, connection: dict | None = None
):
    issues, comments, warnings = cached_github(
        repo, repo_path, connection=connection
    )
    ledger, ledger_error = _read_ledger(repo_path, connection=connection)
    warnings = list(warnings)
    if ledger_error:
        warnings.append(ledger_error)
    model = build_model(issues, comments, ledger, warnings, repo)
    model["repo"] = repo
    return model


def cached_combined_model(
    repo: str, repo_path: str, *, connection: dict | None = None
):
    model = cached_model(repo, repo_path, connection=connection)
    closed_issues, closed_comments, _warnings = cached_github(
        repo, repo_path, "closed", connection=connection
    )
    model["closedNodes"] = build_model(
        closed_issues, closed_comments, [], repo=repo
    )["nodes"]
    return model


def _escape_attr(value: str) -> str:
    return html.escape(str(value), quote=True)


def _render_body(body: str) -> str:
    body = str(body or "")
    if not body:
        return "<p class='dim'>No description.</p>"
    parts = []
    offset = 0
    for match in IMAGE.finditer(body):
        url = match.group(2)
        attachment_id = github_attachment_id(url)
        if attachment_id:
            parts.append(html.escape(body[offset : match.start()]))
            parts.append(
                f"<img class='image-attachment' src='/image?id={attachment_id}' "
                f"alt='{_escape_attr(match.group(1) or 'image')}' loading='lazy'>"
            )
            offset = match.end()
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
            continue
        parts.append(html.escape(body[offset : match.start()]))
        parts.append(
            f"<a class='image-attachment' href='{_escape_attr(url)}' "
            f"target='_blank' rel='noopener'>View image attachment: "
            f"{html.escape(match.group(1) or 'image')}</a>"
        )
        offset = match.end()
    parts.append(html.escape(body[offset:]))
    return f"<div class='issue-body'>{''.join(parts)}</div>"


def render_issue_details(
    node: dict, include_comments: bool = True, quest_pick: bool = False
) -> str:
    comments = (
        sorted(
            node["comments"],
            key=lambda comment: comment.get("createdAt") or "",
            reverse=True,
        )
        if include_comments
        else []
    )
    comment_items = []
    for comment in comments:
        meta_parts = [
            html.escape(str(comment["author"])) if comment.get("author") else "",
            html.escape(_compact_time(comment["createdAt"]))
            if comment.get("createdAt")
            else "",
        ]
        meta = " · ".join(part for part in meta_parts if part)
        url = comment.get("url") or ""
        if url.startswith("https://github.com/"):
            link = f"<a href='{_escape_attr(url)}' target='_blank' rel='noopener'>view comment</a>"
            meta = f"{link} · {meta}" if meta else link
        meta_html = f"<p class=dim>{meta}</p>" if meta else ""
        comment_items.append(
            f"<li>{meta_html}{_render_body(comment['body'])}</li>"
        )
    if include_comments:
        if comment_items:
            activity = (
                f"<h4>Recent comments</h4>"
                f"<ol class='activity-comments'>{''.join(comment_items)}</ol>"
            )
        else:
            activity = "<h4>Recent comments</h4><p class='dim'>No comments yet.</p>"
    else:
        activity = ""
    dates = " · ".join(
        f"{label} {html.escape(_compact_time(node[key]))}"
        for label, key in (("Created", "createdAt"), ("Updated", "updatedAt"))
        if node.get(key)
    )
    date_html = f"<p class='dim'>{dates}</p>" if dates else ""
    labels = ", ".join(node.get("labels", []))
    labels_html = (
        f"<p class='dim issue-labels'>Labels: {html.escape(labels)}</p>" if labels else ""
    )
    full_title = str(node["title"])
    title = full_title[:80] + ("…" if len(full_title) > 80 else "")
    issue_title = f"#{node['number']} {html.escape(title)}"
    url = node.get("url") or ""
    if url.startswith("https://github.com/"):
        issue_title = (
            f"<a href='{_escape_attr(url)}' title='{_escape_attr(full_title)}' "
            f"target='_blank' rel='noopener'>{issue_title}</a>"
        )
    else:
        issue_title = f"<span title='{_escape_attr(full_title)}'>{issue_title}</span>"
    count = len(comments)
    comment_label = "comment" if count == 1 else "comments"
    details_label = (
        f"Details and activity · {count} {comment_label}"
        if include_comments
        else "Description"
    )
    label_data = _escape_attr(json.dumps(node.get("labels", []), ensure_ascii=False))
    digest_html = render_digest(node.get("digest"))
    digest_note = "<p class='dim'>Digest from the last ledger handoff.</p>" if digest_html else ""
    pick_html = (
        f"<label class='quest-pick' onclick='event.stopPropagation()'>"
        f"<input type=checkbox form=quest-start name=issue value='{node['number']}'> quest</label> "
        if quest_pick
        else ""
    )
    return (
        f"<details class='issue-details' data-browse-item data-labels='{label_data}'>"
        f"<summary>{pick_html}{issue_title} · "
        f"{details_label}</summary>"
        f"{digest_html}{digest_note}"
        f"{date_html}{labels_html}<h4>Description</h4>{_render_body(node['body'])}{activity}</details>"
    )

def _title_lines(title: str, width: int = 34, count: int = 3) -> list[str]:
    lines = []
    line = ""
    words = title.split()
    while words and len(lines) < count:
        word = words.pop(0)
        if line and len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line and len(lines) < count:
        lines.append(line)
    if words and lines:
        lines[-1] = lines[-1][: width - 1].rstrip() + "…"
    return lines


def _svg_text(x: int, y: int, text: str, size: int = 12, weight: int = 400) -> str:
    return f"<text x='{x}' y='{y}' font-size='{size}' font-weight='{weight}'>{html.escape(text)}</text>"


_QUEUE_GRAPH_CSS = """
.queue-scroll{overflow-x:auto;margin:1rem 0}
.queue-graph{display:block;max-width:none;color:var(--accent);font-family:inherit}
.queue-graph .lane{fill:var(--bg);stroke:var(--line)}
.queue-graph .card-node{fill:var(--card);stroke:var(--line)}
.queue-graph .card-node.next{stroke:var(--accent);stroke-width:2}
.queue-graph .dependency{fill:none;stroke:var(--accent);stroke-width:2;opacity:.75}
.queue-graph .parent-link{fill:none;stroke:var(--dim);stroke-width:1.4;stroke-dasharray:5 5;opacity:.75}
.queue-graph text{fill:var(--fg)}
"""

CROSS_REPO_EDGE_NOTE = (
    "Dependency and parent-link lines only connect issues within the same repository. "
    "Cross-repo references are not tracked."
)


def _render_queue_graph_svg(repo: str, model: dict) -> str:
    """Build the per-repo dependency graph as an <svg> string."""
    issue_map = {node["number"]: node for node in model["nodes"]}
    positions = {}
    lanes = []
    lane_x = [18, 318, 618, 918, 1218]
    card_w = 264
    row_gap = 82
    top = 64
    for lane_index, stage in enumerate(model["stages"]):
        lane_nodes = [issue_map[number] for number in stage["numbers"] if number in issue_map]
        lanes.append(lane_nodes)
        for row, node in enumerate(lane_nodes):
            positions[node["number"]] = (lane_x[lane_index], top + row * row_gap)
    height = max(460, max((top + len(lane) * row_gap + 25 for lane in lanes), default=460))
    svg = [
        f"<svg class='queue-graph' role='img' aria-label='Open issue roadmap for {html.escape(repo or '')}' viewBox='0 0 1510 {height}' width='1510' height='{height}'>"
    ]
    for edge in model["edges"]:
        start = positions.get(edge["from"])
        end = positions.get(edge["to"])
        if not start or not end:
            continue
        x1, y1 = start[0] + card_w, start[1] + 34
        x2, y2 = end[0], end[1] + 34
        bend = max(28, abs(x2 - x1) * 0.45)
        style = "dependency" if edge["kind"] == "depends" else "parent-link"
        svg.append(
            f"<path class='{style}' d='M{x1} {y1} C{x1 + bend} {y1}, {x2 - bend} {y2}, {x2} {y2}' marker-end='url(#arrow)'/>"
        )
    svg.append(
        "<defs><marker id='arrow' viewBox='0 0 10 10' refX='8' refY='5' markerWidth='6' markerHeight='6' orient='auto'><path d='M0 0L10 5L0 10z' fill='currentColor'/></marker></defs>"
    )
    for lane_index, lane in enumerate(lanes):
        x = lane_x[lane_index]
        svg.append(f"<rect class='lane' x='{x - 6}' y='10' width='276' height='{height - 20}' rx='7'/>")
        svg.append(_svg_text(x, 34, f"{model['stages'][lane_index]['name']} · {len(lane)}", 15, 700))
    for lane in lanes:
        for node in lane:
            x, y = positions[node["number"]]
            href = node["url"] if node["url"].startswith("https://github.com/") else "#"
            label = f"Open issue {node['number']} {node['title']}"
            svg.append(
                f"<a href='{_escape_attr(href)}' target='_blank' rel='noopener' aria-label='{_escape_attr(label)}'>"
            )
            card_class = "card-node next" if node["number"] in model["batch"] else "card-node"
            svg.append(f"<rect class='{card_class}' x='{x}' y='{y}' width='{card_w}' height='68' rx='6'/>")
            badge = f"#{node['number']} · {node['priority']} · {node['size']}"
            if node.get("loop"):
                badge += f" · {node['loop'][:9]}"
            if node.get("decision"):
                badge += f" · {node['decision']}"
            elif node.get("unresolvedDependency"):
                badge += " · blocker unknown"
            if any(label.casefold() == "needs-expert-decision" for label in node["labels"]):
                badge += " · needs expert decision"
            svg.append(_svg_text(x + 10, y + 17, badge, 11, 700))
            for line_index, line in enumerate(_title_lines(node["title"])):
                svg.append(_svg_text(x + 10, y + 35 + line_index * 13, line, 11))
            svg.append("</a>")
    svg.append("</svg>")
    return "".join(svg)


def _browse_script() -> str:
    return """
(function(){
  const filterKey='lupin-roadmap-filter';
  const orderKey='lupin-queue-order';
  const params=new URLSearchParams(location.search);
  const repo=document.getElementById('repo');
  const labels=document.getElementById('roadmap-labels');
  let saved={};
  try{saved=JSON.parse(localStorage.getItem(filterKey)||'{}')||{};}catch(_){}
  const explicitRepo=params.has('repo');
  const explicitLabels=params.has('labels');
  if(!explicitRepo&&saved.repo&&repo&&[...repo.options].some(o=>{
    return o.value===saved.repo||
      new URL(o.value,location.href).searchParams.get('repo')===saved.repo;
  })){
    if(repo.options[0].value.startsWith('/roadmap')){
      location.href='/roadmap?repo='+encodeURIComponent(saved.repo);
      return;
    }
    repo.value=saved.repo;
    repo.form.submit();
    return;
  }
  function currentLabels(){
    return labels?[...labels.selectedOptions].map(option=>option.value):[];
  }
  if(labels&&!explicitLabels&&Array.isArray(saved.labels)){
    [...labels.options].forEach(option=>option.selected=saved.labels.includes(option.value));
  }else if(labels&&explicitLabels){
    const selected=new Set(params.getAll('labels'));
    [...labels.options].forEach(option=>option.selected=selected.has(option.value));
  }
  function selectedRepo(){
    if(!repo)return '';
    if(repo.value.startsWith('/roadmap')){
      return new URL(repo.value,location.href).searchParams.get('repo')||'';
    }
    return repo.value;
  }
  function saveFilter(){
    try{localStorage.setItem(filterKey,JSON.stringify({
      repo:selectedRepo(),
      labels:currentLabels()
    }));}catch(_){}
  }
  if(repo)repo.addEventListener('change',saveFilter,true);
  if(labels)labels.addEventListener('change',()=>{
    saveFilter();
    const url=new URL(location.href);
    url.searchParams.delete('labels');
    currentLabels().forEach(label=>url.searchParams.append('labels',label));
    history.replaceState(null,'',url);
    updateBrowse();
  });
  const chunk=20;
  const input=document.getElementById('roadmap-search');
  const graph=document.querySelector('.queue-scroll');
  const browseLimits=new WeakMap();
  function updateBrowse(){
    const query=input?input.value.trim().toLocaleLowerCase():'';
    const selected=new Set(currentLabels());
    if(graph)graph.hidden=Boolean(query)||selected.size>0;
    document.querySelectorAll('[data-browse-group]').forEach(group=>{
      const items=[...group.querySelectorAll(':scope > [data-browse-item]')];
      const more=group.querySelector(':scope > .browse-more')||
        (group.nextElementSibling&&group.nextElementSibling.classList.contains('browse-more')
          ?group.nextElementSibling:null);
      if(!browseLimits.has(group)){
        browseLimits.set(group,chunk);
        if(more)more.addEventListener('click',()=>{
          browseLimits.set(group,browseLimits.get(group)+chunk);
          updateBrowse();
        });
      }
      const limit=browseLimits.get(group);
      let shown=0,total=0;
      items.forEach(item=>{
        let itemLabels=[];
        try{itemLabels=JSON.parse(item.dataset.labels||'[]');}catch(_){}
        const matchesLabels=[...selected].every(label=>itemLabels.includes(label));
        const matchesSearch=!query||item.textContent.toLocaleLowerCase().includes(query);
        if(!matchesLabels||!matchesSearch){item.hidden=true;return;}
        total++;
        const filtering=Boolean(query||selected.size);
        item.hidden=filtering?false:shown>=limit;
        if(!item.hidden)shown++;
      });
      const empty=(Boolean(query)||selected.size>0)&&total===0;
      group.hidden=empty;
      if(group.parentElement.classList.contains('activity-group'))
        group.parentElement.hidden=empty;
      if(more){
        more.hidden=Boolean(query)||selected.size>0||shown>=total;
        more.textContent='Show 20 more · '+(total-shown)+' remaining';
      }
    });
    document.querySelectorAll('[data-queue-item]').forEach(item=>{
      let itemLabels=[];
      try{itemLabels=JSON.parse(item.dataset.labels||'[]');}catch(_){}
      item.hidden=![...selected].every(label=>itemLabels.includes(label));
    });
  }
  if(input)input.addEventListener('input',updateBrowse);
  updateBrowse();
  let savedOrders={};
  try{savedOrders=JSON.parse(localStorage.getItem(orderKey)||'{}');}catch(_){}
  if(!savedOrders||Array.isArray(savedOrders)||typeof savedOrders!=='object')savedOrders={};
  document.querySelectorAll('[data-next-batch]').forEach(list=>{
    const scope=list.dataset.nextBatch;
    let queue=Array.isArray(savedOrders[scope])?savedOrders[scope]:[];
    const items=[...list.querySelectorAll(':scope > [data-queue-item]')];
    const eligible=new Set(items.map(item=>item.dataset.queueKey));
    queue=queue.filter(key=>eligible.has(String(key)));
    const savedOrder=new Map(queue.map((key,index)=>[String(key),index]));
    items.sort((a,b)=>{
      const ai=savedOrder.has(a.dataset.queueKey)?savedOrder.get(a.dataset.queueKey):Infinity;
      const bi=savedOrder.has(b.dataset.queueKey)?savedOrder.get(b.dataset.queueKey):Infinity;
      return ai-bi||Number(a.dataset.computedIndex)-Number(b.dataset.computedIndex);
    }).forEach(item=>list.appendChild(item));
    function persist(){
      savedOrders[scope]=[...list.querySelectorAll(':scope > [data-queue-item]')]
        .map(item=>item.dataset.queueKey);
      try{localStorage.setItem(orderKey,JSON.stringify(savedOrders));}catch(_){}
    }
    list.addEventListener('dragstart',event=>{
      const item=event.target.closest('[data-queue-item]');
      if(item){event.dataTransfer.setData('text/plain',item.dataset.queueKey);event.dataTransfer.effectAllowed='move';}
    });
    list.addEventListener('dragover',event=>{if(event.target.closest('[data-queue-item]'))event.preventDefault();});
    list.addEventListener('drop',event=>{
      const target=event.target.closest('[data-queue-item]');
      const dragged=items.find(item=>item.dataset.queueKey===event.dataTransfer.getData('text/plain'));
      if(!target||!dragged||target===dragged)return;
      event.preventDefault();
      const bounds=target.getBoundingClientRect();
      list.insertBefore(dragged,event.clientY<bounds.top+bounds.height/2?target:target.nextSibling);
      persist();
    });
    items.forEach(item=>item.addEventListener('dragend',persist));
    persist();
  });
  setTimeout(function(){location.reload()},305000);
})();
"""



BOARD_CSS = """
.roadmap-title{margin:0;padding:0 0 1rem;border:0}.roadmap-title h1{font:600 21px var(--mono)}
.roadmap-facts{color:var(--ink2);font-size:13px}
.roadmap-repos,.roadmap-status-tabs,.roadmap-filter-row{display:flex;gap:.5rem;align-items:center;overflow-x:auto;padding:.65rem 0;border-bottom:1px solid var(--line)}
.roadmap-repo,.roadmap-status-tabs a,.roadmap-chip,.roadmap-view a{display:inline-flex;gap:.45rem;white-space:nowrap;padding:.45rem .8rem;border:1px solid var(--line);border-radius:999px;background:var(--surface);color:var(--ink2);text-decoration:none;font-size:13px}
.roadmap-repo.active,.roadmap-status-tabs a.active,.roadmap-chip.active,.roadmap-view a.active{background:var(--ink);border-color:var(--ink);color:var(--surface)}
.roadmap-status-tabs{padding:0;border:0}.roadmap-status-tabs span{font:12px var(--mono);opacity:.8}
.roadmap-filter-row strong{color:var(--ink3);font:12px var(--mono);text-transform:uppercase;letter-spacing:.05em}
.roadmap-board-toolbar{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.75rem 0;border-bottom:1px solid var(--line)}
.roadmap-view{display:flex;border:1px solid var(--line);border-radius:10px;overflow:hidden;flex:none}
.roadmap-view a{border:0;border-radius:0}
.roadmap-layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;min-height:60vh}
.roadmap-board{padding:1rem;min-width:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1rem;align-content:start}
.roadmap-human{grid-column:1/-1;padding:1rem;background:var(--warnbg);border:1px solid var(--warnline);border-radius:16px}
.roadmap-human header,.roadmap-lane>header{display:flex;align-items:center;gap:.5rem;margin-bottom:.65rem}
.roadmap-human header h2,.roadmap-lane>header h2{margin:0;font:600 13px var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--ink2)}
.roadmap-human header span,.roadmap-lane>header span{margin-left:auto;color:var(--ink3);font:12px var(--mono)}
.roadmap-card-grid{display:grid;grid-template-columns:minmax(0,1fr);gap:.65rem;align-content:start}
.roadmap-human .roadmap-card-grid{grid-template-columns:repeat(3,minmax(0,1fr))}
.roadmap-lane{min-width:0}.roadmap-lane>header{padding:0 .2rem}
.roadmap-card{min-width:0;padding:.8rem;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 2px 0 var(--line)}
.roadmap-card-meta{display:flex;justify-content:space-between;gap:.35rem;color:var(--ink3);font:12px var(--mono)}
.roadmap-card-meta a{color:inherit;text-decoration:none}.roadmap-card h3{font-size:14px;line-height:1.4;font-weight:500;margin:.55rem 0}
.roadmap-card h3 a{color:var(--ink)}.roadmap-epic,.roadmap-tag,.roadmap-cap{display:inline-block;margin:.15rem .15rem .15rem 0;padding:.12rem .45rem;border-radius:6px;background:var(--track);color:var(--ink2);font-size:11px}
.roadmap-epic{background:var(--ink);color:var(--surface)}.roadmap-cap{background:var(--lavbg);color:var(--lav)}
.roadmap-blocker{margin:.5rem 0 0;color:var(--warn);font-size:12px}
.roadmap-detail{border-left:1px solid var(--line);padding:1.25rem;overflow:auto;min-width:0}
.roadmap-detail-top{display:flex;justify-content:space-between;gap:.5rem;color:var(--ink3);font:12px var(--mono)}
.roadmap-detail h2{margin:.6rem 0;font:600 18px var(--sans);text-transform:none;letter-spacing:0;color:var(--ink)}
.roadmap-detail .issue-details{margin-top:1rem}.roadmap-detail .issue-details>summary{display:none}
.roadmap-status{color:var(--ink2);font-size:12px}.roadmap-detail-empty{color:var(--ink3)}
.roadmap-relations{margin:1rem 0}.roadmap-relations h3{font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.06em;color:var(--ink3)}
.roadmap-relations ul{list-style:none;padding:0;margin:0;display:grid;gap:.35rem}
.roadmap-relations li{display:flex;gap:.5rem;align-items:center;padding:.55rem;border:1px solid var(--line);border-radius:10px;font-size:12px}
.roadmap-relations li a{flex:1;color:var(--ink)}.roadmap-relations li span{color:var(--ink3)}
.roadmap-card-meta .quest-pick{display:inline-flex;align-items:center;gap:.2rem}
.roadmap-board>.card{grid-column:1/-1;margin:0}
.roadmap-note{padding:0 1rem}
.updated-label{white-space:nowrap;color:var(--ink3);font:12px var(--mono)}
.repo-warnings{grid-column:1/-1;padding:.7rem 1rem;border:1px solid var(--warnline);background:var(--warnbg);color:var(--warnink);border-radius:12px;font-size:12px}
.repo-warnings summary{cursor:pointer;font:600 12px var(--mono)}
.repo-warnings ul{margin:.5rem 0 0;padding-left:1.1rem;display:grid;gap:.25rem}
@media(max-width:900px){.updated-label{display:none}}
@media(max-width:1000px){.roadmap-layout{grid-template-columns:1fr}.roadmap-detail{border-left:0;border-top:1px solid var(--line)}}
@media(max-width:760px){.roadmap-board{grid-template-columns:1fr}.roadmap-card-grid,.roadmap-human .roadmap-card-grid{grid-template-columns:1fr}.roadmap-facts{display:none}}
"""


def _repo_warnings(models: dict, repos: list[str]) -> str:
    """Per-repo fetch failures, shown instead of an empty board.

    A repo whose issues could not be read renders no cards, which is
    indistinguishable from a repo with no issues -- a silent zero that
    says nothing about whether the GitHub data ever arrived. The model
    already carries the reason; this puts it on screen.
    """
    parts = []
    for repo in repos:
        messages = (models.get(repo) or {}).get("warnings") or []
        if not messages:
            continue
        items = "".join(f"<li>{html.escape(message)}</li>" for message in messages)
        parts.append(
            f"<details class='repo-warnings'><summary>{html.escape(repo)}: "
            f"{len(messages)} warning{'s' if len(messages) != 1 else ''}</summary>"
            f"<ul>{items}</ul></details>"
        )
    return "".join(parts)


def board_fragment(repos: list[str], models: dict, query=None, quest_state=None) -> tuple[str, str]:
    """Render the cross-repository Roadmap board as body plus CSS, with no
    page wrapper -- the dashboard loads this fragment after first paint."""
    query = query or {}
    repo_filter = query.get("repo", "").strip()
    stage_filter = query.get("stage", "").strip()
    tag_filter = query.get("tag", "").strip()
    cap_filter = query.get("cap", "").strip()
    search = query.get("q", "").strip().casefold()
    selected_number = query.get("issue", "").strip()
    selected_repo = query.get("issue_repo", repo_filter).strip()
    active_repos = [repo for repo in repos if not repo_filter or repo == repo_filter]
    stage_names = ["Marked in flight", "Next batch", "Next up", "Later queue", "Blocked or held"]
    stage_items = {name: [] for name in stage_names}
    blocked_by_person = []
    issue_count = active_count = batch_count = 0
    label_names = set()
    capabilities = set()
    all_issues = {}
    for repo in active_repos:
        model = models.get(repo)
        if not model:
            continue
        issue_count += len(model["nodes"])
        active_count += model["activeCount"]
        batch_count += len(model["batch"])
        by_number = {node["number"]: node for node in model["nodes"]}
        parents = {edge["to"]: edge["from"] for edge in model["edges"] if edge["kind"] == "parent"}
        for stage in model["stages"]:
            for number in stage["numbers"]:
                node = by_number.get(number)
                if not node:
                    continue
                cap, _size = classify.classify(node)
                label_names.update(node.get("labels", []))
                capabilities.add(cap)
                all_issues[(repo, number)] = node
                item = (repo, node, cap, parents.get(number))
                if number in model.get("ownerBlocked", []):
                    blocked_by_person.append(item)
                else:
                    stage_items.setdefault(stage["name"], []).append(item)
        for number in model.get("ownerBlocked", []):
            node = by_number.get(number)
            if not node or (repo, number) in all_issues:
                continue
            cap, _size = classify.classify(node)
            label_names.update(node.get("labels", []))
            capabilities.add(cap)
            all_issues[(repo, number)] = node
            blocked_by_person.append((repo, node, cap, parents.get(number)))

    def matches(repo, node, cap, stage):
        stage_matches = (
            not stage_filter
            or stage_filter == stage
            or (stage_filter == "Ready" and stage in {"Next batch", "Next up"})
            or (stage_filter == "Blocked" and stage == "Blocked or held")
        )
        return (
            (not tag_filter or tag_filter in node.get("labels", []))
            and (not cap_filter or cap_filter == cap)
            and stage_matches
            and (not search or search in f"{repo} #{node['number']} {node['title']} {' '.join(node.get('labels', []))}".casefold())
        )

    def item_card(item, stage):
        repo, node, cap, parent = item
        url = node.get("url") or ""
        gh_url = url if url.startswith("https://github.com/") else "#"
        detail = f"/roadmap?view=board&repo={quote(repo, safe='')}&issue={node['number']}&issue_repo={quote(repo, safe='')}"
        epic = f"<span class='roadmap-epic'>Epic · #{parent}</span>" if parent else ""
        labels = "".join(
            f"<span class='roadmap-tag'>{html.escape(label)}</span>"
            for label in node.get("labels", [])
            if label.casefold() not in {"p0", "p1", "p2", "p3"}
        )
        blocked = (
            f"<p class='roadmap-blocker'>Needs a person: {html.escape(str(node.get('decision') or node.get('unresolvedDependency') or 'owner input'))}</p>"
            if stage == "Blocked or held" else ""
        )
        quest_pick = (
            f"<label class='quest-pick'><input type='checkbox' form='quest-start' name='issue' value='{node['number']}'> quest</label>"
            if repo_filter and repo == repo_filter else ""
        )
        return (
            f"<article class='roadmap-card'><div class='roadmap-card-meta'>"
            f"<a href='{_escape_attr(gh_url)}' target='_blank' rel='noopener'>{html.escape(repo)} · #{node['number']} ↗</a>"
            f"<span>{html.escape(node['priority'])} · {html.escape(node['size'])}</span>{quest_pick}</div>"
            f"<h3><a href='{_escape_attr(detail)}'>{html.escape(node['title'])}</a></h3>"
            f"{epic}<div class='roadmap-tags'>{labels}</div>"
            f"<span class='roadmap-cap'>{html.escape(cap)}</span>{blocked}</article>"
        )

    completed_count = sum(
        len((models.get(repo) or {}).get("closedNodes", [])) for repo in active_repos
    )

    def board_link(**overrides):
        params = {"view": "board", "repo": repo_filter, "tag": tag_filter, "cap": cap_filter}
        params.update(overrides)
        return "/roadmap?" + "&".join(
            f"{quote(key, safe='')}={quote(str(value), safe='')}"
            for key, value in params.items()
            if value
        )

    status_html = (
        f"<nav class='roadmap-status-tabs' aria-label='Issue status'>"
        f"<a class='{'active' if not stage_filter else ''}' href='{_escape_attr(board_link(stage=''))}'>Open <span>{issue_count}</span></a>"
        f"<a class='{'active' if stage_filter == 'Marked in flight' else ''}' href='{_escape_attr(board_link(stage='Marked in flight'))}'>In progress <span>{active_count}</span></a>"
        f"<a href='/roadmap?state=closed{('&amp;repo=' + quote(repo_filter, safe='')) if repo_filter else ''}'>Completed <span>{completed_count}</span></a>"
        "</nav>"
    )
    view_html = (
        f"<nav class='roadmap-view' aria-label='Roadmap view'><a class='active' href='{_escape_attr(board_link(stage=''))}'>Board</a>"
        f"<a href='/roadmap?view=list{('&amp;repo=' + quote(repo_filter, safe='')) if repo_filter else ''}'>List</a></nav>"
    )
    repo_links = [("", "All repos"), *((repo, repo) for repo in repos)]
    repo_html = "".join(
        f"<a class='roadmap-repo{' active' if value == repo_filter else ''}' href='/roadmap?view=board"
        f"{('&amp;repo=' + quote(value, safe='')) if value else ''}'>{html.escape(label)}</a>"
        for value, label in repo_links
    )
    tag_html = (
        f"<a class='roadmap-chip{' active' if not tag_filter else ''}' href='{_escape_attr(board_link(tag=''))}'>All tags</a>"
        + "".join(
            f"<a class='roadmap-chip{' active' if label == tag_filter else ''}' href='{_escape_attr(board_link(tag=label))}'>{html.escape(label)}</a>"
            for label in sorted(label_names, key=str.casefold)
        )
    )
    cap_html = "".join(
        f"<a class='roadmap-chip{' active' if cap == cap_filter else ''}' href='{_escape_attr(board_link(cap='' if cap == cap_filter else cap))}'>{html.escape('All capabilities' if not cap else cap)}</a>"
        for cap in ["", *LIST_CAPABILITIES]
    )
    board_lanes = []
    visible_human = [item for item in blocked_by_person if matches(item[0], item[1], item[2], "Blocked or held")]
    if visible_human and (not stage_filter or stage_filter in {"Blocked", "Blocked or held"}):
        board_lanes.append(
            "<section class='roadmap-human'><header><h2>Blocked issues · need a person</h2>"
            f"<span>{len(visible_human)}</span></header><div class='roadmap-card-grid'>"
            + "".join(item_card(item, "Blocked or held") for item in visible_human)
            + "</div></section>"
        )
    lane_groups = [
        ("Marked in flight", ["Marked in flight"]),
        ("Ready", ["Next batch", "Next up"]),
        ("Later", ["Later queue"]),
        ("Blocked or held", ["Blocked or held"]),
    ]
    for title, source_stages in lane_groups:
        if stage_filter and stage_filter not in source_stages and not (
            stage_filter == "Ready" and title == "Ready"
        ):
            continue
        items = [
            item
            for source_stage in source_stages
            for item in stage_items.get(source_stage, [])
            if matches(*item[:3], source_stage)
        ]
        if items:
            board_lanes.append(
                f"<section class='roadmap-lane'><header><h2>{html.escape(title)}</h2><span>{len(items)}</span></header>"
                f"<div class='roadmap-card-grid'>{''.join(item_card(item, title) for item in items)}</div></section>"
            )

    chosen = None
    try:
        chosen = all_issues.get((selected_repo, int(selected_number)))
    except (TypeError, ValueError):
        pass
    if chosen:
        model = models.get(selected_repo, {})
        stage_by_number = {
            number: stage["name"]
            for stage in model.get("stages", [])
            for number in stage["numbers"]
        }
        relation_rows = []
        for edge in model.get("edges", []):
            if chosen["number"] not in {edge["from"], edge["to"]}:
                continue
            other_number = edge["to"] if edge["from"] == chosen["number"] else edge["from"]
            other = all_issues.get((selected_repo, other_number))
            if not other:
                continue
            if edge["kind"] == "parent":
                relation = "contains" if edge["from"] == chosen["number"] else "part of"
            elif edge["kind"] == "split":
                relation = "split into" if edge["from"] == chosen["number"] else "split from"
            else:
                relation = "blocks" if edge["from"] == chosen["number"] else "blocked by"
            href = (
                f"/roadmap?view=board&repo={quote(selected_repo, safe='')}"
                f"&issue={other_number}&issue_repo={quote(selected_repo, safe='')}"
            )
            relation_rows.append(
                f"<li><span>{html.escape(relation)}</span><a href='{_escape_attr(href)}'>"
                f"#{other_number} {html.escape(other['title'])}</a>"
                f"<span>{html.escape(stage_by_number.get(other_number, 'open'))}</span></li>"
            )
        relations = (
            f"<section class='roadmap-relations'><h3>Relations</h3><ul>{''.join(relation_rows)}</ul></section>"
            if relation_rows else ""
        )
        details = render_issue_details(chosen).replace("<details class='issue-details'", "<details open class='issue-details'", 1)
        stage = stage_by_number.get(chosen["number"], "Blocked or held" if chosen["number"] in model.get("ownerBlocked", []) else "open")
        issue_panel = (
            f"<aside class='roadmap-detail'><div class='roadmap-detail-top'>{html.escape(selected_repo)} #{chosen['number']}"
            f"<a href='{_escape_attr(chosen.get('url') or '#')}' target='_blank' rel='noopener'>Open on GitHub ↗</a></div>"
            f"<h2>{html.escape(chosen['title'])}</h2><p class='roadmap-status'>{html.escape(chosen['priority'])} · "
            f"{html.escape(chosen['size'])} · {html.escape(stage)}</p>{relations}{details}</aside>"
        )
    else:
        issue_panel = "<aside class='roadmap-detail roadmap-detail-empty'><p>Select an issue to see its details.</p></aside>"
    quest_panel = _render_quest_section(repo_filter, quest_state) if repo_filter else ""
    repo_param = f"&amp;repo={quote(repo_filter, safe='')}" if repo_filter else ""
    body = (
        "<header class='roadmap-title'><h1>Roadmap</h1><span class='sp'></span>"
        f"<span class='roadmap-facts'>{issue_count} open issues · {active_count} in progress</span></header>"
        f"<nav class='roadmap-repos' aria-label='Repositories'>{repo_html}</nav>"
        f"<div class='roadmap-board-toolbar'>{status_html}{view_html}</div>"
        f"<nav class='roadmap-filter-row' aria-label='Tags'><strong>Tags</strong>{tag_html}</nav>"
        f"<nav class='roadmap-filter-row' aria-label='Capabilities'><strong>Capability</strong>{cap_html}</nav>"
        f"<div class='roadmap-layout'><main class='roadmap-board'>{_repo_warnings(models, active_repos)}{''.join(board_lanes) or '<p class=dim>No issues match these filters.</p>'}{quest_panel}</main>{issue_panel}</div>"
        f"<p class='dim roadmap-note'>{QUEUE_STAGE_NOTE} {LEDGER_STATUS_NOTE} Issue links open GitHub.</p>"
    )
    return body, BOARD_CSS


def render_combined_page(repos: list[str], models: dict, page_fn, query=None, quest_state=None) -> bytes:
    """Render the cross-repository Roadmap board as a full page."""
    body, css = board_fragment(repos, models, query, quest_state)
    return page_fn("Roadmap", body, css, "")


def skipped_warning(skipped: list[str]) -> str:
    """Warning text for the registry records a display read left out. Empty if none."""
    if not skipped:
        return ""
    return f"warning: skipped unreadable Redis record(s): {', '.join(sorted(skipped))}"


def _render_quest_section(repo: str | None, quest_state: dict | None) -> str:
    """The quest card: a "Start quest" button that submits whichever issue
    checkboxes (rendered elsewhere, via `form=quest-start`) are ticked, and,
    when `quest_state` names one already running, its progress and a "Stop
    quest" button. `quest_state` is `None` when the page has no `?quest=`
    id, or that id didn't resolve (see `Handler.quest_state` in serve.py).
    """
    repo_input = f"<input type=hidden name=repo value='{_escape_attr(repo or '')}'>"
    start_form = (
        f"<form id=quest-start class='row' action='/quest/start' method='post'>{repo_input}"
        "<button type='submit'>Start quest</button>"
        "<span class='dim'>Tick issues below, then start.</span></form>"
    )
    if not quest_state:
        return f"<div class='card'><h2>Quest</h2>{start_form}</div>"
    done = quest_state["done"]
    total = quest_state["total"]
    issue_list = ", ".join(f"#{n}" for n in quest_state["pending"] + done)
    progress_pill = "pill on" if total and len(done) == total else "pill"
    progress = (
        f"<div class='row'><span class='pill'>quest {_escape_attr(quest_state['id'])}</span>"
        f"<span class='pill'>{_escape_attr(quest_state['machine'] or '?')}</span>"
        f"<span class='{progress_pill}'>{len(done)}/{total} done</span>"
        f"<span class='pill'>{_escape_attr(quest_state['state'] or '?')}</span></div>"
        f"<p class='dim'>{_escape_attr(issue_list)}</p>"
    )
    stop_form = (
        f"<form class='row' action='/quest/stop' method='post'>{repo_input}"
        f"<input type=hidden name=id value='{_escape_attr(quest_state['id'])}'>"
        "<button type='submit'>Stop quest</button></form>"
    )
    warning = skipped_warning(quest_state.get("skipped", []))
    warning_html = (
        f"<p class='dim'>{_escape_attr(warning)}. Progress may be wrong.</p>" if warning else ""
    )
    return f"<div class='card'><h2>Quest</h2>{progress}{warning_html}{stop_form}{start_form}</div>"


def render_page(
    repo: str | None,
    repos: list[str],
    model: dict | None,
    page_fn,
    quest_state: dict | None = None,
) -> bytes:

    options = ["<option value=''>All repositories</option>"]
    for name in repos:
        selected = " selected" if name == repo else ""
        options.append(
            f"<option value='{_escape_attr(name)}'{selected}>{html.escape(name)}</option>"
        )
    option_html = "".join(options)
    title = f"Work queue roadmap: {repo}" if repo else "Choose a repository"
    css = ".queue-filter{display:flex;gap:.6rem;align-items:center;margin:1rem 0}.queue-filter select{font:inherit;padding:.35rem .55rem;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}"
    if model is None:
        body = (
            "<header><h1>Work queue roadmap</h1><span class='sp'></span><a href='/roadmap'>all repositories</a></header>"
            "<form class='queue-filter' action='/roadmap' method='get'><label for='repo'>Repository</label>"
            f"<select id='repo' name='repo'><option value=''>All repositories</option>{option_html}</select>"
            "<button type='submit'>Open queue</button></form>"
            "<p class='dim'>Select a loopable repository to view its open issue roadmap.</p>"
        )
        return page_fn(title, body, css, "")

    issue_map = {node["number"]: node for node in model["nodes"]}
    svg = _render_queue_graph_svg(repo, model)

    warnings = "".join(
        f"<div class='card warning'>{html.escape(message)}</div>"
        for message in model["warnings"]
    )
    if not model["acyclic"]:
        warnings += "<div class='card warning'>The dependency graph has a cycle. Check the issue links before dispatch.</div>"
    handoff = model.get("handoffStatus")
    handoff_fact = (
        f"<span class='pill'>Latest handoff: {html.escape(handoff)}</span>"
        if handoff
        else ""
    )
    detail_nodes = sorted(
        model["nodes"],
        key=lambda node: _parse_time(node.get("updatedAt"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    issue_details = [
        render_issue_details(node, quest_pick=True) for node in detail_nodes
    ]
    filter_labels = sorted({
        label for node in model["nodes"] for label in node.get("labels", [])
    })
    label_options = "".join(
        f"<option value='{_escape_attr(label)}'>{html.escape(label)}</option>"
        for label in filter_labels
    )
    next_batch = []
    for index, number in enumerate(model["batch"]):
        node = issue_map.get(number)
        if node:
            next_batch.append(
                f"<li data-queue-item data-queue-key='{number}' "
                f"data-labels='{_escape_attr(json.dumps(node.get('labels', []), ensure_ascii=False))}' "
                f"data-computed-index='{index}' draggable='true'>"
                f"#{number} {html.escape(node['title'])}</li>"
            )
    queue_html = (
        f"<h2>Next batch · computed order</h2><ol data-next-batch='{_escape_attr(repo or '')}'>"
        + "".join(next_batch)
        + "</ol>"
    )
    owner_blocked_items = []
    for number in model.get("ownerBlocked", []):
        node = issue_map.get(number)
        if not node:
            continue
        href = node["url"] if node["url"].startswith("https://github.com/") else "#"
        owner_blocked_items.append(
            f"<li><a href='{_escape_attr(href)}' target='_blank' rel='noopener'>"
            f"#{number} {html.escape(node['title'])}</a></li>"
        )
    owner_blocked_html = (
        "<h2>Owner blocked</h2>"
        "<p class='dim'>Needs the owner's decision. Nothing else to do until then.</p>"
        f"<ul class='owner-blocked-list'>{''.join(owner_blocked_items)}</ul>"
    ) if owner_blocked_items else ""
    body = (
        f"<header><h1>Work queue roadmap</h1><span class='sp'></span><a href='/roadmap'>all repositories</a> · "
        f"<a href='/roadmap?repo={quote(repo or '', safe='')}&amp;state=closed'>completed issues</a></header>"
        f"<form class='queue-filter' action='/roadmap' method='get'><label for='repo'>Repository</label>"
        f"<select id='repo' name='repo' onchange='this.form.submit()'>{option_html}</select>"
        "<label for='roadmap-labels'>Labels</label>"
        f"<select id='roadmap-labels' multiple>{label_options}</select></form>"
        "<div class='browse-search'><label for='roadmap-search'>Search issues</label>"
        "<input id='roadmap-search' type='search' placeholder='Titles, labels, descriptions, comments'></div>"
        f"{queue_html}"
        f"<p class='dim'>GitHub data refreshes hourly; this page reloads about every five "
        f"minutes and reads ledger events from Redis.</p>"
        f"<div class='queue-facts'><span class='pill'>{len(model['nodes'])} open issues</span>"
        f"<span class='pill'>{model['activeCount']} marked in flight</span>"
        f"<span class='pill'>{len(model['batch'])} next batch</span>{handoff_fact}</div>"
        f"{_render_quest_section(repo, quest_state)}"
        f"{warnings}<h2>Issue roadmap</h2>"
        "<p class='dim'>Line key: solid lines show dependencies; dashed lines show parent or split links. Cards link to GitHub.</p>"
        f"<div class='queue-scroll'>{svg}</div>"
        f"<p class='dim'>{QUEUE_STAGE_NOTE}</p>"
        f"{owner_blocked_html}"
        "<h2>Issue details and recent activity</h2>"
        f"<div class='queue-comment-list' data-browse-group>{''.join(issue_details)}"
        "<button class='browse-more' type='button' hidden></button></div>"
        f"<p class='dim'>{LEDGER_STATUS_NOTE}</p>"
    )
    css += _QUEUE_GRAPH_CSS
    css += """
.queue-facts{display:flex;gap:.5rem;flex-wrap:wrap;margin:1rem 0}
.queue-filter select,.browse-search input{font:inherit;padding:.35rem .55rem;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
.browse-search{display:flex;gap:.6rem;align-items:center;margin:1rem 0}
.browse-search input{flex:1;min-width:12rem}
.browse-more{font:inherit;margin:.3rem 0;padding:.3rem .55rem;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}
.warning{border-color:var(--warn);color:var(--warn)}
.queue-comment-list{display:grid;gap:.35rem}
.issue-body img.image-attachment{display:block;max-width:100%;max-height:640px;height:auto;object-fit:contain;margin:.6rem 0;border-radius:6px}
.quest-pick{display:inline-flex;align-items:center;gap:.25rem;font-size:.8rem;color:var(--ink2);margin-right:.4rem}
"""
    css += DIGEST_CSS
    return page_fn(title, body, css, _browse_script())

def render_completed_page(repo: str | None, repos: list[str], issues_by_repo: dict, page_fn) -> bytes:
    selected_repos = [repo] if repo else repos
    groups = []
    warnings = []
    for name in selected_repos:
        issues, comments, repo_warnings = issues_by_repo.get(name, ([], {}, []))
        warnings.extend(f"{name}: {warning}" for warning in repo_warnings)
        items = []
        for issue in sorted(issues, key=lambda row: row.get("updatedAt") or "", reverse=True):
            node = dict(issue)
            node["labels"] = _label_names(issue)
            node["comments"] = comments.get(issue["number"], [])
            items.append(render_issue_details(node, include_comments=False))
        groups.append(
            f"<section><h2>{html.escape(name)} · {len(items)} completed issues</h2>"
            f"<div class='queue-comment-list' data-browse-group>{''.join(items)}"
            "<button class='browse-more' type='button' hidden></button></div></section>"
        )
    target = f"/roadmap?repo={quote(repo, safe='')}" if repo else "/roadmap"
    heading = (
        f"Completed issues: {html.escape(repo)}"
        if repo
        else "Completed issues across repositories"
    )
    warning_cards = "".join(
        f"<div class='card warning'>{html.escape(warning)}</div>"
        for warning in warnings
    )
    body = (
        f"<header><h1>{heading}</h1><span class='sp'></span>"
        "<a href='/roadmap'>all repositories</a></header>"
        f"<p><a href='{target}'>Back to active queue</a></p>"
        "<div class='browse-search'><label for='roadmap-search'>Search issues</label>"
        "<input id='roadmap-search' type='search' placeholder='Titles, labels, descriptions'></div>"
        f"{warning_cards}{''.join(groups) or '<p class=dim>No completed issues.</p>'}"
    )
    css = ".browse-search{display:flex;gap:.6rem;align-items:center;margin:1rem 0}.browse-search input{flex:1;min-width:12rem;font:inherit;padding:.35rem .55rem;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}.queue-comment-list{display:grid;gap:.35rem}.browse-more{font:inherit;margin:.3rem 0;padding:.3rem .55rem;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer}"
    return page_fn(heading, body, css, _browse_script())


# Larger backlogs open in List. The Board stays readable with a few dozen issues.
BOARD_MAX_ISSUES = 36
LIST_PAGE_SIZE = 50
EPIC_PAGE_SIZE = 10
LIST_PRIORITIES = ["P0", "P1", "P2", "P3"]
# Keys of model-tiers.json (route.py's lookup table). The mockup calls this
# axis "capability"; classify.classify() already sorts an issue into one of
# these, so the List view's capability filter reuses that, not a new label.
LIST_CAPABILITIES = ["coding", "frontend-ui", "translation", "prose", "cad-spatial", "general"]
LIST_SORTS = [
    ("priority", "Priority"),
    ("updated", "Recently updated"),
    ("number", "Issue number"),
]


def _list_rows(
    repos: list[str], models: dict, repo_filter: str, prio_filter: str, cap_filter: str, search: str
) -> list[dict]:
    """Flatten every repo's open issues into one row per issue.

    Each row also carries its epic (from the "parent" edges build_model()
    already derives for epic-labeled issues) so render_list_page() can group
    by epic without re-parsing issue bodies.
    """
    search = search.strip().lower()
    rows = []
    for repo in repos:
        if repo_filter and repo != repo_filter:
            continue
        model = models.get(repo)
        if not model:
            continue
        by_number = {node["number"]: node for node in model["nodes"]}
        parent_of = {
            edge["to"]: edge["from"] for edge in model["edges"] if edge["kind"] == "parent"
        }
        stage_of = {
            number: stage["name"]
            for stage in model["stages"]
            for number in stage["numbers"]
        }
        owner_blocked = set(model.get("ownerBlocked", []))
        for node in model["nodes"]:
            if prio_filter and node["priority"] != prio_filter:
                continue
            capability, _size = classify.classify(node)
            if cap_filter and capability != cap_filter:
                continue
            if search:
                haystack = " ".join(
                    [str(node["number"]), node["title"], " ".join(node["labels"])]
                ).lower()
                if search not in haystack:
                    continue
            epic_number = parent_of.get(node["number"])
            epic_node = by_number.get(epic_number) if epic_number else None
            stage = stage_of.get(node["number"], "")
            if node["number"] in owner_blocked or stage == "Blocked or held":
                stage = "Blocked"
            elif stage in {"Next batch", "Next up"}:
                stage = "Ready"
            elif stage == "Marked in flight":
                stage = "In progress"
            elif stage == "Later queue":
                stage = "Later"
            rows.append(
                {
                    "repo": repo,
                    "number": node["number"],
                    "title": node["title"],
                    "url": node["url"],
                    "priority": node["priority"],
                    "capability": capability,
                    "stage": stage,
                    "ownerBlocked": node["number"] in owner_blocked,
                    "updatedAt": node.get("updatedAt") or node.get("createdAt"),
                    "epicNumber": epic_number,
                    "epicTitle": epic_node["title"] if epic_node else None,
                }
            )
    return rows


def _sort_list_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "updated":
        return sorted(
            rows,
            key=lambda row: _parse_time(row["updatedAt"])
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
    if sort == "number":
        return sorted(rows, key=lambda row: (row["repo"], row["number"]))

    def priority_rank(row):
        digits = row["priority"][1:]
        return int(digits) if digits.isdigit() else 9

    return sorted(rows, key=lambda row: (priority_rank(row), row["repo"], row["number"]))


LIST_CSS = """
.roadmap-view{display:flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.roadmap-view a{padding:.4rem .8rem;color:var(--ink2);text-decoration:none}
.roadmap-view a.active{background:var(--surface);font-weight:600;color:var(--ink)}
.list-stage-row{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.5rem 0;border-bottom:1px solid var(--line)}
.list-stage-tabs{display:flex;gap:.4rem;overflow-x:auto}
.list-stage-tabs a{white-space:nowrap;padding:.45rem .7rem;border-radius:9px;color:var(--ink2);text-decoration:none;font-size:13px}
.list-stage-tabs a.active{background:var(--surface);color:var(--ink);font-weight:600}
.list-stage-tabs b{font:12px var(--mono);margin-left:.2rem}
.list-owner-blocked{white-space:nowrap;border:1px solid var(--warnline);background:var(--warnbg);color:var(--warnink);border-radius:10px;padding:.4rem .7rem;font-size:13px}
.queue-filter{display:flex;gap:.55rem;align-items:center;flex-wrap:wrap;margin:.75rem 0;padding-bottom:.75rem;border-bottom:1px solid var(--line)}
.queue-filter label{font-size:12px;color:var(--ink3)}
.queue-filter select,.queue-filter input{font:inherit;padding:.4rem .55rem;border:1px solid var(--line);border-radius:8px;background:var(--surface);color:var(--ink)}
.list-table{display:grid;gap:0;background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:0 1rem;overflow:auto}
.list-row{display:grid;grid-template-columns:70px minmax(180px,1fr) 124px 120px 100px 118px 64px 90px;gap:10px;padding:.55rem 0;border-top:1px solid var(--line2);align-items:center;font-size:.85rem;min-width:900px}
.list-issue{display:flex;align-items:center;gap:.45rem}
.list-issue input{width:16px;height:16px;margin:0}
.list-head{position:sticky;top:0;background:var(--surface);font:12px var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--ink3);border-top:0}
.list-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.list-group{margin:.3rem 0}
.list-group-header{display:block;padding:.55rem 0;font-weight:600;text-decoration:none;color:var(--ink)}
.list-group-header:hover{text-decoration:underline}
.list-more{display:block;padding:.3rem 0 .3rem 1.4rem}
.list-pager{display:flex;align-items:center;gap:.8rem;margin:.8rem 0;padding:.7rem 1rem;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
.list-pager .sp{flex:1}
.repo-warnings{margin:.75rem 0;padding:.7rem 1rem;border:1px solid var(--warnline);background:var(--warnbg);color:var(--warnink);border-radius:12px;font-size:12px}
.repo-warnings summary{cursor:pointer;font:600 12px var(--mono)}
.repo-warnings ul{margin:.5rem 0 0;padding-left:1.1rem;display:grid;gap:.25rem}
@media(max-width:760px){.list-stage-row{align-items:flex-start;flex-direction:column}.queue-filter{align-items:stretch}.queue-filter input,.queue-filter select{max-width:100%}}
"""


def list_fragment(repos: list[str], models: dict, query: dict, quest_state=None) -> tuple[str, str]:
    """Render the Roadmap List view as body plus CSS, with no page wrapper.

    The Board groups a few dozen issues by queue stage. List filters and sorts
    server-side and returns 50 rows per page. This keeps large roadmaps small
    enough to render.
    """
    repo_filter = query.get("repo", "").strip()
    prio_filter = query.get("prio", "").strip()
    cap_filter = query.get("cap", "").strip()
    sort = query.get("sort", "priority").strip()
    search = query.get("q", "").strip()
    grouped = query.get("group", "").strip() == "epic"
    stage_filter = query.get("stage", "").strip()
    owner_filter = query.get("owner", "").strip() == "1"

    rows = _sort_list_rows(
        _list_rows(repos, models, repo_filter, prio_filter, cap_filter, search), sort
    )
    stage_counts = {
        stage: sum(row["stage"] == stage for row in rows)
        for stage in ("In progress", "Blocked", "Ready", "Later")
    }
    blocked_owner_count = sum(row["ownerBlocked"] for row in rows)
    if stage_filter:
        rows = [row for row in rows if row["stage"] == stage_filter]
    if owner_filter:
        rows = [row for row in rows if row["ownerBlocked"]]

    base_filters = {
        "view": "list",
        "repo": repo_filter,
        "prio": prio_filter,
        "cap": cap_filter,
        "sort": sort,
        "q": search,
        "group": "epic" if grouped else "",
        "stage": stage_filter,
        "owner": "1" if owner_filter else "",
    }

    def list_link(**overrides) -> str:
        params = dict(base_filters)
        params.update(overrides)
        pairs = [(key, value) for key, value in params.items() if value]
        return "/roadmap?" + "&".join(
            f"{quote(key, safe='')}={quote(str(value), safe='')}" for key, value in pairs
        )

    def options(values, selected, labels=None):
        html_options = []
        for value in values:
            label = labels[value] if labels else value
            marker = " selected" if value == selected else ""
            html_options.append(
                f"<option value='{_escape_attr(value)}'{marker}>{html.escape(label)}</option>"
            )
        return "".join(html_options)

    filters_form = (
        "<form class='queue-filter' action='/roadmap' method='get'>"
        "<input type='hidden' name='view' value='list'>"
        f"<input type='hidden' name='stage' value='{_escape_attr(stage_filter)}'>"
        f"<input type='hidden' name='owner' value='{'1' if owner_filter else ''}'>"
        "<label for='list-q'>Search</label>"
        f"<input id='list-q' type='search' name='q' value='{_escape_attr(search)}' "
        "placeholder='Title, #number or label'>"
        "<label for='list-repo'>Repo</label>"
        f"<select id='list-repo' name='repo'><option value=''>All repos</option>"
        f"{options(repos, repo_filter)}</select>"
        "<label for='list-prio'>Priority</label>"
        f"<select id='list-prio' name='prio'><option value=''>Any priority</option>"
        f"{options(LIST_PRIORITIES, prio_filter)}</select>"
        "<label for='list-cap'>Capability</label>"
        f"<select id='list-cap' name='cap'><option value=''>Any capability</option>"
        f"{options(LIST_CAPABILITIES, cap_filter)}</select>"
        "<label for='list-sort'>Sort</label>"
        f"<select id='list-sort' name='sort'>"
        f"{options([value for value, _label in LIST_SORTS], sort, dict(LIST_SORTS))}</select>"
        "<button type='submit'>Apply</button>"
        f"<a href='{_escape_attr(list_link(group='' if grouped else 'epic'))}'>"
        f"{'Ungroup' if grouped else 'Group by epic'}</a>"
        "</form>"
    )
    stage_tabs = (
        "<nav class='list-stage-tabs' aria-label='Issue stages'>"
        f"<a class='{'active' if not stage_filter else ''}' href='{_escape_attr(list_link(stage='', owner=''))}'>All <b>{sum(stage_counts.values())}</b></a>"
        + "".join(
            f"<a class='{'active' if stage_filter == stage else ''}' href='{_escape_attr(list_link(stage=stage, owner=''))}'>{stage} <b>{stage_counts[stage]}</b></a>"
            for stage in ("In progress", "Blocked", "Ready", "Later")
        )
        + "</nav>"
        f"<a class='list-owner-blocked' href='{_escape_attr(list_link(stage='Blocked', owner='1'))}'>"
        f"{blocked_owner_count} blocked need a person</a>"
    )

    head = (
        "<div class='list-row list-head'><span>Issue</span><span>Title</span>"
        "<span>Epic</span><span>Capability</span><span>Repo</span>"
        "<span>Stage</span><span>Pri</span><span>Updated</span></div>"
    )

    def row_html(row: dict) -> str:
        href = row["url"] if row["url"].startswith("https://github.com/") else "#"
        epic = f"#{row['epicNumber']} {row['epicTitle']}" if row["epicNumber"] else ""
        quest_pick = (
            f"<input type='checkbox' form='quest-start' name='issue' value='{row['number']}' aria-label='Select issue {row['number']} for quest'>"
            if repo_filter and row["repo"] == repo_filter else ""
        )
        return (
            "<div class='list-row'>"
            f"<span class='list-issue'>{quest_pick}<a href='{_escape_attr(href)}' target='_blank' rel='noopener'>#{row['number']}</a></span>"
            f"<span class='list-title'>{html.escape(row['title'])}</span>"
            f"<span class='dim'>{html.escape(epic)}</span>"
            f"<span class='pill'>{html.escape(row['capability'])}</span>"
            f"<span class='dim'>{html.escape(row['repo'])}</span>"
            f"<span class='dim'>{html.escape(row['stage'])}</span>"
            f"<span class='pill'>{html.escape(row['priority'])}</span>"
            f"<span class='dim'>{html.escape(_compact_time(row['updatedAt']))}</span>"
            "</div>"
        )

    if grouped:
        epics: dict[tuple[str, int], dict] = {}
        ungrouped = []
        for row in rows:
            if row["epicNumber"]:
                key = (row["repo"], row["epicNumber"])
                bucket = epics.setdefault(key, {"title": row["epicTitle"], "rows": []})
                bucket["rows"].append(row)
            else:
                ungrouped.append(row)

        open_key = query.get("open", "").strip()
        try:
            shown = max(EPIC_PAGE_SIZE, int(query.get("shown", "") or EPIC_PAGE_SIZE))
        except ValueError:
            shown = EPIC_PAGE_SIZE

        def group_section(key: str, title: str, group_rows: list[dict]) -> str:
            is_open = key == open_key
            toggle_href = list_link(open="" if is_open else key, shown="")
            header = (
                f"<a class='list-group-header' href='{_escape_attr(toggle_href)}'>"
                f"{'▾' if is_open else '▸'} {html.escape(title)} "
                f"<span class='dim'>({len(group_rows)} issues)</span></a>"
            )
            if not is_open:
                return f"<div class='list-group'>{header}</div>"
            visible = group_rows[:shown]
            more = ""
            remaining = len(group_rows) - len(visible)
            if remaining > 0:
                more_href = list_link(open=key, shown=str(shown + EPIC_PAGE_SIZE))
                more = (
                    f"<a class='list-more' href='{_escape_attr(more_href)}'>"
                    f"Show {EPIC_PAGE_SIZE} more · {remaining} remaining</a>"
                )
            return (
                f"<div class='list-group'>{header}"
                f"{''.join(row_html(row) for row in visible)}{more}</div>"
            )

        sections = [
            group_section(f"{repo}:{number}", title["title"] or f"#{number}", title["rows"])
            for (repo, number), title in sorted(
                epics.items(), key=lambda item: (-len(item[1]["rows"]), item[0])
            )
        ]
        if ungrouped:
            sections.append(group_section("none", "No epic", ungrouped))
        table_html = head + (
            "".join(sections) if sections else "<p class='dim'>No issues match these filters.</p>"
        )
        epic_count = len(epics) + (1 if ungrouped else 0)
        footer = (
            f"<div class='list-pager'>{len(rows)} issues in {epic_count} epics. "
            f"Open an epic to see its issues, {EPIC_PAGE_SIZE} at a time.</div>"
        )
    else:
        total = len(rows)
        try:
            page_num = max(1, int(query.get("page", "") or 1))
        except ValueError:
            page_num = 1
        start = (page_num - 1) * LIST_PAGE_SIZE
        if start >= total and total:
            page_num = 1
            start = 0
        page_rows = rows[start : start + LIST_PAGE_SIZE]
        has_prev = page_num > 1
        has_next = start + LIST_PAGE_SIZE < total
        range_text = (
            f"{start + 1}–{min(start + LIST_PAGE_SIZE, total)}" if page_rows else "0"
        )
        prev_link = (
            f"<a href='{_escape_attr(list_link(page=str(page_num - 1)))}'>Prev</a>"
            if has_prev
            else "<span class='dim'>Prev</span>"
        )
        next_link = (
            f"<a href='{_escape_attr(list_link(page=str(page_num + 1)))}'>Next</a>"
            if has_next
            else "<span class='dim'>Next</span>"
        )
        table_html = head + (
            "".join(row_html(row) for row in page_rows)
            if page_rows
            else "<p class='dim'>No issues match these filters.</p>"
        )
        footer = (
            "<div class='list-pager'>"
            f"<span>Showing <b>{range_text}</b> of {total}</span>"
            f"<span class='sp'></span>{prev_link}{next_link}</div>"
        )

    quest_panel = _render_quest_section(repo_filter, quest_state) if repo_filter else ""
    board_link = "/roadmap?view=board" + (
        f"&amp;repo={quote(repo_filter, safe='')}" if repo_filter else ""
    )
    list_link_current = "/roadmap?view=list" + (
        f"&amp;repo={quote(repo_filter, safe='')}" if repo_filter else ""
    )
    body = (
        "<header><h1>Roadmap</h1><span class='sp'></span>"
        f"<span class='roadmap-view'><a href='{board_link}'>Board</a><a class='active' href='{list_link_current}'>List</a></span></header>"
        f"<div class='list-stage-row'>{stage_tabs}</div>"
        f"{filters_form}"
        f"{_repo_warnings(models, [repo for repo in repos if not repo_filter or repo == repo_filter])}"
        f"<div class='list-table'>{table_html}</div>"
        f"{quest_panel}{footer}"
    )
    return body, LIST_CSS


def render_list_page(repos: list[str], models: dict, page_fn, query: dict, quest_state=None) -> bytes:
    """Render the Roadmap List view as a full page."""
    body, css = list_fragment(repos, models, query, quest_state)
    return page_fn("Work across repositories · list", body, css, "")


def repository_names(rows: list[dict]) -> list[str]:
    return [row["repo"] for row in rows if row.get("loopable")]

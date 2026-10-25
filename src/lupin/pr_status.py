"""`lupin pr-status` -- one read-only view of every open pull request.

The whole queue comes from one `gh pr list` call. The command never makes one
`gh pr view` call per pull request. The issue asks for one view without
repeated `gh` calls. `review-route --prefetch` is the opposite case: it
fetches one item that the caller names. A queue view would have to prefetch
every item first, then fetch it again for the table. One list call returns
the same facts for every open pull request.

The command reads claims from Redis. It does not fail when Redis is
unavailable. The claim column shows `unknown` and a warning follows the
table instead.

`gh pr list` returns at most 100 comments and 100 reviews for each pull
request. This view shows what it returns; it does not page or fetch more.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import redis

from . import claims
from . import machines
from .slots import CoordinatorUnreachable

PR_FIELDS = (
    "number,title,body,updatedAt,isDraft,headRefName,headRefOid,mergeable,"
    "reviewDecision,reviews,comments,statusCheckRollup,reviewRequests"
)

# Same redaction as roadmap.py's _run_json: a `gh` error can echo a token
# back (for example, in a URL) if auth is misconfigured.
_TOKEN_RE = re.compile(r"(?:gh[oprsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")

FAILING_CHECKS = frozenset(
    {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
PENDING_CHECKS = frozenset(
    {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", "STALE"}
)

# A pull request body links its issue with `Closes #N`, `Fixes #N`, or
# `Resolves #N`. The claim column follows that link because that is where
# the loop's claim lives.
LINKED_ISSUE_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", re.IGNORECASE)

# An informal approval is a comment line that says "Review verdict: APPROVE"
# or "Verdict: ship" (case-insensitive). The matched line is kept in the
# output so a human can check the wording.
INFORMAL_RE = re.compile(
    r"^\s*(?:review\s+verdict|verdict)\s*:\s*(?:approve|ship)\b", re.IGNORECASE
)

PREVIEW_CHARS = 160


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", text)


def _run_gh(argv: list[str], timeout: int = 60):
    """Run `gh` and return `(stdout, error)`; exactly one is None."""
    try:
        result = subprocess.run(
            ["gh", *argv], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return None, "gh is not installed"
    except OSError as error:
        return None, f"gh could not start: {error}"
    except subprocess.TimeoutExpired:
        return None, f"gh timed out after {timeout} seconds"
    if result.returncode:
        error = (result.stderr or result.stdout or "gh failed").strip()
        return None, _redact(error)
    return result.stdout, None


def _run_gh_json(argv: list[str], timeout: int = 60):
    """Run `gh` and decode its JSON; return `(data, error)`."""
    output, error = _run_gh(argv, timeout=timeout)
    if error:
        return None, error
    try:
        return json.loads(output), None
    except json.JSONDecodeError as error:
        return None, f"invalid JSON from gh: {error}"


def _resolve_repo():
    """Ask `gh` for `OWNER/REPO` in the current directory."""
    output, error = _run_gh(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    if error:
        return None, error
    repo = (output or "").strip()
    if not repo:
        return None, "gh did not name the current repository"
    return repo, None


def fetch_open_prs(repo: str):
    """Fetch every open pull request with one `gh pr list` call."""
    return _run_gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            PR_FIELDS,
        ]
    )


def _read_claims(repo: str, connection: dict | None = None):
    """Return `(claims, warning)`; a Redis failure never fails the command."""
    try:
        resolved = machines.resolve_connection(**(connection or {}))
        return claims.claims_for([repo], **resolved), None
    except (
        CoordinatorUnreachable,
        redis.exceptions.NoPermissionError,
        redis.exceptions.AuthenticationError,
    ) as error:
        return {}, f"claim data is unavailable: {error}. Showing claim=unknown."


def _login(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return "?"
    return entry.get("login") or "?"


def _when(value) -> str:
    return str(value or "")


def _preview(text, *, full: bool) -> str:
    """Collapse whitespace, then cut to a short one-line preview."""
    collapsed = " ".join(str(text or "").split())
    if full or len(collapsed) <= PREVIEW_CHARS:
        return collapsed
    return collapsed[: PREVIEW_CHARS - 1].rstrip() + "…"


def _merge(pr: dict) -> str:
    value = pr.get("mergeable")
    if value == "CONFLICTING":
        return "conflict"
    if value == "MERGEABLE":
        return "clean"
    return "unknown"


def _check_time(check: dict) -> datetime | None:
    """Return the time used to order two runs of one check, or `None`.

    The order is by start, as `gh pr checks` orders them. So `startedAt` is
    first, then `completedAt`, then `createdAt`. `gh` reports `completedAt`
    as `"0001-01-01T00:00:00Z"` for a check that has not completed, so the
    sentinel must not outrank a re-run's real `startedAt`. An empty, zero, or
    unparsable value counts as no time.
    """
    for field in ("startedAt", "completedAt", "createdAt"):
        value = str(check.get(field) or "").strip()
        if not value or value.startswith("0001-01-01T"):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _dedupe_checks(rollup: list) -> list:
    """Keep the newest entry per `(workflowName, name or context)`.

    `statusCheckRollup` can hold an older FAILURE and a newer SUCCESS for
    the same check. A `StatusContext` entry has `context` instead of
    `name`/`workflowName`, so it groups by its `context`. Entries with
    neither cannot be grouped; they are kept as they are. Ties keep the
    last one, and so do entries that have no time at all.
    """
    newest: dict[tuple[str, str], dict] = {}
    unnamed: list = []
    for check in rollup:
        if not isinstance(check, dict):
            unnamed.append(check)
            continue
        key = (
            check.get("workflowName") or "",
            check.get("name") or check.get("context") or "",
        )
        if key == ("", ""):
            unnamed.append(check)
            continue
        current = newest.get(key)
        if current is None:
            newest[key] = check
            continue
        check_time = _check_time(check)
        current_time = _check_time(current)
        if check_time is None:
            if current_time is None:
                newest[key] = check
        elif current_time is None or check_time >= current_time:
            newest[key] = check
    return [*newest.values(), *unnamed]


def _checks(pr: dict) -> str:
    rollup = _dedupe_checks(list(pr.get("statusCheckRollup") or []))
    pending = False
    for check in rollup:
        if not isinstance(check, dict):
            pending = True
            continue
        state = check.get("conclusion") or check.get("state")
        if state in FAILING_CHECKS:
            return "fail"
        if state in PENDING_CHECKS or not state:
            pending = True
    if pending:
        return "pending"
    return "pass" if rollup else "none"


def _latest_reviews(reviews: list, *, full: bool = False) -> list[dict]:
    """Keep the newest review for each reviewer."""
    newest: dict[str, dict] = {}
    for review in reviews or []:
        if not isinstance(review, dict):
            continue
        login = _login(review.get("author"))
        when = _when(review.get("submittedAt"))
        if login not in newest or when >= _when(newest[login].get("submittedAt")):
            newest[login] = review
    return sorted(
        (
            {
                "login": _login(review.get("author")),
                "state": review.get("state") or "none",
                "submitted_at": _when(review.get("submittedAt")),
                "body": _preview(review.get("body"), full=full),
            }
            for review in newest.values()
        ),
        key=lambda row: row["submitted_at"],
    )


def _review_commit(review: dict) -> str:
    commit = review.get("commit_id")
    if commit:
        return str(commit)
    nested = review.get("commit")
    if isinstance(nested, dict):
        return str(nested.get("oid") or "")
    return ""


FORMAL_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED")


def _approval(pr: dict) -> tuple[bool, str]:
    """Return `(approval_at_head, text value)` for the last formal review.

    A formal review is `APPROVED` or `CHANGES_REQUESTED`. Other states do not
    count. A later change request clears an earlier approval.
    """
    formal = [
        review
        for review in pr.get("reviews") or []
        if isinstance(review, dict)
        and review.get("state") in FORMAL_REVIEW_STATES
    ]
    if not formal:
        return False, "none"
    newest = max(formal, key=lambda review: _when(review.get("submittedAt")))
    if newest.get("state") != "APPROVED":
        return False, "changes-requested"
    at_head = bool(pr.get("headRefOid")) and _review_commit(newest) == pr.get("headRefOid")
    return at_head, "head" if at_head else "stale"


def _informal_approvals(pr: dict) -> list[dict]:
    found = []
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        body = comment.get("body") or ""
        for line in str(body).splitlines():
            match = INFORMAL_RE.match(line)
            if match:
                found.append(
                    {
                        "login": _login(comment.get("author")),
                        "created_at": _when(comment.get("createdAt")),
                        "line": line.strip(),
                    }
                )
    return found


def _latest_comment(pr: dict, *, full: bool) -> dict | None:
    comments = [c for c in pr.get("comments") or [] if isinstance(c, dict)]
    if not comments:
        return None
    latest = max(comments, key=lambda comment: _when(comment.get("createdAt")))
    return {
        "login": _login(latest.get("author")),
        "created_at": _when(latest.get("createdAt")),
        "body": _preview(latest.get("body"), full=full),
    }


def _review_requests(pr: dict) -> list[str]:
    names = []
    for request in pr.get("reviewRequests") or []:
        if not isinstance(request, dict):
            continue
        name = request.get("login") or request.get("name") or request.get("slug")
        if name and str(name) not in names:
            names.append(str(name))
    return names


def _linked_issue(pr: dict) -> int | None:
    match = LINKED_ISSUE_RE.search(str(pr.get("body") or ""))
    if not match:
        return None
    return int(match.group(1))


def analyze(pr: dict, repo: str, claim_map: dict | None = None, *, full: bool = False) -> dict:
    """Turn one `gh pr list` entry into the row this command prints."""
    claim_map = claim_map or {}
    number = pr.get("number")
    target = f"{repo}#{number}"
    linked_issue = _linked_issue(pr)
    own_claim = (claim_map.get(target) or {}).get("session")
    claim = None
    if linked_issue is not None:
        claim = (claim_map.get(f"{repo}#{linked_issue}") or {}).get("session")
    claim = claim or own_claim or "-"
    approval_at_head, approval = _approval(pr)
    return {
        "number": number,
        "title": pr.get("title") or "",
        "head_ref_name": pr.get("headRefName") or "",
        "head_ref_oid": pr.get("headRefOid") or "",
        "merge": _merge(pr),
        "checks": _checks(pr),
        "review": pr.get("reviewDecision") or "none",
        "approval": approval,
        "approval_at_head": approval_at_head,
        "latest_reviews": _latest_reviews(pr.get("reviews"), full=full),
        "informal_approvals": _informal_approvals(pr),
        "comments_count": len(pr.get("comments") or []),
        "latest_comment": _latest_comment(pr, full=full),
        "description": _preview(pr.get("body"), full=full),
        "claim": claim,
        "linked_issue": linked_issue,
        "review_requests": _review_requests(pr),
    }


def _review_text(review: dict) -> str:
    text = f"{review['login']} {review['state']} {review['submitted_at']}".rstrip()
    if review.get("body"):
        text += f": {review['body']}"
    return text


def _render_details(row: dict) -> list[str]:
    """The indented lines under one PR line; only lines that have data."""
    lines = []
    if row["description"]:
        lines.append(f"   desc: {row['description']}")
    if row["latest_reviews"]:
        lines.append("   reviews: " + "; ".join(_review_text(r) for r in row["latest_reviews"]))
    comment = row["latest_comment"]
    if comment:
        lines.append(
            f"   comment: {comment['login']} {comment['created_at']}: {comment['body']}"
        )
    if row["review_requests"]:
        lines.append("   reviewers: " + ", ".join(row["review_requests"]))
    return lines


def render_text(rows: list[dict], warnings: list[str]) -> str:
    lines = ["PR  merge  checks  review  approval  claim  comments  title"]
    for row in rows:
        lines.append(
            f"#{row['number']}  merge={row['merge']}  checks={row['checks']}  "
            f"review={row['review']}  approval={row['approval']}  "
            f"claim={row['claim']}  comments={row['comments_count']}  {row['title']}"
        )
        lines.extend(_render_details(row))
    for warning in warnings:
        lines.append(f"warning: {warning}")
    for row in rows:
        for approval in row["informal_approvals"]:
            lines.append(
                f"#{row['number']}: {approval['login']} {approval['created_at']}: "
                f"{approval['line']}"
            )
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="lupin pr-status",
        description="Show every open pull request in one view.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="OWNER/REPO (default: let gh resolve the current repository)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument(
        "--full",
        action="store_true",
        help="do not truncate the description, comment, or review text",
    )
    # The claim column reads Redis, so this parses the same fleet
    # connection flags as `roadmap`. None of them is required: the
    # defaults come from the environment and the fleet config, and `gh`
    # resolves `--repo`.
    parser.add_argument("--redis-host", default=os.environ.get("LUPIN_REDIS_HOST"))
    parser.add_argument(
        "--redis-port",
        type=int,
        default=int(os.environ["LUPIN_REDIS_PORT"]) if os.environ.get("LUPIN_REDIS_PORT") else None,
    )
    parser.add_argument("--redis-username", default=os.environ.get("LUPIN_REDIS_USERNAME"))
    parser.add_argument("--redis-password", default=os.environ.get("LUPIN_REDIS_PASSWORD"))
    parser.add_argument(
        "--config-path",
        default=os.environ.get("LUPIN_FLEET_CONFIG"),
        help="fleet config file (default: $LUPIN_FLEET_CONFIG or ~/.config/lupin/fleet.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    repo = args.repo
    if repo is None:
        repo, error = _resolve_repo()
        if error:
            print(f"lupin: {_redact(error)}", file=sys.stderr)
            return 1

    prs, error = fetch_open_prs(repo)
    if error:
        print(f"lupin: {_redact(error)}", file=sys.stderr)
        return 1
    if not isinstance(prs, list):
        print("lupin: gh returned an unexpected pull request list", file=sys.stderr)
        return 1

    connection = {
        "redis_host": args.redis_host,
        "redis_port": args.redis_port,
        "redis_username": args.redis_username,
        "redis_password": args.redis_password,
        "config_path": args.config_path,
    }
    claim_map, claim_warning = _read_claims(repo, connection)
    warnings = [claim_warning] if claim_warning else []
    rows = [
        analyze(pr, repo, claim_map, full=args.full)
        for pr in prs
        if isinstance(pr, dict)
    ]
    # A failed claim read makes every claim "unknown", not "unclaimed".
    if claim_warning:
        for row in rows:
            row["claim"] = "unknown"

    if args.json:
        print(json.dumps({"repo": repo, "prs": rows, "warnings": warnings}))
        return 0

    print(render_text(rows, warnings))
    return 0

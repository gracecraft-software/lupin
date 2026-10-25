"""Decide how to run a review call, and print the decision as JSON.

This is the wiring for issue #185: `route.route()` (issue #184) already picks
a {model, effort} pair; this module picks which lock that pair needs, so the
shell dispatch code in `hosts/jesus/configuration.nix` can reuse the
existing claude-dispatch.lock/omp.lock machinery instead of a hardcoded
model. It decides; it never touches a lock or a process itself -- same
division of work as route.py.

Three plans, matching the three cases in #185:

- "same_unit": a claude model, run sequentially inside the caller's own
  already-counted loop-claude-* unit. No lock -- this costs nothing extra
  against claude_max_concurrent because it starts no new systemd unit.
- "claude_lock": a claude model, but a separate process is unavoidable. Take
  claude-dispatch.lock and recheck capacity before launching.
- "omp_lock": the routed model runs through omp's shared LM Studio server
  (a "bmo:" or "local:" model -- see the omp case's own comment in
  configuration.nix for why both share one lock). Take the blocking
  omp.lock, same as the omp dispatch path.

`--prefetch` (issue #1) is a second, unrelated mode in the same command: it
fetches issue/PR text up front so a delegation-loop orchestrator, and the
subagent it dispatches, don't each spend a turn on their own `gh` calls. It
does not route anything -- see `prefetch()` below. Its default output is
markdown via `brief_markdown()` (issue #181); `--json` keeps the keyed object
for scripts, trimmed to the fields a worker actually reads.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from . import classify
from . import route

# Same redaction as roadmap.py's _run_json: a `gh` error can echo a token
# back (e.g. in a URL) if auth is misconfigured.
_TOKEN_RE = re.compile(r"(?:gh[oprsu]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")


def dispatch_plan(model: str, *, mode: str = "same-unit") -> str:
    """Return "same_unit", "claude_lock", or "omp_lock" for a routed model.

    `mode` only matters for a claude model: "same-unit" (the default) means
    the caller is itself inside the primary loop's loop-claude-* unit and
    can run the review in-process; "separate" means it cannot, so the result
    picks the lock path instead.
    """
    if model.startswith("bmo:") or model.startswith("local:"):
        return "omp_lock"
    if mode == "separate":
        return "claude_lock"
    return "same_unit"


def decide(
    category: str,
    size: str,
    *,
    bmo_available: bool = True,
    primary_effort: str | None = None,
    mode: str = "same-unit",
    excluded_providers: list[str] | tuple[str, ...] = (),
) -> dict:
    """Route a (category, size) pair and attach its dispatch plan."""
    # `route.route()` reads the configured model-tier file.
    choice = route.route(
        category,
        size,
        bmo_available=bmo_available,
        primary_effort=primary_effort,
        excluded_providers=excluded_providers,
    )
    return {**choice, "plan": dispatch_plan(choice["model"], mode=mode)}

def _run_gh_json(args: list[str], *, repo: str | None = None, timeout: int = 60):
    """Run `gh <args> --json ...`, return (data, error).

    Same shape as roadmap.py's `_run_json`: exactly one of the pair is not
    None. Kept separate because this module takes no repo checkout path --
    `gh` runs from the current directory, or against `--repo` if given.
    """
    argv = ["gh", *args]
    if repo:
        argv += ["--repo", repo]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "gh is not installed"
    except OSError as error:
        return None, f"gh could not start: {error}"
    except subprocess.TimeoutExpired:
        return None, f"gh timed out after {timeout} seconds"
    if result.returncode:
        error = (result.stderr or result.stdout or "gh failed").strip()
        error = _TOKEN_RE.sub("[redacted]", error)
        return None, error
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as error:
        return None, f"invalid JSON from gh: {error}"


def _trim_comment(comment: dict) -> dict:
    """Keep only the fields a worker reads from a comment.

    A raw `gh` comment carries eleven fields (URL, reaction counts, IDs);
    the brief needs who wrote it, when, and what it says.
    """
    author = comment.get("author") or {}
    return {
        "author": {"login": author.get("login") or ""},
        "createdAt": comment.get("createdAt") or "",
        "body": comment.get("body") or "",
    }


def _trim_review(review: dict) -> dict:
    """Keep only the fields a worker reads from a review."""
    author = review.get("author") or {}
    return {
        "author": {"login": author.get("login") or ""},
        "state": review.get("state") or "",
        "submittedAt": review.get("submittedAt") or "",
        "body": review.get("body") or "",
    }


def _fetch_item(item: str, *, repo: str | None = None) -> dict:
    """Fetch one issue or PR's text and (for a PR) its diff stat.

    An issue and a PR share one number counter in a GitHub repo, so there is
    no way to tell which an item is without asking. Ask `gh pr view` first:
    current `gh` resolves a PR number in `gh issue view` too, so the issue
    lookup cannot distinguish the two. Only when the PR lookup fails is the
    item an issue or nothing. Diff stat only -- the changed-file list plus
    additions/deletions -- never the full diff text (out of scope, see issue
    #1: same cost whether lupin or the dispatched agent fetches it).
    """
    pr_data, pr_error = _run_gh_json(
        ["pr", "view", item, "--json", "body,comments,reviews,files"], repo=repo
    )
    if pr_data is not None:
        files = pr_data.pop("files", [])
        return {
            "kind": "pr",
            "body": pr_data.get("body") or "",
            "comments": [
                _trim_comment(comment) for comment in pr_data.get("comments") or []
            ],
            "reviews": [
                _trim_review(review) for review in pr_data.get("reviews") or []
            ],
            "diff_stat": [
                {
                    "path": f["path"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                }
                for f in files
            ],
        }

    issue_data, issue_error = _run_gh_json(
        ["issue", "view", item, "--json", "body,comments"], repo=repo
    )
    if issue_data is None:
        return {"error": f"not a fetchable issue or PR {item!r}: {pr_error}; {issue_error}"}

    return {
        "kind": "issue",
        "body": issue_data.get("body") or "",
        "comments": [
            _trim_comment(comment) for comment in issue_data.get("comments") or []
        ],
    }


def prefetch(items: list[str], *, repo: str | None = None) -> dict:
    """Fetch issue/PR text for every item, keyed by its number (as given).

    A bad item (neither an issue nor a PR) does not fail the whole batch --
    its entry gets {"error": ...} instead, so one typo in an orchestrator's
    list doesn't lose the rest of the prefetch.
    """
    return {item: _fetch_item(item, repo=repo) for item in items}


def brief_markdown(item: str, data: dict) -> str:
    """Render one `_fetch_item()` result as the worker's markdown brief.

    The worker reads this as text, so it carries no JSON escaping: a body's
    newlines stay newlines. Every comment and review keeps its author login
    and its timestamp -- a comment can correct the body, and the worker needs
    to know who corrected it and when.
    """
    kind = data.get("kind") or "issue"
    heading = "PR" if kind == "pr" else "Issue"
    if "error" in data:
        # A batch prefetch keeps a bad item in place instead of losing the
        # rest; show the reason rather than an empty brief.
        return f"# {heading} {item}\n\n{data['error']}\n"
    parts = [f"# {heading} {item}", "", data.get("body") or ""]

    if kind == "pr":
        reviews = data.get("reviews") or []
        parts += ["", f"## Reviews ({len(reviews)})"]
        for review in reviews:
            login = (review.get("author") or {}).get("login") or ""
            parts += [
                "",
                f"### @{login} - {review.get('state') or ''} - {review.get('submittedAt') or ''}",
                "",
                review.get("body") or "",
            ]
        parts += ["", "## Changed files"]
        for changed in data.get("diff_stat") or []:
            parts.append(
                f"- {changed['path']} "
                f"(+{changed.get('additions', 0)} -{changed.get('deletions', 0)})"
            )

    comments = data.get("comments") or []
    parts += ["", f"## Comments ({len(comments)})"]
    for comment in comments:
        login = (comment.get("author") or {}).get("login") or ""
        parts += [
            "",
            f"### @{login} - {comment.get('createdAt') or ''}",
            "",
            comment.get("body") or "",
        ]
    return "\n".join(parts).rstrip("\n") + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--category", help="task category, with --size")
    source.add_argument(
        "--issue-json", help="path to a `gh issue view --json ...` file to classify"
    )
    parser.add_argument("--size", help="task size label, with --category")
    parser.add_argument(
        "--diff-stat", help="path to a `git diff --stat` file (refines --issue-json size)"
    )
    parser.add_argument(
        "--mode",
        choices=("same-unit", "separate"),
        default="same-unit",
        help="same-unit (default): caller is already inside a loop-claude-* unit",
    )
    parser.add_argument(
        "--bmo-unavailable",
        action="store_true",
        help="bmo's lock already timed out -- skip a bmo-dependent tier0 pick",
    )
    parser.add_argument("--primary-effort", default=None)
    parser.add_argument(
        "--exclude-provider",
        action="append",
        default=[],
        metavar="PROVIDER",
        help="provider to avoid (repeat for more than one)",
    )
    parser.add_argument(
        "--prefetch",
        default=None,
        help="comma-separated issue/PR numbers to fetch instead of routing",
    )
    parser.add_argument(
        "--repo", default=None, help="OWNER/REPO for --prefetch (default: gh's own resolution)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --prefetch: print the trimmed keyed object instead of markdown",
    )
    args = parser.parse_args(argv)
    if args.prefetch:
        return args
    if not args.category and not args.issue_json:
        parser.error("one of --category or --issue-json is required (unless using --prefetch)")
    if args.category and not args.size:
        parser.error("--category needs --size")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.prefetch:
        items = [item.strip() for item in args.prefetch.split(",") if item.strip()]
        fetched = prefetch(items, repo=args.repo)
        if args.json:
            print(json.dumps(fetched))
            return 0
        sections = [
            brief_markdown(f"{args.repo}#{item}" if args.repo else item, data)
            for item, data in fetched.items()
        ]
        sys.stdout.write("\n".join(sections))
        return 0
    if args.issue_json:
        with open(args.issue_json, encoding="utf-8") as handle:
            issue = json.load(handle)
        diff_stat = None
        if args.diff_stat:
            with open(args.diff_stat, encoding="utf-8") as handle:
                diff_stat = handle.read()
        category, size = classify.classify(issue, diff_stat)
    else:
        category, size = args.category, args.size

    try:
        result = decide(
            category,
            size,
            bmo_available=not args.bmo_unavailable,
            primary_effort=args.primary_effort,
            mode=args.mode,
            excluded_providers=args.exclude_provider,
        )
    except route.RouteInputError as err:
        print(f"lupin review-route: {err}", file=sys.stderr)
        return 2
    print(json.dumps({**result, "category": category, "size": size}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

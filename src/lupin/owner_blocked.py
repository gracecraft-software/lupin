"""Build the daily report of Lupin work that needs the owner's action."""

from __future__ import annotations

from . import pr_status, roadmap

ISSUE_LIMIT = 1000
PR_LIMIT = 100
RECENT_COMMENTS = 10


def _text(value) -> str:
    return pr_status._redact(str(value or "")).strip()


def _single_line(value) -> str:
    return " ".join(_text(value).split())


def _bullets(value) -> list[str]:
    lines = [line for line in _text(value).splitlines() if line.strip()]
    return [f"- {line}" for line in lines] or ["- (empty)"]


def _labels(issue: dict) -> list[str]:
    labels = issue.get("labels") or []
    return sorted(
        _single_line(label.get("name") if isinstance(label, dict) else label)
        for label in labels
        if (label.get("name") if isinstance(label, dict) else label)
    )


def _comments(comments: list[dict] | None, *, unavailable: bool = False) -> list[str]:
    recent = sorted(
        (comment for comment in (comments or []) if isinstance(comment, dict)),
        key=lambda comment: str(comment.get("createdAt") or ""),
    )[-RECENT_COMMENTS:]
    if not recent:
        if unavailable:
            return ["- Not collected: recent comments are unavailable."]
        return ["- None."]
    lines = []
    for comment in recent:
        author = comment.get("author") or "unknown"
        if isinstance(author, dict):
            author = author.get("login") or "unknown"
        lines.extend(
            [
                f"- @{_single_line(author)} — {_single_line(comment.get('createdAt'))}",
                *_bullets(comment.get("body")),
            ]
        )
    if unavailable:
        lines.append("- Comment list may be incomplete.")
    return lines


def _issue_rows(issues: list[dict] | None) -> list[dict]:
    return sorted(
        (
            issue for issue in (issues or [])
            if isinstance(issue, dict)
            and any(label.casefold() == roadmap.OWNER_BLOCKED_LABEL for label in _labels(issue))
        ),
        key=lambda issue: (str(issue.get("updatedAt") or issue.get("createdAt") or ""), int(issue.get("number") or 0)),
    )


def _ready_pr_rows(prs: list[dict] | None, repo: str, gaps: list[str]) -> list[tuple[dict, dict]]:
    rows = []
    for pr in prs or []:
        if not isinstance(pr, dict):
            gaps.append("GitHub returned a malformed pull request row.")
            continue
        if not isinstance(pr.get("isDraft"), bool):
            gaps.append(f"PR #{pr.get('number', '?')}: draft status is unavailable.")
            continue
        try:
            row = pr_status.analyze(pr, repo, full=True)
        except (KeyError, TypeError, ValueError) as exc:
            gaps.append(f"PR #{pr.get('number', '?')}: status could not be read ({exc}).")
            continue
        number = pr.get("number", "?")
        if row["merge"] == "unknown":
            gaps.append(f"PR #{number}: merge status is unavailable.")
        if row["checks"] == "none":
            gaps.append(f"PR #{number}: check status is unavailable.")
        if (
            not pr["isDraft"]
            and row["merge"] == "clean"
            and row["checks"] == "pass"
            and row["review"] == "APPROVED"
            and row["approval_at_head"]
        ):
            rows.append((pr, row))
    return sorted(
        rows,
        key=lambda item: (str(item[0].get("updatedAt") or ""), int(item[0].get("number") or 0)),
    )


def _issue_url(full_name: str, issue: dict) -> str:
    url = str(issue.get("url") or "")
    if not url.startswith("https://github.com/"):
        url = f"https://github.com/{full_name}/issues/{issue['number']}"
    return _single_line(url)


def build_markdown(
    full_name: str,
    start: str,
    end: str,
    *,
    issues: list[dict] | None,
    comments: dict,
    issue_warnings: list[str],
    prs: list[dict] | None,
    pr_error: str | None,
) -> str:
    """Build a source report. A failed read is never shown as an empty result."""
    gaps = list(issue_warnings)
    issue_list_error = next(
        (
            warning for warning in issue_warnings
            if warning.startswith(
                (
                    "GitHub issue data is unavailable",
                    "GitHub repository data is unavailable",
                    "GitHub returned invalid issue data",
                    "GitHub returned invalid repository data",
                    "Could not identify repository",
                )
            )
        ),
        None,
    )
    if issues is None and issue_list_error is None:
        issue_list_error = "GitHub issue data is unavailable: no response."
        gaps.append(issue_list_error)
    comments_unavailable = any(
        "Recent GitHub comments are unavailable" in warning for warning in issue_warnings
    )
    issue_list_cut = issues is not None and len(issues) >= ISSUE_LIMIT
    if issue_list_cut:
        gaps.append(f"GitHub returned {ISSUE_LIMIT} open issues; the list may be cut.")
    pr_list_cut = prs is not None and len(prs) >= PR_LIMIT
    if pr_list_cut:
        gaps.append(f"GitHub returned {PR_LIMIT} open pull requests; the list may be cut.")
    if prs is None or pr_error:
        gaps.append(f"Pull request data is unavailable: {pr_error or 'no response'}.")
    pr_classification_start = len(gaps)
    ready_prs = _ready_pr_rows(prs, full_name, gaps) if prs is not None and not pr_error else []
    issue_count_unknown = issue_list_error is not None or issue_list_cut
    pr_count_unknown = (
        prs is None or bool(pr_error) or pr_list_cut
        or len(gaps) > pr_classification_start
    )
    owner_issues = _issue_rows(issues) if issues is not None and not issue_list_error else []

    lines = [
        f"# Daily owner-blocked report: {_single_line(full_name)}",
        "",
        "## Window",
        f"- From: {_single_line(start)}",
        f"- To: {_single_line(end)}",
        "- Period: owner-blocked",
        "",
        "## Scope",
        f"- Open issues with the {roadmap.OWNER_BLOCKED_LABEL} label.",
        "- Open, non-draft PRs that are mergeable, approved at the current head, and have passing checks; these wait for the owner to merge.",
        "",
        "## Snapshot",
        f"- Owner-blocked issues: {'unknown' if issue_count_unknown else len(owner_issues)}.",
        f"- PRs waiting for owner merge: {'unknown' if pr_count_unknown else len(ready_prs)}.",
        "",
        "## Owner-blocked issues",
    ]
    if issue_list_error:
        lines.append(f"- Not collected: {_single_line(issue_list_error)}.")
    elif not owner_issues:
        lines.append("- None.")
    else:
        for issue in owner_issues:
            number = issue["number"]
            lines.extend(
                [
                    f"- Issue #{number}: {_single_line(issue.get('title'))}",
                    f"- URL: {_issue_url(full_name, issue)}",
                    f"- Labels: {', '.join(_labels(issue))}",
                    f"- Last updated: {_single_line(issue.get('updatedAt') or 'unknown')}",
                    "- Description:",
                    *_bullets(issue.get("body")),
                    "- Recent comments:",
                    *_comments(
                        comments.get(int(number), []),
                        unavailable=comments_unavailable,
                    ),
                    "",
                ]
            )

    lines.extend(["## Pull requests waiting for owner merge"])
    if prs is None or pr_error:
        lines.append(f"- Not collected: {_single_line(pr_error or 'no response')}.")
    elif not ready_prs:
        lines.append("- None.")
    else:
        for pr, row in ready_prs:
            number = pr["number"]
            lines.extend(
                [
                    f"- PR #{number}: {_single_line(pr.get('title'))}",
                    f"- URL: https://github.com/{_single_line(full_name)}/pull/{number}",
                    f"- Last updated: {_single_line(pr.get('updatedAt') or 'unknown')}",
                    f"- Merge: {row['merge']}; checks: {row['checks']}; approval: {row['approval']}.",
                    "- Description:",
                    *_bullets(pr.get("body")),
                    "- Recent reviews:",
                ]
            )
            reviews = row["latest_reviews"][-RECENT_COMMENTS:]
            if reviews:
                for review in reviews:
                    lines.extend(
                        [
                            f"- @{_single_line(review.get('login'))} — {_single_line(review.get('state'))} {_single_line(review.get('submitted_at'))}",
                            *_bullets(review.get("body")),
                        ]
                    )
            else:
                lines.append("- None.")
            lines.extend(["- Recent comments:", *_comments(pr.get("comments")), ""])

    lines.append("## Data gaps")
    if gaps:
        lines.extend(f"- {_single_line(gap)}" for gap in dict.fromkeys(gaps))
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def collect_markdown(
    repo_path: str,
    full_name: str,
    start: str,
    end: str,
    *,
    connection: dict | None = None,
) -> str:
    """Read owner-blocked issues and ready-to-merge PRs, then build the report."""
    issues, comments, warnings = roadmap.load_github(repo_path, connection=connection)
    prs, pr_error = pr_status.fetch_open_prs(full_name)
    return build_markdown(
        full_name,
        start,
        end,
        issues=issues,
        comments=comments,
        issue_warnings=warnings,
        prs=prs,
        pr_error=pr_error,
    )

from __future__ import annotations

from lupin import debrief, digest, owner_blocked


REPO = "acme/lupin"
START = "2026-10-23T07:00:00Z (2026-10-23 00:00 PDT)"
END = "2026-10-24T07:00:00Z (2026-10-24 00:00 PDT)"
_MISSING = object()


def _pr(number=21, **changes):
    value = {
        "number": number,
        "title": "Ready for owner merge",
        "body": "The change is complete.",
        "updatedAt": "2026-10-23T19:00:00Z",
        "isDraft": False,
        "headRefOid": "head-sha",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "reviews": [
            {
                "state": "APPROVED",
                "submittedAt": "2026-10-23T18:00:00Z",
                "commit_id": "head-sha",
                "author": {"login": "reviewer"},
                "body": "Approved at the current head.",
            }
        ],
        "comments": [
            {
                "createdAt": "2026-10-23T19:00:00Z",
                "author": {"login": "merge-owner"},
                "body": "Please merge after CI.",
            }
        ],
        "statusCheckRollup": [{"state": "SUCCESS"}],
        "reviewRequests": [],
    }
    value.update(changes)
    return value


def _report(
    *, issues=_MISSING, comments=_MISSING, issue_warnings=_MISSING,
    prs=_MISSING, pr_error=None,
):
    return owner_blocked.build_markdown(
        REPO,
        START,
        END,
        issues=[] if issues is _MISSING else issues,
        comments={} if comments is _MISSING else comments,
        issue_warnings=[] if issue_warnings is _MISSING else issue_warnings,
        prs=[] if prs is _MISSING else prs,
        pr_error=pr_error,
    )


def test_report_includes_labeled_issues_and_only_ready_to_merge_prs():
    issue = {
        "number": 14,
        "title": "Choose the data retention period",
        "body": "Choose 30 or 90 days.",
        "labels": [
            {"name": "completely-blocked-on-human"},
            {"name": "P1"},
        ],
        "updatedAt": "2026-10-22T12:00:00Z",
        "url": "https://github.com/acme/lupin/issues/14",
    }
    prs = [
        _pr(),
        _pr(22, isDraft=True),
        _pr(23, statusCheckRollup=[{"state": "IN_PROGRESS"}]),
        _pr(24, reviews=[{**_pr()["reviews"][0], "commit_id": "old-sha"}]),
        _pr(25, mergeable="CONFLICTING"),
        _pr(26, reviewDecision="REVIEW_REQUIRED"),
    ]

    report = _report(
        issues=[issue, {**issue, "number": 15, "labels": [{"name": "P1"}]}],
        comments={14: [{"author": "owner", "createdAt": "2026-10-23T10:00:00Z", "body": "Choose 30 days."}]},
        prs=prs,
    )

    assert "Owner-blocked issues: 1." in report
    assert "PRs waiting for owner merge: 1." in report
    assert "Issue #14: Choose the data retention period" in report
    assert "https://github.com/acme/lupin/issues/14" in report
    assert "@owner" in report and "Choose 30 days." in report
    assert "PR #21: Ready for owner merge" in report
    assert "@reviewer" in report and "@merge-owner" in report
    for number in (22, 23, 24, 25, 26):
        assert f"PR #{number}:" not in report
    assert "Issue #15:" not in report
    assert not digest.is_quiet(report)


def test_empty_complete_report_is_quiet():
    report = _report()

    assert "Owner-blocked issues: 0." in report
    assert "PRs waiting for owner merge: 0." in report
    assert digest.is_quiet(report)


def test_missing_issue_response_is_listed_as_a_data_gap():
    report = _report(issues=None)

    assert "Owner-blocked issues: unknown." in report
    assert "## Data gaps\n- GitHub issue data is unavailable: no response." in report


def test_source_failures_are_not_reported_as_empty_and_tokens_are_redacted():
    report = _report(
        issues=None,
        issue_warnings=["GitHub issue data is unavailable: down"],
        prs=None,
        pr_error="fatal: token ghp_secret rejected",
    )

    assert "Owner-blocked issues: unknown." in report
    assert "PRs waiting for owner merge: unknown." in report
    assert "Not collected:" in report
    assert "ghp_secret" not in report
    assert "[redacted]" in report
    assert not digest.is_quiet(report)


def test_unavailable_issue_comments_are_not_shown_as_none():
    issue = {
        "number": 14,
        "title": "Choose the data retention period",
        "body": "Choose 30 or 90 days.",
        "labels": [{"name": owner_blocked.roadmap.OWNER_BLOCKED_LABEL}],
        "updatedAt": "2026-10-22T12:00:00Z",
    }
    report = _report(
        issues=[issue],
        issue_warnings=["Recent GitHub comments are unavailable: down"],
    )

    assert "recent comments are unavailable" in report
    assert "## Data gaps\n- Recent GitHub comments are unavailable: down" in report


def test_report_counts_are_unknown_when_api_lists_reach_their_limits(monkeypatch):
    monkeypatch.setattr(owner_blocked, "ISSUE_LIMIT", 1)
    monkeypatch.setattr(owner_blocked, "PR_LIMIT", 1)
    issue = {
        "number": 14,
        "title": "Choose the retention period",
        "labels": [{"name": owner_blocked.roadmap.OWNER_BLOCKED_LABEL}],
    }

    report = _report(issues=[issue], prs=[_pr()])

    assert "Owner-blocked issues: unknown." in report
    assert "PRs waiting for owner merge: unknown." in report
    assert "GitHub returned 1 open issues; the list may be cut." in report
    assert "GitHub returned 1 open pull requests; the list may be cut." in report


def test_unknown_pr_merge_or_check_status_makes_the_count_unknown():
    report = _report(
        prs=[
            _pr(26, mergeable="UNKNOWN"),
            _pr(27, statusCheckRollup=[]),
        ],
    )

    assert "PRs waiting for owner merge: unknown." in report
    assert "PR #26: merge status is unavailable." in report
    assert "PR #27: check status is unavailable." in report
    assert "### PR #26:" not in report and "### PR #27:" not in report


def test_report_renders_with_supported_debrief_markdown():
    issue = {
        "number": 14,
        "title": "Choose the data retention period",
        "body": "Choose 30 or 90 days.",
        "labels": [{"name": owner_blocked.roadmap.OWNER_BLOCKED_LABEL}],
    }
    rendered = debrief.render_html(_report(issues=[issue], prs=[_pr()]), "lupin")

    assert "<h1>Daily owner-blocked report: acme/lupin</h1>" in rendered
    assert "<h2>Owner-blocked issues</h2>" in rendered
    assert "<li>Issue #14: Choose the data retention period</li>" in rendered
    assert "<li>PR #21: Ready for owner merge</li>" in rendered
    assert "<li>Choose 30 or 90 days.</li>" in rendered
    assert "###" not in rendered
    assert "> " not in rendered

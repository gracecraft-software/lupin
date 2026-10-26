import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from lupin import ledger, roadmap


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cache_file = str(Path(directory.name) / "cache.json")
        cache_patch = mock.patch.object(roadmap, "CACHE_FILE", cache_file)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        roadmap._GITHUB_CACHE.clear()

    def test_normalizes_priority_and_size_labels(self):
        issues = [
            {
                "number": 8,
                "title": "Label aliases",
                "body": "",
                "labels": [{"name": "priority/p1"}, {"name": "size/M"}],
                "url": "https://github.com/gracecraft/nix/issues/8",
            }
        ]

        node = roadmap.build_model(issues, {}, [], repo="nix")["nodes"][0]

        self.assertEqual(node["priority"], "P1")
        self.assertEqual(node["size"], "size-m")

    def test_parent_and_dependency_links_remain_distinct(self):
        issues = [
            {"number": 1, "title": "Parent", "body": "", "labels": ["epic"]},
            {
                "number": 2,
                "title": "Child",
                "body": "Part of #1. Depends on #3.",
                "labels": [],
            },
            {"number": 3, "title": "Prerequisite", "body": "", "labels": []},
        ]

        edges = roadmap.build_model(issues, {}, [], repo="nix")["edges"]

        self.assertIn({"from": 1, "to": 2, "kind": "parent"}, edges)
        self.assertIn({"from": 3, "to": 2, "kind": "depends"}, edges)

    def test_negated_dependency_phrase_creates_no_edge(self):
        issues = [
            {"number": 77, "title": "A", "body": "", "labels": []},
            {
                "number": 79,
                "title": "B",
                "body": "Step 8 is not blocked by #77. Step 9 is blocked by #77.",
                "labels": [],
            },
        ]

        edges = roadmap.build_model(issues, {}, [], repo="nix")["edges"]

        # The negated mention must not create an edge; the real one still does.
        matches = [e for e in edges if e == {"from": 77, "to": 79, "kind": "depends"}]
        self.assertEqual(len(matches), 1)


    def test_quoted_dependency_phrase_creates_no_edge(self):
        issues = [
            {"number": 77, "title": "Original", "body": "", "labels": []},
            {"number": 78, "title": "Blocked issue", "body": "", "labels": []},
            {"number": 79, "title": "Parent issue", "body": "", "labels": []},
            {
                "number": 158,
                "title": "Quoted report",
                "body": 'The old comment said, "priority P1 since it blocks #78 and part of #79".',
                "labels": [],
            },
        ]

        edges = roadmap.build_model(issues, {}, [], repo="nix")["edges"]

        self.assertFalse(
            any(edge["from"] == 158 or edge["to"] == 158 for edge in edges)
        )

    def test_dispatch_ledger_event_marks_issue_active(self):
        issue = {"number": 12, "title": "Running", "body": "", "labels": []}

        node = roadmap.build_model(
            [issue], {}, [{"issue": 12, "event": "dispatch"}], repo="nix"
        )["nodes"][0]

        self.assertTrue(node["dispatched"])
        self.assertEqual(node["loop"], "dispatch")

    def test_running_ledger_status_marks_issue_in_flight(self):
        issues = [
            {"number": 12, "title": "Running", "body": "", "labels": []},
            {"number": 13, "title": "Triaged", "body": "", "labels": []},
        ]

        model = roadmap.build_model(
            issues,
            {},
            [
                {"issue": 12, "status": "running"},
                {"issue": 13, "status": "triaged"},
            ],
            repo="nix",
        )

        stages = {stage["name"]: stage["numbers"] for stage in model["stages"]}
        self.assertIn(12, stages["Marked in flight"])
        self.assertNotIn(13, stages["Marked in flight"])

    def test_closed_github_fetch_and_cache_are_separate_from_open(self):
        roadmap._GITHUB_CACHE.clear()
        open_issue = {"number": 1, "title": "Active", "body": "", "labels": []}
        closed_issue = {
            "number": 2,
            "title": "Completed",
            "body": "",
            "labels": [],
            "url": "https://github.com/acme/repo/issues/2",
        }
        with mock.patch.object(
            roadmap,
            "load_github",
            side_effect=[([open_issue], {}, []), ([closed_issue], {}, [])],
        ) as load:
            active = roadmap.cached_github("repo", "/repo")
            completed = roadmap.cached_github("repo", "/repo", "closed")
            self.assertIs(roadmap.cached_github("repo", "/repo"), active)
            self.assertIs(roadmap.cached_github("repo", "/repo", "closed"), completed)

        self.assertEqual([issue["title"] for issue in active[0]], ["Active"])
        self.assertEqual([issue["title"] for issue in completed[0]], ["Completed"])
        self.assertEqual(load.call_args_list[0].args, ("/repo", "open"))
        self.assertEqual(load.call_args_list[1].args, ("/repo", "closed"))

    def test_closed_fetch_uses_closed_state_for_cli_and_comments(self):
        issue = {"number": 9, "title": "Done", "body": "", "labels": []}
        response = [
            ({"owner": {"login": "acme"}, "name": "repo"}, None),
            ([issue], None),
        ]
        with (
            mock.patch.object(roadmap, "_run_json", side_effect=response) as run_json,
            mock.patch.object(roadmap, "_read_comments", return_value=({}, None)) as comments,
            mock.patch.object(
                roadmap.gh_cache, "cached_gh_json", side_effect=lambda *a, **kw: a[3]()
            ),
        ):
            roadmap.load_github("/repo", "closed")

        self.assertEqual(run_json.call_args_list[1].args[0][3:5], ["--state", "closed"])
        issue_args = run_json.call_args_list[1].args[0]
        self.assertEqual(issue_args[issue_args.index("--limit") + 1], "100")
        search = issue_args[issue_args.index("--search") + 1]
        self.assertTrue(search.startswith("closed:>="))
        comments.assert_called_once_with("/repo", "acme", "repo", "closed", 100, connection=None)

    def test_closed_graphql_query_filters_closed_issues(self):
        response = {
            "data": {"repository": {"issues": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}
            }}}
        }
        with (
            mock.patch.object(roadmap, "_run_json", return_value=(response, None)) as run_json,
            mock.patch.object(
                roadmap.gh_cache, "cached_gh_json", side_effect=lambda *a, **kw: a[3]()
            ),
        ):
            roadmap._read_comments("/repo", "acme", "repo", "closed")

        query = run_json.call_args.args[0][4]
        self.assertIn("states:CLOSED", query)
        self.assertNotIn("states:OPEN", query)

    def test_repo_identity_asks_gh_once_per_remote_url(self):
        roadmap._IDENTITY_CACHE.clear()
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    roadmap, "_git_remote_url",
                    return_value="https://github.com/acme/repo.git",
                ),
                mock.patch.object(
                    roadmap, "_gh_identity", return_value=("acme", "repo", None)
                ) as gh_identity,
                mock.patch.object(roadmap, "_persist_cache"),
            ):
                first = roadmap._repo_identity(directory)
                second = roadmap._repo_identity(directory)

            self.assertEqual(first, ("acme", "repo", None))
            self.assertEqual(second, ("acme", "repo", None))
            gh_identity.assert_called_once_with(directory)

    def test_repo_identity_uses_github_origin_when_gh_is_missing(self):
        roadmap._IDENTITY_CACHE.clear()
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    roadmap, "_git_remote_url",
                    return_value="git@github.com:acme/repo.git",
                ),
                mock.patch.object(
                    roadmap, "_gh_identity",
                    return_value=(
                        None, None,
                        "GitHub repository data is unavailable: gh is not installed",
                    ),
                ),
                mock.patch.object(roadmap, "_persist_cache"),
            ):
                result = roadmap._repo_identity(directory)

        self.assertEqual(result, ("acme", "repo", None))

    def test_github_repo_from_remote_rejects_non_github_urls(self):
        self.assertIsNone(roadmap._github_repo_from_remote("https://gitlab.com/acme/repo.git"))


    def test_repo_identity_asks_gh_again_when_the_remote_changes(self):
        roadmap._IDENTITY_CACHE.clear()
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(
                    roadmap, "_git_remote_url",
                    side_effect=[
                        "https://github.com/acme/repo.git",
                        "https://github.com/other/repo.git",
                    ],
                ),
                mock.patch.object(
                    roadmap, "_gh_identity",
                    side_effect=[("acme", "repo", None), ("other", "repo", None)],
                ) as gh_identity,
                mock.patch.object(roadmap, "_persist_cache"),
            ):
                first = roadmap._repo_identity(directory)
                second = roadmap._repo_identity(directory)

            self.assertEqual(first, ("acme", "repo", None))
            self.assertEqual(second, ("other", "repo", None))
            self.assertEqual(gh_identity.call_count, 2)

    def test_repo_identity_without_a_remote_is_not_cached(self):
        """A path that is not a real directory is a one-shot lookup: it is
        asked every time, never remembered."""
        roadmap._IDENTITY_CACHE.clear()
        with (
            mock.patch.object(roadmap, "_git_remote_url", return_value=None),
            mock.patch.object(
                roadmap, "_gh_identity", return_value=(None, None, "no remote")
            ) as gh_identity,
            mock.patch.object(roadmap, "_persist_cache"),
        ):
            first = roadmap._repo_identity("/repo")
            second = roadmap._repo_identity("/repo")

        self.assertEqual(first[2], "no remote")
        self.assertEqual(gh_identity.call_count, 2)
        self.assertEqual(roadmap._IDENTITY_CACHE, {})

    def test_repo_identity_of_a_checkout_without_a_remote_is_remembered(self):
        """`gh` denies the same denial on every render for a checkout with
        no `origin`, so the answer is kept for the retry window."""
        roadmap._IDENTITY_CACHE.clear()
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(roadmap, "_git_remote_url", return_value=None),
                mock.patch.object(
                    roadmap, "_gh_identity",
                    return_value=(None, None, "GitHub repository data is unavailable: no git remotes found"),
                ) as gh_identity,
                mock.patch.object(roadmap, "_persist_cache"),
            ):
                roadmap._repo_identity(directory)
                roadmap._repo_identity(directory)

            self.assertEqual(gh_identity.call_count, 1)

    def test_repo_identity_failure_is_not_persisted(self):
        roadmap._IDENTITY_CACHE.clear()
        with (
            mock.patch.object(
                roadmap, "_git_remote_url", return_value="https://github.com/acme/repo.git"
            ),
            mock.patch.object(
                roadmap, "_gh_identity", return_value=(None, None, "GitHub repository data is unavailable: denied")
            ),
            mock.patch.object(roadmap, "_persist_cache") as persist,
        ):
            result = roadmap._repo_identity("/repo")

        self.assertIsNone(result[0])
        persist.assert_not_called()

    def test_git_remote_url_reads_the_origin_of_a_real_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(
                ["git", "-C", directory, "remote", "add", "origin", "https://github.com/acme/repo.git"],
                check=True,
            )
            self.assertEqual(
                roadmap._git_remote_url(directory), "https://github.com/acme/repo.git"
            )

        self.assertIsNone(roadmap._git_remote_url("/definitely/not/a/checkout"))

    def test_completed_page_is_separate_from_active_board_and_has_round_trip_links(self):
        active_issue = {
            "number": 1, "title": "Active issue", "body": "", "labels": [],
            "url": "https://github.com/acme/repo/issues/1",
        }
        closed_issue = {
            "number": 2, "title": "Finished issue", "body": "", "labels": [],
            "url": "https://github.com/acme/repo/issues/2",
        }
        model = roadmap.build_model([active_issue], {}, [], repo="repo")
        render = lambda title, body, css, js: body

        active_page = roadmap.render_page("repo", ["repo"], model, render)
        closed_page = roadmap.render_completed_page(
            "repo", ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )
        combined_closed = roadmap.render_completed_page(
            None, ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )

        self.assertIn("#1 Active issue", active_page)
        self.assertNotIn("#2 Finished issue", active_page)
        self.assertIn("state=closed", active_page)
        self.assertIn("#2 Finished issue", closed_page)
        self.assertNotIn("#1 Active issue", closed_page)
        self.assertIn("href='/roadmap?repo=repo'", closed_page)
        self.assertIn("state=closed", active_page)
        self.assertIn("Back to active queue", combined_closed)
        self.assertIn("#2 Finished issue", combined_closed)

    def test_completed_page_renders_raw_github_label_dicts(self):
        # gh issue list --json labels returns [{"name": ..., "color": ...}],
        # not plain strings -- render_completed_page must normalize these
        # the same way build_model does for the active queue.
        closed_issue = {
            "number": 3, "title": "Labeled issue", "body": "", "url": "",
            "labels": [{"name": "priority/P1", "color": "ff0000"}],
        }
        render = lambda title, body, css, js: body
        page = roadmap.render_completed_page(
            "repo", ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )
        self.assertIn("priority/P1", page)

    def test_completed_page_displays_text_without_heading_and_bullet_markers(self):
        closed_issue = {
            "number": 58,
            "title": "Readable description",
            "body": "## Problem\n\n- item one\n- <script>alert(1)</script>",
            "url": "",
            "labels": [],
        }
        render = lambda title, body, css, js: body
        page = roadmap.render_completed_page(
            "repo", ["repo"], {"repo": ([closed_issue], {}, [])}, render
        )

        class VisibleText(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []

            def handle_data(self, data):
                self.parts.append(data)

        visible = VisibleText()
        visible.feed(page)
        text = "".join(visible.parts)
        self.assertIn("Problem", text)
        self.assertIn("item one", text)
        self.assertNotIn("## Problem", text)
        self.assertNotIn("- item one", text)
        self.assertIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_failed_fetch_is_not_cached(self):
        """A read that came back empty *and* said why is an outage, not a
        snapshot. Caching it kept a repo blank for the whole hour after its
        cause (auth, a drained fetcher) was already fixed, which is how the
        Roadmap stayed empty on jesus.
        """
        outage = ([], {}, ["GitHub issue data is unavailable: auth failed"])
        recovered = ([{"number": 1, "title": "Real", "body": "", "labels": []}], {}, [])
        with mock.patch.object(
            roadmap, "load_github", side_effect=[outage, recovered]
        ) as load:
            first = roadmap.cached_github("repo", "/repo")
            second = roadmap.cached_github("repo", "/repo")

        self.assertEqual(first, outage)
        self.assertEqual(second, recovered)
        self.assertEqual(load.call_count, 2)

    def test_repo_with_genuinely_no_issues_still_caches(self):
        """The other half of the rule: no issues and no warning is a real
        (empty) snapshot and must still be cached, or a quiet repo re-fetches
        on every page load.
        """
        empty = ([], {}, [])
        with mock.patch.object(roadmap, "load_github", return_value=empty) as load:
            roadmap.cached_github("repo", "/repo")
            again = roadmap.cached_github("repo", "/repo")

        self.assertEqual(again, empty)
        self.assertEqual(load.call_count, 1)

    def test_github_cache_expires_after_one_hour(self):
        roadmap._GITHUB_CACHE.clear()
        clock = [0.0]
        old_issue = {"number": 1, "title": "Old", "body": "", "labels": []}
        fresh_issue = {"number": 1, "title": "Fresh", "body": "", "labels": []}
        with (
            mock.patch.object(roadmap.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(
                roadmap, "load_github",
                side_effect=[([old_issue], {}, []), ([fresh_issue], {}, [])],
            ) as load,
        ):
            first = roadmap.cached_github("repo", "/repo")
            clock[0] = roadmap.GITHUB_CACHE_SECONDS - 1
            cached = roadmap.cached_github("repo", "/repo")
            clock[0] = roadmap.GITHUB_CACHE_SECONDS
            fresh = roadmap.cached_github("repo", "/repo")

        self.assertEqual(first[0][0]["title"], "Old")
        self.assertEqual(cached[0][0]["title"], "Old")
        self.assertEqual(fresh[0][0]["title"], "Fresh")
        self.assertEqual(load.call_count, 2)

    def test_github_cache_reloads_from_disk_after_restart(self):
        issue = {"number": 7, "title": "Saved", "body": "", "labels": []}
        github_data = ([issue], {}, [])
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(roadmap, "CACHE_FILE", str(Path(directory) / "cache.json")),
                mock.patch.object(roadmap.time, "monotonic", return_value=100.0),
                mock.patch.object(roadmap.time, "time", return_value=1_000_000.0),
                mock.patch.object(roadmap, "load_github", return_value=github_data) as load,
            ):
                roadmap._GITHUB_CACHE.clear()
                self.assertEqual(roadmap.cached_github("repo", "/repo"), github_data)

                roadmap._GITHUB_CACHE.clear()
                roadmap._load_cache()
                self.assertEqual(roadmap.cached_github("repo", "/repo"), github_data)
                self.assertEqual(load.call_count, 1)

    def test_expired_github_disk_cache_is_refreshed(self):
        old_issue = {"number": 7, "title": "Old", "body": "", "labels": []}
        fresh_issue = {"number": 7, "title": "Fresh", "body": "", "labels": []}
        now = 1_000_000.0
        saved = {
            "github": [[
                "repo", "open", now - roadmap.GITHUB_CACHE_SECONDS - 1,
                ([old_issue], {}, []),
            ]],
        }
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "cache.json"
            cache_file.write_text(json.dumps(saved), encoding="utf-8")
            with (
                mock.patch.object(roadmap, "CACHE_FILE", str(cache_file)),
                mock.patch.object(roadmap.time, "monotonic", return_value=100.0),
                mock.patch.object(roadmap.time, "time", return_value=now),
                mock.patch.object(
                    roadmap, "load_github", return_value=([fresh_issue], {}, [])
                ) as load,
            ):
                roadmap._GITHUB_CACHE.clear()
                roadmap._load_cache()
                result = roadmap.cached_github("repo", "/repo")

        self.assertEqual(result[0][0]["title"], "Fresh")
        self.assertEqual(load.call_count, 1)
    def test_cache_file_is_derived_from_home_not_hardcoded(self):
        # roadmap.py used to hardcode /home/ghosta here. CACHE_FILE must be
        # computed from HOME at import time, so it works for any user.
        with mock.patch.dict(os.environ, {"HOME": "/tmp/fake-home-for-test"}):
            importlib.reload(roadmap)
            self.addCleanup(importlib.reload, roadmap)
            self.assertEqual(
                roadmap.CACHE_FILE,
                "/tmp/fake-home-for-test/.local/state/lupin/cache.json",
            )

    def test_selected_roadmap_shows_handoff_and_five_minute_reload(self):
        model = roadmap.build_model(
            [],
            {},
            [{"event": "handoff", "status": "HANDOFF, not a finish."}],
            repo="nix",
        )
        render = lambda title, body, css, js: json.dumps([title, body, css, js])

        page = roadmap.render_page("nix", ["nix"], model, render)

        self.assertIn("setTimeout(function(){location.reload()},305000)", page)
        self.assertIn("Latest handoff: HANDOFF, not a finish.", page)

    def test_warning_cards_can_be_dismissed_and_stay_dismissed_after_a_reload(self):
        # The owner asked for dismissible warnings (PR #250). The dismissal
        # happens in the browser, so the reader needs no round trip to the
        # server, and the warning must not come back on the next reload.
        model = roadmap.build_model(
            [],
            {},
            [],
            warnings=[
                "GitHub issue data is unavailable: denied",
                "Recent GitHub comments are unavailable: denied",
            ],
            repo="nix",
        )
        render = lambda title, body, css, js: body + js

        page = roadmap.render_page("nix", ["nix"], model, render)

        # The warning text stays in the page source.
        self.assertIn("GitHub issue data is unavailable: denied", page)
        self.assertIn("Recent GitHub comments are unavailable: denied", page)
        # Every warning card has a way to dismiss it.
        self.assertEqual(page.count("class='warning-dismiss'"), 2)
        # Each card keeps its message as its key, so one dismissal does not
        # hide another warning.
        self.assertIn("data-warning='GitHub issue data is unavailable: denied'", page)
        self.assertIn(
            "data-warning='Recent GitHub comments are unavailable: denied'",
            page,
        )
        # The browser keeps the dismissed text, so the warning stays gone
        # after a reload. The dismissal never lives in the URL (issue #87).
        self.assertIn("lupin-roadmap-dismissed-warning:", page)
        self.assertIn("localStorage", page)
        self.assertNotIn("dismiss=", page)

    def test_repo_fetch_warnings_are_dismissible_on_board_and_list(self):
        warnings = ["GitHub issue data is unavailable: denied", "Comments unavailable"]
        models = {
            repo: roadmap.build_model([], {}, [], warnings=warnings, repo=repo)
            for repo in ("repo-a", "repo-b")
        }
        render = lambda title, body, css, js: body + js

        board = roadmap.render_combined_page(["repo-a", "repo-b"], models, render)
        list_page = roadmap.render_list_page(
            ["repo-a", "repo-b"], models, render, {}
        )

        for page in (board, list_page):
            self.assertIn("GitHub issue data is unavailable: denied", page)
            self.assertIn("Comments unavailable", page)
            self.assertEqual(page.count("class='warning-dismiss'"), 4)
            self.assertIn("lupin-roadmap-dismissed-warning:", page)
            self.assertIn("localStorage", page)
            self.assertIn(
                f"data-warning='{roadmap._warning_key('repo-a', warnings[0])}'",
                page,
            )
            self.assertIn(
                f"data-warning='{roadmap._warning_key('repo-b', warnings[0])}'",
                page,
            )
            self.assertNotEqual(
                roadmap._warning_key("repo-a", warnings[0]),
                roadmap._warning_key("repo-a", warnings[1]),
            )

    def test_completed_page_warning_cards_can_be_dismissed(self):
        closed_issue = {
            "number": 4, "title": "Done issue", "body": "", "labels": [],
            "url": "https://github.com/acme/repo/issues/4",
        }
        render = lambda title, body, css, js: body + js

        page = roadmap.render_completed_page(
            "repo",
            ["repo"],
            {"repo": ([closed_issue], {}, ["GitHub issue data is unavailable: denied"])},
            render,
        )

        self.assertIn("GitHub issue data is unavailable: denied", page)
        self.assertIn("class='warning-dismiss'", page)
        self.assertIn("lupin-roadmap-dismissed-warning:", page)

    def test_repo_roadmap_orders_issue_details_by_recency(self):
        issues = [
            {
                "number": 1,
                "title": "Oldest issue",
                "body": "",
                "labels": ["priority/P0"],
                "updatedAt": "2026-09-20T12:00:00Z",
            },
            {
                "number": 2,
                "title": "No timestamp issue",
                "body": "",
                "labels": ["priority/P1"],
            },
            {
                "number": 3,
                "title": "Newest issue",
                "body": "",
                "labels": ["priority/P2"],
                "updatedAt": "2026-09-25T12:00:00Z",
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="nix")
        render = lambda title, body, css, js: body

        page = roadmap.render_page("nix", ["nix"], model, render)
        details = page.split("<h2>Issue details and recent activity</h2>", 1)[1]

        self.assertEqual(
            [node["number"] for node in model["nodes"]], [1, 2, 3]
        )
        self.assertLess(page.index(">#1 ·"), page.index(">#3 ·"))
        # Each summary starts with a quest-pick checkbox (issue #19) before
        # the issue's own title span, so match on the span rather than an
        # exact "<summary>...". span" prefix.
        self.assertLess(
            details.index("<span title='Newest issue'>#3 Newest issue"),
            details.index("<span title='Oldest issue'>#1 Oldest issue"),
        )
        self.assertLess(
            details.index("<span title='Oldest issue'>#1 Oldest issue"),
            details.index("<span title='No timestamp issue'>#2 No timestamp issue"),
        )

    def test_invalid_graphql_data_returns_warning(self):
        with (
            mock.patch.object(roadmap, "_run_json", return_value=([], None)),
            mock.patch.object(
                roadmap.gh_cache, "cached_gh_json", side_effect=lambda *a, **kw: a[3]()
            ),
        ):
            comments, error = roadmap._read_comments("/repo", "owner", "repo")

        self.assertEqual(comments, {})
        self.assertEqual(error, "GitHub returned invalid comment data")

    def test_comments_include_author_time_and_link(self):
        response = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 5,
                                "comments": {
                                    "nodes": [
                                        {
                                            "body": "Update",
                                            "createdAt": "2026-09-26T12:00:00Z",
                                            "url": "https://github.com/acme/repo/issues/5#issuecomment-9",
                                            "author": {"login": "maintainer"},
                                        }
                                    ]
                                },
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        with (
            mock.patch.object(roadmap, "_run_json", return_value=(response, None)),
            mock.patch.object(
                roadmap.gh_cache, "cached_gh_json", side_effect=lambda *a, **kw: a[3]()
            ),
        ):
            comments, error = roadmap._read_comments("/repo", "owner", "repo")

        self.assertIsNone(error)
        self.assertEqual(comments[5][0]["body"], "Update")
        self.assertEqual(comments[5][0]["author"], "maintainer")
        self.assertEqual(comments[5][0]["createdAt"], "2026-09-26T12:00:00Z")

    def test_github_attachment_id_requires_a_canonical_uuid_url(self):
        attachment_id = "12345678-1234-5678-1234-123456789abc"
        base = "https://github.com/user-attachments/assets/"
        self.assertEqual(roadmap.github_attachment_id(base + attachment_id), attachment_id)
        self.assertIsNone(roadmap.github_attachment_id(base + "example.png"))
        self.assertIsNone(
            roadmap.github_attachment_id(
                "https://evil.example/user-attachments/assets/" + attachment_id
            )
        )
        self.assertIsNone(
            roadmap.github_attachment_id(base + attachment_id + "?redirect=evil")
        )

    def test_compact_time_uses_los_angeles_timezone(self):
        self.assertEqual(
            roadmap._compact_time("2026-09-26T20:00:00Z"),
            "Sep 26, 1:00 PM PDT",
        )

    def test_issue_details_render_github_attachments_through_local_proxy(self):
        issue = {
            "number": 9,
            "title": "New issue",
            "body": (
                "Description <script>alert(1)</script>\n"
                "![safe](https://github.com/user-attachments/assets/12345678-1234-5678-1234-123456789abc)\n"
                "![unsafe](https://evil.example/track.png)\n"
                "![malformed](https://[invalid)"
            ),
            "labels": [],
        }
        comments = {
            9: [
                {
                    "body": "Comment <script>alert(2)</script>",
                    "createdAt": "2026-09-26T12:00:00Z",
                    "url": "https://github.com/acme/repo/issues/9#issuecomment-1",
                    "author": "<img src=x>",
                }
            ]
        }
        node = roadmap.build_model([issue], comments, [], repo="repo")["nodes"][0]

        details = roadmap.render_issue_details(node)
        self.assertIn(
            "<summary><span title='New issue'>#9 New issue</span> · "
            "Details and activity · 1 comment</summary>",
            details,
        )
        node["url"] = "https://github.com/acme/repo/issues/9"
        linked = roadmap.render_issue_details(node)
        self.assertIn(
            "<summary><a href='https://github.com/acme/repo/issues/9' "
            "title='New issue' target='_blank' rel='noopener'>#9 New issue</a> · "
            "Details and activity · 1 comment</summary>",
            linked,
        )

        self.assertIn("Details and activity · 1 comment", details)
        self.assertIn("Description &lt;script&gt;alert(1)&lt;/script&gt;", details)
        self.assertIn(
            "src='/image?id=12345678-1234-5678-1234-123456789abc'", details
        )
        self.assertNotIn("src='https://github.com/user-attachments/assets/", details)
        self.assertNotIn("href='https://evil.example/track.png'", details)
        self.assertNotIn("<img src=x>", details)
        self.assertIn("&lt;img src=x&gt;", details)
        self.assertNotIn("<script>", details)


    def test_issue_details_truncate_title_and_keep_full_title_in_tooltip(self):
        full_title = "A" * 80 + " & <details>"
        node = {
            "number": 10,
            "title": full_title,
            "body": "",
            "labels": [],
            "url": "https://github.com/acme/repo/issues/10",
            "comments": [],
        }

        details = roadmap.render_issue_details(node)

        self.assertIn(
            f"title='{roadmap._escape_attr(full_title)}' "
            f"target='_blank' rel='noopener'>#10 {'A' * 80}…</a>",
            details,
        )
        self.assertNotIn(f">{'A' * 80} &amp; &lt;details&gt;</a>", details)



    def test_recent_comments_are_visible_as_escaped_text(self):
        issue = {"number": 1, "title": "Comment test", "body": "", "labels": []}
        model = roadmap.build_model(
            [issue], {1: ["Latest <script>alert(1)</script>"]}, [], repo="nix"
        )
        render = lambda title, body, css, js: body + css + js

        page = roadmap.render_page("nix", ["nix"], model, render)

        self.assertIn("Details and activity · 1 comment", page)
        self.assertIn("Latest &lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("Latest <script>", page)



    def test_browse_filter_does_not_hide_matching_items(self):
        # updateBrowse() must show every item that matches an active label
        # or search filter, not just the first `chunk` of them — the chunk
        # limit only applies with no filter active.
        js = roadmap._browse_script()
        self.assertIn("item.hidden=filtering?false:shown>=limit;", js)
        self.assertNotIn("item.hidden=Boolean(query||selected.size)||shown>=limit;", js)

    def test_queue_notes_and_board_stage_names_render(self):
        model = roadmap.build_model(
            [{"number": 1, "title": "Issue", "body": "", "labels": []}],
            {},
            [],
            repo="alpha",
        )
        render = lambda title, body, css, js: body

        repo_page = roadmap.render_page("alpha", ["alpha"], model, render)
        board_page = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render
        )

        self.assertIn("Line key: solid lines show dependencies", repo_page)
        self.assertIn("dashed lines show parent or split links", repo_page)
        self.assertIn("Ready: the loop can take this issue next.", board_page)
        self.assertIn("In progress: the loop sent this issue to a machine.", board_page)
        self.assertIn("<h2>Ready</h2>", board_page)
    def test_saved_filters_and_manual_queue_order_render_for_open_eligible_issues(self):
        issues = [
            {
                "number": 31,
                "title": "Labelled eligible issue",
                "body": "",
                "labels": ["priority/P0", "team-a"],
            },
            {
                "number": 32,
                "title": "Held issue",
                "body": "",
                "labels": ["priority/P0", "owner-todo"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        rendered = {}

        def render(title, body, css, js):
            rendered["body"] = body
            rendered["js"] = js
            return body + js

        page = roadmap.render_page("alpha", ["alpha"], model, render)

        self.assertIn("<select id='roadmap-labels' multiple>", page)
        self.assertIn("value='team-a'", page)
        self.assertIn("data-labels='[&quot;priority/P0&quot;, &quot;team-a&quot;]'", page)
        self.assertIn("matchesLabels=[...selected].every", rendered["js"])
        self.assertIn("lupin-roadmap-filter", rendered["js"])
        self.assertIn("lupin-queue-order", rendered["js"])
        self.assertIn("queue=queue.filter(key=>eligible.has(String(key)))", rendered["js"])
        self.assertIn("data-next-batch='alpha'", page)
        self.assertIn("data-queue-key='31'", page)
        self.assertNotIn("<li data-queue-item data-queue-key='32'", page)
        self.assertIn("data-computed-index='0' draggable='true'", page)
        self.assertEqual(model["batch"], [31])
        filtered_board = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render, {"tag": "team-a"}
        )
        self.assertIn("Labelled eligible issue", filtered_board)
        self.assertNotIn("Held issue", filtered_board)

    def test_needs_expert_decision_label_stays_eligible_and_batched(self):
        issues = [
            {
                "number": 41,
                "title": "Needs expert decision",
                "body": "",
                "labels": ["priority/P0", "needs-expert-decision"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        held_stage = next(
            stage for stage in model["stages"] if stage["name"] == "Blocked or held"
        )

        self.assertNotIn(41, held_stage["numbers"])
        self.assertIn(41, model["batch"])

    def test_completely_blocked_on_human_label_is_owner_blocked_not_held(self):
        issues = [
            {
                "number": 42,
                "title": "Blocked on human",
                "body": "",
                "labels": ["priority/P0", "completely-blocked-on-human"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        held_stage = next(
            stage for stage in model["stages"] if stage["name"] == "Blocked or held"
        )

        self.assertNotIn(42, held_stage["numbers"])
        self.assertNotIn(42, model["batch"])
        self.assertEqual(model["ownerBlocked"], [42])

    def test_completely_blocked_on_human_issue_renders_in_owner_blocked_section(self):
        issues = [
            {
                "number": 42,
                "title": "Blocked on human",
                "body": "",
                "labels": ["priority/P0", "completely-blocked-on-human"],
                "url": "https://github.com/gracecraft/alpha/issues/42",
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        combined = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)

        self.assertIn("Blocked issues · need a person", combined)
        self.assertIn("#42", combined)
        self.assertIn("Blocked on human", combined)

    def test_no_owner_blocked_issues_hides_attention_section(self):
        issues = [
            {
                "number": 43,
                "title": "Regular issue",
                "body": "",
                "labels": ["priority/P0"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        combined = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, lambda title, body, css, js: body
        )

        self.assertEqual(model["ownerBlocked"], [])
        self.assertNotIn("Blocked issues · need a person", combined)

    def test_legacy_owner_todo_and_needs_decision_labels_remain_held(self):
        issues = [
            {
                "number": 43,
                "title": "Owner todo",
                "body": "",
                "labels": ["priority/P0", "owner-todo"],
            },
            {
                "number": 44,
                "title": "Needs decision",
                "body": "",
                "labels": ["priority/P0", "needs-decision"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        held_stage = next(
            stage for stage in model["stages"] if stage["name"] == "Blocked or held"
        )

        self.assertIn(43, held_stage["numbers"])
        self.assertIn(44, held_stage["numbers"])
        self.assertNotIn(43, model["batch"])
        self.assertNotIn(44, model["batch"])

    def test_needs_expert_decision_badge_appears_on_dependency_graph(self):
        issues = [
            {
                "number": 45,
                "title": "Needs expert decision badge",
                "body": "",
                "labels": ["priority/P0", "needs-expert-decision"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        page = roadmap.render_page(
            "alpha", ["alpha"], model, lambda title, body, css, js: body
        )

        self.assertIn("needs expert decision", page)

    def test_structured_digest_becomes_labelled_bullet_lists(self):
        issue = {"number": 31, "title": "Redis backend", "body": "", "labels": []}
        ledger = [
            {
                "issue": 31,
                "summary": "Redis slot leases, staged in the prep repo.",
                "highlights": ["Added src/lupin/slots_redis.py."],
                "evidence": ["nix build .#checks.aarch64-linux.default — 39/39 passed."],
                "decisions": ["Only the bmo slot falls back to local; others exit 3."],
                "next": ["Owner: create gracecraft/lupin (#201)."],
            }
        ]

        node = roadmap.build_model([issue], {}, ledger, repo="nix")["nodes"][0]
        digest = node["digest"]

        self.assertEqual(
            [field["label"] for field in digest["fields"]],
            ["Highlights", "Evidence", "Decisions", "Next"],
        )
        self.assertEqual(digest["fields"][0]["bullets"], ["Added src/lupin/slots_redis.py."])
        self.assertEqual(
            digest["headline"], "Redis slot leases, staged in the prep repo."
        )

    def test_next_steps_render_with_a_headline(self):
        issue = {"number": 32, "title": "Tunnel", "body": "", "labels": []}
        ledger = [{"issue": 32, "summary": "Mac launchd tunnel added.", "next": "Owner: set the tailnet IP."}]

        node = roadmap.build_model([issue], {}, ledger, repo="nix")["nodes"][0]

        self.assertEqual(node["digest"]["headline"], "Mac launchd tunnel added.")
        self.assertEqual(
            node["digest"]["fields"],
            [{"key": "next", "label": "Next", "bullets": ["Owner: set the tailnet IP."]}],
        )

    def test_entry_without_any_summary_produces_no_digest(self):
        issue = {"number": 33, "title": "Untouched", "body": "", "labels": []}

        node = roadmap.build_model(
            [issue], {}, [{"issue": 33, "event": "dispatch"}], repo="nix"
        )["nodes"][0]

        self.assertIsNone(node["digest"])
        self.assertNotIn("class='digest'", roadmap.render_issue_details(node))

    def test_digest_bullets_drop_markdown_markers_and_blank_lines(self):
        digest = roadmap._digest(
            {
                "highlights": ["- first item\n\n* second item", 7],
                "evidence": "single string",
            }
        )

        self.assertEqual(
            digest["fields"][0]["bullets"], ["first item", "second item"]
        )
        self.assertEqual(digest["fields"][1]["bullets"], ["single string"])

    def test_digest_bullets_are_escaped_in_the_page(self):
        issue = {"number": 34, "title": "Escaped", "body": "", "labels": []}
        ledger = [{"issue": 34, "highlights": ["<script>alert(1)</script>"]}]

        node = roadmap.build_model([issue], {}, ledger, repo="nix")["nodes"][0]
        page = roadmap.render_issue_details(node)

        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_digest_shows_in_the_selected_issue_panel(self):
        issue = {"number": 35, "title": "Digest page", "body": "", "labels": []}
        ledger = [{"issue": 35, "highlights": ["One bullet."]}]
        model = roadmap.build_model([issue], {}, ledger, repo="alpha")
        render = lambda title, body, css, js: body

        combined = roadmap.render_combined_page(
            ["alpha"],
            {"alpha": model},
            render,
            {"issue": "35", "issue_repo": "alpha"},
        )

        self.assertIn("data-digest-field='highlights'", combined)
        self.assertIn("One bullet.", combined)
    def test_board_and_list_views_link_to_each_other(self):
        model = roadmap.build_model([], {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        board = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)
        listing = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {}
        )

        self.assertIn("href='/roadmap?view=list'", board)
        self.assertIn("href='/roadmap?view=board'", listing)

    def test_list_page_paginates_server_side_and_globally(self):
        issues = [
            {
                "number": number,
                "title": f"Issue {number}",
                "body": "",
                "labels": ["priority/P1"],
                "updatedAt": "2026-09-20T12:00:00Z",
                "url": f"https://github.com/acme/alpha/issues/{number}",
            }
            for number in (1, 2, 3)
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        with mock.patch.object(roadmap, "LIST_PAGE_SIZE", 2):
            first = roadmap.render_list_page(
                ["alpha"], {"alpha": model}, render, {}
            )
            second = roadmap.render_list_page(
                ["alpha"], {"alpha": model}, render, {"page": "2"}
            )

        self.assertIn("Showing <b>1–2</b> of 3", first)
        self.assertIn("#1</a>", first)
        self.assertIn("#2</a>", first)
        self.assertNotIn("#3</a>", first)
        self.assertIn("page=2", first)

        self.assertIn("Showing <b>3–3</b> of 3", second)
        self.assertIn("#3</a>", second)
        self.assertNotIn("#1</a>", second)
        self.assertIn("page=1", second)
        self.assertNotIn("page=3", second)

    def test_list_page_filters_by_priority_capability_repo_and_search(self):
        alpha_issues = [
            {
                "number": 1,
                "title": "Fix backend retry logic",
                "body": "",
                "labels": ["priority/P0"],
                "url": "https://github.com/acme/alpha/issues/1",
            },
            {
                "number": 2,
                "title": "Update screenshot in the UI",
                "body": "",
                "labels": ["priority/P2"],
                "url": "https://github.com/acme/alpha/issues/2",
            },
        ]
        beta_issues = [
            {
                "number": 3,
                "title": "Translate the onboarding copy",
                "body": "",
                "labels": ["priority/P0"],
                "url": "https://github.com/acme/beta/issues/3",
            },
        ]
        models = {
            "alpha": roadmap.build_model(alpha_issues, {}, [], repo="alpha"),
            "beta": roadmap.build_model(beta_issues, {}, [], repo="beta"),
        }
        render = lambda title, body, css, js: body

        by_priority = roadmap.render_list_page(
            ["alpha", "beta"], models, render, {"prio": "P0"}
        )
        self.assertIn("#1</a>", by_priority)
        self.assertIn("#3</a>", by_priority)
        self.assertNotIn("#2</a>", by_priority)

        by_capability = roadmap.render_list_page(
            ["alpha", "beta"], models, render, {"cap": "frontend-ui"}
        )
        self.assertIn("#2</a>", by_capability)
        self.assertNotIn("#1</a>", by_capability)
        self.assertNotIn("#3</a>", by_capability)

        by_repo = roadmap.render_list_page(
            ["alpha", "beta"], models, render, {"repo": "beta"}
        )
        self.assertIn("#3</a>", by_repo)
        self.assertNotIn("#1</a>", by_repo)
        self.assertNotIn("#2</a>", by_repo)

        by_search = roadmap.render_list_page(
            ["alpha", "beta"], models, render, {"q": "translate"}
        )
        self.assertIn("#3</a>", by_search)
        self.assertNotIn("#1</a>", by_search)
        self.assertNotIn("#2</a>", by_search)


    def test_list_stage_filter_and_owner_attention(self):
        issues = [
            {
                "number": 1,
                "title": "Ready task",
                "body": "",
                "labels": ["priority/P1"],
            },
            {
                "number": 2,
                "title": "Needs a person",
                "body": "",
                "labels": ["priority/P1", "completely-blocked-on-human"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        ready = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"stage": "Ready"}
        )
        owner_blocked = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"stage": "Blocked", "owner": "1"}
        )

        self.assertIn("#1</a>", ready)
        self.assertNotIn("#2</a>", ready)
        self.assertIn("#2</a>", owner_blocked)
        self.assertNotIn("#1</a>", owner_blocked)
        self.assertIn("1 blocked need a person", owner_blocked)

    def test_board_repo_filter_enables_quest_selection(self):
        issue = {
            "number": 7,
            "title": "Quest candidate",
            "body": "",
            "labels": ["priority/P1"],
        }
        model = roadmap.build_model([issue], {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        board = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render, {"repo": "alpha"}
        )

        self.assertIn("name='issue' value='7'", board)
        self.assertIn("form='quest-start'", board)
        self.assertIn("action='/quest/start'", board)

        listing = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"repo": "alpha"}
        )
        self.assertIn("name='issue' value='7'", listing)
        self.assertIn("action='/quest/start'", listing)
    def test_list_page_groups_by_epic_and_paginates_ten_at_a_time(self):
        epic = {
            "number": 10,
            "title": "Client resilience",
            "body": "- #11 — First child\n- #12 — Second child\n- #13 — Third child",
            "labels": ["epic"],
            "url": "https://github.com/acme/alpha/issues/10",
        }
        children = [
            {
                "number": number,
                "title": f"Child {number}",
                "body": "",
                "labels": ["priority/P1"],
                "url": f"https://github.com/acme/alpha/issues/{number}",
            }
            for number in (11, 12, 13)
        ]
        model = roadmap.build_model([epic, *children], {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        collapsed = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"group": "epic"}
        )
        self.assertIn("Client resilience", collapsed)
        self.assertIn("(3 issues)", collapsed)
        self.assertNotIn("Child 11", collapsed)

        with mock.patch.object(roadmap, "EPIC_PAGE_SIZE", 2):
            opened = roadmap.render_list_page(
                ["alpha"],
                {"alpha": model},
                render,
                {"group": "epic", "open": "alpha:10"},
            )
            expanded = roadmap.render_list_page(
                ["alpha"],
                {"alpha": model},
                render,
                {"group": "epic", "open": "alpha:10", "shown": "4"},
            )

        self.assertIn("Child 11", opened)
        self.assertIn("Child 12", opened)
        self.assertNotIn("Child 13", opened)
        self.assertIn("Show 2 more · 1 remaining", opened)

        self.assertIn("Child 13", expanded)
        self.assertNotIn("Show 2 more", expanded)

    def test_empty_board_and_list_keep_the_repositories_visible(self):
        # Issue #46: a repo with no open issues must still show the repo and
        # say that the repo has no open issues -- never that no repo exists.
        model = roadmap.build_model([], {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        board = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)
        listing = roadmap.render_list_page(["alpha"], {"alpha": model}, render, {})

        for page in (board, listing):
            self.assertIn("The selected repositories have no open issues: alpha.", page)
            self.assertNotIn("No issues match these filters.", page)
            self.assertIn("alpha", page)

        # The no-repository message only appears without any repository.
        no_repos = roadmap.render_combined_page([], {}, render)
        self.assertIn("No repositories are configured for the roadmap.", no_repos)

    def test_board_and_list_show_stage_tabs_with_counts(self):
        # Issue #55: the stage tabs and their counts live on both views, and
        # the blocked summary shows only when blocked issues are there.
        issues = [
            {
                "number": 1,
                "title": "Ready task",
                "body": "",
                "labels": ["priority/P1"],
            },
            {
                "number": 2,
                "title": "Needs a person",
                "body": "",
                "labels": ["priority/P1", "completely-blocked-on-human"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        board = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)
        listing = roadmap.render_list_page(["alpha"], {"alpha": model}, render, {})

        for page in (board, listing):
            self.assertIn("Ready", page)
            self.assertIn("Blocked", page)
            self.assertIn("stage=Ready", page)
        self.assertIn("Ready <span>1</span>", board)
        self.assertIn("Blocked <span>1</span>", board)
        self.assertIn("1 blocked need a person", listing)

        ready = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render, {"stage": "Ready"}
        )
        self.assertIn("Ready task", ready)
        self.assertNotIn("Needs a person", ready)

        clean = roadmap.build_model(
            [{"number": 3, "title": "Plain", "body": "", "labels": []}],
            {},
            [],
            repo="alpha",
        )
        clean_listing = roadmap.render_list_page(["alpha"], {"alpha": clean}, render, {})
        self.assertNotIn("blocked need a person", clean_listing)

    def test_one_stage_vocabulary_on_board_list_detail_and_graph(self):
        # Issue #72: the board, the list, the detail panel and the queue
        # graph all name a stage the same way.
        issues = [
            {
                "number": 1,
                "title": "Ready task",
                "body": "",
                "labels": ["priority/P1"],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        board = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render, {"issue": "1", "issue_repo": "alpha"}
        )
        listing = roadmap.render_list_page(["alpha"], {"alpha": model}, render, {})
        detail = roadmap.render_page("alpha", ["alpha"], model, render)

        old_words = [
            "Marked in flight",
            "Next batch",
            "Next up",
            "Later queue",
            "Blocked or held",
        ]
        for page in (board, listing, detail):
            for word in old_words:
                self.assertNotIn(word, page)

        # The four words the page uses.
        for word in ("In progress", "Ready", "Later", "Blocked"):
            self.assertIn(word, board)
        self.assertIn("Ready · 1", detail)
        self.assertIn("<span class='dim'>Ready</span>", listing)

    def test_quest_helper_points_at_the_issue_checkboxes(self):
        # Issue #73: the quest card sits under the issue cards, so the
        # helper must not tell a person to look below it.
        model = roadmap.build_model(
            [{"number": 1, "title": "Quest candidate", "body": "", "labels": []}],
            {},
            [],
            repo="alpha",
        )
        render = lambda title, body, css, js: body

        board = roadmap.render_combined_page(
            ["alpha"], {"alpha": model}, render, {"repo": "alpha"}
        )
        detail = roadmap.render_page("alpha", ["alpha"], model, render)

        for page in (board, detail):
            self.assertIn("Tick the issues you want, then start.", page)
            self.assertNotIn("Tick issues below, then start.", page)

    def test_stage_note_is_plain_words_and_says_nothing_twice(self):
        # Issue #74: the note explains the stages, the priorities and the
        # sizes in words a new reader can follow.
        model = roadmap.build_model(
            [{"number": 1, "title": "Issue", "body": "", "labels": []}],
            {},
            [],
            repo="alpha",
        )
        render = lambda title, body, css, js: body
        board = roadmap.render_combined_page(["alpha"], {"alpha": model}, render)
        note = roadmap.STAGE_NOTE

        for jargon in (
            "ledger",
            "dispatch action",
            "non-overlapping",
            "bottleneck",
            "worktree",
            "handoff",
        ):
            self.assertNotIn(jargon, note.lower())
        for word in ("In progress", "Ready", "Later", "Blocked"):
            self.assertIn(f"{word}:", note)
        self.assertIn("size-?", note)
        self.assertIn("P0", note)
        self.assertIn("P4", note)

        sentences = [part.strip() for part in note.split(". ") if part.strip()]
        self.assertEqual(len(sentences), len(set(sentences)))

        self.assertIn("Ready: the loop can take this issue next.", board)
        # The size token on a card says what it means.
        self.assertIn("no size label on this issue", board)

    def test_priority_filter_offers_every_priority_the_rows_can_show(self):
        # Issue #75: P4 issues show up in the rows, so P4 is a filter choice.
        issues = [
            {
                "number": 1,
                "title": "Lowest priority",
                "body": "",
                "labels": [],
            },
        ]
        model = roadmap.build_model(issues, {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        listing = roadmap.render_list_page(["alpha"], {"alpha": model}, render, {})

        self.assertIn("<option value='P4'>P4</option>", listing)
        self.assertIn("P4</span>", listing)
        self.assertEqual(roadmap.LIST_PRIORITIES, roadmap.PRIORITIES)
        for labels in ([], ["priority/P0"], ["p4"], ["priority/P2"], ["prio/P3"]):
            self.assertIn(roadmap._priority(labels), roadmap.LIST_PRIORITIES)

        filtered = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"prio": "P4"}
        )
        self.assertIn("#1</a>", filtered)

    def test_epics_footer_and_pager_read_the_same_way_when_empty(self):
        # Issue #78: one epic is "1 epic", and an empty list reads like a
        # full one.
        epic = {
            "number": 10,
            "title": "Client resilience",
            "body": "- #11 — Retry backoff",
            "labels": ["epic"],
            "url": "https://github.com/acme/alpha/issues/10",
        }
        child = {
            "number": 11,
            "title": "Retry backoff",
            "body": "",
            "labels": ["priority/P1"],
            "url": "https://github.com/acme/alpha/issues/11",
        }
        model = roadmap.build_model([epic, child], {}, [], repo="alpha")
        render = lambda title, body, css, js: body

        grouped = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"group": "epic", "q": "backoff"}
        )
        self.assertIn("1 issue in 1 epic.", grouped)
        self.assertNotIn("1 epics", grouped)
        self.assertIn("Open an epic to see its issues.", grouped)

        full = roadmap.render_list_page(["alpha"], {"alpha": model}, render, {})
        empty = roadmap.render_list_page(
            ["alpha"], {"alpha": model}, render, {"q": "zzzznomatch"}
        )
        self.assertIn("Showing <b>1–2</b> of 2 issues", full)
        self.assertIn("Showing <b>0</b> of 0 issues", empty)


class DependencyDagTests(unittest.TestCase):
    """build_dependency_dag combines blockedBy/blocking links -- GitHub's
    real issue-dependency feature, not a text search -- into one DAG that
    can span repos.
    """

    def setUp(self):
        roadmap._DEPENDENCY_CACHE.clear()

    def test_cross_repo_edge_from_blocked_by_and_blocking(self):
        # api#10 is blocked by core#3; core#3 reports the same link as
        # "blocking" api#10. Both directions should produce one DAG.
        repo_links = {
            "api": {10: {"blockedBy": [{"repo": "core", "number": 3}], "blocking": []}},
            "core": {3: {"blockedBy": [], "blocking": [{"repo": "api", "number": 10}]}},
        }

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(dag["cycles"], [])
        self.assertEqual(
            dag["repos"]["api"],
            [{"number": 10, "blockedBy": [{"repo": "core", "number": 3}], "blocking": []}],
        )
        self.assertEqual(
            dag["repos"]["core"],
            [{"number": 3, "blockedBy": [], "blocking": [{"repo": "api", "number": 10}]}],
        )

    def test_cycle_across_repos_is_detected_and_reported_once(self):
        # api#1 blocks core#2, core#2 blocks web#3, web#3 blocks api#1.
        repo_links = {
            "api": {1: {"blockedBy": [], "blocking": [{"repo": "core", "number": 2}]}},
            "core": {2: {"blockedBy": [], "blocking": [{"repo": "web", "number": 3}]}},
            "web": {3: {"blockedBy": [], "blocking": [{"repo": "api", "number": 1}]}},
        }

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(len(dag["cycles"]), 1)
        cycle_keys = {(node["repo"], node["number"]) for node in dag["cycles"][0]}
        self.assertEqual(cycle_keys, {("api", 1), ("core", 2), ("web", 3)})

    def test_self_blocking_issue_is_reported_as_a_cycle(self):
        # Malformed data: an issue listed as blocking itself. This must
        # still show up in "cycles" -- not get silently dropped.
        repo_links = {
            "lupin": {5: {"blockedBy": [], "blocking": [{"repo": "lupin", "number": 5}]}},
        }

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(len(dag["cycles"]), 1)
        cycle_keys = [(node["repo"], node["number"]) for node in dag["cycles"][0]]
        self.assertEqual(cycle_keys, [("lupin", 5), ("lupin", 5)])

    def test_no_dependencies_is_an_empty_but_well_formed_dag(self):
        repo_links = {"solo": {5: {"blockedBy": [], "blocking": []}}}

        dag = roadmap.build_dependency_dag(repo_links)

        self.assertEqual(dag["cycles"], [])
        self.assertEqual(
            dag["repos"]["solo"],
            [{"number": 5, "blockedBy": [], "blocking": []}],
        )

    def test_read_dependencies_keeps_the_target_repo_name(self):
        response = {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [
                            {
                                "number": 10,
                                "blockedBy": {
                                    "nodes": [{"number": 3, "repository": {"name": "core"}}]
                                },
                                "blocking": {"nodes": []},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

        with (
            mock.patch.object(roadmap, "_run_json", return_value=(response, None)),
            mock.patch.object(
                roadmap.gh_cache, "cached_gh_json", side_effect=lambda *a, **kw: a[3]()
            ),
        ):
            links, error = roadmap._read_dependencies("/repo", "acme", "api")

        self.assertIsNone(error)
        self.assertEqual(
            links, {10: {"blockedBy": [{"repo": "core", "number": 3}], "blocking": []}}
        )

    def test_cached_dependency_dag_combines_repos_and_surfaces_warnings(self):
        with mock.patch.object(
            roadmap,
            "load_dependencies",
            side_effect=[
                ({1: {"blockedBy": [], "blocking": [{"repo": "core", "number": 2}]}}, []),
                ({}, ["GitHub dependency links are unavailable: boom"]),
            ],
        ) as load:
            dag = roadmap.cached_dependency_dag(["api", "core"], code_dir="/code")

        self.assertEqual(dag["repos"]["api"][0]["number"], 1)
        self.assertEqual(
            dag["warnings"], {"core": ["GitHub dependency links are unavailable: boom"]}
        )
        self.assertEqual(load.call_count, 2)



def test_roadmap_reads_latest_shared_events(redis_port, flush_redis, monkeypatch):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    issues = [
        {"number": 21, "title": "Parent", "body": "", "labels": []},
        {"number": 22, "title": "Split task", "body": "", "labels": []},
    ]
    monkeypatch.setattr(
        roadmap, "cached_github", lambda *args, **kwargs: (issues, {}, [])
    )
    monkeypatch.setattr(roadmap, "_repo_identity", lambda _path: ("acme", "repo", None))

    before = roadmap.cached_model("repo", "/repo", connection=connection)
    ledger.append_event(
        "acme/repo",
        {
            "event": "dispatch", "issue": 21, "status": "running",
            "children": [22],
        },
        **connection,
    )
    for issue_number in range(100, 110):
        ledger.append_event(
            "acme/repo",
            {
                "event": "dispatch", "issue": issue_number,
                "status": "running",
            },
            **connection,
        )

    ledger.append_event(
        "acme/repo",
        {
            "event": "handoff", "issue": 22, "status": "ready",
            "summary": "Shared handoff.", "highlights": ["Event visible on both hosts."],
        },
        **connection,
    )
    after = roadmap.cached_model("repo", "/repo", connection=connection)

    assert not before["nodes"][0]["dispatched"]
    nodes = {node["number"]: node for node in after["nodes"]}
    assert nodes[21]["dispatched"]
    assert nodes[22]["digest"]["headline"] == "Shared handoff."
    assert after["handoffStatus"] == "ready"
    assert {"from": 21, "to": 22, "kind": "split"} in after["edges"]
    page = roadmap.render_page(
        "repo", ["repo"], after, lambda _title, body, _css, _js: body
    )
    assert "Latest handoff: ready" in page
    assert "Event visible on both hosts." in page
    assert "class='parent-link'" in page


def test_roadmap_ignores_local_ledger_history_and_empty_stream(
    redis_port, flush_redis, monkeypatch, tmp_path
):
    connection = {"redis_host": "127.0.0.1", "redis_port": redis_port}
    issue = {"number": 21, "title": "Old event", "body": "", "labels": []}
    loop_dir = tmp_path / ".loop"
    loop_dir.mkdir()
    (loop_dir / "loop-state.json").write_text(
        json.dumps([{"issue": 21, "event": "dispatch", "summary": "Old"}]),
        encoding="utf-8",
    )
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(
        json.dumps({
            "ledger": [["repo", 0, ([{"issue": 21, "event": "dispatch"}], None)]],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(roadmap, "CACHE_FILE", str(cache_file))
    roadmap._GITHUB_CACHE.clear()
    roadmap._load_cache()
    monkeypatch.setattr(
        roadmap, "cached_github", lambda *args, **kwargs: ([issue], {}, [])
    )
    monkeypatch.setattr(roadmap, "_repo_identity", lambda _path: ("acme", "repo", None))

    model = roadmap.cached_model("repo", str(tmp_path), connection=connection)

    assert not model["nodes"][0]["dispatched"]
    assert model["nodes"][0]["digest"] is None
    assert model["handoffStatus"] is None
    assert model["warnings"] == []


def test_roadmap_shows_empty_ledger_on_unavailable_coordinator(
    closed_port, monkeypatch
):
    issue = {"number": 21, "title": "Issue", "body": "", "labels": []}
    monkeypatch.setattr(
        roadmap, "cached_github", lambda *args, **kwargs: ([issue], {}, [])
    )
    monkeypatch.setattr(roadmap, "_repo_identity", lambda _path: ("acme", "repo", None))

    model = roadmap.cached_model(
        "repo", "/repo",
        connection={"redis_host": "127.0.0.1", "redis_port": closed_port},
    )

    assert not model["nodes"][0]["dispatched"]
    assert model["handoffStatus"] is None
    assert any(
        "Repository ledger is unavailable" in warning
        for warning in model["warnings"]
    )


_REAL_RUN = subprocess.run


def _run_except_gh(argv, *args, **kwargs):
    # git may run for real. gh must never run in a missing checkout.
    if argv[0] == "gh":
        raise AssertionError("gh ran in a missing checkout")
    return _REAL_RUN(argv, *args, **kwargs)


class CheckoutPathTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_missing_checkout_is_named_and_gh_is_not_run(self):
        missing = str(self.root / "gone")
        with mock.patch.object(subprocess, "run", side_effect=_run_except_gh):
            data, error = roadmap._run_json(["gh", "repo", "view"], missing)

        self.assertIsNone(data)
        self.assertEqual(error, f"checkout directory not found: {missing}")

    def test_missing_program_is_reported_as_not_installed(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            data, error = roadmap._run_json(["gh", "repo", "view"], str(self.root))

        self.assertIsNone(data)
        self.assertEqual(error, "gh is not installed")

    def test_load_github_warning_names_missing_checkout(self):
        missing = str(self.root / "gone")
        with mock.patch.object(subprocess, "run", side_effect=_run_except_gh):
            _issues, _comments, warnings = roadmap.load_github(missing)

        self.assertEqual(len(warnings), 1)
        self.assertIn(missing, warnings[0])
        self.assertNotIn("is not installed", warnings[0])

    def test_code_dir_follows_loop_code_dir_env(self):
        code_dir = str(self.root / "code")
        src = str(Path(roadmap.__file__).resolve().parents[1])
        env = {**os.environ, "LUPIN_LOOP_CODE_DIR": code_dir, "PYTHONPATH": src}
        result = subprocess.run(
            [sys.executable, "-c", "from lupin import roadmap; print(roadmap.CODE_DIR)"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), code_dir)


if __name__ == "__main__":
    unittest.main()

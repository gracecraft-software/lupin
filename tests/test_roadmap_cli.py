"""Tests for `lupin roadmap` (issue #10) -- the text/JSON rendering on top
of `roadmap.py`'s dependency DAG (issue #4) and `claims.py`'s claims
(issue #6).

Every test mocks `roadmap._repo_identity`, `roadmap.cached_github`, and
`roadmap.cached_dependency_dag` directly (same objects issue #4's own
tests patch) so nothing here touches the network or changes how a
dependency is found -- only how it's rendered.
"""

from __future__ import annotations

import functools
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import pytest

from lupin import claims, cli, machines, roadmap, roadmap_cli
from lupin.slots import CoordinatorUnreachable


def _issue(number, title, priority="P1", labels=None):
    labels = list(labels) if labels is not None else [{"name": priority}]
    return {"number": number, "title": title, "labels": labels, "body": ""}


def _identity(owner="acme"):
    return lambda path: (owner, path.rsplit("/", 1)[-1], None)


class BuildRoadmapTests(unittest.TestCase):
    def _patch(self, open_issues, dag, identity=None, issue_state=None):
        patches = [
            mock.patch.object(roadmap, "_repo_identity", side_effect=identity or _identity()),
            mock.patch.object(
                roadmap, "cached_github",
                side_effect=lambda repo, path, state="open", **kw: (
                    (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                ),
            ),
            mock.patch.object(
                roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir, **kw: dag
            ),
        ]
        if issue_state is not None:
            # `_issue_state` now takes (path, number, owner, name, connection=...)
            # (issue #35) -- callers here only care about (path, number).
            patches.append(
                mock.patch.object(
                    roadmap_cli, "_issue_state",
                    side_effect=lambda path, number, owner, name, **kw: issue_state(path, number),
                )
            )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_plain_list_marks_ready_blocked_and_claimed(self):
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(431, "rate-limit headers", "P2"),
                _issue(422, "split session store", "P2"),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": []},
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 418}], "blocking": []},
                    {"number": 422, "blockedBy": [], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)
        claims_lookup = mock.Mock(return_value={"acme/api-gateway#422": {"session": "api-gateway#2"}})

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=claims_lookup
        )

        self.assertEqual(code, 0)
        # #422 is claimed, so it's excluded from the "ready" tally even
        # though it (like #418) still shows as a row in the default view.
        self.assertIn("api-gateway · 1 ready · 1 blocked", text)
        self.assertIn("#418  P1  retry backoff", text)
        self.assertIn("#422  P2  split session store", text)
        self.assertIn("claimed by api-gateway #2", text)
        # #431 is blocked, so it's hidden from the default (ready) stage.
        self.assertNotIn("#431", text)
        claims_lookup.assert_called_once_with(["acme/api-gateway"])

    def test_stage_blocked_shows_only_blocked_issues(self):
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(431, "rate-limit headers", "P2"),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": []},
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 418}], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)

        text, code = roadmap_cli.run(
            "api-gateway", 10, "blocked", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertIn("#431", text)
        self.assertIn("blocked", text)
        self.assertNotIn("#418", text)

    def test_no_priority_label_sorts_last_and_warns(self):
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(440, "docs", labels=[]),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": []},
                    {"number": 440, "blockedBy": [], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertIn("warning: #440 has no priority label. Sorted last.", text)
        # #418 (P1) must rank above #440 (no label, falls back to last).
        self.assertLess(text.index("#418"), text.index("#440"))

    def test_broken_dependency_link_is_treated_as_unblocked_with_warning(self):
        open_issues = {"api-gateway": [_issue(431, "rate-limit headers", "P2")]}
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 999}], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag, issue_state=lambda path, number: None)

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertIn(
            "warning: #431 depends on #999, which does not exist. Treated as unblocked.", text
        )
        # Not blocked -- the broken link doesn't count, so it shows as ready.
        row = next(line for line in text.splitlines() if "#431" in line and line.strip()[0].isdigit())
        self.assertTrue(row.rstrip().endswith("ready"))

    def test_closed_blocker_resolves_silently_no_warning(self):
        open_issues = {"api-gateway": [_issue(431, "rate-limit headers", "P2")]}
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 400}], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag, issue_state=lambda path, number: "CLOSED")

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=lambda repos: {}
        )

        self.assertEqual(code, 0)
        self.assertNotIn("warning:", text)

    def test_claim_lookup_failure_is_reported_not_guessed(self):
        open_issues = {"api-gateway": [_issue(418, "retry backoff", "P1")]}
        dag = {
            "repos": {"api-gateway": [{"number": 418, "blockedBy": [], "blocking": []}]},
            "cycles": [],
            "warnings": {},
        }
        self._patch(open_issues, dag)

        def failing_claims(repos):
            raise CoordinatorUnreachable("claims_for")

        text, code = roadmap_cli.run(
            "api-gateway", 10, "ready", False, False, False, claims_lookup=failing_claims
        )

        self.assertEqual(code, 0)
        self.assertIn("warning: claim data is unavailable", text)
        self.assertIn("#418  P1  retry backoff", text)


class EmptyStateTests(unittest.TestCase):
    def test_per_repo_empty_message_names_claimed_and_blocked_counts(self):
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     [_issue(422, "split store", "P2"), _issue(440, "docs", "P3")], {}, []
                 ) if state == "open" else ([], {}, []),
             ), \
             mock.patch.object(
                 roadmap, "cached_dependency_dag",
                 side_effect=lambda repos, code_dir: {
                     "repos": {
                         "api-gateway": [
                             {"number": 422, "blockedBy": [], "blocking": []},
                             {"number": 440, "blockedBy": [{"repo": "api-gateway", "number": 422}], "blocking": []},
                         ]
                     },
                     "cycles": [],
                     "warnings": {},
                 },
             ):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", False, False, False,
                claims_lookup=lambda repos: {"acme/api-gateway#422": {"session": "api-gateway#2"}},
            )

        self.assertEqual(code, 0)
        self.assertEqual(
            text, "No ready tasks in api-gateway. 1 is claimed by other loops, 1 is blocked."
        )

    def test_empty_everywhere_across_enabled_repos(self):
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: ([], {}, []),
             ), \
             mock.patch.object(
                 roadmap, "cached_dependency_dag",
                 return_value={"repos": {}, "cycles": [], "warnings": {}},
             ):
            text, code = roadmap_cli.run(
                None, 10, "ready", False, False, False,
                enabled_repos=lambda: ["api-gateway", "billing-core"],
                claims_lookup=lambda repos: {},
            )

        self.assertEqual(code, 0)
        self.assertEqual(text, "No ready tasks in any repo. Nothing to run.")

    def test_no_enabled_repos_at_all(self):
        text, code = roadmap_cli.run(
            None, 10, "ready", False, False, False,
            enabled_repos=lambda: [], claims_lookup=lambda repos: {},
        )

        self.assertEqual(code, 0)
        self.assertEqual(text, "No ready tasks in any repo. Nothing to run.")


class DagViewTests(unittest.TestCase):
    def _dag_model(self):
        # #410 unblocks #418 and #422; both unblock #431; #431 unblocks #440.
        open_issues = {
            "api-gateway": [
                _issue(418, "retry backoff", "P1"),
                _issue(422, "split store", "P2"),
                _issue(431, "rate-limit headers", "P2"),
                _issue(440, "docs", "P3"),
                _issue(451, "trace ids", "P3"),
            ]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 418, "blockedBy": [], "blocking": [{"repo": "api-gateway", "number": 431}]},
                    {"number": 422, "blockedBy": [], "blocking": [{"repo": "api-gateway", "number": 431}]},
                    {
                        "number": 431,
                        "blockedBy": [
                            {"repo": "api-gateway", "number": 418},
                            {"repo": "api-gateway", "number": 422},
                        ],
                        "blocking": [{"repo": "api-gateway", "number": 440}],
                    },
                    {"number": 440, "blockedBy": [{"repo": "api-gateway", "number": 431}], "blocking": []},
                    {"number": 451, "blockedBy": [], "blocking": []},
                ]
            },
            "cycles": [],
            "warnings": {},
        }
        return open_issues, dag

    def test_dag_renders_edges_glyphs_legend_and_blocking_summary(self):
        open_issues, dag = self._dag_model()
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", True, False, False, claims_lookup=lambda repos: {}
            )

        self.assertEqual(code, 0)
        self.assertIn("#418 ● retry backoff", text)
        self.assertIn("└─> #431", text)
        self.assertIn("(no dependencies)", text)  # #451 has no edges at all
        self.assertIn("● ready  ◐ claimed  ✕ blocked", text)
        self.assertIn("Blocking the most:", text)
        self.assertIn("Next ready on the critical path: #418", text)

    def test_dag_multi_repo_labels_include_repo_name(self):
        open_issues = {
            "api-gateway": [_issue(10, "api task", "P1")],
            "billing-core": [_issue(20, "billing task", "P1")],
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 10, "blockedBy": [], "blocking": [{"repo": "billing-core", "number": 20}]}
                ],
                "billing-core": [
                    {"number": 20, "blockedBy": [{"repo": "api-gateway", "number": 10}], "blocking": []}
                ],
            },
            "cycles": [],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                None, 10, "ready", True, False, False,
                enabled_repos=lambda: ["api-gateway", "billing-core"],
                claims_lookup=lambda repos: {},
            )

        self.assertEqual(code, 0)
        self.assertIn("api-gateway#10", text)
        self.assertIn("billing-core#20", text)

    def test_cycle_is_reported_as_error_and_blocks_rendering(self):
        open_issues = {
            "api-gateway": [_issue(431, "rate-limit headers", "P2"), _issue(440, "docs", "P3")]
        }
        dag = {
            "repos": {
                "api-gateway": [
                    {"number": 431, "blockedBy": [{"repo": "api-gateway", "number": 440}], "blocking": []},
                    {"number": 440, "blockedBy": [{"repo": "api-gateway", "number": 431}], "blocking": []},
                ]
            },
            "cycles": [
                [
                    {"repo": "api-gateway", "number": 431},
                    {"repo": "api-gateway", "number": 440},
                    {"repo": "api-gateway", "number": 431},
                ]
            ],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", True, False, False, claims_lookup=lambda repos: {}
            )

        self.assertEqual(code, 1)
        self.assertEqual(
            text, "error: dependency cycle #431 -> #440 -> #431. Fix the links in the tracker."
        )


class JsonOutputTests(unittest.TestCase):
    def test_json_mirrors_filtered_list(self):
        open_issues = {"api-gateway": [_issue(418, "retry backoff", "P1")]}
        dag = {
            "repos": {"api-gateway": [{"number": 418, "blockedBy": [], "blocking": []}]},
            "cycles": [],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
            text, code = roadmap_cli.run(
                "api-gateway", 10, "ready", False, True, False, claims_lookup=lambda repos: {}
            )

        import json

        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["repos"]["api-gateway"]["ready"], 1)
        self.assertEqual(payload["repos"]["api-gateway"]["issues"][0]["number"], 418)
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["cycles"], [])


if __name__ == "__main__":
    unittest.main()


def _record(name, repos, state="online"):
    """A machine registry record. `repos` is a list of (repo, enabled)."""
    return {
        "name": name,
        "state": state,
        "repos": [{"repo": repo, "enabled": enabled, "loopable": True} for repo, enabled in repos],
        "loops": [],
        "actions": [],
    }


class FleetReposTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.code_dir = tmp.name

    def _checkout(self, *names):
        for name in names:
            os.makedirs(os.path.join(self.code_dir, name))

    def test_adds_a_repo_that_another_machine_enables(self):
        self._checkout("bodysmith")
        records = [_record("ralpha", [("bodysmith", True)])]

        repos, warnings = roadmap_cli.fleet_repos(["plantsmith"], records, "jesus", self.code_dir)

        self.assertEqual(repos, ["plantsmith", "bodysmith"])
        self.assertEqual(warnings, [])

    def test_skips_disabled_repos_and_this_machines_own_record(self):
        self._checkout("bodysmith", "oldrepo", "plantsmith")
        records = [
            _record("ralpha", [("bodysmith", True), ("oldrepo", False)]),
            _record("jesus", [("plantsmith", True)]),
        ]

        repos, warnings = roadmap_cli.fleet_repos([], records, "jesus", self.code_dir)

        self.assertEqual(repos, ["bodysmith"])
        self.assertEqual(warnings, [])

    def test_names_a_repo_that_has_no_checkout_here(self):
        records = [_record("ralpha", [("bodysmith", True)])]

        repos, warnings = roadmap_cli.fleet_repos(["plantsmith"], records, "jesus", self.code_dir)

        self.assertEqual(repos, ["plantsmith"])
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].startswith("bodysmith: enabled on ralpha"), warnings[0])
        self.assertIn(os.path.join(self.code_dir, "bodysmith"), warnings[0])

    def test_does_not_repeat_a_repo_that_this_machine_enables(self):
        self._checkout("plantsmith")
        records = [_record("ralpha", [("plantsmith", True)])]

        repos, warnings = roadmap_cli.fleet_repos(["plantsmith"], records, "jesus", self.code_dir)

        self.assertEqual(repos, ["plantsmith"])
        self.assertEqual(warnings, [])

    def test_ignores_a_repo_name_that_could_leave_the_code_directory(self):
        records = [_record("ralpha", [("../etc", True)])]

        repos, warnings = roadmap_cli.fleet_repos([], records, "jesus", self.code_dir)

        self.assertEqual(repos, [])
        self.assertEqual(warnings, [])


class FleetRoadmapTests(unittest.TestCase):
    """`run` with a machine registry: the repos of other machines show up."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.code_dir = tmp.name
        self.open_issues = {
            "plantsmith": [_issue(7, "water the ferns", "P2")],
            "bodysmith": [_issue(161, "stretch plan", "P1"), _issue(205, "sleep log", "P3")],
        }
        self.dag = {
            "repos": {
                "plantsmith": [{"number": 7, "blockedBy": [], "blocking": []}],
                "bodysmith": [
                    {"number": 161, "blockedBy": [], "blocking": [{"repo": "bodysmith", "number": 205}]},
                    {"number": 205, "blockedBy": [{"repo": "bodysmith", "number": 161}], "blocking": []},
                ],
            },
            "cycles": [],
            "warnings": {},
        }
        patches = [
            mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()),
            mock.patch.object(
                roadmap, "cached_github",
                side_effect=lambda repo, path, state="open", **kw: (
                    (self.open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                ),
            ),
            mock.patch.object(
                roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: self.dag
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, *, records, dag=False, as_json=False, repo=None, enabled=("plantsmith",)):
        for name in ("plantsmith", "bodysmith"):
            os.makedirs(os.path.join(self.code_dir, name), exist_ok=True)
        return roadmap_cli.run(
            repo, 10, "all", dag, as_json, False,
            code_dir=self.code_dir,
            enabled_repos=lambda: list(enabled),
            claims_lookup=lambda repos: {},
            machine_records=records,
            local_host="jesus",
        )

    def test_json_lists_a_repo_that_only_another_machine_enables(self):
        records = lambda: [_record("ralpha", [("bodysmith", True)])]

        text, code = self._run(records=records, as_json=True)

        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(list(payload["repos"]), ["plantsmith", "bodysmith"])
        self.assertEqual(
            [issue["number"] for issue in payload["repos"]["bodysmith"]["issues"]], [161, 205]
        )
        self.assertEqual(payload["warnings"], [])

    def test_plain_list_shows_a_repo_that_only_another_machine_enables(self):
        records = lambda: [_record("ralpha", [("bodysmith", True)])]

        text, code = self._run(records=records)

        self.assertEqual(code, 0)
        self.assertIn("bodysmith · 1 ready · 1 blocked", text)
        self.assertIn("#161  P1  stretch plan", text)

    def test_dag_view_shows_a_repo_that_only_another_machine_enables(self):
        records = lambda: [_record("ralpha", [("bodysmith", True)])]

        text, code = self._run(records=records, dag=True)

        self.assertEqual(code, 0)
        self.assertIn("bodysmith#161", text)
        self.assertIn("└─> bodysmith#205", text)
        self.assertIn("plantsmith#7", text)

    def test_repo_without_a_checkout_gets_a_warning_in_every_view(self):
        records = lambda: [_record("ralpha", [("farmsmith", True)])]

        plain, _ = self._run(records=records)
        dag_text, _ = self._run(records=records, dag=True)
        payload = json.loads(self._run(records=records, as_json=True)[0])

        for text in (plain, dag_text):
            self.assertIn("warning: farmsmith: enabled on ralpha", text)
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertTrue(payload["warnings"][0].startswith("farmsmith: enabled on ralpha"))
        self.assertNotIn("farmsmith", payload["repos"])

    def test_json_is_still_json_when_no_repo_can_be_shown(self):
        records = lambda: [_record("ralpha", [("farmsmith", True)])]

        text, code = self._run(records=records, as_json=True, enabled=())

        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["repos"], {})
        self.assertEqual(payload["claims"], [])
        self.assertTrue(payload["warnings"][0].startswith("farmsmith: enabled on ralpha"))

    def test_plain_view_with_no_repo_still_prints_the_warning(self):
        records = lambda: [_record("ralpha", [("farmsmith", True)])]

        text, code = self._run(records=records, enabled=())

        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("No ready tasks in any repo. Nothing to run."))
        self.assertIn("warning: farmsmith: enabled on ralpha", text)

    def test_unreachable_registry_warns_and_keeps_this_machines_repos(self):
        def records():
            raise CoordinatorUnreachable("machine registry")

        text, code = self._run(records=records, as_json=True)

        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(list(payload["repos"]), ["plantsmith"])
        self.assertEqual(
            payload["warnings"],
            ["machine registry is unavailable. Showing the repos that this machine enables."],
        )

    def test_explicit_repo_does_not_read_the_registry(self):
        records = mock.Mock(return_value=[_record("ralpha", [("bodysmith", True)])])

        text, code = self._run(records=records, as_json=True, repo="plantsmith")

        self.assertEqual(code, 0)
        self.assertEqual(list(json.loads(text)["repos"]), ["plantsmith"])
        records.assert_not_called()

    def test_without_a_registry_function_only_this_machines_repos_show(self):
        text, _ = self._run(records=None, as_json=True)

        self.assertEqual(list(json.loads(text)["repos"]), ["plantsmith"])


class ClaimsArrayTests(unittest.TestCase):
    def _run(self, claims_lookup, as_json=True):
        open_issues = {"bodysmith": [_issue(161, "stretch plan", "P1")]}
        dag = {
            "repos": {"bodysmith": [{"number": 161, "blockedBy": [], "blocking": []}]},
            "cycles": [],
            "warnings": {},
        }
        with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
             mock.patch.object(
                 roadmap, "cached_github",
                 side_effect=lambda repo, path, state="open", **kw: (
                     (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
                 ),
             ), \
             mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag), \
             mock.patch.object(roadmap_cli, "_now", return_value=10_000.0):
            result = roadmap_cli.run(
                "bodysmith", 10, "ready", False, as_json, False, claims_lookup=claims_lookup
            )
            # The clock patch must not reach the stdlib `time` module.
            self.assertNotEqual(time.time(), 10_000.0)
        return result

    def test_json_lists_every_active_claim_in_the_repos_shown(self):
        lookup = lambda repos: {
            # #205 is not an open issue, but its claim is still active.
            "acme/bodysmith#205": {
                "host": "ralpha", "session": "omp-2026-10-08", "since": 9_000.0, "ttl": 6600.4,
            },
            "acme/bodysmith#161": {
                "host": "ralpha", "session": "omp-2026-10-08", "since": 9_940.0, "ttl": 59.6,
            },
            "acme/bodysmith#9": {"host": "jesus", "session": "loop-1", "since": 9_999.0, "ttl": 1.0},
            # A repo that is not in the roadmap is not listed.
            "acme/other#1": {"host": "jesus", "session": "x", "since": 9_999.0, "ttl": 1.0},
        }

        text, code = self._run(lookup)

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(text)["claims"],
            [
                {"target": "acme/bodysmith#9", "host": "jesus", "holder": "loop-1",
                 "age_seconds": 1, "ttl_seconds": 1},
                {"target": "acme/bodysmith#161", "host": "ralpha", "holder": "omp-2026-10-08",
                 "age_seconds": 60, "ttl_seconds": 60},
                {"target": "acme/bodysmith#205", "host": "ralpha", "holder": "omp-2026-10-08",
                 "age_seconds": 1000, "ttl_seconds": 6600},
            ],
        )

    def test_a_claim_without_since_or_ttl_gives_null_not_a_guess(self):
        text, _ = self._run(lambda repos: {"acme/bodysmith#161": {"session": "loop-1"}})

        self.assertEqual(
            json.loads(text)["claims"],
            [{"target": "acme/bodysmith#161", "host": None, "holder": "loop-1",
              "age_seconds": None, "ttl_seconds": None}],
        )

    def test_no_claims_gives_an_empty_array(self):
        text, _ = self._run(lambda repos: {})

        self.assertEqual(json.loads(text)["claims"], [])

    def test_unreachable_claim_store_gives_an_empty_array_and_a_warning(self):
        def failing(repos):
            raise CoordinatorUnreachable("claims_for")

        payload = json.loads(self._run(failing)[0])

        self.assertEqual(payload["claims"], [])
        self.assertIn("claim data is unavailable", payload["warnings"][0])

    def test_plain_output_has_no_claims_section(self):
        text, _ = self._run(
            lambda repos: {"acme/bodysmith#161": {"host": "ralpha", "session": "s", "since": 1.0}},
            as_json=False,
        )

        self.assertNotIn("ralpha", text)


class RoadmapCommandWiringTests(unittest.TestCase):
    def _code_dir_run_gets(self, value):
        """The `code_dir` that `roadmap` passes to `run`, with LUPIN_LOOP_CODE_DIR set to `value`."""
        connection = {
            "redis_host": "h", "redis_port": 1, "redis_username": None, "redis_password": None,
        }
        with mock.patch.dict(os.environ), \
             mock.patch.object(machines, "resolve_connection", return_value=connection), \
             mock.patch.object(roadmap_cli, "run", return_value=("{}", 0)) as run:
            os.environ.pop("LUPIN_LOOP_CODE_DIR", None)
            if value is not None:
                os.environ["LUPIN_LOOP_CODE_DIR"] = value
            cli.main(["roadmap", "--json"])
        return run.call_args.kwargs["code_dir"]

    def test_command_reads_the_code_dir_from_the_environment(self):
        self.assertEqual(self._code_dir_run_gets("/srv/code"), "/srv/code")

    def test_command_defaults_the_code_dir_to_code(self):
        self.assertEqual(self._code_dir_run_gets(None), "/code")

    def test_command_passes_the_registry_and_the_claim_ttl_to_run(self):
        connection = {
            "redis_host": "h", "redis_port": 1, "redis_username": None, "redis_password": None,
        }
        with mock.patch.object(machines, "resolve_connection", return_value=connection), \
             mock.patch.object(roadmap_cli, "run", return_value=("{}", 0)) as run:
            self.assertEqual(cli.main(["roadmap", "--json"]), 0)

        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["claims_lookup"].func, claims.claims_for)
        self.assertTrue(kwargs["claims_lookup"].keywords["with_ttl"])
        self.assertIs(kwargs["machine_records"].func, machines.machines)
        self.assertEqual(kwargs["machine_records"].args, (connection,))


def test_roadmap_json_reads_other_machines_and_claims_from_redis(redis_port, flush_redis, tmp_path):
    """A real Redis: the registry record of `ralpha` and its claims reach the
    JSON that `lupin roadmap --json` prints on `jesus`."""
    import time

    import redis as redis_lib

    connection = {
        "redis_host": "127.0.0.1", "redis_port": redis_port,
        "redis_username": None, "redis_password": None,
    }
    record = _record("ralpha", [("bodysmith", True)])
    record["heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:machine:ralpha", json.dumps(record))
    claims.claim(
        "acme/bodysmith#161", "omp-2026-10-08", ttl=6600,
        redis_host="127.0.0.1", redis_port=redis_port,
    )
    (tmp_path / "bodysmith").mkdir()
    open_issues = {"bodysmith": [_issue(161, "stretch plan", "P1")]}
    dag = {
        "repos": {"bodysmith": [{"number": 161, "blockedBy": [], "blocking": []}]},
        "cycles": [],
        "warnings": {},
    }

    with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
         mock.patch.object(
             roadmap, "cached_github",
             side_effect=lambda repo, path, state="open", **kw: (
                 (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
             ),
         ), \
         mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag):
        text, code = roadmap_cli.run(
            None, 10, "all", False, True, False,
            code_dir=str(tmp_path),
            enabled_repos=lambda: [],
            claims_lookup=functools.partial(claims.claims_for, with_ttl=True, **connection),
            machine_records=functools.partial(machines.machines, connection),
            local_host="jesus",
        )

    payload = json.loads(text)
    assert code == 0
    assert payload["warnings"] == []
    assert payload["repos"]["bodysmith"]["issues"][0]["claimedBy"] == "omp-2026-10-08"
    (entry,) = payload["claims"]
    assert entry["target"] == "acme/bodysmith#161"
    assert entry["holder"] == "omp-2026-10-08"
    assert entry["host"] == machines.hostname()
    assert 6590 <= entry["ttl_seconds"] <= 6600
    assert 0 <= entry["age_seconds"] <= 10


def _cli_roadmap(redis_port, capsys, code_dir, argv, cycles=()):
    """Run `lupin roadmap` with `argv` against the Redis on `redis_port`.

    GitHub and the dependency DAG are faked. `cycles` is the DAG's cycle
    list. Checkouts are read from `code_dir`. Returns (exit code, stdout).
    """
    connection = {
        "redis_host": "127.0.0.1", "redis_port": redis_port,
        "redis_username": None, "redis_password": None,
    }
    dag = {
        "repos": {"bodysmith": [{"number": 161, "blockedBy": [], "blocking": []}]},
        "cycles": list(cycles),
        "warnings": {},
    }
    with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
         mock.patch.object(
             roadmap, "cached_github",
             side_effect=lambda repo, path, state="open", **kw: (
                 ([_issue(161, "stretch plan", "P1")], {}, []) if state == "open" else ([], {}, [])
             ),
         ), \
         mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag), \
         mock.patch.dict(os.environ, {"LUPIN_LOOP_CODE_DIR": code_dir}), \
         mock.patch.object(machines, "resolve_connection", return_value=connection), \
         mock.patch("lupin.serve.enabled_repos", return_value=[]):
        code = cli.main(argv)
    return code, capsys.readouterr().out


def _cli_roadmap_json(redis_port, capsys, code_dir):
    code, out = _cli_roadmap(redis_port, capsys, code_dir, ["roadmap", "--json"])
    return code, json.loads(out)


def _seed_good_box(redis_port):
    """Write a fresh registry record for `good-box`, which enables `bodysmith`."""
    import redis as redis_lib

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    record = _record("good-box", [("bodysmith", True)])
    record["heartbeat"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw.set("lupin:v1:machine:good-box", json.dumps(record))
    return raw


def test_roadmap_json_skips_unreadable_redis_records_and_counts_them(
    redis_port, flush_redis, tmp_path, capsys
):
    raw = _seed_good_box(redis_port)
    raw.set("lupin:v1:machine:old-box", "not json")
    raw.set("lupin:v1:machine:ralpha", json.dumps({"name": "ralpha", "state": "online"}))
    raw.set("lupin:v1:claim:acme/bodysmith#9", "not json")
    (tmp_path / "bodysmith").mkdir()

    code, payload = _cli_roadmap_json(redis_port, capsys, str(tmp_path))

    assert code == 0
    assert list(payload["repos"]) == ["bodysmith"]
    assert payload["warnings"] == [
        "skipped 3 unreadable Redis record(s): claim:acme/bodysmith#9, machine:old-box, machine:ralpha"
    ]


def test_roadmap_json_reads_checkouts_under_the_loop_code_dir(
    redis_port, flush_redis, tmp_path, capsys
):
    _seed_good_box(redis_port)
    (tmp_path / "bodysmith").mkdir()

    code, payload = _cli_roadmap_json(redis_port, capsys, str(tmp_path))

    assert code == 0
    assert payload["warnings"] == []
    assert payload["repos"]["bodysmith"]["issues"][0]["number"] == 161


def test_roadmap_dag_on_a_cycle_still_warns_about_skipped_records(
    redis_port, flush_redis, tmp_path, capsys
):
    raw = _seed_good_box(redis_port)
    raw.set("lupin:v1:machine:old-box", "not json")
    (tmp_path / "bodysmith").mkdir()
    cycle = [[
        {"repo": "bodysmith", "number": 161},
        {"repo": "bodysmith", "number": 162},
        {"repo": "bodysmith", "number": 161},
    ]]

    code, text = _cli_roadmap(redis_port, capsys, str(tmp_path), ["roadmap", "--dag"], cycles=cycle)

    assert code == 1
    assert text.startswith("error: dependency cycle #161 -> #162 -> #161.")
    assert "warning: skipped 1 unreadable Redis record(s): machine:old-box" in text.splitlines()

    code, out = _cli_roadmap(
        redis_port, capsys, str(tmp_path), ["roadmap", "--dag", "--json"], cycles=cycle
    )
    payload = json.loads(out)

    assert code == 1
    assert payload["cycles"] == cycle
    assert payload["warnings"] == ["skipped 1 unreadable Redis record(s): machine:old-box"]


def test_default_claim_lookup_raises_on_a_corrupt_claim(redis_port, flush_redis, tmp_path):
    """Without a `claims_lookup`, `run` reads claims strictly.
    A corrupt claim raises. It is never skipped without a warning.
    """
    import redis as redis_lib

    raw = redis_lib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    raw.set("lupin:v1:claim:acme/bodysmith#161", "[]")
    (tmp_path / "bodysmith").mkdir()
    open_issues = {"bodysmith": [_issue(161, "stretch plan", "P1")]}
    dag = {
        "repos": {"bodysmith": [{"number": 161, "blockedBy": [], "blocking": []}]},
        "cycles": [],
        "warnings": {},
    }
    # The default lookup has no connection argument. Point it at the test server.
    with mock.patch.object(roadmap, "_repo_identity", side_effect=_identity()), \
         mock.patch.object(
             roadmap, "cached_github",
             side_effect=lambda repo, path, state="open", **kw: (
                 (open_issues.get(repo, []), {}, []) if state == "open" else ([], {}, [])
             ),
         ), \
         mock.patch.object(roadmap, "cached_dependency_dag", side_effect=lambda repos, code_dir: dag), \
         mock.patch.object(claims, "_client", return_value=raw), \
         pytest.raises(ValueError, match="not a JSON object"):
        roadmap_cli.run("bodysmith", 10, "ready", False, False, False, code_dir=str(tmp_path))

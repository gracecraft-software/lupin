"""Tests for `quota.py` -- the quota/usage readers moved out of `serve.py`
(issue #8). `serve.py`'s rendering of this data is tested in
`test_serve.py`'s `QuotaRenderingTests`; these tests cover the reading
layer only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from lupin import pace, quota


class UsageReaderFixtures(unittest.TestCase):
    """Shared setup for the two local 7-day usage readers."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.claude_path = os.path.join(self.tempdir.name, "stats-cache.json")
        self.omp_path = os.path.join(self.tempdir.name, "stats.db")

    def write_claude(self, data):
        with open(self.claude_path, "w", encoding="utf-8") as stats_file:
            json.dump(data, stats_file)

    def write_omp(self, rows):
        with closing(sqlite3.connect(self.omp_path)) as db:
            db.execute(
                "CREATE TABLE messages (provider TEXT, model TEXT, timestamp INTEGER, "
                "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
                "cache_write_tokens INTEGER, cost_total REAL)"
            )
            db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            db.commit()


class ClaudeUsageTests(UsageReaderFixtures):
    def test_totals_cover_seven_calendar_days(self):
        # dailyModelTokens holds one flat token total per model per day --
        # confirmed against the real ~/.claude/stats-cache.json on this box,
        # not a nested input/output/cost breakdown. No daily cost exists at
        # all; that only exists as an all-time total under modelUsage.
        today = date.today()
        self.write_claude({
            "lastComputedDate": today.isoformat(),
            "dailyModelTokens": [
                {
                    "date": (today - timedelta(days=6)).isoformat(),
                    "tokensByModel": {"model-a": 100, "model-b": 30},
                },
                {
                    "date": (today - timedelta(days=7)).isoformat(),
                    "tokensByModel": {"old": 900},
                },
            ],
        })
        with mock.patch.object(quota, "CLAUDE_STATS_FILE", self.claude_path):
            rows = quota.claude_usage()

        self.assertEqual(rows, [{
            "provider": "claude",
            "input_tokens": 130,
            "output_tokens": None,
            "cost": None,
            "period": "last 7 days",
            "source": quota.CLAUDE_STATS_SOURCE,
            "last_update": today.isoformat(),
        }])

    def test_missing_file_is_unavailable(self):
        with mock.patch.object(quota, "CLAUDE_STATS_FILE", self.claude_path + ".missing"):
            rows = quota.claude_usage()
        self.assertEqual(rows[0]["provider"], "claude")
        self.assertEqual(rows[0]["error"], quota.NOT_AVAILABLE)
        self.assertNotIn("FileNotFoundError", rows[0]["error"])
        self.assertNotIn("/", rows[0]["source"])

    def test_bad_json_is_unavailable(self):
        with open(self.claude_path, "w", encoding="utf-8") as stats_file:
            stats_file.write("{")
        with mock.patch.object(quota, "CLAUDE_STATS_FILE", self.claude_path):
            rows = quota.claude_usage()
        self.assertEqual(rows[0]["error"], quota.NOT_AVAILABLE)
        self.assertNotIn("JSONDecodeError", rows[0]["error"])


class OmpUsageTests(UsageReaderFixtures):
    def test_window_totals_and_last_update(self):
        now = 1_800_000_000
        self.write_omp([
            ("openai-codex", "m", int((now - 3600) * 1000), 12, 8, 0, 0, 0.4),
            ("openai-codex", "m", int((now - 7200) * 1000), 3, 2, 0, 0, 0.1),
            ("opencode-go", "m", int((now - 9 * 86400) * 1000), 700, 800, 0, 0, 5.0),
        ])
        with (
            mock.patch.object(quota, "OMP_STATS_FILE", self.omp_path),
            mock.patch.object(quota.time, "time", return_value=now),
        ):
            rows = quota.omp_usage()

        by_provider = {row["provider"]: row for row in rows}
        self.assertEqual(
            (by_provider["openai-codex"]["input_tokens"], by_provider["openai-codex"]["output_tokens"],
             by_provider["openai-codex"]["cost"]),
            (15, 10, 0.5),
        )
        self.assertEqual(
            (by_provider["opencode-go"]["input_tokens"], by_provider["opencode-go"]["output_tokens"],
             by_provider["opencode-go"]["cost"]),
            (0, 0, 0),
        )
        self.assertEqual(
            by_provider["opencode-go"]["last_update"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 9 * 86400)),
        )

    def test_missing_file_is_unavailable(self):
        with mock.patch.object(quota, "OMP_STATS_FILE", self.omp_path + ".missing"):
            rows = quota.omp_usage()
        self.assertEqual(rows[0]["error"], quota.NOT_AVAILABLE)
        self.assertNotIn("OperationalError", rows[0]["error"])

    def test_empty_db_is_unavailable(self):
        with closing(sqlite3.connect(self.omp_path)):
            pass
        with mock.patch.object(quota, "OMP_STATS_FILE", self.omp_path):
            rows = quota.omp_usage()
        self.assertEqual(rows[0]["error"], quota.NOT_AVAILABLE)
        self.assertNotIn("OperationalError", rows[0]["error"])


class QuotaUsageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # Default to unavailable local tools; quota API adapters are mocked
        # so tests do not read credentials or contact providers.
        run_patcher = mock.patch.object(quota, "run", return_value=(127, "not found: omp"))
        run_patcher.start()
        self.addCleanup(run_patcher.stop)
        fallbacks = {
            "claude_oauth_quota": "claude",
            "opencode_go_quota": "opencode-go",
        }
        for name, provider in fallbacks.items():
            patcher = mock.patch.object(
                quota,
                name,
                return_value=[{"provider": provider, "note": "quota unavailable"}],
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_omits_ollama_and_keeps_opencode_limits(self):
        # Real-shaped omp usage --json: one provider with limits, one with
        # only notes (no standalone quota API) -- confirmed against the
        # live CLI on this box before writing the parser.
        reset_at_ms = 1_790_547_474_348
        generated_at_ms = 1_790_529_505_926
        report = {
            "generatedAt": generated_at_ms,
            "reports": [
                {
                    "provider": "opencode-go",
                    "limits": [
                        {
                            "id": "monthly",
                            "label": "Monthly limit",
                            "window": {"id": "monthly", "resetsAt": reset_at_ms},
                            "amount": {"used": 64, "usedFraction": 0.64},
                        }
                    ],
                },
                {
                    "provider": "ollama",
                    "limits": [],
                    "notes": ["Ollama does not expose a standalone quota usage API."],
                },
            ],
        }
        with mock.patch.object(quota, "run", return_value=(0, json.dumps(report))):
            rows = quota.quota_usage()

        self.assertFalse(any(row["provider"] == "ollama" for row in rows))
        opencode_row = next(row for row in rows if row["provider"] == "opencode-go" and "used_pct" in row)
        self.assertEqual(opencode_row["duration"], quota.QuotaDuration.MONTHLY)
        self.assertEqual(opencode_row["used_pct"], 64.0)
        self.assertEqual(opencode_row["resets_at"], reset_at_ms)


    def test_claude_oauth_fallback_does_not_duplicate_omp_windows(self):
        report = {
            "reports": [{
                "provider": "claude",
                "limits": [
                    {
                        "id": "five_hour",
                        "label": "5-hour window",
                        "window": {"durationMs": 18_000_000},
                        "amount": {"usedFraction": 0.2},
                    },
                    {
                        "id": "seven_day",
                        "label": "Weekly window",
                        "window": {"durationMs": 604_800_000},
                        "amount": {"usedFraction": 0.4},
                    },
                ],
            }],
        }
        oauth_rows = [
            {
                "provider": "claude",
                "duration": quota.QuotaDuration.FIVE_HOURS,
                "label": "5 hours",
                "used_pct": 30,
            },
            {
                "provider": "claude",
                "duration": quota.QuotaDuration.WEEKLY,
                "label": "7 days",
                "used_pct": 50,
            },
        ]
        with (
            mock.patch.object(quota, "run", return_value=(0, json.dumps(report))),
            mock.patch.object(quota, "claude_oauth_quota", return_value=oauth_rows),
        ):
            rows = [row for row in quota.quota_usage() if row["provider"] == "claude"]

        self.assertEqual(
            [(row["duration"], row["label"], row["used_pct"]) for row in rows],
            [
                (quota.QuotaDuration.FIVE_HOURS, "5-hour window", 20.0),
                (quota.QuotaDuration.WEEKLY, "Weekly window", 40.0),
            ],
        )


    def test_uses_latest_codex_snapshot_when_omp_fails(self):
        sessions = os.path.join(self.tempdir.name, "sessions")
        session_dir = os.path.join(sessions, "2026", "10", "03")
        os.makedirs(session_dir)
        rollout = os.path.join(session_dir, "rollout-test.jsonl")
        with open(rollout, "w", encoding="utf-8") as file:
            file.write(json.dumps({
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {"used_percent": 42, "resets_at": 1_790_547_474},
                        "secondary": {"used_percent": 75, "resets_at": 1_791_000_000},
                    },
                }
            }) + "\n")
        newer_rollout = os.path.join(session_dir, "rollout-newer.jsonl")
        with open(newer_rollout, "w", encoding="utf-8") as file:
            file.write(json.dumps({"payload": {"type": "session_meta"}}) + "\n")
        newer_mtime = os.path.getmtime(rollout) + 1
        os.utime(newer_rollout, (newer_mtime, newer_mtime))

        with (
            mock.patch.object(quota, "run", return_value=(1, "omp failed")),
            mock.patch.object(quota, "CODEX_SESSIONS_DIR", sessions),
        ):
            rows = quota.quota_usage()

        codex_rows = [row for row in rows if row["provider"] == "openai"]
        self.assertEqual(
            [(row["duration"], row["used_pct"]) for row in codex_rows],
            [
                (quota.QuotaDuration.FIVE_HOURS, 42),
                (quota.QuotaDuration.WEEKLY, 75),
            ],
        )
        self.assertEqual(codex_rows[0]["resets_at"], 1_790_547_474_000)

    def test_maps_omp_codex_provider_to_openai(self):
        report = {
            "generatedAt": 1_790_529_505_926,
            "reports": [{
                "provider": "openai-codex",
                "limits": [{
                    "label": "5 hours",
                    "window": {"resetsAt": 1_790_547_474_348},
                    "amount": {"usedFraction": 0.42},
                }],
            }],
        }
        with mock.patch.object(quota, "run", return_value=(0, json.dumps(report))):
            rows = quota.quota_usage()

        self.assertEqual(rows[0]["duration"], quota.QuotaDuration.FIVE_HOURS)
        self.assertEqual(rows[0]["resets_at"], 1_790_547_474_348)

    @staticmethod
    def _codex_report(plan, account_id, limits, email="someone@example.com"):
        return {
            "provider": "openai-codex",
            "limits": [
                {
                    "label": label,
                    "window": {"durationMs": duration_ms, "resetsAt": 1_790_547_474_348},
                    "amount": {"usedFraction": used},
                }
                for label, duration_ms, used in limits
            ],
            "metadata": {"planType": plan, "accountId": account_id, "email": email},
        }

    def _omp_rows(self, *reports):
        report = {"generatedAt": 1_790_529_505_926, "reports": list(reports)}
        with mock.patch.object(quota, "run", return_value=(0, json.dumps(report))):
            return [row for row in quota.quota_usage() if row["provider"] == "openai"]

    def test_labels_each_login_when_a_provider_has_two(self):
        rows = self._omp_rows(
            self._codex_report("free", "id-free", [("30 days", 2_592_000_000, 1.0)]),
            self._codex_report("plus", "id-plus", [
                ("5 hours", 18_000_000, 0.01),
                ("7 days", 604_800_000, 0.0),
            ]),
        )

        self.assertEqual(
            [(row["account"], row["duration"], row["used_pct"]) for row in rows],
            [
                ("free", quota.QuotaDuration.MONTHLY, 100.0),
                ("plus", quota.QuotaDuration.FIVE_HOURS, 1.0),
                ("plus", quota.QuotaDuration.WEEKLY, 0.0),
            ],
        )
        # The label is the plan name only. No email or account ID leaves quota.py.
        self.assertNotIn("someone@example.com", json.dumps(rows, default=str))
        self.assertNotIn("id-plus", json.dumps(rows, default=str))

    def test_one_login_has_no_account_label(self):
        rows = self._omp_rows(
            self._codex_report("plus", "id-plus", [("5 hours", 18_000_000, 0.4)]),
        )
        self.assertEqual(len(rows), 1)
        self.assertNotIn("account", rows[0])

    def test_non_list_reset_credits_keeps_available_count(self):
        report = {
            "provider": "openai-codex",
            "resetCredits": {"availableCount": 42, "credits": 42},
            "limits": [{
                "label": "5 hours",
                "window": {"durationMs": 18_000_000, "resetsAt": 1_790_547_474_348},
                "amount": {"usedFraction": 0.4},
            }],
        }

        rows = self._omp_rows(report)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reset_credits"], {"available": 42, "expires_at": []})


    def test_two_logins_on_one_plan_get_different_labels(self):
        reports = (
            self._codex_report("plus", "id-one", [("5 hours", 18_000_000, 0.1)]),
            self._codex_report("plus", "id-two", [("5 hours", 18_000_000, 0.2)]),
        )
        first, second = (row["account"] for row in self._omp_rows(*reports))

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("plus-") and second.startswith("plus-"))
        # Same logins, same labels on the next read.
        self.assertEqual(first, self._omp_rows(*reports)[0]["account"])

    def test_two_users_in_one_workspace_get_different_labels(self):
        # Issue #169, case 1: two users share one workspace `accountId`.
        reports = (
            self._codex_report("plus", "shared-ws", [("5 hours", 18_000_000, 1.0)], email="a@example.com"),
            self._codex_report("plus", "shared-ws", [("5 hours", 18_000_000, 0.01)], email="b@example.com"),
        )
        rows = self._omp_rows(*reports)

        self.assertNotEqual(rows[0]["account"], rows[1]["account"])
        self.assertFalse(pace.blocked(rows, "openai", now_ms=0))
        text = json.dumps(rows, default=str)
        for secret in ("a@example.com", "b@example.com", "shared-ws"):
            self.assertNotIn(secret, text)

    def test_two_logins_with_no_metadata_still_get_different_labels(self):
        bare = {
            "provider": "openai-codex",
            "limits": [{
                "label": "5 hours",
                "window": {"durationMs": 18_000_000, "resetsAt": 1_790_547_474_348},
                "amount": {"usedFraction": 0.3},
            }],
        }
        rows = self._omp_rows(bare, bare)
        self.assertEqual(len({row["account"] for row in rows}), 2)

    def test_a_login_with_a_note_and_no_limits_keeps_its_label(self):
        rows = self._omp_rows(
            self._codex_report("plus", "id-plus", [("5 hours", 18_000_000, 0.1)]),
            {
                "provider": "openai-codex",
                "limits": [],
                "notes": ["no usage"],
                "metadata": {"planType": "free", "accountId": "id-free"},
            },
        )
        note = next(row for row in rows if "note" in row)
        self.assertEqual((note["account"], note["note"]), ("free", "no usage"))

    def test_unavailable_on_nonzero_exit_or_bad_json(self):
        with mock.patch.object(quota, "run", return_value=(1, "boom")):
            rows = quota.quota_usage()
        omp_row = next(row for row in rows if row["provider"] == "omp")
        self.assertEqual(omp_row["error"], quota.NOT_AVAILABLE)
        self.assertNotIn("RuntimeError", omp_row["error"])

        with mock.patch.object(quota, "run", return_value=(0, "not json")):
            rows = quota.quota_usage()
        omp_row = next(row for row in rows if row["provider"] == "omp")
        self.assertEqual(omp_row["error"], quota.NOT_AVAILABLE)
        self.assertNotIn("JSONDecodeError", omp_row["error"])

    def test_claude_fallback_always_present(self):
        rows = quota.quota_usage()
        self.assertTrue(any(row["provider"] == "claude" for row in rows))

    def test_reset_credits_ride_on_their_login_rows(self):
        # Shape from `omp usage --json` on 2026-10-21: the plus login has two
        # available reset credits. The free login has none.
        def limit(used_fraction):
            return {
                "id": "primary",
                "label": "5 hours",
                "window": {"id": "5h", "label": "5 hours", "durationMs": 18_000_000, "resetsAt": 1_790_547_474_348},
                "amount": {"usedFraction": used_fraction},
            }

        report = {
            "generatedAt": 1_790_529_505_926,
            "reports": [
                {
                    "provider": "openai-codex",
                    "metadata": {"planType": "plus"},
                    "limits": [limit(0.0)],
                    "resetCredits": {
                        "availableCount": 2,
                        "credits": [
                            {"grantedAt": "2026-09-29T18:36:49.931644Z", "expiresAt": "2026-10-29T18:36:49.931644Z", "status": "available"},
                            {"grantedAt": "2026-10-07T22:59:56.486921Z", "expiresAt": "2026-11-06T22:59:56.486921Z", "status": "available"},
                        ],
                    },
                },
                {
                    "provider": "openai-codex",
                    "metadata": {"planType": "free"},
                    "limits": [limit(1.0)],
                    "resetCredits": {"availableCount": 0},
                },
            ],
        }
        with mock.patch.object(quota, "run", return_value=(0, json.dumps(report))):
            rows = quota.quota_usage()

        plus = next(row for row in rows if row.get("provider") == "openai" and row["account"].startswith("plus"))
        free = next(row for row in rows if row.get("provider") == "openai" and row["account"].startswith("free"))
        self.assertEqual(
            plus["reset_credits"],
            {
                "available": 2,
                "expires_at": [
                    int(datetime(2026, 10, 29, 18, 36, 49, 931644, tzinfo=timezone.utc).timestamp() * 1000),
                    int(datetime(2026, 11, 6, 22, 59, 56, 486921, tzinfo=timezone.utc).timestamp() * 1000),
                ],
            },
        )
        self.assertEqual(free["reset_credits"], {"available": 0, "expires_at": []})


class OpencodeGoQuotaTests(UsageReaderFixtures):
    def test_fallback_reads_usage_api(self):
        payload = {
            "usage": {
                "rolling": {"percent": 12, "resetsAt": "2030-01-01T00:00:00Z"},
                "weekly": {"percent": 34, "resetsAt": "2030-01-02T00:00:00Z"},
                "monthly": {"percent": 56, "resetsAt": "2030-01-03T00:00:00Z"},
            }
        }
        with (
            mock.patch.dict(os.environ, {"OPENCODE_API_KEY": ""}),
            mock.patch.object(quota, "OPENCODE_GO_AUTH_FILE", self.omp_path),
            mock.patch("builtins.open", mock.mock_open(read_data=json.dumps({
                "opencode-go": {"type": "api", "key": "test-key"}
            }))),
            mock.patch.object(
                quota.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            rows = quota.opencode_go_quota()

        self.assertEqual(
            [row["duration"] for row in rows],
            [
                quota.QuotaDuration.FIVE_HOURS,
                quota.QuotaDuration.WEEKLY,
                quota.QuotaDuration.MONTHLY,
            ],
        )
        self.assertTrue(all(isinstance(row["resets_at"], int) for row in rows))
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-key"
        )


# `conftest.py` replaces `_omp_token` for each test. `getattr` lets this file load
# on old source that has no `_omp_token`.
REAL_OMP_TOKEN = getattr(quota, "_omp_token", None)
SECRET_KEY = "sk-test-SECRET-0123456789"
EXPIRED_NOTE = "OpenCode Go API key expired -- get a new key in the OpenCode Console"
USAGE_PAYLOAD = {"usage": {"rolling": {"percent": 12, "resetsAt": "2030-01-01T00:00:00Z"}}}


def _http_error(code: int, body: str) -> quota.urllib.error.HTTPError:
    return quota.urllib.error.HTTPError(
        quota.OPENCODE_GO_USAGE_URL, code, "error", {}, io.BytesIO(body.encode())
    )


class OpencodeGoKeyTests(unittest.TestCase):
    """Where `opencode_go_quota()` finds its key, and what it says when the
    key fails. The HTTP call and `omp` are always mocked: these tests never
    contact opencode.ai and never run the real `omp token`.
    """

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.auth_path = os.path.join(tempdir.name, "auth.json")  # absent by default
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(quota, "OPENCODE_GO_AUTH_FILE", self.auth_path).start()
        mock.patch.dict(os.environ, {"OPENCODE_API_KEY": ""}).start()
        mock.patch.object(quota, "_omp_token", REAL_OMP_TOKEN, create=True).start()
        # `omp` is missing unless a test says otherwise.
        self.omp = mock.patch.object(
            quota.subprocess, "run", side_effect=FileNotFoundError("omp")
        ).start()
        self.fetch = mock.patch.object(
            quota.urllib.request,
            "urlopen",
            side_effect=lambda *args, **kwargs: io.BytesIO(json.dumps(USAGE_PAYLOAD).encode()),
        ).start()

    def write_auth_file(self, key):
        with open(self.auth_path, "w", encoding="utf-8") as auth_file:
            json.dump({"opencode-go": {"type": "api", "key": key}}, auth_file)

    def sent_key(self):
        header = self.fetch.call_args.args[0].get_header("Authorization")
        return header.removeprefix("Bearer ")

    def omp_prints(self, stdout, returncode=0, stderr=""):
        self.omp.side_effect = None
        self.omp.return_value = subprocess.CompletedProcess(
            ["omp", "token", "opencode-go"], returncode, stdout, stderr
        )

    def assert_no_key_note(self, rows):
        self.assertEqual(rows, [{"provider": "opencode-go", "note": quota.NO_OPENCODE_GO_KEY}])
        self.fetch.assert_not_called()

    def test_key_from_auth_file_wins_and_omp_is_not_run(self):
        self.write_auth_file(SECRET_KEY)
        with mock.patch.dict(os.environ, {"OPENCODE_API_KEY": "env-key"}):
            rows = quota.opencode_go_quota()
        self.assertEqual(self.sent_key(), SECRET_KEY)
        self.assertEqual(rows[0]["used_pct"], 12)
        self.omp.assert_not_called()

    def test_key_from_env_when_no_auth_file(self):
        with mock.patch.dict(os.environ, {"OPENCODE_API_KEY": f" {SECRET_KEY} "}):
            rows = quota.opencode_go_quota()
        self.assertEqual(self.sent_key(), SECRET_KEY)
        self.assertEqual(rows[0]["used_pct"], 12)
        self.omp.assert_not_called()

    def test_key_only_from_omp_token(self):
        self.omp_prints(SECRET_KEY + "\n")
        rows = quota.opencode_go_quota()
        self.assertEqual(self.sent_key(), SECRET_KEY)
        self.assertEqual(rows[0]["used_pct"], 12)
        self.assertEqual(self.omp.call_args.args[0], ["omp", "token", "opencode-go"])
        self.assertEqual(self.omp.call_args.kwargs["timeout"], 10)
        self.assertFalse(self.omp.call_args.kwargs.get("shell", False))

    def test_omp_stderr_is_not_part_of_the_key(self):
        self.omp_prints(SECRET_KEY + "\n", stderr="warning: update available\n")
        quota.opencode_go_quota()
        self.assertEqual(self.sent_key(), SECRET_KEY)

    def test_omp_missing_gives_the_no_key_note(self):
        self.assert_no_key_note(quota.opencode_go_quota())

    def test_omp_timeout_gives_the_no_key_note_without_the_key(self):
        self.omp.side_effect = subprocess.TimeoutExpired(
            ["omp", "token", "opencode-go"], 10, output=SECRET_KEY.encode()
        )
        rows = quota.opencode_go_quota()
        self.assert_no_key_note(rows)
        self.assertNotIn(SECRET_KEY, repr(rows))

    def test_omp_nonzero_exit_gives_the_no_key_note(self):
        # Even if it printed something, a failed command gives no key.
        self.omp_prints(SECRET_KEY, returncode=1, stderr="not logged in")
        self.assert_no_key_note(quota.opencode_go_quota())

    def test_omp_prints_nothing_gives_the_no_key_note(self):
        for label, stdout in (("empty", ""), ("blank", "  \n")):
            with self.subTest(label):
                self.omp_prints(stdout)
                self.assert_no_key_note(quota.opencode_go_quota())

    def test_omp_output_that_is_not_one_word_is_not_used_as_a_key(self):
        self.omp_prints("error: no such provider\n")
        self.assert_no_key_note(quota.opencode_go_quota())

    def test_expired_key_gives_a_note_and_the_key_stays_secret(self):
        self.omp_prints(SECRET_KEY)
        body = json.dumps({"error": {"code": "api_key_expired", "message": "API key expired"}})
        self.fetch.side_effect = _http_error(401, body)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rows = quota.opencode_go_quota()
        self.assertEqual(rows, [{"provider": "opencode-go", "note": EXPIRED_NOTE}])
        for text in (out.getvalue(), err.getvalue(), repr(rows)):
            self.assertNotIn(SECRET_KEY, text)

    def test_other_http_errors_keep_the_unavailable_note(self):
        self.write_auth_file(SECRET_KEY)
        cases = [
            ("401 other code", 401, '{"error": {"code": "invalid_api_key"}}'),
            ("401 no body", 401, ""),
            ("403", 403, '{"error": {"code": "api_key_expired"}}'),
            ("500", 500, "oops"),
        ]
        for label, code, body in cases:
            with self.subTest(label):
                self.fetch.side_effect = _http_error(code, body)
                rows = quota.opencode_go_quota()
                self.assertEqual(
                    rows, [{"provider": "opencode-go", "note": quota.QUOTA_NOT_AVAILABLE}]
                )
                self.assertNotIn(SECRET_KEY, repr(rows))

    def test_network_error_keeps_the_unavailable_note(self):
        self.write_auth_file(SECRET_KEY)
        self.fetch.side_effect = quota.urllib.error.URLError("no route")
        self.assertEqual(
            quota.opencode_go_quota(),
            [{"provider": "opencode-go", "note": quota.QUOTA_NOT_AVAILABLE}],
        )


class OmpTokenOutputTests(unittest.TestCase):
    """`_omp_token` with a fake `omp` program on PATH. No real `omp` runs."""

    @unittest.skipIf(REAL_OMP_TOKEN is None, "old source has no _omp_token")
    def test_output_that_is_not_utf8_gives_no_key(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            fake = os.path.join(bin_dir, "omp")
            with open(fake, "w", encoding="utf-8") as script:
                script.write("#!/bin/sh\nprintf '\\377\\376abc'\n")
            os.chmod(fake, 0o700)
            with mock.patch.dict(os.environ, {"PATH": bin_dir}):
                self.assertEqual(REAL_OMP_TOKEN("opencode-go"), "")


class ClaudeOauthQuotaTests(unittest.TestCase):
    def test_fallback_reads_live_usage(self):
        payload = {
            "five_hour": {"utilization": 17, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"utilization": 61, "resets_at": 1_893_456_000},
        }
        with (
            mock.patch(
                "builtins.open",
                mock.mock_open(read_data=json.dumps({
                    "claudeAiOauth": {"accessToken": "test-token"}
                })),
            ),
            mock.patch.object(
                quota.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as fetch,
        ):
            rows = quota.claude_oauth_quota()

        self.assertEqual(
            [row["duration"] for row in rows],
            [quota.QuotaDuration.FIVE_HOURS, quota.QuotaDuration.WEEKLY],
        )
        self.assertEqual(
            [row["label"] for row in rows], ["5 hours", "7 days"]
        )
        self.assertTrue(all(isinstance(row["resets_at"], int) for row in rows))
        self.assertEqual(
            fetch.call_args.args[0].get_header("Authorization"), "Bearer test-token"
        )

    def test_missing_file_or_token_is_a_no_key_note(self):
        cases = [
            ("missing file", mock.patch("builtins.open", side_effect=FileNotFoundError())),
            ("no token field", mock.patch(
                "builtins.open",
                mock.mock_open(read_data=json.dumps({"claudeAiOauth": {}})),
            )),
            ("empty token", mock.patch(
                "builtins.open",
                mock.mock_open(read_data=json.dumps({"claudeAiOauth": {"accessToken": ""}})),
            )),
        ]
        for label, patch in cases:
            with self.subTest(label), patch:
                rows = quota.claude_oauth_quota()
                self.assertEqual(
                    rows, [{"provider": "claude", "note": quota.NO_CLAUDE_CREDENTIALS}]
                )

    def test_malformed_credentials_do_not_break_quota_fallback(self):
        with (
            mock.patch("builtins.open", mock.mock_open(read_data="{")),
            mock.patch.object(quota, "run", return_value=(0, json.dumps({"reports": []}))),
            mock.patch.object(
                quota,
                "claude_oauth_quota",
                return_value=[{"provider": "claude", "note": quota.QUOTA_NOT_AVAILABLE}],
            ),
        ):
            rows = quota.quota_usage()

        self.assertIn({"provider": "claude", "note": quota.QUOTA_NOT_AVAILABLE}, rows)
        self.assertTrue(all("_metadata" not in row for row in rows))



class SnapshotTests(unittest.TestCase):
    """`snapshot()` -- the one normalized entry point `machines.py`'s
    heartbeat calls to fill in the `quota` field.
    """

    def test_picks_the_five_hour_row_per_provider(self):
        rows = [
            {"provider": "claude", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 10, "resets_at": 1},
            {"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 40, "resets_at": 2},
            {"provider": "openai", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 25, "resets_at": 3},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()

        self.assertEqual(result["claude"], {
            "pct_left": 60,
            "resets_at": 2,
            "source": quota.quota_source_label("claude"),
            "windows": [
                {"label": None, "pct_left": 90, "resets_at": 1},
                {"label": None, "pct_left": 60, "resets_at": 2},
            ],
        })
        self.assertEqual(result["openai"], {
            "pct_left": 75,
            "resets_at": 3,
            "source": quota.quota_source_label("openai"),
            "windows": [{"label": None, "pct_left": 75, "resets_at": 3}],
        })

    def test_picks_the_account_with_the_most_quota_left(self):
        # A login that is used up must not hide a login with room left, even
        # when the used-up login's 5-hour row comes last.
        rows = [
            {"provider": "openai", "account": "plus", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 0, "resets_at": 8},
            {"provider": "openai", "account": "plus", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 1, "resets_at": 7},
            {"provider": "openai", "account": "free", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 100, "resets_at": 9},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["openai"]["pct_left"], 99)
        self.assertEqual(result["openai"]["resets_at"], 7)

    def test_ranks_logins_by_their_most_used_window(self):
        # Issue #207: login "a" has room on 5 hours but almost none on 7 days.
        # Login "b" has the same room on both windows. `pace.headroom` picks b,
        # so the summary must pick b too.
        later = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000
        rows = [
            {"provider": "openai", "account": "a", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 1, "resets_at": later},
            {"provider": "openai", "account": "a", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 90, "resets_at": later},
            {"provider": "openai", "account": "b", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 50, "resets_at": later},
            {"provider": "openai", "account": "b", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 50, "resets_at": later},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()

        self.assertEqual(result["openai"]["pct_left"], 50)
        self.assertEqual(result["openai"]["pct_left"], pace.headroom(rows, "openai", later))
        self.assertEqual(
            result["openai"]["windows"],
            [
                {"label": None, "pct_left": 50, "resets_at": later},
                {"label": None, "pct_left": 50, "resets_at": later},
            ],
        )

    def test_oauth_rows_use_matching_omp_account_before_deduplication(self):
        reports = [
            {
                "provider": "claude",
                "metadata": {"planType": "pro", "email": "oauth@example.test", "accountId": "acct-oauth"},
                "limits": [
                    {"id": "five_hour", "amount": {"usedFraction": 0.2}, "window": {"durationMs": 18_000_000}},
                    {"id": "seven_day", "amount": {"usedFraction": 0.3}, "window": {"durationMs": 604_800_000}},
                ],
            },
            {
                "provider": "claude",
                "metadata": {"planType": "pro", "email": "other@example.test", "accountId": "acct-other"},
                "limits": [
                    {"id": "five_hour", "amount": {"usedFraction": 0.4}, "window": {"durationMs": 18_000_000}},
                    {"id": "seven_day", "amount": {"usedFraction": 0.5}, "window": {"durationMs": 604_800_000}},
                ],
            },
        ]
        oauth_rows = [
            {"provider": "claude", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 10},
            {"provider": "claude", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 15},
        ]
        oauth_identity = {"email": "oauth@example.test"}
        with (
            mock.patch.object(quota, "claude_oauth_quota", return_value=oauth_rows),
            mock.patch.object(quota, "_claude_identity", return_value=oauth_identity),
            mock.patch.object(quota, "run", return_value=(0, json.dumps({"reports": reports}))),
        ):
            rows = quota.quota_usage()
        self.assertTrue(all("_metadata" not in row for row in rows))

        labeled = [row for row in rows if "used_pct" in row]
        self.assertEqual(len(labeled), 4)
        self.assertEqual(
            [(row["account"], row["duration"]) for row in labeled],
            [
                ("pro-" + hashlib.sha256(b"oauth@example.test\0acct-oauth").hexdigest()[:6], quota.QuotaDuration.FIVE_HOURS),
                ("pro-" + hashlib.sha256(b"oauth@example.test\0acct-oauth").hexdigest()[:6], quota.QuotaDuration.WEEKLY),
                ("pro-" + hashlib.sha256(b"other@example.test\0acct-other").hexdigest()[:6], quota.QuotaDuration.FIVE_HOURS),
                ("pro-" + hashlib.sha256(b"other@example.test\0acct-other").hexdigest()[:6], quota.QuotaDuration.WEEKLY),
            ],
        )
        self.assertEqual([row["used_pct"] for row in labeled[:2]], [20, 30])
        self.assertTrue(all("_metadata" not in row for row in rows))
    def test_snapshot_includes_every_window_for_the_selected_account(self):
        rows = [
            {
                "provider": "claude",
                "account": "plus",
                "duration": quota.QuotaDuration.FIVE_HOURS,
                "label": "5 Hour limit",
                "used_pct": 2,
                "resets_at": 3,
            },
            {
                "provider": "claude",
                "account": "plus",
                "duration": quota.QuotaDuration.WEEKLY,
                "label": "Weekly limit",
                "used_pct": 91,
                "resets_at": None,
            },
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()

        self.assertEqual(result["claude"]["pct_left"], 98)
        self.assertEqual(
            result["claude"]["windows"],
            [
                {"label": "5 Hour limit", "pct_left": 98, "resets_at": 3},
                {"label": "Weekly limit", "pct_left": 9, "resets_at": None},
            ],
        )
        self.assertNotIn("account", result["claude"])

    def test_ignores_a_window_with_no_reset_when_ranking_logins(self):
        # Issue #207, review finding 1: login "a" has room on both windows.
        # Login "b" has more room on 5 hours, but its 7-day row has no
        # `resets_at`. `pace.headroom` drops that row as unknown; the summary
        # must drop it too, or it picks a different login than routing does.
        later = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000
        rows = [
            {"provider": "openai", "account": "a", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 30, "resets_at": later},
            {"provider": "openai", "account": "a", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 30, "resets_at": later},
            {"provider": "openai", "account": "b", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 20, "resets_at": later},
            {"provider": "openai", "account": "b", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 90, "resets_at": None},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()

        self.assertEqual(result["openai"]["pct_left"], 80)
        self.assertEqual(result["openai"]["pct_left"], pace.headroom(rows, "openai", later))

    def test_skips_a_login_that_is_blocked_on_another_window(self):
        # Issue #169, case 3: login "a" has room on 5 hours but none on 7 days.
        later = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000
        rows = [
            {"provider": "openai", "account": "a", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 1, "resets_at": later},
            {"provider": "openai", "account": "a", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 100, "resets_at": later},
            {"provider": "openai", "account": "b", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 50, "resets_at": later},
            {"provider": "openai", "account": "b", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 20, "resets_at": later},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["openai"]["pct_left"], 50)

    def test_one_blocked_login_still_reports_its_five_hour_row(self):
        # One login: the result is the same as before issue #169.
        later = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000
        rows = [
            {"provider": "openai", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 1, "resets_at": later},
            {"provider": "openai", "duration": quota.QuotaDuration.WEEKLY, "used_pct": 100, "resets_at": later},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["openai"]["pct_left"], 99)

    def test_an_account_with_data_beats_an_account_with_only_a_note(self):
        rows = [
            {"provider": "openai", "account": "free", "note": "no usage"},
            {"provider": "openai", "account": "plus", "duration": quota.QuotaDuration.FIVE_HOURS, "used_pct": 30, "resets_at": 7},
        ]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["openai"]["pct_left"], 70)

    def test_falls_back_to_first_row_when_no_five_hour_window(self):
        rows = [{"provider": "opencode-go", "duration": quota.QuotaDuration.MONTHLY, "used_pct": 20, "resets_at": 5}]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["opencode-go"]["pct_left"], 80)
        self.assertEqual(result["opencode-go"]["resets_at"], 5)

    def test_note_only_row_has_no_pct_left_but_still_has_source(self):
        rows = [{"provider": "claude", "note": "quota unavailable"}]
        with mock.patch.object(quota, "quota_usage", return_value=rows):
            result = quota.snapshot()
        self.assertEqual(result["claude"]["pct_left"], None)
        self.assertEqual(result["claude"]["resets_at"], None)
        self.assertEqual(result["claude"]["note"], "quota unavailable")
        self.assertEqual(result["claude"]["source"], quota.quota_source_label("claude"))

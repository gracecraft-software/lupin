"""Tests for fleet-wide quota pacing and weekly targets in `pace.py`."""

import os
import unittest
from unittest.mock import patch

from lupin import pace
from lupin.quota import QuotaDuration

HOUR_MS = 60 * 60 * 1000
MIN_MS = 60 * 1000
DAY_MS = 24 * HOUR_MS

_TARGET_ENV = patch.dict("os.environ")


def setUpModule():
    _TARGET_ENV.start()
    os.environ.pop(pace.WEEKLY_DAILY_TARGETS_SETTING, None)


def tearDownModule():
    _TARGET_ENV.stop()


def _row(provider, used_pct, resets_in_ms, duration=QuotaDuration.FIVE_HOURS, now_ms=0):
    return {
        "provider": provider,
        "duration": duration,
        "used_pct": used_pct,
        "resets_at": now_ms + resets_in_ms,
    }


class BlockedTests(unittest.TestCase):
    """Weekly targets grow with elapsed time; short windows use a 95% reserve.
    Other windows keep their three-hour grace; missing weekly resets block.
    """

    def test_under_95_percent_is_never_blocked(self):
        rows = [_row("claude", 94, 10 * HOUR_MS)]
        self.assertFalse(pace.blocked(rows, "claude", 0))

    def test_95_percent_with_just_over_3h_left_is_blocked(self):
        rows = [_row("claude", 95, 3 * HOUR_MS + MIN_MS)]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_95_percent_with_just_under_3h_left_is_not_blocked(self):
        rows = [_row("claude", 95, 3 * HOUR_MS - MIN_MS)]
        self.assertFalse(pace.blocked(rows, "claude", 0))

    def test_95_percent_with_exactly_3h_left_is_blocked(self):
        # Exact boundary falls on the conservative (blocked) side.
        rows = [_row("claude", 95, 3 * HOUR_MS)]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_100_percent_with_minutes_left_is_still_blocked(self):
        # The 3h exception only lifts the 95-100% reserve band -- it never
        # applies past 100%, which is always blocked.
        rows = [_row("claude", 100, MIN_MS)]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_100_percent_far_from_reset_is_blocked(self):
        rows = [_row("claude", 100, 10 * HOUR_MS)]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_any_blocked_window_blocks_the_whole_provider(self):
        rows = [
            _row("claude", 10, 10 * HOUR_MS, duration=QuotaDuration.FIVE_HOURS),
            _row("claude", 100, 10 * HOUR_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_rows_with_no_usable_data_are_skipped_not_treated_as_0_percent(self):
        rows = [{"provider": "claude", "note": "quota unavailable"}]
        self.assertFalse(pace.blocked(rows, "claude", 0))

    def test_other_providers_rows_do_not_affect_this_provider(self):
        rows = [_row("openai", 100, MIN_MS)]
        self.assertFalse(pace.blocked(rows, "claude", 0))

    def test_claude_weekly_usage_above_90_percent_is_blocked_at_week_end(self):
        rows = [_row("claude", 91, 0, QuotaDuration.WEEKLY)]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_claude_weekly_usage_at_90_percent_is_allowed_at_week_end(self):
        rows = [_row("claude", 90, 0, QuotaDuration.WEEKLY)]
        self.assertFalse(pace.blocked(rows, "claude", 0))

    def test_claude_weekly_usage_at_90_percent_is_blocked_before_week_end(self):
        rows = [_row("claude", 90, 2 * HOUR_MS, QuotaDuration.WEEKLY)]
        self.assertTrue(pace.blocked(rows, "claude", 0))

    def test_weekly_target_tracks_elapsed_time_and_allows_exact_boundary(self):
        cases = [
            ("start", 7 * DAY_MS, 0),
            ("middle", 3.5 * DAY_MS, 45),
            ("end", 0, 90),
        ]
        for label, resets_in_ms, target in cases:
            with self.subTest(label=label):
                self.assertFalse(pace.blocked(
                    [_row("claude", target, resets_in_ms, QuotaDuration.WEEKLY)],
                    "claude",
                    0,
                ))
                if target:
                    self.assertFalse(pace.blocked(
                        [_row("claude", target - 0.1, resets_in_ms, QuotaDuration.WEEKLY)],
                        "claude",
                        0,
                    ))
                self.assertTrue(pace.blocked(
                    [_row("claude", target + 0.1, resets_in_ms, QuotaDuration.WEEKLY)],
                    "claude",
                    0,
                ))

    def test_provider_daily_target_overrides_default_rate(self):
        setting = pace.WEEKLY_DAILY_TARGETS_SETTING
        with patch.dict("os.environ", {setting: '{"claude": 12, "openai": 20}'}):
            self.assertFalse(pace.blocked(
                [_row("openai", 70, 3.5 * DAY_MS, QuotaDuration.WEEKLY)],
                "openai",
                0,
            ))
            self.assertTrue(pace.blocked(
                [_row("openai", 71, 3.5 * DAY_MS, QuotaDuration.WEEKLY)],
                "openai",
                0,
            ))
            self.assertTrue(pace.blocked(
                [_row("claude", 43, 3.5 * DAY_MS, QuotaDuration.WEEKLY)],
                "claude",
                0,
            ))

    def test_weekly_row_without_reset_fails_closed(self):
        rows = [{
            "provider": "claude",
            "duration": QuotaDuration.WEEKLY,
            "used_pct": 0,
            "resets_at": None,
        }]
        self.assertTrue(pace.blocked(rows, "claude", 0))
        self.assertIsNone(pace.headroom(rows, "claude", 0))

    def test_invalid_daily_target_configuration_is_rejected(self):
        setting = pace.WEEKLY_DAILY_TARGETS_SETTING
        for value in ("[]", '{"claude": 101}', '{"claude": true}', "not-json"):
            with self.subTest(value=value), patch.dict("os.environ", {setting: value}):
                with self.assertRaises(pace.PacingConfigError):
                    pace.weekly_daily_targets()


class LeanInTests(unittest.TestCase):
    """Lean-in rule (decision 2), on the provider's longest window with real data.

    Two cases: under 30m to reset with a surplus, or under 50% used and under 24h to reset.
    A surplus means used % is below the elapsed % of that window.
    """

    def test_under_30_minutes_to_reset_leans_in(self):
        # 80% used, 29m left on 5h: about 90% elapsed, so a surplus.
        rows = [_row("claude", 80, 29 * MIN_MS)]
        self.assertTrue(pace.lean_in(rows, "claude", 0))

    def test_under_30_minutes_without_surplus_does_not_lean_in(self):
        # 93% used, 29m left on 5h: about 90% elapsed, so no surplus.
        rows = [_row("claude", 93, 29 * MIN_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_surplus_is_measured_on_the_longest_window(self):
        # 5h window has a surplus (10% used, about 90% elapsed). The 7-day
        # window is the longest: 90% used, 20h left, about 88% elapsed, so
        # no surplus. The result follows the 7-day window: no lean-in.
        rows = [
            _row("claude", 10, 29 * MIN_MS, duration=QuotaDuration.FIVE_HOURS),
            _row("claude", 90, 20 * HOUR_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_exactly_30_minutes_to_reset_does_not_lean_in(self):
        rows = [_row("claude", 80, 30 * MIN_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_exactly_30_minutes_with_surplus_does_not_lean_in(self):
        # 80% used, 30m left on 5h: about 90% elapsed, so a surplus.
        # The 30m check is strict. Exactly 30m does not qualify.
        rows = [_row("claude", 80, 30 * MIN_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_blocked_under_30_minutes_never_leans_in(self):
        # 5h window at 100% is blocked. The 7-day window (longest) has a
        # surplus with 29m left, so it would lean in alone. Blocked wins.
        rows = [
            _row("claude", 100, 29 * MIN_MS, duration=QuotaDuration.FIVE_HOURS),
            _row("claude", 10, 29 * MIN_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_low_use_and_under_24h_leans_in(self):
        rows = [_row("claude", 49, 23 * HOUR_MS)]
        self.assertTrue(pace.lean_in(rows, "claude", 0))

    def test_exactly_50_percent_used_does_not_lean_in(self):
        rows = [_row("claude", 50, 23 * HOUR_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_exactly_24h_remaining_does_not_lean_in(self):
        rows = [_row("claude", 10, 24 * HOUR_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_high_use_and_far_from_reset_does_not_lean_in(self):
        rows = [_row("claude", 80, 23 * HOUR_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_blocked_provider_never_leans_in(self):
        # 96% used, 5h left: not inside the lean-in window at all, but
        # also blocked -- confirms blocked wins even when lean-in's own
        # conditions would otherwise have been false anyway.
        rows = [_row("claude", 96, 5 * HOUR_MS)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_blocked_provider_never_leans_in_even_if_it_would_otherwise_qualify(self):
        # The 5-hour window is at 96% with 5h left (blocked). The 7-day
        # window -- the longest window with real data -- is at 10% used
        # with 20h left, which alone would qualify for lean-in. Blocked
        # still wins: the provider is blocked overall because ANY window
        # is blocked, so lean-in must not trigger.
        rows = [
            _row("claude", 96, 5 * HOUR_MS, duration=QuotaDuration.FIVE_HOURS),
            _row("claude", 10, 20 * HOUR_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_uses_the_longest_window_not_the_first_one(self):
        # 5-hour window looks like a strong lean-in candidate (low use,
        # about to reset) but is not the longest window -- the 7-day
        # window (high use, far from reset) governs instead, so no lean-in.
        rows = [
            _row("claude", 5, 10 * MIN_MS, duration=QuotaDuration.FIVE_HOURS),
            _row("claude", 90, 20 * HOUR_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertFalse(pace.lean_in(rows, "claude", 0))

    def test_no_usable_rows_does_not_lean_in(self):
        self.assertFalse(pace.lean_in([], "claude", 0))

    def test_only_other_duration_window_does_not_lean_in(self):
        rows = [_row("claude", 5, MIN_MS, duration=QuotaDuration.OTHER)]
        self.assertFalse(pace.lean_in(rows, "claude", 0))


class EarliestResetTests(unittest.TestCase):
    def test_returns_the_minimum_across_providers_regardless_of_order(self):
        rows = [
            _row("claude", 100, 5 * HOUR_MS),
            _row("opencode-go", 100, 30 * MIN_MS),
            _row("openai", 100, 2 * HOUR_MS),
        ]
        # Pass providers in an order where the true minimum (opencode-go)
        # is checked last, confirming it's not "whichever was seen first".
        result = pace.earliest_reset(rows, ["claude", "openai", "opencode-go"], 0)
        self.assertEqual(result, 30 * MIN_MS)

    def test_ignores_an_unblocked_providers_window(self):
        rows = [
            _row("claude", 10, 5 * HOUR_MS),  # not blocked
            _row("openai", 100, 2 * HOUR_MS),  # blocked
        ]
        result = pace.earliest_reset(rows, ["claude", "openai"], 0)
        self.assertEqual(result, 2 * HOUR_MS)

    def test_no_blocked_candidates_returns_none(self):
        rows = [_row("claude", 10, 5 * HOUR_MS)]
        self.assertIsNone(pace.earliest_reset(rows, ["claude"], 0))

    def test_blocked_provider_with_no_resets_at_is_ignored(self):
        rows = [{"provider": "claude", "used_pct": 100, "resets_at": None}]
        self.assertIsNone(pace.earliest_reset(rows, ["claude"], 0))


def _acct(account, *args, **kwargs):
    return {**_row(*args, **kwargs), "account": account}


class AccountTests(unittest.TestCase):
    """A provider can have more than one account. `omp` picks the account,
    so only an all-accounts-blocked provider is blocked."""

    def test_one_blocked_account_does_not_block_the_provider(self):
        rows = [
            _acct("free", "openai", 100, 20 * 24 * HOUR_MS, duration=QuotaDuration.MONTHLY),
            _acct("plus", "openai", 1, 2 * HOUR_MS),
        ]
        self.assertFalse(pace.blocked(rows, "openai", 0))

    def test_provider_is_blocked_when_every_account_is_blocked(self):
        rows = [
            _acct("free", "openai", 100, 20 * 24 * HOUR_MS, duration=QuotaDuration.MONTHLY),
            _acct("plus", "openai", 100, 4 * HOUR_MS),
        ]
        self.assertTrue(pace.blocked(rows, "openai", 0))

    def test_a_blocked_window_still_blocks_its_own_account(self):
        # plus is fine on 5 hours but used up on 7 days, so plus is blocked.
        rows = [
            _acct("plus", "openai", 1, 2 * HOUR_MS),
            _acct("plus", "openai", 100, 50 * HOUR_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertTrue(pace.blocked(rows, "openai", 0))

    def test_an_account_with_no_usable_rows_is_not_counted(self):
        rows = [
            _acct("free", "openai", 100, 4 * HOUR_MS),
            {"provider": "openai", "account": "plus", "note": "no usage"},
        ]
        self.assertTrue(pace.blocked(rows, "openai", 0))

    def test_lean_in_ignores_a_blocked_accounts_longer_window(self):
        rows = [
            _acct("free", "openai", 100, 20 * 24 * HOUR_MS, duration=QuotaDuration.MONTHLY),
            _acct("plus", "openai", 10, 20 * MIN_MS),
        ]
        self.assertTrue(pace.lean_in(rows, "openai", 0))

    def test_lean_in_needs_every_open_account_to_qualify(self):
        rows = [
            _acct("a", "openai", 10, 20 * MIN_MS),  # surplus, resets soon
            _acct("b", "openai", 80, 4 * HOUR_MS),  # no surplus
        ]
        self.assertFalse(pace.lean_in(rows, "openai", 0))

    def test_lean_in_is_false_when_every_account_is_blocked(self):
        rows = [
            _acct("a", "openai", 100, 20 * MIN_MS),
            _acct("b", "openai", 100, 20 * MIN_MS),
        ]
        self.assertFalse(pace.lean_in(rows, "openai", 0))


class HeadroomTests(unittest.TestCase):
    def test_one_account_uses_its_most_used_window(self):
        rows = [
            _row("claude", 30, 4 * HOUR_MS),
            _row("claude", 60, 50 * HOUR_MS, duration=QuotaDuration.WEEKLY),
        ]
        self.assertEqual(pace.headroom(rows, "claude", 0), 40)

    def test_best_open_account_wins(self):
        rows = [
            _acct("a", "openai", 70, 4 * HOUR_MS),
            _acct("b", "openai", 20, 4 * HOUR_MS),
        ]
        self.assertEqual(pace.headroom(rows, "openai", 0), 80)

    def test_a_blocked_account_never_sets_the_headroom(self):
        rows = [
            _acct("free", "openai", 100, 20 * 24 * HOUR_MS, duration=QuotaDuration.MONTHLY),
            _acct("plus", "openai", 10, 4 * HOUR_MS),
        ]
        self.assertEqual(pace.headroom(rows, "openai", 0), 90)

    def test_none_when_blocked_or_when_there_is_no_data(self):
        self.assertIsNone(pace.headroom([_row("claude", 100, 4 * HOUR_MS)], "claude", 0))
        self.assertIsNone(pace.headroom([], "claude", 0))
        self.assertIsNone(pace.headroom(
            [{"provider": "claude", "note": "no key"}], "claude", 0,
        ))


if __name__ == "__main__":
    unittest.main()

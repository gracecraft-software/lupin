"""Quota pacing facts for `route()` (issue #36).

Quota is shared by the whole fleet -- never a per-machine signal. This
module takes quota rows (`quota.quota_usage()`'s shape) and a timestamp,
and answers two questions for one provider at a time: is it blocked, and
should a task lean in (route up one tier to use a surplus before it
resets)?

A provider can have more than one account. Rows then have an `account`
label. `omp` picks the account for each session. So a provider is blocked
only when every account is blocked. A blocked account never counts for
lean-in or headroom.

This module has no stored state or Redis access. It reads provider targets from
`LUPIN_WEEKLY_QUOTA_DAILY_TARGETS`; all other facts come from quota rows and a
timestamp. `route.py` and `loop_runtime.py` use the same rules for model and
profile choices.

The short-window rules come from issue #36. The 30-minute surplus rule comes
from issue #99. Weekly windows with a configured daily target use a linear
target across the window.

At an exact weekly target, Lupin allows use. A value above it is blocked.
Other reserve and lean-in boundaries stay conservative: `>=` is blocked and
`<` is required to lean in.
"""

import json
import math
import os

from .quota import QuotaDuration

RESERVE_PCT = 95
RESERVE_GRACE_MS = 3 * 60 * 60 * 1000  # 3 hours
CLAUDE_WEEKLY_BLOCK_PCT = 90
LEAN_IN_SOON_MS = 30 * 60 * 1000  # 30 minutes
LEAN_IN_LOW_USE_PCT = 50
LEAN_IN_LOW_USE_WINDOW_MS = 24 * 60 * 60 * 1000  # 24 hours

WEEKLY_DAILY_TARGETS_SETTING = "LUPIN_WEEKLY_QUOTA_DAILY_TARGETS"
DEFAULT_WEEKLY_DAILY_TARGETS = {"claude": CLAUDE_WEEKLY_BLOCK_PCT / 7}
DAY_MS = 24 * 60 * 60 * 1000


class PacingConfigError(ValueError):
    """A weekly quota target setting is invalid."""


def weekly_daily_targets() -> dict[str, float]:
    """Return the default targets with any provider overrides."""
    raw = os.environ.get(WEEKLY_DAILY_TARGETS_SETTING)
    if raw is None:
        return dict(DEFAULT_WEEKLY_DAILY_TARGETS)
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PacingConfigError(
            f"{WEEKLY_DAILY_TARGETS_SETTING} must be a JSON object: {exc.msg}"
        ) from None
    if not isinstance(configured, dict):
        raise PacingConfigError(
            f"{WEEKLY_DAILY_TARGETS_SETTING} must be a JSON object of provider names and daily percentages"
        )
    targets = dict(DEFAULT_WEEKLY_DAILY_TARGETS)
    for provider, daily_pct in configured.items():
        if not isinstance(provider, str) or not provider or provider.strip() != provider:
            raise PacingConfigError(
                f"{WEEKLY_DAILY_TARGETS_SETTING} has an invalid provider name"
            )
        if (
            isinstance(daily_pct, bool)
            or not isinstance(daily_pct, (int, float))
            or not 0 <= daily_pct <= 100
            or not math.isfinite(daily_pct)
        ):
            raise PacingConfigError(
                f"{WEEKLY_DAILY_TARGETS_SETTING} value for {provider!r} must be a number from 0 to 100"
            )
        targets[provider] = float(daily_pct)
    return targets


def _weekly_targets(value: dict[str, float] | None) -> dict[str, float]:
    return weekly_daily_targets() if value is None else value


def _usable_rows(
    rows: list[dict], provider: str, weekly_targets: dict[str, float]
) -> list[dict]:
    """Rows with a usable percentage and reset, or a configured weekly target."""
    return [
        row for row in rows
        if row.get("provider") == provider
        and isinstance(row.get("used_pct"), (int, float))
        and (
            isinstance(row.get("resets_at"), (int, float))
            or (
                row.get("duration") == QuotaDuration.WEEKLY
                and provider in weekly_targets
            )
        )
    ]


def _window_blocked(
    row: dict, now_ms: float, weekly_targets: dict[str, float]
) -> bool:
    """True when one quota window is over its target or reserve."""
    used_pct = row["used_pct"]
    provider = row.get("provider")
    duration = row.get("duration")
    if duration == QuotaDuration.WEEKLY and provider in weekly_targets:
        resets_at = row.get("resets_at")
        if not isinstance(resets_at, (int, float)):
            return True
        duration_ms = duration.milliseconds
        elapsed_ms = max(0, min(duration_ms, duration_ms - (resets_at - now_ms)))
        ceiling = CLAUDE_WEEKLY_BLOCK_PCT if provider == "claude" else RESERVE_PCT
        target_pct = min(ceiling, weekly_targets[provider] * elapsed_ms / DAY_MS)
        return used_pct > target_pct
    if used_pct >= 100:
        return True
    if used_pct >= RESERVE_PCT:
        return (row["resets_at"] - now_ms) >= RESERVE_GRACE_MS
    return False


def _accounts(
    rows: list[dict], provider: str, weekly_targets: dict[str, float]
) -> list[list[dict]]:
    """`provider`'s usable rows, one list for each account."""
    accounts: dict[str | None, list[dict]] = {}
    for row in _usable_rows(rows, provider, weekly_targets):
        accounts.setdefault(row.get("account"), []).append(row)
    return list(accounts.values())


def _account_blocked(
    account_rows: list[dict], now_ms: float, weekly_targets: dict[str, float]
) -> bool:
    return any(
        _window_blocked(row, now_ms, weekly_targets)
        for row in account_rows
    )


def blocked(
    rows: list[dict],
    provider: str,
    now_ms: float,
    *,
    weekly_targets: dict[str, float] | None = None,
) -> bool:
    """True if every usable account of `provider` has a blocked window."""
    targets = _weekly_targets(weekly_targets)
    accounts = _accounts(rows, provider, targets)
    return bool(accounts) and all(
        _account_blocked(account_rows, now_ms, targets) for account_rows in accounts
    )


def headroom(
    rows: list[dict],
    provider: str,
    now_ms: float,
    *,
    weekly_targets: dict[str, float] | None = None,
) -> float | None:
    """The most quota left on one account that is not blocked."""
    targets = _weekly_targets(weekly_targets)
    open_accounts = [
        account_rows for account_rows in _accounts(rows, provider, targets)
        if not _account_blocked(account_rows, now_ms, targets)
    ]
    if not open_accounts:
        return None
    return max(
        min(100 - row["used_pct"] for row in account_rows)
        for account_rows in open_accounts
    )


def _longest_window(account_rows: list[dict]) -> dict | None:
    """One account's longest window with real data and a known length.

    `QuotaDuration.OTHER` has no `.milliseconds` -- a window of unknown
    length can't be compared to another, so it's excluded here (it still
    counts for `blocked()`, which doesn't need a length).
    """
    candidates = [
        row for row in account_rows
        if (
            isinstance(row.get("duration"), QuotaDuration)
            and row["duration"].milliseconds
            and isinstance(row.get("resets_at"), (int, float))
        )
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: row["duration"].milliseconds)


def _account_leans_in(account_rows: list[dict], now_ms: float) -> bool:
    window = _longest_window(account_rows)
    if window is None:
        return False
    remaining = window["resets_at"] - now_ms
    duration_ms = window["duration"].milliseconds
    # Surplus: used_pct is below the percent of the window already elapsed.
    elapsed_pct = 100 * (duration_ms - remaining) / duration_ms
    has_surplus = window["used_pct"] < elapsed_pct
    if remaining < LEAN_IN_SOON_MS and has_surplus:
        return True
    return window["used_pct"] < LEAN_IN_LOW_USE_PCT and remaining < LEAN_IN_LOW_USE_WINDOW_MS


def lean_in(
    rows: list[dict],
    provider: str,
    now_ms: float,
    *,
    weekly_targets: dict[str, float] | None = None,
) -> bool:
    """True if every open account of `provider` has earned a one-tier-up lean-in."""
    targets = _weekly_targets(weekly_targets)
    open_accounts = [
        account_rows for account_rows in _accounts(rows, provider, targets)
        if not _account_blocked(account_rows, now_ms, targets)
    ]
    return bool(open_accounts) and all(
        _account_leans_in(account_rows, now_ms) for account_rows in open_accounts
    )


def earliest_reset(
    rows: list[dict],
    providers: list[str],
    now_ms: float,
    *,
    weekly_targets: dict[str, float] | None = None,
) -> float | None:
    """Earliest reset among blocked windows for the providers."""
    targets = _weekly_targets(weekly_targets)
    waits = [
        row["resets_at"]
        for provider in providers
        for row in _usable_rows(rows, provider, targets)
        if isinstance(row.get("resets_at"), (int, float))
        and _window_blocked(row, now_ms, targets)
    ]
    return min(waits) if waits else None

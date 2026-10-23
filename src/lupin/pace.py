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

Pure. No stored state, no Redis, no file access -- every number comes from
the rows a caller passes in. `route.py` is the only caller; it decides what
to do with these facts (which tier, which fallback). This module just
reports them.

The literal rules come from issue #36 (comment of 2026-10-07 20:31 UTC).
The surplus test on the 30-minute lean-in comes from issue #99.
The abstract pacing-ratio design from issue #36's earlier comment is not
implemented here.

Boundary convention, used consistently below: every "less than" check is
strict (`<`), every "at or above" check is `>=`. An exact boundary value
(used_pct == 95, remaining == 3h, remaining == 30m, remaining == 24h, used_pct
== 50) always falls on the *conservative* side -- still reserved, still
blocked, no lean-in -- never on the side that spends more quota.
"""

from __future__ import annotations

from .quota import QuotaDuration

RESERVE_PCT = 95
RESERVE_GRACE_MS = 3 * 60 * 60 * 1000  # 3 hours
CLAUDE_WEEKLY_BLOCK_PCT = 90
LEAN_IN_SOON_MS = 30 * 60 * 1000  # 30 minutes
LEAN_IN_LOW_USE_PCT = 50
LEAN_IN_LOW_USE_WINDOW_MS = 24 * 60 * 60 * 1000  # 24 hours


def _usable_rows(rows: list[dict], provider: str) -> list[dict]:
    """Rows with a real `used_pct` and enough reset data for their rules.

    Claude's weekly percentage guard does not need a reset timestamp. Other
    windows do, because their reserve and grace calculations use it.
    """
    return [
        row for row in rows
        if row.get("provider") == provider
        and isinstance(row.get("used_pct"), (int, float))
        and (
            isinstance(row.get("resets_at"), (int, float))
            or (
                provider == "claude"
                and row.get("duration") == QuotaDuration.WEEKLY
            )
        )
    ]


def _window_blocked(row: dict, now_ms: float) -> bool:
    """True when one quota window is reserved or exhausted.

    Claude's weekly window is blocked above 90%, with no reset grace.
    Other windows use the 95% reserve unless under 3 hours remain; 100%
    usage is always blocked.
    """
    used_pct = row["used_pct"]
    if (
        row.get("provider") == "claude"
        and row.get("duration") == QuotaDuration.WEEKLY
    ):
        return used_pct > CLAUDE_WEEKLY_BLOCK_PCT
    if used_pct >= 100:
        return True
    if used_pct >= RESERVE_PCT:
        return (row["resets_at"] - now_ms) >= RESERVE_GRACE_MS
    return False


def _accounts(rows: list[dict], provider: str) -> list[list[dict]]:
    """`provider`'s usable rows, one list for each account.

    Rows with no `account` label are one account.
    """
    accounts: dict[str | None, list[dict]] = {}
    for row in _usable_rows(rows, provider):
        accounts.setdefault(row.get("account"), []).append(row)
    return list(accounts.values())


def _account_blocked(account_rows: list[dict], now_ms: float) -> bool:
    return any(
        _window_blocked(row, now_ms)
        for row in account_rows
    )


def blocked(rows: list[dict], provider: str, now_ms: float) -> bool:
    """True if every account of `provider` has a blocked window.

    An account with no usable rows is unknown, not blocked, and not counted.
    """
    accounts = _accounts(rows, provider)
    return bool(accounts) and all(_account_blocked(account_rows, now_ms) for account_rows in accounts)


def headroom(rows: list[dict], provider: str, now_ms: float) -> float | None:
    """The most quota left (in percent) on one account that is not blocked.

    For one account this is its most-used window. `None` when no account
    has data, or every account is blocked.
    """
    open_accounts = [
        account_rows for account_rows in _accounts(rows, provider)
        if not _account_blocked(account_rows, now_ms)
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


def lean_in(rows: list[dict], provider: str, now_ms: float) -> bool:
    """True if `provider` has earned a one-tier-up lean-in (decision 2).

    Looks only at the longest window with real data of each account. A
    blocked account is skipped. Blocked always wins: a provider with no
    open account never leans in. `omp` can pick any open account, so every
    open account must qualify.

    A surplus means used_pct is below the elapsed percent of the longest
    window. The 30-minute clause needs a surplus. The 24-hour clause
    needs used_pct under 50 and does not check for a surplus.
    """
    open_accounts = [
        account_rows for account_rows in _accounts(rows, provider)
        if not _account_blocked(account_rows, now_ms)
    ]
    return bool(open_accounts) and all(
        _account_leans_in(account_rows, now_ms) for account_rows in open_accounts
    )


def earliest_reset(rows: list[dict], providers: list[str], now_ms: float) -> float | None:
    """Earliest `resets_at` among `providers`' blocked windows.

    `route()` calls this once every candidate it tried is blocked, to
    report how long the caller must wait. Ignores a provider with no
    blocked window (nothing to wait on there) and a blocked window with no
    usable `resets_at` (unknown, not zero) -- `None` means not one of
    `providers` has a usable wait estimate at all.
    """
    waits = [
        row["resets_at"]
        for provider in providers
        for row in _usable_rows(rows, provider)
        if isinstance(row.get("resets_at"), (int, float))
        and _window_blocked(row, now_ms)
    ]
    return min(waits) if waits else None

"""Quota reading: how much of each provider's rate limit is left.

Moved out of `serve.py` (issue #8) so the fleet heartbeat (`machines.py`,
issue #7/#2-C3) can read quota without pulling in the dashboard's HTTP
server. `serve.py` still renders this data on the `/usage` page -- it
imports the functions below instead of defining them.

`snapshot()` is the one entry point other modules should use: one number
per provider, `{provider: {pct_left, resets_at, source}}`. The rest of
this module is the detail `snapshot()` and `serve.py`'s rendering build
on top of.

A provider can have more than one login (for example, a free and a paid
OpenAI account). Then each row has an `account` label. `omp` picks the
login for each session, so Lupin only reads each login's quota.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)

CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CLAUDE_STATS_FILE = os.path.expanduser("~/.claude/stats-cache.json")
CLAUDE_CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_GO_AUTH_FILE = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
    "opencode",
    "auth.json",
)
OMP_STATS_FILE = os.path.expanduser("~/.omp/stats.db")

# The names the Usage page shows for the local usage sources. The page
# shows these names, never the file paths (issue #51).
CLAUDE_STATS_SOURCE = "Claude local stats cache"
OMP_STATS_SOURCE = "OMP local stats database"

# The plain text the page shows when a source cannot be read (issue #51).
# The exception name and the path stay in the log.
NOT_AVAILABLE = "Not available"
QUOTA_NOT_AVAILABLE = "Quota data not available"

# opencode.ai sits behind Cloudflare, which blocks Python urllib's default
# "Python-urllib/x.y" user agent with a 403 (confirmed, issue #34).
_USER_AGENT = "lupin-quota/1.0"

# Notes for a provider the machine has no credentials for. place.py counts
# these apart from read failures.
NO_CLAUDE_CREDENTIALS = "no Claude credentials"
NO_OPENCODE_GO_KEY = "no OpenCode Go API key available"
OPENCODE_GO_KEY_EXPIRED = "OpenCode Go API key expired -- get a new key in the OpenCode Console"
NO_KEY_NOTES = frozenset({NO_CLAUDE_CREDENTIALS, NO_OPENCODE_GO_KEY})


class QuotaDuration(str, Enum):
    FIVE_HOURS = "PT5H"
    WEEKLY = "P7D"
    MONTHLY = "P30D"
    OTHER = "OTHER"

    @property
    def label(self) -> str:
        return {
            QuotaDuration.FIVE_HOURS: "5 hours",
            QuotaDuration.WEEKLY: "7 days",
            QuotaDuration.MONTHLY: "Monthly",
            QuotaDuration.OTHER: "Other",
        }[self]

    @property
    def milliseconds(self) -> int | None:
        return {
            QuotaDuration.FIVE_HOURS: 18_000_000,
            QuotaDuration.WEEKLY: 604_800_000,
            QuotaDuration.MONTHLY: 2_592_000_000,
        }.get(self)


def run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Run a fixed read-only probe. Never a shell, never browser input.

    A private copy of `serve.run` -- this module must not import `serve`
    (the fleet heartbeat in `machines.py` reads quota without starting a
    web server), so it carries its own small subprocess wrapper rather
    than share one.
    """
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(argv)}"
    out = proc.stdout
    if proc.stderr:
        out = out + ("\n" if out and not out.endswith("\n") else "") + proc.stderr
    return proc.returncode, out


def unavailable_usage(
    provider: str,
    source: str,
    error: Exception,
    label: str = "local usage data",
) -> list[dict]:
    """A plain unavailable row. The page never shows the error (issue #51).

    `source` says where the data would come from. `label` is the name the
    page shows for it, so it must be a name a person can read, never a
    file path. The exception name and the path go to the log.
    """
    logger.warning(
        "quota source %s unavailable (%s): %s: %s",
        provider,
        source,
        type(error).__name__,
        error,
    )
    return [{
        "provider": provider,
        "source": label,
        "error": NOT_AVAILABLE,
    }]


def claude_usage() -> list[dict]:
    """Read Claude Code's local telemetry cache.

    dailyModelTokens holds one flat token total per model per day -- no
    input/output split, no cost. That breakdown exists only as an all-time
    total under the top-level modelUsage key, which has no date range, so
    it can't be windowed to 7 days. Report what the daily source actually
    has: a combined token total, and cost as untracked rather than 0.
    """
    try:
        with open(CLAUDE_STATS_FILE, encoding="utf-8") as stats_file:
            stats = json.load(stats_file)
        last_update = stats["lastComputedDate"]
        date.fromisoformat(last_update)
        cutoff = (date.today() - timedelta(days=6)).isoformat()
        total_tokens = 0
        for day in stats["dailyModelTokens"]:
            if cutoff <= day["date"] <= date.today().isoformat():
                total_tokens += sum(day["tokensByModel"].values())
        return [{
            "provider": "claude",
            "input_tokens": total_tokens,
            "output_tokens": None,
            "cost": None,
            "period": "last 7 days",
            "source": CLAUDE_STATS_SOURCE,
            "last_update": last_update,
        }]
    except Exception as error:
        return unavailable_usage("claude", CLAUDE_STATS_FILE, error, CLAUDE_STATS_SOURCE)


def omp_usage() -> list[dict]:
    try:
        with closing(sqlite3.connect(OMP_STATS_FILE)) as db:
            latest = dict(db.execute(
                "SELECT provider, MAX(timestamp) FROM messages GROUP BY provider"
            ))
            if not latest:
                raise ValueError("messages table is empty")
            now = time.time()
            cutoff = (now - 7 * 24 * 60 * 60) * 1000
            totals = db.execute(
                "SELECT provider, SUM(input_tokens), SUM(output_tokens), "
                "SUM(cost_total) FROM messages WHERE timestamp >= ? AND timestamp <= ? "
                "GROUP BY provider",
                (cutoff, now * 1000),
            )
            period_totals = {
                provider: (input_tokens or 0, output_tokens or 0, cost or 0)
                for provider, input_tokens, output_tokens, cost in totals
            }
        return [
            {
                "provider": provider,
                "input_tokens": period_totals.get(provider, (0, 0, 0))[0],
                "output_tokens": period_totals.get(provider, (0, 0, 0))[1],
                "cost": period_totals.get(provider, (0, 0, 0))[2],
                "period": "last 7 days",
                "source": OMP_STATS_SOURCE,
                "last_update": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(timestamp / 1000)
                ),
            }
            for provider, timestamp in sorted(latest.items())
        ]
    except Exception as error:
        return unavailable_usage("stats.db", OMP_STATS_FILE, error, OMP_STATS_SOURCE)


def epoch_ms_to_local(value) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value / 1000))


def codex_session_quota() -> list[dict]:
    """Read the newest OpenAI Codex rate-limit snapshot written by Codex.

    Orca reads the live Codex app-server when it is available. The local
    rollout files provide its cached fallback: exact percentages, but only
    refreshed when Codex writes a session event.
    """
    files = []
    for root, _, names in os.walk(CODEX_SESSIONS_DIR):
        files.extend(
            os.path.join(root, name)
            for name in names
            if name.startswith("rollout-") and name.endswith(".jsonl")
        )
    for rollout_path in sorted(files, key=os.path.getmtime, reverse=True):
        try:
            with open(rollout_path, encoding="utf-8") as rollout:
                for line in reversed(rollout.readlines()):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") or {}
                    if payload.get("type") != "token_count":
                        continue
                    limits = payload.get("rate_limits") or {}
                    rows = []
                    for key, duration in (
                        ("primary", QuotaDuration.FIVE_HOURS),
                        ("secondary", QuotaDuration.WEEKLY),
                    ):
                        limit = limits.get(key) or {}
                        used = limit.get("used_percent")
                        if not isinstance(used, (int, float)):
                            continue
                        rows.append({
                            "provider": "openai",
                            "duration": duration,
                            "used_pct": min(100, max(0, used)),
                            "resets_at": quota_reset_timestamp(limit.get("resets_at")),
                            "generated_at": time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(os.path.getmtime(rollout_path)),
                            ),
                        })
                    if rows:
                        return rows
        except OSError:
            continue
    return [{"provider": "openai", "note": "no Codex session quota data available"}]


def quota_reset_timestamp(value) -> int | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return int(seconds * 1000)
    if isinstance(value, str):
        try:
            return quota_reset_timestamp(float(value))
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return int(parsed.timestamp() * 1000)
            except ValueError:
                return None
    return None


def window_milliseconds(window: dict) -> int | None:
    """The length of a quota window in ms, when the source gives one.

    A window the page cannot name can still have a known length. The
    page needs the length to place the pace marker (issue #80).
    """
    duration_ms = window.get("durationMs")
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        return int(duration_ms)
    return None


def quota_duration(limit: dict, window: dict) -> QuotaDuration:
    duration_ms = window_milliseconds(window)
    if duration_ms is not None:
        for duration in (
            QuotaDuration.FIVE_HOURS,
            QuotaDuration.WEEKLY,
            QuotaDuration.MONTHLY,
        ):
            if duration_ms == duration.milliseconds:
                return duration
    identifier = " ".join(
        str(value).lower()
        for value in (
            window.get("id"),
            limit.get("id"),
            window.get("label"),
            limit.get("label"),
        )
        if value is not None
    )
    normalized = re.sub(r"[^a-z0-9]", "", identifier)
    if any(key in normalized for key in ("5h", "fivehour", "5hour", "session", "primary")):
        return QuotaDuration.FIVE_HOURS
    if any(key in normalized for key in ("7d", "sevenday", "7day", "weekly", "secondary")):
        return QuotaDuration.WEEKLY
    if any(key in normalized for key in ("30d", "monthly", "month")):
        return QuotaDuration.MONTHLY
    return QuotaDuration.OTHER

def _claude_token() -> str | None:
    """The OAuth access token, or None when the file or token is missing.

    A file that exists but cannot be read or parsed raises. The caller
    then reports a read failure.
    """
    try:
        with open(CLAUDE_CREDENTIALS_FILE, encoding="utf-8") as credentials_file:
            credentials = json.load(credentials_file)
    except FileNotFoundError:
        return None
    try:
        token = credentials["claudeAiOauth"]["accessToken"]
    except (KeyError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def claude_oauth_quota() -> list[dict]:
    try:
        token = _claude_token()
        if token is None:
            return [{"provider": "claude", "note": NO_CLAUDE_CREDENTIALS}]
        request = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except (OSError, KeyError, TypeError, ValueError, urllib.error.URLError) as error:
        logger.warning(
            "claude oauth quota unavailable: %s: %s", type(error).__name__, error
        )
        return [{"provider": "claude", "note": QUOTA_NOT_AVAILABLE}]
    if not isinstance(data, dict):
        return [{"provider": "claude", "note": "no quota data reported"}]
    rows = []
    for field, duration in (
        ("five_hour", QuotaDuration.FIVE_HOURS),
        ("seven_day", QuotaDuration.WEEKLY),
    ):
        limit = data.get(field) or {}
        if not isinstance(limit, dict):
            continue
        used = limit.get("utilization", limit.get("used_percentage"))
        if isinstance(used, (int, float)):
            rows.append({
                "provider": "claude",
                "duration": duration,
                "used_pct": min(100, max(0, used)),
                "resets_at": quota_reset_timestamp(limit.get("resets_at")),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            })
    return rows or [{"provider": "claude", "note": "no quota data reported"}]


def _omp_token(provider: str) -> str:
    """The key that `omp` holds for `provider`, or "" if `omp` gives none.

    Only stdout counts. A failed command gives no key. Output that is not
    one word is an error message, not a key. Output that is not UTF-8 gives
    no key. The key is never logged.
    """
    try:
        proc = subprocess.run(
            ["omp", "token", provider],
            shell=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    words = proc.stdout.split()
    return words[0] if proc.returncode == 0 and len(words) == 1 else ""


def opencode_go_quota() -> list[dict]:
    key = ""
    try:
        with open(OPENCODE_GO_AUTH_FILE, encoding="utf-8") as auth_file:
            auth = json.load(auth_file)
        entry = auth.get("opencode-go") or {}
        if entry.get("type") == "api":
            key = entry.get("key", "").strip()
    except (OSError, ValueError, AttributeError):
        pass
    if not key:
        key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key:
        key = _omp_token("opencode-go")
    if not key:
        return [{"provider": "opencode-go", "note": NO_OPENCODE_GO_KEY}]

    request = urllib.request.Request(
        OPENCODE_GO_USAGE_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4096)
        except (OSError, http.client.HTTPException):
            body = b""
        if error.code == 401 and b"api_key_expired" in body:
            return [{"provider": "opencode-go", "note": OPENCODE_GO_KEY_EXPIRED}]
        logger.warning(
            "opencode-go quota unavailable: %s: %s", type(error).__name__, error
        )
        return [{"provider": "opencode-go", "note": QUOTA_NOT_AVAILABLE}]
    except (OSError, ValueError, urllib.error.URLError) as error:
        logger.warning(
            "opencode-go quota unavailable: %s: %s", type(error).__name__, error
        )
        return [{"provider": "opencode-go", "note": QUOTA_NOT_AVAILABLE}]

    if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
        return [{"provider": "opencode-go", "note": "no quota data reported"}]
    usage = data["usage"]
    rows = []
    for field, duration in (
        ("rolling", QuotaDuration.FIVE_HOURS),
        ("weekly", QuotaDuration.WEEKLY),
        ("monthly", QuotaDuration.MONTHLY),
    ):
        limit = usage.get(field) or {}
        if not isinstance(limit, dict):
            continue
        used = limit.get("percent")
        if isinstance(used, (int, float)):
            rows.append({
                "provider": "opencode-go",
                "duration": duration,
                "used_pct": min(100, max(0, used)),
                "resets_at": quota_reset_timestamp(limit.get("resetsAt")),
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            })
    return rows or [{"provider": "opencode-go", "note": "no quota data reported"}]


def add_quota_fallbacks(rows: list[dict]) -> list[dict]:
    if not any(row.get("provider") == "opencode-go" and "used_pct" in row for row in rows):
        rows.extend(opencode_go_quota())
    rows.extend(claude_oauth_quota())
    return rows


def _report_provider(report: dict) -> str:
    provider = report.get("provider", "unknown")
    return "openai" if provider == "openai-codex" else provider


def _account_labels(reports: list[dict]) -> list[str | None]:
    """One label per `omp` report. `None` when the provider has one login.

    The label is the plan name (`free`, `plus`). Two logins on the same
    plan also get a short hash of the user (email) and the workspace
    (`accountId`). Two users can share one workspace, so the hash uses
    both. The email and the account ID never leave this function: the
    label goes to Redis and the dashboard.
    """
    by_provider: dict[str, list[int]] = {}
    for index, report in enumerate(reports):
        by_provider.setdefault(_report_provider(report), []).append(index)
    labels: list[str | None] = [None] * len(reports)
    for indexes in by_provider.values():
        if len(indexes) < 2:
            continue
        metadata = [
            reports[index].get("metadata")
            if isinstance(reports[index].get("metadata"), dict) else {}
            for index in indexes
        ]
        plans = [str(item.get("planType") or "account") for item in metadata]
        for index, item, plan in zip(indexes, metadata, plans):
            if plans.count(plan) == 1:
                labels[index] = plan
            else:
                user = str(item.get("email") or "").strip().lower()
                workspace = str(item.get("accountId") or "")
                identity = f"{user}\0{workspace}" if user or workspace else str(index)
                labels[index] = f"{plan}-{hashlib.sha256(identity.encode()).hexdigest()[:6]}"
    return labels


def _reset_credits(report: dict) -> dict:
    """`reset_credits` for one login's rows. `{}` when omp reports none.

    `available` is omp's count. `expires_at` lists the known expiry times
    of the available credits, in epoch ms, sorted.
    """
    credits = report.get("resetCredits")
    if not isinstance(credits, dict) or not isinstance(credits.get("availableCount"), int):
        return {}
    credit_rows = credits.get("credits")
    if not isinstance(credit_rows, list):
        credit_rows = []
    expires = [
        quota_reset_timestamp(credit.get("expiresAt"))
        for credit in credit_rows
        if isinstance(credit, dict) and credit.get("status") == "available"
    ]
    return {"reset_credits": {
        "available": credits["availableCount"],
        "expires_at": sorted(ms for ms in expires if ms is not None),
    }}


def quota_usage() -> list[dict]:
    rc, out = run(["omp", "usage", "--json"])
    if rc != 0:
        rows = unavailable_usage(
            "omp", "omp usage --json", RuntimeError(f"exit code {rc}"), "omp usage"
        ) + codex_session_quota()
        return add_quota_fallbacks(rows)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as error:
        rows = unavailable_usage(
            "omp", "omp usage --json", error, "omp usage"
        ) + codex_session_quota()
        return add_quota_fallbacks(rows)
    if not isinstance(data, dict):
        rows = unavailable_usage(
            "omp", "omp usage --json", ValueError("expected a JSON object"), "omp usage"
        ) + codex_session_quota()
        return add_quota_fallbacks(rows)

    generated_at = epoch_ms_to_local(data.get("generatedAt"))
    rows = []
    reports = data.get("reports", [])
    for report, account in zip(reports, _account_labels(reports)):
        provider = _report_provider(report)
        if provider == "ollama":
            continue
        who = {"account": account} if account else {}
        # Reset credits belong to the login, so every row of it carries them.
        who.update(_reset_credits(report))
        limits = report.get("limits") or []
        if not limits:
            for note in report.get("notes") or ["no quota data reported"]:
                rows.append({"provider": provider, "note": note, "generated_at": generated_at, **who})
            continue
        for limit in limits:
            amount = limit.get("amount") or {}
            window = limit.get("window") or {}
            used_fraction = amount.get("usedFraction")
            rows.append({
                "provider": provider,
                "duration": quota_duration(limit, window),
                "duration_ms": window_milliseconds(window),
                "label": limit.get("label", limit.get("id", "-")),
                "used_pct": None if used_fraction is None else used_fraction * 100,
                "resets_at": quota_reset_timestamp(window.get("resetsAt")),
                "generated_at": generated_at,
                **who,
            })
    return add_quota_fallbacks(rows)


def quota_source_label(provider: str) -> str:
    return {
        "claude": "Anthropic OAuth usage API",
        "openai": "omp / Codex local snapshot",
        "opencode-go": "omp / Orca usage API",
    }.get(provider, "omp quota data")


def snapshot() -> dict[str, dict]:
    """One number per provider: `{provider: {pct_left, resets_at, source}}`.
    When `pct_left` is None the entry also has `note`, the reason.

    `quota_usage()` can return several rows per provider -- one per
    window (5 hours, 7 days, 30 days). This picks the 5-hour row when
    there is one (the window every reader tags "primary"/"session"), since
    that is the window that actually blocks the next request; otherwise it
    falls back to the first row for that provider. A provider with no
    usable percentage (an error or a "no data" note) still gets an entry,
    with `pct_left`/`resets_at` as `None` -- callers (`machines.py`'s
    heartbeat, eventually `place`) decide what to do with missing data.

    With more than one account, this picks one row for each account as
    above. Then it keeps the account with the most quota left on its
    most-used window, as `pace.headroom` does, because `omp` sends work to
    an account that has quota. The reported row stays the 5-hour row. An
    account that is blocked on any window (`pace.blocked`) is not used when
    another account is not blocked.
    """
    # `pace` imports this module, so import it here, not at the top.
    from . import pace

    rows = quota_usage()
    now_ms = time.time() * 1000
    weekly_targets = pace.weekly_daily_targets()
    per_account: dict[tuple[str, str | None], dict] = {}
    account_rows: dict[tuple[str, str | None], list[dict]] = {}
    for row in rows:
        provider = row.get("provider")
        if not provider:
            continue
        key = (provider, row.get("account"))
        account_rows.setdefault(key, []).append(row)
        if key not in per_account or row.get("duration") == QuotaDuration.FIVE_HOURS:
            per_account[key] = row

    blocked_accounts = {
        key for key in per_account
        if pace.blocked(
            account_rows[key], key[0], now_ms, weekly_targets=weekly_targets
        )
    }

    def _left(key: tuple[str, str | None]) -> float:
        # Rank rows with known resets. A configured weekly row without a reset
        # blocks the account, but cannot give quota headroom.
        values = [
            max(0.0, 100 - row["used_pct"]) for row in account_rows[key]
            if isinstance(row.get("used_pct"), (int, float))
            and isinstance(row.get("resets_at"), (int, float))
        ]
        return min(values) if values else -1.0

    def _rank(key: tuple[str, str | None]) -> tuple[bool, float]:
        # An open account comes before a blocked one. Then most quota left.
        return (key not in blocked_accounts, _left(key))

    best: dict[str, tuple[str, str | None]] = {}
    for key in per_account:
        provider = key[0]
        if provider not in best or _rank(key) > _rank(best[provider]):
            best[provider] = key
    chosen = {provider: per_account[key] for provider, key in best.items()}

    result = {}
    for provider, row in chosen.items():
        used = row.get("used_pct")
        pct_left = None if not isinstance(used, (int, float)) else max(0.0, 100 - used)
        entry = {
            "pct_left": pct_left,
            "resets_at": row.get("resets_at"),
            "source": quota_source_label(provider),
        }
        if pct_left is None:
            entry["note"] = row.get("note")
        result[provider] = entry
    return result

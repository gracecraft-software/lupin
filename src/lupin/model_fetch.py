"""Daily snapshot: which model IDs each subscription can call today, and
what they cost (issue #16).

Two questions per subscription (opencode-go, claude, codex):

1. Which model IDs are callable right now? Provider catalogs change, so
   this is a live fetch, not model-tiers.json's hand-edited tier list.
2. What does each model cost? A baseline $/Mtok price, plus a `promo`
   slot for a time-boxed discount -- separate from the baseline because a
   promo has its own start/end and should never overwrite the baseline
   number. No source for live promo data was found during this issue's
   research (see `snapshot()`), so `promo` is always `None` for now --
   the field exists so issue #18 has somewhere to put real data later.

What's live and what's not, confirmed against this sandbox:

- opencode-go: `OPENCODE_GO_MODELS_URL`, same API key quota.py's
  `opencode_go_quota()` reads: `OPENCODE_GO_AUTH_FILE`, then
  `OPENCODE_API_KEY`, then `omp token opencode-go`. Live. A 401 with
  `api_key_expired` gives the "key expired" error.
- claude: `ANTHROPIC_MODELS_URL`, same OAuth token quota.py's
  `claude_oauth_quota()` already reads from `CLAUDE_CREDENTIALS_FILE`.
  Live, but carries no price -- Anthropic's API never returns one.
- codex: live via `omp models openai-codex --json` -- the same `omp` CLI
  quota.py's `quota_usage()` already shells out to, authenticated for this
  Codex Pro subscription. Its models carry their own `cost` fields, so no
  models.dev price lookup is needed for this one. That call itself runs
  ~10.3s in this sandbox (measured with `time`), so it gets a 15s timeout
  (`_OMP_TIMEOUT`) rather than this module's usual 10s. If `omp` is
  missing, times out, exits non-zero, or prints something unparseable,
  `codex_models()` falls back to models.dev's published OpenAI catalog
  instead -- every ID in it is real, but it's OpenAI's general API
  lineup, not a check of what this Codex Pro subscription specifically
  includes. Marked `live: False` in that fallback case.

Baseline price for all three comes from `MODELS_DEV_CATALOG_URL`
(models.dev's public model/price catalog, no auth needed) rather than
guessed numbers, matched by model ID (`anthropic`, `opencode-go`, `openai`
are its provider keys for these three subscriptions). A live model ID with
no match in that catalog gets `price: None` -- never a made-up number.

Both opencode.ai and models.dev sit behind Cloudflare, which blocks
Python's default `Python-urllib/x.y` user agent (confirmed: the exact
request quota.py's `opencode_go_quota()` sends gets a 403 in this sandbox
today -- a pre-existing bug in that function, not something this module
fixes). `_USER_AGENT` below works around it for the requests this module
makes.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

import redis

from . import quota, slots_redis
from .quota import CLAUDE_CREDENTIALS_FILE, OPENCODE_GO_AUTH_FILE, run

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
OPENCODE_GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
MODELS_DEV_CATALOG_URL = "https://models.dev/api.json"

SNAPSHOT_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "lupin",
    "model-snapshot.json",
)

_USER_AGENT = "lupin-model-fetch/1.0"
_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
_DATE_SUFFIX = re.compile(r"-\d{8}$")
REDIS_KEY = f"{slots_redis.PREFIX}model-snapshot"
REDIS_KEY_TTL = 7 * 24 * 60 * 60
_REDIS_ERRORS = redis.exceptions.RedisError


def _client(connection: dict):
    return slots_redis._client(
        connection.get("redis_host"),
        connection.get("redis_port"),
        connection.get("redis_username"),
        connection.get("redis_password"),
    )


def read_shared_snapshot(**connection) -> dict | None:
    """Read the latest model snapshot shared by the fleet, if available."""
    try:
        raw = slots_redis._call_with_retry(lambda: _client(connection).get(REDIS_KEY))
    except _REDIS_ERRORS:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _snapshot_time(snapshot: dict | None) -> datetime | None:
    value = snapshot.get("fetched_at") if isinstance(snapshot, dict) else None
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when


def latest_snapshot(**connection) -> dict | None:
    """Return the newer timestamped snapshot from Redis or disk.

    Use the shared snapshot if neither timestamp is valid.
    """
    shared = read_shared_snapshot(**connection)
    local = _read_local_snapshot(SNAPSHOT_FILE)
    shared_time = _snapshot_time(shared)
    local_time = _snapshot_time(local)

    if shared_time is not None and local_time is not None:
        return local if local_time > shared_time else shared
    if local_time is not None:
        return local
    if shared_time is not None:
        return shared
    if isinstance(shared, dict):
        return shared
    return local if isinstance(local, dict) else None


def publish_snapshot(data: dict, **connection) -> bool:
    """Publish this pull for the dashboard and other fleet machines."""
    try:
        slots_redis._call_with_retry(
            lambda: _client(connection).set(REDIS_KEY, json.dumps(data), ex=REDIS_KEY_TTL)
        )
    except _REDIS_ERRORS:
        return False
    return True


def _get_json(url: str, headers: dict | None = None, timeout: float = 10.0):
    request = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_price_catalog() -> tuple[dict | None, str | None]:
    """models.dev's public catalog: `{provider: {models: {id: {cost: ...}}}}`.

    Returns `(catalog, None)` on success or `(None, error)` on failure --
    callers fall back to `price: None` per model rather than guessing.
    """
    try:
        return _get_json(MODELS_DEV_CATALOG_URL), None
    except (OSError, ValueError, urllib.error.URLError) as error:
        return None, f"unavailable ({type(error).__name__})"


def _catalog_price(catalog: dict | None, provider: str, model_id: str) -> dict | None:
    if not catalog:
        return None
    models = (catalog.get(provider) or {}).get("models") or {}
    entry = models.get(model_id) or models.get(_DATE_SUFFIX.sub("", model_id))
    cost = entry.get("cost") if isinstance(entry, dict) else None
    if not isinstance(cost, dict):
        return None
    return {field: cost[field] for field in _PRICE_FIELDS if field in cost}


def _priced_model(catalog: dict | None, provider: str, model_id: str, display_name: str | None = None) -> dict:
    price = _catalog_price(catalog, provider, model_id)
    model = {
        "id": model_id,
        "price": price,
        "price_source": "models.dev" if price else None,
        "promo": None,
    }
    if display_name:
        model["display_name"] = display_name
    return model


def claude_models(catalog: dict | None = None) -> dict:
    """Live Claude model IDs this OAuth subscription can call today."""
    try:
        with open(CLAUDE_CREDENTIALS_FILE, encoding="utf-8") as credentials_file:
            token = json.load(credentials_file)["claudeAiOauth"]["accessToken"]
        if not isinstance(token, str) or not token:
            raise ValueError("missing OAuth access token")
        data = _get_json(
            ANTHROPIC_MODELS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
            },
        )
    except (OSError, KeyError, TypeError, ValueError, urllib.error.URLError) as error:
        return {
            "subscription": "claude",
            "live": False,
            "error": f"unavailable ({type(error).__name__})",
            "models": [],
        }

    rows = data.get("data") if isinstance(data, dict) else None
    models = [
        _priced_model(catalog, "anthropic", row["id"], row.get("display_name"))
        for row in rows or []
        if isinstance(row, dict) and row.get("id")
    ]
    return {"subscription": "claude", "live": True, "source": ANTHROPIC_MODELS_URL, "models": models}


def opencode_go_models(catalog: dict | None = None) -> dict:
    """Live opencode-go model IDs this subscription can call today."""
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
        key = quota._omp_token("opencode-go")  # module lookup: tests replace it
    if not key:
        return {
            "subscription": "opencode-go",
            "live": False,
            "error": "no OpenCode Go API key available",
            "models": [],
        }

    try:
        data = _get_json(
            OPENCODE_GO_MODELS_URL,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
    except urllib.error.HTTPError as error:
        try:
            body = error.read(4096)
        except (OSError, http.client.HTTPException):
            body = b""
        expired = error.code == 401 and b"api_key_expired" in body
        return {
            "subscription": "opencode-go",
            "live": False,
            "error": quota.OPENCODE_GO_KEY_EXPIRED if expired else f"unavailable ({type(error).__name__})",
            "models": [],
        }
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {
            "subscription": "opencode-go",
            "live": False,
            "error": f"unavailable ({type(error).__name__})",
            "models": [],
        }

    rows = data.get("data") if isinstance(data, dict) else None
    models = [
        _priced_model(catalog, "opencode-go", row["id"])
        for row in rows or []
        if isinstance(row, dict) and row.get("id")
    ]
    return {"subscription": "opencode-go", "live": True, "source": OPENCODE_GO_MODELS_URL, "models": models}


_OMP_COST_FIELDS = {"input": "input", "output": "output", "cacheRead": "cache_read", "cacheWrite": "cache_write"}


def _omp_price(cost) -> dict | None:
    if not isinstance(cost, dict):
        return None
    price = {dest: cost[src] for src, dest in _OMP_COST_FIELDS.items() if src in cost}
    return price or None


_OMP_TIMEOUT = 15.0  # measured: `omp models openai-codex --json` takes ~10.3s in
# this sandbox (4 runs, `time omp models openai-codex --json`), over this
# module's usual 10.0s (`_get_json`'s default) -- so this call gets its own,
# longer, still-bounded timeout instead of that one.


def _omp_codex_models() -> list[dict] | None:
    """Parsed `models` array from `omp models openai-codex --json`, or
    `None` if `omp` is missing, times out, exits non-zero, or prints
    something unparseable -- any of those just means "no live list"."""
    return_code, output = run(["omp", "models", "openai-codex", "--json"], timeout=_OMP_TIMEOUT)
    if return_code != 0:
        return None
    try:
        data = json.loads(output)
    except ValueError:
        return None
    rows = data.get("models") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else None


def codex_models(catalog: dict | None = None) -> dict:
    """Live via `omp models openai-codex --json` when that succeeds --
    see this module's docstring. Falls back to models.dev's published
    OpenAI catalog, marked `live: False`, otherwise.
    """
    rows = _omp_codex_models()
    if rows is not None:
        models = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            price = _omp_price(row.get("cost"))
            model = {
                "id": row["id"],
                "price": price,
                "price_source": "omp" if price else None,
                "promo": None,
            }
            if row.get("name"):
                model["display_name"] = row["name"]
            models.append(model)
        return {
            "subscription": "codex",
            "live": True,
            "source": "omp models openai-codex --json",
            "models": models,
        }

    if not catalog:
        return {
            "subscription": "codex",
            "live": False,
            "error": "models.dev catalog unavailable; no fallback model list either",
            "models": [],
        }
    models_raw = (catalog.get("openai") or {}).get("models") or {}
    models = [
        _priced_model(catalog, "openai", model_id)
        for model_id in models_raw
    ]
    return {
        "subscription": "codex",
        "live": False,
        "stale_reason": (
            "`omp models openai-codex --json` was unavailable (missing binary, not "
            "authenticated, timed out, or bad output) -- these are models.dev's "
            "published OpenAI models instead, not a live check of this subscription"
        ),
        "source": "models.dev (openai)",
        "models": models,
    }


def _read_local_snapshot(path: str = SNAPSHOT_FILE) -> dict | None:
    """The last snapshot this machine saved to disk, or `None`.

    `read_shared_snapshot()` covers a fleet-wide Redis cache; this is the
    same idea for a machine that cannot reach it.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _merge_previous_subscriptions(fresh: dict, previous: dict | None) -> dict:
    """Keep a failed subscription's last known model list, marked stale.

    An expired OAuth token on one machine used to publish
    `claude: {live: false, models: []}` over the whole fleet snapshot, so
    the dashboard's All models table lost every Claude row -- the same
    "degrade, don't erase" rule `quota_cache.py` merges per provider and
    `benchmark_fetch.py` merges per score. A subscription that fetched
    fine today is replaced outright; one that failed keeps the previous
    list, its own old `fetched_at`, and a reason saying today's fetch
    could not verify it. It stays `live: False`: the rows shown are the
    last verified list, not today's.
    """
    if not isinstance(previous, dict):
        return fresh
    previous_subscriptions = previous.get("subscriptions")
    if not isinstance(previous_subscriptions, dict):
        return fresh
    for name, entry in list(fresh.get("subscriptions", {}).items()):
        if entry.get("live") or not isinstance(entry, dict):
            continue
        old = previous_subscriptions.get(name)
        if not isinstance(old, dict) or not old.get("models"):
            continue
        reason = entry.get("error") or entry.get("stale_reason") or "today's fetch failed"
        fresh["subscriptions"][name] = {
            **old,
            "live": False,
            "error": reason,
            "fetched_at": previous.get("fetched_at"),
            "stale_reason": (
                f"not refreshed ({reason}); showing the last verified list"
            ),
        }
    return fresh


def snapshot() -> dict:
    """One day's record: model list + price per subscription.

    `promo` is always `None` right now -- no live source for time-boxed
    discounts turned up in this issue's research (`grep -rn -i
    "promo|discount" src/lupin/*.py` found nothing to build on, and neither
    opencode.ai, Anthropic's API, nor models.dev expose one). The field
    stays in the shape so issue #18 has somewhere real to put it.

    A subscription whose fetch fails keeps its last verified list, merged
    from the fleet cache (or this machine's file when Redis is
    unreachable) -- see `_merge_previous_subscriptions`.
    """
    catalog, catalog_error = fetch_price_catalog()
    fresh = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "price_catalog_source": MODELS_DEV_CATALOG_URL,
        "price_catalog_error": catalog_error,
        "promo_source": None,
        "subscriptions": {
            "claude": claude_models(catalog),
            "opencode-go": opencode_go_models(catalog),
            "codex": codex_models(catalog),
        },
    }
    previous = read_shared_snapshot() or _read_local_snapshot()
    return _merge_previous_subscriptions(fresh, previous)


def save_snapshot(data: dict, path: str = SNAPSHOT_FILE) -> None:
    """Overwrite `path` with `data`, atomically (same pattern as
    roadmap.py's `_persist_cache`). One file, latest fetch only -- a
    history of days is issue #17's concern if it wants one, not this one's.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, delete=False
    ) as handle:
        json.dump(data, handle, indent=2)
        temporary_path = handle.name
    try:
        os.replace(temporary_path, path)
    except OSError:
        os.unlink(temporary_path)
        raise

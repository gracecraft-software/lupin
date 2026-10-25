"""Pick a {model, effort} for a (category, size) pair from model-tiers.json.

Moved from ghostbook.nix's hosts/jesus/loopgui/route.py (issue #203, part of
#198's plan) at source commit 12c007705e63b08c363062487195c8003d5347d3.
The tiers file comes from `LUPIN_MODEL_TIERS` when set. Otherwise, Lupin
uses the bundled resource.

`category`/`size` come from classify.classify() (issue #183). This looks up
the matching row in model-tiers.json (issue #182), turns size into a tier,
and applies three adjustments:

- bmo can cold-sleep for ~300s. A caller that already tried the bmo lock with
  a short timeout (~20-30s) and gave up passes bmo_available=False here. If
  tier0 has a bmo: entry, tier0 is skipped and tier1 is used -- no block
  (#179 research).
- A review call doesn't need to match the primary pass's effort. If the
  primary ran at high/xhigh, the review still gets the tier's base (lowest)
  effort, to ease pressure on shared quota (#179 §4).
- Quota is fleet-wide, not per-machine (every provider is one account shared
  by the whole fleet, confirmed in issue #36) -- so this is also where quota
  pacing happens, via `pace.py`. See `_apply_pacing` below for the three
  rules (reserve, lean-in, exhaustion fallback). A lean-in moves one tier
  up, to an entry on the *same* provider. A surplus is spent on that
  account, never on another account's quota.

This module only decides; it never touches a lock, a scheduler, or a machine
itself.
"""

from __future__ import annotations

import math
import time

from . import model_fetch, pace
from . import model_tiers as tiers_config
from . import quota as quota_mod

# Size -> tier. size-xs and size-s: tier0. size-m: tier1. size-l, size-xl
# and size-?: tier2. size-? is unknown, so it gets tier2, not the cheapest
# tier. See #179 §3.
_SIZE_TO_TIER = {
    "size-xs": "tier0",
    "size-s": "tier0",
    "size-m": "tier1",
    "size-l": "tier2",
    "size-xl": "tier2",
    "size-?": "tier2",
}

_TIER_ORDER = tiers_config.TIER_ORDER


class RouteInputError(ValueError):
    """Invalid route input or model-tier configuration."""


# route()'s models, mapped to the quota-tracked provider that serves them.
# Moved here from place.py (issue #36) -- quota pacing is route()'s concern
# now, so it needs this mapping too. place.py still exposes the same name
# (`provider_for_model`) for its own, unrelated use (labeling a machine's
# quota entry), pointed at this copy instead of keeping its own.
_PROVIDER_BY_MODEL = {
    "sonnet": "claude",
    "opus": "claude",
    "haiku": "claude",
    # fable is served by the claude account. It shares that account's quota
    # with sonnet and opus.
    "fable": "claude",
}


def provider_for_model(model: str) -> str:
    """Map a routed model to the provider `quota.py` tracks it under.

    The prefix is the same one `lupin run`/`lupin enable --orchestrator` use
    for a model selector, so `opencode-go/glm-5.3` is the opencode-go
    subscription's glm-5.3. A bare name (sonnet/opus/haiku/fable) is a Claude
    subscription alias.

    `openai-codex/` is the real prefix for the OpenAI (Codex) subscription,
    so `openai-codex/gpt-6.1-sol` runs on the OpenAI account. `openai/` is
    the accepted alias for the same account (issue #241). Both map to the
    quota provider "openai", which is the name `quota.py` tracks that
    account under.

    "bmo:"/"local:" models run on local/shared-GPU inference, not a cloud
    account with a quota reading -- `pace.py` never has rows for them, so
    they are never blocked. Falls back to the model name itself for
    anything this table doesn't know yet, rather than raising.
    """
    if model.startswith("bmo:"):
        return "bmo"
    if model.startswith("local:"):
        return "local"
    if model.startswith("opencode-go/"):
        return "opencode-go"
    if model.startswith(("openai-codex/", "openai/")):
        return "openai"
    return _PROVIDER_BY_MODEL.get(model, model)


def _load_tiers(source: str | None = None) -> dict:
    try:
        return tiers_config.load(source)
    except tiers_config.ModelTiersError as error:
        raise RouteInputError(str(error)) from error


def _resolve_tier(tiers: dict, tier: str) -> str:
    """Return `tier` if this category has it, else the next higher one.

    Some rows (frontend-ui) skip tier0 on purpose -- see the "note"
    field in model-tiers.json. Escalating up is the safe direction; there is
    no silent downgrade to a cheaper model the matrix didn't ask for.
    """
    for candidate in _TIER_ORDER[_TIER_ORDER.index(tier) :]:
        if candidate in tiers:
            return candidate
    raise KeyError(f"no tier at or above {tier!r} in this row")


def _lean_in_entry(row: dict, tier: str, provider: str) -> dict | None:
    """First entry on `provider` one tier up from `tier`, or None.

    One rung only: the next tier the row has above `tier`. A lean-in must
    stay on one provider, so it never spends another account's quota. None
    means that tier has no entry on `provider`, and the pick stays where it
    is.
    """
    above = [candidate for candidate in _TIER_ORDER[_TIER_ORDER.index(tier) + 1 :] if candidate in row]
    if not above:
        return None
    for entry in row[above[0]]:
        if provider_for_model(entry["model"]) == provider:
            return entry
    return None


def _fallback_entries(
    row: dict,
    tier: str,
    *,
    start_index: int = 1,
    excluded_providers: frozenset[str] | set[str] = frozenset(),
) -> list[dict]:
    """Other entries route() can try after `tier`'s selected entry.

    Order: the rest of `tier`'s list first, then lower tiers (nearest
    first). A higher tier is not a fallback. Skip bmo:/local: entries so a
    blocked cloud provider waits for its reset rather than quietly using a
    free local model.
    """
    tier_index = _TIER_ORDER.index(tier)
    tier_walk = (tier,) + tuple(reversed(_TIER_ORDER[:tier_index]))
    entries = []
    for candidate_tier in tier_walk:
        if candidate_tier not in row:
            continue
        candidate_entries = (
            row[candidate_tier][start_index:]
            if candidate_tier == tier else row[candidate_tier]
        )
        for entry in candidate_entries:
            if provider_for_model(entry["model"]) in excluded_providers:
                continue
            if entry["model"].startswith("bmo:") or entry["model"].startswith("local:"):
                continue
            entries.append(entry)
    return entries


def _initial_entry(
    row: dict,
    tier: str,
    excluded_providers: frozenset[str],
    *,
    bmo_available: bool = True,
) -> tuple[str, int, dict] | None:
    """First allowed entry in `tier`, or the nearest lower tier."""
    tier_index = _TIER_ORDER.index(tier)
    tier_walk = (tier,) + tuple(reversed(_TIER_ORDER[:tier_index]))
    for candidate_tier in tier_walk:
        for index, entry in enumerate(row.get(candidate_tier, [])):
            if provider_for_model(entry["model"]) in excluded_providers:
                continue
            if not bmo_available and entry["model"].startswith("bmo:"):
                continue
            return candidate_tier, index, entry
    return None



def _model_prices(model: str, snapshot: dict | None) -> list[dict] | None:
    fetched_at = model_fetch._snapshot_time(snapshot)
    if fetched_at is None:
        return None
    age_seconds = time.time() - fetched_at.timestamp()
    if not 0 <= age_seconds <= model_fetch.REDIS_KEY_TTL:
        return None

    if model.startswith("opencode-go/"):
        subscription, selector = "opencode-go", model.split("/", 1)[1]
    elif model.startswith(("openai-codex/", "openai/")):
        subscription, selector = "codex", model.split("/", 1)[1]
    elif model.startswith(("bmo:", "local:")):
        return None
    else:
        subscription, selector = "claude", model
    selector = selector.split(":", 1)[0].lower()
    if not selector:
        return None

    subscriptions = snapshot.get("subscriptions")
    if not isinstance(subscriptions, dict):
        return None
    group = subscriptions.get(subscription)
    if not isinstance(group, dict) or group.get("live") is not True:
        return None
    models = group.get("models")
    if not isinstance(models, list):
        return None

    prices = []
    for entry in models:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        model_id = entry["id"].lower()
        matches = selector in model_id if subscription == "claude" else selector == model_id
        if matches:
            prices.append(entry.get("price"))
    if not prices or any(not isinstance(price, dict) for price in prices):
        return None
    return prices


def _fallback_is_not_more_expensive(
    candidate: str, original: str, snapshot: dict | None
) -> bool:
    candidate_prices = _model_prices(candidate, snapshot)
    original_prices = _model_prices(original, snapshot)
    if not candidate_prices or not original_prices:
        return False

    fields = ["input", "output"]
    all_prices = candidate_prices + original_prices
    for field in ("cache_read", "cache_write"):
        if any(field in price for price in all_prices):
            fields.append(field)
    for price in all_prices:
        for field in fields:
            value = price.get(field)
            if isinstance(value, float) and not math.isfinite(value):
                return False
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value < 0
            ):
                return False

    return all(
        max(price[field] for price in candidate_prices)
        <= min(price[field] for price in original_prices)
        for field in fields
    )


def _apply_pacing(
    row: dict,
    tier: str,
    quota_rows: list[dict],
    excluded_providers: frozenset[str] = frozenset(),
    *,
    bmo_available: bool = True,
) -> dict:
    """Apply quota pacing and safe fallback rules.

    A fallback needs a live model snapshot that is no more than seven days
    old. Its input, output, and any listed cache rates must be no higher than
    the blocked pick. Missing or unclear prices do not permit a fallback.

    Returns `{"model", "effort"}` on an ordinary pick, plus
    `"downgraded_from"` when decision 3 moves off a blocked pick, or a
    quota deferral with `wait_seconds` and `reason` when no allowed entry
    has quota. A deferral keeps the blocked model as context; callers must
    not run it.
    """
    selected = _initial_entry(
        row, tier, excluded_providers, bmo_available=bmo_available
    )
    if selected is None:
        providers = ", ".join(sorted(excluded_providers))
        raise RouteInputError(
            f"no model in {tier!r} or a lower tier remains after excluding "
            f"provider(s): {providers}"
        )
    choice_tier, choice_index, choice = selected
    if not quota_rows:
        return {"model": choice["model"], "effort": choice["effort"]}

    now_ms = time.time() * 1000
    provider = provider_for_model(choice["model"])
    is_blocked = pace.blocked(quota_rows, provider, now_ms)

    if not is_blocked and pace.lean_in(quota_rows, provider, now_ms):
        # `_lean_in_entry` only ever returns an entry on `provider` itself, so
        # the block check above already covers the rung it moves to; nothing
        # to re-check here.
        lean_choice = _lean_in_entry(row, choice_tier, provider)
        if lean_choice is not None:
            choice = lean_choice

    if not is_blocked:
        return {"model": choice["model"], "effort": choice["effort"]}

    blocked_choice = {"model": choice["model"], "effort": choice["effort"]}
    fallback_entries = _fallback_entries(
        row,
        choice_tier,
        start_index=choice_index + 1,
        excluded_providers=excluded_providers,
    )
    snapshot = model_fetch.latest_snapshot() if fallback_entries else None
    examined = [provider]
    for entry in fallback_entries:
        if not _fallback_is_not_more_expensive(
            entry["model"], blocked_choice["model"], snapshot
        ):
            continue
        entry_provider = provider_for_model(entry["model"])
        if pace.blocked(quota_rows, entry_provider, now_ms):
            examined.append(entry_provider)
            continue
        return {
            "model": entry["model"],
            "effort": entry["effort"],
            "downgraded_from": blocked_choice,
        }

    earliest = pace.earliest_reset(quota_rows, examined, now_ms)
    wait_seconds = max(0.0, (earliest - now_ms) / 1000) if earliest is not None else None
    return {
        **blocked_choice,
        "wait_seconds": wait_seconds,
        "reason": "all allowed models are blocked by quota",
    }


def route(
    category: str,
    size: str,
    *,
    bmo_available: bool = True,
    primary_effort: str | None = None,
    tiers: dict | None = None,
    quota_rows: list[dict] | None = None,
    excluded_providers: list[str] | tuple[str, ...] | set[str] | frozenset[str] = (),
) -> dict:
    """Return a model and effort, or a quota deferral with its reason.

    `bmo_available=False` models a timed-out bmo lock: if tier0 has a bmo:
    entry, skip tier0 and use tier1. `primary_effort` is the primary
    pass's effort level ("low"/"medium"/"high"/"xhigh"); it is accepted so
    a caller can log or assert on it, but it never escalates the result --
    a review call always gets the tier's base effort, never matching a
    high/xhigh primary. That de-escalation is the point (#179 §4), not an
    edge case.

    `excluded_providers` removes providers from the selected tier and lower
    tiers. It never upgrades to a higher tier. Unknown providers are rejected.
    `quota_rows` is `quota.quota_usage()`'s row shape. `None` (the default)
    reads live quota for real; pass `[]` for "no data, don't change
    anything" (every pre-#36 test does this, to keep testing this
    function's tier/bmo logic undisturbed by live quota). See
    `_apply_pacing` for the quota behavior.

    Raises `RouteInputError` for a bad route input or model-tier file.
    """
    source = tiers_config.path() if tiers is None else None
    all_tiers = tiers if tiers is not None else _load_tiers(source)
    # Keys that start with "_" (like "_comment") are notes, not categories.
    valid = sorted(key for key in all_tiers if not key.startswith("_"))
    if category not in valid:
        location = f" in {source!r}" if source is not None else ""
        raise RouteInputError(
            f"unknown category {category!r}{location}; valid: {', '.join(valid)}"
        )
    if size not in _SIZE_TO_TIER:
        raise RouteInputError(f"unknown size {size!r}; valid: {', '.join(_SIZE_TO_TIER)}")
    excluded = {provider.strip().lower() for provider in excluded_providers}
    if "" in excluded:
        raise RouteInputError("excluded provider cannot be empty")
    if excluded:
        known_providers = {
            provider_for_model(entry["model"])
            for name in valid
            for entries in all_tiers[name]["tiers"].values()
            for entry in entries
        }
        unknown = excluded - known_providers
        if unknown:
            raise RouteInputError(
                f"unknown excluded provider(s) {', '.join(sorted(unknown))!r}; "
                f"valid: {', '.join(sorted(known_providers))}"
            )
    row = all_tiers[category]["tiers"]

    needed_tier = _SIZE_TO_TIER[size]
    try:
        tier = _resolve_tier(row, needed_tier)
        if (
            tier == "tier0"
            and not bmo_available
            and any(entry["model"].startswith("bmo:") for entry in row["tier0"])
        ):
            needed_tier = "tier1"
            tier = _resolve_tier(row, needed_tier)
    except KeyError as error:
        location = f" in {source!r}" if source is not None else ""
        raise RouteInputError(
            f"category {category!r}{location} has no tier at or above {needed_tier!r}"
        ) from error

    live_quota_rows = quota_mod.quota_usage() if quota_rows is None else quota_rows
    return _apply_pacing(
        row, tier, live_quota_rows, frozenset(excluded), bmo_available=bmo_available
    )

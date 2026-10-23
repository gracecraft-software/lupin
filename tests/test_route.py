import copy
import itertools
import json
import time
import unittest
from datetime import datetime, timezone
from importlib import resources
from unittest import mock

from lupin import route
from lupin.quota import QuotaDuration


def _price_snapshot(tiers: dict | None = None) -> dict:
    if tiers is None:
        tiers = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
    subscriptions = {"claude": [], "opencode-go": [], "codex": []}
    seen = {name: set() for name in subscriptions}
    for category, row in tiers.items():
        if category.startswith("_"):
            continue
        for entries in row["tiers"].values():
            for entry in entries:
                model = entry["model"]
                if model.startswith("opencode-go/"):
                    subscription, model_id = "opencode-go", model.split("/", 1)[1]
                elif model.startswith(("openai-codex/", "openai/")):
                    subscription, model_id = "codex", model.split("/", 1)[1]
                elif model.startswith(("bmo:", "local:")):
                    continue
                else:
                    subscription, model_id = "claude", f"claude-{model}"
                model_id = model_id.split(":", 1)[0]
                if model_id not in seen[subscription]:
                    seen[subscription].add(model_id)
                    subscriptions[subscription].append(
                        {"id": model_id, "price": {"input": 1, "output": 1}}
                    )
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "subscriptions": {
            name: {"live": True, "models": models}
            for name, models in subscriptions.items()
        },
    }


def _route_with_snapshot(category, size, snapshot, *, tiers=None, **kwargs):
    snapshot = copy.deepcopy(snapshot)
    if isinstance(snapshot, dict):
        snapshot.setdefault("fetched_at", datetime.now(timezone.utc).isoformat())
        subscriptions = snapshot.get("subscriptions")
        if isinstance(subscriptions, dict):
            for group in subscriptions.values():
                if isinstance(group, dict):
                    group.setdefault("live", True)
    with mock.patch.object(
        route.model_fetch, "latest_snapshot", return_value=snapshot
    ):
        return route.route(category, size, tiers=tiers, **kwargs)


def _route_with_prices(category, size, *, tiers=None, **kwargs):
    return _route_with_snapshot(
        category, size, _price_snapshot(tiers), tiers=tiers, **kwargs
    )


HOUR_MS = 60 * 60 * 1000
MIN_MS = 60 * 1000

_PROVIDERS = ("claude", "opencode-go", "openai")
_SIZES = ("size-xs", "size-s", "size-m", "size-l", "size-xl", "size-?")
_STATES = ("normal", "blocked", "surplus")


def _grid_rows(states):
    """quota_rows for one grid point: `states` gives each provider's state."""
    now_ms = time.time() * 1000
    rows = []
    for provider, state in zip(_PROVIDERS, states):
        if state == "blocked":
            rows.append({"provider": provider, "duration": QuotaDuration.FIVE_HOURS,
                         "used_pct": 100, "resets_at": now_ms + 10 * HOUR_MS})
        elif state == "surplus":
            rows.append({"provider": provider, "duration": QuotaDuration.FIVE_HOURS,
                         "used_pct": 10, "resets_at": now_ms + HOUR_MS})
    return rows


_TIERS = {
    "coding": {
        "tiers": {
            "tier0": [
                {"model": "bmo:qwen3.8-flash-next", "effort": "low"},
                {"model": "local:deepseek-v4-flash-0731", "effort": "low"},
            ],
            "tier1": [
                {"model": "sonnet", "effort": "medium"},
                {"model": "sonnet", "effort": "high"},
            ],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "opus", "effort": "xhigh"},
            ],
        }
    },
    "frontend-ui": {
        "tiers": {
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "opus", "effort": "xhigh"},
            ],
        }
    },
    "translation": {
        "tiers": {
            "tier0": [{"model": "local:deepseek-v4-flash-0731", "effort": "medium"}],
            "tier1": [{"model": "sonnet", "effort": "medium"}],
            "tier2": [{"model": "opus", "effort": "high"}],
        }
    },
    "cad-spatial": {
        "tiers": {
            # Mirrors cad-spatial's real shape in model-tiers.json: local is
            # listed first, bmo second -- the opposite order from "coding"/
            # "general". A bmo-dependency check that only looks at index 0
            # would miss this row entirely.
            "tier0": [
                {"model": "local:deepseek-v4-flash-0731", "effort": "medium"},
                {"model": "bmo:qwen3.8-flash-next", "effort": "medium"},
            ],
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [{"model": "opus", "effort": "high"}],
        }
    },
    "prose": {
        "tiers": {
            # tier2 lists opus and fable. Both run on the claude account.
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "fable", "effort": "high"},
            ],
        }
    },
}

# prose with a second account in tier2. The fallback tests inject this row.
_PROSE_OTHER_ACCOUNT = {
    "prose": {
        "tiers": {
            "tier1": [{"model": "sonnet", "effort": "high"}],
            "tier2": [
                {"model": "opus", "effort": "high"},
                {"model": "opencode-go/glm-5.3", "effort": "high"},
            ],
        }
    },
}


class RouteTests(unittest.TestCase):
    def test_unknown_category_raises_naming_the_valid_ones(self):
        with self.assertRaisesRegex(ValueError, "unknown category 'code'.*coding"):
            route.route("code", "size-m", tiers=_TIERS, quota_rows=[])

        # The packaged model-tiers.json has a "_comment" note key. It is not a category.
        with self.assertRaisesRegex(ValueError, "unknown category '_comment'") as caught:
            route.route("_comment", "size-m", quota_rows=[])
        self.assertNotIn("valid: _comment", str(caught.exception))

        with self.assertRaisesRegex(route.RouteInputError, "unknown size 'm'.*size-m"):
            route.route("coding", "m", tiers=_TIERS, quota_rows=[])

    def test_small_coding_issue_gets_tier0_bmo(self):
        result = route.route("coding", "size-xs", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "bmo:qwen3.8-flash-next", "effort": "low"})

    def test_medium_coding_issue_gets_tier1_sonnet(self):
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_large_coding_issue_gets_tier2_opus(self):
        result = route.route("coding", "size-l", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_xl_coding_issue_gets_tier2_opus(self):
        result = route.route("coding", "size-xl", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_translation_lookup_uses_its_own_row(self):
        result = route.route("translation", "size-xs", tiers=_TIERS, quota_rows=[])

        self.assertEqual(
            result, {"model": "local:deepseek-v4-flash-0731", "effort": "medium"}
        )

    def test_no_tier0_row_escalates_small_issue_to_tier1(self):
        # frontend-ui has no tier0 -- a small UI issue still lands on tier1,
        # not a KeyError and not a silent drop to a cheaper, nonexistent tier.
        result = route.route("frontend-ui", "size-xs", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "sonnet", "effort": "high"})

    def test_bmo_timeout_falls_back_to_tier1_not_local(self):
        # Small/mechanical coding issue would normally get bmo (tier0). A
        # timed-out bmo lock skips tier0 entirely and lands on tier1 Sonnet,
        # per #179 -- not on tier0's own local fallback entry.
        result = route.route(
            "coding", "size-xs", bmo_available=False, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_bmo_available_keeps_tier0_pick(self):
        result = route.route(
            "coding", "size-xs", bmo_available=True, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "bmo:qwen3.8-flash-next", "effort": "low"})

    def test_non_bmo_tier0_is_unaffected_by_bmo_availability(self):
        # translation's tier0 is local-only; a bmo timeout has nothing to do
        # with it, so it should not get bumped to tier1.
        result = route.route(
            "translation", "size-xs", bmo_available=False, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(
            result, {"model": "local:deepseek-v4-flash-0731", "effort": "medium"}
        )

    def test_bmo_timeout_falls_back_even_when_bmo_is_not_tier0s_first_entry(self):
        # Regression for the index-0 bug: cad-spatial's tier0 lists local
        # first and bmo second. A check that only looked at row["tier0"][0]
        # would see "local:..." and wrongly conclude this row has no bmo
        # dependency, skipping the fallback and returning a bmo model
        # anyway. It must still fall back to tier1 here.
        result = route.route(
            "cad-spatial", "size-xs", bmo_available=False, tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "high"})

    def test_high_effort_primary_does_not_escalate_the_review(self):
        # The primary pass already ran at "high". The review still gets
        # tier1's base entry (medium), not the high-effort entry -- it
        # doesn't need to match, by design (#179 §4).
        result = route.route(
            "coding", "size-m", primary_effort="high", tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})

    def test_xhigh_effort_primary_does_not_escalate_the_review(self):
        result = route.route(
            "coding", "size-l", primary_effort="xhigh", tiers=_TIERS, quota_rows=[]
        )

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_unknown_size_defaults_to_tier2(self):
        # Unknown is the least certain case -- it gets the most capable
        # model, not the cheapest. This was a bug (silently landed on tier1)
        # fixed independently of the tier0-widening question below.
        result = route.route("coding", "size-?", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_loads_the_real_model_tiers_json(self):
        # No injected tiers dict -- confirms the default path actually
        # resolves to the packaged model-tiers.json.
        result = route.route("coding", "size-m", quota_rows=[])

        self.assertEqual(result, {"model": "opencode-go/glm-5.3", "effort": "high"})


class ProviderForModelTests(unittest.TestCase):
    """Which quota provider a routed model runs on (issue #241)."""

    def test_the_codex_prefix_maps_to_the_openai_provider(self):
        # "openai-codex/" is the real prefix for the OpenAI (Codex)
        # subscription. It has to land on the "openai" provider, the name
        # quota.py tracks that account under.
        self.assertEqual(route.provider_for_model("openai-codex/gpt-6.1-sol"), "openai")
        self.assertEqual(route.provider_for_model("openai-codex/gpt-5.6-terra"), "openai")

    def test_the_openai_alias_maps_to_the_same_provider(self):
        self.assertEqual(route.provider_for_model("openai/gpt-6-luna"), "openai")


class RoutePacingTests(unittest.TestCase):
    """Issue #36: quota is fleet-wide, so route() paces on it directly.
    Grace's three literal rules (see pace.py), exercised through route()'s
    own tier/bmo logic -- test_pace.py covers the rules themselves.
    """

    def _row(self, provider, used_pct, resets_in_ms, duration=QuotaDuration.FIVE_HOURS):
        # route() uses the real clock internally (it has no `now` override),
        # so fixtures here anchor `resets_at` to the real current time too.
        return {
            "provider": provider,
            "duration": duration,
            "used_pct": used_pct,
            "resets_at": time.time() * 1000 + resets_in_ms,
        }

    def test_blocked_provider_falls_back_to_next_tiers_other_provider(self):
        # The injected tier2 lists opus (claude) then opencode-go/glm-5.3.
        # Claude blocked -> falls to the opencode-go model, not a wait and not
        # a local/bmo model (prose's tier1 has no local/bmo entry anyway, but
        # this exercises the "other entry in the same tier" branch either
        # way).
        rows = [self._row("claude", 100, 10 * HOUR_MS)]
        result = _route_with_prices("prose", "size-l", tiers=_PROSE_OTHER_ACCOUNT, quota_rows=rows)

        self.assertEqual(result["model"], "opencode-go/glm-5.3")
        self.assertEqual(result["downgraded_from"], {"model": "opus", "effort": "high"})

    def test_a_codex_tier_entry_is_blocked_when_the_openai_quota_is_blocked(self):
        # "openai-codex/" models run on the OpenAI account, so a blocked
        # openai quota blocks them too. The pick must move off the codex
        # model instead of running it while the account is out of quota.
        tiers = {
            "coding": {
                "tiers": {
                    "tier2": [
                        {"model": "openai-codex/gpt-6.1-sol", "effort": "high"},
                        {"model": "opus", "effort": "high"},
                    ],
                }
            }
        }
        rows = [self._row("openai", 100, 10 * HOUR_MS)]
        result = _route_with_prices("coding", "size-l", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "opus")
        self.assertEqual(
            result["downgraded_from"],
            {"model": "openai-codex/gpt-6.1-sol", "effort": "high"},
        )

    def test_a_blocked_codex_tier_entry_waits_when_no_other_provider_is_open(self):
        # Every provider is blocked, so there is nothing to fall to. The
        # route reports a wait on the codex model. It must not hand back a
        # model it knows is blocked.
        tiers = {
            "coding": {
                "tiers": {
                    "tier2": [
                        {"model": "openai-codex/gpt-6.1-sol", "effort": "high"},
                        {"model": "opus", "effort": "high"},
                    ],
                }
            }
        }
        rows = [
            self._row("openai", 100, 10 * HOUR_MS),
            self._row("claude", 100, 10 * HOUR_MS),
        ]
        result = _route_with_prices("coding", "size-l", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "openai-codex/gpt-6.1-sol")
        self.assertNotIn("downgraded_from", result)
        self.assertIsNotNone(result["wait_seconds"])

    def test_fable_on_a_blocked_claude_account_is_not_a_fallback(self):
        # fable is served by the claude account, the same account as opus.
        # Claude blocked -> no other account to fall to. Route must report a
        # wait on opus. It must not return fable.
        rows = [self._row("claude", 100, 10 * HOUR_MS)]
        tiers = {
            "prose": {
                "tiers": {
                    "tier1": [{"model": "sonnet", "effort": "high"}],
                    "tier2": [
                        {"model": "opus", "effort": "high"},
                        {"model": "fable", "effort": "high"},
                    ],
                }
            }
        }
        result = _route_with_prices("prose", "size-l", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "opus")
        self.assertNotIn("downgraded_from", result)
        self.assertIsNotNone(result["wait_seconds"])

    def test_blocked_tier1_does_not_fall_back_to_a_tier2_on_the_same_blocked_account(self):
        # The bug this issue fixes: coding's tier1 (sonnet) and tier2 (opus)
        # are both "claude". A naive one-tier-down drop (the old
        # quota_exhausted behavior) would have landed on tier2's opus --
        # still claude, still blocked. The new fallback must recognize that
        # and keep searching instead of silently returning a blocked model.
        rows = [self._row("claude", 100, 10 * HOUR_MS)]
        result = _route_with_prices("coding", "size-m", tiers=_TIERS, quota_rows=rows)

        # coding has no other provider at any tier in this fixture -- every
        # candidate is claude, so route() must report a wait, not a model
        # it knows is still blocked.
        self.assertEqual(result["model"], "sonnet")
        self.assertIn("wait_seconds", result)
        self.assertIsNotNone(result["wait_seconds"])

    def test_weekly_claude_quota_without_reset_is_still_blocked(self):
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [{"model": "sonnet", "effort": "high"}],
                }
            }
        }
        rows = [{
            "provider": "claude",
            "duration": QuotaDuration.WEEKLY,
            "used_pct": 91,
            "resets_at": None,
        }]

        result = _route_with_prices("coding", "size-m", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "sonnet")
        self.assertEqual(result["reason"], "all allowed models are blocked by quota")
        self.assertIsNone(result["wait_seconds"])

    def test_wait_reports_the_earliest_reset_among_everything_tried(self):
        rows = [
            self._row("claude", 100, 5 * HOUR_MS),
            self._row("opencode-go", 100, 20 * MIN_MS),
        ]
        result = _route_with_prices("prose", "size-l", tiers=_PROSE_OTHER_ACCOUNT, quota_rows=rows)

        # opus (claude) is blocked, falls to opencode-go/glm-5.3 -- also
        # blocked, resetting sooner than claude. The wait must reflect that
        # sooner reset, not claude's (seen first).
        self.assertAlmostEqual(result["wait_seconds"], 20 * 60, delta=2)

    def test_same_tier_fallback_must_not_cost_more(self):
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [
                        {"model": "opencode-go/glm-5.3", "effort": "high"},
                        {"model": "sonnet", "effort": "high"},
                    ]
                }
            }
        }
        snapshot = {
            "subscriptions": {
                "opencode-go": {
                    "models": [
                        {"id": "glm-5.3", "price": {"input": 0.5, "output": 2}}
                    ]
                },
                "claude": {
                    "models": [
                        {
                            "id": "claude-sonnet-5-5",
                            "price": {"input": 3, "output": 15},
                        }
                    ]
                },
            }
        }
        rows = [self._row("opencode-go", 100, HOUR_MS)]

        result = _route_with_snapshot(
            "coding", "size-m", snapshot, tiers=tiers, quota_rows=rows
        )

        self.assertEqual(result["model"], "opencode-go/glm-5.3")
        self.assertIsNotNone(result["wait_seconds"])

    def test_cheaper_same_tier_fallback_remains_available(self):
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [
                        {"model": "sonnet", "effort": "high"},
                        {"model": "opencode-go/glm-5.3", "effort": "high"},
                    ]
                }
            }
        }
        snapshot = {
            "subscriptions": {
                "opencode-go": {
                    "models": [
                        {"id": "glm-5.3", "price": {"input": 0.5, "output": 2}}
                    ]
                },
                "claude": {
                    "models": [
                        {
                            "id": "claude-sonnet-5-5",
                            "price": {"input": 3, "output": 15},
                        }
                    ]
                },
            }
        }
        rows = [self._row("claude", 100, HOUR_MS)]

        result = _route_with_snapshot(
            "coding", "size-m", snapshot, tiers=tiers, quota_rows=rows
        )

        self.assertEqual(result["model"], "opencode-go/glm-5.3")

    def test_missing_stale_unverified_or_invalid_prices_cause_a_deferral(self):
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [
                        {"model": "opencode-go/glm-5.3", "effort": "high"},
                        {"model": "sonnet", "effort": "high"},
                    ]
                }
            }
        }
        stale = _price_snapshot(tiers)
        stale["fetched_at"] = "2000-01-01T00:00:00+00:00"
        unverified = _price_snapshot(tiers)
        unverified["subscriptions"]["opencode-go"]["live"] = False
        invalid = _price_snapshot(tiers)
        invalid["subscriptions"]["claude"]["models"][0]["price"]["input"] = float(
            "nan"
        )
        snapshots = {
            "missing": {},
            "stale": stale,
            "unverified": unverified,
            "invalid": invalid,
        }
        rows = [self._row("opencode-go", 100, HOUR_MS)]

        for state, snapshot in snapshots.items():
            with self.subTest(state=state):
                result = _route_with_snapshot(
                    "coding", "size-m", snapshot, tiers=tiers, quota_rows=rows
                )

                self.assertEqual(result["model"], "opencode-go/glm-5.3")
                self.assertIsNotNone(result["wait_seconds"])

    def test_claude_weekly_over_90_uses_open_lower_tier_model(self):
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [{"model": "openai-codex/gpt-6-luna", "effort": "high"}],
                    "tier2": [{"model": "opus", "effort": "high"}],
                }
            }
        }
        rows = [
            self._row("claude", 91, 6 * 24 * HOUR_MS, QuotaDuration.WEEKLY),
            self._row("openai", 10, 6 * 24 * HOUR_MS, QuotaDuration.WEEKLY),
        ]

        result = _route_with_prices("coding", "size-l", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "openai-codex/gpt-6-luna")
        self.assertEqual(
            result["downgraded_from"], {"model": "opus", "effort": "high"}
        )

    def test_blocked_route_does_not_fall_back_to_a_higher_tier(self):
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [{"model": "sonnet", "effort": "high"}],
                    "tier2": [{"model": "opencode-go/qwen3.8-max", "effort": "high"}],
                }
            }
        }
        rows = [
            self._row("claude", 100, 6 * 24 * HOUR_MS, QuotaDuration.WEEKLY),
            self._row("opencode-go", 10, 6 * 24 * HOUR_MS, QuotaDuration.WEEKLY),
        ]

        result = _route_with_prices("coding", "size-m", tiers=tiers, quota_rows=rows)

        self.assertEqual(result["model"], "sonnet")
        self.assertIsNotNone(result["wait_seconds"])
        self.assertEqual(result["reason"], "all allowed models are blocked by quota")

    def test_does_not_fall_back_to_a_local_or_bmo_model_to_dodge_a_wait(self):
        # coding's tier0 is bmo/local, tier1 sonnet (claude), both blocked.
        # Decision 3 forbids landing on tier0 to avoid the wait.
        rows = [self._row("claude", 100, 3 * HOUR_MS)]
        result = _route_with_prices("coding", "size-m", tiers=_TIERS, quota_rows=rows)

        self.assertNotIn("bmo:", result["model"])
        self.assertNotIn("local:", result["model"])
        self.assertIsNotNone(result.get("wait_seconds"))

    def test_lean_in_bumps_one_tier_on_a_surplus(self):
        rows = [self._row("claude", 10, 1 * HOUR_MS)]  # <50% used, <24h left
        result = _route_with_prices("coding", "size-m", tiers=_TIERS, quota_rows=rows)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_lean_in_under_30_minutes_to_reset_moves_up_on_the_same_provider(self):
        # 10% used with 20 minutes to reset: under the 30-minute rule, so the
        # pick leans in. The higher entry must be on the same provider, so
        # sonnet (claude) is skipped and qwen3.8-max is picked.
        tiers = {
            "coding": {
                "tiers": {
                    "tier1": [{"model": "opencode-go/glm-5.3", "effort": "high"}],
                    "tier2": [
                        {"model": "sonnet", "effort": "high"},
                        {"model": "opencode-go/qwen3.8-max", "effort": "high"},
                    ],
                }
            }
        }
        rows = [self._row("opencode-go", 10, 20 * 60 * 1000)]
        result = _route_with_prices("coding", "size-m", tiers=tiers, quota_rows=rows)

        self.assertEqual(result, {"model": "opencode-go/qwen3.8-max", "effort": "high"})

    def test_lean_in_moves_one_tier_only(self):
        # tier2 has an opencode-go entry, tier1 has none. One rung up finds
        # nothing on opencode-go, so the pick stays on tier0's entry.
        tiers = {
            "coding": {
                "tiers": {
                    "tier0": [{"model": "opencode-go/glm-5.3", "effort": "low"}],
                    "tier1": [{"model": "sonnet", "effort": "high"}],
                    "tier2": [{"model": "opencode-go/qwen3.8-max", "effort": "high"}],
                }
            }
        }
        rows = [self._row("opencode-go", 10, 20 * 60 * 1000)]
        result = _route_with_prices("coding", "size-xs", tiers=tiers, quota_rows=rows)

        self.assertEqual(result, {"model": "opencode-go/glm-5.3", "effort": "low"})

    def test_lean_in_never_goes_past_the_top_tier(self):
        rows = [self._row("claude", 10, 1 * HOUR_MS)]
        result = _route_with_prices("coding", "size-l", tiers=_TIERS, quota_rows=rows)

        self.assertEqual(result, {"model": "opus", "effort": "high"})

    def test_lean_in_and_fallback_stay_on_the_account_with_quota(self):
        # Quota is per account. A surplus leans only to a higher rung on the
        # same provider. A blocked provider falls to an entry on another one.
        glm = {"model": "opencode-go/glm-5.3", "effort": "high"}
        cases = {
            "surplus leans to the next rung on the same provider": (
                self._row("opencode-go", 10, 1 * HOUR_MS),
                [glm],
                [
                    {"model": "sonnet", "effort": "high"},
                    {"model": "opencode-go/qwen3.8-max", "effort": "high"},
                ],
                {"model": "opencode-go/qwen3.8-max", "effort": "high"},
            ),
            "surplus never leans onto another provider": (
                self._row("opencode-go", 10, 1 * HOUR_MS),
                [glm],
                [
                    {"model": "sonnet", "effort": "high"},
                    {"model": "opus", "effort": "high"},
                ],
                glm,
            ),
            "blocked provider defers instead of using a higher tier": (
                self._row("opencode-go", 100, 10 * HOUR_MS),
                [glm],
                [
                    {"model": "sonnet", "effort": "high"},
                    {"model": "opus", "effort": "high"},
                ],
                {
                    "model": glm["model"],
                    "effort": "high",
                    "reason": "all allowed models are blocked by quota",
                },
            ),
        }
        for name, (row, tier1, tier2, expected) in cases.items():
            with self.subTest(case=name):
                tiers = {"coding": {"tiers": {"tier1": tier1, "tier2": tier2}}}
                result = _route_with_prices("coding", "size-m", tiers=tiers, quota_rows=[row])

                self.assertEqual({key: result.get(key) for key in expected}, expected)
                if "reason" in expected:
                    self.assertIsNotNone(result.get("wait_seconds"))

    def test_empty_quota_rows_is_a_no_op(self):
        result = route.route("coding", "size-m", tiers=_TIERS, quota_rows=[])

        self.assertEqual(result, {"model": "sonnet", "effort": "medium"})


_OPENAI_THEN_CLAUDE = {
    "coding": {
        "tiers": {
            "tier1": [{"model": "openai/gpt-5", "effort": "high"}],
            "tier2": [{"model": "opus", "effort": "high"}],
        }
    },
}

_OPENAI_LEAN_IN = {
    "coding": {
        "tiers": {
            "tier1": [{"model": "openai/gpt-5", "effort": "high"}],
            "tier2": [{"model": "openai/gpt-5-pro", "effort": "high"}],
        }
    },
}


class RouteMultiAccountTests(unittest.TestCase):
    """One provider with two logins, for example a free and a paid OpenAI
    account. `omp` picks the login for each session, so route() treats the
    provider as blocked only when every login is blocked. test_pace.py
    covers the rules themselves.
    """

    def _row(self, account, used_pct, resets_in_ms, duration=QuotaDuration.FIVE_HOURS, provider="openai"):
        row = {
            "provider": provider,
            "duration": duration,
            "used_pct": used_pct,
            "resets_at": time.time() * 1000 + resets_in_ms,
        }
        if account:
            row["account"] = account
        return row

    def _free_used_up(self):
        return self._row("free", 100, 20 * 24 * HOUR_MS, QuotaDuration.MONTHLY)

    def test_a_used_up_login_does_not_move_the_pick_off_its_provider(self):
        rows = [self._free_used_up(), self._row("plus", 60, 4 * HOUR_MS)]
        result = _route_with_prices("coding", "size-m", tiers=_OPENAI_THEN_CLAUDE, quota_rows=rows)

        self.assertEqual(result, {"model": "openai/gpt-5", "effort": "high"})

    def test_every_login_blocked_defers_instead_of_upgrading(self):
        rows = [self._free_used_up(), self._row("plus", 100, 4 * HOUR_MS)]
        result = _route_with_prices("coding", "size-m", tiers=_OPENAI_THEN_CLAUDE, quota_rows=rows)

        self.assertEqual(result["model"], "openai/gpt-5")
        self.assertEqual(result["reason"], "all allowed models are blocked by quota")
        self.assertIsNotNone(result["wait_seconds"])

    def test_wait_uses_the_soonest_reset_of_any_login(self):
        rows = [
            self._free_used_up(),
            self._row("plus", 100, 4 * HOUR_MS),
            self._row(None, 100, 10 * HOUR_MS, provider="claude"),
        ]
        result = _route_with_prices("coding", "size-m", tiers=_OPENAI_THEN_CLAUDE, quota_rows=rows)

        self.assertEqual(result["model"], "openai/gpt-5")
        self.assertAlmostEqual(result["wait_seconds"], 4 * 60 * 60, delta=2)

    def test_lean_in_follows_the_login_that_is_not_blocked(self):
        # plus: 10% used, 20 minutes to reset -> surplus. free is used up,
        # and its 30-day window must not hide that.
        rows = [self._free_used_up(), self._row("plus", 10, 20 * MIN_MS)]
        result = route.route("coding", "size-m", tiers=_OPENAI_LEAN_IN, quota_rows=rows)

        self.assertEqual(result, {"model": "openai/gpt-5-pro", "effort": "high"})

    def test_lean_in_needs_every_open_login_to_have_a_surplus(self):
        rows = [self._row("a", 10, 20 * MIN_MS), self._row("b", 80, 4 * HOUR_MS)]
        result = route.route("coding", "size-m", tiers=_OPENAI_LEAN_IN, quota_rows=rows)

        self.assertEqual(result, {"model": "openai/gpt-5", "effort": "high"})


class PackagedTiersTests(unittest.TestCase):
    def test_size_m_coding_with_readable_quota_picks_opencode_go_high(self):
        # Check 1 (issue #99): both accounts readable and unpressed, so no
        # block and no lean-in (resets are 2 days out). The pick is tier1's
        # first entry, which must be the opencode-go row.
        reset_ms = time.time() * 1000 + 2 * 24 * HOUR_MS
        rows = [
            {"provider": "claude", "duration": QuotaDuration.FIVE_HOURS,
             "used_pct": 10, "resets_at": reset_ms},
            {"provider": "opencode-go", "duration": QuotaDuration.FIVE_HOURS,
             "used_pct": 10, "resets_at": reset_ms},
        ]
        result = route.route("coding", "size-m", quota_rows=rows)

        self.assertEqual(result, {"model": "opencode-go/glm-5.3", "effort": "high"})

    def test_every_packaged_tier_entry_can_be_picked(self):
        # An entry route() never returns is dead config. Give each entry a
        # unique effort tag, run the whole quota grid, and require every tag
        # to show up as a pick or as a downgraded_from.
        packaged = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
        states = itertools.product(_STATES, repeat=len(_PROVIDERS))
        grid = list(itertools.product(_SIZES, (True, False), states))
        for category, row in packaged.items():
            if category.startswith("_"):
                continue
            with self.subTest(category=category):
                probed = copy.deepcopy(row)
                probes = {}
                for tier_name, entries in probed["tiers"].items():
                    # A later entry on a provider that an earlier entry in
                    # the same tier already uses shares its blocked state,
                    # so route() never returns it. Probe only the first.
                    providers = set()
                    for index, entry in enumerate(entries):
                        provider = route.provider_for_model(entry["model"])
                        if provider in providers:
                            continue
                        providers.add(provider)
                        entry["effort"] = f"__probe_{tier_name}_{index}__"
                        probes[entry["effort"]] = entry["model"]
                seen = set()
                seen_models = set()
                for size, bmo, states in grid:
                    result = _route_with_prices(
                        category,
                        size,
                        bmo_available=bmo,
                        tiers={category: probed},
                        quota_rows=_grid_rows(states),
                    )
                    seen.add(result.get("effort"))
                    seen_models.add(result.get("model"))
                    if "downgraded_from" in result:
                        seen.add(result["downgraded_from"]["effort"])
                        seen_models.add(result["downgraded_from"]["model"])

                if category == "prose":
                    # digest.py reads the prose tier0 order itself, so an entry
                    # there can be live even when route() never picks it.
                    from lupin import digest

                    seen_models.update(model for model, _effort in digest._error_chain())

                missing = {
                    tag for tag, model in probes.items()
                    if tag not in seen and model not in seen_models
                }
                self.assertEqual(missing, set())

    def test_packaged_tiers_never_list_fable(self):
        # Fable needs the user's approval (delegation-loop SKILL.md). No
        # packaged tier may list it, so no route can pick it. Issue #114.
        packaged = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
        for category, row in packaged.items():
            if category.startswith("_"):
                continue
            for tier_name, entries in row["tiers"].items():
                for entry in entries:
                    self.assertNotEqual(entry["model"], "fable", f"{category} {tier_name}")

    def test_packaged_tiers_offer_a_codex_model_in_every_category(self):
        # Issue #241: every category has an openai-codex fallback, so a
        # blocked Claude account still has a paid model to fall to.
        packaged = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
        for category, row in packaged.items():
            if category.startswith("_"):
                continue
            with self.subTest(category=category):
                models = [
                    entry["model"]
                    for entries in row["tiers"].values()
                    for entry in entries
                ]
                self.assertTrue(
                    [model for model in models if model.startswith("openai-codex/")],
                    f"{category} has no openai-codex model",
                )

    def test_packaged_tiers_never_pick_a_luna_model(self):
        # Owner rule (2026-10-20): no luna model in the default picks.
        packaged = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
        for category, row in packaged.items():
            if category.startswith("_"):
                continue
            for tier_name, entries in row["tiers"].items():
                for entry in entries:
                    self.assertNotIn("luna", entry["model"], f"{category} {tier_name}")

    def test_packaged_codex_models_sit_above_tier0(self):
        # The OpenAI quota window is shared with other work, so no codex
        # model may sit in tier0 (issue #241).
        packaged = json.loads(
            resources.files("lupin").joinpath("model-tiers.json").read_text(encoding="utf-8")
        )
        for category, row in packaged.items():
            if category.startswith("_"):
                continue
            for entry in row["tiers"].get("tier0", []):
                self.assertFalse(
                    entry["model"].startswith("openai-codex/"),
                    f"{category} tier0 {entry['model']}",
                )


if __name__ == "__main__":
    unittest.main()

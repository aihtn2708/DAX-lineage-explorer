"""Policies: how strict the workspace is with a model depends on how much rides on it.

Every model has a tier:

    personal   someone's own analysis: checked and scored, but saves need no review
    team       the team's reports (the default): every save is reviewed, and a save
               that adds errors, or drops the score by more than 10, cannot be approved
    certified  models people rely on: as team, plus a score of at least 80, a drop of
               at most 5, and stricter rules (missing descriptions, format strings and
               metric drift count as errors)

Each tier's settings can be changed by an admin, and each rule can be made stricter
or looser per tier. When a save breaks its tier's policy, nobody can approve it; an
admin can still approve it with a written reason, which the review and the audit
log keep.
"""

from __future__ import annotations

import json
from typing import Optional

from . import rules as R
from .store import Store, _rows, now

TIERS = ("personal", "team", "certified")
DEFAULT_TIER = "team"
SETTINGS = {  # name -> (label, kind)
    "require_review": ("Every save is reviewed", bool),
    "no_new_errors": ("A save may not add errors", bool),
    "max_score_drop": ("Largest score drop a save may make", int),
    "min_score": ("Lowest score a model may have", int),
}
DEFAULTS = {
    "personal": {"require_review": False, "no_new_errors": False, "max_score_drop": None, "min_score": None,
                 "rules": {}},
    "team": {"require_review": True, "no_new_errors": True, "max_score_drop": 10, "min_score": None, "rules": {}},
    "certified": {"require_review": True, "no_new_errors": True, "max_score_drop": 5, "min_score": 80,
                  "rules": {"measure-description": {"severity": "error"}, "measure-format": {"severity": "error"},
                            "metric-drift": {"severity": "error"}}},
}
DESCRIPTION = {
    "personal": "Someone's own analysis: checked and scored, no review needed.",
    "team": "The team's reports: every save reviewed; no new errors; the score may not fall by more than the limit.",
    "certified": "Models people rely on: as team, with a minimum score and stricter rules.",
}


# -- tiers and their settings ------------------------------------------------------------------

def tiers(store: Store) -> dict:
    """Every tier's settings: the defaults, with what an admin changed."""
    with store.connect() as con:
        saved = {r["tier"]: dict(r) for r in con.execute("SELECT * FROM tier_policy")}
    out = {}
    for tier in TIERS:
        settings = json.loads(json.dumps(DEFAULTS[tier]))
        if tier in saved:
            settings.update(json.loads(saved[tier]["settings"] or "{}"))
            settings["updated_by"], settings["updated_at"] = saved[tier]["updated_by"], saved[tier]["updated_at"]
        settings["description"] = DESCRIPTION[tier]
        out[tier] = settings
    return out


def set_tier_settings(store: Store, tier: str, changes: dict, by: str = "") -> dict:
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    current = {k: v for k, v in tiers(store)[tier].items() if k in SETTINGS or k == "rules"}
    for key, value in (changes or {}).items():
        if key == "rules":
            current["rules"] = _clean_rules(value)
        elif key in SETTINGS:
            kind = SETTINGS[key][1]
            if kind is bool:
                current[key] = bool(value)
            elif value in (None, ""):
                current[key] = None
            else:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{SETTINGS[key][0]}: a whole number from 0 to 100, or empty for none") from None
                if not 0 <= number <= 100:
                    raise ValueError(f"{SETTINGS[key][0]}: a whole number from 0 to 100, or empty for none")
                current[key] = number
        else:
            raise ValueError(f"no setting {key!r}")
    with store.connect() as con:
        con.execute("INSERT INTO tier_policy (tier, settings, updated_by, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT (tier) DO UPDATE SET settings = excluded.settings, updated_by = excluded.updated_by, "
                    "updated_at = excluded.updated_at", (tier, json.dumps(current), by.strip()[:200], now()))
    _recheck(store, [m for m, t in tiers_of(store).items() if t == tier])
    return tiers(store)[tier]


def _clean_rules(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("rules must map a rule id to {enabled, severity}")
    out = {}
    for rule_id, override in value.items():
        if rule_id not in R.BY_ID:
            raise ValueError(f"no rule {rule_id!r}")
        if not isinstance(override, dict):
            raise ValueError(f"{rule_id}: give enabled and/or severity")
        clean = {}
        if override.get("severity") not in (None, ""):
            if override["severity"] not in R.SEVERITIES:
                raise ValueError(f"{rule_id}: severity must be one of {', '.join(R.SEVERITIES)}")
            clean["severity"] = override["severity"]
        if override.get("enabled") is not None:
            clean["enabled"] = bool(override["enabled"])
        if clean:
            out[rule_id] = clean
    return out


def reset_tier(store: Store, tier: str) -> dict:
    with store.connect() as con:
        con.execute("DELETE FROM tier_policy WHERE tier = ?", (tier,))
    _recheck(store, [m for m, t in tiers_of(store).items() if t == tier])
    return tiers(store)[tier]


# -- which tier a model is in ------------------------------------------------------------------------

def tiers_of(store: Store) -> dict:
    with store.connect() as con:
        set_tiers = {r["model_id"]: r["tier"] for r in con.execute("SELECT model_id, tier FROM model_policy")}
        ids = [r["id"] for r in con.execute("SELECT id FROM model")]
    return {i: set_tiers.get(i, DEFAULT_TIER) for i in ids}


def tier_of(store: Store, model_id: int) -> str:
    with store.connect() as con:
        row = con.execute("SELECT tier FROM model_policy WHERE model_id = ?", (model_id,)).fetchone()
    return row["tier"] if row else DEFAULT_TIER


def set_tier(store: Store, model_id: int, tier: str, by: str = "") -> str:
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    with store.connect() as con:
        if con.execute("SELECT 1 FROM model WHERE id = ?", (model_id,)).fetchone() is None:
            raise KeyError("no such model")
        con.execute("INSERT INTO model_policy (model_id, tier, updated_by, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT (model_id) DO UPDATE SET tier = excluded.tier, updated_by = excluded.updated_by, "
                    "updated_at = excluded.updated_at", (model_id, tier, by.strip()[:200], now()))
    _recheck(store, [model_id])
    return tier


def exempt_from_review(store: Store) -> set:
    """Models whose tier needs no review of their saves."""
    settings = tiers(store)
    return {m for m, t in tiers_of(store).items() if not settings[t]["require_review"]}


def effective_rules(store: Store, model_id: int, base: Optional[list] = None) -> list:
    """The team's rule set, with this model's tier made stricter or looser."""
    from .check import rule_set
    overrides = tiers(store)[tier_of(store, model_id)]["rules"]
    out = []
    for rule in base if base is not None else rule_set(store):
        rule = dict(rule)
        change = overrides.get(rule["id"], {})
        if "enabled" in change:
            rule["enabled"] = change["enabled"]
        if "severity" in change:
            rule["severity"] = change["severity"]
        out.append(rule)
    return out


def _recheck(store: Store, model_ids) -> None:
    from .check import recheck
    if model_ids:
        recheck(store, list(model_ids))


# -- does a version keep to its tier's policy? ---------------------------------------------------------

def evaluate_version(store: Store, version_id: int, against_id: Optional[int] = None) -> Optional[dict]:
    """Each check of the version's tier, passed or not; None when there is nothing to check."""
    from .check import compare_checks
    version = store.version(version_id)
    if version is None:
        return None
    tier = tier_of(store, version["model_id"])
    settings = tiers(store)[tier]
    if against_id is None:
        previous = store.previous_version(version_id)
        against_id = previous["id"] if previous else None
    standards = compare_checks(store, version_id, against_id)
    checks = []
    if standards is None and (settings["no_new_errors"] or settings["min_score"] is not None
                              or settings["max_score_drop"] is not None):
        checks.append({"id": "readable", "label": "The model can be read and checked", "ok": False,
                       "detail": "no model was read from this save, so it cannot be checked"})
    elif standards is not None:
        if settings["no_new_errors"]:
            errors = [f for f in standards["new"] if f["severity"] == "error"]
            checks.append({"id": "no_new_errors", "label": SETTINGS["no_new_errors"][0], "ok": not errors,
                           "detail": "; ".join(f"{f['label']}: {f['message']}" for f in errors[:5])
                           or "no new errors"})
        if settings["max_score_drop"] is not None and standards["previous"] is not None:
            drop = standards["previous"] - standards["score"]
            checks.append({"id": "max_score_drop", "label": f"Score falls by at most {settings['max_score_drop']}",
                           "ok": drop <= settings["max_score_drop"],
                           "detail": f"{standards['previous']} → {standards['score']}"})
        if settings["min_score"] is not None:
            checks.append({"id": "min_score", "label": f"Score is at least {settings['min_score']}",
                           "ok": standards["score"] >= settings["min_score"], "detail": f"score {standards['score']}"})
    return {"tier": tier, "require_review": settings["require_review"], "checks": checks,
            "ok": all(c["ok"] for c in checks)}


def compliance(store: Store) -> list:
    """Every model's tier, and whether its current version keeps to that tier's policy."""
    tier_map = tiers_of(store)
    out = []
    for m in store.models():
        if m["missing"] or m["kind"] != "model" or not m["version_id"]:
            continue
        verdict = evaluate_version(store, m["version_id"]) or {"checks": [], "ok": True}
        problems = [f"{c['label']}: {c['detail']}" for c in verdict["checks"] if not c["ok"]]
        out.append({"id": m["id"], "name": m["name"], "tier": tier_map.get(m["id"], DEFAULT_TIER), "score": m["score"],
                    "owner": m["owner"], "version": m["version"],
                    "problems": problems, "ok": not problems})
    return sorted(out, key=lambda r: (r["ok"], -TIERS.index(r["tier"]), r["name"].casefold()))


def model_policy(store: Store, model_id: int) -> dict:
    """The model's tier, what that tier asks, and whether the current version keeps to it."""
    tier = tier_of(store, model_id)
    row = next((m for m in compliance(store) if m["id"] == model_id), None)
    return {"tier": tier, "settings": tiers(store)[tier], "order": list(TIERS),
            "problems": row["problems"] if row else [], "ok": row["ok"] if row else True}


def overview(store: Store) -> dict:
    from .check import rule_set
    return {"tiers": tiers(store), "order": list(TIERS), "settings": {k: v[0] for k, v in SETTINGS.items()},
            "rules": [{"id": r["id"], "title": r["title"], "severity": r["severity"], "enabled": r["enabled"]}
                      for r in rule_set(store)],
            "models": compliance(store)}

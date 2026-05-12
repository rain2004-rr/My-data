"""UI quick-send chips: coerce model output and pad with heuristics when needed."""

from __future__ import annotations

from typing import Any

from app.models import MasterJSON

_MAX_LEN = 140
_MAX_CHIPS = 4


def coerce_quick_suggestions(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s[:_MAX_LEN]] if s else []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            t = item.strip()
            if t:
                out.append(t[:_MAX_LEN])
    return out


def fallback_quick_suggestions(master: MasterJSON) -> list[str]:
    ids = [f.id for f in master.ui_schema.fields]
    idset = {i.lower() for i in ids}

    if not ids:
        return [
            "Describe what one contributor should submit in one sentence",
            "Add the first two fields you care about (names + types)",
            "Remove all fields — I want to start from scratch",
            "Any dropdowns need a fixed option list? Tell me the values",
        ]

    pool: list[str] = []

    if any("note" in i for i in idset):
        pool.append("Make notes required")
    if any("chain" in i or "network" in i for i in idset):
        pool.append("Change allowed options on the chain/network select")
    if any("url" in i or "evidence" in i or "link" in i for i in idset):
        pool.append("Require HTTPS for URL fields")
    if any("address" in i or "pool" in i or "evm" in i for i in idset):
        pool.append("Tighten address validation (EVM vs generic text)")
    pool.append("Add one optional field for edge cases")
    pool.append("Adjust QA strategy (consensus / review rules)")
    pool.append("Remove all fields — I want to start from scratch")

    seen: set[str] = set()
    uniq: list[str] = []
    for p in pool:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def merge_quick_suggestions(model_suggestions: list[str], master: MasterJSON) -> list[str]:
    """Prefer model order; pad with fallbacks to reach up to MAX_CHIPS; dedupe case-insensitive."""
    seen: set[str] = set()
    out: list[str] = []

    for s in model_suggestions:
        t = s.strip()[:_MAX_LEN]
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= _MAX_CHIPS:
            return out

    for s in fallback_quick_suggestions(master):
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s[:_MAX_LEN])
        if len(out) >= _MAX_CHIPS:
            break

    return out


def seed_demo_quick_suggestions() -> list[str]:
    """Initial chips for the seeded DeFi demo session."""
    return [
        "Make notes required",
        "Add a screenshot upload (use URL field + instructions in help text)",
        "Limit address validation to EVM L2s only",
        "Remove all fields — I want to start from scratch",
    ]

"""
Normalize LLM-produced master dict before Pydantic validation.
Models often emit invalid enum literals (e.g. component: 'address'); we coerce to a safe shape.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[-\s]+", "_", s)
    return s or "field"

ALLOWED_UI_COMPONENTS = frozenset(
    {
        "text",
        "textarea",
        "select",
        "multiselect",
        "number",
        "url",
        "evm_address",
        "image",
        "video",
        # Image-annotation components (see models.py for shapes).
        "image_keypoint",
        "image_bbox",
        "image_circle",
        "image_annotation",
    }
)
UI_COMPONENT_ALIASES: dict[str, str] = {
    "address": "text",
    "string": "text",
    "input": "text",
    "short_text": "text",
    "long_text": "textarea",
    "email": "text",
    "phone": "text",
    "url_input": "url",
    "link": "url",
    "dropdown": "select",
    "choice": "select",
    "checkbox_group": "multiselect",
    "integer": "number",
    "float": "number",
    "decimal": "number",
    "photo": "image",
    "picture": "image",
    "file_image": "image",
    "image_upload": "image",
    "mp4": "video",
    "movie": "video",
    "file_video": "video",
    "video_upload": "video",
    # Annotation aliases — LLM-friendly synonyms for the three new primitives.
    "point": "image_keypoint",
    "keypoint": "image_keypoint",
    "image_point": "image_keypoint",
    "dot": "image_keypoint",
    "marker": "image_keypoint",
    "rectangle": "image_bbox",
    "bbox": "image_bbox",
    "bounding_box": "image_bbox",
    "image_rectangle": "image_bbox",
    "square": "image_bbox",
    "image_square": "image_bbox",
    "annotation_box": "image_bbox",
    "image_annotation": "image_bbox",
    "circle": "image_circle",
    "image_circle_annotation": "image_circle",
    "circle_annotation": "image_circle",
    # Combined annotator aliases
    "combined_annotation": "image_annotation",
    "multi_annotation": "image_annotation",
    "image_multi_annotation": "image_annotation",
    "annotation": "image_annotation",
    "image_annotate": "image_annotation",
    "image_annotator": "image_annotation",
}

ALLOWED_VALIDATION_TYPES = frozenset(
    {"format", "cross_user_consensus", "expert_sample", "automated_rules"}
)
ALLOWED_CONSENSUS_TYPES = frozenset(
    {"majority_vote", "reputation_weight", "threshold_agreement", "manual_review"}
)

def _parse_loose_dict_string(s: str) -> dict[str, Any] | None:
    """Parse LLM mistakes like "{'value': 'OKX', 'label': 'OKX'}" into a dict."""
    t = s.strip()
    if not t.startswith("{"):
        return None
    try:
        out = json.loads(t.replace("'", '"'))
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def _coerce_ui_field_option(x: Any) -> Any | None:
    """Keep select option as string or object; never stringify dicts (breaks Formily + linkage keys)."""
    if x is None:
        return None
    if isinstance(x, dict):
        return copy.deepcopy(x)
    if isinstance(x, str):
        parsed = _parse_loose_dict_string(x)
        if isinstance(parsed, dict) and ("value" in parsed or "label" in parsed or "name" in parsed):
            return parsed
        return x
    return str(x)


def _normalize_options_linkage_map(meta: Any) -> None:
    """Coerce stringified option dicts inside meta.optionsLinkage.map values."""
    if not isinstance(meta, dict):
        return
    ol = meta.get("optionsLinkage")
    if not isinstance(ol, dict):
        return
    mmap = ol.get("map")
    if not isinstance(mmap, dict):
        return
    fixed: dict[str, Any] = {}
    for k, arr in mmap.items():
        if not isinstance(arr, list):
            continue
        cleaned: list[Any] = []
        for x in arr:
            c = _coerce_ui_field_option(x)
            if c is not None:
                cleaned.append(c)
        fixed[str(k)] = cleaned
    ol["map"] = fixed


def _fix_inverted_options_linkage(fields: list[Any]) -> None:
    """Move optionsLinkage from parent to child when dependency points *after* this field (common LLM mistake).

    Correct: child field (e.g. coin) has meta.optionsLinkage { dependency: "exchange", map: {...} }.
    Wrong: exchange has dependency "coin" — we move the block onto coin with dependency "exchange".
    """
    id_to_idx: dict[str, int] = {}
    for i, f in enumerate(fields):
        if isinstance(f, dict):
            fid = str(f.get("id") or "").strip()
            if fid:
                id_to_idx[fid] = i

    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            continue
        meta = f.get("meta")
        if not isinstance(meta, dict):
            continue
        ol = meta.get("optionsLinkage")
        if not isinstance(ol, dict) or not isinstance(ol.get("map"), dict):
            continue
        dep = ol.get("dependency")
        if not isinstance(dep, str) or not dep.strip():
            continue
        dep = dep.strip()
        j = id_to_idx.get(dep)
        if j is None:
            continue
        if j <= i:
            continue

        child = fields[j]
        if not isinstance(child, dict):
            continue
        ccomp = str(child.get("component") or "").strip().lower()
        if ccomp not in ("select", "multiselect"):
            continue

        parent_id = str(f.get("id") or "").strip()
        new_ol = copy.deepcopy(ol)
        new_ol["dependency"] = parent_id

        cmeta = child.get("meta")
        if not isinstance(cmeta, dict):
            cmeta = {}
            child["meta"] = cmeta
        cmeta["optionsLinkage"] = new_ol

        del meta["optionsLinkage"]
        if not meta:
            f["meta"] = None


def _infer_options_linkage_from_parent_annotations(fields: list[Any]) -> None:
    """
    Fallback: when the LLM annotated child options with a `parent` key instead of
    producing an explicit meta.optionsLinkage.map, synthesise the map so the
    frontend's reliable explicit-map code path is always available.

    Example of LLM output this fixes:
      child field options: [
        {"value": "phone",  "label": "Phone",  "parent": "Electronics"},
        {"value": "laptop", "label": "Laptop", "parent": "Electronics"},
        {"value": "shirt",  "label": "Shirt",  "parent": "Clothing"},
      ]
    After this function, the child field gets:
      meta.optionsLinkage = {
        "dependency": "<preceding select id>",
        "map": {
          "Electronics": [{"value": "phone", "label": "Phone"}, ...],
          "Clothing":    [{"value": "shirt", "label": "Shirt"}, ...],
        }
      }
    """
    _PARENT_KEYS = {"parent", "exchange", "exchange_id", "depends_on", "dependsOn"}

    # Ordered list of select field ids (for finding the nearest preceding select)
    select_ids: list[str] = [
        str(f.get("id") or "")
        for f in fields
        if isinstance(f, dict) and f.get("component") in ("select", "multiselect")
    ]

    def _get_parent_val(opt: Any) -> str | None:
        if not isinstance(opt, dict):
            return None
        for pk in _PARENT_KEYS:
            v = opt.get(pk)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    for f in fields:
        if not isinstance(f, dict):
            continue
        if f.get("component") not in ("select", "multiselect"):
            continue

        # Skip if an explicit map is already present
        meta = f.get("meta") or {}
        ol = meta.get("optionsLinkage") if isinstance(meta, dict) else None
        if isinstance(ol, dict) and isinstance(ol.get("map"), dict) and ol["map"]:
            continue

        opts = f.get("options") or []
        if not any(_get_parent_val(o) is not None for o in opts):
            continue

        # Build the grouped map; strip parent keys from each child option
        grouped: dict[str, list[Any]] = {}
        for opt in opts:
            pv = _get_parent_val(opt)
            if pv is None:
                continue
            clean = {k: v for k, v in opt.items() if k not in _PARENT_KEYS}
            grouped.setdefault(pv, []).append(clean)

        if not grouped:
            continue

        # Find the nearest preceding select field
        fid = str(f.get("id") or "")
        try:
            my_pos = select_ids.index(fid)
        except ValueError:
            continue
        if my_pos == 0:
            continue
        dep_id = select_ids[my_pos - 1]
        if not dep_id:
            continue

        # Install the synthesised map (prefer explicit map over parent-filtered rendering)
        if not isinstance(f.get("meta"), dict):
            f["meta"] = {}
        f["meta"]["optionsLinkage"] = {"dependency": dep_id, "map": grouped}


# ── Built-in validation pattern library ──────────────────────────────────────
#
# Two-tier auto-injection:
#   1. By component type — e.g. every `evm_address` field gets the EVM regex.
#   2. By keyword heuristic on id/label — e.g. a `text` field whose label
#      contains "phone" gets the E.164 phone pattern.
#
# User/LLM-supplied patterns are NEVER overridden. Auto-inject only fills
# in `validation.pattern` and `validation.message` when both are missing.

BUILTIN_VALIDATIONS_BY_COMPONENT: dict[str, dict[str, str]] = {
    "evm_address": {
        "pattern": r"^0x[a-fA-F0-9]{40}$",
        "message": "Must be a 0x-prefixed 40-hex-character EVM address (e.g. 0xabc1…1234).",
    },
    "url": {
        "pattern": r"^https?://[^\s/$.?#].[^\s]*$",
        "message": "Must be a valid URL starting with http:// or https://.",
    },
}

# (compiled regex on id+label, preset)
BUILTIN_VALIDATIONS_BY_KEYWORD: list[tuple[re.Pattern[str], dict[str, str]]] = [
    (
        re.compile(r"\b(email|e[-_]?mail)\b", re.IGNORECASE),
        {
            "pattern": r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
            "message": "Must be a valid email address (e.g. user@example.com).",
        },
    ),
    (
        re.compile(r"\b(phone|mobile|tel)\b", re.IGNORECASE),
        {
            "pattern": r"^\+?[1-9]\d{6,14}$",
            "message": "Must be a valid phone number in E.164 format (e.g. +14155552671).",
        },
    ),
    (
        re.compile(r"\b(btc|bitcoin).*address|address.*(btc|bitcoin)\b", re.IGNORECASE),
        {
            "pattern": r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$",
            "message": "Must be a valid Bitcoin address (Legacy 1…/3… or Bech32 bc1…).",
        },
    ),
    (
        re.compile(r"\b(sol|solana).*address|address.*(sol|solana)\b", re.IGNORECASE),
        {
            "pattern": r"^[1-9A-HJ-NP-Za-km-z]{32,44}$",
            "message": "Must be a valid Solana address (base58, 32–44 chars).",
        },
    ),
    (
        re.compile(r"\b(tx|transaction|txn).*hash\b|\b(tx|txn)_?hash\b", re.IGNORECASE),
        {
            "pattern": r"^0x[a-fA-F0-9]{64}$",
            "message": "Must be a 0x-prefixed 64-hex transaction hash.",
        },
    ),
    (
        re.compile(r"\bzip\b|\bpostal[_\s-]?code\b", re.IGNORECASE),
        {
            "pattern": r"^[A-Za-z0-9\- ]{3,12}$",
            "message": "Must be a valid postal/ZIP code (3–12 alphanumeric chars).",
        },
    ),
]


def _normalize_help_text(fields: list[Any]) -> list[str]:
    """Drop empty/whitespace/placeholder `help_text` so the UI doesn't render
    an empty "?" tooltip. Returns the list of field ids that ended up with no
    help_text — used by the caller to surface a follow-up question to the user.
    """
    PLACEHOLDERS = {"tbd", "todo", "n/a", "na", "-", "none", "null"}
    missing: list[str] = []
    for f in fields:
        if not isinstance(f, dict):
            continue
        ht = f.get("help_text")
        if isinstance(ht, str):
            trimmed = ht.strip()
            if not trimmed or trimmed.lower() in PLACEHOLDERS:
                f["help_text"] = None
                fid = str(f.get("id") or "").strip()
                if fid:
                    missing.append(fid)
        elif ht is None:
            fid = str(f.get("id") or "").strip()
            if fid:
                missing.append(fid)
        else:
            # Non-string non-null → coerce to null and report
            f["help_text"] = None
            fid = str(f.get("id") or "").strip()
            if fid:
                missing.append(fid)
    return missing


_ANNOTATION_SINGLE_PRIMITIVES = {
    "image_keypoint": "keypoint",
    "image_bbox": "bbox",
    "image_circle": "circle",
}


def _merge_annotation_fields_into_combined(
    fields: list[Any], data_schema: dict[str, Any] | None
) -> None:
    """
    Whenever the LLM produced TWO OR MORE single-primitive annotation fields
    (image_keypoint / image_bbox / image_circle) that all link to the same
    image (same `meta.imageSource.fromField`), combine them into ONE
    `image_annotation` field with corresponding `meta.presets`.

    This makes the contributor UX consistent regardless of whether the model
    chose Pattern A (combined) or Pattern B (separate). Mirrors the change
    in `data_schema.fields` so storage matches the new id.
    """
    # Group qualifying fields by their shared image-source field id
    groups: dict[str, list[int]] = {}
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            continue
        comp = f.get("component")
        if comp not in _ANNOTATION_SINGLE_PRIMITIVES:
            continue
        meta = f.get("meta")
        if not isinstance(meta, dict):
            continue
        src = meta.get("imageSource")
        if not isinstance(src, dict):
            continue
        from_field = src.get("fromField")
        if not isinstance(from_field, str) or not from_field.strip():
            continue
        groups.setdefault(from_field.strip(), []).append(i)

    indices_to_remove: list[int] = []
    removed_ids: list[str] = []
    inserted_ids: list[str] = []

    for source_id, indices in groups.items():
        if len(indices) < 2:
            continue
        first_idx = indices[0]
        merged_id = f"{source_id}_annotations"
        # Build the merged field
        primitives: list[str] = []
        presets: list[dict[str, Any]] = []
        labels: list[str] = []
        any_required = False
        any_help_text: str | None = None
        for i in indices:
            f = fields[i]
            comp = f.get("component")
            prim = _ANNOTATION_SINGLE_PRIMITIVES.get(comp)
            if prim is None:
                continue
            if prim not in primitives:
                primitives.append(prim)
            fid = str(f.get("id") or "").strip() or None
            flabel = str(f.get("label") or "").strip() or None
            if flabel:
                labels.append(flabel)
            presets.append({"type": prim, "id": fid, "label": flabel})
            if f.get("required"):
                any_required = True
            ht = f.get("help_text")
            if isinstance(ht, str) and ht.strip() and not any_help_text:
                any_help_text = ht.strip()
            removed_ids.append(fid or "")

        merged_label = " + ".join(labels) if labels else "Annotations"
        merged: dict[str, Any] = {
            "id": merged_id,
            "label": merged_label,
            "component": "image_annotation",
            "required": any_required,
            "help_text": any_help_text
            or "Drag each preset to mark its target on the image. Use the + Add buttons for more marks if needed.",
            "options": None,
            "validation": None,
            "meta": {
                "imageSource": {"fromField": source_id},
                "primitives": primitives,
                "presets": presets,
            },
        }
        fields[first_idx] = merged
        inserted_ids.append(merged_id)
        # All other indices in this group are now stale
        indices_to_remove.extend(indices[1:])

    # Remove stale fields in reverse so indices stay valid
    for i in sorted(set(indices_to_remove), reverse=True):
        del fields[i]

    # Mirror the change in data_schema.fields:
    #   • drop columns whose name matches a removed annotation field id
    #   • add an `object`-typed column for each newly inserted merged field
    if not isinstance(data_schema, dict):
        return
    ds_fields = data_schema.get("fields")
    if not isinstance(ds_fields, list):
        return
    removed_set = {x for x in removed_ids if x}
    if removed_set:
        data_schema["fields"] = [
            d for d in ds_fields if not (isinstance(d, dict) and d.get("name") in removed_set)
        ]
        ds_fields = data_schema["fields"]
    existing_names = {d.get("name") for d in ds_fields if isinstance(d, dict)}
    for new_id in inserted_ids:
        if new_id in existing_names:
            continue
        ds_fields.append(
            {
                "name": new_id,
                "type": "object",
                "description": "Image annotation object: annotations[] of bbox/keypoint/circle, plus annotated_image_url.",
                "required": False,
                "default": None,
                "meta": None,
            }
        )


def _apply_default_validations(fields: list[Any]) -> None:
    """Fill in validation.pattern / validation.message for known formats.

    Skips any field that already has a pattern set — user / LLM intent wins.
    """
    for f in fields:
        if not isinstance(f, dict):
            continue
        existing = f.get("validation")
        if existing is not None and not isinstance(existing, dict):
            existing = None
        v = dict(existing) if isinstance(existing, dict) else {}

        # Never override an explicit pattern
        if v.get("pattern"):
            f["validation"] = v
            continue

        comp = str(f.get("component") or "").strip().lower()
        preset = BUILTIN_VALIDATIONS_BY_COMPONENT.get(comp)

        if preset is None and comp in ("text", "textarea"):
            haystack = f"{f.get('id') or ''} {f.get('label') or ''}"
            for kw_re, kw_preset in BUILTIN_VALIDATIONS_BY_KEYWORD:
                if kw_re.search(haystack):
                    preset = kw_preset
                    break

        if preset is None:
            # Still preserve any existing length/range validators
            if v:
                f["validation"] = v
            continue

        v["pattern"] = preset["pattern"]
        if not v.get("message"):
            v["message"] = preset["message"]
        f["validation"] = v


ALLOWED_DATA_TYPES = frozenset({"string", "number", "boolean", "array", "object"})
DATA_TYPE_ALIASES: dict[str, str] = {
    "str": "string",
    "int": "number",
    "integer": "number",
    "float": "number",
    "double": "number",
    "bool": "boolean",
}


def normalize_master_dict(master: dict[str, Any]) -> dict[str, Any]:
    m = copy.deepcopy(master)

    ui = m.get("ui_schema")
    if isinstance(ui, dict):
        ui.setdefault("version", "1.0")
        fields = ui.get("fields")
        if isinstance(fields, list):
            for i, f in enumerate(fields):
                if not isinstance(f, dict):
                    continue
                fid = f.get("id")
                lbl = f.get("label")
                if isinstance(lbl, str) and lbl.strip() and (not isinstance(fid, str) or not str(fid).strip()):
                    f["id"] = _slug(lbl)
                elif isinstance(fid, str) and str(fid).strip() and not isinstance(lbl, str):
                    f["label"] = str(fid).strip()
                elif isinstance(fid, str) and str(fid).strip() and isinstance(lbl, str) and not lbl.strip():
                    f["label"] = str(fid).strip()
                if not str(f.get("id") or "").strip():
                    f["id"] = f"field_{i}"
                if not str(f.get("label") or "").strip():
                    f["label"] = str(f.get("id"))
                comp = f.get("component", "text")
                if isinstance(comp, str):
                    key = comp.strip().lower()
                    if key in UI_COMPONENT_ALIASES:
                        f["component"] = UI_COMPONENT_ALIASES[key]
                    elif comp not in ALLOWED_UI_COMPONENTS:
                        f["component"] = "text"
                else:
                    f["component"] = "text"
                val = f.get("validation")
                if val is not None and not isinstance(val, dict):
                    f["validation"] = None
                opts = f.get("options")
                if opts is not None and not isinstance(opts, list):
                    f["options"] = None
                elif isinstance(opts, list):
                    cleaned_opts = [_coerce_ui_field_option(x) for x in opts if x is not None]
                    cleaned_opts = [x for x in cleaned_opts if x is not None]
                    f["options"] = cleaned_opts if cleaned_opts else None
                _normalize_options_linkage_map(f.get("meta"))

            _fix_inverted_options_linkage(fields)
            _infer_options_linkage_from_parent_annotations(fields)
            # Merge sibling annotation fields BEFORE validation injection — order
            # matters because the merged image_annotation field has no pattern
            # to validate, and the merge also rewrites data_schema columns.
            _merge_annotation_fields_into_combined(fields, m.get("data_schema"))
            _apply_default_validations(fields)
            _normalize_help_text(fields)

    qa = m.get("qa_strategy")
    if isinstance(qa, dict):
        qa.setdefault("version", "1.0")
        vt = qa.get("validation_types")
        if isinstance(vt, list):
            qa["validation_types"] = [x for x in vt if isinstance(x, str) and x in ALLOWED_VALIDATION_TYPES]
        else:
            qa["validation_types"] = []
        rules = qa.get("consensus_rules")
        if isinstance(rules, list):
            fixed_rules: list[dict[str, Any]] = []
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                t = rule.get("type")
                if not isinstance(t, str) or t not in ALLOWED_CONSENSUS_TYPES:
                    rule["type"] = "manual_review"
                p = rule.get("params")
                if p is None or not isinstance(p, dict):
                    rule["params"] = {}
                fixed_rules.append(rule)
            qa["consensus_rules"] = fixed_rules
        else:
            qa["consensus_rules"] = []

    ds = m.get("data_schema")
    if isinstance(ds, dict):
        ds.setdefault("version", "1.0")
        ds.setdefault("primary_key", "user_id")
        ds.setdefault("status_default", "ADOPT")
        ds.setdefault("status_field_name", "status")
        fields = ds.get("fields")
        if isinstance(fields, list):
            for spec in fields:
                if not isinstance(spec, dict):
                    continue
                t = spec.get("type", "string")
                if isinstance(t, str):
                    tl = t.strip().lower()
                    if tl in DATA_TYPE_ALIASES:
                        spec["type"] = DATA_TYPE_ALIASES[tl]
                    elif t not in ALLOWED_DATA_TYPES:
                        spec["type"] = "string"
                else:
                    spec["type"] = "string"
                meta = spec.get("meta")
                if meta is not None and not isinstance(meta, dict):
                    spec["meta"] = None

    return m


def format_validation_errors(err: Any) -> str:
    """Short English summary for API/UI."""
    try:
        errs = err.errors()  # type: ignore[attr-defined]
    except Exception:
        return str(err)
    parts: list[str] = []
    for item in errs[:5]:
        loc = ".".join(str(x) for x in item.get("loc", ()))
        msg = item.get("msg", "")
        parts.append(f"{loc}: {msg}")
    return "; ".join(parts) if parts else str(err)

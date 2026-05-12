"""Pack optional field-builder rows from the UI into the LLM user message."""

from __future__ import annotations

import json

from app.master_normalize import ALLOWED_UI_COMPONENTS, _slug
from app.models import FieldBuilderDraftItem
from app.reset_intent import augment_message_for_llm_if_reset


def normalize_draft_fields(items: list[FieldBuilderDraftItem] | None) -> list[FieldBuilderDraftItem] | None:
    """Drop empty labels; fill missing ids from label; drop invalid component hints."""
    if not items:
        return None
    out: list[FieldBuilderDraftItem] = []
    for d in items:
        lab = d.label.strip()
        if not lab:
            continue
        iid = (d.id or "").strip() or _slug(lab)
        comp = (d.component or "").strip().lower() if d.component else None
        if comp is not None and comp not in ALLOWED_UI_COMPONENTS:
            comp = None
        notes = d.notes.strip() if d.notes else None
        out.append(
            FieldBuilderDraftItem(
                id=iid,
                label=lab,
                component=comp,
                required=bool(d.required),
                notes=notes,
            )
        )
    return out or None


def build_llm_user_message(user_message: str, draft_fields: list[FieldBuilderDraftItem] | None) -> str:
    base = augment_message_for_llm_if_reset(user_message.strip())
    if not draft_fields:
        return base
    rows = [d.model_dump() for d in draft_fields]
    blob = json.dumps(rows, ensure_ascii=False, indent=2)
    return (
        base
        + "\n\n### Field builder draft (from properties panel)\n"
        "The user composed this list in the UI. Each row may include `notes` in any language describing intent for that field. "
        "Analyze every `notes`, `label`, `required`, and optional `component` hint; then design or update `master` accordingly "
        "(English only inside schema strings). If `component` is missing, infer from notes. Mirror each business `id` into `data_schema`.\n"
        "```json\n"
        + blob
        + "\n```"
    )


def build_visible_user_chat_line(user_message: str, draft_fields: list[FieldBuilderDraftItem] | None) -> str:
    v = user_message.strip()
    if not draft_fields:
        return v
    ids = ", ".join(d.id for d in draft_fields)
    return v + f"\n\n(Field builder draft → agent: {len(draft_fields)} field(s): {ids})"

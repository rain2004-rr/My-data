"""Detect when the user wants to wipe all collection fields and start from a blank form."""

from __future__ import annotations

import re

# Broad but conservative: user wants zero custom fields / full slate.
_RESET_RE = re.compile(
    r"("
    r"\bremove\s+all\b|"
    r"\bdelete\s+all\b|"
    r"\bclear\s+all\b|"
    r"\bremove\s+these\b|"
    r"\bremove\s+.*\bfields?\b|"
    r"\bclear\s+(the\s+)?form\b|"
    r"\bempty\s+(the\s+)?form\b|"
    r"\breset\s+(the\s+)?form\b|"
    r"\bstart\s+from\s+scratch\b|"
    r"\bstart\s+over\b|"
    r"\bwipe\b|"
    r"\bdiscard\s+(the\s+)?(form|fields)\b|"
    r"\brebuild\s+(\w+\s+){0,6}new\s+form\b|"
    r"\bno\s+fields\b|"
    r"\bblank\s+form\b|"
    # Chinese phrases: still match zh-CN reset wording
    r"清空|删除\s*所有|移除\s*所有|清空表单|重置表单"
    r")",
    re.IGNORECASE,
)

_RESET_SUFFIX = (
    "\n\n[Model instruction — mandatory if the user message matches a full reset:] "
    "The user wants ALL custom collection fields removed. "
    "Return `ui_schema.fields` as an empty array `[]`. "
    "Return `data_schema.fields` with ONLY `user_id` and `status` (status.default = \"ADOPT\"). "
    "Set `ui_schema.title` to \"New task form\" and `ui_schema.description` to a short neutral line "
    "unless the user already named a new topic. "
    "Reset `qa_strategy` to a minimal default (e.g. validation_types [\"format\"] and one simple consensus rule). "
    "Do NOT keep prior task fields unless the user explicitly asked to keep them. "
    "In `agent_message`, act as a collaborative designer: briefly confirm the slate is clear, then **ask what kind of form "
    "they want next** and include **1–2 concrete questions** (e.g. data domain, must-have fields, select options, "
    "how strict validation/consensus should be). Invite them to answer so you can draft the next version together."
)


def wants_full_form_reset(user_message: str) -> bool:
    text = (user_message or "").strip()
    if len(text) < 4:
        return False
    return bool(_RESET_RE.search(text))


def augment_message_for_llm_if_reset(user_message: str) -> str:
    if wants_full_form_reset(user_message):
        return user_message + _RESET_SUFFIX
    return user_message

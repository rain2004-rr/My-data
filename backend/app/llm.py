from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import httpx
from openai import OpenAI
from pydantic import ValidationError

from app.master_normalize import format_validation_errors, normalize_master_dict
from app.models import DataFieldSpec, DataSchema, MasterJSON, default_master_for_empty
from app.prompts import (
    REFINE_FIELD_DESCRIPTION_SYSTEM,
    REFINE_LABEL_STUDIO_XML_SYSTEM,
    SYSTEM_PROMPT,
    USER_JSON_REMINDER,
)
from app.quick_suggestions import coerce_quick_suggestions, merge_quick_suggestions

logger = logging.getLogger(__name__)


def _api_key() -> str:
    return (
        os.getenv("QWEN_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )


def _http_timeout() -> httpx.Timeout:
    """Timeouts for upstream LLM (DashScope-compatible OpenAI client).

    Default read/write matches OpenAI Python SDK (600s); a shorter LLM_TIMEOUT was
    causing slow completions (large system + JSON) to fail as APITimeoutError.
    """
    read_s = float(os.getenv("LLM_TIMEOUT", "900"))
    connect_s = float(os.getenv("LLM_CONNECT_TIMEOUT", "30"))
    pool_s = float(os.getenv("LLM_POOL_TIMEOUT", "30"))
    return httpx.Timeout(connect=connect_s, read=read_s, write=read_s, pool=pool_s)


def _client() -> OpenAI:
    api_key = _api_key()
    base_url = os.getenv("QWEN_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=_http_timeout(),
        max_retries=max_retries,
    )


def _model_name() -> str:
    return os.getenv("QWEN_MODEL") or "qwen-plus"


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip optional ```json fences
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    # Try to extract the first complete JSON object from the text
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _ensure_data_schema_constraints(master: MasterJSON) -> MasterJSON:
    """Ensure user_id / status / primary_key when the model omits them."""
    ds = master.data_schema
    names = {f.name for f in ds.fields}
    fields = list(ds.fields)
    if ds.primary_key != "user_id":
        ds = ds.model_copy(update={"primary_key": "user_id"})
    if "user_id" not in names:
        fields.insert(
            0,
            DataFieldSpec(
                name="user_id",
                type="string",
                description="Primary key: contributor id for deduplication (not submission_id alone).",
                required=True,
            ),
        )
    if "status" not in {f.name for f in fields}:
        insert_at = 1 if fields and fields[0].name == "user_id" else 0
        fields.insert(
            insert_at,
            DataFieldSpec(
                name="status",
                type="string",
                description="Row status for filtering valid outputs.",
                required=True,
                default="ADOPT",
            ),
        )
    else:
        patched: list[DataFieldSpec] = []
        for f in fields:
            if f.name == "status" and f.default != "ADOPT":
                patched.append(f.model_copy(update={"default": "ADOPT"}))
            else:
                patched.append(f)
        fields = patched
    return master.model_copy(update={"data_schema": ds.model_copy(update={"fields": fields})})


def call_qwen_for_master(
    conversation_messages: list[dict[str, str]],
    *,
    current_master: MasterJSON | None = None,
) -> tuple[str, MasterJSON, list[str]]:
    """
    conversation_messages: OpenAI-style role/content (no system; system is prepended separately).
    current_master: authoritative session Master; include after manual edits so the model does not rely only on chat.
    """
    api_key = _api_key().strip()
    if not api_key:
        raise RuntimeError(
            "No LLM API key: set QWEN_API_KEY, DASHSCOPE_API_KEY, or OPENAI_API_KEY in backend/.env and restart uvicorn."
        )

    client = _client()
    system_content = SYSTEM_PROMPT
    if current_master is not None:
        blob = current_master.model_dump_json(indent=2)
        max_chars = 14_000
        if len(blob) > max_chars:
            blob = blob[:max_chars] + "\n... (truncated for context length)"
        system_content += (
            "\n\n## Current authoritative master JSON (before this user turn)\n"
            "Treat this as the source of truth for existing fields, labels, and QA rules. "
            "Merge the user's new instructions into a single updated full `master` in your reply.\n"
            f"```json\n{blob}\n```"
        )

    messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
    messages.extend(conversation_messages)
    messages.append({"role": "user", "content": USER_JSON_REMINDER})

    t0 = time.perf_counter()
    completion = client.chat.completions.create(
        model=_model_name(),
        messages=messages,
        temperature=0.2,
    )
    logger.info(
        "LLM chat.completions done model=%s in %.1fs",
        _model_name(),
        time.perf_counter() - t0,
    )
    raw = (completion.choices[0].message.content or "").strip()
    try:
        payload = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("JSON parse failed, using fallback empty master: %s", e)
        empty = default_master_for_empty()
        return (
            "The model did not return valid JSON; reverted to an empty form. Retry or check the API response.",
            empty,
            merge_quick_suggestions([], empty),
        )

    agent_message = str(payload.get("agent_message") or "Form updated.")
    qs_raw = coerce_quick_suggestions(payload.get("quick_suggestions"))
    master_raw = payload.get("master")
    if not isinstance(master_raw, dict):
        empty = default_master_for_empty()
        return agent_message, empty, merge_quick_suggestions(qs_raw, empty)

    normalized = normalize_master_dict(master_raw)
    try:
        master = MasterJSON.model_validate(normalized)
    except ValidationError as e:
        detail = format_validation_errors(e)
        logger.warning("Master JSON validation failed after normalize: %s", e)
        empty = default_master_for_empty()
        return (
            f"Schema validation failed after normalization; reverted to empty form. First issues: {detail}",
            empty,
            merge_quick_suggestions(qs_raw, empty),
        )

    master = _ensure_data_schema_constraints(master)
    quick = merge_quick_suggestions(qs_raw, master)
    return agent_message, master, quick


def refine_field_descriptions(
    master: MasterJSON,
    field_id: str,
    user_instruction: str,
) -> tuple[str | None, str | None, str]:
    """
    For one UI field, rewrite help_text and the matching data_schema.description from user intent.
    Returns (help_text, data_description, agent_message).
    """
    api_key = _api_key().strip()
    if not api_key:
        raise RuntimeError(
            "No LLM API key: set QWEN_API_KEY, DASHSCOPE_API_KEY, or OPENAI_API_KEY in backend/.env and restart uvicorn."
        )

    fid = field_id.strip()
    ui_field = next((f for f in master.ui_schema.fields if f.id == fid), None)
    if ui_field is None:
        raise ValueError(f"Unknown field_id: {field_id!r}")

    data_spec = next((f for f in master.data_schema.fields if f.name == fid), None)
    user_instr = (user_instruction or "").strip()
    if not user_instr:
        raise ValueError("user_instruction must be non-empty")

    snapshot = {
        "field": ui_field.model_dump(),
        "current_help_text": ui_field.help_text,
        "current_data_description": (data_spec.description if data_spec else None),
        "form_title": master.ui_schema.title,
        "form_description": master.ui_schema.description,
    }
    user_blob = json.dumps(snapshot, ensure_ascii=False, indent=2)

    client = _client()
    messages: list[dict[str, str]] = [
        {"role": "system", "content": REFINE_FIELD_DESCRIPTION_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Field id: `{fid}`\n"
                f"User instruction (may be non-English; output strings must still be English):\n{user_instr}\n\n"
                f"Current context JSON:\n{user_blob}\n\n"
                "Return the single JSON object with keys help_text, data_description, agent_message only."
            ),
        },
    ]

    t0 = time.perf_counter()
    completion = client.chat.completions.create(
        model=_model_name(),
        messages=messages,
        temperature=0.3,
    )
    logger.info(
        "LLM refine_field_descriptions done model=%s in %.1fs",
        _model_name(),
        time.perf_counter() - t0,
    )
    raw = (completion.choices[0].message.content or "").strip()
    try:
        payload = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("refine_field_descriptions JSON parse failed: %s", e)
        return (
            ui_field.help_text,
            data_spec.description if data_spec else None,
            "The model did not return valid JSON; kept existing descriptions.",
        )

    agent_message = str(payload.get("agent_message") or "Updated field documentation.")
    ht = payload.get("help_text")
    dd = payload.get("data_description")
    help_text: str | None
    if ht is None:
        help_text = None
    elif isinstance(ht, str):
        help_text = ht
    else:
        help_text = ui_field.help_text
    data_description: str | None
    if dd is None:
        data_description = data_spec.description if data_spec else None
    elif isinstance(dd, str):
        data_description = dd
    else:
        data_description = data_spec.description if data_spec else None

    return help_text, data_description, agent_message


# ── Label Studio XML refinement ──────────────────────────────────────────────


_XML_TAG_RE = re.compile(r"<\s*View\b", re.IGNORECASE)


def _basic_xml_sanity_check(xml_str: str) -> str | None:
    """
    Cheap structural validation — not a full XML parser, but catches the common
    LLM mistakes (missing root, unbalanced View, fenced markdown leaked in).
    Returns None if OK, otherwise an English error string.
    """
    if not isinstance(xml_str, str) or not xml_str.strip():
        return "XML is empty"
    text = xml_str.strip()
    if text.startswith("```"):
        return "XML contains markdown code fences — must be raw XML"
    if not _XML_TAG_RE.search(text):
        return "XML must contain a <View> root element"
    if text.lower().count("<view") != text.lower().count("</view>"):
        return "Unbalanced <View>/</View> tags"
    return None


def refine_label_studio_xml(
    current_xml: str,
    user_instruction: str,
    ui_fields: list[dict[str, Any]],
) -> tuple[str, str]:
    """
    Apply a user instruction to the current Label Studio XML.
    Returns (updated_xml, agent_message).
    Raises ValueError if input is invalid; RuntimeError if LLM not configured.
    """
    api_key = _api_key().strip()
    if not api_key:
        raise RuntimeError(
            "No LLM API key: set QWEN_API_KEY, DASHSCOPE_API_KEY, or OPENAI_API_KEY in backend/.env and restart uvicorn."
        )

    instruction = (user_instruction or "").strip()
    if not instruction:
        raise ValueError("user_instruction must be non-empty")

    # Trim ui_fields to the keys the prompt actually uses, to save tokens
    trimmed_fields = [
        {
            "id": str(f.get("id") or ""),
            "label": str(f.get("label") or ""),
            "component": str(f.get("component") or ""),
        }
        for f in (ui_fields or [])
        if isinstance(f, dict) and str(f.get("id") or "").strip()
    ]

    user_blob = json.dumps(
        {
            "ui_fields": trimmed_fields,
            "current_xml": current_xml,
            "user_instruction": instruction,
        },
        ensure_ascii=False,
        indent=2,
    )

    client = _client()
    messages: list[dict[str, str]] = [
        {"role": "system", "content": REFINE_LABEL_STUDIO_XML_SYSTEM},
        {
            "role": "user",
            "content": (
                "Apply the instruction below to the current XML and return the JSON "
                "object described in the system prompt — only that JSON object, no fences.\n\n"
                f"{user_blob}"
            ),
        },
    ]

    t0 = time.perf_counter()
    completion = client.chat.completions.create(
        model=_model_name(),
        messages=messages,
        temperature=0.2,
    )
    logger.info(
        "LLM refine_label_studio_xml done model=%s in %.1fs",
        _model_name(),
        time.perf_counter() - t0,
    )
    raw = (completion.choices[0].message.content or "").strip()

    try:
        payload = _extract_json_object(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("refine_label_studio_xml JSON parse failed: %s", e)
        return current_xml, "The model did not return valid JSON; kept the previous XML."

    new_xml = payload.get("xml")
    agent_message = str(payload.get("agent_message") or "Updated the labeling XML.")

    if not isinstance(new_xml, str):
        return current_xml, "The model did not return an xml string; kept the previous XML."

    err = _basic_xml_sanity_check(new_xml)
    if err is not None:
        logger.warning("refine_label_studio_xml structural check failed: %s", err)
        return current_xml, f"The model returned malformed XML ({err}); kept the previous XML."

    return new_xml, agent_message

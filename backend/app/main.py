from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Always load .env from the backend directory (sibling to app/), regardless of uvicorn cwd.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_ROOT / ".env"


def _manual_parse_dotenv(path: Path) -> None:
    """Fallback parse common keys when load_dotenv fails (encoding/BOM, etc.)."""
    if not path.is_file():
        return
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            text = path.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, OSError):
            text = None
    if text is None:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "CORS_ORIGINS"):
            os.environ[key] = val


load_dotenv(_ENV_PATH, encoding="utf-8-sig")
if not (os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")):
    _manual_parse_dotenv(_ENV_PATH)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import APIConnectionError, APITimeoutError
from pydantic import ValidationError

from app.draft_packaging import (
    build_llm_user_message,
    build_visible_user_chat_line,
    normalize_draft_fields,
)
from app.llm import (
    _ensure_data_schema_constraints,
    call_qwen_for_master,
    refine_field_descriptions,
    refine_label_studio_xml,
)
from app.master_normalize import format_validation_errors, normalize_master_dict
from app.models import (
    ChatMessage,
    FieldBuilderDraftItem,
    GenerateFormRequest,
    GenerateFormResponse,
    MasterJSON,
    RefineFieldDescriptionRequest,
    RefineFieldDescriptionResponse,
    RefineLabelStudioXmlRequest,
    RefineLabelStudioXmlResponse,
    SaveMasterRequest,
    SaveMasterResponse,
    SeedDemoResponse,
    default_master_for_empty,
)
from app.quick_suggestions import merge_quick_suggestions, seed_demo_quick_suggestions
from app.reset_intent import wants_full_form_reset
from app.sessions import seed_defi_session, store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_RESET_FORM_AGENT_MESSAGE = (
    "All collection fields are cleared—you now have a blank form. "
    "What would you like to collect next? For example: what problem or asset are contributors reporting, "
    "who is the audience, and which 2–3 fields are must-haves on the first version? "
    "If you are not sure, tell me the domain in one sentence and I will propose a first draft and ask you to tune details."
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Loaded .env path=%s exists=%s", _ENV_PATH, _ENV_PATH.is_file())
    has_key = bool(
        os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")
    )
    logger.info("LLM API key configured: %s", has_key)
    logger.info("QWEN_BASE_URL=%s", os.getenv("QWEN_BASE_URL", "(default dashscope compatible)"))
    logger.info("QWEN_MODEL=%s", os.getenv("QWEN_MODEL", "qwen-plus"))
    yield


app = FastAPI(title="Codatta Form Agent API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/demo/seed", response_model=SeedDemoResponse)
def demo_seed():
    """Seed session with multi-turn history and an updated Master JSON (DeFi demo)."""
    sid, state = seed_defi_session()
    return SeedDemoResponse(
        session_id=sid,
        messages=state.messages,
        master=state.master,
        quick_suggestions=seed_demo_quick_suggestions(),
    )


@app.post("/api/generate-form", response_model=GenerateFormResponse)
def generate_form(body: GenerateFormRequest):
    state = store.get_or_create(body.session_id)
    draft_norm: list[FieldBuilderDraftItem] | None = normalize_draft_fields(body.draft_fields)
    visible = build_visible_user_chat_line(body.user_message, draft_norm)
    state.messages.append(ChatMessage(role="user", content=visible))

    # Full reset without draft packaging: do not call upstream LLM (avoids 502 on network/timeouts).
    if wants_full_form_reset(body.user_message) and not draft_norm:
        master = default_master_for_empty()
        agent_message = _RESET_FORM_AGENT_MESSAGE
        quick_suggestions = merge_quick_suggestions([], master)
        state.messages.append(ChatMessage(role="assistant", content=agent_message))
        state.master = master
        return GenerateFormResponse(
            session_id=body.session_id,
            agent_message=agent_message,
            master=master,
            quick_suggestions=quick_suggestions,
        )

    openai_msgs = [{"role": m.role, "content": m.content} for m in state.messages]
    if openai_msgs and openai_msgs[-1]["role"] == "user":
        openai_msgs[-1] = {
            **openai_msgs[-1],
            "content": build_llm_user_message(body.user_message, draft_norm),
        }

    try:
        agent_message, master, quick_suggestions = call_qwen_for_master(
            openai_msgs, current_master=state.master
        )
    except RuntimeError as e:
        logger.warning("generate_form not ready: %s", e)
        raise HTTPException(
            status_code=503,
            detail="助手服务未就绪，请稍后再试或联系管理员检查配置。",
        ) from e
    except APITimeoutError as e:
        logger.exception("LLM call timed out (detail in logs): %s", e)
        raise HTTPException(
            status_code=502,
            detail="助手响应超时，请稍后再试；也可尝试把说明写短一些。",
        ) from e
    except APIConnectionError as e:
        logger.exception("LLM connection failed (detail in logs): %s", e)
        raise HTTPException(
            status_code=502,
            detail="暂时无法连接模型服务，请检查网络后重试。",
        ) from e
    except Exception as e:
        logger.exception("LLM call failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail="模型服务暂时不可用，请稍后再试。",
        ) from e

    if wants_full_form_reset(body.user_message) and len(master.ui_schema.fields) > 0:
        logger.warning(
            "Full form reset was requested but the model still returned %s UI fields; forcing empty master.",
            len(master.ui_schema.fields),
        )
        master = default_master_for_empty()
        agent_message = _RESET_FORM_AGENT_MESSAGE
        quick_suggestions = merge_quick_suggestions([], master)

    state.messages.append(ChatMessage(role="assistant", content=agent_message))
    state.master = master

    return GenerateFormResponse(
        session_id=body.session_id,
        agent_message=agent_message,
        master=master,
        quick_suggestions=quick_suggestions,
    )


@app.post("/api/save-master", response_model=SaveMasterResponse)
def save_master(body: SaveMasterRequest):
    """Persist manually edited Master JSON; chat and model baseline use this snapshot."""
    state = store.get_or_create(body.session_id)
    if not isinstance(body.master, dict):
        raise HTTPException(status_code=400, detail="`master` must be a JSON object")

    normalized = normalize_master_dict(body.master)
    try:
        master = MasterJSON.model_validate(normalized)
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid master JSON: {format_validation_errors(e)}",
        ) from e

    master = _ensure_data_schema_constraints(master)
    state.master = master
    n = len(master.ui_schema.fields)
    info = (
        f"Manual save applied: {n} UI field(s). "
        "The preview and the model's next turn will use this JSON as the baseline—tell me what to adjust next."
    )
    qs = merge_quick_suggestions([], master)
    return SaveMasterResponse(master=master, info=info, quick_suggestions=qs)


@app.post("/api/refine-field-description", response_model=RefineFieldDescriptionResponse)
def refine_field_description(body: RefineFieldDescriptionRequest):
    """LLM-assisted rewrite of one field's help_text and data_schema.description (client merges, then save-master)."""
    if not isinstance(body.master, dict):
        raise HTTPException(status_code=400, detail="`master` must be a JSON object")

    normalized = normalize_master_dict(body.master)
    try:
        master = MasterJSON.model_validate(normalized)
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid master JSON: {format_validation_errors(e)}",
        ) from e

    master = _ensure_data_schema_constraints(master)
    try:
        help_text, data_description, agent_message = refine_field_descriptions(
            master,
            body.field_id,
            body.user_instruction,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        logger.warning("refine_field_description not ready: %s", e)
        raise HTTPException(
            status_code=503,
            detail="助手服务未就绪，请稍后再试或联系管理员检查配置。",
        ) from e
    except APITimeoutError as e:
        logger.exception("refine LLM timed out: %s", e)
        raise HTTPException(
            status_code=502,
            detail="助手响应超时，请稍后再试。",
        ) from e
    except APIConnectionError as e:
        logger.exception("refine LLM connection failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail="暂时无法连接模型服务，请检查网络后重试。",
        ) from e
    except Exception as e:
        logger.exception("refine_field_description failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail="模型服务暂时不可用，请稍后再试。",
        ) from e

    return RefineFieldDescriptionResponse(
        help_text=help_text,
        data_description=data_description,
        agent_message=agent_message,
    )


@app.post("/api/refine-label-studio-xml", response_model=RefineLabelStudioXmlResponse)
def refine_label_studio_xml_endpoint(body: RefineLabelStudioXmlRequest):
    """Chat-driven edit of the Label Studio labeling-interface XML."""
    try:
        new_xml, agent_message = refine_label_studio_xml(
            current_xml=body.current_xml,
            user_instruction=body.user_instruction,
            ui_fields=body.ui_fields,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        logger.warning("refine_label_studio_xml not ready: %s", e)
        raise HTTPException(
            status_code=503,
            detail="助手服务未就绪，请稍后再试或联系管理员检查配置。",
        ) from e
    except APITimeoutError as e:
        logger.exception("refine XML LLM timed out: %s", e)
        raise HTTPException(
            status_code=502,
            detail="助手响应超时，请稍后再试。",
        ) from e
    except APIConnectionError as e:
        logger.exception("refine XML LLM connection failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail="暂时无法连接模型服务，请检查网络后重试。",
        ) from e
    except Exception as e:
        logger.exception("refine_label_studio_xml failed: %s", e)
        raise HTTPException(
            status_code=502,
            detail="模型服务暂时不可用，请稍后再试。",
        ) from e

    return RefineLabelStudioXmlResponse(xml=new_xml, agent_message=agent_message)

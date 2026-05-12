from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --- ui_schema: rendered by the frontend ---


class UIValidation(BaseModel):
    """Light client-side validation hints; strict rules live in qa_strategy / server."""

    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    min: float | None = None
    max: float | None = None
    # Human-readable error shown when `pattern` (or length/range) fails.
    # English only, concise, actionable — tell the user *how* to fix it.
    message: str | None = None


UIComponent = Literal[
    "text",
    "textarea",
    "select",
    "multiselect",
    "number",
    "url",
    "evm_address",
    "image",
    "video",
    # Image-annotation primitives. Each captures structured coordinates of
    # marks drawn on top of an image. The image source is set via
    # meta.imageSource — either "upload" (the annotator owns the upload UI)
    # or { fromField: "<other_field_id>" } to reuse an image from a sibling
    # `image` field. See FormilyCodattaForm.jsx for stored value shapes.
    "image_keypoint",  # one (or many) {x, y} points
    "image_bbox",      # axis-aligned rectangle stored as 4 corners + center
    "image_circle",    # {cx, cy, r}
    # Combined annotator — image + several primitive types in ONE field. Used
    # when the user needs to mark multiple aspects of the same image (e.g. a
    # bounding box AND a center point). Stores image_url, annotated_image_url
    # (a canvas-rendered preview with overlays burnt in), plus any of:
    # bbox, keypoint, circle. Toolset is configured via meta.primitives.
    "image_annotation",
]


class UIField(BaseModel):
    id: str = Field(..., description="Stable field id; maps to business fields in data_schema")
    label: str
    component: UIComponent
    required: bool = False
    placeholder: str | None = None
    help_text: str | None = None
    options: list[str | dict[str, Any]] | None = Field(
        default=None,
        description="Select options: plain strings or {value, label[, parent]} objects (normalized server-side).",
    )
    validation: UIValidation | None = None
    meta: dict[str, Any] | None = Field(
        default=None,
        description="Optional extensions (e.g. form-engine layout, rules) — see schemas/codatta-master.schema.json",
    )


class UISchema(BaseModel):
    version: Literal["1.0"] = "1.0"
    title: str | None = None
    description: str | None = None
    fields: list[UIField] = Field(default_factory=list)


# --- qa_strategy: quality and consensus ---


class QAConsensusRule(BaseModel):
    type: Literal["majority_vote", "reputation_weight", "threshold_agreement", "manual_review"]
    description: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class QAStrategy(BaseModel):
    version: Literal["1.0"] = "1.0"
    validation_types: list[Literal["format", "cross_user_consensus", "expert_sample", "automated_rules"]] = Field(
        default_factory=list
    )
    consensus_rules: list[QAConsensusRule] = Field(default_factory=list)
    notes: str | None = None


# --- data_schema: storage model (user_id PK + status preset) ---


class DataFieldSpec(BaseModel):
    name: str
    type: Literal["string", "number", "boolean", "array", "object"]
    description: str | None = None
    required: bool = False
    default: Any | None = None
    meta: dict[str, Any] | None = Field(default=None, description="Optional extension metadata")


class DataSchema(BaseModel):
    """
    Core business rules:
    - Rows must use user_id as the primary deduplication dimension across submissions;
      do not design business dedupe solely on submission_id.
    - Must include status with default ADOPT for filtering valid campaign output.
    """

    version: Literal["1.0"] = "1.0"
    primary_key: Literal["user_id"] = "user_id"
    fields: list[DataFieldSpec] = Field(default_factory=list)
    status_default: Literal["ADOPT"] = "ADOPT"
    status_field_name: Literal["status"] = "status"


class MasterJSON(BaseModel):
    ui_schema: UISchema
    qa_strategy: QAStrategy
    data_schema: DataSchema


class GenerateFormResponse(BaseModel):
    session_id: str
    agent_message: str
    master: MasterJSON
    quick_suggestions: list[str] = Field(
        default_factory=list,
        description="Short next-step prompts for UI chips; from model + server fallback.",
    )


class FieldBuilderDraftItem(BaseModel):
    """One row from the Edits tab field builder before sending to the agent."""

    id: str = Field(default="", description="Stable id slug; empty means derive from label on the server")
    label: str = Field(..., description="Human label for the field")
    component: str | None = Field(
        default=None,
        description="Optional hint: text, textarea, select, multiselect, number, url, evm_address, image, video",
    )
    required: bool = False
    notes: str | None = Field(
        default=None,
        description="User's free-text intent for this field (any language); agent maps to English schema copy",
    )


class GenerateFormRequest(BaseModel):
    session_id: str
    user_message: str
    draft_fields: list[FieldBuilderDraftItem] | None = Field(
        default=None,
        description="Optional field-builder rows from the UI; appended for model analysis only",
    )


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class SeedDemoResponse(BaseModel):
    session_id: str
    messages: list[ChatMessage]
    master: MasterJSON
    quick_suggestions: list[str] = Field(default_factory=list)


class SaveMasterRequest(BaseModel):
    session_id: str
    master: dict[str, Any]


class SaveMasterResponse(BaseModel):
    master: MasterJSON
    info: str
    quick_suggestions: list[str] = Field(default_factory=list)


class RefineFieldDescriptionRequest(BaseModel):
    """Given current master and user intent, ask the model for clearer help_text / storage descriptions."""

    master: dict[str, Any]
    field_id: str = Field(..., description="Must match ui_schema.fields[].id")
    user_instruction: str = Field(..., description="How to rewrite descriptions (any input language)")


class RefineFieldDescriptionResponse(BaseModel):
    help_text: str | None = Field(default=None, description="Contributor-facing help_text; empty string may clear")
    data_description: str | None = Field(default=None, description="data_schema description for this field")
    agent_message: str = Field(default="", description="Short model note (English)")


class RefineLabelStudioXmlRequest(BaseModel):
    """Chat-driven edit of the Label Studio labeling interface XML."""

    current_xml: str = Field(..., description="The current label_config.xml the user sees in the Studio tab")
    user_instruction: str = Field(..., description="What to change about the XML (any input language)")
    # Optional but recommended: lets the agent reference $field_id binding variables that exist
    ui_fields: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of {id, label, component} from the form, used as binding-variable context",
    )


class RefineLabelStudioXmlResponse(BaseModel):
    xml: str = Field(..., description="Updated Label Studio XML; must start with <View> and end with </View>")
    agent_message: str = Field(default="", description="Short note describing the change, in the user's language")


def default_master_for_empty() -> MasterJSON:
    """Valid empty-form Master JSON (includes enforced data_schema rows)."""
    return MasterJSON(
        ui_schema=UISchema(
            title="New task form",
            description="Add fields via the conversation.",
            fields=[],
        ),
        qa_strategy=QAStrategy(
            validation_types=["format"],
            consensus_rules=[
                QAConsensusRule(
                    type="majority_vote",
                    description="Majority-vote consistency check",
                    params={"min_agree": 2},
                )
            ],
        ),
        data_schema=DataSchema(
            fields=[
                DataFieldSpec(
                    name="user_id",
                    type="string",
                    description="Primary key: contributor id for deduplication (not submission_id alone).",
                    required=True,
                ),
                DataFieldSpec(
                    name="status",
                    type="string",
                    description="Row status for filtering valid outputs.",
                    required=True,
                    default="ADOPT",
                ),
            ]
        ),
    )


def master_for_defi_seed() -> MasterJSON:
    """Seed Master for the DeFi demo (notes + chain allowlist)."""
    return MasterJSON(
        ui_schema=UISchema(
            title="Suspicious DeFi pool reporting",
            description="Each contributor submits one pool: chain, address, evidence, and notes.",
            fields=[
                UIField(
                    id="pool_address",
                    label="pool_address",
                    component="evm_address",
                    required=True,
                    placeholder="0x...",
                    validation=UIValidation(pattern=r"^0x[a-fA-F0-9]{40}$"),
                ),
                UIField(
                    id="chain",
                    label="chain",
                    component="select",
                    required=True,
                    options=["Ethereum", "Base", "Arbitrum"],
                ),
                UIField(
                    id="evidence_url",
                    label="evidence_url",
                    component="url",
                    required=False,
                    placeholder="https://",
                ),
                UIField(
                    id="notes",
                    label="notes",
                    component="textarea",
                    required=False,
                    placeholder="Why this pool looks suspicious…",
                ),
            ],
        ),
        qa_strategy=QAStrategy(
            validation_types=["format", "cross_user_consensus"],
            consensus_rules=[
                QAConsensusRule(
                    type="majority_vote",
                    description="Majority vote on address and chain",
                    params={"min_peer_reviews": 2},
                ),
                QAConsensusRule(
                    type="reputation_weight",
                    description="Higher weight for high-reputation contributors",
                    params={"weight_key": "contributor_reputation"},
                ),
            ],
            notes="EVM address and URL format checks; chain must be in the allowlist.",
        ),
        data_schema=DataSchema(
            fields=[
                DataFieldSpec(
                    name="user_id",
                    type="string",
                    description="Primary key: aggregate multiple submissions per contributor.",
                    required=True,
                ),
                DataFieldSpec(
                    name="status",
                    type="string",
                    description="Default ADOPT; may extend to REJECTED/PENDING later.",
                    required=True,
                    default="ADOPT",
                ),
                DataFieldSpec(name="pool_address", type="string", required=True),
                DataFieldSpec(name="chain", type="string", required=True),
                DataFieldSpec(name="evidence_url", type="string", required=False),
                DataFieldSpec(name="notes", type="string", required=False),
            ]
        ),
    )

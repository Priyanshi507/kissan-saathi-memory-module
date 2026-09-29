"""
Kissan Saathi backend API.

Run it:
    export GOOGLE_API_KEY=your-key-here   # from Google AI Studio
    export ADMIN_TOKEN=your-chosen-secret # for the admin dashboard
    uvicorn api:app --reload --host 0.0.0.0 --port 8000

Without GOOGLE_API_KEY set, /chat still runs the full pipeline end-to-end
(retrieval, logging, profile) and returns a [MOCK ANSWER] -- so the
plumbing can be tested before a real key exists.
"""

import os
import base64
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from farmer_memory import FarmerMemoryModule, SQLiteStorage
from farmer_memory.embeddings import embed_text as embed_text_stub
from procurement import payment_kb
from procurement.notify import NotificationService, sms_from_env
from procurement.registry import merge_farmers
from procurement.store import ProcurementStore
from routes import farmer_routes, ivr_routes, staff_routes
from routes.deps import Deps

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_MIN_SIMILARITY = 0.87  # calibrated against real Gemini embeddings, see calibrate_threshold.py

_client = None
if GOOGLE_API_KEY:
    from google import genai
    from google.genai import types
    _client = genai.Client(api_key=GOOGLE_API_KEY)

    def _gemini_embed(text: str):
        result = _client.models.embed_content(model="gemini-embedding-001", contents=text)
        return result.embeddings[0].values

    embed_fn = _gemini_embed
else:
    embed_fn = embed_text_stub

memory = FarmerMemoryModule(storage=SQLiteStorage("kissan_saathi.db"), embed_fn=embed_fn)
procurement_store = ProcurementStore(memory.storage)
notification_service = NotificationService(procurement_store, sms_from_env(dict(os.environ)))

app = FastAPI(title="Kissan Saathi API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


class HistoryTurn(BaseModel):
    role: str  # "farmer" | "assistant"
    text: str


class ChatRequest(BaseModel):
    farmer_id: str
    message: str
    crop: Optional[str] = None
    intent: Optional[str] = None
    history: Optional[list[HistoryTurn]] = None


class ChatResponse(BaseModel):
    answer: str
    used_memory: bool
    memory_note: Optional[str] = None  # e.g. "Recalled: wheat pest question from 5 days ago"
    is_grievance: bool = False  # true if this looks like a complaint/problem report, not a question


def _relative_time(ts: datetime) -> str:
    """'5 days ago', '3 months ago', 'over a year ago' -- durable phrasing
    for a UI label, not a precise timestamp that would look odd either way."""
    days = (datetime.utcnow() - ts).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago" if years >= 1 else "over a year ago"


def _memory_note(ctx) -> Optional[str]:
    """Builds the single most relevant recalled interaction into a short,
    demo-friendly label -- turns 'used_memory: true' from a claim the app
    just asserts into something the person can actually read and verify."""
    if not ctx.relevant_interactions:
        return None
    top = ctx.relevant_interactions[0]  # already ranked by relevance
    snippet = top.query_text.strip()
    if len(snippet) > 50:
        snippet = snippet[:50].rsplit(" ", 1)[0] + "..."
    return f'Recalled: "{snippet}" from {_relative_time(top.timestamp)}'


GRIEVANCE_TAG = "[GRIEVANCE]"


def call_llm(context_block: str, farmer_message: str, history: Optional[list] = None) -> tuple[str, bool]:
    """Returns (answer_text, is_grievance). Grievance detection is folded
    into this SAME call via a tag the model prepends -- deliberately not a
    second API call, which would add real latency on top of an already
    multi-call pipeline (embedding + generation already, for every message)."""
    if _client is None:
        msg_lower = farmer_message.lower()
        is_grievance = ("paid" in msg_lower and ("not" in msg_lower or "no " in msg_lower)) or any(
            w in msg_lower for w in ["cheated", "refused", "turned away", "unfair", "corrupt"]
        )
        mock_answer = f"[MOCK ANSWER] Got your message: '{farmer_message}'. Context available: {bool(context_block.strip())}"
        return (mock_answer, is_grievance)  # tag never enters the visible text at all in mock mode

    system_prompt = (
        "You are Kissan Saathi, an assistant helping Indian farmers with "
        "crop procurement, MSP, pests, and related questions. Reply in the "
        "same language the farmer wrote in. Use the farmer's past "
        "conversation history below if relevant. If nothing relevant is "
        "available, answer from general knowledge.\n\n"
        "IMPORTANT: if the farmer's message is reporting a PROBLEM rather "
        "than asking a general question -- e.g. a payment that hasn't "
        "arrived, being turned away or refused at a procurement centre, "
        "unfair grading or weighing, or any similar grievance -- your reply "
        "must have TWO parts: first, on its own line by itself, the exact "
        "tag " + GRIEVANCE_TAG + ", and second, immediately after it, a "
        "full written response -- do not stop after the tag, the tag alone "
        "is never a complete reply. That second part must include an "
        "empathetic acknowledgment followed by clear, concrete next steps: "
        "what to document (date, token/receipt number, centre name), who "
        "to contact (the procurement centre's nodal officer, or the state "
        "agriculture department's grievance cell), and that they should "
        "keep records of every interaction. Do not invent specific phone "
        "numbers, portal names, or URLs you are not certain exist -- give "
        "the type of channel, not a fabricated specific one. For a normal "
        "informational question, do NOT include this tag at all.\n\n"
        f"{context_block}"
    )

    contents = []
    for turn in (history or []):
        role = "user" if turn.role == "farmer" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=turn.text)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=farmer_message)]))

    response = _client.models.generate_content(
        model="gemini-3.6-flash",
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )
    text = response.text
    is_grievance = text.strip().startswith(GRIEVANCE_TAG)
    if is_grievance:
        text = text.strip()[len(GRIEVANCE_TAG):].lstrip("\n ")
        if len(text) < 20:
            text = (
                "I understand this is a real problem, not just a question. "
                "Please note down the date, your token or receipt number, "
                "and the procurement centre's name -- then raise this with "
                "the centre's nodal officer, or your state agriculture "
                "department's grievance cell. Keep a record of every "
                "conversation about this until it's resolved."
            )
    return text, is_grievance


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    kwargs = {"min_similarity": GEMINI_MIN_SIMILARITY} if _client else {}
    ctx = memory.retrieve_context(farmer_id=req.farmer_id, query_text=req.message, **kwargs)
    context_block = ctx.as_prompt_text()

    answer, is_grievance = call_llm(context_block, req.message, history=req.history)

    memory.log_interaction(
        farmer_id=req.farmer_id,
        query_text=req.message,
        response_text=answer,
        crop=req.crop,
        intent=req.intent,
        is_grievance=is_grievance,
    )

    return ChatResponse(
        answer=answer,
        used_memory=bool(ctx.relevant_interactions),
        memory_note=_memory_note(ctx),
        is_grievance=is_grievance,
    )


class TranscribeRequest(BaseModel):
    audio_base64: str
    mime_type: str = "audio/mp4"


@app.post("/transcribe")
def transcribe(req: TranscribeRequest):
    if _client is None:
        raise HTTPException(503, "Transcription requires GOOGLE_API_KEY to be set")

    audio_bytes = base64.b64decode(req.audio_base64)
    response = _client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[
            types.Part.from_bytes(data=audio_bytes, mime_type=req.mime_type),
            "Transcribe exactly what is said in this audio clip. Output ONLY "
            "the transcription, in the language it was spoken in (do not "
            "translate it), with no extra commentary or labels.",
        ],
    )
    return {"text": response.text.strip()}


class ChatImageRequest(BaseModel):
    farmer_id: str
    message: Optional[str] = None
    crop: Optional[str] = None
    image_base64: str
    mime_type: str = "image/jpeg"


@app.post("/chat/image", response_model=ChatResponse)
def chat_image(req: ChatImageRequest):
    if _client is None:
        raise HTTPException(503, "Image understanding requires GOOGLE_API_KEY to be set")

    query_text = req.message.strip() if req.message and req.message.strip() else "[Farmer shared a photo]"
    image_bytes = base64.b64decode(req.image_base64)

    kwargs = {"min_similarity": GEMINI_MIN_SIMILARITY}
    ctx = memory.retrieve_context(farmer_id=req.farmer_id, query_text=query_text, **kwargs)

    system_prompt = (
        "You are Kissan Saathi, an assistant helping Indian farmers with "
        "crop procurement, MSP, pests, and related questions. A farmer has "
        "shared a photo, most likely of a crop, pest, disease symptom, or "
        "document. Look at the image carefully and answer helpfully. Reply "
        "in the same language as any caption text the farmer wrote.\n\n"
        f"{ctx.as_prompt_text()}"
    )

    response = _client.models.generate_content(
        model="gemini-3.6-flash",
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=req.mime_type), query_text],
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )
    answer = response.text

    memory.log_interaction(farmer_id=req.farmer_id, query_text=query_text, response_text=answer, crop=req.crop)
    return ChatResponse(answer=answer, used_memory=bool(ctx.relevant_interactions), memory_note=_memory_note(ctx))



@app.post("/farmer/{farmer_id}/consolidate")
def consolidate(farmer_id: str, x_admin_token: Optional[str] = Header(None)):
    """Rebuilds a farmer's memory summary with the AI service. Staff only: it costs an AI call."""
    require_admin(x_admin_token)
    profile = memory.consolidate_profile(farmer_id)
    return {"farmer_id": farmer_id, "summary": profile.summary_text}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "llm_configured": _client is not None,
        "embeddings": "gemini-embedding-001" if _client else "stub (bag-of-words)",
        "time": datetime.utcnow().isoformat(),
    }


# ==================== ADMIN / STAFF DASHBOARD ====================
# Read-mostly views over the SAME interactions table every farmer chat
# already writes to -- no new data collection, just a different way of
# looking at what's already logged. Deliberately minimal auth (one shared
# token, not per-user accounts) -- appropriate for a hackathon demo, NOT
# for a real deployment. Swap ADMIN_TOKEN's default for a real secret
# (and move to per-staff accounts) before this touches real farmer data.

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "kissan-admin-2026")


def require_admin(x_admin_token: Optional[str] = Header(None)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Invalid or missing admin token")


@app.get("/admin/overview")
def admin_overview(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    overview = memory.admin_overview()
    overview["total_farmers"] = len(merge_farmers(memory.admin_farmers(None), procurement_store.list_registered()))
    return {**overview, "procurement": procurement_store.stats()}


@app.get("/admin/trends")
def admin_trends(days: int = 14, x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    return memory.admin_trends(days)


@app.get("/admin/recent-activity")
def admin_recent_activity(limit: int = 12, x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    return memory.admin_recent_activity(limit)


# ==================== PROCUREMENT, PAYMENTS, IVR ====================

def _classify_payment_issue(text, image_bytes, mime_type):
    if _client is None:
        return None
    parts = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type)] if image_bytes else []
    parts.append(text or "Read the message shown in the image.")
    response = _client.models.generate_content(
        model="gemini-3.6-flash",
        contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=payment_kb.classifier_prompt(),
            response_mime_type="application/json",
        ),
    )
    return payment_kb.parse_code(response.text)


def _ivr_answer(farmer_id, text, language):
    kwargs = {"min_similarity": GEMINI_MIN_SIMILARITY} if _client else {}
    ctx = memory.retrieve_context(farmer_id=farmer_id, query_text=text, **kwargs)
    answer, is_grievance = call_llm(ctx.as_prompt_text(), text)
    memory.log_interaction(farmer_id=farmer_id, query_text=text, response_text=answer,
                           channel="ivr", is_grievance=is_grievance)
    return answer


_deps = Deps(
    memory=memory,
    store=procurement_store,
    notifications=notification_service,
    check_admin=require_admin,
    classify_issue=_classify_payment_issue if _client else None,
    answer=_ivr_answer,
    ivr_auth_token=os.environ.get("TWILIO_AUTH_TOKEN"),
    public_base_url=os.environ.get("PUBLIC_BASE_URL"),
    ivr_language=os.environ.get("IVR_LANGUAGE", "hi-IN"),
    ivr_allow_unsigned=os.environ.get("IVR_ALLOW_UNSIGNED") == "1",
)
app.include_router(farmer_routes.build(_deps))
app.include_router(staff_routes.build(_deps))
app.include_router(ivr_routes.build(_deps))

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
import uuid
from app.ai_engine import AncestryAIEngine
from graph_service import FamilyTreeService
from database import init_db, test_connection
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Ancestry MVP API")

# Initialize DB
if test_connection():
    init_db()

# ─────────────────────────────────────────────
# SESSIONS
# ─────────────────────────────────────────────
# No real auth yet — a "session" is just a fresh UUID that doubles as the
# user_id for every person/relationship/event created under it. This is the
# seam real auth will eventually replace: swap session issuance for a login
# flow, keep everything downstream (which all takes a user_id) unchanged.
#
# In-memory, same caveat as pending_confirmations below: won't survive a
# restart or multiple uvicorn workers. A session that disappears on restart
# just means the family tree tied to that UUID becomes unreachable (data
# isn't lost — it's still in Postgres under that user_id — but there's no
# login to get back to it). Move to Redis/DB-backed sessions before this
# goes further than local testing.
active_sessions: set = set()

try:
    ai_engine = AncestryAIEngine(google_api_key=os.getenv("GEMINI_API_KEY"))
except Exception as e:
    print(f"Warning: AI engine not initialized: {e}")
    ai_engine = None


# ─────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────

# In-memory store for pending confirmations, keyed by user_id string.
# Values are either:
#   - the string "AWAITING_DELETE_CONFIRM"  (unchanged from before)
#   - a confirmation.PendingProposal instance (new — for CREATE_* confirmations)
#
# NOTE: this is in-memory and single-process. It will not survive a restart
# or run correctly across multiple uvicorn workers — a "yes" landing on a
# different worker than the one holding the PendingProposal will silently
# find nothing pending. Fine for solo-dev MVP; flag to backend eng to move
# this to Redis (short TTL) or a DB table before running >1 worker.
pending_confirmations: dict = {}

# In-memory job tracker for interview processing, keyed by a fresh interview_id.
# Same in-memory caveat as everything else in this file. Values:
#   {"status": "processing"}
#   {"status": "ready", "transcript": ..., "confirmation_message": ..., "has_content": bool}
#   {"status": "error", "error": "..."}
#
# NOTE: audio bytes themselves are NOT persisted anywhere right now — they're
# read into memory, sent to Gemini for transcription, and discarded. Once
# object storage (S3/R2/etc.) is wired up on the fullstack side, this
# endpoint should persist the file there before/alongside processing rather
# than only ever holding it transiently in a worker process.
interview_jobs: dict = {}

class SessionResponse(BaseModel):
    session_id: str


class ChatRequest(BaseModel):
    """
    Single entry point for ALL user interactions.
    The AI engine classifies intent and acts accordingly:
      - "Add my grandfather Adewale, born 1920, Yoruba from Ibadan"  → proposes CREATE_PERSON, awaits confirmation
      - "Adewale is the father of Tunde"                             → proposes CREATE_RELATIONSHIP, awaits confirmation
      - "Record that Tunde graduated in 1985 in Lagos"               → proposes CREATE_EVENT, awaits confirmation
      - "yes" / "confirm"                                            → commits the pending proposal
      - "no" / "cancel"                                              → discards the pending proposal
      - anything else while a proposal is pending                    → treated as a correction, re-proposed
      - "Tell me the story of Adewale as a griot"                    → STORY
      - "Who is Tunde's father?"                                     → QUERY
      - "delete my entire family tree"                               → DELETE_FAMILY (warning)
      - "YES DELETE EVERYTHING"                                      → CONFIRM deletion

    session_id must come from POST /session/ first — call it once, then
    reuse the returned session_id on every /chat/ call for that family tree.
    """
    session_id: str
    text: str
    language: Optional[str] = "en"


class StoryRequest(BaseModel):
    """Direct story generation for a known person_id"""
    session_id: str
    person_id: str
    style: Optional[str] = "griot"   # griot | modern | children
    language: Optional[str] = "en"


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.post("/session/", response_model=SessionResponse)
async def create_session():
    """
    Mints a fresh session_id. Call this once per family tree you're working
    on, then pass the returned session_id in every /chat/ and /story/ call.
    There's no login yet — the session_id itself is what scopes data, so
    losing it means losing access to that tree (the data stays in Postgres,
    there's just no way back to it without the UUID).
    """
    session_id = str(uuid.uuid4())
    active_sessions.add(session_id)
    return {"session_id": session_id}


def _resolve_user_id(session_id: str) -> uuid.UUID:
    if session_id not in active_sessions:
        raise HTTPException(
            status_code=404,
            detail="Unknown session_id. Call POST /session/ first and use the returned id."
        )
    return uuid.UUID(session_id)


@app.post("/chat/")
async def chat(request: ChatRequest):
    """
    Main conversational endpoint.
    Handles adding people, relationships, events, queries, and stories
    — all via natural language, gated by a confirm-before-write step for
    anything that would create/modify data. No separate create endpoints needed.
    """
    if not ai_engine:
        raise HTTPException(status_code=503, detail="AI engine not available")

    user_id = _resolve_user_id(request.session_id)
    user_key = request.session_id

    try:
        # Check if we are waiting for the user to confirm/correct a pending
        # action — either a destructive delete or a create proposal.
        pending = pending_confirmations.get(user_key)

        result = ai_engine.process_query(
            user_id,
            request.text,
            request.language,
            pending_action=pending
        )

        returned_action = result["action"]

        # ── Update pending-confirmation state for the NEXT message ──────
        # Three possible outcomes:
        #   1. result carries a new/updated PendingProposal ("_pending")
        #      → store it, replacing whatever was there before
        #   2. result is the delete warning (no "_pending" key, just the flag)
        #      → store the string flag, same as before
        #   3. anything else (committed, cancelled, query, story, error)
        #      → this is a terminal outcome; clear whatever was pending
        if "_pending" in result:
            pending_confirmations[user_key] = result["_pending"]
        elif returned_action == "AWAITING_DELETE_CONFIRM":
            pending_confirmations[user_key] = "AWAITING_DELETE_CONFIRM"
        else:
            pending_confirmations.pop(user_key, None)

        return {
            "response": result["response"],
            "action": returned_action,
            "language": request.language,
            **{k: v for k, v in result.items()
               if k not in ("response", "action", "context_used", "_pending")}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/story/")
async def generate_story(request: StoryRequest):
    """
    Direct story generation endpoint when you already know the person_id.
    For story requests via natural language, use /chat/ instead.
    """
    if not ai_engine:
        raise HTTPException(status_code=503, detail="AI engine not available")

    user_id = _resolve_user_id(request.session_id)

    try:
        story = ai_engine.generate_family_story(
            user_id,
            uuid.UUID(request.person_id),
            style=request.style,
            language=request.language
        )
        return {"story": story, "style": request.style}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _run_interview_pipeline(interview_id: str, user_id: uuid.UUID, session_id: str,
                            person_id: str, audio_bytes: bytes, mime_type: str, language: str):
    """
    Runs off the request thread via BackgroundTasks — transcription +
    extraction can take well over the length of a normal request for
    anything but a short clip. Writes its result into interview_jobs for
    GET /interview/{interview_id} to poll, and — if anything was extracted —
    drops the resulting PendingProposal into pending_confirmations exactly
    as /chat/ would, so confirming/correcting it afterward is just a normal
    /chat/ call with "yes" or a correction. No new confirmation logic needed.
    """
    try:
        result = ai_engine.process_interview(user_id, audio_bytes, mime_type, language)

        if result["pending"]:
            pending_confirmations[session_id] = result["pending"]

        interview_jobs[interview_id] = {
            "status": "ready",
            "person_id": person_id,
            "transcript": result["transcript"],
            "confirmation_message": result["confirmation_message"],
            "has_content": result["has_content"],
            "next_step": (
                "Reply via POST /chat/ with this session_id and 'yes' to save, "
                "or tell me what to correct."
            ) if result["has_content"] else None
        }
    except Exception as e:
        interview_jobs[interview_id] = {"status": "error", "person_id": person_id, "error": str(e)}


@app.post("/person/{person_id}/interview")
async def upload_interview(
    background_tasks: BackgroundTasks,
    person_id: str,
    session_id: str = Form(...),
    language: str = Form("en"),
    audio: UploadFile = File(...),
):
    """
    Upload an audio recording (an oral-history interview, a voice note) for
    transcription and extraction. Processing happens in the background —
    this returns immediately with an interview_id to poll via
    GET /interview/{interview_id}.

    person_id anchors who this interview is primarily about (for future
    biography generation grounded in the transcript) — it doesn't restrict
    what extraction can find. If the recording mentions other relatives,
    they're extracted the same as anything from a /chat/ message.

    Supported audio: wav, mp3, aiff, aac, ogg, flac.
    """
    if not ai_engine:
        raise HTTPException(status_code=503, detail="AI engine not available")

    user_id = _resolve_user_id(session_id)

    audio_bytes = await audio.read()
    mime_type = audio.content_type or "audio/mpeg"

    interview_id = str(uuid.uuid4())
    interview_jobs[interview_id] = {"status": "processing", "person_id": person_id}

    background_tasks.add_task(
        _run_interview_pipeline,
        interview_id, user_id, session_id, person_id, audio_bytes, mime_type, language
    )

    return {"interview_id": interview_id, "status": "processing"}


@app.get("/interview/{interview_id}")
async def get_interview_status(interview_id: str):
    """
    Poll for interview processing status. Once status is "ready", the
    transcript and confirmation_message are included — reply via /chat/
    with the same session_id to confirm, correct, or cancel what was found.
    """
    job = interview_jobs.get(interview_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown interview_id")
    return job


@app.get("/family-tree/{person_id}")
async def get_tree(person_id: str, depth: int = 2):
    """Get family tree for a given person"""
    try:
        service = FamilyTreeService()
        tree = service.get_family_tree(uuid.UUID(person_id), depth)
        return tree
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/health")
async def health_check():
    return {"status": "healthy", "database": "connected"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
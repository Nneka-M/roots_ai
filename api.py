from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import uuid
from ai_engine import AncestryAIEngine
from graph_service import FamilyTreeService
from database import init_db, test_connection
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Ancestry MVP API")

# Initialize DB
if test_connection():
    init_db()

# Fixed test user for MVP
TEST_USER_ID = uuid.UUID("12345678-1234-1234-1234-123456789abc")

try:
    ai_engine = AncestryAIEngine(google_api_key=os.getenv("GEMINI_API_KEY"))
except Exception as e:
    print(f"Warning: AI engine not initialized: {e}")
    ai_engine = None


# ─────────────────────────────────────────────
# REQUEST MODELS
# ─────────────────────────────────────────────
pending_confirmations:dict = {}  # In-memory store for pending confirmations (e.g. {"confirmation_id": {"action": "CREATE_PERSON", "data": {...}}})

class ChatRequest(BaseModel):
    """
    Single entry point for ALL user interactions.
    The AI engine classifies intent and acts accordingly:
      - "Add my grandfather Adewale, born 1920, Yoruba from Ibadan"  → CREATE_PERSON
      - "Adewale is the father of Tunde"                             → CREATE_RELATIONSHIP
      - "Record that Tunde graduated in 1985 in Lagos"               → CREATE_EVENT
      - "Tell me the story of Adewale as a griot"                    → STORY
      - "Who is Tunde's father?"                                     → QUERY
        - 'delete my entire family tree'                            → DELETE_FAMILY (warning)  
        -"YES DELETE EVERYTHING"                                    → CONFIRM deletion                               
    """
    text: str
    language: Optional[str] = "en"


class StoryRequest(BaseModel):
    """Direct story generation for a known person_id"""
    person_id: str
    style: Optional[str] = "griot"   # griot | modern | children
    language: Optional[str] = "en"


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.post("/chat/")
async def chat(request: ChatRequest):
    """
    Main conversational endpoint.
    Handles adding people, relationships, events, queries, and stories
    — all via natural language. No separate create endpoints needed.
    """
    if not ai_engine:
        raise HTTPException(status_code=503, detail="AI engine not available")

    user_key = str(TEST_USER_ID)
 
    try:
        # Check if we are waiting for the user to confirm a destructive action
        pending = pending_confirmations.get(user_key)
 
        result = ai_engine.process_query(
            TEST_USER_ID,
            request.text,
            request.language,
            pending_action=pending        # passes "AWAITING_DELETE_CONFIRM" or None
        )
 
        returned_action = result["action"]
 
        # If the engine is now waiting for confirmation, register that state
        if returned_action == "AWAITING_DELETE_CONFIRM":
            pending_confirmations[user_key] = "AWAITING_DELETE_CONFIRM"
 
        # On any terminal outcome (confirmed, cancelled, or unrelated action),
        # clear the pending flag
        elif pending and returned_action in ("DELETE_COMPLETE", "DELETE_CANCELLED", "ERROR"):
            pending_confirmations.pop(user_key, None)
 
        elif pending and returned_action not in ("AWAITING_DELETE_CONFIRM",):
            # User typed something unrelated while a confirmation was pending —
            # treat it as a cancellation
            pending_confirmations.pop(user_key, None)
 
        return {
            "response": result["response"],
            "action": returned_action,
            "language": request.language,
            **{k: v for k, v in result.items()
               if k not in ("response", "action", "context_used")}
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

    try:
        story = ai_engine.generate_family_story(
            TEST_USER_ID,
            uuid.UUID(request.person_id),   # ← fixed: was passing style instead of person_id
            style=request.style,
            language=request.language
        )
        return {"story": story, "style": request.style}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
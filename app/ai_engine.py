"""
ai_engine.py

Thin orchestrator. Owns the LLM client instances (so there's exactly one
place API keys and model config live) and process_query()'s routing logic.
Does NOT contain extraction prompts, DB writes, confirmation-message
formatting, or story-generation prompts anymore — those live in
extraction.py, handlers.py, confirmation.py, and narrative.py respectively.

process_query() is a state machine with three entry states:
  1. pending_action == "AWAITING_DELETE_CONFIRM"  → delete confirmation gate
  2. pending_action is a PendingProposal            → create confirmation gate
  3. pending_action is None                         → fresh extraction, route by action

Deletion logic (_handle_delete_warning / _handle_delete_confirmation) stays
inline here rather than in its own deletion.py for now — it's two short
methods and splitting it out didn't earn its own file yet. Easy to extract
later if it grows.
"""

from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from typing import Dict, Optional
import uuid
import os
import traceback

from database import SessionLocal, Person
from .extraction import EntityExtractor
from .confrmation import PendingProposal, is_confirmation, is_cancellation, build_confirmation_message
from .handlers import commit_proposal
from .query import get_all_family_data, find_person_by_name, handle_query, resolve_references
from .narrative import generate_family_story as _generate_family_story
from .transcription import transcribe_audio

CREATE_ACTIONS = {"CREATE_PERSON", "CREATE_FAMILY_BATCH", "CREATE_RELATIONSHIP", "CREATE_EVENT"}


class AncestryAIEngine:
    def __init__(self, google_api_key: str = None):
        self.llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=google_api_key or os.getenv("GOOGLE_API_KEY"),
            temperature=0.3,  # Lower = more deterministic extraction
        )

        self.embeddings = GoogleGenerativeAIEmbeddings(
            model="gemini-embedding-001",
            google_api_key=google_api_key or os.getenv("GOOGLE_API_KEY")
        )

        self.extractor = EntityExtractor(self.llm)

    # ─────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────

    def process_query(self, user_id: uuid.UUID, query: str, language: str = "en",
                      pending_action: Optional[object] = None) -> Dict:
        """
        Single chat endpoint. Routes to the correct handler based on
        LLM-extracted intent, gated by whichever confirmation state (if any)
        the API layer has stored for this user.

        pending_action:
          - "AWAITING_DELETE_CONFIRM" (str)  → this message is a yes/no answer to a delete warning
          - PendingProposal instance         → this message is a confirm/cancel/correction of a create proposal
          - None                             → fresh message, extract and route normally
        """
        try:
            # ── Delete confirmation gate ──────────────────────────────
            if pending_action == "AWAITING_DELETE_CONFIRM":
                return self._handle_delete_confirmation(user_id, query, language)

            # ── Create confirmation gate ──────────────────────────────
            if isinstance(pending_action, PendingProposal):
                if is_confirmation(query):
                    return commit_proposal(user_id, pending_action, self.extractor)

                if is_cancellation(query):
                    return {
                        "response": "🛑 Cancelled — nothing was saved.",
                        "action": "CREATE_CANCELLED",
                        "context_used": 0
                    }

                # Anything else = a correction. Re-extract with the original
                # text + correction combined, so the LLM has full context to
                # revise the same proposal rather than starting from scratch.
                combined = f"{pending_action.original_text}\n\nCorrection: {query}"
                revised = self.extractor.extract_all_entities(combined)
                revised = resolve_references(user_id, revised)
                new_pending = PendingProposal(revised, combined, language)
                return {
                    "response": build_confirmation_message(revised),
                    "action": "AWAITING_CREATE_CONFIRM",
                    "context_used": 0,
                    "_pending": new_pending,
                }

            # ── Fresh message — extract and route ─────────────────────
            extracted = self.extractor.extract_all_entities(query)
            action = extracted.get("action", "QUERY")

            if action in CREATE_ACTIONS:
                extracted = resolve_references(user_id, extracted)
                pending = PendingProposal(extracted, query, language)
                return {
                    "response": build_confirmation_message(extracted),
                    "action": "AWAITING_CREATE_CONFIRM",
                    "context_used": 0,
                    "_pending": pending,
                }

            elif action == "STORY":
                return self._handle_story(user_id, extracted, language)

            elif action == "DELETE_FAMILY":
                return self._handle_delete_warning(user_id, language)

            else:
                return handle_query(user_id, query, language, self.llm, self.extractor)

        except Exception as e:
            print(f"ERROR in process_query: {str(e)}")
            print(f"TRACEBACK: {traceback.format_exc()}")
            return {
                "response": f"Error processing query: {str(e)}",
                "action": "ERROR",
                "context_used": 0
            }

    # ─────────────────────────────────────────────
    # STORY
    # ─────────────────────────────────────────────

    def _handle_story(self, user_id: uuid.UUID, extracted: Dict, language: str) -> Dict:
        all_persons = get_all_family_data(user_id)
        if not all_persons:
            return {"response": "No family members found. Add someone first!", "action": "STORY", "context_used": 0}

        story_ref = extracted.get("story_person_ref")
        persons_data = extracted.get("persons", [])
        person_name = next(
            (p["name"] for p in persons_data if p["ref_key"] == story_ref and p.get("name")),
            None
        ) if story_ref else None

        target = find_person_by_name(person_name, all_persons) if person_name else all_persons[0]
        story = self.generate_family_story(
            user_id,
            uuid.UUID(target["id"]),
            style=extracted.get("story_style", "griot"),
            language=language
        )
        return {"response": story, "action": "STORY", "context_used": len(all_persons)}

    def generate_family_story(self, user_id: uuid.UUID, person_id: uuid.UUID,
                              style: str = "griot", language: str = "en") -> str:
        """
        Public method — called directly by api.py's /story/ endpoint when
        the caller already knows person_id (bypassing process_query/extraction
        entirely, same as the original design).
        """
        all_persons = get_all_family_data(user_id)
        return _generate_family_story(self.llm, user_id, person_id, all_persons, style, language)

    # ─────────────────────────────────────────────
    # INTERVIEW  (audio → transcript → extraction, non-destructive)
    # ─────────────────────────────────────────────

    def process_interview(self, user_id: uuid.UUID, audio_bytes: bytes,
                          mime_type: str, language: str = "en") -> Dict:
        """
        Full interview pipeline: audio → transcript → extraction → entity
        resolution. Writes nothing to the DB — returns a PendingProposal
        (or None if nothing was extracted) so the caller can drop it straight
        into pending_confirmations and let the existing /chat/ confirm/
        correct/cancel flow handle the rest, unchanged.

        Interview transcripts are free-form reminiscence, not a single
        deliberate command — the extraction prompt's action classifier is
        tuned for short chat messages ("Add my dad...", "Who is...") and
        can't be trusted to pick a sensible action for a rambling life story.
        We force CREATE_FAMILY_BATCH whenever anything was extracted, since
        the point of an interview is always "capture everything mentioned,"
        never a query or a story request.
        """
        transcript = transcribe_audio(self.llm, audio_bytes, mime_type)
        extracted = self.extractor.extract_all_entities(transcript)

        has_content = any(extracted.get(k) for k in ("persons", "relationships", "events", "migrations"))
        if has_content:
            extracted["action"] = "CREATE_FAMILY_BATCH"

        extracted = resolve_references(user_id, extracted)

        if has_content:
            pending = PendingProposal(extracted, transcript, language)
            message = build_confirmation_message(extracted)
        else:
            pending = None
            message = "I transcribed the recording but couldn't identify any new family details to add."

        return {
            "transcript": transcript,
            "pending": pending,
            "confirmation_message": message,
            "has_content": has_content
        }

    # ─────────────────────────────────────────────
    # DELETE FAMILY TREE  (two-step: warn → confirm)
    # ─────────────────────────────────────────────

    def _handle_delete_warning(self, user_id: uuid.UUID, language: str) -> Dict:
        """
        Step 1 — show a stern warning and a count of what will be deleted.
        Returns action=AWAITING_DELETE_CONFIRM so the API layer can set the
        pending_action flag for the next message.
        """
        all_persons = get_all_family_data(user_id)
        count = len(all_persons)

        if count == 0:
            return {
                "response": "Your family tree is already empty — nothing to delete.",
                "action": "QUERY",
                "context_used": 0
            }

        names = ", ".join(p["name"] for p in all_persons[:5])
        if count > 5:
            names += f", and {count - 5} more"

        warning = (
            f"⚠️  **This will permanently delete your entire family tree.**\n\n"
            f"You currently have **{count} family member{'s' if count != 1 else ''}** on record "
            f"({names}).\n\n"
            f"All people, relationships, and events will be erased and **cannot be recovered**.\n\n"
            f"Type **YES, DELETE EVERYTHING** to confirm, or anything else to cancel."
        )

        return {
            "response": warning,
            "action": "AWAITING_DELETE_CONFIRM",
            "context_used": count
        }

    def _handle_delete_confirmation(self, user_id: uuid.UUID, reply: str, language: str) -> Dict:
        """
        Step 2 — called when the API layer knows we're awaiting confirmation.
        Accepts only the exact phrase 'YES, DELETE EVERYTHING' (case-insensitive).
        """
        if reply.strip().upper() != "YES, DELETE EVERYTHING":
            return {
                "response": "🛑 Deletion cancelled. Your family tree is safe.",
                "action": "DELETE_CANCELLED",
                "context_used": 0
            }

        db = SessionLocal()
        try:
            deleted = db.query(Person).filter(Person.user_id == user_id).delete(
                synchronize_session="fetch"
            )
            db.commit()
            return {
                "response": (
                    f"✅ Done. **{deleted} family member{'s' if deleted != 1 else ''}** "
                    f"and all associated relationships and events have been permanently deleted. "
                    f"Your family tree is now empty."
                ),
                "action": "DELETE_COMPLETE",
                "deleted_count": deleted,
                "context_used": 0
            }
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()
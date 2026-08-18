"""
confirmation.py

Generalizes the existing AWAITING_DELETE_CONFIRM pattern to every action that
writes to the tree: CREATE_FAMILY_BATCH, CREATE_PERSON, CREATE_RELATIONSHIP,
CREATE_EVENT — and later, interview-derived extraction.

Nothing is written to the DB during extraction (see extraction.py — it's
pure LLM-in/dict-out). Extraction produces a *proposal*; the proposal is
shown to the user as a plain-language summary; only an explicit "yes"
commits it. Anything else is treated as a correction and re-extracted,
keeping the user in the loop until they confirm.

Owns:
  - PendingProposal        — wraps a pending extraction awaiting confirmation
  - build_confirmation_message() — renders a proposal as a readable, ⚠️-flagged summary
  - is_confirmation() / is_cancellation() — reply classification

NOTE on storage: pending_confirmations currently lives in an in-memory dict
in api.py, keyed by user_id. That's fine for single-worker MVP, but won't
survive a restart or multiple uvicorn workers. Flag to backend eng: this
should move to a short-TTL Redis key (or a `pending_confirmations` DB table)
before this goes past one dev instance — a lost pending confirmation isn't
a functional bug, but a confirmed "yes" landing against a *stale* proposal
after a worker restart would be a real one.
"""

from typing import Dict, Optional

CONFIRM_WORDS = {"yes", "confirm", "correct", "yep", "yeah", "y", "looks good", "that's right", "sure"}
CANCEL_WORDS = {"no", "cancel", "stop", "nevermind", "never mind"}

# Any extracted field/entity below this confidence gets a ⚠️ flag in the
# confirmation message, prompting the user to double-check it specifically.
LOW_CONFIDENCE_THRESHOLD = 0.6


def is_confirmation(reply: str) -> bool:
    return reply.strip().lower() in CONFIRM_WORDS


def is_cancellation(reply: str) -> bool:
    return reply.strip().lower() in CANCEL_WORDS


def _name_for_ref(ref_key: Optional[str], persons: list) -> Optional[str]:
    """Resolve a ref_key to a display name — prefers the existing DB name if
    this ref matched an existing person, falls back to the extracted name."""
    p = next((p for p in persons if p.get("ref_key") == ref_key), None)
    if not p:
        return ref_key
    existing = p.get("existing_match")
    return existing["name"] if existing else (p.get("name") or ref_key)


def build_confirmation_message(extracted: Dict) -> str:
    """
    Turn a raw extraction dict (from EntityExtractor.extract_all_entities,
    annotated by query.resolve_references) into a human-readable proposal,
    flagging any field below LOW_CONFIDENCE_THRESHOLD so the user knows
    exactly what to double-check before confirming.

    Action-aware: CREATE_PERSON/CREATE_FAMILY_BATCH preview persons as
    new-vs-already-in-tree; CREATE_RELATIONSHIP/CREATE_EVENT don't create
    any persons at all, so they preview as links against the existing tree
    and warn upfront if a referenced person can't be found — rather than
    only surfacing that failure after the user confirms.
    """
    action = extracted.get("action")
    persons = extracted.get("persons", [])
    relationships = extracted.get("relationships", [])
    events = extracted.get("events", [])
    migrations = extracted.get("migrations", [])

    if not (persons or relationships or events or migrations):
        return ("I couldn't identify anything to add from that message. "
                "Try including full names and relationships, e.g. "
                "'My dad Emmanuel Maduike is from Nkwerre, Imo.'")

    lines = ["Here's what I found — please check it before I save anything:\n"]

    if action in ("CREATE_PERSON", "CREATE_FAMILY_BATCH"):
        for p in persons:
            name = p.get("name") or f"({p.get('ref_key')} — no name given)"
            existing = p.get("existing_match")
            if existing:
                lines.append(f"🔁 **{name}** — already in your tree as **{existing['name']}**; "
                             f"will link instead of creating a duplicate.")
                continue
            flag = " ⚠️ low confidence overall" if p.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD else ""
            lines.append(f"👤 **{name}** (new){flag}")
            for field, conf in (p.get("field_confidence") or {}).items():
                if conf < LOW_CONFIDENCE_THRESHOLD:
                    lines.append(f"   ⚠️ {field}: **{p.get(field)}** (inferred — please verify)")

        for rel in relationships:
            from_name = _name_for_ref(rel.get("from_ref"), persons)
            to_name = _name_for_ref(rel.get("to_ref"), persons)
            flag = " ⚠️" if rel.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD else ""
            rel_label = (rel.get("relationship_type") or "").replace("_", " ").title()
            lines.append(f"🔗 {from_name} → {rel_label} → {to_name}{flag}")

        for ev in events:
            person_name = _name_for_ref(ev.get("person_ref"), persons)
            flag = " ⚠️" if ev.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD else ""
            lines.append(f"📅 {ev.get('event_type')} — {person_name} "
                         f"({ev.get('event_date') or 'date unknown'}){flag}")

    elif action == "CREATE_RELATIONSHIP":
        for rel in relationships:
            from_p = next((p for p in persons if p.get("ref_key") == rel.get("from_ref")), None)
            to_p = next((p for p in persons if p.get("ref_key") == rel.get("to_ref")), None)
            from_match = from_p.get("existing_match") if from_p else None
            to_match = to_p.get("existing_match") if to_p else None
            from_name = from_match["name"] if from_match else (from_p.get("name") if from_p else rel.get("from_ref"))
            to_name = to_match["name"] if to_match else (to_p.get("name") if to_p else rel.get("to_ref"))

            missing = [n for n, m in ((from_name, from_match), (to_name, to_match)) if not m]
            if missing:
                lines.append(f"⚠️ Couldn't find **{' and '.join(missing)}** in your tree — "
                             f"add {'them' if len(missing) > 1 else 'them'} first, then retry this relationship.")
                continue

            rel_label = (rel.get("relationship_type") or "").replace("_", " ").title()
            lines.append(f"🔗 Linking existing: **{from_name}** → {rel_label} → **{to_name}**")

    elif action == "CREATE_EVENT":
        for ev in events:
            person_p = next((p for p in persons if p.get("ref_key") == ev.get("person_ref")), None)
            match = person_p.get("existing_match") if person_p else None
            person_name = match["name"] if match else (person_p.get("name") if person_p else ev.get("person_ref"))

            if not match:
                lines.append(f"⚠️ Couldn't find **{person_name}** in your tree — add them first, then retry.")
                continue

            lines.append(f"📅 Attaching to existing: **{person_name}** — {ev.get('event_type')} "
                         f"({ev.get('event_date') or 'date unknown'})")

    for mig in migrations:
        person_name = _name_for_ref(mig.get("person_ref"), persons)
        flag = " ⚠️" if mig.get("confidence", 1.0) < LOW_CONFIDENCE_THRESHOLD else ""
        lines.append(f"🗺️ {person_name}: {mig.get('from_place')} → {mig.get('to_place')} "
                     f"({mig.get('approx_date') or 'date unknown'}){flag}")

    lines.append("\nReply **yes** to save this, or tell me what to correct.")
    return "\n".join(lines)


class PendingProposal:
    """
    Wraps a pending extraction so it can be stored in api.py's
    pending_confirmations dict alongside the existing
    AWAITING_DELETE_CONFIRM string flag. api.py distinguishes the two by
    isinstance() check rather than a shared string key.
    """
    def __init__(self, extracted: Dict, original_text: str, language: str):
        self.extracted = extracted
        self.original_text = original_text
        self.language = language
        self.type = "CREATE_CONFIRM"

    def __repr__(self):
        action = self.extracted.get("action", "?")
        n_people = len(self.extracted.get("persons", []))
        return f"<PendingProposal action={action} persons={n_people}>"
"""
query.py

Read-only family data access: fetching persons for a user, matching a name
to a person, and answering free-text questions about the tree. Nothing in
this file writes to the DB — that's handlers.py's job.

Owns:
  - get_all_family_data()   — all persons for a user, with relationships/events
  - find_person_by_name()   — case-insensitive name lookup (public version of
                               the original _find_person_by_name)
  - find_person_in_query()  — legacy substring lookup, kept for backward compat
  - handle_query()          — the QUERY action handler from process_query

Depends on narrative.py for build_person_context() / build_family_overview()
— not yet written; forward reference until that file exists (same pattern
as handlers.py depending on this file).
"""

import uuid
from typing import List, Dict, Optional

from database import SessionLocal, Person
from graph_service import FamilyTreeService
from .extraction import EntityExtractor
from .narrative import build_person_context, build_family_overview  # not yet written — forward reference


# ─────────────────────────────────────────────
# DATA ACCESS
# ─────────────────────────────────────────────

def get_all_family_data(user_id: uuid.UUID) -> List[Dict]:
    """Return all persons for a user as dicts, with relationships and events included."""
    db = SessionLocal()
    graph_service = FamilyTreeService()
    try:
        persons = db.query(Person).filter(Person.user_id == user_id).all()
        print(f"Found {len(persons)} persons for user {user_id}")
        result = []
        for person in persons:
            data = graph_service.get_person_with_relationships(db, person.id)
            if data:
                result.append(data)
        return result
    finally:
        db.close()


# ─────────────────────────────────────────────
# NAME MATCHING
# ─────────────────────────────────────────────

def find_person_by_name(name: Optional[str], all_persons: List[Dict]) -> Optional[Dict]:
    """Case-insensitive name lookup — exact match first, then substring."""
    if not name:
        return None
    name_lower = name.lower()
    for p in all_persons:
        if p["name"].lower() == name_lower:
            return p
    for p in all_persons:
        if name_lower in p["name"].lower() or p["name"].lower() in name_lower:
            return p
        if p.get("native_name") and name_lower in p["native_name"].lower():
            return p
    return None


def find_person_in_query(query_text: str, all_persons: List[Dict]) -> Optional[Dict]:
    """Legacy substring lookup — kept for backward compat with any code still
    calling the old ai_engine.find_person_in_query() directly."""
    query_lower = query_text.lower()
    for person in all_persons:
        if person["name"].lower() in query_lower:
            return person
    if "my " in query_lower:
        sorted_persons = sorted(
            all_persons,
            key=lambda x: x.get("birth_date") or "1900-01-01",
            reverse=True
        )
        return sorted_persons[0] if sorted_persons else (all_persons[0] if all_persons else None)
    return all_persons[0] if all_persons else None


# ─────────────────────────────────────────────
# ENTITY RESOLUTION
# ─────────────────────────────────────────────

def resolve_references(user_id: uuid.UUID, extracted: Dict) -> Dict:
    """
    Runs right after extraction, before a PendingProposal is built. Annotates
    each person in extracted["persons"] with an "existing_match" key:

        {"id": "...", "name": "..."}   — a matching person already in the tree
        None                            — no match, this would be a new person

    This is the single source of truth both confirmation.py (for an accurate
    preview) and handlers.py (to avoid creating duplicates) read from —
    neither module does its own name-matching against the DB anymore.

    Mutates and returns the same extracted dict for convenience.
    """
    all_persons = get_all_family_data(user_id)
    for p in extracted.get("persons", []):
        name = p.get("name")
        match = find_person_by_name(name, all_persons) if name else None
        p["existing_match"] = {"id": match["id"], "name": match["name"]} if match else None
    return extracted


# ─────────────────────────────────────────────
# GENERAL QUERY  (QUERY action handler)
# ─────────────────────────────────────────────

def handle_query(user_id: uuid.UUID, query_text: str, language: str,
                 llm, extractor: EntityExtractor) -> Dict:
    """
    Answers a free-text question about the family tree.

    Re-runs extraction on the query itself (separate from the extraction
    that routed to this handler in the first place) purely to spot a named
    person to focus the context on — e.g. "Who is Tunde's father?" should
    build context around Tunde specifically, not the whole family. This is
    a second LLM call; if that cost matters at scale, it could be replaced
    with a cheaper regex/fuzzy-name pass against all_persons instead of a
    full extraction round-trip, since all we need here is a name, not a
    full entity extraction.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    all_persons = get_all_family_data(user_id)

    if not all_persons:
        return {
            "response": (
                "I don't see any family members in your tree yet.\n"
                "Try something like:\n"
                "  • 'I was born on 26 Jan 2005 in Nkwerre, Imo. "
                "My mum is Emily Maduike from Etung, Cross River. "
                "My dad is Emmanuel Maduike from Nkwerre.'\n"
                "  • 'Add my grandfather Adewale Okafor, born 1920, Yoruba from Ibadan'"
            ),
            "action": "QUERY",
            "context_used": 0
        }

    # Find focus person for richer context
    extracted = extractor.extract_all_entities(query_text)
    persons_data = extracted.get("persons", [])
    person_name = next(
        (p["name"] for p in persons_data if p.get("name") and p.get("ref_key") != "SELF"),
        None
    )
    target = find_person_by_name(person_name, all_persons) if person_name else None
    context = build_person_context(target, all_persons) if target else build_family_overview(all_persons)

    system_prompt = f"""You are an African ancestry assistant with deep knowledge of Yoruba, Igbo, and Hausa family structures.

You have access to this family data:
{context}

Answer the user's question based ONLY on the data provided.
If something is not in the data, say so clearly.
Respond in {language}.
Be culturally sensitive — use terms like Baba, Iya, Nna, Nne where appropriate."""

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=query_text)
    ])

    return {"response": response.content, "action": "QUERY", "context_used": len(all_persons)}
"""
narrative.py

Turns structured family data into text: either a context block fed to
another LLM prompt (build_person_context / build_family_overview — pure
string formatting, no LLM call), or a full generated story
(generate_family_story — the LLM call itself).

Owns:
  - build_person_context()    — focused context block for one person
  - build_family_overview()   — lightweight summary of the whole tree
  - generate_family_story()   — griot / modern / children story generation

query.py's handle_query() and handlers.py's migration flow both depend on
the two context builders here — this is the file that closes those forward
references.

Note: build_person_context/build_family_overview take plain dicts (the
shape returned by query.get_all_family_data()), not ORM objects, so this
module has no DB dependency at all — it's pure formatting + one LLM call.
"""

import uuid
from typing import Dict, List, Optional


# ─────────────────────────────────────────────
# CONTEXT BUILDERS  (pure formatting, no LLM call)
# ─────────────────────────────────────────────

def build_person_context(target_person: Dict, all_persons: List[Dict]) -> str:
    person_name = target_person["name"]
    parts = [f"=== FOCUS: {person_name} ==="]

    if target_person.get("native_name"):
        parts.append(f"Native Name: {target_person['native_name']}")
    parts.append(f"Tribe: {target_person.get('tribe', 'N/A')}")
    parts.append(f"Clan: {target_person.get('clan', 'N/A')}")
    origin = target_person.get("origin") or ", ".join(filter(None, [
        target_person.get("village_origin"), target_person.get("town"), target_person.get("state")
    ]))
    parts.append(f"From: {origin or 'N/A'}")
    if target_person.get("birth_date"):
        parts.append(f"Born: {target_person['birth_date']}")
    if target_person.get("biography"):
        parts.append(f"About: {target_person['biography']}")

    if target_person.get("events"):
        parts.append("\nEVENTS:")
        for ev in target_person["events"]:
            line = f"  • {ev.get('type', 'event')}"
            if ev.get("date"):
                line += f" ({ev['date']})"
            if ev.get("location"):
                line += f" in {ev['location']}"
            parts.append(line)

    if target_person.get("migrations"):
        parts.append("\nMIGRATION HISTORY:")
        for mig in target_person["migrations"]:
            line = f"  • {mig.get('from_place', '?')} → {mig.get('to_place', '?')}"
            if mig.get("approx_date"):
                line += f" ({mig['approx_date']})"
            parts.append(line)

    parents, children, spouses, siblings = [], [], [], []
    for rel in target_person.get("relationships", []):
        rel_type = rel.get("type", "")
        if "to" in rel:
            n = rel["to"]
            if "PARENT" in rel_type:
                children.append(f"{n} (child)")
            elif "SPOUSE" in rel_type:
                spouses.append(f"{n} (spouse)")
            elif "SIBLING" in rel_type:
                siblings.append(f"{n} (sibling)")
        elif "from" in rel:
            n = rel["from"]
            if "PARENT" in rel_type:
                parents.append(f"{n} (parent)")
            elif "SPOUSE" in rel_type:
                spouses.append(f"{n} (spouse)")
            elif "SIBLING" in rel_type:
                siblings.append(f"{n} (sibling)")

    if parents:
        parts.append("\nPARENTS:\n" + "\n".join(f"  • {p}" for p in parents))
    if children:
        parts.append("\nCHILDREN:\n" + "\n".join(f"  • {c}" for c in children))
    if spouses:
        parts.append("\nSPOUSE(S):\n" + "\n".join(f"  • {s}" for s in spouses))
    if siblings:
        parts.append("\nSIBLINGS:\n" + "\n".join(f"  • {s}" for s in siblings))

    parts.append("\n=== ALL FAMILY MEMBERS ===")
    for p in all_persons:
        if p["id"] != target_person["id"]:
            line = f"• {p['name']}"
            if p.get("tribe"):
                line += f" ({p['tribe']}"
                if p.get("town"):
                    line += f", {p['town']}"
                line += ")"
            parts.append(line)

    return "\n".join(parts)


def build_family_overview(all_persons: List[Dict]) -> str:
    parts = [f"FAMILY OVERVIEW ({len(all_persons)} members)"]
    for person in all_persons:
        line = f"- {person['name']}"
        if person.get("tribe"):
            line += f" ({person['tribe']}"
            if person.get("clan"):
                line += f", {person['clan']} clan"
            line += ")"
        if person.get("town"):
            line += f" from {person['town']}"
        parts.append(line)
    return "\n".join(parts)


# ─────────────────────────────────────────────
# STORY GENERATION  (the LLM call)
# ─────────────────────────────────────────────

STYLE_PROMPTS = {
    "griot": """You are a West African griot (oral historian). Tell this family's story with:
- Opening invocation of ancestors
- Praise poetry (oriki) style for notable members
- Traditional proverbs woven in naturally
- Historical and cultural context for their region
- A closing blessing for the lineage

Make it captivating, warm, and deeply respectful of the culture.""",
    "modern": "Tell this as a documentary-style narrative with dates, places, and historical context.",
    "children": "Tell this as a simple, engaging bedtime story for children about their family roots."
}


def generate_family_story(llm, user_id: uuid.UUID, person_id: uuid.UUID,
                          all_persons: List[Dict], style: str = "griot", language: str = "en") -> str:
    """
    all_persons is passed in (from query.get_all_family_data()) rather than
    fetched here, so this module stays DB-free — the caller (ai_engine.py)
    owns fetching data, this module only turns it into prose.
    """
    print(f"Looking for person_id: {str(person_id)}")
    print(f"Available IDs: {[p.get('id') for p in all_persons]}")

    target = next((p for p in all_persons if str(p.get("id")) == str(person_id)), None)
    print(f"Target found: {target is not None}")

    if not target:
        return "Person not found in family tree."

    context = build_person_context(target, all_persons)
    prompt = STYLE_PROMPTS.get(style, STYLE_PROMPTS["griot"])
    response = llm.invoke(f"{prompt}\n\nFamily Data:\n{context}\n\nGenerate the story in {language}.")
    return response.content


def generate_biography_from_interview(llm, transcript: str, target_person: Dict,
                                      all_persons: List[Dict], language: str = "en") -> str:
    """
    NEW — for the future /interview endpoint. Same shape as
    generate_family_story() but grounds the narrative in a real transcript
    instead of structured DB fields alone. Kept here rather than a separate
    file since it's the same "structured context + LLM → prose" pattern as
    generate_family_story(), just with an extra input.

    Not wired into process_query() yet — this exists so the interview
    pipeline (transcription → extraction confirmation → biography) has a
    narrative-generation step ready when that endpoint gets built.
    """
    context = build_person_context(target_person, all_persons)
    prompt = f"""You are writing a biography for a family heritage platform, grounded in a real interview.

Use the interview transcript as your primary source for the person's voice, memories, and lived
experience. Use the structured family data only to fill in factual gaps (dates, relationships,
places) — do not let it override or contradict what the person actually said in the interview.

If the transcript and structured data conflict, prefer the transcript and note the discrepancy
briefly rather than silently picking one.

STRUCTURED FAMILY DATA:
{context}

INTERVIEW TRANSCRIPT:
{transcript}

Write a warm, respectful biography in {language}, grounded primarily in the transcript."""

    response = llm.invoke(prompt)
    return response.content
"""
extraction.py

Pure NLP extraction: free text in, structured dict out. No DB access, no
FamilyTreeService calls — this module never writes anything. That's the
whole point of the split: extraction can be unit-tested against fixed
LLM outputs without touching Postgres, and reused identically by both the
/chat/ flow and (later) the /interview transcript flow.

Owns:
  - EntityExtractor.extract_all_entities()  — the core LLM call
  - EntityExtractor.build_person_payload()  — extracted dict -> DB-ready dict

Everything here was moved out of ai_engine.py unchanged in behavior, plus:
  - confidence scoring per person/relationship/event/migration
  - field_confidence for individually-inferred fields
  - a new "migrations" array parsed from narrative mentions of movement
"""

from typing import Dict
from datetime import datetime
import json


class EntityExtractor:
    def __init__(self, llm):
        """
        llm: a LangChain chat model instance (e.g. ChatGoogleGenerativeAI),
        passed in from ai_engine.py so this module doesn't own API key
        handling or model config — just prompting.
        """
        self.llm = llm

    # ─────────────────────────────────────────────
    # MULTI-ENTITY EXTRACTION  (core NLP step)
    # ─────────────────────────────────────────────

    def extract_all_entities(self, query: str) -> Dict:
        """
        Extract ALL people, relationships, events, and migrations from a
        single free-text message. Returns a structured dict with arrays so
        one message can seed an entire family.

        Key behaviours:
        - "I was born…" → person with ref_key "SELF"; name may be null →
          needs_name_for_self = true
        - Relative pronouns resolved to named persons
        - Ages like "5 years older than me" converted to approximate birth years
        - Relationships are directional: from_ref → to_ref
        - Every person/relationship/event/migration carries a confidence score
          so the caller can flag uncertain extractions before writing anything
        """
        from langchain_core.messages import HumanMessage

        today_year = datetime.now().year

        extraction_prompt = f"""You are an entity-extraction engine for an African family-tree app.
Read the user message carefully, then return ONLY a single valid JSON object — no markdown, no commentary.

TODAY'S YEAR: {today_year}

─── RULES ───
1. Extract EVERY person mentioned, including "I / me / my" (use ref_key "SELF" for the speaker).
2. If "I" is used but no name is given, set name to null and set needs_name_for_self to true.
3. Infer birth years from relative ages (e.g. "5 years older than me, born 2005" → sibling born 2000-01-01).
4. Infer tribe/state from LGA/town where possible (e.g. Nkwerre LGA → Igbo, Imo State).
5. For relationships use ref_key values that match the persons array entries.
6. Relationship types allowed: PARENT_OF | SPOUSE_OF | SIBLING_OF
7. Always use the most senior person as from_ref for PARENT_OF (parent → child).
8. If the message introduces multiple people and their relationships, set action to CREATE_FAMILY_BATCH.
9. If the message is only a question or story request, set action to QUERY or STORY.
10. Embed any birth/death/marriage/graduation events in the events array.
11. If the user wants to delete, clear, remove, or wipe their ENTIRE family tree, set action to DELETE_FAMILY. Do NOT use this for single-person deletion.
12. Assign a "confidence" score (0.0-1.0) to EVERY person, relationship, and event:
    - 1.0 = explicitly and unambiguously stated ("my father Emmanuel Maduike")
    - 0.6-0.8 = inferred with reasonable certainty (age→birth year math, LGA→tribe/state lookup)
    - 0.3-0.5 = guessed from weak signal (vague pronoun resolution, ambiguous name match)
    Include an overall "confidence" on the person/relationship/event object, plus a
    "field_confidence" dict for any inferred field (e.g. birth_date, tribe, state)
    that was not explicitly stated by the user.
13. Extract migration/movement mentions into a separate "migrations" array — any
    statement about a person moving, relocating, or originating from one place and
    living in another (e.g. "we moved from Benin to Lagos in the 60s", "my grandfather
    later settled in London"). Order matters: list migrations in the order the person
    experienced them if inferable from context. Give each migration a confidence score too.

─── OUTPUT SCHEMA ───
{{
  "action": "CREATE_FAMILY_BATCH | CREATE_PERSON | CREATE_RELATIONSHIP | CREATE_EVENT | STORY | DELETE_FAMILY | QUERY",
  "needs_name_for_self": false,
  "detected_language": "en | ig | yo | ha | pcm | ...",  # ISO code for pidgin ("pcm")
  "persons": [
    {{
      "ref_key": "SELF",
      "name": null,
      "native_name": null,
      "birth_date": "YYYY-MM-DD or null",
      "death_date": null,
      "gender": "male | female | unknown",
      "tribe": null,
      "clan": null,
      "village_origin": null,
      "town": null,
      "lga": null,
      "state": null,
      "country": "Nigeria",
      "occupation": [],
      "titles": [],
      "languages": [],
      "biography": null,
      "confidence": 1.0,
      "field_confidence": {{}}
    }}
  ],
  "relationships": [
    {{
      "from_ref": "FATHER",
      "to_ref": "SELF",
      "relationship_type": "PARENT_OF",
      "is_traditional": false,
      "notes": null,
      "confidence": 1.0
    }}
  ],
  "events": [
    {{
      "person_ref": "SELF",
      "event_type": "birth",
      "event_date": "YYYY-MM-DD or null",
      "event_location": null,
      "event_description": null,
      "cultural_significance": null,
      "confidence": 1.0
    }}
  ],
  "migrations": [
    {{
      "person_ref": "SELF",
      "from_place": null,
      "to_place": null,
      "approx_date": "YYYY or a loose string like '1960s' or null",
      "notes": null,
      "confidence": 0.7
    }}
  ],
  "story_person_ref": null,
  "story_style": "griot"
}}

─── USER MESSAGE ───
{query}"""

        response = None
        try:
            response = self.llm.invoke([HumanMessage(content=extraction_prompt)])
            raw = response.content.strip()
            # Strip markdown fences if the model adds them
            if "```" in raw:
                parts = raw.split("```")
                raw = parts[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw.strip())
            # Defensive defaults so downstream code never has to None-check these
            parsed.setdefault("persons", [])
            parsed.setdefault("relationships", [])
            parsed.setdefault("events", [])
            parsed.setdefault("migrations", [])
            return parsed
        except Exception as e:
            print(f"Entity extraction failed: {e}")
            print(f"Raw LLM output: {getattr(response, 'content', 'N/A')}")
            return {"action": "QUERY", "persons": [], "relationships": [], "events": [], "migrations": []}

    # ─────────────────────────────────────────────
    # DATA HELPERS
    # ─────────────────────────────────────────────

    def build_person_payload(self, p: Dict) -> Dict:
        """Convert an extracted person dict to a clean DB-ready dict.
        Strips extraction-only fields (confidence, field_confidence, ref_key,
        lga) that don't belong on the Person model."""
        payload = {
            "name": p["name"],
            "gender": p.get("gender") or "unknown",
            "country": p.get("country") or "Nigeria",
            "occupation": p.get("occupation") or [],
            "titles": p.get("titles") or [],
            "languages": p.get("languages") or [],
        }
        for field in ("native_name", "tribe", "clan", "village_origin", "town", "state", "biography"):
            if p.get(field):
                payload[field] = p[field]

        # LGA → village_origin if village_origin not set
        if p.get("lga") and not payload.get("village_origin"):
            payload["village_origin"] = p["lga"]

        for date_field in ("birth_date", "death_date"):
            raw = p.get(date_field)
            if raw:
                try:
                    payload[date_field] = datetime.strptime(raw, "%Y-%m-%d").date()
                except ValueError:
                    pass

        return payload
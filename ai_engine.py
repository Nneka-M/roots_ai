from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_core.messages import HumanMessage, SystemMessage
from typing import List, Dict, Optional
from graph_service import FamilyTreeService
from database import SessionLocal, Person
import uuid
import os
import traceback
import json
from datetime import datetime, date


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

        self.graph_service = FamilyTreeService()

    # ─────────────────────────────────────────────
    # MAIN ENTRY POINT
    # ─────────────────────────────────────────────

    def process_query(self, user_id: uuid.UUID, query: str, language: str = "en",
                      pending_action: Optional[str] = None) -> Dict:
        """
        Single chat endpoint. Routes to the correct handler based on LLM-extracted intent.

        pending_action: set by the API layer when a confirmation step is in progress.
          - "AWAITING_DELETE_CONFIRM" → treat this message as the user's yes/no answer
        """
        try:
            # ── Confirmation gate (runs BEFORE any LLM extraction) ──────────────
            if pending_action == "AWAITING_DELETE_CONFIRM":
                return self._handle_delete_confirmation(user_id, query, language)

            extracted = self._extract_all_entities(query)
            action = extracted.get("action", "QUERY")

            print(f"DEBUG extracted: {json.dumps(extracted, indent=2, default=str)}")

            if action in ("CREATE_PERSON", "CREATE_FAMILY_BATCH"):
                return self._handle_batch_create(user_id, query, extracted, language)

            elif action == "CREATE_RELATIONSHIP":
                return self._handle_create_relationship(user_id, query, extracted, language)

            elif action == "CREATE_EVENT":
                return self._handle_create_event(user_id, query, extracted, language)

            elif action == "STORY":
                return self._handle_story(user_id, extracted, language)

            elif action == "DELETE_FAMILY":
                return self._handle_delete_warning(user_id, language)

            else:
                return self._handle_query(user_id, query, language)

        except Exception as e:
            print(f"ERROR in process_query: {str(e)}")
            print(f"TRACEBACK: {traceback.format_exc()}")
            return {
                "response": f"Error processing query: {str(e)}",
                "action": "ERROR",
                "context_used": 0
            }

    # ─────────────────────────────────────────────
    # MULTI-ENTITY EXTRACTION  (core NLP step)
    # ─────────────────────────────────────────────

    def _extract_all_entities(self, query: str) -> Dict:
        """
        Extract ALL people, relationships, and events from a single free-text message.
        Returns a structured dict with arrays so one message can seed an entire family.

        Key behaviours:
        - "I was born…" → person with ref_key "SELF"; name may be null → needs_name_for_self = true
        - Relative pronouns resolved to named persons
        - Ages like "5 years older than me" converted to approximate birth years
        - Relationships are directional: from_ref → to_ref
        """
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

─── OUTPUT SCHEMA ───
{{
  "action": "CREATE_FAMILY_BATCH | CREATE_PERSON | CREATE_RELATIONSHIP | CREATE_EVENT | STORY | DELETE_FAMILY | QUERY",
  "needs_name_for_self": false,
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
      "biography": null
    }}
  ],
  "relationships": [
    {{
      "from_ref": "FATHER",
      "to_ref": "SELF",
      "relationship_type": "PARENT_OF",
      "is_traditional": false,
      "notes": null
    }}
  ],
  "events": [
    {{
      "person_ref": "SELF",
      "event_type": "birth",
      "event_date": "YYYY-MM-DD or null",
      "event_location": null,
      "event_description": null,
      "cultural_significance": null
    }}
  ],
  "story_person_ref": null,
  "story_style": "griot"
}}

─── USER MESSAGE ───
{query}"""

        try:
            response = self.llm.invoke([HumanMessage(content=extraction_prompt)])
            raw = response.content.strip()
            # Strip markdown fences if the model adds them
            if "```" in raw:
                parts = raw.split("```")
                raw = parts[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as e:
            print(f"Entity extraction failed: {e}")
            print(f"Raw LLM output: {getattr(response, 'content', 'N/A')}")
            return {"action": "QUERY", "persons": [], "relationships": [], "events": []}

    # ─────────────────────────────────────────────
    # BATCH CREATE  (people + relationships + events)
    # ─────────────────────────────────────────────

    def _handle_batch_create(self, user_id: uuid.UUID, query: str, extracted: Dict, language: str) -> Dict:
        """
        Creates all persons, then all relationships, then all events in one pass.
        If the speaker (SELF) has no name, saves everyone else and asks for their name.
        """
        persons_data = extracted.get("persons", [])
        relationships_data = extracted.get("relationships", [])
        events_data = extracted.get("events", [])
        needs_name = extracted.get("needs_name_for_self", False)

        if not persons_data:
            return {
                "response": (
                    "I couldn't identify any people in that message.\n"
                    "Try: 'My mum is Grace Okafor, Igbo from Enugu. My dad is Peter Okafor from Awka.'"
                ),
                "action": "CREATE_FAMILY_BATCH",
                "context_used": 0
            }

        service = FamilyTreeService()
        ref_to_db_id: Dict[str, str] = {}   # ref_key → string UUID after DB insert
        created_names: List[str] = []
        skipped: List[str] = []

        # ── Step 1: Create persons ──
        for p in persons_data:
            name = p.get("name")

            # SELF with no name: skip creation, flag for follow-up
            if p.get("ref_key") == "SELF" and not name:
                skipped.append("you (name not provided)")
                continue

            if not name:
                skipped.append(f"unnamed {p.get('ref_key', 'person')}")
                continue

            payload = self._build_person_payload(p)
            try:
                new_person = service.create_person(user_id, payload)
                ref_to_db_id[p["ref_key"]] = str(new_person.id)
                created_names.append(name)
                print(f"✅ Created: {name} → {new_person.id}")
            except Exception as e:
                print(f"❌ Failed to create {name}: {e}")
                skipped.append(name)

        # ── Step 2: Create relationships ──
        rel_summaries: List[str] = []
        for rel in relationships_data:
            from_ref = rel.get("from_ref")
            to_ref = rel.get("to_ref")
            rel_type = (rel.get("relationship_type") or "").upper()

            from_id = ref_to_db_id.get(from_ref)
            to_id = ref_to_db_id.get(to_ref)

            if not from_id or not to_id:
                print(f"Skipping rel {from_ref}→{to_ref}: not both in DB")
                continue

            try:
                service.create_relationship(
                    uuid.UUID(from_id),
                    uuid.UUID(to_id),
                    rel_type,
                    is_traditional=rel.get("is_traditional", False),
                    notes=rel.get("notes")
                )
                from_name = next((p["name"] for p in persons_data if p["ref_key"] == from_ref and p.get("name")), from_ref)
                to_name = next((p["name"] for p in persons_data if p["ref_key"] == to_ref and p.get("name")), to_ref)
                rel_label = rel_type.replace("_", " ").title()
                rel_summaries.append(f"{from_name} → {rel_label} → {to_name}")
                print(f"✅ Relationship: {from_name} {rel_type} {to_name}")
            except Exception as e:
                print(f"❌ Failed relationship {from_ref}→{to_ref}: {e}")

        # ── Step 3: Create events ──
        event_summaries: List[str] = []
        for ev in events_data:
            person_ref = ev.get("person_ref")
            person_id_str = ref_to_db_id.get(person_ref)
            if not person_id_str:
                continue

            event_payload = {"event_type": ev.get("event_type", "other")}
            if ev.get("event_date"):
                try:
                    event_payload["event_date"] = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
                except ValueError:
                    pass
            if ev.get("event_location"):
                event_payload["location"] = ev["event_location"]
            if ev.get("event_description"):
                event_payload["description"] = ev["event_description"]
            if ev.get("cultural_significance"):
                event_payload["cultural_significance"] = ev["cultural_significance"]

            try:
                service.create_event(uuid.UUID(person_id_str), event_payload)
                person_name = next(
                    (p["name"] for p in persons_data if p["ref_key"] == person_ref and p.get("name")),
                    person_ref
                )
                event_summaries.append(f"{ev.get('event_type', 'event')} for {person_name}")
            except Exception as e:
                print(f"❌ Failed event for {person_ref}: {e}")

        # ── Build human-friendly response ──
        parts = []

        if created_names:
            parts.append(f"✅ Added {len(created_names)} family member(s): **{', '.join(created_names)}**.")

        if rel_summaries:
            parts.append("🔗 Relationships linked:\n" + "\n".join(f"  • {r}" for r in rel_summaries))

        if event_summaries:
            parts.append("📅 Events recorded: " + ", ".join(event_summaries) + ".")

        if needs_name:
            parts.append(
                "\n⚠️ I noticed you referred to yourself as 'I/me' but didn't give your name. "
                "What's your full name? I'll add you to the tree and connect everyone to you."
            )

        if not parts:
            parts.append(
                "I processed your message but couldn't extract any new family data. "
                "Please include full names and relationships — e.g. 'My dad Emmanuel Maduike is from Nkwerre, Imo.'"
            )

        return {
            "response": "\n".join(parts),
            "action": "CREATE_FAMILY_BATCH",
            "created_count": len(created_names),
            "created_names": created_names,
            "needs_self_name": needs_name,
            "ref_to_id": ref_to_db_id,
            "context_used": len(created_names)
        }

    # ─────────────────────────────────────────────
    # CREATE RELATIONSHIP  (standalone)
    # ─────────────────────────────────────────────

    def _handle_create_relationship(self, user_id: uuid.UUID, query: str, extracted: Dict, language: str) -> Dict:
        all_persons = self.get_all_family_data(user_id)
        relationships_data = extracted.get("relationships", [])
        persons_data = extracted.get("persons", [])

        if not relationships_data:
            return {
                "response": "Could you be more specific? E.g. 'Adewale is the father of Tunde'",
                "action": "CREATE_RELATIONSHIP",
                "context_used": len(all_persons)
            }

        service = FamilyTreeService()
        summaries = []

        for rel in relationships_data:
            from_name = next((p["name"] for p in persons_data if p["ref_key"] == rel.get("from_ref") and p.get("name")), None)
            to_name = next((p["name"] for p in persons_data if p["ref_key"] == rel.get("to_ref") and p.get("name")), None)

            from_person = self._find_person_by_name(from_name, all_persons)
            to_person = self._find_person_by_name(to_name, all_persons)

            missing = []
            if not from_person and from_name:
                missing.append(from_name)
            if not to_person and to_name:
                missing.append(to_name)
            if missing:
                summaries.append(f"⚠️ Couldn't find **{' or '.join(missing)}** in your tree — add them first.")
                continue

            rel_type = (rel.get("relationship_type") or "").upper()
            service.create_relationship(
                uuid.UUID(from_person["id"]),
                uuid.UUID(to_person["id"]),
                rel_type,
                is_traditional=rel.get("is_traditional", False),
                notes=rel.get("notes")
            )
            summaries.append(f"✅ {from_person['name']} {rel_type.replace('_', ' ').title()} {to_person['name']}")

        return {
            "response": "\n".join(summaries),
            "action": "CREATE_RELATIONSHIP",
            "context_used": len(all_persons)
        }

    # ─────────────────────────────────────────────
    # CREATE EVENT  (standalone)
    # ─────────────────────────────────────────────

    def _handle_create_event(self, user_id: uuid.UUID, query: str, extracted: Dict, language: str) -> Dict:
        all_persons = self.get_all_family_data(user_id)
        events_data = extracted.get("events", [])
        persons_data = extracted.get("persons", [])

        if not events_data:
            return {
                "response": "Could you describe the event? E.g. 'Record that Tunde graduated in 1985 in Lagos'",
                "action": "CREATE_EVENT",
                "context_used": len(all_persons)
            }

        service = FamilyTreeService()
        summaries = []

        for ev in events_data:
            person_ref = ev.get("person_ref")
            person_name = next((p["name"] for p in persons_data if p["ref_key"] == person_ref and p.get("name")), None)
            target = self._find_person_by_name(person_name, all_persons)

            if not target:
                summaries.append(f"⚠️ Couldn't find **{person_name or person_ref}** in your tree.")
                continue

            event_payload = {"event_type": ev.get("event_type", "other")}
            if ev.get("event_date"):
                try:
                    event_payload["event_date"] = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
                except ValueError:
                    pass
            if ev.get("event_location"):
                event_payload["location"] = ev["event_location"]
            if ev.get("event_description"):
                event_payload["description"] = ev["event_description"]
            if ev.get("cultural_significance"):
                event_payload["cultural_significance"] = ev["cultural_significance"]

            service.create_event(uuid.UUID(target["id"]), event_payload)
            summaries.append(
                f"✅ Recorded **{ev.get('event_type', 'event')}** for {target['name']}"
                + (f" on {ev['event_date']}" if ev.get("event_date") else "")
                + (f" in {ev['event_location']}" if ev.get("event_location") else "")
            )

        return {
            "response": "\n".join(summaries),
            "action": "CREATE_EVENT",
            "context_used": len(all_persons)
        }

    # ─────────────────────────────────────────────
    # STORY
    # ─────────────────────────────────────────────

    def _handle_story(self, user_id: uuid.UUID, extracted: Dict, language: str) -> Dict:
        all_persons = self.get_all_family_data(user_id)
        if not all_persons:
            return {"response": "No family members found. Add someone first!", "action": "STORY", "context_used": 0}

        story_ref = extracted.get("story_person_ref")
        persons_data = extracted.get("persons", [])
        person_name = next(
            (p["name"] for p in persons_data if p["ref_key"] == story_ref and p.get("name")),
            None
        ) if story_ref else None

        target = self._find_person_by_name(person_name, all_persons) if person_name else all_persons[0]
        story = self.generate_family_story(
            user_id,
            uuid.UUID(target["id"]),
            style=extracted.get("story_style", "griot"),
            language=language
        )
        return {"response": story, "action": "STORY", "context_used": len(all_persons)}

    # ─────────────────────────────────────────────
    # GENERAL QUERY
    # ─────────────────────────────────────────────

    def _handle_query(self, user_id: uuid.UUID, query: str, language: str) -> Dict:
        all_persons = self.get_all_family_data(user_id)

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
        extracted = self._extract_all_entities(query)
        persons_data = extracted.get("persons", [])
        person_name = next(
            (p["name"] for p in persons_data if p.get("name") and p.get("ref_key") != "SELF"),
            None
        )
        target = self._find_person_by_name(person_name, all_persons) if person_name else None
        context = self.build_person_context(target, all_persons) if target else self.build_family_overview(all_persons)

        system_prompt = f"""You are an African ancestry assistant with deep knowledge of Yoruba, Igbo, and Hausa family structures.

You have access to this family data:
{context}

Answer the user's question based ONLY on the data provided.
If something is not in the data, say so clearly.
Respond in {language}.
Be culturally sensitive — use terms like Baba, Iya, Nna, Nne where appropriate."""

        response = self.llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=query)
        ])

        return {"response": response.content, "action": "QUERY", "context_used": len(all_persons)}

    # ─────────────────────────────────────────────
    # STORY GENERATION
    # ─────────────────────────────────────────────

    def generate_family_story(self, user_id: uuid.UUID, person_id: uuid.UUID,
                              style: str = "griot", language: str = "en") -> str:
        all_persons = self.get_all_family_data(user_id)

        print(f"Looking for person_id: {str(person_id)}")
        print(f"Available IDs: {[p.get('id') for p in all_persons]}")

        target = next((p for p in all_persons if str(p.get("id")) == str(person_id)), None)
        print(f"Target found: {target is not None}")

        if not target:
            return "Person not found in family tree."

        context = self.build_person_context(target, all_persons)

        style_prompts = {
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

        prompt = style_prompts.get(style, style_prompts["griot"])
        response = self.llm.invoke(f"{prompt}\n\nFamily Data:\n{context}\n\nGenerate the story in {language}.")
        return response.content

    # ─────────────────────────────────────────────
    # DATA HELPERS
    # ─────────────────────────────────────────────

    def _build_person_payload(self, p: Dict) -> Dict:
        """Convert an extracted person dict to a clean DB-ready dict"""
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

    def get_all_family_data(self, user_id: uuid.UUID) -> List[Dict]:
        """Return all persons for a user as dicts, with relationships included"""
        db = SessionLocal()
        try:
            persons = db.query(Person).filter(Person.user_id == user_id).all()
            print(f"Found {len(persons)} persons for user {user_id}")
            result = []
            for person in persons:
                data = self.graph_service.get_person_with_relationships(db, person.id)
                if data:
                    result.append(data)
            return result
        finally:
            db.close()

    def _find_person_by_name(self, name: Optional[str], all_persons: List[Dict]) -> Optional[Dict]:
        """Case-insensitive name lookup — exact match first, then substring"""
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

    # Kept for backward compat
    def find_person_in_query(self, query: str, all_persons: List[Dict]) -> Optional[Dict]:
        query_lower = query.lower()
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
    # CONTEXT BUILDERS
    # ─────────────────────────────────────────────

    def build_person_context(self, target_person: Dict, all_persons: List[Dict]) -> str:
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

    def build_family_overview(self, all_persons: List[Dict]) -> str:
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
    # DELETE FAMILY TREE  (two-step: warn → confirm)
    # ─────────────────────────────────────────────

    def _handle_delete_warning(self, user_id: uuid.UUID, language: str) -> Dict:
        """
        Step 1 — show a stern warning and a count of what will be deleted.
        Returns action=AWAITING_DELETE_CONFIRM so the API layer can set the
        pending_action flag for the next message.
        """
        all_persons = self.get_all_family_data(user_id)
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
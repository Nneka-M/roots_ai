"""
handlers.py

The DB-writing side of the pipeline. These are the ONLY functions in the
whole system that call FamilyTreeService.create_*() — extraction.py never
touches the DB, confirmation.py never touches the DB. That separation is
the point of the confirmation-gate redesign: nothing lands in Postgres
until a PendingProposal has been explicitly confirmed by the user.

Behavior here is unchanged from the original ai_engine.py methods
(_handle_batch_create, _handle_create_relationship, _handle_create_event) —
they're just relocated, and now called only via commit_proposal(), which
ai_engine.py invokes after confirmation.is_confirmation() returns True.

New in this file: handle_create_migration() and its slot in commit_proposal().

Depends on query.py for get_all_family_data() and find_person_by_name() —
not yet written; forward reference until that file exists.
"""

import uuid
from datetime import datetime
from typing import Dict, List

from graph_service import FamilyTreeService
from .extraction import EntityExtractor
from .confrmation import PendingProposal
from .query import get_all_family_data, find_person_by_name  # not yet written — see note above


# ─────────────────────────────────────────────
# DISPATCHER — called by ai_engine.py after is_confirmation(reply) is True
# ─────────────────────────────────────────────

def commit_proposal(user_id: uuid.UUID, pending: PendingProposal, extractor: EntityExtractor) -> Dict:
    """
    Writes a confirmed proposal to the DB. Dispatches on the action recorded
    at extraction time, then — regardless of top-level action — also commits
    any migrations found in the same message, since migration mentions can
    ride along with any CREATE_* narration ("my dad Emmanuel, we moved from
    Benin to Lagos in the 60s" is CREATE_PERSON + a migration in one message).
    """
    extracted = pending.extracted
    action = extracted.get("action")

    if action in ("CREATE_PERSON", "CREATE_FAMILY_BATCH"):
        result = handle_batch_create(user_id, pending.original_text, extracted, pending.language, extractor)
    elif action == "CREATE_RELATIONSHIP":
        result = handle_create_relationship(user_id, extracted, pending.language)
    elif action == "CREATE_EVENT":
        result = handle_create_event(user_id, extracted, pending.language)
    else:
        result = {"response": "Nothing to confirm.", "action": "ERROR", "context_used": 0}

    migration_summaries = handle_create_migration(user_id, extracted, pending.language)
    if migration_summaries:
        result["response"] = result["response"] + "\n" + "\n".join(migration_summaries)

    return result


# ─────────────────────────────────────────────
# BATCH CREATE  (people + relationships + events)
# ─────────────────────────────────────────────

def handle_batch_create(user_id: uuid.UUID, query_text: str, extracted: Dict,
                        language: str, extractor: EntityExtractor) -> Dict:
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
    linked_names: List[str] = []        # existing persons matched instead of duplicated
    skipped: List[str] = []

    # ── Step 1: Create persons (or link to an existing match) ──
    for p in persons_data:
        name = p.get("name")

        # SELF with no name: skip creation, flag for follow-up
        if p.get("ref_key") == "SELF" and not name:
            skipped.append("you (name not provided)")
            continue

        if not name:
            skipped.append(f"unnamed {p.get('ref_key', 'person')}")
            continue

        # Already resolved against the DB by query.resolve_references() at
        # proposal time — link to that person instead of creating a duplicate.
        existing = p.get("existing_match")
        if existing:
            ref_to_db_id[p["ref_key"]] = existing["id"]
            linked_names.append(existing["name"])
            print(f"🔁 Linked existing: {existing['name']} → {existing['id']}")
            continue

        payload = extractor.build_person_payload(p)
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

    if linked_names:
        parts.append(f"🔁 Linked to existing record(s) instead of duplicating: **{', '.join(linked_names)}**.")

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
# CREATE RELATIONSHIP  (standalone — links people already in the tree)
# ─────────────────────────────────────────────

def handle_create_relationship(user_id: uuid.UUID, extracted: Dict, language: str) -> Dict:
    all_persons = get_all_family_data(user_id)
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

        from_person = find_person_by_name(from_name, all_persons)
        to_person = find_person_by_name(to_name, all_persons)

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
# CREATE EVENT  (standalone — attaches an event to an existing person)
# ─────────────────────────────────────────────

def handle_create_event(user_id: uuid.UUID, extracted: Dict, language: str) -> Dict:
    all_persons = get_all_family_data(user_id)
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
        target = find_person_by_name(person_name, all_persons)

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
# CREATE MIGRATION  (new — attaches a migration event to an existing/just-created person)
# Needs a `migrations` table — see migration_handler.py notes for the schema.
# Returns a list of summary strings (not a full response dict) since it's
# always folded into another action's response by commit_proposal(), never
# a standalone top-level action.
# ─────────────────────────────────────────────

def handle_create_migration(user_id: uuid.UUID, extracted: Dict, language: str) -> List[str]:
    migrations_data = extracted.get("migrations", [])
    if not migrations_data:
        return []

    all_persons = get_all_family_data(user_id)
    persons_data = extracted.get("persons", [])
    service = FamilyTreeService()
    summaries = []

    for mig in migrations_data:
        person_ref = mig.get("person_ref")
        person_name = next((p["name"] for p in persons_data
                             if p["ref_key"] == person_ref and p.get("name")), None)
        target = find_person_by_name(person_name, all_persons)
        if not target:
            summaries.append(f"⚠️ Couldn't link migration to **{person_name or person_ref}**.")
            continue

        payload = {
            "from_place": mig.get("from_place"),
            "to_place": mig.get("to_place"),
            "notes": mig.get("notes"),
        }
        if mig.get("approx_date"):
            payload["approx_date"] = mig["approx_date"]  # stored as-is; may be year-only or "1960s"

        service.create_migration(uuid.UUID(target["id"]), payload)
        summaries.append(
            f"🗺️ {target['name']}: {mig.get('from_place')} → {mig.get('to_place')}"
            + (f" (~{mig['approx_date']})" if mig.get("approx_date") else "")
        )

    return summaries
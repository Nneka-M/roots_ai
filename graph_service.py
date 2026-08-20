from database import SessionLocal, Person, Relationship, Event
from sqlalchemy.orm import joinedload
from typing import List, Dict, Optional
import uuid
from sqlalchemy import text


class FamilyTreeService:
    def __init__(self):
        self.db = SessionLocal()

    def create_person(self, user_id: uuid.UUID, person_data: dict) -> Person:
        """Create a new person"""
        person = Person(user_id=user_id, **person_data)
        self.db.add(person)
        self.db.commit()
        self.db.refresh(person)
        return person

    def create_relationship(self, from_id: uuid.UUID, to_id: uuid.UUID,
                            rel_type: str, **kwargs) -> Relationship:
        """Create relationship between two people"""
        rel = Relationship(
            from_person_id=from_id,
            to_person_id=to_id,
            relationship_type=rel_type,
            **kwargs
        )
        self.db.add(rel)
        self.db.commit()
        return rel

    def create_event(self, person_id: uuid.UUID, event_data: dict) -> Event:
        """Create event for a person"""
        event = Event(person_id=person_id, **event_data)
        self.db.add(event)
        self.db.commit()
        return event

    def create_migration(self, person_id: uuid.UUID, migration_data: dict):
        """Create a migration/relocation record for a person"""
        from database import Migration
        migration = Migration(person_id=person_id, **migration_data)
        self.db.add(migration)
        self.db.commit()
        return migration
    
    def get_family_tree(self, person_id: uuid.UUID, depth: int = 2) -> Dict:
        """Get family tree using recursive SQL queries"""
        result = self.db.execute(
            text("""
            SELECT * FROM get_immediate_family(:pid)
            UNION ALL
            SELECT * FROM get_ancestors(:pid, :depth)
            UNION ALL
            SELECT * FROM get_descendants(:pid, :depth)
        """), {"pid": str(person_id), "depth": depth})

        relatives = []
        for row in result:
            relatives.append({
                "id": str(row.relative_id) if hasattr(row, "relative_id") else str(row[0]),
                "name": row.relative_name if hasattr(row, "relative_name") else row[1],
                "relationship": row.relationship_type if hasattr(row, "relationship_type") else row[2]
            })

        person = self.db.query(Person).filter(Person.id == person_id).first()

        return {
            "person": {
                "id": str(person.id),
                "name": person.name,
                "tribe": person.tribe,
                "town": person.town
            },
            "relatives": relatives,
            "total_relatives": len(relatives)
        }

    def semantic_search(self, user_id: uuid.UUID, query_embedding: List[float],
                        limit: int = 5) -> List[Person]:
        """Find similar people using pgvector"""
        result = self.db.execute(
            text("""
            SELECT id, name, tribe, embedding <=> :embedding as distance
            FROM persons
            WHERE user_id = :user_id
            ORDER BY embedding <=> :embedding
            LIMIT :limit
        """), {
            "embedding": str(query_embedding),
            "user_id": str(user_id),
            "limit": limit
        })
        return list(result)

    def get_person_with_relationships(self, db, person_id: uuid.UUID) -> Optional[Dict]:
        """
        Get full person context for the LLM.

        Relationship keys use:
          {"to": <name>, "type": <REL_TYPE>, "traditional": bool}   for outgoing
          {"from": <name>, "type": <REL_TYPE>, "traditional": bool}  for incoming
        """
        person = db.query(Person).options(
            joinedload(Person.outgoing_relationships),
            joinedload(Person.incoming_relationships),
            joinedload(Person.events)
        ).filter(Person.id == person_id).first()

        if not person:
            return None

        context = {
            "id": str(person.id),
            "name": person.name,
            "native_name": person.native_name,
            "birth_date": str(person.birth_date) if person.birth_date else None,
            "death_date": str(person.death_date) if person.death_date else None,
            "tribe": person.tribe,
            "clan": person.clan,
            "village_origin": person.village_origin,
            "town": person.town,
            "state": person.state,
            "country": person.country,
            "origin": ", ".join(filter(None, [person.village_origin, person.town, person.state])),
            "occupation": person.occupation,
            "titles": person.titles,
            "biography": person.biography,
            "relationships": [],
            "events": []
        }

        # Outgoing: this person IS the "from" side (e.g. "Adewale PARENT_OF Tunde")
        for rel in person.outgoing_relationships:
            if rel.to_person:
                context["relationships"].append({
                    "to": rel.to_person.name,
                    "type": rel.relationship_type,
                    "traditional": rel.is_traditional
                })

        # Incoming: this person IS the "to" side (e.g. "Adewale PARENT_OF <this person>")
        for rel in person.incoming_relationships:
            if rel.from_person:
                context["relationships"].append({
                    "from": rel.from_person.name,
                    "type": rel.relationship_type,
                    "traditional": rel.is_traditional
                })

        for event in person.events:
            context["events"].append({
                "type": event.event_type,
                "date": str(event.event_date) if event.event_date else None,
                "location": event.location,
                "description": event.description,
                "cultural_significance": event.cultural_significance,
            })

        return context
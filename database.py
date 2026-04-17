from sqlalchemy import create_engine, Column, String, Date, Boolean, Text, ForeignKey, ARRAY, JSON, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
import uuid
import os
from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector

load_dotenv()

Base = declarative_base()

class Person(Base):
    __tablename__ = 'persons'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    
    name = Column(String(255), nullable=False)
    native_name = Column(String(255))
    birth_date = Column(Date)
    death_date = Column(Date)
    gender = Column(String(10))
    
    tribe = Column(String(50))
    clan = Column(String(100))
    village_origin = Column(String(100))
    town = Column(String(100))
    state = Column(String(100))
    country = Column(String(100), default='Nigeria')
    
    occupation = Column(ARRAY(Text))
    titles = Column(ARRAY(Text))
    languages = Column(ARRAY(Text))
    biography = Column(Text)
    embedding = Column(Vector(768))
    
    created_at = Column(Date, server_default=func.now())
    updated_at = Column(Date, server_default=func.now(), onupdate=func.now())

    


class Relationship(Base):
    __tablename__ = 'relationships'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_person_id = Column(UUID(as_uuid=True), ForeignKey('persons.id', ondelete='CASCADE'))
    to_person_id = Column(UUID(as_uuid=True), ForeignKey('persons.id', ondelete='CASCADE'))
    
    relationship_type = Column(String(50), nullable=False)
    start_date = Column(Date)
    end_date = Column(Date)
    is_traditional = Column(Boolean, default=False)
    notes = Column(Text)



class Event(Base):
    __tablename__ = 'events'
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    person_id = Column(UUID(as_uuid=True), ForeignKey('persons.id', ondelete='CASCADE'))
    event_type = Column(String(50), nullable=False)
    event_date = Column(Date)
    location = Column(String(255))
    description = Column(Text)
    cultural_significance = Column(Text)

def configure_relationships():
    """Configure relationships after all models are defined"""
    
    Person.outgoing_relationships = relationship(
        "Relationship", 
        foreign_keys=[Relationship.from_person_id],
        backref="from_person",
        lazy="selectin"
    )
    
    Person.incoming_relationships = relationship(
        "Relationship", 
        foreign_keys=[Relationship.to_person_id],
        backref="to_person",
        lazy="selectin"
    )
    
    Person.events = relationship(
        "Event", 
        foreign_keys=[Event.person_id],
        backref="person",
        lazy="selectin"
    )

# Configure relationships
configure_relationships()


# Database connection
DATABASE_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"

print(f"Connecting to: {DATABASE_URL.replace(os.getenv('DB_PASSWORD'), '****')}")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

def init_extensions():
    """Create pgvector extension"""
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        conn.commit()
        print("✅ pgvector extension enabled")

def init_db():
    """Create all tables"""
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    print("✅ Tables created successfully!")

def test_connection():
    """Test database connection"""
    try:
        conn = engine.connect()
        conn.close()
        print("✅ Database connection successful!")
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

if __name__ == "__main__":
    if test_connection():
        init_extensions()  # Create extension first
        init_db()          # Then create tables
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS migrations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    person_id UUID REFERENCES persons(id) ON DELETE CASCADE,
 
    from_place VARCHAR(255),
    to_place VARCHAR(255),
    approx_date VARCHAR(20),
    notes TEXT,
    sequence_order INTEGER,
 
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
 
CREATE INDEX IF NOT EXISTS idx_migrations_person ON migrations(person_id);
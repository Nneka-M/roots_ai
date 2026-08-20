# Roots — Backend Engineer Reference

AI-powered African family history platform. Users describe their family in natural language; the AI extracts, proposes, and only writes to the database after explicit confirmation.

---

## Project Structure

```
roots/
├── api.py                  # FastAPI app — all endpoints, session management
├── database.py             # SQLAlchemy models + DB init
├── graph_service.py        # FamilyTreeService — all DB read/write operations
├── app/
│   ├── __init__.py
│   ├── ai_engine.py        # Orchestrator — routes intents, owns LLM clients
│   ├── extraction.py       # EntityExtractor — LLM in, structured dict out (no DB)
│   ├── confrmation.py      # PendingProposal, confirmation/cancellation logic
│   ├── handlers.py         # DB writers — called only after user confirms
│   ├── query.py            # Read-only DB access + QUERY action handler
│   └── narrative.py        # Context builders + story/biography generation
├── databases/
│   ├── db_one              # Schema: persons, relationships, events, migrations, audit_logs
│   └── db_two              # Stored procedures: get_ancestors(), get_descendants(), get_immediate_family()
├── dockerfile              # PostgreSQL 16 + pgvector
├── docker-compose.yaml     # DB service — reads credentials from .env
├── requirements.txt        # Pinned dependencies
└── .env                    # Local secrets (not in repo)
```

---

## Architecture

```
POST /chat/
  └── api.py
        ├── checks pending_confirmations[session_id]
        └── ai_engine.process_query()
              ├── [if PendingProposal pending]  → confirmation gate
              │     ├── "yes"    → handlers.commit_proposal() → DB write
              │     ├── "no"     → cancelled, nothing written
              │     └── other    → re-extract as correction, new proposal
              ├── [if AWAITING_DELETE_CONFIRM]  → delete confirmation gate
              └── [fresh message]
                    ├── extraction.extract_all_entities()   ← LLM call 1
                    ├── query.resolve_references()          ← DB read (name matching)
                    ├── CREATE_* → PendingProposal → confirmation message
                    ├── STORY   → narrative.generate_family_story()  ← LLM call 2
                    ├── DELETE_FAMILY → warning message
                    └── QUERY   → query.handle_query()  ← LLM call 2
```

**Key design rule:** Nothing is written to the database until the user explicitly confirms. `extraction.py` and `confrmation.py` never touch the DB.

---

## Setup

### Prerequisites
- Python 3.12+
- Docker + Docker Compose
- Google AI Studio API key → [aistudio.google.com](https://aistudio.google.com)

### 1. Clone and activate environment

```bash
cd c:\Users\nneka\Documents\roots

# Windows PowerShell
& .\ancestry_env\Scripts\Activate.ps1

# Windows CMD
.\ancestry_env\Scripts\activate.bat

# macOS/Linux
source ancestry_env/bin/activate
```

### 2. Create `.env`

```env
DB_USER=nneka
DB_PASSWORD=your_secure_password
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ancestry_mvp
GEMINI_API_KEY=your_google_api_key
```

> `GEMINI_API_KEY` — not `GOOGLE_API_KEY`. The AI engine reads this key.

### 3. Start the database

```bash
docker-compose up -d

# Verify
docker ps | grep ancestry-db
docker logs ancestry-db
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Initialise the database

```bash
python database.py
```

Expected output:
```
Connecting to: postgresql://nneka:****@localhost:5432/ancestry_mvp
✅ Database connection successful!
Creating tables...
✅ Tables created successfully!
```

Then run the schema scripts to create stored procedures:

```bash
# Connect to the DB and run in order:
psql -U nneka -d ancestry_mvp -f databases/db_one
psql -U nneka -d ancestry_mvp -f databases/db_two
```

### 6. Run the API

```bash
python api.py
# or
uvicorn api:app --reload --host 127.0.0.1 --port 8000
```

API available at: **http://localhost:8000**  
Interactive docs: **http://localhost:8000/docs**

---

## API Endpoints

### `POST /session/`
Creates a session. Returns a `session_id` UUID that scopes all data for that user.  
Pass this in every `/chat/` and `/story/` request body.

```json
// Response
{ "session_id": "uuid-string" }
```

> Sessions are in-memory. They do not survive a server restart. See **Known Limitations** below.

---

### `POST /chat/`

```json
// Request
{
  "session_id": "uuid-string",
  "text": "My dad is Emmanuel Maduike from Nkwerre, Imo",
  "language": "en"
}

// Response
{
  "response": "Here's what I found — please check it before I save anything:\n\n👤 Emmanuel Maduike (new)\n...\n\nReply yes to save this, or tell me what to correct.",
  "action": "AWAITING_CREATE_CONFIRM",
  "language": "en"
}
```

**Action values:**

| action | Meaning |
|--------|---------|
| `AWAITING_CREATE_CONFIRM` | Proposal shown, awaiting yes/no/correction |
| `CREATE_FAMILY_BATCH` | People (+ relationships + events) saved |
| `CREATE_RELATIONSHIP` | Relationship saved |
| `CREATE_EVENT` | Event saved |
| `CREATE_CANCELLED` | User cancelled, nothing written |
| `AWAITING_DELETE_CONFIRM` | Delete warning shown |
| `DELETE_COMPLETE` | Entire tree deleted |
| `DELETE_CANCELLED` | Delete cancelled |
| `QUERY` | Question answered |
| `STORY` | Story generated |
| `ERROR` | Unhandled exception |

**Confirmation:** reply `"yes"` (or: confirm, yep, sure, looks good) to commit.  
**Cancellation:** reply `"no"` (or: cancel, stop, nevermind) to discard.  
**Correction:** any other reply re-extracts with the original text + correction combined.  
**Delete confirmation:** must be exactly `"YES, DELETE EVERYTHING"` (case-insensitive).

---

### `POST /story/`

```json
// Request
{
  "session_id": "uuid-string",
  "person_id": "uuid-string",
  "style": "griot",
  "language": "en"
}

// Response
{ "story": "In the time of our ancestors...", "style": "griot" }
```

Styles: `griot` | `modern` | `children`

---

### `GET /family-tree/{person_id}?depth=2`

```json
{
  "person": { "id": "...", "name": "Emmanuel", "tribe": "Igbo", "town": "Nkwerre" },
  "relatives": [{ "id": "...", "name": "Tunde", "relationship": "CHILD_OF" }],
  "total_relatives": 1
}
```

`depth` range: 1–5 (default 2).

---

### `GET /health`

```json
{ "status": "healthy", "database": "connected" }
```

---

## Database Schema

### Tables

| Table | Purpose |
|-------|---------|
| `persons` | Family members with cultural context and pgvector embedding |
| `relationships` | Directed edges: `PARENT_OF`, `SPOUSE_OF`, `SIBLING_OF` |
| `events` | Life events per person (birth, death, marriage, graduation, etc.) |
| `migrations` | Movement/relocation history extracted from narrative |
| `audit_logs` | Change log (CREATE/UPDATE/DELETE per entity) |

### Key columns — persons

```sql
id UUID, user_id UUID,
name, native_name, birth_date, death_date, gender,
tribe, clan, village_origin, town, state, country,
occupation TEXT[], titles TEXT[], languages TEXT[],
biography TEXT, embedding VECTOR(768)
```

### Stored procedures (db_two)

| Function | Returns |
|----------|---------|
| `get_ancestors(person_id, max_depth)` | Recursive ancestor chain |
| `get_descendants(person_id, max_depth)` | Recursive descendant chain |
| `get_immediate_family(person_id)` | Parents, children, spouses, siblings |

---

## Module Responsibilities

| Module | Reads DB | Writes DB | LLM calls |
|--------|----------|-----------|-----------|
| `extraction.py` | ✗ | ✗ | ✅ (extract_all_entities) |
| `confrmation.py` | ✗ | ✗ | ✗ |
| `query.py` | ✅ | ✗ | ✅ (handle_query) |
| `handlers.py` | ✅ | ✅ | ✗ |
| `narrative.py` | ✗ | ✗ | ✅ (generate_family_story) |
| `ai_engine.py` | ✅ (delete only) | ✅ (delete only) | ✗ (delegates) |
| `graph_service.py` | ✅ | ✅ | ✗ |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DB_USER` | PostgreSQL username |
| `DB_PASSWORD` | PostgreSQL password |
| `DB_HOST` | Database host (`localhost` when using Docker) |
| `DB_PORT` | Database port (default `5432`) |
| `DB_NAME` | Database name (`ancestry_mvp`) |
| `GEMINI_API_KEY` | Google Generative AI key — **not** `GOOGLE_API_KEY` |

---

## Known Limitations (MVP)

**In-memory sessions and pending confirmations** — both `active_sessions` and `pending_confirmations` in `api.py` are plain Python dicts. They are lost on server restart and will not work correctly across multiple uvicorn workers.

Before scaling beyond a single dev instance:
- Move sessions to Redis (short TTL) or a `sessions` DB table
- Move pending confirmations to the same Redis/DB store
- Add a TTL so stale proposals expire automatically

**No authentication** — the `session_id` UUID is the only identity. Anyone with a session UUID can read and write that tree. Real auth (JWT, OAuth) should replace the session seam in `api.py._resolve_user_id()` — everything downstream already takes a `user_id` UUID and will not need to change.

**`shared_buffers=256MB`** — the docker-compose default is conservative. Increase to 25% of available RAM for any non-laptop deployment.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'extraction'`**  
All imports inside `app/` must be relative (`from .extraction import ...`). Bare imports only work when running from inside the package directory.

**`Connection refused` to database**  
```bash
docker-compose up -d
docker logs ancestry-db
```

**`pgvector extension not found`**  
```bash
python database.py
```

**`GEMINI_API_KEY not set`**  
Check `.env` — the key name is `GEMINI_API_KEY`, not `GOOGLE_API_KEY`.

**Stored procedures missing (`get_immediate_family does not exist`)**  
```bash
psql -U nneka -d ancestry_mvp -f databases/db_two
```

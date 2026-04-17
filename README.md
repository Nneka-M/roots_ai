# Ancestry MVP - Family Tree AI Engine

A modern family tree application with AI-powered natural language processing for building and managing African family histories. Built with FastAPI, PostgreSQL, and Google's Generative AI.

---

## 📋 Project Overview

**Roots** is an MVP ancestry management system that allows users to:
- Add family members and relationships through natural language
- Record life events (births, marriages, ceremonies, etc.)
- Query family relationships
- Generate family stories in various styles (griot, modern, children)
- Leverage AI for semantic understanding and context

The system uses **PostgreSQL** with **pgvector** for semantic embeddings and **Apache AGE** for graph queries, combined with **Google Generative AI** (Gemini) for intelligent processing.

---

## 📁 Project Structure & File Descriptions

### Core Application Files

| File | Purpose |
|------|---------|
| **api.py** | FastAPI application with REST endpoints. Main entry point for the application. Handles `/chat/`, `/story/`, and `/family-tree/` endpoints. Routes all user interactions through the AI engine. |
| **ai_engine.py** | Core AI logic and NLP processing. Extracts entities (people, relationships, events) from natural language, classifies user intent (CREATE, QUERY, DELETE, STORY), and orchestrates database operations. Uses Gemini 2.5 Flash for LLM operations and embeddings. |
| **graph_service.py** | Family tree service layer. Manages CRUD operations for persons, relationships, and events. Executes recursive SQL queries to fetch ancestors, descendants, and immediate family. Provides person context for LLM with relationship and event data. |
| **database.py** | SQLAlchemy ORM models and database initialization. Defines `Person`, `Relationship`, and `Event` tables. Handles pgvector setup, connection pooling, and database migrations. |

### Configuration & Dependency Files

| File | Purpose |
|------|---------|
| **pyproject.toml** | Python project configuration (uv package manager). Specifies Python version (3.12+), project metadata, and dependencies. |
| **requirements.txt** | Python package dependencies. Includes FastAPI, SQLAlchemy, LangChain, Google Generative AI, pgvector, PostgreSQL driver, and utilities. |
| **dockerfile** | Docker image for PostgreSQL with pgvector and Apache AGE extensions. Sets up the database container with necessary build dependencies and configurations. |
| **docker-compose.yaml** | Orchestration file for running PostgreSQL database service. Configures environment, ports, volumes, and PostgreSQL parameters for the application. |
| **.env** | Environment variables (local - not in repo). Must contain: `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `GEMINI_API_KEY`. |

### Database Schema Files

| File | Purpose |
|------|---------|
| **databases/db_one** | PostgreSQL initialization script. Creates `persons`, `relationships`, `events`, and `audit_logs` tables with appropriate indexes, constraints, and extensions (uuid, vector). |
| **databases/db_two** | PostgreSQL stored procedures for graph traversal. Contains functions: `get_ancestors()`, `get_descendants()`, `get_immediate_family()` for recursive family tree queries. |

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.12+** (check with `python --version`)
- **PostgreSQL 16** (via Docker or local installation)
- **Google Generative AI API key** (from [Google AI Studio](https://aistudio.google.com))
- **Docker & Docker Compose** (optional, for containerized database)

### 1. Environment Setup

#### Clone the repository
```bash
cd c:\Users\nneka\Documents\roots
```

#### Activate the virtual environment
```bash
# On Windows PowerShell
& .\ancestry_env\Scripts\Activate.ps1

# On Windows CMD
.\ancestry_env\Scripts\activate.bat

# On macOS/Linux
source ancestry_env/bin/activate
```

#### Create `.env` file
```bash
# Create .env file in project root with:
DB_USER=nneka
DB_PASSWORD=your_secure_password
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ancestry_mvp
GEMINI_API_KEY=your_google_api_key
```

### 2. Database Setup

#### Option A: Using Docker (Recommended)
```bash
# Build and start PostgreSQL with pgvector and Apache AGE
docker-compose up -d

# Verify the container is running
docker ps | grep ancestry-db
```

#### Option B: Local PostgreSQL Installation
If using a local PostgreSQL instance:
1. Create the database: `createdb ancestry_mvp`
2. Install pgvector extension: `CREATE EXTENSION vector;`
3. Run the schema scripts in order:
   ```sql
   -- In PostgreSQL psql:
   \i databases/db_one    -- Creates tables
   \i databases/db_two    -- Creates stored procedures
   ```

### 3. Install Python Dependencies

```bash
# Using pip
pip install -r requirements.txt

# Or using uv (faster)
uv pip install -r requirements.txt
```

### 4. Initialize Database

```bash
# This will create tables and enable extensions
python database.py
```

Expected output:
```
Connecting to: postgresql://nneka:****@localhost:5432/ancestry_mvp
✅ pgvector extension enabled
Creating tables...
✅ Tables created successfully!
✅ Database connection successful!
```

### 5. Run the API Server

```bash
# Start FastAPI development server
python api.py

# Or using uvicorn directly
uvicorn api:app --reload --host 127.0.0.1 --port 8000
```

Server runs at: **http://localhost:8000**

---

## 📡 API Endpoints

### Main Endpoints

#### 1. **POST /chat/** - Universal Chat Endpoint
Main conversational interface for all user interactions.

**Request:**
```json
{
  "text": "Add my grandfather Adewale, born 1920, Yoruba from Ibadan",
  "language": "en"
}
```

**Response:**
```json
{
  "response": "✅ Created: Adewale (born 1920)",
  "action": "CREATE_PERSON",
  "language": "en",
  "created_names": ["Adewale"]
}
```

**Supported Actions:**
- `CREATE_PERSON` - Add a new family member
- `CREATE_RELATIONSHIP` - Define relationships between people
- `CREATE_EVENT` - Record life events
- `CREATE_FAMILY_BATCH` - Add multiple people at once
- `QUERY` - Ask questions about family
- `STORY` - Request family stories
- `DELETE_FAMILY` - Delete entire family tree (requires confirmation)

#### 2. **POST /story/** - Direct Story Generation
Generate stories for a known person by ID.

**Request:**
```json
{
  "person_id": "12345678-1234-1234-1234-123456789abc",
  "style": "griot",
  "language": "en"
}
```

**Response:**
```json
{
  "story": "In the time of our ancestors...",
  "style": "griot"
}
```

**Styles:** `griot`, `modern`, `children`

#### 3. **GET /family-tree/{person_id}** - Get Family Tree
Retrieve family tree structure around a person.

**Query Parameters:**
- `depth` (optional, default: 2) - Generations to retrieve (1-5)

**Response:**
```json
{
  "person": {
    "id": "...",
    "name": "Adewale",
    "tribe": "Yoruba",
    "town": "Ibadan"
  },
  "relatives": [
    {
      "id": "...",
      "name": "Tunde",
      "relationship": "CHILD_OF"
    }
  ],
  "total_relatives": 5
}
```

#### 4. **GET /health** - Health Check
```bash
curl http://localhost:8000/health
```

---

## 🎯 Usage Examples

### Example 1: Add a Person
```bash
curl -X POST http://localhost:8000/chat/ \
  -H "Content-Type: application/json" \
  -d '{"text": "Add my grandmother, Iya Funmilayo, she was born in 1945 in Abeokuta"}'
```

### Example 2: Create Relationships
```bash
curl -X POST http://localhost:8000/chat/ \
  -H "Content-Type: application/json" \
  -d '{"text": "Adewale is the father of Tunde"}'
```

### Example 3: Record Events
```bash
curl -X POST http://localhost:8000/chat/ \
  -H "Content-Type: application/json" \
  -d '{"text": "Tunde graduated from University of Lagos in 1985"}'
```

### Example 4: Query Family
```bash
curl -X POST http://localhost:8000/chat/ \
  -H "Content-Type: application/json" \
  -d '{"text": "Who is Tunde'\''s father?"}'
```

### Example 5: Generate Story
```bash
curl -X POST http://localhost:8000/chat/ \
  -H "Content-Type: application/json" \
  -d '{"text": "Tell me the story of Adewale as a griot"}'
```

---

## 🔧 Development & Integration

### Database Schema

#### Persons Table
Stores family members with cultural context and AI embeddings.

```sql
persons (
  id UUID PRIMARY KEY,
  user_id UUID,
  name VARCHAR(255),
  native_name VARCHAR(255),
  birth_date DATE,
  death_date DATE,
  tribe VARCHAR(50),
  clan VARCHAR(100),
  village_origin VARCHAR(100),
  town VARCHAR(100),
  state VARCHAR(100),
  country VARCHAR(100),
  occupation TEXT[],
  titles TEXT[],
  biography TEXT,
  embedding VECTOR(768)
)
```

#### Relationships Table
Defines connections between people.

```sql
relationships (
  id UUID PRIMARY KEY,
  from_person_id UUID REFERENCES persons(id),
  to_person_id UUID REFERENCES persons(id),
  relationship_type VARCHAR(50),  -- PARENT_OF, SPOUSE_OF, SIBLING_OF, etc.
  is_traditional BOOLEAN,
  start_date DATE,
  end_date DATE
)
```

#### Events Table
Records life events for individuals.

```sql
events (
  id UUID PRIMARY KEY,
  person_id UUID REFERENCES persons(id),
  event_type VARCHAR(50),  -- BIRTH, DEATH, MARRIAGE, NAMING_CEREMONY, etc.
  event_date DATE,
  location VARCHAR(255),
  description TEXT,
  cultural_significance TEXT
)
```

### Key Technologies

- **Framework:** FastAPI (async web framework)
- **Database:** PostgreSQL 16 with pgvector (vector embeddings)
- **ORM:** SQLAlchemy
- **AI:** Google Generative AI (Gemini 2.5 Flash)
- **Embeddings:** Google Generative AI Embeddings (768-dim)
- **Graph Queries:** PostgreSQL recursive CTEs
- **Vector Search:** pgvector with cosine distance

---

## 🐛 Troubleshooting

### Issue: "Connection refused" to database
**Solution:** Ensure PostgreSQL is running:
```bash
# Check if Docker container is running
docker ps | grep ancestry-db

# If not running, start it
docker-compose up -d
```

### Issue: "pgvector extension not found"
**Solution:** The extension needs to be created:
```bash
python database.py
```

### Issue: "GEMINI_API_KEY not set"
**Solution:** Ensure `.env` file exists with valid API key:
```bash
cat .env  # Check contents
# Add GEMINI_API_KEY=your_key if missing
```

### Issue: "ModuleNotFoundError: No module named 'langchain'"
**Solution:** Install dependencies:
```bash
pip install -r requirements.txt
```

---

## 📝 Environment Variables Reference

| Variable | Description | Example |
|----------|-------------|---------|
| `DB_USER` | PostgreSQL username | `nneka` |
| `DB_PASSWORD` | PostgreSQL password | `secure_password` |
| `DB_HOST` | Database host | `localhost` or `postgres-age` |
| `DB_PORT` | Database port | `5432` |
| `DB_NAME` | Database name | `ancestry_mvp` |
| `GEMINI_API_KEY` | Google Generative AI API key | `your-api-key-here` |

---

## 🔌 For Developers

### Adding New Relationship Types
Edit `relationship_type` validation in `ai_engine.py` extraction prompts and add to `db_one` schema constraints.

### Adding New Event Types
Extend the event types in:
1. `database.py` Event model
2. `ai_engine.py` extraction prompt
3. `graph_service.py` event handling

### Customizing AI Behavior
Modify the system prompts in `ai_engine.py`:
- `_extract_all_entities()` - Entity extraction logic
- `_handle_query()` - Query response generation
- `generate_family_story()` - Story generation style

---

## 📦 Dependencies Summary

**Core:**
- fastapi, uvicorn (API)
- sqlalchemy, psycopg2-binary (Database)
- pgvector (Vector embeddings)
- python-dotenv (Environment)

**AI:**
- langchain, langchain-google-genai (LLM orchestration)
- google-generativeai (Gemini API)

**Utilities:**
- pydantic (Data validation)
- httpx (HTTP client)

---

## 🎓 Example Integration Flow

1. **User Input** → `/chat/` endpoint
2. **AI Processing** → `ai_engine.process_query()` extracts entities & intent
3. **Data Storage** → `graph_service` creates persons, relationships, events in DB
4. **Embeddings** → Person context embedded with Google Embeddings
5. **Response** → LLM generates human-friendly response
6. **Output** → User receives confirmation or result

---

## 📄 License

This is a private MVP project.

---

## 🤝 Support

For integration questions or bugs, refer to:
- **API Issues:** Check `api.py` and FastAPI logs
- **Database Issues:** Check PostgreSQL logs in Docker: `docker logs ancestry-db`
- **AI Issues:** Check GEMINI_API_KEY and rate limits
- **Entity Extraction:** Review `_extract_all_entities()` logic in `ai_engine.py`

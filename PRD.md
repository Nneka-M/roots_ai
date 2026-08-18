# Roots — Product Requirements Document
**Audience:** Fullstack Engineer  
**Version:** 1.0  
**Status:** MVP

---

## 1. Product Overview

Roots is an AI-powered African family history platform. Users describe their family in natural language — the AI extracts people, relationships, and events, proposes what it found, and only writes to the database after the user confirms. The result is a queryable, story-generating family tree with deep cultural context (tribe, clan, village origin, migration history, traditional titles).

**Core loop:**
```
User types → AI extracts → System proposes → User confirms → Data saved → User queries/gets stories
```

---

## 2. Current Backend (What Exists)

The Python/FastAPI backend is complete and running. The fullstack engineer consumes it — do not modify it.

### Base URL
```
http://localhost:8000
```

### Endpoints

#### `POST /session/`
Creates a new session (acts as user identity for MVP — no login yet).

Request: none  
Response:
```json
{ "session_id": "uuid-string" }
```

Store this in the frontend. Every subsequent request requires it.

---

#### `POST /chat/`
The single conversational endpoint. Handles all user input.

Request:
```json
{
  "session_id": "uuid-string",
  "text": "My dad is Emmanuel Maduike from Nkwerre, Imo",
  "language": "en"
}
```

Response:
```json
{
  "response": "Here's what I found...",
  "action": "AWAITING_CREATE_CONFIRM",
  "language": "en"
}
```

**Action values the frontend must handle:**

| action | What it means | UI behaviour |
|--------|--------------|--------------|
| `AWAITING_CREATE_CONFIRM` | AI extracted data, awaiting yes/no | Show proposal, display confirm/cancel buttons |
| `CREATE_FAMILY_BATCH` | Data was saved | Show success, refresh tree |
| `CREATE_RELATIONSHIP` | Relationship saved | Show success, refresh tree |
| `CREATE_EVENT` | Event saved | Show success |
| `CREATE_CANCELLED` | User cancelled | Show cancelled state, clear pending |
| `AWAITING_DELETE_CONFIRM` | Delete warning shown | Show warning UI, await typed confirmation |
| `DELETE_COMPLETE` | Tree deleted | Clear UI, show empty state |
| `DELETE_CANCELLED` | Delete cancelled | Resume normal state |
| `QUERY` | AI answered a question | Display response as chat message |
| `STORY` | Story generated | Display in story panel |
| `ERROR` | Something failed | Show error message |

**Confirmation flow:**
- After `AWAITING_CREATE_CONFIRM`, user must reply `"yes"` (or similar) to save, or `"no"` to cancel
- Any other reply is treated as a correction — the AI re-extracts and proposes again
- For `AWAITING_DELETE_CONFIRM`, user must type exactly `"YES, DELETE EVERYTHING"`

---

#### `POST /story/`
Direct story generation when you already have a `person_id`.

Request:
```json
{
  "session_id": "uuid-string",
  "person_id": "uuid-string",
  "style": "griot",
  "language": "en"
}
```

Response:
```json
{ "story": "In the time of our ancestors...", "style": "griot" }
```

Styles: `griot` | `modern` | `children`

---

#### `GET /family-tree/{person_id}?depth=2`
Returns the family tree around a person.

Response:
```json
{
  "person": { "id": "...", "name": "Emmanuel", "tribe": "Igbo", "town": "Nkwerre" },
  "relatives": [
    { "id": "...", "name": "Tunde", "relationship": "CHILD_OF" }
  ],
  "total_relatives": 5
}
```

---

#### `GET /health`
Returns `{ "status": "healthy", "database": "connected" }`. Use for connection checks.

---

## 3. Data Models (Read-Only Reference)

The frontend receives these shapes from the API. Do not invent fields.

### Person
```
id, name, native_name, birth_date, death_date, gender,
tribe, clan, village_origin, town, state, country,
occupation[], titles[], languages[], biography,
relationships[], events[], migrations[]
```

### Relationship types
`PARENT_OF` | `SPOUSE_OF` | `SIBLING_OF`

### Event types
`birth` | `death` | `marriage` | `naming_ceremony` | `graduation` | `traditional_festival` | `other`

### Migration
```
from_place, to_place, approx_date (loose string e.g. "1960s"), notes
```

---

## 4. Frontend Requirements

### 4.1 Tech Stack
- **Framework:** React (Next.js preferred) or React + Vite
- **Styling:** Tailwind CSS
- **State:** React Context or Zustand
- **HTTP:** fetch or axios
- **No backend code** — consume the existing FastAPI API only

### 4.2 Session Management
- On first load, call `POST /session/` and store the returned `session_id` in `localStorage`
- On subsequent loads, read from `localStorage` — do not create a new session unless none exists
- Pass `session_id` in every `/chat/` and `/story/` request body

---

## 5. Pages & Views

### 5.1 Onboarding / Empty State
Shown when a session exists but no family members have been added yet.

- Brief explanation of what Roots does
- Prompt examples the user can tap/click to pre-fill the chat input:
  - "My mum is Grace Okafor, Igbo from Enugu"
  - "Add my grandfather Adewale, born 1920, Yoruba from Ibadan"
  - "I was born on 26 Jan 2005 in Nkwerre, Imo"

### 5.2 Main Layout (once tree has members)
Split into two panels:

**Left panel — Chat**
- Message history (user + AI turns)
- Text input at bottom
- Send button
- Language selector (en, ig, yo, ha, pcm) — passed as `language` field

**Right panel — Family Tree**
- Visual tree or list of family members
- Clicking a person opens their profile card
- Story generation button per person

### 5.3 Chat Panel — Detailed Behaviour

**Normal message:**
- User types, hits send
- Show loading indicator while awaiting response
- Display AI `response` text as a chat bubble

**Confirmation state (`AWAITING_CREATE_CONFIRM`):**
- Display the AI's proposal text (the `response` field — it's already formatted)
- Show two buttons: ✅ Confirm | ✗ Cancel
- Clicking Confirm sends `"yes"` to `/chat/`
- Clicking Cancel sends `"no"` to `/chat/`
- User can also type a correction directly — send it as-is

**Delete warning (`AWAITING_DELETE_CONFIRM`):**
- Display the warning text from `response`
- Show a red-bordered input field
- User must type `YES, DELETE EVERYTHING` exactly — send it as the next message
- Show a Cancel button that sends `"no"`

### 5.4 Person Profile Card
Shown in a side drawer or modal when a person is clicked.

Fields to display (show only non-null values):
- Name + native name
- Born / Died
- Tribe, Clan
- Origin (village → town → state → country)
- Occupation, Titles, Languages
- Biography
- Events (list)
- Migration history (list, ordered)
- Relationships (parents, children, spouses, siblings)

Actions:
- Generate Story (opens style picker: griot / modern / children)
- Close

### 5.5 Story View
- Full-screen or large modal
- Story text rendered with line breaks preserved
- Style badge (Griot / Modern / Children's)
- Close / Back button

---

## 6. Visual & Cultural Design Direction

- Warm earth tones: terracotta, ochre, deep brown, cream
- Adinkra or Ankara-inspired decorative accents (subtle, not overwhelming)
- Typography: clean serif for story text, sans-serif for UI
- Family tree nodes: rounded cards, not cold graph circles
- Mobile-first — many target users are on phones

---

## 7. Error Handling

| Scenario | UI behaviour |
|----------|-------------|
| `/session/` fails | Show "Could not start session" with retry button |
| `/chat/` returns `action: ERROR` | Show error text in chat, allow retry |
| Network timeout | Show "Connection lost — check your internet" |
| Person not found in tree (story/tree endpoint) | Show inline error in the relevant panel |
| Empty tree on story request | Show "Add family members first" prompt |

---

## 8. Out of Scope for MVP

- User authentication / login (session UUID is the identity)
- Photo uploads
- Sharing / export
- Multi-user collaboration
- Offline mode
- Push notifications
- The `/interview/` endpoint (exists in backend code but not wired up)

---

## 9. Open Questions for Engineer

1. Should the family tree panel be a visual node graph (e.g. react-flow) or a structured list? A list is faster to build; a graph is more compelling visually.
2. Should story text support markdown rendering? The backend returns markdown-formatted text (bold, line breaks).
3. Language selector — should it auto-detect from the browser locale as a default?
4. Should the session persist across browser tabs (localStorage) or be tab-scoped (sessionStorage)?

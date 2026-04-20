---
title: Phase 3 — Approval Queue
status: ready
created: 2026-04-19
author: jwj2002
type: Fullstack
complexity: COMPLEX
---

# Phase 3 — Approval Queue

## Summary

A draft review workflow between research and publishing. Topics submitted via a web form kick off `gpt-researcher`; results land in a queue with a status machine; the user reviews, edits, approves, or rejects each draft before it becomes eligible for publishing (Phase 5). A hallucination guard flags drafts with too few real sources so they can't be approved without explicit override.

## Goals

- Replace the current "run CLI, read `outputs/*.md`, hope for the best" workflow with a reviewable queue.
- Catch the hallucination failure mode discovered in Phase 1 (writer fabricates citations when retrieval returns 0 sources) before it leaks into publishing.
- Keep the existing CLI working unchanged — the queue is an additional surface, not a replacement.
- Provide the API surface Phase 5 (publishing) will plug into, without doing Phase 5's work here.

## Scope

### In Scope

- SQLite-backed queue table + CRUD API
- Status machine: `pending → researching → (draft_ready | flagged | failed) → (approved | rejected)`
- Hallucination guard: drafts with `source_count < HALLUCINATION_MIN_SOURCES` (default 3) land as `flagged` and require override to approve
- Submit form (topic + optional style/destination)
- List view (filter by status)
- Draft detail view with markdown editor, source list, approve / reject / regenerate buttons
- WebSocket-based status updates (reuse existing `websocket_manager.py`)
- FastAPI BackgroundTasks to run research out-of-request
- Continue saving approved drafts to the brain (same `save_research()` path used by CLI)

### Out of Scope

- **Publishing adapters** (LinkedIn, blog, website) — Phase 5
- **Style presets** (blog / LinkedIn-data / executive-brief / etc.) — Phase 4. Phase 3 stores `style` as a free-form string; Phase 4 will wire it to prompt selection.
- **Scheduling** (publish at a specific time) — Phase 5
- **Authentication** — single-user personal tool; bind server to `127.0.0.1` and defer auth
- **Voice locking / persona layer** — Phase 6
- **Alembic migrations** — use `SQLModel.metadata.create_all()` for now; add migrations when the schema actually evolves
- **Task queue (Celery/Dramatiq/arq)** — BackgroundTasks is sufficient for single-user load
- **Retry logic for failed research runs** — manual regenerate is enough; auto-retry in Phase 6 if needed

## Architecture Overview

```
   Submit Form (Next.js)                    /queue/new
        │
        ▼
   POST /queue           ───┐
   ← { id, status: pending } │    (validates, inserts QueueItem,
                              │     schedules BackgroundTasks.run_research)
        │                    │
        ▼                    │
   List / Detail pages       │
   ← polls or WS subscribes  │
                             │
   BackgroundTasks.run_research(id):
     1. UPDATE status = researching
     2. await GPTResearcher.conduct_research() + write_report()
     3. save_research() to ~/basic-memory/research/   (existing Phase 2 path)
     4. UPDATE:
          - draft_md, sources_json, source_count, brain_path
          - status = draft_ready  (if source_count >= threshold)
                     flagged       (if < threshold)
                     failed        (on exception)
     5. websocket broadcast { type: "queue_update", id, status }

   Review Page (Next.js /queue/[id])
        │
        ▼
   PUT /queue/{id}      — inline markdown edit
   POST .../approve     — override required if status=flagged
   POST .../reject      — terminal
   POST .../regenerate  — reset to pending + re-schedule research
```

## Backend Specification

### New Module: `backend/queue/`

```
backend/queue/
├── __init__.py
├── database.py      # async engine, session factory, DeclarativeBase
├── models.py        # SQLAlchemy 2.0 ORM models + DraftStatus enum
├── schemas.py       # Pydantic request/response models (separate from ORM)
├── mappers.py       # orm_to_response, parse sources_json, etc.
├── repository.py    # CRUD using AsyncSession
├── service.py       # orchestration: submit, transition, regenerate
├── router.py        # FastAPI /queue routes
├── tasks.py         # BackgroundTasks entry: run_research(queue_item_id)
└── exceptions.py    # QueueError, InvalidStateTransition, NotFoundError
```

Pattern mirrors layered-architecture convention (repository → service → router). No existing backend module in this fork uses this pattern yet, so this establishes it for Phase 3+ work. Pydantic schemas are kept separate from ORM models — two class sets, explicit mappers between them.

### Database

- **Engine:** SQLAlchemy 2.0 async (`sqlite+aiosqlite://`)
- **File:** `${BRAIN_PATH}/queue.db` (lives alongside the vault)
- **Schema bootstrap:** `Base.metadata.create_all(engine)` on server startup. No Alembic yet — add when the schema evolves past Phase 3.

### ORM: `QueueItem` (SQLAlchemy 2.0 declarative)

```python
# backend/queue/models.py
from datetime import datetime
from enum import Enum
from uuid import uuid4
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DraftStatus(str, Enum):
    PENDING      = "pending"        # topic submitted, research not started
    RESEARCHING  = "researching"    # gpt-researcher running
    DRAFT_READY  = "draft_ready"    # research done, source_count >= threshold
    FLAGGED      = "flagged"        # research done, source_count < threshold
    APPROVED     = "approved"       # human reviewed + accepted
    REJECTED     = "rejected"       # human reviewed + rejected (terminal)
    FAILED       = "failed"         # research errored out


class QueueItem(Base):
    __tablename__ = "queue_items"

    id:             Mapped[str]          = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    topic:          Mapped[str]
    style:          Mapped[str | None]   = mapped_column(default=None)   # free-form in Phase 3; enum in Phase 4
    destination:    Mapped[str | None]   = mapped_column(default=None)   # free-form in Phase 3; enum in Phase 5
    status:         Mapped[DraftStatus]  = mapped_column(default=DraftStatus.PENDING, index=True)

    draft_md:       Mapped[str | None]   = mapped_column(default=None)
    sources_json:   Mapped[str | None]   = mapped_column(default=None)    # JSON-encoded list[str] of URLs
    source_count:   Mapped[int]          = mapped_column(default=0)
    brain_path:     Mapped[str | None]   = mapped_column(default=None)    # absolute path on disk for traceback
    error_message:  Mapped[str | None]   = mapped_column(default=None)    # populated only when status == failed

    created_at:     Mapped[datetime]     = mapped_column(default=datetime.utcnow)
    updated_at:     Mapped[datetime]     = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)
```

### Pydantic Schemas (separate from ORM)

```python
# backend/queue/schemas.py
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from .models import DraftStatus


class QueueItemCreate(BaseModel):
    topic: str
    style: str | None = None
    destination: str | None = None


class QueueItemUpdate(BaseModel):
    draft_md: str | None = None
    style: str | None = None
    destination: str | None = None


class ApproveRequest(BaseModel):
    override: bool = False


class QueueItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    topic: str
    style: str | None
    destination: str | None
    status: DraftStatus
    draft_md: str | None
    sources: list[str]                  # parsed from sources_json via mapper
    source_count: int
    brain_path: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class StatsResponse(BaseModel):
    pending: int
    researching: int
    draft_ready: int
    flagged: int
    approved: int
    rejected: int
    failed: int
```

### Mapper

`sources_json` is a TEXT column; `sources` in the response is a list. Conversion lives in `mappers.py`:

```python
# backend/queue/mappers.py
import json
from .models import QueueItem
from .schemas import QueueItemResponse


def to_response(item: QueueItem) -> QueueItemResponse:
    return QueueItemResponse(
        **{k: getattr(item, k) for k in QueueItemResponse.model_fields if k != "sources"},
        sources=json.loads(item.sources_json) if item.sources_json else [],
    )
```

⚠️ **ENUM_VALUE VALUES (these are what the frontend sees):**

- `"pending"`, `"researching"`, `"draft_ready"`, `"flagged"`, `"approved"`, `"rejected"`, `"failed"`

Frontend MUST use the string values, not the Python names (`DraftStatus.PENDING` ≠ `"PENDING"`).

### State Transitions (validated server-side)

| From → To          | Trigger                               | Notes |
|---|---|---|
| pending → researching | BackgroundTasks picks up the job | Automatic |
| researching → draft_ready | Research completes, source_count ≥ `HALLUCINATION_MIN_SOURCES` | Default threshold 3 |
| researching → flagged | Research completes, source_count < threshold | Hallucination guard |
| researching → failed | Research raises | `error_message` populated |
| draft_ready → approved | POST /approve | No override needed |
| flagged → approved | POST /approve with `override=true` | 409 without override |
| draft_ready / flagged → rejected | POST /reject | **Terminal** — retry = new submission |
| draft_ready / flagged / failed → pending (regenerate) | POST /regenerate | Clears draft fields; re-schedules research. **Not allowed from rejected.** |

Invalid transitions raise `InvalidStateTransition` → 409 Conflict.

### API Routes

| Method | Path | Purpose | Status codes |
|---|---|---|---|
| POST | `/queue` | Submit new topic; returns created item with `status=pending` | 201, 422 |
| GET | `/queue` | List items; optional `?status=` filter | 200 |
| GET | `/queue/stats` | Counts by status: `{pending, researching, draft_ready, flagged, approved, rejected, failed}` | 200 |
| GET | `/queue/{id}` | Full item including `draft_md` and `sources` | 200, 404 |
| PUT | `/queue/{id}` | Edit `draft_md` only (and `style`/`destination`). Allowed when status ∈ {draft_ready, flagged, approved}. | 200, 404, 409 |
| POST | `/queue/{id}/approve` | Transition to approved. Body: `{override?: bool}`. Required if status=flagged. | 200, 404, 409 |
| POST | `/queue/{id}/reject` | Transition to rejected (terminal — retry = new submission). | 200, 404, 409 |
| POST | `/queue/{id}/regenerate` | Reset and re-run research. Not allowed from `rejected`. | 202, 404, 409 |
| WS | `/queue/stream` | Subscribe to status-change events. Messages: `{type: "queue_update", id, status}`. | — |

### Request/Response Shapes

```ts
// POST /queue  body
{ topic: string; style?: string; destination?: string }

// QueueItem response (shared across GET, PUT, POST /approve, POST /reject)
{
  id: string;
  topic: string;
  style: string | null;
  destination: string | null;
  status: "pending" | "researching" | "draft_ready" | "flagged" | "approved" | "rejected" | "failed";
  draft_md: string | null;
  sources: string[];                 // parsed from sources_json for DX
  source_count: number;
  brain_path: string | null;
  error_message: string | null;
  created_at: string;                // ISO 8601
  updated_at: string;
}

// POST /queue/{id}/approve  body
{ override?: boolean }               // defaults to false

// WS /queue/stream  server → client
{ type: "queue_update", id: string, status: string }
```

### Hallucination Guard Implementation

**Threshold:** `HALLUCINATION_MIN_SOURCES=3` by default, tunable via env var. Lower = more liberal (fewer flags); higher = stricter.

```python
# backend/queue/service.py (fragment)
HALLUCINATION_MIN_SOURCES = int(os.getenv("HALLUCINATION_MIN_SOURCES", "3"))

def classify_draft(source_count: int) -> DraftStatus:
    if source_count < HALLUCINATION_MIN_SOURCES:
        return DraftStatus.FLAGGED
    return DraftStatus.DRAFT_READY
```

On `POST /queue/{id}/approve`:
```python
if item.status == DraftStatus.FLAGGED and not payload.override:
    raise HTTPException(409, "Flagged drafts require override=true")
```

Rationale from Phase 1: the hallucination failure mode produced a draft with **0 real sources** (fabricated citations). Any threshold ≥ 1 catches the pure-hallucination case; `3` additionally catches thin-retrieval drafts where the research returned too little material to be trustworthy.

### Background Research Task

```python
# backend/queue/tasks.py (fragment)
async def run_research(item_id: str) -> None:
    try:
        item = await repository.get(item_id)
        item.status = DraftStatus.RESEARCHING
        await repository.save(item)

        researcher = GPTResearcher(query=item.topic, ...)
        await researcher.conduct_research()
        report = await researcher.write_report()

        sources = sorted({s.get("url") or s.get("href")
                          for s in researcher.get_research_sources()
                          if s.get("url") or s.get("href")})

        brain_path = save_research(
            report_markdown=report, topic=item.topic, sources=sources,
            config=_config_snapshot(item),
        )

        item.draft_md = report
        item.sources_json = json.dumps(sources)
        item.source_count = len(sources)
        item.brain_path = str(brain_path)
        item.status = classify_draft(len(sources))
    except Exception as e:
        item.status = DraftStatus.FAILED
        item.error_message = str(e)
    finally:
        item.updated_at = datetime.utcnow()
        await repository.save(item)
        await websocket_manager.broadcast_queue_update(item.id, item.status)
```

Note: `except Exception` here is a defensive boundary — we MUST NOT let a research crash kill the whole process. The exception is captured into `error_message` and surfaced via the `failed` status.

## Frontend Specification

Extend `frontend/nextjs/`. This is a Next.js 14 app (App Router) packaged as the `gpt-researcher-ui` component library. Our queue pages are additions — don't refactor the existing researcher UI.

### Pages (App Router)

| Route | Purpose |
|---|---|
| `/queue` | List view with filter tabs by status |
| `/queue/new` | Submit form (topic, optional style, optional destination) |
| `/queue/[id]` | Detail view: markdown editor, source list, action buttons, hallucination banner |

### Components to Create

| Component | Location | Purpose |
|---|---|---|
| `QueueTable` | `frontend/nextjs/components/queue/QueueTable.tsx` | Sortable, filterable list |
| `StatusBadge` | `frontend/nextjs/components/queue/StatusBadge.tsx` | Colored pill per DraftStatus value |
| `DraftEditor` | `frontend/nextjs/components/queue/DraftEditor.tsx` | Markdown textarea + preview |
| `SourcesList` | `frontend/nextjs/components/queue/SourcesList.tsx` | Clickable list of source URLs |
| `HallucinationBanner` | `frontend/nextjs/components/queue/HallucinationBanner.tsx` | Red banner on flagged drafts: *"This draft cites fewer than N real sources and may be hallucinated. Review sources before approving."* |
| `ApproveButton` | `frontend/nextjs/components/queue/ApproveButton.tsx` | Disabled-by-default for flagged drafts; override dialog on click |
| `SubmitForm` | `frontend/nextjs/components/queue/SubmitForm.tsx` | Topic / style / destination inputs |

### Components to Reuse

⚠️ **COMPONENT_API risk — verify PropTypes/TypeScript interfaces of these before using:**

| Component | Expected Location | What we need from it |
|---|---|---|
| Button | `frontend/nextjs/components/` (TBD — verify) | Primary / destructive variants |
| Markdown renderer | TBD — check `components/` for existing renderer | View mode for DraftEditor |
| Form input wrappers | TBD — check existing researcher UI | Consistent styling |

**[TODO — read `frontend/nextjs/components/` during implementation before assuming these exist.]**

### Hooks to Create

| Hook | Return shape | Purpose |
|---|---|---|
| `useQueue(status?)` | `{ items, isLoading, error, refetch }` | List with auto-refresh |
| `useQueueItem(id)` | `{ item, isLoading, error, refetch, mutate }` | Single item with optimistic updates |
| `useQueueStream()` | `{ lastEvent }` | WS subscription to `queue_update` events |
| `useSubmitTopic()` | `{ submit, isSubmitting }` | POST /queue wrapper |

### State Management

- **Data fetching:** TanStack Query (`@tanstack/react-query`) — **[TODO — confirm it's already a dep; add if not]**.
- **Cache keys:** `["queue"]`, `["queue", { status }]`, `["queue", id]`.
- **WS bridge:** `useQueueStream` invalidates `["queue"]` and `["queue", id]` on `queue_update` events to trigger refetch.

## Related Patterns (from codebase discovery)

- **FastAPI app surface:** `backend/server/app.py` — new router plugs in here via `app.include_router(queue.router, prefix="/api")`.
- **Websocket infra:** `backend/server/websocket_manager.py` — extend with `broadcast_queue_update()` method; reuse connection pool.
- **Existing frontend:** `frontend/nextjs/` is a Next.js App Router app, packaged also as a React component library. Next.js config and Tailwind already set up.
- **Brain persistence:** `brain/save_research()` is already called from CLI (PR #8); queue reuses the same function.

## Risk Flags

### ⚠️ ENUM_VALUE (high-risk per critical patterns)

DraftStatus is a fullstack enum. All seven values MUST match between Python and TypeScript:

- Python: `DraftStatus.DRAFT_READY` → value `"draft_ready"`
- TypeScript: use the literal `"draft_ready"` — **never** `"DRAFT_READY"`

**Mitigation:** All values are lowercase snake_case. Define a shared TS type:
```ts
type DraftStatus = "pending" | "researching" | "draft_ready" | "flagged" | "approved" | "rejected" | "failed";
```

### ⚠️ COMPONENT_API (17% of frontend failures)

Planned reuse of Button, Markdown renderer, and form inputs from `frontend/nextjs/components/`. Before writing any queue component:
- [ ] Read the actual TypeScript interfaces of reused components
- [ ] Never invent props that don't exist
- [ ] If a needed component doesn't exist, create it rather than monkey-patching an existing one

### ⚠️ VERIFICATION_GAP (63% of all failures)

New module pattern (`backend/queue/`) doesn't mirror any existing code in this fork. Before implementation:
- [ ] Read `backend/server/app.py` to confirm how routers are registered
- [ ] Read `backend/server/websocket_manager.py` to confirm connection-pool API
- [ ] Confirm TanStack Query is already a dependency; add to `package.json` if not

### ⚠️ MULTI_MODEL (medium-risk)

Queue writes to two places per approval: the SQLite `queue_items` table AND the brain (via `save_research()`). These are independent (different stores, no transaction boundary). Design:
- Brain write happens FIRST (inside `run_research`), then DB update commits the `brain_path`. If the DB write fails after the brain write succeeds, we have an orphan markdown file — the file has its own frontmatter and is still useful; the orphan is acceptable.
- Approval does NOT write to the brain again; the brain copy is write-once at research time.

### Concurrency / Race Conditions

Single-user, single-process — race conditions unlikely. SQLite with `aiosqlite` serializes writes at the DB level. Defer connection pool tuning to if/when we go multi-user.

### Auth Model (Phase 3)

Server binds to `127.0.0.1:8000` — no token, no session, no password. Rationale: the queue never publishes externally in Phase 3; worst case a rogue local process approves a draft that sits in the queue, nothing escapes.

**Upgrade plan (Phase 5):** when publishing adapters go live, add a static bearer token via `QUEUE_API_TOKEN` env var and `Authorization: Bearer {token}` on all requests. Frontend pulls the token from build-time env. That's when any URL Phase 5 exposes becomes a real attack surface.

## Acceptance Criteria

- [ ] `POST /queue` with `{topic}` returns 201 and a QueueItem with `status=pending`
- [ ] Within ~3 minutes, the same item's status reaches `draft_ready` OR `flagged` OR `failed`
- [ ] `GET /queue` returns the item with its populated `draft_md`, `sources`, `source_count`
- [ ] A draft with 0 real sources lands as `flagged`, not `draft_ready`
- [ ] `POST /queue/{id}/approve` returns 409 for a `flagged` item when `override` is absent or false
- [ ] `POST /queue/{id}/approve` with `override=true` transitions flagged → approved
- [ ] `PUT /queue/{id}` updates `draft_md` in-place and bumps `updated_at`
- [ ] `POST /queue/{id}/regenerate` resets the draft and re-runs research
- [ ] WebSocket clients receive `queue_update` events for every status change
- [ ] `/queue` page renders a list with filter tabs
- [ ] `/queue/[id]` renders a markdown editor, source list, and status-appropriate action buttons
- [ ] Flagged drafts show the hallucination banner with source count and threshold
- [ ] Existing CLI (`python cli.py ...`) still works unchanged
- [ ] Approved drafts remain queryable in Claude Desktop via Basic Memory

## Resolved Decisions

All pre-implementation questions have been answered. Decisions captured here for traceability — each one is reflected in the sections above.

1. **ORM:** Raw SQLAlchemy 2.0 + separate Pydantic schemas. Two class sets with explicit mappers. Reflected in `backend/queue/` module layout, model fragment, schema fragment.
2. **Hallucination threshold:** `HALLUCINATION_MIN_SOURCES=3`, tunable via env var.
3. **CLI + queue coexist.** Both call `save_research()`; the brain is identical regardless of source. CLI is not rewritten.
4. **`GET /queue/stats` included** — returns a dict of counts per status.
5. **Rejection is terminal.** Retry = submit a new queue item. Regenerate endpoint explicitly refuses `rejected` items.
6. **Auth Phase 3:** bind to `127.0.0.1`, no auth. Upgrade to `QUEUE_API_TOKEN` bearer token in Phase 5 when publishing adapters go live.

## Completeness

| Section | Status |
|---|---|
| Summary + Goals + Scope | ✅ Complete |
| Backend: models, routes, state machine | ✅ Complete |
| Backend: service and task code sketch | ✅ Complete |
| Frontend: pages, components, hooks | ⚠️ Partial — reused component APIs marked [TODO] pending codebase read |
| API contract (request/response shapes) | ✅ Complete |
| Risk flags | ✅ Complete |
| Acceptance criteria | ✅ 13 items |
| Decisions resolved | ✅ All 6 pre-implementation questions answered |

Spec is **ready for `/spec-review`**. The remaining [TODO] items are intentional — they're questions to answer by reading `frontend/nextjs/components/` during implementation, not decisions to make at spec time.

---

**Next Steps:**
1. `/spec-review docs/features/phase-3-approval-queue.md` — generates GitHub issues per section (expect ~5–7 issues: DB setup, models+schemas, routes, state machine + guard, background task, frontend pages, frontend components)
2. `/orchestrate <issue-number>` per issue
3. Independent issues can run in parallel (`--parallel` flag)

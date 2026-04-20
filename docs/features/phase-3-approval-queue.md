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
                              │     schedules BackgroundTasks →
                              │     process_queue_item(id, real_runner))
        │                    │
        ▼                    │
   List / Detail pages       │
   ← refetch on connect,     │
     invalidate on WS delta  │
                             │
   service.process_queue_item(id, runner):
     1. Load item; if missing, log + return
     2. Compare-and-set: status = researching,
                         active_run_id = new UUID,
                         research_started_at = now
     3. result = await runner.run(topic, style, ...)
     4. Re-load; if active_run_id has changed, discard (a newer
        regenerate superseded this run)
     5. UPDATE:
          - draft_md, sources_json, source_count
          - status = draft_ready  (if source_count >= threshold)
                     flagged       (if < threshold)
                     failed        (on exception)
        NOTE: brain is NOT written here. Brain save happens only
              on POST /approve (Option A).
     6. websocket broadcast { type: "queue_update", id, status,
                              updated_at }

   Review Page (Next.js /queue/[id])
        │
        ▼
   PUT /queue/{id}      — inline markdown edit (draft_ready / flagged only;
                           approved items are IMMUTABLE)
   POST .../approve     — override required if status=flagged;
                           writes to brain via save_research();
                           sets approved_at
   POST .../reject      — terminal; brain never written
   POST .../regenerate  — atomic: mints new active_run_id,
                           resets to pending, rejects if status ∈
                           {pending, researching, rejected}

   Server startup:
     Reconcile any item left in `researching` older than
     RECONCILE_TIMEOUT (default 10 min) → mark failed with
     error_message = "reconciled after server restart"
```

## Backend Specification

### New Module: `backend/queue/`

```
backend/queue/
├── __init__.py
├── database.py       # async engine, session factory, DeclarativeBase
├── models.py         # SQLAlchemy 2.0 ORM models + DraftStatus enum
├── schemas.py        # Pydantic request/response models (separate from ORM)
├── mappers.py        # orm_to_response, parse sources_json, etc.
├── repository.py     # CRUD using AsyncSession
├── runners.py        # ResearchRunner protocol + GPTResearcherRunner impl
├── service.py        # process_queue_item(id, runner) + transitions
├── reconciliation.py # startup scan for stale `researching` items
├── router.py         # FastAPI /queue routes
├── tasks.py          # thin BackgroundTasks adapter → service.process_queue_item
└── exceptions.py     # QueueError, InvalidStateTransition, NotFoundError
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

    id:                   Mapped[str]             = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    topic:                Mapped[str]
    style:                Mapped[str | None]      = mapped_column(default=None)      # free-form in Phase 3; enum in Phase 4
    destination:          Mapped[str | None]      = mapped_column(default=None)      # free-form in Phase 3; enum in Phase 5
    status:               Mapped[DraftStatus]     = mapped_column(default=DraftStatus.PENDING, index=True)

    draft_md:             Mapped[str | None]      = mapped_column(default=None)
    sources_json:         Mapped[str | None]      = mapped_column(default=None)       # JSON-encoded list[str] of URLs
    source_count:         Mapped[int]             = mapped_column(default=0)
    brain_path:           Mapped[str | None]      = mapped_column(default=None)       # populated only on approval
    error_message:        Mapped[str | None]      = mapped_column(default=None)       # populated only when status == failed

    # Durability + concurrency (added per spec review findings #1 and #5)
    active_run_id:        Mapped[str | None]      = mapped_column(default=None)       # UUID of the current research run; used for CAS on regenerate
    research_started_at:  Mapped[datetime | None] = mapped_column(default=None)       # used by startup reconciliation
    approved_at:          Mapped[datetime | None] = mapped_column(default=None)       # set by POST /approve
    approved_with_override: Mapped[bool]          = mapped_column(default=False)      # true if approved from `flagged`

    created_at:           Mapped[datetime]        = mapped_column(default=datetime.utcnow)
    updated_at:           Mapped[datetime]        = mapped_column(default=datetime.utcnow, onupdate=datetime.utcnow)
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
    brain_path: str | None              # populated only when status >= approved
    error_message: str | None
    approved_at: datetime | None
    approved_with_override: bool
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
| pending → researching | BackgroundTasks picks up the job | Sets `active_run_id`, `research_started_at` |
| researching → draft_ready | Research completes, source_count ≥ `HALLUCINATION_MIN_SOURCES` | Default threshold 3 |
| researching → flagged | Research completes, source_count < threshold | Hallucination guard |
| researching → failed | Research raises, OR startup reconciliation marks stale | `error_message` populated |
| draft_ready → approved | POST /approve | No override needed; writes to brain |
| flagged → approved | POST /approve with `override=true` | 409 without override; writes to brain; sets `approved_with_override=true` |
| draft_ready / flagged → rejected | POST /reject | **Terminal** — retry = new submission; brain never written |
| draft_ready / flagged / failed → pending (regenerate) | POST /regenerate | Compare-and-set: mints new `active_run_id`, clears draft fields, re-schedules. **Not allowed from pending, researching, approved, or rejected.** |

**Invariants:**
- **Approved is immutable.** No `PUT /queue/{id}` is allowed when `status == approved`. To amend approved content, create a new queue item with the corrected topic/draft and re-approve.
- **Only one active run per item.** `active_run_id` is updated atomically on regenerate; in-flight runs check their run ID before committing their result and discard if superseded.
- **Brain is written exactly once per approved item.** If `brain_path` is non-null, don't overwrite.

Invalid transitions raise `InvalidStateTransition` → 409 Conflict.

### API Routes

| Method | Path | Purpose | Status codes |
|---|---|---|---|
| POST | `/queue` | Submit new topic; returns created item with `status=pending` | 201, 422 |
| GET | `/queue` | List items; optional `?status=` filter. Clients MUST call this on WS (re)connect to sync state. | 200 |
| GET | `/queue/stats` | Counts by status: `{pending, researching, draft_ready, flagged, approved, rejected, failed}` | 200 |
| GET | `/queue/{id}` | Full item including `draft_md` and `sources` | 200, 404 |
| PUT | `/queue/{id}` | Edit `draft_md` / `style` / `destination`. **Allowed only when status ∈ {draft_ready, flagged}** — approved drafts are immutable. | 200, 404, 409 |
| POST | `/queue/{id}/approve` | Transition to approved. Body: `{override?: bool}`. Required if status=flagged. Writes to brain via `save_research()`; sets `approved_at`. | 200, 404, 409 |
| POST | `/queue/{id}/reject` | Transition to rejected (terminal — retry = new submission). Brain never written. | 200, 404, 409 |
| POST | `/queue/{id}/regenerate` | Atomic reset: mints new `active_run_id`, clears draft fields, re-schedules research. Allowed from {draft_ready, flagged, failed}. | 202, 404, 409 |
| WS | `/queue/stream` | Subscribe to status-change deltas. Messages: `{type: "queue_update", id, status, updated_at}`. Delta stream only; clients refetch via GET on (re)connect. | — |

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

// WS /queue/stream  server → client (delta only)
{ type: "queue_update", id: string, status: string, updated_at: string }
```

### WebSocket Reconnection Contract

The WS stream is a **delta** feed only — no snapshot on connect, no replay of missed events. The reconnection contract is:

1. On WS connect (including reconnect), the client MUST call `GET /queue` (and `GET /queue/{id}` if a detail page is open) to sync state.
2. The client then processes deltas, using `updated_at` to detect out-of-order delivery.
3. If `updated_at` in a delta is older than the locally cached `updated_at` for that item, discard the delta.

This keeps the server simple (no snapshot builder, no replay buffer) and shifts correctness to the client, which already needs refetch-on-focus for other reasons.

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

### Research Runner Protocol (test seam — fix for finding #8)

```python
# backend/queue/runners.py
from typing import Protocol
from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchResult:
    report_markdown: str
    sources: list[str]   # unique, ordered


class ResearchRunner(Protocol):
    async def run(self, topic: str, *, style: str | None = None) -> ResearchResult: ...


class GPTResearcherRunner:
    """Production runner — wraps gpt-researcher."""

    async def run(self, topic: str, *, style: str | None = None) -> ResearchResult:
        researcher = GPTResearcher(query=topic, ...)  # style → prompt mapping comes in Phase 4
        await researcher.conduct_research()
        report = await researcher.write_report()
        sources = sorted({
            (s.get("url") or s.get("href"))
            for s in researcher.get_research_sources()
            if s.get("url") or s.get("href")
        })
        return ResearchResult(report_markdown=report, sources=list(sources))
```

### Service Orchestration (fix for findings #1, #2, #5, #8)

```python
# backend/queue/service.py  (fragment)
import logging
import json
from datetime import datetime
from uuid import uuid4

logger = logging.getLogger(__name__)


async def process_queue_item(item_id: str, runner: ResearchRunner) -> None:
    """Run research for one queue item.

    Safe to run as a BackgroundTask. Injectable `runner` makes this
    deterministically testable without hitting the real research engine.
    """
    item: QueueItem | None = None
    claimed_run_id: str | None = None

    try:
        item = await repository.get(item_id)
        if item is None:
            logger.error("Queue item %s not found; discarding task", item_id)
            return

        # Claim the item: mint a fresh run_id, bump status.
        # A subsequent regenerate will mint a different run_id, which lets
        # us discard this run's result if we've been superseded.
        claimed_run_id = str(uuid4())
        item.active_run_id = claimed_run_id
        item.status = DraftStatus.RESEARCHING
        item.research_started_at = datetime.utcnow()
        item.error_message = None
        await repository.save(item)
        await websocket_manager.broadcast_queue_update(item.id, item.status, item.updated_at)

        # Heavy work — no DB session held here.
        result = await runner.run(item.topic, style=item.style)

        # Re-load and check we haven't been superseded.
        fresh = await repository.get(item_id)
        if fresh is None or fresh.active_run_id != claimed_run_id:
            logger.warning(
                "Run %s for item %s superseded before commit; discarding result",
                claimed_run_id, item_id,
            )
            return

        fresh.draft_md = result.report_markdown
        fresh.sources_json = json.dumps(result.sources)
        fresh.source_count = len(result.sources)
        fresh.status = classify_draft(fresh.source_count)
        await repository.save(fresh)
        await websocket_manager.broadcast_queue_update(fresh.id, fresh.status, fresh.updated_at)

    except Exception as exc:  # defensive boundary — must not kill the server
        logger.exception("Research failed for item %s (run %s)", item_id, claimed_run_id)
        # Only persist a failure if we loaded the item AND still own the run.
        if item is None:
            return
        latest = await repository.get(item_id)
        if latest is None or latest.active_run_id != claimed_run_id:
            return  # superseded; don't stomp on the new run's state
        latest.status = DraftStatus.FAILED
        latest.error_message = str(exc)[:500]
        await repository.save(latest)
        await websocket_manager.broadcast_queue_update(latest.id, latest.status, latest.updated_at)
```

**Why this shape:**
- `item` is `None`-initialized — the `except` block never references an unbound name (fix #2).
- `claimed_run_id` is the compare-and-set token. A regenerate that arrives mid-run mints a new id; the old run's "I'm done, commit my result" step sees the mismatch and bails (fix #5).
- Two `repository.save()` commits, not one — we persist the `researching` claim *before* the long await so a server crash leaves a recoverable stale row (see reconciliation below) rather than an invisible in-flight task (fix #1).
- The `runner` parameter is a Protocol; production uses `GPTResearcherRunner`, tests use a `FakeRunner` that returns a canned `ResearchResult` (fix #8).

### Approval Writes to the Brain (fix for finding #4, Option A)

```python
# backend/queue/service.py  (fragment)
async def approve(item_id: str, *, override: bool = False) -> QueueItem:
    item = await repository.require(item_id)

    if item.status == DraftStatus.FLAGGED and not override:
        raise InvalidStateTransition("Flagged drafts require override=true")
    if item.status not in {DraftStatus.DRAFT_READY, DraftStatus.FLAGGED}:
        raise InvalidStateTransition(f"Cannot approve from {item.status}")
    if item.brain_path is not None:
        raise InvalidStateTransition("Already approved and written to brain")

    # Write to the brain exactly once, then mark approved.
    sources = json.loads(item.sources_json) if item.sources_json else []
    brain_path = save_research(
        report_markdown=item.draft_md or "",
        topic=item.topic,
        sources=sources,
        config={"queue_item_id": item.id, "approved_with_override": item.status == DraftStatus.FLAGGED and override},
    )

    item.brain_path = str(brain_path)
    item.approved_at = datetime.utcnow()
    item.approved_with_override = (item.status == DraftStatus.FLAGGED and override)
    item.status = DraftStatus.APPROVED
    await repository.save(item)
    return item
```

**Key consequences of Option A (save-on-approval):**
- Failed, rejected, and still-in-review research **never** lands in the brain. The brain stays a curated archive of approved content.
- The existing hardcoded `status: "draft_ready"` in `brain/writer.py` is no longer a lie — approval is the trigger. (A later cleanup can switch the frontmatter to `status: "approved"` since that's always true for brain-written items.)
- The CLI flow (`python cli.py ...`) continues to write directly to the brain with `status: "draft_ready"`, as today. The CLI is an unreviewed fast-path; the queue is reviewed. Both are valid surfaces, with different semantics documented in the brain's frontmatter.

### Startup Reconciliation (fix for finding #1)

```python
# backend/queue/reconciliation.py (fragment)
RECONCILE_TIMEOUT = timedelta(minutes=int(os.getenv("QUEUE_RECONCILE_MINUTES", "10")))

async def reconcile_stale_runs() -> int:
    """Called once at server startup. Marks abandoned runs as failed."""
    cutoff = datetime.utcnow() - RECONCILE_TIMEOUT
    stale = await repository.list_stale_researching(cutoff)
    for item in stale:
        item.status = DraftStatus.FAILED
        item.error_message = "reconciled after server restart"
        await repository.save(item)
    return len(stale)
```

Wired into `backend/server/app.py` via an `on_event("startup")` or lifespan handler. A non-zero return is logged. Items can be recovered by the user via `POST /regenerate`.

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

### Components to Reuse (verified against `frontend/nextjs/components/` and `frontend/nextjs/helpers/`)

The existing researcher UI is tightly coupled to the research flow and does **not** expose a shared design-system layer. Only the markdown helper is cleanly reusable.

| Asset | Location | Kind | Use for |
|---|---|---|---|
| `markdownToHtml` | `frontend/nextjs/helpers/markdownHelper.ts` | Pure function | Rendering `draft_md` preview in `DraftEditor` view mode |

**What does NOT exist (per directory inventory):**
- No shared `Button` component — existing UI uses raw `<button>` with per-page Tailwind classes.
- No shared markdown renderer component — the helper returns HTML; callers render it themselves.
- No shared form wrappers — `Task/ResearchForm.tsx` and `ResearchBlocks/elements/InputArea.tsx` are purpose-built for the research UI and tightly coupled to its state.

**Consequence:** the queue module owns its own primitives. Create queue-local `Button`, `Input`, and `StatusBadge` components under `frontend/nextjs/components/queue/`. Do **not** attempt to extract/generalize existing researcher UI for reuse in Phase 3 — that's a separate refactor, out of scope.

**XSS note:** `draft_md` is LLM-generated content. When rendering the HTML output of `markdownToHtml`, sanitize with a library such as DOMPurify before mounting. Never ship a markdown preview that inserts raw HTML from an LLM without sanitization. Add this to the `DraftEditor` component's acceptance criteria.

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

Queue writes to two places on approval: the SQLite `queue_items` table AND the brain (via `save_research()`). These are independent (different stores, no transaction boundary). Design:
- Brain write happens **only on approval** (Option A from the spec review). Research in progress, flagged drafts, rejected drafts, and failures never touch the brain.
- Within `approve()`: brain write happens FIRST, then DB update sets `brain_path + approved_at + status=approved`. If the DB write fails after the brain write succeeds, we have an orphan markdown file and a still-`draft_ready` row. The orphan is self-describing (has frontmatter) and can be manually reconciled.
- The `brain_path is not None` check in `approve()` prevents double-writes if a retry lands on an already-approved item.

### Concurrency / Race Conditions

Single-user, single-process — race conditions unlikely at the load level, but two realistic vectors exist and are addressed:

- **Double-regenerate** (two tabs or double-click): handled by the `active_run_id` compare-and-set token. Only the most recent regenerate's run ID can commit a result; superseded runs log and discard.
- **Server restart mid-research**: handled by startup reconciliation. Items in `researching` older than `QUEUE_RECONCILE_MINUTES` (default 10) are marked `failed` with `error_message="reconciled after server restart"`. User can `POST /regenerate` to retry.

SQLite with `aiosqlite` serializes writes at the DB level. Defer connection pool tuning to if/when we go multi-user.

### Auth Model (Phase 3)

Server binds to `127.0.0.1:8000` — no token, no session, no password. Rationale: the queue never publishes externally in Phase 3; worst case a rogue local process approves a draft that sits in the queue, nothing escapes.

**Upgrade plan (Phase 5):** when publishing adapters go live, add a static bearer token via `QUEUE_API_TOKEN` env var and `Authorization: Bearer {token}` on all requests. Frontend pulls the token from build-time env. That's when any URL Phase 5 exposes becomes a real attack surface.

## Acceptance Criteria

### Backend — happy path
- [ ] `POST /queue` with `{topic}` returns 201 and a QueueItem with `status=pending`
- [ ] Within a deterministic test (using a fake `ResearchRunner`), the same item's status transitions to `draft_ready` OR `flagged` OR `failed` synchronously
- [ ] `GET /queue` returns the item with `draft_md`, `sources`, `source_count`
- [ ] `GET /queue/stats` returns a dict of counts keyed by all 7 `DraftStatus` values

### Backend — hallucination guard (fix #4, #5, #6, #8 testable)
- [ ] A draft with `source_count < HALLUCINATION_MIN_SOURCES` lands as `flagged`, not `draft_ready`
- [ ] `POST /approve` returns 409 for a `flagged` item when `override` is absent or false
- [ ] `POST /approve` with `override=true` transitions `flagged` → `approved` and sets `approved_with_override=true`
- [ ] `POST /approve` writes a new markdown file under `~/basic-memory/research/` and sets `brain_path`
- [ ] A second `POST /approve` on an already-approved item returns 409 (brain never double-written)

### Backend — immutability & durability (fix #1, #2, #3, #5)
- [ ] `PUT /queue/{id}` on an `approved` item returns 409
- [ ] Editing (`PUT`) a `draft_ready` or `flagged` draft bumps `updated_at`
- [ ] `POST /regenerate` on an item currently in `pending` or `researching` returns 409
- [ ] `POST /regenerate` on a `rejected` item returns 409
- [ ] Two concurrent regenerate calls both may pass routing, but only the later one's `active_run_id` commits a result (first is discarded with a log line)
- [ ] Items stuck in `researching` beyond `QUEUE_RECONCILE_MINUTES` transition to `failed` on next server startup with `error_message="reconciled after server restart"`
- [ ] An exception inside `ResearchRunner.run()` is captured into `error_message` and results in `status=failed`; the server does not crash

### Backend — brain integration
- [ ] `failed`, `rejected`, and in-progress items never appear in `~/basic-memory/research/`
- [ ] Approved items appear with full frontmatter (topic, sources, source_count, config snapshot)
- [ ] Claude Desktop (after `basic-memory reindex`) can find approved drafts via its MCP tools

### Frontend
- [ ] `/queue` page renders a list with filter tabs and counts from `GET /queue/stats`
- [ ] `/queue/[id]` renders a markdown editor, source list, and status-appropriate action buttons
- [ ] `/queue/[id]` is read-only when `status == approved` (edit UI disabled with a visible reason)
- [ ] Flagged drafts show the hallucination banner with source count and threshold
- [ ] Approve button for flagged drafts requires an explicit override confirmation dialog
- [ ] `DraftEditor` preview sanitizes `markdownToHtml` output before rendering (XSS guard)
- [ ] On WS reconnect, the UI calls `GET /queue` (and `GET /queue/{id}` if a detail page is open) to re-sync

### Compatibility
- [ ] Existing CLI (`python cli.py ...`) still works unchanged; CLI-written notes continue to land in the brain with `status: draft_ready` frontmatter (unreviewed fast-path semantics)

## Resolved Decisions

All pre-implementation questions and Codex review findings have been answered. Decisions captured here for traceability — each one is reflected in the sections above.

### Pre-Implementation Q&A

1. **ORM:** Raw SQLAlchemy 2.0 + separate Pydantic schemas. Two class sets with explicit mappers. Reflected in `backend/queue/` module layout, model fragment, schema fragment.
2. **Hallucination threshold:** `HALLUCINATION_MIN_SOURCES=3`, tunable via env var.
3. **CLI + queue coexist.** CLI writes directly to brain with `draft_ready` (unreviewed fast-path). Queue writes to brain only on approval (reviewed path). Both are valid surfaces with different semantics.
4. **`GET /queue/stats` included** — returns a dict of counts per status.
5. **Rejection is terminal.** Retry = submit a new queue item. Regenerate endpoint explicitly refuses `rejected` items.
6. **Auth Phase 3:** bind to `127.0.0.1`, no auth. Upgrade to `QUEUE_API_TOKEN` bearer token in Phase 5 when publishing adapters go live.

### Codex Adversarial Review (fold-in)

7. **Option A for brain integration** — brain is written **only on approval**, never during research. Failed/rejected/in-progress research never lands on disk. Eliminates the divergence between hardcoded `status: "draft_ready"` frontmatter and queue truth.
8. **Durability:** `research_started_at` + startup `reconcile_stale_runs()` scan transitions abandoned `researching` rows to `failed`. `QUEUE_RECONCILE_MINUTES` env var (default 10).
9. **Safe run sketch:** `item: QueueItem | None = None` at function start; guarded mutations in `except`/`finally`; re-load before commit to detect supersession.
10. **Approved is immutable:** `PUT /queue/{id}` rejects when `status == approved`. To amend, submit a new queue item.
11. **Regenerate atomicity:** `active_run_id` compare-and-set. Regenerate mints a new UUID; in-flight runs check their run ID before committing and discard if superseded. Regenerate refuses `pending`, `researching`, `approved`, `rejected`.
12. **WS contract:** delta stream only. Clients must `GET /queue` on (re)connect to sync; `updated_at` in deltas lets clients detect out-of-order delivery.
13. **Frontend reuse reality:** only `markdownToHtml` helper is genuinely reusable. Queue owns its own `Button`, `Input`, `StatusBadge` primitives. DraftEditor preview sanitizes rendered HTML.
14. **Test seam:** `process_queue_item(id, runner: ResearchRunner)` service entry point is injectable. Production uses `GPTResearcherRunner`; tests use a fake runner for deterministic synchronous assertions — no polling, no timeouts, no flakiness.

## Completeness

| Section | Status |
|---|---|
| Summary + Goals + Scope | ✅ Complete |
| Backend: models, routes, state machine | ✅ Complete |
| Backend: service, runner protocol, reconciliation | ✅ Complete |
| Frontend: pages, components, hooks | ✅ Complete — reuse table verified against actual `frontend/nextjs/components/` inventory |
| API contract (request/response shapes) | ✅ Complete (WS reconnection contract added) |
| Risk flags | ✅ Complete (MULTI_MODEL updated for save-on-approval) |
| Acceptance criteria | ✅ 23 items across backend, frontend, compatibility |
| Decisions resolved | ✅ All 6 pre-implementation Q&A + all 8 Codex review findings folded in |

Spec is **ready for `/spec-review`**. Every open question has a documented answer; every known risk has a named mitigation; every ambiguity has an acceptance-criterion test.

---

**Next Steps:**
1. `/spec-review docs/features/phase-3-approval-queue.md` — generates GitHub issues per section (expect ~7–9 issues now: DB setup + reconciliation, models + schemas + mappers, runner protocol + service, router + state machine + guard, approval + brain integration, frontend queue list + stats, frontend detail + editor + XSS sanitization, frontend WS + refetch)
2. `/orchestrate <issue-number>` per issue
3. Independent issues can run in parallel (`--parallel` flag)

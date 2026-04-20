---
title: Spec Review — Phase 3 Approval Queue
spec: docs/features/phase-3-approval-queue.md
spec_merged_at: 2026-04-19 (PR #11)
reviewed_at: 2026-04-19
reviewer: Claude Code + Codex adversarial review (fold-in in PR #11)
status: complete
---

# Spec Review — Phase 3 Approval Queue

## Summary

The Phase 3 spec merged as `b2dc1b2b` after a Codex adversarial review pass that surfaced 8 findings (all accepted). This review decomposes the 714-line spec into 8 GitHub issues suitable for `/orchestrate`, each with explicit test requirements and risk flags.

**Scope reviewed:** entire spec — 714 lines covering backend module (10 files), frontend components, API contract, state machine, hallucination guard, durability + concurrency, reconciliation, test seams.

**Gap classification:** No prior implementation exists. Every line of the spec maps to "Missing" — this is net-new work on the fork.

## Issues Created

All issues created on `jwj2002/gpt-researcher` with labels: `phase-3`, `from-spec`, `backend`|`frontend`, `complexity:simple`|`complexity:complex`.

| # | Title | Stack | Complexity |
|---|---|---|---|
| [#12](https://github.com/jwj2002/gpt-researcher/issues/12) | chore(queue): scaffold module, async SQLite engine, lifespan wiring | backend | SIMPLE |
| [#13](https://github.com/jwj2002/gpt-researcher/issues/13) | feat(queue): ORM model, Pydantic schemas, mappers | backend | SIMPLE |
| [#14](https://github.com/jwj2002/gpt-researcher/issues/14) | feat(queue): repository + ResearchRunner protocol + service orchestration | backend | COMPLEX |
| [#15](https://github.com/jwj2002/gpt-researcher/issues/15) | feat(queue): startup reconciliation for stale researching rows | backend | SIMPLE |
| [#16](https://github.com/jwj2002/gpt-researcher/issues/16) | feat(queue): FastAPI router + WebSocket delta broadcast | backend | COMPLEX |
| [#17](https://github.com/jwj2002/gpt-researcher/issues/17) | feat(frontend): queue primitives + TanStack Query hooks | frontend | SIMPLE |
| [#18](https://github.com/jwj2002/gpt-researcher/issues/18) | feat(frontend): /queue list page + /queue/new submit page | frontend | COMPLEX |
| [#19](https://github.com/jwj2002/gpt-researcher/issues/19) | feat(frontend): /queue/[id] detail + XSS-safe DraftEditor + hallucination banner | frontend | COMPLEX |

## Dependency Graph

```
#12 (scaffold) ─┐
                ├─→ #13 (models) ─→ #14 (service) ─┬─→ #15 (reconciliation)
                │                                   └─→ #16 (router + WS) ──┐
                │                                                            │
                └────────────────────── #17 (frontend primitives) ───────────┤
                                         (can start in parallel)             │
                                                                             │
                                   #18 (list + submit)  ◄─────────────────────
                                   #19 (detail + editor) ◄────────────────────
                                   (both need #16 API + #17 primitives)
```

**Critical path:** #12 → #13 → #14 → #16 → (#18, #19).

**Parallel opportunities:**
- **#15 and #16** are both blocked only by #14 and don't touch each other — run in parallel.
- **#17** depends on the API contract but not the API implementation; stub the fetch types and run alongside #12–#16.
- **#18 and #19** both need #16 + #17 and don't touch each other — run in parallel.

## Test Requirements Rolled Up

Each issue includes explicit test requirements. Highlights of what must be covered:

### Backend determinism

- `FakeRunner` for `ResearchRunner` protocol — synchronous, deterministic, no network (#14, #16)
- Compare-and-set coverage: two concurrent regenerates → exactly one result commits (#14 integration)
- Reconciliation: stale vs fresh `researching` rows treated correctly (#15)
- Immutability: `PUT /queue/{id}` on `approved` → 409 (#16)
- Brain write: exactly once per item; second approve attempt → 409 (#14)

### Frontend correctness

- **XSS:** DraftEditor preview strips `<script>` tags and attribute injections from LLM-generated markdown (#19 — mandatory)
- **ENUM_VALUE:** DraftStatus string literals mirror backend values exactly (#17)
- **WS contract:** on simulated reconnect, client refetches `["queue"]` (finding #6 test) (#17)
- **Approved immutability:** editor goes read-only when status is `approved` (#19)
- **Override gating:** flagged-draft approve requires confirmation dialog (#19)

## Risk Flags by Issue

| Issue | Risk | Mitigation in acceptance |
|---|---|---|
| #13 | ENUM_VALUE | serialization test asserts lowercase string values |
| #14 | COMPONENT_API (gpt-researcher internals) | acceptance requires verifying `get_research_sources()` shape before use |
| #14 | LR-001 (custom exceptions) | acceptance requires `InvalidStateTransition`, `NotFoundError` classes |
| #17 | ENUM_VALUE (TS mirror), COMPONENT_API | tests lock literal values + prohibit researcher-UI imports |
| #19 | XSS | DOMPurify-or-equivalent sanitization + a dedicated XSS test |

## Recommended Implementation Order

**Wave 1 — Backend foundation (sequential):**
1. #12 scaffold
2. #13 models/schemas/mappers
3. #14 service + runner protocol

**Wave 2 — Backend parallel:**
- #15 reconciliation
- #16 router + WS

**Wave 3 — Frontend (can start alongside Wave 1 with stubbed types):**
- #17 primitives + hooks
- (Wait for #16 merge, then:) #18 list + submit AND #19 detail + editor in parallel

## Orchestration Notes

- Issues #12, #13, #15, #17 are `complexity:simple` — candidates for `/orchestrate` SIMPLE tier.
- Issues #14, #16, #18, #19 are `complexity:complex` — should get Codex adversarial review post-PROVE per `implementation-routing.md`.
- Fullstack issues (#14 touches gpt_researcher internals; #16 exposes API that #17–#19 consume) — verify ENUM_VALUE alignment between #13 and #17 before merging either.

## Open Concerns (for implementation-time, not blocking)

- **TanStack Query dependency** — #17 assumes it's used. First implementer should verify `frontend/nextjs/package.json` and add `@tanstack/react-query` if missing.
- **gpt-researcher API surface** — `GPTResearcher.get_research_sources()` shape is relied upon by #14. Verify against actual source before implementing the runner.
- **websocket_manager.py API** — #16 extends this module; #14 may need to call `broadcast_queue_update`. Implementer of #14 should stub this call so #16 can wire it up without a circular blocker.

## Next Step

Start with `/orchestrate 12` for the backend foundation. When #14 lands, `#15` and `#16` can run in parallel. Frontend primitives (#17) can start immediately in a separate tab with stubbed API types.

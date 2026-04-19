# content-brain — 6-Phase Build Plan

Fork of `assafelovic/gpt-researcher` adapted into a personal research → knowledge → publishing pipeline.

**Upstream remote:** `assafelovic/gpt-researcher` (for rebasing)
**Origin:** `jwj2002/gpt-researcher`
**Local dir:** `~/projects/content-brain`

---

## Architecture

```
                       ┌──────────────────┐
     Topic  ─────────▶ │   Research Loop  │  (forked gpt-researcher)
                       └────────┬─────────┘
                                │  synthesized markdown + sources
                                ▼
                   ~/basic-memory/research/*.md      ◀── Basic Memory MCP
                                │                  (queryable via Claude)
                                ▼
                     ┌─────────────────────┐
                     │  Approval Queue     │  (FastAPI + React)
                     │  draft → approve    │
                     └──────────┬──────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              LinkedIn       Blog      Personal site
              (Typefully)  (git push)  (git push)
```

---

## Phase 1 — Foundation ✅ DONE (2026-04-19)

Shipped as PR #4, squashed into `main` at `f30db1ed`. Closed issues #1, #2, #3.

**What shipped:**
- Removed `multi_agents_ag2/` and `mcp-server/` (sprawl)
- Pinned `langchain-anthropic>=1.4` and `langchain-huggingface` + `sentence-transformers` in `requirements.txt`
- This `PLAN.md`
- First baseline research artifact at `docs/baseline/mcp-adoption-2026.md`

**Learnings that change downstream phases:**

1. **`claude-opus-4-7` rejects the `temperature` parameter** — extended-thinking models don't accept it. All three LLM tiers currently use `claude-sonnet-4-6`. Revisit in Phase 6 with per-model overrides or the thinking-mode API.
2. **Voyage AI free tier is theater** — 3 RPM without a payment method, unusable for agent workflows. Using local HuggingFace `all-MiniLM-L6-v2`. Voyage can return when a card is added (200M tokens/mo become genuinely free then).
3. **gpt-researcher silently hallucinates on empty retrieval** — if sub-query search returns 0 real sources, the writer stage fabricates citations from training data. Guard added to Phase 3 acceptance.

**Files to open first (still useful for later phases):**
- `gpt_researcher/agent.py` — the orchestrator
- `gpt_researcher/prompts.py` — where quality lives
- `gpt_researcher/config/variables/default.py` — the knobs
- `backend/server/server.py` — the API surface
- `gpt_researcher/skills/researcher.py` — search + scrape pipeline

---

## Phase 2 — Knowledge Store (code DONE; user setup pending)

**Goal:** Every research output persists to a queryable second brain.

**Code shipped (this PR):**
- `brain/` package writes every CLI research run to `~/basic-memory/research/{YYYY-MM-DD}-{slug}.md` with YAML frontmatter (topic, date, sources, source_count, config snapshot).
- `BRAIN_PATH` env var overrides the default. Default `~/basic-memory/` aligns with Basic Memory's convention so no extra config is needed.
- Brain save is non-fatal — a failed write prints a warning but doesn't break the CLI.

**User machine setup (your part):**
Follow `docs/BASIC_MEMORY_SETUP.md`:
1. `uv tool install basic-memory`
2. `basic-memory sync --watch &`
3. Edit `~/Library/Application Support/Claude/claude_desktop_config.json` to register the MCP server
4. Restart Claude Desktop
5. Ask Claude: "What have I researched about X?"

**Exit criteria:**
- [x] Research auto-stores to `~/basic-memory/research/`
- [ ] Claude Desktop can search + cite notes from the vault *(user setup step)*
- [x] Frontmatter includes sources so you can trace back

**Notes:**
- Plain markdown on disk — grep, open in any editor, move anywhere
- Basic Memory rebuilds its index from source files anytime (zero lock-in)
- The brain is *indexer-agnostic* — works with Obsidian, Logseq, etc. too

---

## Phase 3 — Approval Queue

**Goal:** Research runs → draft lands in a review UI → you approve/edit/reject.

**Tasks:**
- Add `queue` module to the backend (alongside `backend/server/`)
- SQLite table — `(id, topic, style, status, draft_md, sources_json, source_count, destination, created_at, scheduled_for)`
- Statuses: `pending → researching → draft_ready → flagged → approved → published`
  - `flagged` = draft produced but `source_count` below threshold (likely hallucinated)
- Routes: `POST /queue` (submit topic), `GET /queue` (list), `GET /queue/{id}`, `PUT /queue/{id}`, `POST /queue/{id}/approve`
- React page — list view, detail/edit view, regenerate button, approve button, **hallucination warning banner on flagged drafts**
- Wire "submit" → kicks off research asynchronously → updates status when done

**Hallucination guard (new — from Phase 1 learning):**
- After research completes, count unique real URLs cited in the draft
- If `source_count < 3` (tunable), set status to `flagged` instead of `draft_ready`
- Approve button for `flagged` drafts requires an explicit override ("I verified the content manually")
- Store the sources JSON with the draft so the reviewer can spot-check provenance in the UI

**Exit criteria:**
- [ ] Submit a topic from the UI, see it move through statuses
- [ ] Edit a draft inline, save changes
- [ ] Drafts with <3 real sources land as `flagged`, not `draft_ready`
- [ ] Flagged drafts show a visible warning in the UI with linked sources
- [ ] Approve a draft (for now, approval just sets status — publishing comes in Phase 5)

**Decision needed before starting:**
- SQLite or Postgres? → Start SQLite, upgrade later if needed.
- React or reuse the existing Next.js frontend? → Reuse if possible (less code), new if the queue UI doesn't fit.
- Minimum source count threshold (suggested: 3).

---

## Phase 4 — Style Presets

**Goal:** Same topic → different post formats per destination.

**Tasks:**
- Create `gpt_researcher/styles/` module
- Write 3–5 preset prompt templates (each is a complete rewrite of `generate_report_prompt`):
  - `linkedin_data_driven` — 200 words, hook + 3 data points + CTA
  - `linkedin_contrarian` — 180 words, strong claim + evidence + reframe
  - `blog_deep_dive` — 1500 words, essay with subheadings
  - `executive_brief` — 180 words, headline + 3 bullets + implication
  - `brain_note` — long-form, for future-you, no audience
- Add `style` field to queue — chosen at submit time
- Queue routes accept `style` param, pass through to research call
- Regenerate button supports changing style without new research (reuse context, re-render)

**Exit criteria:**
- [ ] Same topic, 3+ distinct outputs, each matches its destination's shape
- [ ] Style selector in the submit UI
- [ ] Regenerate-with-different-style works without re-searching

**Decision needed:**
- Write these styles before you start. Draft 3 example posts by hand in each style — if you can't, the style isn't real yet.

---

## Phase 5 — Publishing Adapters

**Goal:** One click from approved draft to live post on any destination.

**Tasks:**
- Adapter interface — `publish(draft) -> {url, published_at}`
- **LinkedIn adapter** — Typefully API (~$15/mo) — one HTTP call. Do NOT try to hit LinkedIn directly.
- **Blog adapter** — writes markdown to your static site's `content/posts/` dir in a separate git repo, commits, pushes. CI deploys.
- **Personal site adapter** — same pattern as blog, different repo.
- **Second-brain-only adapter** — no-op (report already stored in Phase 2).
- Queue stores `destination` on the draft. Approve button routes to the right adapter.

**Exit criteria:**
- [ ] Approve → publish → live link returned within seconds
- [ ] Works for all three destinations
- [ ] Failed publishes are recoverable (status reverts, error shown)

**Decisions needed before starting:**
- Which blog engine? (Astro, Hugo, Ghost, Jekyll, custom?) — determines adapter shape
- Typefully account created? — prerequisite
- Personal site repo URL? — needed for adapter config

---

## Phase 6 — Quality Upgrade + Chat

**Goal:** Make the output genuinely good. Optional chat UI over the brain.

**Tasks (pick from these as needed):**

**Reliability debt from Phase 1:**
- Per-model temperature handling — thinking models (e.g. `claude-opus-4-7`) need `temperature` *omitted entirely*, not set to any value. Add a wrapper or patch `gpt_researcher/utils/llm.py` to detect model family and strip the param when appropriate. Restores opus for `STRATEGIC_LLM`.
- Hallucination regression test — when retrieval returns 0 real sources, the writer should refuse to produce a report rather than fall back to training data. Add as a failing test first, then fix. Consider PR upstream if the fix lands cleanly.

**Quality:**
- Port STORM's multi-persona conversation as a new `skills/personas.py` module
  - Generate 3–5 personas per topic (expert, contrarian, novice, practitioner)
  - Simulate writer↔expert turns grounded in retrieved sources
  - Feed transcript into final draft
- Voice-locking — few-shot layer using your best past posts as style exemplars (store in `~/basic-memory/voice/`)
- Regenerate-with-notes — text box for critique, second-pass rewrite incorporating feedback
- Prompt evals — pytest suite that runs known topics through the pipeline, scores output against `docs/baseline/` (Phase 1 baseline is the first reference point)

**Chat (optional):**
- Already works for free via Claude Desktop + Basic Memory MCP — use that first
- Build dedicated chat UI only if Claude Desktop falls short

**Exit criteria (quality):**
- [ ] Outputs are distinctly better than Phase 4 baseline (subjectively and by eval scores)
- [ ] Voice matches your past writing on blind A/B comparison

---

## What We're NOT Building

- Own scheduler — Typefully / Buffer handle this
- Trending-topics crawler — you provide the topics
- Mobile app
- Agent framework (LangGraph, CrewAI) unless a Phase 6 problem demands it
- Custom vector DB — Basic Memory and its FastEmbed index are enough at personal scale

## Rebase Strategy

Upstream is active — rebase periodically to absorb research-engine improvements:

```bash
git fetch upstream
git rebase upstream/main         # upstream's default branch
# Resolve conflicts in prompts.py and agent.py (highest-change files)
```

Before each rebase, ensure your customizations live in clearly-separated files (`gpt_researcher/styles/`, `queue/`, etc.) to minimize conflicts.

## Open Decisions

Track these as they come up:

- [ ] Blog engine (Phase 5 blocker)
- [ ] Personal site framework (Phase 5 blocker)
- [ ] SQLite vs Postgres for queue (Phase 3 — default SQLite)
- [ ] React frontend approach — reuse gpt-researcher's Next.js app or add new (Phase 3)
- [ ] First 3–5 styles content (Phase 4 blocker — decide before writing prompts)
- [ ] Typefully vs Buffer (Phase 5)
- [ ] Minimum source count for draft approval (Phase 3 — suggested: 3)
- [ ] Voyage embeddings — pay for the card + get 200M free tokens, or stay on local HuggingFace (any phase)
- [ ] PR hallucination-refusal fix upstream to gpt-researcher, or keep as local patch (Phase 6)

## Sequencing Discipline

Ship each phase before starting the next. If Phase 2 alone turns out to be enough tool for your actual needs, that's a valid stopping point. Don't build Phase 5 before Phase 3 is working daily.

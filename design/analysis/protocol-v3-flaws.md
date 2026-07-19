# Protocol-v3 flaw analysis — loopy-loop / team-harness / eval-banana (2026-07-18)

Status: analysis. Companion designs:
- `design/designs/simplification-north-star.md`
- `design/designs/single-goal-assignments.md`
- `design/designs/session-layout-and-ids.md`
- `design/designs/context-and-eval-economy.md`

Scope note on versions: this checkout (`loopy_loop_1`) is at 0.6.0. The system
actually running in UGM (`ultimate_memory/ugm`) is the protocol-v3 engine
(0.8.0, `loopy_loop_4`, PR #80, 2026-07-17). All runtime evidence below is from
the UGM 2026-07-17/18 run: parent session `20260717_182101_7dc9a9a9e501_08630977`,
child `20260717_190245_5c2e83ae6415_71b7c8b0` (27 iterations). Team-harness
evidence is from `team_harness_2/team-harness` (HEAD 0ae608f); eval-banana from
`eval_banana_2/eval-banana` (0.3.6).

The unifying observation: between 0.6.0 (Jul 14) and 0.8.0 (Jul 17) the system
inverted its own founding idea. The founding idea (decisions D1/D5/D8/D9) is
"delegate high-level outcomes to capable harnesses; guide lightly; detect
failures, don't prevent them." Protocol v3 instead grew a compliance layer —
typed envelopes, sealed manifests, capability rosters, receipt families, hash
pinning, mirror trace trees — that now dominates what agents read, write, and
spend tokens on. Each piece answers a real-but-rare failure; the sum is a
system that briefs a frontier model like a bureaucracy briefs a contractor.

Measured summary of one child loop (26 iterations, Codex analysis, verified):
~27.95M coordinator prompt tokens vs ~185k completion tokens (151:1); eval
iterations 8.59M (31%); per-iteration harness runs of 20–37 turns growing from
~20k to ~96k input tokens per turn.

---

## A. Context economics

### A1. Team-harness re-sends an ever-growing conversation, uncached, with dormant compaction
The coordinator loop keeps one `messages` list, appends every assistant/tool
message in full, and re-sends the entire list plus all tool schemas every turn
(`coordinator/loop.py:119-129`, `173-214`). There is no turn cap. Auto-compaction
exists but cannot fire in practice:
- Threshold is `model_limit − ~33k` → ≈967k tokens for gpt-5.5/OpenRouter,
  ≈1.467M for gpt-5.6-sol (`tracking/context.py:147-149`) — runs peak ~96k.
- It only triggers when the last message is `user`-role (`loop.py:247`), which
  in an SDK run happens exactly once (the initial task).
- Manual `/compact` is REPL-only; loopy-loop uses the SDK path.
No `cache_control` / prompt-caching markers are ever sent, so the re-sent
prefix is billed fresh every turn. Cost per run is therefore quadratic in
turns: 20k + 25k + … + 96k, per iteration, for every iteration.

### A2. The rendered iteration prompt is 28–38 KB, ~70% ceremony
Measured on `iterations/0026_outer/prompt.txt` (38,527 bytes):
- goal + criteria restatement: ~4.9 KB
- attempt contract: ~0.7 KB
- **53 absolute paths: ~8.7 KB** — every path repeats the same 140-char
  session prefix
- **frozen workflow/scheduler/capability JSON dump: ~18 KB** pretty-printed
- workflow body: ~6.2 KB
This is the ~20k-token floor of every harness turn (A1 multiplies it by
20–37). The 0.6.0 render was ~30 lines of header; v3 grew it ~10×.

### A3. Large agent reports accumulate in coordinator context
`read_agent_output(tail_bytes=8192)` has a caller-controlled, **unbounded**
`tail_bytes` (`tools/agent_tools.py:1666-1679`); the observed 20–32 KB tool
results enter context this way and are then re-sent every remaining turn (A1).
`read_new_agent_output` is capped at 64 KB — also large as a context payload.
`run.json` additionally persists every turn's full message delta and every
tool result verbatim (`tracking/models.py:18-25`), so traces balloon too.

### A4. No worker-session reuse across iterations
Team-harness fully supports resuming worker sessions (`spawn_agent` +
`resume_from_session_id`, per-provider capability map, `worker_sessions.json`
manifest with provider session ids). Loopy-loop never surfaces the previous
iteration's manifest to the next iteration, and no prompt suggests resuming.
Every iteration's agents re-derive all context from files. (Fresh *coordinator*
per iteration is correct — D1 continuity-in-files — but worker continuations
of genuinely continuous work, e.g. "apply the reviewer's fixes", are being
re-briefed from zero.)

### A5. Eval is structurally expensive, and its biggest cost is accidental
- eval-banana stores each judge's **full stream-json stdout twice** per check
  (`stdout` and `details.raw_response`); UGM `report.json` files are
  **1.5–3.7 MB** (Σ 10.65 MB over 6 reports) while the actually-useful `reason`
  fields total ~4.4 KB. The eval_runner prompt tells the agent to read
  `report.json` → a single ~1M-token tool result. `report.md` (5–6 KB) has the
  same verdicts.
- The stock eval_reviewer prompt **forbids deterministic checks** ("even for
  objective file, structure, command, or data assertions"), so UGM has 83/83
  harness_judge checks and 0 scripts. Every check is a full autonomous Opus
  4.8 high-effort agent run (up to 300 s), executed **serially**. Judge
  subprocess tokens are billed on top of, and invisible to, the 8.59M metered
  eval figure.
- Cadence: reviewer on start + reviewer/runner every 10 inner successes → 7 of
  27 child iterations were eval; one runner concluded a "false advisory
  observation" after 7 failed judges and 1 timeout — maximum spend, no signal.

### A6. Mandated multi-agent recipes (0.6.0 templates)
The 0.6.0 inner/outer prompts hard-code an agentic-team recipe (~90 lines,
duplicated in both): fixed model per step ("using CODEX", "WITH GEMINI!"), the
same plan review mandated **three times** with prescribed models, ~7×
"think ultra deeply". This fights the config-level `model_tiers` abstraction
and multiplies spawns (5–7 agents some iterations). Protocol v3 softened this
("Dynamic delegation is optional… a preference, not a quota") — that
direction is right and should be kept and extended.

---

## B. Assignment and instruction complexity

### B1. The assignment envelope grew from one field to seven
v1 `ChildSessionRequest` was `{workflow_set, goal, schema_version}` — the goal
was one self-contained text. v2 (deployed in UGM) is a typed envelope:
`goal` + `completion_criteria[]` + `stop_criteria[]` + `constraints[]` +
`deliverables[]` + `required_evidence[]` + sha256-pinned `inputs[]`, plus an
immutable dispatch snapshot, ordered atomic renames, and a ledger link. In the
real Phase-0 request (6,140 bytes) the same fact appears in ≥3 grammatical
shapes (completion_criteria[1] ≈ deliverables[1] ≈ required_evidence[3]) and
the assignment is materialized in ≥4 places (task file, dispatch_inputs
snapshot, pending/accepted request, child `goal.md`). Structure invites
restatement; restatement invites drift; none of it makes the child smarter
than one well-written goal paragraph would.

### B2. The goal file describes the looping system
UGM `loopy_loop_goal.txt` is 6,212 bytes; most of it is loop mechanics
("Program organization … This is a double loop", "Delivery contract",
"Evaluation as evidence", recovery policy). The product outcome is one line.
Loop mechanics belong in the workflow-set role prompts (where they are *also*
already stated), not in the frozen goal that every layer re-reads. The goal
should be the one text a product owner would write.

### B3. Prompts are protocol manuals
0.6.0: outer 344 lines / inner 225, ~80–85% prescriptive (24 "do not"s and 28
"only"s in outer alone), with heavy verbatim redundancy across files (PR
policy 3×, atomic-write warning 4×, goal-source caveat 4×). Framework-to-task
text ratio per iteration ≈ 99:1. v3 prompts are shorter (dispatcher 96,
planner 138, outer 125) but nearly 100% protocol: envelope reading order,
path-name enumerations, hash computation, rename ordering, receipt taxonomy.
The dispatcher — whose *job* is writing one good goal — gets ~5 lines about
goal quality and ~80 about publication mechanics. Genuine judgment guidance
("preserve planner intent", "coherent outcome with observable criteria") is a
minority in every prompt.

### B4. Ceremony without demonstrated rent
Artifacts present per session in UGM: `goal_contract.json`,
`workflow_contract.yaml`, `workflow_roster.json`, `harness_capability_roster.json`
(duplicated as an 18 KB dump into every prompt — A2), `trace_seals/`,
`protocol_failures/`, `delivery_receipts/`, `eval_receipts/`, `git_receipts/`
(54 files for 27 iterations), `parent_acceptance/`, `child_outcomes/`,
`trace_finalization_outbox/`, per-attempt `workflow_snapshot/` (7 files each).
Each mechanism guards against a failure that mostly hasn't been observed;
collectively they define what agents must read, produce, and keep consistent.
Agents comply — e.g. a 146-line prose "Dispatcher publication report" narrating
one file publication.

---

## C. Reviewability

### C1. Engine-generated IDs are opaque; agent-chosen names are fine
Session id = `YYYYMMDD_HHMMSS_<goalhash12>_<random8>` — two meaningless hex
blobs; workflow set, role, and purpose absent. Attempt ids are bare 12-hex.
Receipts are keyed by hash (`git-after-97521a5ed6b7.json`,
`eval-483e152e5082.report.md`) — unopenable without cross-referencing. Trace
paths stack two hashes before anything readable
(`attempts/97521a5ed6b7/harness/20260718_064207_7f654370/…`). Contrast the
parts agents named: `0025_eval_runner`, `P0-L08-WP03-COMPONENT-VERSION-REGISTRY`,
`0010-accept-l07-select-l08-….md` — instantly navigable. The convention that
works is already in the tree; the engine just doesn't use it.

### C2. The traces split put durable evidence in the prunable half
`traces/` was meant to be the raw, independently-prunable plane. In practice
**100 LLM-authored markdown documents** (implementation reports, adversarial
reviews, acceptance audits, planning notes) exist *only* under `traces/`, and
the durable `handoff.json`/`plan.md` cite them as evidence via opaque refs
(`trace:trace-97521a5ed6b7:/harness/…/acceptance/l07-audit.md`). Pruning
traces per the runbook's stated policy would sever the justification chain for
every accepted outcome. The mirror tree (`traces/<root>/sessions/<child>/…`)
also duplicates the session hierarchy under a second set of hash keys — and
already produced a data-integrity bug: a malformed sibling root dir keyed by a
*truncated* goal hash (`…7dc9a9e501…`, 10 hex, beside the correct 12-hex one)
holding a stranded subset of trace docs.

### C3. Plans are provenance dumps, not orientation documents
Child `plan.md` is 16.8 KB at revision 10: outcome table, ASCII DAG, and long
"Accepted delivery" paragraphs inlining commit SHAs, PR numbers, and CI run
ids. `handoff.json` is 12.2 KB. The complaint "plans are not descriptive
enough" is precise: they are *verbose* but not *orienting* — a context-free
successor must dig through receipt-grade provenance to answer "what is this
loop doing, what state is it in, what's next, and why." Provenance belongs in
receipts/ledgers; the plan should be the document you'd want on day one of a
handover.

### C4. Session dirs are too wide to eyeball
~25 top-level entries per session dir; `state.json` is 28 KB; the same
information (assignment, goal, criteria) appears at multiple depths. Reviewing
"what happened in iteration 25" requires opening JSON files whose names don't
say what they contain.

### C5. Repo/version sprawl
Four loopy-loop checkouts at 0.5.0–0.8.0 (`loopy_loop_1..4`); docs in this
checkout describe a layout (no traces plane, 3-field child request) that the
deployed engine no longer has; the dev venv here pins team_harness 0.1.2
against a declared ≥0.4.0 floor. Any analysis or agent working from the wrong
checkout reasons about a retired protocol.

---

## D. Flow and behavior

### D1. Work is sliced too fine for the per-iteration overhead
Every leaf costs: outer selection iteration + inner implementation iteration +
outer acceptance iteration, each a full 20–37-turn harness run with the A1/A2
overhead, plus eval every ~6–8 iterations. Narrow reconciliation/verification
leaves make complete loop cycles out of work a single agent session would
finish incidentally. The overhead per iteration is currently so high that
granularity is an economic decision, and nothing in the prompts says so.

### D2. Eval is a scheduled ritual, not a milestone gate
Cadence is mechanical (`run_after_successes: {inner, every: 10}` + on-start).
Eval runs regardless of whether anything integration-shaped happened, and its
results are advisory (v3 moved completion authority to the orchestrator —
correct), which makes the current spend/utility ratio worse: 31% of tokens for
advice the orchestrator may not need at that moment.

### D3. Templates ship with empty criteria
Both stock configs omit `completion_criteria`/`stop_criteria` (defaults `[]`),
so the rendered header advertises empty sections and the only task-specific
content in a 0.6.0 iteration prompt is the one-line goal.

---

## What v3 got right (keep)

- Completion authority with the layer orchestrator, eval advisory (0.8.0).
- Delegation freedom language ("preference, not a quota"; capability roster
  replacing hard-coded model names in prompts).
- Continuity in files + fresh coordinator per iteration (D1) — the Codex
  analysis confirms blanket coordinator-session reuse would *worsen* context
  growth.
- Crash-safe atomic publication of the few files that genuinely need it
  (control, child requests).
- The instinct to separate raw noise from semantic state (the execution was
  wrong — C2 — but the distinction matters).

## Priority map (impact × effort)

| # | Fix | Flaws addressed | Expected effect |
|---|-----|-----------------|-----------------|
| 1 | Prompt-cache coordinator requests; mid-run compaction trigger | A1 | ~an order of magnitude off billed input tokens, no behavior change |
| 2 | Iteration-prompt diet (paths file + roster by reference) | A2 | per-turn floor 20k → ~3k tokens |
| 3 | eval-banana: drop `raw_response` duplication; runner reads `report.md`; lift deterministic ban; milestone cadence | A5, D2 | eval share 31% → single digits |
| 4 | Single-goal assignment (v3.1 envelope = goal text) | B1, B3 | dispatcher writes briefs, not envelopes |
| 5 | Fold traces into session dir; durable home for agent-authored docs | C2 | evidence chain survives pruning; one tree |
| 6 | Readable IDs (slug + role + iteration) | C1 | navigable sessions |
| 7 | Orientation-first plan/handoff contract | C3 | cold-start successors |
| 8 | Worker-session reuse for continuations; result cards | A3, A4 | less re-briefing, smaller contexts |
| 9 | Work-package granularity guidance | D1 | fewer, fuller iterations |

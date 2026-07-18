# Context and eval economy

Status: proposed. Grounding: `design/analysis/protocol-v3-flaws.md` §A1–A5, §D1–D2.
Principles applied: P1, P6, P7. Companion: `single-goal-assignments.md` §3
(prompt diet) removes the per-turn floor this document doesn't cover.

Measured baseline (UGM child loop): 27.95M prompt / 185k completion tokens
across 26 iterations; runs of 20–37 coordinator turns re-sending 20k→96k
tokens per turn, uncached; eval at 31% of metered spend plus unmetered judge
subprocesses. Two families of fix: make the re-sent context cheap and small
(team-harness), and stop generating context nobody needs (loopy-loop +
eval-banana).

## A. Team-harness

### A1. Prompt caching (largest single lever, zero behavior change)
The coordinator re-sends an append-only prefix every turn with no
`cache_control` markers. Add provider-appropriate caching in
`coordinator/client.py`: cache breakpoints on the system prompt and on the
last-but-N message boundary for OpenAI-compatible/OpenRouter (and the codex
provider's equivalent). The conversation is append-only between turns, which
is the ideal caching shape; billed input for turns 2..N becomes mostly
cache-read. This converts the quadratic term's constant into a small one even
before any context shrinking.

### A2. Compaction that can actually fire
Today's trigger needs ~967k–1.467M current-context tokens and a `user`-role
last message — unreachable in SDK runs peaking at 96k. Change
(`coordinator/loop.py`, `tracking/context.py`):
- Add `compact_above_tokens` (Config knob; loopy-loop passes it, default
  ~80k): compact when `ctx.total` exceeds it, at any tool-result boundary,
  not only after `user` messages.
- Compaction target stays the existing "<10k tokens" summary prompt; preserve
  the initial task message verbatim (it carries the assignment).
The knob is a safety net, not the plan: with A1+A3 and the prompt diet, most
runs should finish without compacting.

### A3. Bound what enters the coordinator context
- `read_agent_output`: give `tail_bytes` a ceiling (`Config`, default 16 KB);
  larger requests get the tail plus a truncation banner naming the full log
  path (pattern already used by `read_new_agent_output`).
- Worker footer (`_build_direct_spawn_footer`) gains one sentence: *"End your
  stdout with a result card: ≤15 lines — outcome, key decisions, files
  changed, and absolute paths to any report you wrote. Write long reports to
  files, not stdout."* The coordinator reads cards by default and opens report
  files only when needed — the report then costs one read, not N re-sends.
- `run.json`: store tool-call results truncated to a few KB with a pointer to
  the worker log that already holds the full stream (dedupes A-side trace
  bloat; the full data exists on disk exactly once).

### A4. Worker-session reuse for genuine continuations
The capability exists end-to-end (spawn `resume_from_session_id`, per-provider
support map, `worker_sessions.json` manifest); nothing exposes it across loopy
iterations. Changes:
- loopy-loop passes the previous iteration's `worker_sessions.json` path in
  the rendered header's `paths.json`.
- One line in the shared workflow-set preamble: *"If your task continues a
  previous iteration's work (applying review fixes, re-verifying the same
  change), resume that worker's session via the manifest instead of
  re-briefing from zero. For new work, start fresh."*
Selective by prompt-level judgment (P1), not an engine mandate — the Codex
analysis is right that blanket reuse would grow context rather than save it.
Fresh coordinator per iteration stays (D1).

## B. Loopy-loop

### B1. Iteration granularity is an economic decision — say so
Add to outer/planner judgment guidance: *"Each iteration costs a full
coordinator run before any work happens. Prefer work packages you can carry
to done — implemented, reviewed, integrated — within one iteration; slice
only when a package genuinely exceeds one iteration's reach. Verification and
reconciliation ride along with the work they verify; they are rarely their own
iteration."* This addresses the observed pattern of narrow
reconciliation/verification leaves each costing a full cycle.

### B2. Default model posture
Keep uniformly-strong coordinators (D9) but make the stock tier guidance
explicit that `standard` is the default spawn tier for routine execution and
mechanical steps, with `strong`/`frontier` reserved for architecture,
adversarial review, and disagreement — the current guidance implies it;
prompts hard-coding frontier-everything (0.6.0 recipes) are deleted by
`single-goal-assignments.md` §4.

## C. Eval

### C1. Fix the accidental megabytes (eval-banana)
- Stop duplicating judge stdout: `report.json` keeps `details.raw_response`
  as a *path* to the already-persisted `checks/<stem>.stdout.txt`, and
  `stdout` holds only the tail needed for error display. Report sizes drop
  ~1.5–3.7 MB → tens of KB.
- eval_runner prompt: read `report.md` (5–6 KB, same verdicts + reasons);
  open per-check artifacts only for failures under investigation. Never read
  `report.json` wholesale.

### C2. Lift the deterministic-check ban (revisit D4)
D4's point — agents shouldn't grade themselves with self-authored assertions
that ossify implementation details — argued for judge-led evaluation, but the
stock reviewer prompt hardened it into "deterministic checks are forbidden
even for objective file, structure, command, or data assertions." Result:
83/83 checks are full Opus-agent investigations, many of which verify facts a
script checks for free (file exists, command exits 0, table present).
New reviewer guidance: *"Judge checks evaluate semantics and quality.
Objective, mechanically-verifiable facts belong in deterministic checks —
prefer invoking the repo's own test/lint suites over bespoke assertions.
Keep judge checks for what actually needs judgment."* (Extends existing
proposal P1.2 from `improvement-proposals.md` into the stock template.)
- Judge tier: default the eval harness to the `standard` tier; `strong` for
  final goal checks. Run judges in parallel (they are independent
  subprocesses; the serial for-loop is just latency).

### C3. Milestone-driven cadence instead of a ritual
Replace the stock `run_after_successes: {inner, every: 10}` schedule with
orchestrator-requested eval: the outer requests an eval iteration by writing
`project_state/eval_request.md` (one line: scope + why now) when it reaches a
milestone or intends to declare completion; a small scheduler predicate
(`run_when_requested: true`) makes eval_runner eligible only when a pending
request exists, and the terminal goal check remains mandatory before
`goal_met`. eval_reviewer runs on start and thereafter only when the goal
check's check-set is stale relative to the plan (outer requests it the same
way). Eval stays advisory (0.8.0 semantics — keep); it just stops firing on
iterations where nobody will act on it. Expected effect: eval share of spend
31% → single digits, with *better* timing relative to decisions.

## Sequencing

1. C1 (one-day fixes, huge effect) and A1 (caching).
2. Prompt diet (`single-goal-assignments.md` §3) + A3 result bounding/cards.
3. A2 compaction knob, A4 session-reuse plumbing + preamble line.
4. C2/C3 template + scheduler changes; B1/B2 prompt guidance.

## Acceptance criteria

- Re-run a UGM-scale child loop: total coordinator prompt tokens for a
  comparable 26-iteration program under 5M (from 27.95M), with billed
  (non-cache-read) input a fraction of that.
- No single tool result over 16 KB enters coordinator context uncompacted.
- `report.json` under 100 KB for a 10-check run.
- Eval iterations occur only on request or at terminal goal check; a program
  session's eval spend share reported by the usage ledger under 10%.
- A continued-work iteration (review-fix pattern) demonstrably resumes a
  worker session (visible in `worker_sessions.json` resume records).

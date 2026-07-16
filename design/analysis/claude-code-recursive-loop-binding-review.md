# Review: the recursive loop-layer binding contract (D10–D12)

**Status:** analysis / working notes; not binding, not a decision
**Date:** 2026-07-15
**Reviewer:** independent pass with Claude Code
**Subject:** [`design/designs/recursive-loop-layer-contract.md`](../designs/recursive-loop-layer-contract.md)
and decisions **D10, D11, D12** in [`design/decisions.md`](../decisions.md)
**Baseline:** `main` @ `a5f9933`, working tree as of this review; installed
team-harness **0.4.0**, eval-banana **0.3.1**; `uv run pytest src/tests` → **216 passed**

**Verdict: blocking issues remain.** Four of them (B1–B4). None challenges the
architecture — the recursive session node, the dynamic-delegation boundary, the
state/evidence-versus-trace split, and the layer-local eval rule are all sound and
compatible with D1–D9. The blockers are all in the *seams*: three are migration
gaps where the target contract cannot be reached from the current code without a
step the doc does not name, and one (B2) is a rule whose enforcement mechanism
does not exist and whose obvious implementation would violate D8.

I verified claims against code rather than trusting the companion analysis. Where
the design is accurate I say so, because several of its sharpest claims are
load-bearing and were worth confirming.

---

## What I verified as accurate (no action needed)

These are recorded so a future reader does not re-litigate them:

- **"child requests from a child are silently ignored."** Confirmed:
  `coordinator_app.py:1029-1030` returns early when `state.parent_session_id is not
  None`, before the request directory is even scanned. The request file is never
  read, rejected, logged, or evented. Phase 2's remedy (reject with a durable
  "nested dispatch is not supported by this runtime version" reason) is the right
  fix for a real defect.
- **D12's run-record claim.** Confirmed against the installed dependency:
  team-harness writes its canonical record to `RUNS_DIR/<run_id>/run.json`
  (`harness.py:125` + `tracking/run_log.py:33`) = `~/.team-harness/runs/...`,
  while `output_dir` becomes `session_output_dir` (`harness.py:442-449`) and never
  receives `run.json`. Loopy reads `<harness_output_root>/<run_id>/run.json`
  (`worker.py:329`, path from `harness_runner.py:252-253`); `recovery.py:155` reads
  the global one. The two halves genuinely disagree. See M1 for the consequence.
- **Both eval-banana defects.** `HarnessJudgeCheckDefinition` fields are exactly
  `['schema_version','id','type','description','tags','instructions','model']` with
  `extra='forbid'`; `eval_reviewer/prompt.txt:61` teaches `target_paths`, which
  therefore fails `extra_forbidden`. `--harness-agent` is a real
  `eval-banana run` flag that `eval_runner/prompt.txt:44` does not pass, and
  `cli.py` never creates `.eval-banana/config.toml`. Config discovery does walk
  upward (`eval_banana/config.py:125-128`), so the "judge from an ancestor
  directory" concern is real.
- **The prompt defects behind Phase 0 items 3 and 5.** `inner/prompt.txt:151-152`
  really does say `/_feature_planning` (filesystem root);
  `outer/prompt.txt:268` says "Do not implement planned tasks" while
  `outer/prompt.txt:~315` says "doing the actual plan execution using CODEX" plus
  branch/PR/merge; `outer/prompt.txt:292` and `inner/prompt.txt:153` offer a
  `questions.md` human-question channel that `outer:336` / `inner:225` then forbid
  — and which D5 forbids outright. `GITIGNORE_LINES = [".loopy_loop/sessions/"]`
  (`cli.py:73`) confirms the scratch/lock hygiene gap.
- **Both eval-authority claims.** `outer/prompt.txt:98-107` and
  `eval_runner/prompt.txt:64-65` both write terminal `goal_met`.
  `planner/prompt.txt:80-84` closes the PM root after reviewing child evidence,
  and neither PM workflow sets `emits_goal_check` — so there is genuinely no PM
  root eval.
- **The prompt/PM-goal claim.** `pm_planner_dispatcher/loopy_loop_goal.txt` is a
  mechanism description, verbatim as quoted.
- **Structural invariants match the code.** `_suspended_parent_response()`
  (`coordinator_app.py:375-405`) really does enforce "no session has both a live
  task and a live child"; child config inheritance
  (`coordinator_app.py:1100-1106`) really does copy `completion_criteria` /
  `stop_criteria` into a child with a different goal, which is what the design's
  "session-local goal contract" split fixes.
- **All eight "Primary code anchors" exist** and name real functions.

---

## Blocking

### B1. The terminal-control schema bump has no migration path, and today an unreadable control is terminal — not repairable

**Where:** design "Eval receipt" (*"A new terminal-control schema cites the
same-session eval receipt"*); D11 (*"A malformed terminal request is a repairable
protocol failure, not proof that the goal failed"*); Migration → Legacy
compatibility.

The design mandates a new `control.json` schema and asserts the engine will treat
a malformed terminal request as repairable. Neither is reachable from the current
code, and the migration never says to change it:

- `coordinator_app.py:1474-1477` — `_read_signal()` applies **one global, hard-coded
  gate**: any `schema_version != 1` on *any* signal returns `None`.
- `models.py:220-224` — `ControlSignal` additionally pins `schema_version == 1`.
- `coordinator_app.py:1355-1365` — `_apply_session_control()` maps a `None` signal
  to `state.status = "failed"`, `state.stop_reason = "invalid_control_output"`.
  That is a **terminal session failure**, not a repairable iteration.

Verified empirically — a v2 goal-met control citing an eval receipt, exactly as the
design specifies, reads as `None` under the current engine.

**Failure scenario.** A target repo's workflow set is migrated to the new contract
(templates are copied into the target at `loopy init`, so template version and
engine version drift independently). `eval_runner` produces a passing eval receipt
and writes the v2 `goal_met` control. The engine cannot parse it, marks the session
`failed` / `invalid_control_output`, and a **met goal is durably recorded as a
failure**. There is no repair path, because the session is already terminal — which
is precisely the outcome D11 promises cannot happen.

**Fix.**
1. Add a Phase 1 step before any template emits v2: make `_read_signal`'s version
   gate per-model instead of global, and parse control as a discriminated union on
   `schema_version` (v1 = today's shape; v2 adds the receipt citation).
2. Change `invalid_control_output` from a terminal `failed` to a **failed
   iteration** that leaves the session `running`, so the promised repair path
   exists. This is not unbounded: `_track_workflow_failure_cap()`
   (`coordinator_app.py:694-714`) already stops the session at
   `workflow_consecutive_failures_cap`, which is the correct D8-shaped bound —
   detection with a repair path, terminal only after repeated failure.
   If that semantic change is unwanted, D11's "repairable protocol failure"
   sentence must be struck instead; it cannot stay as written.
3. Add a Legacy-compatibility bullet: *the control reader change ships at least one
   release before any packaged template emits v2.*

### B2. `outer` "records readiness for layer evaluation" has no mechanism, and the obvious mechanism is a D8-forbidden eligibility gate

**Where:** design "Separate eval responsibilities" (*"`outer` accepts completed
tasks and records 'ready for layer evaluation'"*; *"`outer` does not close the
layer before eval runs"*); D11.

The design removes `outer`'s ability to close the layer and makes `eval_runner` the
sole `goal_met` owner. That is right in principle. But nothing converts outer's
readiness record into an eval run, and the timing consequence is severe.

`choose_next_workflow()` (`scheduler.py:7-53`) is **purely cadence-driven**: it reads
workflow configs and `HistoryEntry` only. It cannot observe a readiness artifact.
Tracing the stock configs:

- `eval_reviewer` (priority 100, `run_on_start: true`) wins iteration 1 —
  `is_run_on_start` bypasses `run_after_successes` (`scheduler.py:77-83`).
- `eval_runner` requires `must_follow: eval_reviewer`, `not_before_iteration: 10`,
  **and** `run_after_successes: {inner, every: 10}`.
- Cadence then alternates `outer`/`inner`, so inner's 10th success lands ~iteration
  21; `eval_reviewer` re-runs ~22; **the first `eval_runner` is ~iteration 23** of
  `max_turns: 160`, and subsequent ones roughly every 20 iterations.

So under the new rule, terminal `goal_met` is requestable at roughly **8 points in a
160-turn session**. A goal genuinely met at iteration 5 cannot close until ~23; one
met at 25 waits until ~43. Every intervening turn is real spend on a finished goal.

The tempting fix — let outer's readiness record make `eval_runner` eligible — is
exactly what **D8 forbids**: *"no eligibility gates keyed to semantic state"*, *"no
semantic scheduling vetoes"*. The companion analysis already warned against the
mirror-image version of this (*"Do not solve this by making the scheduler
semantically forbid an outer workflow from acting"*), but the design does not say
what the readiness record actually *does*.

**Fix.** State explicitly which of these the contract means, and add it to Phase 1
item 7:
- **Recommended (D8-safe):** the scheduler stays cadence-only. The readiness record
  is an *input rendered into `eval_runner`'s prompt*, never an eligibility input.
  To make the sole goal-control owner a usable gate rather than a ~20-iteration
  latency, retune the stock eval cadence at the same time — drop
  `eval_runner`'s `not_before_iteration: 10` and lower
  `run_after_successes.every` — so `eval_runner` runs often enough to close a
  finished session promptly. Cadence tuning is a config change, not a semantic
  gate, so it stays inside D8.
- If instead the design intends readiness to drive scheduling, it must say so and
  **amend D8**, per AGENTS.md Rule 1 — not contradict it silently.

### B3. `unresolvable_error` ownership is unspecified under the new terminal-control contract (D5 regression risk)

**Where:** design workflow-set contract (`eval: {goal_control_role: eval_runner}`);
"Separate eval responsibilities" (*"the terminal goal-control owner"*);
*"The one sanctioned `unresolvable_error` path remains available under D5."*

The contract names exactly one **terminal goal-control owner**, and the design says
`eval_runner` "is the sole role that may request terminal `goal_met` for that
session". It then says `unresolvable_error` "remains available" — but never says
*to whom*.

That ambiguity is dangerous in one specific direction. Today `unresolvable_error`
is instructed **only in `outer/prompt.txt` and `planner/prompt.txt`** — verified;
`eval_runner`'s prompt has no `unresolvable_error` path at all. A cold implementer
reading "eval_runner owns terminal control" can reasonably strip the escape hatch
from `outer`, leaving the only terminal writer a role that (per B2) runs roughly
every 20 iterations. A session that hits a genuinely terminal blocker at iteration
6 would then burn ~17 turns before it could report it — or never report it, since
`eval_runner` was never told how.

This directly undercuts D5's *"it must not silently guess, and it must not stall."*

**Fix.** One sentence in "Separate eval responsibilities" plus one contract field:

> `goal_met` is owned; `unresolvable_error` is not. Any role may write a
> terminal-blocker control at any time, and the workflow-set contract lists the
> roles instructed to do so.

```yaml
eval:
  author_role: eval_reviewer
  runner_role: eval_runner
  goal_control_role: eval_runner        # goal_met only
  terminal_blocker_roles: [outer, inner, eval_reviewer, eval_runner]
```

### B4. Moving child requests to `child_requests/pending/` can silently strand them across versions

**Where:** design "Physical layout" (`child_requests/{pending,accepted,rejected}/`);
Migration Phase 1 item 5; Legacy compatibility.

`_dispatch_child_session_if_requested()` scans
`sorted(requests_dir.glob("*.json"))` (`coordinator_app.py:1039`) — **non-recursive**,
directly in `child_requests/`. The dispatcher prompt writes to the rendered
`child_requests` directory (`dispatcher/prompt.txt:37`), which `_render_prompt`
renders as that same flat directory (`worker.py:403-404`).

Both skew directions break, silently:
- migrated template writes `child_requests/pending/req.json` → old engine's flat
  glob never sees it;
- migrated engine scans only `pending/` → an unmigrated template's
  `child_requests/req.json` is never seen.

Either way the request sits on disk, never dispatched, never rejected, never
evented — **the exact failure mode the design itself condemns** in Phase 2:
*"Leaving a valid-looking request unobserved would teach the child coordinator a
capability that does not exist."* The Legacy-compatibility section covers request
*schema* v1/v2 but not the *directory* move.

For contrast, the schema half already fails safely: a v2 request body under the
current engine returns `None` from `_read_signal` and lands in `_reject_request`
(`coordinator_app.py:1045-1047`) as `.json.rejected`. Verified. Noisy, but visible —
which is the right shape. The directory move has no such backstop.

**Fix.** Add a Legacy-compatibility bullet: *during migration the engine scans both
`child_requests/*.json` and `child_requests/pending/*.json`; the assignment's
rendered `child_requests` path stays authoritative for writers, and flips to
`pending/` only after the reader ships.* Also note that the filename-tombstone
idempotency in `_dispatched_request_files()` (`coordinator_app.py:1228-1245`) must
keep working until `request_id` idempotency replaces it — the design says
*"A request ID, not its filename, provides idempotency"* without sequencing the
handover.

---

## Moderate

### M1. The design blesses `long-running-loop-reliability.md` as truthful while D12's own context says otherwise

**Where:** design intro to the companion doc; `long-running-loop-reliability.md:14-19`
(*"this document remains the truthful description of current runtime behavior"*).

Chased to the end, the run-record split is worse than "cost accounting can remain
unknown" (D12's phrasing). Since `_read_harness_usage()` reads a path team-harness
0.4.0 never writes, it returns `None` for **every** iteration → every iteration
counts `iterations_without_usage` → `estimate_cost_usd()` returns `None` →
`_apply_stop_precedence`'s budget branch (`coordinator_app.py:1313-1324`) requires
`cost is not None` → **`max_cost_usd` never fires at all**. The 216 green tests do
not catch this because `test_events_and_usage.py:341-346` writes a synthetic
`run.json` at loopy's expected path.

Meanwhile the reliability doc still states *"`max_cost_usd` stops a session when its
subtree estimate reaches the configured budget"* and *"preflight rejects a budget
without prices"* as shipped behavior. The recursive design endorses that doc
wholesale in the same breath that D12 records the defect. A cold reader gets
contradictory answers depending on which binding doc they open.

**Fix (in the recursive design — do not weaken the reliability doc from here).**
Replace the blanket endorsement with: *"remains the description of current runtime
behavior except where D12 records a known integration defect: the harness
run-record/usage path, and therefore `max_cost_usd`, do not currently function
against installed team-harness 0.4.0."* Phase 0 item 1 should say plainly that the
budget is **inert today**, so it is sequenced as a correctness fix rather than a
metering polish. A follow-up edit to `long-running-loop-reliability.md` is warranted
but is out of this review's scope.

### M2. The physical layout omits recovery-critical artifacts, including files the same document references

**Where:** design "Physical layout".

The tree is presented as *the* layout but omits, among others: `parent.json` (which
Legacy compatibility explicitly relies on: *"readers derive root ID and depth by
following `parent.json`"*), `children.json` (which the assignment's
`children_index` points at), `events.jsonl`, `pending_finished_request.json`,
`harness_outputs/` (Legacy compatibility calls these "legacy traces"),
`updates_from_user.md`, and `eval_results/`.

`salvage.json` is the sharpest omission. D7 requires it so *"the provenance of
surviving edits [is] auditable rather than a mystery diff"*; `recovery.py:232-263`
writes it into the interrupted iteration directory. The design never places it in
any plane. An implementer following the tree literally could file it under
`traces/.../service/`, where **pruning is explicitly legal** — silently voiding a
D7 guarantee and violating the design's own rule that *"every correctness-critical
fact also exists as compact state/evidence or recovery state."*

**Fix.** Title the tree "new and changed artifacts" and add a short "unchanged,
still required" list. Explicitly assign `salvage.json` and
`pending_finished_request.json` to the **recovery journal** plane and state that
they are never prunable.

### M3. `eval_results/` is dropped from the assignment paths, and the design contradicts the stock prompt about where raw eval output goes

**Where:** design "Per-attempt assignment" `absolute_paths`; "Eval receipt"
(*"Raw reports are trace detail and may be pruned later"*).

The design's `absolute_paths` adds `children_index` and `children_root` but omits
`eval_results` — even though the companion analysis (line 324) listed it among
*"the repo root, `eval_results/`, `children.json`, and child sessions directory are
not rendered even though stock workflows rely on them"*. Two of the four were
picked up; this one was dropped.

It matters because it is also a live contradiction:
`eval_runner/prompt.txt:40-44` instructs the agent to create
`<session directory>/eval_results` and pass
`--output-dir <session directory>/eval_results`, while the design says raw eval
reports are trace-plane detail. `sessions.py` has no `eval_results` helper and
`_render_prompt` never renders it, so the agent is today constructing a
session path the engine does not know about.

**Fix.** Pick one and make both ends agree: either add `eval_results` to
`absolute_paths` + the layout as session-local evidence, or route
`--output-dir` at the per-attempt **trace** root (consistent with the receipt's
`raw_report_refs: ["trace:eval/report.json"]`) and say so in Phase 0 item 2, which
already touches this prompt.

### M4. The logical-reference grammar is defined once and used three incompatible ways

**Where:** design "Persist portable identity"; "Child outcome"; "Eval receipt".

Defined: `repo:/`, `session:/`, `parent:/`, `child:<session_id>/...`,
`trace:<trace_manifest_id>/...`.

Used:

| Example | Problem |
| --- | --- |
| `"handoff": "child:project_state/handoff.json"` | no `<session_id>` — unresolvable when a parent has several children |
| `"eval": "child:eval_receipts/eval-7.json"` | same |
| `"trace_ref": "trace:manifest-attempt-abc"` | manifest id, no path |
| `"raw_report_refs": ["trace:eval/report.json"]` | path, no manifest id |
| `"git_before_ref": "session:git_receipts/git-before-attempt-abc.json"` | consistent — this is the shape to standardize on |

Since portable references are the mechanism that makes a moved checkout work
(*"Persist only absolute paths"* is an explicitly rejected alternative), a cold
implementer cannot get this right from the doc.

**Fix.** State one grammar — `<scheme>:<id>/<relative-path>`, where `session:`,
`parent:`, and `repo:` are implicitly scoped to the receipt's own session and
therefore take no id — and correct all four examples. A resolver unit test belongs
in the Verification requirements list.

### M5. Tree-wide budget roll-up is required and tested, but implemented in no phase

**Where:** design "User updates, stop propagation, and resource accounting"
(*"the active leaf receives the ancestor-aware total needed to evaluate a root-tree
budget"*); Phase 2 test list (*"tree-wide usage/budget roll-up"*).

Today `_apply_stop_precedence` (`coordinator_app.py:1313-1315`) evaluates
`session_tree_usage_totals()` for the **active session only**, and the reliability
doc records the consequence as deliberate: *"the child cannot see prior
parent/sibling spend."* The design changes that requirement, and Phase 2 lists it
as a **test gate** — but no phase contains the implementation step. Phase 1's seven
items do not mention resource accounting.

A Phase 2 gate that tests behavior no phase built will either block the guard
removal indefinitely or get waved through.

**Fix.** Add an explicit Phase 1 item: *project a root-tree usage ledger that the
deepest active session can read, and define whether turn/cost limits are
per-session, whole-tree, or both.* (The existing roll-up in
`_mark_child_record_complete` → `session_tree_usage_totals` is already
exactly-once and recursive, so this is additive, not a rewrite.) Note the ordering
dependency on M1: the ledger is meaningless until usage discovery works.

---

## Low

### L1. `session.json` "v2" implies a v1 that does not exist
`create_session_dir` (`sessions.py:89-95`) writes `session.json` with **no
`schema_version` field**, and nothing under `src/loopy_loop/` reads the file back —
it is write-only today. State that *absent `schema_version` means v1* and name the
first intended reader; otherwise "session manifest v2" reads as if it were
amending a versioned predecessor.

### L2. The `RootConfigSnapshot` compatibility note is scoped too broadly
Legacy compatibility says *"a new coordinator must not unilaterally send assignment
fields that make an older released worker crash."* Verified: `TaskResponse` is
`extra='ignore'` (pydantic default), so new **top-level** response fields are safe;
only new **`config_snapshot`** fields break an old worker, because `extra='forbid'`
is on `RootConfigSnapshot` (`models.py:71`) and the worker re-validates it
(`worker.py:198-200`). Reword to "new `config_snapshot` fields" — as written it
discourages the safe half of the wire and under-warns about
`_COORDINATOR_ONLY_FIELDS` (`coordinator_app.py:65-76`), which is the real
mechanism protecting this.

### L3. "Session state remains gitignored by default as today" is not uniformly true
`inner_outer_eval/.gitignore` covers `.loopy_loop/sessions/`, `state.json`,
`state.json.lock`, and archives. `pm_planner_dispatcher/.gitignore` covers **only**
`.loopy_loop/sessions/`, and `_ensure_gitignore()` appends only
`.loopy_loop/sessions/` (`cli.py:73`) on an existing repo. Phase 0 item 3 already
covers the root state lock; extend it to the PM template's root `state.json` and
archives, and soften the layout sentence to "gitignored by the packaged templates,
unevenly today — see Phase 0 item 3."

---

## Cross-check against D1–D9

| Decision | Compatible? | Note |
| --- | --- | --- |
| D1 files/git are truth | Yes | D12 refines rather than replaces; the "no correctness fact behind a prunable reference" rule is the right guard. M2 threatens it only through omission. |
| D2 single worker | Yes | Invariants 1–4 restate it precisely; parallel harness agents inside one assignment stay allowed. Verified against `_suspended_parent_response()`. |
| D3 mechanical success | Yes | Untouched. Child-outcome-is-not-acceptance is the correct extension of the same principle. |
| D4 LLM-as-judge | Yes | Deterministic backstops stay set-owned; the stock rule is not loosened. |
| D5 autonomy / escape hatch | **At risk** | **B3** — terminal ownership ambiguity could strip the hatch from the only role that has it. Also: the design should note that Phase 0 item 5's prompt lint must remove the `questions.md` channel (`outer:292`, `inner:153`), which is a live D5 violation shipping in the stock template today. |
| D6 double loop from day one | Yes | Phase 2's gate list is a faithful expansion of D6's "harden the parent/child machinery first." |
| D7 process lifecycle | **At risk** | **M2** — `salvage.json` is unplaced and could land in a prunable plane. |
| D8 detection not prevention | **At risk** | **B2** — a readiness-driven eligibility gate would be a semantic scheduling gate. The receipt validation itself is *fine*: it gates acceptance, not attempt, and D11 says so explicitly. B1's fix (repairable, capped by the existing failure counter) is the D8-shaped form. |
| D9 uniform coordinators / tiers | Yes | Explicitly preserved; ownership metadata is correctly framed as audit evidence, not an allowlist. |

On the two distinctions the review was asked to probe:

- **Durable session coordinators vs. dynamically spawned harness agents** — this is
  the design's strongest section. The vocabulary table, the "a spawned agent does
  not become a child session merely because it is called 'subagent'" rule, and the
  "Fixed subagent graphs / Treat every spawned agent as a loop layer" rejections
  are clear, correct, and cold-readable. No action.
- **Absolute runtime paths vs. portable references** — the principle is right and
  the rejected-alternative entry is convincing. The execution has M4 (grammar) and
  M3 (a missing path) against it, not the idea.

---

## Cold-readability (AGENTS.md Rule 3)

Good: the vocabulary table, the three-depth topology example, and every
"Alternatives rejected" entry explain rather than name. D10–D12 each state the
conclusion tersely and hand off to a self-contained companion section, which is
exactly the shape Rule 3 asks for.

Weaker, in priority order: M4 (a reader cannot resolve the reference examples), M2
(the layout looks complete but is not), M3 (two binding statements disagree about
`eval_results/`), and B2 (the central new eval rule does not say what enforces it).

One structural note: the design says the analysis at
`design/analysis/loop-layer-state-and-trace-contract.md` is "evidence and working
history". That analysis proposed the next artifact be named
`design/designs/layered-session-and-trace-contract.md`; the accepted doc is
`recursive-loop-layer-contract.md`. Harmless, but a reader following the analysis's
closing pointer will look for a file that does not exist. Worth a one-line
correction in the analysis, or ignoring deliberately.

---

## Suggested minimal close-out

1. **B1** — sequence the control reader change ahead of any v2 emitter; decide
   whether `invalid_control_output` becomes repairable or D11's sentence is struck.
2. **B2** — say what the readiness record does; retune the stock eval cadence in
   the same change.
3. **B3** — one sentence + `terminal_blocker_roles` in the contract.
4. **B4** — one Legacy-compatibility bullet for the dual-directory scan window.
5. **M1–M5** — doc corrections plus one added Phase 1 item (M5); M3 needs a real
   decision, not just wording.

B1–B4 are all migration-sequencing or specification gaps. None requires
re-opening D10–D12's architecture, and none contradicts a decision that would need
amending under AGENTS.md Rule 1 — with the single exception of B2, where the
tempting implementation *would* require amending D8, and where the recommendation
is to stay inside D8 instead.

---

# Final re-review

**Date:** 2026-07-15 (second pass, after the fixes)
**Subject:** updated [`recursive-loop-layer-contract.md`](../designs/recursive-loop-layer-contract.md),
**D10–D12** in [`decisions.md`](../decisions.md), and the updated bindings in
[`long-running-loop-reliability.md`](../designs/long-running-loop-reliability.md)
and [`success-semantics-and-evaluation.md`](../designs/success-semantics-and-evaluation.md)
**Scope:** binding-document correctness only. Implementation is explicitly pending
and was not reviewed as code. The working tree changes only design docs
(`git diff --stat`: `decisions.md`, the two companion designs), so every code
anchor from the first pass still holds unchanged; I re-confirmed the load-bearing
ones (`_read_signal` version gate, `ControlSignal`, `_apply_session_control`,
`_transition_lock`, `config.py:332`) rather than assuming.

## Verdict

**No blocking design issue remains.** All four blockers (B1–B4), all five moderates
(M1–M5), and all three low findings (L1–L3) are resolved. The fixes introduce no
violation of D1–D9 and no contradiction I can construct a failure from.

Two things are worth saying plainly about *how* they were fixed, because both went
beyond the minimum:

- **B1 was fixed better than I proposed.** I offered two options — make
  `invalid_control_output` repairable for everything, or strike D11's sentence. The
  doc took a third: scope the repair path to v2 (archive, session stays `running`,
  protocol-failure receipt, bounded failure counter), leave legacy v1 semantics
  untouched, and sequence the reader ahead of every writer. That closes the failure
  scenario without a behavior change for sessions that never opted in, which is the
  safer trade.
- **M1 was fixed at both ends.** I scoped my recommendation to the recursive design
  and flagged the reliability-doc edit as out of scope. The reliability doc now
  carries its own "Known integration defect (team-harness 0.4.0)" block and no
  longer claims `max_cost_usd` fires as shipped. The contradiction a cold reader
  could hit is gone from the source, not papered over from a distance.

Four residuals follow. All are wording or placement, none blocks acceptance or
implementation, and R1 is the only one I would insist on before the test list is
handed to an implementer.

## Blocking

None.

## Residual findings, severity-ordered

### R1 (Moderate). The verification list re-introduces B3's D5 regression as a test gate

`recursive-loop-layer-contract.md:1073` reads:

> - only the declared goal-control role closes a new-schema session;

Read literally, that is false under the contract the same document now defines.
`outer`, `inner`, and `eval_reviewer` are all in `terminal_blocker_reporting_roles`
(line 393), and any of them publishing `unresolvable_error` *closes* the session —
terminally, by design, and without an eval receipt (lines 703–709).

This matters because the verification list is the hand-off artifact. An implementer
writing a test straight from that bullet would assert "no role other than
`eval_runner` produces a terminal transition," it would fail against the correct
implementation, and the natural way to make it pass is to strip the escape hatch
from `outer` — which is precisely the D5 regression B3 was raised to prevent. The
body of the design is unambiguous two sections earlier, so a careful implementer
would catch it; a test list should not depend on that.

**Fix.** One word: *only the declared goal-control role closes a new-schema session
**with `goal_met`***. Consider a companion bullet asserting the converse — that a
non-owner role's `unresolvable_error` still closes the session — since that is the
D5 property actually worth a test.

### R2 (Moderate). Three artifacts the fixes introduced have no home

The B1 and B2 fixes each created a new durable, agent-visible artifact. None is
placed in the physical layout or reachable through `absolute_paths`:

| Artifact | Introduced at | Placed? |
| --- | --- | --- |
| outer readiness record | lines 696–701 (*"rendered into subsequent workflow prompts"*) | no |
| rejected-control archive | line 772 | no |
| protocol-failure receipt | line 773 (*"the responsible workflow receives"*) | no |

This is the same class of gap M2 raised, freshly re-opened by the fixes for a
different reason: M2 was about omitting artifacts that already exist, this is about
inventing artifacts and not saying where they go. The layout is now explicitly
framed as covering "both unchanged recovery artifacts and new/changed target
artifacts" (line 233), so these three belong in it.

The consequence is concrete rather than cosmetic. The design bans exactly the
workaround an implementer would otherwise reach for: *"Agents must not infer session
paths from the current working directory or from ambiguous strings such as
`project_state/current_state.md`"* (lines 331–333). Without a declared path and an
assignment entry, "readiness is rendered into subsequent workflow prompts" has no
mechanism — which is the shape of the original B2 complaint, one level down. Note
`child_requests/{pending,accepted,rejected}/` is the right precedent: the rejected
half of a protocol got a named directory.

**Fix.** Add the three to the layout (a `control_rejected/` archive alongside
`control.json`; readiness and the protocol-failure receipt wherever the layer's
semantic evidence belongs), and add the readiness path to `absolute_paths` so the
rendering step in Phase 1 item 8 has something to render.

### R3 (Low). The control-repair guarantee is stated unqualified, then scoped in the next paragraph

Line 767: *"A malformed terminal request is a repairable protocol failure, not proof
that the goal failed."* Unqualified. The following paragraph scopes the repair to v2,
and Legacy compatibility (line 1050) says *"legacy v1 terminal records keep their
historical meaning"* — i.e. a malformed v1 control still lands on
`state.status = "failed"` / `invalid_control_output`, terminally
(`coordinator_app.py:1355-1365`, unchanged).

The scoping is correct and D11 now carries it precisely (*"malformed **new-schema**
control is archived and bounded"*). Only the design's topic sentence overreaches. A
cold reader who stops at line 767 gets the wrong answer about today's v1 sessions.

**Fix.** *"A malformed **new-schema** terminal request is a repairable protocol
failure…"*

### R4 (Low). Contract-schema nits in the B3 fix

Two small mismatches between the workflow-set contract example (lines 372–394) and
what D11 says the contract does:

1. **The task-acceptance owner is prose only.** D11 says every contract *"names the
   check author, check runner, task-acceptance owner, terminal `goal_met` control
   owner, and roles instructed to report a terminal blocker"* — five things. Four are
   structured fields; task acceptance exists only as English inside
   `roles.outer.responsibility`. Defensible, since the design says the contract
   *"explains responsibility to agents"* and *"is not a path permission list"* — but
   then D11's verb "names" is doing uneven work across the five.
2. **`terminal_blocker_reporting_roles` is nested under `eval:`**, though the design
   is explicit that the blocker path is *not* an eval concern: it *"does not require a
   passing eval receipt"* and is *"not owned exclusively by the `goal_met` control
   owner."* Filing it under the eval key invites exactly the conflation B3 warned
   about.

**Fix.** Lift `terminal_blocker_reporting_roles` to the contract's top level, and
either add an `acceptance_role` field or reword D11 to distinguish named fields from
described responsibilities.

### R5 (Low). No v2 control example, and the receipt citation must be conditional

The design gives a JSON example for every other new schema — `session.json`, the
assignment, `agent_assignment.json`, child request v2, child outcome, eval receipt —
but not for the v2 control record, which is the one schema whose reader/writer
sequencing got a dedicated Phase 1 item. The requirement is also stated
unconditionally (*"A new terminal-control schema cites the same-session eval
receipt"*, line 766) while `unresolvable_error` explicitly must not need one, so the
real schema needs the citation required only when `stop_reason == "goal_met"`. The
two statements are reconcilable and the intent is clear; the schema just isn't drawn.

### R6 (Low). The logical-reference resolver is tested but not built by any phase

Verification requires *"the logical-reference grammar rejects traversal and resolves
root, parent, self, and any named validated session consistently"* (lines 1066–1067),
but no Phase 1 item names building the resolver. It is strongly implied — Phase 1
item 2 cannot render `absolute_paths` and item 6's receipts cannot store logical refs
without one — so this is a much weaker instance of M5's shape (M5 tested a capability
no phase built at all; this one tests a capability every phase assumes). Worth a
clause in Phase 1 item 1 rather than its own item.

### Not re-raised

The pointer at `loop-layer-state-and-trace-contract.md:1552` still names
`design/designs/layered-session-and-trace-contract.md`, which does not exist. I
flagged this as ignorable last pass and it remains so — that file is non-binding
working notes and out of this review's scope.

## Cross-check against D1–D9

`git diff -- design/decisions.md` removes **zero** lines: D1–D9 are byte-identical,
and D10–D12 plus the two `**Refined by**` lines are pure additions. No decision was
silently amended, so AGENTS.md Rule 1 is satisfied by construction. The three rows I
marked "At risk" last pass all clear:

| Decision | First pass | Now | Why |
| --- | --- | --- | --- |
| D5 autonomy / escape hatch | **At risk** (B3) | Clear | `terminal_blocker_reporting_roles` (line 393) plus *"`unresolvable_error` … is not owned exclusively by the `goal_met` control owner"* (703–709); D11 carries the same rule; Phase 0 item 5 now names the `questions.md` gate for removal. Escape-hatch latency is zero — the B2 cadence question no longer touches D5. R1 is the only snag left, and it is in the test list, not the contract. |
| D7 process lifecycle | **At risk** (M2) | Clear | Lines 296–300 place `salvage.json` and `pending_finished_request.json` in the recovery-journal plane and state they are *"never prunable as trace detail."* The D7 provenance guarantee can no longer be voided by filing salvage under a prunable plane. |
| D8 detection not prevention | **At risk** (B2) | Clear | *"The outer readiness record is context, not scheduler eligibility … the scheduler remains cadence-driven and does not inspect semantic state"* (696–701), reinforced by Phase 1 item 8 and verification line 1076. The retune is a config change. B1's repair path is also D8-shaped: detect, archive, repair, bound with the existing failure counter — no eligibility gate anywhere. |
| D1, D2, D3, D4, D6, D9 | Yes | Yes | Unchanged; nothing in the fixes touches them. `success-semantics-and-evaluation.md` now cross-references D11 while explicitly retaining authority over the D3/D4 boundaries, which is the correct direction of deference. |

Two claims in the fixes were new enough to be worth checking rather than accepting:

- **Phase 1 item 10** (multi-level unwind, transition lock) did not come from this
  review and is accurate. `_transition_lock` is a `threading.RLock`
  (`coordinator_app.py:191`), so a recursive registration path would silently
  re-acquire it and hold it across D7 drain/reap rather than deadlocking — a hazard
  that hides instead of announcing itself, which is exactly what the item names. It
  has a matching verification bullet (line 1082).
- **The reliability doc's retained claim** that *"preflight rejects a budget without
  prices"* is still true as shipped — `config.py:332` raises when `max_cost_usd` is
  set without `model_prices`. Keeping that sentence while marking the budget branch
  inert is correct, not a leftover.

## Close-out

The design is ready to implement. R1 should be fixed before the verification list is
used as a test specification, since its literal reading points an implementer at the
D5 regression. R2 is worth fixing while the layout is still being edited — it is
cheap now and becomes an invented convention later. R3–R6 are cold-readability
polish and can ride along with any future edit.

## Maintainer close-out after the final re-review

The binding design subsequently incorporated R1–R6: verification now scopes the
exclusive owner to `goal_met`; readiness, rejected control, and protocol failures
have named paths and schemas; the repair statement is explicitly v2-only; task
acceptance and terminal-blocker roles are separate contract fields; control v2 is
shown for both terminal reasons; and the logical-reference resolver is an explicit
Phase 1 deliverable. The stale analysis pointer noted above was also corrected.
The binding design remains the final adjudication.

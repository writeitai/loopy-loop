# Adversarial Implementation Review — Recursive Loop Layer Contract (D10–D12)

**Reviewer:** Claude Code (Opus 4.8)
**Date:** 2026-07-16
**Scope:** the uncommitted diffs vs `origin/main` in three owned repositories —
`loopy-loop` (0.7.0, branch `feat/recursive-loop-layer-contract`),
`team-harness` (0.5.0), and `eval-banana` (0.3.2).
**Binding documents read first:** `AGENTS.md`, `design/decisions.md` (D1–D12),
`design/designs/recursive-loop-layer-contract.md`.
**Status of this document:** `design/analysis/` — working notes, per AGENTS.md Rule 3.
No product code was modified during this review.

---

## 0. Read this first — the review conditions invalidate a "final" verdict

**The branch was being actively rewritten by another agent while this review ran.**
This is not a caveat; it is the single most important finding, because it changes
what any review of this branch can honestly claim.

Evidence, captured directly:

- A `codex --yolo` process (PID 37769) held the checkout throughout.
- `src/loopy_loop/coordinator_app.py` mtime moved to `08:02:26`, then `08:03:42`,
  then again — while the wall clock read `08:03:21` / `08:04:29`. Files changed
  seconds before and after each observation.
- `team-harness`'s `get_capabilities()` returned **four** capabilities at review
  start and **six** (`+nested_caller_context_v1`, `+stream_capture_status_v1`)
  ~40 minutes later. A new modified file (`src/team_harness/agents/manager.py`)
  appeared that was absent from the opening `git status`.
- The loopy suite measured, in order: **315 passed** → **104 failed** →
  **105 failed** → **24 failed / 291 passed** → **23 failed / 304 passed**,
  across ~55 minutes, with no action from this review. The last measurement
  (08:08:36) found `coordinator_app.py` had been rewritten **6 seconds
  earlier** — the branch was still in motion when this document was filed.
- One finding (the trace-redaction blocker, §3) was **independently verified as
  live, then verified as fixed** roughly 30 minutes later, by the other agent.

**Consequence.** Every line number below is pinned to a file hash and is already
stale. More importantly, **the release gate defined in the design
(`recursive-loop-layer-contract.md`, "Verification and release gate": the full
suite in all three repositories plus formatting, lint, and type checks)
is not met at any moment this review could observe.** The suite was red at the
last measurement (24 failed / 291 passed at 08:04:56).

**No "no blocking issue remains" verdict can be issued against a tree that is
mid-edit and red.** The findings below are real and were verified against the
hashes named; they should be re-confirmed once the branch is quiesced.

**Reviewed snapshot (08:04:56, md5):**

| File | Hash |
| --- | --- |
| `coordinator_app.py` | `b408b4b4bfbb798ec04cfe6b9ea063ad` |
| `tracing.py` | `111f90ebaf6f3f7b6300742f7419774c` |
| `git_evidence.py` | `7102417ffb8d82e07747a707e1cfc242` |
| `worker.py` | `6e25b83719f2070c8b5c61e499e1beeb` |
| `models.py` | `954216a5746a544c1306b4beb0ebccae` |
| `assignments.py` | `a11aa9bcf72f1432037b46fc7ca576ab` |
| `references.py` | `d2fcea7cee0ce8c309f6b1cf9549d9dc` |

### Divergence from the peer review

`design/analysis/antigravity-recursive-loop-implementation-review.md` returns
**PASS / "No blocking implementation issues remain."** This review does not
concur. That review did not examine release/lock hygiene (§1), and its
supporting claims — e.g. that `seal_attempt_trace` "redact[s] keys in files",
that trace drift "blocks export" — describe mechanisms that are real but whose
*coverage* fails under probing (§3), or that never execute on the traces most
likely to hold secrets (§7). Its two low-severity suggestions are reasonable and
not disputed.

---

## 1. BLOCKER — the branch cannot install or build in CI; the lockfile was never regenerated

**Verified** (reproduced directly, `uv lock --check`).

**Where.** `pyproject.toml:40,47` vs `uv.lock` (unmodified vs `origin/main`;
last touched by `4491be9 "Prepare 0.6.0 release"`), `.github/workflows/ci.yml:28`.

`pyproject.toml` raised both floors to `team-harness>=0.5.0` and
`eval-banana>=0.3.2`. `uv.lock` is **byte-identical to `origin/main`** and still
records `>=0.4.0` / `>=0.3.1` pinned to `registry = https://pypi.org/simple`.
PyPI's newest published versions are **team-harness 0.4.0** and
**eval-banana 0.3.1**; 0.5.0 and 0.3.2 exist only as uncommitted sibling branches
(`feat/loopy-caller-contract`, `feat/loopy-eval-output-contract`).

**Failure sequence.**
1. CI runs `uv sync --extra dev` (`ci.yml:28`).
2. uv sees `pyproject.toml` has drifted from `uv.lock` and re-resolves.
3. Resolution fails outright:
   ```
   × No solution found when resolving dependencies:
   ╰─▶ Because only eval-banana<=0.3.1 is available and your project depends
       on eval-banana>=0.3.2, ... requirements are unsatisfiable.
   ```
4. The job dies at **"Install dependencies"** — before lint, pyright, or pytest.

**Two corrections worth recording**, because both are easy to get wrong:

- The failure is **not** "CI installs 0.4.0 and then returns HTTP 426." uv never
  installs anything, so **the 426 capability-rejection path is unreachable in
  CI**. Anyone debugging this will see an unsatisfiable-resolution error, not a
  capability rejection.
- **Local green is an artifact of an editable bridge, not evidence.** The venv
  contains `_editable_impl_team_harness.pth` and `_editable_impl_eval_banana.pth`
  pointing at the sibling checkouts, so `team_harness.__file__` resolves to the
  0.5.0 working tree even though `importlib.metadata.version()` reports 0.4.0
  (stale `dist-info` recorded when the checkouts sat at those versions). Local
  runs therefore execute **real 0.5.0/0.3.2 code**; CI cannot install at all.
  The design calls editable siblings "a development bridge, not a production
  dependency declaration" — this is exactly that gap, made load-bearing.

**Design conformance.** The design already mandates the ordering ("Merge/publish
the team-harness 0.5.0 and eval-banana 0.3.2 PRs before (or atomically with) the
loopy-loop 0.7.0 PR ... resolve the release lock against those published
versions"). So this is unexecuted release mechanics rather than a design defect —
but it is release-blocking, and **the design never mentions regenerating
`uv.lock`**, which is the step actually missing.

**Incidental credit (verified).** This same stale metadata *vindicates* a design
choice: `worker.py:802` gates on advertised capability names, never on a version
("Advertise harness features by name; never infer them from a version"). A
version gate would misfire on precisely this metadata. That decision is correct.

---

## 2. BLOCKER — an untracked nested git repository permanently bricks the loop

**Verified** by direct reproduction against the live tree
(`git_evidence.py` md5 `7102417f…`, re-confirmed after the concurrent agent's edits).

**Where.** `src/loopy_loop/git_evidence.py:429` `_validate_git_path`, reached via
`_filtered_status_entries:202` → `_parse_status:228`.

With `--untracked-files=all`, git does not descend past a nested repository
boundary — it emits the directory **with a trailing slash**. Reproduced on git
2.49.0:

```
$ git status --porcelain=v1 -z --untracked-files=all
b'?? reference-impl/\x00'
```

`_validate_git_path` splits on `/`, finds a trailing empty segment, and raises.
Confirmed end-to-end:

```
nested clone present: True
GitEvidenceError RAISED -> Git returned an unsafe working-tree path
```

**Failure sequence.**
1. An agent runs `git clone`, vendors a reference implementation, or otherwise
   lands a directory containing `.git` in the working tree and does not gitignore
   it. This is an action these agents plausibly take.
2. `worker.py:311` `_capture_git_boundary(phase="before")` → `GitEvidenceError`
   → `AssignmentContractError` (`worker.py:784`).
3. `worker.py:414` catches it as `fatal_error` →
   `IterationResult(success=False, failure_kind="deterministic")`.
4. **This is line 311, before `run_harness_iteration` at line 407.** No agent ever
   runs, so no agent can delete the clone, gitignore it, or even see the error.
5. Every subsequent attempt fails identically until
   `workflow_consecutive_failures_cap` → `stop_reason="workflow_failure_cap"`.

**Why this is a blocker and not a nuisance.** It is self-inflicted,
unrecoverable, and inverts D8. The system's whole doctrine is "fail-closed
detection with a repair path" — here there is no repair path, because the failure
precedes the agent. It is not even a legible D5 `unresolvable_error`; the session
dies as a generic failure cap, which is exactly the illegible give-up AGENTS.md
Rule 2 forbids. `_path_fact` already handles `type:directory`, so the parser is
the only thing rejecting this.

---

## 3. Trace redaction — verified live, then **FIXED mid-review** by the concurrent agent

Recorded because it demonstrates the moving-target problem concretely, and
because the underlying design lesson stands.

**Originally verified** (`tracing.py` at the earlier hash): structured
(key-based) redaction ran only when `path.suffix` was exactly `.json`/`.jsonl`.
Everything else fell through to a string-level pass that could not match
JSON-quoted form, because the closing quote sits between the key and the `:`.
Probe, identical byte-for-byte content `{"api_key": "sk-live-SURVIVOR-…"}`:

```
redacted out.json       LEAK out.log      LEAK out.txt     LEAK notes.md
redacted out.jsonl      LEAK out.yaml     LEAK out.JSON    LEAK out.json.bak
```

`.JSON` bypassed via case-sensitivity; a `.json` that failed to parse also
bypassed. Export applied the *same weak pass*, so it could not recover what seal
missed — defeating the contract's central claim that "sealing is the security
boundary for such additions" (D12; design §"Complete observable input/output
accounting"), while the manifest hardcoded `sensitivity: "redacted_operational"`.

**Re-verified against the current tree — now fixed:**

```
redacted out.json    redacted out.log    redacted out.JSON
```

**Standing recommendation.** Keep a regression test that asserts redaction on a
**non-`.json` extension** and an **uppercase** extension specifically. The whole
point of the pre-seal sweep is arbitrary agent-written files, which by definition
carry arbitrary names; an extension-keyed policy re-introduces this class
silently. The related `sensitivity` field should be computed, not asserted.

---

## 4. HIGH — the bounded control-protocol counter never accumulates; the engine resets it with its own repair placeholder

**Verified** by code trace on the current tree, and by a subagent's execution repro.

**Where.** `coordinator_app.py:3899` (`_reject_v2_control` increments),
`:3924-3929` (writes the placeholder), `:3528` / `:3548` (`_apply_session_control`
resets).

`_reject_v2_control` increments `control_protocol_consecutive_failures`, archives
the bad control, then writes a placeholder **back to `control.json`**:

```python
write_json_atomic(path=path, payload={
    "schema_version": 2,
    "state": "running",
    "reason": "invalid terminal request archived for autonomous repair",
    "stop_reason": None,
})
```

`_apply_session_control` then resets the counter to `0` on **any** running v2
control — it cannot distinguish the agent's genuine repair from the engine's own
placeholder. `_apply_session_control` runs twice per `/finished`, so the second
call reads the placeholder the first call just wrote.

**Observed (cap = 3, five consecutive invalid controls):**
```
round 0..3: status=running counter=0     # never accumulates
round 4:    status=failed stop=workflow_failure_cap
5 protocol_failures records, every one: consecutive_failure_count = 1
```

**Failure sequence (genuinely unbounded).** `eval_runner` alternates one invalid
control, one valid `running` control. The control counter is reset by the
placeholder; `workflow_consecutive_failures` is reset by the successful run. The
loop churns — burning real money — until `max_turns`/`max_cost_usd`.
`control_protocol_broken` is unreachable unless `cap == 1`.

This contradicts D11 ("a bounded failure counter eventually stops repeated
breakage"), and both `docs/session-layout.md:192` and `docs/http-contract.md:465`,
which assert the bound. The session above died only via an unrelated breaker.

---

## 5. HIGH — a stale eval receipt can close a session; producer identity is self-declared

**Verified** by code trace on the current tree + subagent execution repro.

**Where.** `_validate_v2_control`, `coordinator_app.py:3566-3580` (`known_attempt`),
role check immediately below.

`known_attempt` accepts the current task **or any entry in `state.history`**, with
no recency binding:

```python
known_attempt = (state.current_task is not None and ...) or any(
    item.attempt_id == producer.attempt_id
    and item.workflow_id == producer.workflow_id
    for item in state.history)
```

Two consequences:

1. **Stale receipt closes the session.** The engine binds a passing receipt's git
   state only to *that receipt's own attempt's* `git-after` record
   (`:3682-3707` compares `git_after["head"]` to `receipt.subject.git_commit`) —
   **never to the session's current git state**. A receipt from attempt-1 is
   internally consistent forever. Sequence: iteration 1 `eval_runner` produces a
   valid passing receipt; work continues and the tree changes; a later eval fails;
   `eval_runner` republishes the **old** passing receipt → session closes
   `goal_met` on evidence bound to a superseded tree. Repro confirmed
   `stop goal_met status=goal_met` with a *different* workflow as the live attempt.
2. **The `goal_control_role` check is bypassable.** `producer.workflow_id` is
   self-declared; the engine cannot know who wrote `control.json`. Any role that
   names `eval_runner` plus any historical `eval_runner` attempt satisfies it.

Point 2 is partly inherent to file-based control and is arguably within D8 (the
engine validates structure, not authorship). **Point 1 is not** — D11 requires the
receipt to bind its verdict to "the evaluated git state," and that binding is not
enforced against the state being closed.

`test_recursive_loop_contract_v2.py:1144` is named `..._current_attempt_control...`
but only exercises the current attempt; nothing enforces the name's implication.

**Suggestion (not verified as safe):** require
`producer.attempt_id == state.current_task.attempt_id` for `goal_met`.

---

## 6. HIGH — `_validate_v2_control` re-reads the frozen contract with the non-fallback reader → uncaught `ConfigError` → HTTP 500 wedge

**Verified** structurally on the current tree.

**Where.** `coordinator_app.py:3566` calls `self._read_workflow_contract(...)`,
which raises `ConfigError` when `workflow_contract.json` is absent
(`_read_workflow_contract`, ~`:3509`). Its own caller `_apply_session_control`
deliberately uses `_workflow_contract_for_state`, whose documented legacy fallback
exists precisely for that case ("Pre-v2 sessions did not persist
`workflow_contract.json` … a missing file is projected … with protocol v1
semantics").

`/finished` catches `WorkerUpgradeRequired`, `WorkerBusyError`, and
`FileLockTimeout` — **not `ConfigError`** → HTTP 500.

**Failure sequence.** A legacy v1 PM session resumes with a child active →
`_prepare_state` backfills `workflow_contract.json` for the **active** session
only → the child completes → the parent resumes without a contract file → the
parent's planner writes the v2 `unresolvable_error` form (**which AGENTS.md Rule 2
now teaches as the primary example**) → `/finished` 500s forever. The control is
never archived, no protocol failure is recorded, and the session never stops —
the D5 escape hatch itself becomes the wedge.

**Fix direction:** pass the already-resolved contract into `_validate_v2_control`.

---

## 7. HIGH — D12's "test the real integration" is not satisfied; a rename degrades silently into unknown usage

**Verified.** `grep -rln "caller_contract\|CallerContext\|get_capabilities" src/tests/`
→ **no matches**. `_worker_capabilities()` is referenced only by product code
(`worker.py:178,249`); no test calls it.

Every handshake uses hardcoded literals (`protocol_helpers.py:15-23`,
`test_recursive_loop_contract_v2.py:49-61`); the 426 test posts hand-written
`"capabilities": []`. All `run.json` fixtures are hand-written. Nothing asserts
*"team-harness advertises the four capabilities loopy requires"*, and nothing
drives a real `TeamHarness` to write `run.json` under a real
`CallerContext.trace_root` and read it back.

**The contract itself currently matches** — verified at runtime, not by eye:
capability strings in `team-harness/src/team_harness/caller_contract.py:12-15`
are byte-identical to `loopy/src/loopy_loop/models.py:23-29`; loopy's hand-built
`caller_context` dict (`worker.py:351-364`) validates against the real
`CallerContext` (`extra="forbid"`) with **9/9 field parity**; `run_json_path`
(`harness.py:302`) is read first by `harness_runner.py:287-289`. So this is an
unguarded seam, not a live break.

**Failure sequence.** team-harness renames `run_json_path` → `run_record_path`
(or a `CallerContext` field). Loopy's suite stays green — fakes emit whatever
loopy expects; capability lists are literals. It ships. At runtime
`_normalize_harness_result` silently sets `harness_run_json_path=""` → the
manifest records no canonical run path → recovery's glob finds nothing →
`recovery.py:164` logs "no run.json for interrupted harness run" → usage unknown
→ **`max_cost_usd` cannot fire**. Silent, not loud — the exact defect D12's own
Context section was written to close.

**Note the moving target:** team-harness's advertised set already changed from 4
to 6 capabilities during this review. Nothing in loopy's suite would notice.

**Contributing (LOW).** `_supports_kwargs` (`harness_runner.py:230-231`) returns
`True` for any `**kwargs` factory. The real `TeamHarness.__init__` is keyword-only
with no `**kwargs` (verified), so production is safe — but every `FakeHarness`
uses `def __init__(self, **kwargs)`, so the fail-fast check passes vacuously in
tests.

---

## 8. HIGH — re-entrant `FileLock` deadlocks (two verified sites, one bug class)

**Verified by execution** (subagent, instrumented lock tracer). `filelock` 3.28.0
is non-reentrant across instances: same path, same process → blocks.
**Caveat:** `coordinator_app.py` was rewritten repeatedly during this review; these
two must be re-confirmed on a quiesced tree.

### 8a. Every child session deadlocks when `max_cost_usd` is set

`_apply_stop_precedence` → `root_tree_usage_totals` (now `:3407`).

```
_finish_assignment_locked -> state_store.mutate      # ACQUIRES child's lock
 -> _advance -> _stop_response_if_needed -> _apply_stop_precedence
 -> root_tree_usage_totals -> accumulate -> _read_or_repair_children_payload
 -> _validate_v2_children_payload -> child_store.read_state()
 -> state_store.py:40 with self._lock()              # RE-ACQUIRES SAME LOCK
```

The child holds its own lock via `mutate`; `root_tree_usage_totals` walks *down*
from the root and re-reads that same child's `state.json`.

**Sequence.** Config `max_cost_usd` + `model_prices` → `/register` → parent
`/finished` with a child request → child dispatched → child posts `/finished`.
**Result: blocks 30.1s (`DEFAULT_LOCK_TIMEOUT_SECONDS`) → HTTP 503 "retry
shortly".** Retrying re-deadlocks forever; the child never advances past
iteration 1. Removing `max_cost_usd` → passes in 0.42s.

This makes D6/D10's double loop unusable with a budget. Untested because both
existing `max_cost_usd` tests (`test_events_and_usage.py:171,474`) use a **root**
session, where `root_tree_usage_totals` short-circuits (`root_state = state`, no
lock).

### 8b. Torn-transition recovery deadlocks the same way (no budget needed)

`_adoptable_child_id` → `_mark_child_record_complete` → `parent_store.read_state()`,
called from `_register_attempt`'s mutator — i.e. *inside* the parent's own
`mutate`, rebuilding a `StateStore` for the parent and locking it again.

**Sequence.** Parent dispatches child → crash leaves the documented window (parent
`state.json` still has `current_task`, pointer commit lost, `children.json` =
`running`, child state terminal) → restart → `/register`. **Result: 30.1s → HTTP
503, permanently wedged.** This is exactly the "record running, child state
TERMINAL → finalize (M3)" branch the docstring advertises. Same shape at
`_suspended_parent_response`.

**Telling contrast:** `_reconstruct_v2_children_ledger` reads parent state
*directly*, with a comment about avoiding "deadlocking on our own in-flight
coordinator transaction." The hazard was identified and fixed in one of ~4
nesting sites. **A systematic audit of every `read_state`/`mutate` reachable from
inside a mutator is warranted** — 4 sites found, 2 verified to deadlock.

---

## 9. HIGH — submodule content is invisible to the dirty-tree digest (collision hides a change)

**Verified by probe.** `git_evidence.py:273` `_path_fact`, directory branch
(`:306-318`).

A dirty submodule is reported as one entry ` M vendor`. `_path_fact` lstats it,
sees a directory, and returns only `type:directory` + `mode:` — **no content
digest**. With the submodule already dirty at attempt start (common), an agent
rewriting `vendor/lib.py` *and adding `vendor/backdoor.py`* yields a
byte-identical before/after digest:

```
git-BEFORE: dirty=True digest=sha256:f1cb5fadbb69c16c5e7
git-AFTER : dirty=True digest=sha256:f1cb5fadbb69c16c5e7   IDENTICAL: True
```

This contradicts the design's own two claims
(`recursive-loop-layer-contract.md:1043-1049`): "content digests for **every**
changed tracked or untracked working-tree path", and "prevents matching digests
from hiding an untracked source file". Under D11, `EvalSubject.dirty_tree_digest`
claims to bind a receipt to "the evaluated git state", but that binding is not
unique for submodule content — a reviewer diffing before/after concludes "no
change".

**Related, and worth noting together:** §2 (nested repo) and §9 (submodule) are
the same blind spot from two directions — the digest algorithm has no coherent
story for a directory that is itself a git boundary. It either crashes (§2) or
silently under-reports (§9).

---

## 10. HIGH — frozen input drift is an unrepairable deterministic failure loop

**Verified end-to-end.** `assignments.py:447-460` `build_attempt_assignment`.

Both input verifications the design requires **do exist** (see §12). The defect is
the missing repair path:

```
baseline                                          OK
attempt 2 after ONE byte changed in the input     AssignmentContractError:
                                                  child input no longer matches its frozen hash
```

The input is frozen once at child creation, but `build_attempt_assignment`
re-verifies on **every attempt** for the child's entire lifetime (many iterations,
possibly hours). Any writer touching the parent's `project_state/work_items.md` in
that window kills the child permanently — the error fires **before** the harness
call, so the agent can never see or repair it.

D10 makes this reachable *by design*: ownership metadata is "instruction and audit
evidence, not an ACL", there is "no engine write fence", and the child's
assignment hands it `parent_session_root` as an absolute path.

This is the second place (with §2) where D8's doctrine inverts: the design
promises "a new assignment as the repair path" for snapshot mismatch, but input
mismatch has no equivalent, and it isn't even a legible `unresolvable_error` —
just `workflow_failure_cap`.

---

## 11. MEDIUM findings

### 11a. Crash-abandoned attempts are never sealed → never sanitized, unprunable, unexportable, permanently
**Verified.** `coordinator_app.py` `iteration_abandoned` branch.
`_finalize_completion_trace` — the only caller of `seal_attempt_trace` — is
invoked only from `/finished` and from `/register` recovery of a *pending*
finished request. The crash-abandonment branch appends a `failure_kind="crash"`
history entry and returns without touching the trace.

```
lifecycle after crash-abandonment: active
  prune:  REFUSED -> refusing to prune an active or unsealed trace
  export: REFUSED -> active traces cannot be exported
  secret still on disk unsanitized: True
```

Three consequences: the sanitization sweep never runs on the trace most likely to
hold crash spew; the trace of a crash — highest forensic value — can never be
exported; unprunable garbage accumulates per crash. The `incomplete` lifecycle
state appears designed for exactly this but is only reachable via a *successful*
`/finished` that self-reports `trace_incomplete`.

### 11b. `eval` channel completeness is guessed from directory non-emptiness
**Verified.** `worker.py:468-471`:
`"complete" if any((trace_root / "eval").rglob("*")) else "not_produced"`.
No loopy writer populates `<trace>/eval`, so the status is decided by whatever
arbitrary agents drop there; an agent `mkdir`-ing a single **empty** subdirectory
flips the channel to `complete`. This is precisely the "guessed rather than
marked" pattern D12 forbids ("a channel that a provider does not expose is marked
unavailable rather than guessed").

### 11c. A malformed `eval_readiness/*.json` bricks the session, with no repair path
**Verified.** `worker.py:885-896` `_semantic_prompt_context` raises `ConfigError`
on unreadable/non-object readiness; `worker.py:414-422` converts it into a
deterministic failure **before the harness runs, for every role**. `outer`/
`planner` own that directory in both `contract.yaml` files, so one truncated write
means no agent is ever prompted again. Contradicts the design's own framing
("semantic context only") and D8's repair-path requirement; the readiness section
should be skipped/annotated, not raised.
*Related (LOW):* `worker.py:886-888` uses `sorted(...)[-1]`, so `ready-2.json`
sorts above `ready-12.json` — not "the latest receipt".

### 11d. One malformed sibling session breaks every path helper repo-wide
**Verified.** `references.py:95-96` (`for_session`) validates **every** session in
the repo before filtering to the current tree; `sessions.py:306-329`
`session_dir_path` re-raises anything that isn't exactly `unknown session ID`, and
it backs `state_path`, `control_path`, `goal_contract_path`. An unrelated,
different-root session with corrupt JSON makes a healthy session fail to resolve
`session:/own.txt`. `.loopy_loop/sessions/` accumulates every past root, and D8
lets an agent create a stray dir there. `references.py:70-73` states exactly the
right principle — "a corrupt diagnostic trace must not make state/control paths
unavailable" — but applies it only to traces, not across session trees.

### 11e. Completion fence downgrades after a coordinator restart
**Verified at an earlier hash; appears to be under active repair.**
`_worker_contracts` is in-memory only (`:317`), populated in
`_validate_worker_handshake` (only `register_worker` calls it);
`_create_current_task` does `.get(key, 1)` → **defaults to 1**.

```
REG                     ccv=2
FIN (same coordinator)  ccv=2
RESTART + /finished     ccv=1   <-- silently downgraded
IMPOSTER /finished (no worker/repository_id/assignment_sha256) -> HTTP 200, recorded as success
```

Contradicts "a worker in the wrong checkout must not be able to … post a
plausible result to another repository's session."
**Fix direction:** persist the negotiated contract version on durable state, not a
process-lifetime dict. *(As of the final snapshot, `finish_assignment` had gained
a `WorkerUpgradeRequired` handler and `_worker_contracts` a new write site at
`:462` — re-verify.)*

### 11f. Prompts pin `gpt-5.5`; eval-banana's codex default moved to `gpt-5.6-sol` (suggestion)
`eval-banana/src/eval_banana/harness/template.py:45` sets
`default_model="gpt-5.6-sol"`, but all four loopy eval prompts pass
`--harness-model gpt-5.5`. Mechanically fine — explicit flags override, and the
design *requires* explicit judge selection, so it stays self-consistent with the
receipt's `judge.model`. But loopy pins a model eval-banana deliberately moved
off; if `gpt-5.5` is retired, every judge check errors and no receipt can publish.
Worth a deliberate decision rather than drift.

---

## 12. LOW findings and suggestions

- **`tree_system_extension_sha256` is written but read by nobody**
  (`assignments.py:166`; zero readers; `verify_workflow_snapshot` checks identity
  + 4 hashes + `repository_id`, not this). Relatedly,
  `root_config_snapshot.json` is hash-verified but its **contents are never
  loaded** — the worker executes from `task.config_snapshot` off the wire. No
  attacker here (both come from one coordinator response); the risk is that a
  future divergence goes undetected. *Suggestion.*
- **`enqueue_trace_export` reuses an outbox record keyed on `manifest_id`**
  without checking `payload["trace_root"]` matches the request; export then uses
  the *recorded* root. Unreachable via the coordinator (`uuid4().hex[:12]`), but
  the worker's `f"legacy-{iteration}-{workflow_id}"` fallback is non-unique across
  sessions. *Defensive gap.*
- **`seal_attempt_trace` early-returns only on `lifecycle == "sealed"`**, not
  `"incomplete"`; a second call on an incomplete trace re-runs sanitize +
  re-inventory, re-blessing post-seal drift as sealed evidence. Latent — currently
  guarded by the caller.
- **Protocol-failure records don't reliably name the producing attempt**
  (copies the rejected control's self-declared `producer`; unparseable JSON →
  `"producer": null`). The design requires naming "the workflow/attempt";
  `state.current_task` is authoritative and available.
- **A receipt need not cover all authored checks.** Nothing requires the receipt to
  cover every check in `eval_checks/`; a runner evaluating a copied subset passes
  while silently dropping the reviewer's other checks. Consistent with the design
  text as written ("exactly the receipt's check IDs"), so this is a **gap in the
  contract**, not a code/design mismatch. *Suggestion: have the engine compare the
  receipt's check set against the authored set, or state the subset rule
  explicitly.*
- **Doc drift:** `recursive-loop-layer-contract.md:1095-1096` says the `/finished`
  response is "explicitly unavailable because the trace is sealed before that
  post". The implementation captures `protocol/finished_response.json` and sets
  `service: complete` before sealing. The implementation is *better* than the
  text; the text is stale.

---

## 13. Verified correct — do not re-litigate

These were probed adversarially and found sound. Recorded so future reviews
don't re-spend effort, per AGENTS.md Rule 1.

- **Logical reference resolver.** ~35 attacks, no escape. Absolute paths, `..`,
  `.`, empty/double segments, `\`, null bytes, embedded colons, malformed scopes,
  unknown IDs all rejected. Symlink escapes (dir→`/etc`, file→`/etc/passwd`,
  relative, nested) rejected by `_resolve_beneath` resolving then
  containment-checking. URL-encoded (`%2e%2e`, double-encoded) and unicode
  (fullwidth `．．`, one-dot-leader, NFD) correctly treated as **literal
  filenames**, not decoded. Scope-name confusion is a non-issue (a session named
  `repo`/`root`/`parent` resolves via `session:<id>:/`). macOS case-insensitivity
  does not escape. `_SAFE_ID` uses `\Z` + `fullmatch` — no trailing-newline bypass.
- **Workflow snapshot.** The worker genuinely reads the snapshot, not live files;
  tamper is caught pre-harness (their own test asserts `calls == []` and that the
  assignment file is not written). Hash covers config, prompt, workflow contract;
  goal contract via `manifest.goal_contract_hash`.
- **Absolute path contract.** All 28 keys in the design example are produced;
  `accepted_request` conditional, matching the doc. `snapshot_root` is
  containment-checked. team-harness's spawn envelope carries absolute
  `assignment_path`/`output_dir`/`parent_assignment_path` and guards against
  escaping the output root.
- **Both input verifications exist.** Parent: `coordinator_app.py:2160-2192`
  resolves, `is_file()`-checks, full `file_sha256`, and normalizes
  `parent:/`→`session:<parent_id>:/`. Child: `assignments.py:457`. (The defect is
  the missing repair path, §10.)
- **Git digest, other axes.** Mode-only chmod, regular→symlink, file→dir, empty vs
  non-empty, rename with identical content, `.loopy_loop` vs `.loopy_loop_evil` all
  produce distinct digests. Filenames with newlines/quotes are safe (`-z` means git
  never quotes). `_hash_field` is length-prefixed — no field-boundary ambiguity.
- **Bounded corrupt-ledger repair.** Holds, and this is the invariant the design
  cares most about. Total loss → explicit `ChildLedgerError` + refusal receipt,
  **never** an empty ledger. Structurally-valid-but-emptied ledger with a live
  pointer → `ChildLedgerError` at startup. Garbage ledger + live child on disk →
  correctly rebuilt.
- **Transition lock not held across process recovery.** Phase-A drain runs outside
  `_transition_lock`; `_resume_parent_if_active_child_completed` passes
  `recovery=None`. As designed.
- **Multi-level unwind converges.** Iterative, terminates at
  `parent_session_id is None`, finalizes exactly one level per pass, idempotent
  (already-cleared pointer is a no-op; mismatched pointer raises). No lost-level
  path found.
- **Stale-result fencing on `attempt_id`.** Superseded `result.json` and
  `pending_finished_request.json` both rejected on mismatch.
- **`state_store._validate_committed_shape`** rejects task+child and
  terminal+inflight at every v2 commit.
- **Seal rehash + drift refusal is real.** `_require_finalized_trace_integrity`
  runs in *both* `enqueue_trace_export` and `export_trace_to_directory`;
  `_validated_inventory` rejects absolute/`..`/duplicate paths. Symlinks and
  binary/NUL bytes → explicit omission markers. Seal flip is atomic; a crash
  mid-sanitize leaves the trace `active`, not falsely sealed.
- **Export is outside the correctness path.** Only `cli.py` imports the export
  functions; nothing in `worker.py`/`coordinator_app.py`. Partial-copy honesty
  holds (`OSError` → `attempts += 1`, status stays `pending`, re-raise).
- **Pruning cannot destroy recovery-critical facts.** `pending_finished_request.json`
  and `salvage.json` live under iteration dirs, not `.loopy_loop/traces/`; prune
  refuses active traces.
- **Fresh-tree bootstrap does 426 correctly.** `create_coordinator_app` writes root
  state at startup, so `requires_v2` is always true; validation is read-only and
  precedes mutation. (An earlier hypothesis that a fresh tree skips the gate was
  **wrong** — recorded so it isn't re-raised.)
- **eval-banana surface.** All flags exist (`run`/`validate` `--no-project-config`,
  `validate --harness-agent`, `--harness-model`, `--harness-reasoning-effort`);
  both `eb` and `eval-banana` console scripts are real; report fields
  (`run_passed`, `pass_threshold`, `checks[].check_id/status/details`,
  `details.agent_type/model`) match loopy's validator exactly.
- **Receipt provenance.** Schema, identity, existence, goal hash, artifact hashes
  all validated before `goal_met`; receipts confined to the session's own
  `eval_receipts/`. `report.json` binding enforces `run_passed`,
  `pass_threshold == 1.0`, exactly the receipt's check IDs all `passed`, per-check
  judge match, and exactly-one-`report.json`. `raw_report_sha256s` ≡
  `raw_report_refs` by set-equality in `models.py`. goal_check v2 ↔ receipt
  cross-checked twice.

### Decision compliance

- **D2 — verified.** No parallel loopy workers introduced. Usage projection has
  cycle detection and exactly-one-edge validation; no double counting.
- **D3 — verified.** `_normalize_harness_result` still returns `success=True` on
  any normal return; no exit-code consultation added (`grep` for
  `exit_code`/`returncode` in `harness_runner.py`: no hits).
- **D4 — verified.** Both `eval_reviewer` prompts retain "create only
  `harness_judge` checks. Do not author deterministic checks."
- **D5 — verified.** `unresolvable_error` needs no receipt (`models.py` forbids
  `eval_receipt_ref` on it) and is open to every role in
  `terminal_blocker_reporting_roles`; all four roles listed in both contracts. No
  `paused`/`waiting_for_human`/`gate_request` anywhere in `src/`. **But see §6** —
  the D5 path itself can wedge on a legacy session.
- **D8 — verified.** `eval_readiness` is read only for prompt rendering and path
  exposure; `scheduler.py` is purely cadence-based, with no semantic eligibility
  gate. No path write enforcement, no model allowlists. **But §2, §10, and §11c
  each violate D8's repair-path requirement in practice** — a pre-harness hard
  failure the agent cannot see or fix is a preventive fence in all but name.
- **D9 — verified.** `model_tiers`/`default_tier` remain repo-global and are
  excluded from the per-attempt snapshot; `RootConfigSnapshot.team_harness_model`
  is a single value; no per-session/per-depth model differentiation.
- **D11 engine-side ownership — verified.** `state.goal_met = True` is set only
  from the session's own `control.json`; `_mark_child_record_complete` merely
  projects evidence — **a green child cannot close the root**. Both packaged
  contracts declare `goal_control_role: eval_runner`; the PM set now ships its own
  `eval_reviewer`/`eval_runner`; `outer`/`planner` prompts explicitly forbid
  terminal control. *Caveat:* only as strong as the self-declared producer (§5).
- **Legacy v1 — verified.** `_workflow_contract_for_state` forces protocol 1 for
  legacy states even when the set declares 2 (no retroactive upgrade); v1
  `goal_check` accepted only when protocol < 2; v1 control in a v2 set → repairable
  rejection, not a stop. *Subject to §4 and §6.*

---

## 14. Verdict

**Blocking issues remain. This branch is not ready to merge, and no "final"
verdict is possible against it in its current state.**

The architecture is sound and, in the areas that were hardest to get right, the
implementation is genuinely good: the recursive session node, the iterative
unwind, the corrupt-ledger refusal, the reference resolver, the snapshot fencing,
and the eval-receipt provenance chain all survived deliberate attack (§13). The
capability-over-version negotiation is actively vindicated by the stale metadata
in this very checkout. D2, D3, D4, D9, and D11's engine-side ownership are
correctly implemented.

What blocks it:

1. **The branch is a moving target and its test suite is red** (§0). It was being
   rewritten by another agent throughout this review; the suite ranged from 315
   passed to 105 failed to 24 failed within an hour, and one blocker was fixed
   mid-review. The design's own release gate — full suite in all three repos plus
   lint and types — is unmet at every observed moment.
2. **CI cannot install the branch at all** (§1): `uv.lock` was never regenerated,
   and the required companion versions are unpublished. Verified via
   `uv lock --check`.
3. **Two verified self-inflicted, unrecoverable loop deaths** (§2 nested git repo,
   §10 frozen input drift) — both fire *before* the harness, so no agent can see
   or repair them, inverting D8 and bypassing D5's legible give-up.
4. **The bounded control-protocol counter does not bound anything** (§4) — the
   engine resets it with its own repair placeholder, contradicting D11 and two
   docs.
5. **A stale eval receipt can close a session** (§5), and the D5 escape hatch can
   wedge a legacy session with an HTTP 500 (§6).
6. **The one integration D12 explicitly demands be tested is tested nowhere** (§7),
   and its failure mode is silent (`max_cost_usd` stops firing) rather than loud.

The correct next step is to **quiesce the branch** — stop the concurrent agent,
get the suite green, regenerate `uv.lock` against published companions — and only
then re-run this review. Findings §2, §4, §5, §6, §8, §9, and §10 should each be
re-confirmed against the settled tree; §3 shows they can change under you.

I disagree with the peer review's PASS. I do not believe the disagreement is
about architecture — it is about whether mechanisms that exist are *reached* and
*covered*, which is where every finding above lives.

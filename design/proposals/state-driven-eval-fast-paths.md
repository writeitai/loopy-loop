# Proposal: State-Driven Evaluation Fast Paths

**Status:** Superseded before implementation — historical, non-binding

**Date:** 2026-07-17

**Applies to:** workflow sets with separate task-acceptance, eval-author, and
eval-runner roles, including `inner_outer_eval` and `pm_planner_dispatcher`

> **Do not implement this proposal as written.** It assumes the earlier D11
> contract in which `eval_runner` alone owned completion and a same-attempt
> passing eval was mandatory. D11 was replaced on 2026-07-17: the durable
> orchestrator now owns completion and evals are optional evidence. The useful
> efficiency need is handled by the accepted workflow roster and conditional
> scheduler view in
> [`orchestrator-owned-completion-and-cross-harness-review.md`](../designs/orchestrator-owned-completion-and-cross-harness-review.md).

The remainder records the proposed efficiency improvement against the former
protocol-v2/D11 baseline and is intentionally preserved as historical analysis.
Its present-tense descriptions of eval-owned completion are no longer current.
The binding decisions are
[`D3`, `D4`, `D8`, and `D11`](../decisions.md), and the replacement design above
defines the implementation direction.

## Summary

The stock delivery loop evaluates after every three mechanically successful
`inner` assignments. That cadence is simple during active implementation, but
it becomes wasteful when the delivery is already accepted and only the
evaluation-owned receipt or terminal decision remains.

The proposed fast path lets the workflow contract's task-acceptance role ask
for evaluation now. The request is not proof that the work is good. The engine
checks only the request's identity and provenance, then runs the same
eval-author and eval-runner roles earlier than the fixed cadence would. A
mistaken request therefore causes an early LLM evaluation, whose failing
verdict routes the loop back to repair; it can never manufacture acceptance.

Normal cadence remains the fallback when no valid request exists. No workflow
is prevented from running until a semantic condition is proven.

## The concrete problem

In a July 2026 double-loop run, a child delivery had already:

1. landed and merged its one scoped pull request;
2. been accepted by the `outer` role;
3. published a compact eval-readiness record; and
4. passed all five LLM-as-judge checks in the raw eval report.

The eval runner then failed at the harness/protocol layer while reading that
large report, before it could publish the same-session eval receipt and
`goal_check.json`. The repository work was not lost, but the fixed cadence
required more `outer → inner` cycles before evaluation became eligible again.
Those assignments repeatedly verified that the accepted work had not changed
and that no implementation leaf remained.

This is expensive and confusing. Global iteration 19 looked like nineteen
implementation attempts to an operator, while it was actually a mixture of
outer, inner, and eval workflows; the only scoped pull request had already
merged.

## Current behavior

`WorkflowConfig` in `src/loopy_loop/config.py` declares `run_every`,
`must_follow`, `priority`, and `run_after_successes`.
`choose_next_workflow()` in `src/loopy_loop/scheduler.py` receives only those
definitions, `HistoryEntry` records, and the completed iteration count:

- `_run_after_successes_satisfied()` counts mechanically successful target
  workflows since the candidate last succeeded;
- `_last_successful_workflow_id()` supplies the `must_follow` relationship;
  and
- `_failed_workflow_retry()` retries the latest mechanically failed workflow
  only when no normally eligible workflow exists.

The packaged `inner_outer_eval` reviewer has:

```yaml
run_after_successes:
  workflow_id: inner
  every: 3
```

The eval runner then follows the reviewer through
`must_follow: eval_reviewer`. This produces the mechanical sequence
`(outer → inner) × 3 → eval_reviewer → eval_runner`, as asserted in
`src/tests/test_template_contracts.py`.

Eval readiness is deliberately not scheduler input today.
`_semantic_prompt_context()` in `src/loopy_loop/worker.py` exposes the latest
parseable readiness JSON to agents as prompt context. The binding
[`recursive-loop-layer-contract.md`](../designs/recursive-loop-layer-contract.md)
and [success/evaluation design](../designs/success-semantics-and-evaluation.md)
explicitly say that readiness does not affect workflow eligibility.

## Goals

- Reach layer-owned evaluation promptly when the accountable acceptance role
  believes implementation is ready.
- Recover an eval chain without paying for implementation roles that have no
  remaining implementation work.
- Keep the engine ignorant of whether the work is semantically ready or good.
- Preserve the same eval author, LLM-as-judge checks, eval runner, receipt, and
  terminal-control ownership.
- Make the optimization durable and exactly recoverable across process or
  laptop restarts.
- Preserve the normal fixed cadence when no valid fast-path request exists.

## Non-goals

- Do not infer readiness from git state, prose, pull-request status, test output,
  or the presence of an accepted-work ledger.
- Do not let `outer`, `inner`, a planner, or a spawned subagent publish
  `goal_met`.
- Do not calculate or cache a semantic verdict in the scheduler.
- Do not skip the LLM judge merely because the repository subject appears
  unchanged.
- Do not merge the reviewer and runner roles in the first version.
- Do not add another loopy worker, parallel child session, human gate, approval
  state, or preventive agent fence.

## Binding invariants

Any implementation must preserve these decisions:

- **D2:** one loopy worker and one deepest active durable assignment.
- **D3:** `IterationResult.success` remains mechanical harness success. An eval
  wake request does not change it.
- **D4:** the stock semantic verdict remains LLM-as-judge. Request validation is
  provenance validation, not a deterministic quality check.
- **D5:** no normal human checkpoint is introduced.
- **D8:** the request may accelerate detection but never prevent a workflow
  until readiness is proven. A false-positive request is handled by evaluation
  and repair.
- **D9/D10:** coordinator model policy and dynamic harness delegation remain
  unchanged.
- **D11:** only the declared eval runner may close its own layer, using a valid
  same-attempt receipt and control signal.
- **D12:** compact scheduling facts belong in durable correctness state; verbose
  validation and execution detail belongs in traces.

## Proposed contract

### 1. Readiness v2 may carry an eval wake request

The workflow contract already names `task_acceptance_role`,
`eval.author_role`, and `eval.runner_role`. A fresh v2 readiness record may
ask the engine to start that layer's declared eval chain:

```json
{
  "schema_version": 2,
  "readiness_id": "readiness-...",
  "session_id": "current-session-id",
  "goal_hash": "sha256:...",
  "producer": {
    "workflow_id": "outer",
    "iteration": 12,
    "attempt_id": "current-attempt-id",
    "harness_run_id": "current-harness-run-id"
  },
  "request_eval_now": true,
  "accepted_evidence_refs": ["session:/..."],
  "rationale": "The scoped delivery is accepted; layer evaluation owns the remaining decision.",
  "created_at": "2026-07-17T12:00:00Z"
}
```

The assignment envelope supplies the exact absolute readiness directory plus
session/workflow/iteration/attempt identity. Team Harness's embedded caller
context supplies the current `harness_run_id`. The acceptance role chooses
whether to write the request. The engine does not parse `rationale` or inspect
the evidence references to decide whether the request is wise.

This is layer-local. A child acceptance role can wake only its child eval chain;
a child request cannot wake or close its parent. A parent planner can make a
separate request only for the parent layer.

### 2. Capture provenance while the producer attempt is current

`CoordinatorService._record_finished_task()` should examine a readiness request
only while its claimed producer is still the exact `CurrentTask`. It accepts
the optimization request only when:

- session id and goal hash match the current durable session;
- producer workflow, iteration, attempt, and harness run match the finishing
  task;
- the producer role equals the frozen workflow contract's
  `task_acceptance_role`;
- the record is valid JSON with the supported schema; and
- the producer attempt reaches final mechanical/protocol success.

The engine must not rescan readiness later and trust a record merely because it
names an old successful attempt. Under D8, a later agent can physically write
any session file; history proves that the old attempt existed, not that the
record existed when that attempt finished.

Malformed, mismatched, or multiply-claimed requests disable only the
optimization. They produce an explicit diagnostic and remain available for
inspection, but they do not change iteration success or stop the loop. Repair
means preserving that rejected artifact and publishing a fresh request from a
later accountable attempt; an immutable rejected request is never rewritten or
retroactively accepted.

### 3. Persist an engine-owned pending eval wake

After validation, the coordinator stores a compact `pending_eval_wake` in
`LoopState`, containing:

- readiness id, logical reference, and content hash;
- producer workflow, iteration, attempt, and harness run;
- target eval-author and eval-runner roles from the frozen contract;
- current chain step; and
- retry/dispatch facts needed for crash recovery.

The agent-authored readiness record remains compact session evidence. The
engine-owned state is the scheduling source of truth. Validation details,
duplicate candidates, and raw execution output stay in the attempt trace.

### 4. A pending wake accelerates; it does not gate

`CoordinatorService._advance()` keeps its existing precedence:

1. recover or serve a suspended child;
2. apply stop and terminal control;
3. dispatch a requested child after a successful parent assignment;
4. handle a pending eval wake; and
5. otherwise call normal `choose_next_workflow()`.

For the first chain step, the coordinator selects the declared eval-author role
without waiting for its `run_after_successes` bucket. It still honors:

- the frozen workflow set and contract;
- `enabled` and `not_before_iteration`;
- the single-worker/current-task invariant; and
- all session stop and child-suspension precedence.

No request means step 5 runs exactly as it does today. An invalid request also
falls back to normal scheduling. Therefore readiness is never a prerequisite
for evaluation or implementation.

### 5. Keep the wake through the complete eval chain

After the eval author succeeds, the pending wake advances to the declared
eval-runner step. The existing `must_follow` relationship remains the normal
ordering proof.

The wake is consumed only when the eval runner completes one of these
same-attempt protocol outcomes:

- a passing receipt and `goal_check.json`, together with applied same-attempt
  `goal_met` control, close the session through the existing D11 path;
- a valid non-passing verdict consumes the wake and normal scheduling resumes
  with that evidence available for repair; and
- a mechanical or protocol failure retains the wake so the eval-owned role can
  retry rather than forcing no-op implementation cycles.

Current control handling also permits a runner to publish a valid passing
receipt/projection while leaving `control.json` in its non-terminal
`state: running` placeholder. That does **not** consume the wake: the runner
step remains pending because the layer is neither terminal nor validly
non-passing. Its next attempt must publish a new same-attempt receipt,
projection, and control; D11 forbids reusing the prior attempt's receipt as if a
later attempt had produced it.

An immediate retry may bypass only the failed workflow's ordinary cadence
delay. Existing `goal_check_consecutive_failures_cap` handling and the
per-workflow consecutive-failure limit remain the bounded escapes from a
permanently broken eval workflow. This retry rule must be tested against the
case where a restart occurs after reviewer success but before runner dispatch.

The engine validates receipt/control structure and provenance exactly as it
does today. It still does not reinterpret the judge's semantic reasons.

## Why this is not a semantic scheduler gate

A gate says, “workflow X may not run until semantic fact Y is proven.” This
proposal says, “the accountable agent requested the already-configured
evaluation workflow now.”

The difference matters:

- absence of readiness does not block any existing path;
- invalid readiness falls back to mechanical cadence;
- false-positive readiness only spends an early eval attempt;
- a non-passing eval returns the system to autonomous repair; and
- only existing eval receipt/control validation can terminate the session.

The engine is transporting an agent decision across attempts, not making that
decision itself.

## Required decision amendment before implementation

D11 and its companion designs currently state that readiness is prompt context
and never scheduler input. Implementing this proposal without changing those
texts would silently contradict a binding decision.

If this proposal is accepted, amend D11 narrowly:

> Readiness may carry an identity-bound, additive request that accelerates the
> current layer's declared eval-author → eval-runner chain. It never proves
> semantic readiness, gates another workflow, or authorizes terminal control.
> Fixed mechanical cadence remains the fallback.

D8 need not change if the implementation remains additive as specified above.
If an implementation instead uses readiness to exclude, delay, or approve other
workflows, that would be a semantic scheduling veto and would require a more
fundamental D8 redesign.

The binding recursive-loop and success/evaluation designs, README, session
layout documentation, stock contracts, and prompts must change in the same
implementation PR as the decision amendment.

## State, traces, and observability

The fast path should emit compact events such as:

- `eval_wake_accepted`;
- `eval_wake_rejected` with a repairable reason;
- `eval_wake_dispatched`;
- `eval_wake_step_completed`; and
- `eval_wake_consumed`.

`loopy status` should show the active wake and current chain step. As a related
observability improvement, status should distinguish global iteration from
per-role run count, for example:

```text
current_task: inner (global iteration 19, inner run 6)
pending_eval_wake: none
```

This does not change scheduling, but it prevents operators from mistaking
iteration count for the number of implementation attempts.

### Related but separate: parent-plan discoverability

The parent session intentionally keeps the complete backlog in the versioned
roadmap and phase files rather than copying every work package into session
state. That avoids two competing plan authorities. However, agent-maintained
`project_state/current_state.md` and `child_sessions.md` can remain stale while
the parent is suspended behind a live child; only engine-owned `children.json`
is current during that interval. A separate state/observability proposal should
define one obvious engine-maintained overview that links to the canonical
roadmap and reports the selected item, active child, next-candidate/blocked
summary, and accepted/finished references without duplicating full work-package
descriptions. This proposal does not make that projection part of eval
scheduling.

## Failure and recovery cases

- **Crash after readiness capture:** `pending_eval_wake` survives in
  `LoopState`; restart dispatches the same logical next step.
- **Crash with an eval step live:** normal `CurrentTask` identity/liveness
  recovery applies. The wake is not consumed by abandonment alone.
- **Duplicate finish request:** readiness id/hash and attempt identity make
  capture idempotent.
- **Stale or forged readiness:** provenance validation rejects the optimization;
  normal cadence continues.
- **False semantic readiness:** the independent LLM judge fails and normal
  repair continues.
- **Eval author failure:** retain the author step and use existing failure
  accounting.
- **Eval runner transport/protocol failure:** retain the runner step; retry
  without manufacturing a receipt.
- **Passing receipt/projection without applied terminal control:** retain the
  runner step and require a fresh receipt/projection/control from its next
  attempt.
- **Valid non-passing eval:** consume the wake and expose the receipt/reasons to
  subsequent roles.
- **New child dispatch:** child-suspension precedence remains higher than the
  parent's pending wake; each layer keeps its own state.

## Rollout and tests

1. Decide whether to accept the narrow D11 amendment.
2. Add a typed readiness-v2 model and `pending_eval_wake` state model.
3. Capture requests in `_record_finished_task()` and add the wake transition
   before normal scheduling in `_advance()`.
4. Update both packaged workflow sets, assignment paths, prompts, status, events,
   README, and binding designs.
5. Test:
   - exact producer/session/goal matching;
   - wrong-role, stale, late-written, duplicate, and malformed requests;
   - false-positive requests leading to non-passing eval and repair;
   - unchanged fallback cadence without a request;
   - author and runner failure/retry;
   - passing receipt/projection with running control retaining the runner step;
   - crash recovery before and during both eval steps;
   - child/parent isolation at two and three durable depths;
   - one live worker/current task across the whole tree; and
   - no `goal_met` without the existing valid same-attempt eval receipt and
     control.

All new or touched functions must use named arguments at call sites where the
repository convention requires them and must have useful docstrings.

## Alternatives

### Run evaluation after every inner assignment

Changing `run_after_successes.every` from three to one is fully mechanical and
fits current D11. It removes most waiting but can multiply expensive judge runs
during normal implementation. A target repository can use it today when
latency matters more than eval cost.

### Retry a mechanically failed eval before normal workflows

A configurable scheduler preference for the latest mechanically failed eval
would solve the observed report-transport incident without semantic state. It
does not solve the broader case where accepted work should be evaluated before
the next fixed cadence bucket. It can complement the wake mechanism.

### Skip full evaluation for an unchanged subject

A layer-local subject fingerprint could avoid repeat judge calls when the goal,
checks, judge binding, git state, accepted evidence, and user-input cursor are
unchanged. This requires a new honest non-terminal “not evaluated/unchanged”
disposition; it must never reuse an old passing receipt or authorize
`goal_met`. It is a promising separate proposal, not part of the first wake
implementation.

### Let the engine infer readiness

Rejected. Parsing plans, PR status, tests, or accepted ledgers would make the
coordinator a semantic judge and create exactly the eligibility coupling D3,
D8, and D11 avoid.

### Let outer invoke eval-banana and close the layer

Rejected. It collapses task acceptance, check authorship, execution, and
terminal ownership into one role, weakening D4 and contradicting D11.

## Open questions

- Should runner retry remain pending until the existing failure cap, or should
  the workflow set declare a smaller immediate-retry count before returning to
  normal cadence?
- Should a newer valid readiness request supersede an older pending wake, or be
  recorded as a duplicate while the single current chain completes?
- Should readiness v1 remain prompt-only forever, with only v2 capable of
  requesting a wake? The safer migration answer is yes.
- Should per-role run counts be added in the same implementation or a small
  separate observability change?

## Primary implementation anchors

- `src/loopy_loop/config.py` — workflow and contract configuration
- `src/loopy_loop/models.py` — typed readiness and pending wake state
- `src/loopy_loop/coordinator_app.py` —
  `_record_finished_task()`, `_advance()`, goal-check and receipt validation
- `src/loopy_loop/scheduler.py` — bounded cadence bypass for a validated wake
- `src/loopy_loop/assignments.py` — absolute layer-local paths in assignments
- `src/loopy_loop/worker.py` — semantic prompt context
- `src/tests/test_scheduler.py` and
  `src/tests/test_template_contracts.py` — cadence behavior
- recursive session/recovery tests — durable layer isolation and crash windows

# Design: Orchestrator-Owned Completion, Layer Handoffs, and Cross-Harness Review

**Status:** Accepted and implemented

**Introduced in:** loopy-loop 0.8.0 and team-harness 0.5.4

**Date accepted:** 2026-07-17

**Applies to:** standalone `inner_outer_eval`, the
`pm_planner_dispatcher` double loop, future recursive loop layers, assignment
context, semantic state and handoff, optional evaluation, and team-harness
delegation.

This is the binding companion design for amended D3, D4, D6, and D8–D12 in
[`design/decisions.md`](../decisions.md). It supersedes the mandatory-eval and
eval-owned terminal-control parts of
[`recursive-loop-layer-contract.md`](./recursive-loop-layer-contract.md) and
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md).
The shipped protocol-v2 behavior remains documented for compatibility. The
loopy-loop 0.8.0 stock contracts explicitly select the v3 implementation;
the 0.7 stock contracts remain v2. Custom sets retain the protocol version they explicitly declare,
contract files that omit `session_protocol_version` remain pinned to the
historical v2 default, and sets with no contract remain derived v1. Only an
explicit v3 declaration selects v3.

## Summary

Each durable loop layer has one persistent **orchestrator**. That role owns the
layer's plan, integrates the work and reviews performed inside the layer,
communicates a semantic handoff upward, and decides when its own goal is
complete. For the stock workflow sets, the owner is `outer` in
`inner_outer_eval` and `planner` in `pm_planner_dispatcher`.

Evaluation is useful information, not a mandatory ceremony and not a second
orchestrator. An eval role may author or run checks on a schedule; the
orchestrator may invoke an eval directly, wait for a scheduled evaluator, ask
for another opinion, or decide from other evidence. When eval evidence is
produced or cited, its provenance remains strict. Its absence or failure does
not programmatically prevent the orchestrator from completing the session.

The system helps capable models make those decisions by giving every attempt:

1. absolute paths to the layer's canonical plan, tasks, current state,
   decisions, accepted-work ledger, optional eval index, and handoff;
2. a frozen roster of scheduled workflow roles and an attempt-frozen,
   conditional scheduler forecast; and
3. a frozen capability roster of all enabled harness families and their models
   through the semantic strength tiers `frontier`, `strong`, `standard`, and
   `economy`.

Prompts should prefer independent parallel analyses and review by a different
enabled harness family when that materially improves confidence. This
preference is strongest for eval-check creation, because weak or gameable
checks distort every later judgment. It is guidance, not a fixed graph, review
quota, model gate, or vendor requirement.

## Why the amendment is necessary

Protocol v2 solved real problems: it bound control and eval artifacts to the
correct session/goal/attempt, froze workflow contracts, separated durable state
from traces, and made recursive parent/child recovery explicit. Those
structural guarantees remain.

It also made one semantic mechanism universal. The current
`coordinator_app.py` terminal-control path requires a same-attempt passing eval
receipt and `goal_check.json`; both stock workflow contracts assign
`goal_met` to `eval_runner`; and `emits_goal_check` can turn missing evaluator
output into workflow failure. The PM set consequently carries another eval
reviewer/runner even though each child can already produce scoped evaluation.

That is too rigid for an agentic orchestrator:

- a direct review, repo-owned test suite, child handoff, delivery receipt, or
  prepared target-specific evaluation may be stronger evidence than the stock
  scheduled judge;
- a malformed advisory observation can starve the `outer` or `planner` role
  that actually understands the accumulated plan;
- a planner cannot avoid duplicate eval work even when it knows an eval role is
  about to run; and
- the parent has been pushed toward leaf-level dispatch because the child
  orchestration layer is not visibly carrying its own plan and handoff.

The correction is not to remove evaluation or provenance. It is to restore the
semantic decision to the durable orchestrator while giving that orchestrator
better state, schedule, capability, and review context.

## Hard protocol truth versus semantic judgment

The engine continues to enforce facts required for a trustworthy state machine:

- repository, root, session, workflow, and exact current-attempt identity;
- parent/child topology and single-deepest-assignment invariants;
- atomic transition and crash-recovery rules;
- schema validity, reference containment, and content hashes;
- truthful provenance for any cited evidence;
- explicit turn/cost/stop limits; and
- D5's identity-bound terminal blocker contract.

The engine does **not** decide:

- whether the current plan is the best plan;
- whether a milestone should be split or combined;
- whether an eval is needed or deserves more weight than another review;
- which enabled harness or strength tier must be used;
- how many reviewers constitute enough review; or
- whether the session's accumulated semantic evidence is sufficient.

Those are responsibilities of the named orchestration role. Detection stays
visible: a failing test, review, eval, or constraint observation should normally
lead to repair, rerouting, or a written disposition. It does not silently become
an engine-owned semantic veto.

## Layer roles and completion ownership

### Stock role contract

| Workflow set | Role | Durable responsibility |
| --- | --- | --- |
| `inner_outer_eval` | `outer` | Own the layer plan, select and accept leaves, integrate evidence, maintain handoff, and decide `goal_met` |
| `inner_outer_eval` | `inner` | Execute one bounded leaf selected from the layer plan and report evidence upward |
| `inner_outer_eval` | `eval_reviewer` | Optionally author/revise outcome-oriented eval checks and update the eval evidence index |
| `inner_outer_eval` | `eval_runner` | Optionally run checks and publish provenance-rich observations for the next outer attempt |
| `pm_planner_dispatcher` | `planner` | Own the high-level program plan, accept/reroute child outcomes, maintain root handoff, run target-requested final evidence, and decide `goal_met` |
| `pm_planner_dispatcher` | `dispatcher` | Faithfully turn one planner-selected milestone outcome into a child-session request |

`inner` never bootstraps or rewrites the layer plan when selection is absent.
It reports the missing/ambiguous leaf to `outer` and returns useful evidence;
`outer` repairs the plan on its next attempt. Workflow state accountability is
therefore declared per artifact with one owner and optional contributing roles,
not one flat `accountable_roles` list that makes every role appear to own every
file.

The stock PM set contains only `planner` and `dispatcher`. It does not repeat
the child's scheduled eval roles. The planner can dynamically delegate a
program-level review or evaluation through team-harness when useful, and a
target goal can explicitly ask it to run prepared final evaluations.

Future workflow sets declare the analogous orchestration role explicitly. The
engine must not infer completion ownership from a role name containing
`outer`, `planner`, or `eval`.

### `inner_outer_eval` is topology-neutral

The one-layer set must run unchanged in both forms:

- as a root session given a broad goal; and
- as a child session given a scoped parent request.

In both cases, its own `goal_contract.json` is authoritative, `outer` creates
and maintains its plan, inner attempts execute leaves, optional evaluators
produce observations, and outer writes the handoff and terminal decision.
Parent request/input artifacts are useful origin context when present; no
prompt may assume they exist. This invariant prevents the double loop from
quietly becoming a special mode that the one-layer system cannot reproduce.

### Child evidence never completes an ancestor

A child owns only its scoped goal. Its semantic handoff and terminal outcome
flow to the parent, which independently chooses `accepted`, `rework`, or
`reroute`. A child may correctly finish a phase while integration, release, or
later phases remain. The engine therefore records a factual child outcome but
never projects child `goal_met` into parent `goal_met`.

## Correct planning granularity in the double loop

The PM planner maintains a high-level program plan and dispatches a bounded,
coherent **outcome** that leaves meaningful planning work to the child outer
role. A phase, milestone, or integrated feature is usually the right unit. A
leaf instruction such as “execute exactly WP-0.1” usually is not.

For example:

| Layer | Appropriate goal |
| --- | --- |
| PM planner | “Make the development foundations ready and evidenced.” |
| Child outer plan | Reconcile the relevant design authorities, identify the remaining foundation work packages, sequence them, deliver coherent PRs, and verify the phase outcome |
| Inner leaf | Implement one selected schema migration or repair one failing contract test |

For Ultimate Memory, the rough phase spine (phases 0 through 8) is the natural
initial PM projection. The planner may split a phase that proves too broad,
combine tightly coupled outcomes, or reorder them when dependencies demand it.
That is planner judgment, not an engine-enforced size rule. The dispatcher must
preserve the selected outcome and its observable completion criteria instead of
pre-solving the child's decomposition.

Near overall completion, the Ultimate Memory goal can point the planner to
`plan/implementation_evals/` and instruct it to run the prepared final suite.
That requirement belongs to the target goal. It does not make evals mandatory
for every Loopy session or duplicate them in the PM scheduler.

## Canonical semantic state spine

Every fresh protocol-v3 session receives the same engine-created project-state
skeleton.
The files are compact, durable semantic state; raw agent conversations remain
in traces.

```text
project_state/
├── plan.md
├── tasks/
├── current_state.md
├── decisions/
├── finished.md
├── eval_state.md
└── handoff.json
```

| Path | Accountable writer | Meaning |
| --- | --- | --- |
| `plan.md` | layer orchestrator | Current decomposition, ordering, dependencies, and completion reasoning for this layer only |
| `tasks/` | orchestrator; inner contributes evidence | Stable per-leaf selections/status/evidence so plan prose is not the only ledger |
| `current_state.md` | layer orchestrator | Short resumption view: current outcome, active leaf/child, blockers, risks, and next decision |
| `decisions/` | layer orchestrator | Meaningful choices and rationale that later attempts must not rediscover |
| `finished.md` | layer orchestrator | Append-only accepted-work ledger with commit/PR/test/review references |
| `eval_state.md` | layer orchestrator; eval roles contribute observations | Optional index of check intent, observations, provenance, disagreements, and possible next eval action |
| `handoff.json` | layer orchestrator | Rolling semantic summary that can be consumed by a parent or an operator |

### Minimum inspectable plan and task shape

The engine scaffolds stable headings so an operator can find the plan without
teaching every prompt a new format. It does not parse the prose to choose work
or validate whether the plan is wise. `plan.md` starts with:

```markdown
# Layer Plan

- Revision: <monotonically increasing integer>
- Layer goal: <short outcome summary>
- Current milestone: <stable outcome ID or none>

## Outcomes

| ID | Outcome | Status | Dependencies | Evidence |
| --- | --- | --- | --- | --- |

## Active selection

<One selected leaf/task ID, why it is next, and its portable logical task ref.>

## Risks, assumptions, and replanning triggers
```

Each `tasks/<stable-id>.md` records the parent outcome ID, objective, current
status, dependencies, completion evidence expected, accepted evidence, and
remaining questions. Suggested statuses (`proposed`, `ready`, `active`,
`accepted`, `rework`, `superseded`) are shared language for agents and humans,
not engine states or scheduler gates. Only the layer orchestrator changes plan
revision, active selection, and semantic task status. Inner/delegate attempts
produce evidence for outer to integrate rather than racing those canonical
fields. The attempt assignment resolves the selected logical task reference to
the absolute path that `inner` reads.

### Prompt-level state lifecycle

The stock prompts make the file contract operational without asking the engine
to interpret it:

1. At attempt start, `outer`/`planner` reads its absolute goal, plan,
   `current_state.md`, accepted-work ledger, decisions relevant to the current
   milestone, rolling handoff, workflow roster, and scheduler view.
2. It inspects this attempt's child/delegate/review/test/eval/delivery evidence
   and makes the next semantic decision.
3. When facts changed, it updates the task ledger and plan revision, then writes
   a compact current-state resumption view and advances the rolling handoff
   before yielding. Atomic file replacement prevents half-written durable
   artifacts; semantic consistency remains its responsibility.
4. `inner` reads only the absolute selected-task path plus relevant goal/state
   context, performs that leaf, and reports evidence upward. It does not repair
   an absent plan itself.
5. `dispatcher` reads the planner-selected outcome, freezes the dispatch input,
   and transports it without inventing leaf decomposition.

No missing or weak plan heading makes a workflow engine-ineligible. The next
outer/planner attempt sees the omission and repairs it.

`eval_state.md` always has a path so prompts do not need topology branches, but
it may state that no evaluation has been created or run. Its emptiness is not a
protocol error. Protocol-v3 contracts retire the separate `eval_readiness/`
channel: any useful readiness/eval headline moves into `eval_state.md`, while
the conditional scheduler view explains when a scheduled eval role is due.
Frozen v2 sessions retain their historical readiness files and readers.

### Handoff contract

`handoff.json` is updated throughout the session, not improvised after the last
attempt. Its structurally required fields are identity and revision metadata;
its semantic lists remain flexible:

```json
{
  "schema_version": 1,
  "session_id": "<session>",
  "goal_sha256": "<goal hash>",
  "revision": 7,
  "producer": {
    "workflow_id": "outer",
    "attempt_id": "<attempt>"
  },
  "summary": "What this layer now believes and why.",
  "accepted_outcomes": [],
  "open_work": [],
  "risks": [],
  "decision_refs": [],
  "evidence_refs": [],
  "delivery_refs": [],
  "eval_refs": [],
  "updated_at": "<UTC timestamp>"
}
```

After each attempt, the engine retains the last provenance-valid handoff bytes
and hash. At any terminal transition it writes a topology-neutral
`session_outcome.json` together with the frozen terminal identity, optional
accepted control, goal, delivery, trace-seal, and optional eval references. A parent's
`child_outcomes/` record links that same outcome instead of synthesizing a
different story. A root operator sees the same result shape. Missing or stale
handoff is surfaced as factual incompleteness for review; it is not a hidden
semantic gate that overrides the orchestrator.

The terminal outcome resolves delivery evidence across the whole session, not
only the terminal-control attempt: implementation, PR creation, merge, and
completion synthesis normally occur in different attempts. Cited receipts and
the accepted-work ledger guide selection, and every projected receipt retains
its original attempt identity. A syntactically invalid `handoff.json` is
quarantined/diagnosed and represented as unavailable or invalid in the outcome;
it must not crash stack reconstruction or turn handoff into a control gate.

The orchestration role increments `handoff.json.revision` monotonically on each
accepted rewrite. The engine records the observed revision/hash and reports a
missing, malformed, or non-monotonic revision as completeness diagnostics; it
does not infer semantic staleness by parsing the summary or veto authentic
control.

The engine-owned terminal projection has a topology-neutral minimum shape:

```json
{
  "schema_version": 1,
  "session_id": "<session>",
  "root_session_id": "<root>",
  "goal_sha256": "<goal hash>",
  "lifecycle": "terminal",
  "terminal_status": "stopped | goal_met | failed | max_turns",
  "stop_reason": "<orchestrator control or engine lifecycle reason>",
  "terminal_state_revision": 13,
  "control": {"ref": "session:/control.json", "sha256": "<hash>"},
  "handoff": {
    "status": "valid | missing | invalid | non_monotonic",
    "ref": "<optional logical ref>",
    "sha256": "<optional hash>",
    "revision": 7
  },
  "fallback_summary": {
    "source": "control_reason",
    "text": "<exact control reason when no valid handoff exists>"
  },
  "evidence_refs": [],
  "delivery_refs": [],
  "eval_refs": [],
  "trace_seal_refs": [],
  "created_at": "<UTC timestamp>"
}
```

`control` is null for engine lifecycle stops such as `max_turns`,
`workflow_failure_cap`, or `stop_requested`. `fallback_summary` is not a
programmatic semantic synthesis. It copies the authenticated orchestrator's
control reason, or the factual engine stop reason when no terminal control
exists, so a parent is never handed a blank outcome when handoff is
unavailable. A valid handoff remains the richer semantic channel. Every
referenced artifact retains its own producer/attempt identity and hash.

The engine stores the accepted control and handoff bytes in durable state and
uses those snapshots whenever it regenerates the outcome. Mutable artifact
edits after terminal acceptance therefore cannot rewrite parent-visible
meaning. A later trace-finalization pass may only add trace-seal references.

## Absolute assignment paths

Durable records continue to use portable logical references, but every running
coordinator receives worker-local absolute paths. The assignment path map must
name at least:

```text
layer_goal
layer_goal_contract
layer_inputs
layer_plan
layer_tasks
layer_current_state
layer_decisions
layer_finished_ledger
layer_eval_state
layer_handoff
session_state
session_outcome
workflow_contract
workflow_roster
scheduler_view
harness_capability_roster
user_inputs
child_requests
children_index
child_outcomes
parent_acceptance
git_receipts
delivery_receipts
session_control
attempt_root
trace_root
```

For a child, `layer_inputs` is the immutable child-local copy, never the
parent's mutable source. Optional origin context uses stable keys
`parent_goal`, `parent_goal_contract`, `parent_handoff`, and
`accepted_child_request`; parent/child-only keys are explicitly null when the
topology makes them inapplicable. Active-child identity is read from the named
`session_state`/`children_index`, never reconstructed from directory names.
Prompts refer to Assignment keys and their rendered absolute values, never
infer state from cwd and never concatenate `.loopy_loop/sessions/...`
themselves.
Team-harness direct-spawn envelopes carry the delegate's own assignment/output
paths plus the relevant layer-state paths. A nested harness coordinator also
inherits the full capability roster and current layer identity; it remains an
attempt-local delegate rather than a durable child session.

## Workflow and scheduler awareness

The user must be able to inspect what the outer roles think will happen, and an
orchestrator must know which scheduled roles may produce evidence next. Two
different artifacts provide that context.

### Session-frozen workflow roster

`workflow_roster.json` is derived from the frozen workflow set and contains all
enabled scheduled roles, including:

- role ID and plain-language responsibility;
- cadence and ordering configuration (`run_every`, `must_follow`, and related
  mechanical settings);
- expected durable outputs and state accountability;
- orchestration completion and terminal-blocker authority; and
- optional eval author/runner responsibilities.

This roster is stable for the session. It is not the list of agents that a
team-harness coordinator will dynamically spawn.

Its minimum structural shape is:

```json
{
  "schema_version": 1,
  "session_id": "<session>",
  "workflow_contract_sha256": "<frozen contract hash>",
  "created_at": "<UTC timestamp>",
  "completion_role": "outer",
  "roles": [
    {
      "workflow_id": "outer",
      "responsibility": "<plain-language responsibility>",
      "cadence": {},
      "expected_outputs": [],
      "authorities": []
    }
  ]
}
```

### Attempt-frozen scheduler view

`scheduler_view.json` captures the current session phase, recent mechanical
history, cadence counters, and a conditional forecast:

> If this attempt returns normally and produces no terminal control, child
> request, stop condition, or harness failure, workflow X would be selected
> next for reasons Y.

The forecast states its assumptions and may include later due roles. It is not
a reservation or semantic eligibility rule. A new control signal, child
request, failure, user update, or recovery event can change the real next
selection. This information lets an outer orchestrator decide, for example,
not to duplicate an expensive eval when `eval_runner` is already due, while
leaving it free to run one immediately when evidence is urgent.

Its minimum structural shape is:

```json
{
  "schema_version": 1,
  "session_id": "<session>",
  "state_revision": 12,
  "attempt_id": "<current attempt>",
  "workflow_roster_sha256": "<roster hash>",
  "history_watermark": 19,
  "captured_at": "<UTC timestamp>",
  "conditional_forecast": {
    "next_workflow_id": "eval_runner",
    "reasons": ["<mechanical cadence reason>"],
    "assumptions": [
      "current attempt returns normally",
      "no terminal control or child request is accepted",
      "no stop, failure, user update, or recovery changes state"
    ]
  }
}
```

The scheduler computes this projection from the same frozen config and history
used for real selection. A null `next_workflow_id` is honest when no conditional
selection is currently derivable.

## Harness capability roster and strength tiers

`harness_capability_roster.json` is frozen from root execution configuration
for the session tree and rendered into every harness coordinator's prompt. It
contains no credentials. It records the common harness-coordinator
provider/model separately from the delegate catalog, then separates two worker
choices:

- **harness family**: a distinct enabled agent CLI/provider/tool ecosystem,
  useful for different perspectives and failure modes; and
- **strength tier**: the relative capability/cost bundle selected within that
  family.

The canonical stock tiers are:

| Tier | Intended use |
| --- | --- |
| `frontier` | Maximum-capability work: the hardest architecture/planning, adversarial review, ambiguous debugging, eval-policy/check design, and highest-stakes judging |
| `strong` | Complex reasoning, implementation, and review where high capability matters but the maximum tier is unnecessary |
| `standard` | Balanced default for ordinary implementation, analysis, and review |
| `economy` | Bounded mechanical work, broad reconnaissance, formatting, and low-risk checks |

The names describe relative strength across providers, not particular models.
For example, an Anthropic-family mapping could be `frontier` → Fable,
`strong` → Opus, `standard` → Sonnet, and `economy` → Haiku. Other harness
families map their corresponding models without changing stock prompts.

Config may omit an unavailable family/tier mapping, but the frozen roster
materializes every canonical tier for every enabled family and marks missing
cells explicitly as unavailable. A project may add a documented local tier.
The engine validates that configured mappings are well-formed; it does not
certify that a concrete model deserves its semantic label. Stock prompts use
canonical tier names and inspect availability. Model IDs and effort strings
occur only in config and the generated roster, never in stock role prompts.

An illustrative shape is:

```json
{
  "schema_version": 1,
  "root_session_id": "<root session>",
  "root_execution_config_sha256": "<frozen config hash>",
  "created_at": "<UTC timestamp>",
  "coordinator": {
    "provider": "<configured provider>",
    "model": "<configured strong coordinator model>"
  },
  "tiers": {
    "frontier": "maximum-capability configured bundle",
    "strong": "complex high-capability bundle",
    "standard": "balanced default",
    "economy": "lower cost and latency"
  },
  "harnesses": {
    "<family-a>": {
      "frontier": {"model": "<configured id>", "effort": "<configured>"},
      "strong": {"model": "<configured id>", "effort": "<configured>"},
      "standard": {"model": "<configured id>", "effort": "<configured>"},
      "economy": {"available": false}
    },
    "<family-b>": {
      "frontier": {"model": "<configured id>", "effort": "<configured>"},
      "strong": {"model": "<configured id>", "effort": "<configured>"},
      "standard": {"model": "<configured id>", "effort": "<configured>"},
      "economy": {"model": "<configured id>"}
    }
  },
  "default_tier": "standard"
}
```

Requested and effective family/model/effort stay in team-harness's audit and
trace records. They inform later review but never become a model-policy gate.

## Prompt contract for cross-harness collaboration

Every coordinator prompt should receive the following meaning, rendered
against the actual enabled roster:

> For consequential planning, design, uncertain analysis, review, or eval-check
> authoring, prefer independent delegates from different enabled harness
> families when that materially improves confidence. Run independent analyses
> in parallel when they do not depend on one another. After a primary artifact
> exists, prefer review by a family different from its primary author. Use the
> `frontier` tier for high-stakes review and eval-policy/check design when it is
> available and worth the cost. You own synthesis and the final layer artifact.
> These are judgment defaults, not quotas or completion gates; do not spawn
> agents merely to satisfy a count.

Important operational boundaries:

- parallel delegates write separate findings or trace outputs; they do not
  concurrently edit one canonical plan, check set, or implementation file;
- independent pre-draft analyses may run in parallel;
- reviewers run after a stable draft exists, though several reviewers can then
  review that draft in parallel;
- one implementation/integration owner incorporates changes and resolves
  disagreements; and
- if only one harness family is usable, proceed autonomously and record that
  limitation when it materially affects confidence.

This revives the useful part of the old “one harness works, another reviews”
pattern without restoring a hard-coded Codex/Claude/Gemini chain. The roster,
not vendor names in prompt text, determines what is available today.

### Role-specific guidance

| Role | Prompt preference |
| --- | --- |
| `inner` | Parallelize independent research; keep one integration owner; for meaningful changes prefer post-diff review by another enabled family |
| `outer` | Use cross-family analysis/review for high-impact plan changes, architecture choices, leaf acceptance, and completion synthesis when useful |
| `planner` | Seek independent views on program sequencing, cross-phase risk, child acceptance, and final completion when the confidence gain justifies it |
| `dispatcher` | Preserve planner intent; optionally cross-review a consequential child goal, but do not turn dispatch into a mandatory review ceremony |
| `eval_reviewer` | Apply the stronger eval-check authoring protocol below |
| `eval_runner` | Choose judge family/tier deliberately; where practical avoid the primary implementer's and check author's family, without inventing a quorum |

## Stronger collaboration for eval-check creation

Eval checks define what later evidence will notice and what it will miss. For a
non-trivial check set, the eval-reviewer coordinator should normally:

1. ask independent delegates from different available harness families to
   analyze goal coverage and likely failure modes in parallel;
2. have one accountable author/integrator draft a single coherent set of
   outcome-oriented `harness_judge` checks;
3. give the stable draft to different-family reviewers, in parallel where
   useful, asking them to attack:
   - missing goal coverage and redundant checks;
   - false-positive and false-negative paths;
   - implementation coupling and self-grading bias;
   - gameability and wording ambiguity;
   - evidence discoverability; and
   - whether a repo-owned prepared test/eval should also be cited;
4. reconcile disagreements, revise the canonical checks, and record the
   important rationale in `eval_state.md` or `decisions/`; and
5. leave candidate analyses and full reviewer transcripts in traces.

Use `frontier` delegates for this work when the goal is high-stakes or subtle
and the roster provides them. For a trivial check, unavailable second family,
or disproportionate cost, the coordinator may use fewer passes and say why
when that limitation matters. No exact agent count, all-family review, or
review receipt is required for check publication or session completion.

D4 remains unchanged at its trust boundary: the generic stock set authors
outcome-oriented LLM-judge checks, not agent-invented deterministic checks.
Repo-owned suites and prepared evaluations are legitimate additional evidence
because their criteria were not invented by the current implementer.

## Optional evaluation and terminal control

### Evaluation lifecycle

Eval roles may create checks and observations on mechanical cadence. Their
outputs update `eval_state.md` and, when valid, canonical eval receipts. The
outer orchestrator sees those facts on its next attempt. It may:

- repair the implementation in response;
- repair or supersede a weak/stale check;
- ask another family to review or rerun it;
- explain why the result does not apply;
- wait for an imminent scheduled evaluator rather than duplicate work; or
- decide from other evidence without running an eval.

Provenance validation remains strict whenever a receipt exists or is cited:
the receipt must bind the correct session and goal, exact check definitions,
producer and harness, judge settings, raw/canonical report hashes, and
evaluated git state. Its producing workflow must be one of the frozen
contract's `check_runner_roles`; validation is not hard-coded to a role named
`eval_runner`, because outer/planner may invoke an eval directly. Strict
provenance answers “what exactly produced this observation?” It does not answer
“must the orchestrator obey it?”

Raw-report validation happens when the engine accepts the canonical
session-side receipt: it verifies the available raw/canonical bytes and hashes,
records subject/evaluated-git identity, and seals the compact receipt. A later
cross-attempt control citation validates that accepted receipt, its subject,
producer role, evaluated git identity, and seal; it does not require the
gitignored raw trace bytes still to be retained. An unaccepted workflow-authored
receipt is not promoted merely because control cites it. This preserves D12's
independent trace-retention boundary.

`goal_check.json` remains readable as a legacy or optional iteration
projection. It is not the session stop switch. An absent, non-passing, stale,
or malformed observation is recorded as an eval diagnostic; it must not:

- change a normally returned `IterationResult.success` to false;
- consume a generic workflow-harness failure budget;
- increment a terminal-control protocol-failure counter;
- produce `goal_check_broken`; or
- prevent the completion owner from receiving another turn or writing control.

### Protocol-v3 successful control

Changing completion authority mid-session would make frozen provenance
ambiguous, so fresh amended workflow contracts use
`session_protocol_version: 3`. Every v2 session—already live or explicitly
created from a custom v2 contract—continues with its frozen v2 owners and
requirements.

A v3 `goal_met` signal has exact identity and a reasoned disposition. The
example shows the normal evidence and handoff references, but
`evidence_refs`, `eval_receipt_refs`, and `handoff_ref` are optional schema
fields; lists may be empty:

```json
{
  "schema_version": 3,
  "control_id": "<stable unique id>",
  "state": "stopped",
  "reason": "Why this session's own goal is now complete.",
  "stop_reason": "goal_met",
  "producer": {
    "session_id": "<current session>",
    "workflow_id": "<declared completion role>",
    "attempt_id": "<current attempt>"
  },
  "evidence_refs": [],
  "eval_receipt_refs": ["<optional logical eval receipt reference>"],
  "handoff_ref": "session:/project_state/handoff.json",
  "created_at": "<UTC timestamp>"
}
```

The engine rejects stale identity, a producer other than the frozen completion
role, malformed references, or false provenance. It does not require an eval
reference, a passing verdict, or a same-attempt eval. If receipts are cited,
their bytes and subject identity must validate; the orchestrator's rationale
may explain how conflicting evidence was resolved. `handoff_ref` is expected
and validated when present, but its absence or staleness is reported as outcome
completeness information rather than used to invalidate otherwise authentic
control.

V1/v2 keep the historical singular `eval_receipt_ref`; v3 deliberately adds
plural `eval_receipt_refs` because an orchestrator may weigh independent or
conflicting observations across attempts. A malformed advisory eval by itself
is only an eval diagnostic. A `control.json` that affirmatively cites a stale,
foreign, or malformed receipt makes a false protocol claim and is rejected as
control; the orchestrator can repair the references or submit a new disposition
without the bad citation.

The v3 `unresolvable_error` form keeps D5's attempted-routes and evidence
requirements and does not require a handoff or eval. There is still no
`paused`/`waiting_for_human` state.

### Workflow contract shape

Completion ownership moves out of the eval sub-contract:

```yaml
session_protocol_version: 3
orchestration:
  completion_role: outer
  plan_owner: outer
  handoff_owner: outer
  task_acceptance_owner: outer
terminal_blocker_reporting_roles: [outer, inner, eval_reviewer, eval_runner]
evaluation:
  advisory: true
  check_author_roles: [eval_reviewer, outer]
  check_runner_roles: [eval_runner, outer]
```

Allowing `outer` in the eval roles is accountability for an outer attempt that
directly coordinates or delegates evaluation; it does not assert that the same
underlying model both implemented and independently judged the work. D4 forbids
invented deterministic gates and the prompts strongly prefer cross-family
check analysis/review and a judge with different failure modes. Independence is
visible in the delegate/judge provenance and weighed by outer—it is not enforced
by preventing the durable orchestrator from obtaining evidence when a scheduled
eval role is unavailable.

The PM contract names `planner` in the orchestration fields and omits scheduled
eval workflows, while declaring `planner` as a permitted check runner (and
author when it creates target-specific checks) for directly coordinated,
delegated, or prepared program-level evals:

```yaml
orchestration:
  completion_role: planner
  plan_owner: planner
  handoff_owner: planner
  child_acceptance_owner: planner
terminal_blocker_reporting_roles: [planner, dispatcher]
evaluation:
  advisory: true
  check_author_roles: [planner]
  check_runner_roles: [planner]
```

The exact schema may normalize repeated owners internally, but the top-level
semantic distinction is required: evaluation produces evidence; orchestration
decides. Declaring planner as a runner authorizes provenance; it does not add a
scheduled PM eval role.

## State versus traces

The compact session state contains facts needed to resume, schedule, or explain
the result:

- goal, topology, plan, task/accepted-work ledger, and decisions;
- workflow roster, scheduler view, and harness capability roster;
- current-state and semantic handoff;
- child outcomes and parent dispositions;
- completion rationale;
- eval headline and receipt links when present; and
- git, delivery, recovery, and trace-seal receipts.

The gitignored trace tree contains full prompts, visible turns, tool/spawn I/O,
independent candidate analyses, reviewer transcripts, raw eval reports,
process/provider identity, verbose git evidence, timing, and usage. A
coordinator must promote conclusions future attempts need into compact state.
It must never expect a later outer/planner attempt to read megabytes of raw
reports or reconstruct the plan from chat history.

## Implemented protocol changes

The 0.8.0/0.5.4 implementation delivers the following coordinated protocol
amendment. Its release validation covers the changed stock workflow defaults.

1. **Models and workflow contracts**
   (`src/loopy_loop/models.py`, workflow contract loader): add protocol v3,
   top-level orchestration ownership, optional eval roles, v3 control, roster,
   scheduler-view, handoff, and terminal-outcome schemas. Preserve frozen v1/v2
   readers. Audit every `session_protocol_version >= 2` branch and replace it
   with explicit version dispatch where v2 and v3 semantics differ; v3 must
   never enter `_validate_v2_control` or other v2 eval-gate paths. Preserve the
   current compatibility rule that a contract file omitting
   `session_protocol_version` defaults exactly to v2; do not let the new stock
   default silently upgrade those custom contracts.
2. **Session creation and state** (`sessions.py`): create the canonical semantic
   spine, session-frozen workflow roster, and session-tree capability roster;
   use atomic writes and logical references.
3. **Assignments** (`assignments.py`, `coordinator_app.py`): add the named
   absolute semantic paths—including child-local `layer_inputs` and stable
   optional parent-context keys—roster paths, structured roster summaries, and
   attempt-frozen scheduler view to `assignment.json` and rendered prompts.
4. **Scheduler context** (`coordinator_app.py`): expose mechanical history and a
   conditional forecast without making it an eligibility promise or semantic
   gate.
5. **Terminal control** (`coordinator_app.py`): authorize the top-level
   completion role; remove the universal same-attempt passing receipt and
   `goal_check.json` prerequisite for v3; validate any cited evidence
   provenance; snapshot valid `handoff.json` into `session_outcome.json`; treat
   absent/malformed handoff as completeness diagnostics; and resolve delivery
   receipts across all session attempts rather than only the control attempt.
6. **Advisory eval handling** (`worker.py`, `coordinator_app.py`): keep valid
   receipts, but record missing/malformed/non-passing output as diagnostics
   rather than mechanical failure, `goal_check_broken`, or terminal starvation.
   Validate receipt producers against the frozen `check_runner_roles`, not a
   hard-coded `eval_runner` role.
7. **Stock one-layer prompts/contracts**
   (`templates/inner_outer_eval/`): make outer the completion/handoff owner;
   retain optional scheduled eval roles; make the set standalone-safe; render
   the workflow/scheduler/capability context and cross-harness preferences.
   Remove inner's “create a plan if absent” bootstrap; inner reports a missing
   selection to outer. Replace flat state `accountable_roles` with per-artifact
   owner/contributor metadata. Retire v3 `eval_readiness/` and project any
   useful headline into `eval_state.md` plus scheduler context; preserve the
   frozen v2 reader.
8. **Stock PM prompts/contracts** (`templates/pm_planner_dispatcher/`): ship
   planner plus dispatcher only; dispatch phase/milestone outcomes; make planner
   own completion/handoff and target-requested final evaluations.
9. **Capability config** (`config.py`): freeze the full matrix rather than only
   resolved defaults/prose; use canonical tier semantics; render missing cells;
   keep selection guidance/audit-only. Replace arbitrary stock tier examples
   with complete `frontier`, `strong`, `standard`, and `economy` examples
   without silently rewriting custom configs.
10. **Team-harness boundary**: carry the roster path/summary through nested
    caller context and direct-spawn assignments; keep requested/effective
    family/model/effort in traces. Change team-harness only where Loopy cannot
    supply this through its existing caller/system-prompt contract.
11. **Version/capability handshake** (`coordinator_app.py`, `worker.py`): add
    explicit protocol-v3 worker capabilities for the new assignment/control
    contract and fail registration with HTTP 426 before dispatch when any are
    missing. A v2-capable worker is not implicitly v3-capable.
12. **Docs and target setup**: update `README.md`, `docs/session-layout.md`, and
    `docs/http-contract.md` after the code lands. Update Ultimate Memory's PM
    plan to phase-sized outcomes, render the prepared final-eval path in its
    planner goal, and use the standardized harness tiers.

## Verification scenarios

The amended release is not complete until tests prove:

1. a fresh standalone root `inner_outer_eval` creates and advances its own plan,
   handoff, and terminal outcome without parent-only assumptions, while inner
   never creates/owns the plan when selection is missing;
2. the same set behaves identically as a child;
3. outer can complete with a valid eval, without any eval, and with a diagnosed
   non-passing or malformed advisory eval;
4. optional receipts from any declared runner role are provenance-validated
   across attempts, while stale/sibling/foreign or undeclared-role receipts are
   rejected as evidence;
5. a PM session exposes only planner and dispatcher, sends a phase/milestone
   goal, and lets the child outer decompose it into leaves;
6. parent acceptance consumes the child's handoff/outcome but never closes the
   parent automatically;
7. workflow roster and conditional scheduler view are inspectable and frozen at
   the documented boundaries;
8. every coordinator sees all enabled harness families and configured
   `frontier`/`strong`/`standard`/`economy` cells, with no hard-coded
   vendor/model IDs in stock role prompts;
9. eval-check prompts request parallel independent criteria analysis and
   different-family review as a preference, while single-family/no-review paths
   remain valid;
10. parallel delegates are assigned separate findings/trace outputs and one
    coordinator owns integration; any contrary concurrent canonical edit is
    observable for repair rather than claimed impossible by an ACL;
11. a valid terminal handoff is hash-bound into the same outcome shape for root
    and nested sessions, while missing/malformed handoff is diagnosed without a
    crash or control veto and earlier-attempt delivery receipts remain linked;
12. already-live v2 sessions resume under their frozen v2 authority and eval
    requirements, v3 never enters a v2-only validation branch, and v3
    `eval_readiness/` is retired without breaking v2 readers; and
13. a worker missing any required v3 capability receives HTTP 426 before a v3
    assignment is mutated or dispatched.

The coordinated release gate remains formatting, lint, type checking, and the
full relevant suites in loopy-loop and any changed support repositories.

## Independent design review

Claude Code and Antigravity independently reviewed the amendment, identified
concrete compatibility/state-path/provenance gaps, and re-reviewed the revised
documents. Both final passes returned **PASS** with no remaining design
blocker. The findings and maintainer dispositions are recorded in the
[Claude Code review](../analysis/claude-code-orchestrator-owned-completion-review.md)
and
[Antigravity review](../analysis/antigravity-orchestrator-owned-completion-review.md).
These verdicts cover the design only. The implemented 0.8.0/0.5.4 changes still
require their own code review and coordinated release evidence before the tags
are published.

## Alternatives rejected

**Keep eval-runner as completion owner.** This confuses an evidence producer
with the persistent role that understands the plan and makes one generic eval
shape mandatory.

**Remove evaluation.** Independent outcome judgment remains valuable. The
amendment changes authority and cadence, not the availability or provenance of
evaluation.

**Let both outer and eval-runner write success.** Dual terminal authority is
ambiguous and race-prone. One declared orchestrator decides after considering
all available evidence.

**Hard-code an author/reviewer vendor chain.** It becomes stale, fails when one
provider is unavailable, and prevents the coordinator from adapting. Use the
enabled capability roster and semantic preferences.

**Require two reviewers or all harness families.** Review value is
task-dependent; a quota wastes cost and becomes another gate. Preserve the
preference and audit trail, not a count.

**Allow parallel agents to edit the canonical artifact together.** Independent
analysis parallelizes well; racing writes do not. One coordinator/integrator
owns the durable artifact.

**Make the scheduler forecast a promise.** Terminal control, child dispatch,
failures, stops, and recovery can legitimately change the next workflow. A
conditional view provides useful context without lying.

**Dispatch exact leaves from the PM layer.** This hides the real plan from the
child outer role, bloats parent context, and makes the one-layer orchestrator a
mere executor. Dispatch coherent outcomes and let each layer plan its own goal.

**Put semantic plans and handoffs only in traces.** Traces are large,
gitignored, and independently retained. Continuity needs compact state that can
be read without reconstructing conversations.

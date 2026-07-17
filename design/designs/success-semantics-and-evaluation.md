# Design: Mechanical Success, Orchestrator Completion, and Evaluation Evidence

**Status:** Accepted and implemented. D3's mechanical-success behavior is
released; the protocol-v3 orchestrator-owned completion amendment is
implemented in loopy-loop 0.8.0 and team-harness 0.5.4. V2 sessions retain
their frozen historical contract.

**Date recorded:** 2026-07-12

**Date amended:** 2026-07-17

**Applies to:** the loopy-loop coordinator/worker boundary, terminal control,
and optional evaluation in packaged workflow sets.

This document is the binding companion for D3 and D4 in
[`design/decisions.md`](../decisions.md). The complete recursive role, state,
handoff, schedule, and cross-harness contract lives in
[`orchestrator-owned-completion-and-cross-harness-review.md`](./orchestrator-owned-completion-and-cross-harness-review.md).

The shared principle is:

> Do not infer semantic success from noisy mechanical signals. Give one named
> durable orchestrator the relevant evidence and responsibility to decide.

Evaluation can be excellent evidence. It is not the universal owner of that
decision.

## Decision 1 — Iteration success means “the assignment ran,” not “the work is good”

### Decision

`IterationResult.success` is `True` whenever a `team-harness` invocation
returns normally and `False` only when the harness itself raises. It is not a
judgment about whether the requested work was accomplished. Per-worker status
and exit codes in `TeamHarnessResult.agents` are intentionally not converted
into that boolean.

Reference: `src/loopy_loop/harness_runner.py` —
`_normalize_harness_result()` returns `success=True`; the `success=False` paths
are exception handlers in `run_harness_iteration()`.

Semantic completion is a separate, explicit act. Each workflow contract names
one persistent orchestration role. That role writes identity-bound
`control.json` when it judges its session goal complete:

- `outer` for `inner_outer_eval`; and
- `planner` for `pm_planner_dispatcher`.

The orchestrator may consider its plan and accepted-work ledger, implementation
evidence, repo-owned tests, direct reviews, child outcomes, decisions, git and
delivery receipts, and optional eval observations. No one evidence type is a
generic protocol prerequisite.

### Why mechanical success stays narrow

`team-harness`'s coordinator is an orchestrator, not a build system. It can
legitimately return a useful synthesis after one delegate fails, route around a
dead worker, or decide that another delegate supplied enough evidence.
Conversely, every delegate can exit zero while producing useless work. Mapping
worker exits to “the task is good” would manufacture false precision.

Loopy can reliably observe whether the harness invocation itself completed.
It records that mechanical fact and leaves the semantic decision to the role
that has durable knowledge of the goal and plan. This behavior dates to the
first `harness_runner.py` implementation (`a4cca5e`, 2026-04-19); the amendment
changes who owns semantic completion, not mechanical success.

### Consequences

- Scheduler cadence (`run_every`, `must_follow`, and
  `run_after_successes`) continues to use mechanical history.
- A delegate's non-zero exit can coexist with a mechanically successful
  attempt, because the coordinator may still have integrated useful work.
- A missing, non-passing, stale, or malformed advisory eval is recorded as
  evidence/diagnostics. It does not retroactively flip `HistoryEntry.success`,
  consume the generic harness-failure budget, or starve the orchestrator.
- Crash recovery continues to trust a matching locally persisted result; it
  does not reconstruct success from raw worker streams.
- `control.json`, written by the exact current completion owner, is the sole
  semantic stop request. Evidence artifacts do not stop the loop by
  themselves.

### Protocol integrity is different from semantic judgment

The engine still rejects a stale or sibling control record, a producer that is
not the frozen completion owner, malformed paths, invalid topology, or false
evidence provenance. Those are claims about the durable protocol, not opinions
about work quality.

For protocol v3, successful control contains exact current
session/workflow/attempt identity and a non-empty rationale. Evidence references
are optional and may be empty; asserted references are validated. Eval receipt
references are optional and may come from an earlier
attempt in the same session. If cited, their subject identity and hashes must
validate and their producer must be a runner role declared by the frozen
contract. The engine does not require a passing verdict or reinterpret the
orchestrator's weighting of conflicting evidence.

Protocol-v2 sessions retain their frozen same-attempt eval requirements,
including a session explicitly created later from a custom v2 contract. Their
authority is never silently reinterpreted as v3.

### Alternatives rejected

**Derive success from delegate exit codes.** This creates both false negatives
(one delegate failed but the coordinator recovered) and false positives (all
delegates exited cleanly but the outcome is wrong).

**Make an evidence artifact stop the loop automatically.** Evidence and
decision are different responsibilities. The persistent orchestrator must
integrate the evidence and leave a reasoned terminal disposition.

**Let both orchestrator and evaluator write success.** Dual authority creates
races and ambiguity. Evaluation informs one owner; it does not become another
owner.

## Decision 2 — When evaluation is used, stock checks are outcome-oriented LLM judgments

### Decision

The generic packaged eval workflow authors only outcome-oriented
`harness_judge` checks. It does not invent deterministic checks. This boundary
applies when an orchestrator chooses to use evaluation; it does not require an
eval run and does not grant an evaluator terminal authority.

Reference: the `eval_reviewer` prompt under
`src/loopy_loop/templates/inner_outer_eval/.loopy_loop/workflow_sets/inner_outer_eval/workflows/`.

### Why agents do not invent deterministic checks

This rule comes from observed failure. Agent-authored deterministic checks
became brittle string matches, targeted the wrong behavior, passed for the
wrong reason, or were easy for the implementer to game. Allowing the current
implementer to invent its own machine-enforced pass criteria let it redefine
“done” around what it had already produced.

An outcome-oriented judge instead states what good behavior looks like in
natural language and evaluates the evidence against that description. It has
non-determinism and model-trust costs, but avoids pretending that a weak
agent-invented script is an objective contract.

### Important boundary: repository-owned checks are legitimate evidence

The rejected category is **agent-invented pass/fail logic**, not deterministic
testing in general. Running an existing repository-owned command such as
`pytest`, `import-linter`, `alembic upgrade`, or a prepared implementation-eval
suite does not have the same self-grading failure mode. The project established
those criteria independently of the current implementation attempt.

A target-specific workflow may therefore combine:

- qualitative `harness_judge` observations;
- repository-owned deterministic tests; and
- prepared project-level evals.

All are evidence for the orchestrator. None becomes a universal Loopy engine
gate merely because it is deterministic or expensive.

### Eval-check authoring needs stronger independent review

Eval definitions are high-leverage artifacts: missing, overlapping, ambiguous,
or gameable checks distort every later observation. For a non-trivial check set,
the eval-reviewer coordinator should normally:

1. run independent goal-coverage and failure-mode analyses in parallel across
   different enabled harness families when available;
2. assign one accountable author/integrator to draft the canonical checks;
3. ask a different family to review the stable draft, with parallel reviewers
   where useful;
4. explicitly test coverage gaps, false positives/negatives, implementation
   coupling, gameability, wording ambiguity, and evidence discoverability; and
5. synthesize disagreements into one coherent outcome-oriented check set.

Use the session's `frontier` tier for subtle or high-stakes eval-policy/check
work when the capability roster offers it and the confidence gain justifies the
cost. Concrete model IDs and enabled families come from the frozen roster, not
from hard-coded stock prompt text.

This is strong prompt guidance, not a required number of agents, a fixed vendor
graph, an all-family quorum, or a completion receipt. If only one family is
usable or the check is trivial, the coordinator proceeds autonomously with an
appropriate smaller review shape.

### Running and interpreting evals

- Prefer a judge whose harness/model family differs from the primary
  implementer and check author where practical.
- The eval runner publishes a provenance-rich observation. It never writes the
  session's successful terminal control in the amended contract.
- A scheduled eval may run before, during, or near the end of work. The
  orchestrator can also invoke one directly or avoid duplicate work when the
  scheduler view shows an eval role is about to run.
- A passing judge result is evidence, not proof. A failing result is also
  evidence, not an unconditional veto. The orchestrator repairs, reruns,
  supersedes, explains, or weighs it against other facts.
- When an eval receipt is produced or cited, exact subject and provenance
  validation remains mandatory even though semantic deference is not.

### Alternatives rejected

**Let agents invent deterministic checks.** Rejected from experience: the
checks were brittle, wrong-target, and gameable.

**Run deterministic-first everywhere.** This is appropriate when the target
already owns a trustworthy suite, but not as a blanket rule for generic repos
where the current agent would have to invent the gate.

**Require an eval before every completion.** The persistent orchestrator can
have stronger direct evidence, a target may already own better checks, and a
scheduled evaluator can fail for reasons unrelated to work quality.

**Restore a hard-coded author/reviewer model chain.** Cross-family review is
valuable, but concrete providers and models change. The enabled harness/tier
roster supplies current choices and the coordinator adapts.

## Compact summary

| Question | Binding answer |
| --- | --- |
| What does `IterationResult.success` mean? | The harness invocation returned normally |
| Who decides the session goal is complete? | The workflow contract's persistent orchestrator (`outer` or `planner` in stock sets) |
| Is evaluation required? | No; it is optional evidence unless a target's own goal asks for a particular eval |
| What stops the session? | Exact-current-attempt, identity-bound `control.json` from the completion owner |
| What happens to bad eval output? | It becomes visible diagnostic evidence, not mechanical failure or a universal gate |
| What kind of stock checks may agents author? | Outcome-oriented `harness_judge` checks, not invented deterministic gates |
| How should non-trivial eval checks be designed? | Prefer parallel independent cross-family analysis, one integrator, and different-family review |
| Are review diversity and model tiers enforced? | No; the roster informs prompt-guided, audited coordinator judgment |

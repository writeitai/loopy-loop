# Design: Success Semantics and Evaluation Strategy

**Status:** Accepted (documenting decisions already in the codebase)
**Date recorded:** 2026-07-12
**Applies to:** `loopy-loop` coordinator/worker loop and the packaged
`inner_outer_eval` workflow set.

This document records two design decisions that are **deliberate and load-bearing**,
but were until now implicit in the code rather than written down. A reader skimming
the code can easily mistake each for a defect. They are not defects. This document
exists so the next reader — human or agent — does not "fix" them by accident.

Both decisions share one principle:

> **Do not infer semantic success from noisy mechanical signals. Push the
> success/acceptance decision to an explicit, purpose-built evaluation layer.**

D11 and its companion
[`recursive loop-layer contract`](./recursive-loop-layer-contract.md) refine
how that evaluation layer composes across session depths: each session evaluates
its own goal and names one terminal goal-control owner. This document remains
authoritative for D3/D4's mechanical-success and LLM-as-judge boundaries; the
new design adds subject provenance and ownership without changing either.

---

## Decision 1 — Iteration success means "the assignment ran," not "the work is good"

### Decision

`IterationResult.success` is `True` whenever a `team-harness` run returns normally,
and `False` only when the harness itself raises (`ConfigError`, `TeamHarnessError`,
or an unexpected exception). It is **not** a judgment about whether the requested
work was actually accomplished. `TeamHarnessResult.agents` (per-worker statuses and
exit codes) is intentionally **not** consulted to decide iteration success.

Reference: `src/loopy_loop/harness_runner.py` — `_normalize_harness_result()`
returns `success=True`; the `success=False` paths live only in `run_harness_iteration()`'s
exception handlers.

**Semantic success is decided elsewhere**, by artifacts the workflow writes:

- `control.json` — the session stop switch (`running` → `stopped` with a
  `stop_reason`). In a fresh v2 session it must identify the exact current
  session/workflow/attempt; successful control comes from the declared
  goal-control role and cites the same-session eval receipt. This, and only
  this, stops the loop.
- `goal_check.json` — a per-iteration projection of the canonical eval receipt.
  Evidence only; a valid `goal_check.json` does **not** by itself stop the loop.

### Context / why

`team-harness`'s coordinator is an orchestrator, not a build system. It can
legitimately return a normal result after a worker has failed — it may synthesize a
final answer, decide it has enough information, or route around a dead worker. Worker
exit codes are therefore a **noisy** proxy for "did the assignment succeed": a
non-zero worker can accompany a perfectly good outcome, and an all-green set of
workers can accompany a useless one. Mapping those signals to a boolean would
manufacture false precision.

So `loopy-loop` draws the line at the only thing it can observe reliably — *did the
assignment run to completion without the harness itself erroring* — and delegates the
"was it any good" question to an explicit evaluation step that produces
an eval receipt and matching `goal_check.json`, with workflow-owned
`control.json` as the actual gate. D11 defines the exact role and provenance
contract; D5 keeps human involvement out of normal operation.

This has been the behavior since the first commit of `harness_runner.py`
(`a4cca5e`, 2026-04-19); it is original design intent, not drift.

### Consequences

- **The evaluation layer is the real arbiter of completion**, not the harness return
  value. Everything downstream depends on that layer being run and being honest
  (see Decision 2, and the "known limitation" below).
- **The scheduler keys cadence off mechanical success.** `run_every`, `must_follow`,
  and `run_after_successes` all read `HistoryEntry.success`. A run where a worker
  actually failed but the harness returned normally still advances these counters.
  This is an **accepted, bounded inaccuracy**: `control.json`/`goal_check.json` remain
  the true gates, so the worst case is a slightly-off cadence, not a false "goal met."
- **Crash recovery treats a locally-written result as authoritative.** The
  `pending_finished_request.json` / `result.json` recovery path trusts the recorded
  iteration result; it does not re-derive success from worker artifacts.

### Known limitation (documented, not a call to revert)

Because acceptance for an entire iteration ultimately rests on the evaluation layer,
and that layer is LLM-as-judge by design (Decision 2), a whole iteration's
"success" can rest on a single model judgment with no deterministic backstop. For
low-stakes goals this is an acceptable, conscious trade. For high-stakes work it
should be **backstopped**, not reverted — see the note in Decision 2 about
repo-owned deterministic checks and the active
[`P1.2` proposal](../proposals/improvement-proposals.md#p12--target-owned-deterministic-evaluation-backstop).

### Alternatives considered and rejected

- *Derive iteration success from worker exit codes / `result.agents`.* Rejected:
  unreliable for the reasons above; produces false negatives (good outcome, failed
  worker) and false positives (all-green, useless outcome).
- *Make `goal_check.json` directly stop the loop.* Rejected: conflates evidence with
  control. Keeping `control.json` as the sole stop switch means the accountable
  current workflow must make an explicit, auditable stop decision. A human gate
  is not part of this path (D5).

### When to revisit

If cadence inaccuracy causes a concrete problem, tune the workflow set's
mechanical eval frequency and the evidence rendered into eval prompts. Do not
make semantic readiness or an accepted eval determine workflow eligibility:
D8 forbids semantic scheduling gates, and D11 keeps readiness as prompt context.
Accepted eval affects terminal control, not which assignment may run next.

---

## Decision 2 — Evaluation is LLM-as-judge; agents do not author deterministic checks

### Decision

In the packaged `inner_outer_eval` workflow set, the eval workflows create **only**
`harness_judge` (LLM-as-judge) checks that describe *desired outcomes*. Authoring
deterministic checks is explicitly forbidden in the stock template.

Reference:
`src/loopy_loop/templates/inner_outer_eval/.loopy_loop/workflow_sets/inner_outer_eval/workflows/eval_reviewer/prompt.txt`
— "Only create harness_judge checks"; "Do not create deterministic checks.
Deterministic checks are forbidden."

### Context / why

This rule comes from direct experience, not theory. When agents were allowed to
**author** deterministic checks, they produced bad ones: brittle string-matching,
checks that tested the wrong thing, checks that passed for the wrong reason, and
checks an agent could trivially satisfy without doing the real work. In practice,
letting the implementer invent its own pass/fail criteria let it game itself.

`harness_judge` on a described *outcome* removes that failure mode: the check states
what good looks like in natural language, and a judge evaluates against it. The
implementer cannot quietly redefine "done" into something it already produced.

### Scope and boundary (important — read before applying to other repos)

The thing that failed was **agent-authored** checks, not deterministic checks as a
category. Two very different things get conflated under "deterministic check":

- **Agent invents a check** → the failure mode above. Correctly forbidden.
- **Run a check the repo already owns** → e.g. `uv run pytest`, `import-linter`,
  `alembic upgrade`, `make test`, evaluated on exit code. The agent did not invent
  these; they are the project's own contract. Running them is deterministic but is
  **not** the failure mode this rule targets.

Therefore the "deterministic forbidden" rule is correct **for generic target repos
where the only deterministic checks would be agent-invented**. For a target repo that
already owns a trustworthy contract-test suite, the right configuration is *both*:
LLM-as-judge for the qualitative "did this achieve the outcome," **and** a
deterministic gate that shells out to the repo's own suite as a backstop under the
judge. That backstop does not reintroduce the agent-authoring problem, and it removes
the single-judgment point of failure noted in Decision 1. A workflow set targeting
such a repo should override the stock rule accordingly, in a dedicated child workflow
set rather than by loosening the stock template.

### Consequences

- **Evaluation is outcome-focused and resistant to self-gaming**, at the cost of the
  usual LLM-as-judge properties: non-determinism, per-check inference cost, and the
  judge as a point of trust.
- **The judge should not share failure modes with the implementer.** Prefer judging
  with a different model family than the one that implemented the change.
- **A single judge pass is evidence, not a hard gate for high-stakes stops.** Keep
  `control.json` as the stop switch (Decision 1); for high-stakes goals, require
  repeated/independent judgments or a deterministic backstop before a terminal
  `goal_met`.

### Alternatives considered and rejected

- *Let agents author deterministic checks (the prior state).* Rejected on evidence:
  produced nonsensical, gameable checks.
- *Deterministic-first everywhere, judge as residual (the "obvious" best practice).*
  Rejected **as a blanket rule** because in generic repos the only deterministic
  checks available are the agent-authored ones that failed. It is the *right* rule
  only where the deterministic checks are repo-owned (see boundary above).

### When to revisit

Revisit per target repo, not globally: when a target owns a trustworthy contract-test
suite, add the deterministic backstop (do not remove the judge). When judge cost or
flakiness becomes material, add repetition/consensus and cross-family judging rather
than abandoning the approach.

---

## Summary

| | Decision 1 | Decision 2 |
|---|---|---|
| **What** | Iteration success = harness completed, not work-is-good | Eval = LLM-as-judge on outcomes; agents don't author deterministic checks |
| **Why** | Worker exit codes are a noisy proxy for real success | Agent-authored deterministic checks were gameable nonsense |
| **True gate** | identity-bound `control.json` (stop) + eval receipt/`goal_check.json` (evidence) | The judge's verdict, recorded as evidence |
| **Shared principle** | Delegate the success decision to an explicit eval layer | Same |
| **Backstop for high-stakes** | Run eval on a suitably frequent mechanical cadence; keep semantic acceptance in control | Add repo-owned deterministic check under the judge |

Both decisions are sound. Neither should be reverted. The one thing worth adding —
for high-stakes targets only — is a deterministic backstop built from the target
repo's **own** contract tests, which strengthens both decisions without undoing
either. That conditional follow-up is tracked as
[`P1.2`](../proposals/improvement-proposals.md#p12--target-owned-deterministic-evaluation-backstop),
not as part of this implemented design.

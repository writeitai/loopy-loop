# Claude Code review: loop state and trace contract

**Reviewer:** Claude Code
**Date:** 2026-07-15
**Baseline:** `main` at `a5f9933`; team-harness 0.4.0; eval-banana 0.3.1
**Scope:** read-only audit of the one-loop template, the planner/dispatcher double
loop, possible deeper recursion, state/evidence ownership, harness traces, evals,
recovery, and git/PR hand-off
**Status:** historical analysis, not a binding specification or current-runtime
description

The adjudicated design is
[`recursive-loop-layer-contract.md`](../designs/recursive-loop-layer-contract.md).
This note preserves the independent review findings that led to it without retaining
the original transcript-scale evidence dump.

## Verdict at the reviewed baseline

The coordinator's core state machine was sound: one worker, attempt fencing,
depth-first parent suspension, crash-window reconciliation, and the rule that
`IterationResult.success` reports harness completion rather than semantic quality.
The important gaps were at dependency and prompt boundaries. Agent identity and path
ownership were communicated mostly through prose; one canonical harness record lived
outside the session tree; the stock eval path had dependency drift; and constraints
that were documented were not always detectable at runtime.

This was an architecture review, not a release verdict. Claims below describe the
2026-07-15 baseline unless their disposition explicitly says otherwise.

## Severity-ordered findings

| Severity | Finding at the audited revision | Design disposition |
| --- | --- | --- |
| Critical | Loopy read usage from a session-local `run.json`, while team-harness 0.4.0 wrote the canonical record to `~/.team-harness/runs/<run_id>/run.json`. Usage stayed unknown and `max_cost_usd` could not fire. | **Accepted.** D12 and the caller-owned harness contract require the canonical record under a caller-supplied attempt location and explicit capability negotiation. |
| High | The eval-author prompt taught `target_paths`, which eval-banana 0.3.1 rejected. | **Accepted as dependency-contract drift.** Prompt-conformant checks must be exercised against the real dependency schema. |
| High | Stock `harness_judge` execution required an eval-banana harness configuration that clean `loopy init` did not supply or explain. | **Accepted.** Eval configuration and effective judge identity became explicit assignment/evidence concerns. |
| High | Prompt scratch names (`_feature_planning`, `_additional_context`) had contradictory locations and could escape the gitignored session tree into a PR. | **Accepted in principle.** Runtime assignments now provide explicit absolute homes. Preventive write fences remained rejected under D8. |
| Medium-high | Both `outer` and `eval_runner` could declare `goal_met`; `outer` could close a session before the eval layer ran. | **Accepted.** D11 separates task acceptance from layer-level `goal_met`, assigns one goal-control role, and preserves `unresolvable_error` for declared blocker-reporting roles. |
| Medium | Packaged `.gitignore` files diverged and did not consistently cover generated state/scratch. | **Accepted as hygiene**, not as a correctness fence. |
| Medium | Rendered assignments omitted role, loop depth, ancestry, and several paths used by stock prompts; relative names were ambiguous across parent and child sessions. | **Accepted and central to D10.** Assignments carry role, root/parent/depth identity and absolute paths; persisted evidence uses portable logical references. |
| Medium | A child session's nested `child_requests/` was silently ignored. | **Accepted and central to recursive dispatch.** Unsupported requests must be durably rejected; the recursive runtime may dispatch at allowed depths. |
| Medium | Session lookup used a repo-wide `rglob`, making lookup ambiguous and dependent on unrelated session trees. | **Accepted.** Resolution is rooted in validated session identity/topology. |
| Medium | Recovery depended on the host-global team-harness run directory and a session export would omit the canonical coordinator record. | **Accepted as a caller-contract problem.** The session/attempt owns the durable record; distributed recovery was not otherwise invented. |
| Low-medium | The PM template's example goal described the dispatch mechanism rather than a project outcome. | **Accepted as template clarity.** |
| Low | A child inherited its root parent's frozen system extension rather than the child workflow set's own extension. | **Folded into frozen workflow-set and per-assignment contract work.** |
| Low | Eval prompts omitted paths that they nevertheless referenced. | **Accepted as assignment/prompt consistency.** |
| Low | The inner prompt duplicated one reviewer family. | **Accepted as prompt cleanup, not architectural.** |

## Contract conclusions that informed D10-D12

- A recursive layer is a durable session node, not every dynamically spawned harness
  agent. Coordinators own session state; spawned agents receive bounded assignments
  and report observable I/O through the harness.
- Every assignment must state the agent's role, goal, owning session, depth, ancestry,
  attempt identity, and relevant absolute paths. Agents must not infer their state
  directory from the checkout working directory.
- Durable state/evidence and high-volume execution trace are different planes. A
  correctness fact cannot exist only in trace data that may later be unavailable.
- Parent acceptance of a child outcome does not prove the parent's goal. Each layer
  owns its own eval evidence and terminal `goal_met` decision.
- Child dispatch, completion, recovery, and usage roll-up must work recursively and
  idempotently while preserving the deliberate single-worker stack.
- Cross-repository assumptions require named capabilities and real integration tests;
  package version inference and synthetic fixtures are insufficient.

## Recommendations retained

1. Make the harness run record caller-owned and session-local, while retaining enough
   identity for recovery and usage accounting.
2. Freeze workflow definitions and goals into each session/attempt; bind results to
   attempt and repository identity.
3. Render identity and all state/evidence paths explicitly; persist portable logical
   references instead of absolute paths.
4. Separate coordinator-to-session contracts from coordinator-to-spawned-agent
   contracts.
5. Make nested-dispatch limits visible and repairable rather than silently ignoring
   requests.
6. Keep scheduling cadence mechanical. Eval readiness is context for agents, not a
   semantic scheduler gate.
7. Test the actual loopy/team-harness/eval-banana seam and mixed-version behavior.

## Rejected, corrected, or superseded recommendations

- **No parallel loopy workers:** rejected by D2.
- **No semantic success from process exit codes:** rejected by D3.
- **No agent-authored deterministic stock checks:** rejected by D4. A target repo's
  own tests remain valid evidence; this is not permission for eval agents to invent
  deterministic checks.
- **No scheduler veto or path-level write ACL:** rejected by D8. Detection and repair
  remain the contract shape.
- **No mandatory `state/` and `trace/` directory rewrite:** the conceptual separation
  was adopted without requiring that disruptive physical migration.
- **No assumption that session-local spawned-worker output already included
  `run.json`:** the original placement claim was corrected. In team-harness 0.4.0 the
  canonical coordinator record was global even when worker artifacts used
  `output_dir`.
- **No cloud exporter, trace pruning policy, or generic credential detector was
  selected by this audit.** Export/retention remain future work; broad credential
  spotting was later removed from implementation scope.

## Historical verification conditions

The review reproduced the run-record path mismatch, eval schema rejection, missing
eval harness configuration, and target-repo gitignore behavior against the named
dependency versions. It also inspected scheduler cadence and recursive recovery paths.
Those probes established design inputs only. They do not certify the later D10-D12
implementation or the current branches; that requires a fresh, quiescent-tree review
and the release gates in the binding design.

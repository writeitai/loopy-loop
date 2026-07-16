# Antigravity review: loop state and trace contract

**Reviewer:** Antigravity AI Coding Assistant
**Date:** 2026-07-15
**Scope:** independent audit of coordinator/worker/harness ownership, one- through
three-layer execution, recovery, eval and git boundaries, and a possible cloud trace
contract
**Status:** historical analysis; not binding and not a current-runtime description

The final adjudication is the binding
[`recursive-loop-layer-contract.md`](../designs/recursive-loop-layer-contract.md).

## Verdict at the reviewed baseline

Antigravity found the file-backed, single-worker stack suitable for local,
repository-centred execution. Its central concern was that the boundary from loopy to
team-harness and then to dynamically spawned CLIs was implicit: spawned agents learned
session identity, depth, goal, and paths only from prose, while recovery and canonical
run records depended on host-local state.

The review also explored cloud execution. Those ideas were intentionally not all
adopted; local-first operation remains the contract.

## Severity-ordered findings and disposition

| Severity | Finding or recommendation | Adjudication |
| --- | --- | --- |
| High | Spawned coordinators lacked a structured assignment carrying session, iteration, depth, goal, and absolute artifact paths. | **Accepted in substance.** D10 distinguishes durable session coordinators from dynamic harness agents and gives each a role-specific assignment envelope. Environment variables were not made the source of truth. |
| High for distributed deployment | Recovery read team-harness's host-global `RUNS_DIR`, so a coordinator on another host could not inspect or reap that run. | **Partially accepted.** The caller now owns the canonical run location through a negotiated harness contract. General remote/distributed recovery was not added. |
| Medium | Branch, PR, and merge evidence existed mainly in prompts/markdown and was not reconciled with repository state. | **Accepted as compact git evidence and eval context.** The engine does not prevent edits or semantically veto scheduling. |
| Medium | Deeper recursion needed explicit depth identity, ownership, bounded dispatch, recursive recovery, and subtree accounting. | **Accepted and central to D10.** The existing pointer walk generalized; the prior child-only dispatch guard did not. |
| Medium | Parent, feature, and delivery layers needed distinct ownership and direct-child evidence boundaries. | **Accepted.** Child outcome is evidence for parent acceptance, never automatic parent success. |
| Medium | Stock evaluation needed protection from self-gaming. | **Narrowed.** D4 remains: stock eval agents author only LLM-as-judge checks. Target-owned tests may be evidence, but no new mandatory agent-authored deterministic gate was accepted. |
| Low/future | A cloud trace envelope should link session, attempt, parent span, usage, worker identity, prompts, responses, and eval verdicts. | **Deferred.** Local capture and inspection are in scope; cloud transport, export policy, and retention automation are future work. |

## Recommendations that informed the design

- Model every loop layer as the same recursive session node with explicit root,
  parent, depth, role, and attempt identity.
- Pass an authoritative bounded assignment to spawned coordinators and agents instead
  of asking them to discover state by searching the checkout.
- Preserve the depth-first single-active-leaf invariant and unwind parents
  idempotently after child completion.
- Keep semantic state/evidence distinct from voluminous harness traces and link the
  planes with stable session/attempt identities.
- Carry exact git/PR/eval evidence in layer-local records rather than relying only on
  prose assertions.
- Negotiate cross-repository capabilities explicitly and test the real integration.

## Corrected, rejected, or superseded material

- The original sequence diagram incorrectly showed canonical `run.json` under
  session `harness_outputs/`. With team-harness 0.4.0 it was written under
  `~/.team-harness/runs/<run_id>/run.json`; only caller-directed worker artifacts
  were session-local.
- Repointing recovery to a session path without first changing team-harness would
  therefore have broken recovery. The adopted direction was a negotiated caller-owned
  run-record contract.
- Structured environment variables were considered useful transport metadata but
  were not accepted as durable truth; assignment files and absolute paths are.
- A configurable depth number alone was insufficient. Recursive dispatch shipped
  only with identity, recovery, ownership, eval, and unwind contracts.
- Mandatory stock deterministic checks were rejected under D4. Existing target-owned
  tests remain legitimate evidence.
- Engine rejection of dirty git state and container write fences were rejected under
  D8.
- Direct fire-and-forget cloud posting was rejected. It would add a network dependency
  and a second correctness path to a deliberately local-first loop.
- Generic credential detection, export, and pruning are not current features and are
  not implied by this review.

## Historical verification conditions

This was an independent source review of the pre-D10-D12 runtime. Its verdict depended
on local execution and the installed team-harness 0.4.0 behavior. It did not review the
final recursive implementation and must not be used as a current release gate.

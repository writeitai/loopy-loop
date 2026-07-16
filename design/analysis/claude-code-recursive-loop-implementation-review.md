# Claude Code implementation review: recursive loop contract

**Reviewer:** Claude Code, Opus 4.8

**Date:** 2026-07-16

**Verdict:** **PASS for merge readiness after support-package publication**

## Settled implementation revisions

- loopy-loop: `57ffa30b0db5d130313446bad0141cee530f7387`
- team-harness: `217351f3dec830441e74e02b1c196e106e253753`
- eval-banana: `c979f70fedd0e9ebc02f4b8e33cd3868d52b6c37`

Claude performed an adversarial review of the complete implementation, reproduced
critical paths with real processes and files, and then performed a focused re-review of
the final team-harness shutdown-grace correction. Both reviews were read-only. The
second review returned PASS and found no blocker.

## Blocking findings and their disposition

| Finding | Final disposition |
| --- | --- |
| A scratch or copied top-level state directory could hijack root discovery. | Fixed. Root discovery requires a structurally valid, non-symlinked root manifest whose identity matches the directory. Genuine legacy v1 roots remain readable. |
| Unrelated malformed or duplicate trace IDs could poison a healthy referenced trace. | Fixed. Resolution filters for the requested identity first; a duplicate of the selected valid identity still fails. |
| An explicit workflow contract without a protocol field could silently derive v1. | Fixed. An explicit contract defaults to v2; only the no-contract compatibility path derives v1. |
| Rewriting agent-visible session and workflow projections between attempts could downgrade trust. | Fixed. The complete v2 workflow contract is persisted in engine state, restored into projections, and frozen per attempt for live and recovery completion. |
| A process-probe failure plus a SIGTERM-ignoring worker could leave team-harness finalization pending forever. | Fixed. Shutdown and retained lifecycle work are bounded, trusted process groups receive probe-independent SIGKILL when required, tasks settle before `asyncio.run()` returns, and both snapshots retain structured timeout evidence. |
| `check_definition_sha256` did not bind the bytes behind deterministic `script_path`. | Fixed. The digest covers exact YAML and frozen referenced-script bytes, and execution uses those same bytes while preserving the logical script path, imports, argv, and adjacent assets. |
| Finalization evidence stored only an exception class because of a redaction rationale. | Fixed. Caller-owned raw traces retain the exact lifecycle exception message; no generic credential detector or sanitizer was introduced. |

## Final lifecycle correction

The first bounded-finalization fix used equal inner and outer shutdown timers. Claude
showed that the outer timer therefore won before a worker received its intended
SIGTERM grace period. The final team-harness revision gives the inner natural-exit
period one named SIGTERM grace interval before the outer hard bound. A real-process
regression proves a responsive worker receives SIGTERM and exits normally; the
probe-failure and TERM-ignoring cases still take the bounded SIGKILL path. Claude's
micro-review of this correction returned PASS.

## Contract checks confirmed

Claude also confirmed:

- recursive dispatch and iterative unwind through three depths;
- D5 identity-bound `goal_met` and `unresolvable_error` control, with no pause state;
- layer-local eval ownership and receipt binding to session, goal, workflow, attempt,
  check definitions, judge settings, and evaluated Git state;
- team-harness `assignment_path` as the spawned actor's envelope and
  `parent_assignment_path` as the direct enclosing assignment at every nesting depth;
- raw, gitignored, attempt-owned trace capture separated from correctness state;
- docstrings on every diff-touched production function and named arguments for
  meaningful project-owned calls; and
- absence of credential scanning/redaction, trace export/pruning/cloud transport,
  deterministic stock evals, parallel loopy workers, fixed agent graphs, human pause
  states, and preventive path fences.

The final validation evidence was 380 passing loopy-loop tests, 522 passing
team-harness tests, and 131 passing eval-banana tests, with Ruff formatting/lint and
Pyright clean. team-harness's two ANSI assertions require `NO_COLOR` to be unset and a
color-capable `TERM`; this pre-existing environment requirement is unrelated to the
feature.

## Non-blocking observations and adjudication

- `load_workflow_set_contract()` still exposes unused raw contract text that does not
  contain the injected v2 default. The running trust path uses the validated contract
  object, and the text has no consumer. This is not a current downgrade path; it must be
  serialized from the validated object before any future consumer relies on it.
- Goal-check rejection could expose more field-qualified repair detail, and the loopy
  eval report reader could explicitly gate the report schema version. Both fail closed
  today and were not expanded into this already large contract change.
- Recursion has no numeric maximum depth by design. Workflow child interfaces and
  optional tree-wide cost limits bound useful work without adding a D8 scheduling
  fence.

These observations are not merge blockers and do not justify reintroducing removed
peripheral subsystems.

## Release disposition

Code readiness is separate from package availability. Merge and publish eval-banana
0.3.2 and team-harness 0.5.0 first, refresh loopy-loop's dependency lock against those
published releases, run loopy-loop's normal install-path CI, and only then merge or
publish loopy-loop 0.7.0. Editable sibling checkouts are only the development bridge.

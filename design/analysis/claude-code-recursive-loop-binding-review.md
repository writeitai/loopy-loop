# Claude Code review: recursive loop binding design (D10-D12)

**Reviewer:** Claude Code
**Initial review:** 2026-07-15
**Final design re-review:** 2026-07-15
**Baseline for code claims:** `main` at `a5f9933`; team-harness 0.4.0;
eval-banana 0.3.1; 216 loopy tests passed
**Scope:** binding-document correctness, migration safety, compatibility with D1-D9,
and cold readability; implementation was explicitly pending
**Status:** historical design review; the binding design and decisions are canonical

## Final design verdict

After revision, **no blocking design issue remained**. Claude judged the recursive
session node, dynamic-agent boundary, state/evidence-versus-trace split, and layer-local
eval ownership sound and compatible with D1-D9. This verdict approved the design for
implementation; it did not approve any later code.

## Initial severity-ordered findings

### Blocking

| ID | Finding | Required change | Final disposition |
| --- | --- | --- | --- |
| B1 | The proposed v2 `control.json` had no reader migration. The old reader rejected non-v1 control and converted malformed control into terminal failure, contradicting the promised repair path. | Ship a version-discriminated reader before v2 writers; archive malformed v2 control, keep the session repairable, and bound repeated protocol failure. | **Resolved.** Legacy v1 semantics remain unchanged; new-schema failures use the bounded repair path. |
| B2 | `outer` was to record eval readiness, but no mechanism consumed it. Making readiness a scheduler eligibility condition would violate D8. | Treat readiness only as rendered prompt context and retune mechanical eval cadence. | **Resolved.** Scheduling remains cadence-only. |
| B3 | The new goal-control owner was clear for `goal_met` but ownership of D5's `unresolvable_error` escape hatch was ambiguous. | Separate `goal_control_role` from declared terminal-blocker reporting roles. | **Resolved.** Goal completion is exclusive; legitimate blocker reporting remains available to configured roles without an eval receipt. |
| B4 | Moving child requests from a flat directory to `pending/` could strand requests under mixed engine/template versions. | Scan both locations during migration and change the rendered writer only after the reader ships; retain idempotency across the transition. | **Resolved.** |

### Moderate

| ID | Finding | Final disposition |
| --- | --- | --- |
| M1 | The reliability design still described budget enforcement as working even though the team-harness 0.4.0 run-record mismatch made usage unknown. | Both binding documents recorded the known integration defect and sequenced its repair. |
| M2 | The layout omitted recovery-critical artifacts such as `salvage.json`, `pending_finished_request.json`, `parent.json`, and `children.json`. | The layout was reframed and recovery-journal artifacts were made non-prunable correctness data. |
| M3 | `eval_results/` had no assignment path and conflicted with the proposed trace placement. | The design chose explicit paths and separated compact eval receipts from raw reports. |
| M4 | Logical-reference examples used incompatible grammars. | One grammar was defined, including root, parent, self, child, named session, and trace scopes. |
| M5 | Tree-wide usage projection was a verification requirement but no implementation phase built it. | A phase item and tests were added, sequenced after real usage discovery. |

### Low

- Define absent `session.json.schema_version` as legacy v1 and identify its reader.
- Scope mixed-version warnings specifically to strict `config_snapshot` additions;
  safe top-level response additions need not be blocked.
- Describe existing `.gitignore` coverage honestly and normalize it during migration.

All were incorporated into the binding design.

## Final re-review residuals

The second pass found no blocker. It identified six documentation issues:

1. The verification list said only the goal-control owner could close a session; it
   needed to say “close with `goal_met`” so D5 blocker roles remained valid.
2. Eval-readiness, rejected-control, and protocol-failure artifacts needed declared
   locations and assignment paths.
3. The repairability statement needed to say **new-schema** malformed control; legacy
   v1 retained historical terminal semantics.
4. Task-acceptance ownership and terminal-blocker reporting needed distinct structured
   fields rather than being conflated under `eval`.
5. The design needed concrete v2 control examples with an eval receipt required only
   for `goal_met`, not `unresolvable_error`.
6. The logical-reference resolver needed an explicit implementation phase, not only a
   verification bullet.

The maintainer incorporated all six before close-out. The final design review therefore
ended with **no blocking or residual design finding**.

## Findings that shaped the final design

- New wire schemas require an ordered reader-before-writer migration and capability
  negotiation; templates and engines can drift independently.
- Malformed v2 protocol output is detected, archived, exposed for repair, and bounded;
  it is not silently converted into semantic failure.
- Eval readiness informs agents but never controls scheduler eligibility.
- `goal_met` ownership is layer-local and exclusive, while D5 blocker reporting is a
  separate concern.
- Every correctness/recovery artifact has a declared plane and path. Prunable trace
  detail cannot be the only copy of a correctness fact.
- Portable references require one traversal-safe grammar and a real resolver.
- Recursive unwind must be iterative, with long process drain/reap work outside the
  transition lock.

## Rejected alternatives

- A readiness-driven scheduler gate: rejected under D8.
- Removing `unresolvable_error` from non-eval roles: rejected under D5.
- Immediately switching writers to `child_requests/pending/`: rejected as unsafe in a
  mixed-version deployment.
- Treating every spawned harness agent as a session layer or fixing a static subagent
  graph: rejected; dynamic agents receive assignments but do not own durable loop
  state.
- Persisting absolute paths as identity: rejected because moved checkouts would break;
  absolute paths are runtime instructions, logical references are durable identity.

## Historical verdict conditions

The final “ready to implement” verdict covered the revised design documents only. It
relied on source checks against the pre-implementation baseline and did not run or
approve the three-repository implementation. A current implementation verdict still
requires a quiescent tree and the binding release gates.

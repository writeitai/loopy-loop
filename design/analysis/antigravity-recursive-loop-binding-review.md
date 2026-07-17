# Antigravity review: recursive loop binding design (D10-D12)

**Reviewer:** Antigravity AI Coding Assistant
**Initial review:** 2026-07-15
**Final design re-review:** 2026-07-15
**Scope:** concurrency, recursive ownership, path portability, mixed-version safety,
eval boundaries, and dependency alignment in
[`recursive-loop-layer-contract.md`](../designs/recursive-loop-layer-contract.md) and
D10-D12
**Status:** historical design review; implementation was not the subject

## Final design verdict

After revision, Antigravity reported **no blocking design issue**. The recursive
session abstraction was judged compatible with D1-D9 and suitable for implementation.
This verdict does not certify the later code.

## Severity-ordered findings

| Severity | Finding | Final disposition |
| --- | --- | --- |
| High | Recursive parent resumption could re-enter registration while holding the coordinator transition lock, allowing bounded process drain/reap to block all state transitions. | **Accepted.** The design requires iterative parent unwind and plans process recovery outside the transition lock. |
| High | The proposed mixed-version guarantee had no negotiation fields in worker registration, even though older workers rejected unknown snapshot fields. | **Accepted.** Registration carries protocol version and named capabilities; coordinators must not infer support from package versions. |
| Medium | `parent:/` and direct-child references could not name a non-immediate ancestor in a three-level tree. | **Accepted.** The grammar includes `root:/` and validated `session:<session_id>:/...` scopes. |
| Medium | Complete local trace capture could expose secrets if the design promised only future export-time sanitization. | **Superseded.** Broad credential detection/redaction was later removed from scope. The current binding contract treats local traces as raw, gitignored capture; sealing is an integrity/completeness boundary, not a sanitization claim. Export remains future work. |
| Low | Logical-reference examples disagreed about slashes. | **Accepted.** One canonical grammar was specified. |
| Low | The dirty-tree digest did not define treatment of untracked files and git object boundaries. | **Accepted in narrowed form.** The design specifies a versioned canonical dirty-tree subject and explicitly documents its limits rather than claiming a general security boundary. |

## Dependency checks preserved from the review

- With team-harness 0.4.0, canonical `run.json` was global; `output_dir` controlled
  spawned-worker artifacts but not that coordinator record. The design correctly made
  caller-owned run placement a negotiated future capability rather than assuming it
  already existed.
- The installed eval-banana package exposed both `eb` and `eval-banana` entry points,
  so interpreter-script path insertion was a valid execution strategy at that
  baseline.

## Recommendations that informed the design

- Unwind recursively but iterate operationally; never hold a state-transition lock
  across process lifecycle work.
- Negotiate behavior through named capabilities and protocol versions.
- Give deep sessions portable references to any validated ancestor without relative
  path traversal.
- Define versioned git evidence precisely, including untracked content and declared
  exclusions.
- Sequence control-schema readers before writers; keep malformed v2 output repairable
  and bounded.
- Keep eval readiness as prompt context, preserve D5 blocker roles, and scan both child
  request locations during migration.

## Rejected or superseded alternatives

- Package-version inference instead of capability negotiation was rejected.
- Recursive registration under a reentrant lock was rejected even though it might not
  deadlock immediately; it would violate responsiveness and recovery containment.
- Fragile `../../..` ancestry and persisted absolute paths were rejected.
- Broad local credential spotting, chmod policy, export enforcement, and pruning are
  not part of the current feature. The review's secret-redaction recommendation is
  retained only as historical input, not as current behavior.

## Historical verdict conditions

The final PASS covered the amended binding documents and decisions. It was based on
the pre-implementation source and installed dependency versions. It did not execute or
adversarially review the final three-repository implementation.

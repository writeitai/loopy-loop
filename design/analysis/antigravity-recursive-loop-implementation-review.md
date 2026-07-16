# Antigravity implementation review: recursive loop contract

**Reviewer:** Antigravity AI Coding Assistant
**Date:** 2026-07-16
**Reviewed repositories:** loopy-loop, team-harness, and eval-banana feature
worktrees
**Scope:** recursive state invariants, recovery, assignments, agent context, traces,
eval provenance, and v1 compatibility
**Status:** **historical moving-tree review; a new final review is pending**

## Historical verdict

Antigravity reported **PASS** for the snapshot it observed, citing 315 passing loopy
tests, 516 team-harness tests, and 117 eval-banana tests. It found no blocker, high, or
medium issue and offered two low-severity suggestions.

That PASS is not the current release gate. The three worktrees were subsequently
changed substantially, including removal of generic credential redaction and local
trace export/prune. A separate Claude review also observed the tree changing during
review and found live blockers at other snapshots. The disagreement is preserved as
historical evidence, not averaged into a verdict.

## Areas reported as verified

| Area | Historical conclusion |
| --- | --- |
| Recursive state | Persistence invariants prevented a live task and live child from coexisting; multi-level completion unwound toward the root. |
| Recovery | Process drain/reap planning ran outside the transition lock; durable reconciliation occurred under the lock; stale attempts remained fenced. |
| Assignment integrity | Attempt metadata carried stable repository/session identity and absolute paths; frozen workflow tampering failed before harness execution. |
| Dynamic agents | team-harness propagated caller and parent assignment context so spawned coordinators knew their bounded role without discovering session state. |
| Eval provenance | Layer-local receipts were checked against report pass state, judge identity, and git evidence before `goal_met`. |
| Compatibility | Legacy state and run-record discovery had explicit fallback behavior. |

These remain useful regression targets only where the simplified implementation still
claims them. They must be re-run rather than assumed.

## Low-severity suggestions at the snapshot

1. Include the underlying session/lock reason when logging `WorkerBusyError` before
   returning HTTP 409, to make multi-level retry diagnostics clearer.
2. Restrict local trace directory permissions for multi-user hosts.

The second suggestion was coupled to a then-present redaction/export subsystem and is
not a core recursive-contract requirement. Host permission policy can be designed
separately if needed.

## Superseded material

The original review described process-level credential redaction, pre-seal
sanitization, export drift checks, and pruning preservation as verified features.
Those mechanisms were later removed from scope. The current feature does not promise
generic credential spotting, local sanitized traces, export, pruning, or cloud
transport. Their deletion does not invalidate the state/assignment findings, but it
does invalidate that part of the historical PASS.

## Findings that informed ongoing work

- Preserve recursive state invariants and iterative unwind tests at depths one, two,
  and three.
- Preserve absolute, role-specific assignments for coordinators and dynamically
  spawned harness agents.
- Preserve real cross-repository capability and run-record integration tests.
- Keep layer-local eval ownership and legacy compatibility explicit.
- Improve conflict diagnostics without changing HTTP 409 retry semantics.

## New final review required

No post-cleanup Antigravity verdict is recorded. A valid final review must identify the
exact revisions, run the current release gates in all three repositories, inspect only
the retained scope, and report its own result. Until then, this historical PASS must not
be cited as approval of the current PRs.

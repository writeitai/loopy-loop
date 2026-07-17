# Claude Code review of the protocol-v3 implementation

Status: external implementation review, resolved before release

Date: 2026-07-17

Reviewer: Claude Code, `claude-opus-4-8`, `xhigh`

## Review scope

Claude reviewed the protocol-v3 Loopy implementation and the capability-roster
transport in Team Harness against the binding design in
`design/designs/orchestrator-owned-completion-and-cross-harness-review.md`.
The review was read-only and asked for concrete blocker, high, and material
medium findings rather than new product scope.

## Findings and disposition

### Every v3 terminal child needs one outcome shape — fixed

Claude found that `coordinator_app.py` originally built
`session_outcome.json` only from an accepted terminal `control.json`. A child
that ended through an engine lifecycle reason such as `max_turns`,
`workflow_failure_cap`, or `stop_requested` therefore could not be projected
to its parent. Stack unwind could raise instead of handing the factual child
result upward.

The implementation now gives every v3 terminal lifecycle the same
`SessionOutcome` shape. Control-owned stops include the accepted control hash;
engine-owned stops use `control: null` and a factual `engine_stop_reason`
fallback. `_ensure_session_outcome()` is called before child projection and
parent unwind. `test_v3_non_control_child_stop_writes_outcome_and_resumes_parent`
exercises the workflow-failure path through a real child ledger.

### Negative provenance coverage was too thin — fixed

Claude asked for adversarial coverage showing that plausible-looking files do
not become trusted evidence merely because they exist. The focused v3 suite
now covers false completion authority, stale attempts, unaccepted or changed
eval evidence, false handoff attempt provenance, exact child-outcome linkage,
and restoration from engine-owned terminal snapshots after mutable files are
changed.

These checks validate identity and provenance only. They do not add semantic
gates or make evaluation mandatory.

### Claimed missing assignment-builder docstring — no change

The review reported that `build_attempt_assignment()` lacked a docstring. The
reviewed tree already contained the docstring “Build the identity-bound,
absolute-path envelope for an attempt.” No code change was needed.

## Final assessment

The blocker and material coverage gap were accepted and repaired. The docstring
finding was not reproducible. The resulting change remains within the accepted
design: durable orchestrators own completion, evaluation is advisory, the
engine validates provenance, and parent acceptance stays separate from child
completion.


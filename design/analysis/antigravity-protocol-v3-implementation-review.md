# Antigravity review of the protocol-v3 implementation

Status: external implementation review, resolved before release

Date: 2026-07-17

Reviewer: Antigravity CLI, multi-hour print timeout

## Review scope

Antigravity performed a read-only adversarial review of the protocol-v3 Loopy
changes and Team Harness capability-roster transport. It inspected the full
working tree and ran tests while the terminal-outcome repair was still being
edited, so one report describes a transient intermediate state.

## Findings and disposition

### Protocol-3 stale completion was mistaken for a legacy worker — fixed

In `_finish_assignment_locked()`, the no-current-task retry branch required the
remembered worker contract to equal version 2. A correctly registered
protocol-3 worker therefore failed the same safety check intended to reject
unregistered legacy workers.

The check now accepts any validated version 2 or newer contract and continues
to reject missing or version-1 handshakes. The error message is version-neutral,
and `test_v3_stale_finished_accepts_valid_v3_worker_handshake` exercises the
previously rejected path.

### `JsonValue` resolution — transient import issue fixed; rebuild unnecessary

Antigravity observed a transient tree in which
`AcceptedTerminalControlSnapshot` referenced Pydantic's `JsonValue` without an
import, producing schema-resolution failures. The final implementation imports
`JsonValue`. An explicit `LoopState.model_rebuild()` was suggested as well,
but is unnecessary once the type is present in module globals:
`LoopState.model_json_schema()` and the focused suite both complete without it.
Adding a redundant global rebuild would obscure rather than strengthen the
model contract, so it was not adopted.

### Keyword-only helper style — fixed

`_is_full_sha256()` now declares `value` as keyword-only, matching its existing
call sites and the repository's named-argument convention.

## Final assessment

The real compatibility and style findings were fixed and regression-tested.
The schema finding was resolved at its cause; the extra rebuild suggestion was
rejected based on direct schema generation and test evidence. No additional
architecture or feature work was introduced.


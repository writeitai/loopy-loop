# Session Layout

One session directory is created per fresh coordinator run and reused for all iterations in that session.

```text
.loopy_loop/
└── sessions/
    └── <session_id>/
        ├── session.json
        ├── events.jsonl
        └── iterations/
            ├── 0001_planner/
            │   ├── prompt.txt
            │   ├── result.json
            │   ├── result_text.txt
            │   ├── harness_run_id.txt
            │   └── control.json
            └── 0002_goal_check/
                ├── prompt.txt
                ├── result.json
                ├── result_text.txt
                ├── harness_run_id.txt
                ├── control.json
                └── goal_check.json
```

## Session Files

`session.json`

- Session metadata written once when the session directory is created
- Contains `session_id`, `goal_hash`, and `created_at`

`events.jsonl`

- Reserved append-only event log for diagnostics
- Created at session start in v1

## Iteration Files

`prompt.txt`

- The rendered assignment prompt sent to `TeamHarness.run(task=...)`

`result.json`

- loopy-loop normalized result payload:

```json
{
  "success": true,
  "text": "final response",
  "error": null,
  "harness_run_id": "run-123"
}
```

`result_text.txt`

- Plain text copy of the result text, or empty string on failure

`harness_run_id.txt`

- The `team_harness` run id, or empty string on failure before a run id exists

## Control Contracts

`control.json`

- Read only from the current iteration directory
- Unknown keys are ignored
- Required schema:

```json
{
  "unresolvable_error": true,
  "reason": "Missing private package registry credentials",
  "schema_version": 1
}
```

`goal_check.json`

- Authoritative only at:

```text
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_goal_check/goal_check.json
```

- Required schema in v1:

```json
{
  "goal_met": false,
  "reason": "CTA exists, but deployment docs are still missing.",
  "schema_version": 1
}
```

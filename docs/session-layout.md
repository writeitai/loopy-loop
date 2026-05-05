# Session Layout

One session directory is created per fresh coordinator run and reused for all iterations in that session.

```text
.loopy_loop/
└── sessions/
    └── <session_id>/
        ├── session.json
        ├── events.jsonl
        ├── project_state/
        ├── eval_checks/
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

`project_state/`

- Optional workflow-owned markdown state for reusable workflows
- Common files include `README.md`, `current_state.md`, `what_we_have.md`,
  `decisions.md`, `eval_results.md`, and `what_we_should_do/plan.md`
- The coordinator does not parse these files

`eval_checks/`

- Optional workflow-owned eval-banana checks for this session
- A workflow can run only these checks with:

```bash
eval-banana run --check-dir .loopy_loop/sessions/<session_id>/eval_checks
```

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

- Authoritative only at the current iteration directory for `goal_check` or a
  workflow configured with `emits_goal_check=true`:

```text
.loopy_loop/sessions/<session_id>/iterations/<NNNN>_<workflow_id>/goal_check.json
```

- Required schema in v1:

```json
{
  "goal_met": false,
  "reason": "CTA exists, but deployment docs are still missing.",
  "schema_version": 1
}
```

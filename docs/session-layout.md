# Session Layout

One session directory is created per fresh coordinator run and reused for all iterations in that session.

```text
.loopy_loop/
└── sessions/
    └── <session_id>/
        ├── session.json
        ├── events.jsonl
        ├── control.json
        ├── updates_from_user.md
        ├── project_state/
        │   └── finished.md
        ├── eval_checks/
        ├── eval_results/
        │   └── <eval_banana_run_id>/
        │       ├── report.json
        │       ├── report.md
        │       └── checks/
        ├── harness_outputs/
        │   └── 0001_planner/
        │       └── <team_harness_run_id>/
        └── iterations/
            ├── 0001_planner/
            │   ├── prompt.txt
            │   ├── result.json
            │   ├── result_text.txt
            │   ├── harness_run_id.txt
            │   └── pending_finished_request.json
            └── 0002_goal_check/
                ├── prompt.txt
                ├── result.json
                ├── result_text.txt
                ├── harness_run_id.txt
                └── goal_check.json
```

## Session Files

`session.json`

- Session metadata written once when the session directory is created
- Contains `session_id`, `goal_hash`, and `created_at`
- New session ids use `<YYYYMMDD>_<HHMMSS>_<goal_hash>_<random>` so session
  directories sort chronologically by name

`events.jsonl`

- Reserved append-only event log for diagnostics
- Created at session start in v1

`control.json`

- Session-scoped workflow stop switch
- Created with `state: "running"` when the session starts
- Workflows update it only when they want the loop to stop

`updates_from_user.md`

- Human-writable inbox for requests that arrive after the session starts
- The outer workflow treats non-empty content as highest-priority planning input
- The outer workflow clears the file only after reflecting the request into
  `project_state/`

`project_state/`

- Optional workflow-owned markdown state for reusable workflows
- `loopy_loop_goal.txt` is the source of truth for the target, constraints, and
  completion intent; do not copy or restate the goal into `project_state/`
- Common files include `README.md`, `memory.md`, `current_state.md`,
  `what_we_have.md`, `decisions.md`, `eval_results.md`, `finished.md`, and
  `what_we_should_do/plan.md`
- `README.md` should explain ownership rules: `memory.md` is essential durable
  facts only, `finished.md` is outer-owned accepted completions only,
  `eval_results.md` owns eval detail, and `current_state.md` carries live
  status, the latest eval headline, and the next action
- The coordinator does not parse these files

`project_state/finished.md`

- Append-only ledger for outer-verified completed work
- Entries should summarize the completed task and link to the relevant iteration
  and harness output files
- For implementation work, entries should include delivery evidence for each
  changed repo: repo path or remote, branch, PR URL, merge status, merge commit
  when merged, and checks/CI status
- Default implementation delivery is branch + PR + passing checks + merge,
  unless the task is session-state-only, eval-only, research-only,
  planning-only, or has no usable remote/auth

`eval_checks/`

- Optional workflow-owned eval-banana checks for this session
- A workflow can run only these checks and write results into the session with:

```bash
eval-banana run \
  --cwd . \
  --check-dir .loopy_loop/sessions/<session_id>/eval_checks \
  --output-dir .loopy_loop/sessions/<session_id>/eval_results
```

`eval_results/`

- Optional workflow-owned eval-banana run output for this session
- Each `eval-banana run --output-dir .../eval_results` creates a child
  `<eval_banana_run_id>/` containing `report.json`, `report.md`, and per-check
  artifacts
- `project_state/eval_results.md` should summarize and link these reports; it
  should not copy full raw reports
- `project_state/current_state.md` should only carry the latest eval headline
  and the next action

`harness_outputs/`

- Root for team-harness coordinator and worker artifacts
- Each loopy-loop iteration gets its own output root:

```text
.loopy_loop/sessions/<session_id>/harness_outputs/<NNNN>_<workflow_id>/
```

- team-harness then creates a child directory named with its run id:

```text
.loopy_loop/sessions/<session_id>/harness_outputs/<NNNN>_<workflow_id>/<team_harness_run_id>/
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
  "harness_run_id": "run-123",
  "harness_output_dir": ".loopy_loop/sessions/<session_id>/harness_outputs/0001_planner/run-123"
}
```

`result_text.txt`

- Plain text copy of the result text, or empty string on failure

`harness_run_id.txt`

- The `team_harness` run id, or empty string on failure before a run id exists

`pending_finished_request.json`

- Durable handoff record written after `result.json` and before the worker calls
  `/finished`
- Removed after `/finished` is acknowledged or after `/register` recovers it
- If a worker exits in that handoff window, the next `/register` uses this file
  to record the completed task instead of marking it `abandoned`
- If the file is missing but `result.json` exists for the active task, the
  coordinator can reconstruct the finished request from `result.json`

`salvage.json`

- Written into the interrupted iteration's directory during crash recovery,
  when the coordinator applied the recovery policy (`recovery_policy`, default
  bounded drain) to agent processes a dead worker's harness run left behind
- Records the reap reports: which orphaned agents were drained (allowed to
  finish), reaped (killed), or skipped, so the provenance of any surviving
  working-tree edits is auditable rather than a mystery diff
- The iteration is still re-run — its `result.json` never existed and is never
  fabricated; the corresponding history entry carries
  `error="abandoned_after_drain"` instead of plain `"abandoned"`
- Schema: `{"schema_version": 1, "recorded_at": ..., "policy": ...,
  "reaped_runs": N, "settled_workers": N, "reports": [...]}`

## Control Contracts

`control.json`

- Read only from the session directory
- Initial schema:

```json
{
  "state": "running",
  "reason": "session active",
  "stop_reason": null,
  "schema_version": 1
}
```

- Stop schema:

```json
{
  "state": "stopped",
  "reason": "Accepted evidence satisfies the goal.",
  "stop_reason": "goal_met",
  "schema_version": 1
}
```

- `stop_reason` must be `goal_met` or `unresolvable_error` when stopped

`goal_check.json`

- Per-iteration eval artifact for `goal_check` or a workflow configured with
  `emits_goal_check=true`:

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

- A valid `goal_check.json` does not stop the loop by itself. A workflow that
  wants to stop must update session `control.json`.

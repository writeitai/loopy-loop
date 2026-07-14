# design_director — the top-level programme loop

The director judges the overall state of a design-phase target repo and dispatches one
stage child session at a time, choosing the child's workflow set from the five stage
sets this template ships: `design_investigation`, `design_shape`, `design_bind`,
`design_harden`, and `design_phase_review`. (A deployment may add its own implementation
set — e.g. `inner_outer_eval` — to the palette, but none ships or is selectable here.)
It is the only set that concludes the programme. The full artifact contract every set
obeys is `plan/README.md` in the target repo.

Workflows: `planner` (judge + programme log) → `dispatcher` (one child request) →
`goal_check` (final programme gate, `emits_goal_check`, sole writer of `goal_met`).

Assumptions: loopy-loop ≥ 0.5.0 (eval-banana on PATH, child `workflow_set` per request,
atomic `*.json` child-request publication); the repo was scaffolded by
`loopy init --template design_loop` (this palette + the `plan/` skeleton +
`.eval-banana/config.toml` + `design_goal.md` seed all land together). Run the director
on the strongest available coordinator model.

Shipped checks (`eval_checks/`, ids `dl_dir_*`) gate only the programme conclusion.
`goal_check` runs them IN PLACE with `eval-banana --check-dir` (never copied, never
auto-discovered — `.loopy_loop` is excluded from bare-run discovery in
`.eval-banana/config.toml`).

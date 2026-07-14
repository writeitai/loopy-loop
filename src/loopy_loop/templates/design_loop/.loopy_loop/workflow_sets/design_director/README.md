# design_director — the top-level programme loop

The director judges the overall state of a design-phase target repo and dispatches one
stage child session at a time, choosing the child's workflow set from the palette
(`design_investigation`, `design_shape`, `design_bind`, `design_harden`,
`design_phase_review`, later implementation sets). It is the only set that concludes the
programme. The full artifact contract every set obeys is `plan/README.md` in the target
repo; the design-loop's own design rationale lives in the writeitai
writeit-loops-and-standards repository.

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

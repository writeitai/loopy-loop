# design_investigation — one bounded investigation, graduated or bust

A stage child set dispatched by `design_director`. It advances one investigation brief
through acquisition → evidence memos → proposal-fit → verification → synthesis, writing
the durable tree under `plan/analysis/research/<id>/`. Its goal check passes only when
the synthesis is verified AND durable — graduation is the stopping condition, so an
investigation cannot finish while its evidence sits only in gitignored scratch.

Workflows: `investigator` (producer; parallel memo fan-out via team-harness inside a
turn) → `research_critic` (refute coverage, facts, saturation, alternatives; every 3
investigator successes) → `goal_check` (runs shipped checks, terminates the child).

Write scope (enforced fail-closed by `dl_inv_write_barrier`): may write
`plan/analysis/**`, `plan/proposals/**`, `questions.md`, `_additional_context/`
(gitignored cache). Must not touch seeds, the programme log, `decisions.md`,
`plan/designs/`, `plan/requirements/`, `plan/plans/`, `plan/implementation_evals/`.

Terminates on "round complete and recorded" (D7): open unknowns become Q-entries and
`project_state/outcome.md` recommendations — not reasons to keep running.

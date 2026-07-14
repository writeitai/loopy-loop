# design_shape — frame, proposals, draft requirements

A stage child set dispatched by `design_director`. It turns accumulated evidence into
the pre-binding layer: the problem frame, competing PRP proposals per core area, and
DRAFT requirements — comparing rough shapes without committing to any. Divergence
discipline is conditional per core area (contested / constrained / non-architectural),
not a flat minimum.

Workflows: `shaper` (producer) → `divergence_critic` (attacks strawmen, proposal
monopolies, frame gaps; every 3 shaper successes) → `goal_check`.

Write scope (enforced fail-closed by `dl_shp_write_barrier`): may write
`plan/analysis/**`, `plan/proposals/**`, `questions.md`, and `plan/requirements/**`
**as DRAFT-marked files only** (`dl_shp_requirements_draft_only`). Must not touch
seeds, the programme log, `decisions.md`, `plan/designs/`, `plan/plans/`,
`plan/implementation_evals/`.

Terminates on "round complete and recorded" (D7): the shape round's deliverables exist
and are honest; whether they suffice to start binding is the director's judgment
(informed by a design_phase_review dispatch if it wants one).

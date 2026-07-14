# design_bind — disposition proposals into binding architecture

A stage child set dispatched by `design_director`. The ONLY set that writes binding
artifacts: it dispositions proposals into decisions, writes/updates binding designs and
current requirements, and propagates each accepted change atomically across the corpus.
This is the single most load-bearing boundary: research and shaping never bind.

Workflows: `designer` (producer) → `binding_reviewer` (independent review of the
binding package — the ugm media lesson: a correct synthesis can still be mistranslated
into an incomplete package; every 2 designer successes) → `goal_check`.

Write scope (enforced fail-closed by `dl_bnd_write_barrier`): may write `decisions.md`,
`plan/designs/**`, `plan/requirements/**`, `plan/proposals/**` (dispositions),
`questions.md`, `plan/analysis/**` (reconciliation notes). Must not touch seeds, the
programme log, `plan/plans/`, `plan/implementation_evals/`,
`plan/analysis/phase_reviews/`.

Terminates on "round complete and recorded" (D7): the assigned binding package is
integrated and reviewed; remaining open items are routed, not resolved by fiat.

# design_harden — attack and operationalize a coherent binding snapshot

A stage child set dispatched by `design_director` once a binding snapshot exists. It
runs the fixed review chain: `questions_gardener` → `red_team` → `coherence_review` →
`roadmap_writer` → `implementation_eval_writer` → `goal_check` (a `must_follow` chain;
each runs once per pass, re-running if the gate fails).

The set's product is **evidence and operational artifacts**: numbered objections (O-)
and review findings (F-) recorded with status `open` and proposed corrections, stamped
with the corpus digest they reviewed, plus a build roadmap and implementation eval
checks. Dispositioning the findings is the bind set's job when the director routes
them. Semantic repairs to binding docs are NOT its
job — accepted findings are routed back to the director for a `design_bind` round.

Write scope (enforced fail-closed by `dl_hrd_write_barrier`): may write
`plan/analysis/**` (objections, design_reviews), `questions.md`, `plan/plans/**`,
`plan/implementation_evals/**`. Must not touch seeds, the programme log,
`decisions.md`, `plan/designs/`, `plan/requirements/`, `plan/proposals/`.

Digest discipline: red_team and coherence_review stamp their reports with the corpus
digest (sha256 over the binding docs) so `dl_hrd_reviews_current` can verify the review
matches the corpus as it stands — any later binding edit stales the evidence.

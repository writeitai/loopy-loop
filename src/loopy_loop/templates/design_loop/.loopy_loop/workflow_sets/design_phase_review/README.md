# design_phase_review — independent sufficiency evidence for the director

A small stage child set dispatched by `design_director` whenever it wants an
independent, digest-pinned assessment before a judgment call — most typically "is
exploration sufficient to start shaping/binding?", but the Goal can ask about any
transition. The report's VERDICT is evidence the director weighs, never a gate
(decision D3: evidence informs, nothing vetoes).

Workflows: `phase_reviewer` (read-only assessment) → `goal_check`.

Write scope (enforced fail-closed by `dl_phr_write_barrier`): may write ONLY
`plan/analysis/phase_reviews/**`. Everything else — including questions.md — is
protected; gaps the reviewer finds are listed in the report for the director to route.

Terminates when a complete, digest-pinned report exists (D7): a "not sufficient"
verdict is a successful round.

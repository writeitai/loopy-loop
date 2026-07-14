<!-- Merge into the target repo's CLAUDE.md for the design phase. -->

# Design-phase working rules

This repository is in its **design phase**: the product is the document corpus under
`plan/` + `decisions.md` + `questions.md`. Read `plan/README.md` (the artifact contract)
first — it defines every artifact class, its owner, and the evidence discipline. Three
rules are non-negotiable.

## Rule 1 — Every document must be understandable cold

A design or decision doc is read by someone who was not in the session that produced
it. Explain, don't just name: state what a technique is, what problem it solves, and why
it was chosen, with a concrete example. Define jargon on first use; keep technical terms
as anchors in parentheses. The reasoning lives in the doc, not in any session's memory.

## Rule 2 — Design the FULL scope; sequencing lives only in `plan/plans/`

No "Phase 1 / v1 / for now / later / defer / MVP" framing in requirements, designs, or
decisions. Distinguish simplification (removing machinery a simpler mechanism makes
unnecessary at any scale — keep it) from deferral (tagging a piece "build later" — never
in binding docs). A genuine exclusion is a documented non-goal with rationale. Numbers
are measured starting points, not committed constants.

## Rule 3 — The binding boundary constitution

Until this section is tailored to the specific product, the **binding boundary is the
`design_goal.md` seed itself**: its full-scope boundary, explicit non-goals, and any
fixed (identity) technology choices ARE the constitution. Every design must stay inside
them; a design that would change the product's boundary or where authority lives is out
of scope until the seed is amended (which happens only via `updates_from_user`).

A project that needs a sharper boundary than the seed states should replace this
paragraph with it: say where each kind of authority lives, which technology choices are
identity (fixed) versus verifiable hypotheses, which extension points exist, and which
invariants no adapter or adjacent product may bypass — then defend it in every design.

## Process rules

- Constraints are **detection, not prevention** (loopy-loop D8): your session's write
  barrier is an eval check; if you wrote somewhere your set must not, revert it — your
  goal check will not pass otherwise.
- Evidence never strengthens as it flows (`plan/README.md`, evidence discipline).
- Autonomy first (loopy-loop D5): no waiting-for-human states; `updates_from_user` is
  evidence, `unresolvable_error` is the last resort with a specific recorded reason.

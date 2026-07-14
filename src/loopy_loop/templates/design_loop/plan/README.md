# The artifact contract

This repository is in its **design phase**: the durable product is the document corpus
described here, produced autonomously by the design loop (a loopy-loop `design_director`
session dispatching stage sessions). Every agent working in this repo — in any session —
follows this contract. It is the single in-repo reference for what each artifact class
holds, who may write it, and how classes link together.

## Authority and precedence

Four meanings of "true", in precedence order for the current system:

1. `design_goal.md` (root) — the immutable human seed: intent, boundary, constraints.
   Never edited by agents; superseding it means a new programme.
2. `plan/requirements/` — binding **WHAT** the complete system must do.
3. `plan/designs/` — binding **HOW** it works. `plan/designs/overall_design.md` indexes
   every design doc with status `current | planned | folded`. Current designs control
   wherever documents disagree — but an unexplained disagreement is itself a defect to
   record and fix.
4. `decisions.md` (root) — the canonical, indexed **history** of why: stable D-numbers,
   context, rationale, consequences. Refinements append ("Refined by Dn"); withdrawn
   entries remain as tombstones; numbers are never reused.

Everything else is evidence, candidates, critique, or process record — never binding.

## Artifact classes

| Class | Path | May be written by | Holds / rules |
|---|---|---|---|
| Seed | `design_goal.md`, `open_questions_from_author.md`, `investigation_plan.md` | human only | Product intent, boundary, constraints, raw author concerns. Agents read, triage, and cite — never edit. |
| Problem frame | `plan/analysis/problem_frame.md` | investigation, shape | The job, users, domain invariants, vocabulary, what "good" means — self-contained for a cold reader. |
| Research tree | `plan/analysis/research/<id>/` | investigation | `00_question_brief.md`, `sources.md` (acquisition manifest: remote, commit, license, date — makes gitignored `_additional_context/` reproducible), evidence memos, `proposal_fit/`, `verify/{completeness,numbers_facts,invariant_coherence}.md`, `saturation.md`, `04_synthesis.md`. Only the synthesis reconciles; evidence memos never mint decisions or canonical IDs. |
| Analysis (general) | `plan/analysis/` | investigation, shape, harden | Evidence and argument; may be messy or superseded; preserved as the audit trail. |
| Proposals | `plan/proposals/PRP-###.md` + `index.md` | shape, investigation (create); **bind (disposition only)** | Candidate shapes between evidence and commitment. **Normative header (parsed by gates): line 1 `# PRP-###: <title>`; a `Status: <value>` line within the first 10 lines.** Fields: problem, candidate shape (with one worked example), drivers, evidence links (every external fact cites analysis), assumptions/unknowns (Q-IDs), competes_with/composes_with, falsification conditions, expected deltas, disposition. Status: `active` → `absorbed-into-decision` (names D-IDs) \| `rejected` (rationale kept) \| `parked` (reopening trigger). |
| Requirements | `plan/requirements/` | bind (shape may add DRAFT files) | Highest abstraction; capabilities, properties, constraints, non-goals. Exactly one current document; it names what it supersedes. **Draft syntax (parsed by gates): a draft file's FIRST Markdown heading is `# DRAFT: <title>`; promotion to current removes the marker.** |
| Designs | `plan/designs/` | bind | Complete current mechanism, contracts, worked examples, failure behavior, non-goals. **No build sequencing, no MVP/phase/defer hedging** — sequencing lives in `plan/plans/` only. Self-contained per the CLAUDE.md rules. |
| Decision log | `decisions.md` | bind | One entry per material commitment: Decision / Context / Consequences; cites the analysis and the PRPs absorbed **and rejected**; links the binding design. |
| Questions | `questions.md` | any set may append; harden gardens; bind resolves | The living register of what is NOT settled, typed (open decision, missing design, risk, inconsistency, measurement). Resolved items are pruned to a resolved section naming the resolving D-number. Never a hidden second decision log. |
| Objections & reviews | `plan/analysis/objections.md`, `plan/analysis/design_reviews/` | harden (creates, status `open`); **bind (dispositions)** | Numbered findings (O-, F-) with evidence, affected IDs, severity, proposed correction, and status `open | accepted | rejected | superseded`. Harden never dispositions its own findings; the bind round that integrates or rejects a routed finding updates its status in the same transaction (accepted links the D-number; rejected keeps rationale). |
| Phase reviews | `plan/analysis/phase_reviews/` | phase_review | Digest-pinned sufficiency reports (frame, seed triage, landscape coverage, saturation, divergence). Their verdict is evidence for the director, not a gate. |
| Build plans | `plan/plans/` | harden | Roadmap and phases derived from binding docs; order never weakens scope. |
| Implementation evals | `plan/implementation_evals/eval_checks/` | harden | eval-banana checks binding design invariants to the FUTURE implementation (acceptance of code that does not exist yet). Product artifacts — distinct from any session's checks. |
| Programme log | `plan/process/programme_log.md` | **director only** | Append-only record of every dispatch decision: what was dispatched, why, which evidence was cited, what the child returned. |
| Context cache | `_additional_context/` | investigation | Gitignored clones/papers; recreatable from each tree's `sources.md`. |

## ID conventions

`D` decisions · `O` objections · `F` design-review findings · `Q` questions ·
`PRP-` proposals · `R-` requirement anchors (when the requirements doc assigns them).
IDs are stable, grep-addressable, never recycled. Cross-references always carry the
prefix. Provisional labels inside research trees (`P-F1-1` style) never leak into
binding docs — only a bind round assigns canonical IDs.

## Evidence discipline (short form)

Claims flow one way, without strengthening:
`source → evidence memo (with status: VERIFIED-CODE / VERIFIED-PRIMARY /
VERIFIED-SECONDARY / INFERRED / ASSUMED / UNVERIFIED / CONFLICTED / STALE)
→ proposal-fit / synthesis → PRP → decision → design`.
A decision citing an unverified claim as fact, or citing evidence that exists only in a
gitignored scratch directory, is a defect. Unknowns stay visible: labeled assumption,
measurement obligation, or Q-entry — never silently defaulted.

## Session mechanics every agent must honor

- **The repo is the durable state.** Sessions are restartable scaffolding; anything a
  future session needs must land in the files above before your session ends.
- **Write scope.** Your workflow set ships a write-barrier eval check listing paths your
  session must not modify. Nothing physically stops a write — but your session's goal
  check cannot pass until an illegal write is reverted. If you believe the barrier is
  wrong, record that in `project_state/outcome.md`; the barrier changes only as a
  reviewed library change, never mid-session.
- **Baseline.** The first workflow of a stage session records
  `project_state/write_barrier_baseline.json`:
  `{"base_commit": "<git rev-parse HEAD; the empty-tree hash
  4b825dc642cb6eb9a060e54bf8d69288fbee4904 if the repo has no commits>",
  "dirty_at_start": {"<path>": "<sha256 of the file's current bytes>"}}` for every path
  in `git status --porcelain` — if the file is missing, create it before other work.
- **Outcome.** Before a stage session ends, `project_state/outcome.md` states: what was
  produced (paths), what remains open (IDs), and a recommendation for the director's
  next dispatch. This file plus the eval report is all the director reads. Gates parse
  anchored lines where the set's contract names them (`Tree: plan/analysis/research/<id>`
  for investigations; `Report: plan/analysis/phase_reviews/<file>.md` and
  `Verdict: <...>` for phase reviews) — exact form, own line.
- **Commits.** Commit completed artifact changes with descriptive messages as you go;
  the write barrier compares against the session's base commit, so history stays clean
  per session.

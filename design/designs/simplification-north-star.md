# Simplification north star

Status: proposed. Grounding: `design/analysis/protocol-v3-flaws.md`.

This document is the acceptance filter for every other design and every future
feature. It exists because between 0.6.0 and 0.8.0 the system drifted from
"lightly guide capable harnesses toward a goal" to "specify a compliance
protocol that harnesses execute." The three concrete designs
(`single-goal-assignments.md`, `session-layout-and-ids.md`,
`context-and-eval-economy.md`) are applications of these principles.

## Principles

**P1 — The intelligence is in the harness, not the protocol.**
Codex, Claude Code, and Antigravity are the strongest components in the
system. Loopy-loop's job is only: persist state across iterations, route work
between layers, and keep evidence findable. Anything the protocol does beyond
that is second-guessing a frontier model. When choosing between "add a
mechanism" and "add one sentence of guidance and trust the agent," choose the
sentence.

**P2 — One goal, one text.**
An assignment at every boundary (root config → session, dispatcher → child) is
a single descriptive prose goal. Descriptive is good — a goal may be many
paragraphs and should say what done looks like, what matters, and where to
record evidence — but it is *one text written for a reader*, not a schema. If
something feels like it needs a field, write it as a sentence.

**P3 — Mechanics live in role prompts, at the workflow-set level.**
How the loop works, who owns which files, how to publish a child request —
that is the workflow-set's static prompt material, written once per role. It
never belongs in goal files, assignment texts, or per-iteration renders. A
goal file must read like something a product owner wrote.

**P4 — Reviewability is a first-class requirement.**
A human must be able to `ls` their way through a run: every engine-generated
path carries role, ordinal, and a human slug; hashes appear only as short
uniqueness suffixes. If reviewing a decision requires opening a JSON file to
learn what a filename means, the naming is wrong.

**P5 — One tree per session.**
Everything a session produces lives under its session directory. Prunable raw
noise is a clearly-named subdirectory, not a parallel top-level mirror.
Anything an agent *authored* (plans, reports, audits, reviews) is semantic
evidence and lives in the durable part, next to whatever cites it.

**P6 — Ceremony must pay rent.**
Every protocol artifact (receipt family, seal, contract, snapshot, hash pin)
must name the failure it prevented in a real run. On review, an artifact that
cannot point at an incident it would have caught gets deleted. Detection over
prevention (D8): prefer noticing a rare failure after the fact to making every
iteration pay an up-front compliance cost.

**P7 — Tokens are an architectural constraint.**
Context is re-processed every turn, so every byte placed in a prompt or a tool
result is multiplied by the turns that follow it. Defaults: reference files by
path instead of inlining; return cards (a few lines + paths), not reports;
per-iteration rendered header budget ~2 KB; anything bigger goes in a file the
agent can open on demand.

**P8 — Prompts state outcomes and ownership, then stop.**
A role prompt says: who you are, what outcome you own, what you must not own
(one or two boundaries), where things live, and what good judgment looks like
here. It does not enumerate steps, mandate team compositions, fix model names,
or repeat rules another prompt already states. Target: a role prompt fits on
one screen (~60–80 lines); shared rules are stated once in a shared preamble,
not per-prompt.

## Kill list (apply P6 now)

Delete unless a concrete incident justifies each — and record the incident in
`design/decisions.md` if kept:

- `trace_seals/`, `trace_finalization_outbox/`, `protocol_failures/` dirs
- sha256-pinned `inputs[]` + immutable dispatch snapshots (git already
  provides immutability; the goal text can cite paths/commits)
- the 5 structured assignment arrays (→ `single-goal-assignments.md`)
- per-attempt `workflow_snapshot/` (7 files/attempt; keep the one rendered
  prompt.txt already stored in the iteration dir)
- the 18 KB frozen roster/scheduler/contract dump in every rendered prompt
  (→ file by reference)
- hash-keyed receipt filenames (→ role+ordinal names, `session-layout-and-ids.md`)
- `goal_contract.json` / `workflow_contract.yaml` duplication where
  `session.json` + the workflow-set on disk already say the same thing

## How to use this document

- New feature or field → it must cite which principle it serves and survive P6.
- Existing mechanism questioned → same test, in reverse.
- Prompt edits → P3/P8 review: does this line guide judgment, or script
  compliance? Script lines need a justifying incident.

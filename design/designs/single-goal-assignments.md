# Single-goal assignments and prompt diet

Status: proposed. Grounding: `design/analysis/protocol-v3-flaws.md` §A2, §B1–B4, §D3.
Principles applied: P2, P3, P7, P8.

## Problem

The v2/v3 child request splits one outcome into seven typed fields plus hashed
input snapshots; the same fact gets restated in 3–4 grammatical shapes and
materialized in ≥4 files. The rendered iteration prompt is 28–38 KB, ~70%
mechanical ceremony (53 absolute paths, an 18 KB frozen JSON dump). The goal
file in UGM is 6 KB of loop mechanics around a one-line product outcome.
Dispatcher/role prompts are protocol manuals with a few lines of judgment
guidance buried inside.

## Design

### 1. The child request becomes a goal text again

Wire schema (v3.1):

```json
{
  "schema_version": 3,
  "request_id": "phase-0-foundations",
  "workflow_set": "inner_outer_eval",
  "goal": "<multi-paragraph prose>",
  "origin": {"parent_attempt_id": "…", "parent_work_item_id": "…"}
}
```

Dropped: `completion_criteria[]`, `stop_criteria[]`, `constraints[]`,
`deliverables[]`, `required_evidence[]`, sha256-pinned `inputs[]`, the
immutable `dispatch_inputs/` snapshot, and the mandated
snapshot→hash→rename→ledger ordering. Kept: atomic rename of the one request
file (crash safety that has paid rent), request id as idempotency key.

The `goal` is a brief, written the way you'd brief a strong engineer joining
the effort. It *should* be descriptive — including acceptance expectations,
constraints, and pointers to inputs — but as prose, in whatever shape fits the
work. If the dispatcher wants the child to read a phase file, it writes "Start
from `plan/phases/phase-0.md`; treat it as the scope contract." Immutability
of referenced inputs comes from git (cite a commit when it matters), not from
hash pinning.

Dispatcher prompt replaces the envelope contract + "Child goal requirements"
bullets with roughly:

> Write the child's goal as a self-contained brief: the outcome and why it
> matters now, what done observably looks like, constraints worth knowing,
> and where to record evidence. Preserve planner intent; don't decompose into
> leaves — the child owns its own plan. Write it so an agent with no other
> context could start working from it alone.

The child's `goal.md` is that text, verbatim. One authored artifact instead of
four materializations.

### 2. Root-level goal files carry zero loop mechanics

`loopy_loop_goal.txt` describes the product outcome only. Everything currently
in UGM's goal under "Program organization", "Delivery contract", "Evaluation
as evidence", "Recovery and blockers" moves to (or already exists in) the
workflow-set role prompts and is deleted from the goal. Same for
`completion_criteria`/`stop_criteria` in root config: keep the fields
(optional, they render fine when present) but stop using them as a second
place to restate the goal — stock templates should either populate them
meaningfully or omit the section from the render when empty (fixes the
empty-section render of §D3).

### 3. Iteration prompt diet: 2 KB header + files by reference

Rendered header becomes:

```
loopy-loop assignment — iteration 0026, role: outer, session: 03_phase-0-foundations

Goal:
<goal text>

You are inside a durable looping session. Key paths:
- session dir:      <abs path>          (everything below is relative to it)
- project_state/    your durable working state (plan.md, handoff.md, …)
- child_requests/pending/   publish child requests here
- control.json      terminal control
- raw/0026_outer/   scratch + verbose output for this iteration
- paths.json        full path map, rosters, scheduler view — read if needed

Workflow body:
<role prompt>
```

Concretely:
- The 53-path enumeration collapses to the ~6 paths above plus one
  `paths.json` in the iteration dir holding the complete map for the rare
  agent that needs more.
- The 18 KB frozen roster/scheduler/capability JSON is never inlined; it lives
  as files referenced from `paths.json`. The model-tier table stays in the
  system-prompt extension (it's small and behaviorally load-bearing).
- Budget check in CI: rendered header (everything before `Workflow body:`)
  ≤ 2 KB + goal length. A test renders each stock template and fails on
  regression.

Effect: per-turn floor drops ~20k → ~3k tokens, multiplied across every turn
of every harness run (flaw A1 makes this multiplicative, see
`context-and-eval-economy.md`).

### 4. Role prompts: one screen, judgment-first

Rewrite each stock role prompt to the P8 shape (~60–80 lines):
identity + owned outcome; 1–2 ownership boundaries; where things live (by
reference to the header); judgment guidance specific to the role; nothing
else. Shared rules (atomic writes, PR policy, permission policy, goal-source
caveat) move to **one** shared preamble file per workflow set, included once
by the renderer — never repeated per role prompt. Delete from all stock
prompts:
- fixed team recipes and model-name mandates ("using CODEX", "WITH GEMINI!",
  triple plan review) — delegation stays free-form per v3's own language:
  optional, roster-informed, "a preference, not a quota";
- step-numbered runbooks where the step order is obvious from the outcome;
- "think ultra deeply" incantations (effort belongs to tier config).

### 5. Plans and handoffs: orientation first

Replace the mandated multi-field plan/README/ledger formats with one contract,
stated in the outer/planner prompt:

> `project_state/plan.md` is for a successor with zero context. Lead with:
> what this loop is building, current status in a few sentences, what's next
> and why, and open risks. Keep it under ~150 lines; when a section stops
> helping a newcomer, cut or archive it. Provenance (commit SHAs, PR numbers,
> CI run ids, acceptance details) lives in `project_state/ledger.md`
> (append-only), not in the plan.

`handoff.json` stays typed (parents parse it) but slims to: `summary`,
`status`, `open_work[]`, `risks[]`, and refs — with refs pointing at durable
session-relative paths (see `session-layout-and-ids.md`; `trace:<hash>` refs
are retired). The 8-section task README and 9-field finished.md templates are
replaced by "write enough that a cold reader could pick the task up" plus a
2–3 bullet example.

## Migration

1. Engine: accept schema_version 3 alongside 2; dispatcher template writes 3.
2. Renderer: new header + `paths.json`; keep old field names in
   `assignment.json` for one release for tooling that reads it.
3. Templates: rewrite prompts (worst-first: dispatcher, outer); move shared
   rules to the preamble file.
4. UGM: regenerate goal file (product outcome only) before the next program
   session; existing sessions finish on v2.

## Acceptance criteria

- A dispatched child request contains exactly one authored text; no field of
  it restates another field.
- Rendered header ≤ 2 KB + goal (CI-enforced).
- Each stock role prompt ≤ 80 lines; shared rules appear in exactly one file
  per workflow set.
- Root goal files in stock templates contain no loop-mechanics vocabulary
  (loop, iteration, workflow, session, eval cadence…) — grep-testable.
- A reviewer can read `goal.md` + `plan.md` of any session and correctly state
  what it is doing and what happens next, without opening JSON.

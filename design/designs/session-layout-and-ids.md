# Session layout, readable IDs, and the end of the traces mirror

Status: proposed. Grounding: `design/analysis/protocol-v3-flaws.md` §C1–C4.
Principles applied: P4, P5, P6.

## Problem

Engine-generated IDs are timestamp+hash blobs carrying no workflow, role, or
purpose; receipts are keyed by bare attempt hashes; trace paths stack two
hashes before anything readable. The separate `traces/` mirror tree duplicates
the session hierarchy under a second set of hash keys, holds 100 LLM-authored
evidence documents that durable handoffs cite via `trace:<hash>` refs, is
declared prunable — and has already produced a truncated-hash duplicate-dir
bug. Session dirs have ~25 top-level entries.

## Design

### 1. IDs: ordinal + slug + role; hashes only as suffixes

- **Session id**: `NNN_<slug>` where `NNN` is a per-scope ordinal (root
  sessions numbered within the repo, children within their parent) and
  `<slug>` is kebab-case, derived from the request id when present (children:
  `01_phase-0-foundations`) or from the first meaningful words of the goal
  (roots: `001_ultimate-memory-program`). Uniqueness within scope is enforced
  by the ordinal; no random component in the name. Timestamp, goal hash, and a
  uuid stay as *fields in `session.json`* for machine use — they leave the
  directory name.
- **Iteration dirs**: unchanged (`0026_outer`) — this convention already works.
- **Attempt ids**: keep the hex id for the wire protocol, but every on-disk
  artifact keyed by attempt gains the iteration prefix:
  `git_receipts/0026_outer_after.json`, `eval_receipts/0025_eval_runner.report.md`,
  raw dirs `raw/0026_outer/…`. Rule of thumb (P4): a reviewer reading `ls`
  output must be able to say what each entry is without opening it.
- **Harness run dirs**: `raw/0026_outer/harness/` — the run's own
  `run_id` stays inside `run.json`; it no longer names two nesting levels.
- IDs are derived exactly once, at creation, and passed as values thereafter —
  never re-derived by parsing paths or re-hashing content (the class of bug
  behind the truncated trace dir).

Full example after this change:

```
.loopy_loop/sessions/001_ultimate-memory-program/
  children/01_phase-0-foundations/
    iterations/0026_outer/
    project_state/plan.md
    git_receipts/0026_outer_after.json
    raw/0026_outer/harness/run.json
```

versus today's
`sessions/20260717_182101_7dc9a9a9e501_08630977/children/20260717_190245_5c2e83ae6415_71b7c8b0/…`
with evidence at
`traces/20260717_182101_…/sessions/20260717_190245_…/attempts/97521a5ed6b7/harness/20260718_064207_7f654370/acceptance/l07-audit.md`.

### 2. Fold traces back into the session tree

Answering the standing question — was separating traces a good idea? The
*distinction* (raw noise vs durable semantic state) is right; the *mechanism*
(a parallel top-level mirror tree) is not. Verdict: keep the distinction as a
**subdirectory boundary inside each session**, delete the mirror.

- Each iteration gets `sessions/…/raw/<NNNN>_<role>/` holding only
  **mechanically produced** streams: team-harness `run.json`,
  `coordinator_input.json`, worker `stdout.jsonl`/`stderr.log`, git
  `-diff.patch`/`-status.jsonl`, eval `checks/*.stdout.txt`, locks, reap
  reports.
- **Placement rule (the load-bearing line): if an LLM authored it as a
  document — report, audit, review, plan, analysis — it is evidence, and it
  lives in the durable tree** (`project_state/` for state, or the iteration
  dir for per-attempt reports like `iterations/0026_outer/acceptance-audit.md`).
  Agents are told: scratch and verbose dumps → `raw/`; anything another agent
  or human might cite → durable tree. The current situation (100 evidence .md
  files only under prunable traces, cited by handoffs) inverts this.
- Pruning policy attaches to `raw/` only: `.gitignore` covers
  `sessions/**/raw/`; a `loopy prune-raw [--older-than]` command deletes raw
  dirs without touching evidence. `trace:<hash>` refs are retired — evidence
  refs in handoffs/plans become session-relative paths, which are stable,
  human-readable, and survive pruning.
- `trace_manifest.json`, `trace_seals/`, `trace_finalization_outbox/` are
  deleted per the P6 kill list; `result.json`'s `trace_ref` becomes a plain
  relative path into `raw/`.

Why not keep the separate tree with better naming? Two trees require two id
schemes, cross-tree refs, finalization machinery, and seals to keep them
honest — that machinery *is* the complexity, and the truncated-hash bug shows
its cost. One tree with a gitignored `raw/` subdir gets the same
retention/privacy split with zero cross-referencing.

### 3. Narrow the session dir

Top-level of a session dir after consolidation (~12 entries):
`session.json`, `state.json`, `events.jsonl`, `goal.md`, `control.json`,
`project_state/`, `iterations/`, `raw/`, `child_requests/`, `children/`,
`inputs/`, `receipts/` (git+eval+delivery receipt files merged into one dir,
each self-describing per §1). `harness_capability_roster.json`,
`workflow_roster.json`, `workflow_contract.yaml`, `goal_contract.json`
collapse into `session.json` (they are all frozen-at-creation facts about the
same session).

## Migration

- New layout applies to new sessions only; readers support both during a
  deprecation release (path helpers in `sessions.py` already centralize
  construction).
- A `loopy migrate-session <id>` best-effort relinker is optional; UGM's
  in-flight program can simply finish on the old layout.
- Update `docs/session-layout.md` in the same PR (it is already stale against
  the deployed engine — flaw C5).

## Acceptance criteria

- `find .loopy_loop/sessions -maxdepth 4` output is readable: every entry
  names its role/ordinal/slug; no bare-hash filenames outside `session.json`
  internals.
- Zero LLM-authored `.md` files under `raw/` in a full stock-template run
  (spot-check test greps for markdown headers in `raw/`).
- Deleting `raw/` entirely leaves every ref in `plan.md`/`handoff.json`/
  receipts resolvable.
- One id-derivation site per id kind; no path parsing to reconstruct ids.

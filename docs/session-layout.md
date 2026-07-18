# Session Layout

A loopy-loop session is one directory that holds everything the session
produces. Inside it, one clearly-named subdirectory (`raw/`) holds prunable
mechanical noise; everything else is durable evidence and state. This is the
**folded layout**, used by every new protocol-v3 session.

Older sessions use the historical **mirror layout**, where raw attempt traces
lived in a parallel top-level `.loopy_loop/traces/` tree keyed by attempt
hashes. Those sessions keep loading and operating unchanged; the mirror layout
is documented in the [Legacy sessions](#legacy-sessions-mirror-layout)
appendix. Path construction for both layouts is centralized in
`src/loopy_loop/sessions.py`; the layout is recorded as a field in
`session.json` and never re-derived by parsing a directory name.

## Readable session IDs

A new session directory is named for a human reading `ls`, not for a machine:

- **Root session:** `NNN_<slug>` — a repo-scoped ordinal plus a slug derived
  from the first meaningful words of the goal, e.g.
  `001_ship-the-ultimate-memory-program`.
- **Child session:** `NN_<slug>` — an ordinal within the parent's `children/`
  directory plus a slug derived from the child request's `request_id`, e.g.
  `01_phase-0-foundations`.

The ordinal alone makes the id unique within its scope; there is no random
suffix. The id is derived exactly once, at creation
(`sessions.create_session_id`), and passed as a value thereafter. It is never
reconstructed by parsing a path or re-hashing content — the class of bug that
produced a truncated-hash duplicate directory in the mirror layout. The
timestamp, goal hash, and a uuid are kept as machine fields in `session.json`
(`created_at`, `goal_hash`, `session_uuid`); they leave the directory name.

## Recursive session tree

A root session is a direct child of `.loopy_loop/sessions/`. A requested child
is physically nested under its parent's `children/` directory, and the same
shape recurses. Only the deepest live session receives work; a parent with an
`active_child_session_id` is suspended until that child becomes terminal.

```text
.loopy_loop/
├── repository.json                     # checkout identity, not session state
└── sessions/
    └── 001_ship-the-ultimate-memory-program/
        ├── .gitignore                  # ignores raw/ for this session
        ├── session.json                # immutable manifest (+ layout, uuid, hashes)
        ├── state.json                  # engine scheduling source of truth
        ├── events.jsonl                # best-effort observability projection
        ├── goal.md                     # the exact resolved goal for this layer
        ├── goal_contract.json          # goal text bound to criteria + origin
        ├── workflow_contract.json      # frozen role/authority contract
        ├── workflow_roster.json        # frozen scheduled-role roster
        ├── harness_capability_roster.json   # root only; shared by the tree
        ├── control.json                # terminal control
        ├── session_outcome.json        # after any v3 terminal transition
        ├── project_state/              # durable semantic spine (see below)
        │   ├── plan.md
        │   ├── tasks/
        │   ├── current_state.md
        │   ├── decisions/
        │   ├── finished.md
        │   ├── eval_state.md
        │   └── handoff.json
        ├── inputs/                     # immutable + append-only session inputs
        │   ├── user_updates.jsonl
        │   ├── accepted_request.json   # child only
        │   └── artifacts/              # frozen child inputs
        ├── iterations/
        │   └── 0026_outer/             # one durable dir per attempt
        │       ├── prompt.txt
        │       ├── paths.json
        │       ├── result.json
        │       ├── trace_ref.json      # plain session-relative path into raw/
        │       ├── workflow_snapshot/<attempt_id>/…
        │       └── acceptance-audit.md # example per-attempt evidence (durable)
        ├── raw/                        # prunable, gitignored (see prune-raw)
        │   └── 0026_outer/
        │       ├── protocol/           # task_response, assignment, rendered_prompt…
        │       ├── harness/            # team-harness run.json, worker streams
        │       ├── agents/
        │       ├── eval/               # raw eval-banana output
        │       ├── git/                # verbose before/after status + diff
        │       └── service/            # finished_exchange, recovery
        ├── receipts/                   # merged git + delivery receipts
        │   └── 0026_outer_git_after.json
        ├── eval_checks/                # session-scoped eval check definitions
        ├── eval_receipts/              # validated, hash-bound eval verdicts
        ├── child_requests/{pending,accepted,rejected}/
        ├── children.json
        ├── child_outcomes/
        ├── parent_acceptance/
        ├── control_rejected/
        ├── protocol_failures/
        └── children/
            └── 01_phase-0-foundations/
                ├── parent.json
                └── …same per-session files…
```

## Raw vs durable: the placement rule

The distinction between *raw mechanical noise* and *durable semantic evidence*
is the load-bearing one. It is expressed as a subdirectory boundary inside each
session, not a separate tree.

- **`raw/<NNNN>_<workflow>/`** holds only mechanically produced streams:
  team-harness `run.json`, worker `stdout.jsonl`/`stderr.log`, git
  `before/after-status.jsonl` and `-diff.patch`, raw eval output, the captured
  finished exchange, and recovery records. The attempt is identified by its
  iteration prefix; the attempt hash stays available inside the artifacts. Raw
  writers use atomic file writes for crash safety, but nothing here is sealed:
  there is no per-attempt trace manifest, no seal receipt, and no finalization
  outbox.
- **The durable tree** is everything else. The rule agents are told: scratch
  and verbose dumps go to `raw/`; anything you or another agent might later
  cite as evidence — a report, audit, review, plan, or analysis — goes in the
  durable tree (`project_state/` for layer state, or the iteration dir for a
  per-attempt document like `iterations/0026_outer/acceptance-audit.md`), never
  the raw dir.

Pruning attaches to `raw/` only. Each session ships a `.gitignore` ignoring
`raw/`, and `.loopy_loop/sessions/**/raw/` is in the generated repo `.gitignore`
and the templates. The command

```text
loopy prune-raw [--older-than DAYS] [--session ID] [--legacy-traces]
```

deletes raw attempt directories (optionally filtered by age or session) and,
with `--legacy-traces`, the historical `.loopy_loop/traces/` mirror. It never
touches the durable tree, so every reference in `plan.md`, `handoff.json`, or a
receipt stays resolvable after pruning.

An iteration's reference to its raw artifacts is a plain session-relative path:
`iterations/<NNNN>_<workflow>/trace_ref.json` records
`raw/<NNNN>_<workflow>` (and the logical `session:/raw/<NNNN>_<workflow>` ref).
There are no `trace:<hash>` references in a folded session.

## Receipts

New sessions keep git boundary and delivery receipts in one `receipts/`
directory with self-describing names. An engine-authored git receipt is
`receipts/<NNNN>_<workflow>_git_<phase>.json` (e.g.
`0026_outer_git_after.json`), so a reviewer reading `ls` can tell what each
entry is without opening it (P4).

`eval_receipts/` keeps its own directory in both layouts. Eval-receipt
filenames are eval-id keyed (already self-describing), and each receipt's raw
report is bound to the producing attempt by identity — a passing receipt cites
`session:/raw/<NNNN>_<workflow>/eval/report.json` and the matching
`receipts/<NNNN>_<workflow>_git_after.json`. Accepted receipts get an
engine-owned sidecar in `eval_receipts/accepted/<eval_id>.json`.

## Attempt artifacts

Every modern attempt has a durable iteration directory
`iterations/<NNNN>_<workflow_id>/`:

```text
iterations/0026_outer/
├── prompt.txt                 # the rendered assignment
├── paths.json                 # full machine path map, rosters, scheduler view
├── result.json                # compact iteration result (recovery-critical)
├── result_text.txt
├── harness_run_id.txt
├── trace_ref.json             # plain session-relative path into raw/
├── workflow_snapshot/<attempt_id>/{assignment.json, scheduler_view.json, …}
└── goal_check.json            # optional/legacy projection
```

`materialize_workflow_snapshot()` freezes the scheduler-selected workflow
configuration, prompt, contract, root execution snapshot, and hashes.
`assignment.json` binds one actor, objective, checkout, session, workflow,
iteration, and attempt. The worker verifies the snapshot, reconstructs the
assignment independently, and checks the coordinator's frozen assignment
SHA-256 before calling the harness. The rendered prompt prints the
authoritative assignment path and every named absolute path (including the
iteration's raw scratch dir); agents use those values rather than infer paths
from cwd or directory names.

## Protocol-v3 semantic state spine

`sessions._create_v3_semantic_spine()` scaffolds the same compact state in
every root and child. The coordinator scaffolds headings and a revision-zero
handoff but does not parse plan prose, choose tasks, or judge semantic
sufficiency; the layer orchestrator (`outer` or `planner`) keeps the spine
coherent.

| Path | Accountable owner | Purpose |
| --- | --- | --- |
| `project_state/plan.md` | layer orchestrator | Outcomes, dependencies, revision, active selection, replanning triggers |
| `project_state/tasks/` | orchestrator; leaf/dispatcher may add evidence | Stable per-task objective, status, dependencies, accepted evidence |
| `project_state/current_state.md` | orchestrator | Short resumption view: active outcome, blockers, risks, next decision |
| `project_state/decisions/` | orchestrator | Durable choices and rationale later attempts should not rediscover |
| `project_state/finished.md` | orchestrator | Append-only accepted-work ledger with commit, PR, test, review, delivery refs |
| `project_state/eval_state.md` | orchestrator; eval roles add observations | Optional evaluation intent, observations, disagreement, provenance |
| `project_state/handoff.json` | orchestrator | Rolling semantic summary for a parent or operator |

`handoff.json` binds the session and goal, a monotonically increasing revision,
the producing workflow/attempt, a summary, and flexible accepted/open/risk/
decision/evidence/delivery/eval lists. A missing, malformed, or non-monotonic
handoff does not override an authentic orchestrator completion decision.

## Frozen identity, scope, and role contracts

- **`goal.md` / `goal_contract.json`** — the exact resolved goal for this layer
  bound to completion/stop criteria, constraints, deliverables, required
  evidence, and origin metadata. A child's contract also binds its accepted
  request and every declared parent input by logical reference and SHA-256, with
  the exact bytes copied into the child's own `inputs/` tree.
- **`session.json` / `parent.json`** — the immutable manifest:
  session/root/parent identity, depth, workflow set, layer kind, goal and
  contract hashes, layout, a `session_uuid`, origin provenance, and creation
  time. A child also has `parent.json`.
- **`workflow_contract.json`** — the session-frozen role and authority
  contract. Protocol v3 separates `orchestration` (one role owning the plan,
  handoff, and completion decision) from `evaluation` (optional check authors
  and runners producing advisory evidence) and
  `terminal_blocker_reporting_roles`.
- **`workflow_roster.json`** — every scheduled role with its responsibility,
  cadence, expected outputs, and authorities.
- **`harness_capability_roster.json`** — created once at the root and referenced
  by every descendant; freezes the configured harness coordinator, enabled
  delegate families, and each family's `frontier`/`strong`/`standard`/`economy`
  model bundle. Guidance and audit context, not a policy gate.

The complete parsed contract also lives in engine-owned `state.json`. Before
dispatch, the coordinator restores the on-disk contract and roster projections
from that trust root and hash-pins them into the attempt snapshot.

## Coordinator state, control, and outcome

- **`state.json`** is the engine-owned scheduling source of truth: current task,
  iteration history, failure and usage ledgers, active-child pointer, frozen
  contracts, capability-roster trust root, latest handoff revision/hash,
  accepted eval-receipt seals, and terminal state. Agents receive its absolute
  path as read-only engine state and request transitions by publishing typed
  workflow-owned files.
- **`events.jsonl`** is a best-effort post-commit observability stream.
  Consumers tolerate duplicates, gaps, and a truncated last line and dedupe by
  `event_id`. Scheduling and recovery use `state.json`, not this projection.
- **`control.json`** begins as a neutral running record. A v3 terminal record
  must identify the exact current session/workflow/attempt. `goal_met` comes
  from the contract's `orchestration.completion_role`; it may cite logical
  evidence, zero or more accepted eval receipts, and the canonical handoff. No
  eval receipt, passing verdict, or same-attempt eval is required.
  `unresolvable_error` is the D5 last resort and must list autonomous routes
  already tried. There is no paused or waiting-for-human state.
- **`session_outcome.json`** is written after any v3 terminal transition. It
  binds session/root/goal identity, terminal status, the frozen transition
  revision/timestamp, the accepted control hash, handoff status, and evidence,
  delivery, and accepted-eval references. In the folded layout there are no
  trace-seal references. The coordinator stores accepted terminal-control and
  valid-handoff bytes in `state.json`, so a restart cannot rewrite the terminal
  basis.

## Child request and acceptance records

A recursive workflow atomically publishes a request under
`child_requests/pending/`. A valid request is copied unchanged into
`accepted/`, indexed in revisioned `children.json`, and frozen into the child;
an invalid request is archived in `rejected/` with its original hash and
diagnosis. A v3 parent's `child_outcomes/<request_id>.json` references the
child's `session_outcome.json` by logical reference and hash; the parent
orchestrator separately records `accepted`, `rework`, or `reroute` in
`parent_acceptance/`. A child's `goal_met` never completes its parent.

## Logical references and atomicity

Durable references use `repo:/…`, `session:/…`, `root:/…`, `parent:/…`, and
`session:<session_id>:/…` forms; `LogicalReferenceResolver` validates declared
topology, physical nesting, symlinks, and scope before returning a confined
absolute path. (The `trace:<manifest_id>:/…` form is mirror-only.)
Engine-owned recovery-critical JSON uses same-directory temp files plus atomic
replace; JSONL inputs are append/flushed/fsynced per record.

## Legacy sessions (mirror layout)

Sessions created before the folded layout keep their frozen shape and finish
under it. The implementation reads them without reinterpreting them:

- **Session ids** are `YYYYMMDD_HHMMSS_<goalhash12>_<random8>`. Id validation
  accepts both this and the new `NNN_<slug>` form.
- **Raw attempt traces** live in a parallel top-level mirror tree,
  `.loopy_loop/traces/<root>/sessions/<session>/attempts/<attempt_id>/`, with a
  per-attempt `trace_manifest.json`, a hashed inventory, and the same
  `protocol/harness/agents/eval/git/service` subareas. Durable evidence cites
  raw reports via `trace:<manifest_id>:/…` references.
- **Sealing and finalization.** Each finalized trace is sealed into a hashed
  inventory anchored by a compact `trace_seals/<attempt>.json` receipt; a
  `.loopy_loop/trace_finalization_outbox/` provides crash-safe finalization.
  `loopy traces list` / `loopy traces inspect` read these manifests, and
  `session_outcome.json` carries `trace_seal_refs`.
- **Receipts** are split into per-family directories with hash-keyed names:
  `git_receipts/git-<phase>-<attempt>.json`, `eval_receipts/`,
  `delivery_receipts/`. A mirror session also keeps `harness_outputs/` (a
  legacy-compatible projection) and, for v1/v2, `eval_readiness/`.

Frozen v1/v2 protocol semantics (identity-bound control, same-attempt passing
eval for v2 completion, `updates_from_user.md`, flat child requests) are tied
to the session's frozen contract, not renegotiated by a worker.

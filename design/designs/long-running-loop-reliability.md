# Design: Reliability and Operations for Long-Running Loops

**Status:** Historical binding baseline for releases 0.3.0–0.6.0; retained
mechanisms remain in 0.7, while the recursive v2 contract supersedes the
explicit limitations identified below
**Date consolidated:** 2026-07-15
**Applies to:** the coordinator/worker runtime, planner/dispatcher child sessions,
crash recovery, operational telemetry, failure containment, and model selection.

This document explains the long-running reliability mechanisms shipped through
0.6.0, whose historical identifiers (P0.1, P1.1, and so on) still appear in
code comments, tests, and the changelog. Those mechanisms remain binding where
the 0.7 design does not refine them. Work that did not ship is kept separately in
[`../proposals/improvement-proposals.md`](../proposals/improvement-proposals.md).

The implemented 0.7 design in
[`recursive-loop-layer-contract.md`](./recursive-loop-layer-contract.md)
generalizes the same depth-first session edge, replaces the v1 child protocol,
and adds stronger assignment, evaluation, Git-subject, and trace contracts. It
is authoritative for fresh v2 sessions. Statements below about a two-level
limit, flat child requests, the global team-harness run-record mismatch, or
missing grandchildren describe the shipped 0.3–0.6 baseline and legacy v1
resume behavior, not fresh 0.7 sessions.

The design is driven by three existing decisions:

- files and git are the durable source of truth (D1);
- exactly one loopy worker owns an assignment at a time (D2); and
- evaluation artifacts decide work quality; transport and recovery code must
  not manufacture semantic success (D3/D4).

Together, the mechanisms below let the planner/dispatcher double loop run for a
long time without losing its place, duplicating verifiably live local work after
a crash, or hiding cost and failure state from an operator. Remote or otherwise
unverifiable worker identity has a narrower guarantee, documented below.

---

## Durable parent→child session state

### The parent records which child suspends it

When a top-level workflow dispatches a child, the parent's `LoopState` records
`active_child_session_id`. The child records `parent_session_id`, while
`children.json` is an audit index of every dispatched child and its outcome. The
pointer in `state.json`, not the presence of a request file, determines which
session is active.

On `--resume`, `CoordinatorService._reconstruct_session_stack()` in
`src/loopy_loop/coordinator_app.py` walks parent→child pointers to the deepest
live session. It finalizes a child already terminal on disk and resumes its
parent. It also repairs interrupted handoffs: a dangling pointer is cleared,
and a fully-created running child whose final parent-pointer write did not land
can be adopted from `children.json` by `_adoptable_child_id()`.

This solves a specific crash case. Before the pointer existed, restarting while
a child ran reopened the latest top-level session; the child stayed marked
running but became unreachable, and the parent could dispatch duplicate work.
Now the relationship is reconstructable from files alone.

`src/tests/test_session_stack_recovery.py` covers restart during a child,
restart after child termination but before parent resume, dangling-pointer
repair, child adoption, and terminal-child finalization.

### The handoff is staged and recoverable, not a multi-file transaction

The filesystem cannot atomically commit parent `state.json`, child `state.json`,
`children.json`, and the consumed request as one unit. Instead,
`_dispatch_child_session_if_requested()` writes them in a discoverable order and
startup reconciliation handles every crash window.

For a legacy v1 flat child request, the originating filename is stored on the
child record. While that record is running, `_dispatched_request_files()`
treats a leftover request file as a tombstone, so a crash between recording and
unlinking cannot dispatch it twice. Invalid or unusable v1 requests are renamed
to `*.json.rejected` with the reason logged; the rejected file itself does not
gain a structured reason. A completed child's old filename may be reused for
genuinely new v1 work.

Fresh v2 sessions strengthen the same staged transition with request IDs,
immutable accepted bodies, child-local input copies, structured rejection
receipts, and bounded ledger reconstruction. Those details are specified in
the recursive v2 design rather than retrofitted into this historical v1
description.

The 0.3–0.6 implementation intentionally supported a depth-first, two-level
tree: only a top-level session dispatched one child at a time. Version 0.7
removed that historical depth guard after the same typed edge passed
three-depth dispatch/recovery/unwind tests. D2/D6 still require one deepest
active assignment and one active child per ancestor; they do not require a
depth-two limit.

### One transition function enforces the suspended-parent invariant

`CoordinatorService._advance()` is the common transition used after register,
normal completion, and recovered completion. Its fixed order is:

1. if the state is a suspended parent, replay the live child's task or finalize
   a terminal child and continue on the parent;
2. apply session control and stop precedence;
3. dispatch a pending child request after a successful parent workflow;
4. choose the next eligible workflow;
5. stamp and persist the new `CurrentTask`; and
6. return the corresponding run/stop response.

Centralizing this sequence prevents formerly duplicated register/finish paths
from drifting. In particular, a parent with a live child cannot acquire its own
task; a duplicate completion retry receives the child's live task instead.

---

## Dispatch identity, ownership, and crash-consistent artifacts

Every dispatch has a fresh `attempt_id` on `CurrentTask` and `TaskResponse`. The
worker echoes it in `/finished` and records it in `result.json`. Coordinates such
as `(session_id, workflow_id, iteration)` identify the logical slot but do not
prove that a completion or artifact came from the live dispatch rather than a
superseded one. Attempt matching in `CoordinatorService._finish_assignment_locked()` and
`_read_recoverable_finished_request()` fences stale requests and stale local
artifacts without turning them into current results.

The worker also sends `{hostname, pid, starttime}` on `/register` and every
`/finished`. The coordinator stores this `WorkerIdentity` on the live
`CurrentTask`, making ownership and same-host liveness verifiable. `starttime`
is a pid-reuse-proof process token supplied by team-harness. There is no periodic
heartbeat file: `src/loopy_loop/worker_identity.py` probes the recorded process
identity directly.

Recovery-relevant artifact mutations are individually atomic. The helpers
`write_text_atomic()` and `write_json_atomic()` in `src/loopy_loop/sessions.py`
write a unique temporary file, flush it, and replace the destination. They are
used when publishing iteration results and text, prompts, harness ids,
`pending_finished_request.json`, later `children.json` mutations, and
`salvage.json`. Initial session scaffolding includes direct writes, so this is
not a blanket guarantee for every engine-created file. For the recovery-relevant
publications above, a process crash may leave an earlier complete version or
omit a later file, but it cannot leave a truncated artifact that recovery
mistakes for valid evidence.

This guarantee does not extend magically to workflow-authored files such as
`control.json`, `goal_check.json`, or child requests. Packaged prompts instruct
workflows to publish those via temporary-file-plus-rename. The engine validates
them and never trusts torn/invalid content as evidence: invalid child requests
are rejected, invalid expected goal checks count toward `goal_check_broken`, and
invalid legacy v1 control stops with `invalid_control_output`. In v2, invalid
terminal control is archived with a protocol-failure receipt and restored to
running for bounded autonomous repair. The crash model is process crash
consistency, not power-loss durability across every file (there is no
multi-file fsync transaction).

---

## Worker liveness and orphaned-agent recovery

Coordinator recovery and worker recovery are different problems:

- A **coordinator crash** does not kill agent CLIs, because they are children of
  the separate worker process. The durable session stack and result artifacts
  are sufficient to resume.
- A **worker crash** can orphan agent CLIs which keep spending money and writing
  to the checkout. State reconstruction alone would make immediate redispatch
  unsafe because two writers could overlap.

On a second `/register`, `CoordinatorService._raise_if_worker_alive()` first
checks the recorded worker identity. If that exact local process is alive, the
coordinator refuses the second worker with HTTP 409. If liveness is false or
unverifiable and a complete `pending_finished_request.json` or `result.json`
exists, the coordinator records that real result. Otherwise it performs orphan
recovery outside the state lock, so `loopy status` and `loopy stop` remain
responsive during a potentially long drain.

The liveness guarantee is deliberately same-host. A remote hostname, a missing
start-time token, or an unavailable process-identity provider produces
"unknown," not "dead"; registration falls through rather than treating the
worker as verifiably alive. Same-host orphan reaping is still attempted when
team-harness has usable run records, but process reaping itself is same-host. A
remote worker is skipped and receives legacy abandonment because its processes
cannot be reached from the coordinator host. Deployments that separate
coordinator and worker hosts therefore do not get the local duplicate-worker
proof and must account for
that limitation operationally.

D7 assigns the actual agent-process mechanism to team-harness, which owns the
processes it spawns. `recover_interrupted_iteration()` in
`src/loopy_loop/recovery.py` discovers the interrupted harness runs and invokes
team-harness's process-group operation:

- `drain` (the default) lets in-flight agents finish within one shared bounded
  timeout, preserving near-complete edits; team-harness reaps any process that
  remains past that timeout; or
- `reap` terminates the tracked processes immediately.

For an identity-tracked run, any reaper report that says a process may still be
writing makes recovery refuse replacement work. This fail-closed rule cannot be
claimed for the legacy/remote cases described above, where identity or reaping
is unavailable and registration falls back to abandonment.

Whenever at least one harness run was processed, recovery atomically writes
`salvage.json` with the policy, counters, and team-harness reaper reports — even
if every report is unsettled and registration is then refused. The history code
changes from `abandoned` to `abandoned_after_<policy>` only when at least one
orphan reached a settled outcome. The salvage file does not contain a
loopy-generated diffstat.

Drained agents may have left useful edits in the git working tree, but the task
which entered recovery has no coordinator-owned iteration result. Synthesizing
`result.json` from agent output would falsely close an iteration and violate D3.
Git preserves the work and `salvage.json` preserves its provenance. If the crash
abandonment does not itself trip `workflow_consecutive_failures_cap`,
`max_turns`, or another stop condition, normal scheduling continues and a later
real iteration can evaluate those edits; otherwise the session stops without
claiming the drained work succeeded. Recovery does not guarantee that the same
workflow is selected next.

`src/tests/test_worker_liveness_recovery.py` covers live-worker refusal,
dead-worker reclaim, stale-owner completions, bounded recovery, unsettled-writer
refusal, and salvage records.

---

## The planner/dispatcher template is executable from a clean init

The `pm_planner_dispatcher` workflow set is a parent template whose dispatcher
creates child sessions using `inner_outer_eval`. That child workflow set is a
hard dependency of the template, not an optional example.

`PACKAGED_TEMPLATE_EXTRA_SOURCES` in `src/loopy_loop/cli.py` therefore copies the
canonical `inner_outer_eval` workflow files whenever
`loopy init --template pm_planner_dispatcher` runs. The files are sourced from
the child template itself rather than duplicated under the PM template, so the
two cannot drift.

The acceptance contract is more than file presence:
`test_clean_pm_init_can_dispatch_an_inner_outer_eval_child()` in
`src/tests/test_cli.py` starts from an empty directory, initializes the PM
template, dispatches a child, and runs its first assignment through the worker
path with the child's own goal.

The stock dispatcher also separates its mutable parent ledger from child input
bytes. It atomically freezes one selection under
`project_state/dispatch_inputs/<request_id>.json`, hashes that file in the
child request, publishes the request, and only then marks the ledger item as
waiting. This ordering keeps coordinator hash validation stable while the
parent continues tracking lifecycle state.

---

## Operational events, usage, and cost

### `state.json` is truth; `events.jsonl` is a legibility projection

`src/loopy_loop/events.py` writes one schema-versioned JSON line for significant
transitions such as session start/stop, task dispatch/finish, abandonment,
goal-check evidence, and child start/finish. Each session, including a child,
has its own stream.

Events are appended **after** the state mutation commits. This ordering protects
correctness: an append failure or crash can create a gap, but it cannot roll back
the durable state. Replayed finalization can also duplicate an event. Consumers
must key on `event_id`, tolerate gaps/duplicates, and ignore a torn final line.
No scheduler or recovery decision may depend on the event stream.

The CLI makes that projection useful without adding a dashboard:

- `loopy status` walks the session stack and reports each session plus subtree
  totals; `--watch` re-renders it;
- `loopy events` prints the deepest active session stream; `--follow` switches
  streams as children start or finish, and `--json` preserves raw envelopes.

These commands live in `src/loopy_loop/cli.py`; stream and CLI behavior are
covered by `src/tests/test_events_and_usage.py`.

### Unknown usage stays unknown

The worker's `_read_harness_usage()` reads coordinator-model turn usage from
team-harness's `run.json`. It cannot measure the separate Codex/Claude/Gemini CLI
accounts. Missing records return `None`, not zero; partially measured runs keep
their known token subtotal but count as an unknown iteration.

**Historical integration defect (team-harness 0.4.0; resolved by the 0.7
contract).** Team-harness wrote its
complete `run.json` to `~/.team-harness/runs/<run_id>/run.json`, while the worker
looked for it under the caller-supplied session harness-output directory.
Recovery used the global path, confirming the mismatch. Ordinary successful
runs therefore recorded usage as unknown and could not trigger
`max_cost_usd`. Loopy 0.7 with team-harness 0.5 instead uses one caller-owned
canonical run directory returned explicitly on success and structured failure;
the real integration is tested. Unknown or partial provider usage still remains
honestly unknown.

`LoopState.usage_totals` is the durable per-session ledger. When a child
finalizes, its totals are copied to its `children.json` record;
`session_tree_usage_totals()` derives a parent's subtree total without
double-storing it. A resumed pre-ledger session reconciles historical iterations
as usage-unknown rather than pretending they cost nothing.

When a run record is available and `model_prices` is configured,
`estimate_cost_usd()` prices the known coordinator tokens. The intended budget
branch stops a session when its subtree estimate reaches `max_cost_usd`, and
preflight rejects a budget without prices. Until the defect above is fixed, that
branch has no known cost to compare. Even after the fix this is an estimate with
an explicit blind spot, not a claim to total agent spend.
The shipped budget is session-subtree-wide; per-workflow and wall-clock budgets
were deliberately left out until a concrete need appears. The budget and prices
are coordinator-side root settings loaded when the coordinator starts (and
reloaded on resume), not fields frozen in the worker config snapshot. While a
child is running, the coordinator checks that currently loaded shared threshold
against the child's known measured subtree only; the suspended parent can
include child spend only after finalization and the child cannot see prior
parent/sibling spend. The threshold is checked after results and can overshoot by
an iteration; it is not a single global hard cap across an in-flight parent and
child.

---

## Failure containment

Failed iterations record one `FailureKind` from `src/loopy_loop/models.py`:

- `transient`: team-harness/provider marked the failure retryable and its own
  retries were exhausted;
- `deterministic`: a confirmed auth/config/request failure that retrying cannot
  fix;
- `crash`: the iteration was abandoned by the worker-crash recovery path (this
  label does not prove that an unverifiable remote worker actually died); or
- `unknown`: the available evidence cannot support a stronger classification.

`classify_failure_detail()` in `src/loopy_loop/harness_runner.py` derives the
classification from team-harness's structured detail where available. The kind
is carried through `result.json`, `/finished`, and session history; normal
`task_finished` events include it as well. Crash abandonment is explicit in
history and the `iteration_abandoned` event even though that event's payload
does not repeat the `failure_kind` field. A stopped run is therefore diagnosable
without scraping prose logs.

`CoordinatorService._track_workflow_failure_cap()` maintains a durable
consecutive-failure counter per workflow. A success resets that workflow's
counter; reaching `workflow_consecutive_failures_cap` (default 5) stops the
session with `workflow_failure_cap`, including when crash-abandoned iterations
reach the cap. This prevents one wedged workflow from consuming all remaining
turns.

loopy-loop does not add a second retry/backoff loop. Provider retry policy and
backoff remain in team-harness and are configurable through the
`team_harness_*retry*` fields. Repeating retries at both layers would multiply
latency without adding evidence. `src/tests/test_failure_taxonomy.py` covers the
classifier, durable counters, precedence, child behavior, and wire compatibility.

---

## Dependency, documentation, and model-tier readiness

### Eval tooling is part of the runnable product

The packaged eval workflows invoke `eval-banana`, so it is a normal dependency
in `pyproject.toml`, not an optional install mode. Tool installers such as
`uv tool install` and pipx may expose only the primary package's entry points;
`ensure_interpreter_scripts_on_path()` in `src/loopy_loop/worker.py` finds the
directory containing the installed eval-banana script and appends it to `PATH`.
Appending preserves any target-repo or operator-selected executable that is
already earlier on `PATH`.

Version 0.7.1 requires eval-banana 0.3.5 because eval receipts carry
eval-banana's canonical check-definition digest. The eval runner copies that
digest from `report.json`; `CoordinatorService._validate_eval_receipt_artifacts()`
uses eval-banana's public digest function to recompute it. A raw YAML file hash
is intentionally not substituted for the canonical protocol. That release also
retains the exact judge prompt and uses one collision-safe stem for each
check's result, streams, and deterministic evidence directory, so two check IDs
cannot overwrite each other's trace evidence on a case-insensitive filesystem.

The repository's Agent Skill was rewritten with the 0.5.0 release to describe
the session-local layout, workflow sets, single identity-verified ping-pong
worker, child sessions, attempts, and crash recovery. It lives at
`skills/loopy-loop/SKILL.md`; the README uses the same single-worker contract.
This was necessary because agent-facing setup instructions are effectively an
API surface: confidently generating a removed layout is worse than omitting a
feature. The clean-init tests validate the templates themselves, although the
prose Skill is not mechanically compared with them.

### Named model tiers centralize project-local worker choice

D9 keeps every harness coordinator in a session tree on the same strong
`team_harness_model`. Cost control happens per spawned worker. A root config may
declare `model_tiers` as tier name → agent → `{model, effort}` and an optional
`default_tier`.

`load_root_config()` in `src/loopy_loop/config.py` rejects a `default_tier`
combined with duplicate explicit mappings. `resolve_model_tiers()` then derives
the concrete per-agent defaults from the named default, and
`render_model_tier_guidance()` appends the tier table to every harness
coordinator's system prompt. Workflow prompts can request `economy` or `strong`
without embedding model ids. Children inherit the parent's frozen resolved
config snapshot; editing root YAML mid-session does not silently change their
model policy.

Tier selection is guidance plus audit evidence, never an engine veto (D8). The
harness records requested and effective model/effort per spawn; review or eval
can detect a poor choice and repair it. The engine does not enforce a tier
allowlist or weaken child coordinators by depth.

---

## Deliberate boundaries

The shipped hardening does not imply the following features:

- no parallel loopy workers or concurrent child sessions (D2);
- no human-answer gate or `waiting_for_human` state (D5);
- no independently configured per-child budgets (the configuration exposes one
  root-tree-wide `max_cost_usd`; child-specific limits remain withdrawn as
  needless complexity);
- no per-depth coordinator profiles (D9);
- no agent-authored deterministic checks in the stock eval template (D4);
- no breadth-first child scheduling (D2/D6); deeper v2 chains recurse through
  the same one-child, depth-first edge;
- no synthetic success result for drained work (D3/D7); and
- no claim that coordinator-token cost includes agent-CLI spend.

Changing a D-numbered boundary requires amending that decision. Changing another
current scope choice, such as child-specific budgets, requires an explicit
design update rather than treating the missing feature as a bug.

---

## Historical proposal map

| Review id | Shipped behavior | Release | Primary implementation/evidence |
|---|---|---:|---|
| P0.1 | Durable child pointer, stack reconstruction, attempts, request idempotence, atomic artifacts | 0.4.0 | `coordinator_app.py`, `sessions.py`, `test_session_stack_recovery.py` |
| P0.4 | PM template includes its canonical child workflow set | 0.4.0 | `cli.py`, `test_cli.py` |
| P1.1 | Events, usage/cost ledger, budget, stack-aware status/events CLI | 0.5.0 | `events.py`, `models.py`, `test_events_and_usage.py` |
| P1.3 | Agent Skill and README aligned with the current API | 0.5.0 | `skills/loopy-loop/SKILL.md`, `README.md` |
| P2.1 (part) | Eval dependency/PATH readiness and named worker model tiers | 0.5.0/0.6.0 | `pyproject.toml`, `worker.py`, `config.py`, `test_config.py` |
| P2.2 | One `_advance()` transition | 0.4.0 | `coordinator_app.py`, `test_session_stack_recovery.py` |
| P2.3 | Failure taxonomy and per-workflow failure cap | 0.5.0 | `harness_runner.py`, `models.py`, `test_failure_taxonomy.py` |
| P2.5 | Worker liveness, drain/reap recovery, salvage provenance | 0.3.0 | `worker_identity.py`, `recovery.py`, `test_worker_liveness_recovery.py` |

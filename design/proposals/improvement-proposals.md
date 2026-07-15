# Improvement Proposals

**Status:** Proposed — non-binding, unimplemented work only
**Date reviewed:** 2026-07-15
**Applies to:** possible follow-up work after the reliability and operations
features described in
[`../designs/long-running-loop-reliability.md`](../designs/long-running-loop-reliability.md).

This file contains only proposals that remain relevant and are not implemented.
It is not a description of current behavior. Binding choices live in
[`../decisions.md`](../decisions.md) and implemented designs live in
[`../designs/`](../designs/).

The proposal identifiers are retained from the July 2026 improvement review so
historical commits, tests, and changelog entries remain traceable. Implemented
items from that review moved to the reliability design linked above. Rejected,
withdrawn, or now-unnecessary ideas are intentionally absent; their lasting
dispositions live in the decision log and the implemented design's boundaries.

---

## P1.2 — Target-owned deterministic evaluation backstop

### Current state

The packaged `inner_outer_eval` workflow set uses `harness_judge` checks only.
Its `eval_reviewer` prompt forbids agents from authoring deterministic checks,
because experience showed that implementers invent brittle or gameable checks.
This is the binding D4 policy and remains correct for a generic target repo.

The boundary in D4 is equally important: a deterministic contract that the
target repo already owns is not agent-authored. Examples include the target's
existing `pytest` suite, migration check, import boundary check, or `make test`
target. Such a contract can catch a bad LLM judgment without recreating the
self-authored-check failure mode.

### Proposal

When a target repo has a trustworthy contract suite and the cost of a false
`goal_met` is material, give that target a dedicated child workflow set which:

- keeps the LLM judge for qualitative outcome assessment;
- runs the target's pre-existing contract command as an additional hard gate;
- records the command, exit status, and report as session evidence; and
- derives the deterministic portion of `goal_check.json` from the report rather
  than asking an agent to paraphrase console output.

Do not loosen the stock `inner_outer_eval` rule. The dedicated workflow set owns
the command and its parsing contract; an implementation agent must not create or
rewrite the gate during the run.

### Why it remains conditional

There is no honest generic command loopy-loop can run for every repository.
Adding this to the stock template would either assume a toolchain or return to
agent-authored checks. Implement it with the first high-stakes target that has a
real suite, and test the workflow set against that target's actual report format.

**Effort:** S–M per target workflow set. **Status:** Deferred until a suitable
target exists.

---

## P2.1 — Centralize shipped model defaults and define dependency compatibility

### Current state

Two parts of the original P2.1 are complete:

- `eval-banana` is a normal loopy-loop dependency and the worker makes its
  installed CLI visible to spawned agents; and
- root configs can define named worker `model_tiers`, with D9 fixing the policy
  of one strong coordinator model and prompt-guided, audited per-spawn worker
  choice.

Full per-session execution profiles are not missing work. D9 deliberately
rejects coordinator-model differentiation by parent/child depth for now.

Two narrower maintenance risks remain. First, loopy-loop's own stock model ids
are repeated across `src/loopy_loop/config.py`, the CLI scaffold, packaged YAML,
README examples, and repository-local harness/eval configuration. Model churn
therefore still requires a coordinated edit. Second, published dependencies on
`team-harness` and `eval-banana` have minimum versions but no documented upper
compatibility policy; a breaking pre-1.0 release can be selected by a fresh
install.

### Proposal

1. Make one repository-owned definition the source for model ids used by shipped
   defaults and generated templates. Generate or validate the duplicated
   examples from that definition so drift fails CI. This concerns loopy-loop's
   product defaults; project-local workflow prompts should continue to name
   tiers, not model ids.
2. Define and test the supported loopy-loop × team-harness × eval-banana version
   range. Choose exact pins, compatible upper bounds, or a tested compatibility
   matrix based on observed release discipline, then encode the choice in
   `pyproject.toml` and CI. The goal is reproducible compatibility, not exact
   pinning for its own sake.

Do not revive per-child budgets, per-session/per-child provider profiles, or
weaker child coordinator models as part of this work. Per-child budgets were
withdrawn as needless complexity; per-depth coordinator changes would require
amending D9. A repo-global profile shared uniformly by the whole session tree is
a separate idea and is not ruled out by D9.

**Effort:** S–M. **Status:** Proposed.

---

## P2.4 — Complete operator preflight and active-session control

### Current state

`loopy status` already walks the durable parent→child stack, reports usage and
estimated cost, and supports `--watch`. `loopy events --follow` likewise follows
the deepest active session. Those parts of the original proposal shipped in
0.5.0.

The remaining gap is command-side validation and control. There is no `doctor`
or standalone `validate` command. `loopy stop` mutates only the latest top-level
`StateStore`; while a child is active, that child does not see the request and
the parent cannot honor it until the child terminates. There is also no recorded
force-stop path for a hung worker or orphaned agent processes.

### Proposal

- **`loopy validate`:** expose the existing `run_preflight()` and workflow-graph
  validation without starting a session. Report all actionable configuration,
  prompt, workflow-reference, and required-tool errors in one run.
- **`loopy doctor`:** add environment diagnostics that preflight cannot infer
  from config alone: Python/package versions, installed agent CLIs and auth,
  writable session paths, git state, port availability, and the effective
  model/dependency compatibility information from P2.1.
- **Active-session-aware `loopy stop`:** resolve the durable session stack and
  record a tree-level stop intent that the live child observes at its next
  coordinator check-in, while ensuring the suspended parent cannot dispatch more
  work afterward. This is cooperative and does not interrupt the harness already
  running. Define the multi-file transition and its restart behavior before
  implementing it; do not merely set two independent flags and hope both writes
  land.
- **`loopy stop --force`:** for an explicit operator intervention, terminate the
  loopy-owned worker process, then use team-harness's D7 drain/reap machinery for
  the agent process groups that worker spawned. Finally write a durable terminal
  outcome explaining what was stopped and what cleanup occurred. It must never
  leave a possibly-live writer while dispatching replacement work.

This proposal does **not** add a `paused` or `waiting_for_human` state. Terminating
the coordinator and later using `--resume` can prevent new dispatch after the
in-flight worker finishes or fails its handoff, but it does not pause the running
harness. D5 rejects a preferred human gate and D8 rejects arbitrary preventive
mid-run approval flows.

**Effort:** M. **Status:** Proposed.

---

## Future direction — Deeper depth-first child chains

### Current state

The shipped planner/dispatcher runtime supports a depth-first, two-level tree:
only a top-level session can dispatch a child, and that parent stays suspended
until the child terminates. P0.1 removed the original durability prerequisite
by making the active-child pointer, staged dispatch, stack reconstruction, and
child finalization crash-recoverable. D9 also defines model policy for "any
deeper level," so a deeper sequential chain is not rejected architecture.

The common parent→child machinery already walks durable pointers and rolls a
finished child's whole-subtree usage into its parent. The explicit guard in
`CoordinatorService._dispatch_child_session_if_requested()` still prevents a
session with `parent_session_id` from dispatching a grandchild.

### Proposal

If a concrete target needs hierarchical decomposition beyond one planner and
one implementation loop, generalize the existing depth-first stack without
adding concurrency:

- allow the deepest active session to dispatch one child and suspend until it
  finishes;
- preserve one live task and one live child edge across the entire checkout;
- make restart reconstruction, terminal-child unwinding, request idempotence,
  status/events, stop handling, and usage aggregation work at every depth; and
- add crash-window tests at multiple nested edges before calling the deeper
  form supported.

Do not combine this with breadth-first children or parallel loopy workers; D2
and D6 keep execution sequential on the shared checkout. Do not add nesting
only for abstraction's sake: the existing two-level double loop is simpler and
already adequate for most targets.

**Effort:** M–L. **Status:** Deferred until a target demonstrates a real
three-level decomposition need.

# Design: Running loopy-loop on DeepSWE

- **Status:** Accepted benchmark design; implementation pending; Datacurve submission
  contract pending
- **Date recorded:** 2026-07-13
- **Applies to:** A pinned DeepSWE v1.1 campaign executed through Pier with
  loopy-loop and `team-harness`
- **Analysis:**
  [`../analysis/deepswe-benchmark-assessment.md`](../analysis/deepswe-benchmark-assessment.md)

This document defines how to run a reproducible, externally verified DeepSWE
benchmark of loopy-loop. Its protocol is binding for a DeepSWE campaign unless this
design is amended. It describes the experiment, the Pier integration, the required
accounting and artifacts, and the operational runbook. Acceptance here does not imply
that Datacurve has accepted the submission, and it does not add a new core architecture
decision. Implementation begins only after the open submission questions and resource
limits are settled.

The design must be read with [`../decisions.md`](../decisions.md), especially D1
through D5 and D7, and with
[`success-semantics-and-evaluation.md`](./success-semantics-and-evaluation.md).

## Goal

Produce evidence that answers two different questions without conflating them:

1. **Does loopy-loop's scaffold add value when the model family is held constant?**
2. **How capable is the best disclosed loopy-loop system when `team-harness` may use
   several specialized model families?**

The deliverable is not merely a score. It is a reproducible campaign containing
complete configuration, trajectories, session state, patches, independent verifier
results, failure classifications, and total usage/cost accounting.

## Non-goals

This campaign does not attempt to:

- call a multi-model system the result of any one model;
- replace or reinterpret DeepSWE's existing fixed-`mini-swe-agent` model table;
- prove multi-day project management, crash recovery, or arbitrary parent/child
  planning from DeepSWE alone;
- add multiple concurrent loopy workers to one checkout;
- expose the held-out verifier to the implementation or internal eval agents;
- change `IterationResult.success`, `control.json`, or `goal_check.json` semantics;
- tune prompts, routing, budgets, or model assignments separately for individual
  benchmark tasks;
- optimize against the ten-task integration smoke result and then present the result
  as if the protocol had been fixed in advance.

## Benchmark protocol decisions

These decisions bind this benchmark campaign. They are not amendments to
loopy-loop's Architecture Decision Log.

### B1. Submit loopy-loop as an agent system

The top-level identity is the complete system:

> `loopy-loop <commit> + team-harness <commit> — <configuration name>`

Every model, provider, reasoning setting, role, and fallback used anywhere in a trial
must be disclosed. A heterogeneous result must never be displayed as the score of one
constituent model.

### B2. Run a direct executor control plus homogeneous and heterogeneous loopy arms

The direct `team-harness` arm controls for the executor and native worker interface.
The homogeneous loopy arm then isolates the effect of adding loopy's repeated durable
workflow as far as practical. The heterogeneous arm demonstrates the product ceiling.
None substitutes for the others.

### B3. Treat the DeepSWE verifier as the benchmark authority

The binary reward and structured output from DeepSWE's pristine verifier environment
determine whether a trial passes. The following are diagnostic only:

- `IterationResult.success` — says the harness returned normally (D3);
- loopy-loop `goal_met` / `control.json` — says the workflow decided to stop;
- `goal_check.json` and `eval-banana` output — internal evidence;
- worker exit codes — noisy execution details.

A loopy run that writes `goal_met` but fails the DeepSWE verifier is a benchmark
failure and an observed false-closure event. Nothing in the adapter may translate an
internal loopy signal directly into benchmark reward.

### B4. Freeze one generic protocol across all tasks

All tasks in an arm use the same:

- workflow files and hashes;
- model-role mapping and reasoning settings;
- tool and network policy;
- maximum turns, time, and spend policy;
- retry and infrastructure-error policy;
- adapter version and patch-finalization behavior.

Dynamic routing by the frozen `team-harness` coordinator is allowed and recorded.
Human selection of a better model or prompt for a particular task is not.

### B5. Give scored trials no human help

Operators may start, monitor, and collect a campaign, but they may not answer a task
question, edit a checkout, choose a route, or repair a patch during a scored trial.
`unresolvable_error` remains loopy-loop's last-resort stop mechanism (D5); when it is
caused by task-solving inability, it is not an infrastructure exclusion. The committed
patch is still sent to the verifier when the Pier protocol permits, and the verifier
remains authoritative.

Organizer communication, campaign setup, and budget approval happen before a trial
and are not human gates inside the autonomous loop.

### B6. Preserve and report all consumed compute

Every inference call and retry contributes to the system's resource usage, regardless
of whether its output reaches the final patch. Unknown usage is labeled unknown, not
zero. A quality score with incomplete accounting may be reported, but it may not be
used to claim cost or token efficiency.

### B7. Preserve raw evidence

The campaign retains the complete Pier trial, loopy session, `team-harness` outputs,
final patch, verifier output, and usage reconciliation. Summary tables are projections
over those artifacts, not the only surviving evidence.

## Experiment matrix

### Required configurations

| ID | Harness and model configuration | Purpose |
|---|---|---|
| `M0` | `mini-swe-agent` + Model A | Fixed-scaffold model baseline |
| `H1` | one direct `team-harness` run; Model A in every inference role | Same-executor control without loopy |
| `L1` | loopy-loop + `team-harness`; Model A in every inference role | Loopy treatment with a homogeneous model |
| `L3` | loopy-loop + `team-harness`; frozen Models A/B/C and roles | Heterogeneous complete-system arm |

`M0` may initially use an official published DeepSWE row only if the benchmark
version, exact model snapshot, provider route, effort setting, and scoring protocol
match. A contemporaneous rerun is preferred because it controls for model and
infrastructure drift.

`H1` uses the same `team-harness` version, coordinator model, worker interface,
provider route, tools, and task environment as `L1`, but calls
`TeamHarness.run(...)` once with a frozen benchmark prompt and no loopy coordinator,
workflow cadence, durable project-state protocol, or internal eval loop. It is the
primary control for the claim that loopy itself adds value. `M0` remains important
leaderboard context, but `M0` versus `L1` measures the whole scaffold change rather
than loopy alone.

`H1` does not make compute identical: `L1` may invoke `team-harness` repeatedly. It
controls the executor, model family, and tool interface while treating the extra calls
and internal evaluation as part of the loopy treatment. Resource-adjusted results
remain mandatory; add a strictly cost-matched `H1`/`R1` control only after the total
system cap described below can be enforced honestly.

An optional `R1` arm may repeatedly invoke bare `team-harness` with git and a minimal
progress file under the same limits. `R1` would separate loopy's structured workflow
from the simpler benefit of restarting the executor. It is not required for the first
campaign, but any claim specifically about durable workflow structure is stronger with
it.

### What "Model A everywhere" means

`H1` and `L1` are homogeneous only if Model A handles every inference role that exists
in the arm and can affect the patch or stopping time:

- the `team-harness` coordinator;
- implementation workers;
- outer/planning work;
- eval-reviewer and eval-runner work;
- any `eval-banana` judge;
- summaries, critiques, or fallback calls.

Using a fixed cross-family judge can be a useful later ablation, but then the arm must
be labeled "homogeneous implementation + fixed judge," not single-model. For this
campaign, the independent DeepSWE verifier reduces the need for a cross-family
internal judge, so using Model A for the internal judge is acceptable and removes a
causal confound. The external verifier remains the true authority.

### What "Models A/B/C" means

The heterogeneous manifest must enumerate the complete set. For example:

| Role | Agent interface | Exact model | Effort | Provider |
|---|---|---|---|---|
| Team coordinator and outer workflows | `team-harness` provider | Model A | fixed | fixed |
| Primary implementer | Codex CLI | Model A | fixed | fixed |
| Independent reviewer / alternate implementer | Claude Code | Model B | fixed | fixed |
| Researcher / tester / alternate implementer | Gemini CLI | Model C | fixed | fixed |
| Internal evaluation | explicitly assigned | A, B, or C | fixed | fixed |

This table is illustrative. The actual model snapshots and roles are chosen before
the protocol freeze. If the internal evaluator or a fallback uses a fourth model, the
system is a four-model configuration and must be named accordingly.

### Budget views

Report two views instead of relying on one headline:

1. **Unadjusted system quality:** pass rate under the predeclared limits for each
   configuration.
2. **Efficiency frontier:** quality against total dollars, wall time, output tokens,
   and LLM calls.

A strictly cost-matched arm is desirable only when a reliable per-trial cost cap can
be enforced. loopy-loop 0.5.0 has the ledger and `max_cost_usd` capability originally
tracked in [`improvement-proposals.md` P1.1](./improvement-proposals.md). Its implemented
boundary is explicit in [`config.py`](../../src/loopy_loop/config.py): it records
`team-harness` **coordinator-model** tokens and estimated cost, while
Codex/Claude/Gemini subprocess usage remains unknown. The stop is enforced in
[`coordinator_app.py`](../../src/loopy_loop/coordinator_app.py). The built-in budget
therefore cannot enforce a complete multi-model system cap. Until the adapter and
`team-harness` can measure and stop on total spend reliably, we must not advertise an
approximate cost match as exact.

## System boundary and execution architecture

One Pier trial owns one isolated repository checkout and one selected agent
configuration. `L1` and `L3` each own one loopy-loop instance:

```text
DeepSWE instruction
        |
        v
Pier trial sandbox
  `-- custom loopy Pier agent
        |-- writes excluded benchmark config + goal
        |-- starts one loopy coordinator on loopback
        `-- starts one loopy worker
              `-- team-harness coordinator
                    |-- Model A / Codex worker
                    |-- Model B / Claude worker       (L3 only)
                    `-- Model C / Gemini worker       (L3 only)

target git checkout + .loopy_loop session evidence
        |
        v
committed base..HEAD solution patch
        |
        v
DeepSWE pristine verifier container -> reward.json + reports
```

`H1` uses the same custom-agent boundary, installation, accounting, patch hygiene, and
verification path, but its adapter mode calls `team-harness` directly and does not
start loopy-loop. Keeping both modes in one integration package avoids accidental
environment differences between the executor control and the loopy arm.

Pier or Modal may run independent task sandboxes concurrently. That is benchmark-level
parallelism over isolated repositories, not multiple loopy workers sharing a checkout,
so D2 remains intact.

At DeepSWE source snapshot `6db64a4`, every v1.1 task manifest gives the agent 5,400
seconds in a two-CPU, 8-GiB, 20-GiB, no-GPU environment with unrestricted internet
disabled. The separate verifier has a 1,800-second timeout. The campaign job generator
must read and validate the limits from the pinned task definitions and pass the adapter
its soft-deadline parameters; neither layer may hardcode these assessed values.

Individual DeepSWE tasks are one bounded issue-to-patch goal. The campaign therefore
uses a dedicated, frozen variant of `inner_outer_eval`, not
`pm_planner_dispatcher`. D6 requires the planner/dispatcher double loop for a large,
multi-phase target project; it does not require a project-management parent around
each independent benchmark trial. A future registered arm may test the double loop,
but the campaign must not switch architectures task by task.

## Pier adapter design

Pier supports custom agents through its import-path mechanism. Implement a thin
adapter against Pier's
[`BaseAgent`](https://github.com/datacurve-ai/pier/blob/fefa7475a32bb05271abdea378e8083c83eb5c35/src/pier/agents/base.py)
interface and load it through the supported custom-agent configuration. Keep the
adapter benchmark-specific so Pier concerns do not enter loopy-loop's coordinator
state machine.

The adapter declares its install commands through `install_spec()`, combines every
required provider domain in `network_allowlist()`, implements `setup()` and `run()`,
sets `SUPPORTS_ATIF = True`, writes `trajectory.json` under Pier's agent log
directory, and populates `AgentContext` totals. ATIF v1.7's
[`subagent_trajectories`](https://github.com/datacurve-ai/pier/blob/fefa7475a32bb05271abdea378e8083c83eb5c35/src/pier/models/trajectories/trajectory.py)
can retain an independently valid trajectory and model identity for every embedded
`team-harness` worker.

### Adapter responsibilities

For each trial the adapter must:

1. Receive the DeepSWE instruction from Pier. Pier does not pass its resolved task
   timeout into `BaseAgent.run()`: the campaign job generator must read the pinned task
   manifests and pass a matching soft deadline and cleanup reserve through frozen
   custom-agent kwargs.
2. Record the task id, repository base commit, environment image, and wall-clock
   deadline before starting any agent.
3. In `L1`/`L3`, materialize the frozen loopy config, goal, and workflow set in the
   checkout; in `H1`, materialize only the frozen direct-executor prompt/config. Exclude
   every adapter-owned file from git.
4. Inject only the credentials and network routes declared by the selected arm.
5. In `L1`/`L3`, start the loopy coordinator on an ephemeral loopback port; in `H1`,
   prepare the same `team-harness` executor without loopy.
6. In `L1`/`L3`, start exactly one loopy worker; in `H1`, invoke
   `TeamHarness.run(...)` exactly once. Wait until the selected mode is terminal or the
   adapter's controlled deadline expires.
7. Capture coordinator and worker stdout/stderr separately.
8. On normal termination or controlled timeout, stop the coordinator and worker,
   invoke the supported team-harness drain/reap behavior where necessary (D7), and
   leave no model process running.
9. Remove any adapter-owned paths from the intended solution diff.
10. Commit the remaining solution changes so DeepSWE v1.1's `pre_artifacts.sh` can
    extract `base..HEAD`.
11. Emit the top-level trajectory, nested/subagent trajectories, usage fields, terminal
    loopy status when applicable, patch metadata, and error classification expected by
    Pier.
12. Copy or package the complete `team-harness` output and, for `L1`/`L3`, the complete
    loopy session before the sandbox is destroyed.

The loopy adapter modes should use public loopy CLI behavior—coordinator plus
worker—rather than calling private coordinator methods. This exercises the product
path and keeps the benchmark integration from becoming a second execution
implementation. `H1` is deliberately the exception because its purpose is to call the
same executor without loopy.

### Installation and network policy

The adapter declares and pins everything it installs:

- loopy-loop commit or immutable wheel;
- `team-harness` commit/version;
- `eval-banana` version if used;
- Pier commit/version;
- Python and agent CLI versions;
- benchmark workflow bundle hash.

The installation specification must install every CLI used by the selected arm, not
only loopy-loop itself. `H1` and `L1` install the same one worker interface; `L3`
installs the frozen Codex/Claude/Gemini (or selected A/B/C) interfaces and records
their exact versions.

DeepSWE tasks may disallow general internet access. Pier supports agent-specific
network allowlists, so every custom arm must declare its exact API and authentication
endpoints and deny unrelated outbound access; `L3` declares the union needed by all
three providers. Installation access and inference access are separate needs and must
both be covered.

Secrets are passed through Pier's environment/secret mechanism. They must never be
written into the target checkout, loopy session, ATIF trajectory, process command
line, or published artifact. Before publication, artifacts are scanned for credential
patterns; non-secret task and agent evidence is not rewritten merely to make a run
look cleaner.

### Checkout and patch hygiene

DeepSWE v1.1 grades committed changes only. The adapter should put these patterns in
`.git/info/exclude`, which does not alter the submitted patch:

```gitignore
/.loopy_loop/
/loopy_loop_config.yaml
/loopy_loop_goal.txt
```

If the dedicated workflow needs another adapter-owned path, add it to this fixed list.
Do not modify the target repository's `.gitignore` for benchmark machinery.

The workflow prompt requires implementation iterations to commit coherent work. The
adapter also reserves a short cleanup window before the hard Pier deadline to make a
final checkpoint commit of non-benchmark changes. On cleanup it must:

- verify the original base commit;
- remove adapter-owned files from the index and final tree even if an agent force-added
  them;
- preserve legitimate new tests and source files;
- record `git status`, the final commit id, patch hash, and diffstat;
- fail validation if benchmark metadata remains in `base..HEAD`.

The adapter must not inspect or selectively edit the solution for quality. Its scrub
list is path-based and frozen before the campaign.

### Timeout and process cleanup

Pier wraps `BaseAgent.run()` in an outer `asyncio.wait_for`, but does not pass the
resolved timeout to the custom agent. The job generator therefore validates the pinned
task manifests and passes two immutable adapter kwargs: the expected Pier timeout and
a cleanup reserve. The adapter's soft deadline is their difference.

When the soft deadline expires, the adapter terminates processes, writes artifacts,
commits the current patch, populates the available trajectory/context, and then raises
`asyncio.TimeoutError`. Pier converts that into `AgentTimeoutError`, preserving
DeepSWE's scored-timeout semantics. Returning normally after cleanup would incorrectly
turn a timeout into a normal agent completion and is forbidden. If Pier cancels the
adapter before the soft deadline, cleanup also runs in `finally` and the cancellation
is re-raised.

The adapter owns the loopy coordinator and worker processes. `team-harness` owns the
agent CLI process groups it spawns. On cancellation the adapter must use that ownership
split and the existing drain/reap APIs; it must not attempt to adopt orphaned model
processes. The policy and timeout are recorded per trial.

## Benchmark workflow set

Create a benchmark-owned workflow set derived once from the packaged
`inner_outer_eval` ownership model and store it with the adapter. Do not copy the stock
prompts unchanged: the stock inner/outer prompts assume a real delivery repository and
normally ask agents to create branches, open and merge PRs, use authenticated external
tools, perform broad research, and maintain a project backlog. Those are sensible
product defaults but add irrelevant work and unavailable network dependencies inside a
90-minute isolated benchmark trial.

The benchmark set keeps the useful outer-plan / inner-implement / eval-evidence split,
but explicitly removes remote delivery and task-specific research machinery. It works
on the current benchmark branch, commits locally, and treats one DeepSWE instruction as
one bounded goal. This is a separate workflow set; it does not loosen or rewrite the
stock `inner_outer_eval` template.

The same benchmark workflow bytes are used in every task in both loopy arms (`L1` and
`L3`); only the model-role config differs. `M0` and `H1` do not load the loopy workflow
bundle.

The workflow set should:

- treat the DeepSWE instruction as the complete goal;
- inspect and run only tests available in the agent checkout;
- prohibit attempts to locate hidden tests, the reference solution, grader files, or
  benchmark infrastructure outside the checkout;
- prohibit branch publication, PR creation/merge, browser use, and non-inference
  network access;
- require normal repository hygiene and coherent commits;
- use its LLM judge for internal progress/termination evidence;
- never claim that internal `goal_met` equals a DeepSWE pass;
- stop with a specific `unresolvable_error` only after autonomous options are
  exhausted;
- avoid task-specific branches, repository-specific prompts, or language-specific
  routing added after inspecting benchmark outcomes.

Do not add the DeepSWE hidden verifier as an `eval-banana` check. It is intentionally
unavailable until grading. Running visible repository-owned tests is allowed under D4;
having an agent invent new deterministic acceptance criteria for the stock template is
not.

The exact `max_turns`, cadence, and timeout remain open protocol parameters. Choose
them from organizer limits and a mechanical smoke run, record the rationale, and freeze
them before the first scored full-corpus pass.

## Usage and cost accounting

### Required accounting boundary

The total for one trial is the union of:

- `team-harness` coordinator calls;
- all worker CLI/model calls;
- internal evaluator/judge calls;
- retries and fallback calls;
- summarization/compaction calls;
- calls that returned errors after consuming tokens;
- provider-side cache reads/writes and their billed cost where available.

Provider billing/API records are the authority for dollars charged. ATIF and
`team-harness` artifacts provide the per-role decomposition. Reconcile the two and
record any difference.

### Required fields

For each call, where the provider exposes them, capture:

- provider and exact returned model id;
- role, agent, and parent loopy iteration;
- start/end timestamps and status;
- input, cached-input, cache-write, reasoning, and output tokens;
- billed cost and currency;
- retry/fallback relationship;
- context peak and summarization count;
- trajectory pointer.

For each trial, aggregate:

- total and per-provider cost;
- total token categories;
- total LLM calls and `team-harness` worker runs;
- loopy workflow iterations;
- wall-clock and active inference time;
- fields known to be unavailable.

Do not sum a provider invoice total and its constituent ATIF calls as though they were
different usage. The reconciliation identifies one billable total and one explanatory
breakdown.

### Readiness rule

Before a full run, the ten-task smoke must show complete accounting for every selected
provider or an organizer-approved representation of unknown fields. Subscription-based
CLI usage with no reliable per-trial allocation is unsuitable for an efficiency claim;
prefer API credentials or a metering gateway that can attribute calls to trial ids.

## Reproducibility manifest and artifacts

### Campaign manifest

Create a machine-readable manifest before the protocol freeze containing at least:

```yaml
campaign_id: <immutable id>
deep_swe_commit: <sha>
deep_swe_version: v1.1
pier_commit: <sha>
loopy_loop_commit: <sha>
team_harness_commit: <sha>
eval_banana_version: <version-or-null>
adapter_commit: <sha>
workflow_bundle_sha256: <hash-or-null>
direct_prompt_sha256: <hash-or-null>
arm: M0 | H1 | L1 | L3
models:
  - role: <role>
    provider: <provider>
    requested_model: <id>
    reasoning_effort: <setting>
limits:
  max_turns: <integer>
  pier_agent_timeout_seconds: <integer>
  adapter_cleanup_reserve_seconds: <integer>
  cost_cap_usd: <number-or-null>
repetitions: <integer>
retry_policy: <named frozen policy>
network_policy_sha256: <hash>
```

Also record container image digests, agent CLI versions, provider response model ids,
environment type, region, and the commands used to launch the job.

### Per-trial artifact set

Retain:

```text
<campaign>/<arm>/<run>/<task>/
├── pier/                    # job/trial metadata and top-level trajectory
├── loopy-session/           # complete .loopy_loop session (L1/L3 only)
├── team-harness/            # coordinator and worker artifacts
├── process-logs/            # adapter/coordinator/worker stdout + stderr
├── git/
│   ├── base.txt
│   ├── head.txt
│   ├── status.txt
│   ├── solution.patch
│   └── patch.sha256
├── verifier/                # reward.json, CTRF, stdout, logs, reports
├── usage/
│   ├── calls.jsonl
│   ├── reconciliation.json
│   └── totals.json
└── trial-summary.json       # status, failure class, pointers, hashes
```

Large raw artifacts do not need to live in the git repository. Publish them in an
organizer-approved immutable store and commit the manifest, checksums, schemas, and
analysis code. Preserve the DeepSWE benchmark canary and comply with its data-use and
publication requirements.

The tree above is a superset. `M0` has no `team-harness` or loopy session, and `H1`
has no loopy session. Omit inapplicable directories and record them as `not_applicable`
in `trial-summary.json`; never represent them as missing evidence.

## Failure and retry policy

Classify failures before looking at whether a patch would have passed.

### Outcomes included in scoring

- DeepSWE verifier rejection is a failed attempt.
- The current DeepSWE leaderboard policy scores agent timeouts and context-window
  failures as failures, even if partial work exists.
- A normal loopy terminal state such as `unresolvable_error`, `max_turns`,
  `workflow_failure_cap`, or `max_cost_usd` is not an infrastructure exclusion. When
  Pier proceeds to verification, the committed patch receives the verifier's reward.
- Empty, malformed, or missing committed patches attributable to the submitted agent
  system remain in the denominator.
- An exhausted predeclared model retry budget remains in the denominator.
- Worker, coordinator, workflow, or benchmark-adapter behavior caused by the submitted
  agent system remains in the denominator.

An internal evaluator false closure is therefore not a separate scoring rule. It is a
diagnostic label on the common case `loopy goal_met` + `verifier failure`.

### Candidate infrastructure exclusions

- provider-wide outage or rate-limit incident outside the configured retry policy;
- Pier/Modal control-plane failure;
- corrupted benchmark image or checkout;
- verifier infrastructure failure unrelated to the submitted patch;
- confirmed credential injection failure before the first model call.

The organizer's rules decide which candidate exclusions are actually removed from the
denominator. Store the raw attempt either way.

Do not selectively rerun surprising failures. Rerun only errors matching the frozen
infrastructure policy. If an adapter or workflow code change is required, increment
the campaign version and rerun every affected configuration/task set; do not combine
pre-fix and post-fix trials into one row.

## Execution phases

### Phase 0 — Agree on the submission contract

Before material benchmark spend, send Datacurve a one-page version of this protocol
and obtain answers on:

- acceptance of a multi-model custom agent;
- display category and label;
- self-run versus Datacurve-reproduced artifacts;
- required repetitions and confidence-interval method;
- timeout, resource, network, and spend ceilings;
- ATIF, subagent, usage, and cost requirements;
- infrastructure-error and rerun policy.

If there is no official path, the same protocol can produce an independently published
DeepSWE agent-system study. It must not imply official leaderboard acceptance.

### Phase 1 — Build and validate the adapter

Implement the Pier adapter, benchmark workflow bundle, manifest schema, artifact
collector, cost reconciler, and patch-hygiene validator.

Validate locally on one task, then in the intended remote environment. This phase may
change code freely because no scored campaign exists yet.

### Phase 2 — Deterministic ten-task smoke

Use DeepSWE's documented deterministic sample:

```text
--n-tasks 10 --sample-seed 0
```

Run one attempt for `H1`, `L1`, and `L3` (and `M0` if rerunning the baseline). The
smoke is an integration test, not a benchmark result. Its go/no-go criteria are
mechanical:

- every task starts from the expected base commit;
- the selected executor path starts and terminates cleanly, including exactly one
  loopy worker in `L1`/`L3`;
- no orphaned model processes remain;
- a forced soft-deadline smoke is surfaced by Pier as `AgentTimeoutError`, not normal
  completion;
- adapter files never enter the solution patch;
- all non-empty solution changes are committed and extracted;
- hidden verifier material is inaccessible during the agent phase;
- Pier and nested trajectories validate;
- usage reconciles for every provider;
- verifier artifacts are collected without influencing the agent;
- secrets scan clean;
- projected full-run spend and capacity are acceptable.

Prompt or adapter changes discovered here are allowed only to correct generic
integration defects. Record every change and rerun the entire smoke after the last
change. Do not tune based on which solution behaviors passed or failed.

### Phase 3 — Freeze and register the campaign

Tag or commit the adapter and workflow bundle. Generate immutable manifests for all
arms. Archive the smoke artifacts separately. Record the planned analysis, repetitions,
and stopping rules before launching the full corpus.

### Phase 4 — First complete 113-task pass

Run one complete pass of `H1`, `L1`, and `L3`. If `M0` is being rerun, execute it in
the same environment window. Do not modify a configuration between tasks.

This pass may be published as **preliminary pass@1** if clearly labeled. Proceeding to
additional repetitions is based on the preapproved budget and organizer protocol, not
on whether the first score is favorable.

### Phase 5 — Repeated full-corpus passes

The current v1.1
[`leaderboard-live.json`](https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json)
reports four whole-benchmark runs for leading configurations and calculates confidence
intervals from run-to-run variation. Unless Datacurve specifies otherwise, matching
that presentation means four complete passes per submitted configuration: 452 scored
task attempts per arm before infrastructure exclusions.

The three custom-agent arms (`H1`, `L1`, and `L3`) therefore require 1,356 full-corpus
attempts. A fresh `M0` baseline raises the total to 1,808, before the smoke run and
valid infrastructure reruns. Approve inference and sandbox budgets against those
counts, not against one 113-task pass.

Do not silently lower the repetition count for the more expensive arm. If resources
permit only one pass, report one pass and avoid a confidence claim that implies four.

### Phase 6 — Analyze, publish, and submit

Generate all summary results from the retained artifacts. Have a separate reviewer
verify manifest hashes, exclusions, totals, and claims. Publish failures as well as
successes, then send the complete package to Datacurve under the agreed system label.

## Analysis plan

### Primary metrics

- pass@1 over all scored attempts;
- 95% uncertainty calculated using the organizer's method;
- paired pass-rate difference between `M0` and `H1` where exact pairing is valid;
- paired pass-rate difference between `H1` and `L1`;
- paired pass-rate difference between `L1` and `L3`;
- pass@4 if four attempts per task are completed, using DeepSWE's current definition:
  tasks with at least one passing rollout divided by tasks attempted.

For our own paired intervals, resample whole tasks with all their repetitions together;
do not treat four attempts of one task as four independent task specifications. Publish
the exact analysis code.

### Efficiency metrics

- mean and median total dollars per scored attempt;
- dollars per passing attempt and expected dollars per solve;
- input, cached, reasoning, and output tokens;
- wall time;
- total LLM calls and agent steps;
- pass rate plotted against dollars and wall time.

### loopy-specific diagnostic metrics

- workflow iterations per task;
- `team-harness` child-agent runs and role distribution;
- retry and fallback counts;
- terminal `control.json` state and stop reason;
- external false-closure rate: loopy `goal_met`, verifier fail;
- external false-nonclosure rate: no loopy `goal_met`, verifier pass;
- fraction of tasks completed in one iteration versus requiring repeated fresh
  contexts;
- orphan cleanup, timeout, and artifact-integrity failures.

These diagnostics explain behavior; they do not replace DeepSWE reward.

### Interpretation rules

- `H1` versus `M0` describes the `team-harness`/native-interface scaffold change; it
  does not measure loopy.
- `L1 > H1` supports an advantage from adding loopy's repeated durable workflow, only
  with disclosed resource differences and uncertainty.
- `L1 = H1` may still show that loopy adds durability or auditability, but DeepSWE has
  not demonstrated a quality advantage.
- `L1 < H1` is a real negative result and should trigger trajectory analysis rather
  than a relabeled claim.
- `L3 > L1` supports the value of the heterogeneous complete system, not the superiority
  of any constituent model.
- A raw win obtained with substantially more cost must be described as a quality/cost
  trade, not an unqualified improvement.

## Required implementation surface

Keep the benchmark integration outside the production dependency path. A likely
repository layout is:

```text
benchmarks/deepswe/
├── README.md
├── protocol.yaml
├── adapter/
│   └── deepswe_pier_agent.py
├── configs/
│   ├── direct-team-harness.yaml
│   ├── homogeneous.yaml
│   └── heterogeneous.yaml
├── workflow_sets/
│   └── deepswe_inner_outer_eval/
├── schemas/
│   ├── campaign.schema.json
│   └── trial-summary.schema.json
└── tools/
    ├── validate_patch.py
    ├── collect_artifacts.py
    ├── reconcile_usage.py
    └── analyze_results.py
```

This design does not create those files yet.

No core scheduling change is expected. The necessary product-facing capabilities
already exist:

- heterogeneous worker names and per-agent model/effort overrides in
  `src/loopy_loop/config.py`;
- propagation into `TeamHarness(...)` in
  `src/loopy_loop/harness_runner.py::_build_harness_kwargs()`;
- session and iteration evidence under `.loopy_loop/sessions/`;
- a single coordinator/worker execution path;
- crash/process ownership described by D7.

Likely new work is limited to the Pier adapter, benchmark workflow/configuration,
artifact packaging, patch hygiene, and complete usage/cost instrumentation. If usage
instrumentation requires `team-harness` changes, make them in that repository and pin
the resulting commit. Do not paper over missing worker usage in loopy-loop summaries.

## Readiness checklist

### External agreement

- [ ] Datacurve confirms whether and how it will accept the custom agent system.
- [ ] Required repetitions, limits, artifact schema, and rerun policy are written down.
- [ ] The public system label and multi-model metadata representation are agreed.

### Adapter and workflow

- [ ] Custom Pier agent installs from immutable sources.
- [ ] One loopy coordinator and one worker run successfully per sandbox.
- [ ] Benchmark workflow is generic, frozen, hashed, and hidden-verifier-safe.
- [ ] Controlled timeout leaves a committed, metadata-free patch and no orphaned
      processes.
- [ ] The job generator validates task timeouts and the controlled-timeout test is
      classified by Pier as `AgentTimeoutError`.
- [ ] Pier/ATIF trajectories represent every subagent and model role.

### Accounting and artifacts

- [ ] Provider usage can be attributed to each task/attempt.
- [ ] Total cost includes coordinator, workers, judges, retries, and failed calls.
- [ ] Unknown fields are explicit and organizer-approved.
- [ ] Complete loopy, `team-harness`, git, Pier, and verifier artifacts are retained.
- [ ] Manifest/schema validation and credential scanning pass.

### Experiment

- [ ] Model A and the A/B/C role mapping are frozen with exact snapshots.
- [ ] `M0`, `H1`, `L1`, and `L3` limits and retry rules are frozen.
- [ ] Ten-task smoke passes all mechanical criteria after the last code change.
- [ ] Full-run budget and sandbox/provider capacity are approved.
- [ ] Analysis code, exclusions, confidence method, and publication template are frozen.

## Open questions

1. Which exact Model A has a current DeepSWE baseline and can be used consistently by
   the `team-harness` coordinator, native worker interface, and internal evaluator?
2. Which exact A/B/C combination and roles reflect the intended loopy-loop product
   rather than a benchmark-specific ensemble?
3. Can every selected provider expose per-trial billable tokens and cost through the
   chosen CLI/API route?
4. What task timeout and loopy `max_turns` fit Datacurve's official protocol?
5. Does Pier want one top-level `model` value such as `multi-model`, an omitted model,
   or another ensemble representation?
6. Will Datacurve accept nested ATIF subagent trajectories produced by the adapter, or
   must it reproduce the system on its own infrastructure?
7. Is one published official `M0` row sufficiently matched, or must we budget a fresh
   baseline run?
8. Where will large raw session and trajectory artifacts be retained and for how long?

## Decision boundary

This accepted design permits implementation only after the external protocol and
accounting requirements are concrete. It deliberately requires no change to D1–D7 and
no major loopy-loop architecture change. If Pier integration reveals that a core
change is actually necessary, amend this design and review the relevant architecture
decision before implementing it.

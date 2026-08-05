# DeepSWE Benchmark Assessment for loopy-loop

- **Status:** Working analysis; not an architecture decision
- **Date:** 2026-07-13
- **External sources last verified:** 2026-07-13
- **Source snapshots inspected:** DeepSWE
  [`6db64a4`](https://github.com/datacurve-ai/deep-swe/tree/6db64a40f3318d8659238ff34a8cc4b491c49205)
  and Pier
  [`fefa747`](https://github.com/datacurve-ai/pier/tree/fefa7475a32bb05271abdea378e8083c83eb5c35)
- **Scope:** Whether DeepSWE can credibly evaluate and publish a loopy-loop run,
  including a run in which `team-harness` uses several model families.
- **Companion benchmark design:**
  [`../designs/deepswe-benchmark-execution.md`](../designs/deepswe-benchmark-execution.md)

This document records the evidence and reasoning behind the proposed DeepSWE
benchmark campaign. It is deliberately under `design/analysis/`: it describes the
benchmark as it exists on the date above and may become stale when DeepSWE changes.
It does not commit loopy-loop to an implementation or amend any decision in
[`../decisions.md`](../decisions.md).

## Executive conclusion

DeepSWE can technically run and score loopy-loop, and DeepSWE explicitly invites
people to submit a **model or agent**. A loopy-loop run using three models is therefore
a legitimate candidate submission.

It is not, however, the same kind of result as the rows on the current public
leaderboard. Those rows hold `mini-swe-agent` fixed so that the table compares models
rather than agent scaffolds. A loopy-loop + `team-harness` run changes the scaffold,
the orchestration policy, the number of model calls, and potentially the set of model
families. It measures the quality of a complete **agent system**.

The honest expectation is therefore:

- DeepSWE can score the system.
- Datacurve explicitly accepts agent submissions for consideration.
- Datacurve may require a separate agent/harness category or publish the result as a
  case study rather than place it among the existing model rows.
- There is no public policy promising how a multi-model submission will be displayed.
  We should obtain that answer before paying for the full benchmark.

This is still a strong opportunity. DeepSWE itself names multi-harness evaluation as
a natural next step. The most persuasive campaign would add a direct `team-harness`
control, a homogeneous loopy-loop result, and a heterogeneous three-model result. That
separates the value of the executor, the durable loopy outer workflow, and model-family
specialization.

## Evidence from DeepSWE

### The current leaderboard is intentionally a model comparison

The [DeepSWE leaderboard](https://deepswe.datacurve.ai/) labels its entries
"Models" and says that every model runs on `mini-swe-agent` for consistency. The
[methodology](https://deepswe.datacurve.ai/blog/deepswe#evaluation-harness) is more
explicit: the harness is held fixed so the leaderboard reflects model capability,
not the scaffolding around it. Every model receives the same `bash` tool and shared
prompt.

That design answers this question:

> Under one standardized, model-agnostic scaffold, which model solves the most
> DeepSWE tasks, and at what token, cost, step, and time profile?

It does not answer this question:

> What is the strongest coding system a developer can construct by combining a
> durable outer loop, a team coordinator, native coding agents, and several models?

As of 2026-07-13, the public
[`leaderboard-live.json`](https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json)
contains a singular `model` and `harness` for each configuration, and every published
row uses `harness: "mini-swe-agent"`. That is evidence about the current table, not a
rule forbidding other agent systems.

### DeepSWE explicitly invites agent submissions

The official [Run DeepSWE](https://deepswe.datacurve.ai/run) page says both
"score a new agent" and "submit your model or agent to the leaderboard." Submission
is handled by contacting `serena@datacurve.ai`.

There is no public submission checklist covering multi-model systems, required
repetitions, maximum spend, or whether externally generated artifacts are accepted.
Consequently, the invitation establishes a real submission path but does not
guarantee placement in the existing model table.

### DeepSWE recognizes the missing harness dimension

The methodology's
[limitations and future work](https://deepswe.datacurve.ai/blog/deepswe#future-work)
states that holding `mini-swe-agent` fixed isolates model effects at the cost of
realism. It calls running the same models under multiple harnesses a natural next
step, so that model and scaffold effects can be separated.

This is almost exactly the analytical opening for loopy-loop. A homogeneous-model
loopy run would contribute to that decomposition. A heterogeneous-model run would go
one step beyond it and evaluate a complete ensemble system.

## Why DeepSWE is a relevant benchmark

DeepSWE v1.1 currently contains 113 tasks across 91 repositories and five languages.
Its public description emphasizes short, behavior-focused prompts paired with larger
changes: the published reference solutions average 668 added lines across seven
files. The benchmark uses purpose-written behavioral verifiers instead of requiring
the agent to reproduce one particular implementation shape.

The [DeepSWE repository](https://github.com/datacurve-ai/deep-swe) describes a useful
trust boundary:

1. The agent works in an isolated task environment.
2. The agent commits its intended solution.
3. Pier extracts the committed patch.
4. The patch is applied in a pristine verifier environment.
5. Held-out tests produce structured reward and test artifacts.

At the source snapshot above, all 113 task manifests give the agent 5,400 seconds,
two CPUs, 8 GiB of memory, 20 GiB of storage, no GPU, and no unrestricted internet.
The separate verifier receives 1,800 seconds. These are current benchmark facts, not
limits that this analysis proposes; the actual campaign must read and pin the task
manifests it runs.

This fits loopy-loop's decisions well:

- D1 already treats files and git as durable truth.
- D2's single-worker rule remains intact inside one task sandbox; Pier may parallelize
  independent task sandboxes without allowing multiple loopy workers to edit one
  checkout.
- D3's `IterationResult.success` is not used as the benchmark score. The independent
  DeepSWE verifier is the semantic authority.
- D4 permits benchmark-owned deterministic verification. The forbidden failure mode
  is an agent inventing its own acceptance checks, not an independent benchmark
  running held-out tests.
- D5 remains intact because a scored trial receives no human help while it is running.

DeepSWE is therefore credible evidence of software-engineering quality. It is not a
complete test of every loopy-loop claim. In particular, a task that completes in one
or a few harness invocations does not strongly exercise multi-day continuity, process
recovery, or parent/child project management. Those durability claims need a separate
fault-injection or evolving-project evaluation.

## What a three-model result would and would not establish

Suppose one DeepSWE trial uses a `team-harness` coordinator and Codex, Claude, and
Gemini workers. The resulting score would establish:

- the end-to-end agent system can solve a measured fraction of externally verified
  tasks;
- loopy-loop can drive a heterogeneous team autonomously inside isolated repositories;
- its repository state, workflow sequencing, evaluation, and stopping behavior can
  produce gradeable patches;
- the system's achieved quality can be compared with its total cost, time, and model
  usage.

The result alone would **not** establish:

- that loopy-loop's outer loop caused an improvement;
- that any one constituent model deserves the score;
- that the system is more efficient than a single-agent baseline;
- that using three model families is better than spending the same budget on repeated
  calls to one strong model;
- that loopy-loop is better at crash recovery or multi-day project continuity.

The causal problem is straightforward: model mix, scaffold, tool interface, call
count, retries, context, wall time, and dollar spend all change at once. A high score
would be a valid product result but a weak architecture experiment.

## The experiment needed to isolate loopy-loop's contribution

The current DeepSWE model baseline is useful context, but it does not by itself isolate
loopy-loop. Moving from `mini-swe-agent` to loopy-loop also introduces
`team-harness`, native worker interfaces, different prompts, and potentially more calls.
The minimum causal comparison therefore has four configurations:

| Configuration | Purpose | Valid claim |
|---|---|---|
| `mini-swe-agent` + Model A | Standardized model baseline | Model A's capability under DeepSWE's fixed scaffold |
| one direct `team-harness` run + Model A everywhere | Same-executor control | Capability of the executor and native worker interface without loopy's repeated durable workflow |
| loopy-loop + `team-harness` + Model A everywhere | Homogeneous loopy arm | Effect of adding loopy's repeated durable workflow around the same executor and model family |
| loopy-loop + `team-harness` + Models A/B/C | Heterogeneous loopy arm | Performance ceiling of the disclosed complete system |

The comparisons then have distinct meanings:

- direct `team-harness` versus `mini-swe-agent` measures the executor/native-scaffold
  change;
- homogeneous loopy versus direct `team-harness` is the primary evidence for
  loopy-loop's added value;
- heterogeneous loopy versus homogeneous loopy measures the additional value of the
  model mix.

An optional fifth arm can run a bare restart loop that repeatedly invokes
`team-harness` with git and a minimal progress file but without loopy's workflow and
evaluation protocol. That distinguishes structured durable orchestration from the
simpler benefit of "try the agent again." It is useful, but the direct executor
control is the minimum requirement.

Even the direct control does not make compute identical: repeated loopy iterations
may consume more calls than one `team-harness` run. The comparison identifies the
effect of adding the complete loopy treatment, including that extra inference. A
quality-versus-cost analysis is therefore still mandatory, and a truly cost-matched
control should be added once total-system budget enforcement is reliable.

The homogeneous arm must include **every inference role**, not merely the final coding
worker. If the team coordinator, eval reviewer, eval runner, or an `eval-banana` judge
uses another model, the arm is not literally single-model. Either use Model A for all
of them or label the fixed additional judge explicitly.

The published Model A row is a useful comparator when its exact model snapshot,
reasoning effort, DeepSWE version, and provider route match. A contemporaneous baseline
rerun is stronger because it reduces version and infrastructure drift.

### Raw quality is not enough

The current leaderboard reports pass rate together with average cost, output tokens,
and agent steps. loopy-loop must account for **all** inference and orchestration work:

- every `team-harness` coordinator turn;
- every Codex, Claude, Gemini, or other child-agent call;
- eval-reviewer, eval-runner, and judge calls;
- retries, summaries, and failed calls that consumed billable tokens;
- all benchmark attempts, including scored timeouts and agent failures.

If usage from one provider or CLI is unavailable, it must be marked unknown rather
than silently treated as zero. A result with incomplete accounting may still be a
quality result, but it cannot support an efficiency claim.

The campaign should publish at least:

- raw pass@1;
- pass@4 if the organizer confirms the current four-run protocol;
- mean and median total cost per scored attempt;
- input and output tokens per provider and in aggregate;
- wall time, LLM calls, loopy iterations, and spawned child-agent runs;
- quality-versus-cost and quality-versus-time comparisons;
- the difference between loopy-loop's terminal claim and the external verifier result.

The last metric is especially important. A `goal_met` stop followed by a verifier
failure is externally observed false closure. A normally stopped loop without
`goal_met` whose committed patch passes is false non-closure. Both tell us something
useful about the inner evaluation policy without redefining D3.

## Acceptance assessment

| Question | Assessment | Evidence |
|---|---|---|
| Can DeepSWE execute and grade a custom loopy agent? | High confidence | DeepSWE uses Pier and invites users to score new agents |
| Can we submit it for official consideration? | Explicitly yes | The Run page requests model or agent submissions by email |
| Will a three-model system fit the existing model table? | Unlikely without a policy/category change | The current table deliberately fixes `mini-swe-agent` and has no multi-model precedent |
| Could it appear as a separate agent/harness result? | Plausible, not promised | DeepSWE identifies multi-harness comparison as future work |
| Is a three-model score alone evidence that loopy-loop is better? | No | Model mix and compute are confounded with orchestration |

The submission should be named as a system, for example:

> `loopy-loop <commit> + team-harness <commit> — heterogeneous A/B/C`

It must not be labeled as the score of Model A, B, or C. Exact model snapshots,
providers, roles, effort settings, and any fallback routing belong in the visible
configuration metadata.

## Questions to settle with Datacurve before the full run

Contact the DeepSWE maintainers with a short protocol, not merely a request to "add
loopy-loop." Ask:

1. Will they accept a custom multi-model agent system as an official submission?
2. Would it appear in the existing table, a separate agent/harness track, or an
   accompanying case study?
3. Must Datacurve reproduce the run, or will it accept complete self-run Pier
   artifacts?
4. How many whole-benchmark repetitions are required? The live v1.1 artifact currently
   reports four runs for leading rows, but the public submission page does not state
   this as a requirement.
5. What per-task time, resource, network, and spend ceilings apply to custom agents?
6. Which Pier, ATIF, token, cost, and trajectory fields are mandatory for a
   multi-model system?
7. How should the top-level `model` field identify an ensemble?
8. Which failures may be excluded as infrastructure errors, and which must be scored
   as agent failures?

The execution design treats organizer agreement as a campaign precondition. It is
not a human gate inside an autonomous loopy-loop trial and does not conflict with D5.

## Recommendation

Proceed with DeepSWE, but position it precisely:

1. Propose a separately labeled agent/harness submission before funding the full run.
2. Build one Pier integration that supports direct `team-harness`, homogeneous loopy,
   and heterogeneous loopy modes.
3. Run a deterministic ten-task smoke subset only to validate integration.
4. Freeze the protocol before examining or optimizing full-benchmark outcomes.
5. Run the direct `team-harness` control and homogeneous loopy configuration to
   establish the outer workflow's contribution.
6. Run the heterogeneous configuration to establish the system ceiling.
7. Publish complete artifacts and cost accounting whether the result is flattering or
   not.

The strongest defensible headline is not "three models beat one model." It is:

> Holding the executor and implementation model constant, adding loopy-loop's durable
> repeated workflow changed externally verified task success by X at a disclosed cost;
> allowing three specialized model families changed it by a further Y.

That result would show both whether the architecture contributes and what the full
system can achieve.

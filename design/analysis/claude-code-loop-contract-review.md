# Architecture and Contract Audit: loopy-loop Session, Loop, and Trace Contracts

**Status:** Analysis / working notes (`design/analysis/`, per `AGENTS.md` Rule 3 —
*not* binding, may be superseded). Read-only audit; no runtime code, binding design,
doc, config, or test was changed to produce it.
**Date:** 2026-07-15
**Audited revision:** `a5f9933` (branch `main`, clean tree)
**Installed dependencies at audit time:** `team-harness 0.4.0`, `eval-banana 0.3.1`
(the published minimums are `team-harness>=0.4.0`, `eval-banana>=0.3.1`)
**Scope:** the one-loop session, the planner/dispatcher double loop, and a possible
future depth-first triple loop; the split between durable semantic state and
execution trace; role/goal/path knowledge given to coordinators and spawned agents;
eval creation and execution; state recovery; request/result contracts; harness output
placement; gitignore behavior; trace completeness.

> **Independent pass; not the adjudicated recommendation.** A later source-level
> check corrected one placement claim in this report: when loopy supplies
> `output_dir`, team-harness 0.4.0 puts spawned-worker stdout/stderr and
> `worker_sessions.json` in that session-local output tree, not beside the global
> `run.json`. The full coordinator `run.json` is still global, so the headline
> trace-split and usage-accounting finding remains valid. See the
> [consolidated review](./loop-layer-state-and-trace-contract.md) for the verified
> layout and final recommendations.

**How to read this document.** Sections 1–7 describe **current behavior** and are
evidence-backed — every claim names a file and line, and the ones marked *Reproduced*
were verified by executing the code, not by reading it. Sections 8–11 are
**recommendations and proposals** and are labelled as such. Nothing here amends
`design/decisions.md`; where I think a decision's *implementation* diverges from the
decision, I say so explicitly rather than proposing to change the decision. Where I
suggest something a decision forbids, I say that too, and do not recommend it.

**Relationship to the other working notes in this directory.**
`design/analysis/antigravity-loop-contract-review.md` (untracked at audit time) is an
independent pass over the same brief. It overlaps on several conclusions. It is also
**factually wrong on one point that matters**: its sequence diagram states
"TH->>FS: Writes initial run.json to harness_outputs/" (line 46) and its Priority 2
recommendation asserts "the runs are fully self-contained in the session workspace"
(line 284). Neither is true of team-harness 0.4.0 — see Finding 1, which that pass
missed and which this one treats as the headline result. I flag this because
`design/analysis/` is explicitly allowed to be superseded, and a future reader should
not average the two.

---

## 1. Executive verdict

**The architecture is sound and the decision log is unusually honest. The contract
gaps that matter are not in the state machine — they are at the three seams where
loopy-loop hands off to something it does not own: team-harness's run record, the
eval-banana judge, and the natural-language prompt.**

The coordinator/worker core is in good shape. The single-worker ping-pong (D2), the
durable session stack, attempt fencing, staged child dispatch with crash-window
reconciliation, and the `_advance()` single-transition discipline are coherent,
well-commented, and well-tested. `state.json` is genuinely the source of truth and
`events.jsonl` is genuinely a projection. I found no defect in that core.

The problems are at the edges, and they share one root cause: **loopy-loop's contract
with its own dependencies and with its agents is asserted in prose but not verified
anywhere.** Three concrete consequences, all verified by execution:

1. **The usage ledger reads a file that is never written** (Finding 1, Critical). The
   worker looks for `run.json` in the session's harness output directory;
   team-harness 0.4.0 writes it only to `~/.team-harness/runs/<run_id>/run.json`.
   Usage is therefore *always* unknown, the token ledger is permanently zero, and
   `max_cost_usd` — a documented, shipped stop condition — **can never fire**. The
   repo already knows the correct path: `recovery.py:155` reads it from `RUNS_DIR`
   while `worker.py:329` reads it from the session. Two modules, two paths, one wrong.
   The unit test authors its own fixture at the wrong path, so the seam is never
   exercised. This also means **D9's audit trail — `requested_model`/`effective_model`
   per spawned agent — lives outside the session tree entirely**, so the reviewer or
   eval check D9 relies on cannot see it in the evidence it is given.

2. **The stock eval path cannot pass on a clean target repo** (Findings 2 and 3,
   High). The `eval_reviewer` prompt teaches a `target_paths` field that eval-banana
   0.3.1 rejects with `extra_forbidden`, and the eval workflows require an
   `.eval-banana/config.toml` that `loopy init` never creates and no document
   mentions. Both fail closed (no false `goal_met`), which is the right failure
   direction and consistent with D8 — but the effect is that the recommended template's
   only evidence channel is dead on arrival until someone manually fixes two things
   nothing tells them about.

3. **Execution scratch has no contained home, and git does not stop it** (Finding 4,
   High). The stock prompts send agents to `_feature_planning/` and
   `_additional_context/` (explicitly: "download relevant repos, SDKs, examples, or
   docs"), the harness runs with `cwd = repo_root`, and `loopy init` appends exactly
   one line to a target repo's `.gitignore`. The same prompts instruct the inner loop
   to branch, PR, and merge. Trace and scratch can therefore be delivered as product.

Underneath those three sits the systemic issue this audit was really asked about:
**the two worlds are a convention, not a contract.** Durable semantic state and
execution trace are separated *by where the prompts happen to tell agents to write*,
not by anything the engine renders, validates, or detects. The rendered prompt block
gives absolute paths; the prompt bodies speak in bare relative names
(`project_state/memory.md`, `children.json`); the harness cwd is the repo root. Every
one of those relative names resolves, taken literally, to a path that belongs to no
session — and in the double loop, parent and child have *identically named*
`project_state/current_state.md`, `memory.md`, and `finished.md` at different depths.
The joining of "Session project_state directory" to "project_state/memory.md" is an
inference the agent is trusted to make correctly, every iteration, at every depth.

On the specific questions asked:

| Question | Verdict |
|---|---|
| Does every coordinator/agent know its **role**? | Partially — from prose only. Not rendered; not machine-checkable. |
| Its **loop depth**? | **No.** Nothing renders depth. Parenthood is inferred from directory shape (`worker.py:413`), not from the durable `parent_session_id`. |
| Its **owning session**? | Yes — `Session ID` and absolute session paths are rendered. |
| Its **exact goal**? | Yes — `config_snapshot.goal` plus `Session goal path`; the "never the repo-root goal file" rule is stated in every stock prompt. This part is done well. |
| Its **exact assignment**? | Partially — free-text `goal` only for child sessions; no structured acceptance contract. |
| **Absolute paths to only the correct locations**? | **No.** Paths are absolute and correct, but the same block is rendered to every role regardless of need (a child gets `child_requests/`, which is structurally inert there), and `eval_results/`, `children.json`, and `children/` are never rendered at all despite prompts referring to them. |
| Can parents **safely consume child evidence** without conflating ownership? | Mechanically yes (nesting + `children.json` make paths unambiguous). Contractually **no** — the parent is told to read `children.json` by a bare name that resolves to the wrong place under the actual cwd, and no path to it is rendered. |

**Bottom line.** Nothing here requires amending D1–D9. Finding 1 is a plain bug.
Findings 2, 3, 4, and 8 are places where the codebase's *own* stated principles
(D1 files-are-truth, D4 judge-based eval, D8 detection-not-prevention) are asserted in
prose without the detection that D8 itself says every constraint should have. The
highest-leverage change is not new architecture — it is turning three or four of these
prose contracts into checks that fail loudly.

---

## 2. Current contract map

### 2.1 The processes and who owns what

```
loopy coordinator (FastAPI, one process)
  owns: LoopState in <session>/state.json  — the ONLY durable dispatch truth
  owns: session scaffolding, child dispatch, stop precedence, history, ledger
  reads (never writes): control.json, goal_check.json, child_requests/*.json
  exposes: POST /register, POST /finished          (docs/http-contract.md)

loopy worker (one process, D2)
  owns: its own OS process; renders the prompt; runs ONE assignment
  writes: <iteration>/{prompt.txt,result.json,result_text.txt,harness_run_id.txt,
                       pending_finished_request.json}
  calls: TeamHarness.run(task=<rendered prompt>)   with cwd = repo_root

team-harness (SDK, in-process in the worker; D7 owns the agent processes)
  writes: ~/.team-harness/runs/<run_id>/run.json   <-- GLOBAL, outside the repo
  creates: <session>/harness_outputs/<NNNN>_<wf>/<run_id>/   (session_output_dir)
  spawns: agent CLIs (codex / claude / gemini) in their own process groups

agent CLIs (spawned; bill to their own accounts; not measurable by loopy)
  write: the target repo working tree, session project_state/, eval_checks/,
         and — per the stock prompts — _feature_planning/ and _additional_context/
```

### 2.2 The one-loop session (`inner_outer_eval`)

Four workflows in one workflow set, one session, no children.

| Workflow | Role (prompt) | Cadence (`config.yaml`) | Writes |
|---|---|---|---|
| `eval_reviewer` | author harness_judge checks | `priority: 100`, `run_on_start: true`, `run_after_successes: {inner, 10}` | `eval_checks/*.yaml`, `project_state/eval_results.md` |
| `eval_runner` | run checks, emit evidence | `priority: 90`, `must_follow: eval_reviewer`, `not_before_iteration: 10`, `emits_goal_check: true` | `goal_check.json`, `eval_results/`, `control.json` (on pass) |
| `outer` | plan, review, own `finished.md` | `priority: 10` | `project_state/*`, `control.json` |
| `inner` | implement one leaf task | `priority: 20`, `must_follow: outer`, `not_before_iteration: 1` | the repo, branches, PRs, `project_state/current_state.md` |

**Traced schedule** (from `scheduler.py`, by hand): iteration 1 `eval_reviewer`
(the only `run_on_start`), then `outer → inner → outer → inner …`. `eval_reviewer`
becomes eligible again only after 10 successful `inner` runs — iteration 22 — and
`eval_runner` first runs at **iteration 23**. See Finding 5 for why that interacts
badly with `outer` also being allowed to declare `goal_met`.

### 2.3 The planner/dispatcher double loop (`pm_planner_dispatcher`, D6)

```
parent session (pm_planner_dispatcher)
  planner   -> PM state, selects ONE item, reviews terminal child evidence
  dispatcher-> writes ONE <name>.json into child_requests/  {workflow_set, goal, schema_version}
        |
        v  coordinator: _dispatch_child_session_after_success -> _dispatch_child_session_if_requested
child session (inner_outer_eval), physically at
  .loopy_loop/sessions/<parent>/children/<child>/
  - inherits the PARENT's FROZEN config_snapshot; only goal/goal_hash/workflow_set change
                                                   (coordinator_app.py:1100-1106)
  - parent.active_child_session_id = <child>       -> the durable stack pointer
  - parent is suspended: _suspended_parent_response() forbids it acquiring a task
  - on child terminal: children.json record finalized (status, stop_reason, subtree usage),
    pointer cleared, parent resumed
```

The child→parent contract is: `children.json` (audit index) + `state.json`'s
`active_child_session_id` (the live pointer) + physical nesting. The parent→child
contract is a **single free-text `goal` string**. There is no structured acceptance
criteria field, no work-item id, no back-pointer to the requesting iteration.

### 2.4 The request/result contract

Well-specified and correctly implemented. `attempt_id` fences stale completions
(`_finish_assignment_locked`, `_read_recoverable_finished_request`); `WorkerIdentity`
fences a second worker (`_raise_if_worker_alive` → 409) and stale `/finished` from a
non-owner. `_advance()` is the single transition. Recovery-relevant writes are atomic
(`write_json_atomic` / `write_text_atomic`). I have no findings against this layer.

One accurate subtlety worth restating because it is easy to misread as a bug:
`IterationResult.success` is `True` whenever the harness returns
(`harness_runner.py:254`), regardless of agent exit codes. **This is D3 and is
correct.** Do not "fix" it.

---

## 3. State-versus-trace ownership model

The brief asks for two deliberately separate worlds. Here is what actually exists
today, classified. The **Separation** column is the honest answer to "what keeps this
in its world?"

### World A — durable semantic state (what the work *means*)

| Artifact | Owner | Location | Separation mechanism |
|---|---|---|---|
| `state.json` | coordinator | `<session>/` | engine-written; gitignored |
| `control.json` | workflow | `<session>/` | validated on read; gitignored |
| `goal_check.json` | eval workflow | `<iteration>/` | validated on read; gitignored |
| `goal.md` | engine | `<session>/` | engine-written; gitignored |
| `project_state/**` | workflow | `<session>/` | **prompt convention only** |
| `eval_checks/**` | eval_reviewer | `<session>/` | **prompt convention only** |
| `children.json` | coordinator | `<session>/` | engine-written; gitignored |
| `child_requests/*.json` | dispatcher | `<session>/` | validated; rejected→`.rejected` |
| **git branches / PRs / merges** | inner loop | **the target repo** | **prompt convention only** |
| `updates_from_user.md` | human | `<session>/` | engine-scaffolded |

### World B — execution trace (what the machine *did*)

| Artifact | Producer | Location | In session tree? | Gitignored? |
|---|---|---|---|---|
| `prompt.txt`, `result.json`, `result_text.txt` | worker | `<iteration>/` | yes | yes |
| `harness_run_id.txt`, `pending_finished_request.json` | worker | `<iteration>/` | yes | yes |
| `salvage.json` | recovery | `<iteration>/` | yes | yes |
| `events.jsonl` | coordinator | `<session>/` | yes | yes |
| harness/agent artifacts | harness coordinator + agents | `<session>/harness_outputs/<NNNN>_<wf>/<run_id>/` | yes | yes |
| **`run.json`** (turns, usage, agent identity, **requested/effective model**) | team-harness | **`~/.team-harness/runs/<run_id>/`** | **NO** | n/a (outside repo) |
| agent CLI stdout/stderr | agent CLIs | team-harness's run dir | **NO** | n/a |
| `_feature_planning/`, `_additional_context/` | agents, per stock prompts | **repo root (cwd)** | **NO** | **NO** |
| `.loopy_loop/state.json.lock` | `StateStore` | repo root `.loopy_loop/` | no | **NO** (in a repo with a pre-existing `.gitignore`) |

**The model that is actually implemented:** *everything under
`.loopy_loop/sessions/` is both worlds, undifferentiated, and gitignored; everything
outside it is neither world and is not ignored.* The separation between A and B is
therefore **directory-name convention inside one gitignored subtree**, plus prose in
prompts. There is no schema, no ownership assertion, and no detection.

**The three leaks, precisely:**

1. **Trace escapes the session downward** (into git): `_feature_planning/`,
   `_additional_context/`, `.loopy_loop/state.json.lock` → Finding 4, Finding 6.
2. **Trace escapes the session sideways** (out of reach): `run.json` and agent logs
   live in `~/.team-harness/runs/` → Finding 1, Finding 10. A cloud export of the
   session directory — the stated future goal — would ship a trace **missing the token
   ledger, the agent process identities, and the D9 model audit trail**.
3. **State has no protection from trace**: nothing prevents an agent resolving
   `project_state/memory.md` against cwd (`repo_root`) and writing durable semantic
   state into the target repo's working tree, where it is not ignored and where the
   inner loop's own PR instructions will collect it.

---

## 4. Invariants

These are the invariants the current system actually maintains (I1–I8) and the ones it
only *asserts* (A1–A5). The distinction is the point of this section: everything in
the second list is a place where D8's own principle — express a constraint as
something detectable — has not been applied.

**Maintained and enforced by code:**

- **I1.** Exactly one `CurrentTask` is live per session; a verifiably-alive worker's
  task is never reclaimed. (`_raise_if_worker_alive`, 409.)
- **I2.** A parent with a live child never acquires its own task.
  (`_suspended_parent_response`, `coordinator_app.py:375-405`.)
- **I3.** At most one child edge is live per parent; child dispatch is depth-first and
  sequential. (`active_child_session_id`; D2/D6.)
- **I4.** A completion is applied only to the attempt that produced it.
  (`attempt_id` matching in `_finish_assignment_locked` and
  `_read_recoverable_finished_request`.)
- **I5.** A child request file produces at most one child.
  (`_dispatched_request_files` tombstone via `children.json`.)
- **I6.** `state.json` commits before its `events.jsonl` projection; no scheduling
  decision reads events. (`_flush_pending_events` after `mutate`.)
- **I7.** A drained/abandoned iteration never gets a synthesized `result.json`.
  (D3/D7; `recovery.py` docstring; enforced by absence.)
- **I8.** Torn or schema-invalid workflow output is never accepted as evidence.
  (`_read_signal` → `None` → `invalid_control_output` / `goal_check_broken` /
  `*.json.rejected`.)

**Asserted in prose, not detected — each is a candidate for a D8-style check:**

- **A1.** *"Workflows write durable state only under their own session directory."*
  Nothing detects a write to `<repo_root>/project_state/` or to a sibling session.
- **A2.** *"The planner never writes `child_requests/`; the dispatcher never writes
  `finished.md`."* (`planner/prompt.txt:92`, `dispatcher/prompt.txt:74`.) Prompt-only;
  no check.
- **A3.** *"Execution scratch stays out of the delivered diff."* Prompt-only, and the
  prompts themselves are ambiguous about where scratch goes (Finding 4).
- **A4.** *"The judge does not share failure modes with the implementer"* (D4). Not
  expressible today: the judge's model lives in `.eval-banana/config.toml`, outside
  loopy's config and outside the config snapshot (Finding 3).
- **A5.** *"Coordinators pick sensible model tiers"* (D9 — deliberately guidance, not a
  fence; correct). But D9 says the remedy is "an outer reviewer or an eval check
  verifies `requested_model`/`effective_model` on the child's agent records". Those
  records are in `~/.team-harness/runs/`, not in the session tree — so **the remedy
  D9 names is not currently reachable from the evidence** (Finding 1/10).

---

## 5. Findings — current behavior

Severity reflects impact on the autonomy goal (D5): does it silently produce a wrong
outcome, block progress, or merely add friction?

---

### Finding 1 — CRITICAL — The usage ledger reads a `run.json` that is never written; `max_cost_usd` is inert; the D9 audit trail is outside the session tree

**Current behavior.** `worker.py:_read_harness_usage` reads the harness run record from
the iteration's session-local harness output directory:

```python
# src/loopy_loop/worker.py:329
path = Path(harness_output_dir) / "run.json"
```

where `harness_output_dir` is `<session>/harness_outputs/<NNNN>_<wf>/<run_id>`
(`harness_runner.py:253`, `_normalize_harness_result`).

team-harness 0.4.0 writes `run.json` to exactly one place, and it is not that one:

```python
# team_harness/harness.py:125
run_dir = RUNS_DIR / run_id
# team_harness/tracking/run_log.py:33
self.path = run_dir / "run.json"
# team_harness/config.py:20
RUNS_DIR = Path.home() / ".team-harness" / "runs"
```

`session_output_dir` is created (`_prepare_session_output_dir`, `harness.py:442-449`),
advertised to the harness coordinator LLM in its system prompt as a place to write
artifacts (`team_harness/coordinator/system_prompt.py:157-163`), and recorded as a
*field inside* the run record (`harness.py:375`) — but **nothing writes `run.json`
into it**. An exhaustive grep of every `session_output_dir` use in team-harness 0.4.0
confirms this.

**The repo already contains the correct path.** `recovery.py` reads the same record
from the right place:

```python
# src/loopy_loop/recovery.py:155
run_json = Path(th_config.RUNS_DIR) / run_id / "run.json"
```

So `recovery.py` and `worker.py` disagree about where `run.json` lives, and
`worker.py` is wrong.

**Evidence (reproduced, not inferred).**
- `~/.team-harness/runs/` on this machine holds **1333** `run.json` files. Every one
  sampled carries `turns[].usage` with real `prompt_tokens`/`completion_tokens`
  (e.g. `{'prompt_tokens': 10381, 'completion_tokens': 135}`, 12/12 turns measured).
  The data exists.
- A filesystem search for `*/harness_outputs/*/run.json` across the home tree returns
  **zero** results.
- A real past loopy run,
  `…/WorkflowPlatform/.loopy_loop/sessions/20260512_074434_c5b5b4304fcc_63f4fd6c/harness_outputs/0018_outer/20260512_232739_a659b033/`,
  contains only `generated_scratch` — no `run.json`. Count of `run.json` under that
  session's `harness_outputs/`: **0**.

**Failure scenario.** Any iteration, any provider. `_read_harness_usage` finds no file
→ returns `None` → `IterationResult.usage = None` → `_record_finished_task` takes the
`else` branch (`coordinator_app.py:668-669`) and increments
`iterations_without_usage` → `usage_totals.prompt_tokens` and `completion_tokens`
remain `0` forever. Then:

- `estimate_cost_usd(prompt_tokens=0, completion_tokens=0, prices=…)` returns `0.0`.
- `_apply_stop_precedence` (`coordinator_app.py:1313-1324`) evaluates
  `cost >= budget` as `0.0 >= budget`. `max_cost_usd` is validated `gt=0`
  (`config.py:222-230`), so this is **always False**.
- **`stop_reason="max_cost_usd"` can never be reached.** A documented, shipped,
  README-advertised budget stop is inert. An unattended long-horizon run (the entire
  point of the system, D5) has no cost ceiling.
- `loopy status` always prints `subtree_usage: prompt_tokens=0 completion_tokens=0`
  and, with prices configured, `subtree_estimated_cost_usd: 0.0000`.

**Why it survived.** The unit test authors the fixture at the wrong path and fakes the
producer:

```python
# src/tests/test_events_and_usage.py:384-399
run_dir = tmp_path / "harness" / "run-9"
run_dir.joinpath("run.json").write_text(json.dumps({"turns": [{"usage": {...}}]}))
def fake_run_harness_iteration(**kwargs): 
    return IterationResult(..., harness_output_dir=str(run_dir))
```

The test proves `_read_harness_usage` *parses* a `run.json` correctly — which it does.
Nothing tests that the file is ever **there**. No session with a `usage_totals` ledger
exists anywhere on this machine, which suggests the 0.5.0 feature has never been
exercised end to end.

**Contract consequences beyond cost.** `run.json` is also where D9's audit trail lives
— team-harness records requested/effective model and effort per spawned agent there.
D9's stated remedy for a coordinator choosing a bad tier is that "an outer reviewer or
an eval check verifies `requested_model`/`effective_model` on the child's agent
records". **Those records are not in the session tree**, so a reviewer or eval given
the session directory cannot perform that check. Likewise a cloud export of the
session ships a trace with no token accounting and no agent identities (Finding 10).

**Decision alignment.** Not a D-violation. D3's "unknown usage stays unknown" is
honored — the ledger correctly reports *unknown*, not a fabricated zero, so the
degradation is honest and D1's files-are-truth premise is intact. This is a plain
path bug in a feature the design doc describes as working
(`designs/long-running-loop-reliability.md`, "Operational events, usage, and cost").

---

### Finding 2 — HIGH — `eval_reviewer` teaches a `target_paths` field that eval-banana ≥0.3.1 rejects

**Current behavior.** The stock eval-authoring prompt states:

```
# templates/inner_outer_eval/.../eval_reviewer/prompt.txt:61
- target_paths are resolved from the repo root.
```

eval-banana 0.3.1's `HarnessJudgeCheckDefinition` (`eval_banana/models.py:80-84`)
accepts only `schema_version`, `id`, `type`, `description`, `tags` (from
`BaseCheckDefinition`), `instructions`, and `model`. `BaseCheckDefinition` sets
`model_config = ConfigDict(extra="forbid")` (`models.py:34-35`). The string
`target_paths` does not appear anywhere in the installed package.

**Failure scenario (reproduced).** An `eval_reviewer` that follows its prompt writes:

```yaml
schema_version: 1
id: repo-has-impl
type: harness_judge
description: The repo contains a runnable implementation for the goal.
instructions: |
  Decide whether ...
target_paths: ["README.md"]
```

`eval-banana validate --cwd . --check-dir <session eval_checks>` → **exit 1**:

```
Invalid check definition in …/repo_has_impl.yaml: 1 validation error …
harness_judge.target_paths
  Extra inputs are not permitted [type=extra_forbidden, input_value=['README.md']]
```

The `eval_runner` prompt then correctly writes `goal_met: false` ("If eval-banana is
unavailable, checks are missing, validation fails, or any scenario fails, set goal_met
to false"). The loop runs to `max_turns` (160) with the eval layer never able to
accept. Fail-closed — no false `goal_met` — but the evidence channel is dead.

**Corroboration that the field was removed, not invented.** This repo's own
`.eval-banana/config.toml` still documents `llm_max_input_chars` as "Maximum
characters of each **target file** sent to harness_judge checks" — a concept 0.3.1's
`harness_judge` (which has only `instructions`) no longer has. The prompt and the
repo's own eval config are stale against the same schema change.

**Decision alignment.** Not a D-violation; a dependency-drift bug. It is exactly the
maintenance risk `proposals/improvement-proposals.md` P2.1 names ("no documented upper
compatibility policy"), now realized in a packaged prompt rather than in a pin.

---

### Finding 3 — HIGH — The eval path requires an `.eval-banana/config.toml` that `loopy init` never creates and no document mentions

**Current behavior.** D4 mandates that stock eval workflows create **only**
`harness_judge` checks. eval-banana refuses to run a `harness_judge` check when no
harness agent is configured:

```python
# eval_banana/runner.py:80-108  (require_harness_for_harness_judge)
if config.harness_agent is not None:
    return
...
msg = ("harness_judge check requires a harness but none is configured "
       f"(first offender: {harness_judge_paths[0]}). "
       "Fix: set [harness] agent in .eval-banana/config.toml or pass "
       "--harness-agent on the command line.")
raise SystemExit(msg)
```

`Config.harness_agent` defaults to `None` (`eval_banana/config.py:112`). The only
sources are `.eval-banana/config.toml`, `EVAL_BANANA_HARNESS_AGENT`, or
`--harness-agent`.

`loopy init` ships neither the file nor the flag: `PACKAGED_TEMPLATE_FILES_BY_NAME`
(`cli.py:34-57`) lists no `.eval-banana/` entry. The `eval_runner` prompt's commands
pass `--cwd`, `--check-dir`, and `--output-dir` but **not** `--harness-agent`
(`eval_runner/prompt.txt:43-44`). Neither `README.md`, `skills/loopy-loop/SKILL.md`,
`docs/session-layout.md`, nor `docs/http-contract.md` mentions `eval-banana init`,
`[harness] agent`, or that the judge needs its own configuration.

**Failure scenario (reproduced).** In a clean target repo initialized with
`loopy init --template pm_planner_dispatcher`, with a **schema-correct** harness_judge
check (so Finding 2 is not in play):

```
$ eval-banana validate --cwd . --check-dir .loopy_loop/sessions/S1/eval_checks
harness_judge check requires a harness but none is configured (first offender: …).
Fix: set [harness] agent in .eval-banana/config.toml or pass --harness-agent …
validate exit: 1
$ eval-banana run … ; echo $?
1
```

Every eval run aborts. `goal_met` is `false` forever via the eval path.

**Two further contract consequences.**

1. **The judge's model is outside every loopy contract.** `.eval-banana/config.toml`
   sets `[harness] agent` and `model` independently of `team_harness_model`, of
   `model_tiers` (D9's single source of truth for model ids), and of
   `RootConfigSnapshot`. Because child sessions inherit the parent's *frozen snapshot*
   (`coordinator_app.py:1100-1106`), the judge's identity is **not** part of what a
   child inherits — it is ambient, read from disk at eval time. D4's "prefer judging
   with a different model family than the implementer" is therefore neither
   configurable through loopy nor auditable from session evidence (invariant A4).

2. **Ambient inheritance from an ancestor directory.**
   `eval_banana/config.py:find_local_config` walks *upward* from cwd looking for
   `.eval-banana/config.toml`. A target repo nested under a directory that has one
   silently inherits that judge agent/model. Nothing in the session records which
   config was in effect. (Not hit in the clean-room test — the ancestor scan of
   `/tmp/loopy_audit/target_repo` found none — but it is reachable in the real layout
   this repo lives in.)

**Decision alignment.** D4 is correct and should not change. This is D4 being *made
mandatory by policy* while the setup step it implies is neither shipped nor documented.

---

### Finding 4 — HIGH — Execution scratch has three contradictory homes, none of them gitignored, and the inner loop is told to PR the result

**Current behavior — four facts that combine badly.**

1. **The harness runs at the repo root.** `harness_runner.py:156` sets
   `"cwd": str(repo_root)`. Every bare relative path in a prompt resolves there, not
   in the session directory.

2. **The stock prompts name three different locations.** `outer/prompt.txt:277`:
   *"create a dedicated directory in `_feature_planning/` withing the session
   directory"* — but under `cwd = repo_root`, `_feature_planning/` **is** the repo
   root, not the session directory; the sentence contradicts the runtime.
   `inner/prompt.txt:137` says `_feature_planning/` "withing the session directory",
   while `inner/prompt.txt:151-152` say **`/_feature_planning`** — a leading slash,
   i.e. the *filesystem root*. Three phrasings, three destinations, no rendered path
   for any of them.

3. **The prompts direct bulk third-party content there.** `inner/prompt.txt:147-148`
   and `outer/prompt.txt:284-285`: *"do not be afraid to download relevant repos,
   SDKs, examples, or docs into `_additional_context/` and inspect them locally."*

4. **`loopy init` ignores exactly one path.**

   ```python
   # src/loopy_loop/cli.py:73
   GITIGNORE_LINES = [".loopy_loop/sessions/"]
   ```

   `_ensure_gitignore` (`cli.py:499-511`) appends only that. The packaged template
   `.gitignore` is copied via `_write_if_missing` (`cli.py:215-221`), so in **any repo
   that already has a `.gitignore`** — i.e. every real target — it is never applied.

**Failure scenario (reproduced).** A target repo with a pre-existing `.gitignore`:

```
$ printf '__pycache__/\n*.pyc\n' > .gitignore && git init -q .
$ loopy init --template pm_planner_dispatcher
$ cat .gitignore
__pycache__/
*.pyc
.loopy_loop/sessions/          <-- the only line added
```

`_feature_planning/`, `_additional_context/`, and `_outputs/` are **not ignored**. The
same `inner` prompt that created them instructs (lines 205-211): *"for implementation
work that changes repo files, making a new branch, opening a PR, waiting for checks,
and merging it"*. An agent staging its work with `git add -A` delivers downloaded
third-party repositories and planning scratch into the target repo's merged PR — trace
becoming product, in the one place the system claims as durable truth (D1: "files and
git").

**The tell.** loopy-loop's *own* `.gitignore` ignores exactly these:

```
_claude_prompts/
_feature_planning/
_outputs/
_additional_context/
.agents/
.claude/
```

The maintainer's local hygiene is real and correct — it just never made it into the
template that ships to target repos.

**Decision alignment.** Not a D-violation. D8 forbids *preventive* fences (no path
sandbox) — and a `.gitignore` entry is not a fence, it is the ordinary hygiene that
makes the diff legible enough for detection to work at all.

---

### Finding 5 — MEDIUM-HIGH — Two `goal_met` authorities with different evidence bars, and no ordering between them

**Current behavior.** In `inner_outer_eval`, two workflows are authorized to stop the
session with `goal_met`:

- `eval_runner/prompt.txt:64-65`: *"If **and only if** goal_check.json has goal_met
  true, update the rendered Session control path to stop the loop with stop_reason
  goal_met"*, where `goal_met: true` requires validation success, run success, and all
  scenarios passing (lines 74-77). Disciplined, evidence-bound.
- `outer/prompt.txt:96-108`: *"If the full session goal is satisfied after your review
  of accepted completion evidence, update the rendered Session control path"* with
  `stop_reason: goal_met`. **No eval evidence required.**

The engine accepts either: `_apply_session_control` (`coordinator_app.py:1355-1374`)
reads `control.json` from the session directory and honors `goal_met` regardless of
which workflow wrote it. This is correct per D3 (control.json is the sole stop switch)
and is not a bug in the engine.

**Failure scenario.** From the traced cadence (§2.2), `eval_runner`'s first possible
run is **iteration 23** (`not_before_iteration: 10` + `must_follow: eval_reviewer` +
`run_after_successes: {inner, every: 10}`). `outer` runs from iteration 2. An `outer`
at iteration 4 that judges its own accepted-completion ledger sufficient can write
`{"state":"stopped","stop_reason":"goal_met"}` and terminate the session **before the
eval layer has run even once** — no `goal_check.json`, no judge, no `eval_results/`.
The session reports `status: goal_met` with zero eval evidence in its own directory.

`designs/success-semantics-and-evaluation.md` names the weaker version of this as its
"Known limitation": *"a whole iteration's 'success' can rest on a single model
judgment with no deterministic backstop."* The stronger, currently-reachable version is
that a whole **session's** `goal_met` can rest on **zero** judgments — the planner's own
say-so about work the planner itself scoped. That is the self-gaming failure mode D4
was written to eliminate, reintroduced one layer up.

**Decision alignment.** Not a D-violation as written — D3 deliberately makes
`control.json` the agent-owned gate, and D8 forbids an engine fence ("this workflow
may not stop until X is proven" is exactly the semantic scheduling veto D8 rejects).
The D8-shaped remedy is *detection*: a check that fails acceptance when
`stop_reason: goal_met` is recorded with no passing eval report in the session. See
Recommendation R5.

---

### Finding 6 — MEDIUM — `.gitignore` templates diverge, and a lock file lands in the target repo unignored

**Current behavior.** The two packaged templates ship different `.gitignore` files:

```
templates/inner_outer_eval/.gitignore        -> 4 lines
  .loopy_loop/sessions/
  .loopy_loop/state.json
  .loopy_loop/state.json.lock
  .loopy_loop/state.json.archive_*.json
templates/pm_planner_dispatcher/.gitignore   -> 1 line
  .loopy_loop/sessions/
```

Neither reaches a repo that already has a `.gitignore` (Finding 4). `GITIGNORE_LINES`
covers only the first.

**Two of those four lines are stale, one is not.** `.loopy_loop/state.json` and
`.archive_*` are legacy: state now lives per-session, and `_write_state_unlocked`
(`state_store.py:90-99`) resolves to `session_state_path(...)`. But
`.loopy_loop/state.json.lock` **is still created at the repo root**:

```python
# src/loopy_loop/state_store.py:72-80
def _lock(self) -> FileLock:
    self.loopy_dir.mkdir(parents=True, exist_ok=True)
    lock_path = self._effective_state_path()
    if lock_path is None:
        lock_path = self.loopy_dir / LOCK_FILENAME     # <-- .loopy_loop/state.json.lock
```

`_effective_state_path()` returns `None` whenever no top-level session exists yet
(`latest_top_level_state_path` → `None`), which is exactly the first
`_prepare_state()` read on a fresh coordinator start, and any `loopy status`/`stop`
before the first session. So a fresh run leaves an unignored lock file in the target
repo, visible in `git status`, collectable by an agent running `git add -A`.

---

### Finding 7 — MEDIUM — The rendered prompt carries no role and no depth, and infers parenthood from directory shape

**Current behavior.** `worker.py:_render_prompt` (lines 367-420) renders:

```
Goal, Completion criteria, Stop criteria
Session ID, Workflow set, Iteration, Workflow ID
Session directory / goal path / project_state / eval_checks / updates_from_user /
  child_requests / control path / finished ledger path / harness outputs directory
Iteration directory, Iteration harness output root
[Parent session directory]        (conditional)
[goal_check.json output path]     (conditional)
```

What is **not** rendered:

- **Role.** Nothing says "you are a child implementation session" or "you are the PM
  parent". The role exists only in the workflow's own `prompt.txt` body.
- **Loop depth.** Nothing renders depth at any level.
- **Parent session id.** Only the parent *directory* is rendered, and only via a
  **path-shape heuristic**:

  ```python
  # src/loopy_loop/worker.py:413-414
  if session_dir.parent.name == "children":
      lines.append(f"Parent session directory: {session_dir.parent.parent}")
  ```

  The durable, authoritative fact — `LoopState.parent_session_id` (`models.py:155`) —
  is **not in `RootConfigSnapshot`** (`models.py:70-90`), so the worker cannot see it
  and reconstructs a weaker version from the filesystem layout. Every other part of the
  system treats the pointer as truth (`designs/long-running-loop-reliability.md`: "The
  pointer in `state.json`, not the presence of a request file, determines which session
  is active"); the prompt renderer is the one place that reads the directory instead.
- **`eval_results/`, `children.json`, `children/`.** Never rendered — see Finding 8's
  companion problem below.

**The same block is rendered to every role.** A child `inner_outer_eval` session
receives `Session child_requests directory:` (line 404) even though a child can never
dispatch a child (Finding 8). `inner` receives `Session control path:` even though its
prompt never mentions control and it has no authority to stop the loop.

**The systemic consequence — relative names against the wrong cwd.** The rendered block
gives **absolute** paths; the prompt bodies speak in **bare relative** names:

- `inner/prompt.txt:23-30`: "State files to read: `project_state/README.md`,
  `project_state/memory.md`, …"
- `outer/prompt.txt:229`: "Read the goal, `project_state/memory.md`, and all session
  project_state files."
- `planner/prompt.txt:52`: "Inspect `children.json` and child session directories."
- `dispatcher/prompt.txt:24`: "State files to read: … `children.json`"

Under `cwd = repo_root` (`harness_runner.py:156`), `project_state/memory.md` resolves
to `<repo_root>/project_state/memory.md` and `children.json` to
`<repo_root>/children.json` — paths belonging to **no session**, and **not gitignored**
(Finding 4). Correct behavior depends entirely on the agent inferring that it must join
the bare name to the rendered absolute directory. `children.json` has **no** rendered
path at all, so the planner and dispatcher — the two workflows explicitly told to read
it — must derive `<Session directory>/children.json` unaided.

In the double loop this ambiguity is worst: parent and child both have
`project_state/current_state.md`, `project_state/memory.md`, and
`project_state/finished.md`. The filenames are identical; only the absolute prefix
distinguishes them. A resolution mistake writes the child's status into the parent's
ledger (or into the repo root), silently.

---

### Finding 8 — MEDIUM — A child session's `child_requests/` is discarded with no rejection, no log, and no trace

**Current behavior.** Child dispatch returns before the request directory is ever
scanned when the session has a parent:

```python
# src/loopy_loop/coordinator_app.py:1029-1030
def _dispatch_child_session_if_requested(self, *, state, caller=None):
    if state.parent_session_id is not None:
        return None
```

The `child_requests/` path is nonetheless rendered into **every** child workflow's
prompt (`worker.py:404`). A grandchild request written by a child session is therefore:
never dispatched, never renamed to `*.json.rejected` (that path is inside the loop it
returns before), never logged, and never surfaced in `events.jsonl`. It sits on disk
as a valid-looking file forever.

**Failure scenario.** A workflow set designed for three levels — or a child
`inner_outer_eval` session whose `outer` decides a sub-package deserves its own loop —
writes `child_requests/item.json`, marks its state `waiting_for_child`, and finishes.
The coordinator dispatches the next ordinary workflow. The session waits for a child
that will never exist until `max_turns` or `workflow_failure_cap` ends it. The only
diagnostic is the absence of a `child_started` event.

**This is documented but not detected.** `skills/loopy-loop/SKILL.md:380-381` says it
plainly: *"A child session writing its own `child_requests/` is never dispatched — a
nested workflow design that expects grandchildren waits forever."*
`proposals/improvement-proposals.md` ("Future direction — Deeper depth-first child
chains") records the guard as deliberate. Both are honest. But D8's own principle is
that a constraint should be *"something the agent can see, contest, and repair
against"* — and this constraint is invisible at runtime. Compare the sibling path: an
invalid request at depth 1 becomes an inspectable `*.json.rejected` with a logged
reason. At depth 2 the same file is silently ignored. Contrast is the finding.

---

### Finding 9 — MEDIUM — `session_dir_path` resolves sessions by filesystem search rather than by the durable pointer

**Current behavior.**

```python
# src/loopy_loop/sessions.py:146-155
def session_dir_path(*, repo_root: Path, session_id: str) -> Path:
    root = sessions_root_path(repo_root=repo_root)
    direct = root / session_id
    if direct.exists():
        return direct
    if root.exists():
        for candidate in sorted(root.rglob(session_id)):
            if candidate.is_dir() and candidate.name == session_id:
                return candidate
    return direct
```

Every child-session path derivation — and `session_dir_path` backs *every* other path
helper in the module — misses the `direct` fast path and falls through to an
`rglob` over the entire sessions tree. Three consequences:

- **Cost.** O(size of the whole session tree) per path call, on a tree that grows with
  every iteration's `harness_outputs/` and `iterations/` content. `_render_prompt`
  alone calls it ~10 times per iteration.
- **Ambiguity.** On any duplicate name it silently takes `sorted(...)` first. Session
  ids embed a timestamp, goal hash, and 8 random hex chars, so collision is not a real
  risk — but a copied or restored session tree makes it reachable.
- **Silent wrong answer.** When a session does not exist anywhere it returns the
  *top-level* `direct` path rather than `None`. Callers cannot distinguish "not found"
  from "found at top level". Today this is benign (`_reconstruct_session_stack` treats
  the resulting missing `state.json` as a dangling pointer and clears it, which is the
  correct outcome) but it is correct by luck, not by contract.

The durable parent→child pointer chain that `_reconstruct_session_stack` already walks
is authoritative and would give an exact answer.

---

### Finding 10 — MEDIUM — Crash recovery and the trace both depend on `~/.team-harness/runs`, outside the session and outside the repo

**Current behavior.** `recovery.py:155` reads
`Path(th_config.RUNS_DIR) / run_id / "run.json"`, where
`RUNS_DIR = Path.home() / ".team-harness" / "runs"` (`team_harness/config.py:20`).
This is correct today (it is where the file is — Finding 1), but it means the
crash-durable record that D7's whole mechanism depends on is **not part of the session
and not part of the repo**.

**Failure scenario.** Coordinator and worker run under different `HOME` values —
different users, a container without the home mount, a systemd unit with a private
home. `run_json.exists()` is False → `logger.warning("no run.json for interrupted
harness run %s")` → `continue` → `outcome.reaped_runs == 0` → no `salvage.json`, no
orphans handled, history records plain `"abandoned"`. **The D7 identity-tracked
guarantee degrades to the legacy path with a log line and no durable record that it
did.** From the session directory afterwards, a bounded-drain recovery that reaped
three agents and one that reached nothing look similar: the distinguishing signal is
the *absence* of `salvage.json`, which is also what an older team-harness produces.

**Trace-completeness scenario (the stated cloud-export goal).** Exporting
`.loopy_loop/sessions/<id>/` ships: prompts, results, events, harness/agent artifacts,
eval reports. It **omits**: per-turn token usage, coordinator model/provider/api_base,
coordinator retry records, agent process identities, and **requested/effective model
and effort per spawned agent** — i.e. exactly the D9 audit trail (invariant A5). The
session directory is not a self-contained trace, and the design docs do not say so.

`design/analysis/antigravity-loop-contract-review.md:284` proposes fixing this by
pointing `recovery.py` at the session's `harness_outputs/` because "the runs are fully
self-contained in the session workspace". That premise is false (Finding 1); doing it
would break recovery rather than fix the export. The correct direction is the
opposite — get the record *into* the session — see Recommendation R1.

---

### Finding 11 — LOW-MEDIUM — The PM template's goal file describes the mechanism, not a target

**Current behavior.** `templates/pm_planner_dispatcher/loopy_loop_goal.txt` reads, in
full:

```
Manage project work by selecting one concrete implementation item at a time,
dispatching it to a child implementation loop, and reviewing the child evidence
before accepting completion.
```

That is a description of the planner/dispatcher **procedure**, not of anything to
build. Compare `templates/inner_outer_eval/loopy_loop_goal.txt`, which is a real
target (a browser-playable Bomberman-like game with stated MVP criteria).

The planner prompt is built on the goal being a target:

- line 47: "Read the session goal path and treat it as the source of truth."
- line 61-62: "If no available item exists, create or refine PM work items **from the
  session goal**."
- line 78-79: "Do not stop just because one item is accepted. **Stop only when the full
  session goal is satisfied.**"

**Failure scenario.** A clean `loopy init --template pm_planner_dispatcher` gives a
planner whose only source of truth instructs it to select items one at a time —
circular. It must invent a backlog with no target, which D6 explicitly warns against
("The planner drives the target's *own* authoritative plan; it does not invent a
parallel backlog"). And a procedure statement is never "satisfied", so the planner has
no legitimate route to `goal_met` — the session can only end at `max_turns` (120) or
`unresolvable_error`.

**Why the tests do not catch it.** `test_clean_pm_init_can_dispatch_an_inner_outer_eval_child()`
(`test_cli.py`) proves the *mechanics* — init, dispatch, first child assignment. The
reliability design's claim that "the planner/dispatcher template is executable from a
clean init" is true in the mechanical sense the test asserts. Semantic coherence of the
scaffolded goal is a different property and is untested.

This is a two-line template fix, not an architecture issue — hence LOW-MEDIUM. But it
is the first thing a new user of the double loop runs.

---

### Finding 12 — LOW — The same workflow set gets a different system-prompt contract as a child than as a root

**Current behavior.** `templates/inner_outer_eval/loopy_loop_config.yaml` ships a
substantive `team_harness_system_prompt_extension`: the `finished.md` ownership rule
("Only the outer loop should add entries"), the `updates_from_user.md` obligation, and
the full PR/branch/merge rule.

`templates/pm_planner_dispatcher/loopy_loop_config.yaml` ships
`team_harness_system_prompt_extension: ""`, with a well-reasoned comment explaining why
(a PM-only instruction would reach the child's implementer and contradict its job —
the same warning as `SKILL.md:151-152`).

Because children inherit the parent's **frozen snapshot**
(`coordinator_app.py:1100-1106`), a child `inner_outer_eval` session spawned by a PM
parent runs with extension `""`. **The identical workflow set therefore executes under
two different system-prompt contracts depending on whether it was init'd as a root or
spawned as a child.**

Impact is low: the `inner` and `outer` prompt bodies carry the PR/merge policy and the
`finished.md` ownership rule themselves, so the extension is largely redundant. But the
asymmetry is real, undocumented, and a trap for anyone who later moves a rule *out* of
the prompt bodies and into the extension, believing it applies uniformly. The config
comment explains why the PM extension is empty; nothing records that the child set
consequently loses its own.

---

### Finding 13 — LOW — Prompt-internal drift in the eval workflows' "Inputs available" lists

`eval_reviewer/prompt.txt:7-15` and `eval_runner/prompt.txt:6-16` both enumerate
"Inputs available in the rendered assignment" and **omit `Session goal path`** — while
lines 17-19 of each instruct: *"Treat the rendered Goal input and the Session goal path
as canonical."* The renderer does provide it (`worker.py:396`), so this is
documentation drift inside the prompt rather than a functional break; an agent reading
its own input list to decide what exists would conclude the canonical goal path is not
available.

Separately, `eval_results/` is never rendered by `_render_prompt`; both eval prompts
construct `<session directory>/eval_results` by hand
(`eval_runner/prompt.txt:43-44`). This works, but it makes the eval output location a
prompt convention rather than a rendered contract — the one output location that
`docs/session-layout.md:139-148` treats as part of the session file contract.

---

### Finding 14 — LOW — The `inner` prompt runs the same reviewer family twice

`inner/prompt.txt` defines three plan-review stages: line 160 "reviewing the plan …
**using CODEX**", line 166 "reviewing the plan … **WITH CODEX!**", line 172 "…
**WITH GEMINI!**". Lines 160 and 166 are byte-identical in intent — almost certainly a
copy-paste where one was meant to be CLAUDE (the `outer` prompt's equivalent pair is
CODEX at line 299 and CLAUDE at line 305). The implementation stage is CODEX
(line 178), so one of the two duplicate reviewers shares the implementer's family —
mildly against D4's "the judge should not share failure modes with the implementer",
though this is plan review rather than the eval layer proper.

---

## 6. One-loop / double-loop / triple-loop assessment

### 6.1 One loop (`inner_outer_eval`) — works; the eval half is broken by Findings 2/3/5

The state machine is sound. The contract issues are: the eval path cannot pass on a
clean repo (F2, F3), `outer` can stop with `goal_met` before any eval runs (F5), and
scratch escapes into git (F4). None of these are architecture; all are template and
dependency-seam defects.

### 6.2 Double loop (`pm_planner_dispatcher` + child `inner_outer_eval`) — architecturally solid

The parent/child machinery is the best-engineered part of the codebase. Depth-first,
one child at a time, durable pointer, staged dispatch with every crash window
reconciled at startup, subtree usage rolled up at finalization without double-storing.
`test_session_stack_recovery.py` (824 lines) covers restart-during-child,
restart-after-child-terminal, dangling pointer, adoption, and terminal finalization.

**Can a parent safely consume child evidence without conflating ownership?**

*Mechanically, yes.* Nesting (`<parent>/children/<child>/`) plus `children.json`
(session_id, workflow_set, status, stop_reason, request_file, subtree usage) makes
every child artifact addressable and unambiguous. Parent `project_state/` and child
`project_state/` are physically distinct. `_mark_child_record_complete` is idempotent
across crash-replayed finalizations and preserves the first observed completion time.
There is no path by which the engine attributes a child's history to the parent.

*Contractually, no — three gaps:*

1. **`children.json` has no rendered path** (F7). The planner and dispatcher are the
   only consumers of child evidence and both are told to read it by a bare name that,
   under `cwd = repo_root`, resolves to `<repo_root>/children.json`.
2. **Identical filenames at both depths** (F7). `project_state/current_state.md`,
   `memory.md`, and `finished.md` exist at parent and child. Only the absolute prefix
   distinguishes them, and the prompts use relative names.
3. **The evidence is incomplete** (F1, F10). The child's `run.json` — its token spend
   and its D9 model audit trail — is in `~/.team-harness/runs/`, not in
   `children/<child_id>/`. A planner reviewing "child evidence" cannot see what the
   child cost or which tier its agents actually used. `children.json`'s `usage` field
   is populated from `session_tree_usage_totals`, which is all zeros (F1).

**The assignment contract is thin.** `ChildSessionRequest` is `{workflow_set, goal,
schema_version}` (`models.py:257-267`). Everything the dispatcher is told to include —
"the selected PM item id/title", "concrete acceptance criteria", "expected delivery
evidence" (`dispatcher/prompt.txt:50-57`) — is prose crammed into the single `goal`
string. Nothing links the child back to the PM item it implements; the planner
re-associates them by reading markdown. That works, and it is D1-consistent (files are
truth), but it means "did this child do what it was asked?" is answerable only by an
LLM re-reading two markdown files.

### 6.3 Triple loop (depth ≥ 3) — one guard away mechanically, further away contractually

**What already generalizes to arbitrary depth** (verified by reading):

- `create_session_dir` nests under any parent (`sessions.py:78-85`).
- `_reconstruct_session_stack` is a `while True` walk down `active_child_session_id`
  — depth-agnostic (`coordinator_app.py:839-879`).
- `session_tree_usage_totals` recurses correctly *by induction*: each child's record
  stores its own **subtree** total at finalization
  (`_mark_child_record_complete` → `session_tree_usage_totals(state=child_state)`), so
  summing one level of `children.json` is correct at every level (`coordinator_app.py:1273-1278`).
- `_resume_parent_if_active_child_completed` recurses via `register_worker` and would
  unwind multiple levels.
- `worker.py:413`'s parent detection (`session_dir.parent.name == "children"`) happens
  to work at any depth, since every nesting level uses `children/`.
- `_status_lines` and `_deepest_active_session_id` walk the chain with cycle guards.
- `sessions.py:session_dir_path`'s `rglob` finds arbitrarily nested sessions (F9 —
  correct here, for the wrong reason).

**The single mechanical blocker** is `coordinator_app.py:1029-1030` (F8), exactly as
`proposals/improvement-proposals.md` ("Future direction — Deeper depth-first child
chains") records. That proposal's conditions are the right ones and I endorse them
unchanged.

**The contractual blockers are the real cost, and they get worse with depth:**

| Gap | At depth 2 | At depth 3 |
|---|---|---|
| No rendered depth/role (F7) | tolerable — two roles, two workflow sets | a session cannot tell "am I the middle or the leaf?" |
| Relative `project_state/` names (F7) | 2 identical trees | 3 identical trees; a misresolution is invisible |
| Grandparent → grandchild evidence | n/a | **no contract at all** — `children.json` indexes only direct children; nothing aggregates status two levels down |
| `loopy stop` (`cli.py:470-488`) | flags top-level only; child does not see it; honored after child terminates | a 3-deep chain must unwind twice before a stop takes effect; worst case is two full child sessions |
| `max_cost_usd` | already inert (F1); design notes an in-flight parent/child overshoot | overshoot compounds per level |

**Assessment.** A triple loop is not architecturally rejected and the state machinery
would mostly carry it. But shipping it on top of a prompt contract that renders no
depth, no role, and no ancestry — where the durable `parent_session_id` is not even
visible to the worker — would multiply Finding 7 by the number of levels. **Fix the
contract before removing the guard.** That ordering matches D6's own logic (harden the
prerequisites first, they are not later polish).

---

## 7. Eval and git/PR assessment

### 7.1 Eval creation and execution locations

| Concern | Current | Verdict |
|---|---|---|
| Check authoring | `eval_reviewer` → `<session>/eval_checks/*.yaml` | Correct location; **schema is wrong** (F2) |
| Check execution | `eval_runner` → `eval-banana run --cwd . --check-dir <session eval_checks> --output-dir <session>/eval_results` | Correct; `--cwd .` correctly evaluates the repo from root |
| Judge configuration | `.eval-banana/config.toml` — **not shipped, not documented**, found by upward directory walk | **Broken** (F3) |
| Judge model | ambient; outside `model_tiers`, outside `RootConfigSnapshot`, not inherited by children | **Gap** vs D4/D9 (F3) |
| Eval output path | `<session>/eval_results/<run_id>/` | Correct and gitignored; **never rendered** (F13) |
| Evidence → stop | `goal_check.json` is evidence; `control.json` stops | **Correct** (D3) |
| Who may declare `goal_met` | `eval_runner` (evidence-bound) **and** `outer` (unbound) | **Gap** (F5) |
| Deterministic checks | forbidden in stock template | **Correct** (D4) — do not change |

**The eval layer is the system's arbiter of quality (D3/D4) and it is currently the
least functional part of it.** Two independent defects (F2, F3) each individually
prevent any check from running on a clean target repo, and a third (F5) lets the loop
declare success without it. All three fail in safe directions — no false `goal_met`
*from the eval path* — but the aggregate is that the recommended template's evidence
channel does not work out of the box, and the one authority that *can* stop without it
is the planner judging its own plan.

**D4 remains correct and should not be loosened.** `P1.2` (target-owned deterministic
backstop) remains the right conditional follow-up, unchanged.

### 7.2 Git and PR delivery

**What is well specified.** The PR/branch/merge policy is stated consistently in three
places — `inner/prompt.txt:61-75`, `outer/prompt.txt:211-226`, `SKILL.md:391-406` —
and is coherent: branch → PR → checks → merge; default yes for repo-changing work; no
for session-state-only/eval-only/research-only/planning-only or no-remote/auth; one PR
per repo for multi-repo work; never merge on failing checks or for
destructive/monetary actions. `docs/session-layout.md:120-126` requires delivery
evidence (repo, branch, PR URL, merge status, merge commit, checks/CI) in
`finished.md`. Eval workflows are explicitly barred from creating branches or PRs
(`eval_reviewer/prompt.txt:48-50`, `eval_runner/prompt.txt:32-34`). This is a genuinely
well-drawn boundary.

**What is missing.**

1. **Delivery evidence is unstructured and unverified.** PR URL, merge status, and
   checks status live as prose in `finished.md`. Nothing parses them; nothing verifies
   a claimed merge; a planner reviewing child evidence takes the child's word for it.
   The system's own D1 premise ("files and git are truth") applies to git *state* —
   but the loop never actually reads git state. `git log`, `gh pr view`, and the merge
   commit are the ground truth, and no workflow is required to reconcile the claim
   against them.
2. **Nothing scopes what may be committed** (F4). No `.gitignore` protection, no
   detection of scratch in the diff, no branch-name convention rendered.
3. **No write barrier, though D8 describes exactly one.** D8's own worked example is:
   *"a workflow set ships a deterministic 'write barrier' check that diffs protected
   paths against the session-start digest; a child session can physically write
   anywhere, but cannot terminate successfully while the barrier fails."* No packaged
   workflow set ships such a check. The mechanism D8 names as the concrete shape of the
   principle does not yet exist anywhere in the repo.

---

## 8. Recommended target contract (PROPOSAL — not current behavior)

The through-line of every finding is: **the engine renders paths but not
identity, and the prompts speak in names the runtime cannot resolve.** The target
contract closes that gap without adding a single fence (D8) and without touching
D1–D9.

### 8.1 Render identity, not just paths

Extend `_render_prompt` (and `RootConfigSnapshot` where the fact is not derivable) so
every coordinator receives its own identity explicitly rather than inferring it:

```
Role: child implementation session          # from the workflow set + parent linkage
Loop depth: 2                               # 1 = top-level
Owning session: <session_id>
Parent session: <parent_session_id>         # from LoopState.parent_session_id, NOT the path
Parent session directory: <abs>             # keep; drop the path-shape heuristic
Ancestry: <root_id> > <parent_id> > <self>  # for depth >= 2
```

`parent_session_id` must be added to `RootConfigSnapshot` for this — the worker cannot
see it today (F7). That is an additive wire change; released workers validate the
snapshot with `extra="forbid"` (`coordinator_app.py:63-64`), so it is a coordinated
version bump, not a free addition. Worth doing once, alongside R1.

### 8.2 Render every path the stock prompts name; render only what the role needs

Add the three missing renders — `Session eval_results directory`,
`Session children index path` (`children.json`), `Session children directory` — and
gate the role-inappropriate ones: do not render `Session child_requests directory` to a
session that cannot dispatch (F8), and do not render `Session control path` to a
workflow with no stop authority. Then **replace every bare relative name in the stock
prompts with the rendered label**: `project_state/memory.md` →
`<Session project_state directory>/memory.md`. This single edit removes the largest
class of silent ownership conflation (F7) and costs nothing but prompt text.

### 8.3 Make the two worlds addressable

Give trace a rendered, session-local home and stop relying on the repo root:

```
Iteration scratch directory: <session>/iterations/<NNNN>_<wf>/scratch/
```

Point `_feature_planning/` and `_additional_context/` at it in the stock prompts,
resolve the three-way contradiction (F4), and — belt and braces — extend
`GITIGNORE_LINES` so a scratch dir that still lands at the repo root cannot be
committed.

### 8.4 Make the session directory a complete trace

Copy or hard-link team-harness's `run.json` into
`<session>/harness_outputs/<NNNN>_<wf>/<run_id>/run.json` at iteration end, and read
usage from there. This simultaneously fixes F1, makes the D9 audit trail visible to
reviewers and eval checks (A5), and makes cloud export of the session directory
lossless (F10). Recovery keeps reading `RUNS_DIR` — that is where the *live* record is
and must stay (contra the antigravity note's Priority 2).

### 8.5 Express the prose invariants as shipped detection (D8's own shape)

For each of A1–A5, ship a **set-owned deterministic check** — not agent-authored (D4's
boundary), not a fence (D8) — whose failure blocks acceptance:

- **write barrier**: no changes outside the session dir + declared repo paths;
- **scratch barrier**: no `_feature_planning/`, `_additional_context/`,
  `.loopy_loop/state.json.lock` in the delivered diff;
- **evidence barrier**: `stop_reason: goal_met` requires a passing eval report in this
  session (closes F5);
- **delivery barrier**: a `finished.md` entry claiming a merge must match real git
  state.

D8 already describes the first of these as the canonical shape. This is implementing
the decision, not amending it.

---

## 9. Phased recommendations (PROPOSAL)

Ordered by (correctness × blast radius) ÷ effort. Each names its finding and its
decision alignment.

### Phase 0 — Correctness bugs (do first; small, isolated, no design change)

| # | Action | Fixes | Effort |
|---|---|---|---|
| **R1** | Persist team-harness's `run.json` into the iteration's harness output dir at run end and read usage from there. Keep `recovery.py` on `RUNS_DIR`. Add an integration test that runs a **real** `TeamHarness` (or a fake that mirrors its actual write locations) and asserts `usage is not None` — the current fixture authors the file at the path under test. | **F1**, F10, A5 | S |
| **R2** | Delete the `target_paths` line from `eval_reviewer/prompt.txt`; state the actual 0.3.1 harness_judge schema (`schema_version`, `id`, `type`, `description`, `tags`, `instructions`, `model`). Add a test that round-trips a prompt-conformant check through `eval_banana`'s loader so prompt-vs-dependency drift fails CI. Refresh the stale "target file" comment in this repo's `.eval-banana/config.toml`. | **F2** | S |
| **R3** | Ship `.eval-banana/config.toml` in both templates (or pass `--harness-agent` explicitly in the `eval_runner` prompt), and document the judge's configuration in `README.md` + `SKILL.md`. Prefer a judge family different from the implementer (D4). Record the effective judge agent/model in session evidence. | **F3**, A4 | S |
| **R4** | Extend `GITIGNORE_LINES` to `.loopy_loop/state.json.lock`, `_feature_planning/`, `_additional_context/`, `_outputs/`. Reconcile the two template `.gitignore` files. Drop the genuinely dead `.loopy_loop/state.json` / `.archive_*` lines (verify first). | **F4**, **F6** | S |
| **R5** | Fix `pm_planner_dispatcher/loopy_loop_goal.txt` to be a real example target with completion criteria (mirroring the `inner_outer_eval` goal's shape). | **F11** | XS |
| **R6** | Resolve the `_feature_planning` three-way contradiction in `inner`/`outer` prompts; fix the duplicate CODEX reviewer (F14); add `Session goal path` to the eval prompts' input lists (F13). | F4, F13, F14 | XS |

### Phase 1 — Contract (the substance of this audit)

| # | Action | Fixes | Effort |
|---|---|---|---|
| **R7** | Render role, depth, owning session, parent session id, and ancestry (§8.1). Add `parent_session_id` to `RootConfigSnapshot`; retire the `session_dir.parent.name == "children"` heuristic. | **F7** | S–M |
| **R8** | Render `eval_results`, `children.json`, `children/`; gate role-inappropriate paths; **rewrite every bare relative name in the stock prompts to use rendered labels** (§8.2). | **F7**, F13 | S |
| **R9** | Detect what is currently silent: log + `*.json.rejected` a child request written by a session that cannot dispatch, and emit an event (§F8). Cheap, and it is the D8-shaped fix. | **F8** | S |
| **R10** | Add the evidence barrier: `goal_met` in `control.json` requires a passing eval report in the same session (set-owned check, not an engine fence). | **F5** | M |
| **R11** | Resolve session paths from the durable pointer chain, not `rglob`; return `None` for not-found instead of a plausible top-level path. | **F9** | S–M |

### Phase 2 — Structural (only with a concrete driver)

| # | Action | Fixes | Effort |
|---|---|---|---|
| **R12** | Ship D8's write barrier + scratch barrier + delivery barrier as a workflow-set-owned check bundle (§8.5). Reconcile `finished.md` delivery claims against real git state. | A1–A3, §7.2 | M |
| **R13** | Structure `ChildSessionRequest`: add optional `work_item_id`, `acceptance_criteria`, `origin_iteration`. Additive; keeps `goal` as the human-readable form. Makes parent→child assignment machine-checkable. | §6.2 | M |
| **R14** | Define the cloud trace export contract on a session directory that is complete after R1 (the antigravity note's §4 schema sketch is a reasonable starting point — but only after R1, since its premise about `run.json` placement is wrong). | F10 | M |
| **R15** | Depth ≥ 3: **only after R7/R8**, and only with a target that demonstrates a real three-level need — per `proposals/improvement-proposals.md` ("Future direction"), whose conditions I endorse unchanged. Add grandparent→grandchild evidence aggregation and tree-wide `stop` (P2.4) as prerequisites, not follow-ups. | §6.3 | M–L |

**What I am explicitly not recommending**, because D1–D9 dispose of them: parallel
loopy workers (D2), deriving iteration success from agent exit codes (D3), loosening
the stock deterministic-check ban (D4), any `paused`/`waiting_for_human` gate or
approval flow (D5), path-level write enforcement or scheduling vetoes (D8), and
per-depth or per-session coordinator model differentiation (D9).

---

## 10. Alternatives and tradeoffs (PROPOSAL)

### A. `run.json` placement (Finding 1) — three options

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A1. Copy/link `run.json` into the session at run end** (R1) | Fixes usage, D9 audit trail, and cloud export together; no team-harness change; recovery untouched | one duplicated file per run; a snapshot, not live | **Recommended** |
| A2. Read usage from `RUNS_DIR` in the worker (mirror `recovery.py`) | one-line fix | leaves the trace incomplete; keeps the session non-exportable; breaks under split `HOME` | Fixes the symptom, not the contract |
| A3. Ask team-harness to write into `session_output_dir` | cleanest long-term; the harness already knows the dir | cross-repo change + release; loopy must handle older versions anyway | Right long-term; do A1 now |

### B. Where scratch lives (Finding 4)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **B1. Rendered session-local scratch dir** (§8.3) | scratch is trace, lands in the gitignored session tree, exports with it | prompts must be rewritten; agents may still ignore it | **Recommended** |
| B2. Keep repo-root scratch, just gitignore it | one-line | trace pollutes the working tree; invisible to session export; `git clean -x` hazard | Do as belt-and-braces alongside B1 (R4) |
| B3. Run the harness with `cwd = session dir` | relative names resolve into the session automatically | **breaks everything**: the target repo *is* the work product; agents must edit it | Rejected |

### C. Preventing a premature `goal_met` (Finding 5)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **C1. Evidence barrier check** (R10) | pure D8 detection; agent can see, contest, repair | costs an eval run; needs F2/F3 fixed first | **Recommended** |
| C2. Engine refuses `goal_met` without a passing `goal_check.json` | airtight | **a semantic gate — D8 explicitly rejects this**; would need amending D8 | Not recommended |
| C3. Remove `outer`'s stop authority | simple | leaves no stop path until iteration 23; a genuinely-done loop must burn 20 iterations | Not recommended |
| C4. Loosen `eval_runner`'s cadence so evals run early | cheap; more evidence sooner | more judge cost; does not close the hole, only narrows it | Reasonable complement to C1 |

### D. Triple loop (§6.3)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **D1. Keep the guard; fix the contract first** (R7/R8, then reassess) | the machinery already generalizes; the *contract* is the actual blocker | defers a capability nobody has demanded | **Recommended** — matches the existing proposal |
| D2. `max_depth` config now (as `antigravity…md:289` proposes) | small diff | ships depth on a contract that renders no depth; multiplies F7 per level; no grandparent evidence contract; `stop` unwinds slowly | Premature |
| D3. Never support depth ≥ 3 | simplest | contradicts D9 ("any deeper level") and the existing proposal | Over-committal |

### E. A high-level redesign I considered and am **not** proposing

**Split the session directory into `state/` and `trace/` subtrees with separate
ownership**, e.g. `<session>/state/{control.json,project_state/,eval_checks/}` and
`<session>/trace/{iterations/,harness_outputs/,events.jsonl}`.

*Attraction:* it makes the two worlds structural instead of conventional — exactly the
brief's framing. Export ships `trace/`; a reviewer reads `state/`; the write barrier is
a one-line path predicate; the state/trace boundary stops being a naming convention.

*Why I am not proposing it:* it is a breaking change to every packaged prompt, both
docs, the Skill, and every existing on-disk session, and it buys **nothing that R1+R8
do not buy more cheaply**. The current flat layout is already unambiguous — each
artifact has exactly one owner and one location. The problem was never that state and
trace share a directory; it is that (a) one trace file lives *outside* the tree
entirely (F1/F10) and (b) prompts address the tree with names that do not resolve
(F7). Fix those two and the flat layout is fine. Recording the rejection here so a
future reader does not re-derive it.

---

## 11. Open questions

Ordered by how much they would change the recommendations.

1. **Has the usage ledger ever worked in production?** (F1) I found no session with a
   populated `usage_totals` anywhere on this machine, and 0 `run.json` under any
   `harness_outputs/`. If it demonstrably worked against some team-harness version,
   that version mirrored the file and the fix may belong upstream (option A3). If it
   never worked, R1 is a straight bug fix and the design doc's cost section needs a
   correction alongside it.
2. **Was `target_paths` valid in an eval-banana before 0.3.1?** (F2) If yes, this is
   dependency drift and P2.1's compatibility-policy proposal should be prioritized. If
   the field never existed, the prompt was speculative from the start and the lesson is
   about testing prompts against dependency schemas, not about pinning.
3. **Is the judge's model supposed to be inside loopy's config?** (F3) D9 makes
   `model_tiers` the single source of truth for model ids; the judge's model sits
   outside it. Bringing it in respects D9's spirit but couples loopy to eval-banana's
   config format. Leaving it out means D4's cross-family-judging guidance stays
   unenforceable and unauditable. This is a real design choice, not an oversight to
   patch.
4. **Should `outer` retain `goal_met` authority at all?** (F5) The alternatives
   (C1/C3/C4) trade differently depending on whether the eval layer is expected to be
   reliable once F2/F3 are fixed. Worth deciding explicitly rather than by default.
5. **Is `pm_planner_dispatcher`'s process-goal deliberate?** (F11) Is the operator
   expected to always replace `loopy_loop_goal.txt`, making the scaffold a placeholder?
   If so, say so in the file. If the PM loop is meant to run against a target's *own*
   authoritative plan (D6's phrasing), the template should point at that plan rather
   than describing the dispatch procedure.
6. **What is the intended deployment topology?** Several guarantees are same-host by
   construction (worker liveness, orphan reaping, `RUNS_DIR` access). If
   coordinator/worker separation is a real target, F10's silent degradation needs a
   durable record; if it is not, the docs could say "same host, by design" and close
   several caveats at once.
7. **What exactly is exported to the cloud?** (F10) The whole session subtree, or a
   schema projection? It determines whether R1's copy is sufficient or whether agent
   CLI stdout/stderr (also in `RUNS_DIR`) must be captured too.
8. **How should prompt↔dependency contracts be tested?** F2 and F3 are the same class
   of bug: prose asserting a contract with a dependency, with no test. A round-trip
   test (prompt-conformant artifact → dependency's own validator) would have caught
   both. Is that worth generalizing, or is the eval seam the only one that matters?

---

## Appendix — Evidence index

Claims marked **(reproduced)** were verified by executing code during this audit.

| Claim | Anchor |
|---|---|
| Iteration success = harness returned (D3) | `harness_runner.py:254` (`_normalize_harness_result` → `success=True`) |
| Usage read from session dir | `worker.py:329` |
| Reaper record read from `RUNS_DIR` | `recovery.py:155` |
| `run.json` written only to `RUNS_DIR/<run_id>` | `team_harness/harness.py:125`, `team_harness/tracking/run_log.py:33`, `team_harness/config.py:20` |
| 1333 `run.json` in `RUNS_DIR`, all with `turns[].usage`; 0 under any `harness_outputs/` | **(reproduced)** |
| A real past run's harness output dir holds only `generated_scratch` | **(reproduced)** — `…/20260512_074434_c5b5b4304fcc_63f4fd6c/harness_outputs/0018_outer/20260512_232739_a659b033/` |
| Usage test authors its fixture at the path under test | `src/tests/test_events_and_usage.py:342-399` |
| Budget compare that can never fire | `coordinator_app.py:1313-1324`; `config.py:222-230` (`gt=0`) |
| `target_paths` teaching | `templates/inner_outer_eval/.../eval_reviewer/prompt.txt:61` |
| harness_judge forbids extras | `eval_banana/models.py:34-35, 80-84` |
| `target_paths` → `extra_forbidden` (exit 1) | **(reproduced)** |
| No harness agent → SystemExit | `eval_banana/runner.py:80-108`; `eval_banana/config.py:112` |
| Clean repo eval abort (exit 1) | **(reproduced)** |
| eval-banana config found by upward walk | `eval_banana/config.py:find_local_config` |
| harness cwd = repo root | `harness_runner.py:156` |
| `_feature_planning` three ways | `outer/prompt.txt:277`; `inner/prompt.txt:137, 151-152` |
| "download repos into `_additional_context/`" | `inner/prompt.txt:147-148`; `outer/prompt.txt:284-285` |
| One gitignore line | `cli.py:73`, `cli.py:499-511`, `cli.py:215-221` |
| Target repo gets only `.loopy_loop/sessions/` | **(reproduced)** |
| Root lock file path | `state_store.py:72-80` |
| Two `goal_met` authorities | `eval_runner/prompt.txt:64-65`; `outer/prompt.txt:96-108` |
| First `eval_runner` ≈ iteration 23 | `scheduler.py` + `eval_runner/config.yaml` (`not_before_iteration: 10`, `must_follow`, `run_after_successes`) — traced by hand |
| No role/depth rendered; parent by path shape | `worker.py:367-420`, esp. `413-414`; `models.py:70-90` (no `parent_session_id`) |
| Grandchild guard | `coordinator_app.py:1029-1030` |
| Grandchild gap documented | `skills/loopy-loop/SKILL.md:380-381` |
| `rglob` session resolution | `sessions.py:146-155` |
| Child inherits frozen parent snapshot | `coordinator_app.py:1100-1106` |
| Subtree usage rolled up at finalization | `coordinator_app.py:1273-1278` |
| Suspended-parent invariant | `coordinator_app.py:375-405` |
| PM goal is a procedure | `templates/pm_planner_dispatcher/loopy_loop_goal.txt` |
| Extension asymmetry | both templates' `loopy_loop_config.yaml` |
| Prior note's `run.json` error | `design/analysis/antigravity-loop-contract-review.md:46, 284` |

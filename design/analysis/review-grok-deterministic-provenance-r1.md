I'll critically review the design against the source, starting with the design corpus and the coordinator/model code the claims rest on.Reading the coordinator functions and contract details that the design claims rest on.Checking how assignment surfaces attempt identity (for stamp fallback) and whether `LoopState` schema changes would break frozen sessions:# Design review: deterministic handoff + eval provenance

## Code-grounded claims: confirm / refute

| Claim | Verdict | Evidence |
|---|---|---|
| `_handoff_producer_is_known` accepts any past attempt | **Confirmed** (quote slightly incomplete) | Current attempt **or** any `state.history` entry with same session/workflow/attempt (`coordinator_app.py:5764-5783`). Design omits `session_id` match. |
| Untouched stale handoff is `valid` | **Confirmed** | Same digest + same revision never trips `non_monotonic` (`:5727-5736`). Observation is non-gating (`:5672` docstring; success only flipped by control/goal_check). |
| `expected_outputs` advisory-only; only control repaired | **Mostly confirmed; imprecise** | No finish-time linter over `expected_outputs`. **v3 control** is reject/repair (`:6243-6317`). **v1 goal_check** also flips success/caps (`:1501-1526`). Handoff observation feeds **terminal** `handoff_ref` validation only (`:5559-5577`), not mid-loop repair. |
| Stock `check_runner_roles: [outer]`; `eval_results.md` owned by `eval_runner` | **Confirmed** | `contract.yaml:38-49`. Roster advertises `eval_state.md` for check-runner, **not** `eval_results.md` (`coordinator_app.py:367-375`) — despite comment claiming check-runner writes `eval_results.md`. |
| Move check_runner → `[eval_runner]` is safe | **Mostly safe for stock path; not free** | `_accept_current_eval_receipts` only seals when `active.workflow_id ∈ check_runner_roles` and never gates (`:5580-5594`). Stock prompts write `eval_results.md`, not `EvalReceipt` JSON. Caveats below. |
| Same-rev provenance re-stamp is misflagged `non_monotonic` today | **Confirmed** | Same revision + any digest change → `non_monotonic` (`:5727-5736`). Producer/`updated_at` are in `LayerHandoff` bytes (`models.py:420-438`), so a pure re-stamp **must** change digest. |
| Relaxation “won’t break” tamper detection | **Not proven; under-specified** | Design says “provenance-only” but never defines the structural compare. Naïve “same rev + current producer OK” **does** destroy non_monotonic. |

---

## 1. Correctness (wrong / imprecise)

1. **“Only control.json is enforced”** — wrong for the engine as a whole (goal_check path). Right for v3 mid-loop role outputs of interest. Cite `coordinator_app.py:1501-1547`.

2. **Postmortem’s `_handoff_producer_is_known` snippet** drops `entry.session_id == state.active_session_id` and the current-task branch (`:5772-5782`). Conclusion still holds.

3. **Comment vs code at roster** (`:369-375`): comment says check-runner produces `eval_results.md`; code still appends `eval_state.md`. Design inherits that mess when it says “first enforced expected_output.”

4. **`models.py:588-589`** still says handoff observation “never gates scheduling/control.” Design **does** gate finish success / re-dispatch. That’s a deliberate model change; docs that still say “diagnostic only” are wrong the moment D13 ships. Terminal control already soft-gates via `handoff_ref` (`:5559-5577`).

5. **D14 / design: “path not in engine core”** while Change B §2 and phase 3 hardcode `project_state/eval_results.md` in `_build_workflow_roster`. That is the same hardcoding pattern as `handoff.json` / `plan.md` today (`:355-375`) — so either the invariant is already violated, or the design’s wording is false.

6. **“Triggered attempt = eval_request stood at dispatch”** is underspecified relative to real lifecycle: scheduler uses presence of `eval_request.md` (`scheduler.py:76-81`, `sessions.py:904-910`); **eval_runner prompt deletes it during the run**. Finish-time existence is a race against a successful run. For stock `run_when_requested: true` on `eval_runner` only, **“role finished” ≈ “was triggered”** — the dual “request-gated enforcement” path is mostly noise if check_runner is only `eval_runner`.

---

## 2. Invariant violations

**Inv 1 (D11 optional/advisory eval)** — **OK if implemented carefully.** Presence/stamp of a file when the check-runner **runs** is structural, not “must pass.” Risk: treating `run_passed: false` or commit mismatch as engine failure would violate D11; design keeps that prompt-only (good). Respawning for **missing** stamp is D8-shaped, not D11, as long as sessions with empty `check_runner_roles` are untouched (`:5593` pattern).

**Inv 2 (user-owned contracts; no hard-coded paths)** — **Violated by design text as written.** Wiring `eval_results.md` into engine roster/enforcement embeds a stock path. Fix: derive currency-tracked outputs from **declared contract** (e.g. `state[]` entries with a `currency: true` / `kind: role_output` flag, or contract-level `currency_outputs`), not string literals in `coordinator_app.py`.

**Inv 3 (D8 structural vs semantic)** — **Handoff current-attempt ownership is a legitimate D8 carve-out** (matches “current-attempt ownership… stale input,” `decisions.md:288-291`). **Eval stamp presence** is also structural. **Not structural:** engine interpreting “commit ≠ HEAD ⇒ must re-run” as a gate (design leaves to prompts — keep it there). **Danger zone:** same-rev relaxation that accepts arbitrary content rewrite = smuggled hole in hash/monotonic integrity, not a semantic gate, but it **weakens** the structural story.

**Inv 4 (D12 compact continuity)** — **OK.** Raw root is discoverability only; routine path still handoff/plan. Risk is prompt wording that makes raw-trace reading the default — keep “fallback when summary contradicts live state.”

**Inv 5 (minimal engine)** — **Mostly OK.** No merge-when-approved rule. Adding a generalized “role-output currency linter” + per-kind counters + markdown provenance parser is still real engine surface. If the parser embeds eval-banana’s fenced schema, that’s cross-tool coupling not declared as an engine protocol.

---

## 3. Risks / gaps / edge cases

**A. Same-revision re-stamp (highest implement risk)**  
Must specify: load `accepted_handoff_snapshot`; accept same `revision` iff  
`producer` is the finishing attempt **and** all fields except `producer`/`updated_at` equal the prior accepted payload (or raw canonicalized equal after stripping those keys).  
Otherwise either (i) re-stamp stays `non_monotonic` and D13 is self-blocking, or (ii) full content rewrite at fixed revision becomes legal.

**B. Conditional eval enforcement**  
- Finish-time `eval_request.md` check is wrong (file consumed).  
- Prefer: enforce currency on **every finish of a declared check_runner role** (stock set already only schedules that role when requested).  
- “Skipped/no-request stamped file” is optional polish, not a second enforcement mode.

**C. Stamp verification for `eval_results.md` under-specified**  
eval-banana design gives a parseable fence (`output-destination-capability.md:54-64`). loopy design says “presence + stamp” without: required parse rules, failure if fence missing but file exists, coupling to eval-banana schema version, or whether engine only checks attempt id and ignores `run_passed`.

**D. Re-stamp every owner iteration**  
Outer must rewrite handoff even on no-op iterations or burn `handoff_protocol_consecutive_failures` toward shared cap → `handoff_protocol_broken`. That can terminate a healthy session for forgetfulness, not corruption. No backoff, no “warn N times,” no distinction between missing file and stale producer.

**E. Dual failure on one finish**  
Order today: observe handoff → apply control (`:1491-1547`). If currency fails and control also invalid, two counters / two rejects / which error wins is unspecified.

**F. Archive semantics**  
Control `rename`s the file and writes a placeholder (`:6269-6310`). Design says **don’t** overwrite `handoff.json` for stale case (leave bytes, repair by rewrite). Good, but invalid-schema / missing cases need the same clarity (rename vs leave vs placeholder).

**G. D13 does not fix the incident alone**  
Stall was outer **not merging** while re-polishing with a schema-valid frozen handoff. Forcing re-stamp only makes `open_work: ["merge M1"]` **freshly stamped**, not true. Convergence still depends on **D15 prompt** (anti-over-polish + reconcile). Substrate fix removes one failure mode; it does not create a stop condition.

**H. `check_runner_roles: [eval_runner]` side effects**  
- Roster: outer loses `eval_check_runner` authority; gains nothing for `eval_results.md` unless you also change the advertised path.  
- Receipt sealing only on eval_runner finish — fine if receipts are dead for stock v3 (they are; roster retired `eval_receipts/`, tests assert that).  
- BYO sets that still put `outer` as runner and write receipts break silently until updated. Frozen contracts keep old behavior — OK.

**I. Version skew fallback**  
Prompt hand-writes stamped markdown if no `--result-out` reintroduces partial hand-copy failure (wrong path, bad fence). Enforcement then thrash-respawns. Document minimum version is not a runtime guard.

**J. Frozen-contract sessions**  
Already-running M1-class sessions **never** get D13/D14. Design admits this; operators must restart/amend. Call that out as operational, not just technical.

**K. Raw root only for orchestration role**  
Need explicit gate in `_write_iteration_paths` (`worker.py:956-1021` has no `raw` key today). Confirm layout: folded `raw/<iter>_<workflow>/…` (`sessions.py:1311-1328`) matches prompt guidance (`raw/<iter>/…` in design is slightly vague).

**L. LoopState field**  
`handoff_protocol_consecutive_failures` needs default `0` on old state loads (pydantic default OK) and projection/docs; stop_reason string must be allowed wherever stop reasons are enumerated.

---

## 4. Over- / under-engineered

**Over:**  
- Generalized “role-output currency linter” for two outputs before a second kind exists — start with handoff-specific reject path parallel to control; add generic helper when a second kind ships.  
- Dual “triggered vs untriggered” eval enforcement modes.  
- Shared naming `role_output_rejected/` while control keeps `control_rejected/` mid-migration — fine, but don’t block on unified taxonomy.

**Under:**  
- Exact non_monotonic relaxation algorithm (must be field-level).  
- How engine reads stamp from markdown (schema, fail modes).  
- Interaction order with control rejection + counters.  
- Contract-driven declaration of currency outputs (to satisfy inv 2).  
- Whether `success=False` on handoff failure should skip control application or still run it.  
- Tests for: semantic same-rev rewrite still `non_monotonic`; non-owner past producer still `valid`; eval_runner without file respawns; no-eval contract unchanged.

**Simpler sufficient core:**  
1) Owner finish requires `producer == finishing attempt` or archive+respawn.  
2) Provenance-only same-rev advance via structural compare.  
3) Contract/path for eval result declared in workflow-set; enforce presence+attempt only when that role finishes.  
4) eval-banana `--result-out`.  
5) `paths.json` raw root + outer prompt reconcile/anti-polish.  

Drop: multi-mode trigger logic, deep “linter framework,” engine interpretation of stale commits.

---

## 5. Verdict

**Not sound to implement as written.** Direction is right; three items must be fixed first.

### Top 3 blockers

1. **Specify same-revision “provenance-only” as a structural field/digest compare** so non_monotonic still catches content rewrites. Without this, D13 either self-deadlocks or opens a tamper hole (`coordinator_app.py:5727-5736`).

2. **Remove engine hardcoding of `eval_results.md` / finish-time `eval_request` fiction.** Enforce currency from **declared contract** on **check_runner role finish**; define stamp parse (fence schema + attempt match) or enforce a tiny sidecar the engine already understands. Align D14 text with inv 2.

3. **Be honest about causal fix for the stall:** D13/D14 are necessary substrate; **D15 prompt is load-bearing** and untestable by the engine. Ship D13 without turning forgetful re-stamp into an easy `handoff_protocol_broken` death spiral (reset rules, surface repair assignment clearly, consider not sharing the tight control cap until measured).

Secondary before merge: dual-failure ordering; archive-vs-leave for handoff; LoopState/stop_reason; raw-path prompt accuracy; BYO migration notes for `check_runner_roles`.

**Implement after those three are written into the binding design with algorithms and tests.** Until then, treat the corpus as directionally correct analysis, not an implementable contract.

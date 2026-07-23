I'll re-review the revised design against the original blockers and the real source, reading the design docs and first-round reviews first.Next I'll read D13–D15 and the postmortem, then ground-check the design against coordinator/scheduler/model code.Checking one more critical interaction: whether `goal_met` can omit `handoff_ref`, and how config caps are declared.# Re-review: deterministic handoff + eval provenance (r2)

## Per-blocker (claims 1–7)

| # | Status | Evidence |
|---|--------|----------|
| **1** Frozen discriminator | **RESOLVED** | Design §0 + D13: `WorkflowSetContract.schema_version` 1→2 + typed `currency_outputs`; new behavior only when frozen `schema_version >= 2` (`deterministic-handoff…md:49–69`, `decisions.md:526–527`). `_validate_finished_binding` returns the attempt-frozen contract (`coordinator_app.py:1615–1691`); finish passes it into `_record_finished_task` (`:1365–1383`). Live stock is `schema_version: 1` (`contract.yaml:1`) — cannot trip new path. `session_protocol_version` is already 3 on both old/new → wrong discriminator; schema bump is right. |
| **2** Tiered handoff disposition | **PARTIALLY** | A1 location is correct: extend `_validate_control_handoff_reference` (`:5559–5578`) called from `_validate_v3_control` (`:5472–5476`) → `_reject_v2_control` (`:6243–6317`). A2 correctly avoids `success=False` / general cap (`:1574–1576`, `:2598–2618`) and does not need `_failed_workflow_retry` — outer is always eligible (`run_every: 1`). **Hole:** `handoff_ref` is optional today (`models.py:695`, `http-contract.md:485–490`); A1 text is only “when … citing” (`deterministic-handoff…md:97–99`) while D13 says goal_met **must cite** (`decisions.md:533–535`). Omitting `handoff_ref` still accepts `goal_met` with a stale accepted snapshot. |
| **3** Currency only if `request.success` | **RESOLVED** | Explicit rule + crash rationale (`deterministic-handoff…md:89–91`, `:145–149`, `:253–254`; D13 `:532–534`). Matches Codex crash-archive concern; ordering vs `_apply_session_control` stated. |
| **4** Same-rev re-stamp algorithm | **RESOLVED** | Exact IFF: producer==finishing owner ∧ `updated_at` non-decreasing ∧ strip({producer,updated_at}) equal else `non_monotonic` (`deterministic-handoff…md:123–141`). Implementable on parsed `LayerHandoff` / `AcceptedHandoffSnapshot` (`models.py:420–438`, `:496–505`); preserves `:5727–5736` tamper case for any other field rewrite. |
| **5** Eval quarantine-only (D11) | **RESOLVED** | No fail/respawn/cap path remains (`deterministic-handoff…md:153–192`, D14 `:597–602`, tests 7–8). `check_runner_roles` unchanged is consistent — it is receipt authority (`models.py:316–320`, `:5593`, `:5549`), independent of `currency_outputs` advisory location. Mandatory-final-eval sentence removal stated (still present in stock outer `prompt.txt:44–46` as work to do). |
| **6** eval-banana observe-git / per-check / envelope | **PARTIALLY** | Docs aligned: `--result-out` only, no `--result-name`, observe commit+dirty, status envelope (`output-destination…md:38–87`; loopy `:163–170`). Per-check **model** matches (`models.py:80–84`, `harness_judge.py:250–298`). **Overclaim:** “family/model/effort can differ by check via per-check model overrides” — only `model` is per-check; effort is global harness config. |
| **7** Honest causality (D15 load-bearing) | **RESOLVED** | Design lead + D15 + postmortem (`deterministic-handoff…md:17–20`, D15 `:635–637`, postmortem `:136–139`). |

---

## NEW inconsistencies / source mismatches introduced by the revision

1. **D13 vs binding design vs code on `handoff_ref` (critical).**  
   D13: “Terminal `goal_met` control **must cite** a handoff…” (`decisions.md:533–535`).  
   Binding A1: only extends validation **when** control cites handoff (`deterministic-handoff…md:97–99`).  
   Code/docs: `handoff_ref` optional (`models.py:695`, `http-contract.md:485`).  
   Net: A1 does not close “declare done on stale continuity” without either requiring citation under `schema_version>=2`+`kind:handoff`, or checking producer currency on every `goal_met` independent of citation.

2. **A1 still double-counts the general workflow failure cap.**  
   `_reject_v2_control` → `invalid_v3` → `success=False` (`:1544–1547`) → `_track_workflow_failure_cap` (`:1574–1576`). Soft path is correctly exempted; hard path is not. Design only discusses the control cap (`:250–252`). Not a death spiral for rare completion attempts, but unstated dual-counter behavior.

3. **INV2 residual for handoff path.**  
   Currency targets come from `currency_outputs` (good for eval). A1 still rides the hard-coded canonical check `reference != "session:/project_state/handoff.json"` (`:5564–5565`) and `handoff_path()`. Design never requires `currency_outputs[kind=handoff].path` ≡ protocol handoff identity. Stock matches; BYO divergence would split observation vs currency.

4. **“Quarantine” has no existing engine object for `eval_results.md`.**  
   Receipts have seals (`accepted_eval_receipt_seals`); markdown results do not. Quarantine = emit `eval_stale` only is fine for INV1, but “not accepted as current evidence” implies an acceptance registry that does not exist. Implement as pure diagnostic unless a structure is specified.

5. **eval-banana “per-check effort differs”** overstates current models (see #6).

6. **Config surface for soft cap underspecified.**  
   `handoff_protocol_consecutive_failures_cap` named; no placement on `RootConfig` / `RootConfigSnapshot` (where `goal_check_consecutive_failures_cap` lives, `models.py:106–115`). LoopState field + stop_reason enum allow-list also still needed (r1 secondary).

7. **Observation docstring will be wrong after A1.**  
   `models.py:588–589` “never gates scheduling/control” — observation stays non-gating, but control path will gate on handoff currency. Update when implementing.

---

## Remaining MUST-FIX (ranked)

1. **Close the `goal_met` / `handoff_ref` hole.** Under frozen `schema_version>=2` with a `kind:handoff` currency entry: require `handoff_ref` on `goal_met` **or** reject `goal_met` when `accepted_handoff_snapshot.handoff.producer != completing attempt` even if uncited. Align binding A1 wording with D13. Without this, Codex r1 blocker #1 is only half-fixed.

2. **State A1 cap interaction:** control reject may also tick `workflow_consecutive_failures` via `success=False`; either accept and document, or exempt control-protocol failures from the general workflow cap (symmetric to A2).

3. **Contract validation:** if `currency_outputs` declares `kind:handoff`, require path/owner match orchestration handoff identity (or document that A1 always means the canonical protocol handoff, and `currency_outputs` path is advisory for prompts only).

4. **Define eval quarantine operationally:** diagnostic event only; no iteration effect; optional note that there is no engine “accepted eval_results” seal.

Nits (non-blocking): fix effort wording; add RootConfig/LoopState/stop_reason fields; update “never gates” comment; keep version-skew hand-write as degrade-to-unverified only (already stated).

---

## Final verdict

**Yes-with-noted-nits** — direction and r1 structural blockers are largely fixed (discriminator, D11 eval demotion, same-rev algorithm, success gating, soft vs hard tiering, D15 honesty). **Do not implement A1 until MUST-FIX #1 is written into the binding design** (one paragraph + test: `goal_met` without re-stamp, with and without `handoff_ref`, both rejected under schema 2). After that, sound to implement.

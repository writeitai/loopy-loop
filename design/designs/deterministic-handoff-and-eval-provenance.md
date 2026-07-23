# Deterministic handoff currency, located+fresh eval output, and orchestrator reconciliation

*Binding design (`design/designs/`). Records the accepted form of **D13–D15**. Motivated by
`design/analysis/inner-outer-eval-nonconvergence-postmortem.md`. Revised 2026-07-22 after
adversarial review by Codex (`gpt-5.6-sol`) and Grok (`grok-4.5`) —
`design/analysis/review-{codex,grok}-deterministic-provenance-r1.md`; this revision is what
their blockers require. The eval-banana half is specified in that repo's
`design/output-destination-capability.md`; this document is the loopy-loop side and the
contract between the two.*

## Problem, in one paragraph

A capable orchestrator can be defeated by its own durable state. When the rolling handoff can be
schema-valid yet stale, when the advisory eval verdict is hand-copied to an unpredictable path
with no freshness marker, and when the orchestrator is never told where ground truth lives, the
loop can spin after the work is objectively done (the M1 incident). The fix is three
**substrate-determinism** changes — not a new engine behavioral rule. **Be honest about causality:
the load-bearing fix for the M1 stall is the D15 *prompt* (reconcile-against-live-state +
anti-over-polish); D13/D14 are the necessary substrate that stops the orchestrator being *misled*,
but neither creates a stop condition.** Each change is a structural-integrity or discoverability
property (D8-compatible) or prompt guidance; none makes the engine decide whether work is *good*.

## Design invariants (any violation is a defect)

- **INV1 — Evaluation is optional and advisory (D11).** No change may make a passing eval a gate,
  make an eval role mandatory, or turn absent/malformed/stale advisory eval output into iteration
  failure, a respawn, or a cap. A session with no eval role behaves exactly as today.
- **INV2 — Workflow-sets are user-owned; the engine enforces *declared* contracts, never
  hard-coded paths.** No new path literal (`project_state/eval_results.md`, `eval_request.md`) in
  engine core; currency targets are read from a typed contract declaration.
- **INV3 — Detection with accountable repair, never prevention/semantic gates (D8),** using only
  D8's explicit structural carve-out (schema, identity, current-attempt ownership, hashes,
  stale-input protection).
- **INV4 — Compact artifacts carry continuity (D12);** raw-trace reading is a bounded fallback.
- **INV5 — Minimal engine:** no convergence heuristic, loop budget, structured-verdict, or
  merge-when-approved rule in the engine; behavioral guidance lives in the replaceable prompt.

*(An earlier draft carried an INV6 "frozen contracts stay on old behavior" and a `schema_version`
1→2 discriminator. **The operator has waived backward compatibility** (2026-07-22): already-live
sessions need not keep the old behavior. The discriminator is dropped — new behavior simply applies
to any v3 contract that **declares** the feature via `currency_outputs`. Old contracts that predate
the field lack it and get nothing; that is a natural consequence of the declaration, not a
compat guarantee we maintain.)*

---

## 0. The typed currency declaration (foundational, no version gate)

Add a **typed, validated `currency_outputs`** list to `WorkflowSetContract` (rather than reading
the untyped `state: list[dict]` or hard-coding paths — INV2). It is the sole trigger for the new
behavior; there is no `schema_version` gate.

```yaml
currency_outputs:
  - path: project_state/handoff.json
    owner_role: outer
    kind: handoff        # structural continuity: completion-gated + repairable (Change A)
  - path: project_state/eval_results.md
    owner_role: eval_runner
    kind: advisory        # optional evidence: engine emits a diagnostic only, never fails (Change B)
```

The engine reads `path`/`owner_role`/`kind` from here; **no path literal lives in engine core**
(INV2). `kind` selects the disposition: `handoff` → structural enforcement (Change A);
`advisory` → observability diagnostic only (Change B). A contract with no `currency_outputs`
enforces nothing new.

**Strict contract validation (added, since compat is waived — we can be strict):** on contract
load the engine validates `currency_outputs` — each `path` is repo-relative and confined (no
absolute/`..`), paths are unique, `owner_role` names a declared role, and — critically — a
`kind: handoff` entry **must** name the canonical protocol handoff identity: `path ==
project_state/handoff.json` and `owner_role == orchestration.handoff_owner`. This closes the
"BYO contract splits observation vs currency" gap (Grok r2 #3 / Codex r2 #5): `kind: handoff` is an
opt-in *marker on the engine's canonical handoff*, whose identity is fixed by protocol — only the
`advisory` path is genuinely user-chosen. `orchestration` is required for a `kind: handoff` entry.

---

## Change A — Deterministic handoff currency (engine) → D13

### Current behavior (verified)

`_record_finished_task` runs, for v3, `_accept_current_eval_receipts` then `_observe_layer_handoff`
(coordinator_app.py ~1490–1500), **then** `_apply_session_control` (~1541), which can set
`goal_met` (~5405). `_observe_layer_handoff` is non-gating, and `_handoff_producer_is_known`
(5764–5783) accepts the handoff when its producer is the current attempt **or any past attempt in
the active session's history** — so an un-re-stamped handoff reads `valid`, and an untouched file's
unchanged digest+revision never trips `non_monotonic`. Terminal control validation
(`_validate_control_handoff_reference`, 5559–5577) checks the cited handoff matches the *accepted
snapshot* — but a stale handoff **is** the accepted snapshot, so it passes. Net: **completion can be
declared on a stale handoff, and a successor can read one.**

### The rule

For a `kind: handoff` currency output whose `owner_role` is the finishing attempt's role, **and
only when `request.success` is true** (a crashed attempt must not archive or fail against the prior
valid handoff — INV3/robustness), the handoff at `path` must exist, be schema-valid, name this
session/goal, and carry `producer == {workflow_id, attempt_id}` of the finishing attempt. A
non-owner finish (inner, eval roles) requires no re-stamp and keeps today's observation semantics.

**Two dispositions, by whether this finish declares completion:**

**(A1) Completion path — hard, via the proven repair machinery.** This is the correctness
guarantee. In `_validate_v3_control`, when a contract declares a `kind: handoff` currency output
and the signal is terminal `goal_met`, add a currency reason **independent of whether `handoff_ref`
is cited**: the `accepted_handoff_snapshot` must exist and its `handoff.producer` must equal the
**completing attempt**. Because `handoff_ref` is optional today (`models.py:695`) and
`_validate_control_handoff_reference` runs only when it is present (`coordinator_app.py:5472`), a
citation-only check would be bypassed by an uncited `goal_met` (Grok/Codex r2 blocker). So the
completion currency check does **not** depend on citation; additionally, a `kind: handoff` contract
**requires** `goal_met` to cite the handoff (a missing `handoff_ref` is itself a reason), so the
two checks compose. Any reason → the existing `_reject_v2_control` flow (archive `control.json`,
`protocol_failures` record, `engine_repair` placeholder, control counter, re-dispatch the owner).
Structural (INV3): a terminal transition must be backed by a handoff the same attempt brought
current — not a judgment of work quality.

*Cap accounting (honest).* A1 rides the **existing** control-rejection path, which flips
`success=False` and therefore ticks both `control_protocol_consecutive_failures` **and** the general
workflow-failure cap (`_track_workflow_failure_cap`, coordinator_app.py ~1574). This is pre-existing
control-reject behavior, not introduced here; A1 is **not** a single-counter disposition. We accept
and document it (a completion repeatedly citing stale continuity is a real repeated failure worth
counting); a future refinement could exempt control-protocol failures from the general cap
symmetrically to A2, but that is out of scope.

**(A2) Non-completion owner finish — soft continuity hygiene, purely observability.** A `success`
owner finish that leaves the handoff un-re-stamped emits a `handoff_stale` diagnostic and writes a
`protocol_failures` repair note surfaced in the owner's next assignment. It does **nothing else**:
it does **not** flip `success`, touch any cap, or archive/overwrite `handoff.json` (the continuity
artifact is left intact). The standing owner (`run_every: 1`) re-runs on its own cadence and reads
the repair note; the correctness gate is A1, so an un-repaired stale handoff can never leak into a
completion regardless. A2 is deliberately *not* a hard failure: making it one would require a repair
scheduling primitive that forces the owner to run before successors (the stock scheduler unlocks
`inner`, not an immediate owner repair — Codex r2), adding engine surface against INV5 for a case A1
already protects. (An optional lenient `handoff_protocol_consecutive_failures` counter/cap could
catch a *permanently* broken owner, but is not required; if added it is exempt from the general cap.)

This tiering is deliberate: correctness (don't finish on stale state) is hard-enforced through the
proven control path; continuity hygiene is a visible nudge with no failure semantics, and the
successor-protection it aims at is really delivered by the **D15 reconcile-against-live-state
prompt** — the honest load-bearing fix.

### Same-revision re-stamp — exact algorithm (both reviewers: top blocker)

A pure re-stamp changes `producer`/`updated_at`, hence the digest, so today it is misflagged
`non_monotonic`. Relax **precisely**, preserving tamper detection. On observing a handoff whose
`revision == state.handoff_revision`:

```
accept as a legitimate provenance-only re-stamp  IFF
  handoff.producer == the finishing owner attempt {workflow_id, attempt_id}   AND
  handoff.updated_at >= accepted_snapshot.updated_at                          AND
  strip(handoff, {producer, updated_at}) == strip(accepted_snapshot.handoff, {producer, updated_at})
      (field-by-field equality of the parsed models with those two keys removed)
otherwise -> non_monotonic  (unchanged tamper detection)
```

A revision **increment** with `producer == current owner attempt` is always legitimate (material
change). A same-revision change to any field other than `producer`/`updated_at` stays
`non_monotonic`. This makes "re-stamp" expressible without opening a fixed-revision content-rewrite
hole.

### Finish ordering (Codex blocker)

In `_record_finished_task`, currency validation for the finishing role's `kind: handoff` output
runs **before** `_apply_session_control` consumes terminal control. A1 is enforced inside control
validation (so a stale-cited completion is rejected, not consumed); A2 (no terminal control this
finish) takes the soft path. Under no ordering can a stale handoff both fail currency and let
`goal_met` through.

---

## Change B — Located, fresh advisory eval output — **engine-diagnostic-only** → D14

Revised from the first draft, which made a missing/stale eval output fail+respawn+cap — that
violates INV1/D11 ("absent or malformed advisory eval is a visible diagnostic; it does not turn a
mechanically completed harness invocation into failure or starve orchestration"). The reliability
win comes from eval-banana writing the result correctly **by construction**, plus engine
an optional engine **diagnostic** on stale/missing evidence — never a gate.

### eval-banana side (see eval-banana `design/output-destination-capability.md`)

`eval-banana run` gains `--result-out <abs-path>` (there is no `--result-name`) writing a single
self-describing result document (a fenced, parseable `eval-banana-result v1` block + human body)
atomically to a caller-named path, with a provenance block eval-banana **observes** rather than
echoes: `commit_before`/`commit_after` (bracketing the run) and the **working-tree dirty digest**
observed at run start, recorded with its **algorithm name** (`loopy-git-status-diff-v1-sha256` is
loopy's algorithm; eval-banana names whatever it uses so a reader knows — cross-tool digest equality
is *not* assumed, see below); the run verdict; **per-check `model`** (only `model` is a per-check
override in eval-banana; `family` and `effort` are run-level harness config — Codex r2); tool
version; timestamp; and a **formal `status` envelope** (`completed | no_checks | config_error |
harness_error`) so a run that fails before a report exists still writes a stamped document.
`--provenance-attempt <id>` is the one caller-echoed field (opaque to the tool). This removes the
hand-transcription failure mode that put the M1 verdict off-channel. Full spec + schema:
`eval-banana/design/output-destination-capability.md`.

### loopy-loop side — declared, diagnostic-only, never fatal, no format coupling

1. **Declared, not hardcoded (INV2).** The `kind: advisory` `currency_outputs` entry names the eval
   result path + `owner_role`. The engine reads the path; no literal in core.
2. **A pure presence diagnostic, not a gate, and never opens the file (INV1 + minimalism).** There
   is **no** engine "accepted eval_results" seal — markdown results are agent-consumed, unlike
   sealed receipts. The engine **never reads or parses the result document at all**: it does not
   interpret the verdict, the git observations, or the provenance block, and does not compare git
   digests across tools. Its entire involvement is a single **presence diagnostic**: when the
   declared eval owner finishes `success` and the declared path is absent, it emits `eval_missing`.
   That is all — no `success` flip, no respawn, no cap, and deliberately **no engine staleness
   check** (that would require reading the file). The freshness *judgment* — observed commit vs live
   HEAD, dirty tree ⇒ re-run — is entirely the **reading agent's**, per the prompt (D14), from the
   provenance eval-banana wrote. This keeps the engine's eval involvement to a one-line
   `path.exists()`.
3. **`check_runner_roles` is left unchanged.** It governs receipt acceptance + citation authority
   (models.py 315; coordinator_app.py 5549, 5593); repurposing it would silently revoke outer's
   receipt authority. The advisory currency declaration is independent of it — no regression.
4. **Prompts wire paths and own the freshness decision.** `eval_runner` passes `--result-out
   <declared path> --provenance-attempt <this attempt>`; `outer`/`inner` read that path and **check
   the observed `commit_before/after` against live `HEAD` (and treat a dirty tree as needs-fresh-run)
   before relying on the verdict** (D14 independence — a verdict whose subject ≠ current HEAD is
   stale and is re-run or set aside, never obeyed). The engine does not adjudicate this.
5. **Remove the mandatory-final-eval sentence** from the stock `outer` prompt ("A terminal goal
   check is required before you declare the goal met") — it contradicts D11's explicit permission to
   decide evaluation is unnecessary (Codex). Replace with: a terminal eval is available and
   recommended near completion, but the orchestrator may decide it is unnecessary.

### D14 decision content

`eval_results.md` is advisory. The orchestrator's acceptance/merge is driven by the task and **live**
repo/CI facts, **never gated by a previously-established eval check.** eval-banana's observed
provenance makes "previously-established" deterministically visible to the *reading agent*; the
engine's role is location-by-construction (eval-banana writes the right file) plus an optional
diagnostic. **All judgment stays with the orchestrator.**

---

## Change C — Orchestrator reconciles against ground truth (prompt + minimal engine) → D15

### Engine (minimal)

Add one read-only key to the orchestration role's `paths.json`: the session **raw root**
(`<session>/raw/`, folded layout; each iteration under `raw/<iter>_<workflow>/…`, per
`raw_dir_path`/`iteration_dir_name` in `sessions.py`). Add it in `assignments.py`'s attempt-paths
builder (the `is_v3` branch that populates `paths.json`), **gated to the orchestration completion
role only**. Non-orchestration roles are unaffected. The prompt names the real
`raw/<iter>_<workflow>/…` shape (not `raw/<iter>/…`).

### Prompt (stock, user-owned — the load-bearing convergence fix)

Extend the stock `outer` prompt with:

- **A path map of *declared* artifacts + the raw fallback.** Live repo + git/CI; the compact
  durable artifacts (`handoff.json`, `project_state/tasks/`, `finished.md`, `eval_results.md`, and
  `ledger.md` **only if it is a declared state path** — otherwise drop it, do not name undeclared
  files); and, as a bounded fallback, **specific relevant prior iterations** selected from history
  under the raw root — not a tree scan (Codex).
- **Reconcile-before-deciding.** The handoff is a summary, not authority. Before selecting the next
  task or declaring completion, reconcile it against live repo/CI. When it is missing, ambiguous, or
  **contradicts** the repo (says "merge M1" but the PR is merged; an approval exists the summary
  omits), trust live state and, if needed, read the selected raw traces — do not re-derive work from
  a stale summary. (D12-consistent: routine continuity still rides compact artifacts; trace-reading
  is the violated-contract safety net.)
- **Anti-over-polish (prompt-owned, INV5/D8/D11).** When the selected work is delivered, checks are
  green, and an advisory review approves, accept and advance; residual non-blocking items are new or
  deferred tasks, not a reopen. Explicitly **not** an engine rule.

The `inner` prompt gets the "check the eval result's provenance against the observed commit before
relying on it" note (reinforces D14).

---

## What deliberately stays out of the engine

No convergence heuristic, loop budget, structured-verdict, merge-when-approved rule, mandatory eval,
or engine interpretation of "commit ≠ HEAD ⇒ must re-run." The engine gains only: a contract-declared
handoff-currency check (hard at completion, diagnostic per-iteration), an optional eval-provenance
diagnostic (no parse, no gate, no store), and one read-only discoverability path. Everything
behavioral is prompt-level and user-replaceable.

## Failure, recovery, adoption (backward compatibility waived)

The operator has waived backward compatibility, so there is **no frozen discriminator and no
retrofit story**: the new behavior applies to any contract that declares `currency_outputs`, and a
contract without it is simply unaffected. Notes that remain:

- **Caps.** Completion staleness (A1) rides the existing control-rejection path and ticks its
  existing counters (control + general), documented above — not a new death-spiral, and A2 has **no**
  failure semantics at all (pure diagnostic), so no cap interaction.
- **`request.success` gating.** Currency runs only after a mechanically successful attempt; a crashed
  owner attempt leaves the prior handoff and records the real failure (Codex).
- **Completion ordering.** A1 is evaluated inside `_validate_v3_control` (before `goal_met` is
  consumed) and does **not** depend on `handoff_ref` being cited, so an uncited `goal_met` cannot
  bypass it.
- **eval-banana version.** The new eval-banana (`--result-out`) is **required** — no hand-write
  fallback (compat waived). loopy-loop documents the minimum eval-banana version; the prompts assume
  it. If the file is absent/unstamped the engine emits a diagnostic only (never a failure), so even a
  misconfiguration degrades to "unverified," not a thrash loop.
- **Trace I/O failure** stays non-gating (D12).

## Test plan (contract/behavior)

1. Contract **without** `currency_outputs` → zero new behavior (handoff + eval identical to today).
2. Owner completion (`goal_met`) whose handoff was **not** re-stamped this attempt → control rejected
   via `_reject_v2_control`, owner re-dispatched; re-stamped completion accepted. **Covers both a
   cited `handoff_ref` and an omitted one** — both rejected.
3. Non-completion owner finish leaving handoff stale → `handoff_stale` diagnostic + repair note,
   `success` unchanged, **no cap touched**, `handoff.json` bytes intact.
4. Same-revision re-stamp changing only `producer`/`updated_at` by the current owner → **accepted**;
   same-revision change to any other field → **`non_monotonic`**.
5. Non-owner finish with an owner-stamped (past-attempt) handoff → unchanged `valid`.
6. Crashed owner attempt (`success=False`) → no currency failure, real error recorded.
7. Eval owner finishes with the declared result path absent → `eval_missing` **presence diagnostic
   only, iteration still succeeds**, no respawn, no cap (INV1); a present-but-stale result triggers
   **no engine event** (the engine never reads it — freshness is the agent's job).
8. Contract validation: a `kind: handoff` entry whose path ≠ `project_state/handoff.json` or whose
   `owner_role` ≠ `orchestration.handoff_owner` → contract load rejected; duplicate/absolute/`..`
   paths rejected.
9. Orchestration role's `paths.json` includes the raw root at `raw/<iter>_<workflow>/…`; other roles
   unchanged.
10. eval-banana: `--result-out` written atomically with observed commit(s)+dirty digest(+algorithm)
    and per-check `model`; each `status` value still writes a stamped document; symlink refusal; pure
    back-compat when `--result-out` absent.

## Phased adoption

1. eval-banana `--result-out` capability (observed git, per-check `model`, status envelope, fenced
   result schema) + design doc + released min version.
2. loopy-loop schema: typed, **validated** `currency_outputs` on `WorkflowSetContract` (path
   confinement/uniqueness; `kind: handoff` ≡ canonical handoff identity). No `schema_version` gate.
3. loopy-loop engine: A1 (`_validate_v3_control` completion currency, citation-independent) + A2
   (per-iteration `handoff_stale` diagnostic), the exact same-revision relaxation, `request.success`
   gating, tests. Update the `models.py` "handoff observation never gates" docstring.
4. loopy-loop engine: optional eval-provenance diagnostic driven by the `advisory` entry; raw-root
   `paths.json` key gated to the orchestration role.
5. Stock `inner_outer_eval`: add `currency_outputs`; outer prompt path-map + reconcile +
   anti-over-polish + **remove mandatory-final-eval**; eval_runner `--result-out`; inner/outer
   provenance-check note.
6. D13–D15 recorded; companion-doc links updated.

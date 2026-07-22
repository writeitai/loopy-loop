# Analysis — why an `inner_outer_eval` layer stalled after the work was done

*Working notes, 2026-07-22. Non-binding (`design/analysis/`). The conclusions that
survive review are recorded as **D13–D15** in `design/decisions.md` and specified in
`design/designs/deterministic-handoff-and-eval-provenance.md`.*

## The incident (worked example)

A real Phase-0 build loop (`inner_outer_eval`, session
`001_goal-implement-programming-phase-0`, delivery child
`01_m1-integrated-auth-member-invitations`) delivered milestone **M1**: `inner`
implemented it and merged **PR #38**. A follow-up corrective, **PR #39**, was then opened,
went **green and mergeable**, and the loop's own pre-merge review wrote an explicit
**"APPROVE FOR MERGE — merge PR #39 at head `4cc2221`; residual medium items are not a
reopen of the original blockers"** verdict. PR #39 was never merged. The outer coordinator
instead spent hours re-polishing non-blocking items and never converged. `inner` behaved
correctly throughout; the failure was entirely in `outer`'s decision loop.

Three independent substrate weaknesses combined to produce it. None is a bug in a single
function — each is a *missing* determinism guarantee that let a smart agent be misled by
its own durable state.

## Root cause 1 — a schema-valid handoff can be silently stale

`project_state/handoff.json` (owner: `outer`) is the rolling semantic summary a later
attempt trusts. At the stall it was **schema-valid but frozen** — `updated_at` hours old,
`accepted_outcomes: []`, `open_work` still naming "merge M1" — while the repository had
moved on (PR #38 merged; #39 open and approved).

The engine already observes the handoff on every v3 finish
(`_observe_layer_handoff`, `coordinator_app.py`) and even checks producer identity — but
the check is too weak. `_handoff_producer_is_known` (5764–5783) accepts the handoff when its
producer is the current attempt **or any matching entry in the active session's history**
(the entry need not even have *succeeded*):

```python
if (active.session_id == state.active_session_id
        and producer.workflow_id == active.workflow_id
        and producer.attempt_id == active.attempt_id):
    return True
return any(
    entry.session_id == state.active_session_id
    and entry.workflow_id == producer.workflow_id
    and entry.attempt_id == producer.attempt_id
    for entry in state.history
)
```

A prior owner-role check (5715–5726) already limits this to the declared handoff owner, so the
remaining hole is purely *currency*: a handoff last stamped by a *prior* owner attempt passes as
`valid`. The revision-monotonicity check doesn't catch it either — an untouched file has the same
digest and revision, so "revision moved backward" never triggers. And terminal-control validation
(`_validate_control_handoff_reference`, 5559–5577) only checks the cited handoff matches the
*accepted snapshot* — but a stale handoff **is** the accepted snapshot, so completion citing it
passes. The engine had no way to know the current handoff-owner attempt had **not re-stamped** the
file — because nothing required it to. Staleness was undetectable by construction, at both the
next-attempt-reads-it and the declare-completion boundaries.

## Root cause 2 — the eval verdict was written off-channel, and freshness was invisible

The `eval_runner` prompt tells the agent to run `eval-banana`, read the resulting
`report.md`, and then **hand-transcribe** the verdict into `project_state/eval_results.md`.
Two failure modes follow from "the agent hand-copies a verdict into a path it types by
hand":

- **Wrong destination.** In the incident the decisive approval landed in
  `project_state/evidence/m1-corrective-review.md`, not the canonical
  `project_state/eval_results.md` the outer prompt tells `outer` to read. `outer` read the
  canonical (empty/stale) file, never saw the approval, and had no signal that a verdict
  existed elsewhere.
- **No freshness signal.** `eval_results.md` carries no machine-readable provenance, so even
  when it *is* present, a reader cannot tell whether a "PASS" reflects the current commit or
  a verdict from three commits ago. The engine's expected-outputs advertisement compounds
  this: the check-runner role advertises only `project_state/eval_state.md`, **never
  `eval_results.md`** (see the comment at `_build_workflow_roster`), so the file the whole
  role exists to produce is not a declared, enforceable output at all.

The engine's decision here was deliberate and correct at the policy level — evaluation is
optional advisory evidence (**D11**), and the engine must not make a passing eval a gate.
But "advisory" was conflated with "un-located and un-versioned." Advisory evidence can still
be required to *land in a known place with known provenance* when the role runs; that is a
structural-integrity property (D8), not a semantic gate.

## Root cause 3 — `outer` was never told where the ground truth is

The outer prompt says "review inner's attempt on its merits — the diff, the repository
state, checks, and git/CI facts" and "read their verdict in `project_state/eval_results.md`."
It never tells `outer`:

- that the raw per-iteration harness traces exist and where (`<session>/raw/<iter>/…`), or
- what to do when the durable summary is missing, ambiguous, or **contradicts the live
  repository** — namely reconcile against live git/CI state rather than trust the summary.

`paths.json` hands `outer` its own attempt's `trace_root`, but exposes **no** path to the
sibling/prior iterations' raw traces, so even a diligent `outer` had nothing to open. When
root cause 1 and 2 fed it a stale handoff and an empty results file, `outer` had no
instructed fallback, so it re-derived a to-do list from stale state and looped.

This must be reconciled with **D12**: continuity should *not* routinely depend on re-reading
raw traces; the coordinator promotes conclusions into compact artifacts. The fix is not
"read traces every time" — it is "when the compact artifacts are missing or contradict the
repo, reconcile against live state and, as a bounded fallback, the traces." That is a safety
net for exactly the case where the compact-artifact contract was violated.

## What is *not* the problem (rejected fixes)

- **A "merge when approved + green" engine rule.** Rejected. That is a semantic completion
  gate — precisely what D8/D11 forbid the engine from owning. The anti-over-polish guidance
  belongs in the (user-owned, replaceable) workflow-set prompt, not the engine.
- **Making evals a mandatory gate.** Rejected — this is the exact D11 overcorrection already
  reverted once. Evals stay optional and advisory.
- **A convergence/loop-budget heuristic in the engine.** Rejected. The bet is: fix the
  deterministic substrate (provenance, currency, discoverability), keep the engine minimal,
  and trust capable agents plus user-owned prompts.

## The three determinism gaps, stated as requirements

1. **Handoff currency is deterministic.** A successful handoff-owner attempt must re-stamp the
   handoff with its own producer identity. A terminal completion citing an un-re-stamped handoff
   is hard-rejected via the existing control-repair path; a non-completion owner finish that
   leaves it stale is a soft, lenient, non-fatal nudge (diagnostic + repair note) — never a
   death-spiral. Frozen-gated on a contract discriminator so live sessions are untouched. Eval
   outputs carry producer identity so a past-attempt verdict is visibly stale. → **D13**.
2. **Advisory eval output is located by construction and freshness-visible — diagnostic, not
   enforced.** eval-banana writes the machine-readable result to a caller-named destination with
   *observed* provenance (commit + dirty digest, per-check `model`); the workflow-set contract
   declares that destination; the engine emits at most a **presence diagnostic** (`eval_missing`)
   and **never** reads the file, fails, respawns, or caps — the reading agent judges freshness
   (D11 — evals stay optional/advisory). → **D14**, split loopy-loop ⇄ eval-banana.
3. **The orchestrator reconciles against ground truth.** The engine exposes the session raw root
   to the orchestration role; the stock prompt gives `outer` the path map and the instruction to
   reconcile the handoff against live repo/CI state (and, as a fallback, specific selected prior
   traces) when the durable summary is missing or contradicts the repo. → **D15**.

All three are structural-integrity / discoverability changes (D8-compatible), plus user-owned
prompt guidance; none adds a semantic gate to the engine. **Honest causality:** #1 and #2 stop the
orchestrator from being *misled* by stale durable state, but the behavior that actually converges
the M1 case — accept delivered/green/approved work, reconcile against the live repo — is the **#3
prompt**. The substrate fixes are necessary and make the prompt's job reliable; they are not, by
themselves, a stop condition.

# Antigravity review: orchestrator-owned completion design

**Reviewer:** Antigravity AI Coding Assistant

**Initial review:** 2026-07-17

**Final re-review:** 2026-07-17

**Scope:** D3/D4/D6/D8–D12 amendment, protocol-v3 compatibility,
standalone/nested role ownership, semantic state/handoff, absolute paths,
optional eval provenance, and cross-harness collaboration

**Status:** historical design review; the binding designs and decision log are
canonical

## Final verdict

**PASS.** The final re-review found no unresolved blocker or correction. It
confirmed orchestrator-owned completion, optional eval evidence, exact v1/v2/v3
migration, standalone outer plan ownership, phase/milestone PM dispatch, upward
handoff/fallback, absolute path context, concrete schedule/capability rosters,
standardized strength tiers, non-enforced cross-harness review guidance,
trace-independent accepted receipts, and unchanged D5 autonomy.

## Initial findings and disposition

| Finding | Final disposition |
| --- | --- |
| Child immutable inputs had no named absolute Assignment path. | Added `layer_inputs` plus stable optional parent/request keys. |
| The control example made `handoff_ref` look mandatory despite missing handoff being diagnostic. | Made handoff/eval/evidence refs explicitly optional and defined terminal fallback/completeness states. |
| Receipt validation could remain hard-coded to `eval_runner`. | Required validation against frozen `check_runner_roles`, including outer/planner direct coordination. |
| Terminal outcomes could lose delivery evidence from earlier attempts. | Required session-wide delivery resolution with original attempt provenance. |
| Protocol v3 lacked explicit worker capability negotiation. | Added named v3 capabilities and HTTP 426 fail-fast before mutation/dispatch. |

The review also suggested making malformed handoff non-fatal, naming parent
context keys, and showing rejected-control/protocol-failure directories; those
changes were incorporated.

## Post-review tier amendment

During the review, Antigravity suggested automatically mapping a custom
`strong` tier to `frontier`. That suggestion was not adopted: configuration is
still never silently rewritten. After the PASS, the owner clarified that
`strong` is not a custom compatibility alias but one of four canonical strength
tiers. The binding design now uses `frontier`, `strong`, `standard`, and
`economy`; an illustrative Anthropic mapping is Fable-, Opus-, Sonnet-, and
Haiku-class respectively. Sparse or provider-specific configuration remains
visible as configured rather than being reinterpreted.

## Verdict boundary

This PASS covers the binding documentation only. It does not approve the
future protocol-v3 implementation or coordinated loopy-loop/team-harness
release.

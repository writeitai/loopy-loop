# Claude Code review: orchestrator-owned completion design

**Reviewer:** Claude Code (`claude-opus-4-8`, `xhigh`)

**Initial review:** 2026-07-17

**Final re-review:** 2026-07-17

**Scope:** current code anchors and the binding D3/D4/D6/D8–D12,
protocol-v3, state/handoff, schedule/capability, prompt, and compatibility
amendments

**Status:** historical design review; the binding designs and decision log are
canonical

## Final verdict

**PASS.** Claude found the architecture coherent and confirmed all requested
properties against the settled documents and current code anchors. The final
pass reported no blocking architecture issue.

## Findings that shaped the final design

| Finding | Final disposition |
| --- | --- |
| The older fast-path proposal asserted the eval-owned D11 invariant that the amendment replaces. | Marked the proposal superseded and historical without deleting its analysis. |
| Outer appeared able to author, run, and cite its own checks without explaining D4 independence. | Clarified that role identity is durable accountability; delegate/judge provenance and cross-family review provide independence as guidance, not an engine role-name gate. |
| Existing `session_protocol_version >= 2` branches would accidentally route v3 into v2 eval-gate validation. | Required exact version dispatch and a test that v3 never enters v2-only validation. |
| Inner still had a prompt path to create the layer plan; flat state accountability could not express owner versus contributor. | Required removing inner plan bootstrap and adding per-artifact owner/contributor metadata. |
| The fate of `eval_readiness/` was unspecified. | Retired it for v3 into `eval_state.md` plus scheduler context while preserving frozen v2 readers. |
| Durable plan state referred to an absolute task path. | Corrected it to a portable logical task ref resolved to an absolute attempt path. |
| The D5 blocker field was accidentally renamed and PM blocker roles were omitted. | Retained `terminal_blocker_reporting_roles` and declared `[planner, dispatcher]` for PM. |
| A contract file omitting a protocol version was an undocumented middle migration case. | Permanently pinned that compatibility default to v2; only explicit v3 opts in. |
| One recursive-design sentence could imply non-empty evidence refs. | Made evidence refs optional/empty everywhere; only asserted refs are validated. |

Additional accepted improvements include a single accountable writer for
`eval_state.md`, monotonic handoff revisions as diagnostic structure, complete
canonical tier examples, explicit v1/v2 singular versus v3 plural eval receipt
fields, session-wide delivery lineage, and accepted-receipt validation that
does not depend on later retention of raw trace bytes.

## Verdict boundary

This PASS covers the revised architecture documents. It does not certify the
future implementation, migrations, tests, releases, or the Ultimate Memory
workflow upgrade.

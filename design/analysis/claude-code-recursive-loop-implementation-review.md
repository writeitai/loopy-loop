# Claude Code implementation review: recursive loop contract

**Reviewer:** Claude Code, Opus 4.8
**Date:** 2026-07-16
**Reviewed repositories:** loopy-loop 0.7.0, team-harness 0.5.0, and eval-banana
0.3.2 feature branches
**Scope:** adversarial review of uncommitted diffs against each repository's main
branch and conformance with D1-D12
**Status:** **historical moving-tree review; a new final review is pending**

This file does not certify the current implementation. The reviewer observed active
edits throughout the run, test results changed repeatedly, and at least one finding was
fixed while it was being written. Subsequent scope cleanup also removed the generic
credential-redaction pipeline and local trace export/prune subsystem. Findings about
those deleted mechanisms are retained only in the superseded section below.

## Historical verdict

**Not ready to merge at the observed snapshot.** Claude considered the recursive
architecture sound and several difficult mechanisms correct, but could not issue a
final verdict against a moving, red tree. Its release verdict was conditional on:

1. quiescing all three worktrees;
2. fixing or adjudicating the live blockers;
3. publishing/otherwise resolving the required support-package versions and lockfile;
4. running formatting, lint, types, and full tests in all three repositories; and
5. repeating the adversarial review against that exact settled tree.

Those conditions must be checked afresh after the current scope reduction. This note
does not claim which historical findings remain live.

## Severity-ordered findings at the reviewed snapshot

### Release blockers

| Finding | Observed consequence | Recommended disposition |
| --- | --- | --- |
| The loopy dependency floors referenced unpublished team-harness 0.5.0 and eval-banana 0.3.2 while `uv.lock` still described older published releases. | CI dependency resolution failed before tests. Local editable siblings hid the problem. | Merge/publish support packages first (or use an atomic supported mechanism), regenerate/check the lock, then test the install path CI uses. |
| The branch changed throughout review and its loopy suite was red at every final observation. | File hashes and line numbers became stale; no review result represented a release candidate. | Freeze the tree and rerun every gate and reviewer. |

### High

| ID | Finding at that snapshot | Contract concern |
| --- | --- | --- |
| H1 | An untracked nested git repository appeared as a trailing-slash directory and caused pre-harness git evidence validation to fail forever. | D8 requires a visible repair path; the agent never ran to repair it. |
| H2 | The v2 control-protocol failure counter was reset by the engine's own running placeholder. | Repeated malformed control could avoid the intended bound. |
| H3 | A historical passing eval receipt could close a later git state; producer role was self-declared. | Layer completion was not bound tightly enough to the state being closed. |
| H4 | v2 control validation re-read a missing workflow contract through a non-fallback reader. | A legacy parent could wedge `/finished` with HTTP 500, including D5 blocker control. |
| H5 | No real integration test exercised capability parity and caller-owned `run.json` across loopy and team-harness. | A support-package rename could silently make usage unknown and disable budget stopping. |
| H6 | Re-entrant `FileLock` acquisition was reachable in child budget accounting and torn-transition recovery. | A child could repeatedly time out instead of advancing. |
| H7 | Dirty submodule content could keep the same digest because the implementation hashed only the directory fact. | Git evidence overstated what it bound. |
| H8 | Frozen child inputs were revalidated before every attempt, but drift caused a pre-harness permanent failure. | Detection became a preventive fence with no agent-visible repair path. |

### Medium

| ID | Finding at that snapshot | Recommendation |
| --- | --- | --- |
| M1 | Crash-abandoned attempts remained active rather than being finalized incomplete. | Finalize abandonment honestly without inventing missing output. |
| M2 | Eval-channel completeness was inferred from directory non-emptiness. | Mark provider/channel availability explicitly; do not guess. |
| M3 | Malformed eval-readiness JSON failed before the harness on every role. | Surface it as repairable context instead of bricking the session. |
| M4 | One corrupt unrelated session could break reference resolution for a healthy tree. | Scope resolution to the validated root/session topology. |
| M5 | The negotiated completion fence was stored only in process memory and could downgrade after restart. | Persist the negotiated protocol/fence with durable assignment state. |
| M6 | Loopy pinned an eval model different from eval-banana's newer default. | Make the choice deliberate and auditable; do not rely on ambient defaults. |

### Low suggestions

- Verify every frozen snapshot field that is claimed to be binding, or remove unused
  hashes.
- Attribute protocol failures to the coordinator's current task rather than only to
  an untrusted producer field.
- Define whether an eval receipt must cover every authored check or an explicit subset.
- Keep design text synchronized with the actually observable `/finished` exchange.

## Mechanisms the review found sound

These positive findings are historical but useful regression targets:

- traversal-safe logical reference resolution and symlink containment;
- frozen workflow definitions verified before harness execution;
- explicit absolute assignment paths and parent assignment propagation;
- current-attempt and pending-result fencing by `attempt_id`;
- persistence-layer rejection of task-plus-child and terminal-plus-inflight states;
- bounded corrupt-child-ledger refusal rather than silently rebuilding an empty ledger;
- iterative multi-level unwind and process recovery planned outside the transition
  lock in the paths the reviewer checked;
- mechanical iteration success semantics (D3), one loopy worker (D2), stock
  LLM-as-judge authoring (D4), no semantic scheduler gate (D8), uniform model tiers
  (D9), and layer-local goal control (D11);
- eval receipt checks for identity, goal, git subject, artifact hashes, report pass
  threshold, check IDs, and judge identity.

These checks should be preserved where the simplified implementation still claims the
same behavior. They are not proof that the current code still has it.

## Superseded findings and removed scope

The reviewed branch contained a broad credential detector, capture-time stream
redaction, trace export/outbox, pruning, and associated seal policies. That subsystem
was later removed as unrelated to the core recursive contract.

Accordingly, the following historical review material is **not a current finding**:

- extension-sensitive redaction failures and signed/CLI credential patterns;
- whether abandoned traces could be sanitized, exported, or pruned;
- export drift refusal, export outbox idempotency, and prune retention;
- claims that a sealed trace is sanitized or safe to upload;
- permission-hardening recommendations tied to redacted export artifacts.

The retained scope is raw, gitignored local observable capture and whatever integrity
boundary the current binding design still specifies. Export, pruning, cloud transport,
and generic credential spotting require a separate future design and review.

## Recommendations carried forward

1. Review only a quiescent tree and record the exact revisions of all three repos.
2. Exercise the real cross-repository caller contract, not hand-written capability and
   `run.json` fixtures alone.
3. Ensure every pre-harness validation has an autonomous, agent-visible repair path or
   is limited to genuine identity/integrity failure.
4. Bind `goal_met` to the current frozen workflow contract, current attempt, and current
   evaluated git subject.
5. Persist negotiated protocol state across coordinator restart.
6. Validate nested git/submodule behavior according to the documented digest limits;
   do not overstate Git evidence as a complete security boundary.
7. Run the support-package release/lock sequence before declaring the loopy PR ready.

## New final review required

No final post-cleanup Claude verdict is recorded here. Once the feature is scoped,
green, committed, and dependency-installable, rerun Claude Code against those exact
revisions and replace this pending status only with the reviewer’s actual result.

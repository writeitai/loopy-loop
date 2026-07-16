# Concurrency, Interface & Contract Audit: Recursive Loop Layer Contract (D10-D12)
**Date:** July 15, 2026  
**Auditor:** Antigravity AI Coding Assistant  
**Repository Target:** [loopy-loop](../../README.md)  
**Status:** Completed Analysis  

This document presents a severity-ordered contract review of the proposed recursive loop binding architecture in [recursive-loop-layer-contract.md](../designs/recursive-loop-layer-contract.md) and Decisions **D10-D12** in [decisions.md](../decisions.md). The audit checks for cold-readability, factual accuracy, internal consistency, compatibility with **D1-D9**, logical versus absolute path resolution, recursive stack safety, layer-local eval boundaries, and mixed-version safety against current source code and dependencies.

---

## 1. Executive Verdict

The proposed recursive loop layer contract (**D10-D12**) represents a well-designed, robust generalization of the parent-child coordinator model. By mapping different loop depths (PM planner $\rightarrow$ Feature epic $\rightarrow$ Code delivery) onto the same recursive session node, it avoids duplicating scheduling and recovery logic. It properly respects the core decisions (**D1-D9**)—retaining files/git as the durable source of truth (**D1**), enforcing the single-worker model (**D2**), using LLM-as-judge checks without deterministic agent-authored gates (**D3**/**D4**), and maintaining full autonomy via `unresolvable_error` (**D5**).

However, the proposed binding contract and its planned execution trace plane contain two **blocking high-severity issues** and several medium-to-low issues that must be addressed before the implementation is finalized:
1. **A major locking bottleneck** in recursive stack recovery that will deadlock or block the entire coordinator service during process draining.
2. **A missing negotiation surface** in coordinator-worker registration that violates the mixed-version safety guarantees.

With the concrete fixes detailed below, no further blocking issues remain.

---

## 2. Severity-Ordered Findings

### Priority 1: High Severity

#### A. Stack Resumption Holds Transition Lock During Bounded Process Draining
* **Concept:** Recursive state invariants and coordinator concurrency.
* **Anchor:** [src/loopy_loop/coordinator_app.py#L213-L225](../../src/loopy_loop/coordinator_app.py#L213-L225), [src/loopy_loop/coordinator_app.py#L577-L582](../../src/loopy_loop/coordinator_app.py#L577-L582), and [src/loopy_loop/coordinator_app.py#L1173-L1195](../../src/loopy_loop/coordinator_app.py#L1173-L1195).
* **Context:** In `loopy-loop`'s two-phase recovery design, process draining and reaping (Phase A) are deliberately executed *outside* the reentrant `_transition_lock` in [_plan_orphan_recovery](../../src/loopy_loop/coordinator_app.py#L259) to keep CLI operations (`loopy status` and `loopy stop`) responsive. 
* **The Issue:** When a child session completes (or its recovery triggers a stop), [_resume_parent_if_active_child_completed](../../src/loopy_loop/coordinator_app.py#L1173) is called under the `_transition_lock` in both `register_worker` and `finish_assignment`. This method recursively invokes `self.register_worker()`. Because the transition lock is reentrant (`threading.RLock`), the recursive call is allowed, but it executes [_plan_orphan_recovery](../../src/loopy_loop/coordinator_app.py#L259) for the parent session *while the thread still holds the transition lock*.
* **Consequence:** If the parent session has an orphaned runner that requires draining, the coordinator will block inside the lock for up to `recovery_drain_timeout_s` (seconds or minutes). During this time, any concurrent coordinator request (such as status queries, stop requests, or other worker finished notifications) will block, violating the failure containment and responsiveness goals of **D7**.
* **Concrete Fix:** 
  1. Refactor `_resume_parent_if_active_child_completed()` to only modify states (finalize child, clear active child pointer, set `self.state_store = parent_store`) and return `True` (resumed parent) instead of recursively executing `register_worker()`.
  2. Modify `register_worker()` and `_finish_assignment_locked()` to check if a parent was resumed, release the lock, and loop/dispatch `register_worker()` on the newly active parent session *outside the lock*, permitting `_plan_orphan_recovery()` to run as a non-blocking process:
     ```python
     # Inside register_worker loop:
     while True:
         recovery = self._plan_orphan_recovery()  # Done outside the lock
         with self._transition_lock:
             response = self._register_attempt(caller=caller, recovery=recovery)
             if response is not None and response.action == STOP_ACTION:
                 if self._resume_parent_if_active_child_completed(caller=caller):
                     continue  # Lock released, plan recovery for parent next iteration
         if response is not None:
             return response
     ```

#### B. Missing Version/Capability Negotiation in RegisterRequest
* **Concept:** Migration and mixed-version safety.
* **Anchor:** [src/loopy_loop/models.py#L70-L91](../../src/loopy_loop/models.py#L70-L91), [src/loopy_loop/models.py#L93-L107](../../src/loopy_loop/models.py#L93-L107), and [recursive-loop-layer-contract.md#L941-L945](../../design/designs/recursive-loop-layer-contract.md#L941-L945).
* **Context:** The migration contract states: *"Coordinator/worker wire additions use explicit capability/version negotiation. `RootConfigSnapshot` currently rejects unknown fields, so a new coordinator must not unilaterally send assignment fields that make an older released worker crash."*
* **The Issue:** `RootConfigSnapshot` enforces `extra="forbid"` in `models.py`. However, [RegisterRequest](../../src/loopy_loop/models.py#L109) and [WorkerIdentity](../../src/loopy_loop/models.py#L93) do not carry any `version` or `capabilities` fields. 
* **Consequence:** A new coordinator has no mechanism to determine if the registering worker is an older version. It will unilaterally serialize newer config fields (such as `model_tiers` or `recovery_policy`) to the worker, causing Pydantic validation to fail and the worker to crash immediately on registration.
* **Concrete Fix:** Update `RegisterRequest` and `WorkerIdentity` schemas to carry a `version` string (e.g., `"0.6.0"`) and a `capabilities` list. The coordinator must validate these fields and filter the returned `TaskResponse.config_snapshot` to only include keys supported by that worker version.

---

### Priority 2: Medium Severity

#### A. Lack of Logical References for Non-Immediate Ancestors
* **Concept:** Absolute runtime paths versus portable references.
* **Anchor:** [recursive-loop-layer-contract.md#L292-L298](../../design/designs/recursive-loop-layer-contract.md#L292-L298).
* **Context:** The logical path scheme defines `session:/` (local), `parent:/` (immediate parent), and `child:<session_id>/...` (direct children) to support moving checkouts without breaking hardcoded paths.
* **The Issue:** In a three-depth (or deeper) session tree (e.g., program $\rightarrow$ feature $\rightarrow$ task), the deepest task delivery session may need to reference files or configurations owned by the grandparent (root program session). `parent:/` resolves only to the immediate parent (feature session), and `child:<session_id>/...` is scoped to descendants. There is no logical reference scheme to access non-immediate ancestors.
* **Consequence:** Grandchild sessions must either use fragile relative paths (e.g. `../../..`) or hardcoded absolute paths, breaking the portability invariant across host environments.
* **Concrete Fix:** Generalize the logical path resolver to support a `root:/` reference, or a generic `session:<session_id>/...` scheme that can resolve any session path within the active topology.

#### B. Local Trace Secret Hygiene
* **Concept:** Complete observable agent I/O and trace safety.
* **Anchor:** [recursive-loop-layer-contract.md#L773-L808](../../design/designs/recursive-loop-layer-contract.md#L773-L808).
* **Context:** The design requires capturing complete observable input/output channels, including direct-spawn arguments, environment variables, and process I/O. It specifies that cloud export is redacted.
* **The Issue:** Spawned processes (e.g., Codex or Claude CLI) receive API tokens (like `OPENAI_API_KEY`) and authentication path configurations through their environment. Storing these verbatim in local `.loopy_loop/traces/` files creates a security leak.
* **Consequence:** Any process or subagent executing inside the repository can read local trace directories and extract long-lived API keys.
* **Concrete Fix:** Add a local sanitization requirement: the worker and `team-harness` must filter out sensitive keys (e.g., redacting any environment variable containing `API_KEY`, `AUTH`, or `TOKEN` values) *before* writing the environment to local trace manifests, rather than postponing redaction to the cloud export phase.

---

### Priority 3: Low Severity

#### A. Discrepancies in Logical Path Notation (With/Without Slashes)
* **Concept:** Internal consistency.
* **Anchor:** [recursive-loop-layer-contract.md#L292-L298](../../design/designs/recursive-loop-layer-contract.md#L292-L298) and [recursive-loop-layer-contract.md#L404-L441](../../design/designs/recursive-loop-layer-contract.md#L404-L441).
* **Context:** Section "Identity and path contract" defines the portable logical formats using trailing slashes: `repo:/`, `session:/`, `parent:/`.
* **The Issue:** The concrete examples provided in `assignment.json` omit the trailing slash (e.g., `"goal_ref": "session:goal.md"` and `"git_before_ref": "session:git_receipts/..."`).
* **Consequence:** This discrepancy leads to parsing and validation ambiguities when agent models or parsers implement the logical path scheme.
* **Concrete Fix:** Standardize the contract documentation. Either require trailing slashes for all schemes (e.g. `session:/goal.md`) or define them consistently without (e.g. `session:goal.md`).

#### B. Ambiguity in "Dirty-Tree Digest" Calculations
* **Concept:** Git and PR evidence.
* **Anchor:** [recursive-loop-layer-contract.md#L743-L750](../../design/designs/recursive-loop-layer-contract.md#L743-L750).
* **Context:** The contract requires recording a `dirty-tree digest` at attempt boundaries.
* **The Issue:** The calculation method for this digest is unspecified. If the digest only hashes `git diff` output, it will omit untracked files created by the agent process.
* **Consequence:** Re-runs could execute in a state containing hidden untracked files, yielding non-reproducible outcomes despite a matching dirty-tree digest.
* **Concrete Fix:** Define `dirty-tree digest` to hash both:
  1. The output of `git diff HEAD`.
  2. The content of any untracked files returned by `git status --porcelain` (excluding `.loopy_loop/` and standard `.gitignore` patterns).

---

## 3. Verification & Dependency Alignment

### A. Team-Harness (runs/ vs output_dir/)
* **Claim:** `run.json` must be written under the attempt trace root or durably copied there.
* **Verification:** In installed team-harness 0.4.0, `run_dir` is hardcoded to `RUNS_DIR / run_id` (which resolves globally to `~/.team-harness/runs/`). The `output_dir` constructor argument only maps to `session_output_dir` (used for worker subprocess logs). 
* **Design Compatibility:** The recursive contract correctly identifies this limitation and calls for a new capabilites contract where `team-harness` writes `run.json` under a caller-supplied attempt trace root. This is correct, but the preflight capability checks must enforce this constraint.

### B. Worker CLI Scripts and PATH Insertion
* **Claim:** `ensure_interpreter_scripts_on_path` adds the correct directory to find `eval-banana`.
* **Verification:** As verified in `.venv/bin/python`, the package `eval-banana` installs two entry points: `eb` and `eval-banana`. The logic in `worker.py` that checks for `eval-banana` will successfully resolve the scripts directory and append it to `PATH`, allowing the workflows to execute `eval-banana` normally.

---

## 4. Conclusion

The proposed recursive loop layer contract (**D10-D12**) is architecturally sound and successfully resolves the ambiguities surrounding nested coordinators, traces, and execution scopes. 

Aside from the transition lock concurrency leak (Priority 1-A) and the missing registration version field (Priority 1-B), **no blocking issues remain** in the binding design or decisions. Implementing the fixes outlined in this review will ensure a high-performance, deadlock-free, and backward-compatible recursive execution model.

---

## Final re-review

Following the initial audit, the companion design document `design/designs/recursive-loop-layer-contract.md` and decision log `design/decisions.md` (specifically D10–D12) were updated to resolve the identified concerns. The updated design has been evaluated against the current repository and the installed virtual environment (`.venv`) dependencies (`team-harness: 0.4.0` and `eval-banana: 0.3.1`).

### Severity-Ordered Residual Verdict

**Verdict:** **No blocking design issues remain.** All previously identified high, medium, and low severity issues have been fully resolved by the updated design specifications.

Here is the status of the resolutions:

1. **Iterative Parent Unwind Outside Long Recovery Locks (Resolved High-Severity 1-A):**
   The updated design specifies refactoring the multi-level parent unwind into an iterative transition loop, explicitly requiring process drain/reap planning to run outside the transition lock. No recursive registration path is permitted to hold the lock during recovery.
2. **Version and Capability Negotiation (Resolved High-Severity 1-B):**
   The design now incorporates explicit capability and version negotiation for coordinator/worker registration. The new registration protocol carries `worker_protocol_version` and capability names, preventing the coordinator from sending newer configurations that older workers do not support.
3. **Portable Logical References including Non-Immediate Ancestors (Resolved Medium-Severity 2-A):**
   The logical reference grammar has been generalized to include implicit `root:/path` and named `session:<session_id>:/path` scopes. This allows grandchild or deep sessions to resolve references to non-immediate ancestors without hardcoding paths or relying on fragile relative paths.
4. **Capture-Time Local Secret Redaction (Resolved Medium-Severity 2-B):**
   The design specifies that secret filtering occurs locally prior to writing trace manifests. Values containing credentials (e.g. `*_TOKEN`, `*_KEY`) and commands are redacted at capture-time, mitigating security leaks inside local trace directories.
5. **Versioned Dirty-Tree Digests including Untracked Content (Resolved Low-Severity 3-B):**
   The dirty-tree digest now uses a versioned canonical algorithm that explicitly hashes the bytewise-sorted output of `git status --porcelain=v1 -z --untracked-files=all` (including file modes/type and digests for changed and untracked files), ensuring untracked content is tracked.
6. **Repairable Control-Schema Migration (Resolved):**
   The design includes a version-discriminated control reader accepting legacy v1 and new v2 schemas, routing malformed v2 records to a rejected-control archive with a retry/failure cap rather than converting them to semantic failure.
7. **D8-Safe Eval Cadence (Resolved):**
   Evaluation readiness is treated purely as prompt context and not as scheduler eligibility. Cadence is mechanically retuned to run eval within a small number of successful implementation attempts, preventing hard prevention fences.
8. **Terminal-Blocker Roles (Resolved):**
   The design allows any workflow role listed in `terminal_blocker_reporting_roles` to report a terminal blocker (`unresolvable_error`) with a detailed reason, rather than restricting it to the goal-control owner.
9. **Dual Child-Request Directory Migration (Resolved):**
   The coordinator reader is designed to scan both `child_requests/*.json` and `child_requests/pending/*.json` before changing the rendered writer path, ensuring a safe migration.

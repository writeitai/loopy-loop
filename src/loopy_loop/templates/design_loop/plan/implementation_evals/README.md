# Implementation evals

eval-banana checks (`eval_checks/*.yaml`) that bind design invariants to the FUTURE
implementation — acceptance criteria for code that does not exist yet. Written by
`design_harden` (implementation_eval_writer). Conventions (from the ugm exemplar):

1. Lead with the current design section as the binding source; cite decisions for
   rationale and refinement history.
2. Tags carry the primary D-numbers and one subsystem.
3. One coherent conjunction per check; split schema/runtime/surface/global-absence.
4. Target the final complete system: absent or partial subsystem = score 0.
5. Designs control over decision prose; note discrepancies instead of failing code that
   follows the current design.
6. Score 1 only if every condition demonstrably holds, citing implementation paths.

These are product artifacts of the design — distinct from any loop session's own
checks. Run them only against an implementation, scoped:
`eval-banana run --check-dir plan/implementation_evals/eval_checks`.

<!-- No checks yet. -->

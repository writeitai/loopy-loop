# Review of `a04a222` — `design_loop` template packaging

The clean built-wheel path works: a Hatchling wheel built from `a04a222` contained exactly the same 90 `design_loop` files as the source tree, including `.gitignore`, `.eval-banana/config.toml`, and the complete `.loopy_loop/` tree; both an extracted wheel install and direct zip-backed `importlib.resources` traversal scaffolded that exact path set, and every file decoded as UTF-8. Eval-banana 0.3.0 explicitly discovers a `--check-dir` below excluded `.loopy_loop`, gives deterministic checks the repo as `project_root`/cwd, inherits `LOOPY_SESSION_DIR`, and creates the requested session output directory; all 27 shipped YAMLs validate and no deterministic script retains copy-into-session/source-path derivation. The root config and all six graphs preflight, success-path scheduling produces the intended cycles, all five stage sets dispatch as children from the same scaffold, and the engine correctly ignores child-authored grandchild requests. The default and two older templates remain on their old paths. I found no blocking defect in that clean path, but the template still has integration and prompt robustness problems that should be fixed before calling it dependable in a real pre-existing target repo. The requested `design/reviews/2026-07-13_design_loop_sets_codex.md` was not present in this checkout, any local ref/reflog, or the surrounding workspace, so it could not be read.

## TR-1 — should-fix — Required root integrations are silently skipped when the target already has those files

**Evidence.** `_init_packaged_template` sends every packaged path through `_copy_template_file_if_missing` (`src/loopy_loop/cli.py:226-255`), and `_write_if_missing` silently returns when the destination exists (`src/loopy_loop/cli.py:526-531`). That policy is unsafe for three design-specific integration files. The shipped `.gitignore` requires `.loopy_loop/sessions/`, `.eval-banana/results/`, and `_additional_context/` (`src/loopy_loop/templates/design_loop/.gitignore:1-3`), but the post-init merger adds only `.loopy_loop/sessions/` (`src/loopy_loop/cli.py:108`, `src/loopy_loop/cli.py:534-545`). The eval config says its harness and discovery settings are required (`src/loopy_loop/templates/design_loop/.eval-banana/config.toml:1-5`, `src/loopy_loop/templates/design_loop/.eval-banana/config.toml:13-25`), while eval-banana refuses harness-judge checks when no harness is configured (`/Users/jpuc/.local/share/uv/tools/eval-banana/lib/python3.13/site-packages/eval_banana/runner.py:80-108`). Finally, the shipped `CLAUDE.md` says it must be merged into the target's file (`src/loopy_loop/templates/design_loop/CLAUDE.md:1`), but no merge exists. This contradicts the user-facing claim that init lays down the required eval config and design rules (`README.md:488-490`).

**Concrete failure scenario.** Run the command in a normal code repo that already has `.gitignore`, `CLAUDE.md`, and an older/custom `.eval-banana/config.toml`. Init reports success, but `_additional_context/` is not gitignored and can be committed with cloned research sources; the design-phase rules never reach agents; and a config without `[harness] agent` bricks every qualitative gate (or a non-1.0 threshold makes the console-level pass verdict inconsistent with the workflow's “every check” contract). There is no warning that the promised scaffold is incomplete semantically even though all workflow files exist.

**Specific fix.** Give `design_loop` a template-aware reconciliation step: idempotently append all three ignore entries; validate existing eval-banana config for `pass_threshold = 1.0`, a usable harness, and `.loopy_loop` exclusion, then merge missing settings or fail init with an exact repair message; and install the design rules under a collision-free in-repo path that every workflow reads, or idempotently merge a delimited section into an existing `CLAUDE.md`. Preserve unrelated user content—do not overwrite whole files.

## TR-2 — should-fix — Session lookup depends on an agent keeping a shell-local `export` alive

**Evidence.** Each goal-check prompt emits three separate-looking shell lines; for example the director uses `export LOOPY_SESSION_DIR="<Session directory>"` followed by validate and run (`src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_director/workflows/goal_check/prompt.txt:22-26`), and the bind and harden prompts repeat the pattern (`src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_bind/workflows/goal_check/prompt.txt:23-26`, `src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_harden/workflows/goal_check/prompt.txt:23-26`). Session-dependent deterministic checks fail closed when the variable is absent (`src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_shape/eval_checks/dl_shp_requirements_draft_only.yaml:10-20`). Eval-banana itself is correct: its deterministic `subprocess.run` supplies no replacement `env`, so it inherits the CLI process environment (`/Users/jpuc/.local/share/uv/tools/eval-banana/lib/python3.13/site-packages/eval_banana/runners/deterministic.py:93-105`). Loopy, however, only renders the absolute session path as prompt text (`src/loopy_loop/worker.py:381-405`) before handing the prompt to the harness (`src/loopy_loop/worker.py:221-240`); it does not establish `LOOPY_SESSION_DIR` for the evaluator command.

**Concrete failure scenario.** A coding agent executes `export ...` in one terminal-tool call and `eval-banana run ...` in a later call, or executes the placeholder literally instead of substituting the rendered path. Exports do not cross process boundaries, so every session-dependent check reports `LOOPY_SESSION_DIR not set` (or looks under a literal `<Session directory>`), the goal check emits false, and the stage burns repair turns on infrastructure rather than design work. A single-call probe passes, which confirms the mechanism but not this prompt-level guarantee.

**Specific fix.** Make the run command self-contained: `LOOPY_SESSION_DIR="<absolute rendered session path>" eval-banana run ...`, ideally by having `_render_prompt` provide an exact shell-quoted command/value rather than asking the agent to transcribe a label. `validate` does not execute scripts and needs no export. Apply the same form to all six goal-check prompts and add a worker-level rendered-prompt test asserting the real session path is in the command.

## TR-3 — should-fix — The shipped constitution is knowingly empty and points users outside the scaffold

**Evidence.** `CLAUDE.md` announces three non-negotiable rules, but Rule 3 contains only a project-specific fill-in comment (`src/loopy_loop/templates/design_loop/CLAUDE.md:8`, `src/loopy_loop/templates/design_loop/CLAUDE.md:25-31`). The setup documentation tells the user only to fill `design_goal.md` before running (`README.md:490`), while the bind workflow explicitly relies on the target's `CLAUDE.md` design rules (`src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_bind/workflows/designer/prompt.txt:14-15`). The director README also says rationale lives in a `writeitai/writeit-loops-and-standards` repository that is not part of the scaffold (`src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_director/README.md:7-9`) and describes nonexistent “later implementation sets” as part of the palette (`src/loopy_loop/templates/design_loop/.loopy_loop/workflow_sets/design_director/README.md:3-7`). No references to `design/decisions.md` or `standards/design_loop` remain, and the other initially absent `plan/analysis/...` paths are declared outputs of their owning stages rather than dangling inputs.

**Concrete failure scenario.** A user follows the documented setup exactly, fills only `design_goal.md`, and starts the loop. Agents are told that an empty heading is a binding constitution, while the designer is told to enforce it; a director or future maintainer may also search for an unavailable external rationale or choose an implementation set that init did not install. The run can continue, but its most product-specific boundary is neither stated nor mechanically required, so it can produce a coherent corpus for the wrong authority boundary.

**Specific fix.** Replace Rule 3 with an operative in-repo rule that treats the completed `design_goal.md` boundary/non-goals/identity choices as the constitution, or add a required scaffold field and reject placeholder seeds at startup. Remove the external-repository sentence or summarize the rationale locally, and describe only the five dispatchable stage sets; label future implementation sets as explicitly not shipped and not selectable.

## TR-4 — note — Tests do not guard the wheel/source inventory or exercise this template's graph and child path

**Evidence.** The new CLI test spot-checks root files, one goal-check prompt per set, and merely that each eval directory has at least one YAML (`src/tests/test_cli.py:199-250`); the idempotence test adds no inventory assertion (`src/tests/test_cli.py:253-264`). It never builds/installs a wheel, compares the archive's resource set with the source tree, validates all 27 checks, preflights all six sets, or dispatches a design stage child. The existing PM template has an actual clean-init child-dispatch test (`src/tests/test_cli.py:312-413`), but there is no design-loop equivalent. The recursive scan cannot detect a build omission: when running from a wheel it can enumerate only what Hatchling included.

**Concrete failure scenario.** A future Hatchling configuration/filter change omits a new hidden file or subtree. Source tests still pass because source traversal sees it, and wheel runtime silently scans the reduced archive, so init succeeds with an incomplete scaffold. Likewise, a workflow config can acquire an unsatisfiable cadence or a dispatcher can name a missing set without any template-specific runtime test failing.

**Specific fix.** Add a release test that builds the wheel, compares every `loopy_loop/templates/design_loop/` archive path to the source inventory (including dotfiles), installs that wheel into an isolated target, and asserts the exact scaffold inventory. Add parameterized preflight/success-schedule tests for all six sets plus a director planner→dispatcher→each-stage child dispatch test. The current empirical result—90 source files, 90 wheel files—is the baseline.

## TR-5 — note — The recursive resource walk unnecessarily expands import-time failure scope

**Evidence.** The recursive `Traversable.iterdir()` walk is defined at `src/loopy_loop/cli.py:39-61` and executed while importing `loopy_loop.cli` (`src/loopy_loop/cli.py:100-106`), before the Click group and commands are defined (`src/loopy_loop/cli.py:159-173`). The insertion order is otherwise sound: design is inserted before `PACKAGED_TEMPLATE_NAMES` is captured (`src/loopy_loop/cli.py:104-107`), so `click.Choice` deterministically exposes `default`, the two existing packaged names, then `design_loop` (`src/loopy_loop/cli.py:164-171`). `_init_packaged_template` then copies every scanned path and reads it as UTF-8 (`src/loopy_loop/cli.py:226-255`); the present wheel's zip-backed traversable and all present text files pass this path.

**Concrete failure scenario.** If a future/broken distribution omits the `design_loop` resource directory or uses a resource loader whose directory is unreadable, importing the CLI raises during the scan. Commands unrelated to templates—`loopy status`, `events`, `stop`, or even init of an older template—then fail before Click can parse the command. This does not occur in the wheel reviewed here, but the failure blast radius is avoidable.

**Specific fix.** Keep the template name in the static choice list, but defer `_scan_template_relative_paths("design_loop")` until `_init_packaged_template` is actually asked for that template, with an actionable `ClickException` naming the missing/corrupt resource. Cache the result after the first scan if desired.

## Blocking findings

- None.

**Verdict: The clean wheel and real scheduling/dispatch path are sound; merge is non-blocked, but TR-1 through TR-3 should be fixed before advertising the template as robust for existing target repositories.**

---

## Dispositions (2026-07-15, fixes applied in the same branch)

Verdict was non-blocking (clean wheel + scheduling/dispatch path sound). All five
findings addressed:

- **TR-1 accepted → fixed.** `_ensure_gitignore` now takes `extra_lines`; design_loop
  init ensures all three ignore entries even in a pre-existing repo. Added
  `_design_loop_integration_warnings`: init warns (with a repair pointer) when a
  pre-existing `.eval-banana/config.toml` lacks a `[harness] agent`, or when a
  pre-existing `CLAUDE.md` was left unmerged. Greenfield init stays warning-free; a new
  test covers the pre-existing-config warning + gitignore reconciliation.
- **TR-2 accepted → fixed.** The goal_check run command is now self-contained —
  `LOOPY_SESSION_DIR="<Session directory>" eval-banana run …` on one line — instead of a
  standalone `export` that would not survive across separate agent shell calls.
  `validate` (no scripts) keeps no env. Applied to all six goal_check prompts; verified
  the barrier still fires with the inline form.
- **TR-3 accepted → fixed.** CLAUDE.md Rule 3 now carries an operative default (the
  `design_goal.md` seed's boundary/non-goals ARE the constitution until a project
  sharpens it) so it is never an empty non-binding heading. The director README now
  names only the five shipped stage sets, marks implementation sets as not-shipped and
  not-selectable, and drops the external-repository pointer.
- **TR-4 partially fixed.** Added a drift-guard test (`_scan_template_relative_paths`
  output == on-disk template tree) and an all-six-graphs preflight test on a scaffolded
  target. The heavier build-a-wheel-in-CI inventory test is deferred (slow); the wheel
  path was verified manually and by this review (90 source == 90 wheel files, installed
  wheel scaffolds all 90).
- **TR-5 accepted → fixed.** The template list is no longer scanned at import: the name
  is added to a static choice list and `_resolve_packaged_template_files` scans on
  demand inside `_init_packaged_template`, raising an actionable `ClickException` if the
  resource is missing/corrupt. Import of `loopy_loop.cli` no longer touches template
  resources, so an unrelated command can't fail on a bad design_loop resource.

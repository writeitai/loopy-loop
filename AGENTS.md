# AGENTS.md — working agreement for this repo

`loopy-loop` runs long-running AI agent workflows inside a repository. A FastAPI
**coordinator** owns durable loop state in files and picks the next workflow; a single
**worker** runs each assignment through `team-harness`. Continuity lives in files and git,
not in a chat transcript. Read `README.md` for the user-facing model, and
`design/decisions.md` for *why the system is the way it is*.

This file is the working agreement for any agent (or human) changing this repo. Three
things are non-negotiable.

## Rule 1 — Respect the deliberate decisions; don't "fix" them

`design/decisions.md` is the canonical Architecture Decision Log (D1, D2, …). **Several
decisions there look like defects if you only skim the code, and are recorded precisely so
they don't get "fixed" by accident.** Before changing behavior in these areas, read the
relevant decision (and its companion design doc):

- **D3 — iteration success ≠ "work is good."** `IterationResult.success` means only that
  the harness ran without erroring. Do not make it consult worker exit codes / infer
  semantic success. The eval layer (`control.json` + `goal_check.json`) decides quality.
- **D4 — evaluation is LLM-as-judge; agents don't author deterministic checks.** Do not
  "add deterministic checks" to the stock `inner_outer_eval` eval workflows. (A target
  repo that owns its own test suite is a different case — see D4.)
- **D2 — single worker is deliberate.** Do not add parallel loopy workers as a scaling
  feature.
- **D5 — full autonomy with a last-resort escape hatch.** See Rule 2.
- **D8 — constraints are detection, not prevention.** Do not add preventive fences
  (path-level write enforcement, semantic scheduling vetoes, approval flows, arbitrary
  mid-run hard-fails). Express constraints as evaluation-layer checks whose failure
  blocks *acceptance* of the work, with repair as the path forward.

If you believe a decision is genuinely wrong, propose amending `design/decisions.md`
(state what changes and why) — do not silently contradict it in code.

## Rule 2 — Autonomy is the goal; the human escape hatch is a LAST resort (D5)

This system is meant to run **fully autonomously, with no human in the loop**. Human
involvement is a last resort, never a normal step.

- **The one sanctioned escape hatch already exists.** When a workflow hits a *genuinely
  terminal* blocker — a decision only a human can make, a missing credential, a
  billable/destructive action it isn't permitted to take — it writes the session
  `control.json` with:
  ```json
  {"state": "stopped", "reason": "<specific terminal blocker>", "stop_reason": "unresolvable_error", "schema_version": 1}
  ```
  This stops the loop as terminal, with a recorded reason. That is the entire
  human-in-the-loop mechanism, and it is enough.
- **Exhaust autonomous options first.** Re-scope, retry with a better child goal, route
  around the blocker. Reach for `unresolvable_error` only when a blocker is genuinely
  unavoidable without a human. It should fire rarely.
- **Make the give-up legible.** The recorded blocker is the only thing a human sees when a
  run stops — state exactly what was missing and what was tried.
- **Do NOT build or assume a preferred human gate.** No `paused` / `waiting_for_human`
  state, no `gate_request.json`, no external-action approval flow. That was considered and
  rejected (D5; `design/designs/improvement-proposals.md` P0.2). Do not pause a run to ask
  a question when you could either solve it autonomously or stop cleanly with
  `unresolvable_error`.

## Rule 3 — Design and decision docs must be understandable cold, by future agents AND humans

A design or decision doc is read by someone who was **not** in the conversation that
produced it — a future agent with no memory of this session, or a human who is not a
specialist. Write for them.

- **Explain, don't just name.** Naming a mechanism ("the ping-pong protocol", "cadence
  scheduling", "the double loop") is not explaining it. State, in plain language, *what it
  is, what problem it solves, and why we chose it*, with a concrete example where it helps.
- **The reasoning lives in the doc, not in your head.** Don't rely on a future reader
  re-deriving the rationale. A decision-log entry may state the conclusion tersely; the
  companion design section must be self-contained.
- **Anchor claims in the code.** Reference the file/function (e.g. `coordinator_app.py`,
  `harness_runner.py`) so a reader can verify — but lead with the plain-English meaning.
- **Keep `design/` honest about status.** `design/designs/` is binding; `design/analysis/`
  is working notes (may be superseded); `improvement-proposals.md` is *proposed, not
  decided*. Don't cite a proposal as if it were a decision.

When in doubt on any rule, favor the version a stranger could read cold and fully
understand.

---

Note: `CLAUDE.md` points here. Keep this file as the single source; don't fork the two.

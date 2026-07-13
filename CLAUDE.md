# CLAUDE.md

The working agreement for this repo lives in **[`AGENTS.md`](./AGENTS.md)** — read it first.
It is the single source; this file only points to it so the two never diverge.

Quick orientation for what most often trips up a change here:

- **`design/decisions.md`** is the canonical decision log. Several decisions look like
  defects if you only skim the code and are recorded so they don't get "fixed" by accident
  (notably **D3** iteration-success semantics and **D4** LLM-as-judge evaluation).
- **Autonomy is the goal; the human escape hatch is a last resort (D5).** The system runs
  unattended. When a workflow hits a genuinely terminal blocker it stops by writing
  `control.json` with `stop_reason: "unresolvable_error"` and a specific recorded reason —
  that is the entire human-in-the-loop mechanism. Do **not** build or assume a preferred
  `paused` / `waiting_for_human` gate; exhaust autonomous options first, then stop cleanly.

See `AGENTS.md` Rules 1–3 for the full agreement.

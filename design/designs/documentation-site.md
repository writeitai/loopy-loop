# Design: Public Documentation Site

**Status:** Accepted and implemented (`website/` plus `.github/workflows/docs-deploy.yml`)
**Date recorded:** 2026-07-12
**Applies to:** the self-contained `loopy-loop` documentation site in `website/`
and its GitHub Pages deployment target on a `writeit.ai` subdomain.

This document was written before implementation and now records the decisions behind
the shipped site, so a later reader understands *why* the stack looks the way it does.
The implementation landed in `website/`; its build and deployment workflow lives at
`.github/workflows/docs-deploy.yml`.

The shared principle:

> **Ship the docs as a self-hostable static module that lives with the code, authored
> in MDX the native Next.js way, styled to read as part of the WriteIt family, and
> searchable from the keyboard.**

---

## Goals

1. **Presentable, human-first documentation.** Comprehensible to a developer meeting
   `loopy-loop` for the first time — not just an API dump. Good navigation, prose,
   examples.
2. **Self-hostable module.** A `clone → build → host` static site, no proprietary
   docs SaaS, no external service required to run it. It ships *in this repo* so docs
   version with the code.
3. **On a `writeit.ai` subdomain, styled like `writeit.ai`.** Reachable at a
   subdomain pointed by CNAME, and visually in the same colour family as the main
   site, so a link from `writeit.ai` feels continuous.
4. **Authored in MDX, the way Next.js's own docs work** — Markdown files that are
   routes, with the option to drop in components.
5. **Keyboard search (⌘K / Ctrl+K).** A command-palette search over the docs.

---

## Precedent survey (what already exists in the org)

Three sibling frontends were reviewed before choosing an approach:

- **`orchestra/public_and_admin_app/fe`** (the `247agents.io` site) — **the model we
  followed.** It is a Next.js 16 App-Router site whose docs are native
  [`@next/mdx`](https://nextjs.org/docs/app/building-your-application/configuring/mdx)
  pages: each `src/app/(public)/docs/**/page.mdx` file *is* a route. It uses Tailwind
  v4 + shadcn/ui + `@tailwindcss/typography` (`prose`), `rehype-pretty-code` (Shiki)
  for code highlighting, `rehype-slug` for heading anchors, a hand-maintained
  navigation array (`src/lib/docs/navigation.ts`), a docs `layout.tsx` giving a
  three-column sidebar / prose / on-this-page shell with prev–next pagination, and it
  statically exports (`output: 'export'`) to a static host. It was assessed as
  "highly self-contained and very easy to lift" into a standalone docs site. It has
  **no search** and its dark-mode tokens exist but are not wired to a toggle.

- **`writeit/fe`** (the `writeit.ai` marketing site) — **the source of the palette and
  the hosting reference, not the docs engine.** Next.js 15 static export on Firebase
  Hosting. Its docs engine is a hand-rolled `next-mdx-remote` system whose route was
  actually deleted (commit `1db91162`); we do **not** reuse it. What we take from here
  is the brand colour palette (below) and the confirmation that WriteIt ships static
  Next.js exports behind per-subdomain hosting.

- There is **no `composer` repo** in `moje/` (an earlier assumption); the real
  precedent is orchestra, above.

---

## Decision 1 — Replicate orchestra's native `@next/mdx` pattern

### Decision

Build the site by replicating orchestra's docs system: **native `@next/mdx` with
`page.mdx`-as-route**, Tailwind v4 + shadcn/ui + `@tailwindcss/typography`,
`remark-gfm` + `rehype-slug` + `rehype-pretty-code`, a hand-maintained navigation
array, a docs layout with sidebar + on-this-page TOC + prev/next, and
`output: 'export'` for a fully static build.

### Context / why

It is a proven in-house pattern, it is exactly the "Next.js docs authored in MDX"
feel that was asked for (MDX files *are* the pages), and it is genuinely
self-contained — roughly six files to model. Staying on the org's existing convention
means no new framework to learn or maintain.

### Alternatives considered and rejected

- **Fumadocs** (a purpose-built MDX docs framework). It ships ⌘K search and dark mode
  for free, which is attractive. Rejected as the default because it is a new framework
  outside the org's convention and adds surface area; the one thing it would save us —
  search — we can add with a small, self-hostable dependency (Decision 4). *This
  remains the fallback if we later decide the search wiring is not worth owning.*
- **Reuse `writeit/fe`'s `next-mdx-remote` engine.** Rejected: older approach, no
  Tailwind, no search, and its route was deleted — orchestra's system is strictly the
  better internal precedent.

---

## Decision 2 — Live inside this repo, under `website/`

### Decision

The site lives in the `loopy-loop` repository at `website/`, not in a separate repo
and not inside the `writeit` monorepo.

### Context / why

Goal 2 (self-hostable module) and the choice of GitHub Pages (Decision 3) both point
here: the docs version alongside the code they describe, in one place a contributor
can build and host. Putting them in the private `writeit` monorepo would couple an
Apache-2.0 OSS project's docs to a private marketing repo and undercut "self-hostable
module"; a separate docs repo would let docs drift from code.

### Consequences

- Docs PRs and code PRs can move together; a change that alters the CLI can update its
  page in the same change.
- The `website/` app has its own `package.json` / toolchain, independent of the Python
  package. It is not published to PyPI; it is only ever built into a static site.

---

## Decision 3 — Host on GitHub Pages at `loopy.writeit.ai` via CNAME

### Decision

Publish the static export to **GitHub Pages** from this repo, served at the custom
subdomain **`loopy.writeit.ai`** pointed via CNAME.

### Context / why

GitHub Pages keeps hosting with the OSS repo (no separate cloud project), supports a
custom domain, and needs no server since the site is a static export. The
product-branded subdomain (`loopy.writeit.ai`) was chosen over a generic
`docs.writeit.ai` so the URL ties to the tool by name.

### Mechanics

- **Build/deploy:** a GitHub Actions workflow builds the static export, runs the
  search indexer (Decision 4) over the output, and deploys with `actions/deploy-pages`.
  A `.nojekyll` marker is included so Next's `_next/` assets are served.
- **Next.js config:** `output: 'export'` plus `trailingSlash: true` so directory-style
  URLs resolve to `index.html` on Pages. Because the site is served at the subdomain
  **root**, no `basePath` is needed.
- **Custom domain:** a one-time provisioning step, since an Actions-based Pages
  deploy does not read a `CNAME` file from the artifact. In the repository's
  **Settings → Pages**, set the source to **GitHub Actions** and the custom domain to
  `loopy.writeit.ai`; add a DNS record in the `writeit.ai` zone
  (`loopy.writeit.ai  CNAME  writeitai.github.io`); then enable Enforce HTTPS. The
  committed `public/CNAME` records the intended domain but does not bind it on its own.
  Until the domain is bound, the site is served under
  `writeitai.github.io/loopy-loop/`, where root-relative `/_next/` and `/pagefind/`
  URLs would not resolve — so this step precedes sharing the link. See
  `website/README.md` for the exact checklist.

### Alternatives considered and rejected

- **Firebase Hosting** (what `writeit` and `orchestra` use). It would match org ops
  most closely and CNAME the same way, but it pulls the OSS project into a private GCP
  project. Rejected in favour of keeping hosting self-contained with the repo. (If ops
  consistency later matters more than repo-locality, this is the natural switch and
  requires no code change — only a different deploy step.)
- **Vercel.** Best Next.js DX, but a new platform in the stack for no gain over static
  Pages here.

---

## Decision 4 — Keyboard (⌘K) search via Pagefind + `cmdk`

### Decision

Add search with **[Pagefind](https://pagefind.app/)** (a static, self-hostable search
that indexes the built HTML as a post-build step) surfaced through a **shadcn
`Command` dialog (`cmdk`)** bound to ⌘K / Ctrl+K.

### Context / why

GitHub Pages is static-only, and the site must stay a self-hostable module — so an
external search service (Algolia DocSearch and friends) is out. Pagefind builds its
index from the exported HTML with no backend and a tiny client runtime, and `cmdk`
(already the basis of shadcn's `Command` component) gives the classic command-palette
UX. This closes orchestra's one real gap (it has no search) without leaving the
self-hostable constraint.

### Consequences

- Search index is generated at build time and served as static assets; no query-time
  server.
- The palette is a small client component mounted globally and opened by a keyboard
  listener.

---

## Decision 5 — WriteIt palette, open-font substitute, light-first

### Decision

Style the site with `writeit.ai`'s brand colours, substitute an **open font** for the
domain-locked proxima-nova, and ship **light mode first** (dark mode as a fast-follow).

### The palette (from `writeit/fe/styles/globals.scss`)

| Token | Hex | Role |
|---|---|---|
| sand | `#f7ebbd` | page background |
| ink | `#222433` | text / headings |
| green | `#5ca493` | primary accent |
| gold | `#ebaa1a` | secondary accent |
| red | `#f34832` | alert / emphasis |

These become the shadcn/Tailwind CSS variables (`--background`, `--foreground`,
`--primary`, etc.), replacing orchestra's monochrome neutral tokens.

### Context / why

- **Font:** `writeit.ai` uses **proxima-nova**, a paid Adobe Typekit font served from
  a domain-locked kit. It cannot ship in a self-hostable OSS module and will not render
  off `writeit.ai` domains. We therefore match the *colours* exactly and pick a close
  open substitute. The implementation chose **Hanken Grotesk**, loaded through
  `next/font`; it self-hosts cleanly and is visually adjacent to proxima-nova. This
  keeps the module truly self-hostable while preserving brand feel.
- **Light-first:** `writeit.ai` itself is light-only, so light mode is the faithful
  match. Dark mode is a cheap fast-follow (`next-themes` + the shadcn dark tokens
  orchestra already defines) and is deferred, not designed out.

---

## Decision 6 — Restructure existing content into MDX; reconcile `SKILL.md`

### Decision

The first version restructures **existing** documentation into MDX pages rather than
writing net-new prose. The same release sequence also reconciled the drifted
`skills/loopy-loop/SKILL.md` against the current API.

### Context / why

The content was roughly 80% already written — the then-466-line `README.md`,
`docs/session-layout.md`, `docs/http-contract.md`, the accepted
`success-semantics-and-evaluation.md` design doc, the `CHANGELOG`, and `SKILL.md`.
This was primarily a re-homing and structuring job. Before the implementation,
`SKILL.md` referenced the older `.loopy_loop/workflows/` layout and pre-0.2.0 polling
language. The site and corrected Skill now follow the README's two-endpoint /
`workflow_sets/` model.

### Implemented information architecture

Each entry is one `page.mdx`, sourced from the material in parentheses.

| Route | Source material |
|---|---|
| `/docs` — Introduction | README overview, tagline, value prop |
| `/docs/getting-started` | README install (`uv`/`pip`), Agent Skill, `loopy init`, first run |
| `/docs/concepts` | Architecture: coordinator/worker, team-harness delegation, the iteration loop, session dir as durable state |
| `/docs/configuration` | `loopy_loop_config.yaml`, `team_harness_*` settings |
| `/docs/workflows` | Workflow sets, scheduling fields, cadence, reserved `goal_check`, the three templates |
| `/docs/success-and-control` | `control.json` vs `goal_check.json`, `stop_reason` values (from the success-semantics design doc) |
| `/docs/evaluation` | LLM-as-judge / eval-banana; why agent-authored deterministic checks are forbidden |
| `/docs/session-layout` | `docs/session-layout.md` |
| `/docs/http-contract` | `docs/http-contract.md` |
| `/docs/child-sessions` | `child_requests` contract, `pm_planner_dispatcher` |
| `/docs/cli-reference` | `init` / `coordinator` / `worker` / `status` / `events` / `stop` |
| `/docs/troubleshooting` | `SKILL.md` "Common Pitfalls" |

The nav order and grouping live in one hand-maintained array (orchestra's
`navigation.ts` pattern), which also drives prev/next.

---

## Stack summary

`Next.js` (App Router) · `@next/mdx` · `output: 'export'` + `trailingSlash: true` ·
`Tailwind v4` + `shadcn/ui` + `@tailwindcss/typography` · `remark-gfm` +
`rehype-slug` + `rehype-pretty-code` (Shiki) · `Pagefind` + `cmdk` for ⌘K search ·
WriteIt palette with Hanken Grotesk · GitHub Pages deployment target at
`loopy.writeit.ai` (the external Pages/DNS provisioning remains an operational step).

---

## Follow-up (out of scope for this repo's PR)

The remaining cross-repository follow-up is to add a "Docs" link on `writeit.ai`
pointing to `https://loopy.writeit.ai`. That change belongs in the `writeit` repo and
is not part of this implementation.

---

## Summary

| Decision | Choice | Why |
|---|---|---|
| 1. Engine | Replicate orchestra's native `@next/mdx` pattern | Proven in-house, MDX-as-routes, self-contained; not a new framework |
| 2. Location | `website/` in this repo | Self-hostable module; docs version with code |
| 3. Hosting | GitHub Pages at `loopy.writeit.ai` via CNAME | Hosting stays with the OSS repo; static, no server |
| 4. Search | Pagefind + `cmdk` (⌘K) | Static, self-hostable, closes orchestra's search gap |
| 5. Style | WriteIt palette + Hanken Grotesk, light-first | Brand-continuous; font stays self-hostable; faithful to a light-only site |
| 6. Content | Restructure existing docs into MDX; fix `SKILL.md` drift | Content ~80% written; keep docs on the current API |

### When to revisit

- If owning the search wiring proves not worth it, switch Decision 1 to **Fumadocs**
  (search + dark mode built in). The content (MDX) ports with little change.
- If org ops-consistency outweighs repo-locality, switch Decision 3 to **Firebase
  Hosting** (same CNAME flow, different deploy step, no code change).
- **Dark mode** remains an optional follow-up (`next-themes` + shadcn dark tokens).

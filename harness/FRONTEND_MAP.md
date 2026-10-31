# FRONTEND_MAP — NO APPLICATION, TWO RENDERED PAGES

**Classification: VERIFIED — no frontend application; two server-rendered HTML outputs.**

Corrected 2026-09-08 on two counts. The `find` below was cited as returning nothing while
`package.json`, `tsconfig.json` and `src/index.ts` were all git-tracked — WebStorm's default
scaffold, one `console.log`, no TypeScript source. They were deleted this session, so the
evidence is now correct rather than merely asserted. And the claim that the tearsheet is the
only rendered output omitted `web/ops.py`, which the same repository's ROADMAP and
PROJECT_STATE both count as shipped.

Evidence, re-run 2026-09-08: `find . -path ./.git -prune -o -path ./.venv -prune -o \( -name
package.json -o -name '*.tsx' -o -name '*.jsx' -o -name '*.vue' -o -name index.html \)
-print` → nothing. No build step, no bundler, no node_modules, no framework.

**The operations page** — `web/ops.py` (584 lines) served by `scripts/run_ops.py`. Stdlib
`http.server`, one route (`GET /`, 404 for everything else), no JavaScript at all, no
dependency, no build. It refreshes with `<meta refresh>` so it works in a terminal browser
over a phone tether. It shows feed lag, same-venue disagreements, account state and open
tickets — and **no strategy result, by design**, which `tests/test_ops_page.py` asserts by
rendering a page and requiring that "sharpe", "equity curve" and four other strings never
appear. Deferred results pages are P6 in `harness/ROADMAP.md`; the condition that lifts the
deferral is one strategy surviving out of sample *and* 30 days of paper divergence.

**The tearsheet** — `analytics/tearsheet.py`: a static HTML page with hand-written
SVG charts, produced by `examples/run_tearsheet.py` into `runs/` (gitignored). It is a
report, not an application. Its design deliberately puts walk-forward verdicts *above* the
equity curve (a test asserts document order) and prints the cost model in the header, so
the persuasive artefact carries its own caveats.

Charter §30–§34 (dashboard, explorer, asset/strategy/portfolio/research pages, UX audit,
cache contracts, data-propagation audit) — nothing to audit. **P6, DEFERRED**; condition in
`ROADMAP.md`.

Charter §76 (UI must distinguish LIVE / DELAYED / HISTORICAL / STALE / ESTIMATED /
MODEL-DERIVED) — the underlying signals already exist in code: `BacktestResult.stale_marks`,
`StreamStats.is_complete`, `PerformanceSummary.annualisation_is_meaningful`,
`DivergenceReport.agrees`. A frontend built later should surface these, not recompute them.

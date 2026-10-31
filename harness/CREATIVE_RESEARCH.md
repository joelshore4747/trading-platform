# CREATIVE_RESEARCH

Charter §57–§58. **Three entries since 2026-09-03, all run the same day: CR-001 and CR-002 REJECTED, CR-003 PROMISING (synthetic; ADR 0009 proposed)**; was zero at the audit (no file, ADR or roadmap step records a creative-lab experiment). Candidate areas below are **PROPOSED**. This file establishes the lab's rules so the first entry
is held to them.

## The template — every idea, no exceptions
```
ID:               CR-NNN
IDEA:             one sentence
WHY IT MIGHT WORK: the mechanism, not the hope
WHAT WOULD DISPROVE IT: stated BEFORE the experiment
DATA REQUIRED:    and whether it exists here (see EXTERNAL_DATA_MAP)
BASELINES:        buy-and-hold · random control · simplest strategy · existing strategy
EXPERIMENT:       reproducible command
RESULT:           numbers; deflated for trials
CONCLUSION:       REJECTED / PROMISING / needs data — never "works"
NEXT EXPERIMENT:  or RETIRED
```

## The gate — from this repository's own evidence, not from the charter
- **Novelty is not evidence.** An idea must beat the null harness (ADR 0003: ~1,000+ OOS bars
  or the false-discovery rate is 7–8%) and survive the gauntlet like anything else.
- **Count the trials.** Every creative idea tried is a trial against the same data;
  `analytics/multiplicity.py` deflates for it. Thirty ideas in thirty days is thirty trials.
- **The probe matters** (ADR 0005). A weak edge on a strategy that barely trades is invisible;
  state which probe produced any detection claim.
- **Never touches production behaviour** — a creative result becomes a `/new-strategy`
  candidate, not a parameter change.

## Candidate areas the repository is already equipped to test
| Area | Why here, now | Data status |
|---|---|---|
| Information half-life of funding settlements (§47) | funding is modelled; settlement bursts measured (2.67x volume at :00) | archive has none — needs funding feed |
| Cross-symbol lead/lag on the 39-symbol perp panel | `data/panel.py` exists; 14.5 days of full cross-section | have |
| Does the 07–10 UTC coverage bias distort intraday seasonality? | measured 56% vs 25% | have — a data-quality study, not a strategy |
| Plateau vs spike: is `neighbour_ratio` or `parameter_stability` right? (ADR 0005 open) | both gates exist; disagree on genuine edges | synthetic |
| Regime topology on calibrated synthetics (§46) | GARCH regimes exist | synthetic |

## Deferred until data exists (§18–§20)
Government-contract networks, news propagation, supply-chain contagion, knowledge graph.
No ingestion exists; an experiment without data is a proposal.

## Entries

Ranked by how cheaply each can be falsified on data the repository holds today. Every
statistic and threshold below was written before any of it was run. Author: creative-researcher
(Fable), 2026-09-03; appended by the parent session because the brief had no write tool. Files
read: CREATIVE_RESEARCH.md, DATA_MAP.md, EXTERNAL_DATA_MAP.md, EXPERIMENT_INDEX.md,
docs/adr/0005-cross-section.md, docs/ROADMAP.md 25-28, data/panel.py, data/vendor.py,
data/synthetic.py, analytics/walkforward.py, analytics/windows.py, analytics/multiplicity.py,
analytics/trials.py, scripts/measure_null.py, scripts/import_vendor.py,
strategy/examples/cross_sectional_momentum.py, strategy/base.py, engine/backtester.py 205-260.

---
ID:                CR-001
IDEA:              The archive's 54% of missing minutes may be missing *because* the market was
                   moving (a collector that drops under load), not merely because 18-23 UTC is
                   under-sampled - and if so every volatility, fill and cost figure measured on
                   this data is biased low, including E-011's.
WHAT WOULD DISPROVE IT:
                   Two pre-registered statistics on the 39 screened symbols (TONUSDT out), every
                   return computed strictly within a `vendor.load_segments` segment, never across
                   a hole:
                   (a) Gap-edge test. Take the LAST bar before each gap and the FIRST bar after
                       it. Compare their |1m log return| and volume against interior bars of the
                       same symbol at the same UTC hour: 1,000 draws of an equal number of
                       hour-matched interior bars give the null band. DISPROVED if the edge-bar
                       median |return| AND median volume both sit inside the 2.5-97.5 percentile
                       band of that null for at least 35 of 39 symbols.
                   (b) Day-level test. For each of the 32 days and each symbol: coverage
                       fraction vs realised variance (mean squared within-segment 1m log return).
                       Spearman rho per symbol; cross-symbol median; null by 1,000 shuffles of
                       the day labels. DISPROVED if the median rho sits inside the null band.
                   (c) Recorded either way, not a disproof: the share of gap minutes common to
                       all 40 symbols. Near 1.0 means one collector process, so (a) and (b) are
                       about the collector; well below 1.0 means symbol-specific holes and the
                       mechanism below is wrong regardless of (a)/(b).
                   If (a) and (b) are both null, missingness is MCAR conditional on hour of day,
                   per-hour figures are unbiased, and the only correction owed is reweighting
                   unconditional intraday figures by 1/coverage(hour) - a reporting fix. If the
                   LAST-bar-before-gap statistic fires, the collector fails under load: MNAR,
                   and every vol-derived number on this dataset understates the market.
WHY IT MIGHT WORK: Borrowed from survey statistics (Rubin's MCAR/MAR/MNAR): non-response that
                   correlates with the quantity surveyed biases every estimate, and no amount of
                   data makes it go away. The mechanism here is mundane and testable: a websocket
                   collector is likeliest to disconnect, back up, or be rate-limited exactly when
                   message volume spikes, i.e. when the market moves. Nobody "loses" a trade; what
                   loses is every downstream estimate if the holes are activity-conditioned.
                   Evidence this is worth seconds of compute: `Segment` already exists because
                   a return across a hole is "a multi-day move labelled as one bar"
                   (data/panel.py:531-533), and E-011's cost-heavy result (in-sample Sharpe -2.52)
                   would be, if anything, optimistic if the missing minutes are the expensive ones.
DATA REQUIRED:     Exists. historical.zip -> data/cache via scripts/import_vendor.py. Needs only
                   data/cache.read_bars + vendor.load_segments + vendor.coverage_by_hour; no
                   Panel, no engine. Note the 56%/25% figure in DATA_MAP was computed on ONE
                   reference symbol (scripts/import_vendor.py:112-113, `sorted(loaded)[0]`;
                   vendor.py:500-503 quotes the BTC file). Whether it generalises to all 39 is
                   UNKNOWN and falls out of this experiment for free.
BASELINES:         Not a strategy, so the baselines are nulls: hour-matched interior bars
                   (random control) - the naive unconditional per-hour figure (simplest) - the
                   existing single-symbol coverage_by_hour measurement (existing).
                   Buy-and-hold: n/a.
EXPERIMENT:        .venv/bin/python scripts/import_vendor.py historical.zip
                   .venv/bin/python scripts/measure_gap_bias.py --cache data/cache --market um \
                       --draws 1000 --seed 0            # no engine
                   Inputs: the 39 symbols screen() kept (data/cache/um/_universe.json), 1m.
                   Outputs: per-symbol table [gaps, shared_gap_share, edge |r| percentile,
                   edge volume percentile, day rho, permutation p]; cross-symbol medians; the
                   verdict against (a)/(b)/(c) as written above. Record first with
                   scripts/experiment.py new (status QUEUED), fill result after.
                   Trial cost: 2 pre-registered statistics, 0 strategy trials. Runtime: seconds
                   (845,462 rows). Coverage caveat: the test is itself weighted to 07-10 UTC.
RESULT:            2026-09-03, E-013, 10 s. (a) 38/39 symbols inside both bands (threshold
                   35); 0 above the 97.5th percentile on either metric; the one outlier
                   (WLFIUSDT, 0.2nd pct on |r|) is in the wrong direction. (b) median rho
                   -0.026 inside [-0.084, +0.086]. (c) 98.8% of missing minutes common to all
                   39 symbols.
CONCLUSION:        REJECTED. Missingness is MCAR conditional on hour of day; one collector,
                   one outage pattern; per-hour figures unbiased; the only correction owed is
                   1/coverage(hour) reweighting of unconditional intraday figures. E-011's
                   figures are not biased low by the holes.
NEXT EXPERIMENT:   RETIRED. Reweighting note added to DATA_MAP. Clears the way for CR-002.

---
ID:                CR-002
IDEA:              BTCUSDT's one-minute return leads the cross-section of the 38 alt perpetuals'
                   next-minute returns, because price discovery lives in the deepest book and
                   alt-perp quotes re-centre with latency.
WHAT WOULD DISPROVE IT:
                   ONE pre-registered statistic, not 38 x 2 leaders x 7 lags. On the intersection
                   Panel (39 symbols, ~20,856 rows), with every row listed in `panel.gaps`
                   excluded from the differenced series (data/panel.py:524-541):
                   S = cross-sectional mean over the 38 followers of the Spearman correlation
                   between r_BTC(t) and r_alt(t+1). Spearman, not Pearson, so the 2026-08-21
                   02:44 minute (XRP +21.97%, roadmap 26) cannot be the result on its own.
                   Null: 1,000 circular shifts of the BTC column by a random offset >= 60 rows
                   applied within each contiguous segment (preserves BTC's own autocorrelation
                   and the alts' contemporaneous structure; destroys the alignment).
                   DISPROVED, any one of:
                   (1) S below the 99th percentile of the null (one-sided: the mechanism
                       predicts positive; a negative lead is a different hypothesis).
                   (2) Mechanism check. Lag 0 (contemporaneous) must dwarf lag +1, and lag -1
                       (alts leading BTC) must sit inside its own null band. If lag -1 is as
                       large as lag +1, the "lead" is symmetric factor autocorrelation, not
                       propagation, and the idea is dead even with (1) passed.
                   (3) Economics. Mean signed next-minute alt return when |r_BTC(t)| is in its
                       top decile, signed by r_BTC, must exceed one round trip of the default
                       CostModel in engine/execution.py (read the defaults there; they are not
                       restated here). Below it, the effect exists and is untradeable at 1m,
                       which is REJECTED for this repository's purpose.
WHY IT MIGHT WORK: Liquidity topology: BTC perp has the deepest book on the venue, so large
                   flows, liquidations and spot-perp arbitrage print there first. Alt-perp
                   market makers hedge in BTC and re-quote with latency; the loser is the
                   alt-perp liquidity provider who is slow to re-centre, and the taker who picks
                   them off pays taker fees for the privilege - which is exactly why the effect
                   should already be gone at 1-minute granularity. External prior, from search
                   snippets only (fetch of the 2026 Springer paper was blocked by the egress
                   proxy; contents UNKNOWN): tick-level work reports BTC->alt leads of tens of
                   seconds (Anderson, IMFI 2023, snippet: "16 to 118 seconds, average 57" -
                   INFERRED from the snippet, paper unread). If that is right, most of the lead
                   is inside a 1m bar and lag +1 carries only the residue. Honest prior: S small
                   and positive, dead on (3). ADR 0005 says the downstream pipeline sees an edge
                   only from IC ~0.05 upward; a rank correlation of 0.02 would be invisible to it,
                   so stage 2 below should not even be attempted unless S is well above that.
                   Whatever is found is a 07-10 UTC phenomenon two to one (DATA_MAP); the
                   US-hours version is under-sampled.
DATA REQUIRED:     Exists. Panel.load(root="data/cache", market="um", symbols=<39 screened>,
                   interval="1m", fields=("close",), grid="intersection"); intersection costs
                   1.3% (roadmap 26). No funding series ships, so stage 2 is a price result,
                   not a tradeable one, until a funding feed exists (EXTERNAL_DATA_MAP).
BASELINES:         buy-and-hold: BTCUSDT and an equal-weight alt basket over the same instants
                   (stage 2, via walk_forward benchmark_symbol) - random control: the
                   circular-shift null above - simplest: the lag-0 contemporaneous correlation,
                   which must be far larger (the leakage anchor) - existing:
                   CrossSectionalMomentum (E-08), the only cross-sectional probe this
                   repository has run.
EXPERIMENT:        Stage 1 (falsifies (1)-(3), no engine):
                   .venv/bin/python scripts/import_vendor.py historical.zip
                   .venv/bin/python scripts/measure_leadlag.py --cache data/cache --market um \
                       --leader BTCUSDT --lags -1 0 1 --draws 1000 --seed 0   # PROPOSED script
                   Inputs: Panel as above. Outputs: table lag x {mean Spearman, null 99th pct,
                   followers above their own null}; the conditional-return-vs-cost line for
                   (3); a Pearson column reported alongside, unregistered. Runtime: seconds.
                   Register in TrialRegistry (analytics/trials.py) BEFORE running: hypothesis
                   as IDEA, mechanism as WHY, parameter_points=1 (one lag pre-registered; the
                   other two are diagnostics, not shots).
                   Stage 2, only if stage 1 survives all three: a /new-strategy candidate
                   `LeaderFollower` acting in on_cross_section from ctx.closes("BTCUSDT", 2),
                   run through analytics.walkforward.walk_forward on vendor.merge of the 39
                   symbols' bars in an InMemoryBarFeed (split_walk_forward counts DISTINCT
                   TIMESTAMPS, windows.py:195, so instants, not rows); train 3000 / test 1000 /
                   warmup 60 instants; grid <= 9 cells over {|r_BTC| decile threshold, holding
                   minutes}; periods_per_year=365*24*60; default CostModel; fully collateralised
                   crypto_perpetual as in examples/run_walk_forward_btc.py. Deflate with
                   analytics/multiplicity.deflated_sharpe_ratio for SweepSurface.attempted.
                   Trial cost: stage 1 = 1; stage 2 = 9 cells.
RESULT:            2026-09-03, E-014, 6.5 s. S(+1) = +0.0016, percentile 66.5 of the
                   shift null (99th pct +0.0088). Lag 0 = +0.431; lag -1 = -0.002, in band.
                   Conditional next-minute alt return +0.16 bp vs 26 bp round trip.
CONCLUSION:        REJECTED on (1) and (3). No one-minute lead; the co-movement is
                   contemporaneous factor structure. Stage 2 is not attempted.
NEXT EXPERIMENT:   RETIRED at 1m. A sub-minute version needs tick data this repository does
                   not hold; noted, not scheduled.

---
ID:                CR-003
IDEA:              On a broad genuine edge the walk-forward's argmax wanders between adjacent,
                   near-equal cells; `parameter_stability` counts every wander as a change while
                   `neighbour_ratio` reads the same plateau as evidence, and a plateau-aware
                   stability (count only NON-adjacent moves, using the `_is_adjacent` that already
                   exists) would recover ADR 0005's false negatives without admitting more noise -
                   or it would not, and either answer closes ADR 0005's open item.
WHAT WOULD DISPROVE IT:
                   Both arms are required (ADR 0003's two-sided rule). The alternative statistic
                   is computed OFFLINE from WalkForwardReport.folds (each FoldResult carries
                   `params` and `surface`, walkforward.py:202-229); nothing in verdicts() is
                   touched by this experiment. Changing a gate is a §69 decision, not a result.
                   DISPROVED, any one of:
                   (1) Null arm. With zero persistence (IC ~0), the discovery rate under
                       plateau-aware stability exceeds the current null rate's 95% upper bound
                       (DiscoveryRate.upper, analytics/nulltest.py:179). More noise admitted =
                       REJECTED, whatever the detection arm shows.
                   (2) Detection arm. At the persistence rows where ADR 0005 saw stability as
                       the binding objection (0.90, 0.95: "4 of 5" survived), the seeds that
                       failed on parameter_stability ALONE are not recovered - either the count
                       recovered is 0, or they fail on a different gate once stability is
                       redefined.
                   (3) Mechanism. In those failing seeds the argmax's moves are NOT between
                       adjacent cells (it jumps across the grid). Then the plateau story is
                       wrong and the stricter gate was right.
WHY IT MIGHT WORK: Model disagreement as a diagnostic: two gates reading identical evidence in
                   opposite directions is itself the finding (ADR 0005 "The objection that
                   migrates"). When an effect is strong and broad, many cells work and the
                   in-sample maximum is decided by noise among near-equals, so its coordinate
                   is a random walk on the plateau. `parameter_stability` (walkforward.py:288)
                   compares `_params_key` equality, so a one-step drift on a 3x3 grid scores
                   the same as a jump from corner to corner. Who loses: nobody trades this; a
                   false negative here costs the lab a real edge it already paid trials to find.
                   The honest prior: it might also let a noisy grid's random walk pass, which
                   is what arm (1) is for.
DATA REQUIRED:     Synthetic - but NOT present as committed. The latent-drift "persistence"
                   generator ADR 0005 / E-08 used is not in the repository (VERIFIED: `rg
                   persistence` hits only the GARCH comment in data/calibration.py:61 and two
                   unrelated tests; EXPERIMENT_INDEX E-08 reproduce column is "-").
                   data/synthetic.correlated_series (synthetic.py:515) has a shared factor but no
                   persistent per-symbol drift. Step 0 is rebuilding it, seeded, inside the
                   script (~40 lines): per-symbol AR(1) latent drift with persistence in
                   {0.0, 0.9, 0.95}, IC reported as the rank correlation of trailing vs forward
                   return, so the rows are comparable to ADR 0005's table.
BASELINES:         existing: current parameter_stability verdict as written - random control:
                   the null arm (persistence 0) - simplest: no stability gate at all (the upper
                   bound on recovery, and what it costs in null rate) - buy-and-hold: the
                   walk-forward's own benchmark column, unchanged.
EXPERIMENT:        .venv/bin/python scripts/measure_plateau.py --seeds 10 --symbols 12 \
                       --instants 900 --persistence 0.0 0.9 0.95        # PROPOSED script
                   Same probe and settings as ADR 0005, deliberately untuned:
                   CrossSectionalMomentum, train=300 / test=100 / warmup=120, 9-cell grid over
                   lookback x rebalance_every, default CostModel, 12 symbols, 900 instants; 10
                   seeds per row rather than 5 because 5 "cannot locate that threshold". For
                   every run collect report.verdicts, parameter_stability, and the offline
                   plateau_stability = share of fold transitions that were same-or-adjacent
                   (walkforward._is_adjacent on the chosen cell's coord). Output: per row,
                   survived(current) / survived(plateau-aware) / null rate both ways / (3)'s
                   adjacency share. Runtime: UNKNOWN (ADR 0005 does not record its own).
                   Trial cost: 0 strategy trials (harness calibration); record as an experiment
                   with scripts/experiment.py new. Any change to verdicts() that follows is a
                   separate, explicit decision - a gate is a safety control.
RESULT:            2026-09-03, E-015, 92 s, 10 seeds x 5 persistence rows. Null arm: 0/10 vs
                   0/10 at p=0 and p=0.5. Detection: 5 of 6 stability-only failures recovered
                   at p=0.9/0.95; every recovered seed's moves were adjacent, the one refused
                   had jumped. Recoveries are at IC 0.11-0.42, above real momentum.
CONCLUSION:        PROMISING (synthetic). The plateau-aware reading admits no more noise and
                   refuses fewer genuine plateaus. A gate is a safety control: ADR 0009 is
                   PROPOSED for a human decision; verdicts() is unchanged.
NEXT EXPERIMENT:   Re-run on real data the first time a strategy reaches the stability
                   objection; until then the two readings are both reported, offline.

## Findings the lab made along the way (§119, from the creative-researcher run)
1. **P2 · reproducibility.** The latent-drift generator behind ADR 0005 / E-008 is not committed
   (`rg persistence` finds only a GARCH comment). CR-003 must rebuild it; commit it when it does
   and backfill E-008's reproduce column.
2. **P3 · provenance.** The 56%/25% coverage figure in DATA_MAP was measured on one reference
   symbol (`scripts/import_vendor.py`, `sorted(loaded)[0]`). Whether it holds for all 39 is
   UNKNOWN; CR-001 (c) answers it.
3. **P3 · external evidence UNKNOWN.** The one paper worth reading on BTC→alt lead/lag was
   blocked by the egress proxy; CR-002's tick-level prior is a search snippet, labelled INFERRED.

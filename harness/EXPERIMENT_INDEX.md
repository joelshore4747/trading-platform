# EXPERIMENT_INDEX

Every measured run recorded in `docs/ROADMAP.md` and ADRs, in charter §42 shape. Statuses
are honest: this repository has produced **no VALIDATED strategy**. All VERIFIED from the
roadmap/ADR text; reproduce commands named where they exist.

| id | Hypothesis | Dataset | Method | Result | Status | Reproduce |
|---|---|---|---|---|---|---|
| E-01 | SMACrossover has an edge on mixed synthetic regimes | old generator (pre-calibration) | rolling WF, 16-cell grid | efficiency 0.86 / anchored 1.86 — **retracted** (generator Sharpe 6.36) | RETIRED | superseded |
| E-02 | SMACrossover on pure chop should fail | old generator | rolling WF | efficiency **1.24** — a false pass → multi-verdict design | RETIRED (finding kept) | `examples/run_walk_forward.py` |
| E-03 | Same, calibrated generator, 10 bp | calibrated | rolling WF | efficiency 0.27, stability 0%, chop control 0.18 | REJECTED | `examples/run_walk_forward.py` |
| E-04 | Pipeline false-discovery rate on white noise | `Regime.NULL` + Gaussian | 100 trials/row | 1%@4,800 OOS bars; 7–8%@<1,000 | COMPLETED (ADR 0003) | `scripts/measure_null.py` |
| E-05 | Pipeline detection floor | synthetic edges | oracle-ish sweep | Sharpe 4.93 found 5%; 18.7 always — above generator ceiling | COMPLETED (ADR 0003) | same |
| E-06 | Stablecoin depeg effect on reported figures | real BTCUSDT + simulated depeg to 0.8789 | metrics compare | return unchanged; vol 20.07→54.61% | COMPLETED (ADR 0002) | `scripts/measure_depeg.py` |
| E-07 | Point-in-time universe grows correctly | Coin Metrics 38 assets | registry | 1 asset 2011 → 38 in 2024; MKR delisting exercised | COMPLETED | `examples/run_universe.py` |
| E-08 | CrossSectionalMomentum detection vs IC | synthetic, 12 symbols, persistence sweep | WF 9-cell, 5 seeds/row | turns on IC ≈0.05; real CS momentum 0.02–0.05 | PROMISING-as-probe / strategy EXPERIMENT (ADR 0005) | generator not committed; a reconstruction lives in `scripts/measure_plateau.py` (E-015) |
| E-09 | Agents catch known defects | `evals/` 8 mutants | headless `claude -p`, judge | 8/8 identified after confound fix; controls clean | COMPLETED (ADR 0006) | `scripts/run_agent_eval.py` |
| E-10 | Suite catches known defects | `evals/` 11 mutants | pytest per mutant | 10/11 killed; L02 hole found & closed | COMPLETED, in CI | `scripts/run_mutation_eval.py` |
| E-11 | SMACrossover on REAL BTCUSDT 1m | 21,158 bars, 46% cov | rolling WF 18 folds, 16 cells | first run: fiction (35a); re-run: **in-sample Sharpe −2.52**, 6 verdicts | REJECTED | `examples/run_walk_forward_btc.py` |
| E-013 | Archive gaps are activity-conditioned (CR-001) | 39 perps, 1m, 824,304 bars | edge-vs-hour-matched null (1,000 draws); day coverage vs variance shuffle | 38/39 inside both bands; rho −0.026 in band; 98.8% of gaps shared by all symbols | REJECTED | `scripts/measure_gap_bias.py --seed 0` |
| E-014 | BTC 1m return leads the alt cross-section (CR-002 stage 1) | 39 perps, intersection panel, 20,856 rows | Spearman on ranks, within-segment circular-shift null | S(+1) +0.0016 in band; lag 0 +0.43; +0.16 bp vs 26 bp cost | REJECTED | `scripts/measure_leadlag.py --seed 0` |
| E-015 | Plateau-aware stability vs the current gate (CR-003) | synthetic, 12 symbols, AR(1) drift, 5 persistence rows x 10 seeds | walk-forward, offline plateau statistic, both arms | 0/10 vs 0/10 at null; 5/6 stability-only failures recovered on strong edges | PROMISING (ADR 0009 proposed) | `scripts/measure_plateau.py --seeds 10` |
| E-12 | Gauntlet finds what single audits miss | E-11's result | 41 agents, adversarial verify | 27 confirmed / 8 refuted; found 35a | COMPLETED | `gauntlet` workflow |

**Canonical records since 2026-09-02: `harness/experiments/E-NNN-*.toml`** (validated by
`scripts/experiment.py`; results frozen once committed). This table is the human index; the
twelve rows above were backfilled as E-001…E-012 with `backfilled = true` and `commit` /
`dataset_version` = `"UNKNOWN"`, because the roadmap never recorded them. New experiments
start from `scripts/experiment.py new` and add a row here.

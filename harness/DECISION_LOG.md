# DECISION_LOG

**Canonical location: `docs/adr/`.** All six records below are **VERIFIED** Accepted ADRs (status line in each file); the 2026-09-01 decisions are **VERIFIED** user choices, recorded as ADR 0007. This file is an index in the charter's §100 shape; the
records themselves are not duplicated here (charter §112: one source of truth).

| # | Decision | Problem | Evidence | Consequence | Revisit when |
|---|---|---|---|---|---|
| 0001 | Orders fill on the next bar at its open | same-bar fills are lookahead | loop ordering; 17 guards | every number rests on it | never — it is the prime directive |
| 0002 | A quote currency is a traded asset; rate never assumed | USDT reported as USD | depeg: vol 20→55%, return unchanged | `core/fx.py` refuses; `assume_pegged()` visible | multi-currency portfolio needed |
| 0003 | False-discovery rate is governed by OOS evidence, not fold count | fold claim was confounded | 4,800 OOS bars → 1%; <1,000 → 7–8% | verdicts need ≥ ~1,000 OOS bars; 60 seeds minimum | detector improves |
| 0004 | Count trials, report deflation; PBO deleted | multiplicity uncorrected | deflated Sharpe on 144-cell grid | every sweep reports trials | — |
| 0005 | The fill gate was never the binding one; probe mattered | SMACrossover trades too little to reach the gates | ranker: 46 fills/100 bars; detection near IC 0.05 | any detection claim names its probe | oracle arm run |
| 0006 | The defences were never measured | prose ≠ control | `evals/`: 10/11 killed, 8/8 identified, controls held | mutation eval in CI; agent eval opt-in | every agent edit |

## Decisions taken 2026-09-01 (this audit) — ADR 0007
| Decision | Options | Chosen | Reason | Revisit when |
|---|---|---|---|---|
| Governance | CLAUDE.md governs / charter governs / decide after audit | **charter governs** | user's call; falsification kept via charter §81/§103/§121 | if scope widening precedes a validated strategy for >2 quarters |
| Ledger currency | USD + GBP display / GBP ledger / multi-currency | **USD + GBP display** | venues are USD/USDT; ADR 0002 | UK tax reporting or GBP venue |
| Audit location | /harness + pointers / move docs / docs only | **/harness + pointers** | §112 | — |
| Margin model (35a) | commission-only / initial margin / spot-only | **initial margin** | leverage must be modelled to be bounded | funding feed lands |
| Phase order | adapter before supervisor / supervisor first | **provable first** | adapter unverifiable here | VPS available |

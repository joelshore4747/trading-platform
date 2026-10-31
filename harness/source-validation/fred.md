# source-validation — FRED / ALFRED (Federal Reserve Bank of St. Louis)

Written before any data from this source informs anything. **UNVERIFIED against
the live API**: `api.stlouisfed.org` answers 403 from the sandbox this was
written in, so everything below is from the published reference and the first
real response is the verification.

| question | answer | status |
|---|---|---|
| Who publishes it | Federal Reserve Bank of St. Louis, aggregating ~800k series from BLS, BEA, Census, Treasury and others | VERIFIED (documented) |
| What is the primary source | **Usually not FRED.** FRED is an aggregator; CPI is BLS's, GDP is BEA's. A discrepancy is resolved at the originating agency, not here | VERIFIED (documented) |
| Incentive to mislead | None apparent. A public research service with no commercial position in the data | INFERRED |
| Revision policy | **Series-specific and material.** GDP is revised twice within three months and again in annual benchmarks; CPI is revised for two months then annually; UNRATE is revised with the annual seasonal-factor update. Recorded per series in `macro_series.revision_policy` | VERIFIED (documented) |
| Vintages available | **Yes — this is why FRED rather than another aggregator.** ALFRED serves every value a series has ever had, keyed by `realtime_start`. Without it a macro backtest is lookahead by construction | VERIFIED (documented) |
| Release schedule | Per series. Most US macro releases are 08:30 New York | VERIFIED (documented) |
| Release *time* in the API | **No.** The API gives a `realtime_start` DATE and no time of day. So a value with no separately recorded release time is treated as available at the END of its vintage day — conservative, and stated rather than assumed. Where a precise time is known it is recorded in `macro_observations.released_at` | VERIFIED (code) |
| Reachability | 403 from this sandbox, along with every other external host. Runs on the VPS | VERIFIED (measured) |
| Credential | `FRED_API_KEY`, free, from the environment only. Goes in the query string because the API requires it there, and the URL is redacted anywhere it might be printed | VERIFIED (code) |
| Contradicted by | Not yet compared against BLS or BEA directly. When it is, a disagreement is a finding about one of them, not a tie to break | UNKNOWN |

## What could go wrong, and what stops it

**Reading today's figure for a past period.** The whole reason this source is
stored by vintage. `store/macro.read_as_of` returns the most recent vintage
*available* at a given instant; `read_latest` exists for reporting and is named
to be hard to misuse.

**Reading a figure the morning it was published.** A vintage is a date, not an
instant. Handled by treating an unstamped value as available at the end of its
vintage day.

**Mining.** ~800k series and a few hundred strategies means something always
correlates. A strategy may only read a series its pre-registered `Trial` named
in advance (`store/macro.SeriesGrant`), and the registry is append-only.

**Silent truncation.** A realtime range narrower than all of history returns a
subset that looks like a complete series. `scripts/fetch_macro.py` always asks
for the full range and says so.

**A rejected request looking like an empty series.** A response with no
`observations` key raises rather than returning `[]`.

## Fitness

Fit for macro research **once a hypothesis names the series**, and only through
`read_as_of`. Not fit for anything intraday: the finest resolution here is
daily, most series are monthly or quarterly, and a strategy that reads a monthly
series on every 1-minute bar is reading one number thirty thousand times.

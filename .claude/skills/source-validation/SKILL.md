---
name: source-validation
description: Evaluate an external source — a data vendor, an API, a paper, a claim from the web — for provenance, reachability, incentive, contradiction and fitness before anything from it enters a dataset or a research conclusion. Use before adding a data source or citing an external fact as evidence.
---

# Source validation

## purpose
A number from a source you cannot vouch for is a rumour with a decimal point.
This skill decides whether a source may be cited as VERIFIED, INFERRED, or must
stay UNKNOWN.

## when_to_use
Before `adding-a-data-source`; before a `market-researcher` or
`creative-researcher` brief cites a fact; when two sources disagree; when a
vendor changes its schema or terms.

## inputs
The source (URL, vendor, paper, dataset id); the claim it is being used to
support; the date the claim matters for.

## procedure
1. **Provenance.** Who produced it, from what raw data, when. Primary (the
   venue's own API), secondary (a vendor's redistribution), or tertiary (a post
   about a vendor's data)?
2. **Reachability.** Can it be fetched from where the code runs? This sandbox
   returns `403 to CONNECT` for every market-data host, so reachability here is
   UNKNOWN until tested on the VPS. Record where it was tested.
3. **Incentive.** What does the publisher gain if the claim is believed? A
   strategy vendor's backtest and an exchange's fee schedule carry different weight.
4. **Contradiction.** Has anyone with access to the same data said otherwise?
   Search for it; absence of contradiction after a search is INFERRED, not VERIFIED.
5. **Resolution and stamping.** Bar interval, open- vs close-stamped, timezone,
   adjusted vs raw prices, survivorship (does the universe include delisted
   names?). The D01 defect was an open-stamped file read as close-stamped.
6. **Stability.** Terms of use, rate limits, schema history, known outages.
7. **Fitness.** Does it answer the question at the resolution the question
   needs? Daily stablecoin rates understate an intraday depeg 4.4x (`core/fx.py`).

## tools
`WebFetch`/`WebSearch` where permitted; `data/connectors.py`;
`harness/EXTERNAL_DATA_MAP.md`; `core/credentials.py` for anything needing a key.

## validation
The source can be re-fetched and yields the same values for the same query. Its
stamping convention was checked on ten rows by hand. Its coverage fraction over
the window is a number.

## outputs
A source card for `harness/EXTERNAL_DATA_MAP.md`: name / type / provenance /
reachability (where tested) / stamping / resolution / coverage / incentive /
contradictions / fitness / classification / date checked.

## failure_modes
Citing a source from memory. Treating a vendor's description of its data as a
check of the data. Assuming reachable-on-laptop means reachable-in-production.
Letting an adjusted price series into a backtest without saying so.

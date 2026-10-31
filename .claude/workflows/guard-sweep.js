export const meta = {
  name: 'guard-sweep',
  description: 'Sweep the test suite for guards that cannot fail, proving each by mutation',
  whenToUse: 'After a suite goes green on a change that should have broken something, or before trusting a property because a test claims to cover it. This is the most repeated defect class here, and the fixed corpus in evals/ covers only what has already happened.',
  phases: [
    { title: 'Partition', detail: 'group the suite into reviewable units' },
    { title: 'Sweep', detail: 'guard-auditor reads each unit for assertions that cannot discriminate' },
    { title: 'Prove', detail: 'mutate the covered code and confirm the test stays green' },
  ],
}

const SUSPECTS = {
  type: 'object',
  required: ['suspects'],
  properties: {
    suspects: {
      type: 'array',
      items: {
        type: 'object',
        required: ['test', 'claim', 'mutation'],
        properties: {
          test: { type: 'string', description: 'tests/test_x.py::test_name' },
          claim: { type: 'string', description: 'the property a reader would believe is covered' },
          mutation: {
            type: 'string',
            description: 'the concrete source edit that should make this test fail',
          },
          pattern: { type: 'string' },
        },
      },
    },
  },
}

const PROOF = {
  type: 'object',
  required: ['stayed_green', 'evidence'],
  properties: {
    stayed_green: {
      type: 'boolean',
      description: 'true = the test passed with the property broken = confirmed fake guard',
    },
    evidence: { type: 'string', description: 'the actual pytest output, both runs' },
    note: { type: 'string' },
  },
}

// Explicit list rather than a glob so a new test file is a deliberate addition
// here. The corpus in evals/ covers defects that already happened; this covers
// the rest of the suite, and silently skipping a file would read as coverage.
const units = Array.isArray(args) && args.length
  ? args
  : [
      'tests/test_lookahead.py',
      'tests/test_risk.py',
      'tests/test_risk_composition.py',
      'tests/test_engine.py',
      'tests/test_portfolio.py',
      'tests/test_walkforward.py',
      'tests/test_windows.py',
      'tests/test_universe.py',
      'tests/test_panel.py',
      'tests/test_nulltest.py',
      'tests/test_cross_section.py',
      'tests/test_vendor.py',
    ]

phase('Partition')
log(`sweeping ${units.length} test file(s)`)

const swept = await pipeline(
  units,
  (unit) =>
    agent(
      `Read ${unit} in full, and the source it covers.\n\n` +
        `For each test, treat the NAME as a claim and the BODY as the evidence for it. ` +
        `Report only tests where breaking the named property would leave the test green. ` +
        `For each, give the exact source edit that would demonstrate it.\n\n` +
        `Do not report weak-looking assertions that are correct for what the test actually ` +
        `claims. Precision is the gate here: a sweep that flags every 'assert x is not None' ` +
        `trains its reader to ignore it, which is the same failure one level up.`,
      { agentType: 'guard-auditor', label: `sweep:${unit.split('/').pop()}`, phase: 'Sweep', schema: SUSPECTS }
    ),
  (report, unit) => {
    if (!report || !report.suspects || report.suspects.length === 0) return []
    // A suspicion nobody tested is a suspicion. Each one is proved or dropped.
    return parallel(
      report.suspects.map((s) => () =>
        agent(
          `Prove or disprove that this test cannot fail.\n\n` +
            `Test: ${s.test}\nClaims: ${s.claim}\nProposed mutation: ${s.mutation}\n\n` +
            `Work in a scratch copy — copy the repo to a temp directory and use ` +
            `PYTHONPATH=<copy>/src so the real tree is never modified. Then:\n` +
            `  1. run the test unmutated and confirm it passes\n` +
            `  2. apply the mutation\n` +
            `  3. run it again\n\n` +
            `stayed_green=true means the test passed with the property broken — a confirmed ` +
            `fake guard. If the mutation makes it fail, the test is fine: say so, that is a ` +
            `correct and useful result. Paste both pytest outputs as evidence.`,
          { label: `prove:${s.test.split('::').pop()}`, phase: 'Prove', schema: PROOF }
        ).then((p) => ({ ...s, unit, ...(p || { stayed_green: false, evidence: 'proof failed' }) }))
      )
    )
  }
)

const all = swept.flat().filter(Boolean)
const confirmed = all.filter((s) => s.stayed_green)

log(`${all.length} suspect(s) tested, ${confirmed.length} confirmed`)

return {
  confirmed: confirmed.map((s) => ({
    test: s.test,
    claim: s.claim,
    mutation: s.mutation,
    evidence: s.evidence,
  })),
  cleared: all.filter((s) => !s.stayed_green).map((s) => s.test),
  units_swept: units,
  note:
    'Confirmed entries are candidates for evals/defects/ — a fake guard found once should ' +
    'become a corpus entry so it cannot come back unnoticed.',
}

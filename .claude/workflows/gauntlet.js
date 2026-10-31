export const meta = {
  name: 'gauntlet',
  description: 'Run every audit a result must survive before it is believed, then attack the synthesis',
  whenToUse: 'Before any backtest number informs a decision. Roadmap step 35 made executable: docs/AI_WORKFLOW.md section 4 describes this loop in prose and nothing ran it.',
  phases: [
    { title: 'Audit', detail: 'lookahead, risk, overfitting, strategy and guard reviews in parallel' },
    { title: 'Verify', detail: 'each finding attacked by an independent skeptic' },
    { title: 'Pre-mortem', detail: 'assume the result is wrong; say how' },
    { title: 'File', detail: 'every surviving finding becomes a ticket, without transcription' },
  ],
}

// The subject: whatever the caller passed, or the working tree's changes.
const subject =
  typeof args === 'string' && args.trim()
    ? args.trim()
    : 'the current working-tree changes (use `git diff` and `git status` to find them)'

const FINDINGS = {
  type: 'object',
  required: ['verdict', 'findings'],
  properties: {
    verdict: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'file', 'why', 'confirmed'],
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'integer' },
          why: { type: 'string', description: 'the mechanism, not the category' },
          confirmed: { type: 'boolean', description: 'true only if a test was run and shown' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
        },
      },
    },
  },
}

const VERDICT = {
  type: 'object',
  required: ['survives', 'why'],
  properties: {
    survives: { type: 'boolean', description: 'false when the finding is refuted' },
    why: { type: 'string' },
  },
}

// Each lens is a distinct failure mode, not a redundant re-read. CLAUDE.md
// names lookahead as the one bug that matters; the risk layer has produced
// seven gaps in one audit; and the guard lens exists because a green suite has
// four times been the thing hiding the defect.
const LENSES = [
  {
    key: 'lookahead',
    agent: 'lookahead-auditor',
    prompt:
      `Audit ${subject} for lookahead bias and any other route by which future ` +
      `information reaches a decision. Work every category in your brief. Prove what you can.`,
  },
  {
    key: 'risk',
    agent: 'risk-auditor',
    prompt:
      `Audit ${subject} for reachable risk-limit gaps. Composition first: several orders ` +
      `within one bar, a limit interacting with the halt, an equity-relative cap after a drawdown.`,
  },
  {
    key: 'overfitting',
    agent: 'overfitting-auditor',
    prompt:
      `Audit ${subject} as an evaluation. Was the reported window ever fitted on, how many ` +
      `free parameters against how many bars, and what are the fills per fold?`,
  },
  {
    key: 'guards',
    agent: 'guard-auditor',
    prompt:
      `Audit the tests covering ${subject}. Which of them cannot fail? Prove it by mutation ` +
      `in a scratch copy and paste the pytest output.`,
  },
  {
    key: 'strategy',
    agent: 'strategy-reviewer',
    prompt: `Review ${subject} as a strategy. Is there a stated mechanism, and is it plausible?`,
  },
]

phase('Audit')

// Pipeline, not a barrier: a lookahead finding starts being attacked while the
// overfitting audit is still reading. Nothing here needs the other lenses'
// results to proceed.
const audited = await pipeline(
  LENSES,
  (lens) =>
    agent(lens.prompt, {
      agentType: lens.agent,
      label: `audit:${lens.key}`,
      phase: 'Audit',
      schema: FINDINGS,
    }),
  (report, lens) => {
    if (!report || !report.findings || report.findings.length === 0) {
      return { lens: lens.key, verdict: report ? report.verdict : 'no report', confirmed: [] }
    }
    // Every finding gets an independent skeptic whose job is to refute it. An
    // audit that is never contradicted is an audit nobody checked.
    return parallel(
      report.findings.map((f) => () =>
        agent(
          `A reviewer claims this defect exists in ${f.file}:\n\n${f.title}\n${f.why}\n\n` +
            `Try to REFUTE it. Read the code. Run something if you can. ` +
            `Default to survives=false if you cannot substantiate the claim — ` +
            `a plausible-sounding finding that nobody could confirm is noise, and noise ` +
            `in an audit report is how real findings get ignored.`,
          { label: `verify:${lens.key}:${f.file}`, phase: 'Verify', schema: VERDICT }
        ).then((v) => ({ ...f, lens: lens.key, refuted: !v || !v.survives, why_verdict: v?.why }))
      )
    ).then((checked) => ({
      lens: lens.key,
      verdict: report.verdict,
      confirmed: checked.filter(Boolean).filter((f) => !f.refuted),
      refuted: checked.filter(Boolean).filter((f) => f.refuted),
    }))
  }
)

const lanes = audited.filter(Boolean)
const confirmed = lanes.flatMap((l) => l.confirmed || [])
const refuted = lanes.flatMap((l) => l.refuted || [])

log(`${confirmed.length} finding(s) survived verification, ${refuted.length} refuted`)

phase('Pre-mortem')

// Runs whatever the audits said. A clean sweep is exactly when a pre-mortem is
// most valuable: this repository's prime directive is that the default outcome
// of a strategy is "this does not work", so five clean audits are a claim that
// needs attacking rather than a result to celebrate.
const premortem = await agent(
  `It is six months from now and ${subject} turned out to be wrong. ` +
    `Write the post-mortem.\n\n` +
    `Audits already run returned ${confirmed.length} confirmed finding(s):\n` +
    (confirmed.map((f) => `  - [${f.lens}] ${f.file}: ${f.title}`).join('\n') || '  (none)') +
    `\n\nDo not repeat them. Name the failure they would NOT have caught — the premise ` +
    `nobody questioned, the check that could not have fired, the data property assumed ` +
    `rather than measured. Then state the cheapest test that would settle each one now.`,
  { label: 'pre-mortem', phase: 'Pre-mortem' }
)

phase('File')

// The weakest link in this harness has always been the last one: a real finding
// reported into a transcript, and existing only for as long as somebody copied
// it into markdown by hand. Only CONFIRMED findings are filed — the refuted ones
// stay in this run's output, because a log full of noise is a log nobody reads.
const SEVERITY = { critical: 'P0', high: 'P1', medium: 'P2', low: 'P3' }
const toFile = confirmed.map((f) => ({
  kind: 'defect',
  severity: SEVERITY[f.severity] || 'P2',
  title: f.title,
  path: f.file,
  line: f.line,
  detail: f.why,
  source: `gauntlet:${f.lens}`,
}))

const filed =
  toFile.length === 0
    ? { filed: [], note: 'nothing survived verification, so nothing was filed' }
    : await agent(
        `File these ${toFile.length} verified finding(s) as tickets. Run the CLI once per ` +
          `finding and report the ids it prints back. Change nothing else.\n\n` +
          `For each object below:\n\n` +
          '```bash\n' +
          `.venv/bin/python scripts/ticket.py file --kind defect --severity <severity> \\\n` +
          `  --title "<title>" --path <path> [--line <line>] \\\n` +
          `  --repro "<the command or test that shows it; REQUIRED — a defect is refused ` +
          `without one>" \\\n` +
          `  --detail "<detail>" --source "<source>"\n` +
          '```\n\n' +
          `The reproduction is not optional and must be a real command or test, taken from ` +
          `the verification that confirmed the finding. If a finding has no reproduction you ` +
          `can state, file it as --kind question instead and say so: a defect nobody can ` +
          `reproduce is an opinion, and this repository has been wrong often enough to know ` +
          `the difference.\n\n` +
          `Then run \`.venv/bin/python scripts/ticket.py debt\` to regenerate ` +
          `harness/TECHNICAL_DEBT.md.\n\n` +
          JSON.stringify(toFile, null, 2),
        { agentType: 'mechanical-engineer', label: 'file-tickets', phase: 'File' }
      )

log(`${toFile.length} confirmed finding(s) handed to the ticket log`)

return {
  subject,
  confirmed,
  refuted: refuted.map((f) => ({ file: f.file, title: f.title, why: f.why_verdict })),
  lens_verdicts: lanes.map((l) => ({ lens: l.lens, verdict: l.verdict })),
  premortem,
  filed,
}

---
name: agent-evaluation
description: Measure an agent, prompt, model or configuration against the defect corpus in evals/ (charter §70) and record which configuration performed best. Use after changing any agent brief, adding an agent, or before trusting an auditor's clean verdict on something that matters.
---

# Agent evaluation (charter §70)

## purpose
Agents are prose, and prose is not a control. This skill wraps the existing
`evals/` harness so a change to an agent is measured rather than assumed.

## when_to_use
A brief in `.claude/agents/` changed; a new agent was added; a model was switched;
a workflow was edited; an auditor returned clean on a change that should have
tripped it.

## inputs
The agent name; the corpus ids it is expected to catch (`expected_agent` in
`evals/defects/*.toml`); a budget (each agent run costs real money and ~10–40 min).

## procedure
1. **Level 1 first, free**: `.venv/bin/python scripts/run_mutation_eval.py`.
   Confirms the corpus still applies and the suite still kills 10/11 (G01 is the
   declared survivor). An ERROR is never a pass.
2. **Plan the agent run**: `scripts/run_agent_eval.py --dry-run` shows routing and
   cost. Say the cost before spending it.
3. **Run with controls**: `--id <ID> --controls 1`. The control is an unmutated
   tree; a flag there is either a false positive or a real defect — adjudicate,
   do not assume. Both controls flagged so far were real (A01, 9b).
4. **Judge with a control**: the judge is cross-checked against a mismatched
   defect; a judge that "identifies" the wrong defect is measuring itself.
5. **Compare configurations** one variable at a time: model, prompt, tools. The
   two runs recorded in ADR 0006 differed by the harness's own defect both times.
6. **Record** the result in `harness/AGENT_PROTOCOL.md` (the Measured column) and
   the agent log (`scripts/agent_log.py`).

## tools
`evals/` (corpus, mutate, suite, agent, judge), `scripts/run_mutation_eval.py`,
`scripts/run_agent_eval.py`, `guard-sweep` and `gauntlet` workflows.

## validation
The run can report both outcomes: the control comes back clean or adjudicated,
the mutant comes back identified. A configuration that flags everything scores
100% on detection and fails the control; report both numbers or neither.

## outputs
Per id: identified / missed / flagged-on-control; per configuration: the pair of
numbers; a one-line recommendation of which configuration to keep. Corpus
additions for any defect the run found that the corpus lacked.

## failure_modes
Editing a corpus entry to make an agent pass. Withholding Bash from an auditor
told to prove findings. Sharing a venv between mutant and tree. Reading a
survivor as "the test suite is fine". Running the whole corpus without a budget.

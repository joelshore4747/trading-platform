# agent-log

Charter §68. One JSONL file per UTC day; one line per meaningful agent action, appended by
`scripts/agent_log.py`. Fields, in charter order: agent · model · timestamp · request · tools ·
data_sources · files_changed · experiments_run · tests_run · results · decision · confidence.

Append-only. Nothing here is ever edited or deleted; a wrong record is corrected by a later
record that says so. The agent name must match a brief in `.claude/agents/`, so the log cannot
name an agent that does not exist.

Read a day: `.venv/bin/python -c "from scripts.agent_log import read_log; ..."` or `jq -c . harness/agent-log/<date>.jsonl`.

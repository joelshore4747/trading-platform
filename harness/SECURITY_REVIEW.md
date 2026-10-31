# SECURITY_REVIEW

Scope, corrected 2026-09-08. This was "a library with no network listener, no auth, no
uploads, no database". Two of those four stopped being true and the line was not revisited:

- **There is a listener.** `scripts/run_ops.py` binds a socket (`ThreadingHTTPServer`,
  default `127.0.0.1:8099`).
- **There is a database.** Postgres, seven migrations, since ADR 0010.
- **There is still no auth**, and that now matters rather than being N/A. The control is
  positional, not cryptographic: the page binds loopback and `run_ops.py` refuses any other
  interface without `--i-mean-it`, because it shows account equity, open positions and every
  filed defect. Off-machine access is an SSH tunnel. Anyone who can reach the port can read
  all of it, and anyone who can run a process on the host can bind nothing to stop them.
- **There are still no uploads**, and no user-supplied input reaches the page at all: it is
  `GET /` and a 404 for everything else, with every value passed through `html.escape`.

The rest of §37 remains **N/A-ABSENT** and is listed as such rather than scored.

## What exists — VERIFIED
| Control | Evidence |
|---|---|
| No live trading path | `brokers/base.py`: every adapter raises `BrokerNotImplementedError`; `is_live` False on all; `tests/test_connectors.py` guards it |
| Credentials env-only, redacted, fail-loud | `core/credentials.py`; blank == absent |
| `.env*` unreadable by the agent | `.claude/settings.json` deny: `Read(./.env)`, `.env.*`, `secrets/**`, `*.pem`, `*.key`, `id_rsa*` |
| Credential-shaped writes blocked | `.claude/hooks/block-secrets.sh` (PreToolUse on Write/Edit) |
| Destructive git/shell denied | deny: `git push --force`, `reset --hard`, `clean -fdx`, `filter-branch`, `rm -rf`, `sudo`, installing `ccxt` |
| Committed artefacts check | CI step refuses tracked `.parquet/.env/runs/` |
| Read-only scopes policy | `.env.example` header; withdrawal permission "NEVER" |

## Charter §69 agent-safety list vs current enforcement
| Must never silently… | Enforced? |
|---|---|
| change fees / starting capital / slippage | **Hook** (`check-defaults.sh`, PostToolUse): cautions when a `CostModel`, `RiskLimits`, `starting_cash` or `initial_margin_rate` default line changes; never blocks. Tested both ways (`tests/test_check_defaults_hook.py`). Closed F-SEC-1 on 2026-09-01 |
| change historical data | ✅ `historical.zip` committed; cache gitignored; `write_bars` refuses corruption |
| delete experiments / accounts | N/A yet (no §42 records, no accounts) |
| rewrite research conclusions | ✅ culturally — roadmap keeps retractions; not mechanically enforced |
| enable live trading | ✅ structurally — nothing can place an order |
| remove safety controls | PARTIAL — `everything_disabled` exists for isolation; a hook could flag `RiskLimits(…=None)` outside `tests/` |

## Findings
**F-SEC-1 · P2 · VERIFIED · CLOSED 2026-09-01.** §69 prohibitions on fees/capital/
slippage are prose. Recommendation: a PostToolUse hook that diffs `engine/execution.py`
`CostModel` defaults and `risk/limits.py` `RiskLimits` defaults against HEAD and injects a
caution (never blocks — `prompt_guard`'s lesson). Verification: edit a default, observe the
note.

**F-SEC-2 · P3 · VERIFIED.** No agent action log (§68). The gauntlet writes per-agent
journals under the session dir, not the repo. PROPOSED: append agent/model/files-changed to
`live/journal`-style JSONL in `/harness/agent-log/`.

N/A-ABSENT: sessions, password reset, XSS, CSRF, SSRF, SQLi, IDOR, uploads, rate limiting,
webhooks — nothing to attack yet. Revisit at P6/P10.

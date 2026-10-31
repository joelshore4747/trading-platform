# Safety rules (charter §8, §69)

These are the actions an agent must never take silently. Some are enforced by hooks and the
permission deny-list in `.claude/settings.json`; the rest are enforced by this file, which
is weaker — so when one applies, **raise it and stop** rather than act.

## Four stages, kept apart (§8)
RESEARCH → SIMULATION → PAPER → LIVE. A strategy gains no execution authority by being
researched. A paper account never places a live order. Nothing moves toward LIVE without an
explicit human decision recorded as an ADR. Today: every broker adapter raises; `is_live` is
False everywhere. Setting any `is_live=True` is a LIVE-stage change.

## Never silently (§69)
| Action | Enforcement today |
|---|---|
| Change `CostModel` defaults (commission, slippage, spread, minimum) | **hook**: `check-defaults.sh` cautions on any diff to a default line |
| Change `RiskLimits` defaults or relax a limit | **hook**: same |
| Change `starting_cash` default or `initial_margin_rate` default | **hook**: same |
| Change or delete historical data (`historical.zip`, cache) | `write_bars` refuses corruption; archive committed; cache gitignored |
| Delete experiments, accounts, journal entries | prose — raise |
| Rewrite a research conclusion or retraction | prose — raise; `docs/ROADMAP.md` keeps retractions |
| Change strategy parameters in a reviewed result | prose — record as a new trial |
| Touch the event loop's ordering (`engine/backtester.py`) | plan-mode milestone; `tests/test_lookahead.py` guards |
| Enable live trading | structural — nothing can place an order |
| Remove a safety control (`RiskLimits(...=None)` outside tests) | prose — raise |
| Read `.env*`, keys, secrets | **deny-list** |
| `git push --force`, `reset --hard`, `rm -rf`, `sudo`, install `ccxt` | **deny-list** |
| Write a credential-shaped string | **hook**: `block-secrets.sh` blocks |

The hook **cautions, never blocks**: a guard that undoes your edit is switched off within a
day (`docs/PROMPTS.md`). The caution says which default moved and asks for the reason to be
stated in the response and the commit.

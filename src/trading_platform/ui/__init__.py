"""Verdict — the UI.

Every quant tool is a dashboard with forty knobs and a beautiful equity curve.
This one is built against that. Its product is the word "no", delivered fast,
with evidence — so the screen is a judgement, not a control panel: the verdict
at the top, the objections under it, the evidence under those.

The strongest outcome available is ``SURVIVES``, which means only that this run
failed to falsify an edge. There is deliberately no outcome meaning "this
works".

Pure Python, no extra dependency: it renders a verdict as text, not a web page.
The core package never imports this. (A ``ui`` extra once declared fastapi and
uvicorn here; nothing used them and it was removed on 2026-09-02.)
"""

from __future__ import annotations

from trading_platform.ui.runner import RunConfig, RunOutcome, execute
from trading_platform.ui.verdict import Outcome, Verdict, decide

__all__ = [
    "Outcome",
    "RunConfig",
    "RunOutcome",
    "Verdict",
    "decide",
    "execute",
]

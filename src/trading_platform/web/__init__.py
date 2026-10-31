"""The operations page. One page, one question: is this thing running.

Deliberately not a results page — see `ops.py` for why the roadmap's frontend
deferral is untouched by this.
"""

from trading_platform.web.ops import OpsSnapshot, read_snapshot, render_page

__all__ = ["OpsSnapshot", "read_snapshot", "render_page"]

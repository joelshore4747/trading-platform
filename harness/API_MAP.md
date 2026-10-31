# API_MAP — ABSENT

**Classification: VERIFIED ABSENT.**

Evidence: `grep -rn "fastapi\|FastAPI\|APIRouter\|uvicorn" src/ scripts/ examples/` → no
matches. No route, no endpoint, no serialiser, no auth middleware.

Corrected 2026-09-08: this said `pyproject.toml` declares `fastapi>=0.115` and
`uvicorn>=0.32` under an optional `ui` extra. **That extra was removed on 2026-09-02**
precisely because nothing imported it; `pyproject.toml` now carries only a comment recording
the deletion. The runtime dependency list is `pyarrow` and nothing else.

There is one HTTP listener, and it is not an API: `scripts/run_ops.py` serves a single
read-only HTML page from `http.server` (see `harness/FRONTEND_MAP.md`). It has one route, no
JSON, no content negotiation and no auth, and adding any of those would be the API this
document says is absent.

What exists instead: `src/trading_platform/ui/runner.py` — an in-process function that turns
a run configuration into a `Verdict` dataclass (`ui/verdict.py`). It is the seam an API
would call. Its docstring: "the UI can never flatter a run that the command line would use."

Charter §94 asks for domain-level APIs (market, assets, strategies, experiments, portfolios,
orders, monitors, research, intelligence). None can be mapped. Priority per `ROADMAP.md`:
**P6/P10, DEFERRED** behind the research-engine condition.

Finding (P3, VERIFIED): the unused optional dependency is dead weight and implies a
capability that does not exist. Recommend removing the `ui` extra until an API is built,
or renaming it `api` when one is.

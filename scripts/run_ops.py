"""Serve the operations page. One page, no dependencies, no build step.

    scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"
    .venv/bin/python scripts/run_ops.py            # http://127.0.0.1:8099
    .venv/bin/python scripts/run_ops.py --once     # print the HTML and exit

Binds to 127.0.0.1 by default and refuses anything else without `--i-mean-it`:
the page shows account equity, open positions and every defect anyone has filed,
and none of that belongs on 0.0.0.0. On a VPS, reach it through an SSH tunnel
(`ssh -L 8099:127.0.0.1:8099 you@host`) rather than opening a port.

Read-only. Nothing here can write to the store, place an order, or change a
setting; it runs SELECTs and returns HTML.
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_platform.store.connection import connect, is_configured
from trading_platform.web.ops import read_snapshot, render_page


def page(refresh_s: int) -> bytes:
    """One request: one connection, one read, one render.

    A connection per request rather than a pool: this page is looked at by one
    person a few times a minute, and a pooled connection that goes stale between
    views is a bug for no benefit.
    """
    with connect() as conn:
        snapshot = read_snapshot(conn)
    return render_page(snapshot, refresh_s=refresh_s).encode("utf-8")


def handler_for(refresh_s: int) -> type[BaseHTTPRequestHandler]:
    class Ops(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path not in ("/", "/index.html"):
                self.send_error(404, "one page, at /")
                return
            try:
                body = page(refresh_s)
            except Exception as exc:
                # Never render a partial page as a healthy one.
                body = (
                    f"<!doctype html><meta charset=utf-8><title>operations</title>"
                    f"<body style='font:15px monospace;padding:2rem'>"
                    f"<h1>PAGE FAILED</h1><p>{type(exc).__name__}: {exc}</p>"
                    f"<p>The collector may still be running — it needs no database.</p>"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            pass  # one viewer; the access log is noise

    return Ops


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--refresh", type=int, default=30, help="seconds between refreshes")
    parser.add_argument("--once", action="store_true", help="print the HTML and exit")
    parser.add_argument(
        "--i-mean-it",
        action="store_true",
        help="allow a non-loopback bind. This page shows account equity, open positions "
        "and every filed defect.",
    )
    args = parser.parse_args(argv)

    if not is_configured():
        raise SystemExit(
            "TRADING_PLATFORM_DSN is unset. This page reads the store; "
            'scripts/dev_postgres.sh start && eval "$(scripts/dev_postgres.sh env)"'
        )
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.i_mean_it:
        raise SystemExit(
            f"refusing to bind {args.host}: this page shows account equity, open positions "
            "and every defect anyone has filed. Use an SSH tunnel "
            "(ssh -L 8099:127.0.0.1:8099 you@host), or pass --i-mean-it."
        )

    if args.once:
        sys.stdout.write(page(args.refresh).decode("utf-8"))
        return 0

    server = ThreadingHTTPServer((args.host, args.port), handler_for(args.refresh))
    print(f"operations page on http://{args.host}:{args.port}  (ctrl-c to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

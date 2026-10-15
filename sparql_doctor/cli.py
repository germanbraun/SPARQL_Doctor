"""Command-line entry point: `sparql-doctor <endpoint-url> [options]`."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import platform
import shlex
import socket
import sys
import threading
import time
from typing import List

from . import __version__
from .checks import CHECK_IDS, CHECKS
from .model import CheckResult, Context, Status
from .report import render_json, render_markdown, render_text
from .transport import DEFAULT_USER_AGENT, Target, redact_headers

EPILOG = """examples:
  sparql-doctor https://query.wikidata.org/sparql
  sparql-doctor <your-endpoint-url> -H 'Authorization: Bearer ...' \\
      --service https://query.wikidata.org/sparql --markdown report.md
  sparql-doctor http://localhost:7200/repositories/kb --quick --only keepalive,query-length

Attach the --markdown (human) or --json (machine) output to support tickets.
Secrets in Authorization/Cookie headers and URL credentials are redacted from all reports.
"""


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="sparql-doctor", description="Diagnostic report for a SPARQL endpoint.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("endpoint", help="SPARQL endpoint URL (credentials as user:pass@host are supported)")
    p.add_argument("-H", "--header", action="append", default=[], metavar="'Name: value'",
                   help="extra request header, e.g. Authorization (repeatable)")
    p.add_argument("-A", "--user-agent", default=DEFAULT_USER_AGENT,
                   help="User-Agent for normal probes (default: %(default)s)")
    p.add_argument("--service", action="append", default=[], metavar="URL",
                   help="federation target to test via SERVICE from the endpoint (repeatable)")
    p.add_argument("--origin", default="https://example.org", help="Origin used for the CORS check")
    p.add_argument("--only", help=f"comma-separated checks to run ({','.join(CHECK_IDS)})")
    p.add_argument("--skip", help="comma-separated checks to skip")
    p.add_argument("--idle-max", type=float, default=75, metavar="SECONDS",
                   help="longest idle period to probe for keep-alive timeouts (default: %(default)s)")
    p.add_argument("--quick", action="store_true", help="shorter probes: --idle-max 20, smaller size limits")
    p.add_argument("--max-get", type=int, default=128 * 1024, metavar="CHARS",
                   help="largest GET query to try (default: %(default)s)")
    p.add_argument("--max-post", type=int, default=2 * 1024 * 1024, metavar="BYTES",
                   help="largest POST body to try (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=30, help="per-request timeout in seconds (default: %(default)s)")
    p.add_argument("--service-timeout", type=float, default=60, help="timeout for SERVICE probes (default: %(default)s)")
    p.add_argument("--delay", type=float, default=0.15, help="minimum gap between requests (default: %(default)s s)")
    p.add_argument("-k", "--insecure", action="store_true", help="do not verify TLS certificates")
    p.add_argument("--no-proxy", action="store_true", help="ignore HTTP(S)_PROXY environment variables")
    p.add_argument("--markdown", metavar="FILE", help="write a Markdown report (ideal for tickets)")
    p.add_argument("--json", metavar="FILE", help="write a JSON report ('-' for stdout)")
    p.add_argument("-v", "--verbose", action="store_true", help="show all findings, hints and tables")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--version", action="version", version=f"sparql-doctor {__version__}")
    a = p.parse_args(argv)
    if a.quick:
        a.idle_max = min(a.idle_max, 20)
        a.max_get = min(a.max_get, 32 * 1024)
        a.max_post = min(a.max_post, 256 * 1024)
    for opt in ("only", "skip"):
        val = getattr(a, opt)
        if val:
            ids = [x.strip() for x in val.split(",") if x.strip()]
            unknown = set(ids) - set(CHECK_IDS)
            if unknown:
                p.error(f"unknown check(s) for --{opt}: {', '.join(sorted(unknown))}")
            setattr(a, opt, ids)
    return a


def _headers(raw: List[str]) -> dict:
    out = {}
    for h in raw:
        if ":" not in h:
            raise SystemExit(f"invalid header {h!r}; expected 'Name: value'")
        k, v = h.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _safe_command(argv: List[str]) -> str:
    out, skip_next = [], False
    for i, arg in enumerate(argv):
        if skip_next:
            k = arg.split(":", 1)[0]
            out.append(f"{k}: ***" if k.strip().lower() in ("authorization", "cookie", "x-api-key") else arg)
            skip_next = False
            continue
        out.append(arg)
        skip_next = arg in ("-H", "--header")
    from .transport import redact_url
    return "sparql-doctor " + " ".join(shlex.quote(redact_url(x) if "://" in x else x) for x in out)


def run_checks(ctx: Context, selected: List[str], progress) -> List[CheckResult]:
    results = {}
    funcs = {cid: (title, fn) for cid, title, fn in CHECKS}

    def execute(cid: str) -> CheckResult:
        title, fn = funcs[cid]
        start = time.monotonic()
        try:
            res = fn(ctx)
        except Exception as e:  # noqa: BLE001 - one broken check must not kill the report
            res = CheckResult(cid, title)
            res.add(Status.FAIL, f"Check crashed: {type(e).__name__}: {e}", "Please report this as a sparql-doctor bug.")
            res.finalize()
        res.duration = time.monotonic() - start
        progress(res)
        return res

    order = [cid for cid in CHECK_IDS if cid in selected]
    if "connectivity" in order:
        results["connectivity"] = execute("connectivity")
        order.remove("connectivity")
        if ctx.notes.get("unreachable"):
            for cid in order:
                results[cid] = CheckResult(cid, funcs[cid][0]).skip("Endpoint unreachable (see Connectivity).")
            return [results[c] for c in CHECK_IDS if c in results]
        if ctx.notes.get("no_sparql"):
            # TLS/HTTP details are still useful; everything else would just repeat the same failure
            for cid in [c for c in order if c != "http"]:
                results[cid] = CheckResult(cid, funcs[cid][0]).skip("No SPARQL query succeeded (see Connectivity).")
                order.remove(cid)

    # The idle-timeout probe mostly sleeps; run it alongside the other checks.
    bg = None
    if "keepalive" in order:
        order.remove("keepalive")
        bg = threading.Thread(target=lambda: results.__setitem__("keepalive", execute("keepalive")), daemon=True)
        bg.start()
    for cid in order:
        results[cid] = execute(cid)
    if bg:
        bg.join()
    return [results[c] for c in CHECK_IDS if c in results]


def main(argv: List[str] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    a = parse_args(argv)
    color = sys.stdout.isatty() and not a.no_color and os.environ.get("NO_COLOR") is None
    try:
        target = Target(a.endpoint, headers=_headers(a.header), user_agent=a.user_agent, timeout=a.timeout,
                        insecure=a.insecure, use_proxy=not a.no_proxy, delay=a.delay)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    def log(msg: str) -> None:
        print(f"  … {msg}", file=sys.stderr, flush=True)

    ctx = Context(target=target, origin=a.origin, services=a.service, idle_max=a.idle_max, max_get=a.max_get,
                  max_post=a.max_post, service_timeout=a.service_timeout, log=log)
    selected = a.only or list(CHECK_IDS)
    if a.skip:
        selected = [c for c in selected if c not in a.skip]

    started = dt.datetime.now(dt.timezone.utc).astimezone()
    t0 = time.monotonic()
    print(f"sparql-doctor {__version__}: examining {target.safe_url}", file=sys.stderr)

    def progress(res: CheckResult) -> None:
        print(f"  {res.status.value.upper():4}  {res.title} ({res.duration:.1f}s)", file=sys.stderr, flush=True)

    try:
        results = run_checks(ctx, selected, progress)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    proxy = target.proxy()
    meta = {
        "tool_version": __version__,
        "endpoint": target.safe_url,
        "effective_endpoint": ctx.target.safe_url,
        "started": started.isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - t0, 1),
        "user_agent": target.user_agent,
        "headers": redact_headers(target.headers),
        "command": _safe_command(argv),
        "options": {"idle_max": a.idle_max, "max_get": a.max_get, "max_post": a.max_post, "timeout": a.timeout,
                    "origin": a.origin, "services": a.service, "insecure": a.insecure},
        "client": {"hostname": socket.gethostname(), "platform": platform.platform(terse=True),
                   "python": platform.python_version(), "proxy": f"{proxy[0]}:{proxy[1]}" if proxy else None},
    }
    print(file=sys.stderr)
    if a.json == "-":
        print(render_json(meta, results))
    else:
        print(render_text(meta, results, color=color, verbose=a.verbose))
    for path, render in ((a.json, render_json), (a.markdown, render_markdown)):
        if path and path != "-":
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(render(meta, results))
            print(f"Report written to {path}", file=sys.stderr)
    if any(r.status == Status.FAIL for r in results):
        return 2
    return 1 if any(r.status == Status.WARN for r in results) else 0

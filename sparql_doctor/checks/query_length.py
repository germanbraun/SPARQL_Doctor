"""Maximum query length for GET (URL length) and POST (body size).

Queries are padded with a SPARQL comment so they stay trivially cheap to evaluate.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from ..model import CheckResult, Context, Status
from ..transport import Response, Target, sparql_request
from .common import human_bytes

BASE = "ASK {}\n#"
GET_LADDER = [1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576]
POST_LADDER = [16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024, 4 * 1024 * 1024, 16 * 1024 * 1024]


def _padded(length: int) -> str:
    return BASE + "x" * max(0, length - len(BASE))


def _url_len(target: Target, qlen: int) -> int:
    return len(target.path_with({"query": _padded(qlen)}))


def _search(probe: Callable[[int], Response], ladder: List[int], limit: int, precision: int
            ) -> Tuple[Optional[int], Optional[int], Optional[Response], List[Tuple[int, str]]]:
    """Ladder up to `limit`, then bisect between last success and first failure."""
    log: List[Tuple[int, str]] = []
    ok_at: Optional[int] = None
    fail_at: Optional[int] = None
    fail_resp: Optional[Response] = None
    for size in [s for s in ladder if s <= limit] or [limit]:
        resp = probe(size)
        log.append((size, resp.describe()))
        if resp.ok:
            ok_at = size
        else:
            fail_at, fail_resp = size, resp
            break
    if ok_at is not None and fail_at is not None:
        lo, hi = ok_at, fail_at
        while hi - lo > precision:
            mid = (lo + hi) // 2
            resp = probe(mid)
            log.append((mid, resp.describe()))
            if resp.ok:
                lo = mid
            else:
                hi, fail_resp = mid, resp
        ok_at, fail_at = lo, hi
    return ok_at, fail_at, fail_resp, log


def run(ctx: Context) -> CheckResult:
    r = CheckResult("query-length", "Maximum query length")
    t = ctx.target
    tbl = r.table("Method", "Largest OK", "Smallest failing", "Failure")

    get_ok, get_fail, get_resp, get_log = _search(
        lambda n: sparql_request(t, _padded(n), "GET"), GET_LADDER, ctx.max_get, precision=128)
    r.data["get"] = {"max_ok_query_chars": get_ok, "first_fail_query_chars": get_fail,
                     "max_ok_url_chars": _url_len(t, get_ok) if get_ok else None, "log": get_log}
    tbl.rows.append(["GET (query chars)", f"{get_ok:,}" if get_ok else "-", f"{get_fail:,}" if get_fail else "none",
                     get_resp.describe() if get_resp else "-"])

    post_results = {}
    for method in ("POST", "POST-DIRECT"):
        ok, fail, resp, log = _search(lambda n, m=method: sparql_request(t, _padded(n), m), POST_LADDER,
                                      ctx.max_post, precision=max(4096, ctx.max_post // 64))
        post_results[method] = (ok, fail, resp)
        r.data[method.lower()] = {"max_ok_bytes": ok, "first_fail_bytes": fail, "log": log}
        tbl.rows.append([{"POST": "POST form (bytes)", "POST-DIRECT": "POST direct (bytes)"}[method],
                         human_bytes(ok) if ok else "-", human_bytes(fail) if fail else f"none ≤ {human_bytes(ctx.max_post)}",
                         resp.describe() if resp else "-"])

    # Findings - GET
    if get_ok is None:
        r.add(Status.FAIL, f"Even a {GET_LADDER[0]}-char GET query failed: {get_resp.describe() if get_resp else '?'}")
    elif get_fail is None:
        r.add(Status.PASS, f"GET accepts queries of at least {get_ok:,} chars (tested limit).")
    else:
        url = _url_len(t, get_ok)
        kind = get_resp.error_kind or get_resp.status
        sev = Status.WARN if get_ok < 4000 else Status.INFO
        r.add(sev, f"GET limit: queries up to ~{get_ok:,} chars (URL ≈{url:,} chars) work; "
                   f"{get_fail:,} chars fail with {get_resp.describe()}.",
              ("The failure is a connection reset rather than an HTTP 414 - clients see 'Connection reset by peer' "
               "instead of a clear error. " if kind in ("reset", "closed", "broken-pipe") else "")
              + f"Make sure clients switch to POST above ~{max(1000, int(get_ok * 0.8)):,} chars "
                "(RDF4J switches automatically above ~4 KB of URL).")

    # Findings - POST
    for method, (ok, fail, resp) in post_results.items():
        label = "POST (form)" if method == "POST" else "POST (direct)"
        if ok is None:
            r.add(Status.WARN, f"{label} failed even for {human_bytes(POST_LADDER[0])}: {resp.describe() if resp else '?'}")
        elif fail is None:
            r.add(Status.PASS, f"{label} accepts at least {human_bytes(ok)} (tested limit).")
        else:
            r.add(Status.WARN if ok < 1024 * 1024 else Status.INFO,
                  f"{label} limit: {human_bytes(ok)} works, {human_bytes(fail)} fails with {resp.describe()}.",
                  "Large generated queries (VALUES blocks, long FILTER IN lists) beyond this size will fail; "
                  "raise client_max_body_size / maxPostSize on the proxy or server, or batch the VALUES.")
    return r.finalize()

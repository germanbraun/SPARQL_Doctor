"""CORS: can a browser application on another origin query this endpoint?

Browsers send GET and form-encoded POST as "simple" requests (no preflight); only
direct POST (application/sparql-query) and requests with an Authorization header
need a successful OPTIONS preflight."""

from __future__ import annotations

from ..model import CheckResult, Context, Status
from ..transport import SPARQL_JSON, raw_request, sparql_request


def _allows_origin(acao, origin: str) -> bool:
    return acao is not None and (acao.strip() == "*" or acao.strip() == origin)


def run(ctx: Context) -> CheckResult:
    r = CheckResult("cors", "CORS")
    t = ctx.target
    origin = ctx.origin
    auth = "Authorization" in t.headers
    tbl = r.table("Request", "Status", "Allow-Origin", "Allow-Methods", "Allow-Headers", "Credentials")

    get = sparql_request(t, "ASK {}", "GET", headers={"Origin": origin})
    post = sparql_request(t, "ASK {}", "POST", headers={"Origin": origin})
    pre = raw_request(t, "OPTIONS", t.path_with(), {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type" + (",authorization" if auth else ""),
        "Accept": SPARQL_JSON,
    })
    for label, resp in (("GET (simple)", get), ("POST form (simple)", post),
                        ("OPTIONS preflight for direct POST", pre)):
        tbl.rows.append([label, resp.describe(), resp.header("Access-Control-Allow-Origin") or "-",
                         resp.header("Access-Control-Allow-Methods") or "-",
                         resp.header("Access-Control-Allow-Headers") or "-",
                         resp.header("Access-Control-Allow-Credentials") or "-"])

    get_ok = get.ok and _allows_origin(get.header("Access-Control-Allow-Origin"), origin)
    post_ok = post.ok and _allows_origin(post.header("Access-Control-Allow-Origin"), origin)
    methods = (pre.header("Access-Control-Allow-Methods") or "").upper()
    allow_h = (pre.header("Access-Control-Allow-Headers") or "").lower()
    # judged on status + headers only: some servers send a broken body on OPTIONS
    pre_ok = (pre.status is not None and 200 <= pre.status < 300
              and _allows_origin(pre.header("Access-Control-Allow-Origin"), origin)
              and ("POST" in methods or "*" in methods or not methods)
              and ("content-type" in allow_h or "*" in allow_h)
              and (not auth or "authorization" in allow_h or "*" in allow_h))

    r.data.update({"origin": origin, "get_allowed": get_ok, "form_post_allowed": post_ok, "preflight_allowed": pre_ok})
    if not get_ok and not post_ok:
        r.add(Status.INFO, f"CORS is not enabled for origin {origin}.",
              "Only matters for browser apps calling the endpoint directly from another origin; server-side clients "
              "(application backends, RDF4J, Jena) are unaffected.")
        return r.finalize()
    if get_ok and post_ok:
        r.add(Status.PASS, f"Browser apps on {origin} can query via GET and form POST.")
    else:
        missing = "form POST" if get_ok else "GET"
        r.add(Status.WARN, f"Browser apps on {origin} can use {'GET' if get_ok else 'form POST'} but not {missing}.",
              "Browser clients (e.g. YASGUI) switch to form POST for long queries; enable CORS for both methods.")
    if not pre_ok:
        why = pre.describe() if not (pre.status and 200 <= pre.status < 300) else "missing Allow-Origin/Allow-Headers"
        if auth:
            r.add(Status.WARN, f"The preflight fails ({why}), so authenticated browser requests are blocked.",
                  "Requests with an Authorization header always need a preflight. Allow OPTIONS and the "
                  "'Authorization' and 'Content-Type' headers in the CORS configuration.")
        else:
            r.add(Status.INFO, f"The preflight fails ({why}): browsers can use GET and form POST, "
                               f"but not direct POST (application/sparql-query) or custom headers.")
    if get.header("Access-Control-Allow-Origin") == "*" and get.header("Access-Control-Allow-Credentials") == "true":
        r.add(Status.WARN, "Allow-Origin '*' combined with Allow-Credentials 'true' is rejected by browsers.")
    return r.finalize()

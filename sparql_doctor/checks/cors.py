"""CORS: can a browser application on another origin query this endpoint?"""

from __future__ import annotations

from ..model import CheckResult, Context, Status
from ..transport import SPARQL_JSON, raw_request, sparql_request


def _allows_origin(acao, origin: str) -> bool:
    return acao is not None and (acao.strip() == "*" or acao.strip() == origin)


def run(ctx: Context) -> CheckResult:
    r = CheckResult("cors", "CORS")
    t = ctx.target
    origin = ctx.origin
    tbl = r.table("Request", "Status", "Allow-Origin", "Allow-Methods", "Allow-Headers", "Credentials")

    simple = sparql_request(t, "ASK {}", "GET", headers={"Origin": origin})
    pre = raw_request(t, "OPTIONS", t.path_with(), {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,accept" + (",authorization" if "Authorization" in t.headers else ""),
        "Accept": SPARQL_JSON,
    })
    for label, resp in (("GET with Origin", simple), ("OPTIONS preflight (POST)", pre)):
        tbl.rows.append([label, resp.describe(), resp.header("Access-Control-Allow-Origin") or "-",
                         resp.header("Access-Control-Allow-Methods") or "-",
                         resp.header("Access-Control-Allow-Headers") or "-",
                         resp.header("Access-Control-Allow-Credentials") or "-"])

    get_ok = simple.ok and _allows_origin(simple.header("Access-Control-Allow-Origin"), origin)
    pre_acao = pre.header("Access-Control-Allow-Origin")
    methods = (pre.header("Access-Control-Allow-Methods") or "").upper()
    allow_h = (pre.header("Access-Control-Allow-Headers") or "").lower()
    pre_ok = (pre.status is not None and 200 <= pre.status < 300 and _allows_origin(pre_acao, origin)
              and ("POST" in methods or "*" in methods or not methods)
              and ("content-type" in allow_h or "*" in allow_h))

    r.data.update({"origin": origin, "get_allowed": get_ok, "preflight_allowed": pre_ok})
    if get_ok and pre_ok:
        r.add(Status.PASS, f"Browser apps on {origin} can query via GET and POST.")
    elif get_ok:
        r.add(Status.WARN, f"Browser apps on {origin} can use GET, but the POST preflight is not allowed "
                           f"({pre.describe()}).",
              "Browser clients (e.g. YASGUI hitting the endpoint directly) switch to POST "
              "for long queries; these will fail with a CORS error. Allow OPTIONS and 'Content-Type' in the CORS config.")
    else:
        r.add(Status.INFO, f"CORS is not enabled for origin {origin}.",
              "Only matters for browser apps calling the endpoint directly from another origin; server-side clients "
              "(application backends, RDF4J, Jena) are unaffected.")
    if simple.header("Access-Control-Allow-Origin") == "*" and simple.header("Access-Control-Allow-Credentials") == "true":
        r.add(Status.WARN, "Allow-Origin '*' combined with Allow-Credentials 'true' is rejected by browsers.")
    if "Authorization" in t.headers and pre_ok and "authorization" not in allow_h and "*" not in allow_h:
        r.add(Status.WARN, "Preflight does not allow the 'Authorization' header - authenticated browser requests will fail.")
    return r.finalize()

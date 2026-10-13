"""User-Agent policy: does the endpoint reject generic or missing User-Agents?
(Wikidata / Wikimedia return 403 for library default UAs since 2024/25.)"""

from __future__ import annotations

from ..model import CheckResult, Context, Status
from ..transport import sparql_request
from .common import short

PROBE = "ASK {}"

AGENTS = [
    ("(no User-Agent header)", None),
    ("Java default", "Java/17.0.10"),
    ("Apache HttpClient default", "Apache-HttpClient/4.5.14 (Java/17.0.10)"),
    ("Python requests default", "python-requests/2.32.3"),
    ("curl default", "curl/8.7.1"),
    ("Browser", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"),
]

POLICY_LINKS = {
    "wikidata.org": "https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy",
    "wikipedia.org": "https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy",
    "wikimedia.org": "https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy",
}


def run(ctx: Context) -> CheckResult:
    r = CheckResult("user-agent", "User-Agent policy")
    t = ctx.target
    agents = AGENTS + [("Descriptive (this run)", t.user_agent)]
    tbl = r.table("Client", "User-Agent", "Result", "Body excerpt")
    results = []
    for label, ua in agents:
        resp = sparql_request(t, PROBE, "GET", user_agent=ua)
        results.append((label, ua, resp))
        tbl.rows.append([label, ua or "-", resp.describe(), "" if resp.ok else short(resp.text(200), 120)])

    descriptive_ok = results[-1][2].ok
    blocked = [(label, resp) for label, _, resp in results[:-1] if not resp.ok]
    limited = [label for label, _, resp in results if resp.status == 429]
    policy = next((url for dom, url in POLICY_LINKS.items() if t.host.endswith(dom)), None)

    if not descriptive_ok and len(blocked) == len(results) - 1:
        r.add(Status.FAIL if not any(x.ok for _, _, x in results) else Status.WARN,
              "Requests fail regardless of User-Agent - not a UA policy issue (see Connectivity).")
    elif blocked and descriptive_ok:
        names = ", ".join(f"{label} ({resp.describe()})" for label, resp in blocked)
        r.add(Status.WARN, f"Endpoint enforces a User-Agent policy - rejected: {names}.",
              "Configure the client with a descriptive User-Agent including tool name, version and a contact, e.g. "
              "'MyCompanyKG/1.4 (https://example.com; ops@example.com)'. Set it in the HTTP client configuration "
              "of your application or RDF library; for federated SERVICE calls the *endpoint's* outgoing UA matters, too."
              + (f" Policy: {policy}" if policy else ""))
    elif not blocked:
        r.add(Status.PASS, "No User-Agent policy detected - all tested User-Agents are accepted.")
    else:
        r.add(Status.WARN, "Mixed results: " + ", ".join(f"{label}: {resp.describe()}" for label, resp in blocked))
    if limited:
        r.add(Status.WARN, f"Rate limiting (429) observed for: {', '.join(limited)}.",
              "Respect Retry-After and reduce concurrency.")
    r.data["policy_url"] = policy
    return r.finalize()

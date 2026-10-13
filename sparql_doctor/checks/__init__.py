"""Check registry. Each check is `run(ctx) -> CheckResult`."""

from . import connectivity, cors, federation, formats, http_version, keepalive, query_length, user_agent

# Order matters: connectivity runs first (and may follow redirects / abort the rest);
# keepalive is long-running and is executed in the background by the runner.
CHECKS = [
    ("connectivity", "Connectivity & SPARQL protocol", connectivity.run),
    ("http", "HTTP version & TLS", http_version.run),
    ("keepalive", "Keep-alive & idle timeout", keepalive.run),
    ("user-agent", "User-Agent policy", user_agent.run),
    ("cors", "CORS", cors.run),
    ("formats", "Result formats", formats.run),
    ("query-length", "Maximum query length", query_length.run),
    ("federation", "Federation (SERVICE) reachability", federation.run),
]

CHECK_IDS = [c[0] for c in CHECKS]

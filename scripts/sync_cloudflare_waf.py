#!/usr/bin/env python3
"""Keep each zone's Cloudflare WAF host allowlist in sync with the live Traefik routers.

The zones have proxied wildcard DNS, so scanners reach the edge with any made-up
subdomain. One custom rule per zone blocks every hostname that no router serves:
    not http.host in {"a.example" "b.example" ...}

Allowed hostnames = Host() rules of the enabled routers on both edge Traefiks
(rpi5 and param, read from their /api/http/routers) + the zone's explicit
(non-wildcard) proxied DNS records, e.g. a Cloudflare Tunnel. *.local.* names are
left out: Cloudflare can't serve them (Universal SSL covers one level only).

Safety: aborts without changing anything if either Traefik returns too few routers,
a REQUIRED_HOSTS name is missing, or more than MAX_REMOVALS names would be dropped
in one run (WAF_SYNC_FORCE=true overrides the last one). If a Traefik can't be
reached at all (e.g. param is down), the run is additive only: new names from the
reachable one are added, nothing is removed, and the job still succeeds.

Env: CF_WAF_API_TOKEN (Zone:Read + Zone WAF:Edit on the zones), optional DRY_RUN=true.
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.request

ZONES = ["example.com", "example.ca", "example.com"]
RULE_DESCRIPTION = "Block hostnames with no Traefik router (wildcard DNS scanner noise)"
API_PATH = "/api/http/routers?per_page=1000"
TRAEFIKS = {
    "rpi5": ("traefik.local.example.com", "10.10.9.5"),
    "param": ("traefik-dashboard.local.example.com", "10.10.9.2"),
}
MIN_ROUTERS = 10
MAX_EXPRESSION = 4096
MAX_REMOVALS = int(os.environ.get("MAX_REMOVALS", "5"))
REQUIRED_HOSTS = os.environ.get(
    "REQUIRED_HOSTS",
    "cloud.example.com git.example.com bitwarden.example.com homeassistant.example.com auth.example.com",
).split()
DRY_RUN = os.environ.get("DRY_RUN", "false") == "true"
FORCE = os.environ.get("WAF_SYNC_FORCE", "false") == "true"
CF = "https://api.cloudflare.com/client/v4"


def fail(msg):
    print(f"ABORT: {msg} - nothing changed")
    sys.exit(1)


def routers(host, ip):
    # The dashboard routers sit behind forward-auth-local, which lets rpi5
    # (where this job runs) through by IP. Pinned to the Traefik's IP; its
    # cert is fine but TLS isn't verified so a cert change can't break the
    # sync - the safety checks bound what a bad answer can do.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(f"https://{ip}{API_PATH}", headers={"Host": host})
    return json.load(urllib.request.urlopen(req, timeout=30, context=ctx))


def router_hosts(name, routers):
    if len(routers) < MIN_ROUTERS:
        fail(f"{name} Traefik returned only {len(routers)} routers")
    hosts = set()
    for r in routers:
        if r.get("status") == "enabled":
            hosts.update(h.lower() for h in re.findall(r"Host\(`([^`]+)`\)", r.get("rule", "")))
    print(f"{name}: {len(routers)} routers, {len(hosts)} hostnames")
    return hosts


def cf(method, path, body=None):
    req = urllib.request.Request(
        CF + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['CF_WAF_API_TOKEN']}",
                 "Content-Type": "application/json"},
    )
    # Retry network blips (e.g. a TLS handshake timeout). Not POST: a timed-out
    # create may still have landed, and a retry would add a duplicate rule.
    attempts = 1 if method == "POST" else 3
    for attempt in range(1, attempts + 1):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30))
            break
        except urllib.error.HTTPError as e:
            d = json.load(e)
            break
        except OSError as e:
            if attempt == attempts:
                fail(f"Cloudflare {method} {path}: {e}")
            print(f"WARN: Cloudflare {method} {path}: {e}; retry {attempt}/{attempts - 1}")
            time.sleep(5 * attempt)
    if not d.get("success"):
        fail(f"Cloudflare {method} {path}: {d.get('errors')}")
    return d["result"]


def zone_of(host):
    return next((z for z in ZONES if host == z or host.endswith("." + z)), None)


def main():
    hosts, unreachable = set(), []
    for name, (host, ip) in TRAEFIKS.items():
        try:
            answer = routers(host, ip)
        except (OSError, ValueError) as e:
            print(f"WARN: {name} Traefik unreachable ({e}); additive-only run")
            unreachable.append(name)
            continue
        hosts |= router_hosts(name, answer)
    if len(unreachable) == len(TRAEFIKS):
        print("WARN: no Traefik reachable - nothing changed")
        return

    plans = []
    for zone in ZONES:
        zid = cf("GET", f"/zones?name={zone}")[0]["id"]
        records = cf("GET", f"/zones/{zid}/dns_records?per_page=500")
        published = {r["name"].lower() for r in records
                     if r["proxied"] and r["type"] in ("A", "AAAA", "CNAME") and "*" not in r["name"]}
        entry = cf("GET", f"/zones/{zid}/rulesets/phases/http_request_firewall_custom/entrypoint")
        rule = next((r for r in entry.get("rules", []) if r.get("description") == RULE_DESCRIPTION), None)
        current = set(re.findall(r'"([^"]+)"', rule["expression"])) if rule else set()

        # A down Traefik's names are unknown, so keep everything already allowed.
        candidates = hosts | published | (current if unreachable else set())
        allowed = sorted(h for h in candidates
                         if zone_of(h) == zone and "*" not in h and ".local." not in h)
        if not allowed:
            fail(f"no hostnames for {zone}: the rule would block the whole zone")

        expression = "not http.host in {" + " ".join(f'"{h}"' for h in allowed) + "}"
        if len(expression) > MAX_EXPRESSION:
            fail(f"{zone} expression is {len(expression)} chars (limit {MAX_EXPRESSION})")
        added, removed = sorted(set(allowed) - current), sorted(current - set(allowed))
        print(f"{zone}: {len(allowed)} allowed, +{added or '[]'} -{removed or '[]'}")
        plans.append((zone, zid, entry["id"], rule, expression, added, removed))

    missing = [h for h in REQUIRED_HOSTS if not any(f'"{h}"' in p[4] for p in plans)]
    if missing:
        fail(f"required hostnames missing from the routers: {missing}")
    removals = sum(len(p[6]) for p in plans)
    if removals > MAX_REMOVALS and not FORCE:
        fail(f"{removals} hostnames would be removed (max {MAX_REMOVALS}); "
             "re-run with WAF_SYNC_FORCE=true if that's intended")

    for zone, zid, rsid, rule, expression, added, removed in plans:
        if rule and not added and not removed:
            continue
        if DRY_RUN:
            print(f"DRY_RUN: would update {zone}")
            continue
        body = {"description": RULE_DESCRIPTION, "expression": expression,
                "action": "block", "enabled": True}
        if rule:
            cf("PATCH", f"/zones/{zid}/rulesets/{rsid}/rules/{rule['id']}", body)
        else:
            cf("POST", f"/zones/{zid}/rulesets/{rsid}/rules", body)
        print(f"{zone}: rule updated")


if __name__ == "__main__":
    main()

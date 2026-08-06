#!/usr/bin/env python3
"""Uptime monitor for krunalkumar.dpdns.org.

Runs from GitHub Actions on a schedule (see .github/workflows/monitor.yml).
Checks the production site, the blog, and the Vercel origin; measures
response times; reads the TLS certificate expiry; then writes three JSON
files consumed by the status page (index.html) and the README badge:

  data/status.json   current snapshot + computed uptime percentages
  data/history.json  one compact row per run, pruned to HISTORY_DAYS
  data/badge.json    shields.io endpoint schema

Stdlib only — no dependencies, so the Actions run stays fast.
"""

import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST = "krunalkumar.dpdns.org"

# key, label, url, follow_redirects, ok_codes
CHECKS = [
    ("home", "Website — home", f"https://{HOST}/", True, list(range(200, 300))),
    ("blog", "Website — blog", f"https://{HOST}/blog", True, list(range(200, 300))),
    # The *.vercel.app origin 308-redirects to the custom domain. Checking it
    # without following redirects separates "Vercel is down" from "the custom
    # domain / its DNS is broken" during an incident.
    ("vercel", "Vercel origin", "https://krunalkumar.vercel.app/",
     False, list(range(200, 300)) + [301, 302, 307, 308]),
]

TIMEOUT = 15          # seconds per request
RETRY_WAIT = 5        # seconds before the single retry
HISTORY_DAYS = 35     # prune history rows older than this
CERT_WARN_DAYS = 14   # "degraded" when the certificate expires sooner
DATA = Path(__file__).resolve().parent / "data"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe(url, follow, ok_codes):
    """One GET. Returns (ok, http_code_or_None, ms)."""
    opener = (urllib.request.build_opener() if follow
              else urllib.request.build_opener(NoRedirect))
    req = urllib.request.Request(url, headers={
        "User-Agent": "status-monitor (+https://github.com/officialkrunalkumar/status)",
    })
    start = time.monotonic()
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        return False, None, round((time.monotonic() - start) * 1000)
    ms = round((time.monotonic() - start) * 1000)
    return code in ok_codes, code, ms


def check(url, follow, ok_codes):
    """Probe with one retry so a transient runner blip doesn't raise an alarm."""
    ok, code, ms = probe(url, follow, ok_codes)
    if not ok:
        time.sleep(RETRY_WAIT)
        ok, code, ms = probe(url, follow, ok_codes)
    return ok, code, ms


def cert_expiry():
    """Returns (days_left, iso_date) for the site certificate, or (None, None)."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((HOST, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                cert = tls.getpeercert()
        expires = datetime.strptime(
            cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (expires - datetime.now(timezone.utc)).days, expires.date().isoformat()
    except Exception:
        return None, None


def uptime(history, hours):
    """Percent of runs in the window where the home check passed."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = [r for r in history
            if datetime.fromisoformat(r["t"]) >= cutoff and "home" in r["r"]]
    if not rows:
        return None
    up = sum(1 for r in rows if r["r"]["home"][0])
    return round(100 * up / len(rows), 2)


def main():
    now = datetime.now(timezone.utc)
    results = {}
    for key, label, url, follow, ok_codes in CHECKS:
        ok, code, ms = check(url, follow, ok_codes)
        results[key] = {"label": label, "url": url, "ok": ok, "code": code, "ms": ms}

    cert_days, cert_date = cert_expiry()

    DATA.mkdir(exist_ok=True)
    try:
        history = json.loads((DATA / "history.json").read_text())
    except Exception:
        history = []

    history.append({
        "t": now.isoformat(timespec="seconds"),
        "r": {k: [1 if v["ok"] else 0, v["ms"]] for k, v in results.items()},
    })
    cutoff = now - timedelta(days=HISTORY_DAYS)
    history = [r for r in history if datetime.fromisoformat(r["t"]) >= cutoff]

    if not results["home"]["ok"]:
        overall = "down"
    elif (any(not v["ok"] for v in results.values())
          or (cert_days is not None and cert_days < CERT_WARN_DAYS)):
        overall = "degraded"
    else:
        overall = "up"

    status = {
        "updated": now.isoformat(timespec="seconds"),
        "overall": overall,
        "checks": results,
        "cert": {"days_left": cert_days, "expires": cert_date},
        "uptime": {
            "d1": uptime(history, 24),
            "d7": uptime(history, 24 * 7),
            "d30": uptime(history, 24 * 30),
        },
    }

    badge = {
        "schemaVersion": 1,
        "label": "site",
        "message": overall,
        "color": {"up": "#34d399", "degraded": "#fbbf24", "down": "#f87171"}[overall],
    }

    (DATA / "status.json").write_text(json.dumps(status, indent=1) + "\n")
    (DATA / "history.json").write_text(json.dumps(history, separators=(",", ":")) + "\n")
    (DATA / "badge.json").write_text(json.dumps(badge, indent=1) + "\n")

    print(f"{now:%Y-%m-%d %H:%M} UTC  overall={overall}  "
          + "  ".join(f"{k}={'ok' if v['ok'] else 'FAIL'}({v['code']},{v['ms']}ms)"
                      for k, v in results.items())
          + f"  cert={cert_days}d")
    # Exit 0 always: the workflow decides separately whether to fail the run.
    return 0


if __name__ == "__main__":
    sys.exit(main())

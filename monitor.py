#!/usr/bin/env python3
"""Uptime monitor for krunalkumar.dpdns.org.

Runs from GitHub Actions on a schedule (see .github/workflows/monitor.yml).
Probes every public surface of the site — pages, labs, machine-readable feeds,
static assets, redirects and the 404 route — plus DNS, TLS and the security
headers, then writes the JSON the status page (index.html) renders:

  data/status.json    current snapshot: every check, cert, DNS, headers, uptime
  data/history.json   one row per run, full detail, pruned to HISTORY_HOURS
  data/daily.json     one row per UTC day per check, pruned to DAILY_DAYS
  data/incidents.json log of degraded/down episodes (last INCIDENTS kept)
  data/pages.json     full sitemap sweep, refreshed at most once per SWEEP_HOURS
  data/badge.json     shields.io endpoint schema

Splitting history (short, detailed) from daily (long, aggregated) is what
keeps 90 days of uptime on a page that refetches every minute: the detailed
rows would otherwise grow to megabytes now that there are ~36 checks per run.

Stdlib only — no dependencies, so the Actions run stays fast.
"""

import json
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST = "krunalkumar.dpdns.org"
SITE = "https://" + HOST

# Check severity, which is what turns individual failures into an overall
# state:
#   critical  the site is effectively down if this fails
#   major     a real user-facing section is broken -> degraded
#   minor     feeds, assets, redirects -> degraded, but flagged as minor
CRITICAL, MAJOR, MINOR = "critical", "major", "minor"

# Groups, in the order the status page renders them.
GROUPS = [
    ("pages", "Site pages"),
    ("content", "Writing & labs"),
    ("games", "Games"),
    ("mayuri", "Mayuri assistant"),
    ("feeds", "Machine-readable"),
    ("assets", "Static assets"),
    ("infra", "Edge & routing"),
]

# key, label, group, url, severity, follow_redirects, ok_codes, must_contain
CHECKS = [
    ("home", "Home", "pages", SITE + "/", CRITICAL, True, None, None),
    ("about", "About", "pages", SITE + "/about", MAJOR, True, None, None),
    ("services", "Services", "pages", SITE + "/services", MAJOR, True, None, None),
    ("projects", "Projects", "pages", SITE + "/projects", MAJOR, True, None, None),
    ("research", "Research", "pages", SITE + "/research", MAJOR, True, None, None),
    ("internships", "Internships", "pages", SITE + "/internships", MAJOR, True, None, None),
    ("contact", "Contact", "pages", SITE + "/contact", MAJOR, True, None, None),
    ("reviews", "Client reviews", "pages", SITE + "/client-reviews", MAJOR, True, None, None),
    ("verify", "Credential verification", "pages", SITE + "/verify", MAJOR, True, None, None),

    ("blog", "Blog index", "content", SITE + "/blog", MAJOR, True, None, None),
    # One representative article and one representative lab. Probing all 74
    # labs every run would be slow and noisy; the daily sitemap sweep (see
    # sweep()) covers every URL on the site instead.
    ("post", "Blog article", "content",
     SITE + "/blog/types-of-cyberattacks", MAJOR, True, None, None),
    ("labs", "Labs index", "content", SITE + "/labs", MAJOR, True, None, None),
    ("lab", "Lab — hacklab", "content", SITE + "/labs/hacklab", MAJOR, True, None, None),

    # /games is the largest section of the site now (67 games), which is why it
    # gets a group of its own rather than being folded in with the labs.
    # Sampling follows the same reasoning as the lab check above: the games run
    # on two shells, so one game from each stands in for all 67 — snake sits
    # directly on game-shell.js, the terminal games layer term-shell.js over it.
    # The daily sitemap sweep still covers every game page individually.
    ("games", "Games hub", "games", SITE + "/games", MAJOR, True, None, None),
    ("game", "Game — Snake", "games", SITE + "/games/snake", MAJOR, True, None, None),
    ("gameterm", "Game — cmatrix", "games",
     SITE + "/games/cmatrix", MAJOR, True, None, None),

    # Mayuri, the help-menu assistant docked in the corner of every page, has
    # no page or API of her own: particle-bg.js builds her at runtime and
    # main.css styles her. Either file breaking takes her off every page at
    # once while each page keeps returning 200, so the checks go after the
    # files themselves: the home page still embeds her script, the script is
    # served and still contains her, and her rules are still in the
    # stylesheet (whether the stylesheet is served at all is the "css" check
    # under assets, which is why the styles check is only minor).
    ("mayurihome", "Mayuri on the home page", "mayuri", SITE + "/",
     MAJOR, True, None, "/assets/js/particle-bg.js"),
    ("mayuri", "Mayuri runtime", "mayuri",
     SITE + "/assets/js/particle-bg.js", MAJOR, True, None, "mayuri-panel"),
    ("mayuricss", "Mayuri styles", "mayuri",
     SITE + "/assets/css/main.css", MINOR, True, None, ".mayuri-button"),

    ("sitemap", "Sitemap", "feeds", SITE + "/sitemap.xml", MINOR, True, None, "<loc>"),
    ("rss", "RSS feed", "feeds", SITE + "/feed.xml", MINOR, True, None, "<rss"),
    ("atom", "Atom feed", "feeds", SITE + "/atom.xml", MINOR, True, None, "<feed"),
    ("llms", "llms.txt", "feeds", SITE + "/llms.txt", MINOR, True, None, None),
    ("robots", "robots.txt", "feeds", SITE + "/robots.txt", MINOR, True, None, "Sitemap:"),
    ("manifest", "Web app manifest", "feeds",
     SITE + "/site.webmanifest", MINOR, True, None, "start_url"),

    ("css", "Stylesheet", "assets", SITE + "/assets/css/main.css", MAJOR, True, None, ":root"),
    ("js", "Boot script", "assets", SITE + "/assets/js/boot.js", MAJOR, True, None, None),
    # Every game page loads game-shell.js, so losing it breaks all 67 of them
    # while each page still returns a perfectly healthy 200 — precisely the
    # failure the page checks above cannot see. Same for the section's CSS.
    ("gameshell", "Game shell runtime", "assets",
     SITE + "/assets/js/games/game-shell.js", MAJOR, True, None, "GameShell"),
    ("gamescss", "Games stylesheet", "assets",
     SITE + "/assets/css/games.css", MAJOR, True, None, ".game-grid"),
    # The hub's grid is in the HTML; hub.js only adds filtering and best scores
    # on top, so losing it degrades /games rather than breaking it.
    ("gamehub", "Games hub script", "assets",
     SITE + "/assets/js/games/hub.js", MINOR, True, None, None),
    ("partials", "Header partial", "assets", SITE + "/partials/header", MAJOR, True, None, None),
    ("resume", "Résumé PDF", "assets",
     SITE + "/assets/pdf/Krunalkumar-Shah-Resume.pdf", MINOR, True, None, None),

    # The *.vercel.app origin 308-redirects to the custom domain. Checking it
    # without following redirects separates "Vercel is down" from "the custom
    # domain / its DNS is broken" during an incident.
    ("vercel", "Vercel origin", "infra", "https://krunalkumar.vercel.app/",
     MAJOR, False, [200, 301, 302, 307, 308], None),
    ("www", "www → apex redirect", "infra", "https://www." + HOST + "/",
     MINOR, False, [301, 302, 307, 308], None),
    ("https", "HTTP → HTTPS redirect", "infra", "http://" + HOST + "/",
     MINOR, False, [301, 302, 307, 308], None),
    # A soft-404 (HTTP 200 on a missing page) quietly wrecks search indexing,
    # so the 404 route is checked for the code it is supposed to return.
    ("notfound", "404 handling", "infra",
     SITE + "/_status-probe-404", MINOR, True, [404], None),
]

# Headers the site is expected to send. Losing one is a real regression, so
# they are surfaced on the page — but they never move the overall state,
# because a missing header is not an outage.
SECURITY_HEADERS = [
    ("strict-transport-security", "HSTS"),
    ("content-security-policy", "CSP"),
    ("x-content-type-options", "X-Content-Type-Options"),
    ("x-frame-options", "X-Frame-Options"),
    ("referrer-policy", "Referrer-Policy"),
    ("permissions-policy", "Permissions-Policy"),
    ("cross-origin-opener-policy", "COOP"),
]

TIMEOUT = 15          # seconds per request
RETRY_WAIT = 5        # seconds before the single retry
WORKERS = 8           # parallel probes — the whole run finishes in a few seconds
BODY_LIMIT = 65536    # bytes read when a check asserts on content
HISTORY_HOURS = 36    # detailed per-run rows: the 24 h windows, plus headroom
DAILY_DAYS = 90       # keep per-day aggregates this long
INCIDENTS = 25        # keep this many past incidents
SWEEP_HOURS = 20      # re-sweep the whole sitemap at most this often
CERT_WARN_DAYS = 14   # "degraded" when the certificate expires sooner
UA = "status-monitor (+https://github.com/officialkrunalkumar/status)"
DATA = Path(__file__).resolve().parent / "data"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe(url, follow, ok_codes, must_contain=None, want_headers=False):
    """One GET. Returns a dict: ok, code, ms, note, headers, location."""
    opener = (urllib.request.build_opener() if follow
              else urllib.request.build_opener(NoRedirect))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    out = {"ok": False, "code": None, "ms": 0, "note": None,
           "headers": None, "location": None}
    start = time.monotonic()
    body = b""
    try:
        try:
            resp = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            resp = e                      # 4xx/5xx still carry code + headers
        with resp:
            out["code"] = getattr(resp, "status", None) or resp.code
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            out["location"] = hdrs.get("location")
            if want_headers:
                out["headers"] = hdrs
            if must_contain:
                body = resp.read(BODY_LIMIT)
    except Exception as e:
        out["ms"] = round((time.monotonic() - start) * 1000)
        out["note"] = type(e).__name__
        return out

    out["ms"] = round((time.monotonic() - start) * 1000)
    if out["code"] not in (ok_codes or list(range(200, 300))):
        out["note"] = "unexpected HTTP " + str(out["code"])
        return out
    if must_contain and must_contain.encode() not in body:
        out["note"] = "body missing " + must_contain
        return out
    out["ok"] = True
    return out


def check(url, follow, ok_codes, must_contain=None, want_headers=False):
    """Probe with one retry so a transient runner blip doesn't raise an alarm."""
    r = probe(url, follow, ok_codes, must_contain, want_headers)
    if not r["ok"]:
        time.sleep(RETRY_WAIT)
        r = probe(url, follow, ok_codes, must_contain, want_headers)
    return r


def cert_expiry():
    """Returns (days_left, iso_date, issuer) for the site certificate."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((HOST, 443), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
                cert = tls.getpeercert()
        expires = datetime.strptime(
            cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName")
        return (expires - datetime.now(timezone.utc)).days, expires.date().isoformat(), issuer
    except Exception:
        return None, None, None


def dns_lookup():
    """Times an A/AAAA resolution for the host. Returns (ms, [addresses])."""
    start = time.monotonic()
    try:
        infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return None, []
    ms = round((time.monotonic() - start) * 1000)
    return ms, sorted({i[4][0] for i in infos})


def security_headers(hdrs):
    """Grades the home page's response headers against SECURITY_HEADERS."""
    if not hdrs:
        return {"present": [], "missing": [h[1] for h in SECURITY_HEADERS],
                "score": 0, "total": len(SECURITY_HEADERS), "hsts_days": None}
    present = [label for name, label in SECURITY_HEADERS if name in hdrs]
    missing = [label for name, label in SECURITY_HEADERS if name not in hdrs]
    hsts_days = None
    m = re.search(r"max-age=(\d+)", hdrs.get("strict-transport-security", ""))
    if m:
        hsts_days = int(m.group(1)) // 86400
    return {"present": present, "missing": missing, "score": len(present),
            "total": len(SECURITY_HEADERS), "hsts_days": hsts_days}


# --------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------

def rebuild_daily(daily, history):
    """Recompute the day buckets covered by `history`, keep the older ones.

    History is short (HISTORY_HOURS) and daily is long (DAILY_DAYS), so every
    day still fully present in history is recomputed from scratch — which makes
    today's partial bucket self-healing — while days that have aged out of
    history keep whatever was stored for them. Rows are
    [up, total, mean_ms] per check key.
    """
    fresh = {}
    for row in history:
        day = fresh.setdefault(row["t"][:10], {})
        for key, pair in row["r"].items():
            slot = day.setdefault(key, [0, 0, []])   # up, total, ms samples
            slot[1] += 1
            if pair[0]:
                slot[0] += 1
                slot[2].append(pair[1])

    by_day = {d["d"]: d for d in daily}
    # The oldest day in the window is only *partly* covered by history — the
    # earlier part of it has already been pruned — so recomputing that one from
    # scratch would silently shrink the bucket that was written back while the
    # whole day was still there. Keep the stored value for it instead. Every
    # other day in history is complete, and the newest one is today, which has
    # to keep being recomputed for the self-healing above to work.
    oldest = history[0]["t"][:10] if history else None
    newest = history[-1]["t"][:10] if history else None
    for day, checks in fresh.items():
        if day == oldest and day != newest and day in by_day:
            continue
        by_day[day] = {"d": day, "r": {
            k: [v[0], v[1], round(sum(v[2]) / len(v[2])) if v[2] else 0]
            for k, v in checks.items()}}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=DAILY_DAYS)).date().isoformat()
    return sorted((d for d in by_day.values() if d["d"] >= cutoff), key=lambda d: d["d"])


def uptime_from_daily(daily, days, key="home"):
    """Percent of runs in the window where `key` passed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    up = total = 0
    for d in daily:
        if d["d"] >= cutoff and key in d["r"]:
            up += d["r"][key][0]
            total += d["r"][key][1]
    return round(100 * up / total, 3) if total else None


def uptime_from_history(history, hours, key="home"):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = [r for r in history
            if datetime.fromisoformat(r["t"]) >= cutoff and key in r["r"]]
    if not rows:
        return None
    return round(100 * sum(1 for r in rows if r["r"][key][0]) / len(rows), 3)


def percentiles(history, hours=24, key="home"):
    """p50/p95 response time across successful checks in the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    ms = sorted(r["r"][key][1] for r in history
                if key in r["r"] and r["r"][key][0]
                and datetime.fromisoformat(r["t"]) >= cutoff)
    if not ms:
        return {"p50": None, "p95": None, "n": 0}
    return {"p50": ms[len(ms) // 2],
            "p95": ms[min(len(ms) - 1, int(len(ms) * 0.95))],
            "n": len(ms)}


def update_incidents(incidents, now, overall, failing):
    """Open an incident when the site leaves 'up', close it when it returns.

    Each incident records the worst state it reached and every check that
    failed at any point during it, so a blip that escalates stays one entry
    rather than becoming several.
    """
    rank = {"up": 0, "degraded": 1, "down": 2}
    open_inc = incidents[-1] if incidents and incidents[-1].get("end") is None else None

    if overall == "up":
        if open_inc:
            open_inc["end"] = now.isoformat(timespec="seconds")
            start = datetime.fromisoformat(open_inc["start"])
            open_inc["minutes"] = max(1, round((now - start).total_seconds() / 60))
        return incidents

    if open_inc:
        if rank[overall] > rank[open_inc["level"]]:
            open_inc["level"] = overall
        open_inc["checks"] = sorted(set(open_inc["checks"]) | set(failing))
        open_inc["last"] = now.isoformat(timespec="seconds")
    else:
        incidents.append({
            "start": now.isoformat(timespec="seconds"),
            "last": now.isoformat(timespec="seconds"),
            "end": None,
            "minutes": None,
            "level": overall,
            "checks": sorted(failing),
        })
    return incidents[-INCIDENTS:]


# --------------------------------------------------------------------------
# full-sitemap sweep
# --------------------------------------------------------------------------

def sweep_due(now):
    """(is_due, previous_sweep) — the sweep schedules itself off its own file."""
    try:
        prev = json.loads((DATA / "pages.json").read_text(encoding="utf-8"))
        last = datetime.fromisoformat(prev["updated"])
        return (now - last) >= timedelta(hours=SWEEP_HOURS), prev
    except Exception:
        return True, None


def sweep(now):
    """GETs every URL in the sitemap. Slow-ish, so it runs about once a day."""
    try:
        req = urllib.request.Request(SITE + "/sitemap.xml", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            xml = resp.read().decode("utf-8", "replace")
    except Exception:
        return None

    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if not urls:
        return None

    def one(u):
        res = probe(u, True, None)
        if not res["ok"]:                       # one retry, same as check()
            time.sleep(1)
            res = probe(u, True, None)
        return {"url": u, "path": u.replace(SITE, "") or "/",
                "ok": res["ok"], "code": res["code"], "ms": res["ms"]}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(one, urls))

    bad = [r for r in results if not r["ok"]]
    ok_ms = sorted(r["ms"] for r in results if r["ok"])
    return {
        "updated": now.isoformat(timespec="seconds"),
        "total": len(results),
        "ok": len(results) - len(bad),
        "failing": bad[:25],
        "median_ms": ok_ms[len(ok_ms) // 2] if ok_ms else None,
        "slowest": sorted(results, key=lambda r: -r["ms"])[:5],
        "sections": sections(results),
    }


def sections(results):
    """Rolls sweep results up by top-level path segment (labs, blog, …)."""
    buckets = {}
    for r in results:
        seg = r["path"].strip("/").split("/")[0] or "home"
        b = buckets.setdefault(seg, {"section": seg, "total": 0, "ok": 0, "ms": []})
        b["total"] += 1
        if r["ok"]:
            b["ok"] += 1
            b["ms"].append(r["ms"])
    out = []
    for b in buckets.values():
        ms = sorted(b.pop("ms"))
        b["median_ms"] = ms[len(ms) // 2] if ms else None
        out.append(b)
    return sorted(out, key=lambda b: -b["total"])


# --------------------------------------------------------------------------

def read(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def write(name, obj, compact=False):
    text = (json.dumps(obj, separators=(",", ":")) if compact
            else json.dumps(obj, indent=1)) + "\n"
    (DATA / name).write_text(text, encoding="utf-8")


def main():
    now = datetime.now(timezone.utc)
    DATA.mkdir(exist_ok=True)

    def run_one(spec):
        key, label, group, url, sev, follow, codes, contains = spec
        r = check(url, follow, codes, contains, want_headers=(key == "home"))
        return key, {"label": label, "group": group, "url": url, "sev": sev,
                     "ok": r["ok"], "code": r["code"], "ms": r["ms"],
                     "note": r["note"], "location": r["location"],
                     "_headers": r["headers"]}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = dict(pool.map(run_one, CHECKS))

    home_headers = results["home"].get("_headers")
    for v in results.values():
        v.pop("_headers", None)

    cert_days, cert_date, cert_issuer = cert_expiry()
    dns_ms, dns_addrs = dns_lookup()
    headers = security_headers(home_headers)

    # ---- append to history, rebuild daily from it, then prune ----
    history = read("history.json", [])
    history.append({
        "t": now.isoformat(timespec="seconds"),
        "r": {k: [1 if v["ok"] else 0, v["ms"]] for k, v in results.items()},
    })
    daily = rebuild_daily(read("daily.json", []), history)
    hcut = now - timedelta(hours=HISTORY_HOURS)
    history = [r for r in history if datetime.fromisoformat(r["t"]) >= hcut]

    # ---- overall state ----
    failing = [k for k, v in results.items() if not v["ok"]]
    cert_low = cert_days is not None and cert_days < CERT_WARN_DAYS
    if not results["home"]["ok"]:
        overall = "down"
    elif failing or cert_low:
        overall = "degraded"
    else:
        overall = "up"

    incidents = update_incidents(read("incidents.json", []), now, overall, failing)

    # ---- full sitemap sweep, at most once per SWEEP_HOURS ----
    due, prev_sweep = sweep_due(now)
    pages = (sweep(now) or prev_sweep) if due else prev_sweep
    if pages:
        write("pages.json", pages)

    status = {
        "updated": now.isoformat(timespec="seconds"),
        "overall": overall,
        "site": SITE,
        "groups": [{"key": k, "label": l} for k, l in GROUPS],
        "summary": {
            "total": len(results),
            "ok": len(results) - len(failing),
            "failing": failing,
            "worst": ("critical" if not results["home"]["ok"]
                      else "major" if any(results[k]["sev"] == MAJOR for k in failing)
                      else "minor" if failing else None),
        },
        "checks": results,
        "cert": {"days_left": cert_days, "expires": cert_date, "issuer": cert_issuer},
        "dns": {"ms": dns_ms, "addresses": dns_addrs},
        "headers": headers,
        "uptime": {
            "d1": uptime_from_history(history, 24),
            "d7": uptime_from_daily(daily, 7),
            "d30": uptime_from_daily(daily, 30),
            "d90": uptime_from_daily(daily, 90),
        },
        "response": percentiles(history, 24),
        "open_incident": bool(incidents and incidents[-1].get("end") is None),
    }

    badge = {
        "schemaVersion": 1,
        "label": "site",
        "message": overall,
        "color": {"up": "#34d399", "degraded": "#fbbf24", "down": "#f87171"}[overall],
    }

    write("status.json", status)
    write("history.json", history, compact=True)
    write("daily.json", daily, compact=True)
    write("incidents.json", incidents)
    write("badge.json", badge)

    print("{:%Y-%m-%d %H:%M} UTC  overall={}  {}/{} ok  home={}ms  dns={}ms  "
          "cert={}d  headers={}/{}".format(
              now, overall, status["summary"]["ok"], status["summary"]["total"],
              results["home"]["ms"], dns_ms, cert_days,
              headers["score"], headers["total"])
          + ("  FAILING: " + ", ".join(failing) if failing else "")
          + ("  sweep={}/{}".format(pages["ok"], pages["total"])
             if pages and due else ""))
    # Exit 0 always: the workflow decides separately whether to fail the run.
    return 0


if __name__ == "__main__":
    sys.exit(main())

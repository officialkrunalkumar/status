# status

Self-built uptime monitor and status page for [krunalkumar.dpdns.org](https://krunalkumar.dpdns.org/).

**Live page:** https://officialkrunalkumar.github.io/status/

![site status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fofficialkrunalkumar%2Fstatus%2Fmain%2Fdata%2Fbadge.json)

## How it works

Everything runs on GitHub's infrastructure — deliberately separate from the
site's own hosting (Vercel) and DNS (dpdns.org), so the status page stays
reachable during an incident on either.

```
GitHub Actions (cron, ~10 min)
  └─ monitor.py            checks the site + blog + Vercel origin,
     │                     measures response times, reads TLS cert expiry
     ├─ data/status.json   current snapshot + uptime percentages
     ├─ data/history.json  one row per run (pruned to 35 days)
     └─ data/badge.json    shields.io endpoint for the badge above
        └─ committed back to this repo
           └─ index.html   static page on GitHub Pages that renders the JSON
```

- **Checks** ([monitor.py](monitor.py)): home page, blog, and the
  `*.vercel.app` origin without following redirects — the last one separates
  "Vercel is down" from "the custom domain / DNS is broken", which are very
  different incidents. Each check retries once after 5 s so a transient
  network blip on the runner doesn't raise a false alarm.
- **Alerting**: when the home page is unreachable, the workflow's last step
  exits non-zero. GitHub emails the repo owner about failed runs — free
  downtime alerts with zero configuration. Degraded states (blog or origin
  failing, certificate expiring within 14 days) show on the page but don't
  fail the run.
- **Page** ([index.html](index.html)): dependency-free static HTML that
  fetches the JSON and renders current status, per-check cards, certificate
  countdown, 24 h / 7 d / 30 d uptime, a 30-day daily uptime strip (with a
  plain table fallback), and a 24-hour response-time chart. Auto-refreshes
  every minute.

## Notes

- GitHub runs scheduled workflows on a best-effort basis: expect checks
  every ~10–15 minutes, occasionally delayed. The page shows a warning
  banner when data is older than 30 minutes.
- GitHub disables cron workflows in repos with no activity for 60 days; the
  monitor's own data commits keep the repo active.
- The `[skip ci]` marker in data commits (plus GitHub's own guard against
  `GITHUB_TOKEN`-triggered workflows) prevents the commit from re-triggering
  the workflow.

## Running locally

```bash
python3 monitor.py        # runs the checks, writes data/*.json
python3 -m http.server    # then open http://localhost:8000/
```

<img src="logo.svg" alt="" width="56" align="left" hspace="12" />

# status

Self-built uptime monitor and status page for [krunalkumar.dpdns.org](https://krunalkumar.dpdns.org/).

<br clear="left" />

**Live page:** https://status.krunalkumar.dpdns.org

![site status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fofficialkrunalkumar%2Fstatus%2Fmain%2Fdata%2Fbadge.json)

## How it works

Everything runs on GitHub's infrastructure — deliberately separate from the
site's own hosting (Vercel) and DNS (dpdns.org), so the status page stays
reachable during an incident on either.

```
GitHub Actions (cron, every 5 min)
  └─ monitor.py              36 checks in parallel + DNS, TLS, security headers
     │                       once a day, also sweeps every URL in the sitemap
     ├─ data/status.json     current snapshot, grouped by section
     ├─ data/history.json    one row per run, full detail, last 36 h
     ├─ data/daily.json      one row per UTC day, last 90 days
     ├─ data/incidents.json  log of degraded/down episodes
     ├─ data/pages.json      last full sitemap sweep
     └─ data/badge.json      shields.io endpoint for the badge above
        └─ committed back to this repo
           └─ index.html     static page on GitHub Pages that renders the JSON
```

## What is checked

Every run probes all 36 targets in parallel (stdlib `ThreadPoolExecutor`), so
the whole thing still finishes in a few seconds. Each one retries once after
5 s, so a transient blip on the runner doesn't raise a false alarm.

| Group | Checks |
| --- | --- |
| **Site pages** | home, about, services, projects, research, internships, contact, client-reviews, verify |
| **Writing & labs** | blog index, one article, labs index, one lab |
| **Games** | games hub, one canvas game (snake), one terminal game (cmatrix) |
| **Mayuri assistant** | home page embeds her script, her runtime (`particle-bg.js`), her styles in `main.css` |
| **Machine-readable** | sitemap.xml, feed.xml (RSS), atom.xml, llms.txt, robots.txt, site.webmanifest |
| **Static assets** | main.css, boot.js, game-shell.js, games.css, hub.js, header partial, résumé PDF |
| **Edge & routing** | Vercel origin, `www` → apex, HTTP → HTTPS, 404 route |

Beyond plain reachability:

- **Content assertions** — feeds and config files must actually contain what
  makes them valid (`<loc>`, `<rss`, `Sitemap:`, `start_url`, `:root`). A file
  that 200s with an empty or wrong body is a failure, not a pass.
- **Redirects** are checked *without* following them, so the check proves the
  redirect exists and returns 30x rather than silently landing on the target.
  The `*.vercel.app` origin one separates "Vercel is down" from "the custom
  domain / DNS is broken" — very different incidents.
- **404 handling** asserts a missing path returns 404. A soft-404 (HTTP 200 on
  a missing page) quietly wrecks search indexing and is invisible otherwise.
- **Shared runtimes** — all 67 games load `game-shell.js` and `games.css`, so
  those are checked directly rather than inferred from a game page. If the
  shell 404s every game breaks while every game page still returns 200, which
  no page-level check would ever notice.
- **Mayuri** — the help-menu assistant docked in the corner of every page has
  no page or API of her own: `particle-bg.js` builds her at runtime and
  `main.css` styles her. So she gets three checks of her own — the home page
  still embeds her script, the script is served and still contains her, and
  her rules are still in the stylesheet. Any of those can break while every
  page keeps returning 200.
- **TLS certificate** — days left, expiry date, issuer. Under 14 days is a
  degraded state.
- **DNS** — resolution time and the addresses returned.
- **Security headers** — HSTS (plus its `max-age`), CSP, X-Content-Type-Options,
  X-Frame-Options, Referrer-Policy, Permissions-Policy, COOP. Shown on the page
  but deliberately not part of the overall state: a missing header is a
  regression, not an outage.
- **Full sitemap sweep** — about once a day the monitor pulls `sitemap.xml` and
  GETs every URL in it (currently 177, including all 67 games and 62 labs),
  rolled up by
  section with the slowest pages listed. It schedules itself off the age of
  `data/pages.json`, so it needs no second workflow.

## Severity and alerting

Each check carries a severity, which is what turns individual failures into an
overall state:

- `critical` (home) failing → **down**
- anything else failing, or the certificate expiring within 14 days → **degraded**

When the state is **down**, the workflow's last step exits non-zero and GitHub
emails the repo owner about the failed run — free downtime alerts with zero
configuration. Degraded states show on the page but don't fail the run.

Episodes are recorded in `data/incidents.json`: an incident opens when the site
leaves `up`, absorbs every check that fails while it's open, keeps the worst
level it reached, and closes with a duration when the site recovers.

## Data layout

History is split in two on purpose. With ~36 checks per run, keeping 30 days of
per-run detail would grow to megabytes on a page that refetches every minute —
so detailed rows are kept for 36 h (charts, recent detail) and rolled into
one row per UTC day for 90 days (uptime percentages, the daily strip).

That 36 h is tied to the 5-minute cadence: it is twice the longest window
anything reads (the 24 h uptime tile and the response-time chart), and halving
it when the check interval halved keeps `history.json` the same size instead of
doubling what every visitor downloads.

The daily buckets are rebuilt from history on every run rather than incremented,
which makes today's partial bucket self-healing; days that have aged out of
history keep their stored values.

## The page

[index.html](index.html) is dependency-free static HTML — no build, no
framework, no external requests (the favicon and the KS_ mark are inlined), so
it renders even if everything else is on fire. It shows current state, the four
uptime windows, response/TLS/DNS/header vitals, all 36 checks grouped by
section, a 30-day daily strip, a 24-hour response-time chart, the latest
sitemap sweep and the incident log. Auto-refreshes every minute.

The KS_ mark's cursor is tinted with the current overall state — green,
amber, red — so the logo doubles as the status light.

## Notes

- The cron asks for a run every 5 minutes, which is the shortest interval
  GitHub accepts — but it is only a request. Scheduled workflows run on a
  best-effort basis and are dropped outright when the runner pool is busy, so
  the real gap is regularly much longer than the interval asked for. The page
  shows a warning banner when data is older than 30 minutes.
- Each run commits to `main`, and this site is built from the branch, so every
  run is also a Pages deployment. Pages has a soft limit of 10 builds per hour
  on branch-built sites, which is the practical ceiling on how fresh this data
  can get — lifting it would mean pointing the page at `raw.githubusercontent`
  for its JSON instead of the copy Pages serves.
- GitHub disables cron workflows in repos with no activity for 60 days; the
  monitor's own data commits keep the repo active.
- The `[skip ci]` marker in data commits (plus GitHub's own guard against
  `GITHUB_TOKEN`-triggered workflows) prevents the commit from re-triggering
  the workflow.
- Adding a check is one line in `CHECKS` in [monitor.py](monitor.py); the page
  picks it up automatically from `status.json`, including its group. A new
  group is one line in `GROUPS` and renders as its own panel, in that order.

## Running locally

```bash
python3 monitor.py        # runs the checks, writes data/*.json
python3 -m http.server    # then open http://localhost:8000/
```

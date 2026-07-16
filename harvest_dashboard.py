#!/usr/bin/env python3
"""
RenUSA team hours dashboard — full-year, interactive.

Pulls all time entries from Harvest v2 for the whole RenUSA team (every active
user with an @renusa.org email) from 2026-01-01 through today, writes them to
docs/data.json, and generates a self-contained docs/index.html that includes
the data inline plus a client-side filter/aggregation layer (Chart.js +
vanilla JS).

The HTML is published to GitHub Pages from the docs/ folder, so the dashboard
is live on the web after each build.

Runs in GitHub Actions on Fridays 18:00 UTC and on manual dispatch.
Credentials come from two repo secrets:
  HARVEST_ACCOUNT_ID
  HARVEST_TOKEN

The token must belong to a Harvest Administrator so it can see every team
member's time, not just the token owner's.
"""

import os
import sys
import json
import datetime as dt
import urllib.request
import urllib.parse
import urllib.error

API_BASE = "https://api.harvestapp.com/v2"
USER_AGENT = "RenUSA Harvest Dashboard (ddonofrio@thecaseygroup.us)"

# ---------------- Config you can edit ----------------
# Whole-team mode: include every active Harvest user whose email is on this
# domain. Leave TEAM_DOMAIN empty ("") to fall back to TEAM_EMAILS only.
TEAM_DOMAIN = "renusa.org"
# Extra individuals to include even if they're not on TEAM_DOMAIN
# (e.g. a contractor on a renusa-adjacent address). Emails, lowercase.
TEAM_EMAILS = []
# Specific people to leave OUT even though they match TEAM_DOMAIN
# (e.g. a shared/service account). Emails, lowercase.
EXCLUDE_EMAILS = []
YEAR = 2026
# -----------------------------------------------------

# .strip() guards against a trailing newline/space slipping in when the secret
# was pasted — otherwise it becomes an invalid HTTP header value.
ACCOUNT_ID = (os.environ.get("HARVEST_ACCOUNT_ID") or "").strip()
TOKEN = (os.environ.get("HARVEST_TOKEN") or "").strip()


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def api_get(path, params=None):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Harvest-Account-Id", str(ACCOUNT_ID))
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        die("Harvest API " + str(e.code) + " on " + path + ": " + body)
    except urllib.error.URLError as e:
        die("Network error on " + path + ": " + str(e))


def get_all(path, key, params=None):
    params = dict(params or {})
    params.setdefault("per_page", 100)
    page = 1
    out = []
    while True:
        params["page"] = page
        data = api_get(path, params)
        out.extend(data.get(key, []))
        if not data.get("next_page"):
            break
        page += 1
    return out


def resolve_team_ids():
    domain = (TEAM_DOMAIN or "").lower().lstrip("@")
    extra = set(e.lower() for e in TEAM_EMAILS)
    exclude = set(e.lower() for e in EXCLUDE_EMAILS)
    if not domain and not extra:
        return None  # no filter — every active user
    users = get_all("/users", "users", {"is_active": "true"})
    ids = set()
    matched = []
    for u in users:
        email = (u.get("email") or "").lower()
        if not email or email in exclude:
            continue
        if (domain and email.endswith("@" + domain)) or email in extra:
            ids.add(u.get("id"))
            matched.append(email)
    if not ids:
        die("No active Harvest users matched @%s or TEAM_EMAILS. "
            "Check the token has Administrator access." % domain)
    print("Team = %d members: %s" % (len(matched), ", ".join(sorted(matched))))
    return ids


def fetch_entries(keep_ids, from_date, to_date):
    raw = get_all("/time_entries", "time_entries",
                  {"from": from_date.isoformat(), "to": to_date.isoformat()})
    out = []
    for te in raw:
        user = te.get("user") or {}
        uid = user.get("id")
        if keep_ids is not None and uid not in keep_ids:
            continue
        proj = te.get("project") or {}
        client = te.get("client") or {}
        out.append({
            "date": te.get("spent_date"),
            "person": user.get("name", "Unknown"),
            "project": proj.get("name", "No project"),
            "client": client.get("name", ""),
            "hours": float(te.get("hours") or 0.0),
        })
    return out


def build_payload(entries, from_date, to_date):
    people = sorted({e["person"] for e in entries})
    projects = sorted({e["project"] for e in entries})
    clients = sorted({e["client"] for e in entries if e["client"]})
    return {
        "meta": {
            "generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            "year": YEAR,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "people": people,
            "projects": projects,
            "clients": clients,
            "entry_count": len(entries),
            "total_hours": round(sum(e["hours"] for e in entries), 1),
        },
        "entries": entries,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RenUSA Team Hours — __YEAR__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --navy:#0A2240; --gold:#C8963C; --red:#B03030; --cream:#F5F3EE; --white:#fff;
    --ink:#1a2333; --muted:#5b6470; --line:#e7e3d9;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: var(--cream); color: var(--ink); font-family: 'Source Sans 3', system-ui, sans-serif; }
  .stripe { height: 6px; background: var(--red); }
  header { background: var(--navy); color: #fff; padding: 22px 32px 16px; }
  header h1 { font-family: 'Bebas Neue', sans-serif; font-size: 38px; letter-spacing: 1px; font-weight: 400; line-height: 1; }
  header .sub { font-size: 13px; color: #cdd6e3; margin-top: 6px; letter-spacing: .3px; }
  .goldbar { height: 5px; background: var(--gold); }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 22px 32px 60px; }
  .stats { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 22px; }
  .stat { flex: 1; min-width: 160px; background: #fff; border-top: 4px solid var(--navy); padding: 14px 18px; }
  .stat .label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
  .stat .num { font-family: 'Bebas Neue', sans-serif; font-size: 44px; line-height: 1; color: var(--navy); margin-top: 4px; }
  .filters { background: #fff; padding: 14px 18px; border-left: 4px solid var(--gold); margin-bottom: 18px; display: flex; flex-wrap: wrap; gap: 18px; align-items: center; }
  .filters .group { display: flex; flex-direction: column; gap: 4px; }
  .filters label.head { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
  .filters .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { background: var(--cream); border: 1px solid var(--line); padding: 5px 11px; border-radius: 14px; font-size: 13px; cursor: pointer; user-select: none; }
  .chip.on { background: var(--navy); color: #fff; border-color: var(--navy); }
  .filters select, .filters input[type=date] { padding: 5px 8px; border: 1px solid var(--line); background: #fff; font: inherit; }
  nav.tabs { display: flex; gap: 4px; border-bottom: 2px solid var(--navy); margin-bottom: 18px; }
  nav.tabs button { background: transparent; border: 0; padding: 10px 18px; font: inherit; font-size: 14px; cursor: pointer; color: var(--muted); border-bottom: 3px solid transparent; margin-bottom: -2px; letter-spacing: .3px; }
  nav.tabs button.active { color: var(--navy); border-bottom-color: var(--gold); font-weight: 600; }
  .view { display: none; }
  .view.active { display: block; }
  .chart-card { background: #fff; padding: 18px; margin-bottom: 18px; }
  .chart-card h2 { font-family: 'Bebas Neue', sans-serif; font-size: 22px; letter-spacing: .5px; color: var(--navy); margin-bottom: 10px; font-weight: 400; }
  .chart-wrap { position: relative; height: 320px; }
  table { width: 100%; border-collapse: collapse; background: #fff; }
  th { background: var(--cream); font-size: 11px; text-transform: uppercase; letter-spacing: .6px; text-align: left; padding: 9px 12px; color: var(--navy); border-bottom: 2px solid var(--navy); }
  td { padding: 9px 12px; border-bottom: 1px solid var(--line); font-size: 14px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.person { font-weight: 600; }
  .empty { padding: 20px; color: var(--muted); font-style: italic; text-align: center; }
  .person-card { background: #fff; padding: 20px; margin-bottom: 18px; border-top: 4px solid var(--navy); }
  .person-card .head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .person-card h3 { font-family: 'Bebas Neue', sans-serif; font-size: 26px; color: var(--navy); font-weight: 400; letter-spacing: .5px; }
  .person-card .total { font-family: 'Bebas Neue', sans-serif; font-size: 32px; color: var(--gold); }
  .person-card .meta { font-size: 13px; color: var(--muted); margin-top: 4px; }
  .person-card .badges { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .person-card .badge { background: var(--cream); padding: 4px 10px; font-size: 12px; color: var(--ink); border-radius: 3px; }
  .person-card .badge strong { color: var(--navy); }
  .person-card .pcols { display: grid; grid-template-columns: 280px 1fr; gap: 20px; margin: 16px 0; align-items: start; }
  @media (max-width: 820px) { .person-card .pcols { grid-template-columns: 1fr; } }
  .person-card .donut-wrap { position: relative; height: 260px; }
  .person-card .ptable { overflow-x: auto; }
  .person-card .ptable table { font-size: 13px; }
  .person-card .ptable td, .person-card .ptable th { padding: 7px 10px; }
  .person-card .ptable tr.dim td { color: var(--muted); }
  .person-card .trend-wrap { position: relative; height: 200px; background: var(--cream); padding: 12px; margin-top: 8px; }
  .person-card .trend-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 6px; }
  .flags-section { margin: 0 0 22px; }
  .flags-section h2 { font-family: 'Bebas Neue', sans-serif; font-size: 22px; color: var(--navy); letter-spacing: .5px; font-weight: 400; margin-bottom: 8px; }
  .flag-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
  .flag-card { background: #fff; border-left: 4px solid var(--gold); padding: 11px 14px; }
  .flag-card.red { border-left-color: var(--red); }
  .flag-card.info { border-left-color: var(--muted); }
  .flag-card .who { font-weight: 700; color: var(--navy); font-size: 13px; letter-spacing: .2px; }
  .flag-card .what { font-size: 14px; color: var(--ink); margin-top: 3px; }
  .flag-card .why { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .flag-empty { background: #fff; padding: 14px 18px; border-left: 4px solid #1f7a44; font-size: 13px; color: var(--muted); }
  .flag-empty strong { color: #1f7a44; }
  footer .americana { height: 8px; background: repeating-linear-gradient(90deg, var(--red) 0 40px, #fff 40px 80px, var(--navy) 80px 120px); }
  footer .bar { background: var(--navy); color: #cdd6e3; font-size: 12px; padding: 12px 32px; }
</style>
</head>
<body>
<div class="stripe"></div>
<header>
  <h1>RenUSA Team Hours · __YEAR__</h1>
  <div class="sub">__SUB__</div>
</header>
<div class="goldbar"></div>

<div class="wrap">

  <div class="stats">
    <div class="stat"><div class="label">Total hours, ytd</div><div class="num" id="statTotal">—</div></div>
    <div class="stat"><div class="label">Entries</div><div class="num" id="statEntries">—</div></div>
    <div class="stat"><div class="label">Projects</div><div class="num" id="statProjects">—</div></div>
    <div class="stat"><div class="label">Avg hrs/week</div><div class="num" id="statAvg">—</div></div>
  </div>

  <div class="filters">
    <div class="group">
      <label class="head">People</label>
      <div class="chips" id="peopleChips"></div>
    </div>
    <div class="group">
      <label class="head">Project</label>
      <select id="projectSelect"><option value="">All projects</option></select>
    </div>
    <div class="group">
      <label class="head">From</label>
      <input type="date" id="fromDate">
    </div>
    <div class="group">
      <label class="head">To</label>
      <input type="date" id="toDate">
    </div>
    <div class="group">
      <label class="head">&nbsp;</label>
      <button class="chip" id="resetBtn">Reset filters</button>
    </div>
  </div>

  <section class="flags-section" id="flagsSection"></section>

  <nav class="tabs">
    <button data-tab="week" class="active">By week</button>
    <button data-tab="month">By month</button>
    <button data-tab="project">By project</button>
    <button data-tab="person">By person</button>
  </nav>

  <section class="view active" id="view-week">
    <div class="chart-card">
      <h2>Hours per ISO week</h2>
      <div class="chart-wrap"><canvas id="weekChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Week table</h2>
      <div id="weekTableHost"></div>
    </div>
  </section>

  <section class="view" id="view-month">
    <div class="chart-card">
      <h2>Hours per month</h2>
      <div class="chart-wrap"><canvas id="monthChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Month table</h2>
      <div id="monthTableHost"></div>
    </div>
  </section>

  <section class="view" id="view-project">
    <div class="chart-card">
      <h2>Top projects (Pareto)</h2>
      <div class="chart-wrap"><canvas id="projectChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Project table</h2>
      <div id="projectTableHost"></div>
    </div>
  </section>

  <section class="view" id="view-person">
    <div id="personHost"></div>
  </section>

</div>

<footer>
  <div class="americana"></div>
  <div class="bar">RenUSA · Auto-generated from Harvest. Data range __FROM__ → __TO__. Filters and views are client-side.</div>
</footer>

<script>
const DATA = __DATA_JSON__;

// ----- helpers -----
const $ = sel => document.querySelector(sel);
function fmt(n, d=1) { return Number(n).toFixed(d); }

// ISO week: returns "GGGG-Www" matching Python's "%G-W%V"
function isoWeek(dateStr) {
  const d = new Date(dateStr + 'T00:00:00Z');
  const target = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const dayNr = (target.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = target.valueOf();
  const jan4 = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const jan4DayNr = (jan4.getUTCDay() + 6) % 7;
  jan4.setUTCDate(jan4.getUTCDate() - jan4DayNr + 3);
  const week = 1 + Math.round((firstThursday - jan4.valueOf()) / (7 * 24 * 3600 * 1000));
  return target.getUTCFullYear() + '-W' + String(week).padStart(2, '0');
}

function monthKey(dateStr) { return dateStr.slice(0, 7); }

// ----- state -----
const STATE = {
  people: new Set(DATA.meta.people),
  project: '',
  from: DATA.meta.from,
  to: DATA.meta.to,
};

// ----- filter -----
function filtered() {
  return DATA.entries.filter(e => {
    if (!STATE.people.has(e.person)) return false;
    if (STATE.project && e.project !== STATE.project) return false;
    if (e.date < STATE.from) return false;
    if (e.date > STATE.to) return false;
    return true;
  });
}

// ----- aggregations -----
function sumBy(entries, keyFn) {
  const m = new Map();
  for (const e of entries) {
    const k = keyFn(e);
    m.set(k, (m.get(k) || 0) + e.hours);
  }
  return m;
}

function sumByPerson(entries, keyFn) {
  // returns Map<keyFn(e), Map<person, hours>>
  const m = new Map();
  for (const e of entries) {
    const k = keyFn(e);
    if (!m.has(k)) m.set(k, new Map());
    const inner = m.get(k);
    inner.set(e.person, (inner.get(e.person) || 0) + e.hours);
  }
  return m;
}

// ----- charts (one instance each, destroyed on re-render) -----
const CHARTS = {};
function destroy(name) {
  if (CHARTS[name]) { CHARTS[name].destroy(); delete CHARTS[name]; }
}

const PEOPLE_COLORS = {};
const PALETTE = ['#0A2240', '#C8963C', '#B03030', '#3a6e7c', '#7d4f7a', '#4a6a3a'];
function colorFor(person) {
  if (!(person in PEOPLE_COLORS)) {
    PEOPLE_COLORS[person] = PALETTE[Object.keys(PEOPLE_COLORS).length % PALETTE.length];
  }
  return PEOPLE_COLORS[person];
}

// ----- flags -----
const FLAG_THRESHOLDS = {
  LOW_WEEK: 30,             // light week
  HIGH_WEEK: 55,            // heavy week
  STALE_DAYS: 7,            // most recent entry older than this = stale
  TREND_DROP_RATIO: 0.5,    // last week < 50% of 4-wk avg
  TREND_SURGE_RATIO: 1.5,   // last week > 150% of 4-wk avg
  TREND_MIN_AVG: 15,        // only flag drop/surge if 4-wk avg >= this
  QUIET_LOOKBACK_WEEKS: 8,  // window to identify "actively worked" projects
  QUIET_RECENT_WEEKS: 2,    // ...that have had 0h in the last N weeks
  QUIET_MIN_AVG: 5,         // "actively worked" = >= this many h/wk avg
};

// Most recent ISO week whose Sunday is on or before STATE.to.
// If today is mid-week, current week is partial; use the prior week.
function lastCompletedWeek(allWeeks) {
  if (!allWeeks.length) return null;
  const toDate = new Date(STATE.to + 'T00:00:00Z');
  const dayOfWeek = (toDate.getUTCDay() + 6) % 7; // Mon=0..Sun=6
  const currentWeek = isoWeek(STATE.to);
  if (dayOfWeek < 6 && allWeeks[allWeeks.length - 1] === currentWeek && allWeeks.length >= 2) {
    return allWeeks[allWeeks.length - 2];
  }
  return allWeeks[allWeeks.length - 1];
}

function daysBetween(aIso, bIso) {
  return Math.floor((new Date(bIso) - new Date(aIso)) / 86400000);
}

function computeFlags(entries) {
  const flags = [];
  const people = [...STATE.people].sort();
  if (!people.length || !entries.length) return flags;

  const byWeekPerson = sumByPerson(entries, e => isoWeek(e.date));
  const allWeeks = [...byWeekPerson.keys()].sort();
  if (!allWeeks.length) return flags;

  const refWeek = lastCompletedWeek(allWeeks);

  for (const p of people) {
    const personEntries = entries.filter(e => e.person === p);
    if (!personEntries.length) continue;

    // ---- 1. Hours in last completed week ----
    const lwHours = byWeekPerson.get(refWeek)?.get(p) || 0;
    if (lwHours === 0) {
      flags.push({ severity: 'red', who: p,
        what: `No hours logged in ${refWeek}`,
        why: `Most recent completed week shows zero time entries.` });
    } else if (lwHours < FLAG_THRESHOLDS.LOW_WEEK) {
      flags.push({ severity: 'gold', who: p,
        what: `Light week: ${lwHours.toFixed(1)}h in ${refWeek}`,
        why: `Below ${FLAG_THRESHOLDS.LOW_WEEK}h threshold.` });
    } else if (lwHours > FLAG_THRESHOLDS.HIGH_WEEK) {
      flags.push({ severity: 'gold', who: p,
        what: `Heavy week: ${lwHours.toFixed(1)}h in ${refWeek}`,
        why: `Above ${FLAG_THRESHOLDS.HIGH_WEEK}h threshold — watch for burnout.` });
    }

    // ---- 2. Trend vs prior 4-week average ----
    const refIdx = allWeeks.indexOf(refWeek);
    if (refIdx >= 4) {
      const priorWeeks = allWeeks.slice(refIdx - 4, refIdx);
      const priorHours = priorWeeks.map(w => byWeekPerson.get(w)?.get(p) || 0);
      const priorAvg = priorHours.reduce((s, h) => s + h, 0) / priorHours.length;
      if (priorAvg >= FLAG_THRESHOLDS.TREND_MIN_AVG) {
        const ratio = lwHours / priorAvg;
        if (ratio < FLAG_THRESHOLDS.TREND_DROP_RATIO) {
          flags.push({ severity: 'gold', who: p,
            what: `Sharp drop: ${lwHours.toFixed(1)}h vs ${priorAvg.toFixed(1)}h avg`,
            why: `${Math.round((1 - ratio) * 100)}% below their prior 4-week baseline.` });
        } else if (ratio > FLAG_THRESHOLDS.TREND_SURGE_RATIO) {
          flags.push({ severity: 'gold', who: p,
            what: `Surge: ${lwHours.toFixed(1)}h vs ${priorAvg.toFixed(1)}h avg`,
            why: `${Math.round((ratio - 1) * 100)}% above their prior 4-week baseline.` });
        }
      }
    }

    // ---- 3. Stale logging ----
    const mostRecentDate = personEntries.map(e => e.date).sort().slice(-1)[0];
    const daysSince = daysBetween(mostRecentDate, STATE.to);
    if (daysSince > FLAG_THRESHOLDS.STALE_DAYS) {
      flags.push({ severity: 'red', who: p,
        what: `Stale logging: last entry ${daysSince} days ago`,
        why: `Last time entry on ${mostRecentDate}. May not be tracking time consistently.` });
    }

    // ---- 4. Going quiet on a project ----
    const lookback = FLAG_THRESHOLDS.QUIET_LOOKBACK_WEEKS;
    const recent = FLAG_THRESHOLDS.QUIET_RECENT_WEEKS;
    if (allWeeks.length >= lookback) {
      const lookbackWeeks = new Set(allWeeks.slice(-lookback));
      const recentWeeks = new Set(allWeeks.slice(-recent));
      const lookbackHours = new Map();
      const recentHours = new Map();
      for (const e of personEntries) {
        const w = isoWeek(e.date);
        if (lookbackWeeks.has(w)) lookbackHours.set(e.project, (lookbackHours.get(e.project) || 0) + e.hours);
        if (recentWeeks.has(w)) recentHours.set(e.project, (recentHours.get(e.project) || 0) + e.hours);
      }
      for (const [proj, h] of lookbackHours.entries()) {
        const avgPerWeek = h / lookback;
        if (avgPerWeek >= FLAG_THRESHOLDS.QUIET_MIN_AVG && (recentHours.get(proj) || 0) === 0) {
          flags.push({ severity: 'gold', who: p,
            what: `Quiet on "${proj}"`,
            why: `${h.toFixed(1)}h over prior ${lookback} weeks, 0h in last ${recent}.` });
        }
      }
    }
  }

  const sevOrder = { red: 0, gold: 1, info: 2 };
  flags.sort((a, b) => sevOrder[a.severity] - sevOrder[b.severity] || a.who.localeCompare(b.who));
  return flags;
}

function renderFlagCard(f) {
  return `
    <div class="flag-card ${f.severity}">
      <div class="who">${escapeHtml(f.who)}</div>
      <div class="what">${escapeHtml(f.what)}</div>
      <div class="why">${escapeHtml(f.why)}</div>
    </div>`;
}

function renderFlags(entries) {
  const flags = computeFlags(entries);
  $('#flagsSection').innerHTML = `
    <h2>Things to pay attention to ${flags.length ? `(${flags.length})` : ''}</h2>
    ${flags.length === 0
      ? `<div class="flag-empty"><strong>✓ All clear.</strong> Nothing flagged in the current filter.</div>`
      : `<div class="flag-list">${flags.map(renderFlagCard).join('')}</div>`}
  `;
}

// ----- renderers -----
function renderStats(entries) {
  const total = entries.reduce((s, e) => s + e.hours, 0);
  $('#statTotal').textContent = fmt(total);
  $('#statEntries').textContent = entries.length.toLocaleString();
  $('#statProjects').textContent = new Set(entries.map(e => e.project)).size;
  const weeks = new Set(entries.map(e => isoWeek(e.date))).size || 1;
  $('#statAvg').textContent = fmt(total / weeks);
}

// Build chart datasets + table for a time-series view.
// When exactly one person is selected, stack by top-8 projects + Other.
// Otherwise, stack by person (cross-person comparison).
function buildTimeSeries(entries, periodFn, periodLabel) {
  const periods = [...new Set(entries.map(e => periodFn(e.date)))].sort();
  const stackByProject = STATE.people.size === 1;
  const PROJ_PALETTE = ['#0A2240', '#C8963C', '#B03030', '#3a6e7c', '#7d4f7a', '#4a6a3a', '#a86b3c', '#5b6470', '#999'];
  const TOP_N = 8;

  let datasets, tableHeaders, tableRows;

  if (stackByProject) {
    const projTotals = sumBy(entries, e => e.project);
    const sorted = [...projTotals.entries()].sort((a, b) => b[1] - a[1]);
    const topProjects = sorted.slice(0, TOP_N).map(([p]) => p);
    const otherCount = Math.max(0, sorted.length - TOP_N);
    const OTHER = '__OTHER__';
    const cats = otherCount > 0 ? [...topProjects, OTHER] : topProjects;
    const catLabel = c => c === OTHER ? `Other (${otherCount} projects)` : c;

    const agg = new Map();
    periods.forEach(p => agg.set(p, new Map()));
    for (const e of entries) {
      const per = periodFn(e.date);
      const cat = topProjects.includes(e.project) ? e.project : OTHER;
      const inner = agg.get(per);
      inner.set(cat, (inner.get(cat) || 0) + e.hours);
    }

    datasets = cats.map((c, i) => ({
      label: catLabel(c),
      data: periods.map(per => +(agg.get(per).get(c) || 0).toFixed(1)),
      backgroundColor: PROJ_PALETTE[i % PROJ_PALETTE.length],
      stack: 'team',
    }));

    tableHeaders = `<th>${escapeHtml(periodLabel)}</th>${cats.map(c => `<th class="num">${escapeHtml(catLabel(c))}</th>`).join('')}<th class="num">Total</th>`;
    tableRows = periods.map(per => {
      const cells = cats.map(c => fmt(agg.get(per).get(c) || 0));
      const total = [...agg.get(per).values()].reduce((s, h) => s + h, 0);
      return `<tr><td>${per}</td>${cells.map(v => `<td class="num">${v}</td>`).join('')}<td class="num"><b>${fmt(total)}</b></td></tr>`;
    }).join('');
  } else {
    const byPeriodPerson = sumByPerson(entries, e => periodFn(e.date));
    const people = [...STATE.people].sort();

    datasets = people.map(p => ({
      label: p,
      data: periods.map(per => +(byPeriodPerson.get(per)?.get(p) || 0).toFixed(1)),
      backgroundColor: colorFor(p),
      stack: 'team',
    }));

    tableHeaders = `<th>${escapeHtml(periodLabel)}</th>${people.map(p => `<th class="num">${escapeHtml(p)}</th>`).join('')}<th class="num">Total</th>`;
    tableRows = periods.map(per => {
      const cells = people.map(p => fmt(byPeriodPerson.get(per)?.get(p) || 0));
      const total = [...(byPeriodPerson.get(per)?.values() || [])].reduce((s, h) => s + h, 0);
      return `<tr><td>${per}</td>${cells.map(v => `<td class="num">${v}</td>`).join('')}<td class="num"><b>${fmt(total)}</b></td></tr>`;
    }).join('');
  }

  return { periods, datasets, tableHeaders, tableRows, stackByProject };
}

function renderWeek(entries) {
  destroy('week');
  const { periods, datasets, tableHeaders, tableRows, stackByProject } =
    buildTimeSeries(entries, isoWeek, 'Week');

  CHARTS.week = new Chart($('#weekChart'), {
    type: 'bar',
    data: { labels: periods, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Hours' } } },
      plugins: {
        legend: { position: 'bottom' },
        title: { display: true, text: stackByProject ? 'Stacked by project' : 'Stacked by person', font: { size: 11, weight: 'normal' }, color: '#5b6470' },
      },
    },
  });

  $('#weekTableHost').innerHTML = periods.length
    ? `<table><tr>${tableHeaders}</tr>${tableRows}</table>`
    : `<div class="empty">No data in this filter.</div>`;
}

function renderMonth(entries) {
  destroy('month');
  const { periods, datasets, tableHeaders, tableRows, stackByProject } =
    buildTimeSeries(entries, monthKey, 'Month');

  CHARTS.month = new Chart($('#monthChart'), {
    type: 'bar',
    data: { labels: periods, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Hours' } } },
      plugins: {
        legend: { position: 'bottom' },
        title: { display: true, text: stackByProject ? 'Stacked by project' : 'Stacked by person', font: { size: 11, weight: 'normal' }, color: '#5b6470' },
      },
    },
  });

  $('#monthTableHost').innerHTML = periods.length
    ? `<table><tr>${tableHeaders}</tr>${tableRows}</table>`
    : `<div class="empty">No data in this filter.</div>`;
}

function renderProject(entries) {
  destroy('project');
  const byProj = sumBy(entries, e => e.project);
  const byProjClient = new Map();
  for (const e of entries) {
    if (!byProjClient.has(e.project)) byProjClient.set(e.project, e.client || '');
  }
  const sorted = [...byProj.entries()].sort((a, b) => b[1] - a[1]);
  const top = sorted.slice(0, 15);
  CHARTS.project = new Chart($('#projectChart'), {
    type: 'bar',
    data: {
      labels: top.map(([p]) => p),
      datasets: [{ label: 'Hours', data: top.map(([_, h]) => +h.toFixed(1)), backgroundColor: '#0A2240' }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      scales: { x: { beginAtZero: true, title: { display: true, text: 'Hours' } } },
      plugins: { legend: { display: false } },
    },
  });

  const total = sorted.reduce((s, [_, h]) => s + h, 0);
  const rows = sorted.map(([p, h]) => {
    const pct = total ? ((h / total) * 100).toFixed(1) : '0.0';
    return `<tr><td>${escapeHtml(byProjClient.get(p))}</td><td>${escapeHtml(p)}</td><td class="num">${fmt(h)}</td><td class="num">${pct}%</td></tr>`;
  }).join('');
  $('#projectTableHost').innerHTML = sorted.length
    ? `<table><tr><th>Client</th><th>Project</th><th class="num">Hours</th><th class="num">% of total</th></tr>${rows}</table>`
    : `<div class="empty">No data in this filter.</div>`;
}

function renderPerson(entries) {
  // destroy any prior per-person chart instances
  Object.keys(CHARTS)
    .filter(k => k.startsWith('donut-') || k.startsWith('weekly-'))
    .forEach(destroy);

  const people = [...STATE.people].sort();
  const byPerson = sumBy(entries, e => e.person);
  const totalAll = entries.reduce((s, e) => s + e.hours, 0) || 1;

  // Determine latest month present in filter window (for "active now" column).
  // Use STATE.to so the column has a stable meaning regardless of data sparsity.
  const latestMonth = STATE.to.slice(0, 7);
  const latestMonthLabel = new Date(latestMonth + '-01T00:00:00Z')
    .toLocaleString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });

  const host = $('#personHost');
  host.innerHTML = '';
  if (!people.length) {
    host.innerHTML = `<div class="empty">No people selected — use the people chips above.</div>`;
    return;
  }

  const PROJECT_PALETTE = [
    '#0A2240', '#C8963C', '#B03030', '#3a6e7c', '#7d4f7a',
    '#4a6a3a', '#a86b3c', '#5b6470', '#8c4a4a', '#2e5d6f',
    '#9c8033', '#6b5b7d',
  ];

  for (const p of people) {
    const personEntries = entries.filter(e => e.person === p);
    const total = byPerson.get(p) || 0;
    const pct = ((total / totalAll) * 100).toFixed(1);

    // Per-project aggregates for this person.
    const projHours = new Map();
    const projClient = new Map();
    const projLastDate = new Map();
    const projLatestMonthHours = new Map();
    for (const e of personEntries) {
      projHours.set(e.project, (projHours.get(e.project) || 0) + e.hours);
      if (!projClient.has(e.project)) projClient.set(e.project, e.client || '');
      if (!projLastDate.has(e.project) || e.date > projLastDate.get(e.project)) {
        projLastDate.set(e.project, e.date);
      }
      if (e.date.slice(0, 7) === latestMonth) {
        projLatestMonthHours.set(e.project, (projLatestMonthHours.get(e.project) || 0) + e.hours);
      }
    }
    const projectsSorted = [...projHours.entries()].sort((a, b) => b[1] - a[1]);
    const projectCount = projectsSorted.length;
    const personTotal = total || 1;

    // Latest-month activity for this person (across all their projects).
    const latestMonthTotal = [...projLatestMonthHours.values()].reduce((s, h) => s + h, 0);
    const mostRecentDate = personEntries.length
      ? personEntries.map(e => e.date).sort().slice(-1)[0]
      : '—';

    // Card scaffolding.
    const safe = p.replace(/\W+/g, '_');
    const card = document.createElement('div');
    card.className = 'person-card';

    // Project rows (table). Highlight rows with no latest-month activity in muted style.
    const projRows = projectsSorted.map(([pr, h]) => {
      const share = ((h / personTotal) * 100).toFixed(1);
      const lm = projLatestMonthHours.get(pr) || 0;
      const lastSeen = projLastDate.get(pr) || '';
      const dim = lm === 0 ? ' class="dim"' : '';
      return `<tr${dim}>
        <td>${escapeHtml(pr)}</td>
        <td>${escapeHtml(projClient.get(pr) || '')}</td>
        <td class="num">${fmt(h)}</td>
        <td class="num">${share}%</td>
        <td>${lastSeen}</td>
        <td class="num">${lm > 0 ? fmt(lm) : '—'}</td>
      </tr>`;
    }).join('');

    card.innerHTML = `
      <div class="head">
        <div>
          <h3>${escapeHtml(p)}</h3>
          <div class="meta">${pct}% of filtered team total · ${personEntries.length.toLocaleString()} entries · most recent entry ${mostRecentDate}</div>
          <div class="badges">
            <div class="badge">Projects: <strong>${projectCount}</strong></div>
            <div class="badge">${escapeHtml(latestMonthLabel)} hours: <strong>${fmt(latestMonthTotal)}</strong></div>
            <div class="badge">Top project: <strong>${escapeHtml(projectsSorted[0]?.[0] || '—')}</strong></div>
          </div>
        </div>
        <div class="total">${fmt(total)} h</div>
      </div>

      <div class="pcols">
        <div class="donut-wrap"><canvas id="donut-${safe}"></canvas></div>
        <div class="ptable">
          ${projectsSorted.length
            ? `<table>
                 <tr>
                   <th>Project</th><th>Client</th>
                   <th class="num">Hours</th><th class="num">% of their time</th>
                   <th>Last entry</th><th class="num">${escapeHtml(latestMonthLabel)} hrs</th>
                 </tr>
                 ${projRows}
               </table>`
            : `<div class="empty">No entries for this person in the current filter.</div>`}
        </div>
      </div>

      <div class="trend-title">Weekly hours — ${escapeHtml(p)}</div>
      <div class="trend-wrap"><canvas id="weekly-${safe}"></canvas></div>
    `;
    host.appendChild(card);

    // Donut: top 8 projects + "Other" if many.
    const TOP_N = 8;
    let donutLabels, donutData;
    if (projectsSorted.length > TOP_N) {
      const top = projectsSorted.slice(0, TOP_N);
      const rest = projectsSorted.slice(TOP_N).reduce((s, [, h]) => s + h, 0);
      donutLabels = [...top.map(([pr]) => pr), `Other (${projectsSorted.length - TOP_N})`];
      donutData = [...top.map(([, h]) => +h.toFixed(1)), +rest.toFixed(1)];
    } else {
      donutLabels = projectsSorted.map(([pr]) => pr);
      donutData = projectsSorted.map(([, h]) => +h.toFixed(1));
    }
    if (donutData.length) {
      CHARTS['donut-' + safe] = new Chart(document.getElementById('donut-' + safe), {
        type: 'doughnut',
        data: {
          labels: donutLabels,
          datasets: [{
            data: donutData,
            backgroundColor: donutLabels.map((_, i) => PROJECT_PALETTE[i % PROJECT_PALETTE.length]),
            borderWidth: 1,
            borderColor: '#fff',
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 } } },
            tooltip: {
              callbacks: {
                label: ctx => `${ctx.label}: ${ctx.parsed.toFixed(1)}h (${(ctx.parsed/personTotal*100).toFixed(1)}%)`,
              },
            },
          },
          cutout: '55%',
        },
      });
    }

    // Weekly trend: full-width line chart.
    const byWeek = sumBy(personEntries, e => isoWeek(e.date));
    const weeks = [...byWeek.keys()].sort();
    CHARTS['weekly-' + safe] = new Chart(document.getElementById('weekly-' + safe), {
      type: 'line',
      data: {
        labels: weeks,
        datasets: [{
          label: 'Hours',
          data: weeks.map(w => +byWeek.get(w).toFixed(1)),
          borderColor: colorFor(p),
          backgroundColor: colorFor(p) + '33',
          fill: true,
          tension: 0.25,
          pointRadius: 3,
          pointHoverRadius: 5,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { intersect: false, mode: 'index' } },
        scales: {
          x: { ticks: { font: { size: 10 } } },
          y: { beginAtZero: true, ticks: { font: { size: 10 } }, title: { display: true, text: 'Hours', font: { size: 11 } } },
        },
      },
    });
  }
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

// ----- wire-up -----
function renderAll() {
  const entries = filtered();
  renderFlags(entries);
  renderStats(entries);
  renderWeek(entries);
  renderMonth(entries);
  renderProject(entries);
  renderPerson(entries);
}

function buildPeopleChips() {
  const host = $('#peopleChips');
  host.innerHTML = '';
  for (const p of DATA.meta.people) {
    const el = document.createElement('span');
    el.className = 'chip on';
    el.textContent = p;
    el.dataset.person = p;
    el.addEventListener('click', () => {
      if (STATE.people.has(p)) { STATE.people.delete(p); el.classList.remove('on'); }
      else { STATE.people.add(p); el.classList.add('on'); }
      renderAll();
    });
    host.appendChild(el);
  }
}

function buildProjectSelect() {
  const sel = $('#projectSelect');
  for (const pr of DATA.meta.projects) {
    const o = document.createElement('option');
    o.value = pr; o.textContent = pr;
    sel.appendChild(o);
  }
  sel.addEventListener('change', e => { STATE.project = e.target.value; renderAll(); });
}

function wireDates() {
  const f = $('#fromDate'), t = $('#toDate');
  f.value = STATE.from; t.value = STATE.to;
  f.min = DATA.meta.from; f.max = DATA.meta.to;
  t.min = DATA.meta.from; t.max = DATA.meta.to;
  f.addEventListener('change', e => { STATE.from = e.target.value; renderAll(); });
  t.addEventListener('change', e => { STATE.to = e.target.value; renderAll(); });
}

function wireTabs() {
  document.querySelectorAll('nav.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('nav.tabs button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      document.getElementById('view-' + btn.dataset.tab).classList.add('active');
    });
  });
}

$('#resetBtn').addEventListener('click', () => {
  STATE.people = new Set(DATA.meta.people);
  STATE.project = '';
  STATE.from = DATA.meta.from;
  STATE.to = DATA.meta.to;
  $('#projectSelect').value = '';
  $('#fromDate').value = STATE.from;
  $('#toDate').value = STATE.to;
  document.querySelectorAll('#peopleChips .chip').forEach(c => c.classList.add('on'));
  renderAll();
});

buildPeopleChips();
buildProjectSelect();
wireDates();
wireTabs();
renderAll();
</script>
</body>
</html>
"""


def render_html(payload):
    meta = payload["meta"]
    sub = (f"Data range {meta['from']} → {meta['to']}  ·  "
           f"{meta['entry_count']} entries  ·  generated {meta['generated']}")
    html = (HTML_TEMPLATE
            .replace("__YEAR__", str(meta["year"]))
            .replace("__SUB__", sub)
            .replace("__FROM__", meta["from"])
            .replace("__TO__", meta["to"])
            .replace("__DATA_JSON__", json.dumps(payload)))
    return html


def main():
    if not ACCOUNT_ID or not TOKEN:
        die("Missing HARVEST_ACCOUNT_ID or HARVEST_TOKEN environment variables.")
    today = dt.date.today()
    from_date = dt.date(YEAR, 1, 1)
    to_date = today

    keep_ids = resolve_team_ids()
    entries = fetch_entries(keep_ids, from_date, to_date)
    payload = build_payload(entries, from_date, to_date)

    os.makedirs("docs", exist_ok=True)
    with open("docs/data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(render_html(payload))

    print(f"Wrote docs/index.html with {len(entries)} entries, "
          f"{len(payload['meta']['people'])} people, "
          f"{len(payload['meta']['projects'])} projects.")


if __name__ == "__main__":
    main()

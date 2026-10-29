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
    # Returns (team entries for the dashboard, raw entries for everyone).
    # The raw list feeds the budget actuals, which count everyone on a
    # project the same way Harvest's budget report does.
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
    return out, raw


def fetch_budgets():
    # Harvest's own Project Budget report: budget, spent and remaining per
    # project, all-time and across everyone who logged time (not just the
    # team). Same numbers as Reports -> Project Budget in the Harvest UI.
    # Non-fatal: if the token can't read it, the hours dashboard still builds.
    try:
        raw = get_all("/reports/project_budget", "results",
                      {"is_active": "true"})
    except SystemExit:
        print("WARNING: could not read the project budget report; "
              "Budget tab will be empty.", file=sys.stderr)
        return []
    out = []
    for r in raw:
        budget = r.get("budget")
        if not budget or r.get("budget_by") == "none":
            continue  # no budget set on this project
        out.append({
            "project_id": r.get("project_id"),
            "budget_by": r.get("budget_by"),
            "project": r.get("project_name", ""),
            "client": r.get("client_name", ""),
            # "project_cost" / "task_fees" budgets are dollars; the rest are hours
            "unit": "$" if r.get("budget_by") in ("project_cost", "task_fees") else "h",
            "monthly": bool(r.get("budget_is_monthly")),
            "budget": float(budget),
            "spent": float(r.get("budget_spent") or 0.0),
            "remaining": float(r.get("budget_remaining") or 0.0),
        })
    print("Budgets: %d active projects with a budget set." % len(out))
    return out


def budget_scope(b):
    # Per-task and per-person budgets only count time on the tasks or people
    # that have their own budget. Returns (time entry field, allowed ids), or
    # None when all of the project's time counts.
    if b["budget_by"] in ("task", "task_fees"):
        path, key, field = "/task_assignments", "task_assignments", "task"
    elif b["budget_by"] == "person":
        path, key, field = "/user_assignments", "user_assignments", "user"
    else:
        return None
    try:
        rows = get_all("/projects/%s%s" % (b["project_id"], path), key)
    except SystemExit:
        print("WARNING: could not read %s for %s; counting all its time."
              % (key, b["project"]), file=sys.stderr)
        return None
    return field, {(r.get(field) or {}).get("id") for r in rows if r.get("budget")}


def add_monthly_actuals(budgets, raw_entries, to_date):
    # Harvest's budget report only gives the current month for monthly
    # budgets, so rebuild every month from the time entries: hours for hour
    # budgets, billable amount (hours x billable rate) for dollar budgets.
    # rounded_hours matches Harvest's reports when time rounding is on.
    by_id = {b["project_id"]: b for b in budgets}
    scopes = {b["project_id"]: budget_scope(b) for b in budgets}
    tallies = {b["project_id"]: {"all": {}, "billable": {}} for b in budgets}
    for te in raw_entries:
        pid = (te.get("project") or {}).get("id")
        b = by_id.get(pid)
        if not b:
            continue
        scope = scopes[pid]
        if scope and (te.get(scope[0]) or {}).get("id") not in scope[1]:
            continue  # task or person without its own budget: Harvest skips it
        hrs = float(te.get("rounded_hours") or te.get("hours") or 0.0)
        billable = bool(te.get("billable"))
        if b["unit"] == "$":
            all_val = bill_val = hrs * float(te.get("billable_rate") or 0.0) if billable else 0.0
        else:
            all_val, bill_val = hrs, (hrs if billable else 0.0)
        day = te.get("spent_date") or ""
        t = tallies[pid]
        t["all"][day] = t["all"].get(day, 0.0) + all_val
        t["billable"][day] = t["billable"].get(day, 0.0) + bill_val
    this_month = to_date.isoformat()[:7]

    def by_month(daily):
        out = {}
        for d, v in daily.items():
            out[d[:7]] = out.get(d[:7], 0.0) + v
        return out

    def harvest_view(b, actuals):
        # What Harvest's "spent" should equal if these actuals are right.
        return actuals.get(this_month, 0.0) if b["monthly"] else sum(actuals.values())

    for b in budgets:
        t = tallies[b["project_id"]]
        # Some hour budgets count only billable time. Use whichever reading
        # lines up with Harvest's own number for this project.
        daily = min(t.values(), key=lambda a: abs(harvest_view(b, by_month(a)) - b["spent"]))
        b["actuals"] = {m: round(v, 2) for m, v in sorted(by_month(daily).items())}
        # Daily totals let the Budget tab add up any date range.
        b["daily"] = {d: round(v, 2) for d, v in sorted(daily.items()) if v}
        ours = harvest_view(b, b["actuals"])
        if not b["monthly"]:
            # Total budgets are all-time; whatever Harvest counts beyond this
            # year's entries was spent before the dashboard's window.
            b["before"] = round(max(b["spent"] - ours, 0.0), 2)
            ours += b["before"]
        if abs(ours - b["spent"]) > max(1.0, 0.01 * b["spent"]):
            print("NOTE: %s / %s (%s budget): rebuilt %s%.2f vs Harvest %.2f" %
                  (b["client"], b["project"], b["budget_by"], b["unit"], ours, b["spent"]))


def build_payload(entries, from_date, to_date, budgets=None):
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
        "budgets": budgets or [],
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
  .budget-summary { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 18px; }
  .budget-summary .stat.red { border-top-color: var(--red); }
  .budget-summary .stat.red .num { color: var(--red); }
  .budget-summary .stat.gold { border-top-color: var(--gold); }
  .budget-summary .stat.gold .num { color: var(--gold); }
  .budget-note { font-size: 12px; color: var(--muted); margin-bottom: 10px; }
  .bbar { position: relative; height: 10px; background: var(--cream); min-width: 120px; }
  .bbar span { position: absolute; left: 0; top: 0; bottom: 0; background: #1f7a44; }
  .bbar.gold span { background: var(--gold); }
  .bbar.red span { background: var(--red); }
  td.over { color: var(--red); font-weight: 700; }
  tr.brow { cursor: pointer; }
  tr.brow:hover td { background: #faf8f3; }
  tr.brow td.proj::before { content: '▸'; display: inline-block; width: 14px; color: var(--muted); }
  tr.brow.open td.proj::before { content: '▾'; }
  tr.brow.open td { background: #faf8f3; border-bottom-color: transparent; }
  td .sub { display: block; font-size: 11px; color: var(--muted); white-space: nowrap; }
  tr.bdetail > td { background: #faf8f3; padding: 4px 16px 18px; }
  .bdetail .figs { display: flex; gap: 22px; flex-wrap: wrap; font-size: 13px; color: var(--muted); margin: 4px 0 10px; }
  .bdetail .figs strong { color: var(--navy); font-size: 15px; }
  .bdetail .chart-wrap { height: 260px; background: #fff; padding: 10px; }
  .bva { overflow-x: auto; margin-bottom: 18px; }
  .bva h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin: 6px 0 8px; font-weight: 600; }
  .bva table { font-size: 13px; }
  .bva td, .bva th { padding: 7px 10px; white-space: nowrap; }
  .bva td.cell { text-align: right; font-variant-numeric: tabular-nums; line-height: 1.25; }
  .bva td.cell .var { display: block; font-size: 11px; font-weight: 700; }
  .bva td.cell.under .var { color: #1f7a44; }
  .bva td.cell.over { background: #fbeaea; }
  .bva td.cell.over .var { color: var(--red); }
  .bva td.cell.open .var { color: var(--muted); font-weight: 400; }
  .bva tr.tally td { background: var(--cream); font-size: 12px; color: var(--muted); }
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
    <button data-tab="budget" class="active">Budget vs. actual</button>
    <button data-tab="week">By week</button>
    <button data-tab="month">By month</button>
    <button data-tab="project">By project</button>
    <button data-tab="person">By person</button>
    <button data-tab="projcards">Project cards</button>
  </nav>

  <section class="view active" id="view-budget">
    <div class="filters budget-range">
      <div class="group">
        <label class="head">Time frame</label>
        <div class="chips" id="budgetPresets"></div>
      </div>
      <div class="group">
        <label class="head">From</label>
        <input type="date" id="budgetFrom">
      </div>
      <div class="group">
        <label class="head">To</label>
        <input type="date" id="budgetTo">
      </div>
    </div>
    <div class="budget-summary" id="budgetSummary"></div>
    <div class="chart-card">
      <h2>Projects by % of budget used</h2>
      <div class="budget-note">For everyone on the project, using the time frame above (the people, project and date filters at the top don't apply here). Monthly budgets are the monthly amount times the months covered, with partial months prorated by day and nothing counted before a project's first logged time. Total budgets show everything used from the project's start through the end date, plus how much of that landed in the time frame. Click a project for its month-by-month detail.</div>
      <div id="budgetTableHost"></div>
    </div>
  </section>

  <section class="view" id="view-week">
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
    <div class="chart-card">
      <h2>Budget vs. actual by month</h2>
      <div class="budget-note">Each month's actual against the Harvest budget, for everyone on the project: billable dollars for dollar budgets, hours for hour budgets. Green is under budget, red is over. The current month is still open, so it only turns red once it's already over. The project and date filters apply; the people filter doesn't.</div>
      <div id="bvaHost"></div>
    </div>
  </section>

  <section class="view" id="view-person">
    <div id="personHost"></div>
  </section>

  <section class="view" id="view-projcards">
    <div class="budget-note">One card per project in the current filter. Projects with a Harvest budget come first, most over budget at the top. The budget bar is Harvest's all-time total; the hours and people table follow the filters above.</div>
    <div id="projCardHost"></div>
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

  // ---- 5. Project budgets (only projects that appear in the current filter) ----
  const inFilter = new Set(entries.map(e => (e.client || '') + '|' + e.project));
  for (const b of DATA.budgets || []) {
    if (!b.budget || !inFilter.has(b.client + '|' + b.project)) continue;
    const pct = (b.spent / b.budget) * 100;
    if (pct < BUDGET_WARN_PCT) continue;
    const amt = n => b.unit === '$' ? '$' + Math.round(n).toLocaleString() : n.toFixed(1) + 'h';
    flags.push({ severity: pct > 100 ? 'red' : 'gold', who: b.project,
      what: pct > 100 ? `Over budget: ${pct.toFixed(0)}% used` : `Nearing budget: ${pct.toFixed(0)}% used`,
      why: `${amt(b.spent)} of ${amt(b.budget)}${b.monthly ? ' this month' : ''}` +
           (b.remaining < 0 ? `, ${amt(-b.remaining)} over.` : `, ${amt(b.remaining)} left.`) });
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

// Budget tab: Harvest budgets measured over a chosen time frame, for everyone
// on the project. Independent of the filters at the top of the page.
const BUDGET_WARN_PCT = 80;  // gold at or above this, red over 100
const BSTATE = { from: '', to: '', preset: 'month', open: new Set() };

const utcDate = s => new Date(s + 'T00:00:00Z');
const isoDay = d => d.toISOString().slice(0, 10);
function monthEnd(m) { return isoDay(new Date(Date.UTC(+m.slice(0, 4), +m.slice(5, 7), 0))); }
function daysInMonth(m) { return +monthEnd(m).slice(8); }
function monthsBetween(firstM, lastM) {
  const out = [];
  for (let d = utcDate(firstM + '-01'); isoDay(d).slice(0, 7) <= lastM; d.setUTCMonth(d.getUTCMonth() + 1)) {
    out.push(isoDay(d).slice(0, 7));
  }
  return out;
}
const budgetAmt = (n, unit) => unit === '$' ? '$' + Math.round(n).toLocaleString() : fmt(n) + 'h';
const budgetLevel = pct => pct > 100 ? 'red' : pct >= BUDGET_WARN_PCT ? 'gold' : '';

function budgetPresets() {
  const m = DATA.meta.to.slice(0, 7), y = +m.slice(0, 4), mo = +m.slice(5, 7);
  const prevM = isoDay(new Date(Date.UTC(y, mo - 2, 1))).slice(0, 7);
  const q = Math.floor((mo - 1) / 3);
  const [ly, lq] = q === 0 ? [y - 1, 3] : [y, q - 1];
  const qFrom = (yy, qq) => isoDay(new Date(Date.UTC(yy, qq * 3, 1)));
  const qTo = (yy, qq) => isoDay(new Date(Date.UTC(yy, qq * 3 + 3, 0)));
  return [
    { key: 'month', label: 'This month', from: m + '-01', to: monthEnd(m) },
    { key: 'last', label: 'Last month', from: prevM + '-01', to: monthEnd(prevM) },
    { key: 'quarter', label: 'This quarter', from: qFrom(y, q), to: qTo(y, q) },
    { key: 'lastq', label: 'Last quarter', from: qFrom(ly, lq), to: qTo(ly, lq) },
    { key: 'ytd', label: 'Year to date', from: DATA.meta.from, to: monthEnd(m) },
  ];
}

function budgetDaily(b) {
  if (b.daily) return b.daily;
  // Data built before daily totals existed: spread each month over its days.
  const out = {};
  for (const [m, v] of Object.entries(b.actuals || {})) {
    const n = daysInMonth(m);
    for (let i = 1; i <= n; i++) out[m + '-' + String(i).padStart(2, '0')] = v / n;
  }
  return out;
}

// Budget, used and in-range amounts for one project over from..to.
function budgetInRange(b, from, to) {
  let inRange = 0, through = 0;
  for (const [d, v] of Object.entries(budgetDaily(b))) {
    if (d > to) continue;
    through += v;
    if (d >= from) inRange += v;
  }
  if (!b.monthly) return { budget: b.budget, used: (b.before || 0) + through, inRange };
  // No budget for months before the project's first time entry: it hadn't
  // started yet.
  const firstM = Object.keys(b.actuals || {}).sort()[0] || to.slice(0, 7);
  let budget = 0;
  for (const m of monthsBetween(from.slice(0, 7), to.slice(0, 7))) {
    if (m < firstM) continue;
    const s = from > m + '-01' ? from : m + '-01';
    const e = to < monthEnd(m) ? to : monthEnd(m);
    budget += b.budget * ((utcDate(e) - utcDate(s)) / 864e5 + 1) / daysInMonth(m);
  }
  return { budget, used: inRange, inRange };
}

function renderBudget() {
  const { from, to } = BSTATE;
  Object.keys(CHARTS).filter(k => k.startsWith('bd')).forEach(destroy);
  if (!from || !to || from > to) {
    $('#budgetSummary').innerHTML = '';
    $('#budgetTableHost').innerHTML = `<div class="empty">Pick a From date on or before the To date.</div>`;
    return;
  }
  const rows = (DATA.budgets || []).map(b => {
    const r = budgetInRange(b, from, to);
    return { b, ...r, remaining: r.budget - r.used, pct: r.budget ? (r.used / r.budget) * 100 : 0 };
  });
  rows.sort((a, b) => b.pct - a.pct);

  const over = rows.filter(r => r.pct > 100).length;
  const near = rows.filter(r => budgetLevel(r.pct) === 'gold').length;
  $('#budgetSummary').innerHTML = `
    <div class="stat red"><div class="label">Over budget</div><div class="num">${over}</div></div>
    <div class="stat gold"><div class="label">${BUDGET_WARN_PCT}%+ used</div><div class="num">${near}</div></div>
    <div class="stat"><div class="label">Under ${BUDGET_WARN_PCT}%</div><div class="num">${rows.length - over - near}</div></div>`;

  const body = rows.map(r => {
    const b = r.b, lv = budgetLevel(r.pct), open = BSTATE.open.has(b.project_id);
    const row = `<tr class="brow${open ? ' open' : ''}" data-id="${b.project_id}">
      <td>${escapeHtml(b.client)}</td>
      <td class="proj">${escapeHtml(b.project)}${b.monthly ? ' <span style="color:var(--muted);font-size:12px">(monthly)</span>' : ''}</td>
      <td class="num">${budgetAmt(r.budget, b.unit)}${b.monthly ? `<span class="sub">${budgetAmt(b.budget, b.unit)}/mo</span>` : ''}</td>
      <td class="num">${budgetAmt(r.used, b.unit)}${b.monthly ? '' : `<span class="sub">${budgetAmt(r.inRange, b.unit)} in range</span>`}</td>
      <td class="num${r.remaining < 0 ? ' over' : ''}">${budgetAmt(r.remaining, b.unit)}</td>
      <td><div class="bbar ${lv}"><span style="width:${Math.min(r.pct, 100)}%"></span></div></td>
      <td class="num${lv === 'red' ? ' over' : ''}">${r.pct.toFixed(0)}%</td>
    </tr>`;
    return open ? row + `<tr class="bdetail"><td colspan="7">${budgetDetailHtml(r)}</td></tr>` : row;
  }).join('');
  $('#budgetTableHost').innerHTML = rows.length
    ? `<table><tr><th>Client</th><th>Project</th><th class="num">Budget</th><th class="num">Used</th><th class="num">Remaining</th><th></th><th class="num">% used</th></tr>${body}</table>`
    : `<div class="empty">No project budgets found. Budgets are set per project in Harvest (Projects → Edit → Budget), and the token needs Administrator access to read them.</div>`;

  document.querySelectorAll('#budgetTableHost tr.brow').forEach(tr => tr.addEventListener('click', () => {
    const id = +tr.dataset.id;
    BSTATE.open.has(id) ? BSTATE.open.delete(id) : BSTATE.open.add(id);
    renderBudget();
  }));
  for (const r of rows) if (BSTATE.open.has(r.b.project_id)) drawBudgetDetail(r);
}

function budgetDetailHtml(r) {
  const b = r.b, u = n => budgetAmt(n, b.unit);
  const fig = (label, val) => `<div>${label}<br><strong>${val}</strong></div>`;
  let figs;
  if (b.monthly) {
    const openM = DATA.meta.to.slice(0, 7);
    const closed = monthsBetween(BSTATE.from.slice(0, 7), BSTATE.to.slice(0, 7))
      .filter(m => m < openM && m >= DATA.meta.from.slice(0, 7) && m in (b.actuals || {}));
    const overN = closed.filter(m => b.actuals[m] > b.budget).length;
    figs = fig('Budget / month', u(b.budget)) + fig('Budget, time frame', u(r.budget))
      + fig('Used, time frame', u(r.used)) + fig(r.remaining < 0 ? 'Over by' : 'Remaining', u(Math.abs(r.remaining)))
      + fig('Closed months over', closed.length ? `${overN} of ${closed.length}` : '—');
  } else {
    figs = fig('Total budget', u(b.budget)) + fig('Used through ' + BSTATE.to, u(r.used))
      + fig('Used in time frame', u(r.inRange)) + fig(r.remaining < 0 ? 'Over by' : 'Remaining', u(Math.abs(r.remaining)))
      + (b.before ? fig(`Before ${DATA.meta.from}`, u(b.before)) : '');
  }
  return `<div class="figs">${figs}</div><div class="chart-wrap"><canvas id="bdChart${b.project_id}"></canvas></div>`;
}

// Month-by-month chart for one project. Months inside the time frame are
// full color; the rest of the year is faded for context.
function drawBudgetDetail(r) {
  const b = r.b, actuals = b.actuals || {};
  const openM = DATA.meta.to.slice(0, 7);
  const months = monthsBetween(DATA.meta.from.slice(0, 7), openM);
  const inFrame = m => m >= BSTATE.from.slice(0, 7) && m <= BSTATE.to.slice(0, 7);
  const label = m => utcDate(m + '-01').toLocaleString('en-US', { month: 'short', timeZone: 'UTC' }) + (m === openM ? '*' : '');
  const FADE = '#ddd8cc', unitLabel = b.unit === '$' ? 'Billable $' : 'Hours';
  const val = m => +(actuals[m] || 0).toFixed(2);
  let datasets;
  if (b.monthly) {
    const color = m => !inFrame(m) ? FADE : val(m) > b.budget ? '#B03030' : m === openM ? '#C8963C' : '#1f7a44';
    datasets = [
      { type: 'bar', label: 'Actual', data: months.map(val), backgroundColor: months.map(color), order: 2 },
      { type: 'line', label: 'Monthly budget', data: months.map(() => b.budget), borderColor: '#0A2240', borderDash: [6, 4], pointRadius: 0, borderWidth: 2, order: 1 },
    ];
  } else {
    let run = b.before || 0;
    const cum = months.map(m => +(run += val(m)).toFixed(2));
    datasets = [
      { type: 'bar', label: 'Used that month', data: months.map(val), backgroundColor: months.map(m => inFrame(m) ? '#0A2240' : FADE), order: 3 },
      { type: 'line', label: 'Used to date', data: cum, borderColor: '#C8963C', backgroundColor: '#C8963C', pointRadius: 3, borderWidth: 2, order: 2 },
      { type: 'line', label: 'Total budget', data: months.map(() => b.budget), borderColor: '#B03030', borderDash: [6, 4], pointRadius: 0, borderWidth: 2, order: 1 },
    ];
  }
  CHARTS['bd' + b.project_id] = new Chart(document.getElementById('bdChart' + b.project_id), {
    data: { labels: months.map(label), datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      scales: { y: { beginAtZero: true, title: { display: true, text: unitLabel } } },
      plugins: {
        legend: { position: 'bottom' },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${budgetAmt(c.parsed.y, b.unit)}` } },
        title: { display: true, text: '* current month, still in progress · faded months are outside the time frame', font: { size: 11, weight: 'normal' }, color: '#5b6470' },
      },
    },
  });
}

function wireBudgetRange() {
  const presets = budgetPresets();
  const f = $('#budgetFrom'), t = $('#budgetTo'), host = $('#budgetPresets');
  f.min = t.min = DATA.meta.from;
  f.max = t.max = DATA.meta.year + '-12-31';
  const apply = p => {
    BSTATE.preset = p ? p.key : '';
    if (p) { BSTATE.from = f.value = p.from; BSTATE.to = t.value = p.to; }
    host.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c.dataset.key === BSTATE.preset));
    renderBudget();
  };
  for (const p of presets) {
    const el = document.createElement('span');
    el.className = 'chip'; el.dataset.key = p.key; el.textContent = p.label;
    el.addEventListener('click', () => apply(p));
    host.appendChild(el);
  }
  f.addEventListener('change', () => { BSTATE.from = f.value; apply(null); });
  t.addEventListener('change', () => { BSTATE.to = t.value; apply(null); });
  apply(presets[0]);
}

// Budget vs. actual by month (By project tab). Monthly budgets compare each
// month to the budget; total budgets show the running total against it.
function renderBudgetByMonth() {
  const host = $('#bvaHost');
  const firstM = STATE.from.slice(0, 7), lastM = STATE.to.slice(0, 7);
  const openM = DATA.meta.to.slice(0, 7);
  const months = [];
  for (let d = new Date(firstM + '-01T00:00:00Z'); d.toISOString().slice(0, 7) <= lastM; d.setUTCMonth(d.getUTCMonth() + 1)) {
    months.push(d.toISOString().slice(0, 7));
  }
  const mLabel = m => new Date(m + '-01T00:00:00Z').toLocaleString('en-US', { month: 'short', timeZone: 'UTC' })
    + (m === openM ? '*' : '');
  const amt = (n, unit) => unit === '$' ? '$' + Math.round(n).toLocaleString() : fmt(n) + 'h';
  const signed = (n, unit) => (n > 0 ? '+' : n < 0 ? '−' : '') + amt(Math.abs(n), unit);

  const budgets = (DATA.budgets || []).filter(b => b.actuals && (!STATE.project || b.project === STATE.project));
  if (!budgets.length || !months.length) {
    host.innerHTML = `<div class="empty">No budgeted projects in this filter.</div>`;
    return;
  }
  const byName = (a, b) => (a.client + a.project).localeCompare(b.client + b.project);
  const monthly = budgets.filter(b => b.monthly).sort(byName);
  const total = budgets.filter(b => !b.monthly).sort(byName);
  const head = first => `<tr><th>Client</th><th>Project</th><th class="num">${first}</th>${months.map(m => `<th class="num">${mLabel(m)}</th>`).join('')}`;

  let html = '';
  if (monthly.length) {
    const overCount = months.map(() => 0), underCount = months.map(() => 0);
    const rows = monthly.map(b => {
      let net = 0;
      const cells = months.map((m, i) => {
        // No time at all usually means the project hadn't started yet.
        if (!(m in b.actuals)) return `<td class="cell open">—</td>`;
        const a = b.actuals[m], v = a - b.budget;
        const isOpen = m === openM;
        if (!isOpen) net += v;
        const cls = v > 0 ? 'over' : isOpen ? 'open' : 'under';
        if (v > 0) overCount[i]++; else if (!isOpen) underCount[i]++;
        const varText = isOpen && v <= 0 ? amt(-v, b.unit) + ' left' : (v > 0 ? 'over ' : 'under ') + amt(Math.abs(v), b.unit);
        return `<td class="cell ${cls}">${amt(a, b.unit)}<span class="var">${varText}</span></td>`;
      }).join('');
      const closed = months.filter(m => m !== openM && m in b.actuals).length;
      const netCell = closed
        ? `<td class="cell ${net > 0 ? 'over' : 'under'}">${signed(net, b.unit)}<span class="var">${net > 0 ? 'over' : 'under'}</span></td>`
        : `<td class="cell open">—</td>`;
      return `<tr><td>${escapeHtml(b.client)}</td><td>${escapeHtml(b.project)}</td><td class="num">${amt(b.budget, b.unit)}</td>${cells}${netCell}</tr>`;
    }).join('');
    const tally = `<tr class="tally"><td colspan="3">Projects over / under</td>${months.map((m, i) =>
      `<td class="num">${overCount[i]} / ${m === openM ? '…' : underCount[i]}</td>`).join('')}<td></td></tr>`;
    html += `<div class="bva"><h3>Monthly budgets</h3><table>${head('Budget / mo')}<th class="num">Net, closed months</th></tr>${rows}${tally}</table></div>`;
  }
  if (total.length) {
    const rows = total.map(b => {
      // Running total starts with anything spent before this year and
      // before the first month shown.
      let run = (b.before || 0) + Object.entries(b.actuals).filter(([m]) => m < firstM).reduce((s, [, v]) => s + v, 0);
      const cells = months.map(m => {
        run += b.actuals[m] || 0;
        const v = run - b.budget;
        return `<td class="cell ${v > 0 ? 'over' : 'under'}">${amt(run, b.unit)}<span class="var">${v > 0 ? 'over ' + amt(v, b.unit) : amt(-v, b.unit) + ' left'}</span></td>`;
      }).join('');
      return `<tr><td>${escapeHtml(b.client)}</td><td>${escapeHtml(b.project)}</td><td class="num">${amt(b.budget, b.unit)}</td>${cells}</tr>`;
    }).join('');
    html += `<div class="bva"><h3>Total budgets · running total used</h3><table>${head('Budget')}</tr>${rows}</table></div>`;
  }
  host.innerHTML = html + (months.includes(openM) ? `<div class="budget-note">* Current month, still in progress.</div>` : '');
}

// Project cards: the person-card idea turned around — who worked on each
// project, with its Harvest budget bar when one is set.
function renderProjectCards(entries) {
  const budgetOf = new Map((DATA.budgets || []).map(b => [b.client + '|' + b.project, b]));
  const latestMonth = STATE.to.slice(0, 7);
  const latestMonthLabel = new Date(latestMonth + '-01T00:00:00Z')
    .toLocaleString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });

  const projs = new Map();
  for (const e of entries) {
    const key = (e.client || '') + '|' + e.project;
    if (!projs.has(key)) projs.set(key, { project: e.project, client: e.client || '', hours: 0, people: new Map() });
    const p = projs.get(key);
    p.hours += e.hours;
    if (!p.people.has(e.person)) p.people.set(e.person, { hours: 0, last: '', lm: 0 });
    const pp = p.people.get(e.person);
    pp.hours += e.hours;
    if (e.date > pp.last) pp.last = e.date;
    if (e.date.slice(0, 7) === latestMonth) pp.lm += e.hours;
  }
  const cards = [...projs.entries()].map(([key, p]) => {
    const b = budgetOf.get(key);
    return { ...p, budget: b, pct: b && b.budget ? (b.spent / b.budget) * 100 : -1 };
  });
  // Budgeted first (highest % used on top), then the rest by hours.
  cards.sort((a, b) => (b.pct - a.pct) || (b.hours - a.hours));

  const host = $('#projCardHost');
  if (!cards.length) { host.innerHTML = `<div class="empty">No data in this filter.</div>`; return; }

  const amt = (n, unit) => unit === '$' ? '$' + Math.round(n).toLocaleString() : fmt(n) + 'h';
  host.innerHTML = cards.map(c => {
    const people = [...c.people.entries()].sort((a, b) => b[1].hours - a[1].hours);
    const rows = people.map(([name, v]) => `<tr${v.lm === 0 ? ' class="dim"' : ''}>
        <td>${escapeHtml(name)}</td>
        <td class="num">${fmt(v.hours)}</td>
        <td class="num">${((v.hours / (c.hours || 1)) * 100).toFixed(1)}%</td>
        <td>${v.last}</td>
        <td class="num">${v.lm > 0 ? fmt(v.lm) : '—'}</td>
      </tr>`).join('');
    let budgetHtml = `<div class="meta">No budget set in Harvest.</div>`;
    if (c.budget) {
      const b = c.budget;
      const lv = c.pct > 100 ? 'red' : c.pct >= BUDGET_WARN_PCT ? 'gold' : '';
      budgetHtml = `
        <div class="meta">Budget${b.monthly ? ' (this month)' : ''}: <strong>${amt(b.spent, b.unit)}</strong> of ${amt(b.budget, b.unit)} used ·
          <span class="${b.remaining < 0 ? 'over' : ''}" style="${b.remaining < 0 ? 'color:var(--red);font-weight:700' : ''}">${amt(b.remaining, b.unit)} remaining</span> ·
          <strong style="${lv === 'red' ? 'color:var(--red)' : ''}">${c.pct.toFixed(0)}%</strong></div>
        <div class="bbar ${lv}" style="margin-top:6px;max-width:420px"><span style="width:${Math.min(c.pct, 100)}%"></span></div>`;
    }
    return `<div class="person-card"${c.pct > 100 ? ' style="border-top-color:var(--red)"' : ''}>
      <div class="head">
        <div>
          <h3>${escapeHtml(c.project)}</h3>
          <div class="meta">${escapeHtml(c.client)}${c.client ? ' · ' : ''}${people.length} ${people.length === 1 ? 'person' : 'people'} in this filter</div>
          ${budgetHtml}
        </div>
        <div class="total">${fmt(c.hours)} h</div>
      </div>
      <div class="ptable" style="margin-top:14px">
        <table>
          <tr><th>Person</th><th class="num">Hours</th><th class="num">% of project</th><th>Last entry</th><th class="num">${escapeHtml(latestMonthLabel)} hrs</th></tr>
          ${rows}
        </table>
      </div>
    </div>`;
  }).join('');
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
  renderBudgetByMonth();
  renderPerson(entries);
  renderProjectCards(entries);
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

wireBudgetRange();
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
    entries, raw_entries = fetch_entries(keep_ids, from_date, to_date)
    budgets = fetch_budgets()
    add_monthly_actuals(budgets, raw_entries, to_date)
    payload = build_payload(entries, from_date, to_date, budgets)

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

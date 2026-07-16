# RenUSA Team Hours Dashboard

Pulls hours from Harvest once a week for the **entire RenUSA team** (every
active user with an `@renusa.org` email), publishes a branded dashboard to
GitHub Pages, and flags anyone who is under or over the hour thresholds. Runs
itself — you just read it on Fridays.

Live at **https://dave-renusa.github.io/renusatime/** once the workflow has run.

The pull runs server-side inside GitHub Actions, so there is nothing to install
on your machine and no CORS proxy needed. The Harvest token lives in GitHub as
an encrypted secret and is never written into any file.

This is the whole-team sibling of the 4-person `dave-renusa/harvest` dashboard.
Same engine, same design; the only difference is who it covers.

## One-time setup (about 3 minutes)

The code is already here and Pages is already on. You only need to add the two
Harvest secrets.

1. **Get your two Harvest values** (same ones the `harvest` repo uses).
   - **Account ID:** visible at https://id.getharvest.com after you sign in.
   - **Personal Access Token:** create one at
     https://id.getharvest.com/developers. **Generate it while signed in as a
     Harvest _Administrator_** — the token only returns time for people it can
     see, so a member or scoped-manager token will show a partial team. If you
     already have an Administrator token in the `harvest` repo, reuse that same
     value here.

2. **Add them as repo secrets** (this is where you enter the token, not me).
   Settings → Secrets and variables → Actions → New repository secret. Add two
   secrets with these exact names:
   - `HARVEST_ACCOUNT_ID`
   - `HARVEST_TOKEN`

3. **Run it once now.** Actions tab → "RenUSA team hours dashboard" → Run
   workflow. This overwrites the placeholder `docs/index.html` with real numbers
   and confirms the token works. After it finishes, refresh the live URL above.

## Adjusting who's included

Open `harvest_dashboard.py` and edit the config block near the top:
- `TEAM_DOMAIN` — the email domain that defines the team (`renusa.org`). Set it
  to `""` to include every active Harvest user regardless of domain.
- `TEAM_EMAILS` — extra individuals to include who aren't on that domain.
- `EXCLUDE_EMAILS` — specific people to leave out (e.g. a service account).
- `LOW_HOURS_WEEK` / `HIGH_HOURS_WEEK` flags live in the HTML's
  `FLAG_THRESHOLDS` (30h light, 55h heavy).

Change the schedule by editing the `cron` line in
`.github/workflows/harvest.yml`.

## Notes and limits

- The dashboard is **public** — anyone with the link can see named team hours.
  That was a deliberate choice; if it should be private later, turn off Pages in
  Settings and view via `git pull && open docs/index.html` instead.
- Output (what people actually shipped) is not in Harvest. This is the hours
  half only.
- The Harvest API rate limit is generous for a weekly pull; the script
  paginates and will not hit it.

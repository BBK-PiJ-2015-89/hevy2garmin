# Cutover runbook: Python dashboard to the Next.js web dashboard

Tracking issue: [#456](https://github.com/drkostas/hevy2garmin/issues/456).

Both dashboards read and write the same database and the same `platform_credentials`
rows, so they can run side by side against one Neon project. The flip is a Vercel
project setting, not a code change, which is what makes it reversible.

## The gate

**Blocked right now, on two issues.**

[#475](https://github.com/drkostas/hevy2garmin/issues/475) affects new forks. The
web path never creates its schema, while the Python path has nine
`CREATE TABLE IF NOT EXISTS` statements. A fork that follows the recommended
Root Directory `web` against a fresh Neon database gets pages that all render
200 and writes that all fail with `relation "app_cache" does not exist`. The
dashboard looks healthy and cannot save anything, and setup cannot be completed
because the Hevy key writes to `platform_credentials`. Verified against an empty
local database.

[#473](https://github.com/drkostas/hevy2garmin/issues/473) affects existing ones: with auth
configured, which `web/.env.example` marks as required, the middleware answers
`/api/cron/*` with a 401 before the route can check `CRON_SECRET`, so scheduled
syncing does not run at all on the web path. Python exempts those paths, so the
same deployment syncs on Python and silently stops on the web. Verified against a
production build, and the one-line fix is verified in that issue too.


Do not flip until all of these are green on `main`:

| Check | What it proves |
| --- | --- |
| Fresh fork (Vercel-style base install) | the Python path still installs and boots from its base dependencies |
| Fresh fork (web, Vercel-style install) | the web path installs from its lockfile and builds with a bare environment |
| Tests (Web) | web unit suite |
| Tests (TypeScript) | the `hevy2garmin` TS package |
| Tests (Postgres), Tests (SQLite 3.10 and 3.12) | the Python path across both stores |
| Playwright parity smoke | all 8 pages render their no-database state and are reachable from the nav, on desktop and mobile |

The parity smoke runs a production build with only a password in the environment
and no `DATABASE_URL`, because that is what a fresh fork sees before it wires Neon.
It runs on its own port so it can never adopt a developer's dev server, which would
otherwise pull in a real `.env.local`. It runs both the desktop and mobile projects,
because the two navs are separate markup and a single-project run cannot see a
regression in the other.

A gap the gate does not close, which is how #475 survived a fully green suite.
The smoke runs with no `DATABASE_URL` at all, and the fresh-fork job proves the
app installs, builds and answers with a bare environment but never provisions a
database. Neither covered a database that is present and empty, which is exactly
where a forker lands after adding Neon as the README tells them to.
`Web against an empty database` now covers that, asserting a write round-trips
and the row actually lands, since reads return 200 either way.

**The green `Vercel` check on a PR is not about the web app
([#478](https://github.com/drkostas/hevy2garmin/issues/478)).** `hevy2garmin-demo`
is the project linked to this repository, so it produces every preview, and it
builds the Python dashboard. `hevy2garmin-web` has no GitHub link at all and
therefore cannot produce one; its last deployment was manual. So nothing verifies
that the web app deploys on Vercel, which is the thing the cutover moves everyone
to. That matters because `next.config.ts` switches output on `process.env.VERCEL`,
making the build that ships to Vercel different from the one every local and CI
check exercises, and because #466 already hit a failure that reproduced only on
Vercel. Before flipping, at minimum redeploy `hevy2garmin-web` from current
`main` and exercise it.

What the smoke does not prove: it exercises routing, auth, rendering and nav
reachability, not the production server shape. `next.config.ts` sets
`output: "standalone"` only off Vercel, and the suite serves that build with
`next start`, so the server under test matches neither Vercel's runtime nor
`.next/standalone/server.js`. That is a deliberate trade, since forcing the
standalone output on Vercel corrupts the Edge middleware bundle. Treat a green
smoke as evidence the app is coherent, not as a production rehearsal.

## The flip

Per-deployment, in the Vercel project. Nothing is pushed to the repo.

1. Settings, then General, then Root Directory. Set it to `web`.
2. Redeploy.

A fork that leaves Root Directory empty keeps deploying the Python dashboard from
`api/index.py`. That is why the setting is used instead of rewriting the root
`vercel.json`: a config change would reach every fork on its next "Sync fork" and
break deployments whose owners had not opted in.

### Garmin tokens heal themselves

The one failure that would hurt every fork at once is the flat-versus-nested token
shape (#459). garmin-auth below 0.3 wrote the DI payload flat; 0.3 and later nest
it under `garmin_tokens`, which is the only shape either store reads. A fork whose
row predates that change would be told to reconnect Garmin, and would have to redo
MFA, for no real reason.

Both paths self-heal, so this needs no action at the flip. Python runs the fix in
its schema init in `db_postgres.py`. The web runs the same statement in
`normalizeGarminTokenRow`, called from `getGarminClient` before `DBTokenStore` is
built. Both are idempotent, both are guarded on `credentials ? 'di_token' AND NOT
(credentials ? 'garmin_tokens')`, and the web's never throws. A fork that flips to
the web path and never runs Python again still heals on its first Garmin call.

## Rollback

Clear the Root Directory field and redeploy. The next deployment serves the Python
dashboard again.

No data migration is involved in either direction, and nothing you synced while the
web path was live is lost by going back. Python declares nine tables and the web
reads and writes eight of them under identical names: `synced_workouts`,
`pending_uploads`, `platform_credentials`, `custom_mappings`, `app_cache`,
`hr_cache`, `routine_schedules` and `synced_routines`. (`user_profile` is not a
table; it is a key inside `app_cache`.)

One exception, worth knowing before you roll back. `sync_log` is written only by
the Python path, from `syncstate.record_sync_log`, and holds the per-run counts
that feed the Python dashboard's history panel via `get_sync_log`. The web path
never writes it, and its own `/history` page reads `synced_workouts` instead, a
per-workout view. So while the web path is live the run-level log stops
accumulating, and after a rollback the Python history panel shows a gap for that
window. The per-workout record of what actually synced is unaffected, because that
lives in the shared `synced_workouts`.

Keep the Python entry point for at least one release after the flip so this remains
a one-setting revert.

## What changes for the operator

Verified differences at the time of writing. None of these block the flip, but a
deployer migrating from the Python path should know about them.

**intervals.icu cleanup stops.** When the Python sync deletes a watch duplicate it
also removes the matching activity from intervals.icu (`sync.py`, via
`try_delete_icu_activity`). The web path has no intervals.icu support at all, so
`INTERVALS_API_KEY` and `INTERVALS_ATHLETE_ID` become inert and deleted watch
duplicates stay on intervals.icu. This is the one silent behaviour change worth
announcing.

**Session lifetime is no longer configurable.** Python reads
`H2G_SESSION_TTL_DAYS`, defaulting to 30 days. The web path fixes the same 30 days
in `web/lib/auth.ts`. Deployers on the default see no change; anyone who set a
custom value silently returns to 30 days.

**`H2G_TRUST_FORWARDED_PREFIX` has no web equivalent.** Relevant only when serving
behind a reverse proxy at a subpath.

**Garmin login moves to the Worker.** `GARMIN_EMAIL`, `GARMIN_PASSWORD` and
`H2G_DIRECT_GARMIN_LOGIN` are Python-only. The web path authenticates through the
Cloudflare Worker from `/setup`, overridable with `GARMIN_LOGIN_WORKER_URL`. Garmin
blocks SSO from cloud IPs, which is why the web path uses the Worker rather than
logging in directly.

**`DEMO_MODE` does not hold on the web path yet ([#471](https://github.com/drkostas/hevy2garmin/issues/471)).**
Do not flip a deployment that relies on it until that is fixed. `web/.env.example`
says `true` means every mutating `/api` call is refused, but of the 30 route files
under `web/app/api` exporting a `POST`, `PUT`, `PATCH` or `DELETE`, one calls
`demoMode()`, and `proxy.ts` adds no middleware check. Python enforces it at 11
sites. This is dormant rather than live, because the public demo still serves the
Python dashboard, but a demo that flips to `web` stops being read-only while its
`.env.example` still claims otherwise.

## Auth environment

The web path accepts the Python names, so an existing deployment does not have to
change variables to move. Verified against `web/lib/auth.ts`.

Session signing key, first match wins:

1. `HEVY2GARMIN_SECRET`, used as raw bytes, preserving existing deployments.
2. Otherwise `H2G_SECRET`, else `H2G_PASSWORD`, else `H2G_PASSWORD_HASH`, each run
   through `SHA-256("h2g-session-" + seed)` to match the Python derivation.

Login password:

1. `H2G_PASSWORD_HASH` when set, an argon2id string from `hevy2garmin hash-password`.
2. Otherwise the plaintext `H2G_PASSWORD`.

Web-only variables, which have no Python counterpart: `DATABASE_URL`,
`CRON_SECRET`, `GITHUB_REPO`, `GARMIN_LOGIN_WORKER_URL`. See `web/.env.example`.

## Announcing

Policy on #456 is announce before flip. Say so in the README and the changelog
before changing any project setting, and call out the intervals.icu change above,
since that is the one an affected user would otherwise discover on their own.

# Dogfood Portal

A self-hosted, open-source hackathon **submission and judging portal**, built for DOGFOOD 2026 by Hackathon
Raptors. Participants form teams and submit projects; judges score only what they are assigned in their own
tracks; organizers watch progress, normalize scores across harsh and generous judges, export everything as
CSV, and publish results. One command, no cloud accounts, works with the network off.

**Claimed tiers: T1 + T2** — [acceptance-report.txt](acceptance-report.txt) shows 7/7 checks passing.
T3 and T4 are not claimed.

## Run it

```bash
docker compose up --build
```

Open http://localhost:8080. The first boot creates the tables, loads `fixtures.json` and prints the demo
session headers. Restarting is safe: seeding never duplicates or overwrites data.
(Building the image needs the internet once, for the Python packages; after that it runs fully offline.
All CSS is vendored, no CDN.)

To start from an empty database: `docker compose down -v`.

## Demo logins

Every seeded account uses the password **`dogfood-demo`** (demo only — see [THREAT-MODEL.md](THREAT-MODEL.md) §6).

| Role | Email | What to look at |
|---|---|---|
| Organizer | `organizer@dogfood.local` | Organizer → event → progress, rubric, assignments, results, audit log |
| Admin | `admin@dogfood.local` | Organizer pages + Users & roles |
| Judge A | `jdg_09`'s email from `fixtures.json` | Judging queue (track: Developer tools) |
| Participant | any team member email from `fixtures.json` | Events → Raptors Live Demo 2026 → create a team, submit |

There are two events:
- **Sample Hack 2026** (`evt_01`): the fixture event. Its deadline (2026-03-01) is in the past, so it is **closed**,
  and it already has 126 scores.
- **Raptors Live Demo 2026** (`evt_demo`): **open** for 30 days after first boot, for trying the full lifecycle.

## Run the acceptance checker

```bash
python3 run.py .dogfood.toml > acceptance-report.txt
```

The four `Cookie:` headers in [.dogfood.toml](.dogfood.toml) are fixed demo sessions that the portal creates on boot
while `DEMO_SESSIONS=1` (the default). They are public, so set `DEMO_SESSIONS=0` for any real deployment.

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest
```

199 tests, most of them over HTTP the way curl and run.py see the app, including every "no" in the role
table (participant/judge/visitor against organizer routes, judge B against judge A's scores, etc.).

## What it does

**T1 — core**
- Accounts with bcrypt passwords and server-side sessions (hashed at rest, HttpOnly, SameSite=Lax), CSRF
  protection on every form, roles: visitor, participant, judge, organizer, admin — checked in the backend.
- Organizers create and edit events: open/close dates (UTC), tracks, prizes, team size, custom questions.
- Teams by invite link (POST to join, size limit that holds under concurrent joins, rotatable link).
- Projects: draft → edit → submit, until the deadline. Title, tagline, description, thumbnail, demo video,
  repo, live link, tags, track, custom answers.
- Deadline enforced on the server with the database clock, for create **and** edit (409).
- Public gallery with search and filters (event, track, tag). The fixture's duplicate submission is detected,
  flagged and kept out of the gallery and results.

**T2 — judging**
- Judge invitations (one-time, hashed, expiring links) and per-judge tracks; admin-only role changes.
- Balanced, in-track auto-assignment (target reviews per project), plus manual add/remove.
- Organizer-weighted rubric (weights sum to 100%).
- Judges see and score only their own assigned, in-track projects; other judges' scores are 403.
- Progress dashboard: judges not started, incomplete batches, projects under target.
- Per-judge z-score normalization with confidence weights; flat judges abstain; raw vs normalized side by side
  with rank movement. See **[JUDGING.md](JUDGING.md)**.
- CSV export at every stage: projects, scores, progress, results, audit log (formula-injection safe).
- Results hidden until the organizer publishes; scores frozen after publishing.
- Audit log of scores, submissions, deadline changes, role changes, exports, publishing — readable in the UI.

Free extras: every page action also has a JSON API; OpenAPI docs at `/docs`.

## Documents

| | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request flow, where the auth and role checks live and why |
| [DATA-MODEL.md](DATA-MODEL.md) | Every table, how fixtures are imported, how data gets in and out |
| [JUDGING.md](JUDGING.md) | Assignment, rubric maths, normalization and every awkward fixture case |
| [THREAT-MODEL.md](THREAT-MODEL.md) | Assets, actors, trust boundaries; each attack stopped / partly / not stopped, with the test that proves it |
| [normalization-report.md](normalization-report.md) | Raw vs normalized ranking on the real fixtures (`python scripts/normalization_report.py`) |

## What does not work yet (honest list)

- **No community voting, comments or public ballot (T3).** Not built, not claimed.
- **No email.** The portal runs offline, so judge invite links are shown to the organizer to pass on, and
  there is no password reset or email verification. Registration is open to anyone who can reach the portal.
- **No TLS in compose.** Put it behind a reverse proxy and set `COOKIE_SECURE=1`.
- **Login throttling is in-memory**, per process; it resets on restart and isn't shared between replicas.
- **No global rate limiting**; the public gallery can be scraped.
- **Normalization weakness:** judges with a very small (but non-zero) spread turn small differences into large
  z-scores. Documented with real examples in [JUDGING.md](JUDGING.md) §6–7; not yet corrected.
- **Rubric shape is locked once judging starts** (weights and labels can still change). By design, but it means
  a criterion forgotten at the start can't be added later.
- **No per-team deadline extensions**, and no way to leave a team.
- The demo sessions and the shared demo password are public — for the hackathon demo only.

## Demo video

_Link to be added._

## License

[MIT](LICENSE)

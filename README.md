# ⚖️ Calibrate

**Hackathon judging that corrects for harsh and generous judges.**

![License: MIT](https://img.shields.io/badge/license-MIT-green)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-336791)
![Tests: 223 passing](https://img.shields.io/badge/tests-223%20passing-brightgreen)
![run.py: 7/7](https://img.shields.io/badge/run.py-7%2F7%20PASS-brightgreen)

A self-hosted, open-source **hackathon submission and judging portal**, built for DOGFOOD 2026 by
Hackathon Raptors.

- 👥 **Participants** form teams by invite link and submit projects until the deadline.
- 🧑‍⚖️ **Judges** score only the projects assigned to them in their own tracks, and can never see another judge's scores.
- 🗂️ **Organizers** assign judges, set the rubric, track progress, export CSVs and publish fair, normalized results.
- 🌍 **Everyone** can browse the gallery, vote and comment.

One command, no cloud accounts, works with the network off.

**Quick links:** [Run it](#-run-it) · [Demo logins](#-demo-logins) · [How judging works](JUDGING.md) ·
[Threat model](THREAT-MODEL.md) · [Architecture](ARCHITECTURE.md) · [Data model](DATA-MODEL.md)

🏆 **Claimed tiers: T1 + T2 + T3.** [acceptance-report.txt](acceptance-report.txt) shows all 7 run.py checks
passing. run.py has no T3 checks, so it prints `claimed but not verified: T3`; T3 is backed by
[tests/test_community.py](tests/test_community.py) (24 tests) instead. T4 is not claimed.

## 💡 Why Calibrate

Judges are not calibrated instruments. In the shared fixture data, judge averages range from **2.95** to
**4.23** on a 1–5 scale, so a project's raw score depends a lot on *who* happened to review it.

Calibrate compares every score with **that judge's own usual scoring** (a per-judge z-score), then puts the
result back on the 1–5 scale. Judges who give everyone the same score carry no ranking information, so they
**abstain** instead of dragging every project toward the middle.

On the fixture data, **39 of 40 projects change rank**. For example, *Small Relay* is 14th by raw average
but 33rd after normalization: its raw score was propped up by a judge who gave 4/4/4 to everything. Raw and
normalized scores are always shown side by side, with the reasons flagged.
Full method and every edge case: [JUDGING.md](JUDGING.md).

## 🚀 Run it

```bash
docker compose up --build
```

Then open **http://localhost:8080**.

- The first boot creates the tables, loads `fixtures.json` and prints the demo session headers.
- Restarting is safe: seeding never duplicates or overwrites data.
- Building the image needs the internet once (Python packages). After that it runs fully offline; all CSS is
  bundled, no CDN.
- To start again from an empty database: `docker compose down -v`.

## 🔑 Demo logins

Every seeded account uses the password **`dogfood-demo`** (demo only — see [THREAT-MODEL.md](THREAT-MODEL.md) §6).

| Role | Email | What to look at |
|---|---|---|
| Organizer | `organizer@dogfood.local` | Organizer → event → progress, rubric, assignments, results, audit log |
| Admin | `admin@dogfood.local` | Organizer pages + Users & roles |
| Judge A | `sofia.duarte@example.org` (jdg_09) | Judging queue (Developer tools + the demo event's AI tools track) |
| Judge B | `jonas.vogel@example.org` (jdg_26) | A peer of judge A; cannot read judge A's scores |
| Participant | any team member email from `fixtures.json` | Events → Raptors Live Demo 2026 → create a team, submit |

There are two events:
- **Sample Hack 2026** (`evt_01`): the fixture event. Its deadline (2026-03-01) is in the past, so it is **closed**,
  and it already has 126 scores.
- **Raptors Live Demo 2026** (`evt_demo`): **open** for 30 days after first boot, for trying the full lifecycle.
  Its community vote is open too (Events → the event → Community vote).

## ✅ Run the acceptance checker

```bash
python3 run.py .dogfood.toml > acceptance-report.txt
```

The four `Cookie:` headers in [.dogfood.toml](.dogfood.toml) are fixed demo sessions that the portal creates on boot
while `DEMO_SESSIONS=1` (the default). They are public, so set `DEMO_SESSIONS=0` for any real deployment.

## 🧪 Run the tests

```bash
pip install -r requirements-dev.txt
pytest
```

223 tests, most of them over HTTP the way curl and run.py see the app, including every "no" in the role
table (participant/judge/visitor against organizer routes, judge B against judge A's scores, etc.).

## ✨ What it does

**🧱 T1 — core**
- Accounts with bcrypt passwords and server-side sessions (hashed at rest, HttpOnly, SameSite=Lax), CSRF
  protection on every form, roles: visitor, participant, judge, organizer, admin — checked in the backend.
- Organizers create and edit events: open/close dates (UTC), tracks, prizes, team size, custom questions.
- Teams by invite link (POST to join, size limit that holds under concurrent joins, rotatable link).
- Projects: draft → edit → submit, until the deadline. Title, tagline, description, thumbnail, demo video,
  repo, live link, tags, track, custom answers.
- Deadline enforced on the server with the database clock, for create **and** edit (409).
- Public gallery with search and filters (event, track, tag). The fixture's duplicate submission is detected,
  flagged and kept out of the gallery and results.

**🧑‍⚖️ T2 — judging**
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

**🗳️ T3 — public**
- Community voting by logged-in participants: one vote per project (database unique rule), never for your own
  team, only inside the organizer's voting window (database clock). Holds under concurrent duplicate votes.
- Vote tallies hidden from everyone but staff until the window closes.
- Ballot in a random order per user (seeded by user + event: stable for you, different for everyone else).
- Comments on gallery projects (rate-limited, escaped), which staff can hide; hidden comments are kept, not deleted.
- Anti-abuse: login required, rate limits on votes and comments, every vote / duplicate / rate-limit hit in the
  audit log, and a staff-only signal counting votes from accounts created after voting opened.
  Sybil accounts are **not** fully stopped (open registration, no email) — see [THREAT-MODEL.md](THREAT-MODEL.md).

**🎁 Extras:** every page action also has a JSON API, with OpenAPI docs at `/docs`.

## 🛠️ Tech stack

| Part | Choice | Why |
|---|---|---|
| Backend | Python + **FastAPI** | Dependencies are a natural place for auth and role checks |
| Database | **PostgreSQL 16** + SQLAlchemy 2 | Real constraints, row locks, one clock for deadlines |
| Frontend | Server-rendered **Jinja2** + Pico.css | No JavaScript build, no CDN, strict Content Security Policy |
| Auth | Our own: bcrypt + server-side sessions + CSRF tokens | No auth-as-a-service, per the rules |
| Run | **Docker Compose** (app + db) | One command, offline |
| Tests | **pytest** (223 tests) | Most hit the API over HTTP, like curl does |

## 📚 Documents

| | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request flow, where the auth and role checks live and why |
| [DATA-MODEL.md](DATA-MODEL.md) | Every table, how fixtures are imported, how data gets in and out |
| [JUDGING.md](JUDGING.md) | Assignment, rubric maths, normalization and every awkward fixture case |
| [THREAT-MODEL.md](THREAT-MODEL.md) | Assets, actors, trust boundaries; each attack stopped / partly / not stopped, with the test that proves it |
| [normalization-report.md](normalization-report.md) | Raw vs normalized ranking on the real fixtures (`python scripts/normalization_report.py`) |

## ⚠️ What doesn't work yet (honest list)

- **Sybil voting is only detected, not prevented.** Anyone can register and vote; we flag votes from new
  accounts to staff but don't block them.
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

## 🎬 Demo video

_Link to be added._

## 👤 Author

**Krishiv Sharma**
- 🎓 2nd-year B.Tech CSE (Cybersecurity) student
- 🛡️ Built solo for **DOGFOOD 2026** by Hackathon Raptors, with a focus on backend-enforced isolation and an honest threat model
- 🐙 GitHub: [@KrishivSharma45](https://github.com/KrishivSharma45)

## 📄 License

[MIT](LICENSE)

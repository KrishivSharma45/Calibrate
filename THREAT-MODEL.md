# Threat model

What we protect, from whom, and — for each attack — whether it is **stopped**, **partly stopped** or
**not stopped**, with the mechanism and the test that proves it. Where we did not solve something we say so;
the honest list is more useful to whoever deploys this than a heroic one.

Scope: the portal as shipped (`docker compose up`, T1 + T2). Community voting (T3) is **not built**, so
attacks on public voting are assessed as "not applicable yet" with what would be needed.

---

## 1. Assets

| Asset | Why it matters |
|---|---|
| Judge scores and comments | The outcome of the event. Leaking them early lets people lobby or retaliate; changing them changes winners. |
| Results before publishing | Same, for the aggregate. |
| Submissions (drafts especially) | Unfinished work; ideas can be copied before the deadline. |
| The deadline | Fairness: everyone gets the same amount of time. |
| Accounts, sessions, roles | Whoever holds an organizer session controls the event. |
| Team invite links, judge invite links | Bearer secrets: holding one lets you join a team / become that judge. |
| The audit log | The evidence if anything above is disputed. |
| Participant emails | Personal data. |

## 2. Actors

| Actor | Can do legitimately | What they might try |
|---|---|---|
| Visitor (anonymous) | Browse gallery, published results | Scrape, read drafts, read scores, guess links |
| Participant | Team up, submit until deadline | Submit late, edit another team's project, read scores/results early, become a judge |
| Judge | Score assigned projects in own tracks | Read other judges' scores, score outside their track, change scores after publishing, collude |
| Organizer | Run the event, see everything | Move the deadline for one team, quietly change weights/scores (insider) |
| Admin | Change roles | Abuse of power (insider) |
| Outside attacker | Nothing | Session theft, CSRF, XSS, injection, brute force, DoS |

## 3. Trust boundaries

```
 Browser / curl / run.py
        │  HTTP (TLS is the deployer's reverse proxy; not in compose)
        ▼
 ┌─────────────────────── app container (FastAPI, non-root) ───────────────────────┐
 │  every request: attach_auth (session cookie → user, role from DB)               │
 │                 csrf_protect (unsafe + non-JSON needs a token)                  │
 │  route: require_user / require_role  → 401 / 403                               │
 │  service: object checks (own team? own score? assigned + in track? deadline?)   │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 │ SQL (ORM, bound parameters)
                                 ▼
                     Postgres container (not published to the host)
```

Everything left of the app is untrusted, including our own HTML: a hidden button is never a control. The
database is trusted; a DB superuser can do anything (see §5, audit log tampering).

---

## 4. Attacks

### Sybil votes — **not applicable yet / would not be stopped**
There is no community voting (T3 not built). If it were added today, registration is open, needs no email
verification (the portal must run offline, so we cannot send mail), and has no CAPTCHA. One person could make
many accounts. What would be needed: invite- or event-code-gated registration, one vote per *verified* person,
and rate limits per IP. We are not claiming any of this.

### Ballot stuffing — **stopped for judge scores; voting not built**
- One score per (judge, project) is a **database unique constraint**; a second submit updates the same row.
  `test_judge_cannot_score_same_project_twice`.
- Values must be whole numbers 1–5 (app check and DB `CHECK`). `test_invalid_scores_rejected`, `test_score_value_must_be_1_to_5`.
- A judge can only score projects assigned to them **and** in their tracks. `test_wrong_assignment_still_cannot_cross_tracks`.

### Submission scraping — **partly stopped**
- The gallery is **public by design** (a T1 requirement), so submitted projects can be scraped. No rate limit on the gallery.
- **Drafts** return **404** (not 403) to non-members and judges, so their existence is not even confirmed. `test_draft_is_private_until_submitted`.
- Team pages and invite links are hidden from non-members (404). `test_outsiders_cannot_see_team_or_its_invite_link`.
- Participant emails never appear in public pages or the gallery API; only names of team members.

### Judge collusion — **partly stopped**
- Judges cannot see each other's scores or comments (backend-enforced; §IDOR below), so they cannot anchor on each other through the portal.
- Normalization limits how much a single judge's *scale* matters, and flat or single-score judges get weight 0.
- Every score create/update is audited with before/after values, so late coordinated changes are visible.
- **Not stopped:** judges agreeing out of band to push a project. We do not detect statistical collusion (e.g. two judges' z-scores being suspiciously aligned). Mitigation is assignment spread (≈3 judges per project, least-loaded) and human review of the audit log.

### Deadline gaming — **stopped**
- The deadline is checked **on the server** using the **database clock** (`SELECT now()`), not the browser's, on **create and on edit**. Returns **409 `submissions_closed`**. `test_closed_event_refuses_submission_like_run_py`, `test_teammate_can_edit_until_deadline_then_not`.
- An event that has not opened yet also refuses. `test_not_yet_open_event_refuses`.
- Teams cannot be created or joined after the deadline (no "add a member who wrote code later"). `test_no_team_changes_after_deadline`.
- There is no per-team extension. If an organizer moves the deadline for everyone, it is logged as its own action `event.deadline_change` with old and new times. `test_deadline_change_is_audited_with_old_and_new`.

### IDOR on scores — **stopped**
- `GET /api/judge/scores?judge=<id>`: a judge gets **403 for any id but their own**, including ids that do not exist (no probing for valid ids). The query is always filtered by `current_user.id`. `test_judge_b_cannot_read_other_judges` (3 cases), run.py check 5.
- Participants 403, visitors 401. `test_participant_blocked`, `test_visitor_blocked`.
- Writing: the score row is looked up by `(current_user.id, project_id)`, so no request can touch another judge's row. `test_judge_cannot_overwrite_another_judges_score`.
- Projects outside the judge's queue are 404, even if an organizer mis-assigns (track check is independent). `test_wrong_assignment_still_cannot_cross_tracks`.
- Results are 403 to everyone but staff until published; the public view has no per-judge data. `test_results_hidden_before_publish`, `test_publish_then_public_view_has_no_judge_data`.
- Organizer-only routes: 27 combinations (9 routes × judge/participant/visitor) tested. `test_organizer_tools_are_staff_only`.

### Session theft — **partly stopped**
Stopped / reduced:
- Cookie is `HttpOnly` (JavaScript cannot read it) and `SameSite=Lax`. `test_json_login_sets_hardened_cookie`.
- 256-bit random tokens; the database stores only **SHA-256** of the token, so a DB dump does not contain usable sessions.
- New token on every login (session fixation), server-side logout (a copied cookie dies), 7-day expiry, expired rows deleted. `test_login_rotates_session`, `test_logout_kills_session_server_side`, `test_garbage_or_expired_session_is_anonymous`.
- XSS, the usual way to steal sessions, is reduced by Jinja autoescaping, a strict CSP (`default-src 'self'`, no inline script), and storing only `http(s)` URLs. `test_gallery_escapes_html`, `test_bad_input_is_rejected`.

Not stopped:
- The `Secure` flag is **off by default** because the demo runs on plain `http://localhost`. Behind TLS, set `COOKIE_SECURE=1`. Compose does not provide TLS.
- The **four demo session tokens are public** (they are in `.dogfood.toml` and the README) and the seeded accounts share the password `dogfood-demo`. They exist only while `DEMO_SESSIONS=1`; set `DEMO_SESSIONS=0` and boot deletes them (`test_demo_sessions_removed_when_disabled`). The shared password is **not** removed automatically — change or delete seeded accounts before real use.
- No binding of a session to IP / device.

### CSRF — **stopped**
- A single **app-wide** dependency protects every POST/PUT/PATCH/DELETE, so a new route cannot forget it.
- Form posts need a per-session token (compared in constant time); anonymous forms (login/register) use a double-submit cookie. `test_form_post_without_csrf_token_is_refused`, `test_anonymous_form_needs_matching_cookie`.
- JSON requests are exempt **because** a cross-site page cannot send `Content-Type: application/json` without a CORS preflight, and the app sends no CORS headers. The classic `enctype="text/plain"` fake-JSON trick is refused. `test_text_plain_post_is_refused`.
- `SameSite=Lax` is a second layer.
- State never changes on GET: opening a team invite link only shows a confirmation page; joining is a POST. `test_create_team_and_join_by_invite`.

### Privilege escalation — **stopped**
- Self-registration always creates a participant; a `role` field in the form is ignored. `test_register_cannot_choose_role`.
- Only **admins** change roles (organizers cannot). Admins cannot change their own role. `test_non_admins_cannot_change_roles`, `test_admin_cannot_change_own_role`, `test_participant_cannot_escalate_self`.
- Anyone on a team cannot be made a judge or staff (would judge their own project). `test_team_member_cannot_become_judge`.
- The role is read from the database on **every request**, so a demotion takes effect immediately, even for live sessions. `test_admin_changes_role_and_it_is_audited_and_immediate`.
- Organizers can invite judges, but cannot turn an existing participant/organizer into a judge. `test_cannot_invite_a_participant_as_judge`.
- Judges and organizers cannot join or create teams. `test_judges_and_organizers_cannot_join_teams`.

### CSV injection in exports — **stopped (for the known trigger characters)**
- Any cell starting with `=`, `+`, `-`, `@`, tab or carriage return gets a leading `'`, so spreadsheets show it as text instead of running it. Team and project names are attacker-controlled and land on the organizer's machine. `test_csv_neutralises_formulas` (uses a `=HYPERLINK(...)` title).
- Exports are staff-only and every export is audited. `test_csv_export_blocked_for_others`, `test_export_is_audited`.
- Caveat: this is the standard OWASP mitigation; it defends spreadsheets, and a program that strips the leading `'` would re-expose the value.

---

## 5. Other attacks we considered

| Attack | Status | How |
|---|---|---|
| Password brute force | **Partly** | 10 failures per email and per IP in 15 min → 429 (`test_login_throttled_after_repeated_failures`). In-memory, per process: resets on restart and is not shared between replicas. bcrypt cost 12. |
| Account enumeration | **Stopped** | Unknown email is checked against a dummy bcrypt hash: same time, same message (`test_wrong_password_and_unknown_email_look_the_same`). Registration does say "email already exists" — a deliberate usability trade-off. |
| Open redirect after login | **Stopped** | `next=` must be a local path; `//evil`, `https://evil`, `/\evil` fall back to `/` (`test_safe_next`, `test_html_login_flow_and_no_open_redirect`). |
| SQL injection | **Stopped** | ORM with bound parameters only; search escapes `%`/`_` (`test_gallery_search_and_filters`). |
| Stored XSS via links | **Stopped** | Only `http(s)` URLs stored; links get `rel="noopener noreferrer nofollow ugc"`. |
| Clickjacking | **Stopped** | `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`. |
| Team invite link leak | **Partly** | 192-bit token, members can rotate it (old one 404s), joining needs login + a POST, team size cap, closes at the deadline, `Referrer-Policy: same-origin`. Anyone holding a valid link can still join until then. |
| Judge invite link leak | **Partly** | One-time use, 7-day expiry, stored hashed, re-invite cancels old links. Whoever opens it first becomes that judge. Links are shown to the organizer (no email offline). |
| Team-size race | **Stopped** | Row lock (`SELECT … FOR UPDATE`) on Postgres + unique constraint backstop; E2E check on Docker: two concurrent joins for the last slot → one 200, one 409. |
| Audit log tampering | **Partly** | The app only ever inserts audit rows (no update/delete path). A database superuser can still edit them; no hash-chaining or external log shipping. |
| Insider organizer changes weights after seeing scores | **Partly** | Allowed (sometimes needed), but audited with before/after weights; criteria cannot be added/removed once scoring starts. |
| Denial of service | **Not stopped** | No global rate limiting or request-size limits beyond defaults. Put a reverse proxy in front. |
| Dependency / supply chain | **Partly** | Pinned versions in `requirements.txt`; no automated vulnerability scanning. |

## 6. If you deploy this for real

1. `DEMO_SESSIONS=0`, and delete or re-password the seeded demo accounts.
2. Run behind TLS and set `COOKIE_SECURE=1`.
3. Change the Postgres password in `docker-compose.yml` (it is only reachable inside the compose network).
4. Put a reverse proxy with rate limiting in front (gallery scraping, DoS, login throttling across replicas).
5. Ship the audit log somewhere the database admin cannot edit, if disputes are likely.

# Judging

How projects get judges, how a score becomes a number, how judges are evened out, and what we do with
the awkward cases in the fixtures. Every number in this file comes from the real `fixtures.json` via
`python scripts/normalization_report.py` (full output: [normalization-report.md](normalization-report.md)).

Code: [`services/assignment.py`](src/app/services/assignment.py) (assignment, rubric),
[`services/judging.py`](src/app/services/judging.py) (scores, isolation),
[`services/results.py`](src/app/services/results.py) (normalization, ranking).

---

## 1. Assignment

**Goal:** every project gets about 3 reviews, no judge is overloaded, and judges only see their own tracks.

- **Eligible project:** submitted, not a flagged duplicate, has a track.
- **Eligible judge:** a judge attached to that project's track (`judge_tracks`).

**Algorithm** (`plan_assignments`, a pure function with its own unit tests):

1. Sort projects **most-constrained first**: fewest eligible judges, then fewest current assignments, then id.
   If easy projects went first they could use up the only judge a hard project can have.
2. For each missing slot (`target − current`), give it to the eligible judge with the **lowest load in this
   event**, ties broken by judge id. Same input → same output, so an organizer can explain any assignment.
3. **Add-only.** Existing assignments (and the scores behind them) are never removed, so running it twice is
   safe; the second run adds nothing. If a track has fewer judges than the target, the project is reported
   as *short* rather than borrowing a judge from another track.

Organizers can then add a judge by hand (must be in the project's track) or remove one (refused once that
judge has scored it). Every change is in the audit log.

On the fixture event, the seeded assignments are exactly the 126 scored pairs. 8 projects have only 2 reviews;
auto-assign with target 3 adds exactly 8 assignments, no track is short, and a second run adds 0. The progress
dashboard then shows 7 judges with an *incomplete batch*.

## 2. The rubric and the weighted total

The organizer defines criteria and weights per event (default: functionality 40%, quality 30%, innovation 30%,
the three criteria in the fixtures). Weights must sum to 100% (checked to ±0.1%).

A judge gives each criterion a whole number from 1 to 5. One review becomes one number:

    t = Σ (value_c × weight_c)        e.g. 4, 3, 5 → 4×0.4 + 3×0.3 + 5×0.3 = 4.0

`t` stays on the 1–5 scale. Once any score exists for an event the rubric's *shape* is locked (no adding or
removing criteria, or existing scores would be incomplete); weights and labels can still change, totals are
recomputed, and the change is audited with old and new weights.

## 3. Why not just average?

Judges are not calibrated instruments. In the fixtures, judge averages range from **2.95** (jdg_27) to **4.23**
(jdg_02). A project that happens to get a generous judge gains about a point over an equally good project that
got a harsh one. With 2–5 reviews per project, that luck does not average out.

The test `test_harsh_judges_favourite_beats_generous_judges_least_favourite` is the argument in one example:
a harsh judge gives 3, 2, 1 and a generous judge gives 5, 5, 4. Raw averaging ranks the generous judge's
*least* favourite (4) above the harsh judge's *favourite* (3). Normalization reverses that.

## 4. Normalization: per-judge z-scores

For each judge *j* in the event (over their weighted totals `t`):

    μ_j = mean(t),  σ_j = population std-dev(t),  n_j = number of scores

    z = (t − μ_j) / σ_j          "how much better than this judge's usual"

**Confidence weight** per judge:

    w_j = 0                  if σ_j = 0   (flat judge, or only one score)
    w_j = min(1, n_j / 3)    otherwise    (fewer than 3 scores → less weight)

**Per project** *p*, over its reviews:

    z̄_p = Σ w_j · z_pj / Σ w_j           (undefined if Σ w_j = 0)
    normalized_p = μ + z̄_p · σ            μ, σ = mean and std-dev of ALL totals in the event

The last step puts the result back on the familiar 1–5-ish scale (fixture event: μ = 3.577, σ = 0.636), so raw
and normalized numbers can sit side by side. It is not clamped; a very strong project can exceed 5.

**Ranking:** normalized desc, then raw mean desc, then number of reviews desc, then project id (so ties are
always broken the same way). Each project also gets a rank within its track and its raw rank, and the results
page shows the movement.

## 5. The awkward cases

### The judge who marks everything the same (jdg_07)

jdg_07 (Iva Petrova) gave **4/4/4 to all three** projects. σ = 0, so z is undefined: this judge's scores contain
no information about which project is better.

Two options: treat their z as 0 (a vote for "exactly average"), or let them **abstain** (weight 0). We abstain.
A zero vote pulls every project they reviewed toward the middle — that *hurts* strong projects and *helps*
weak ones, purely because of who was assigned. Test: `test_flat_judge_abstains_instead_of_voting_average`.

Real effect in the fixtures: **Small Relay** (prj_19) ranks **14th raw** but **33rd normalized**. Its raw mean
3.65 is propped up by jdg_07's automatic 4.0. Its only informative reviewer, jdg_29, gave a total of 3.3 against
their own average of 3.53 (z = −0.59). Without the flat judge, the evidence says below average.

The flat judge is flagged on the results page and the project shows `low_confidence` (effective reviews 1.0).

### Judges with one or two scores

- One score: σ = 0 by definition → same as flat, weight 0. jdg_23 is the case in the fixtures.
- Two scores: σ exists but is a shaky estimate → weight 2/3 and flag `few_scores`
  (jdg_03, jdg_05, jdg_17, jdg_27, jdg_28).
- Projects whose reviewer weights sum below 2 get `low_confidence` so an organizer knows to look twice.

### Projects with 0 reviews (or only abstaining reviewers)

No crash, no invented number: raw mean shown if there is one, normalized is empty, rank is empty, flag
`no_reviews` / `no_informative_reviews`, shown as *not ranked yet*. The fixtures have none, but the live demo
event starts with zero reviews and the results page renders fine (tested).

### Two review batches nobody finished

Unfinished work is an assignment with no score. The progress dashboard shows per judge *assigned / done /
remaining* and marks **not started** and **incomplete** batches, plus projects below the review target.
Normalization only ever uses scores that exist; a missing score is never treated as a low score.

### The duplicate submission (prj_07 / prj_41)

Team tm_07 submitted "Dry Harbour" twice. The later one (prj_41, 17:57) is the real submission; prj_07 is
flagged `duplicate_of = prj_41`. Scores on a flagged duplicate are **ignored**, so the pair is not counted twice
(three judges scored both).

Side effect worth knowing: **jdg_01's only score was on prj_07**, so jdg_01 disappears from the results
entirely, and **jdg_12** drops from two scores to one (and so to weight 0).

### Projects with different numbers of reviews (2 vs 5)

Handled by averaging z over whatever reviews exist and reporting *effective reviews* (Σ w_j). We deliberately
did **not** shrink projects with fewer reviews toward the mean (a Bayesian prior): with this data that would
mostly punish projects for how many judges they were assigned, which they don't control. The trade-off is
that a project with one strong informative review can rank high; it is flagged `low_confidence` instead.

## 6. What changes on the fixture data

**39 of 40** ranked projects change position. The largest moves:

| Project | Raw rank | Normalized rank | Why |
|---|---:|---:|---|
| Small Relay (prj_19) | 14 | 33 | raw mean propped up by flat jdg_07; the only informative reviewer (jdg_29) scored it below their own average (z −0.59) |
| Paper Anchor (prj_39) | 28 | 13 | jdg_18 scored it above their usual (z +1.41), jdg_26 below (z −1.03). jdg_18's σ is only 0.094, so this rise is partly the near-flat effect in §7 |
| Glass Signal (prj_01) | 29 | 18 | jdg_08 placed it low (z −1.15); jdg_03 and jdg_28 placed it high (z +1.0 each) but have 2 scores (weight 2/3), and jdg_28's σ is 0.1 — again partly near-flat |
| Flat Meadow (prj_28) | 27 | 38 | all three reviewers placed it below their own average (z −0.89, −1.46, −0.55); its raw 3.4 came from the two most generous judges (jdg_02 4.23, jdg_30 4.08) |
| Deep Beacon (prj_38) | 11 | 21 | one high score from generous jdg_30 (z +1.18); the other two placed it at or below their average (z −1.02, −0.10) |

The first, fourth and fifth rows are normalization doing its job. The second and third show its main
weakness honestly (§7): judges with a very small spread turn small differences into large z-scores.

Full table with every project and judge: [normalization-report.md](normalization-report.md).

## 7. Known limitations (honest list)

- **Near-flat judges amplify tiny differences.** jdg_18's three totals have σ = 0.094; a 0.13 difference becomes
  z ≈ +1.4, with full weight. jdg_28 (σ = 0.1) does the same at weight 2/3. This visibly moves Paper Anchor and
  Glass Signal (§6). A floor on σ (e.g. treat σ < 0.25 as 0.25) would be the next thing we'd add.
- **z-scores assume judges see comparable batches.** A judge who happened to get only strong projects looks
  "harsh" on them. Balanced, in-track assignment reduces this but cannot remove it with ~3 reviews per project.
- **Rankings across tracks** compare judges who never saw the same projects. The per-track rank is the more
  defensible number for track prizes.
- Population σ (divide by n), not sample σ; with n = 2–3 either choice is rough, which is why weights and flags exist.

## 8. Isolation during judging (what a judge can and cannot see)

| Actor | Own scores | Other judges' scores | Other tracks' projects to score | Aggregate results | Audit log |
|---|---|---|---|---|---|
| Visitor | 401 | 401 | – | 403 until published | 401 |
| Participant | 403 | 403 | – | 403 until published | 403 |
| Judge | 200 | **403** (for any other id, even non-existent) | **404** (even if mis-assigned) | 403 until published | 403 |
| Organizer / admin | all | all | all | always | yes |

All enforced in the backend and tested over HTTP ([tests/test_judging.py](tests/test_judging.py),
[tests/test_results.py](tests/test_results.py)). After publishing, the public sees ranks and scores but never
per-judge data. Scores are frozen once results are published.

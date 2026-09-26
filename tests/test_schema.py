"""The database itself refuses bad data, independent of any route code."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Event,
    Project,
    Role,
    RubricCriterion,
    Score,
    ScoreItem,
    Team,
    TeamMember,
    Track,
    User,
)


@pytest.fixture
def world(db):
    """One event, one track, one team, one judge, one project, one criterion."""
    db.add_all([
        Event(id="evt_1", name="E", submissions_close=datetime(2026, 3, 1, 18, tzinfo=UTC)),
        Track(id="trk_1", event_id="evt_1", name="T"),
        Team(id="tm_1", event_id="evt_1", name="Team", invite_token="tok1"),
        User(id="usr_1", email="p@x.org", name="P", role=Role.participant),
        User(id="jdg_1", email="j@x.org", name="J", role=Role.judge),
        Project(id="prj_1", event_id="evt_1", team_id="tm_1", track_id="trk_1", title="A"),
        RubricCriterion(id="crt_1", event_id="evt_1", key="quality", label="Quality", weight=1),
    ])
    db.commit()
    return db


def test_timestamps_come_back_as_utc(world):
    world.expire_all()
    close = world.get(Event, "evt_1").submissions_close
    assert close.tzinfo is not None
    assert close == datetime(2026, 3, 1, 18, tzinfo=UTC)


def test_naive_datetime_is_rejected(db):
    db.add(Event(id="evt_x", name="X", submissions_close=datetime(2026, 3, 1)))
    with pytest.raises(Exception, match="naive datetime"):
        db.commit()


def test_score_value_must_be_1_to_5(world):
    world.add(Score(id="sc_1", judge_id="jdg_1", project_id="prj_1",
                    items=[ScoreItem(criterion_id="crt_1", value=7)]))
    with pytest.raises(IntegrityError):
        world.commit()


def test_judge_cannot_score_same_project_twice(world):
    world.add(Score(id="sc_1", judge_id="jdg_1", project_id="prj_1"))
    world.commit()
    world.add(Score(id="sc_2", judge_id="jdg_1", project_id="prj_1"))
    with pytest.raises(IntegrityError):
        world.commit()


def test_user_joins_only_one_team_per_event(world):
    world.add(Team(id="tm_2", event_id="evt_1", name="Other", invite_token="tok2"))
    world.add(TeamMember(team_id="tm_1", user_id="usr_1", event_id="evt_1"))
    world.commit()
    world.add(TeamMember(team_id="tm_2", user_id="usr_1", event_id="evt_1"))
    with pytest.raises(IntegrityError):
        world.commit()


def test_team_has_one_active_project(world):
    world.add(Project(id="prj_2", event_id="evt_1", team_id="tm_1", title="B"))
    with pytest.raises(IntegrityError):
        world.commit()


def test_flagged_duplicate_is_allowed(world):
    world.add(Project(id="prj_2", event_id="evt_1", team_id="tm_1", title="A",
                      duplicate_of_id="prj_1"))
    world.commit()
    assert world.get(Project, "prj_2").duplicate_of_id == "prj_1"


def test_weight_out_of_range_rejected(world):
    world.add(RubricCriterion(id="crt_2", event_id="evt_1", key="x", label="X", weight=1.5))
    with pytest.raises(IntegrityError):
        world.commit()

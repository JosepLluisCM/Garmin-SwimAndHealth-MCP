"""
Integration tests for high-level workout builder tools (workout_builders.py).
"""
import json

import pytest
from mcp.server.fastmcp import FastMCP
from unittest.mock import MagicMock

from garmin_mcp import workouts, workout_builders
from garmin_mcp.workout_builders import build_swim_json, _pace_to_mps


@pytest.fixture
def app_with_builders(mock_garmin_client):
    """FastMCP app with workout_builders registered.

    Also configures `workouts` because workout_builders.schedule_week reuses
    the `_is_already_scheduled` helper defined there, which reads from the
    `garmin_client` module-level global in workouts.py. Both modules must
    point at the same mock for the helper to see the right state.
    """
    # Default: pre-check finds no existing schedules so the POST path runs.
    mock_garmin_client.query_garmin_graphql.return_value = {
        "data": {"workoutScheduleSummariesScalar": []}
    }
    workouts.configure(mock_garmin_client)
    workout_builders.configure(mock_garmin_client)
    app = FastMCP("Test Workout Builders")
    app = workout_builders.register_tools(app)
    return app


@pytest.mark.asyncio
async def test_schedule_week_uses_client_post_not_garth(
    app_with_builders, mock_garmin_client
):
    """schedule_week must route through garmin_client.client.post

    Regression: garminconnect 0.3.2 removed the `.garth` attribute. The old
    code called `garmin_client.garth.post(...)` which raises AttributeError.
    This test pins the fix.
    """
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_garmin_client.client.post.return_value = mock_response

    result = await app_with_builders.call_tool(
        "schedule_week",
        {"week": [{"date": "2026-05-12", "workout_id": 1234567890}]},
    )

    assert result is not None
    payload = json.loads(result[0][0].text)
    assert payload["status"] == "complete"
    assert payload["scheduled"][0]["status"] == "scheduled"
    assert payload["scheduled"][0]["workout_id"] == 1234567890
    # Must call .client.post, never .garth.*
    mock_garmin_client.client.post.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_week_is_idempotent(
    app_with_builders, mock_garmin_client
):
    """schedule_week skips the POST when the workout is already scheduled.

    Matches the idempotency behaviour of schedule_workout / schedule_workouts.
    """
    mock_garmin_client.query_garmin_graphql.return_value = {
        "data": {
            "workoutScheduleSummariesScalar": [
                {
                    "workoutId": 1234567890,
                    "scheduleDate": "2026-05-12",
                    "workoutName": "Easy Run",
                }
            ]
        }
    }

    result = await app_with_builders.call_tool(
        "schedule_week",
        {"week": [{"date": "2026-05-12", "workout_id": 1234567890}]},
    )

    assert result is not None
    payload = json.loads(result[0][0].text)
    assert payload["status"] == "complete"
    assert payload["scheduled"][0]["status"] == "already_scheduled"
    assert payload["scheduled"][0]["idempotent"] is True
    # Critically: no POST happened
    mock_garmin_client.client.post.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_week_partial_idempotency(
    app_with_builders, mock_garmin_client
):
    """Mixed week: some entries already scheduled, others new.

    Verifies the pre-check runs per-item, not once for the whole batch.
    """
    def graphql_side_effect(query):
        # Return existing schedule only for 2026-05-12
        if "2026-05-12" in query["query"]:
            return {
                "data": {
                    "workoutScheduleSummariesScalar": [
                        {
                            "workoutId": 111,
                            "scheduleDate": "2026-05-12",
                            "workoutName": "Easy Run",
                        }
                    ]
                }
            }
        return {"data": {"workoutScheduleSummariesScalar": []}}

    mock_garmin_client.query_garmin_graphql.side_effect = graphql_side_effect

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_garmin_client.client.post.return_value = mock_response

    result = await app_with_builders.call_tool(
        "schedule_week",
        {
            "week": [
                {"date": "2026-05-12", "workout_id": 111},  # already scheduled
                {"date": "2026-05-14", "workout_id": 222},  # new
            ]
        },
    )

    payload = json.loads(result[0][0].text)
    scheduled = payload["scheduled"]
    assert scheduled[0]["status"] == "already_scheduled"
    assert scheduled[1]["status"] == "scheduled"
    # Only the new one triggered the POST
    assert mock_garmin_client.client.post.call_count == 1


# =============================================================================
# Swim builder (build_swim_json + create_swim_workout)
# =============================================================================

SWIM_STEPS = [
    {"kind": "warmup", "distance_m": 300, "stroke": "freestyle"},
    {
        "kind": "repeat",
        "iterations": 8,
        "skip_last_rest": True,
        "steps": [
            {
                "kind": "interval",
                "distance_m": 100,
                "stroke": "freestyle",
                "equipment": "swim_pull_buoy",
                "pace_mps": 1.0,
            },
            {"kind": "rest", "send_off_seconds": 90},
        ],
    },
    {"kind": "interval", "distance_m": 200, "stroke": "backstroke",
     "hr_zone": "Z3"},
    {"kind": "rest", "rest_seconds": 30},
    {"kind": "cooldown", "distance_m": 200, "stroke": "freestyle"},
]


def test_build_swim_json_top_level_swim_fields():
    """Sport must be swimming (id 4) and pool length set."""
    wo = build_swim_json("Test Swim", SWIM_STEPS, pool_length_m=25.0)
    assert wo["sportType"] == {"sportTypeId": 4, "sportTypeKey": "swimming"}
    assert wo["poolLength"] == 25.0
    assert wo["poolLengthUnit"]["unitKey"] == "meter"
    assert wo["workoutSegments"][0]["sportType"]["sportTypeId"] == 4


def test_build_swim_json_stroke_and_equipment_ids():
    """Stroke / equipment keys map to confirmed numeric IDs."""
    wo = build_swim_json("Test Swim", SWIM_STEPS)
    steps = wo["workoutSegments"][0]["workoutSteps"]
    warmup = steps[0]
    assert warmup["strokeType"]["strokeTypeId"] == 6  # freestyle
    assert warmup["equipmentType"]["equipmentTypeId"] == 0  # none

    interval = steps[1]["workoutSteps"][0]
    assert interval["strokeType"]["strokeTypeId"] == 6
    assert interval["equipmentType"]["equipmentTypeId"] == 4  # pull buoy


def test_build_swim_json_end_conditions():
    """Distance=3, send-off=9 (fixed.repetition), fixed rest=8."""
    wo = build_swim_json("Test Swim", SWIM_STEPS)
    steps = wo["workoutSegments"][0]["workoutSteps"]

    assert steps[0]["endCondition"]["conditionTypeId"] == 3  # distance
    assert steps[0]["endConditionValue"] == 300.0

    repeat = steps[1]
    assert repeat["type"] == "RepeatGroupDTO"
    assert repeat["numberOfIterations"] == 8
    assert repeat["skipLastRestStep"] is True

    send_off = repeat["workoutSteps"][1]
    assert send_off["stepType"]["stepTypeId"] == 5  # rest
    assert send_off["endCondition"]["conditionTypeId"] == 9  # fixed.repetition
    assert send_off["endConditionValue"] == 90.0

    fixed_rest = steps[3]
    assert fixed_rest["endCondition"]["conditionTypeId"] == 8  # fixed.rest
    assert fixed_rest["endConditionValue"] == 30.0


def test_build_swim_json_pace_omits_primary_target():
    """Exact pace goes to a secondary target; primary targetType is OMITTED."""
    wo = build_swim_json("Test Swim", SWIM_STEPS)
    interval = wo["workoutSegments"][0]["workoutSteps"][1]["workoutSteps"][0]
    assert "targetType" not in interval  # would crash _fix_hr_zone_step if None
    assert interval["secondaryTargetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert interval["secondaryTargetValueOne"] == 1.0
    assert "secondaryTargetValueTwo" not in interval  # no band for swim
    assert interval["secondaryTargetValueUnit"]["unitKey"] == "meter"


def test_build_swim_json_hr_zone_primary_target():
    """hr_zone produces a primary heart.rate.zone target with zoneNumber."""
    wo = build_swim_json("Test Swim", SWIM_STEPS)
    bk = wo["workoutSegments"][0]["workoutSteps"][2]
    assert bk["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert bk["zoneNumber"] == 3
    assert bk["strokeType"]["strokeTypeId"] == 2  # backstroke


def test_build_swim_json_rest_step_has_no_stroke_or_equipment():
    """Rest steps must not carry stroke/equipment blocks."""
    wo = build_swim_json("Test Swim", SWIM_STEPS)
    fixed_rest = wo["workoutSegments"][0]["workoutSteps"][3]
    assert "strokeType" not in fixed_rest
    assert "equipmentType" not in fixed_rest


def test_build_swim_json_time_duration():
    """duration (mm:ss) maps to conditionTypeId 2 (time) in seconds."""
    wo = build_swim_json(
        "Time Swim",
        [{"kind": "interval", "duration": "1:30", "stroke": "freestyle"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert step["endCondition"]["conditionTypeId"] == 2
    assert step["endConditionValue"] == 90.0


def test_build_swim_json_reverse_im_stroke():
    """reverse_im_by_round maps to strokeTypeId 10."""
    wo = build_swim_json(
        "IM Swim",
        [{"kind": "interval", "distance_m": 100, "stroke": "reverse_im_by_round"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert step["strokeType"]["strokeTypeId"] == 10


def test_build_swim_json_drill_type_is_independent_of_stroke():
    """drill_type sets drillType (kick=1) and keeps the real stroke (free=6)."""
    wo = build_swim_json(
        "Drill Swim",
        [{"kind": "interval", "distance_m": 50, "stroke": "freestyle",
          "drill_type": "kick"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert step["drillType"]["drillTypeId"] == 1
    assert step["strokeType"]["strokeTypeId"] == 6  # stroke unchanged


def test_build_swim_json_drill_type_none_omits_drilltype():
    """drill_type 'none' (or omitted) produces no drillType — a regular swim."""
    wo = build_swim_json(
        "Plain Swim",
        [{"kind": "interval", "distance_m": 50, "stroke": "freestyle",
          "drill_type": "none"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert "drillType" not in step


def test_build_swim_json_lap_button_end_condition():
    """lap_button maps to conditionTypeId 1."""
    wo = build_swim_json(
        "Lap Swim",
        [{"kind": "interval", "lap_button": True, "stroke": "freestyle"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert step["endCondition"]["conditionTypeId"] == 1


def test_pace_to_mps_conversions():
    """mm:ss/100m and seconds/100m both convert to m/s."""
    assert _pace_to_mps("1:40") == pytest.approx(1.0)
    assert _pace_to_mps("1:20") == pytest.approx(1.25)
    assert _pace_to_mps(90) == pytest.approx(100.0 / 90)


def test_build_swim_json_pace_per_100m_single():
    """pace_per_100m sets a single exact pace (no band) in m/s."""
    wo = build_swim_json(
        "Pace Swim",
        [{"kind": "interval", "distance_m": 100, "stroke": "freestyle",
          "pace_per_100m": "1:30"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert "targetType" not in step
    assert step["secondaryTargetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert step["secondaryTargetValueOne"] == pytest.approx(100.0 / 90)
    assert "secondaryTargetValueTwo" not in step


def test_build_swim_json_effort_target():
    """effort maps to swim.instruction (id 18) with the level code."""
    wo = build_swim_json(
        "Effort Swim",
        [{"kind": "interval", "distance_m": 100, "stroke": "freestyle",
          "effort": "hard"}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert step["secondaryTargetType"]["workoutTargetTypeId"] == 18
    assert step["secondaryTargetValueOne"] == 5.0  # hard


def test_build_swim_json_css_offset_target():
    """css_offset maps to swim.css.offset (id 17), seconds (negative allowed)."""
    wo = build_swim_json(
        "CSS Swim",
        [{"kind": "interval", "distance_m": 100, "stroke": "freestyle",
          "css_offset": -5}],
    )
    step = wo["workoutSegments"][0]["workoutSteps"][0]
    assert step["secondaryTargetType"]["workoutTargetTypeId"] == 17
    assert step["secondaryTargetValueOne"] == -5.0


def test_build_swim_json_invalid_effort_raises():
    """Unknown effort label raises."""
    with pytest.raises(ValueError, match="Invalid effort"):
        build_swim_json(
            "Bad",
            [{"kind": "interval", "distance_m": 100, "stroke": "freestyle",
              "effort": "sprintish"}],
        )


@pytest.mark.asyncio
async def test_create_swim_workout_tool_uploads(app_with_builders, mock_garmin_client):
    """create_swim_workout uploads the built JSON and returns the new ID."""
    mock_garmin_client.upload_workout.return_value = {
        "workoutId": 555,
        "workoutName": "Test Swim",
    }

    result = await app_with_builders.call_tool(
        "create_swim_workout",
        {"name": "Test Swim", "steps": SWIM_STEPS, "pool_length_m": 25.0},
    )

    payload = json.loads(result[0][0].text)
    assert payload["status"] == "success"
    assert payload["workout_id"] == 555

    # Verify the uploaded payload is a swimming workout
    uploaded = mock_garmin_client.upload_workout.call_args[0][0]
    assert uploaded["sportType"]["sportTypeId"] == 4
    assert uploaded["poolLength"] == 25.0


@pytest.mark.asyncio
async def test_create_swim_workout_tool_invalid_stroke(
    app_with_builders, mock_garmin_client
):
    """Invalid stroke key returns an error, no upload."""
    result = await app_with_builders.call_tool(
        "create_swim_workout",
        {"name": "Bad", "steps": [{"kind": "interval", "distance_m": 100,
                                   "stroke": "doggy_paddle"}]},
    )
    assert "Error creating swim workout" in result[0][0].text
    mock_garmin_client.upload_workout.assert_not_called()

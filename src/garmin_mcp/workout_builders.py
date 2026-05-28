"""
High-level workout builders for Garmin Connect MCP Server.

These tools construct the internal Garmin Connect JSON internally and delegate
to the existing upload_workout / schedule_workout endpoints.
"""
import json
from typing import Any, Dict, List, Optional

# The garmin_client will be set by the main file
garmin_client = None


def configure(client):
    """Configure the module with the Garmin client instance"""
    global garmin_client
    garmin_client = client


# =============================================================================
# JSON BUILDERS
# =============================================================================

HR_ZONE_MAP = {
    "Z1": 1,
    "Z2": 2,
    "Z3": 3,
    "Z4": 4,
    "Z5": 5,
}


def _zone_number(zone: str) -> int:
    """Resolve a human-friendly zone string like 'Z3' to Garmin's zoneNumber."""
    zone_upper = zone.strip().upper()
    if zone_upper in HR_ZONE_MAP:
        return HR_ZONE_MAP[zone_upper]
    # Fallback: if user passed a digit directly
    try:
        z = int(zone_upper)
        if 1 <= z <= 5:
            return z
    except ValueError:
        pass
    raise ValueError(f"Invalid hr_zone '{zone}'. Use Z1-Z5 or 1-5.")


# =============================================================================
# SWIM MAPPINGS (reverse-engineered against a live Garmin account, 25m pool)
# The displayed value keys off the numeric ID; the string key is cosmetic.
# =============================================================================

SWIM_STROKE_TYPES = {
    "choice": 1,  # Garmin key: any_stroke
    "backstroke": 2,
    "breaststroke": 3,
    "drill": 4,
    "butterfly": 5,  # Garmin key: fly
    "freestyle": 6,  # Garmin key: free
    "im": 7,  # Garmin key: individual_medley
    "mixed": 8,
    "im_by_round": 9,  # Garmin key: individual_medley_by_round
    "reverse_im_by_round": 10,  # Garmin key: reverse_individual_medley_by_round
}

# All IDs confirmed live (Garmin keys the display off the numeric ID; the
# string key is cosmetic and gets normalized to fins/kickboard/snorkel on read).
SWIM_EQUIPMENT = {
    "none": 0,
    "swim_fins": 1,
    "swim_kickboard": 2,
    "swim_paddles": 3,
    "swim_pull_buoy": 4,
    "swim_snorkel": 5,
}

# Drill subtype (independent of stroke; 0/none = regular swim).
SWIM_DRILL_TYPES = {
    "none": 0,
    "kick": 1,
    "pull": 2,
    "drill": 3,
}

# Effort levels (secondaryTargetType swim.instruction, id 18). Codes 2 and 8
# are skipped by Garmin's own dropdown.
SWIM_EFFORT = {
    "recovery": 1,
    "easy": 3,
    "moderate": 4,
    "hard": 5,
    "very_hard": 6,
    "all_out": 7,
    "ascending": 9,
    "descending": 10,
}

SWIM_STEP_TYPES = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "interval": (3, "interval"),
    "recovery": (4, "recovery"),
    "rest": (5, "rest"),
}


def _mmss_to_seconds(value: Any) -> float:
    """Parse "mm:ss" / "m:ss" (e.g. "1:30") or a plain number of seconds."""
    if isinstance(value, str) and ":" in value:
        mins, _, secs = value.strip().partition(":")
        return int(mins) * 60 + float(secs)
    return float(value)


def _pace_to_mps(value: Any) -> float:
    """Convert a pace per 100m to meters/second.

    Accepts "mm:ss" / "m:ss" strings (e.g. "1:30"), or a number of seconds
    per 100m (e.g. 90). m/s = 100 / seconds_per_100m.
    """
    seconds = _mmss_to_seconds(value)
    if seconds <= 0:
        raise ValueError(f"Invalid pace '{value}': must be positive")
    return 100.0 / seconds


def _resolve_pace_mps(cfg: dict) -> float | None:
    """Resolve a step's exact target pace to m/s.

    pace_per_100m ("mm:ss"/seconds) or pace_mps (m/s). Garmin's swim editor only
    supports a single exact pace (no band), so only a single value is accepted.
    """
    if cfg.get("pace_per_100m") is not None:
        return _pace_to_mps(cfg["pace_per_100m"])
    if cfg.get("pace_mps") is not None:
        return float(cfg["pace_mps"])
    return None


def _swim_target(cfg: dict) -> dict:
    """Build the target-related keys for a swim step.

    Swim intensity targets all live in the SECONDARY slot:
    - effort -> swim.instruction (id 18)
    - css_offset -> swim.css.offset (id 17), seconds relative to CSS
    - pace (single or band) -> pace.zone (id 6), m/s
    The primary targetType is OMITTED (an explicit None crashes the HR-zone fixer).
    hr_zone still sets a primary heart.rate.zone target, but Garmin IGNORES HR for
    swimming — kept only for compatibility.
    """
    keys: dict = {}
    hr_zone = cfg.get("hr_zone")
    if hr_zone is not None:
        keys["targetType"] = {
            "workoutTargetTypeId": 4,
            "workoutTargetTypeKey": "heart.rate.zone",
        }
        keys["zoneNumber"] = _zone_number(str(hr_zone))
        return keys

    effort = cfg.get("effort")
    css_offset = cfg.get("css_offset")
    pace = _resolve_pace_mps(cfg)

    if effort is not None:
        if effort not in SWIM_EFFORT:
            raise ValueError(
                f"Invalid effort '{effort}'. Use one of: {', '.join(SWIM_EFFORT)}"
            )
        keys["secondaryTargetType"] = {
            "workoutTargetTypeId": 18,
            "workoutTargetTypeKey": "swim.instruction",
        }
        keys["secondaryTargetValueOne"] = float(SWIM_EFFORT[effort])
    elif css_offset is not None:
        keys["secondaryTargetType"] = {
            "workoutTargetTypeId": 17,
            "workoutTargetTypeKey": "swim.css.offset",
        }
        keys["secondaryTargetValueOne"] = float(css_offset)
    elif pace is not None:
        keys["secondaryTargetType"] = {
            "workoutTargetTypeId": 6,
            "workoutTargetTypeKey": "pace.zone",
        }
        keys["secondaryTargetValueOne"] = pace
        keys["secondaryTargetValueUnit"] = {
            "unitId": 1,
            "unitKey": "meter",
            "factor": 100.0,
        }
    return keys


def _swim_end_condition(cfg: dict, kind: str) -> tuple[dict, float]:
    """Resolve a step's end condition to (endCondition dict, endConditionValue)."""
    if cfg.get("lap_button"):
        return {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}, 0.0
    if kind == "rest":
        if cfg.get("send_off_seconds") is not None:
            return (
                {"conditionTypeId": 9, "conditionTypeKey": "fixed.repetition"},
                float(cfg["send_off_seconds"]),
            )
        if cfg.get("rest_seconds") is not None:
            return (
                {"conditionTypeId": 8, "conditionTypeKey": "fixed.rest"},
                float(cfg["rest_seconds"]),
            )
        raise ValueError(
            "rest step needs one of: rest_seconds, send_off_seconds, lap_button"
        )
    # swim block: distance, time, or lap button.
    if cfg.get("distance_m") is not None:
        return (
            {"conditionTypeId": 3, "conditionTypeKey": "distance"},
            float(cfg["distance_m"]),
        )
    if cfg.get("duration") is not None:
        return (
            {"conditionTypeId": 2, "conditionTypeKey": "time"},
            _mmss_to_seconds(cfg["duration"]),
        )
    raise ValueError(
        f"swim '{kind}' step needs one of: distance_m, duration, lap_button"
    )


def _auto_swim_description(cfg: dict, kind: str) -> str | None:
    """Generate a watch-facing description when the caller omitted one."""
    if kind == "rest":
        if cfg.get("send_off_seconds") is not None:
            s = int(cfg["send_off_seconds"])
            return f"On {s // 60}:{s % 60:02d}"
        if cfg.get("rest_seconds") is not None:
            return f"Rest {int(cfg['rest_seconds'])}s"
        return None
    stroke = cfg.get("stroke", "freestyle")
    parts = []
    if cfg.get("distance_m") is not None:
        parts.append(f"{int(cfg['distance_m'])}m")
    elif cfg.get("duration") is not None:
        secs = int(_mmss_to_seconds(cfg["duration"]))
        parts.append(f"{secs // 60}:{secs % 60:02d}")
    parts.append(stroke)
    equip = cfg.get("equipment", "none")
    if equip and equip != "none":
        parts.append(f"w/ {equip.replace('swim_', '').replace('_', ' ')}")
    return " ".join(parts) if parts else None


def _build_swim_step(cfg: dict, step_order: int) -> dict:
    """Build a single executable swim step (or rest step) DTO."""
    kind = cfg.get("kind", "interval")
    if kind not in SWIM_STEP_TYPES:
        raise ValueError(
            f"Invalid swim step kind '{kind}'. "
            f"Use one of: {', '.join(SWIM_STEP_TYPES)}"
        )
    step_type_id, step_type_key = SWIM_STEP_TYPES[kind]
    end_condition, end_value = _swim_end_condition(cfg, kind)

    step: dict = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": step_type_id, "stepTypeKey": step_type_key},
        "endCondition": end_condition,
        "endConditionValue": end_value,
    }

    description = cfg.get("description") or _auto_swim_description(cfg, kind)
    if description:
        step["description"] = description

    # Targets (HR zone or secondary pace); omitted entirely when not set.
    step.update(_swim_target(cfg))

    # Stroke + equipment apply to swim blocks, not rest steps.
    if kind != "rest":
        stroke_key = cfg.get("stroke", "freestyle")
        if stroke_key not in SWIM_STROKE_TYPES:
            raise ValueError(
                f"Invalid stroke '{stroke_key}'. "
                f"Use one of: {', '.join(SWIM_STROKE_TYPES)}"
            )
        step["strokeType"] = {
            "strokeTypeId": SWIM_STROKE_TYPES[stroke_key],
            "strokeTypeKey": stroke_key,
            "displayOrder": 0,
        }
        # Drill subtype is orthogonal to stroke: the app keeps the real stroke
        # (e.g. freestyle) and marks kick/pull/drill via drillType only.
        # "none" (or omitted) = a regular swim, no drillType set.
        drill_key = cfg.get("drill_type")
        if drill_key is not None and drill_key != "none":
            if drill_key not in SWIM_DRILL_TYPES:
                raise ValueError(
                    f"Invalid drill_type '{drill_key}'. "
                    f"Use one of: {', '.join(SWIM_DRILL_TYPES)}"
                )
            drill_id = SWIM_DRILL_TYPES[drill_key]
            step["drillType"] = {
                "drillTypeId": drill_id,
                "drillTypeKey": drill_key,
                "displayOrder": drill_id,
            }
        equip_key = cfg.get("equipment", "none")
        if equip_key not in SWIM_EQUIPMENT:
            raise ValueError(
                f"Invalid equipment '{equip_key}'. "
                f"Use one of: {', '.join(SWIM_EQUIPMENT)}"
            )
        step["equipmentType"] = {
            "equipmentTypeId": SWIM_EQUIPMENT[equip_key],
            "equipmentTypeKey": equip_key,
            "displayOrder": 0,
        }

    return step


def _build_swim_steps(steps: List[Dict[str, Any]]) -> List[dict]:
    """Recursively build a list of swim step / repeat-group DTOs.

    stepOrder restarts at 1 within each level (nested steps included), matching
    the known-good walk/run builder which Garmin accepts.
    """
    built: List[dict] = []
    for i, cfg in enumerate(steps, start=1):
        if cfg.get("kind") == "repeat":
            iterations = int(cfg.get("iterations", 1))
            group = {
                "type": "RepeatGroupDTO",
                "stepOrder": i,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                "numberOfIterations": iterations,
                "smartRepeat": False,
                "endCondition": {
                    "conditionTypeId": 7,
                    "conditionTypeKey": "iterations",
                },
                "endConditionValue": float(iterations),
                "workoutSteps": _build_swim_steps(cfg.get("steps", [])),
            }
            if cfg.get("skip_last_rest"):
                group["skipLastRestStep"] = True
            built.append(group)
        else:
            built.append(_build_swim_step(cfg, i))
    return built


def build_swim_json(
    name: str,
    steps: List[Dict[str, Any]],
    pool_length_m: float = 25.0,
    description: Optional[str] = None,
) -> dict:
    """Build the Garmin Connect JSON for a swimming workout.

    See create_swim_workout for the structure of `steps`.
    """
    workout: dict = {
        "workoutName": name,
        "sportType": {"sportTypeId": 4, "sportTypeKey": "swimming"},
        "poolLength": float(pool_length_m),
        "poolLengthUnit": {"unitId": 1, "unitKey": "meter", "factor": 100.0},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {"sportTypeId": 4, "sportTypeKey": "swimming"},
                "workoutSteps": _build_swim_steps(steps),
            }
        ],
    }
    if description:
        workout["description"] = description
    return workout


def build_walk_run_json(
    name: str,
    run_seconds: int,
    walk_seconds: int,
    repeats: int,
    warmup_min: int,
    cooldown_min: int,
    hr_zone: str = "Z3",
) -> dict:
    """Build the Garmin Connect JSON for a walk/run interval workout.

    Parameters match create_walk_run_workout exactly.
    """
    zone = _zone_number(hr_zone)
    return {
        "workoutName": name,
        "description": (
            f"{warmup_min}m warmup + {repeats}x({run_seconds}s run / {walk_seconds}s walk) Z{zone} + "
            f"{cooldown_min}m cooldown"
        ),
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                    "description": f"Warmup {warmup_min} min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(warmup_min * 60),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": 2,
                    "numberOfIterations": repeats,
                    "workoutSteps": [
                        {
                            "type": "ExecutableStepDTO",
                            "stepOrder": 1,
                            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                            "description": f"Run {run_seconds}s Z{zone}",
                            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                            "endConditionValue": float(run_seconds),
                            "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                            "zoneNumber": zone,
                        },
                        {
                            "type": "ExecutableStepDTO",
                            "stepOrder": 2,
                            "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                            "description": f"Walk {walk_seconds}s Z{zone}",
                            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                            "endConditionValue": float(walk_seconds),
                            "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                            "zoneNumber": zone,
                        },
                    ],
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 3,
                    "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                    "description": f"Cooldown {cooldown_min} min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(cooldown_min * 60),
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
            ],
        }],
    }


def build_z2_walk_json(
    name: str,
    duration_min: int,
    hr_min: int,
    hr_max: int,
) -> dict:
    """Build the Garmin Connect JSON for a steady Z2 walking workout with absolute HR range."""
    return {
        "workoutName": name,
        "description": f"Walk {duration_min} min at Z2 ({hr_min}-{hr_max} bpm)",
        "sportType": {"sportTypeId": 12, "sportTypeKey": "walking"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 12, "sportTypeKey": "walking"},
            "workoutSteps": [
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 1,
                    "stepType": {"stepTypeId": 1, "stepTypeKey": "warmup"},
                    "description": "Warmup 5 min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": 300.0,
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 2,
                    "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                    "description": f"Walk {duration_min} min Z2",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": float(duration_min * 60),
                    "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
                    "zoneNumber": 2,
                },
                {
                    "type": "ExecutableStepDTO",
                    "stepOrder": 3,
                    "stepType": {"stepTypeId": 2, "stepTypeKey": "cooldown"},
                    "description": "Cooldown 5 min",
                    "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                    "endConditionValue": 300.0,
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
                },
            ],
        }],
    }


# Simplified internal exercise catalog (English → Garmin exerciseName key or fallback)
# Garmin strength workouts use exerciseName as a free-text label when the exercise
# is not in their catalog. For structured strength, we use "Other" (generic) and
# put the user name in description / exerciseName.

def build_strength_json(
    name: str,
    exercises: List[Dict[str, Any]],
) -> dict:
    """Build the Garmin Connect JSON for a strength workout.

    Each exercise maps to a generic step; if the name is not recognised in the
    Garmin catalog we use 'Other' and put the original name in exerciseName.
    """
    steps: List[dict] = []
    step_order = 1

    for ex in exercises:
        ex_name = ex.get("name", "Exercise")
        sets = int(ex.get("sets", 1))
        reps = int(ex.get("reps", 1))
        rest_seconds = int(ex.get("rest_seconds", 60))

        # Work step
        steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
            "description": f"{ex_name}: {sets} sets x {reps} reps",
            "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
            "endConditionValue": float(sets * 45),  # rough estimate: 45s per set
            "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            "exerciseName": ex_name,
        })
        step_order += 1

        # Rest step (skip after last exercise)
        if rest_seconds > 0 and ex != exercises[-1]:
            steps.append({
                "type": "ExecutableStepDTO",
                "stepOrder": step_order,
                "stepType": {"stepTypeId": 4, "stepTypeKey": "recovery"},
                "description": f"Rest {rest_seconds}s",
                "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time"},
                "endConditionValue": float(rest_seconds),
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            })
            step_order += 1

    return {
        "workoutName": name,
        "description": f"Strength: {len(exercises)} exercises",
        "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 5, "sportTypeKey": "strength_training"},
            "workoutSteps": steps,
        }],
    }


# =============================================================================
# MCP TOOLS
# =============================================================================

def register_tools(app):
    """Register all high-level workout builder tools with the MCP server app"""

    @app.tool()
    async def create_walk_run_workout(
        name: str,
        run_seconds: int,
        walk_seconds: int,
        repeats: int,
        warmup_min: int,
        cooldown_min: int,
        hr_zone: str = "Z3",
    ) -> str:
        """Create a walk/run interval workout and upload it to Garmin Connect.

        Builds the internal Garmin JSON automatically and returns the new workout ID.

        Args:
            name: Workout name (e.g. "W3 Mié 2:2")
            run_seconds: Duration of each run interval in seconds
            walk_seconds: Duration of each walk/recovery interval in seconds
            repeats: Number of run/walk repetitions
            warmup_min: Warmup duration in minutes
            cooldown_min: Cooldown duration in minutes
            hr_zone: Target heart-rate zone (Z1-Z5, default Z3)
        """
        try:
            workout_json = build_walk_run_json(
                name=name,
                run_seconds=run_seconds,
                walk_seconds=walk_seconds,
                repeats=repeats,
                warmup_min=warmup_min,
                cooldown_min=cooldown_min,
                hr_zone=hr_zone,
            )
            result = garmin_client.upload_workout(workout_json)

            if isinstance(result, dict):
                curated = {
                    "status": "success",
                    "workout_id": result.get("workoutId"),
                    "name": result.get("workoutName"),
                    "message": "Workout uploaded successfully",
                }
                curated = {k: v for k, v in curated.items() if v is not None}
                return json.dumps(curated, indent=2)
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error creating walk/run workout: {str(e)}"

    @app.tool()
    async def create_z2_walk_workout(
        name: str,
        duration_min: int,
        hr_min: int,
        hr_max: int,
    ) -> str:
        """Create a steady Z2 walking workout and upload it to Garmin Connect.

        Args:
            name: Workout name
            duration_min: Main walking block duration in minutes
            hr_min: Minimum heart rate in bpm (used for description; target is Z2)
            hr_max: Maximum heart rate in bpm (used for description; target is Z2)
        """
        try:
            workout_json = build_z2_walk_json(
                name=name,
                duration_min=duration_min,
                hr_min=hr_min,
                hr_max=hr_max,
            )
            result = garmin_client.upload_workout(workout_json)

            if isinstance(result, dict):
                curated = {
                    "status": "success",
                    "workout_id": result.get("workoutId"),
                    "name": result.get("workoutName"),
                    "message": "Workout uploaded successfully",
                }
                curated = {k: v for k, v in curated.items() if v is not None}
                return json.dumps(curated, indent=2)
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error creating Z2 walk workout: {str(e)}"

    @app.tool()
    async def create_strength_workout(
        name: str,
        exercises: List[Dict[str, Any]],
    ) -> str:
        """Create a strength workout and upload it to Garmin Connect.

        Each exercise is mapped to a generic step; unsupported names fallback to
        "Other" with the original name stored in exerciseName.

        Args:
            name: Workout name
            exercises: List of dicts with keys: name, sets, reps, rest_seconds
        """
        try:
            workout_json = build_strength_json(name=name, exercises=exercises)
            result = garmin_client.upload_workout(workout_json)

            if isinstance(result, dict):
                curated = {
                    "status": "success",
                    "workout_id": result.get("workoutId"),
                    "name": result.get("workoutName"),
                    "message": "Workout uploaded successfully",
                }
                curated = {k: v for k, v in curated.items() if v is not None}
                return json.dumps(curated, indent=2)
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error creating strength workout: {str(e)}"

    @app.tool()
    async def create_swim_workout(
        name: str,
        steps: List[Dict[str, Any]],
        pool_length_m: float = 25.0,
        description: Optional[str] = None,
    ) -> str:
        """Create a detailed swimming workout and upload it to Garmin Connect.

        Builds the correct Garmin swim DTO (stroke types, equipment, distance
        blocks, timed rest, send-off intervals, pool length, skip-last-rest)
        and uploads it. You provide a structured `steps` list; this tool maps
        the human-friendly keys to Garmin's numeric IDs so the workout syncs to
        the watch correctly.

        Args:
            name: Workout name (e.g. "Threshold 8x100").
            steps: Ordered list of step dicts. Each step has a "kind":
                Swim blocks — kind = "warmup" | "interval" | "cooldown" | "recovery":
                    Provide exactly one end condition:
                    - distance_m (float): block distance in meters
                    - duration (str/int): block time as "mm:ss" or seconds
                    - lap_button (bool): end on lap-button press
                    - stroke (str): freestyle (default), backstroke, breaststroke,
                      butterfly, im, mixed, im_by_round, reverse_im_by_round,
                      drill, choice
                    - drill_type (str): marks the step as kick, pull, or drill
                      (kept independent of stroke, e.g. freestyle + kick). "none"
                      or omitted = a regular swim.
                    - equipment (str): none (default), swim_paddles, swim_pull_buoy,
                      swim_kickboard, swim_fins, swim_snorkel
                    - description (str): free text shown on the watch (auto-generated if omitted)
                    Intensity target (set at most one; all map to swim's secondary slot):
                    - pace_per_100m (str/int): exact target pace "mm:ss" per 100m
                      (e.g. "1:30") or seconds per 100m. (Garmin swim has no pace band.)
                    - pace_mps (float): same exact pace in raw m/s (1.0 = 1:40/100m).
                    - effort (str): perceived effort — recovery, easy, moderate, hard,
                      very_hard, all_out, ascending, descending.
                    - css_offset (int): seconds relative to your Critical Swim Speed
                      (e.g. 0, -5, 5).
                    - hr_zone (str/int): "Z1".."Z5". NOTE: Garmin IGNORES HR targets for
                      swimming (kept for compatibility) — use pace/effort/css_offset.
                Rest steps — kind = "rest":
                    - rest_seconds (float): fixed countdown rest (e.g. 30 -> "rest 0:30")
                    - send_off_seconds (float): send-off / interval clock (e.g. 90 -> "on 1:30")
                    - lap_button (bool): rest until lap-button press
                    (provide exactly one)
                Repeat groups — kind = "repeat":
                    - iterations (int): number of repeats
                    - skip_last_rest (bool): drop the trailing rest on the final repeat
                    - steps (list): nested step dicts (same format)
            pool_length_m: Pool length in meters (default 25).
            description: Optional workout-level description.

        Example (warmup, 8x100 free on 1:30 with pull buoy skipping last rest, cooldown):
            steps = [
                {"kind": "warmup", "distance_m": 300, "stroke": "freestyle"},
                {"kind": "repeat", "iterations": 8, "skip_last_rest": True, "steps": [
                    {"kind": "interval", "distance_m": 100, "stroke": "freestyle",
                     "equipment": "swim_pull_buoy"},
                    {"kind": "rest", "send_off_seconds": 90},
                ]},
                {"kind": "cooldown", "distance_m": 200, "stroke": "freestyle"},
            ]
        """
        try:
            workout_json = build_swim_json(
                name=name,
                steps=steps,
                pool_length_m=pool_length_m,
                description=description,
            )
            result = garmin_client.upload_workout(workout_json)

            if isinstance(result, dict):
                curated = {
                    "status": "success",
                    "workout_id": result.get("workoutId"),
                    "name": result.get("workoutName"),
                    "message": "Swim workout uploaded successfully",
                }
                curated = {k: v for k, v in curated.items() if v is not None}
                return json.dumps(curated, indent=2)
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Error creating swim workout: {str(e)}"

    @app.tool()
    async def schedule_week(week: List[Dict[str, Any]]) -> str:
        """Schedule a list of workouts for the week in a single call.

        Idempotent: if a workout is already scheduled for that date, it is
        reported as already scheduled and the POST is skipped (avoids
        duplicating calendar entries).

        Args:
            week: List of dicts with keys: date (YYYY-MM-DD), workout_id (int)
        """
        # Imported here (not at module top) to avoid any import-time ordering
        # surprises between sibling modules. Both modules share the same
        # garmin_client instance via configure() in __main__.
        from garmin_mcp.workouts import _is_already_scheduled

        try:
            results = []
            for item in week:
                calendar_date = item["date"]
                workout_id = int(item["workout_id"])

                if _is_already_scheduled(workout_id, calendar_date):
                    results.append({
                        "date": calendar_date,
                        "workout_id": workout_id,
                        "status": "already_scheduled",
                        "idempotent": True,
                    })
                    continue

                # garminconnect 0.3.2 dropped the .garth attribute; use .client.
                url = f"workout-service/schedule/{workout_id}"
                response = garmin_client.client.post(
                    "connectapi", url, json={"date": calendar_date}
                )
                if response.status_code == 200:
                    results.append({
                        "date": calendar_date,
                        "workout_id": workout_id,
                        "status": "scheduled",
                    })
                else:
                    results.append({
                        "date": calendar_date,
                        "workout_id": workout_id,
                        "status": "failed",
                        "http_status": response.status_code,
                    })
            return json.dumps({
                "status": "complete",
                "scheduled": results,
            }, indent=2)
        except Exception as e:
            return f"Error scheduling week: {str(e)}"

    return app

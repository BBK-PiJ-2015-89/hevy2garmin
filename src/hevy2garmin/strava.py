"""Optional Strava visual strength upload.

When enabled, this module uploads a second, structured Strava strength activity
using a separate FIT strength activity. The Garmin sync remains the source of
truth for Garmin Connect; this is only for Strava's exercise/set UI.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from hevy2garmin.fit import _parse_timestamp, generate_fit
from hevy2garmin.mapper import fit_exercise_strings, lookup_exercise

logger = logging.getLogger("hevy2garmin")

_BASE_URL = "https://www.strava.com"
_API_BASE_URL = "https://www.strava.com/api/v3"
_STATE_PREFIX = "strava_visual_upload_"
_TRUTHY = {"1", "true", "yes", "on"}
_DESCRIPTION_FOOTER = (
    "Bespoke sync by Graeme's Hevy2Garmin build: Hevy workout detail mixed "
    "with Garmin HR, polished for Strava."
)
_DEFAULT_DUPLICATE_START_OFFSET_SECONDS = 300

_STRAVA_EXERCISE_ALIASES = {
    "LUNGE": "LUNGE_GENERIC",
    "DUMBBELL_LUNGE": "LUNGE_GENERIC",
    "WEIGHTED_LUNGE": "LUNGE_GENERIC",
    "WALKING_DUMBBELL_LUNGE": "DUMBBELL_WALKING_LUNGES",
    "WEIGHTED_WALKING_LUNGE": "WALKING_LUNGE",
    "DUMBBELL_BULGARIAN_SPLIT_SQUAT": "DUMBBELL_BULGARIAN_SPLIT_SQUATS",
}

_STRAVA_GENERIC_EXERCISE_TYPES_BY_CATEGORY = {
    0: "BENCH_PRESS_GENERIC",
    1: "CALF_RAISE_GENERIC",
    2: "CARDIO_GENERIC",
    3: "CARRY_GENERIC",
    4: "CHOP_GENERIC",
    5: "CORE_GENERIC",
    6: "CORE_GENERIC",
    7: "CURL_GENERIC",
    8: "DEADLIFT_GENERIC",
    9: "FLYE_GENERIC",
    10: "HIP_RAISE_GENERIC",
    11: "HIP_STABILITY_GENERIC",
    12: "HIP_SWING_GENERIC",
    13: "HYPEREXTENSION_GENERIC",
    14: "LATERAL_RAISE_GENERIC",
    15: "LEG_CURL_GENERIC",
    16: "LEG_RAISE_GENERIC",
    17: "LUNGE_GENERIC",
    18: "OLYMPIC_LIFT_GENERIC",
    19: "PLANK_GENERIC",
    20: "PLYO_GENERIC",
    21: "PULL_UP_GENERIC",
    22: "PUSH_UP_GENERIC",
    23: "ROW_GENERIC",
    24: "SHOULDER_PRESS_GENERIC",
    25: "SHOULDER_STABILITY_GENERIC",
    26: "SHRUG_GENERIC",
    27: "SIT_UP_GENERIC",
    28: "SQUAT_GENERIC",
    29: "TOTAL_BODY_GENERIC",
    30: "TRICEPS_EXTENSION_GENERIC",
    31: "WARM_UP_GENERIC",
}

_STRAVA_TITLE_EXERCISE_TYPES = {
    "bulgariansplitsquatdumbbell": "DUMBBELL_BULGARIAN_SPLIT_SQUATS",
    "curtsylungedumbbell": "CURTSY_LUNGE",
    "laterallunge": "LATERAL_LUNGE",
    "lunge": "LUNGE_GENERIC",
    "lungedumbbell": "LUNGE_GENERIC",
    "weightedlunge": "LUNGE_GENERIC",
    "reverselunge": "REVERSE_LUNGE",
    "reverselungebarbell": "BARBELL_REVERSE_LUNGE",
    "reverselungedumbbell": "DUMBBELL_REVERSE_LUNGE",
    "walkinglunge": "WALKING_LUNGE",
    "walkinglungedumbbell": "DUMBBELL_WALKING_LUNGES",
    "walkinglungesandbag": "SANDBAG_LUNGE",
}


@dataclass
class StravaCredentials:
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass
class StravaUploadResult:
    status: str
    activity_id: int | None = None
    upload_id: str | None = None
    error: str | None = None


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _strava_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("strava", {})
    return raw if isinstance(raw, dict) else {}


def visual_strength_upload_enabled(config: dict[str, Any] | None = None) -> bool:
    """Return whether the optional Strava visual strength upload is enabled."""
    env = os.environ.get("STRAVA_VISUAL_STRENGTH_UPLOAD")
    if env is not None:
        return _truthy(env)
    return bool(_strava_config(config).get("visual_strength_upload"))


def _credentials_from_config(config: dict[str, Any] | None) -> StravaCredentials | None:
    cfg = _strava_config(config)
    client_id = str(cfg.get("client_id") or os.environ.get("STRAVA_CLIENT_ID", "")).strip()
    client_secret = str(
        cfg.get("client_secret") or os.environ.get("STRAVA_CLIENT_SECRET", "")
    ).strip()
    refresh_token = str(
        cfg.get("refresh_token") or os.environ.get("STRAVA_REFRESH_TOKEN", "")
    ).strip()
    if not client_id or not client_secret or not refresh_token:
        return None
    return StravaCredentials(client_id, client_secret, refresh_token)


def _save_refresh_token(creds: StravaCredentials, token_payload: dict[str, Any]) -> None:
    """Persist a rotated Strava refresh token when DB-backed credentials exist."""
    new_refresh = str(token_payload.get("refresh_token") or "").strip()
    if not new_refresh or new_refresh == creds.refresh_token:
        return

    creds.refresh_token = new_refresh
    if os.environ.get("STRAVA_REFRESH_TOKEN"):
        logger.info(
            "Strava visual upload: refresh token rotated; update STRAVA_REFRESH_TOKEN"
        )
        return

    try:
        from hevy2garmin import db

        if not db.get_database_url():
            return
        store = db.get_db()
        if not hasattr(store, "_get_conn"):
            return
        with store._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT credentials FROM platform_credentials WHERE platform = 'strava'"
                )
                row = cur.fetchone()
                existing = {}
                if row:
                    existing = (
                        row["credentials"]
                        if isinstance(row["credentials"], dict)
                        else json.loads(row["credentials"])
                    )
                existing.update(
                    {
                        "client_id": creds.client_id,
                        "client_secret": creds.client_secret,
                        "refresh_token": new_refresh,
                    }
                )
                cur.execute(
                    """
                    INSERT INTO platform_credentials (platform, auth_type, credentials, status, connected_at)
                    VALUES ('strava', 'oauth', %s, 'active', NOW())
                    ON CONFLICT (platform) DO UPDATE
                       SET credentials = EXCLUDED.credentials,
                           status = 'active'
                    """,
                    (json.dumps(existing),),
                )
            conn.commit()
    except Exception:
        logger.debug("Could not persist rotated Strava refresh token", exc_info=True)


def refresh_access_token(
    creds: StravaCredentials,
    *,
    session: Any = requests,
    base_url: str | None = None,
) -> str:
    """Refresh and return a Strava access token."""
    resp = session.post(
        f"{(base_url or os.environ.get('STRAVA_BASE_URL') or _BASE_URL).rstrip('/')}/oauth/token",
        data={
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Strava did not return an access token")
    _save_refresh_token(creds, payload)
    return token


def _format_iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_iso_offset(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def _is_uk_timezone(tz_name: str) -> bool:
    return tz_name.lower() in {"europe/london", "gb", "gb-eire"}


def _last_sunday_at_1am_utc(year: int, month: int) -> datetime:
    day = 31
    while datetime(year, month, day).weekday() != 6:
        day -= 1
    return datetime(year, month, day, 1, tzinfo=timezone.utc)


def _uk_utc_offset_seconds(at: datetime) -> int:
    probe = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
    probe_utc = probe.astimezone(timezone.utc)
    bst_start = _last_sunday_at_1am_utc(probe_utc.year, 3)
    bst_end = _last_sunday_at_1am_utc(probe_utc.year, 10)
    return 3600 if bst_start <= probe_utc < bst_end else 0


def _strava_local_time(dt: datetime, config: dict[str, Any] | None) -> datetime:
    tz_name = str(
        ((config or {}).get("user_profile") or {}).get("timezone") or ""
    ).strip()
    if not tz_name:
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, ValueError):
        if _is_uk_timezone(tz_name):
            return dt.astimezone(timezone(timedelta(seconds=_uk_utc_offset_seconds(dt))))
        return dt


def _utc_offset_seconds(config: dict[str, Any] | None, at: datetime) -> int:
    tz_name = str(
        ((config or {}).get("user_profile") or {}).get("timezone") or ""
    ).strip()
    if not tz_name:
        return 0
    try:
        offset = at.astimezone(ZoneInfo(tz_name)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        if _is_uk_timezone(tz_name):
            return _uk_utc_offset_seconds(at)
        return 0
    return int(offset.total_seconds()) if offset is not None else 0


def _duplicate_start_offset_seconds(config: dict[str, Any] | None) -> int:
    raw = _strava_config(config).get(
        "duplicate_start_offset_seconds",
        _DEFAULT_DUPLICATE_START_OFFSET_SECONDS,
    )
    try:
        return max(0, min(3600, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_DUPLICATE_START_OFFSET_SECONDS


def _workout_duration_seconds(workout: dict[str, Any]) -> int:
    start = _parse_timestamp(workout.get("start_time") or workout.get("startTime"))
    end = _parse_timestamp(workout.get("end_time") or workout.get("endTime"))
    if start is None or end is None:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int(round((end - start).total_seconds())))


def _precise_exercise_types_enabled(config: dict[str, Any] | None) -> bool:
    return _truthy(_strava_config(config).get("precise_exercise_types"))


def _include_optional_upload_details(config: dict[str, Any] | None) -> bool:
    return _truthy(_strava_config(config).get("include_optional_upload_details"))


def _timing_profile(config: dict[str, Any] | None) -> dict[str, int]:
    timing = ((config or {}).get("timing") or {}) if isinstance(config, dict) else {}
    return {
        "working_set_s": int(timing.get("working_set_seconds") or 40),
        "warmup_set_s": int(timing.get("warmup_set_seconds") or 25),
        "rest_sets_s": int(timing.get("rest_between_sets_seconds") or 75),
        "rest_exercises_s": int(timing.get("rest_between_exercises_seconds") or 120),
    }


def _set_timeline(
    workout: dict[str, Any],
    duration_s: float,
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    profile = _timing_profile(config)
    exercises = workout.get("exercises") or []
    all_sets: list[dict[str, Any]] = []
    for ex_idx, ex in enumerate(exercises):
        sets = ex.get("sets") or []
        for set_idx, set_data in enumerate(sets):
            if set_data.get("type") == "rest":
                continue
            explicit_dur = set_data.get("duration_seconds")
            if explicit_dur and float(explicit_dur) > 0:
                set_dur = float(explicit_dur)
            elif set_data.get("type") == "warmup":
                set_dur = float(profile["warmup_set_s"])
            else:
                set_dur = float(profile["working_set_s"])

            is_last_set = set_idx == len(sets) - 1
            is_last_exercise = ex_idx == len(exercises) - 1
            if is_last_set and is_last_exercise:
                rest_dur = 0.0
            elif is_last_set:
                rest_dur = float(profile["rest_exercises_s"])
            else:
                rest_dur = float(profile["rest_sets_s"])

            all_sets.append(
                {
                    "exercise_index": ex_idx,
                    "exercise": ex,
                    "set": set_data,
                    "set_dur": set_dur,
                    "rest_dur": rest_dur,
                }
            )

    ideal_total = sum(item["set_dur"] + item["rest_dur"] for item in all_sets)
    scale = 1.0
    if ideal_total > 0:
        scale = max(0.3, min(2.0, duration_s / ideal_total))

    cursor = 0.0
    for item in all_sets:
        item["start_offset_s"] = cursor
        item["duration_s"] = item["set_dur"] * scale
        cursor += item["duration_s"] + item["rest_dur"] * scale
    return all_sets


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def _exercise_type(exercise: dict[str, Any], config: dict[str, Any] | None) -> str | None:
    title = exercise.get("title") or exercise.get("name") or ""
    title_override = _STRAVA_TITLE_EXERCISE_TYPES.get(_title_key(str(title)))
    if title_override and _precise_exercise_types_enabled(config):
        return title_override
    cat, sub, _ = lookup_exercise(title, exercise.get("exercise_template_id"))
    if not _precise_exercise_types_enabled(config):
        return _STRAVA_GENERIC_EXERCISE_TYPES_BY_CATEGORY.get(cat)
    category_name, exercise_name = fit_exercise_strings(cat, sub)
    if not category_name:
        return None
    return _STRAVA_EXERCISE_ALIASES.get(exercise_name, exercise_name)


def _build_sets(
    workout: dict[str, Any],
    start_dt: datetime,
    duration_s: float,
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    sets: list[dict[str, Any]] = []
    include_timing = _include_optional_upload_details(config)
    for item in _set_timeline(workout, duration_s, config):
        set_data = item["set"]
        exercise_type = _exercise_type(item["exercise"], config)
        if not exercise_type:
            name = item["exercise"].get("title") or item["exercise"].get("name") or "Unknown"
            logger.warning(
                "Strava visual upload: skipping unmapped exercise '%s'", name
            )
            continue

        payload: dict[str, Any] = {"exercise_type": exercise_type}
        if include_timing:
            payload["start_time"] = _format_iso_offset(
                start_dt + timedelta(seconds=float(item["start_offset_s"]))
            )

        reps = set_data.get("reps")
        if reps is not None:
            payload["repetitions"] = int(reps)

        weight = set_data.get("weight_kg")
        if weight is None:
            weight = set_data.get("weight")
        if weight is not None:
            payload["weight"] = max(0.0, float(weight))

        duration = set_data.get("duration_seconds")
        if duration is not None:
            payload["duration"] = int(max(1, round(float(duration))))
        elif "repetitions" not in payload:
            payload["duration"] = int(max(1, round(float(item["duration_s"]))))

        sets.append(payload)
    return sets


def _build_streams(hr_samples: list[Any] | None, duration_s: float) -> dict[str, list[int]]:
    if not hr_samples:
        return {}

    points: dict[int, int] = {}
    if isinstance(hr_samples[0], dict):
        for sample in hr_samples:
            if not isinstance(sample, dict):
                continue
            try:
                offset = int(round(float(sample["time"])))
                bpm = int(sample["hr"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= offset <= duration_s and 0 < bpm < 256:
                points[offset] = bpm
    else:
        values = []
        for sample in hr_samples:
            try:
                bpm = int(sample)
            except (TypeError, ValueError):
                continue
            if 0 < bpm < 256:
                values.append(bpm)
        if len(values) == 1:
            points[0] = values[0]
        elif len(values) > 1:
            for idx, bpm in enumerate(values):
                offset = int(round(duration_s * idx / (len(values) - 1)))
                points[offset] = bpm

    times = sorted(points)
    if not times:
        return {}
    duration_key = int(round(duration_s))
    if times[0] > 0:
        points[0] = points[times[0]]
    if duration_key > 0 and times[-1] < duration_key:
        points[duration_key] = points[times[-1]]
    times = sorted(points)
    return {"time": times, "heartrate": [points[t] for t in times]}


def _fmt_number(value: Any, digits: int = 1) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num.is_integer():
        return str(int(num))
    return f"{num:.{digits}f}".rstrip("0").rstrip(".")


def _fmt_duration(seconds: Any) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes and secs:
        return f"{minutes}m {secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _set_description(index: int, set_data: dict[str, Any]) -> str | None:
    set_type = str(set_data.get("type") or "normal").strip().lower()
    if set_type == "rest":
        return None
    label = "Warm-up" if set_type == "warmup" else f"Set {index}"
    parts: list[str] = []

    weight = set_data.get("weight_kg")
    if weight is None:
        weight = set_data.get("weight")
    reps = set_data.get("reps")
    distance = set_data.get("distance_meters")
    duration = set_data.get("duration_seconds")
    rpe = set_data.get("rpe")

    if weight is not None and reps is not None:
        parts.append(f"{_fmt_number(weight)} kg x {int(reps)}")
    elif reps is not None:
        parts.append(f"{int(reps)} reps")
    elif weight is not None:
        parts.append(f"{_fmt_number(weight)} kg")

    if distance:
        parts.append(f"{_fmt_number(float(distance) / 1000)} km")
    if duration:
        formatted = _fmt_duration(duration)
        if formatted:
            parts.append(formatted)
    if rpe is not None:
        parts.append(f"RPE {_fmt_number(rpe)}")

    if not parts:
        return f"{label}: logged"
    return f"{label}: {' | '.join(parts)}"


def generate_strava_description(
    workout: dict[str, Any],
    *,
    calories: int | None = None,
    avg_hr: int | None = None,
) -> str:
    """Generate a detailed Strava description from the Hevy workout."""
    title = str(workout.get("title") or "Strength Training")
    start = _parse_timestamp(workout.get("start_time") or workout.get("startTime"))
    end = _parse_timestamp(workout.get("end_time") or workout.get("endTime"))
    duration_s = int((end - start).total_seconds()) if start and end else 0

    lines: list[str] = [title]
    summary: list[str] = []
    if duration_s > 0:
        summary.append(_fmt_duration(duration_s))
    if calories:
        summary.append(f"{calories} kcal")
    if avg_hr:
        summary.append(f"avg HR {avg_hr} bpm")
    if summary:
        lines.append(" | ".join(summary))

    for exercise in workout.get("exercises") or []:
        name = exercise.get("title") or exercise.get("name") or "Exercise"
        set_lines: list[str] = []
        normal_index = 1
        for set_data in exercise.get("sets") or []:
            line = _set_description(normal_index, set_data)
            if line is None:
                continue
            set_lines.append(line)
            if str(set_data.get("type") or "normal").lower() != "warmup":
                normal_index += 1
        if set_lines:
            lines.append("")
            lines.append(str(name))
            lines.extend(set_lines)

    lines.append("")
    lines.append(_DESCRIPTION_FOOTER)
    return "\n".join(lines).strip()


def build_strength_payload(
    workout: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    hr_samples: list[Any] | None = None,
    calories: int | None = None,
    start_offset_seconds: int = 0,
) -> dict[str, Any]:
    """Build Strava's structured JSON strength payload from a Hevy workout."""
    start = _parse_timestamp(workout.get("start_time") or workout.get("startTime"))
    end = _parse_timestamp(workout.get("end_time") or workout.get("endTime"))
    if start is None or end is None:
        raise ValueError("workout is missing a valid start_time/end_time")
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    duration_s = max(1, int(round((end - start).total_seconds())))
    start_for_upload = start + timedelta(seconds=max(0, int(start_offset_seconds)))
    strava_start = _strava_local_time(start_for_upload, config)
    sets = _build_sets(workout, strava_start, duration_s, config)
    if not sets:
        raise ValueError("no mapped strength sets to upload to Strava")

    payload: dict[str, Any] = {
        "version": "1.0",
        "start_time": _format_iso_offset(strava_start),
        "utc_offset": _utc_offset_seconds(config, start),
        "elapsed_time": duration_s,
        "sets": sets,
    }
    include_optional = _include_optional_upload_details(config)
    if include_optional:
        payload["active_time"] = duration_s
        payload["creator"] = {"name": "hevy2garmin"}
    if include_optional and calories is not None:
        payload["total_calories"] = int(calories)
    streams = _build_streams(hr_samples, duration_s) if include_optional else {}
    if include_optional and streams:
        payload["streams"] = streams
    return payload


def _state_key(hevy_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(hevy_id))
    return f"{_STATE_PREFIX}{safe}"


def get_visual_upload_state(store: Any, hevy_id: str) -> dict[str, Any] | None:
    """Return stored Strava visual upload state for a Hevy workout."""
    if store is None or not hasattr(store, "get_app_config"):
        return None
    try:
        state = store.get_app_config(_state_key(hevy_id))
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _mark_uploaded(
    store: Any,
    hevy_id: str,
    result: StravaUploadResult,
    external_id: str,
    *,
    replaced_activity_id: int | None = None,
    file_type: str = "fit",
) -> None:
    if store is None or not hasattr(store, "set_app_config") or not result.activity_id:
        return
    try:
        state = {
            "activity_id": result.activity_id,
            "upload_id": result.upload_id,
            "external_id": external_id,
            "structured": True,
            "file_type": file_type,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }
        if replaced_activity_id is not None:
            state["replaced_activity_id"] = replaced_activity_id
        store.set_app_config(_state_key(hevy_id), state)
    except Exception:
        logger.debug("Could not store Strava visual upload state", exc_info=True)


def _api_base() -> str:
    return (os.environ.get("STRAVA_API_BASE_URL") or _API_BASE_URL).rstrip("/")


def _external_id(
    workout: dict[str, Any],
    *,
    unique: bool = False,
    extension: str = "fit",
) -> str:
    wid = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(workout.get("id") or "workout"))
    ext = re.sub(r"[^A-Za-z0-9]+", "", extension.lower()) or "fit"
    if unique:
        return f"hevy2garmin-{wid}-{uuid.uuid4().hex[:12]}.{ext}"
    return f"hevy2garmin-{wid}.{ext}"


def _shift_workout_for_duplicate(
    workout: dict[str, Any],
    *,
    start_offset_seconds: int,
) -> dict[str, Any]:
    shifted = copy.deepcopy(workout)
    offset = timedelta(seconds=max(0, int(start_offset_seconds)))
    for key, alt_key in (("start_time", "startTime"), ("end_time", "endTime")):
        value = _parse_timestamp(workout.get(key) or workout.get(alt_key))
        if value is None:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        shifted[key] = _format_iso_z(value + offset)
    return shifted


def _fit_profile_from_config(config: dict[str, Any] | None) -> dict[str, Any]:
    user_profile = ((config or {}).get("user_profile") or {}) if isinstance(config, dict) else {}
    timing = ((config or {}).get("timing") or {}) if isinstance(config, dict) else {}
    return {
        "weight_kg": user_profile.get("weight_kg", 80.0),
        "birth_year": user_profile.get("birth_year", 1990),
        "vo2max": user_profile.get("vo2max", 45.0),
        "timezone": user_profile.get("timezone", ""),
        "working_set_s": timing.get("working_set_seconds", 40),
        "warmup_set_s": timing.get("warmup_set_seconds", 25),
        "rest_sets_s": timing.get("rest_between_sets_seconds", 75),
        "rest_exercises_s": timing.get("rest_between_exercises_seconds", 120),
    }


def build_strength_fit_file(
    workout: dict[str, Any],
    *,
    output_path: str | Path,
    config: dict[str, Any] | None = None,
    hr_samples: list[Any] | None = None,
    start_offset_seconds: int = 0,
) -> dict[str, Any]:
    """Build the separate Strava FIT strength duplicate."""
    shifted = _shift_workout_for_duplicate(
        workout,
        start_offset_seconds=start_offset_seconds,
    )
    fit_hr_samples = hr_samples if _include_optional_upload_details(config) else None
    return generate_fit(
        shifted,
        hr_samples=fit_hr_samples,
        output_path=str(output_path),
        profile=_fit_profile_from_config(config),
    )


def _upload_fit(
    token: str,
    fit_path: str | Path,
    *,
    name: str,
    description: str,
    external_id: str,
    session: Any = requests,
) -> dict[str, Any]:
    data = {
        "name": name,
        "description": description,
        "trainer": "1",
        "commute": "0",
        "data_type": "fit",
        "sport_type": "WeightTraining",
        "external_id": external_id,
    }
    path = Path(fit_path)
    files = {
        "file": (
            external_id,
            path.read_bytes(),
            "application/octet-stream",
        )
    }
    resp = session.post(
        f"{_api_base()}/uploads",
        headers={"Authorization": f"Bearer {token}"},
        data=data,
        files=files,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _update_activity_metadata(
    token: str,
    activity_id: int | str,
    *,
    name: str,
    description: str,
    session: Any = requests,
) -> None:
    resp = session.put(
        f"{_api_base()}/activities/{activity_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": name, "description": description},
        timeout=30,
    )
    resp.raise_for_status()


def _delete_activity(
    token: str,
    activity_id: int | str,
    *,
    session: Any = requests,
) -> None:
    resp = session.delete(
        f"{_api_base()}/activities/{activity_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()


def _is_not_found_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 404:
        return True
    text = str(exc).lower()
    return "404" in text and "not found" in text


def _poll_upload(
    token: str,
    upload_id: str,
    *,
    session: Any = requests,
    poll_seconds: int = 20,
) -> StravaUploadResult:
    deadline = time.monotonic() + max(1, poll_seconds)
    last_payload: dict[str, Any] = {}
    while True:
        resp = session.get(
            f"{_api_base()}/uploads/{upload_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        last_payload = resp.json()
        if last_payload.get("error"):
            return StravaUploadResult(
                status="failed",
                upload_id=str(upload_id),
                error=str(last_payload.get("error")),
            )
        activity_id = last_payload.get("activity_id")
        if activity_id:
            return StravaUploadResult(
                status="uploaded",
                upload_id=str(upload_id),
                activity_id=int(activity_id),
            )
        if time.monotonic() >= deadline:
            return StravaUploadResult(
                status="processing",
                upload_id=str(upload_id),
                error=str(last_payload.get("status") or "still processing"),
            )
        time.sleep(1)


def try_upload_visual_strength(
    workout: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    store: Any = None,
    hr_samples: list[Any] | None = None,
    calories: int | None = None,
    avg_hr: int | None = None,
    update_existing: bool = False,
    replace_existing: bool = False,
    force_new_external_id: bool = False,
    session: Any = requests,
) -> StravaUploadResult:
    """Best-effort upload of a visual Strava strength activity.

    Never raises into the Garmin sync path. Returns a small status object for
    tests and logs.
    """
    if not visual_strength_upload_enabled(config):
        return StravaUploadResult(status="skipped")

    hevy_id = str(workout.get("id") or "")
    existing_state = get_visual_upload_state(store, hevy_id) if hevy_id else None
    structured_state = (
        existing_state
        if existing_state
        and existing_state.get("activity_id")
        and existing_state.get("structured") is True
        else None
    )
    if (
        structured_state
        and not update_existing
        and not replace_existing
    ):
        logger.info("Strava visual upload: already uploaded for %s", hevy_id)
        return StravaUploadResult(status="skipped")

    creds = _credentials_from_config(config)
    if creds is None:
        logger.warning(
            "Strava visual upload enabled, but Strava credentials are not configured"
        )
        return StravaUploadResult(status="failed", error="missing credentials")

    title = str(workout.get("title") or "Strength Training")
    description = generate_strava_description(
        workout,
        calories=calories,
        avg_hr=avg_hr,
    )
    token = ""
    replaced_activity_id = None

    try:
        token = refresh_access_token(creds, session=session)
        existing_file_type = str((structured_state or {}).get("file_type") or "").lower()
        if structured_state and replace_existing:
            replaced_activity_id = int(structured_state["activity_id"])
        elif structured_state and update_existing and existing_file_type == "fit":
            activity_id = int(structured_state["activity_id"])
            try:
                _update_activity_metadata(
                    token,
                    activity_id,
                    name=title,
                    description=description,
                    session=session,
                )
                logger.info("Strava visual upload: updated activity %s", activity_id)
                return StravaUploadResult(status="updated", activity_id=activity_id)
            except Exception as exc:
                if not _is_not_found_error(exc):
                    raise
                logger.info(
                    "Strava visual upload: stored activity %s no longer exists; creating a fresh structured copy",
                    activity_id,
                )
                force_new_external_id = True
        elif structured_state and update_existing:
            replaced_activity_id = int(structured_state["activity_id"])
            force_new_external_id = True
            logger.info(
                "Strava visual upload: replacing older structured activity %s with a FIT copy",
                replaced_activity_id,
            )

        start_offset_seconds = (
            _workout_duration_seconds(workout)
            + _duplicate_start_offset_seconds(config)
        )
        external_id = _external_id(
            workout,
            unique=force_new_external_id or replace_existing,
            extension="fit",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            fit_path = Path(tmp_dir) / external_id
            build_strength_fit_file(
                workout,
                output_path=fit_path,
                config=config,
                hr_samples=hr_samples,
                start_offset_seconds=start_offset_seconds,
            )
            upload = _upload_fit(
                token,
                fit_path,
                name=title,
                description=description,
                external_id=external_id,
                session=session,
            )
        upload_id = upload.get("id_str") or upload.get("id")
        if not upload_id:
            raise RuntimeError("Strava did not return an upload id")
        result = _poll_upload(token, str(upload_id), session=session)
        if result.status == "uploaded":
            delete_error = None
            if replaced_activity_id is not None:
                try:
                    _delete_activity(token, replaced_activity_id, session=session)
                    logger.info(
                        "Strava visual upload: deleted old activity %s",
                        replaced_activity_id,
                    )
                except Exception as exc:
                    delete_error = str(exc)
                    logger.warning(
                        "Strava visual upload: new activity created but old "
                        "activity %s could not be deleted: %s",
                        replaced_activity_id,
                        exc,
                    )
            _mark_uploaded(
                store,
                hevy_id,
                result,
                external_id,
                replaced_activity_id=replaced_activity_id,
            )
            if replaced_activity_id is not None:
                if delete_error is None:
                    result.status = "replaced"
                else:
                    result.error = (
                        "created new Strava copy, but could not delete old one: "
                        f"{delete_error}"
                    )
            logger.info(
                "Strava visual upload: created activity %s", result.activity_id
            )
        elif result.status == "processing":
            logger.warning(
                "Strava visual upload: upload %s is still processing", upload_id
            )
        else:
            logger.warning("Strava visual upload failed: %s", result.error)
            try:
                if replaced_activity_id is not None:
                    _update_activity_metadata(
                        token,
                        replaced_activity_id,
                        name=title,
                        description=description,
                        session=session,
                    )
                    result.status = "metadata_updated"
                    result.activity_id = replaced_activity_id
                    result.error = (
                        "Structured Strava re-upload failed, so the existing "
                        "structured Strava copy title and description were refreshed instead."
                    )
                    logger.info(
                        "Strava visual upload fallback: updated activity %s metadata",
                        replaced_activity_id,
                    )
            except Exception as exc:
                logger.warning("Strava visual upload fallback failed: %s", exc)
        return result
    except Exception as exc:
        logger.warning("Strava visual upload failed: %s", exc)
        if token:
            try:
                if replaced_activity_id is not None:
                    _update_activity_metadata(
                        token,
                        replaced_activity_id,
                        name=title,
                        description=description,
                        session=session,
                    )
                    logger.info(
                        "Strava visual upload fallback: updated activity %s metadata after upload exception",
                        replaced_activity_id,
                    )
                    return StravaUploadResult(
                        status="metadata_updated",
                        activity_id=replaced_activity_id,
                        error=(
                            "Structured Strava re-upload failed, so the existing "
                            "structured Strava copy title and description were refreshed instead."
                        ),
                    )
            except Exception as fallback_exc:
                logger.warning("Strava visual upload fallback failed: %s", fallback_exc)
        return StravaUploadResult(status="failed", error=str(exc))

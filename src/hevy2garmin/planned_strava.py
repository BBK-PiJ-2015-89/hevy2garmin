"""Update Strava activities from Garmin planned-workout metadata."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from hevy2garmin import db
from hevy2garmin._isotime import parse_iso
from hevy2garmin.config import load_config
from hevy2garmin.garmin import get_client

logger = logging.getLogger("hevy2garmin")

STRAVA_API = "https://www.strava.com/api/v3"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parse_iso(value)
    except Exception:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_window(date_text: str) -> tuple[int, int]:
    start = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
    return (
        int((start - timedelta(hours=6)).timestamp()),
        int((start + timedelta(days=1, hours=6)).timestamp()),
    )


def _activity_name(activity: dict[str, Any]) -> str:
    return str(activity.get("activityName") or activity.get("name") or "").strip()


def _activity_sport(activity: dict[str, Any]) -> str:
    activity_type = _as_dict(activity.get("activityType"))
    return str(activity_type.get("typeKey") or activity.get("sportType") or "").lower()


def _activity_start(activity: dict[str, Any]) -> str | None:
    return activity.get("startTimeGMT") or activity.get("startTimeLocal")


def _workout_id(activity: dict[str, Any]) -> str:
    return str(activity.get("workoutId") or "").strip()


def _is_run(activity: dict[str, Any]) -> bool:
    sport = _activity_sport(activity)
    return not sport or "running" in sport or sport == "run"


def _matches_plan(activity: dict[str, Any], plan: dict[str, Any]) -> bool:
    if not _is_run(activity):
        return False
    workout_id = _workout_id(activity)
    if workout_id and workout_id == str(plan.get("garminWorkoutId") or ""):
        return True
    name = _activity_name(activity).lower()
    title = str(plan.get("workoutTitle") or "").lower()
    return bool(name and title and (name == title or title in name))


def _format_duration(step: dict[str, Any]) -> str:
    duration_type = step.get("durationType")
    value = step.get("durationValue")
    if duration_type == "distance":
        try:
            metres = float(value)
        except (TypeError, ValueError):
            return ""
        if metres <= 0:
            return ""
        if metres >= 1000:
            km = metres / 1000
            return f"{km:g} km" if metres % 1000 else f"{int(km)} km"
        return f"{int(metres)} m"
    if duration_type == "time":
        try:
            total = int(value)
        except (TypeError, ValueError):
            return ""
        if total <= 0:
            return ""
        mins, secs = divmod(total, 60)
        return f"{mins}:{secs:02d}" if mins else f"{secs}s"
    return "lap button"


def _format_target(step: dict[str, Any]) -> str:
    target_type = step.get("targetType")
    if target_type == "pace":
        return f" @ {step.get('targetLow') or '?'}-{step.get('targetHigh') or '?'}/km"
    if target_type == "heartRate":
        return f" @ {step.get('targetLow') or '?'}-{step.get('targetHigh') or '?'} bpm"
    return ""


def planned_description(plan: dict[str, Any]) -> str:
    title = str(plan.get("workoutTitle") or "Planned workout")
    lines = [title, "", "Structured planned workout"]
    if plan.get("description"):
        lines.extend(["", str(plan["description"])])
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    if steps:
        lines.append("")
        for index, raw_step in enumerate(steps):
            step = _as_dict(raw_step)
            if step.get("kind") == "repeat":
                lines.append(
                    f"Repeat: previous {step.get('previousSteps') or '?'} steps x {step.get('reps') or '?'}"
                )
                continue
            name = step.get("name") or f"Step {index + 1}"
            lines.append(f"{name}: {_format_duration(step)}{_format_target(step)}")
    lines.extend(["", "Synced from Garmin planned workout."])
    return "\n".join(lines)


def _load_plans(store: Any) -> list[dict[str, Any]]:
    value = _as_dict(store.get_app_config("planned_workouts"))
    workouts = _as_dict(value.get("workouts"))
    plans: list[dict[str, Any]] = []
    for raw in workouts.values():
        plan = _as_dict(raw)
        if plan.get("garminWorkoutId") and plan.get("workoutTitle") and plan.get("scheduledDate"):
            plans.append(plan)
    return plans


def _strava_credentials(store: Any) -> dict[str, str] | None:
    config = load_config()
    strava = _as_dict(config.get("strava"))
    creds = {
        "client_id": str(strava.get("client_id") or os.environ.get("STRAVA_CLIENT_ID") or "").strip(),
        "client_secret": str(strava.get("client_secret") or os.environ.get("STRAVA_CLIENT_SECRET") or "").strip(),
        "refresh_token": str(strava.get("refresh_token") or os.environ.get("STRAVA_REFRESH_TOKEN") or "").strip(),
    }
    return creds if all(creds.values()) else None


def _refresh_strava_token(store: Any, creds: dict[str, str]) -> str:
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Strava token refresh failed ({response.status_code})")
    payload = response.json()
    token = str(payload.get("access_token") or "")
    if not token:
        raise RuntimeError("Strava did not return an access token")
    rotated = str(payload.get("refresh_token") or "")
    if rotated and rotated != creds["refresh_token"] and not os.environ.get("STRAVA_REFRESH_TOKEN"):
        try:
            config = load_config()
            strava = _as_dict(config.get("strava"))
            strava["refresh_token"] = rotated
            if hasattr(store, "_get_conn"):
                import json
                with store._get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE platform_credentials
                               SET credentials = credentials || %s::jsonb, status = 'active'
                             WHERE platform = 'strava'
                            """,
                            (json.dumps({"refresh_token": rotated}),),
                        )
                    conn.commit()
        except Exception:
            logger.debug("Could not persist rotated Strava refresh token", exc_info=True)
    return token


def _strava_activities(token: str, date_text: str) -> list[dict[str, Any]]:
    after, before = _date_window(date_text)
    response = requests.get(
        f"{STRAVA_API}/athlete/activities",
        params={"after": after, "before": before, "per_page": 50},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Strava activity list failed ({response.status_code})")
    payload = response.json()
    return payload if isinstance(payload, list) else []


def _matching_strava_activity(
    activities: list[dict[str, Any]], garmin_activity: dict[str, Any]
) -> dict[str, Any] | None:
    garmin_start = _parse_time(_activity_start(garmin_activity))
    if garmin_start is None:
        return None
    matches = []
    for activity in activities:
        sport = str(activity.get("sport_type") or activity.get("type") or "").lower()
        if sport and "run" not in sport:
            continue
        strava_start = _parse_time(activity.get("start_date") or activity.get("start_date_local"))
        if strava_start and abs((strava_start - garmin_start).total_seconds()) <= 10 * 60:
            matches.append(activity)
    return matches[0] if len(matches) == 1 else None


def _update_strava_activity(token: str, activity_id: int, plan: dict[str, Any]) -> None:
    response = requests.put(
        f"{STRAVA_API}/activities/{activity_id}",
        json={"name": plan["workoutTitle"], "description": planned_description(plan)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Strava activity update failed ({response.status_code})")


def sync_planned_strava() -> dict[str, Any]:
    result: dict[str, Any] = {"checked": 0, "updated": 0, "skipped": 0, "errors": []}
    store = db.get_db()
    plans = _load_plans(store)
    if not plans:
        return result

    updated = _as_dict(store.get_app_config("planned_strava_updates"))
    creds = _strava_credentials(store)
    if not creds:
        result["skipped"] = len(plans)
        result["errors"].append("Strava credentials not configured")
        return result

    token = _refresh_strava_token(store, creds)
    config = load_config()
    garmin_client = get_client(
        config.get("garmin_email"),
        config.get("garmin_password", ""),
        config.get("garmin_token_dir", "~/.garminconnect"),
    )

    for plan in plans:
        workout_id = str(plan.get("garminWorkoutId") or "")
        date_text = str(plan.get("scheduledDate") or "")
        if not date_text or updated.get(workout_id):
            result["skipped"] += 1
            continue
        result["checked"] += 1
        try:
            activities = garmin_client.get_activities_by_date(date_text, date_text) or []
            garmin_matches = [activity for activity in activities if _matches_plan(activity, plan)]
            if len(garmin_matches) != 1:
                result["skipped"] += 1
                continue
            strava = _matching_strava_activity(_strava_activities(token, date_text), garmin_matches[0])
            if not strava:
                result["skipped"] += 1
                continue
            _update_strava_activity(token, int(strava["id"]), plan)
            updated[workout_id] = {
                "stravaActivityId": strava["id"],
                "garminActivityId": garmin_matches[0].get("activityId"),
                "workoutTitle": plan["workoutTitle"],
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
            store.set_app_config("planned_strava_updates", updated)
            result["updated"] += 1
        except Exception as exc:
            result["errors"].append(f"{plan.get('workoutTitle', 'Planned workout')}: {exc}")
    return result

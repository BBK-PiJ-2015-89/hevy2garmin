"""Tests for optional Strava visual strength uploads."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from hevy2garmin.strava import (
    build_strength_payload,
    generate_strava_description,
    try_upload_visual_strength,
    visual_strength_upload_enabled,
)


class _Resp:
    def __init__(self, payload: dict, status_error: Exception | None = None) -> None:
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    def json(self) -> dict:
        return self._payload


class _Store:
    def __init__(self) -> None:
        self.values: dict[str, dict] = {}

    def get_app_config(self, key: str) -> dict | None:
        return self.values.get(key)

    def set_app_config(self, key: str, value: dict) -> None:
        self.values[key] = value


def _config(enabled: bool = True) -> dict:
    return {
        "strava": {
            "visual_strength_upload": enabled,
            "client_id": "cid",
            "client_secret": "secret",
            "refresh_token": "refresh",
        },
        "user_profile": {"timezone": "Europe/London"},
        "timing": {
            "working_set_seconds": 40,
            "warmup_set_seconds": 25,
            "rest_between_sets_seconds": 75,
            "rest_between_exercises_seconds": 120,
        },
    }


def test_enabled_can_come_from_config_or_env(monkeypatch):
    monkeypatch.delenv("STRAVA_VISUAL_STRENGTH_UPLOAD", raising=False)
    assert visual_strength_upload_enabled({"strava": {"visual_strength_upload": True}})
    monkeypatch.setenv("STRAVA_VISUAL_STRENGTH_UPLOAD", "false")
    assert not visual_strength_upload_enabled({"strava": {"visual_strength_upload": True}})
    monkeypatch.setenv("STRAVA_VISUAL_STRENGTH_UPLOAD", "yes")
    assert visual_strength_upload_enabled({})


def test_build_strength_payload_contains_sets_and_hr(sample_workout: dict) -> None:
    payload = build_strength_payload(
        sample_workout,
        config=_config(),
        hr_samples=[{"time": 0, "hr": 80}, {"time": 60.2, "hr": 112}],
        calories=321,
    )

    assert payload["version"] == "1.0"
    assert payload["start_time"] == "2026-04-01T20:00:00Z"
    assert payload["elapsed_time"] == 2700
    assert payload["total_calories"] == 321
    assert payload["streams"] == {"time": [0, 60, 2700], "heartrate": [80, 112, 112]}
    assert payload["sets"][0] == {
        "exercise_type": "BARBELL_BENCH_PRESS",
        "start_time": "2026-04-01T20:00:00Z",
        "repetitions": 12,
        "weight": 40.0,
    }
    assert payload["sets"][4]["exercise_type"] == "OVERHEAD_DUMBBELL_PRESS"
    assert payload["sets"][4]["repetitions"] == 12


def test_lunge_variants_upload_as_generic_strava_lunge_with_weight() -> None:
    workout = {
        "id": "lunge-day",
        "title": "Legs",
        "start_time": "2026-04-01T20:00:00+00:00",
        "end_time": "2026-04-01T20:20:00+00:00",
        "exercises": [
            {
                "title": "Lunge (Dumbbell)",
                "exercise_template_id": "B537D09F",
                "sets": [{"type": "normal", "weight_kg": 14, "reps": 8}],
            },
            {
                "title": "Weighted Lunge",
                "sets": [{"type": "normal", "weight_kg": 20, "reps": 6}],
            },
        ],
    }

    payload = build_strength_payload(workout, config=_config())

    assert [item["exercise_type"] for item in payload["sets"]] == ["LUNGE", "LUNGE"]
    assert payload["sets"][0]["weight"] == 14.0
    assert payload["sets"][1]["weight"] == 20.0


def test_hr_stream_is_anchored_to_full_duration(sample_workout: dict) -> None:
    payload = build_strength_payload(
        sample_workout,
        config=_config(),
        hr_samples=[{"time": 180, "hr": 92}, {"time": 240, "hr": 100}],
    )

    assert payload["streams"] == {
        "time": [0, 180, 240, 2700],
        "heartrate": [92, 92, 100, 100],
    }


def test_generate_description_lists_workout_details(sample_workout: dict) -> None:
    desc = generate_strava_description(sample_workout, calories=200, avg_hr=90)

    assert "Push" in desc
    assert "45m" in desc
    assert "200 kcal" in desc
    assert "avg HR 90 bpm" in desc
    assert "Bench Press (Barbell)" in desc
    assert "Warm-up: 40 kg x 12" in desc
    assert "Set 1: 60 kg x 10" in desc
    assert "Bespoke sync by Graeme's Hevy2Garmin build" in desc
    assert "delete" not in desc.lower()


def test_upload_refreshes_token_posts_json_and_marks_state(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access", "refresh_token": "rotated"}),
        _Resp({"id": 123, "id_str": "123", "status": "success"}),
    ]
    session.get.return_value = _Resp(
        {"id": 123, "id_str": "123", "error": None, "activity_id": 456}
    )
    store = _Store()

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        hr_samples=[{"time": 0, "hr": 90}],
        calories=200,
        avg_hr=90,
        session=session,
    )

    assert result.status == "uploaded"
    assert result.activity_id == 456
    token_call, upload_call = session.post.call_args_list
    assert token_call.args[0] == "https://strava.test/oauth/token"
    assert upload_call.args[0] == "https://strava.test/api/v3/uploads"
    assert upload_call.kwargs["data"]["sport_type"] == "WeightTraining"
    assert upload_call.kwargs["data"]["data_type"] == "json"
    assert upload_call.kwargs["data"]["external_id"] == "hevy2garmin-test-workout-123.json"
    assert "Bench Press (Barbell)" in upload_call.kwargs["data"]["description"]
    assert "Set 1: 60 kg x 10" in upload_call.kwargs["data"]["description"]
    assert "avg HR 90 bpm" in upload_call.kwargs["data"]["description"]
    assert "delete" not in upload_call.kwargs["data"]["description"].lower()
    uploaded_json = json.loads(upload_call.kwargs["files"]["file"][1].decode("utf-8"))
    assert uploaded_json["sets"][0]["exercise_type"] == "BARBELL_BENCH_PRESS"
    assert uploaded_json["streams"] == {"time": [0, 2700], "heartrate": [90, 90]}
    assert store.values["strava_visual_upload_test-workout-123"]["activity_id"] == 456


def test_missing_credentials_is_non_fatal(sample_workout: dict) -> None:
    session = MagicMock()
    result = try_upload_visual_strength(
        sample_workout,
        config={"strava": {"visual_strength_upload": True}},
        session=session,
    )
    assert result.status == "failed"
    assert "missing credentials" in result.error
    session.post.assert_not_called()


def test_already_uploaded_is_skipped(sample_workout: dict) -> None:
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999},
    )
    session = MagicMock()
    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        session=session,
    )
    assert result.status == "skipped"
    session.post.assert_not_called()


def test_update_existing_visual_activity_updates_description(sample_workout: dict) -> None:
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999},
    )
    session = MagicMock()
    session.post.return_value = _Resp({"access_token": "access"})
    session.put.return_value = _Resp({})

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        calories=200,
        avg_hr=90,
        update_existing=True,
        session=session,
    )

    assert result.status == "updated"
    assert result.activity_id == 999
    session.put.assert_called_once()
    assert session.put.call_args.args[0] == "https://www.strava.com/api/v3/activities/999"
    assert "Bench Press (Barbell)" in session.put.call_args.kwargs["json"]["description"]
    assert "delete" not in session.put.call_args.kwargs["json"]["description"].lower()
    session.get.assert_not_called()


def test_replace_existing_visual_activity_deletes_and_reuploads(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    monkeypatch.setattr(
        "hevy2garmin.strava.uuid.uuid4",
        lambda: SimpleNamespace(hex="abcdef1234567890"),
    )
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999, "external_id": "old.json"},
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 124, "id_str": "124", "status": "success"}),
    ]
    session.delete.return_value = _Resp({})
    session.get.return_value = _Resp(
        {"id": 124, "id_str": "124", "error": None, "activity_id": 457}
    )

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        replace_existing=True,
        session=session,
    )

    assert result.status == "replaced"
    assert result.activity_id == 457
    session.delete.assert_called_once()
    assert session.delete.call_args.args[0] == "https://strava.test/api/v3/activities/999"
    _, upload_call = session.post.call_args_list
    assert (
        upload_call.kwargs["data"]["external_id"]
        == "hevy2garmin-test-workout-123-abcdef123456.json"
    )
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["activity_id"] == 457
    assert state["replaced_activity_id"] == 999

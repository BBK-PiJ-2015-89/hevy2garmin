"""Tests for optional Strava visual strength uploads."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from hevy2garmin.strava import (
    build_strength_payload,
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
    assert payload["streams"] == {"time": [0, 60], "heartrate": [80, 112]}
    assert payload["sets"][0] == {
        "exercise_type": "BARBELL_BENCH_PRESS",
        "start_time": "2026-04-01T20:00:00Z",
        "repetitions": 12,
        "weight": 40.0,
    }
    assert payload["sets"][4]["exercise_type"] == "OVERHEAD_DUMBBELL_PRESS"
    assert payload["sets"][4]["repetitions"] == 12


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
    uploaded_json = json.loads(upload_call.kwargs["files"]["file"][1].decode("utf-8"))
    assert uploaded_json["sets"][0]["exercise_type"] == "BARBELL_BENCH_PRESS"
    assert uploaded_json["streams"]["heartrate"] == [90]
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

"""Tests for optional Strava visual strength uploads."""

from __future__ import annotations

from pathlib import Path
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




def _stub_fit_builder(monkeypatch):
    calls = []

    def fake_build_strength_fit_file(workout, *, output_path, config=None, hr_samples=None, start_offset_seconds=0):
        Path(output_path).write_bytes(b"fit")
        calls.append({
            "workout": workout,
            "output_path": Path(output_path),
            "config": config,
            "hr_samples": hr_samples,
            "start_offset_seconds": start_offset_seconds,
        })
        return {"duration_s": 2700, "calories": 123, "avg_hr": None}

    monkeypatch.setattr(
        "hevy2garmin.strava.build_strength_fit_file",
        fake_build_strength_fit_file,
    )
    return calls


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


def test_build_strength_payload_uses_minimal_strava_json(sample_workout: dict) -> None:
    payload = build_strength_payload(
        sample_workout,
        config=_config(),
        hr_samples=[{"time": 0, "hr": 80}, {"time": 60.2, "hr": 112}],
        calories=321,
    )

    assert payload["version"] == "1.0"
    assert payload["start_time"] == "2026-04-01T21:00:00+01:00"
    assert payload["utc_offset"] == 3600
    assert payload["elapsed_time"] == 2700
    assert "active_time" not in payload
    assert "creator" not in payload
    assert "total_calories" not in payload
    assert "streams" not in payload
    assert payload["sets"][0] == {
        "exercise_type": "BENCH_PRESS_GENERIC",
        "repetitions": 12,
        "weight": 40.0,
    }
    assert payload["sets"][4]["exercise_type"] == "SHOULDER_PRESS_GENERIC"
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
            {
                "title": "Reverse Lunge (Dumbbell)",
                "sets": [{"type": "normal", "weight_kg": 16, "reps": 8}],
            },
            {
                "title": "Walking Lunge (Dumbbell)",
                "sets": [{"type": "normal", "weight_kg": 12, "reps": 10}],
            },
        ],
    }

    payload = build_strength_payload(workout, config=_config())

    assert [item["exercise_type"] for item in payload["sets"]] == [
        "LUNGE_GENERIC",
        "LUNGE_GENERIC",
        "LUNGE_GENERIC",
        "LUNGE_GENERIC",
    ]
    assert payload["sets"][0]["weight"] == 14.0
    assert payload["sets"][1]["weight"] == 20.0


def test_recent_hevy_exercises_upload_with_safe_strava_categories() -> None:
    workout = {
        "id": "full-body-1",
        "title": "Full Body 1",
        "start_time": "2026-09-15T13:58:00+00:00",
        "end_time": "2026-09-15T14:30:00+00:00",
        "exercises": [
            {
                "title": "Goblet Squat",
                "sets": [{"type": "normal", "weight_kg": 5, "reps": 8}],
            },
            {
                "title": "Floor Press (Dumbbell)",
                "sets": [{"type": "normal", "weight_kg": 14, "reps": 10}],
            },
            {
                "title": "Dumbbell Row",
                "sets": [{"type": "normal", "weight_kg": 14, "reps": 8}],
            },
            {
                "title": "Bicep Curl (Dumbbell)",
                "sets": [{"type": "normal", "weight_kg": 14, "reps": 20}],
            },
        ],
    }

    payload = build_strength_payload(workout, config=_config())

    assert [item["exercise_type"] for item in payload["sets"]] == [
        "SQUAT_GENERIC",
        "BENCH_PRESS_GENERIC",
        "ROW_GENERIC",
        "CURL_GENERIC",
    ]


def test_optional_hr_stream_is_anchored_to_full_duration(sample_workout: dict) -> None:
    config = _config()
    config["strava"]["include_optional_upload_details"] = True
    payload = build_strength_payload(
        sample_workout,
        config=config,
        hr_samples=[{"time": 180, "hr": 92}, {"time": 240, "hr": 100}],
    )

    assert payload["streams"] == {
        "time": [0, 180, 240, 2700],
        "heartrate": [92, 92, 100, 100],
    }
    assert payload["sets"][0]["start_time"] == "2026-04-01T21:00:00+01:00"


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


def test_upload_refreshes_token_posts_fit_and_marks_state(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    fit_calls = _stub_fit_builder(monkeypatch)
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
    assert upload_call.kwargs["data"]["data_type"] == "fit"
    assert upload_call.kwargs["data"]["external_id"] == "hevy2garmin-test-workout-123.fit"
    assert "Bench Press (Barbell)" in upload_call.kwargs["data"]["description"]
    assert "Set 1: 60 kg x 10" in upload_call.kwargs["data"]["description"]
    assert "avg HR 90 bpm" in upload_call.kwargs["data"]["description"]
    assert "delete" not in upload_call.kwargs["data"]["description"].lower()
    assert upload_call.kwargs["files"]["file"] == (
        "hevy2garmin-test-workout-123.fit",
        b"fit",
        "application/octet-stream",
    )
    assert fit_calls[0]["start_offset_seconds"] == 3000
    assert fit_calls[0]["hr_samples"] == [{"time": 0, "hr": 90}]
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["activity_id"] == 456
    assert state["structured"] is True
    assert state["file_type"] == "fit"


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
        {"activity_id": 999, "structured": True, "file_type": "fit"},
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


def test_text_only_fallback_state_does_not_block_structured_upload(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    fit_calls = _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999},
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 123, "id_str": "123", "status": "success"}),
    ]
    session.get.return_value = _Resp(
        {"id": 123, "id_str": "123", "error": None, "activity_id": 456}
    )

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        session=session,
    )

    assert result.status == "uploaded"
    assert result.activity_id == 456
    assert session.post.call_count == 2
    assert store.values["strava_visual_upload_test-workout-123"]["structured"] is True
    assert store.values["strava_visual_upload_test-workout-123"]["file_type"] == "fit"
    assert fit_calls[0]["start_offset_seconds"] == 3000


def test_update_existing_fit_visual_activity_replaces_fit_copy(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    monkeypatch.setattr(
        "hevy2garmin.strava.uuid.uuid4",
        lambda: SimpleNamespace(hex="abcdef1234567890"),
    )
    fit_calls = _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999, "structured": True, "file_type": "fit"},
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 123, "id_str": "123", "status": "success"}),
    ]
    session.get.return_value = _Resp(
        {"id": 123, "id_str": "123", "error": None, "activity_id": 456}
    )
    session.delete.return_value = _Resp({})

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        hr_samples=[{"time": 0, "hr": 90}],
        calories=200,
        avg_hr=90,
        update_existing=True,
        session=session,
    )

    assert result.status == "replaced"
    assert result.activity_id == 456
    session.put.assert_not_called()
    session.delete.assert_called_once()
    upload_call = session.post.call_args_list[1]
    assert upload_call.kwargs["data"]["external_id"] == "hevy2garmin-test-workout-123-abcdef123456.fit"
    assert fit_calls[0]["hr_samples"] == [{"time": 0, "hr": 90}]
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["file_type"] == "fit"
    assert state["replaced_activity_id"] == 999


def test_update_existing_json_visual_activity_migrates_to_fit(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    monkeypatch.setattr(
        "hevy2garmin.strava.uuid.uuid4",
        lambda: SimpleNamespace(hex="abcdef1234567890"),
    )
    _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999, "external_id": "old.json", "structured": True},
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 123, "id_str": "123", "status": "success"}),
    ]
    session.get.return_value = _Resp(
        {"id": 123, "id_str": "123", "error": None, "activity_id": 456}
    )
    session.delete.return_value = _Resp({})

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        update_existing=True,
        session=session,
    )

    assert result.status == "replaced"
    assert result.activity_id == 456
    session.delete.assert_called_once()
    upload_call = session.post.call_args_list[1]
    assert upload_call.kwargs["data"]["external_id"] == "hevy2garmin-test-workout-123-abcdef123456.fit"
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["file_type"] == "fit"
    assert state["replaced_activity_id"] == 999


def test_deleted_existing_visual_activity_creates_fresh_structured_copy(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    monkeypatch.setattr(
        "hevy2garmin.strava.uuid.uuid4",
        lambda: SimpleNamespace(hex="abcdef1234567890"),
    )
    _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999, "structured": True, "file_type": "fit"},
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 123, "id_str": "123", "status": "success"}),
    ]
    session.delete.return_value = _Resp({})
    session.get.return_value = _Resp(
        {"id": 123, "id_str": "123", "error": None, "activity_id": 456}
    )

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        update_existing=True,
        session=session,
    )

    assert result.status == "replaced"
    assert result.activity_id == 456
    session.put.assert_not_called()
    session.delete.assert_called_once()
    assert session.post.call_count == 2
    upload_call = session.post.call_args_list[1]
    assert (
        upload_call.kwargs["data"]["external_id"]
        == "hevy2garmin-test-workout-123-abcdef123456.fit"
    )
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["activity_id"] == 456
    assert state["structured"] is True
    assert state["file_type"] == "fit"


def test_new_visual_upload_failure_does_not_touch_existing_strava_activity(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    _stub_fit_builder(monkeypatch)
    store = _Store()
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"message": "Error Processing Data"}, RuntimeError("Error Processing Data")),
    ]
    session.delete.return_value = _Resp({})
    session.put.return_value = _Resp({})

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        session=session,
    )

    assert result.status == "failed"
    assert "Error Processing Data" in result.error
    session.get.assert_not_called()
    session.put.assert_not_called()
    assert "strava_visual_upload_test-workout-123" not in store.values


def test_failed_strava_processing_does_not_touch_existing_strava_activity(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    _stub_fit_builder(monkeypatch)
    store = _Store()
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 123, "id_str": "123", "status": "success"}),
    ]
    session.get.side_effect = [
        _Resp({"id": 123, "id_str": "123", "error": "Error Processing Data"}),
    ]
    session.put.return_value = _Resp({})

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        session=session,
    )

    assert result.status == "failed"
    assert result.error == "Error Processing Data"
    session.put.assert_not_called()
    assert "strava_visual_upload_test-workout-123" not in store.values


def test_replace_existing_deletes_old_copy_before_reupload(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999, "external_id": "old.json", "structured": True},
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"message": "Error Processing Data"}, RuntimeError("Error Processing Data")),
    ]
    session.put.return_value = _Resp({})

    result = try_upload_visual_strength(
        sample_workout,
        config=_config(),
        store=store,
        replace_existing=True,
        session=session,
    )

    assert result.status == "failed"
    assert "Error Processing Data" in result.error
    session.delete.assert_called_once()
    assert session.delete.call_args.args[0] == "https://strava.test/api/v3/activities/999"
    session.put.assert_not_called()
    assert session.method_calls[1][0] == "delete"
    assert session.method_calls[2][0] == "post"

def test_replace_existing_continues_when_delete_is_unauthorized(
    sample_workout: dict,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STRAVA_BASE_URL", "https://strava.test")
    monkeypatch.setenv("STRAVA_API_BASE_URL", "https://strava.test/api/v3")
    monkeypatch.setattr(
        "hevy2garmin.strava.uuid.uuid4",
        lambda: SimpleNamespace(hex="abcdef1234567890"),
    )
    fit_calls = _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {
            "activity_id": 999,
            "external_id": "old.fit",
            "structured": True,
            "file_type": "fit",
            "upload_start_offset_seconds": 3000,
        },
    )
    session = MagicMock()
    session.post.side_effect = [
        _Resp({"access_token": "access"}),
        _Resp({"id": 124, "id_str": "124", "status": "success"}),
    ]
    session.delete.return_value = _Resp(
        {},
        RuntimeError("401 Client Error: Unauthorized for url"),
    )
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

    assert result.status == "replaced_old_not_deleted"
    assert result.activity_id == 457
    assert "old copy 999" in result.error
    session.delete.assert_called_once()
    assert session.method_calls[1][0] == "delete"
    assert session.method_calls[2][0] == "post"
    assert fit_calls[0]["start_offset_seconds"] == 6600
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["activity_id"] == 457
    assert state["file_type"] == "fit"
    assert state["upload_start_offset_seconds"] == 6600
    assert state["undeleted_activity_id"] == 999


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
    _stub_fit_builder(monkeypatch)
    store = _Store()
    store.set_app_config(
        "strava_visual_upload_test-workout-123",
        {"activity_id": 999, "external_id": "old.json", "structured": True},
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
        == "hevy2garmin-test-workout-123-abcdef123456.fit"
    )
    state = store.values["strava_visual_upload_test-workout-123"]
    assert state["activity_id"] == 457
    assert state["structured"] is True
    assert state["file_type"] == "fit"
    assert state["replaced_activity_id"] == 999

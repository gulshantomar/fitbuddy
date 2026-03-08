"""Phase 6 testing and evaluation suite for FitBuddy."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import ai
from app import db as db_module
from app import main


class _FakeResponse:
    """Tiny fake Gemini response object."""

    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeModels:
    """Tiny fake Gemini models client."""

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self._text = text
        self._error = error

    def generate_content(self, model: str, contents: str) -> _FakeResponse:
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._text)


class _FakeClient:
    """Tiny fake top-level Gemini client."""

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self.models = _FakeModels(text=text, error=error)


@pytest.fixture
def db_session_factory(tmp_path: Path) -> Iterator[sessionmaker]:
    """Create an isolated SQLite DB and override FastAPI DB dependency."""
    test_db_file = tmp_path / "fitbuddy_test.db"
    engine = create_engine(f"sqlite:///{test_db_file}", connect_args={"check_same_thread": False})
    test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db_module.Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = test_session_local()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = override_get_db
    try:
        yield test_session_local
    finally:
        main.app.dependency_overrides.clear()
        db_module.Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
async def async_client(db_session_factory: sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    """Provide an async HTTP client bound to FastAPI ASGI app."""
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_home_route_returns_form_page(async_client: httpx.AsyncClient) -> None:
    response = await async_client.get("/")
    assert response.status_code == 200
    assert "Generate Your 7-Day Workout Plan" in response.text


@pytest.mark.anyio
async def test_health_route_returns_ok(async_client: httpx.AsyncClient) -> None:
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_generate_route_success_saves_user_record(
    async_client: httpx.AsyncClient,
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "generate_workout_plan", lambda *_: "## Day 1\n- Squats")

    response = await async_client.post(
        "/generate",
        data={
            "name": "Alex",
            "age": "24",
            "weight": "70",
            "goal": "Weight Loss",
            "intensity": "Medium",
        },
    )

    assert response.status_code == 200
    assert "Workout Plan for Alex" in response.text

    with db_session_factory() as db:
        row = db.query(db_module.UserRecord).filter(db_module.UserRecord.name == "Alex").first()
        assert row is not None
        assert row.age == 24
        assert row.weight == 70
        assert row.goal == "Weight Loss"
        assert row.original_plan == "## Day 1\n- Squats"


@pytest.mark.anyio
async def test_submit_feedback_route_updates_existing_plan(
    async_client: httpx.AsyncClient,
    db_session_factory: sessionmaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "generate_workout_plan", lambda *_: "Initial plan")

    await async_client.post(
        "/generate",
        data={
            "name": "Jordan",
            "age": "22",
            "weight": "66",
            "goal": "General Fitness",
            "intensity": "Low",
        },
    )

    with db_session_factory() as db:
        user = db.query(db_module.UserRecord).filter(db_module.UserRecord.name == "Jordan").first()
        assert user is not None
        user_id = user.id

    monkeypatch.setattr(main, "update_workout_plan", lambda *_: "Updated plan with more cardio")

    response = await async_client.post(
        "/submit-feedback",
        data={"user_id": str(user_id), "feedback": "Please add more cardio."},
    )

    assert response.status_code == 200
    assert "Updated plan with more cardio" in response.text

    with db_session_factory() as db:
        refreshed = db.get(db_module.UserRecord, user_id)
        assert refreshed is not None
        assert refreshed.updated_plan == "Updated plan with more cardio"


@pytest.mark.anyio
async def test_admin_route_shows_saved_users(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "generate_workout_plan", lambda *_: "Quick plan")

    await async_client.post(
        "/generate",
        data={
            "name": "Morgan",
            "age": "29",
            "weight": "78",
            "goal": "Muscle Gain",
            "intensity": "High",
        },
    )

    response = await async_client.get("/admin")
    assert response.status_code == 200
    assert "User Records Dashboard" in response.text
    assert "Morgan" in response.text


@pytest.mark.anyio
async def test_generate_route_handles_ai_api_down(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_generation(*_args, **_kwargs):
        raise RuntimeError("Gemini API is currently unavailable")

    monkeypatch.setattr(main, "generate_workout_plan", fail_generation)

    response = await async_client.post(
        "/generate",
        data={
            "name": "Chris",
            "age": "30",
            "weight": "75",
            "goal": "General Fitness",
            "intensity": "Medium",
        },
    )

    assert response.status_code == 503
    assert "Workout generation is temporarily unavailable" in response.text
    assert "Gemini API is currently unavailable" in response.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("overrides", "expected_detail"),
    [
        ({"age": "9"}, "Age must be between 10 and 120."),
        ({"weight": "501"}, "Weight must be between 20 and 500."),
        ({"goal": "Bulk"}, "Invalid goal selected."),
    ],
)
async def test_generate_route_invalid_inputs_return_400(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    expected_detail: str,
) -> None:
    monkeypatch.setattr(main, "generate_workout_plan", lambda *_: "Unused")

    payload = {
        "name": "Taylor",
        "age": "25",
        "weight": "72",
        "goal": "Weight Loss",
        "intensity": "Medium",
    }
    payload.update(overrides)

    response = await async_client.post("/generate", data=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == expected_detail


def test_validate_inputs_accepts_valid_data() -> None:
    main._validate_inputs("Alex", 25, 70.5, "Weight Loss", "Medium")


@pytest.mark.parametrize(
    ("name", "age", "weight", "goal", "intensity", "expected_detail"),
    [
        ("A", 25, 70.0, "Weight Loss", "Medium", "Name must be at least 2 characters."),
        ("Alex", 8, 70.0, "Weight Loss", "Medium", "Age must be between 10 and 120."),
        ("Alex", 25, 600.0, "Weight Loss", "Medium", "Weight must be between 20 and 500."),
        ("Alex", 25, 70.0, "Bulking", "Medium", "Invalid goal selected."),
        ("Alex", 25, 70.0, "Weight Loss", "Extreme", "Invalid intensity selected."),
    ],
)
def test_validate_inputs_rejects_invalid_data(
    name: str,
    age: int,
    weight: float,
    goal: str,
    intensity: str,
    expected_detail: str,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        main._validate_inputs(name, age, weight, goal, intensity)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == expected_detail


def test_ai_content_generation_success_with_mocked_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """GenAI content generation check for successful API behavior."""
    monkeypatch.setattr(ai, "_model_candidates", lambda: ["gemini-test"])
    monkeypatch.setattr(ai, "_get_client", lambda: _FakeClient(text="Mocked 7-day plan"))

    result = ai.generate_workout_plan("Riya", "Weight Loss", "Medium")

    assert result == "Mocked 7-day plan"


def test_ai_api_connection_failure_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """GenAI API connection check for downtime/error behavior."""
    monkeypatch.setattr(ai, "_model_candidates", lambda: ["gemini-test"])
    monkeypatch.setattr(ai, "_get_client", lambda: _FakeClient(error=Exception("connection refused")))

    with pytest.raises(RuntimeError, match="AI generation failed"):
        ai.generate_workout_plan("Riya", "Weight Loss", "Medium")


@pytest.mark.anyio
async def test_generate_route_response_time_under_3_seconds(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Performance test: mocked generation request should be fast."""
    monkeypatch.setattr(main, "generate_workout_plan", lambda *_: "Fast plan")

    start = time.perf_counter()
    response = await async_client.post(
        "/generate",
        data={
            "name": "Sam",
            "age": "27",
            "weight": "74",
            "goal": "General Fitness",
            "intensity": "Medium",
        },
    )
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed < 3.0, f"Response took {elapsed:.3f}s, expected < 3.0s"

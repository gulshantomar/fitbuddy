"""Beginner-friendly FastAPI app with form routes."""

from datetime import datetime

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

load_dotenv()

from app.ai import generate_workout_plan, quick_nutrition_tip, render_markdown_safe, update_workout_plan
from app.db import UserRecord, get_db, init_db

app = FastAPI(title="FitBuddy")
templates = Jinja2Templates(directory="templates")

app.mount("/static", StaticFiles(directory="static"), name="static")

ALLOWED_GOALS = {"Muscle Gain", "Weight Loss", "General Fitness"}
ALLOWED_INTENSITY = {"Low", "Medium", "High"}


@app.on_event("startup")
def startup() -> None:
    """Initialize database tables at startup."""
    init_db()


def _validate_inputs(name: str, age: int, weight: float, goal: str, intensity: str) -> None:
    """Keep validation simple and readable for beginners."""
    if len(name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Name must be at least 2 characters.")
    if age < 10 or age > 120:
        raise HTTPException(status_code=400, detail="Age must be between 10 and 120.")
    if weight < 20 or weight > 500:
        raise HTTPException(status_code=400, detail="Weight must be between 20 and 500.")
    if goal not in ALLOWED_GOALS:
        raise HTTPException(status_code=400, detail="Invalid goal selected.")
    if intensity not in ALLOWED_INTENSITY:
        raise HTTPException(status_code=400, detail="Invalid intensity selected.")


@app.get("/")
def home(request: Request):
    """Show the form page."""
    return templates.TemplateResponse(request, "index.html", {"request": request})


@app.post("/generate")
def generate(
    request: Request,
    name: str = Form(...),
    age: int = Form(...),
    weight: float = Form(...),
    goal: str = Form(...),
    intensity: str = Form(...),
    db: Session = Depends(get_db),
):
    """Generate and save a workout plan."""
    name = name.strip()
    _validate_inputs(name, age, weight, goal, intensity)

    try:
        plan = generate_workout_plan(name, goal, intensity)
    except RuntimeError as exc:
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "request": request,
                "name": name,
                "plan": "Workout generation is temporarily unavailable.",
                "plan_html": render_markdown_safe("Workout generation is temporarily unavailable."),
                "tip": "Please try again in a moment.",
                "user_id": None,
                "generated_at": datetime.utcnow(),
                "error_message": str(exc),
            },
            status_code=503,
        )

    user = UserRecord(
        name=name,
        age=age,
        weight=weight,
        goal=goal,
        intensity=intensity,
        original_plan=plan,
    )

    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error while saving your plan.") from exc

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "request": request,
            "name": name,
            "plan": plan,
            "plan_html": render_markdown_safe(plan),
            "tip": quick_nutrition_tip(goal, intensity),
            "user_id": user.id,
            "generated_at": user.generated_at,
        },
    )


@app.post("/submit-feedback")
def submit_feedback(
    request: Request,
    user_id: int = Form(...),
    feedback: str = Form(...),
    db: Session = Depends(get_db),
):
    """Update a saved plan from user feedback."""
    feedback = feedback.strip()
    if len(feedback) < 5:
        raise HTTPException(status_code=400, detail="Feedback must be at least 5 characters.")

    user = db.get(UserRecord, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User record not found.")

    try:
        updated_plan = update_workout_plan(user.original_plan, feedback)
    except RuntimeError as exc:
        return templates.TemplateResponse(
            request,
            "result.html",
            {
                "request": request,
                "name": user.name,
                "plan": user.updated_plan or user.original_plan,
                "plan_html": render_markdown_safe(user.updated_plan or user.original_plan),
                "tip": "Please try again in a moment.",
                "user_id": user.id,
                "generated_at": user.generated_at,
                "error_message": str(exc),
            },
            status_code=503,
        )

    try:
        user.updated_plan = updated_plan
        db.commit()
        db.refresh(user)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error while updating your plan.") from exc

    return templates.TemplateResponse(
        request,
        "result.html",
        {
            "request": request,
            "name": user.name,
            "plan": user.updated_plan,
            "plan_html": render_markdown_safe(user.updated_plan or ""),
            "tip": quick_nutrition_tip(user.goal, user.intensity),
            "user_id": user.id,
            "generated_at": user.generated_at,
        },
    )


@app.get("/admin")
def admin_page(request: Request, db: Session = Depends(get_db)):
    """Show all saved users and plans."""
    users = db.query(UserRecord).order_by(UserRecord.generated_at.desc()).all()
    return templates.TemplateResponse(request, "all_users.html", {"request": request, "users": users})


@app.get("/health")
def health_check() -> JSONResponse:
    """Simple health endpoint."""
    return JSONResponse(content={"status": "ok"})
"""Simple Gemini and markdown helper functions."""

import os

import bleach
from google import genai
from markdown import markdown

DEFAULT_MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-2.0-flash",
]
DEFAULT_TIMEOUT_MS = 30_000


def _model_candidates() -> list[str]:
    """Read model names from env and return a safe default when missing."""
    raw = os.getenv("GEMINI_MODEL_CANDIDATES", "")
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    return parsed or DEFAULT_MODEL_CANDIDATES


def _request_timeout_ms() -> int:
    """Read timeout from env and keep a sane lower bound."""
    raw = os.getenv("GEMINI_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)).strip()
    try:
        timeout_ms = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_MS
    return timeout_ms if timeout_ms >= 1000 else DEFAULT_TIMEOUT_MS


def _should_try_next_model(error_text: str) -> bool:
    """Continue on model-specific or transient failures and try fallback candidates."""
    retryable_markers = (
        "not found",
        "404",
        "resource_exhausted",
        "429",
        "quota",
        "deadline",
        "timed out",
        "timeout",
        "unavailable",
        "503",
    )
    return any(marker in error_text for marker in retryable_markers)


def _friendly_runtime_error(exc: Exception) -> RuntimeError:
    """Map common provider errors to clear user-facing messages."""
    message = str(exc).lower()
    if "api key" in message or "permission" in message or "unauthorized" in message:
        return RuntimeError("Gemini API key is invalid or missing required permissions.")
    if "quota" in message or "resource_exhausted" in message or "429" in message:
        return RuntimeError("Gemini quota is exhausted. Please check billing or try again later.")
    return RuntimeError("AI generation failed. Please try again.")


def _get_client() -> genai.Client:
    """Create Gemini client from API key."""
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is missing. Add it to your .env file.")
    return genai.Client(api_key=api_key, http_options={"timeout": _request_timeout_ms()})


def _generate_text(prompt: str) -> str:
    """Generate text using the first available model candidate."""
    client = _get_client()
    last_error: Exception | None = None

    for model_name in _model_candidates():
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            text = (response.text or "").strip()
            if text:
                return text
            last_error = ValueError("Gemini returned an empty response.")
        except Exception as exc:  # pragma: no cover
            last_error = exc
            message = str(exc).lower()
            if _should_try_next_model(message):
                continue
            raise _friendly_runtime_error(exc) from exc

    if last_error is None:
        raise RuntimeError("No Gemini model candidates are configured.")
    raise RuntimeError(f"No working Gemini model was found. Last error: {last_error}")


def generate_workout_plan(name: str, goal: str, intensity: str) -> str:
    """Generate a 7-day workout plan."""
    prompt = (
        f"Create a clean 7-day workout plan for {name} with goal '{goal}' and intensity '{intensity}'. "
        "Use Markdown headings and bullet points. For each day include Warm-up, Main Workout, and Cooldown. "
        "Keep it concise and practical for beginners."
    )
    return _generate_text(prompt)


def update_workout_plan(original_plan: str, feedback: str) -> str:
    """Update an existing plan based on user feedback."""
    prompt = (
        f"Original plan:\n{original_plan}\n\n"
        f"User feedback:\n{feedback}\n\n"
        "Rewrite the plan while keeping the same day-by-day structure."
    )
    return _generate_text(prompt)


def quick_nutrition_tip(goal: str, intensity: str) -> str:
    """Return a fast local nutrition tip without extra AI calls."""
    goal_tips = {
        "Muscle Gain": "Eat protein in every meal and add a post-workout protein plus carb snack.",
        "Weight Loss": "Focus on high-protein meals and fiber-rich foods to stay full longer.",
        "General Fitness": "Build balanced meals with protein, whole grains, vegetables, and enough water.",
    }
    intensity_tips = {
        "High": " Also replace fluids and electrolytes after tough sessions.",
        "Medium": " Keep hydration steady through the day.",
        "Low": " Stay consistent with portions and regular meal timing.",
    }
    return f"{goal_tips.get(goal, goal_tips['General Fitness'])}{intensity_tips.get(intensity, '')}".strip()


def render_markdown_safe(content: str) -> str:
    """Render markdown to sanitized HTML for templates."""
    html = markdown(content or "", extensions=["extra", "sane_lists", "nl2br"])
    allowed_tags = [
        "p",
        "br",
        "strong",
        "em",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "blockquote",
        "code",
        "pre",
        "hr",
    ]
    return bleach.clean(html, tags=allowed_tags, attributes={}, strip=True)

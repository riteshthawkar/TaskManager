from __future__ import annotations
import os
import secrets
import logging
from pathlib import Path
from datetime import datetime, date, time, timedelta
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler
from starlette.middleware.sessions import SessionMiddleware
from markupsafe import Markup, escape

from app.database import get_db, ensure_schema_compatibility, SessionLocal, engine
from app.models import Project, Task, Subtask, UserStats, DeepWorkSession, Event
from app.llm import analyze_task, followup_analyze, generate_motivation, suggest_deep_work
from app.notifications import check_and_send_notifications, email_config_issues, current_email_provider
from app.time_utils import (
    APP_TIMEZONE_NAME,
    local_today,
    local_now,
    utc_now_naive,
    utc_naive_to_local,
    local_date_input_to_utc_naive_end_of_day,
    local_date_input_value,
    local_date_from_input,
    shift_utc_naive_by_local_days,
)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("taskmanager")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        logger.warning("Invalid integer for %s=%r. Falling back to %s.", name, value, default)
        return default


APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
APP_USERNAME = os.getenv("APP_USERNAME", "admin").strip() or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET")
SESSION_SECRET_VALUE = SESSION_SECRET or secrets.token_urlsafe(32)
_session_https_override = os.getenv("SESSION_HTTPS_ONLY")
SESSION_HTTPS_ONLY = env_flag("SESSION_HTTPS_ONLY", IS_PRODUCTION) if _session_https_override is not None else IS_PRODUCTION
ENABLE_SCHEDULER = env_flag("ENABLE_SCHEDULER", not IS_PRODUCTION)
NOTIFICATION_CHECK_MINUTES = env_int("NOTIFICATION_CHECK_MINUTES", 10)

# Background scheduler for notifications
scheduler = BackgroundScheduler()
scheduler.add_job(
    check_and_send_notifications,
    "interval",
    minutes=NOTIFICATION_CHECK_MINUTES,
    id="notification_check",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=300,
)


def validate_configuration() -> None:
    global APP_PASSWORD
    if not APP_PASSWORD and not IS_PRODUCTION:
        APP_PASSWORD = "dev-password"

    missing = []
    if IS_PRODUCTION and not SESSION_SECRET:
        missing.append("SESSION_SECRET")
    if IS_PRODUCTION and not APP_PASSWORD:
        missing.append("APP_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    if not IS_PRODUCTION:
        if not SESSION_SECRET:
            logger.warning("SESSION_SECRET is not set; using an ephemeral development secret.")
        if os.getenv("APP_PASSWORD") in (None, ""):
            logger.warning("APP_PASSWORD is not set; using development default password: dev-password")
        if SESSION_HTTPS_ONLY:
            logger.warning("SESSION_HTTPS_ONLY=true in development. HTTP local logins may fail because secure cookies are not sent.")


def ensure_user_stats_row() -> None:
    db = SessionLocal()
    try:
        if not db.query(UserStats).first():
            db.add(UserStats(total_xp=0, current_streak=0, longest_streak=0, tasks_completed=0))
            db.commit()
        logger.info("UserStats ready")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        validate_configuration()
        applied_migrations = ensure_schema_compatibility()
        logger.info("Tables ready")
        if applied_migrations:
            logger.info("Applied schema updates: %s", ", ".join(applied_migrations))
        ensure_user_stats_row()
        logger.info("Email provider: %s", current_email_provider())
        email_issues = email_config_issues()
        if email_issues:
            logger.warning("Email reminders are not fully configured: %s", ", ".join(email_issues))
        if ENABLE_SCHEDULER:
            scheduler.start()
            logger.info("Scheduler started (notifications every %s minutes)", NOTIFICATION_CHECK_MINUTES)
        else:
            logger.info("Scheduler disabled; run notifications from a dedicated worker or cron service.")
    except Exception:
        logger.exception("Startup failed")
        raise

    yield

    if ENABLE_SCHEDULER and scheduler.running:
        try:
            scheduler.shutdown()
        except Exception:
            logger.exception("Scheduler shutdown failed")


app = FastAPI(title="TaskManager", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_VALUE,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
    session_cookie="taskmanager_session",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def is_safe_redirect_target(target: str | None) -> bool:
    return bool(target) and target.startswith("/") and not target.startswith("//")


def login_redirect_url(request: Request) -> str:
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return f"/login?next={quote(next_path, safe='/?=&')}"


def get_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def csrf_input(request: Request) -> Markup:
    return Markup(f'<input type="hidden" name="_csrf" value="{escape(get_csrf_token(request))}">')


templates.env.globals["csrf_input"] = csrf_input
templates.env.globals["local_dt"] = utc_naive_to_local
templates.env.globals["date_input_value"] = local_date_input_value


def require_authenticated(request: Request) -> None:
    if request.session.get("authenticated"):
        return
    raise HTTPException(
        status_code=303,
        detail="Authentication required",
        headers={"Location": login_redirect_url(request)},
    )


async def validate_csrf(request: Request) -> None:
    if request.url.path != "/login" and not request.session.get("authenticated"):
        raise HTTPException(
            status_code=303,
            detail="Authentication required",
            headers={"Location": login_redirect_url(request)},
        )
    if request.url.path == "/login":
        return

    expected = request.session.get("csrf_token")
    provided = request.headers.get("x-csrf-token")
    if not provided:
        form = await request.form()
        provided = form.get("_csrf")

    if not expected or not provided or not secrets.compare_digest(str(provided).strip(), str(expected).strip()):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "manifest-src 'self';"
    )
    return response


@app.get("/health/live")
async def live_health():
    return {"status": "ok"}


@app.get("/health")
async def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:
        logger.exception("Readiness check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "unavailable"},
        )


@app.head("/")
async def dashboard_head():
    return HTMLResponse(status_code=200, content="")


# ── Helpers ──────────────────────────────────────────────────────

PRIORITY_LABELS = {1: "Low", 2: "Medium-Low", 3: "Medium", 4: "High", 5: "Critical"}
XP_BY_PRIORITY = {1: 10, 2: 20, 3: 30, 4: 40, 5: 50}
TASK_STATUS_LABELS = {
    "pending": "Pending",
    "in_progress": "In Progress",
    "waiting": "Waiting",
    "blocked": "Blocked",
    "completed": "Completed",
}
TASK_EDITABLE_STATUSES = ("pending", "in_progress", "waiting", "blocked")
TASK_QUEUE_STATUSES = {"pending", "in_progress"}
TASK_HOLD_STATUSES = {"waiting", "blocked"}
TASK_REPEAT_LABELS = {
    "none": "Does not repeat",
    "daily": "Daily",
    "weekdays": "Weekdays",
    "weekly": "Weekly",
}
WORKDAY_START = time(8, 0)
WORKDAY_END = time(20, 0)
FOCUS_EVENT_PREFIX = "Focus:"
FOCUS_EVENT_MARKER = "[focus-task:"


def get_stats(db: Session) -> UserStats:
    stats = db.query(UserStats).first()
    if not stats:
        stats = UserStats(total_xp=0, current_streak=0, longest_streak=0, tasks_completed=0)
        db.add(stats)
        db.commit()
        db.refresh(stats)
    return stats


def parse_date_input(value: str) -> datetime | None:
    """Treat date inputs as local end-of-day, then store as UTC-naive."""
    return local_date_input_to_utc_naive_end_of_day(value)


def parse_day_input(value: str) -> date | None:
    return local_date_from_input(value)


def parse_tags_text(value: str | None) -> list[str]:
    if not value:
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in value.split(","):
        tag = raw_tag.strip()
        if not tag:
            continue
        lowered = tag.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        tags.append(tag[:30])
    return tags[:8]


def normalize_tags_text(value: str | None) -> str:
    return ", ".join(parse_tags_text(value))


templates.env.globals["task_tags"] = parse_tags_text
templates.env.globals["task_repeat_labels"] = TASK_REPEAT_LABELS
templates.env.globals["task_status_labels"] = TASK_STATUS_LABELS


def template_context(request: Request, **context) -> dict:
    base = {
        "request": request,
        "alerts": request.session.pop("alerts", []),
        "authenticated": bool(request.session.get("authenticated")),
        "app_username": APP_USERNAME,
        "app_timezone_name": APP_TIMEZONE_NAME,
        "csrf_token": get_csrf_token(request),
        "hide_nav": False,
    }
    base.update(context)
    return base


def push_alert(request: Request, kind: str, message: str) -> None:
    alerts = list(request.session.get("alerts", []))
    alerts.append({"kind": kind, "message": message})
    request.session["alerts"] = alerts[-4:]


def parse_clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def get_projects(db: Session) -> list[Project]:
    return db.query(Project).order_by(Project.name.asc()).all()


def project_lookup(projects: list[Project]) -> dict[int, Project]:
    return {project.id: project for project in projects}


def resolve_project_id(raw_value: str, db: Session) -> int | None:
    if not raw_value:
        return None
    try:
        project_id = int(raw_value)
    except ValueError as exc:
        raise ValueError("Invalid project selection.") from exc
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise ValueError("Selected project does not exist.")
    return project.id


def task_is_ready(task: Task, today: date) -> bool:
    return task.start_on is None or task.start_on <= today


def task_sort_key(task: Task, today: date, now: datetime) -> tuple:
    ready_rank = 0 if task_is_ready(task, today) else 1
    status_rank = {
        "in_progress": 0,
        "pending": 1,
        "waiting": 2,
        "blocked": 3,
        "completed": 4,
    }.get(task.status, 5)
    overdue_rank = 0 if task.deadline and task.deadline < now else 1
    deadline_rank = task.deadline or datetime.max
    start_rank = task.start_on or date.max
    priority_rank = -task.priority
    return (ready_rank, status_rank, overdue_rank, deadline_rank, start_rank, priority_rank, task.created_at)


def build_today_queue(tasks: list[Task], today: date, now: datetime) -> list[Task]:
    ready_tasks = [
        task for task in tasks
        if task.status in TASK_QUEUE_STATUSES and task_is_ready(task, today)
    ]
    return sorted(ready_tasks, key=lambda task: task_sort_key(task, today, now))


def build_attention_queue(tasks: list[Task], today: date, now: datetime) -> list[Task]:
    attention_tasks = [task for task in tasks if task.status in TASK_HOLD_STATUSES]
    return sorted(attention_tasks, key=lambda task: task_sort_key(task, today, now))


def build_later_queue(tasks: list[Task], today: date, now: datetime) -> list[Task]:
    later_tasks = [
        task for task in tasks
        if task.status in TASK_QUEUE_STATUSES and not task_is_ready(task, today)
    ]
    return sorted(later_tasks, key=lambda task: task_sort_key(task, today, now))


def get_subtasks_for_task(db: Session, task_id: int) -> list[Subtask]:
    return db.query(Subtask).filter(Subtask.task_id == task_id).order_by(Subtask.created_at.asc(), Subtask.id.asc()).all()


def get_subtask_summary(db: Session, task_ids: list[int]) -> dict[int, dict[str, int]]:
    summary = {task_id: {"total": 0, "completed": 0, "pending": 0} for task_id in task_ids}
    if not task_ids:
        return summary
    subtasks = db.query(Subtask).filter(Subtask.task_id.in_(task_ids)).all()
    for subtask in subtasks:
        bucket = summary.setdefault(subtask.task_id, {"total": 0, "completed": 0, "pending": 0})
        bucket["total"] += 1
        if subtask.status == "completed":
            bucket["completed"] += 1
        else:
            bucket["pending"] += 1
    return summary


def task_recurrence_anchor(deadline: datetime | None, estimated_completion: datetime | None, start_on: date | None) -> date | None:
    if deadline:
        return utc_naive_to_local(deadline).date()
    if estimated_completion:
        return utc_naive_to_local(estimated_completion).date()
    if start_on:
        return start_on
    return None


def next_task_occurrence(current: date, repeat: str) -> date | None:
    if repeat == "daily":
        return current + timedelta(days=1)
    if repeat == "weekdays":
        next_day = current + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return next_day
    if repeat == "weekly":
        return current + timedelta(days=7)
    return None


def prepare_task_form_fields(
    db: Session,
    *,
    project_id: str,
    tags: str,
    start_on: str,
    priority: int,
    deadline: str,
    estimated_completion: str,
    repeat: str,
    repeat_until: str,
) -> dict:
    try:
        resolved_project_id = resolve_project_id(project_id, db)
        start_on_date = parse_day_input(start_on)
        deadline_dt = parse_date_input(deadline)
        estimated_completion_dt = parse_date_input(estimated_completion)
        repeat_until_date = parse_day_input(repeat_until)
    except ValueError as exc:
        raise ValueError("Invalid date or project selection.") from exc

    repeat_value = (repeat or "none").strip().lower()
    if repeat_value not in TASK_REPEAT_LABELS:
        raise ValueError("Invalid repeat option.")

    if repeat_value != "none" and not (start_on_date or deadline_dt):
        raise ValueError("Recurring tasks need a start date or deadline.")

    anchor_date = task_recurrence_anchor(deadline_dt, estimated_completion_dt, start_on_date)
    if repeat_value == "none":
        repeat_until_date = None
    else:
        if not repeat_until_date:
            raise ValueError("Repeat-until date is required for recurring tasks.")
        if anchor_date and repeat_until_date < anchor_date:
            raise ValueError("Repeat-until date must be on or after the first task date.")

    return {
        "project_id": resolved_project_id,
        "tags_text": normalize_tags_text(tags),
        "start_on": start_on_date,
        "priority": max(1, min(5, priority)),
        "deadline": deadline_dt,
        "estimated_completion": estimated_completion_dt,
        "repeat": repeat_value,
        "repeat_until": repeat_until_date,
    }


def time_to_minutes(value: time | str) -> int:
    parsed = parse_clock(value) if isinstance(value, str) else value
    return parsed.hour * 60 + parsed.minute


def minutes_to_time(total_minutes: int) -> time:
    total_minutes = max(0, min(total_minutes, 23 * 60 + 59))
    return time(total_minutes // 60, total_minutes % 60)


def round_up_to_quarter(current: time) -> time:
    minutes = time_to_minutes(current)
    rounded = ((minutes + 14) // 15) * 15
    return minutes_to_time(min(rounded, time_to_minutes(WORKDAY_END)))


def format_clock(value: time | str) -> str:
    parsed = parse_clock(value) if isinstance(value, str) else value
    return parsed.strftime("%I:%M %p").lstrip("0")


def format_slot_label(slot: dict) -> str:
    day = "Today" if slot["date"] == local_today() else slot["date"].strftime("%a, %b %d")
    return f"{day} · {format_clock(slot['start_time'])} - {format_clock(slot['end_time'])}"


def focus_marker_for_task(task_id: int) -> str:
    return f"{FOCUS_EVENT_MARKER}{task_id}]"


def focus_duration_for_task(task: Task) -> int:
    if task.priority >= 5:
        return 90
    if task.priority >= 3:
        return 50
    return 25


def task_target_date(task: Task) -> date | None:
    if task.estimated_completion:
        return utc_naive_to_local(task.estimated_completion).date()
    if task.deadline:
        return utc_naive_to_local(task.deadline).date()
    return None


def find_event_overlaps(
    db: Session,
    event_date: date,
    start_clock: time,
    end_clock: time,
    exclude_event_id: int | None = None,
    exclude_event_ids: set[int] | None = None,
) -> list[Event]:
    query = db.query(Event).filter(Event.event_date == event_date)
    if exclude_event_id is not None:
        query = query.filter(Event.id != exclude_event_id)

    overlaps: list[Event] = []
    for event in query.order_by(Event.start_time.asc()).all():
        if exclude_event_ids and event.id in exclude_event_ids:
            continue
        existing_start = parse_clock(event.start_time)
        existing_end = parse_clock(event.end_time)
        if start_clock < existing_end and end_clock > existing_start:
            overlaps.append(event)
    return overlaps


def recurring_dates(base_date: date, repeat: str, repeat_end: date | None) -> list[date]:
    dates = [base_date]
    if repeat == "none" or not repeat_end:
        return dates

    current = base_date + timedelta(days=1)
    while current <= repeat_end:
        should_add = False
        if repeat == "daily":
            should_add = True
        elif repeat == "weekdays":
            should_add = current.weekday() < 5
        elif repeat == "weekly":
            should_add = current.weekday() == base_date.weekday()

        if should_add:
            dates.append(current)
        current += timedelta(days=1)

    return dates


def find_next_focus_slot(
    db: Session,
    duration_minutes: int,
    latest_date: date | None = None,
    days_ahead: int = 7,
) -> dict | None:
    today = local_today()
    search_end = today + timedelta(days=days_ahead)
    if latest_date:
        search_end = min(search_end, max(latest_date, today))

    for offset in range((search_end - today).days + 1):
        target_date = today + timedelta(days=offset)
        start_boundary = WORKDAY_START
        if target_date == today:
            start_boundary = max(WORKDAY_START, round_up_to_quarter(local_now().time()))

        cursor = time_to_minutes(start_boundary)
        end_of_day = time_to_minutes(WORKDAY_END)
        if cursor + duration_minutes > end_of_day:
            continue

        events = db.query(Event).filter(Event.event_date == target_date).order_by(Event.start_time.asc()).all()
        for event in events:
            event_start = time_to_minutes(event.start_time)
            event_end = time_to_minutes(event.end_time)
            if event_end <= cursor:
                continue
            if event_start - cursor >= duration_minutes:
                return {
                    "date": target_date,
                    "start_time": minutes_to_time(cursor),
                    "end_time": minutes_to_time(cursor + duration_minutes),
                }
            cursor = max(cursor, event_end)

        if end_of_day - cursor >= duration_minutes:
            return {
                "date": target_date,
                "start_time": minutes_to_time(cursor),
                "end_time": minutes_to_time(cursor + duration_minutes),
            }

    return None


def count_scheduled_focus_blocks(db: Session, task_id: int) -> int:
    marker = focus_marker_for_task(task_id)
    return db.query(Event).filter(
        Event.event_date >= local_today(),
        Event.description.contains(marker),
    ).count()


def calculate_xp(priority: int, deadline: datetime | None, completed_at: datetime) -> int:
    """Calculate XP with early completion bonus."""
    base_xp = XP_BY_PRIORITY.get(priority, 30)
    if deadline and completed_at < deadline:
        hours_early = (deadline - completed_at).total_seconds() / 3600
        bonus = min(int(hours_early), base_xp)  # Up to 2x XP
        return base_xp + bonus
    return base_xp


def get_streak_multiplier(streak: int) -> float:
    if streak >= 30:
        return 2.0
    if streak >= 7:
        return 1.5
    if streak >= 3:
        return 1.2
    return 1.0


def get_task_history(db: Session, limit: int = 30) -> list:
    """Build completed task history for LLM context."""
    completed = db.query(Task).filter(
        Task.status == "completed",
        Task.completed_at.isnot(None),
    ).order_by(Task.completed_at.desc()).limit(limit).all()
    projects = project_lookup(get_projects(db))
    subtask_summary = get_subtask_summary(db, [task.id for task in completed])

    history = []
    for t in completed:
        duration_days = None
        on_time = None
        if t.completed_at and t.created_at:
            duration_days = round((t.completed_at - t.created_at).total_seconds() / 86400, 1)
        if t.deadline and t.completed_at:
            on_time = t.completed_at <= t.deadline

        history.append({
            "title": t.title,
            "priority": t.priority,
            "project": projects.get(t.project_id).name if t.project_id in projects else None,
            "tags": parse_tags_text(t.tags_text),
            "subtasks_completed": subtask_summary.get(t.id, {}).get("completed", 0),
            "subtasks_total": subtask_summary.get(t.id, {}).get("total", 0),
            "start_on": t.start_on.isoformat() if t.start_on else None,
            "deadline": utc_naive_to_local(t.deadline).strftime("%Y-%m-%d") if t.deadline else None,
            "created_at": utc_naive_to_local(t.created_at).strftime("%Y-%m-%d") if t.created_at else None,
            "completed_at": utc_naive_to_local(t.completed_at).strftime("%Y-%m-%d") if t.completed_at else None,
            "duration_days": duration_days,
            "on_time": on_time,
        })
    return history


def get_pending_tasks_data(db: Session) -> list:
    """Build pending task data for LLM context."""
    pending = db.query(Task).filter(Task.status != "completed").order_by(Task.priority.desc()).all()
    projects = project_lookup(get_projects(db))
    subtask_summary = get_subtask_summary(db, [task.id for task in pending])
    return [
        {
            "id": t.id,
            "title": t.title,
            "description": t.description or "",
            "project": projects.get(t.project_id).name if t.project_id in projects else None,
            "tags": parse_tags_text(t.tags_text),
            "subtasks_completed": subtask_summary.get(t.id, {}).get("completed", 0),
            "subtasks_total": subtask_summary.get(t.id, {}).get("total", 0),
            "start_on": t.start_on.isoformat() if t.start_on else None,
            "priority": t.priority,
            "deadline": utc_naive_to_local(t.deadline).strftime("%Y-%m-%d") if t.deadline else None,
            "repeat": t.repeat,
            "status": t.status,
        }
        for t in pending
    ]


def get_history_stats(history: list) -> dict:
    """Compute on-time rate and avg speed from history."""
    durations = [t["duration_days"] for t in history if isinstance(t.get("duration_days"), (int, float))]
    on_time = [t for t in history if t.get("on_time") is not None]
    on_time_count = sum(1 for t in on_time if t["on_time"])
    return {
        "on_time_rate": round(on_time_count / len(on_time) * 100) if on_time else 0,
        "avg_speed": round(sum(durations) / len(durations), 1) if durations else 0,
    }


def get_dw_session_history(db: Session, limit: int = 15) -> list:
    """Build deep work session history for LLM context."""
    sessions = db.query(DeepWorkSession).filter(
        DeepWorkSession.status == "completed"
    ).order_by(DeepWorkSession.ended_at.desc()).limit(limit).all()

    result = []
    for s in sessions:
        task = db.query(Task).filter(Task.id == s.task_id).first() if s.task_id else None
        result.append({
            "task_title": task.title if task else "General Focus",
            "planned_duration": s.planned_duration,
            "actual_duration": s.actual_duration,
            "date": utc_naive_to_local(s.started_at).strftime("%Y-%m-%d") if s.started_at else None,
        })
    return result


def maybe_spawn_next_recurring_task(db: Session, task: Task) -> Task | None:
    if task.repeat == "none":
        return None

    base_anchor = task_recurrence_anchor(task.deadline, task.estimated_completion, task.start_on)
    if not base_anchor:
        return None

    next_anchor = next_task_occurrence(base_anchor, task.repeat)
    if not next_anchor:
        return None
    if task.repeat_until and next_anchor > task.repeat_until:
        return None

    delta_days = (next_anchor - base_anchor).days
    root_id = task.parent_task_id or task.id

    related_tasks = db.query(Task).filter(
        (Task.id == root_id) | (Task.parent_task_id == root_id)
    ).all()
    next_deadline = shift_utc_naive_by_local_days(task.deadline, delta_days)
    next_estimated = shift_utc_naive_by_local_days(task.estimated_completion, delta_days)
    next_start_on = task.start_on + timedelta(days=delta_days) if task.start_on else None

    for existing in related_tasks:
        if existing.id == task.id:
            continue
        if existing.status == "completed":
            continue
        if existing.start_on == next_start_on and existing.deadline == next_deadline:
            return None

    next_task = Task(
        title=task.title,
        description=task.description,
        project_id=task.project_id,
        tags_text=task.tags_text,
        start_on=next_start_on,
        priority=task.priority,
        deadline=next_deadline,
        estimated_completion=next_estimated,
        repeat=task.repeat,
        repeat_until=task.repeat_until,
        parent_task_id=root_id,
        status="pending",
    )
    db.add(next_task)
    db.flush()
    for subtask in get_subtasks_for_task(db, task.id):
        db.add(Subtask(
            task_id=next_task.id,
            title=subtask.title,
            status="pending",
        ))
    return next_task


def update_streak(stats: UserStats) -> None:
    """Update streak based on today's completion."""
    today = local_today()
    if stats.last_completed_date == today:
        return  # Already counted today
    if stats.last_completed_date == today - timedelta(days=1):
        stats.current_streak += 1
    else:
        stats.current_streak = 1
    stats.last_completed_date = today
    if stats.current_streak > stats.longest_streak:
        stats.longest_streak = stats.current_streak


# ── Routes ───────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if request.session.get("authenticated"):
        destination = next if is_safe_redirect_target(next) else "/"
        return RedirectResponse(url=destination, status_code=303)
    return templates.TemplateResponse("login.html", template_context(
        request,
        next_path=next if is_safe_redirect_target(next) else "/",
        hide_nav=True,
    ))


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next_path: str = Form("/"),
    _: None = Depends(validate_csrf),
):
    safe_next = next_path if is_safe_redirect_target(next_path) else "/"
    if secrets.compare_digest(username.strip(), APP_USERNAME) and secrets.compare_digest(password, APP_PASSWORD):
        request.session.clear()
        request.session["authenticated"] = True
        request.session["username"] = APP_USERNAME
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        push_alert(request, "success", "Signed in successfully.")
        return RedirectResponse(url=safe_next, status_code=303)

    push_alert(request, "error", "Invalid username or password.")
    return RedirectResponse(url=f"/login?next={quote(safe_next, safe='/?=&')}", status_code=303)


@app.post("/logout")
async def logout(request: Request, _: None = Depends(validate_csrf)):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.post("/notifications/run", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def run_notifications_now(request: Request):
    summary = check_and_send_notifications()
    push_alert(
        request,
        "info",
        "Reminder scan complete: "
        f"{summary['tasks_scanned']} scanned, "
        f"{summary['sent_24h']} sent (24h), "
        f"{summary['sent_2h']} sent (2h), "
        f"{summary['sent_overdue']} sent (overdue).",
    )
    return RedirectResponse(url="/schedule", status_code=303)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def dashboard(request: Request, db: Session = Depends(get_db)):
    today = local_today()
    now = utc_now_naive()
    stats = get_stats(db)
    all_tasks = db.query(Task).filter(Task.status != "completed").all()
    tasks = sorted(all_tasks, key=lambda task: task_sort_key(task, today, now))
    completed_tasks = db.query(Task).filter(Task.status == "completed").order_by(Task.completed_at.desc()).limit(5).all()
    today_events = db.query(Event).filter(Event.event_date == today).order_by(Event.start_time.asc()).all()
    projects = get_projects(db)
    projects_by_id = project_lookup(projects)
    ready_tasks = [task for task in tasks if task.status in TASK_QUEUE_STATUSES and task_is_ready(task, today)]
    later_tasks = build_later_queue(tasks, today, now)
    attention_tasks = build_attention_queue(tasks, today, now)
    today_queue = build_today_queue(tasks, today, now)
    subtask_summary = get_subtask_summary(db, [task.id for task in all_tasks])
    overdue_tasks_count = sum(1 for task in ready_tasks if task.deadline and task.deadline < now)

    # Build history for LLM context
    history = get_task_history(db)
    h_stats = get_history_stats(history)

    # Get nearest deadline for motivation
    nearest = None
    for t in tasks:
        if t.deadline:
            nearest = utc_naive_to_local(t.deadline).strftime("%Y-%m-%d %H:%M")
            break

    motivation = generate_motivation(
        stats.total_xp, stats.current_streak, stats.tasks_completed,
        len(all_tasks), nearest,
        on_time_rate=h_stats["on_time_rate"],
        avg_speed=h_stats["avg_speed"],
    )

    # XP level calculation (every 100 XP = 1 level)
    level = stats.total_xp // 100 + 1
    xp_in_level = stats.total_xp % 100
    streak_multiplier = get_streak_multiplier(stats.current_streak)

    # Check for active deep work session
    active_session = db.query(DeepWorkSession).filter(DeepWorkSession.status == "active").first()

    # Get deep work suggestion if no active session
    dw_suggestion = None
    if not active_session and tasks:
        tasks_data = get_pending_tasks_data(db)
        actionable_tasks_data = [item for item in tasks_data if item["status"] in TASK_QUEUE_STATUSES]
        dw_history = get_dw_session_history(db)
        if actionable_tasks_data:
            dw_suggestion = suggest_deep_work(
                actionable_tasks_data,
                stats.total_deep_work_minutes,
                stats.deep_work_sessions_completed,
                stats.current_streak,
                history=history,
                dw_history=dw_history,
            )
    next_focus_slot = find_next_focus_slot(db, 50, latest_date=today + timedelta(days=2))
    today_focus_blocks = sum(1 for event in today_events if event.title.startswith(FOCUS_EVENT_PREFIX))
    task_focus_counts = {task.id: count_scheduled_focus_blocks(db, task.id) for task in all_tasks}

    return templates.TemplateResponse("index.html", template_context(
        request,
        tasks=ready_tasks,
        all_tasks=all_tasks,
        today_queue=today_queue[:5],
        later_tasks=later_tasks[:4],
        attention_tasks=attention_tasks[:4],
        completed_tasks=completed_tasks,
        stats=stats,
        motivation=motivation,
        level=level,
        xp_in_level=xp_in_level,
        streak_multiplier=streak_multiplier,
        priority_labels=PRIORITY_LABELS,
        projects_by_id=projects_by_id,
        now=now,
        active_session=active_session,
        dw_suggestion=dw_suggestion,
        next_focus_slot=next_focus_slot,
        next_focus_slot_label=format_slot_label(next_focus_slot) if next_focus_slot else None,
        today_events_count=len(today_events),
        today_focus_blocks=today_focus_blocks,
        open_tasks_count=len(all_tasks),
        today_queue_count=len(today_queue),
        overdue_tasks_count=overdue_tasks_count,
        later_tasks_count=len(later_tasks),
        attention_tasks_count=len(attention_tasks),
        subtask_summary=subtask_summary,
        task_focus_counts=task_focus_counts,
        focus_duration_for_task=focus_duration_for_task,
    ))


@app.get("/projects", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def projects_page(request: Request, db: Session = Depends(get_db)):
    projects = get_projects(db)
    project_cards = []
    for project in projects:
        open_count = db.query(Task).filter(Task.project_id == project.id, Task.status != "completed").count()
        completed_count = db.query(Task).filter(Task.project_id == project.id, Task.status == "completed").count()
        project_cards.append({
            "project": project,
            "open_count": open_count,
            "completed_count": completed_count,
        })
    return templates.TemplateResponse("projects.html", template_context(
        request,
        projects=project_cards,
    ))


@app.post("/projects/create", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        push_alert(request, "error", "Project name cannot be empty.")
        return RedirectResponse(url="/projects", status_code=303)

    existing = db.query(Project).filter(Project.name.ilike(cleaned_name)).first()
    if existing:
        push_alert(request, "info", f'Project "{existing.name}" already exists.')
        return RedirectResponse(url="/projects", status_code=303)

    project = Project(name=cleaned_name[:120], description=description.strip())
    db.add(project)
    db.commit()
    push_alert(request, "success", f'Created project "{project.name}".')
    return RedirectResponse(url="/projects", status_code=303)


@app.post("/projects/{project_id}/delete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        push_alert(request, "error", "Project not found.")
        return RedirectResponse(url="/projects", status_code=303)

    db.query(Task).filter(Task.project_id == project_id).update({"project_id": None}, synchronize_session=False)
    project_name = project.name
    db.delete(project)
    db.commit()
    push_alert(request, "success", f'Deleted project "{project_name}".')
    return RedirectResponse(url="/projects", status_code=303)


@app.get("/tasks/add", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def add_task_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("add_task.html", template_context(
        request,
        step="input",
        projects=get_projects(db),
    ))


@app.get("/tasks/completed", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def completed_tasks_page(request: Request, db: Session = Depends(get_db)):
    completed_tasks = db.query(Task).filter(Task.status == "completed").order_by(Task.completed_at.desc()).all()
    projects_by_id = project_lookup(get_projects(db))
    return templates.TemplateResponse("completed_tasks.html", template_context(
        request,
        completed_tasks=completed_tasks,
        projects_by_id=projects_by_id,
        priority_labels=PRIORITY_LABELS,
    ))


@app.get("/tasks/{task_id}", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def task_detail_page(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    projects = get_projects(db)
    subtasks = get_subtasks_for_task(db, task.id)
    subtask_summary = get_subtask_summary(db, [task.id]).get(task.id, {"total": 0, "completed": 0, "pending": 0})
    return templates.TemplateResponse("task_detail.html", template_context(
        request,
        task=task,
        subtasks=subtasks,
        subtask_summary=subtask_summary,
        projects=projects,
        projects_by_id=project_lookup(projects),
        priority_labels=PRIORITY_LABELS,
    ))


@app.post("/tasks/{task_id}/subtasks/create", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def create_subtask(
    task_id: int,
    request: Request,
    title: str = Form(...),
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        push_alert(request, "error", "Task not found.")
        return RedirectResponse(url="/", status_code=303)
    if task.status == "completed":
        push_alert(request, "error", "Completed tasks cannot accept new subtasks.")
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    cleaned_title = title.strip()
    if not cleaned_title:
        push_alert(request, "error", "Subtask title cannot be empty.")
        return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

    db.add(Subtask(task_id=task_id, title=cleaned_title[:200]))
    db.commit()
    push_alert(request, "success", f'Added subtask to "{task.title}".')
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/subtasks/{subtask_id}/toggle", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def toggle_subtask(subtask_id: int, request: Request, db: Session = Depends(get_db)):
    subtask = db.query(Subtask).filter(Subtask.id == subtask_id).first()
    if not subtask:
        push_alert(request, "error", "Subtask not found.")
        return RedirectResponse(url="/", status_code=303)
    parent_task = db.query(Task).filter(Task.id == subtask.task_id).first()
    if parent_task and parent_task.status == "completed":
        push_alert(request, "error", "Reopen the task before changing its subtasks.")
        return RedirectResponse(url=f"/tasks/{subtask.task_id}", status_code=303)

    if subtask.status == "completed":
        subtask.status = "pending"
        subtask.completed_at = None
        push_alert(request, "info", f'Marked "{subtask.title}" as pending.')
    else:
        subtask.status = "completed"
        subtask.completed_at = utc_now_naive()
        push_alert(request, "success", f'Completed subtask "{subtask.title}".')
    db.commit()
    return RedirectResponse(url=f"/tasks/{subtask.task_id}", status_code=303)


@app.post("/subtasks/{subtask_id}/delete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_subtask(subtask_id: int, request: Request, db: Session = Depends(get_db)):
    subtask = db.query(Subtask).filter(Subtask.id == subtask_id).first()
    if not subtask:
        push_alert(request, "error", "Subtask not found.")
        return RedirectResponse(url="/", status_code=303)

    task_id = subtask.task_id
    subtask_title = subtask.title
    db.delete(subtask)
    db.commit()
    push_alert(request, "success", f'Deleted subtask "{subtask_title}".')
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/analyze", response_class=HTMLResponse, dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def analyze_task_route(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    user_deadline: str = Form(""),
    project_id: str = Form(""),
    tags: str = Form(""),
    start_on: str = Form(""),
    repeat: str = Form("none"),
    repeat_until: str = Form(""),
    db: Session = Depends(get_db),
):
    history = get_task_history(db)
    pending = get_pending_tasks_data(db)
    schedule = get_today_events(db)
    analysis = analyze_task(
        title, description,
        history=history, pending=pending, schedule=schedule,
        user_deadline=user_deadline if user_deadline else None,
    )
    step = "review" if analysis.get("questions") else "confirm"
    return templates.TemplateResponse("add_task.html", template_context(
        request,
        step=step,
        title=title,
        description=description,
        user_deadline=user_deadline,
        project_id=project_id,
        tags=tags,
        start_on=start_on,
        repeat=repeat,
        repeat_until=repeat_until,
        analysis=analysis,
        projects=get_projects(db),
    ))


@app.post("/tasks/followup", response_class=HTMLResponse, dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def followup_task_route(
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    title = form.get("title", "")
    description = form.get("description", "")
    user_deadline = form.get("user_deadline", "")
    project_id = form.get("project_id", "")
    tags = form.get("tags", "")
    start_on = form.get("start_on", "")
    repeat = form.get("repeat", "none")
    repeat_until = form.get("repeat_until", "")

    # Collect questions and answers
    questions = []
    answers = []
    i = 0
    while True:
        q = form.get(f"question_{i}")
        a = form.get(f"answer_{i}")
        if q is None:
            break
        questions.append(q)
        answers.append(a or "")
        i += 1

    history = get_task_history(db)
    pending = get_pending_tasks_data(db)
    schedule = get_today_events(db)
    analysis = followup_analyze(
        title, description, questions, answers,
        history=history, pending=pending, schedule=schedule,
        user_deadline=user_deadline if user_deadline else None,
    )
    return templates.TemplateResponse("add_task.html", template_context(
        request,
        step="confirm",
        title=title,
        description=description,
        user_deadline=user_deadline,
        project_id=project_id,
        tags=tags,
        start_on=start_on,
        repeat=repeat,
        repeat_until=repeat_until,
        analysis=analysis,
        projects=get_projects(db),
    ))


@app.post("/tasks/confirm", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def confirm_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    project_id: str = Form(""),
    tags: str = Form(""),
    start_on: str = Form(""),
    priority: int = Form(...),
    deadline: str = Form(""),
    estimated_completion: str = Form(""),
    repeat: str = Form("none"),
    repeat_until: str = Form(""),
    db: Session = Depends(get_db),
):
    cleaned_title = title.strip()
    if not cleaned_title:
        push_alert(request, "error", "Task title cannot be empty.")
        return RedirectResponse(url="/tasks/add", status_code=303)

    try:
        prepared = prepare_task_form_fields(
            db,
            project_id=project_id,
            tags=tags,
            start_on=start_on,
            priority=priority,
            deadline=deadline,
            estimated_completion=estimated_completion,
            repeat=repeat,
            repeat_until=repeat_until,
        )
    except ValueError as exc:
        push_alert(request, "error", str(exc))
        return RedirectResponse(url="/tasks/add", status_code=303)

    task = Task(
        title=cleaned_title,
        description=description.strip(),
        **prepared,
    )
    db.add(task)
    db.commit()
    push_alert(request, "success", f'Task "{title}" added with priority P{task.priority}.')
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/update", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def update_task(
    task_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    project_id: str = Form(""),
    tags: str = Form(""),
    start_on: str = Form(""),
    priority: int = Form(...),
    deadline: str = Form(""),
    estimated_completion: str = Form(""),
    status: str = Form("pending"),
    repeat: str = Form("none"),
    repeat_until: str = Form(""),
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        push_alert(request, "error", "Task not found.")
        return RedirectResponse(url="/", status_code=303)

    cleaned_title = title.strip()
    if not cleaned_title:
        push_alert(request, "error", "Task title cannot be empty.")
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    try:
        prepared = prepare_task_form_fields(
            db,
            project_id=project_id,
            tags=tags,
            start_on=start_on,
            priority=priority,
            deadline=deadline,
            estimated_completion=estimated_completion,
            repeat=repeat,
            repeat_until=repeat_until,
        )
    except ValueError as exc:
        push_alert(request, "error", str(exc))
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    task.title = cleaned_title
    task.description = description.strip()
    task.project_id = prepared["project_id"]
    task.tags_text = prepared["tags_text"]
    task.start_on = prepared["start_on"]
    task.priority = prepared["priority"]
    task.deadline = prepared["deadline"]
    task.estimated_completion = prepared["estimated_completion"]
    if status == "completed" and task.status == "completed":
        pass
    elif status not in TASK_EDITABLE_STATUSES:
        push_alert(request, "error", "Invalid task status.")
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)
    task.status = status
    task.repeat = prepared["repeat"]
    task.repeat_until = prepared["repeat_until"]
    if task.status != "completed":
        task.completed_at = None

    db.commit()
    push_alert(request, "success", f'Updated "{task.title}".')
    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)


@app.post("/tasks/{task_id}/complete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def complete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        push_alert(request, "error", "Task not found.")
        return RedirectResponse(url="/", status_code=303)
    if task.status == "completed":
        push_alert(request, "info", f'"{task.title}" is already completed.')
        return RedirectResponse(url="/", status_code=303)
    pending_subtasks = db.query(Subtask).filter(Subtask.task_id == task.id, Subtask.status != "completed").count()
    if pending_subtasks:
        push_alert(request, "error", f'Finish the remaining {pending_subtasks} subtask{"s" if pending_subtasks != 1 else ""} before completing "{task.title}".')
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    now = utc_now_naive()
    task.status = "completed"
    task.completed_at = now

    stats = get_stats(db)
    update_streak(stats)
    xp = calculate_xp(task.priority, task.deadline, now)
    multiplier = get_streak_multiplier(stats.current_streak)
    xp = int(xp * multiplier)

    task.xp_earned = xp
    stats.total_xp += xp
    stats.tasks_completed += 1
    next_task = maybe_spawn_next_recurring_task(db, task)

    db.commit()
    if next_task:
        push_alert(
            request,
            "success",
            f'Completed "{task.title}" for +{xp} XP. Next {TASK_REPEAT_LABELS[task.repeat].lower()} task is ready.',
        )
    else:
        push_alert(request, "success", f'Completed "{task.title}" for +{xp} XP.')
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/delete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task_title = task.title
        db.query(Subtask).filter(Subtask.task_id == task.id).delete(synchronize_session=False)
        db.delete(task)
        db.commit()
        push_alert(request, "success", f'Deleted "{task_title}".')
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/start", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def start_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task and task.status != "completed":
        task.status = "in_progress"
        db.commit()
        push_alert(request, "success", f'"{task.title}" is now in progress.')
    elif task and task.status == "completed":
        push_alert(request, "info", f'"{task.title}" is already completed.')
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/plan-focus", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def plan_focus_block(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        push_alert(request, "error", "Task not found.")
        return RedirectResponse(url="/", status_code=303)
    if task.status == "completed":
        push_alert(request, "info", f'"{task.title}" is already completed.')
        return RedirectResponse(url="/", status_code=303)
    if task.status in TASK_HOLD_STATUSES:
        push_alert(request, "error", f'"{task.title}" is {TASK_STATUS_LABELS[task.status].lower()}. Move it back to active before planning focus time.')
        return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

    if count_scheduled_focus_blocks(db, task.id):
        push_alert(request, "info", f'"{task.title}" already has a scheduled focus block.')
        return RedirectResponse(url="/schedule", status_code=303)

    duration = focus_duration_for_task(task)
    latest_date = task_target_date(task)
    slot = find_next_focus_slot(db, duration, latest_date=latest_date)
    if not slot:
        push_alert(request, "error", f'No {duration}m focus slot is free before the task target date.')
        return RedirectResponse(url="/schedule", status_code=303)

    event = Event(
        title=f"{FOCUS_EVENT_PREFIX} {task.title}",
        description=f"{focus_marker_for_task(task.id)} Auto-scheduled focus block for task #{task.id}.",
        event_date=slot["date"],
        start_time=slot["start_time"].strftime("%H:%M"),
        end_time=slot["end_time"].strftime("%H:%M"),
        category="work",
        color="accent",
    )
    db.add(event)
    db.commit()
    push_alert(request, "success", f'Planned {duration}m focus block for "{task.title}" on {format_slot_label(slot)}.')
    return RedirectResponse(url="/schedule", status_code=303)


# ── Deep Work Routes ─────────────────────────────────────────────

DEEP_WORK_XP = {25: 15, 50: 35, 90: 60}


@app.get("/deepwork", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def deep_work_page(request: Request, db: Session = Depends(get_db)):
    active_session = db.query(DeepWorkSession).filter(DeepWorkSession.status == "active").first()
    active_task = None
    if active_session and active_session.task_id:
        active_task = db.query(Task).filter(Task.id == active_session.task_id).first()

    stats = get_stats(db)
    tasks = db.query(Task).filter(Task.status.in_(tuple(TASK_QUEUE_STATUSES))).order_by(Task.priority.desc()).all()

    # Past sessions
    past_sessions = db.query(DeepWorkSession).filter(
        DeepWorkSession.status == "completed"
    ).order_by(DeepWorkSession.ended_at.desc()).limit(10).all()

    # Attach task titles to past sessions
    past_with_tasks = []
    for s in past_sessions:
        task = db.query(Task).filter(Task.id == s.task_id).first() if s.task_id else None
        past_with_tasks.append({"session": s, "task_title": task.title if task else "General Focus"})

    return templates.TemplateResponse("deepwork.html", template_context(
        request,
        active_session=active_session,
        active_task=active_task,
        tasks=tasks,
        stats=stats,
        past_sessions=past_with_tasks,
    ))


@app.post("/deepwork/start", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def start_deep_work(
    request: Request,
    task_id: int = Form(0),
    duration: int = Form(25),
    db: Session = Depends(get_db),
):
    # Cancel any existing active session
    existing = db.query(DeepWorkSession).filter(DeepWorkSession.status == "active").first()
    if existing:
        existing.status = "cancelled"

    if task_id > 0:
        linked_task = db.query(Task).filter(Task.id == task_id).first()
        if not linked_task:
            push_alert(request, "error", "Task not found.")
            return RedirectResponse(url="/deepwork", status_code=303)
        if linked_task.status not in TASK_QUEUE_STATUSES:
            push_alert(request, "error", f'Only active tasks can start a deep work session. "{linked_task.title}" is {TASK_STATUS_LABELS.get(linked_task.status, linked_task.status)}.')
            return RedirectResponse(url="/deepwork", status_code=303)

    session = DeepWorkSession(
        task_id=task_id if task_id > 0 else None,
        planned_duration=duration,
        started_at=utc_now_naive(),
    )
    db.add(session)
    db.commit()
    push_alert(request, "success", f"Started a {duration}m deep work session.")
    return RedirectResponse(url="/deepwork", status_code=303)


@app.post("/deepwork/{session_id}/complete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def complete_deep_work(
    session_id: int,
    request: Request,
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    session = db.query(DeepWorkSession).filter(DeepWorkSession.id == session_id).first()
    if not session:
        push_alert(request, "error", "Deep work session not found.")
        return RedirectResponse(url="/deepwork", status_code=303)
    if session.status == "completed":
        push_alert(request, "info", "This deep work session is already completed.")
        return RedirectResponse(url="/deepwork", status_code=303)
    if session.status != "active":
        push_alert(request, "info", "This deep work session is no longer active.")
        return RedirectResponse(url="/deepwork", status_code=303)

    now = utc_now_naive()
    session.status = "completed"
    session.ended_at = now
    session.notes = notes
    actual_minutes = int((now - session.started_at).total_seconds() / 60)
    session.actual_duration = actual_minutes

    # Award XP: base XP for the duration tier + bonus if completed full duration
    base_xp = DEEP_WORK_XP.get(session.planned_duration, 15)
    if actual_minutes >= session.planned_duration:
        base_xp = int(base_xp * 1.5)  # 50% bonus for completing full session
    session.xp_earned = base_xp

    stats = get_stats(db)
    multiplier = get_streak_multiplier(stats.current_streak)
    xp = int(base_xp * multiplier)
    stats.total_xp += xp
    stats.total_deep_work_minutes += actual_minutes
    stats.deep_work_sessions_completed += 1
    session.xp_earned = xp

    db.commit()
    push_alert(request, "success", f"Deep work complete for +{xp} XP.")
    return RedirectResponse(url="/deepwork", status_code=303)


@app.post("/deepwork/{session_id}/cancel", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def cancel_deep_work(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = db.query(DeepWorkSession).filter(DeepWorkSession.id == session_id).first()
    if session:
        session.status = "cancelled"
        session.ended_at = utc_now_naive()
        db.commit()
        push_alert(request, "info", "Deep work session cancelled.")
    return RedirectResponse(url="/deepwork", status_code=303)


# ── Schedule / Events Routes ────────────────────────────────────

def get_today_events(db: Session, target_date: date = None) -> list:
    """Get events for a given date (defaults to today)."""
    target = target_date or local_today()
    events = db.query(Event).filter(
        Event.event_date == target
    ).order_by(Event.start_time.asc()).all()
    return [
        {"id": e.id, "title": e.title, "description": e.description,
         "start_time": e.start_time, "end_time": e.end_time,
         "category": e.category, "color": e.color}
        for e in events
    ]


def schedule_redirect_url(week_start: date | None = None, selected_date: date | None = None) -> str:
    query: list[str] = []
    if week_start:
        query.append(f"week_start={week_start.isoformat()}")
    if selected_date:
        query.append(f"selected_date={selected_date.isoformat()}")
    if not query:
        return "/schedule"
    return "/schedule?" + "&".join(query)


def schedule_edit_url(event_id: int, week_start: date | None = None, selected_date: date | None = None) -> str:
    base_url = schedule_redirect_url(week_start, selected_date)
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}edit_event_id={event_id}"


def schedule_series_edit_url(event_id: int, week_start: date | None = None, selected_date: date | None = None) -> str:
    return schedule_edit_url(event_id, week_start, selected_date) + "&edit_scope=series"


def week_start_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def root_event_for_series(db: Session, event: Event) -> Event:
    if event.parent_event_id:
        parent = db.query(Event).filter(Event.id == event.parent_event_id).first()
        return parent or event
    return event


def parse_query_date(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


@app.get("/schedule", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def schedule_page(
    request: Request,
    db: Session = Depends(get_db),
    week_start: str | None = None,
    selected_date: str | None = None,
    edit_event_id: int | None = None,
    edit_scope: str | None = None,
):
    today = local_today()
    default_week_start = today - timedelta(days=today.weekday())
    start_of_week = parse_query_date(week_start, default_week_start)
    selected_day = parse_query_date(selected_date, today)

    if not (start_of_week <= selected_day <= start_of_week + timedelta(days=6)):
        selected_day = start_of_week

    week_days = []
    for i in range(7):
        d = start_of_week + timedelta(days=i)
        events = db.query(Event).filter(Event.event_date == d).order_by(Event.start_time.asc()).all()
        week_days.append({
            "date": d,
            "events": events,
            "is_today": d == today,
            "is_selected": d == selected_day,
        })

    selected_events = db.query(Event).filter(
        Event.event_date == selected_day
    ).order_by(Event.start_time.asc()).all()
    editing_event = None
    editing_scope = "single"
    editing_form_event = None
    if edit_event_id is not None:
        editing_event = db.query(Event).filter(Event.id == edit_event_id).first()
        if editing_event and edit_scope == "series" and (editing_event.repeat != "none" or editing_event.parent_event_id):
            editing_scope = "series"
            editing_form_event = root_event_for_series(db, editing_event)
        else:
            editing_scope = "single"
            editing_form_event = editing_event

    pending_count = db.query(Task).filter(Task.status != "completed").count()
    prev_week_start = start_of_week - timedelta(days=7)
    next_week_start = start_of_week + timedelta(days=7)
    week_end = start_of_week + timedelta(days=6)
    prev_selected_day = selected_day - timedelta(days=7)
    next_selected_day = selected_day + timedelta(days=7)

    return templates.TemplateResponse("schedule.html", template_context(
        request,
        today=today,
        selected_day=selected_day,
        week_days=week_days,
        today_events=selected_events,
        pending_count=pending_count,
        next_focus_slot=find_next_focus_slot(db, 50, latest_date=today + timedelta(days=2)),
        week_start=start_of_week,
        week_end=week_end,
        prev_week_start=prev_week_start,
        next_week_start=next_week_start,
        prev_selected_day=prev_selected_day,
        next_selected_day=next_selected_day,
        default_week_start=default_week_start,
        editing_event=editing_event,
        editing_form_event=editing_form_event,
        editing_scope=editing_scope,
    ))


@app.post("/events/add", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def add_event(
    request: Request,
    title: str = Form(...),
    event_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    category: str = Form("general"),
    description: str = Form(""),
    repeat: str = Form("none"),
    repeat_until: str = Form(""),
    week_start: str = Form(""),
    selected_date: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect_week = parse_query_date(week_start, week_start_for(local_today())) if week_start else None
    redirect_selected = parse_query_date(selected_date, local_today()) if selected_date else None
    redirect_url = schedule_redirect_url(redirect_week, redirect_selected)
    try:
        base_date = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError as exc:
        push_alert(request, "error", "Invalid event date.")
        return RedirectResponse(url=redirect_url, status_code=303)

    try:
        start_clock = datetime.strptime(start_time, "%H:%M").time()
        end_clock = datetime.strptime(end_time, "%H:%M").time()
    except ValueError as exc:
        push_alert(request, "error", "Invalid event time.")
        return RedirectResponse(url=redirect_url, status_code=303)

    if start_clock >= end_clock:
        push_alert(request, "error", "End time must be after start time.")
        return RedirectResponse(url=redirect_url, status_code=303)

    allowed_repeats = {"none", "daily", "weekdays", "weekly"}
    if repeat not in allowed_repeats:
        push_alert(request, "error", "Invalid repeat option.")
        return RedirectResponse(url=redirect_url, status_code=303)

    repeat_end = None
    if repeat_until:
        try:
            repeat_end = datetime.strptime(repeat_until, "%Y-%m-%d").date()
        except ValueError as exc:
            push_alert(request, "error", "Invalid repeat-until date.")
            return RedirectResponse(url=redirect_url, status_code=303)

    if repeat != "none" and not repeat_end:
        push_alert(request, "error", "Repeat-until date is required for recurring events.")
        return RedirectResponse(url=redirect_url, status_code=303)
    if repeat_end and repeat_end < base_date:
        push_alert(request, "error", "Repeat-until date must be on or after the event date.")
        return RedirectResponse(url=redirect_url, status_code=303)

    for occurrence_date in recurring_dates(base_date, repeat, repeat_end):
        overlaps = find_event_overlaps(db, occurrence_date, start_clock, end_clock)
        if overlaps:
            conflict = overlaps[0]
            push_alert(
                request,
                "error",
                f'Event overlaps with "{conflict.title}" on {occurrence_date.strftime("%b %d")} at {conflict.start_time}.',
            )
            return RedirectResponse(url=redirect_url, status_code=303)

    # Create the parent event
    parent = Event(
        title=title,
        description=description,
        event_date=base_date,
        start_time=start_time,
        end_time=end_time,
        category=category,
        repeat=repeat,
        repeat_until=repeat_end,
    )
    db.add(parent)
    db.flush()  # get parent.id

    # Generate recurring instances
    if repeat != "none" and repeat_end:
        for current in recurring_dates(base_date, repeat, repeat_end)[1:]:
            db.add(Event(
                title=title,
                description=description,
                event_date=current,
                start_time=start_time,
                end_time=end_time,
                category=category,
                repeat=repeat,
                parent_event_id=parent.id,
            ))

    db.commit()
    created_count = len(recurring_dates(base_date, repeat, repeat_end))
    if created_count > 1:
        push_alert(request, "success", f'Added "{title}" and scheduled {created_count} events in the series.')
    else:
        push_alert(request, "success", f'Added "{title}" to your schedule.')
    return RedirectResponse(url=schedule_redirect_url(week_start_for(base_date), base_date), status_code=303)


@app.post("/events/{event_id}/update", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def update_event(
    event_id: int,
    request: Request,
    title: str = Form(...),
    event_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    category: str = Form("general"),
    description: str = Form(""),
    scope: str = Form("single"),
    repeat: str = Form("none"),
    repeat_until: str = Form(""),
    week_start: str = Form(""),
    selected_date: str = Form(""),
    db: Session = Depends(get_db),
):
    today = local_today()
    redirect_week = parse_query_date(week_start, week_start_for(today)) if week_start else None
    redirect_selected = parse_query_date(selected_date, today) if selected_date else None
    event = db.query(Event).filter(Event.id == event_id).first()
    redirect_url = schedule_redirect_url(redirect_week, redirect_selected)
    if not event:
        push_alert(request, "error", "Event not found.")
        return RedirectResponse(url=redirect_url, status_code=303)

    try:
        updated_date = datetime.strptime(event_date, "%Y-%m-%d").date()
        start_clock = datetime.strptime(start_time, "%H:%M").time()
        end_clock = datetime.strptime(end_time, "%H:%M").time()
    except ValueError:
        push_alert(request, "error", "Invalid event date or time.")
        return RedirectResponse(url=schedule_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

    if start_clock >= end_clock:
        push_alert(request, "error", "End time must be after start time.")
        return RedirectResponse(url=schedule_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

    overlaps = find_event_overlaps(db, updated_date, start_clock, end_clock, exclude_event_id=event.id)
    if overlaps:
        conflict = overlaps[0]
        push_alert(
            request,
            "error",
            f'Event overlaps with "{conflict.title}" on {updated_date.strftime("%b %d")} at {conflict.start_time}.',
        )
        return RedirectResponse(url=schedule_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

    if scope == "series" and (event.repeat != "none" or event.parent_event_id):
        root_event = root_event_for_series(db, event)
        repeat_value = (repeat or root_event.repeat or "none").strip().lower()
        allowed_repeats = {"none", "daily", "weekdays", "weekly"}
        if repeat_value not in allowed_repeats:
            push_alert(request, "error", "Invalid repeat option.")
            return RedirectResponse(url=schedule_series_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

        repeat_end = None
        if repeat_until:
            try:
                repeat_end = datetime.strptime(repeat_until, "%Y-%m-%d").date()
            except ValueError:
                push_alert(request, "error", "Invalid repeat-until date.")
                return RedirectResponse(url=schedule_series_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

        if repeat_value != "none" and not repeat_end:
            push_alert(request, "error", "Repeat-until date is required for recurring events.")
            return RedirectResponse(url=schedule_series_edit_url(event_id, redirect_week, redirect_selected), status_code=303)
        if repeat_end and repeat_end < updated_date:
            push_alert(request, "error", "Repeat-until date must be on or after the event date.")
            return RedirectResponse(url=schedule_series_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

        series_events = db.query(Event).filter(
            (Event.id == root_event.id) | (Event.parent_event_id == root_event.id)
        ).all()
        exclude_ids = {item.id for item in series_events}
        occurrence_dates = recurring_dates(updated_date, repeat_value, repeat_end)
        for occurrence_date in occurrence_dates:
            series_overlaps = find_event_overlaps(
                db,
                occurrence_date,
                start_clock,
                end_clock,
                exclude_event_ids=exclude_ids,
            )
            if series_overlaps:
                conflict = series_overlaps[0]
                push_alert(
                    request,
                    "error",
                    f'Series update overlaps with "{conflict.title}" on {occurrence_date.strftime("%b %d")} at {conflict.start_time}.',
                )
                return RedirectResponse(url=schedule_series_edit_url(event_id, redirect_week, redirect_selected), status_code=303)

        root_event.title = title.strip() or root_event.title
        root_event.description = description.strip()
        root_event.event_date = updated_date
        root_event.start_time = start_time
        root_event.end_time = end_time
        root_event.category = category
        root_event.repeat = repeat_value
        root_event.repeat_until = repeat_end
        child_events = [item for item in series_events if item.id != root_event.id]
        for child_event in child_events:
            db.delete(child_event)
        db.flush()
        for occurrence_date in occurrence_dates[1:]:
            db.add(Event(
                title=root_event.title,
                description=root_event.description,
                event_date=occurrence_date,
                start_time=start_time,
                end_time=end_time,
                category=category,
                repeat=repeat_value,
                parent_event_id=root_event.id,
            ))
        db.commit()
        push_alert(request, "success", f'Updated the "{root_event.title}" series.')
        return RedirectResponse(url=schedule_redirect_url(week_start_for(updated_date), updated_date), status_code=303)

    event.title = title.strip() or event.title
    event.description = description.strip()
    event.event_date = updated_date
    event.start_time = start_time
    event.end_time = end_time
    event.category = category
    db.commit()
    push_alert(request, "success", f'Updated "{event.title}".')
    return RedirectResponse(url=schedule_redirect_url(week_start_for(updated_date), updated_date), status_code=303)


@app.post("/events/{event_id}/delete-series", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_event_series(
    event_id: int,
    request: Request,
    week_start: str = Form(""),
    selected_date: str = Form(""),
    db: Session = Depends(get_db),
):
    """Delete an event and all its recurring instances."""
    today = local_today()
    redirect_week = parse_query_date(week_start, week_start_for(today)) if week_start else None
    redirect_selected = parse_query_date(selected_date, today) if selected_date else None
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        series_title = event.title
        if event.parent_event_id:
            parent_id = event.parent_event_id
            db.query(Event).filter(Event.parent_event_id == parent_id).delete(synchronize_session=False)
            parent = db.query(Event).filter(Event.id == parent_id).first()
            if parent:
                db.delete(parent)
        else:
            db.query(Event).filter(Event.parent_event_id == event_id).delete(synchronize_session=False)
            db.delete(event)
        db.commit()
        push_alert(request, "success", f'Deleted the "{series_title}" series.')
    return RedirectResponse(url=schedule_redirect_url(redirect_week, redirect_selected), status_code=303)


@app.post("/events/{event_id}/delete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_event(
    event_id: int,
    request: Request,
    week_start: str = Form(""),
    selected_date: str = Form(""),
    db: Session = Depends(get_db),
):
    today = local_today()
    redirect_week = parse_query_date(week_start, week_start_for(today)) if week_start else None
    redirect_selected = parse_query_date(selected_date, today) if selected_date else None
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        title = event.title
        db.delete(event)
        db.commit()
        push_alert(request, "success", f'Deleted "{title}" from your schedule.')
    return RedirectResponse(url=schedule_redirect_url(redirect_week, redirect_selected), status_code=303)

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
from app.models import Task, UserStats, DeepWorkSession, Event
from app.llm import analyze_task, followup_analyze, generate_motivation, suggest_deep_work
from app.notifications import check_and_send_notifications, email_config_issues, current_email_provider

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
    """Treat date inputs as end-of-day deadlines."""
    if not value:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(parsed, time(23, 59, 59))


def template_context(request: Request, **context) -> dict:
    base = {
        "request": request,
        "alerts": request.session.pop("alerts", []),
        "authenticated": bool(request.session.get("authenticated")),
        "app_username": APP_USERNAME,
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
    day = "Today" if slot["date"] == date.today() else slot["date"].strftime("%a, %b %d")
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
        return task.estimated_completion.date()
    if task.deadline:
        return task.deadline.date()
    return None


def find_event_overlaps(
    db: Session,
    event_date: date,
    start_clock: time,
    end_clock: time,
    exclude_event_id: int | None = None,
) -> list[Event]:
    query = db.query(Event).filter(Event.event_date == event_date)
    if exclude_event_id is not None:
        query = query.filter(Event.id != exclude_event_id)

    overlaps: list[Event] = []
    for event in query.order_by(Event.start_time.asc()).all():
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
    today = date.today()
    search_end = today + timedelta(days=days_ahead)
    if latest_date:
        search_end = min(search_end, max(latest_date, today))

    for offset in range((search_end - today).days + 1):
        target_date = today + timedelta(days=offset)
        start_boundary = WORKDAY_START
        if target_date == today:
            start_boundary = max(WORKDAY_START, round_up_to_quarter(datetime.now().time()))

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
        Event.event_date >= date.today(),
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
            "deadline": t.deadline.strftime("%Y-%m-%d") if t.deadline else None,
            "created_at": t.created_at.strftime("%Y-%m-%d") if t.created_at else None,
            "completed_at": t.completed_at.strftime("%Y-%m-%d") if t.completed_at else None,
            "duration_days": duration_days,
            "on_time": on_time,
        })
    return history


def get_pending_tasks_data(db: Session) -> list:
    """Build pending task data for LLM context."""
    pending = db.query(Task).filter(Task.status != "completed").order_by(Task.priority.desc()).all()
    return [
        {"id": t.id, "title": t.title, "description": t.description or "",
         "priority": t.priority,
         "deadline": t.deadline.strftime("%Y-%m-%d") if t.deadline else None,
         "status": t.status}
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
            "date": s.started_at.strftime("%Y-%m-%d") if s.started_at else None,
        })
    return result


def update_streak(stats: UserStats) -> None:
    """Update streak based on today's completion."""
    today = date.today()
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
    stats = get_stats(db)
    tasks = db.query(Task).filter(Task.status != "completed").order_by(Task.priority.desc(), Task.deadline.asc()).all()
    completed_tasks = db.query(Task).filter(Task.status == "completed").order_by(Task.completed_at.desc()).limit(5).all()
    today_events = db.query(Event).filter(Event.event_date == date.today()).order_by(Event.start_time.asc()).all()

    # Build history for LLM context
    history = get_task_history(db)
    h_stats = get_history_stats(history)

    # Get nearest deadline for motivation
    nearest = None
    for t in tasks:
        if t.deadline:
            nearest = t.deadline.strftime("%Y-%m-%d %H:%M")
            break

    motivation = generate_motivation(
        stats.total_xp, stats.current_streak, stats.tasks_completed,
        len(tasks), nearest,
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
        dw_history = get_dw_session_history(db)
        dw_suggestion = suggest_deep_work(
            tasks_data,
            stats.total_deep_work_minutes,
            stats.deep_work_sessions_completed,
            stats.current_streak,
            history=history,
            dw_history=dw_history,
        )
    next_focus_slot = find_next_focus_slot(db, 50, latest_date=date.today() + timedelta(days=2))
    today_focus_blocks = sum(1 for event in today_events if event.title.startswith(FOCUS_EVENT_PREFIX))
    urgent_tasks = tasks[:3]
    task_focus_counts = {task.id: count_scheduled_focus_blocks(db, task.id) for task in tasks}

    return templates.TemplateResponse("index.html", template_context(
        request,
        tasks=tasks,
        completed_tasks=completed_tasks,
        stats=stats,
        motivation=motivation,
        level=level,
        xp_in_level=xp_in_level,
        streak_multiplier=streak_multiplier,
        priority_labels=PRIORITY_LABELS,
        now=datetime.utcnow(),
        active_session=active_session,
        dw_suggestion=dw_suggestion,
        urgent_tasks=urgent_tasks,
        next_focus_slot=next_focus_slot,
        next_focus_slot_label=format_slot_label(next_focus_slot) if next_focus_slot else None,
        today_events_count=len(today_events),
        today_focus_blocks=today_focus_blocks,
        task_focus_counts=task_focus_counts,
        focus_duration_for_task=focus_duration_for_task,
    ))


@app.get("/tasks/add", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def add_task_page(request: Request):
    return templates.TemplateResponse("add_task.html", template_context(
        request,
        step="input",
    ))


@app.post("/tasks/analyze", response_class=HTMLResponse, dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def analyze_task_route(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    user_deadline: str = Form(""),
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
        analysis=analysis,
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
        analysis=analysis,
    ))


@app.post("/tasks/confirm", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def confirm_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: int = Form(...),
    deadline: str = Form(""),
    estimated_completion: str = Form(""),
    db: Session = Depends(get_db),
):
    deadline_dt = parse_date_input(deadline)
    est_dt = parse_date_input(estimated_completion)
    task = Task(
        title=title,
        description=description,
        priority=max(1, min(5, priority)),
        deadline=deadline_dt,
        estimated_completion=est_dt,
    )
    db.add(task)
    db.commit()
    push_alert(request, "success", f'Task "{title}" added with priority P{task.priority}.')
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/complete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def complete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        push_alert(request, "error", "Task not found.")
        return RedirectResponse(url="/", status_code=303)
    if task.status == "completed":
        push_alert(request, "info", f'"{task.title}" is already completed.')
        return RedirectResponse(url="/", status_code=303)

    now = datetime.utcnow()
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

    db.commit()
    push_alert(request, "success", f'Completed "{task.title}" for +{xp} XP.')
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/delete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_task(task_id: int, request: Request, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task_title = task.title
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
    tasks = db.query(Task).filter(Task.status != "completed").order_by(Task.priority.desc()).all()

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

    session = DeepWorkSession(
        task_id=task_id if task_id > 0 else None,
        planned_duration=duration,
        started_at=datetime.utcnow(),
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

    now = datetime.utcnow()
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
        session.ended_at = datetime.utcnow()
        db.commit()
        push_alert(request, "info", "Deep work session cancelled.")
    return RedirectResponse(url="/deepwork", status_code=303)


# ── Schedule / Events Routes ────────────────────────────────────

def get_today_events(db: Session, target_date: date = None) -> list:
    """Get events for a given date (defaults to today)."""
    target = target_date or date.today()
    events = db.query(Event).filter(
        Event.event_date == target
    ).order_by(Event.start_time.asc()).all()
    return [
        {"id": e.id, "title": e.title, "description": e.description,
         "start_time": e.start_time, "end_time": e.end_time,
         "category": e.category, "color": e.color}
        for e in events
    ]


@app.get("/schedule", response_class=HTMLResponse, dependencies=[Depends(require_authenticated)])
async def schedule_page(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    week_days = []
    for i in range(7):
        d = start_of_week + timedelta(days=i)
        events = db.query(Event).filter(Event.event_date == d).order_by(Event.start_time.asc()).all()
        week_days.append({"date": d, "events": events, "is_today": d == today})

    today_events = db.query(Event).filter(
        Event.event_date == today
    ).order_by(Event.start_time.asc()).all()

    pending_count = db.query(Task).filter(Task.status != "completed").count()

    return templates.TemplateResponse("schedule.html", template_context(
        request,
        today=today,
        week_days=week_days,
        today_events=today_events,
        pending_count=pending_count,
        next_focus_slot=find_next_focus_slot(db, 50, latest_date=today + timedelta(days=2)),
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
    db: Session = Depends(get_db),
):
    try:
        base_date = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError as exc:
        push_alert(request, "error", "Invalid event date.")
        return RedirectResponse(url="/schedule", status_code=303)

    try:
        start_clock = datetime.strptime(start_time, "%H:%M").time()
        end_clock = datetime.strptime(end_time, "%H:%M").time()
    except ValueError as exc:
        push_alert(request, "error", "Invalid event time.")
        return RedirectResponse(url="/schedule", status_code=303)

    if start_clock >= end_clock:
        push_alert(request, "error", "End time must be after start time.")
        return RedirectResponse(url="/schedule", status_code=303)

    allowed_repeats = {"none", "daily", "weekdays", "weekly"}
    if repeat not in allowed_repeats:
        push_alert(request, "error", "Invalid repeat option.")
        return RedirectResponse(url="/schedule", status_code=303)

    repeat_end = None
    if repeat_until:
        try:
            repeat_end = datetime.strptime(repeat_until, "%Y-%m-%d").date()
        except ValueError as exc:
            push_alert(request, "error", "Invalid repeat-until date.")
            return RedirectResponse(url="/schedule", status_code=303)

    if repeat != "none" and not repeat_end:
        push_alert(request, "error", "Repeat-until date is required for recurring events.")
        return RedirectResponse(url="/schedule", status_code=303)
    if repeat_end and repeat_end < base_date:
        push_alert(request, "error", "Repeat-until date must be on or after the event date.")
        return RedirectResponse(url="/schedule", status_code=303)

    for occurrence_date in recurring_dates(base_date, repeat, repeat_end):
        overlaps = find_event_overlaps(db, occurrence_date, start_clock, end_clock)
        if overlaps:
            conflict = overlaps[0]
            push_alert(
                request,
                "error",
                f'Event overlaps with "{conflict.title}" on {occurrence_date.strftime("%b %d")} at {conflict.start_time}.',
            )
            return RedirectResponse(url="/schedule", status_code=303)

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
    return RedirectResponse(url="/schedule", status_code=303)


@app.post("/events/{event_id}/delete-series", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_event_series(event_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete an event and all its recurring instances."""
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
    return RedirectResponse(url="/schedule", status_code=303)


@app.post("/events/{event_id}/delete", dependencies=[Depends(require_authenticated), Depends(validate_csrf)])
async def delete_event(event_id: int, request: Request, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        title = event.title
        db.delete(event)
        db.commit()
        push_alert(request, "success", f'Deleted "{title}" from your schedule.')
    return RedirectResponse(url="/schedule", status_code=303)

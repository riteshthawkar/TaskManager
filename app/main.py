from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime, date, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from app.database import engine, get_db, Base
from app.models import Task, UserStats, DeepWorkSession, Event
from app.llm import analyze_task, generate_motivation, suggest_deep_work
from app.notifications import check_and_send_notifications

BASE_DIR = Path(__file__).resolve().parent

# Background scheduler for notifications
scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send_notifications, "interval", minutes=30, id="notification_check")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup
    try:
        Base.metadata.create_all(bind=engine)
        print("[STARTUP] Tables created successfully")
    except Exception as e:
        print(f"[STARTUP ERROR] Failed to create tables: {e}")

    # Start scheduler
    try:
        scheduler.start()
        print("[STARTUP] Scheduler started")
    except Exception as e:
        print(f"[STARTUP ERROR] Scheduler failed: {e}")

    # Ensure UserStats row exists
    from app.database import SessionLocal
    try:
        db = SessionLocal()
        if not db.query(UserStats).first():
            db.add(UserStats(total_xp=0, current_streak=0, longest_streak=0, tasks_completed=0))
            db.commit()
        db.close()
        print("[STARTUP] UserStats ready")
    except Exception as e:
        print(f"[STARTUP ERROR] UserStats init failed: {e}")

    yield

    try:
        scheduler.shutdown()
    except Exception:
        pass


app = FastAPI(title="TaskManager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Helpers ──────────────────────────────────────────────────────

PRIORITY_LABELS = {1: "Low", 2: "Medium-Low", 3: "Medium", 4: "High", 5: "Critical"}
XP_BY_PRIORITY = {1: 10, 2: 20, 3: 30, 4: 40, 5: 50}


def get_stats(db: Session) -> UserStats:
    stats = db.query(UserStats).first()
    if not stats:
        stats = UserStats(total_xp=0, current_streak=0, longest_streak=0, tasks_completed=0)
        db.add(stats)
        db.commit()
        db.refresh(stats)
    return stats


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

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    stats = get_stats(db)
    tasks = db.query(Task).filter(Task.status != "completed").order_by(Task.priority.desc(), Task.deadline.asc()).all()
    completed_tasks = db.query(Task).filter(Task.status == "completed").order_by(Task.completed_at.desc()).limit(5).all()

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

    return templates.TemplateResponse("index.html", {
        "request": request,
        "tasks": tasks,
        "completed_tasks": completed_tasks,
        "stats": stats,
        "motivation": motivation,
        "level": level,
        "xp_in_level": xp_in_level,
        "streak_multiplier": streak_multiplier,
        "priority_labels": PRIORITY_LABELS,
        "now": datetime.utcnow(),
        "active_session": active_session,
        "dw_suggestion": dw_suggestion,
    })


@app.get("/tasks/add", response_class=HTMLResponse)
async def add_task_page(request: Request):
    return templates.TemplateResponse("add_task.html", {
        "request": request,
        "step": "input",
    })


@app.post("/tasks/analyze", response_class=HTMLResponse)
async def analyze_task_route(request: Request, title: str = Form(...), description: str = Form(""),
                             db: Session = Depends(get_db)):
    history = get_task_history(db)
    pending = get_pending_tasks_data(db)
    schedule = get_today_events(db)
    analysis = analyze_task(title, description, history=history, pending=pending, schedule=schedule)
    return templates.TemplateResponse("add_task.html", {
        "request": request,
        "step": "review",
        "title": title,
        "description": description,
        "analysis": analysis,
    })


@app.post("/tasks/confirm")
async def confirm_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: int = Form(...),
    deadline: str = Form(...),
    db: Session = Depends(get_db),
):
    deadline_dt = datetime.strptime(deadline, "%Y-%m-%d") if deadline else None
    task = Task(
        title=title,
        description=description,
        priority=max(1, min(5, priority)),
        deadline=deadline_dt,
    )
    db.add(task)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/complete")
async def complete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        return RedirectResponse(url="/", status_code=303)

    now = datetime.utcnow()
    task.status = "completed"
    task.completed_at = now

    stats = get_stats(db)
    xp = calculate_xp(task.priority, task.deadline, now)
    multiplier = get_streak_multiplier(stats.current_streak)
    xp = int(xp * multiplier)

    task.xp_earned = xp
    stats.total_xp += xp
    stats.tasks_completed += 1
    update_streak(stats)

    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/delete")
async def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        db.delete(task)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/tasks/{task_id}/start")
async def start_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if task:
        task.status = "in_progress"
        db.commit()
    return RedirectResponse(url="/", status_code=303)


# ── Deep Work Routes ─────────────────────────────────────────────

DEEP_WORK_XP = {25: 15, 50: 35, 90: 60}


@app.get("/deepwork", response_class=HTMLResponse)
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

    return templates.TemplateResponse("deepwork.html", {
        "request": request,
        "active_session": active_session,
        "active_task": active_task,
        "tasks": tasks,
        "stats": stats,
        "past_sessions": past_with_tasks,
    })


@app.post("/deepwork/start")
async def start_deep_work(
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
    return RedirectResponse(url="/deepwork", status_code=303)


@app.post("/deepwork/{session_id}/complete")
async def complete_deep_work(
    session_id: int,
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    session = db.query(DeepWorkSession).filter(DeepWorkSession.id == session_id).first()
    if not session:
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
    return RedirectResponse(url="/deepwork", status_code=303)


@app.post("/deepwork/{session_id}/cancel")
async def cancel_deep_work(session_id: int, db: Session = Depends(get_db)):
    session = db.query(DeepWorkSession).filter(DeepWorkSession.id == session_id).first()
    if session:
        session.status = "cancelled"
        session.ended_at = datetime.utcnow()
        db.commit()
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


@app.get("/schedule", response_class=HTMLResponse)
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

    return templates.TemplateResponse("schedule.html", {
        "request": request,
        "today": today,
        "week_days": week_days,
        "today_events": today_events,
        "pending_count": pending_count,
    })


@app.post("/events/add")
async def add_event(
    title: str = Form(...),
    event_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    category: str = Form("general"),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    event = Event(
        title=title,
        description=description,
        event_date=datetime.strptime(event_date, "%Y-%m-%d").date(),
        start_time=start_time,
        end_time=end_time,
        category=category,
    )
    db.add(event)
    db.commit()
    return RedirectResponse(url="/schedule", status_code=303)


@app.post("/events/{event_id}/delete")
async def delete_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).filter(Event.id == event_id).first()
    if event:
        db.delete(event)
        db.commit()
    return RedirectResponse(url="/schedule", status_code=303)

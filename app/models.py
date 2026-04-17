from datetime import datetime, date

from sqlalchemy import Integer, String, DateTime, Date, Text, Boolean, ForeignKey, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=True)
    tags_text: Mapped[str] = mapped_column(Text, default="")
    start_on: Mapped[date] = mapped_column(Date, nullable=True)
    planned_for_date: Mapped[date] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 (low) to 5 (critical)
    deadline: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # user's desired deadline
    estimated_completion: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # LLM's estimate
    repeat: Mapped[str] = mapped_column(String(20), default="none")  # none, daily, weekdays, weekly
    repeat_until: Mapped[date] = mapped_column(Date, nullable=True)
    parent_task_id: Mapped[int] = mapped_column(Integer, nullable=True)  # links to original recurring task
    deadline_confidence: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, in_progress, waiting, blocked, completed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    xp_earned: Mapped[int] = mapped_column(Integer, default=0)
    reminder_24h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_2h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    overdue_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class Subtask(Base):
    __tablename__ = "subtasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, completed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class TaskNote(Base):
    __tablename__ = "task_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskActivity(Base):
    __tablename__ = "task_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    activity_type: Mapped[str] = mapped_column(String(40), default="update")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserStats(Base):
    __tablename__ = "user_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    total_xp: Mapped[int] = mapped_column(Integer, default=0)
    current_streak: Mapped[int] = mapped_column(Integer, default=0)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_completed_date: Mapped[date] = mapped_column(Date, nullable=True)
    tasks_completed: Mapped[int] = mapped_column(Integer, default=0)
    total_deep_work_minutes: Mapped[int] = mapped_column(Integer, default=0)
    deep_work_sessions_completed: Mapped[int] = mapped_column(Integer, default=0)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workday_start: Mapped[str] = mapped_column(String(5), default="08:00")
    workday_end: Mapped[str] = mapped_column(String(5), default="20:00")
    default_focus_minutes: Mapped[int] = mapped_column(Integer, default=50)
    daily_top_task_target: Mapped[int] = mapped_column(Integer, default=3)
    reminder_day_hours: Mapped[int] = mapped_column(Integer, default=24)
    reminder_final_hours: Mapped[int] = mapped_column(Integer, default=2)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class DeepWorkSession(Base):
    __tablename__ = "deep_work_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    planned_duration: Mapped[int] = mapped_column(Integer, nullable=False)  # minutes
    actual_duration: Mapped[int] = mapped_column(Integer, default=0)  # minutes
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ended_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, completed, cancelled
    notes: Mapped[str] = mapped_column(Text, default="")
    xp_earned: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)  # HH:MM
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)    # HH:MM
    category: Mapped[str] = mapped_column(String(30), default="general")  # meeting, personal, work, break
    color: Mapped[str] = mapped_column(String(20), default="accent")
    repeat: Mapped[str] = mapped_column(String(20), default="none")  # none, daily, weekdays, weekly
    repeat_until: Mapped[date] = mapped_column(Date, nullable=True)
    parent_event_id: Mapped[int] = mapped_column(Integer, nullable=True)  # links to original repeating event
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

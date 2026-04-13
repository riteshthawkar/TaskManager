from datetime import datetime, date

from sqlalchemy import Integer, String, DateTime, Date, Text, Boolean, ForeignKey, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=3)  # 1 (low) to 5 (critical)
    deadline: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, in_progress, completed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    xp_earned: Mapped[int] = mapped_column(Integer, default=0)
    reminder_24h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_2h_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    overdue_sent: Mapped[bool] = mapped_column(Boolean, default=False)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

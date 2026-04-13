import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app.database import SessionLocal
from app.models import Task
from app.llm import generate_motivation

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")


def send_email(subject: str, body: str) -> bool:
    """Send an email notification via SMTP."""
    if not all([SMTP_USER, SMTP_PASS, NOTIFY_EMAIL]):
        print(f"[EMAIL MOCK] To: {NOTIFY_EMAIL}\nSubject: {subject}\n{body}\n")
        return False

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        print(f"[EMAIL SENT] {subject}")
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


def build_email_body(task: Task, alert_type: str, motivation: str) -> str:
    """Build a styled HTML email body."""
    priority_labels = {1: "Low", 2: "Medium-Low", 3: "Medium", 4: "High", 5: "Critical"}
    priority_colors = {1: "#4CAF50", 2: "#8BC34A", 3: "#FFC107", 4: "#FF9800", 5: "#F44336"}

    deadline_str = task.deadline.strftime("%B %d, %Y at %I:%M %p") if task.deadline else "No deadline"
    p_label = priority_labels.get(task.priority, "Medium")
    p_color = priority_colors.get(task.priority, "#FFC107")

    alert_headers = {
        "reminder": ("Reminder: Task Due Within 24 Hours", "#2196F3"),
        "urgent": ("Urgent: Task Due in 2 Hours!", "#FF9800"),
        "overdue": ("Overdue: Task Past Deadline!", "#F44336"),
    }
    header_text, header_color = alert_headers.get(alert_type, ("Task Notification", "#2196F3"))

    return f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 500px; margin: 0 auto;">
        <div style="background: {header_color}; color: white; padding: 16px 20px; border-radius: 8px 8px 0 0;">
            <h2 style="margin: 0; font-size: 18px;">{header_text}</h2>
        </div>
        <div style="border: 1px solid #e0e0e0; border-top: none; padding: 20px; border-radius: 0 0 8px 8px;">
            <h3 style="margin: 0 0 8px 0;">{task.title}</h3>
            <p style="color: #666; margin: 0 0 12px 0;">{task.description or 'No description'}</p>
            <div style="display: flex; gap: 12px; margin-bottom: 16px;">
                <span style="background: {p_color}; color: white; padding: 4px 10px; border-radius: 12px; font-size: 13px;">
                    Priority: {p_label}
                </span>
                <span style="background: #f5f5f5; padding: 4px 10px; border-radius: 12px; font-size: 13px;">
                    Due: {deadline_str}
                </span>
            </div>
            <div style="background: #FFF8E1; border-left: 3px solid #FFC107; padding: 12px; border-radius: 4px;">
                <p style="margin: 0; font-style: italic; color: #555;">{motivation}</p>
            </div>
        </div>
    </div>
    """


def check_and_send_notifications():
    """Check all pending tasks and send notifications as needed."""
    db: Session = SessionLocal()
    try:
        now = datetime.utcnow()
        pending_tasks = db.query(Task).filter(
            Task.status.in_(["pending", "in_progress"]),
            Task.deadline.isnot(None),
        ).all()

        # Get stats for motivation
        from app.models import UserStats
        stats = db.query(UserStats).first()
        total_xp = stats.total_xp if stats else 0
        streak = stats.current_streak if stats else 0
        completed = stats.tasks_completed if stats else 0
        pending_count = len(pending_tasks)

        for task in pending_tasks:
            time_until = task.deadline - now
            nearest = task.deadline.strftime("%Y-%m-%d %H:%M") if task.deadline else "None"

            # 24-hour reminder
            if not task.reminder_24h_sent and timedelta(hours=0) < time_until <= timedelta(hours=24):
                motivation = generate_motivation(total_xp, streak, completed, pending_count, nearest)
                body = build_email_body(task, "reminder", motivation)
                if send_email(f"Reminder: '{task.title}' is due within 24 hours", body):
                    task.reminder_24h_sent = True

            # 2-hour urgent
            elif not task.reminder_2h_sent and timedelta(hours=0) < time_until <= timedelta(hours=2):
                motivation = generate_motivation(total_xp, streak, completed, pending_count, nearest)
                body = build_email_body(task, "urgent", motivation)
                if send_email(f"URGENT: '{task.title}' is due in 2 hours!", body):
                    task.reminder_2h_sent = True

            # Overdue
            elif not task.overdue_sent and time_until <= timedelta(hours=0):
                motivation = generate_motivation(total_xp, streak, completed, pending_count, nearest)
                body = build_email_body(task, "overdue", motivation)
                if send_email(f"OVERDUE: '{task.title}' has passed its deadline!", body):
                    task.overdue_sent = True

        db.commit()
    except Exception as e:
        print(f"[NOTIFICATION ERROR] {e}")
        db.rollback()
    finally:
        db.close()

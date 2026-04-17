import os
import json
import smtplib
import logging
from html import escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from urllib import error as urlerror
from urllib import request as urlrequest

from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app.database import SessionLocal
from app.models import Task, UserSettings, PushSubscription
from app.llm import generate_motivation
from app.time_utils import utc_naive_to_local, utc_now_naive

try:
    from pywebpush import webpush, WebPushException
except Exception:  # pragma: no cover - optional dependency guard
    webpush = None
    WebPushException = Exception

load_dotenv()
logger = logging.getLogger("taskmanager.notifications")

EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "").strip().lower()
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
# App passwords are often pasted with spaces for readability.
SMTP_PASS = os.getenv("SMTP_PASS", "").strip().replace(" ", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "").strip()
EMAIL_REPLY_TO = os.getenv("EMAIL_REPLY_TO", "").strip()
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "").strip()
EMAIL_API_TIMEOUT_MS = int(os.getenv("EMAIL_API_TIMEOUT_MS", "20000"))
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_CLAIMS_SUBJECT = os.getenv("VAPID_CLAIMS_SUBJECT", "mailto:taskmanager@example.com").strip()


def current_email_provider() -> str:
    if EMAIL_PROVIDER:
        return EMAIL_PROVIDER
    if RESEND_API_KEY:
        return "resend"
    if SMTP_USER or SMTP_PASS:
        return "smtp"
    return "disabled"


def email_config_issues() -> list[str]:
    issues = []
    provider = current_email_provider()
    if provider == "disabled":
        return ["EMAIL_PROVIDER is not configured"]
    if provider == "resend":
        if not RESEND_API_KEY:
            issues.append("RESEND_API_KEY missing")
        if not EMAIL_FROM:
            issues.append("EMAIL_FROM missing")
    elif provider == "smtp":
        if not SMTP_USER:
            issues.append("SMTP_USER missing")
        if not SMTP_PASS:
            issues.append("SMTP_PASS missing")
    else:
        issues.append(f"Unsupported EMAIL_PROVIDER: {provider}")
    if not NOTIFY_EMAIL:
        issues.append("NOTIFY_EMAIL missing")
    return issues


def push_notifications_enabled() -> bool:
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and webpush is not None)


def push_config_issues() -> list[str]:
    issues = []
    if not VAPID_PUBLIC_KEY:
        issues.append("VAPID_PUBLIC_KEY missing")
    if not VAPID_PRIVATE_KEY:
        issues.append("VAPID_PRIVATE_KEY missing")
    if webpush is None:
        issues.append("pywebpush dependency missing")
    return issues


def send_email(subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an email notification using the configured provider."""
    issues = email_config_issues()
    if issues:
        logger.warning("Email send skipped (%s). Subject=%s", ", ".join(issues), subject)
        return False

    provider = current_email_provider()
    if provider == "resend":
        return send_email_via_resend(subject, html_body, text_body)
    if provider == "smtp":
        return send_email_via_smtp(subject, html_body, text_body)

    logger.warning("Email send skipped (unsupported provider: %s). Subject=%s", provider, subject)
    return False


def send_email_via_smtp(subject: str, html_body: str, text_body: str | None = None) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = SMTP_USER
        msg["To"] = NOTIFY_EMAIL
        msg["Subject"] = subject
        if text_body:
            msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        logger.info("Email sent via SMTP. Subject=%s", subject)
        return True
    except Exception as e:
        logger.exception("SMTP email send failed. Subject=%s Error=%s", subject, e)
        return False


def send_email_via_resend(subject: str, html_body: str, text_body: str | None = None) -> bool:
    payload = {
        "from": EMAIL_FROM,
        "to": [NOTIFY_EMAIL],
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body
    if EMAIL_REPLY_TO:
        payload["reply_to"] = [EMAIL_REPLY_TO]

    request = urlrequest.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "taskmanager-reminders/1.0",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(request, timeout=max(1, EMAIL_API_TIMEOUT_MS // 1000)) as response:
            raw_body = response.read().decode("utf-8") if response else ""
            response_payload = json.loads(raw_body) if raw_body else {}
            logger.info(
                "Email sent via Resend. Subject=%s EmailId=%s Status=%s",
                subject,
                response_payload.get("id", "unknown"),
                getattr(response, "status", "unknown"),
            )
            return True
    except urlerror.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "Resend email send failed. Subject=%s Status=%s Body=%s",
            subject,
            exc.code,
            error_body[:500],
        )
        return False
    except Exception as e:
        logger.exception("Resend email send failed. Subject=%s Error=%s", subject, e)
        return False


def send_push_message(title: str, body: str, url: str = "/", db: Session | None = None) -> int:
    if not push_notifications_enabled():
        logger.warning("Push notification send skipped (%s).", ", ".join(push_config_issues()))
        return 0

    owns_session = db is None
    if db is None:
        db = SessionLocal()

    delivered = 0
    try:
        subscriptions = db.query(PushSubscription).filter(PushSubscription.enabled.is_(True)).all()
        for subscription in subscriptions:
            payload = json.dumps({
                "title": title,
                "body": body,
                "url": url,
            })
            try:
                webpush(
                    subscription_info={
                        "endpoint": subscription.endpoint,
                        "keys": {
                            "p256dh": subscription.p256dh,
                            "auth": subscription.auth,
                        },
                    },
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": VAPID_CLAIMS_SUBJECT},
                )
                subscription.last_used_at = utc_now_naive()
                delivered += 1
            except WebPushException as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning("Push delivery failed for endpoint %s status=%s", subscription.endpoint[:80], status_code)
                if status_code in {404, 410}:
                    subscription.enabled = False
            except Exception as exc:
                logger.warning("Push delivery failed for endpoint %s error=%s", subscription.endpoint[:80], exc)
    finally:
        if owns_session:
            db.commit()
            db.close()
        else:
            db.flush()
    return delivered


def format_deadline(deadline: datetime | None) -> str:
    if not deadline:
        return "No deadline"
    local_deadline = utc_naive_to_local(deadline)
    return local_deadline.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")


def render_email_template(
    *,
    preview: str,
    title: str,
    eyebrow: str,
    accent_color: str,
    accent_glow: str,
    intro: str,
    highlight_value: str,
    highlight_label: str,
    stats: list[tuple[str, str]],
    motivation: str,
    footer_note: str,
) -> tuple[str, str]:
    safe_preview = escape(preview)
    safe_title = escape(title)
    safe_eyebrow = escape(eyebrow)
    safe_intro = escape(intro)
    safe_highlight_value = escape(highlight_value)
    safe_highlight_label = escape(highlight_label)
    safe_motivation = escape(motivation)
    safe_footer = escape(footer_note)

    stat_cells_html = "".join(
        f"""
        <td style="padding: 0 6px 12px 0;" valign="top">
            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
                   style="border-collapse: separate; border-spacing: 0; background: #0f1310; border: 1px solid #1f2a20; border-radius: 12px;">
                <tr>
                    <td style="padding: 12px 14px;">
                        <div style="font-size: 11px; line-height: 1.4; color: #8f9d92; text-transform: uppercase; letter-spacing: 0.08em;">{escape(label)}</div>
                        <div style="margin-top: 4px; font-size: 14px; line-height: 1.4; font-weight: 700; color: #f3fff0;">{escape(value)}</div>
                    </td>
                </tr>
            </table>
        </td>
        """
        for label, value in stats
    )

    text_lines = [
        eyebrow,
        title,
        "",
        intro,
        "",
        f"{highlight_label}: {highlight_value}",
    ]
    for label, value in stats:
        text_lines.append(f"{label}: {value}")
    text_lines.extend([
        "",
        "Motivation",
        motivation,
        "",
        footer_note,
    ])

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{safe_title}</title>
</head>
<body style="margin: 0; padding: 0; background: #050705; color: #f3fff0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;">
  <div style="display: none; max-height: 0; overflow: hidden; opacity: 0; mso-hide: all;">{safe_preview}</div>
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background: #050705;">
    <tr>
      <td align="center" style="padding: 28px 14px;">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="max-width: 620px; border-collapse: separate; border-spacing: 0;">
          <tr>
            <td style="padding-bottom: 14px; color: #95ff45; font-size: 12px; letter-spacing: 0.12em; text-transform: uppercase; font-weight: 700;">
              TaskManager
            </td>
          </tr>
          <tr>
            <td style="background: #0a0d0a; border: 1px solid #192019; border-radius: 22px; overflow: hidden; box-shadow: 0 24px 60px rgba(0,0,0,0.45);">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                <tr>
                  <td style="padding: 28px 28px 18px; background: #090c09; border-bottom: 1px solid #192019;">
                    <div style="font-size: 11px; line-height: 1.4; color: {accent_color}; text-transform: uppercase; letter-spacing: 0.12em; font-weight: 700;">{safe_eyebrow}</div>
                    <h1 style="margin: 10px 0 0; font-size: 28px; line-height: 1.2; letter-spacing: -0.04em; color: #f3fff0;">{safe_title}</h1>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 24px 28px 10px;">
                    <p style="margin: 0 0 18px; font-size: 15px; line-height: 1.7; color: #cfdccf;">{safe_intro}</p>
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
                           style="border-collapse: separate; border-spacing: 0; background: #0d110d; border: 1px solid #1a231b; border-radius: 16px;">
                      <tr>
                        <td style="padding: 18px 20px;">
                          <div style="font-size: 11px; line-height: 1.4; color: #8f9d92; text-transform: uppercase; letter-spacing: 0.1em;">{safe_highlight_label}</div>
                          <div style="margin-top: 8px; font-size: 22px; line-height: 1.3; font-weight: 800; color: {accent_color}; text-shadow: 0 0 20px {accent_glow};">{safe_highlight_value}</div>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 8px 22px 4px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
                      <tr>
                        {stat_cells_html}
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 8px 28px 24px;">
                    <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%"
                           style="border-collapse: separate; border-spacing: 0; background: #101510; border-left: 4px solid {accent_color}; border-radius: 14px;">
                      <tr>
                        <td style="padding: 18px 18px 16px;">
                          <div style="font-size: 11px; line-height: 1.4; color: #8f9d92; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 700;">Motivation</div>
                          <p style="margin: 10px 0 0; font-size: 15px; line-height: 1.7; color: #e3eee1;">{safe_motivation}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
                <tr>
                  <td style="padding: 0 28px 28px; font-size: 12px; line-height: 1.7; color: #8f9d92;">
                    {safe_footer}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
    return html, "\n".join(text_lines)


def build_task_email(task: Task, alert_type: str, motivation: str, window_hours: int | None = None) -> tuple[str, str]:
    """Build a consistent TaskManager email for reminders and alerts."""
    priority_labels = {1: "Low", 2: "Medium-Low", 3: "Medium", 4: "High", 5: "Critical"}
    priority_colors = {1: "#30ff85", 2: "#63ffaf", 3: "#ffe566", 4: "#ffb800", 5: "#ff4d6a"}

    deadline_str = format_deadline(task.deadline)
    p_label = priority_labels.get(task.priority, "Medium")
    p_color = priority_colors.get(task.priority, "#FFC107")

    alert_meta = {
        "reminder": {
            "eyebrow": "Upcoming deadline",
            "title": task.title,
            "intro": "A task on your board is approaching its deadline. This is the right moment to either finish it or deliberately reschedule it.",
            "highlight_label": "Due",
            "accent_color": "#95ff45",
            "accent_glow": "rgba(149,255,69,0.35)",
            "footer": f"You are receiving this because reminder checks are enabled for your TaskManager account. This reminder is sent inside your {window_hours or 24}-hour planning window.",
        },
        "urgent": {
            "eyebrow": "Urgent deadline",
            "title": task.title,
            "intro": "This task is close to its deadline. If you still plan to finish it today, it should be the next thing you touch.",
            "highlight_label": "Due soon",
            "accent_color": "#ffb800",
            "accent_glow": "rgba(255,184,0,0.35)",
            "footer": f"Urgent reminders are sent when a task enters its final {window_hours or 2}-hour window.",
        },
        "overdue": {
            "eyebrow": "Deadline missed",
            "title": task.title,
            "intro": "This task has passed its deadline. Decide whether to complete it immediately, reduce its scope, or move the date consciously.",
            "highlight_label": "Missed deadline",
            "accent_color": "#ff4d6a",
            "accent_glow": "rgba(255,77,106,0.35)",
            "footer": "Overdue reminders are sent once so your inbox stays useful.",
        },
    }
    meta = alert_meta.get(alert_type, alert_meta["reminder"])

    preview = f"{task.title} is tied to {deadline_str}."
    if task.description:
        preview = f"{preview} {task.description[:80]}"

    description_value = task.description.strip() if task.description else "No description"
    estimated_value = format_deadline(getattr(task, "estimated_completion", None))

    return render_email_template(
        preview=preview,
        title=meta["title"],
        eyebrow=meta["eyebrow"],
        accent_color=meta["accent_color"],
        accent_glow=meta["accent_glow"],
        intro=meta["intro"],
        highlight_value=deadline_str,
        highlight_label=meta["highlight_label"],
        stats=[
            ("Priority", p_label),
            ("Estimated completion", estimated_value),
            ("Description", description_value),
        ],
        motivation=motivation,
        footer_note=meta["footer"],
    )


def check_and_send_notifications() -> dict:
    """Check all pending tasks and send notifications as needed."""
    db: Session = SessionLocal()
    summary = {
        "tasks_scanned": 0,
        "day_window_hours": 24,
        "final_window_hours": 2,
        "sent_day_window": 0,
        "sent_final_window": 0,
        "sent_overdue": 0,
        "push_sent": 0,
    }
    try:
        settings = db.query(UserSettings).first()
        day_window_hours = max(1, min(getattr(settings, "reminder_day_hours", 24), 168))
        final_window_hours = max(1, min(getattr(settings, "reminder_final_hours", 2), 24))
        if final_window_hours >= day_window_hours:
            final_window_hours = max(1, min(day_window_hours - 1, 24))
        summary["day_window_hours"] = day_window_hours
        summary["final_window_hours"] = final_window_hours

        now = utc_now_naive()
        pending_tasks = db.query(Task).filter(
            Task.status.in_(["pending", "in_progress"]),
            Task.deadline.isnot(None),
        ).all()
        summary["tasks_scanned"] = len(pending_tasks)

        # Get stats for motivation
        from app.models import UserStats
        stats = db.query(UserStats).first()
        total_xp = stats.total_xp if stats else 0
        streak = stats.current_streak if stats else 0
        completed = stats.tasks_completed if stats else 0
        pending_count = len(pending_tasks)

        for task in pending_tasks:
            time_until = task.deadline - now
            nearest_local = utc_naive_to_local(task.deadline) if task.deadline else None
            nearest = nearest_local.strftime("%Y-%m-%d %H:%M") if nearest_local else "None"

            # Overdue
            if not task.overdue_sent and time_until <= timedelta(hours=0):
                motivation = generate_motivation(total_xp, streak, completed, pending_count, nearest)
                html_body, text_body = build_task_email(task, "overdue", motivation)
                if send_email(f"OVERDUE: '{task.title}' has passed its deadline!", html_body, text_body):
                    task.overdue_sent = True
                    summary["sent_overdue"] += 1
                summary["push_sent"] += send_push_message("Task overdue", f'"{task.title}" has passed its deadline.', url=f"/tasks/{task.id}", db=db)

            # 2-hour urgent
            elif not task.reminder_2h_sent and timedelta(hours=0) < time_until <= timedelta(hours=final_window_hours):
                motivation = generate_motivation(total_xp, streak, completed, pending_count, nearest)
                html_body, text_body = build_task_email(task, "urgent", motivation, window_hours=final_window_hours)
                if send_email(f"URGENT: '{task.title}' is due within {final_window_hours} hour{'s' if final_window_hours != 1 else ''}", html_body, text_body):
                    task.reminder_2h_sent = True
                    summary["sent_final_window"] += 1
                summary["push_sent"] += send_push_message(
                    "Task due soon",
                    f'"{task.title}" is due within {final_window_hours} hour{"s" if final_window_hours != 1 else ""}.',
                    url=f"/tasks/{task.id}",
                    db=db,
                )

            # broader reminder
            elif not task.reminder_24h_sent and timedelta(hours=0) < time_until <= timedelta(hours=day_window_hours):
                motivation = generate_motivation(total_xp, streak, completed, pending_count, nearest)
                html_body, text_body = build_task_email(task, "reminder", motivation, window_hours=day_window_hours)
                if send_email(f"Reminder: '{task.title}' is due within {day_window_hours} hour{'s' if day_window_hours != 1 else ''}", html_body, text_body):
                    task.reminder_24h_sent = True
                    summary["sent_day_window"] += 1
                summary["push_sent"] += send_push_message(
                    "Upcoming deadline",
                    f'"{task.title}" is due within {day_window_hours} hour{"s" if day_window_hours != 1 else ""}.',
                    url=f"/tasks/{task.id}",
                    db=db,
                )

        db.commit()
        logger.info(
            "Notification scan complete: scanned=%s sent_day_window=%s sent_final_window=%s sent_overdue=%s push_sent=%s windows=(%sh,%sh)",
            summary["tasks_scanned"],
            summary["sent_day_window"],
            summary["sent_final_window"],
            summary["sent_overdue"],
            summary["push_sent"],
            summary["day_window_hours"],
            summary["final_window_hours"],
        )
    except Exception as e:
        logger.exception("[NOTIFICATION ERROR] %s", e)
        db.rollback()
    finally:
        db.close()
    return summary

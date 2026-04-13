import os
import json
from datetime import datetime, timedelta

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _build_history_block(history: list) -> str:
    """Format completed task history into a readable block for prompts."""
    if not history:
        return "No task history yet — this is a new user."

    lines = []
    for t in history:
        created = t.get("created_at", "?")
        completed = t.get("completed_at", "?")
        deadline = t.get("deadline", "none")
        duration_days = t.get("duration_days", "?")
        on_time = t.get("on_time", "?")
        lines.append(
            f'- "{t["title"]}" | P{t["priority"]} | '
            f'took {duration_days}d | deadline: {deadline} | on_time: {on_time}'
        )

    avg_days = [t["duration_days"] for t in history if isinstance(t.get("duration_days"), (int, float))]
    avg = round(sum(avg_days) / len(avg_days), 1) if avg_days else "?"

    on_time_count = sum(1 for t in history if t.get("on_time"))
    total = len(history)
    on_time_pct = round(on_time_count / total * 100) if total else 0

    summary = (
        f"\nHistory summary: {total} tasks completed, "
        f"avg completion time {avg} days, "
        f"{on_time_pct}% completed before deadline."
    )

    return "\n".join(lines[-15:]) + summary  # last 15 tasks max


TASK_ANALYSIS_PROMPT = """You are an intelligent, self-improving productivity manager. You learn from the user's past performance to make better suggestions over time.

The user wants to add a new task. Your job:
1. Look at their COMPLETED TASK HISTORY below to understand their patterns — how long similar tasks actually took, whether they tend to finish on time, what priority levels they typically handle.
2. Ask 2-3 short clarifying questions (only for things you can't infer).
3. Suggest a realistic priority (1=low to 5=critical) and deadline based on BOTH the task description AND the user's actual track record.

COMPLETED TASK HISTORY:
{history}

CURRENT PENDING TASKS:
{pending}

TODAY'S SCHEDULE (events/meetings):
{schedule}

Rules:
- If the user consistently misses deadlines, suggest slightly more generous timelines.
- If they finish tasks faster than expected, tighten the deadline.
- If a similar task was done before, use that as a baseline.
- Factor in their current workload (pending tasks above).
- Factor in their schedule — if today is packed with meetings, suggest a later deadline.
- Priority should reflect actual urgency relative to what else is on their plate.

Today's date: {today}

Respond in this exact JSON format:
{{
    "questions": ["question1", "question2"],
    "suggested_priority": 3,
    "suggested_deadline": "YYYY-MM-DD",
    "reasoning": "Explain your reasoning, referencing their history and schedule if relevant"
}}"""


def analyze_task(title: str, description: str, history: list = None,
                 pending: list = None, schedule: list = None) -> dict:
    """Analyze a task using history + schedule aware LLM."""
    history = history or []
    pending = pending or []
    schedule = schedule or []

    history_block = _build_history_block(history)
    pending_block = json.dumps(
        [{"title": t["title"], "priority": t["priority"],
          "deadline": t.get("deadline"), "status": t["status"]}
         for t in pending],
        default=str,
    ) if pending else "No pending tasks."

    schedule_block = "No events today." if not schedule else "\n".join(
        f'- {e["start_time"]}–{e["end_time"]}: {e["title"]} ({e.get("category", "general")})'
        for e in schedule
    )

    try:
        prompt = TASK_ANALYSIS_PROMPT.format(
            history=history_block,
            pending=pending_block,
            schedule=schedule_block,
            today=datetime.now().strftime("%Y-%m-%d"),
        )
        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Task: {title}\nDescription: {description}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=500,
        )
        result = json.loads(response.choices[0].message.content)
        return {
            "questions": result.get("questions", []),
            "suggested_priority": max(1, min(5, result.get("suggested_priority", 3))),
            "suggested_deadline": result.get("suggested_deadline",
                                             (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")),
            "reasoning": result.get("reasoning", ""),
        }
    except Exception as e:
        return {
            "questions": ["How urgent is this task?", "What's a reasonable deadline?"],
            "suggested_priority": 3,
            "suggested_deadline": (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"),
            "reasoning": f"(Using defaults — API error: {e})",
        }


MOTIVATION_PROMPT = """You are an encouraging productivity coach. Generate a short (1-2 sentence) motivational message.

Context:
- Total XP: {total_xp}
- Current streak: {current_streak} days
- Tasks completed: {tasks_completed}
- Pending tasks: {pending_count}
- Nearest deadline: {nearest_deadline}
- On-time rate: {on_time_rate}%
- Avg completion speed: {avg_speed} days

Be specific. Reference their on-time rate or speed if notable. Keep it punchy — no platitudes."""


def generate_motivation(total_xp: int, current_streak: int, tasks_completed: int,
                        pending_count: int, nearest_deadline: str,
                        on_time_rate: int = 0, avg_speed: float = 0) -> str:
    """Generate a personalized motivational message."""
    try:
        prompt = MOTIVATION_PROMPT.format(
            total_xp=total_xp,
            current_streak=current_streak,
            tasks_completed=tasks_completed,
            pending_count=pending_count,
            nearest_deadline=nearest_deadline or "None",
            on_time_rate=on_time_rate,
            avg_speed=round(avg_speed, 1) if avg_speed else "N/A",
        )
        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Give me a motivational nudge."},
            ],
            temperature=0.9,
            max_tokens=100,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        if current_streak > 0:
            return f"You're on a {current_streak}-day streak! Keep the momentum going."
        return "Every task you complete is a step forward. Let's get one done today!"


DEEP_WORK_PROMPT = """You are a productivity coach specializing in deep work.

PENDING TASKS:
{tasks_json}

COMPLETED TASK HISTORY (for pattern analysis):
{history}

DEEP WORK SESSION HISTORY:
{dw_history}

User stats: {dw_sessions} sessions completed, {total_dw_minutes} total minutes, {current_streak}-day streak.
Today: {today}

Analyze patterns:
- Which types of tasks did the user do deep work on before? How long did sessions last?
- Which pending task would benefit most from focused time given deadline proximity and priority?
- What duration fits best based on task complexity AND the user's actual session history?

Respond in JSON:
{{
    "recommended_task_id": <id>,
    "recommended_task_title": "<title>",
    "suggested_duration": <25, 50, or 90>,
    "reasoning": "<reference history patterns if available>",
    "tip": "<contextual deep work tip>"
}}"""


def suggest_deep_work(tasks: list, total_dw_minutes: int, dw_sessions: int,
                      current_streak: int, history: list = None,
                      dw_history: list = None) -> dict:
    """Suggest a deep work session using task + session history."""
    if not tasks:
        return {
            "recommended_task_id": None,
            "recommended_task_title": "No tasks available",
            "suggested_duration": 25,
            "reasoning": "Add some tasks first, then I can recommend a session.",
            "tip": "Start with a clear goal before each deep work session.",
        }

    history = history or []
    dw_history = dw_history or []

    tasks_json = json.dumps([
        {"id": t["id"], "title": t["title"], "description": t["description"][:100],
         "priority": t["priority"], "deadline": t["deadline"], "status": t["status"]}
        for t in tasks
    ], default=str)

    history_block = _build_history_block(history)

    dw_lines = []
    for s in dw_history[-10:]:
        dw_lines.append(
            f'- "{s.get("task_title", "General")}" | {s["actual_duration"]}min '
            f'(planned {s["planned_duration"]}min) | {s.get("date", "?")}'
        )
    dw_block = "\n".join(dw_lines) if dw_lines else "No deep work history yet."

    try:
        prompt = DEEP_WORK_PROMPT.format(
            tasks_json=tasks_json,
            history=history_block,
            dw_history=dw_block,
            total_dw_minutes=total_dw_minutes,
            dw_sessions=dw_sessions,
            current_streak=current_streak,
            today=datetime.now().strftime("%Y-%m-%d"),
        )
        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Suggest a deep work session for me."},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=300,
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        best = max(tasks, key=lambda t: t["priority"])
        duration = 25 if best["priority"] <= 2 else 50 if best["priority"] <= 4 else 90
        return {
            "recommended_task_id": best["id"],
            "recommended_task_title": best["title"],
            "suggested_duration": duration,
            "reasoning": f"'{best['title']}' is highest priority and needs focused attention.",
            "tip": "Eliminate all distractions. Close unnecessary tabs and put your phone away.",
        }

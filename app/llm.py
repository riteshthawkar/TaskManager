from __future__ import annotations

import os
import json
from datetime import datetime, timedelta

from openai import OpenAI
from dotenv import load_dotenv

from app.time_utils import local_now

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
2. Ask 0-3 short clarifying questions, but ONLY if something material is unclear from the description. If the task is already clear enough, return an empty questions array.
3. Suggest a realistic priority (1=low to 5=critical) and deadline based on BOTH the task description AND the user's actual track record.
4. Estimate how long the task will ACTUALLY take to complete (estimated_completion_date). This should be your honest assessment based on complexity and history — it may be earlier than the deadline for easy tasks, or you may flag if the task seems too complex for the given deadline.
5. Suggest a lightweight breakdown of 0-5 concrete next steps if the task is big enough to benefit from it.
6. Provide a deadline_confidence of `high`, `medium`, or `low` based on how certain you are about the proposed timing.

COMPLETED TASK HISTORY:
{history}

CURRENT PENDING TASKS:
{pending}

TODAY'S SCHEDULE (events/meetings):
{schedule}

USER'S REQUESTED DEADLINE (if any): {user_deadline}

Rules:
- If the user consistently misses deadlines, suggest slightly more generous timelines.
- If they finish tasks faster than expected, tighten the deadline.
- If a similar task was done before, use that as a baseline.
- Factor in their current workload (pending tasks above).
- Factor in their schedule — if today is packed with meetings, suggest a later deadline.
- Priority should reflect actual urgency relative to what else is on their plate.
- If the user gave a deadline, respect it as the suggested_deadline but give your honest estimated_completion_date.
- For simple tasks, the estimated_completion_date should be sooner than the deadline.
- If the task seems too complex for the user's deadline, flag this in reasoning.
- Return deadline values in the user's local time using `YYYY-MM-DDTHH:MM`.
- If the user gives only a date with no time, assume end of day.
- Use an explicit time, not just a date.

Current local date/time: {today}

Respond in this exact JSON format:
{{
    "questions": ["question1", "question2"],
    "suggested_priority": 3,
    "suggested_deadline": "YYYY-MM-DDTHH:MM",
    "estimated_completion_date": "YYYY-MM-DDTHH:MM",
    "deadline_confidence": "medium",
    "suggested_breakdown": ["step 1", "step 2"],
    "reasoning": "Explain your reasoning, referencing their history and schedule if relevant"
}}"""


FOLLOWUP_PROMPT = """You are an intelligent productivity manager. The user previously described a task. You asked clarifying questions, and they have now answered them.

Use their answers to REFINE your suggestions. Be more precise now that you have more information.

COMPLETED TASK HISTORY:
{history}

CURRENT PENDING TASKS:
{pending}

TODAY'S SCHEDULE:
{schedule}

USER'S REQUESTED DEADLINE (if any): {user_deadline}

Rules:
- Return deadline values in the user's local time using `YYYY-MM-DDTHH:MM`.
- If the user gives only a date with no time, assume end of day.
- Use an explicit time, not just a date.

Current local date/time: {today}

Respond in this exact JSON format:
{{
    "suggested_priority": 3,
    "suggested_deadline": "YYYY-MM-DDTHH:MM",
    "estimated_completion_date": "YYYY-MM-DDTHH:MM",
    "deadline_confidence": "medium",
    "suggested_breakdown": ["step 1", "step 2"],
    "reasoning": "Refined reasoning based on clarifications"
}}"""


DAY_PLAN_PROMPT = """You are an expert daily planner.

Your job is to decide how the user should spend a specific day based on:
- their open tasks
- the day's fixed commitments (classes, meetings, personal events)
- deadlines, planned dates, status, and subtask progress
- realistic capacity

You are choosing exact work blocks for the day. Pick start times from the available free slots below.
You decide:
- which tasks deserve time today
- the exact start time for each block
- how many minutes each task should get today
- what the day's focus should be
- what the user should watch out for

Target planning date: {target_date}
Current local time: {current_time}
Planning mode: {planning_mode}

User settings:
{settings_json}

Open task candidates:
{tasks_json}

Fixed schedule for the day:
{schedule_json}

Available free slots:
{free_slots_json}

Completed task history:
{history}

Rules:
- Return 2-6 recommendations.
- Use only task IDs that exist in the candidate list.
- Use a `start_time` that fits inside one of the listed available free slots.
- Recommend minutes for TODAY only, not total task duration.
- Do not fill the entire day; leave breathing room.
- Prefer urgent or already-planned tasks, but do not overload the day.
- Avoid blocked or waiting work.
- If the day is crowded, choose fewer tasks and be explicit about tradeoffs.
- Keep minutes practical: 25, 30, 45, 50, 60, 75, 90, 120, or 150.
- Return `start_time` as `HH:MM` in local time.
- Keep summary concise and actionable.

Respond in this exact JSON format:
{{
    "summary": "Short summary of the day's plan",
    "reasoning": "Why these tasks deserve time today",
    "recommendations": [
        {{
            "task_id": 12,
            "start_time": "09:30",
            "minutes": 50,
            "reason": "Why it should get time today"
        }}
    ],
    "watchouts": ["One short risk or reminder"]
}}"""


def _build_context_blocks(history, pending, schedule):
    """Build formatted context blocks for LLM prompts."""
    history_block = _build_history_block(history or [])
    pending_block = json.dumps(
        [{"title": t["title"], "priority": t["priority"],
          "deadline": t.get("deadline"), "deadline_confidence": t.get("deadline_confidence"),
          "planned_for_date": t.get("planned_for_date"), "start_on": t.get("start_on"),
          "status": t["status"]}
         for t in (pending or [])],
        default=str,
    ) if pending else "No pending tasks."
    schedule_block = "No events today." if not schedule else "\n".join(
        f'- {e["start_time"]}–{e["end_time"]}: {e["title"]} ({e.get("category", "general")})'
        for e in schedule
    )
    return history_block, pending_block, schedule_block


def _normalize_datetime_string(value: str | None, fallback: str) -> str:
    """Accept local date-only or local datetime responses from the model."""
    if not value:
        return fallback
    cleaned = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%dT%H:%M")
        except (TypeError, ValueError):
            continue
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d").strftime("%Y-%m-%dT23:59")
    except (TypeError, ValueError):
        return fallback


def _normalize_questions(value) -> list[str]:
    if not isinstance(value, list):
        return []
    questions = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            questions.append(cleaned)
        if len(questions) == 3:
            break
    return questions


def _normalize_breakdown(value) -> list[str]:
    if not isinstance(value, list):
        return []
    steps = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        steps.append(cleaned[:200])
        if len(steps) == 5:
            break
    return steps


def _normalize_priority(value, fallback: int = 3) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return fallback


def _normalize_confidence(value, fallback: str = "medium") -> str:
    cleaned = str(value or fallback).strip().lower()
    return cleaned if cleaned in {"high", "medium", "low"} else fallback


def _normalize_minutes(value, fallback: int = 50) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = fallback
    minutes = max(25, min(180, minutes))
    rounded = int(round(minutes / 5) * 5)
    return max(25, min(180, rounded))


def _normalize_watchouts(value) -> list[str]:
    if not isinstance(value, list):
        return []
    watchouts = []
    for item in value:
        cleaned = str(item).strip()
        if cleaned:
            watchouts.append(cleaned[:240])
        if len(watchouts) == 4:
            break
    return watchouts


def local_today_str() -> str:
    return local_now().strftime("%Y-%m-%d")


def _normalize_clock_string(value, fallback: str | None = None) -> str | None:
    if value is None:
        return fallback
    cleaned = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%H:%M")
        except (TypeError, ValueError):
            continue
    return fallback


def analyze_task(title: str, description: str, history: list = None,
                 pending: list = None, schedule: list = None,
                 user_deadline: str = None) -> dict:
    """Analyze a task using history + schedule aware LLM."""
    history_block, pending_block, schedule_block = _build_context_blocks(history, pending, schedule)
    default_deadline = (local_now() + timedelta(days=7)).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

    try:
        prompt = TASK_ANALYSIS_PROMPT.format(
            history=history_block,
            pending=pending_block,
            schedule=schedule_block,
            user_deadline=user_deadline or "Not specified — suggest one",
            today=local_now().strftime("%Y-%m-%d %H:%M"),
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
        suggested_deadline = (
            _normalize_datetime_string(user_deadline, default_deadline)
            if user_deadline
            else _normalize_datetime_string(result.get("suggested_deadline"), default_deadline)
        )
        estimated_completion = _normalize_datetime_string(
            result.get("estimated_completion_date"),
            suggested_deadline,
        )
        return {
            "questions": _normalize_questions(result.get("questions")),
            "suggested_priority": _normalize_priority(result.get("suggested_priority", 3)),
            "suggested_deadline": suggested_deadline,
            "estimated_completion_date": estimated_completion,
            "deadline_confidence": _normalize_confidence(result.get("deadline_confidence")),
            "suggested_breakdown": _normalize_breakdown(result.get("suggested_breakdown")),
            "reasoning": str(result.get("reasoning", "")),
        }
    except Exception:
        suggested_deadline = _normalize_datetime_string(user_deadline, default_deadline) if user_deadline else default_deadline
        return {
            "questions": [],
            "suggested_priority": 3,
            "suggested_deadline": suggested_deadline,
            "estimated_completion_date": suggested_deadline,
            "deadline_confidence": "medium",
            "suggested_breakdown": [],
            "reasoning": "AI analysis is temporarily unavailable, so default suggestions were used.",
        }


def followup_analyze(title: str, description: str, questions: list, answers: list,
                     history: list = None, pending: list = None,
                     schedule: list = None, user_deadline: str = None) -> dict:
    """Re-analyze task after user answers clarifying questions."""
    history_block, pending_block, schedule_block = _build_context_blocks(history, pending, schedule)
    default_deadline = (local_now() + timedelta(days=7)).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")

    qa_block = "\n".join(
        f"Q: {q}\nA: {a}" for q, a in zip(questions, answers)
    )

    try:
        prompt = FOLLOWUP_PROMPT.format(
            history=history_block,
            pending=pending_block,
            schedule=schedule_block,
            user_deadline=user_deadline or "Not specified",
            today=local_now().strftime("%Y-%m-%d %H:%M"),
        )
        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": (
                    f"Task: {title}\nDescription: {description}\n\n"
                    f"Clarifications:\n{qa_block}"
                )},
            ],
            response_format={"type": "json_object"},
            temperature=0.7,
            max_tokens=400,
        )
        result = json.loads(response.choices[0].message.content)
        suggested_deadline = (
            _normalize_datetime_string(user_deadline, default_deadline)
            if user_deadline
            else _normalize_datetime_string(result.get("suggested_deadline"), default_deadline)
        )
        estimated_completion = _normalize_datetime_string(
            result.get("estimated_completion_date"),
            suggested_deadline,
        )
        return {
            "suggested_priority": _normalize_priority(result.get("suggested_priority", 3)),
            "suggested_deadline": suggested_deadline,
            "estimated_completion_date": estimated_completion,
            "deadline_confidence": _normalize_confidence(result.get("deadline_confidence")),
            "suggested_breakdown": _normalize_breakdown(result.get("suggested_breakdown")),
            "reasoning": str(result.get("reasoning", "")),
        }
    except Exception:
        suggested_deadline = _normalize_datetime_string(user_deadline, default_deadline) if user_deadline else default_deadline
        return {
            "suggested_priority": 3,
            "suggested_deadline": suggested_deadline,
            "estimated_completion_date": suggested_deadline,
            "deadline_confidence": "medium",
            "suggested_breakdown": [],
            "reasoning": "AI refinement is temporarily unavailable, so default suggestions were used.",
        }


def plan_day(tasks: list, schedule: list, free_slots: list, settings: dict,
             history: list = None, planning_mode: str = "initial",
             target_date: str | None = None) -> dict:
    history_block = _build_history_block(history or [])
    tasks_json = json.dumps(tasks or [], default=str)
    schedule_json = json.dumps(schedule or [], default=str)
    free_slots_json = json.dumps(free_slots or [], default=str)
    settings_json = json.dumps(settings or {}, default=str)
    valid_task_ids = {int(item["id"]) for item in tasks or [] if item.get("id") is not None}
    fallback_minutes = settings.get("default_focus_minutes", 50) if settings else 50

    try:
        prompt = DAY_PLAN_PROMPT.format(
            target_date=target_date or local_today_str(),
            current_time=local_now().strftime("%Y-%m-%d %H:%M"),
            planning_mode=planning_mode,
            settings_json=settings_json,
            tasks_json=tasks_json,
            schedule_json=schedule_json,
            free_slots_json=free_slots_json,
            history=history_block,
        )
        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Plan this day realistically."},
            ],
            response_format={"type": "json_object"},
            temperature=0.5,
            max_tokens=700,
        )
        result = json.loads(response.choices[0].message.content)
        recommendations = []
        seen = set()
        for item in result.get("recommendations", []):
            try:
                task_id = int(item.get("task_id"))
            except (TypeError, ValueError):
                continue
            if task_id not in valid_task_ids or task_id in seen:
                continue
            seen.add(task_id)
            recommendations.append({
                "task_id": task_id,
                "start_time": _normalize_clock_string(item.get("start_time")),
                "minutes": _normalize_minutes(item.get("minutes"), fallback_minutes),
                "reason": str(item.get("reason", "")).strip()[:280],
            })
        if not recommendations:
            raise ValueError("No valid day plan recommendations returned.")
        return {
            "summary": str(result.get("summary", "")).strip()[:280],
            "reasoning": str(result.get("reasoning", "")).strip()[:800],
            "recommendations": recommendations[:6],
            "watchouts": _normalize_watchouts(result.get("watchouts")),
        }
    except Exception:
        fallback_recommendations = []
        for item in (tasks or [])[:4]:
            fallback_recommendations.append({
                "task_id": int(item["id"]),
                "start_time": None,
                "minutes": _normalize_minutes(item.get("suggested_minutes"), fallback_minutes),
                "reason": "Selected from the top of your ready queue using priority, deadlines, and planned work.",
            })
        return {
            "summary": "Focus on the most urgent ready tasks and leave space for the rest of the day.",
            "reasoning": "AI day planning is temporarily unavailable, so the app used your current queue order and schedule constraints.",
            "recommendations": fallback_recommendations,
            "watchouts": ["Leave some unscheduled time so delays do not break the whole day."],
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
            today=local_today_str(),
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

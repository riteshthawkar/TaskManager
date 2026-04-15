# TaskManager Roadmap

This roadmap is ordered by user impact and implementation risk. Phase 1 is implemented, and the core of Phase 2 is now in progress in the current worktree.

## Phase 1: Daily Planning Loop

Goal: make the app better at turning tasks into action for the current day.

- In-app toast feedback for all major task, deep-work, and schedule actions.
- Dashboard "Today" panel with top priorities, next free focus slot, and schedule summary.
- Auto-schedule a focus block from a task when the user needs time reserved on the calendar.
- Protect the schedule from overlapping events, including recurring series.

Acceptance criteria:

- Users always get clear success or error feedback after writes.
- Overlapping events are rejected before they are written.
- A user can reserve focused work for a task in one click.
- The dashboard answers "what should I do next?" without extra navigation.

## Phase 2: Task Structure

Goal: support larger and more realistic workloads.

- Projects with grouped tasks. Implemented.
- Tags, start dates, and a "later" queue. Implemented.
- Recurring tasks and habits. Implemented for date-based recurring tasks.
- Subtasks and progress tracking. Pending.
- Richer task states such as blocked or waiting. Pending.

Acceptance criteria:

- Users can break complex work into smaller units.
- Repeated work does not need manual recreation.
- Dashboard and AI logic understand project and subtask context.

## Phase 3: Scheduling Intelligence

Goal: make the calendar and AI behave like a planning assistant instead of a static list.

- Auto-suggest task slots based on deadline, estimate, and open calendar gaps.
- Deadline confidence and feasibility warnings.
- Better task-to-calendar linking and edit flows.
- "Reschedule this week" suggestions when the plan becomes unrealistic.

Acceptance criteria:

- The app can suggest when work should happen, not only when it is due.
- Calendar pressure is visible before deadlines are missed.

## Phase 4: Mobile PWA Polish

Goal: make the app feel strong on a phone without losing desktop quality.

- Bottom navigation on mobile.
- Better standalone-PWA affordances and install onboarding.
- Push notifications for reminders.
- Faster page transitions and more resilient offline shell behavior.

Acceptance criteria:

- Common daily flows are one-thumb friendly.
- The installed app behaves consistently on iPhone and Android.

## Phase 5: Review and Insight Layer

Goal: help users understand their work patterns and improve them over time.

- Weekly review page.
- Deep-work and on-time completion trends.
- AI coaching based on recent outcomes.
- Better explanations for deadline and priority recommendations.

Acceptance criteria:

- Users can tell what is improving, what is slipping, and why.
- AI suggestions are easier to trust because the reasoning is visible.

import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv

from app.time_utils import APP_TIMEZONE, APP_TIMEZONE_NAME

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./taskmanager.db")

# Render uses "postgres://" but SQLAlchemy needs "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")
is_postgres = DATABASE_URL.startswith("postgresql://")

connect_args = {"check_same_thread": False} if is_sqlite else {}
engine_kwargs = {"connect_args": connect_args}

if is_postgres:
    # Render/Postgres can drop idle SSL connections. Pre-ping and recycling
    # prevent stale pooled connections from taking down the next request.
    engine_kwargs.update({
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_use_lifo": True,
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def _datetime_sql_type(dialect: str) -> str:
    """Return a compatible SQL type name for raw ALTER TABLE migrations."""
    return "TIMESTAMP" if dialect == "postgresql" else "DATETIME"


def _date_sql_type(dialect: str) -> str:
    return "DATE"


def _normalize_db_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    raise ValueError(f"Unsupported datetime value: {value!r}")


def _create_metadata_table(connection) -> None:
    connection.execute(text(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            key VARCHAR(100) PRIMARY KEY,
            value VARCHAR(500)
        )
        """
    ))


def _get_metadata_value(connection, key: str) -> str | None:
    row = connection.execute(
        text("SELECT value FROM app_metadata WHERE key = :key"),
        {"key": key},
    ).first()
    return row[0] if row else None


def _set_metadata_value(connection, key: str, value: str) -> None:
    connection.execute(text("DELETE FROM app_metadata WHERE key = :key"), {"key": key})
    connection.execute(
        text("INSERT INTO app_metadata (key, value) VALUES (:key, :value)"),
        {"key": key, "value": value},
    )


def _local_naive_to_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    local_value = value.replace(tzinfo=APP_TIMEZONE)
    return local_value.astimezone(timezone.utc).replace(tzinfo=None)


def _migrate_task_datetimes_to_utc(connection) -> int:
    migration_key = "task_datetime_storage_v1"
    if _get_metadata_value(connection, migration_key):
        return 0

    rows = connection.execute(text(
        "SELECT id, deadline, estimated_completion FROM tasks "
        "WHERE deadline IS NOT NULL OR estimated_completion IS NOT NULL"
    )).all()

    updated = 0
    for row in rows:
        deadline = _normalize_db_datetime(row[1])
        estimated_completion = _normalize_db_datetime(row[2])
        converted_deadline = _local_naive_to_utc_naive(deadline)
        converted_estimated = _local_naive_to_utc_naive(estimated_completion)
        if converted_deadline != deadline or converted_estimated != estimated_completion:
            connection.execute(
                text(
                    "UPDATE tasks SET deadline = :deadline, estimated_completion = :estimated_completion "
                    "WHERE id = :task_id"
                ),
                {
                    "deadline": converted_deadline,
                    "estimated_completion": converted_estimated,
                    "task_id": row[0],
                },
            )
            updated += 1

    _set_metadata_value(connection, migration_key, APP_TIMEZONE_NAME)
    return updated


def ensure_schema_compatibility() -> list[str]:
    """Create tables and add any columns needed by newer app versions."""
    Base.metadata.create_all(bind=engine)

    applied_migrations: list[str] = []
    dialect = engine.dialect.name

    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        _create_metadata_table(connection)

        if "tasks" in existing_tables:
            task_columns = {column["name"] for column in inspector.get_columns("tasks")}
            if "estimated_completion" not in task_columns:
                datetime_type = _datetime_sql_type(dialect)
                connection.execute(text(
                    f"ALTER TABLE tasks ADD COLUMN estimated_completion {datetime_type}"
                ))
                applied_migrations.append("tasks.estimated_completion")
                task_columns.add("estimated_completion")

            if "project_id" not in task_columns:
                connection.execute(text(
                    "ALTER TABLE tasks ADD COLUMN project_id INTEGER"
                ))
                applied_migrations.append("tasks.project_id")
                task_columns.add("project_id")

            if "tags_text" not in task_columns:
                connection.execute(text(
                    "ALTER TABLE tasks ADD COLUMN tags_text TEXT NOT NULL DEFAULT ''"
                ))
                applied_migrations.append("tasks.tags_text")
                task_columns.add("tags_text")

            if "start_on" not in task_columns:
                date_type = _date_sql_type(dialect)
                connection.execute(text(
                    f"ALTER TABLE tasks ADD COLUMN start_on {date_type}"
                ))
                applied_migrations.append("tasks.start_on")
                task_columns.add("start_on")

            if "planned_for_date" not in task_columns:
                date_type = _date_sql_type(dialect)
                connection.execute(text(
                    f"ALTER TABLE tasks ADD COLUMN planned_for_date {date_type}"
                ))
                applied_migrations.append("tasks.planned_for_date")
                task_columns.add("planned_for_date")

            if "repeat" not in task_columns:
                connection.execute(text(
                    "ALTER TABLE tasks ADD COLUMN repeat VARCHAR(20) NOT NULL DEFAULT 'none'"
                ))
                applied_migrations.append("tasks.repeat")
                task_columns.add("repeat")

            if "repeat_until" not in task_columns:
                date_type = _date_sql_type(dialect)
                connection.execute(text(
                    f"ALTER TABLE tasks ADD COLUMN repeat_until {date_type}"
                ))
                applied_migrations.append("tasks.repeat_until")
                task_columns.add("repeat_until")

            if "parent_task_id" not in task_columns:
                connection.execute(text(
                    "ALTER TABLE tasks ADD COLUMN parent_task_id INTEGER"
                ))
                applied_migrations.append("tasks.parent_task_id")
                task_columns.add("parent_task_id")

            if "deadline_confidence" not in task_columns:
                connection.execute(text(
                    "ALTER TABLE tasks ADD COLUMN deadline_confidence VARCHAR(20) NOT NULL DEFAULT 'medium'"
                ))
                applied_migrations.append("tasks.deadline_confidence")
                task_columns.add("deadline_confidence")

            if dialect == "postgresql" and "repeat" in task_columns:
                connection.execute(text(
                    "UPDATE tasks SET repeat = 'none' WHERE repeat IS NULL"
                ))
            connection.execute(text(
                "UPDATE tasks SET deadline_confidence = 'medium' WHERE deadline_confidence IS NULL"
            ))
            connection.execute(text(
                "UPDATE tasks SET status = 'pending' WHERE status IS NULL"
            ))
            connection.execute(text(
                "UPDATE tasks SET status = 'pending' WHERE status NOT IN ('pending', 'in_progress', 'waiting', 'blocked', 'completed')"
            ))

            migrated_rows = _migrate_task_datetimes_to_utc(connection)
            if migrated_rows:
                applied_migrations.append(f"tasks.datetime_utc_storage({migrated_rows})")

        if "subtasks" in existing_tables:
            subtask_columns = {column["name"] for column in inspector.get_columns("subtasks")}
            if "status" in subtask_columns:
                connection.execute(text(
                    "UPDATE subtasks SET status = 'pending' WHERE status IS NULL"
                ))
                connection.execute(text(
                    "UPDATE subtasks SET status = 'pending' WHERE status NOT IN ('pending', 'completed')"
                ))

        if "events" in existing_tables:
            event_columns = {column["name"] for column in inspector.get_columns("events")}

            if "repeat" not in event_columns:
                connection.execute(text(
                    "ALTER TABLE events ADD COLUMN repeat VARCHAR(20) NOT NULL DEFAULT 'none'"
                ))
                applied_migrations.append("events.repeat")

            if "repeat_until" not in event_columns:
                connection.execute(text(
                    "ALTER TABLE events ADD COLUMN repeat_until DATE"
                ))
                applied_migrations.append("events.repeat_until")

            if "parent_event_id" not in event_columns:
                connection.execute(text(
                    "ALTER TABLE events ADD COLUMN parent_event_id INTEGER"
                ))
                applied_migrations.append("events.parent_event_id")
                event_columns.add("parent_event_id")

            if "planner_source" not in event_columns:
                connection.execute(text(
                    "ALTER TABLE events ADD COLUMN planner_source VARCHAR(30) NOT NULL DEFAULT ''"
                ))
                applied_migrations.append("events.planner_source")
                event_columns.add("planner_source")

            if "planner_plan_id" not in event_columns:
                connection.execute(text(
                    "ALTER TABLE events ADD COLUMN planner_plan_id INTEGER"
                ))
                applied_migrations.append("events.planner_plan_id")
                event_columns.add("planner_plan_id")

            if "planner_block_id" not in event_columns:
                connection.execute(text(
                    "ALTER TABLE events ADD COLUMN planner_block_id INTEGER"
                ))
                applied_migrations.append("events.planner_block_id")
                event_columns.add("planner_block_id")

            if dialect == "postgresql" and "repeat" in event_columns:
                connection.execute(text(
                    "UPDATE events SET repeat = 'none' WHERE repeat IS NULL"
                ))
            if "planner_source" in event_columns:
                connection.execute(text(
                    "UPDATE events SET planner_source = '' WHERE planner_source IS NULL"
                ))

    return applied_migrations


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

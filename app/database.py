import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv

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


def ensure_schema_compatibility() -> list[str]:
    """Create tables and add any columns needed by newer app versions."""
    Base.metadata.create_all(bind=engine)

    applied_migrations: list[str] = []
    dialect = engine.dialect.name

    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())

        if "tasks" in existing_tables:
            task_columns = {column["name"] for column in inspector.get_columns("tasks")}
            if "estimated_completion" not in task_columns:
                datetime_type = _datetime_sql_type(dialect)
                connection.execute(text(
                    f"ALTER TABLE tasks ADD COLUMN estimated_completion {datetime_type}"
                ))
                applied_migrations.append("tasks.estimated_completion")

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

            if dialect == "postgresql" and "repeat" in event_columns:
                connection.execute(text(
                    "UPDATE events SET repeat = 'none' WHERE repeat IS NULL"
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

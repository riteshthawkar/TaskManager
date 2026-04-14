from app.database import ensure_schema_compatibility
from app.notifications import check_and_send_notifications


def main() -> None:
    ensure_schema_compatibility()
    check_and_send_notifications()


if __name__ == "__main__":
    main()

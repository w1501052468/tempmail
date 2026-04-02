import os

from ..db import run_startup_migrations
from ..services.cleanup_service import run_cleanup


def _should_run_startup_migrations() -> bool:
    value = os.getenv("TEMPMAIL_SKIP_STARTUP_MIGRATIONS", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def main() -> int:
    if _should_run_startup_migrations():
        run_startup_migrations()
    result = run_cleanup()
    print(
        f"Cleanup complete: purged_mailboxes={result['purged_mailboxes']} deleted_files={result['deleted_files']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

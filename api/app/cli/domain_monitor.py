import os
import sys
import time
import argparse

from ..db import get_connection, run_startup_migrations
from ..runtime_config import load_runtime_config
from ..services.domain_service import run_domain_checks


def _should_run_startup_migrations() -> bool:
    value = os.getenv("TEMPMAIL_SKIP_STARTUP_MIGRATIONS", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def run_once() -> int:
    if _should_run_startup_migrations():
        run_startup_migrations()
    with get_connection() as conn:
        results = run_domain_checks(conn)
    print(f"Domain monitor complete: checked={len(results)}")
    return 0


def run_forever() -> int:
    if _should_run_startup_migrations():
        run_startup_migrations()

    while True:
        sleep_seconds = 30
        try:
            with get_connection() as conn:
                results = run_domain_checks(conn)
                runtime_config = load_runtime_config(conn, force_refresh=True)
                sleep_seconds = runtime_config.domain_monitor_loop_seconds
            print(f"Domain monitor complete: checked={len(results)}")
        except Exception as exc:
            print(f"Domain monitor iteration failed: {exc}", file=sys.stderr)
        time.sleep(max(5, int(sleep_seconds)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify managed domains and update their status")
    parser.add_argument("--loop", action="store_true", help="Run continuously and reload timing from runtime config")
    args = parser.parse_args()
    if args.loop:
        return run_forever()
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())

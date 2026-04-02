import argparse
import os
import sys

from ..config import get_settings
from ..errors import PermanentDeliveryError, TemporaryDeliveryError
from ..db import run_startup_migrations
from ..services.ingest_service import ingest_message
from ..storage import ensure_storage_dirs


def _should_run_startup_migrations() -> bool:
    value = os.getenv("TEMPMAIL_SKIP_STARTUP_MIGRATIONS", "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist an inbound email from Postfix")
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--sender", default=None)
    parser.add_argument("--client-address", default=None)
    parser.add_argument("--helo-name", default=None)
    args = parser.parse_args()

    if _should_run_startup_migrations():
        run_startup_migrations()
    ensure_storage_dirs()
    raw_message = sys.stdin.buffer.read()
    if not raw_message:
        print("Inbound message was empty", file=sys.stderr)
        return os.EX_DATAERR

    settings = get_settings()
    print(
        f"Ingest start recipient={args.recipient} sender={args.sender or '-'} base_domains={','.join(settings.base_domains)} bytes={len(raw_message)}",
        file=sys.stderr,
    )

    try:
        result = ingest_message(
            raw_message=raw_message,
            recipient=args.recipient,
            sender=args.sender,
            client_address=args.client_address,
            helo_name=args.helo_name,
        )
        if result.get("status") == "discarded":
            print(
                f"Discarded inbound message for {result['recipient']}",
                file=sys.stderr,
            )
        else:
            print(
                f"Stored inbound message {result['message_id']} for {result['recipient']}",
                file=sys.stderr,
            )
        return os.EX_OK
    except PermanentDeliveryError as exc:
        print(str(exc), file=sys.stderr)
        return os.EX_NOUSER
    except TemporaryDeliveryError as exc:
        print(str(exc), file=sys.stderr)
        return os.EX_TEMPFAIL


if __name__ == "__main__":
    raise SystemExit(main())

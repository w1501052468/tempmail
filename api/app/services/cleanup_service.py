from ..db import get_connection
from ..runtime_config import load_runtime_config
from ..storage import remove_relative_path
from .system_event_service import emit_system_event


def run_cleanup() -> dict:
    purged_mailboxes = 0
    deleted_files = 0
    delete_failures: list[dict[str, str]] = []

    with get_connection() as conn:
        settings = load_runtime_config(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mailboxes
                SET status = 'expired'
                WHERE status = 'active'
                  AND expires_at <= NOW()
                """
            )

            mailbox_rows: list[dict] = []
            cur.execute(
                """
                SELECT id, address
                FROM mailboxes
                WHERE status IN ('expired', 'disabled')
                  AND COALESCE(disabled_at, expires_at) <= NOW() - make_interval(mins => %s)
                ORDER BY COALESCE(disabled_at, expires_at) ASC
                LIMIT %s
                """,
                (settings.purge_grace_minutes, settings.cleanup_batch_size),
            )
            mailbox_rows = list(cur.fetchall())
            mailbox_ids = [row["id"] for row in mailbox_rows]

            if mailbox_ids:
                cur.execute(
                    """
                    SELECT relative_path
                    FROM (
                      SELECT raw_path AS relative_path
                      FROM messages
                      WHERE mailbox_id = ANY(%s)
                      UNION ALL
                      SELECT a.storage_path AS relative_path
                      FROM attachments a
                      JOIN messages m ON m.id = a.message_id
                      WHERE m.mailbox_id = ANY(%s)
                    ) paths
                    WHERE relative_path IS NOT NULL
                      AND relative_path <> ''
                    """,
                    (mailbox_ids, mailbox_ids),
                )
                storage_paths = list(dict.fromkeys(row["relative_path"] for row in cur.fetchall()))

                cur.execute(
                    """
                    DELETE FROM mailboxes
                    WHERE id = ANY(%s)
                    """,
                    (mailbox_ids,),
                )
                purged_mailboxes = cur.rowcount

                for relative_path in storage_paths:
                    try:
                        remove_relative_path(relative_path)
                        deleted_files += 1
                    except Exception as exc:
                        delete_failures.append(
                            {
                                "path": relative_path,
                                "reason": str(exc) or exc.__class__.__name__,
                            }
                        )

            cur.execute(
                """
                DELETE FROM access_events
                WHERE created_at < NOW() - make_interval(days => %s)
                """,
                (settings.access_event_retention_days,),
            )
            cur.execute(
                """
                DELETE FROM system_events
                WHERE created_at < NOW() - make_interval(days => %s)
                """,
                (settings.access_event_retention_days,),
            )

        if purged_mailboxes:
            emit_system_event(
                conn,
                event_type="mailbox_purged",
                source="janitor",
                summary=f"Purged {purged_mailboxes} expired/disabled mailboxes",
                payload={
                    "mailboxes": [row["address"] for row in mailbox_rows],
                    "deleted_files": deleted_files,
                    "delete_failures": len(delete_failures),
                },
            )

    return {
        "purged_mailboxes": purged_mailboxes,
        "deleted_files": deleted_files,
        "delete_failures": len(delete_failures),
    }

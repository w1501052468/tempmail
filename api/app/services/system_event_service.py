from psycopg.types.json import Json


def emit_system_event(
    conn,
    *,
    event_type: str,
    source: str,
    summary: str,
    level: str = "info",
    mailbox_id=None,
    message_id=None,
    address: str | None = None,
    payload: dict | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO system_events (
                event_type,
                level,
                source,
                mailbox_id,
                message_id,
                address,
                summary,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_type,
                level,
                source,
                mailbox_id,
                message_id,
                address,
                summary,
                Json(payload or {}),
            ),
        )


def list_system_events(
    conn,
    *,
    limit: int = 100,
    after_id: int | None = None,
    event_type: str | None = None,
    source: str | None = None,
) -> list[dict]:
    bounded_limit = max(1, min(limit, 500))
    clauses = ["TRUE"]
    params: list[object] = []

    if after_id is not None:
        clauses.append("id > %s")
        params.append(max(0, int(after_id)))
    if event_type:
        clauses.append("event_type = %s")
        params.append(event_type.strip())
    if source:
        clauses.append("source = %s")
        params.append(source.strip())

    order_by = "id ASC" if after_id is not None else "id DESC"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              id,
              event_type,
              level,
              source,
              mailbox_id,
              message_id,
              address,
              summary,
              payload,
              created_at
            FROM system_events
            WHERE {' AND '.join(clauses)}
            ORDER BY {order_by}
            LIMIT %s
            """,
            [*params, bounded_limit],
        )
        return list(cur.fetchall())

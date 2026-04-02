from datetime import datetime, timezone
import hashlib
import os
from uuid import uuid4

from psycopg.types.json import Json

from ..db import get_connection
from ..errors import PermanentDeliveryError, TemporaryDeliveryError
from ..mail_parser import parse_raw_email
from ..runtime_config import load_runtime_config
from ..storage import sanitize_filename, write_bytes
from .policy_service import evaluate_domain_policies
from .system_event_service import emit_system_event


def ingest_message(
    *,
    raw_message: bytes,
    recipient: str,
    sender: str | None,
    client_address: str | None,
    helo_name: str | None,
) -> dict:
    normalized_recipient = recipient.strip().lower()
    if not normalized_recipient:
        raise PermanentDeliveryError("Recipient was empty")
    raw_sha256 = hashlib.sha256(raw_message).hexdigest()
    received_at = datetime.now(timezone.utc)

    with get_connection() as conn:
        settings = load_runtime_config(conn)
        if len(raw_message) > settings.message_size_limit_bytes:
            raise PermanentDeliveryError("Message exceeded size limit")

        policy_decision = evaluate_domain_policies(conn, recipient=normalized_recipient, sender=sender)
        if not policy_decision.recipient_base_domain:
            emit_system_event(
                conn,
                event_type="smtp_rejected",
                source="policy",
                level="warning",
                address=normalized_recipient,
                summary=f"Rejected inbound recipient outside active managed base domains: {normalized_recipient}",
                payload={"recipient": normalized_recipient, "sender": sender, "client_ip": client_address},
            )
            raise PermanentDeliveryError("Recipient base domain is not allowed")

        if policy_decision.matched and policy_decision.policy:
            policy = policy_decision.policy
            policy_payload = {
                "policy_id": str(policy["id"]),
                "scope": policy["scope"],
                "pattern": policy["pattern"],
                "action": policy["action"],
                "recipient_base_domain": policy_decision.recipient_base_domain,
                "sender_domain": policy_decision.sender_domain,
                "sender": sender,
                "recipient": normalized_recipient,
                "client_ip": client_address,
            }
            if policy_decision.action == "reject":
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO access_events (action, ip, mailbox_id, metadata)
                        VALUES (%s, %s, NULL, %s)
                        """,
                        ("message_rejected", client_address, Json(policy_payload)),
                    )
                emit_system_event(
                    conn,
                    event_type="smtp_rejected",
                    source="policy",
                    level="warning",
                    address=normalized_recipient,
                    summary=f"Rejected inbound message for {normalized_recipient}",
                    payload=policy_payload,
                )
                raise PermanentDeliveryError(
                    f"Recipient rejected by policy {policy['scope']}:{policy['pattern']}"
                )
            if policy_decision.action == "discard":
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO access_events (action, ip, mailbox_id, metadata)
                        VALUES (%s, %s, NULL, %s)
                        """,
                        ("message_discarded", client_address, Json(policy_payload)),
                    )
                emit_system_event(
                    conn,
                    event_type="smtp_discarded",
                    source="policy",
                    address=normalized_recipient,
                    summary=f"Discarded inbound message for {normalized_recipient}",
                    payload=policy_payload,
                )
                return {
                    "recipient": normalized_recipient,
                    "status": "discarded",
                    "message_id": None,
                    "mailbox_id": None,
                }

        parsed = parse_raw_email(raw_message, settings)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, address
                FROM mailboxes
                WHERE lower(address) = lower(%s)
                  AND status = 'active'
                  AND expires_at > NOW()
                LIMIT 1
                """,
                (normalized_recipient,),
            )
            mailbox = cur.fetchone()
            if not mailbox:
                raise PermanentDeliveryError("Recipient mailbox does not exist or already expired")

        message_id = uuid4()
        raw_relative_path = os.path.join(
            "raw",
            received_at.strftime("%Y"),
            received_at.strftime("%m"),
            received_at.strftime("%d"),
            str(mailbox["id"]),
            f"{message_id}.eml",
        ).replace("\\", "/")

        written_paths: list[str] = []
        try:
            write_bytes(raw_relative_path, raw_message)
            written_paths.append(raw_relative_path)

            attachment_rows: list[tuple] = []
            for attachment in parsed.attachments:
                attachment_id = uuid4()
                attachment_name = sanitize_filename(attachment.filename, "attachment.bin")
                attachment_relative_path = os.path.join(
                    "attachments",
                    received_at.strftime("%Y"),
                    received_at.strftime("%m"),
                    received_at.strftime("%d"),
                    str(message_id),
                    f"{attachment_id}-{attachment_name}",
                ).replace("\\", "/")
                write_bytes(attachment_relative_path, attachment.payload)
                written_paths.append(attachment_relative_path)
                attachment_rows.append(
                    (
                        attachment_id,
                        message_id,
                        attachment.filename,
                        attachment.content_type,
                        attachment.size_bytes,
                        attachment.sha256,
                        attachment_relative_path,
                    )
                )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO messages (
                        id,
                        mailbox_id,
                        envelope_from,
                        envelope_to,
                        helo_name,
                        client_address,
                        subject,
                        message_id,
                        date_header,
                        from_header,
                        to_header,
                        reply_to,
                        text_body,
                        html_body,
                        headers_json,
                        raw_path,
                        raw_sha256,
                        size_bytes,
                        attachment_count,
                        received_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        message_id,
                        mailbox["id"],
                        sender,
                        mailbox["address"],
                        helo_name,
                        client_address,
                        parsed.subject,
                        parsed.message_id,
                        parsed.date_header,
                        parsed.from_header,
                        parsed.to_header,
                        parsed.reply_to,
                        parsed.text_body,
                        parsed.html_body,
                        Json(parsed.headers_json),
                        raw_relative_path,
                        raw_sha256,
                        len(raw_message),
                        len(attachment_rows),
                        received_at,
                    ),
                )

                if attachment_rows:
                    cur.executemany(
                        """
                        INSERT INTO attachments (
                            id,
                            message_id,
                            filename,
                            content_type,
                            size_bytes,
                            sha256,
                            storage_path
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        attachment_rows,
                    )

                cur.execute(
                    """
                    INSERT INTO access_events (action, ip, mailbox_id, metadata)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        "message_received",
                        client_address,
                        mailbox["id"],
                        Json({"recipient": mailbox["address"], "message_id": str(message_id)}),
                    ),
                )
                emit_system_event(
                    conn,
                    event_type="message_received",
                    source="smtp",
                    mailbox_id=mailbox["id"],
                    message_id=message_id,
                    address=mailbox["address"],
                    summary=f"Received message for {mailbox['address']}",
                    payload={
                        "client_ip": client_address,
                        "helo_name": helo_name,
                        "sender": sender,
                        "subject": parsed.subject,
                        "attachment_count": len(attachment_rows),
                        "size_bytes": len(raw_message),
                    },
                )

            return {
                "mailbox_id": str(mailbox["id"]),
                "message_id": str(message_id),
                "recipient": mailbox["address"],
                "status": "stored",
            }
        except PermanentDeliveryError:
            raise
        except Exception as exc:
            from ..storage import remove_relative_path

            for relative_path in reversed(written_paths):
                try:
                    remove_relative_path(relative_path)
                except Exception:
                    pass
            raise TemporaryDeliveryError(f"Failed to persist inbound message: {exc}") from exc

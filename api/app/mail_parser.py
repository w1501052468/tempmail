from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import html as html_lib
import hashlib
import re
from typing import Any

from .config import get_settings


@dataclass
class ParsedAttachment:
    filename: str | None
    content_type: str
    size_bytes: int
    sha256: str
    payload: bytes


@dataclass
class ParsedMessage:
    subject: str | None
    message_id: str | None
    from_header: str | None
    to_header: str | None
    reply_to: str | None
    date_header: Any
    text_body: str | None
    html_body: str | None
    headers_json: dict[str, Any]
    attachments: list[ParsedAttachment]


TEXT_BODY_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
}

SKIPPED_TEXT_CONTENT_TYPES = {
    "text/html",
    "text/calendar",
    "text/vcard",
    "text/x-vcard",
}


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _normalize_body(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if not normalized.strip():
        return None
    return normalized.strip()


def _text_candidate_rank(content_type: str) -> int:
    if content_type == "text/plain":
        return 30
    if content_type in TEXT_BODY_CONTENT_TYPES:
        return 20
    if content_type.startswith("text/") and content_type not in SKIPPED_TEXT_CONTENT_TYPES:
        return 10
    return 0


def _html_to_text(value: str) -> str | None:
    cleaned = re.sub(r"(?is)<(script|style|head|title|meta|link|noscript).*?>.*?</\1>", " ", value)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(p|div|section|article|blockquote|h[1-6]|ul|ol|table|tr)>", "\n", cleaned)
    cleaned = re.sub(r"(?i)<li\b[^>]*>", "- ", cleaned)
    cleaned = re.sub(r"(?i)<td\b[^>]*>", " ", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html_lib.unescape(cleaned).replace("\xa0", " ")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return _normalize_body(cleaned)


def parse_raw_email(raw_message: bytes, runtime_config=None) -> ParsedMessage:
    settings = runtime_config or get_settings()
    message = BytesParser(policy=policy.default).parsebytes(raw_message)

    text_body: str | None = None
    text_body_rank = -1
    html_body: str | None = None
    attachments: list[ParsedAttachment] = []

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""

        if disposition == "attachment" or filename:
            attachments.append(
                ParsedAttachment(
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    payload=payload,
                )
            )
            continue

        decoded = _normalize_body(_decode_part(part))
        if not decoded:
            continue

        if content_type == "text/html":
            candidate = decoded[: settings.max_html_body_chars]
            if html_body is None or len(candidate) > len(html_body):
                html_body = candidate
            continue

        candidate_rank = _text_candidate_rank(content_type)
        if candidate_rank:
            candidate = decoded[: settings.max_text_body_chars]
            if candidate_rank > text_body_rank or (
                candidate_rank == text_body_rank and len(candidate) > len(text_body or "")
            ):
                text_body = candidate
                text_body_rank = candidate_rank

    if not text_body and not html_body and not message.is_multipart():
        decoded = _normalize_body(_decode_part(message))
        if decoded:
            text_body = decoded[: settings.max_text_body_chars]

    if not text_body and html_body:
        fallback_text = _html_to_text(html_body)
        if fallback_text:
            text_body = fallback_text[: settings.max_text_body_chars]

    headers: dict[str, Any] = {}
    for key in message.keys():
        values = message.get_all(key, [])
        headers[key] = values[0] if len(values) == 1 else values

    date_header = None
    raw_date = message.get("Date")
    if raw_date:
        try:
            date_header = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError, IndexError):
            date_header = None

    return ParsedMessage(
        subject=message.get("Subject"),
        message_id=message.get("Message-ID"),
        from_header=message.get("From"),
        to_header=message.get("To"),
        reply_to=message.get("Reply-To"),
        date_header=date_header,
        text_body=text_body,
        html_body=html_body,
        headers_json=headers,
        attachments=attachments[: settings.max_attachments_per_message],
    )

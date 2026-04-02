from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateMailboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str | None = Field(default=None, description="Optional full mailbox address")
    domain: str | None = Field(default=None, description="Optional base domain")
    ttl_minutes: int | None = Field(default=None, ge=1, le=10080)


class MailboxCreateResponse(BaseModel):
    address: str
    token: str
    expires_at: datetime
    created_at: datetime
    list_messages_url: str
    message_detail_url_template: str


class MailboxDisableResponse(BaseModel):
    address: str
    status: str
    disabled_at: datetime


class AttachmentInfo(BaseModel):
    id: UUID
    filename: str | None
    content_type: str
    size_bytes: int
    download_url: str


class MessageSummary(BaseModel):
    id: UUID
    from_header: str | None
    subject: str | None
    received_at: datetime
    size_bytes: int
    attachment_count: int
    preview: str | None


class InboxListResponse(BaseModel):
    mailbox_address: str
    expires_at: datetime
    items: list[MessageSummary]


class MessageDetailResponse(BaseModel):
    id: UUID
    mailbox_address: str
    envelope_from: str | None
    envelope_to: str
    subject: str | None
    message_id: str | None
    from_header: str | None
    to_header: str | None
    reply_to: str | None
    date_header: datetime | None
    received_at: datetime
    size_bytes: int | None = None
    text_body: str | None
    html_body: str | None
    headers_json: dict[str, Any]
    raw_url: str
    attachments: list[AttachmentInfo]


class MessageDeleteResponse(BaseModel):
    id: UUID
    mailbox_address: str
    subject: str | None
    attachment_count: int
    deleted_files: int


class MailboxPurgeResponse(BaseModel):
    mailbox_address: str
    deleted_messages: int
    deleted_attachments: int
    deleted_files: int


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminLoginResponse(BaseModel):
    username: str
    expires_in_hours: int


class AdminMailboxRecord(BaseModel):
    id: UUID
    address: str
    status: str
    created_at: datetime
    expires_at: datetime
    disabled_at: datetime | None = None
    last_accessed_at: datetime | None = None
    message_count: int | None = None


class AdminMailboxListResponse(BaseModel):
    total: int
    items: list[AdminMailboxRecord]


class AdminMessageRecord(BaseModel):
    id: UUID
    mailbox_id: UUID
    mailbox_address: str
    subject: str | None = None
    from_header: str | None = None
    received_at: datetime
    size_bytes: int
    attachment_count: int = 0


class AdminMessageListResponse(BaseModel):
    total: int
    items: list[AdminMessageRecord]


class AdminRuntimeConfigResponse(BaseModel):
    runtime: dict[str, Any]
    deployment: dict[str, Any]


class AdminSystemEventRecord(BaseModel):
    id: int
    event_type: str
    level: str
    source: str
    mailbox_id: UUID | None = None
    message_id: UUID | None = None
    address: str | None = None
    summary: str
    payload: dict[str, Any]
    created_at: datetime


class AdminSystemEventListResponse(BaseModel):
    items: list[AdminSystemEventRecord]


class DomainPolicyRecord(BaseModel):
    id: UUID
    scope: str
    pattern: str
    action: str
    priority: int
    status: str
    note: str | None = None
    match_count: int = 0
    last_matched_at: datetime | None = None
    updated_by: str | None = None
    created_at: datetime
    updated_at: datetime


class AdminDomainPolicyListResponse(BaseModel):
    total: int
    items: list[DomainPolicyRecord]


class DomainPolicyCreateRequest(BaseModel):
    scope: str
    pattern: str
    action: str
    priority: int = Field(default=100, ge=0, le=100000)
    status: str = "active"
    note: str | None = None


class DomainPolicyUpdateRequest(BaseModel):
    scope: str
    pattern: str
    action: str
    priority: int = Field(default=100, ge=0, le=100000)
    status: str = "active"
    note: str | None = None


class ManagedDomainRecord(BaseModel):
    id: UUID
    domain: str
    status: str
    source: str
    note: str | None = None
    expected_mx_host: str | None = None
    failure_count: int = 0
    last_error: str | None = None
    root_mx_hosts: list[str] = Field(default_factory=list)
    wildcard_mx_hosts: list[str] = Field(default_factory=list)
    last_checked_at: datetime | None = None
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    updated_by: str | None = None


class AdminManagedDomainListResponse(BaseModel):
    total: int
    items: list[ManagedDomainRecord]


class ManagedDomainCreateRequest(BaseModel):
    domain: str
    note: str | None = None

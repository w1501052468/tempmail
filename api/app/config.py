from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Temp Mail Receiver"
    root_domain: str | None = None
    web_hostname: str | None = Field(default=None, validation_alias="WEB_HOSTNAME")
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    trust_proxy_headers: bool = False

    database_dsn: str = "postgresql://tempmail:tempmail@db:5432/tempmail"
    data_dir: str = "/data"

    base_domains_csv: str = Field(default="", validation_alias="BASE_DOMAINS")
    default_base_domain: str | None = None

    token_prefix: str = "tm_"
    token_bytes: int = 32
    app_token_hash_secret: str = "change-me"
    admin_username: str = "admin"
    admin_password: str = "change-this-admin-password"
    admin_session_secret: str | None = None
    admin_session_hours: int = 12

    mailbox_default_ttl_minutes: int = 60
    mailbox_min_ttl_minutes: int = 10
    mailbox_max_ttl_minutes: int = 1440
    mailbox_local_part_length: int = 10
    mailbox_subdomain_length: int = 8
    mailbox_local_part_min_length: int | None = Field(default=None, validation_alias="MAILBOX_LOCAL_PART_MIN_LENGTH")
    mailbox_local_part_max_length: int | None = Field(default=None, validation_alias="MAILBOX_LOCAL_PART_MAX_LENGTH")
    mailbox_subdomain_min_length: int | None = Field(default=None, validation_alias="MAILBOX_SUBDOMAIN_MIN_LENGTH")
    mailbox_subdomain_max_length: int | None = Field(default=None, validation_alias="MAILBOX_SUBDOMAIN_MAX_LENGTH")

    create_rate_limit_count: int = 30
    create_rate_limit_window_seconds: int = 3600
    inbox_rate_limit_count: int = 600
    inbox_rate_limit_window_seconds: int = 600

    message_size_limit_bytes: int = 10 * 1024 * 1024
    max_text_body_chars: int = 200000
    max_html_body_chars: int = 200000
    max_attachments_per_message: int = 20

    purge_grace_minutes: int = 60
    access_event_retention_days: int = 7
    cleanup_batch_size: int = 200

    smtp_hostname: str | None = Field(default=None, validation_alias="SMTP_HOSTNAME")
    postfix_hostname: str | None = None
    domain_monitor_loop_seconds: int = 30
    domain_verify_pending_interval_seconds: int = 30
    domain_verify_active_interval_seconds: int = 6 * 3600
    domain_verify_disabled_interval_seconds: int = 1800
    domain_verify_failure_threshold: int = 2
    domain_dns_timeout_seconds: float = 5.0
    domain_dns_resolvers_csv: str = Field(default="", validation_alias="DOMAIN_DNS_RESOLVERS")

    @field_validator("base_domains_csv", mode="before")
    @classmethod
    def normalize_base_domains_csv(cls, value: object) -> str:
        if isinstance(value, list):
            items = [str(item).strip().lower() for item in value if str(item).strip()]
            return ",".join(items)
        normalized = str(value or "").strip().lower()
        return normalized

    @field_validator("default_base_domain", mode="before")
    @classmethod
    def normalize_default_base_domain(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None

    @field_validator("domain_dns_resolvers_csv", mode="before")
    @classmethod
    def normalize_domain_dns_resolvers_csv(cls, value: object) -> str:
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return ",".join(items)
        return str(value or "").strip()

    @field_validator(
        "mailbox_local_part_length",
        "mailbox_subdomain_length",
        "mailbox_local_part_min_length",
        "mailbox_local_part_max_length",
        "mailbox_subdomain_min_length",
        "mailbox_subdomain_max_length",
        mode="before",
    )
    @classmethod
    def normalize_optional_positive_int(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        normalized = int(value)
        if normalized <= 0:
            raise ValueError("Mailbox length config must be positive")
        return normalized

    @field_validator("root_domain", "web_hostname", "smtp_hostname", "postfix_hostname", mode="before")
    @classmethod
    def normalize_optional_hostname(cls, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized or None

    @property
    def base_domains(self) -> list[str]:
        domains = [
            item.strip().lower()
            for item in self.base_domains_csv.split(",")
            if item.strip()
        ]
        return domains

    @property
    def domain_dns_resolvers(self) -> list[str]:
        return [
            item.strip()
            for item in self.domain_dns_resolvers_csv.split(",")
            if item.strip()
        ]

    @property
    def effective_mailbox_local_part_min_length(self) -> int:
        return self.mailbox_local_part_min_length or self.mailbox_local_part_length

    @property
    def effective_mailbox_local_part_max_length(self) -> int:
        return self.mailbox_local_part_max_length or self.mailbox_local_part_length

    @property
    def effective_mailbox_subdomain_min_length(self) -> int:
        return self.mailbox_subdomain_min_length or self.mailbox_subdomain_length

    @property
    def effective_mailbox_subdomain_max_length(self) -> int:
        return self.mailbox_subdomain_max_length or self.mailbox_subdomain_length


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if not settings.default_base_domain and settings.base_domains:
        settings.default_base_domain = settings.base_domains[0]
    if settings.default_base_domain and settings.base_domains and settings.default_base_domain not in settings.base_domains:
        raise ValueError("DEFAULT_BASE_DOMAIN must exist in BASE_DOMAINS")
    root_domain = (settings.root_domain or "").strip().lower()
    if not settings.web_hostname:
        settings.web_hostname = f"mail.{root_domain}" if root_domain else "mail.example.com"
    effective_smtp_hostname = settings.smtp_hostname or settings.postfix_hostname
    if not effective_smtp_hostname:
        effective_smtp_hostname = f"mx.{root_domain}" if root_domain else "mx.example.com"
    settings.smtp_hostname = effective_smtp_hostname
    settings.postfix_hostname = effective_smtp_hostname
    if not settings.admin_session_secret:
        settings.admin_session_secret = settings.app_token_hash_secret
    return settings

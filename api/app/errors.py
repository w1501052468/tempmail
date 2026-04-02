class RateLimitExceededError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class InvalidDomainError(Exception):
    pass


class InvalidMailboxAddressError(Exception):
    pass


class MailboxConflictError(Exception):
    pass


class MailboxCreationError(Exception):
    pass


class MessageNotFoundError(Exception):
    pass


class PermanentDeliveryError(Exception):
    pass


class TemporaryDeliveryError(Exception):
    pass

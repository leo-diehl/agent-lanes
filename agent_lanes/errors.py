class HandoffError(Exception):
    """Base class for expected handoff runtime failures."""


class ConfigError(HandoffError):
    """Raised when handoff config is invalid."""


class StoreError(HandoffError):
    """Raised when task state cannot transition safely."""


class TimeoutError(HandoffError):
    """Raised when a wait operation times out."""

"""Exception hierarchy. Messages are user-facing (shown in the UI as is)."""


class VpnDeckError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigImportError(VpnDeckError):
    """The picked file or link cannot be imported."""


class TunnelError(VpnDeckError):
    """A tunnel failed to start or stop."""

"""
VPN Deck - Plugin modules
"""

from .binary_manager import BinaryManager
from .config_manager import ConfigManager
from .diagnostics import Diagnostics
from .errors import ConfigImportError, TunnelError, VpnDeckError
from .importer import plan_import, read_import_file
from .models import ConfigKind, ConnectionStatus, ImportPlan, Json, OpResult, StoredConfig, to_rpc
from .network_watch import NetworkWatcher, is_online, read_routes
from .restore import restore_last_active
from .runner import CommandRunner, SubprocessRunner
from .service_manager import ServiceManager
from .settings import USER_EDITABLE, Settings
from .singbox_manager import SingBoxManager
from .status import StatusProbe
from .subscription import SubscriptionConfig, curl_fetcher
from .tunnels import AwgBackend, SingBoxBackend, TunnelBackend, TunnelController

__all__ = [
    "AwgBackend", "BinaryManager", "CommandRunner", "ConfigImportError", "ConfigKind",
    "ConfigManager", "ConnectionStatus", "Diagnostics", "ImportPlan", "Json", "NetworkWatcher",
    "OpResult", "ServiceManager", "Settings", "SingBoxBackend", "SingBoxManager",
    "StatusProbe", "StoredConfig", "SubscriptionConfig", "curl_fetcher", "SubprocessRunner", "TunnelBackend", "TunnelController",
    "TunnelError", "USER_EDITABLE", "VpnDeckError", "is_online", "plan_import",
    "read_import_file", "read_routes", "restore_last_active", "to_rpc",
]

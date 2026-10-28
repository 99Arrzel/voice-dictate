import threading

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import unwrap_msg

# Exported by gnome-extension/extension.js; absent until that extension is loaded (log out and in once).
ADDRESS = DBusAddress(
    "/org/voicedictate/Indicator",
    bus_name="org.voicedictate.Indicator",
    interface="org.voicedictate.Indicator",
)


class Shell:
    def __init__(self):
        self.lock = threading.Lock()
        self.connection = None

    def focused_app(self) -> str | None:
        """WM_CLASS of the focused window, or None when the extension is not available."""
        with self.lock:
            try:
                if self.connection is None:
                    self.connection = open_dbus_connection(bus="SESSION")
                message = new_method_call(ADDRESS, "FocusedApp")
                return unwrap_msg(self.connection.send_and_get_reply(message, timeout=0.5))[0]
            except Exception:
                return None

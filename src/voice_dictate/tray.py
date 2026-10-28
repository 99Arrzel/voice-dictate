"""Top-bar status icon via StatusNotifierItem, shown by Ubuntu's AppIndicator extension.

Never pops anything over the screen: the icon, a short label beside it, and a click-to-open
menu of status lines are the whole UI. If the daemon dies, the icon disappears with it.
"""

import logging
import threading
from pathlib import Path
from queue import Queue

from jeepney import (
    DBusAddress, HeaderFields, MatchRule, MessageType,
    new_error, new_method_call, new_method_return, new_signal,
)
from jeepney.bus_messages import message_bus
from jeepney.io.threading import DBusRouter, open_dbus_connection

log = logging.getLogger("voice-dictate")

ICON_DIR = Path(__file__).parent / "icons"
# Uncoloured, so the shell tints it like Wi-Fi and the other panel icons.
IDLE_ICON = "audio-input-microphone-symbolic"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ITEM_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
WATCHER = DBusAddress("/StatusNotifierWatcher", bus_name="org.kde.StatusNotifierWatcher",
                      interface="org.kde.StatusNotifierWatcher")

INTROSPECT = f"""<node>
  <interface name="{ITEM_IFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <signal name="NewIcon"/>
    <signal name="XAyatanaNewLabel"><arg type="s"/><arg type="s"/></signal>
  </interface>
  <interface name="{MENU_IFACE}">
    <method name="GetLayout">
      <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
      <arg type="u" direction="out"/><arg type="(ia{{sv}}av)" direction="out"/>
    </method>
    <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
  </interface>
</node>"""


def icon_path(name: str) -> str:
    """The host loads absolute paths as plain images, so the SVG's own colour survives."""
    return str(ICON_DIR / f"{name}.svg")


class Tray:
    def __init__(self):
        self.lock = threading.Lock()
        self.icon = icon_path("loading")
        self.label = ""
        self.lines = ["Starting…"]
        self.revision = 1
        self.router = DBusRouter(open_dbus_connection(bus="SESSION"))
        self.calls: Queue = Queue()
        self.router.filter(MatchRule(type="method_call"), queue=self.calls)
        watcher_owner = MatchRule(type="signal", sender=message_bus.bus_name, member="NameOwnerChanged")
        watcher_owner.add_arg_condition(0, WATCHER.bus_name)
        self.router.filter(watcher_owner, queue=self.calls)
        self.router.send_and_get_reply(message_bus.AddMatch(watcher_owner))
        threading.Thread(target=self._serve, daemon=True).start()
        self._register()

    def set(self, icon: str, label: str, lines: list[str]) -> None:
        """`icon` is a name from icons/ or "idle"; `lines` become the click-to-open menu."""
        path = IDLE_ICON if icon == "idle" else icon_path(icon)
        with self.lock:
            changed_icon, self.icon = path != self.icon, path
            changed_label, self.label = label != self.label, label
            changed_menu = lines != self.lines
            if changed_menu:
                self.lines = list(lines)
                self.revision += 1
        try:
            if changed_icon:
                self._emit(ITEM_PATH, ITEM_IFACE, "NewIcon")
            if changed_label:
                self._emit(ITEM_PATH, ITEM_IFACE, "XAyatanaNewLabel", "ss", (label, ""))
            if changed_menu:
                self._emit(MENU_PATH, MENU_IFACE, "LayoutUpdated", "ui", (self.revision, 0))
        except Exception as error:
            log.warning("tray update failed: %s", error)

    def _register(self) -> None:
        try:
            self.router.send_and_get_reply(
                new_method_call(WATCHER, "RegisterStatusNotifierItem", "s", (ITEM_PATH,)), timeout=2)
            log.info("tray icon registered")
        except Exception as error:
            # The watcher's NameOwnerChanged retriggers this once the shell (re)starts.
            log.warning("tray icon not registered yet: %s", error)

    def _emit(self, path: str, interface: str, name: str, signature=None, body=()) -> None:
        self.router.send(new_signal(DBusAddress(path, interface=interface), name, signature, body))

    def _item_properties(self) -> dict:
        with self.lock:
            return {
                "Category": ("s", "Hardware"),
                "Id": ("s", "voice-dictate"),
                "Title": ("s", "Dictation"),
                "Status": ("s", "Active"),
                "WindowId": ("i", 0),
                "IconThemePath": ("s", ""),
                "Menu": ("o", MENU_PATH),
                "ItemIsMenu": ("b", True),
                "IconName": ("s", self.icon),
                "IconPixmap": ("a(iiay)", []),
                "OverlayIconName": ("s", ""),
                "OverlayIconPixmap": ("a(iiay)", []),
                "AttentionIconName": ("s", ""),
                "AttentionIconPixmap": ("a(iiay)", []),
                "AttentionMovieName": ("s", ""),
                "XAyatanaLabel": ("s", self.label),
                "XAyatanaLabelGuide": ("s", ""),
            }

    def _menu_properties(self) -> dict:
        return {
            "Version": ("u", 3),
            "TextDirection": ("s", "ltr"),
            "Status": ("s", "normal"),
            "IconThemePath": ("as", []),
        }

    def _menu_items(self) -> list[tuple[int, dict]]:
        with self.lock:
            return [(index + 1, {"label": ("s", line), "enabled": ("b", False)})
                    for index, line in enumerate(self.lines)]

    def _layout(self) -> tuple:
        children = [("(ia{sv}av)", (item_id, props, [])) for item_id, props in self._menu_items()]
        return (0, {"children-display": ("s", "submenu")}, children)

    def _serve(self) -> None:
        while True:
            message = self.calls.get()
            try:
                self._dispatch(message)
            except Exception:
                log.exception("tray request failed")

    def _dispatch(self, message) -> None:
        fields = message.header.fields
        if message.header.message_type == MessageType.signal:
            if message.body[2]:
                self._register()
            return
        path, interface, member = fields.get(HeaderFields.path), fields.get(HeaderFields.interface), fields.get(HeaderFields.member)
        reply = self._answer(path, interface, member, message.body)
        if reply is None:
            self.router.send(new_error(message, "org.freedesktop.DBus.Error.UnknownMethod"))
        else:
            self.router.send(new_method_return(message, *reply))

    def _answer(self, path, interface, member, body):
        if member == "Introspect":
            return "s", (INTROSPECT,)
        if interface == "org.freedesktop.DBus.Properties":
            properties = self._item_properties() if path == ITEM_PATH else self._menu_properties()
            if member == "GetAll":
                return "a{sv}", (properties,)
            if member == "Get" and body[1] in properties:
                return "v", (properties[body[1]],)
            return None
        if path != MENU_PATH:
            return None
        if member == "GetLayout":
            return "u(ia{sv}av)", (self.revision, self._layout())
        if member == "GetGroupProperties":
            return "a(ia{sv})", (self._menu_items(),)
        if member == "GetProperty":
            return "v", (dict(self._menu_items()).get(body[0], {}).get(body[1], ("s", "")),)
        if member == "AboutToShow":
            return "b", (False,)
        if member == "AboutToShowGroup":
            return "aiai", ([], [])
        if member == "Event":
            return None, ()
        if member == "EventGroup":
            return "ai", ([],)
        return None

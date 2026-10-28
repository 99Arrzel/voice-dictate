// Voice Dictate — reports the focused app to the voice-dictate daemon over the session bus,
// which Wayland clients cannot query themselves. The status icon is the daemon's own tray item.

import Gio from 'gi://Gio';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.voicedictate.Indicator';
const OBJECT_PATH = '/org/voicedictate/Indicator';
const IFACE_XML = `
<node>
  <interface name="org.voicedictate.Indicator">
    <method name="FocusedApp">
      <arg type="s" direction="out" name="wmClass"/>
    </method>
  </interface>
</node>`;

export default class VoiceDictateExtension extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null);
    }

    disable() {
        Gio.bus_unown_name(this._nameId);
        this._dbus.unexport();
        this._dbus = null;
    }

    FocusedApp() {
        return global.display.focus_window?.get_wm_class() ?? '';
    }
}

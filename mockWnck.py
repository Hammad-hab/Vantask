"""
mock_wnck.py — a drop-in stand-in for `gi.repository.Wnck` when developing
on a platform that doesn't have libwnck (i.e. macOS).

This is NOT a real window manager bridge. It fakes a handful of windows
in-process and exposes the same shape of API/signals that vantusk.py
already uses:

    Screen.get_default()
    Screen.get_windows()
    Screen.connect("window-opened" | "window-closed" | "active-window-changed", ...)
    Window.get_xid() / get_name() / get_pid() / get_icon()
    Window.get_window_type() / is_skip_tasklist()
    Window.is_active() / is_minimized()
    Window.activate() / minimize() / unminimize()
    Window.connect("state-changed", ...)
    WindowType.NORMAL

That's the full subset vantusk.py calls, so nothing else in vantusk.py
needs to change -- just the import at the top.

Includes a small control panel (`show_control_panel()`) so you can
add/remove/rename fake windows live and watch the real dock react.

USAGE in vantusk.py:

    import sys
    import gi
    gi.require_version("Gtk", "3.0")

    try:
        gi.require_version("Wnck", "3.0")
        from gi.repository import Wnck
    except (ValueError, ImportError):
        if sys.platform == "darwin":
            import mock_wnck as Wnck
            print("[vantusk] libwnck unavailable -- using mock_wnck (dev only)")
        else:
            raise
"""

import itertools

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk, GLib

_xid_counter = itertools.count(1)

DEMO_ICON_NAMES = [
    "applications-internet",
    "utilities-terminal",
    "system-file-manager",
    "accessories-text-editor",
    "applications-multimedia",
]


class WindowType:
    """Mirrors the handful of Wnck.WindowType values vantusk.py checks."""
    NORMAL = 0
    DESKTOP = 1
    DOCK = 2
    DIALOG = 3
    TOOLBAR = 4
    MENU = 5
    UTILITY = 6
    SPLASHSCREEN = 7


class Window(GObject.GObject):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "name-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, screen, name, pid=0, icon_name=None):
        super().__init__()
        self._screen = screen
        self._xid = next(_xid_counter)
        self._name = name
        self._pid = pid
        self._icon_name = icon_name or "application-x-executable"
        self._minimized = False
        self._active = False
        self._window_type = WindowType.NORMAL
        self._skip_tasklist = False

    # -- API subset used by vantusk.py --------------------------------
    def get_xid(self):
        return self._xid

    def get_name(self):
        return self._name

    def get_pid(self):
        return self._pid

    def get_window_type(self):
        return self._window_type

    def is_skip_tasklist(self):
        return self._skip_tasklist

    def is_active(self):
        return self._active

    def is_minimized(self):
        return self._minimized

    def get_icon(self):
        theme = Gtk.IconTheme.get_default()
        for name in (self._icon_name, "application-x-executable", "image-missing"):
            try:
                return theme.load_icon(name, 40, 0)
            except GLib.Error:
                continue
        return None

    def minimize(self):
        self._minimized = True
        was_active = self._active
        self._active = False
        self.emit("state-changed", 0, 0)
        if was_active:
            self._screen.emit("active-window-changed", None)

    def unminimize(self, timestamp=0):
        self._minimized = False
        self.emit("state-changed", 0, 0)

    def activate(self, timestamp=0):
        self._minimized = False
        self._screen._set_active(self)

    def set_name(self, name):
        self._name = name
        self.emit("name-changed")

    def close(self):
        self._screen.remove_window(self)


class Screen(GObject.GObject):
    __gsignals__ = {
        "window-opened": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "window-closed": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
        "active-window-changed": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_PYOBJECT,)),
    }

    _instance = None

    def __init__(self):
        super().__init__()
        self._windows = []

    @classmethod
    def get_default(cls):
        if cls._instance is None:
            cls._instance = cls()
            # Mirrors the real Wnck quirk vantusk.py already works around:
            # the window list isn't populated synchronously, it needs a
            # mainloop pass.
            GLib.idle_add(cls._instance._seed_demo_windows)
        return cls._instance

    def get_windows(self):
        return list(self._windows)

    def _set_active(self, window):
        for w in self._windows:
            was_active = w._active
            w._active = (w is window)
            if was_active != w._active:
                w.emit("state-changed", 0, 0)
        self.emit("active-window-changed", None)

    def add_window(self, name, pid=0, icon_name=None, activate=True):
        w = Window(self, name, pid=pid, icon_name=icon_name)
        self._windows.append(w)
        self.emit("window-opened", w)
        if activate or not any(x.is_active() for x in self._windows):
            self._set_active(w)
        return w

    def remove_window(self, window):
        if window in self._windows:
            self._windows.remove(window)
            self.emit("window-closed", window)

    def _seed_demo_windows(self):
        demo = [("Firefox", "applications-internet"),
                ("Files", "system-file-manager"),
                ("Terminal", "utilities-terminal")]
        for name, icon in demo:
            self.add_window(name, pid=0, icon_name=icon, activate=False)
        if self._windows:
            self._set_active(self._windows[0])
        return False  # GLib.idle_add: run once


# ---------------------------------------------------------------------
# Optional control panel -- add/remove/activate fake windows live.
# Call show_control_panel() once from your dev entrypoint alongside
# VantuskWindow to drive the mock interactively.
# ---------------------------------------------------------------------

def show_control_panel():
    screen = Screen.get_default()

    win = Gtk.Window(title="mock_wnck control")
    win.set_default_size(260, 300)
    win.set_keep_above(True)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_border_width(10)
    win.add(box)

    entry = Gtk.Entry()
    entry.set_placeholder_text("New window name")
    box.pack_start(entry, False, False, 0)

    icon_counter = itertools.cycle(DEMO_ICON_NAMES)

    def on_add(_btn):
        name = entry.get_text().strip() or f"App {next(_xid_counter)}"
        screen.add_window(name, pid=0, icon_name=next(icon_counter))
        entry.set_text("")
        refresh_list()

    add_btn = Gtk.Button(label="Add window")
    add_btn.connect("clicked", on_add)
    box.pack_start(add_btn, False, False, 0)

    list_box = Gtk.ListBox()
    box.pack_start(list_box, True, True, 0)

    def refresh_list():
        for child in list(list_box.get_children()):
            list_box.remove(child)
        for w in screen.get_windows():
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            label = Gtk.Label(label=("● " if w.is_active() else "  ") + w.get_name())
            label.set_halign(Gtk.Align.START)
            row.pack_start(label, True, True, 4)

            activate_btn = Gtk.Button(label="Activate")
            activate_btn.connect("clicked", lambda _b, w=w: (w.activate(), refresh_list()))
            row.pack_start(activate_btn, False, False, 0)

            min_btn = Gtk.Button(label="Min")
            min_btn.connect("clicked", lambda _b, w=w: (w.minimize(), refresh_list()))
            row.pack_start(min_btn, False, False, 0)

            close_btn = Gtk.Button(label="✕")
            close_btn.connect("clicked", lambda _b, w=w: (w.close(), refresh_list()))
            row.pack_start(close_btn, False, False, 0)

            list_box.add(row)
        list_box.show_all()

    screen.connect("window-opened", lambda *_: refresh_list())
    screen.connect("window-closed", lambda *_: refresh_list())
    screen.connect("active-window-changed", lambda *_: refresh_list())

    GLib.idle_add(refresh_list)

    win.show_all()
    return win


if __name__ == "__main__":
    # Standalone smoke test: just the control panel, no real dock.
    show_control_panel()
    Gtk.main()
import os
import json
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Wnck

CSS = """
window {
    background-color: rgba(32, 32, 32, 0.92);
    border-radius: 16px;
}

.dock {
    background-color: transparent;
}

.task-tile {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    transition: background-color 120ms ease;
}

.task-tile:hover {
    background-color: #3b3b3b;
}

.task-tile.active {
    background-color: #454545;
    border-color: #60cdff;
}

.task-tile.active-dot {
    background-color: #60cdff;
}
"""

PROCESS_FILE = "/tmp/processz.tmp"
ICON_SIZE = 40
DOCK_MARGIN_BOTTOM = 14


def read_vantyl_tasks():
    """Read Vantyl's pid -> {name, cmd} map. Returns {} if the file is
    missing, empty, or mid-write (best-effort; Vantusk should never crash
    because Vantyl happened to be writing to the file at the same moment)."""
    p = Path(PROCESS_FILE)
    if not p.is_file():
        return {}
    try:
        raw = p.read_text().strip()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        return {}


class TaskTile(Gtk.EventBox):
    """One running window in the dock."""

    def __init__(self, wnck_window, friendly_name=None):
        super().__init__()
        self.wnck_window = wnck_window

        self.set_visible_window(True)
        self.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        self.get_style_context().add_class("task-tile")
        self.connect("button-press-event", self._on_click)
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(6)
        self.add(box)

        image = self._build_icon(wnck_window)
        box.pack_start(image, False, False, 0)

        self.set_tooltip_text(friendly_name or wnck_window.get_name())

        self._update_active_style()

    def _build_icon(self, wnck_window):
        pixbuf = wnck_window.get_icon()
        if pixbuf:
            if pixbuf.get_width() != ICON_SIZE:
                pixbuf = pixbuf.scale_simple(
                    ICON_SIZE, ICON_SIZE, GdkPixbuf.InterpType.BILINEAR
                )
            return Gtk.Image.new_from_pixbuf(pixbuf)

        image = Gtk.Image.new_from_icon_name(
            "application-x-executable", Gtk.IconSize.DIALOG
        )
        image.set_pixel_size(ICON_SIZE)
        return image

    def _update_active_style(self):
        ctx = self.get_style_context()
        if self.wnck_window.is_active():
            ctx.add_class("active")
        else:
            ctx.remove_class("active")

    def _on_enter(self, widget, event):
        cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "pointer")
        self.get_window().set_cursor(cursor)

    def _on_leave(self, widget, event):
        self.get_window().set_cursor(None)

    def _on_click(self, widget, event):
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return
        # Click an active window to minimize it, click any other to raise it
        # -- standard taskbar toggle behavior.
        if self.wnck_window.is_active() and not self.wnck_window.is_minimized():
            self.wnck_window.minimize()
        else:
            if self.wnck_window.is_minimized():
                self.wnck_window.unminimize(Gdk.CURRENT_TIME)
            self.wnck_window.activate(Gdk.CURRENT_TIME)


class VantuskWindow(Gtk.Window):

    def __init__(self):
        super().__init__()
        self.set_title("Vantusk")
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.stick()  # visible on every JWM virtual desktop

        self.load_css()

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box.set_border_width(8)
        self.box.get_style_context().add_class("dock")
        self.add(self.box)

        self.tiles = {}  # xid -> TaskTile

        self.wnck_screen = Wnck.Screen.get_default()
        self.wnck_screen.connect("window-opened", self._on_window_opened)
        self.wnck_screen.connect("window-closed", self._on_window_closed)
        self.wnck_screen.connect(
            "active-window-changed", self._on_active_window_changed
        )

        # Wnck needs a mainloop pass to populate its initial window list.
        GLib.idle_add(self._initial_populate)

        self.connect("realize", self._on_realize)

    def load_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # -- window list -----------------------------------------------------

    def _should_show(self, wnck_window):
        # Skip the desktop pseudo-window, Vantyl/Vantusk themselves, and
        # anything explicitly hidden from the taskbar (window managers set
        # this via _NET_WM_STATE_SKIP_TASKBAR).
        if wnck_window.get_window_type() != Wnck.WindowType.NORMAL:
            return False
        if wnck_window.is_skip_tasklist():
            return False
        return True

    def _initial_populate(self):
        vantyl_tasks = read_vantyl_tasks()
        for w in self.wnck_screen.get_windows():
            if self._should_show(w):
                self._add_tile(w, vantyl_tasks)
        return False  # run once

    def _friendly_name_for(self, wnck_window, vantyl_tasks):
        pid = wnck_window.get_pid()
        entry = vantyl_tasks.get(str(pid)) or vantyl_tasks.get(pid)
        if entry and entry.get("name"):
            return entry["name"]
        return wnck_window.get_name()

    def _add_tile(self, wnck_window, vantyl_tasks=None):
        xid = wnck_window.get_xid()
        if xid in self.tiles:
            return

        if vantyl_tasks is None:
            vantyl_tasks = read_vantyl_tasks()

        name = self._friendly_name_for(wnck_window, vantyl_tasks)
        tile = TaskTile(wnck_window, friendly_name=name)
        self.tiles[xid] = tile
        self.box.pack_start(tile, False, False, 0)
        tile.show_all()

        wnck_window.connect("state-changed", lambda *_: tile._update_active_style())

        self._reposition()

    def _remove_tile(self, xid):
        tile = self.tiles.pop(xid, None)
        if tile:
            self.box.remove(tile)
        self._reposition()

    def _on_window_opened(self, screen, wnck_window):
        if self._should_show(wnck_window):
            self._add_tile(wnck_window)

    def _on_window_closed(self, screen, wnck_window):
        self._remove_tile(wnck_window.get_xid())

    def _on_active_window_changed(self, screen, previous_window):
        for tile in self.tiles.values():
            tile._update_active_style()

    # -- positioning -------------------------------------------------------

    def _on_realize(self, *_):
        self._reposition()

    def _reposition(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()

        self.resize(1, 1)  # let GTK recompute natural size from box contents
        width, height = self.get_size()

        x = geo.x + (geo.width - width) // 2
        y = geo.y + geo.height - height - DOCK_MARGIN_BOTTOM
        self.move(x, y)


def main():
    win = VantuskWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
import argparse
import os
import sys
import json
import shlex
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
import cairo

import gi
gi.require_version("Gtk", "3.0")

try:
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck
except (ValueError, ImportError):
    if sys.platform == "darwin":
        import mockWnck as Wnck
        print("[Vantask] libwnck unavailable -- using mock_wnck (dev only)")
    else:
        raise
else:
    print('Successfully imported')

from gi.repository import Gtk, Gdk, GdkPixbuf, GLib

CSS = """
window {
    background-color: rgb(32, 32, 32);
}

.dock {
    background-color: transparent;
}

.task-tile {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    transition: background-color 120ms ease, opacity 120ms ease;
}

.task-tile:hover {
    background-color: #3b3b3b;
}

.task-tile.active {
    background-color: #454545;
    border-color: #60cdff;
}


.start-button {
    background-color: rgba(96, 205, 255, 0.14);
    border-color: rgba(96, 205, 255, 0.35);
}

.start-button:hover {
    background-color: rgba(96, 205, 255, 0.24);
}

.dock-separator {
    background-color: #4a4a4a;
    min-width: 1px;
    margin: 6px 4px;
}
"""

PROCESS_FILE = "/tmp/processz.tmp"
ICON_SIZE = 40
DOCK_MARGIN_BOTTOM = 150

# Where Vantyl lives -- adjust if it's laid out differently on your machine.
# # Fallback if no <launcher_cmd/> is present in task.xml (or no task file given).
DEFAULT_LAUNCHER_CMD = ["python3.14", os.path.expanduser("~/Documents/Hammad/Vantyl/vantyl.py")]
EXCLUDED_WINDOW_NAMES = {'Vantyl'}
# ---------------------------------------------------------------------
# Vantyl process registry (best-effort pid -> {name, cmd} lookup)
# ---------------------------------------------------------------------

def read_vantyl_tasks():
    """Read Vantyl's pid -> {name, cmd} map. Returns {} if the file is
    missing, empty, or mid-write (best-effort; Vantask should never crash
    because Vantyl happened to be writing to the file at the same moment)."""
    p = Path(PROCESS_FILE)
    if not p.is_file():
        return {}
    try:
        raw = p.read_text().strip()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _normalize_exec(cmd):
    """Reduce an exec string to just its executable name for loose
    matching (e.g. 'libreoffice --writer' -> 'libreoffice'). Deliberately
    lossy -- see task.xml matching caveat: pinned entries that differ only
    by flags (e.g. --writer vs --calc) will collide under this scheme."""
    if not cmd:
        return ""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    return os.path.basename(parts[0]) if parts else ""


# ---------------------------------------------------------------------
# task.xml parsing
# ---------------------------------------------------------------------

def resolve_icon_path(icon_attr, task_file_path):
    """Resolve a pinned <pinned icon="..."/> attribute.

    If it looks like a path (contains a separator or starts with '~'),
    resolve it relative to the task.xml file's directory and return an
    absolute path -- or None if the file doesn't exist, so callers fall
    back to a theme icon instead of crashing.

    If it doesn't look like a path, it's treated as an icon-theme name
    and returned unchanged.
    """
    if not icon_attr:
        return None

    looks_like_path = os.path.sep in icon_attr or icon_attr.startswith("~")
    if not looks_like_path:
        return icon_attr  # theme icon name, unchanged

    candidate = os.path.expanduser(icon_attr)
    if not os.path.isabs(candidate):
        base_dir = os.path.dirname(os.path.abspath(task_file_path))
        candidate = os.path.join(base_dir, candidate)

    if os.path.isfile(candidate):
        return os.path.abspath(candidate)

    print(f"Ignoring <pinned icon=\"{icon_attr}\"/>: file not found")
    return None

def parse_task_file(path):
    """Parse task.xml into (pinned, launcher_cmd):
        pinned: list of {"name": str, "icon": str|None, "exec": str}
        launcher_cmd: str|None -- exec string from <launcher_cmd exec="..."/>,
                      used for the Start button. None if not specified,
                      in which case DEFAULT_LAUNCHER_CMD is used instead.

    Unlike menu.xml this is intentionally flat -- no folders. Malformed
    or incomplete <pinned> entries (missing name/exec) are skipped with
    a warning rather than raising, so one bad line doesn't take down the
    whole dock.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    pinned = []
    launcher_cmd = None

    for child in root:
        if child.tag == "launcher_cmd":
            exec_cmd = child.get("exec")
            if not exec_cmd:
                print(f"Skipping <launcher_cmd>: missing exec ({ET.tostring(child, encoding='unicode').strip()})")
            elif launcher_cmd is not None:
                print(f"Ignoring duplicate <launcher_cmd exec=\"{exec_cmd}\"/>: first one wins")
            else:
                launcher_cmd = exec_cmd
            continue

        if child.tag != "pinned":
            continue  # unknown tags silently skipped, same convention as menu.xml

        name = child.get("name")
        exec_cmd = child.get("exec")
        if not name or not exec_cmd:
            print(f"Skipping <pinned>: missing name or exec ({ET.tostring(child, encoding='unicode').strip()})")
            continue

        icon = resolve_icon_path(child.get("icon"), path)
        pinned.append({"name": name, "icon": icon, "exec": exec_cmd})

    return pinned, launcher_cmd


def load_icon_image(icon_value, pixel_size, fallback_name="application-x-executable"):
    """Build a Gtk.Image from either an absolute icon path or a
    theme icon name, falling back gracefully on any failure. Mirrors
    Vantyl's AppTile._build_icon_image so pinned tiles and running-task
    tiles look consistent."""
    if icon_value:
        looks_like_path = os.path.sep in icon_value
        if looks_like_path:
            if os.path.isfile(icon_value):
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        icon_value, pixel_size, pixel_size, True
                    )
                    image = Gtk.Image.new_from_pixbuf(pixbuf)
                    image.set_halign(Gtk.Align.CENTER)
                    return image
                except Exception as exc:
                    print(f"Failed to load icon '{icon_value}': {exc}")
        else:
            icon_theme = Gtk.IconTheme.get_default()
            name = icon_value if icon_theme.has_icon(icon_value) else fallback_name
            image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.DIALOG)
            image.set_pixel_size(pixel_size)
            image.set_halign(Gtk.Align.CENTER)
            return image

    image = Gtk.Image.new_from_icon_name(fallback_name, Gtk.IconSize.DIALOG)
    image.set_pixel_size(pixel_size)
    image.set_halign(Gtk.Align.CENTER)
    return image


# ---------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------

class TaskTile(Gtk.EventBox):
    """One running window in the dock that ISN'T pinned."""

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
        return load_icon_image(None, ICON_SIZE)

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
        if self.wnck_window.is_active() and not self.wnck_window.is_minimized():
            self.wnck_window.minimize()
        else:
            if self.wnck_window.is_minimized():
                self.wnck_window.unminimize(Gdk.CURRENT_TIME)
            self.wnck_window.activate(Gdk.CURRENT_TIME)


class PinnedTile(Gtk.EventBox):
    """A permanent pinned-app slot from task.xml.

    Two states:
      - not running: dimmed, click launches pinned_entry["exec"].
      - bound to a live window: behaves exactly like TaskTile (click to
        activate/minimize, gets the "active" highlight).
    """

    def __init__(self, pinned_entry, on_launch):
        super().__init__()
        self.pinned_entry = pinned_entry
        self.on_launch = on_launch
        self.wnck_window = None

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

        image = load_icon_image(pinned_entry.get("icon"), ICON_SIZE)
        box.pack_start(image, False, False, 0)

        self.set_tooltip_text(pinned_entry["name"])
        self._update_style()

    def bind(self, wnck_window):
        self.wnck_window = wnck_window
        self._update_style()

    def unbind(self):
        self.wnck_window = None
        self._update_style()

    def is_running(self):
        return self.wnck_window is not None

    def _update_style(self):
        ctx = self.get_style_context()
        if self.wnck_window and self.wnck_window.is_active():
            ctx.add_class("active")
        else:
            ctx.remove_class("active")
        if self.wnck_window:
            ctx.remove_class("not-running")
        else:
            ctx.add_class("not-running")

    def _on_enter(self, widget, event):
        cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "pointer")
        self.get_window().set_cursor(cursor)

    def _on_leave(self, widget, event):
        self.get_window().set_cursor(None)

    def _on_click(self, widget, event):
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return
        if self.wnck_window:
            if self.wnck_window.is_active() and not self.wnck_window.is_minimized():
                self.wnck_window.minimize()
            else:
                if self.wnck_window.is_minimized():
                    self.wnck_window.unminimize(Gdk.CURRENT_TIME)
                self.wnck_window.activate(Gdk.CURRENT_TIME)
        else:
            self.on_launch(self.pinned_entry)


class StartButton(Gtk.EventBox):
    """Permanent, always-first tile that launches Vantyl -- Vantum's
    equivalent of the Windows-logo Start button."""

    def __init__(self, on_click):
        super().__init__()
        self.set_visible_window(True)
        self.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
        )
        self.get_style_context().add_class("task-tile")
        self.get_style_context().add_class("start-button")
        self.connect(
            "button-press-event",
            lambda w, e: on_click()
            if e.button == 1 and e.type == Gdk.EventType.BUTTON_PRESS
            else None,
        )
        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_border_width(6)
        self.add(box)

        image = load_icon_image("assets/launcher-icon.png", ICON_SIZE)
        box.pack_start(image, False, False, 0)
        self.set_tooltip_text("Vantyl")

    def _on_enter(self, widget, event):
        cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "pointer")
        self.get_window().set_cursor(cursor)

    def _on_leave(self, widget, event):
        self.get_window().set_cursor(None)


# ---------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------

class VantaskWindow(Gtk.Window):

    def __init__(self, task_file=None):
        super().__init__()
        self.set_title("Vantask")
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_can_focus(False)
        self.stick()
        self.load_css()

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box.set_border_width(8)
        self.box.get_style_context().add_class("dock")
        self.add(self.box)

        # xid -> ("pinned", PinnedTile) | ("dynamic", TaskTile)
        self.tiles = {}
        self.pinned_tiles = []

        self.launcher_cmd = DEFAULT_LAUNCHER_CMD  # default until _build_pinned_tiles may override it
        self._build_start_button()
        self._build_pinned_tiles(task_file)

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

    # -- start button / pinned setup --------------------------------------

    def _build_start_button(self):
        start = StartButton(self._launch_vantyl)
        self.box.pack_start(start, False, False, 0)

        separator = Gtk.Box()
        separator.get_style_context().add_class("dock-separator")
        separator.set_size_request(1, ICON_SIZE)
        self.box.pack_start(separator, False, False, 0)

    def _launch_vantyl(self):
        try:
            subprocess.Popen(self.launcher_cmd)
        except Exception as exc:
            print(f"Failed to launch launcher ('{self.launcher_cmd}'): {exc}")

    def _build_pinned_tiles(self, task_file):
        self.launcher_cmd = DEFAULT_LAUNCHER_CMD

        if not task_file or not os.path.isfile(task_file):
            if task_file:
                print(f"task.xml not found at '{task_file}' -- no pinned apps loaded")
            return

        try:
            pinned_entries, launcher_cmd = parse_task_file(task_file)
        except ET.ParseError as exc:
            print(f"Failed to parse '{task_file}': {exc}")
            return

        if launcher_cmd:
            try:
                self.launcher_cmd = shlex.split(launcher_cmd)
            except ValueError as exc:
                print(f"Invalid <launcher_cmd exec=\"{launcher_cmd}\"/>: {exc} -- using default")

        for entry in pinned_entries:
            tile = PinnedTile(entry, on_launch=self._launch_pinned)
            self.pinned_tiles.append(tile)
            self.box.pack_start(tile, False, False, 0)
            tile.show_all()

        separator = Gtk.Box()
        separator.get_style_context().add_class("dock-separator")
        separator.set_size_request(1, ICON_SIZE)
        self.box.pack_start(separator, False, False, 0)
        
    def _launch_pinned(self, pinned_entry):
        try:
            subprocess.Popen(shlex.split(pinned_entry["exec"]))
        except Exception as exc:
            print(f"Failed to launch '{pinned_entry['exec']}': {exc}")

    # -- window list -----------------------------------------------------

    def _should_show(self, wnck_window):
        # Skip the desktop pseudo-window, Vantyl/Vantask themselves, and
        # anything explicitly hidden from the taskbar (window managers set
        # this via _NET_WM_STATE_SKIP_TASKBAR).
        if wnck_window.get_window_type() != Wnck.WindowType.NORMAL:
            return False
        if wnck_window.is_skip_tasklist():
            return False
        if wnck_window.get_name() in EXCLUDED_WINDOW_NAMES:
            return False
        return True
        
    def _initial_populate(self):
        for w in self.wnck_screen.get_windows():
            if self._should_show(w):
                self._handle_window_opened(w)
        return False  # run once

    def _friendly_name_for(self, wnck_window, vantyl_tasks):
        pid = wnck_window.get_pid()
        entry = vantyl_tasks.get(str(pid)) or vantyl_tasks.get(pid)
        if entry and entry.get("name"):
            return entry["name"]
        return wnck_window.get_name()

    def _match_pinned(self, wnck_window, vantyl_tasks):
        """Best-effort: match a running window to an unbound pinned tile
        via Vantyl's process registry. Returns None if no match -- the
        window just becomes a normal dynamic tile in that case."""
        pid = wnck_window.get_pid()
        entry = vantyl_tasks.get(str(pid)) or vantyl_tasks.get(pid)
        running_exe = _normalize_exec(entry.get("cmd")) if entry else ""
        if not running_exe:
            return None

        for pinned_tile in self.pinned_tiles:
            if pinned_tile.is_running():
                continue
            if _normalize_exec(pinned_tile.pinned_entry.get("exec")) == running_exe:
                return pinned_tile
        return None

    def _handle_window_opened(self, wnck_window):
        if not self._should_show(wnck_window):
            return

        xid = wnck_window.get_xid()
        if xid in self.tiles:
            return

        vantyl_tasks = read_vantyl_tasks()
        pinned_tile = self._match_pinned(wnck_window, vantyl_tasks)

        if pinned_tile is not None:
            pinned_tile.bind(wnck_window)
            self.tiles[xid] = ("pinned", pinned_tile)
            wnck_window.connect(
                "state-changed", lambda *_: pinned_tile._update_style()
            )
        else:
            name = self._friendly_name_for(wnck_window, vantyl_tasks)
            tile = TaskTile(wnck_window, friendly_name=name)
            self.tiles[xid] = ("dynamic", tile)
            self.box.pack_start(tile, False, False, 0)
            tile.show_all()
            wnck_window.connect(
                "state-changed", lambda *_: tile._update_active_style()
            )

        GLib.idle_add(self._reposition)

    def _remove_tile(self, xid):
        entry = self.tiles.pop(xid, None)
        if entry is None:
            return
        kind, tile = entry
        if kind == "pinned":
            tile.unbind()
        else:
            self.box.remove(tile)
        GLib.idle_add(self._reposition)

    def _on_window_opened(self, screen, wnck_window):
        self._handle_window_opened(wnck_window)

    def _on_window_closed(self, screen, wnck_window):
        self._remove_tile(wnck_window.get_xid())

    def _on_active_window_changed(self, screen, previous_window):
        for kind, tile in self.tiles.values():
            if kind == "pinned":
                tile._update_style()
            else:
                tile._update_active_style()

    # -- positioning -------------------------------------------------------

    def _on_realize(self, *_):
        self._reposition()

    def _apply_rounded_shape(self, width, height):
        radius = 12  # match your CSS border-radius

        surface = cairo.ImageSurface(cairo.FORMAT_A1, width, height)
        ctx = cairo.Context(surface)
        ctx.set_source_rgba(0, 0, 0, 1)

        ctx.new_sub_path()
        ctx.arc(width - radius, radius, radius, -90 * (3.14159 / 180), 0)
        ctx.arc(width - radius, height - radius, radius, 0, 90 * (3.14159 / 180))
        ctx.arc(radius, height - radius, radius, 90 * (3.14159 / 180), 180 * (3.14159 / 180))
        ctx.arc(radius, radius, radius, 180 * (3.14159 / 180), 270 * (3.14159 / 180))
        ctx.close_path()
        ctx.fill()

        region = Gdk.cairo_region_create_from_surface(surface)
        self.get_window().shape_combine_region(region, 0, 0)

    def _reposition(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)

        try:
            geo = monitor.get_workarea()
        except AttributeError:
            geo = monitor.get_geometry()

        _, natural = self.get_preferred_size()
        width, height = natural.width, natural.height

        x = geo.x + (geo.width - width) // 2
        y = geo.y + geo.height - height - DOCK_MARGIN_BOTTOM
        y = max(geo.y, min(y, geo.y + geo.height - height))

        self.move(x, y)
        self.resize(width, height)

        # Mask must be re-cut every time the window's actual size changes,
        # using the SAME width/height we just resized to -- not a stale
        # get_size() read from before layout settled.
        if self.get_realized():
            self._apply_rounded_shape(width, height)

        return False

def parse_args():
    parser = argparse.ArgumentParser(description="Vantask task dock")
    parser.add_argument(
        "task_file",
        nargs="?",
        default=None,
        help="Path to a task XML file describing pinned apps",
    )
    return parser.parse_args()

def main():
    args = parse_args()
    win = VantaskWindow(task_file=args.task_file)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    print('Showing windows!')

    Gtk.main()

if __name__ == "__main__":
    main()
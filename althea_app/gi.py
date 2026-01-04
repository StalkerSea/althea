"""Centralized PyGObject imports.

Keeping the GI version requirements in one place avoids subtle mismatches and
makes refactors safer.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Handy", "1")
gi.require_version("Notify", "0.7")

try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import Gtk, AppIndicator3 as appindicator
except ValueError:  # Ayatana-based distros
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import Gtk, AyatanaAppIndicator3 as appindicator

from gi.repository import GLib
from gi.repository import GObject, Handy
from gi.repository import GdkPixbuf
from gi.repository import Notify
from gi.repository import Gdk
from gi.repository import Pango

GObject.type_ensure(Handy.ActionRow)

__all__ = [
    "Gtk",
    "GLib",
    "GObject",
    "Handy",
    "GdkPixbuf",
    "Notify",
    "Gdk",
    "Pango",
    "appindicator",
]

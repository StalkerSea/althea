#!/usr/bin/python
import sys
import os
import errno
from shutil import rmtree
import json
import urllib.request
from urllib.request import urlopen
import urllib.parse
import requests
import subprocess
import signal
import threading
import keyring
import re
import shlex
import time
from time import sleep
import platform
from packaging import version
import psutil
import logging

# PyGObject

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Handy", "1")
gi.require_version("Notify", "0.7")
try:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import Gtk, AppIndicator3 as appindicator
except ValueError:  # Fix for Solus and other Ayatana users
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import Gtk, AyatanaAppIndicator3 as appindicator
from gi.repository import GLib
from gi.repository import GObject, Handy
from gi.repository import GdkPixbuf
from gi.repository import Notify
from gi.repository import Gdk
from gi.repository import Pango

GObject.type_ensure(Handy.ActionRow)

installedcheck = False
computer_cpu_platform = platform.machine()


DEFAULT_SETTINGS = {
    # "window_and_tray" (default): open main window + tray indicator
    # "tray_only": start in tray (no main window)
    "startup_mode": "window_and_tray",
}


SETTINGS = dict(DEFAULT_SETTINGS)


logger = logging.getLogger("althea")


def get_connected_udid():
    """Return a single device UDID, preferring USB and falling back to network.

    Note: `idevice_id -l` can list devices even before they are trusted/paired.
    Avoid requiring `ideviceinfo` here, otherwise we incorrectly report "no device".
    """
    return get_connected_device().get("udid", "")


def get_connected_device():
    """Return {udid, transport} where transport is 'usb' | 'network' | 'none'."""
    # Prefer USB via default usbmuxd.
    try:
        log_info("get_connected_device: running idevice_id -l")
        out = subprocess.check_output(["idevice_id", "-l"], stderr=subprocess.STDOUT)
        udids = [
            line.strip()
            for line in out.decode(errors="replace").splitlines()
            if line.strip()
        ]
        if udids:
            log_info(f"get_connected_device: usb udids={udids}")
            return {"udid": udids[0], "transport": "usb"}
    except subprocess.CalledProcessError as e:
        log_info(f"get_connected_device: idevice_id -l failed rc={e.returncode}")
    except Exception as e:
        log_info(f"get_connected_device: idevice_id -l failed: {e!r}")

    # Fallback to network via netmuxd.
    try:
        env = os.environ.copy()
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        log_info("get_connected_device: running idevice_id -n -l (via netmuxd)")
        out = subprocess.check_output(
            ["idevice_id", "-n", "-l"], env=env, stderr=subprocess.STDOUT
        )
        udids = [
            line.strip()
            for line in out.decode(errors="replace").splitlines()
            if line.strip()
        ]
        if udids:
            log_info(f"get_connected_device: network udids={udids}")
            return {"udid": udids[0], "transport": "network"}
    except subprocess.CalledProcessError as e:
        log_info(f"get_connected_device: idevice_id -n -l failed rc={e.returncode}")
    except Exception as e:
        log_info(f"get_connected_device: idevice_id -n -l failed: {e!r}")

    return {"udid": "", "transport": "none"}


def get_network_udid():
    """Return a device UDID specifically from network connection."""
    try:
        env = os.environ.copy()
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        log_info("get_network_udid: running idevice_id -n -l")
        out = subprocess.check_output(
            ["idevice_id", "-n", "-l"], env=env, stderr=subprocess.STDOUT
        )
        udids = [
            line.strip()
            for line in out.decode(errors="replace").splitlines()
            if line.strip()
        ]
        if udids:
            log_info(f"get_network_udid: candidates={udids}")
            return udids[0]
    except subprocess.CalledProcessError:
        pass

    return ""


def resource_path(relative_path):
    """Return an absolute path for bundled resources."""
    global installedcheck
    system_base = "/usr/lib/althea"
    if os.path.exists(os.path.join(system_base, "althea")):
        installedcheck = True
        base_path = system_base
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)


# Global variables
ipa_path_exists = False
savedcheck = False
InsAltStore = subprocess.Popen(
    "test", stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=True
)

def _safe_cmdline_str(proc_info):
    cmdline = proc_info.get("cmdline")
    if not cmdline:
        return ""
    if isinstance(cmdline, str):
        return cmdline
    try:
        return " ".join(cmdline)
    except TypeError:
        return ""


def is_process_running(process_name, *, ignore_pid=None):
    """Check if a process containing `process_name` in its cmdline is running."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if ignore_pid is not None and proc.info.get("pid") == ignore_pid:
                continue
            cmdline_str = _safe_cmdline_str(proc.info)
            if process_name in cmdline_str:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False

def kill_process_by_name(process_name):
    """Kill all processes with the given name."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline_str = _safe_cmdline_str(proc.info)
            if process_name in cmdline_str:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue


def stop_services():
    for needle in (AltServer, AnisetteServer, Netmuxd):
        try:
            kill_process_by_name(needle)
        except Exception:
            pass


def is_anisette_accessible(timeout=0.75):
    try:
        with urllib.request.urlopen("http://127.0.0.1:6969", timeout=timeout) as resp:
            data = resp.read(2048)
        return b"{" in data
    except Exception:
        return False


def is_netmuxd_ready(timeout=0.5):
    try:
        env = os.environ.copy()
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        proc = subprocess.run(
            ["idevice_id", "-n", "-l"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception:
        return False


def is_altserver_running():
    # Match by full path placed in cmdline.
    return is_process_running(AltServer)


def start_anisette_server():
    if is_anisette_accessible(timeout=0.5):
        return
    try:
        kill_process_by_name(AnisetteServer)
    except Exception:
        pass
    log_info("Starting anisette-server")
    subprocess.Popen(
        [AnisetteServer, "-n", "127.0.0.1", "-p", "6969"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_netmuxd():
    if is_netmuxd_ready(timeout=0.25):
        return
    try:
        kill_process_by_name(Netmuxd)
    except Exception:
        pass
    log_info("Starting netmuxd")
    subprocess.Popen(
        [Netmuxd, "--disable-unix", "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_altserver():
    if is_altserver_running():
        return
    log_info("Starting AltServer")
    env = os.environ.copy()
    env["ALTSERVER_ANISETTE_SERVER"] = "http://127.0.0.1:6969"
    env["AVAHI_COMPAT_NOWARN"] = "1"

    # If we force USBMUXD_SOCKET_ADDRESS to netmuxd, AltServer may not see USB devices.
    # Prefer default usbmuxd when any USB device is present; otherwise use netmuxd.
    try:
        out = subprocess.check_output(["idevice_id", "-l"], stderr=subprocess.DEVNULL)
        has_usb = any(line.strip() for line in out.decode(errors="replace").splitlines())
    except Exception:
        has_usb = False

    if has_usb:
        env.pop("USBMUXD_SOCKET_ADDRESS", None)
        log_info("AltServer env: using default usbmuxd socket (USB present)")
    else:
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        log_info("AltServer env: using netmuxd socket (no USB devices)")

    subprocess.Popen(
        [f"{altheapath}/AltServer"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_anisette_server():
    log_info("Restart anisette-server requested")
    start_anisette_server()


def restart_netmuxd():
    log_info("Restart netmuxd requested")
    start_netmuxd()


def restart_altserver_process():
    log_info("Restart AltServer requested")
    try:
        kill_process_by_name(AltServer)
    except Exception:
        pass
    start_altserver()


def _is_usbmuxd_responsive(timeout_s: float = 2.0) -> bool:
    """Best-effort probe that usbmuxd is responding.

    We avoid pairing/lockdownd assumptions by using `idevice_id -l`.
    """
    try:
        proc = subprocess.run(
            ["idevice_id", "-l"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def restart_lockdownd_service():
    """Restart the host-side services involved in lockdownd communication.

    Many distros do not ship a `lockdownd.service`. When missing, fall back to
    restarting `usbmuxd`, which is the typical host-side daemon.
    """
    log_info("Restart lockdownd requested")

    # Try systemd units in order.
    units = ["lockdownd", "usbmuxd"]
    last_out = ""
    for unit in units:
        try:
            proc = subprocess.run(
                ["systemctl", "restart", unit],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=6,
                check=False,
            )
            out = (proc.stdout or b"").decode(errors="replace")
            last_out = out.strip()
            if proc.returncode == 0:
                log_info(f"Restart lockdownd: systemctl restart {unit} succeeded")
                return
            log_info(
                f"Restart lockdownd: systemctl restart {unit} failed rc={proc.returncode} output_tail={last_out[-250:]!r}"
            )
        except FileNotFoundError:
            raise RuntimeError("systemctl not found on this system")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out restarting {unit}")

    # If both units failed, surface a helpful message.
    if last_out:
        raise RuntimeError(last_out)
    raise RuntimeError("Failed to restart lockdownd/usbmuxd")
login_or_file_chooser = "login"
apple_id = "lol"
password = "lol"
Warnmsg = "warn"
Failmsg = "fail"
icon_name = "changes-prevent-symbolic"
command_six = Gtk.CheckMenuItem(label="Launch at Login")

# Paths
altheapath = os.path.join(
    os.environ.get("XDG_DATA_HOME") or f"{os.environ['HOME']}/.local/share",
    "althea",
)
AltServer = os.path.join(altheapath, "AltServer")
AnisetteServer = os.path.join(altheapath, "anisette-server")
Netmuxd = os.path.join(altheapath, "netmuxd")
AltStore = os.path.join(altheapath, "AltStore.ipa")
PATH = AltStore
AutoStart = resource_path("resources/AutoStart.sh")


def _settings_path():
    return os.path.join(altheapath, "config.json")


def load_settings():
    try:
        path = _settings_path()
        if not os.path.exists(path):
            return dict(DEFAULT_SETTINGS)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            settings.update(raw)
        return settings
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    os.makedirs(altheapath, exist_ok=True)
    path = _settings_path()
    payload = dict(DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        payload.update(settings)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def _log_path():
    return os.path.join(altheapath, "althea.log")


def setup_logging():
    os.makedirs(altheapath, exist_ok=True)

    # Avoid duplicate handlers if main() is called again.
    if getattr(setup_logging, "_configured", False):
        return

    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(_log_path(), encoding="utf-8")
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    setup_logging._configured = True


def log_info(message):
    try:
        logger.info(message)
    except Exception:
        pass


def log_exception(message):
    try:
        logger.exception(message)
    except Exception:
        pass

# Environment Exports
export_anisette = "export ALTSERVER_ANISETTE_SERVER='http://127.0.0.1:6969'"
export_netmuxd = "export USBMUXD_SOCKET_ADDRESS='127.0.0.1:27015'"

# Check version
with open(resource_path("resources/version"), "r", encoding="utf-8") as f:
    LocalVersion = f.readline().strip()


# Functions
def connectioncheck():
    try:
        urlopen("http://www.example.com", timeout=5)
        return True
    except:
        return False


def menu():
    menu = Gtk.Menu()

    if notify():
        command_upd = Gtk.MenuItem(label="Download Update")
        command_upd.connect("activate", showurl)
        menu.append(command_upd)

        menu.append(Gtk.SeparatorMenuItem())

    commands = [
        ("List Devices", lambda x: openwindow(DeviceListWindow)),  # NEW BUTTON
        ("About althea", on_abtdlg),
        ("Settings", lambda x: openwindow(SettingsWindow)),
        ("View Logs", lambda x: openwindow(LogWindow)),
        ("Install AltStore", altstoreinstall),
        ("Install an IPA file", altserverfile),
        ("Pair", lambda x: openwindow(PairWindow)),
        ("Main Window", lambda x: openwindow(MainWindow)),
        ("Restart AltServer", restart_altserver),
        ("Quit althea", lambda x: quitit()),
    ]

    for label, callback in commands:
        command = Gtk.MenuItem(label=label)
        command.connect("activate", callback)
        menu.append(command)
        if label == "Settings":
            menu.append(Gtk.SeparatorMenuItem())

    CheckRun11 = subprocess.run(f"test -e /usr/lib/althea/althea", shell=True)
    if installedcheck:
        global command_six
        CheckRun12 = subprocess.run(
            f"test -e $HOME/.config/autostart/althea.desktop", shell=True
        )
        if CheckRun12.returncode == 0:
            command_six.set_active(command_six)
        command_six.connect("activate", launchatlogin1)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(command_six)

    menu.show_all()
    return menu


def on_abtdlg(self):
    about = Gtk.AboutDialog()
    width = 100
    height = 100
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
        resource_path("resources/3.png"), width, height
    )
    about.set_logo(pixbuf)
    about.set_program_name("althea")
    about.set_version("0.5.0")
    about.set_authors(
        [
            "vyvir",
            "AltServer-Linux",
            "made by NyaMisty",
            "Provision",
            "made by Dadoum",
        ]
    )
    about.set_artists(["nebula"])
    about.set_comments("A GUI for AltServer-Linux written in Python.")
    about.set_website("https://github.com/vyvir/althea")
    about.set_website_label("Github")
    about.set_copyright("GUI by vyvir")
    about.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
    about.run()
    about.destroy()


def paircheck():
    try:
        log_info("paircheck: running idevicepair validate")
        pairchecking = subprocess.run(
            ["idevicepair", "validate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=2,
            check=False,
        )
        if pairchecking.returncode == 0:
            return False
        out = (pairchecking.stdout or b"").decode(errors="replace")
        log_info(f"paircheck: not paired (rc={pairchecking.returncode}) output_tail={out[-200:]!r}")
        return True
    except subprocess.TimeoutExpired:
        log_info("paircheck: timed out; treating as not paired")
        return True
    except Exception as e:
        log_info(f"paircheck: failed: {e!r}; treating as not paired")
        return True


def altstoreinstall(_):
    # Always show the install queue UI for this flow.
    try:
        install_queue_manager.ensure_window()
    except Exception:
        pass

    # Do not block the GTK main loop with usbmuxd/lockdownd calls.
    def _ui_warning_and_wait(iosv: str) -> bool:
        ok = {"val": False}
        ev = threading.Event()

        def _show():
            global Warnmsg
            Warnmsg = (
                f"""\niOS {iosv} is not supported by AltStore.\n"
                f"The lowest supported version is iOS 15.0.\n"
                f"You can still continue, but errors may occur.\n"""
            )
            dlg = WarningDialog(parent=None)
            dlg.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
            resp = dlg.run()
            dlg.destroy()
            ok["val"] = resp == Gtk.ResponseType.OK
            ev.set()
            return False

        GLib.idle_add(_show)
        ev.wait()
        return ok["val"]

    def _finish(needs_pair: bool):
        try:
            install_queue_manager.ensure_window()
        except Exception:
            pass
        if needs_pair:
            openwindow(PairWindow)
        else:
            win1()
        return False

    def _worker():
        iosv = ios_version()
        log_info(f"altstoreinstall: detected iOS version string={iosv!r}")
        try:
            parsed = version.parse(iosv)
        except Exception:
            parsed = version.parse("0.0")

        if parsed < version.parse("15.0"):
            if not _ui_warning_and_wait(iosv):
                return

        needs_pair = paircheck()
        GLib.idle_add(lambda: _finish(needs_pair))

    threading.Thread(target=_worker, daemon=True).start()


def altserverfile(_):
    if paircheck():
        global login_or_file_chooser
        login_or_file_chooser = "file_chooser"
        openwindow(PairWindow)
    else:
        win2 = FileChooserWindow()
        global ipa_path_exists
        if ipa_path_exists == True:
            global PATH
            PATH = win2.PATHFILE
            win1()
            ipa_path_exists = False


def notify():
    if (connectioncheck()) == True:
        LatestVersion = (
            urllib.request.urlopen(
                "https://raw.githubusercontent.com/vyvir/althea/main/resources/version"
            )
            .readline()
            .rstrip()
            .decode()
        )
        if LatestVersion > LocalVersion:
            Notify.init("MyProgram")
            n = Notify.Notification.new(
                "An update is available!",
                "Click 'Download Update' in the tray menu.",
                resource_path("resources/3.png"),
            )
            n.set_timeout(Notify.EXPIRES_DEFAULT)
            n.show()
            return True
        else:
            return False
    else:
        return False


def showurl(_):
    Gtk.show_uri_on_window(
        None, "https://github.com/vyvir/althea/releases", Gdk.CURRENT_TIME
    )
    quitit()


def openwindow(window):
    w = window()
    w.show_all()


def quitit():
    log_info("Quit requested")
    stop_services()
    Gtk.main_quit()


def restart_altserver(_):
    log_info("Restart AltServer (menu) requested")
    stop_services()
    sleep(1)  # Wait for processes to terminate
    subprocess.run("idevicepair pair", shell=True)
    subprocess.run(f"{Netmuxd} --disable-unix --host 127.0.0.1 &", shell=True)
    sleep(3)  # Give netmuxd more time to start

    # Set up environment with anisette server
    env = os.environ.copy()
    env["ALTSERVER_ANISETTE_SERVER"] = "http://127.0.0.1:6969"
    env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
    env["AVAHI_COMPAT_NOWARN"] = "1"

    subprocess.Popen(
        [f"{altheapath}/AltServer"],
        env=env
    )


class InstallTaskStatus:
    PENDING = "Pending"
    INSTALLING = "Installing"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELED = "Canceled"


class InstallTask:
    def __init__(self, ipa_path: str, apple_id: str, password: str):
        self.ipa_path = ipa_path
        self.apple_id = apple_id
        self.password = password
        self.created_at = time.time()
        self.status = InstallTaskStatus.PENDING
        self.progress = None  # float in [0,1] or None
        self.detail = ""
        self._proc = None
        self._cancel_requested = False


class InstallQueueWindow(Handy.Window):
    def __init__(self, manager):
        super().__init__(title="Install Queue")
        self.present()
        self.set_default_size(700, 420)
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_border_width(10)

        self.manager = manager

        self.handle = Handy.WindowHandle()
        self.add(self.handle)

        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.handle.add(self.vbox)

        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "Install Queue"
        self.vbox.pack_start(self.hb, False, True, 0)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.vbox.pack_start(self.scrolled, True, True, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.scrolled.add(self.listbox)

        self._rows_by_task = {}
        self.refresh()

    def refresh(self):
        # Full redraw is simplest and reliable.
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self._rows_by_task.clear()

        tasks = self.manager.snapshot()
        for idx, task in enumerate(tasks):
            row = self._make_row(task, idx)
            self._rows_by_task[id(task)] = row
            self.listbox.add(row)
        self.show_all()

    def update_task(self, task):
        # Called from GTK main loop.
        row = self._rows_by_task.get(id(task))
        if row is None:
            # If tasks changed, just redraw.
            self.refresh()
            return
        row._althea_update_from_task(task)

    def _make_row(self, task, idx: int):
        row = Gtk.ListBoxRow()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_border_width(6)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        outer.pack_start(top, False, False, 0)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        top.pack_start(title_box, True, True, 0)

        name = os.path.basename(str(task.ipa_path))
        title_lbl = Gtk.Label(label=name)
        title_lbl.set_xalign(0)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        title_box.pack_start(title_lbl, False, False, 0)

        subtitle_lbl = Gtk.Label(label=f"{task.status}")
        subtitle_lbl.set_xalign(0)
        subtitle_lbl.get_style_context().add_class("dim-label")
        subtitle_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_box.pack_start(subtitle_lbl, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        top.pack_start(btn_box, False, False, 0)

        up_btn = Gtk.Button(label="↑")
        down_btn = Gtk.Button(label="↓")
        cancel_btn = Gtk.Button(label="Cancel")

        btn_box.pack_start(up_btn, False, False, 0)
        btn_box.pack_start(down_btn, False, False, 0)
        btn_box.pack_start(cancel_btn, False, False, 0)

        pb = Gtk.ProgressBar()
        pb.set_show_text(True)
        pb.set_text(task.status)
        outer.pack_start(pb, False, False, 0)

        row.add(outer)

        def _on_up(_):
            self.manager.move_up(task)
            self.refresh()

        def _on_down(_):
            self.manager.move_down(task)
            self.refresh()

        def _on_cancel(_):
            self.manager.cancel(task)
            self.refresh()

        up_btn.connect("clicked", _on_up)
        down_btn.connect("clicked", _on_down)
        cancel_btn.connect("clicked", _on_cancel)

        def _update(task_obj):
            title = os.path.basename(str(task_obj.ipa_path))
            title_lbl.set_text(title)
            subtitle = task_obj.status
            if task_obj.detail:
                subtitle = f"{subtitle} — {task_obj.detail}"
            subtitle_lbl.set_text(subtitle)

            if task_obj.progress is None:
                pb.pulse()
            else:
                pb.set_fraction(max(0.0, min(1.0, float(task_obj.progress))))

            pb.set_text(task_obj.status)

            is_pending = task_obj.status == InstallTaskStatus.PENDING
            is_installing = task_obj.status == InstallTaskStatus.INSTALLING
            # Determine the current index dynamically so button state stays correct after reordering.
            try:
                current_idx = row.get_index()
            except AttributeError:
                current_idx = idx
            up_btn.set_sensitive(is_pending and current_idx > 0)
            down_btn.set_sensitive(is_pending)
            cancel_btn.set_sensitive(is_pending or is_installing)

        row._althea_update_from_task = _update
        row._althea_update_from_task(task)
        return row


class InstallQueueManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = []
        self._current = None
        self._window = None

    def ensure_window(self):
        if self._window is None:
            self._window = InstallQueueWindow(self)
        else:
            try:
                self._window.present()
            except Exception:
                pass
        return self._window

    def snapshot(self):
        with self._lock:
            return list(self._tasks)

    def enqueue(self, task: InstallTask):
        with self._lock:
            self._tasks.append(task)

        GLib.idle_add(lambda: self.ensure_window().refresh())
        self._maybe_start_next()

    def move_up(self, task: InstallTask):
        with self._lock:
            if task.status != InstallTaskStatus.PENDING:
                return
            try:
                i = self._tasks.index(task)
            except ValueError:
                return
            if i <= 0:
                return
            self._tasks[i - 1], self._tasks[i] = self._tasks[i], self._tasks[i - 1]

    def move_down(self, task: InstallTask):
        with self._lock:
            if task.status != InstallTaskStatus.PENDING:
                return
            try:
                i = self._tasks.index(task)
            except ValueError:
                return
            if i >= len(self._tasks) - 1:
                return
            self._tasks[i + 1], self._tasks[i] = self._tasks[i], self._tasks[i + 1]

    def cancel(self, task: InstallTask):
        with self._lock:
            if task.status == InstallTaskStatus.PENDING:
                task.status = InstallTaskStatus.CANCELED
                try:
                    self._tasks.remove(task)
                except ValueError:
                    pass
                return

            if task.status == InstallTaskStatus.INSTALLING:
                task._cancel_requested = True
                proc = task._proc
            else:
                return

        try:
            if proc is not None:
                proc.terminate()
        except Exception:
            pass

    def _maybe_start_next(self):
        with self._lock:
            if self._current is not None and self._current.status == InstallTaskStatus.INSTALLING:
                return

            # Pick first pending task
            next_task = None
            for t in self._tasks:
                if t.status == InstallTaskStatus.PENDING:
                    next_task = t
                    break
            if next_task is None:
                self._current = None
                return
            self._current = next_task
            next_task.status = InstallTaskStatus.INSTALLING
            next_task.detail = "Starting…"

        GLib.idle_add(lambda: self._notify_update(next_task))
        threading.Thread(target=self._run_task, args=(next_task,), daemon=True).start()

    def _notify_update(self, task: InstallTask):
        try:
            win = self.ensure_window()
            win.update_task(task)
        except Exception:
            pass
        return False

    def _run_task(self, task: InstallTask):
        try:
            self._run_altserver_install(task)
        except Exception as e:
            log_exception(f"Install task crashed: {e}")
            task.status = InstallTaskStatus.FAILED
            task.detail = "Internal error"
            GLib.idle_add(lambda: self._notify_update(task))
        finally:
            # Start next regardless of outcome.
            GLib.idle_add(lambda: self.ensure_window().refresh())
            self._maybe_start_next()

    def _run_altserver_install(self, task: InstallTask):
        # Resolve device + transport
        device = get_connected_device()
        udid = device.get("udid", "")
        transport = device.get("transport", "none")

        if not udid:
            task.status = InstallTaskStatus.FAILED
            task.detail = "No device detected"
            GLib.idle_add(lambda: self._notify_update(task))
            return

        # Prepare env
        env = os.environ.copy()
        env["ALTSERVER_ANISETTE_SERVER"] = "http://127.0.0.1:6969"
        env["AVAHI_COMPAT_NOWARN"] = "1"
        if transport == "network":
            env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        else:
            env.pop("USBMUXD_SOCKET_ADDRESS", None)

        # Spawn AltServer
        args = [AltServer, "-u", udid, "-a", task.apple_id, "-p", task.password, task.ipa_path]
        if any(a is None or a == "" for a in args):
            task.status = InstallTaskStatus.FAILED
            task.detail = "Missing arguments"
            GLib.idle_add(lambda: self._notify_update(task))
            return

        os.makedirs(altheapath, exist_ok=True)
        log_fp = open(_log_path(), "ab", buffering=0)

        task.detail = "Running…"
        GLib.idle_add(lambda: self._notify_update(task))

        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=1,
            universal_newlines=True,
        )
        task._proc = proc

        warn_prompt_seen = False
        two_factor_seen = False

        def _ask_yes_no(prompt_text: str) -> bool:
            result = {"ok": False}
            ev = threading.Event()

            def _show():
                dialog = Gtk.MessageDialog(
                    transient_for=self.ensure_window(),
                    flags=0,
                    message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.OK_CANCEL,
                    text=prompt_text,
                )
                resp = dialog.run()
                dialog.destroy()
                result["ok"] = resp == Gtk.ResponseType.OK
                ev.set()
                return False

            GLib.idle_add(_show)
            ev.wait()
            return result["ok"]

        def _ask_2fa_code() -> str:
            result = {"code": ""}
            ev = threading.Event()

            def _show():
                dialog = VerificationDialog(self.ensure_window())
                resp = dialog.run()
                if resp == Gtk.ResponseType.OK:
                    result["code"] = dialog.entry2.get_text().strip()
                else:
                    result["code"] = ""
                dialog.destroy()
                ev.set()
                return False

            GLib.idle_add(_show)
            ev.wait()
            return result["code"]

        # Progress parsing helpers
        re_progress = re.compile(r"(?:Signing\s+Progress|Progress)\s*:\s*([0-9eE+\-\.]+)")
        re_progress_pct = re.compile(r"Progress\s*:\s*([0-9.]+)\s*%")

        try:
            for line in proc.stdout:
                if line is None:
                    continue
                try:
                    log_fp.write(line.encode("utf-8", errors="replace"))
                except Exception:
                    pass

                if task._cancel_requested:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

                # Update progress
                m_pct = re_progress_pct.search(line)
                if m_pct:
                    try:
                        task.progress = float(m_pct.group(1)) / 100.0
                        task.detail = f"{float(m_pct.group(1)):.0f}%"
                        GLib.idle_add(lambda: self._notify_update(task))
                    except Exception:
                        pass
                else:
                    m = re_progress.search(line)
                    if m:
                        try:
                            val = float(m.group(1))
                            if 0.0 <= val <= 1.0:
                                task.progress = val
                                task.detail = f"{int(val * 100)}%"
                                GLib.idle_add(lambda: self._notify_update(task))
                        except Exception:
                            pass

                # Prompts
                if (not warn_prompt_seen) and "Are you sure you want to continue?" in line:
                    warn_prompt_seen = True
                    ok = _ask_yes_no("Continue installation?")
                    if not ok:
                        task.status = InstallTaskStatus.CANCELED
                        task.detail = "Canceled by user"
                        GLib.idle_add(lambda: self._notify_update(task))
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                    try:
                        if proc.stdin:
                            proc.stdin.write("\n")
                            proc.stdin.flush()
                    except Exception:
                        pass

                if (not two_factor_seen) and "Enter two factor code" in line:
                    two_factor_seen = True
                    code = _ask_2fa_code()
                    if not code:
                        task.status = InstallTaskStatus.CANCELED
                        task.detail = "2FA canceled"
                        GLib.idle_add(lambda: self._notify_update(task))
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                        break
                    try:
                        if proc.stdin:
                            proc.stdin.write(code + "\n")
                            proc.stdin.flush()
                    except Exception:
                        pass

                if "Notify: Installation Succeeded" in line:
                    task.progress = 1.0
                    task.status = InstallTaskStatus.SUCCEEDED
                    task.detail = "Done"
                    GLib.idle_add(lambda: self._notify_update(task))
                    break

                if "Could not" in line:
                    task.status = InstallTaskStatus.FAILED
                    task.detail = "Failed"
                    GLib.idle_add(lambda: self._notify_update(task))
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break

            rc = proc.wait(timeout=10)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
            raise
        finally:
            try:
                log_fp.close()
            except Exception:
                pass

        if task.status == InstallTaskStatus.INSTALLING:
            if task._cancel_requested:
                task.status = InstallTaskStatus.CANCELED
                task.detail = "Canceled"
            elif rc == 0:
                task.status = InstallTaskStatus.SUCCEEDED
                task.detail = "Done"
                task.progress = 1.0
            else:
                task.status = InstallTaskStatus.FAILED
                task.detail = f"Exit {rc}"
        GLib.idle_add(lambda: self._notify_update(task))


install_queue_manager = InstallQueueManager()


def enqueue_install(ipa_path: str, apple_id_value: str, password_value: str):
    try:
        install_queue_manager.ensure_window()
    except Exception:
        pass
    task = InstallTask(ipa_path=ipa_path, apple_id=apple_id_value, password=password_value)
    install_queue_manager.enqueue(task)


def use_saved_credentials():
    # Do not remove the shared application log file.
    dialog = Gtk.MessageDialog(
        flags=0,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.YES_NO,
        text="Do you want to login automatically?",
    )
    dialog.format_secondary_text("Your login and password have been saved earlier.")
    response = dialog.run()
    if response == Gtk.ResponseType.YES:
        global apple_id
        global password
        apple_id = keyring.get_password("althea", "apple_id")
        password = keyring.get_password("althea", "password")
        global savedcheck
        savedcheck = True
        # Enqueue install without creating a blank Login window.
        ipa = globals().get("PATH")
        if not ipa:
            ipa = f"{altheapath}/AltStore.ipa"
        enqueue_install(str(ipa), str(apple_id or ""), str(password or ""))
    else:
        try:
            apple_id = keyring.delete_password("althea", "apple_id")
            password = keyring.delete_password("althea", "password")
        except keyring.errors.KeyringError:
            pass
        win3 = Login()
        win3.show_all()
    dialog.destroy()


def win1():
    try:
        if keyring.get_password("althea", "apple_id"):
            use_saved_credentials()
        else:
            openwindow(Login)
    except keyring.errors.KeyringError:
        openwindow(Login)


def win2(_):
    try:
        if keyring.get_password("althea", "apple_id"):
            use_saved_credentials()
        else:
            openwindow(Login)
    except keyring.errors.KeyringError:
        openwindow(Login)


def actionCallback(notification, action, user_data=None):
    Gtk.show_uri_on_window(
        None, "https://github.com/vyvir/althea/releases", Gdk.CURRENT_TIME
    )
    quitit()


def launchatlogin1(_):
    global command_six
    if command_six.get_active():
        global AutoStart
        os.popen(AutoStart).read()
        return True
    else:
        silent_remove("$HOME/.config/autostart/althea.desktop")
        return False


def silent_remove(filename):
    try:
        os.remove(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def altstore_download(value):
    baseUrl = "https://cdn.altstore.io/file/altstore/apps.json"
    json_data = requests.get(baseUrl)
    if json_data.status_code == 200:
        data = json_data.json()
        for app in data["apps"]:
            if app["name"] == "AltStore":
                if value == "Check":
                    size = app["versions"][0]["size"]
                    return size == os.path.getsize(f"{(altheapath)}/AltStore.ipa")
                    break
                if value == "Download":
                    latest = app["versions"][0]["downloadURL"]
                    r = requests.get(latest, allow_redirects=True)
                    latest_filename = latest.split("/")[-1]
                    open(f"{(altheapath)}/{(latest_filename)}", "wb").write(r.content)
                    os.rename(
                        f"{(altheapath)}/{(latest_filename)}",
                        f"{(altheapath)}/AltStore.ipa",
                    )
                    subprocess.run(f"chmod 755 {(altheapath)}/AltStore.ipa", shell=True)
                    break
        return True
    else:
        return False


def ios_version():
    def _parse_product_version(text: str) -> str:
        for line in text.splitlines():
            if "ProductVersion:" in line:
                return line.split("ProductVersion:", 1)[1].strip()
        # When using -k ProductVersion, ideviceinfo often prints just the value.
        candidate = text.strip()
        if candidate and candidate[0].isdigit():
            return candidate
        return ""

    udid = get_connected_udid()
    log_info(f"ios_version: detected udid={udid!r}")

    env_netmuxd = os.environ.copy()
    env_netmuxd["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"

    # Important: USB devices should use the default usbmuxd socket first.
    attempts = []
    if udid:
        attempts.extend(
            [
                ("usb-default", ["ideviceinfo", "-u", udid, "-k", "ProductVersion"], None),
                ("usb-default", ["ideviceinfo", "-u", udid, "-s"], None),
                ("netmuxd", ["ideviceinfo", "-n", "-u", udid, "-k", "ProductVersion"], env_netmuxd),
                ("netmuxd", ["ideviceinfo", "-n", "-u", udid, "-s"], env_netmuxd),
            ]
        )
    else:
        attempts.extend(
            [
                ("usb-default", ["ideviceinfo", "-k", "ProductVersion"], None),
                ("usb-default", ["ideviceinfo", "-s"], None),
                ("netmuxd", ["ideviceinfo", "-n", "-k", "ProductVersion"], env_netmuxd),
                ("netmuxd", ["ideviceinfo", "-n", "-s"], env_netmuxd),
            ]
        )

    for mode, cmd, env in attempts:
        try:
            log_info(f"ios_version: running ({mode}) {' '.join(cmd)}")
            proc = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=4,
                check=False,
            )
            out = proc.stdout.decode(errors="replace")
            if proc.returncode != 0:
                tail = " | ".join(out.splitlines()[-3:])
                log_info(
                    f"ios_version: ({mode}) rc={proc.returncode} output_tail={tail!r}"
                )
                continue

            pv = _parse_product_version(out)
            if pv:
                log_info(f"ios_version: ({mode}) ProductVersion={pv}")
                return pv
            log_info(
                f"ios_version: ({mode}) could not parse ProductVersion from output_tail={out[-200:]!r}"
            )
        except Exception as e:
            log_info(f"ios_version: ({mode}) failed: {e!r}")
            continue

    log_info("ios_version: failed to detect ProductVersion, defaulting to 0.0")
    return "0.0"


# Classes
class SplashScreen(Handy.Window):
    def __init__(self):
        super().__init__(title="Loading")
        self.set_resizable(False)
        self.set_default_size(512, 288)
        self.present()
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_keep_above(True)

        self.mainBox = Gtk.Box(
            spacing=6,
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
        )
        self.add(self.mainBox)

        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            filename=resource_path("resources/4.png"),
            width=512,
            height=288,
            preserve_aspect_ratio=False,
        )
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.show()
        self.mainBox.pack_start(image, False, True, 0)

        self.lbl1 = Gtk.Label(label="Starting althea...")
        self.mainBox.pack_start(self.lbl1, False, False, 6)
        self.loadalthea = Gtk.ProgressBar()
        self.mainBox.pack_start(self.loadalthea, True, True, 0)
        self.t = threading.Thread(target=self.startup_process)
        self.t.start()
        self.wait_for_t(self.t)

    def _ui_set_text(self, text):
        def _apply():
            try:
                self.lbl1.set_text(text)
            except Exception:
                pass
            return False

        GLib.idle_add(_apply)

    def _ui_set_fraction(self, frac):
        def _apply():
            try:
                self.loadalthea.set_fraction(frac)
            except Exception:
                pass
            return False

        GLib.idle_add(_apply)

    def _is_anisette_accessible(self, timeout=1.0):
        return is_anisette_accessible(timeout=timeout)

    def _start_anisette_server(self):
        start_anisette_server()

    def _is_netmuxd_ready(self, timeout=0.5):
        return is_netmuxd_ready(timeout=timeout)

    def _prompt_anisette_unreachable(self, details):
        done = threading.Event()
        result = {"response": Gtk.ResponseType.CANCEL}

        def _run_dialog():
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.NONE,
                text="anisette-server is not accessible",
            )
            dialog.format_secondary_text(
                "althea cannot continue until it can reach http://127.0.0.1:6969.\n\n"
                "Click ‘Restart & Retry’ to restart anisette-server and try again.\n\n"
                f"Details: {details}"
            )
            dialog.add_button("Quit", Gtk.ResponseType.CANCEL)
            dialog.add_button("Restart & Retry", Gtk.ResponseType.OK)
            response = dialog.run()
            dialog.destroy()
            result["response"] = response
            done.set()
            return False

        GLib.idle_add(_run_dialog)
        done.wait()
        return result["response"]

    def wait_for_t(self, t):
        if not self.t.is_alive():
            global indicator
            try:
                indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
            except Exception:
                pass
            self.t.join()
            self.destroy()

            if SETTINGS.get("startup_mode") != "tray_only":
                openwindow(MainWindow)
        else:
            GLib.timeout_add(200, self.wait_for_t, self.t)

    def download_bin(self, name, link):
        arch_suffix = ""
        netmuxd_arch = ""

        if name == "netmuxd":
            if computer_cpu_platform == "x86_64":
                netmuxd_arch = "amd64"
            elif computer_cpu_platform == "aarch64":
                netmuxd_arch = "arm64"
            elif (
                "v7" in computer_cpu_platform
                or "ARM" in computer_cpu_platform
                or "hf" in computer_cpu_platform
            ):
                netmuxd_arch = "armv7"
            else:
                netmuxd_arch = "amd64"
            url = f"{link}/netmuxd-linux-{netmuxd_arch}"
        else:
            match computer_cpu_platform:
                case "x86_64":
                    url = f"{link}-x86_64"
                case "aarch64":
                    url = f"{link}-aarch64"
                case _:
                    if (
                        computer_cpu_platform.find("v7") != -1
                        or computer_cpu_platform.find("ARM") != -1
                        or computer_cpu_platform.find("hf") != -1
                    ):
                        url = f"{link}-armv7"
                    else:
                        self._ui_set_text(
                            "Could not identify the CPU architecture, downloading the x86_64 version..."
                        )
                        url = f"{link}-x86_64"

        r = requests.get(url, allow_redirects=True)
        open(f"{(altheapath)}/{name}", "wb").write(r.content)
        subprocess.run(f"chmod +x {(altheapath)}/{name}", shell=True)
        subprocess.run(f"chmod 755 {(altheapath)}/{name}", shell=True)

    def startup_process(self):
        self._ui_set_text("Checking if anisette-server is already running...")
        self._ui_set_fraction(0.1)
        anisette_running = self._is_anisette_accessible(timeout=0.5)
        if not os.path.isfile(f"{(altheapath)}/anisette-server"):
            self._ui_set_text("Downloading anisette-server...")
            self.download_bin(
                "anisette-server",
                "https://github.com/vyvir/althea/releases/download/v0.5.0/anisette-server",
            )
            self._ui_set_fraction(0.2)
            self._ui_set_text("Downloading Apple Music APK...")
            r = requests.get(
                "https://apps.mzstatic.com/content/android-apple-music-apk/applemusic.apk",
                allow_redirects=True,
            )
            open(f"{(altheapath)}/am.apk", "wb").write(r.content)
            os.makedirs(f"{(altheapath)}/lib/x86_64", exist_ok=True)
            self._ui_set_fraction(0.3)
            self._ui_set_text("Extracting necessary libraries...")
            CheckRunB = subprocess.run(
                f'unzip -j "{(altheapath)}/am.apk" "lib/x86_64/libstoreservicescore.so" -d "{(altheapath)}/lib/x86_64"',
                shell=True,
            )
            CheckRunC = subprocess.run(
                f'unzip -j "{(altheapath)}/am.apk" "lib/x86_64/libCoreADI.so" -d "{(altheapath)}/lib/x86_64"',
                shell=True,
            )
            silent_remove(f"{(altheapath)}/am.apk")
            self._ui_set_fraction(0.4)

        # Ensure anisette-server is reachable before continuing.
        # Start anisette first, but don't block here; it can take time to become reachable.
        self._ui_set_fraction(0.5)
        if anisette_running:
            self._ui_set_text("anisette-server is already running...")
        else:
            self._ui_set_text("Starting anisette-server...")
            self._start_anisette_server()

        self._ui_set_text("Downloading netmuxd for WiFi support...")
        self._ui_set_fraction(0.55)
        if not os.path.isfile(f"{(altheapath)}/netmuxd"):
            self.download_bin(
                "netmuxd",
                "https://github.com/jkcoxson/netmuxd/releases/latest/download",
            )

        self._ui_set_text("Starting netmuxd...")
        self._ui_set_fraction(0.6)
        # Only restart netmuxd if it doesn't look usable (faster on most launches).
        if not self._is_netmuxd_ready(timeout=0.25):
            try:
                kill_process_by_name(Netmuxd)
            except Exception:
                pass

            start_netmuxd()

            # Poll quickly instead of sleeping a fixed amount.
            for _ in range(10):
                if self._is_netmuxd_ready(timeout=0.2):
                    break
                sleep(0.2)

        if not os.path.isfile(f"{(altheapath)}/AltServer"):
            self.download_bin(
                "AltServer",
                "https://github.com/NyaMisty/AltServer-Linux/releases/download/v0.0.5/AltServer",
            )
            self._ui_set_text("Downloading AltServer...")
            self._ui_set_fraction(0.7)
        self._ui_set_fraction(0.8)
        if not os.path.isfile(f"{(altheapath)}/AltStore.ipa"):
            self._ui_set_text("Downloading AltStore...")
            altstore_download("Download")
        else:
            self._ui_set_text("Checking latest AltStore version...")
            if not altstore_download("Check"):
                self._ui_set_text("Downloading new version of AltStore...")
                altstore_download("Download")
        self._ui_set_text("Starting AltServer...")
        self._ui_set_fraction(1.0)

        if is_altserver_running():
            self._ui_set_text("AltServer is already running...")
        else:
            start_altserver()

        # Final check: anisette must be reachable before we consider startup complete.
        while not self._is_anisette_accessible(timeout=1.0):
            self._ui_set_text("anisette-server is not reachable")

            if not os.path.isfile(f"{(altheapath)}/anisette-server"):
                response = self._prompt_anisette_unreachable(
                    "anisette-server binary is missing."
                )
            else:
                response = self._prompt_anisette_unreachable(
                    "Connection failed (server not responding)."
                )

            if response == Gtk.ResponseType.OK:
                try:
                    kill_process_by_name(AnisetteServer)
                except Exception:
                    pass
                self._ui_set_text("Restarting anisette-server...")
                self._start_anisette_server()
                sleep(0.5)

                # If AltServer started before anisette was ready, it may have exited; ensure it's up.
                if not is_altserver_running():
                    start_altserver()
                continue

            GLib.idle_add(quitit)
            return 1

        return 0


class Login(Gtk.Window):
    def __init__(self):
        super().__init__(title="Login")
        self.present()
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_resizable(False)
        self.set_border_width(10)

        grid = Gtk.Grid()
        self.add(grid)

        label = Gtk.Label(label="Apple ID: ")
        label.set_justify(Gtk.Justification.LEFT)

        self.entry1 = Gtk.Entry()

        label1 = Gtk.Label(label="Password: ")
        label1.set_justify(Gtk.Justification.LEFT)

        self.entry = Gtk.Entry()
        self.entry.set_visibility(False)
        global icon_name
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, icon_name)
        self.entry.connect("icon-press", self.on_icon_toggled)

        self.button = Gtk.Button.new_with_label("Login")
        self.button.connect("clicked", self.on_click_me_clicked)

        grid.add(label)
        grid.attach(self.entry1, 1, 0, 2, 1)
        grid.attach_next_to(label1, label, Gtk.PositionType.BOTTOM, 1, 2)
        grid.attach(self.entry, 1, 2, 1, 1)
        grid.attach_next_to(self.button, self.entry, Gtk.PositionType.RIGHT, 1, 1)

        # Do not remove the shared application log file.

    def on_click_me_clicked1(self):
        # Deprecated path (kept for compatibility): enqueue using saved credentials.
        try:
            ipa = globals().get("PATH")
            if not ipa:
                ipa = f"{altheapath}/AltStore.ipa"
            enqueue_install(str(ipa), str(globals().get("apple_id") or ""), str(globals().get("password") or ""))
        finally:
            try:
                self.destroy()
            except Exception:
                pass

    def on_click_me_clicked(self, button):
        # Track where the installer output starts within the shared log.
        self._install_log_path = _log_path()
        self._install_log_offset = os.path.getsize(self._install_log_path) if os.path.exists(self._install_log_path) else 0
        try:
            if not keyring.get_password("althea", "apple_id"):
                self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    flags=0,
                    message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.YES_NO,
                    text="Do you want to save your login and password?",
                )
                dialog.format_secondary_text(
                    "This will allow you to login automatically."
                )
                response = dialog.run()
                if response == Gtk.ResponseType.YES:
                    apple_id = self.entry1.get_text().lower()
                    password = self.entry.get_text()
                    keyring.set_password("althea", "apple_id", apple_id)
                    keyring.set_password("althea", "password", password)
                dialog.destroy()
        except keyring.errors.KeyringError:
            pass
        apple_id_value = self.entry1.get_text().lower().strip()
        password_value = self.entry.get_text()
        ipa = globals().get("PATH")
        if not ipa:
            ipa = f"{altheapath}/AltStore.ipa"
        enqueue_install(str(ipa), apple_id_value, password_value)
        try:
            self.destroy()
        except Exception:
            pass

    def onclickmethread(self):
        # Legacy implementation kept for now; new installs go through the queue manager.
        if ios_version() >= "15.0":
            global savedcheck
            global apple_id
            global password
            if not savedcheck:
                apple_id = self.entry1.get_text().lower()
                password = self.entry.get_text()
            device = get_connected_device()
            UDID = device.get("udid", "")
            transport = device.get("transport", "none")
            global InsAltStore
            global PATH

            if not UDID:
                log_info("AltStore install: no device UDID found (USB and network both failed)")

                def _no_device_dialog():
                    global Failmsg
                    Failmsg = "No device detected. Connect via USB (trust this computer) or enable Wi-Fi sync and ensure netmuxd is running."
                    try:
                        dialog2 = FailDialog(self)
                        dialog2.run()
                        dialog2.destroy()
                    except Exception:
                        pass
                    try:
                        self.destroy()
                    except Exception:
                        pass
                    return False

                GLib.idle_add(_no_device_dialog)
                return

            if not isinstance(PATH, (str, bytes, os.PathLike)):
                log_info(f"AltStore install: PATH invalid ({PATH!r}); defaulting to bundled AltStore.ipa")
                PATH = f"{altheapath}/AltStore.ipa"

            if not isinstance(AltServer, (str, bytes, os.PathLike)) or not AltServer:
                log_info(f"AltStore install: AltServer path invalid ({AltServer!r}); defaulting")
                # Recompute using current altheapath.
                try:
                    globals()["AltServer"] = os.path.join(altheapath, "AltServer")
                except Exception:
                    pass

            # Validate we have no None args.
            args = [AltServer, "-u", UDID, "-a", apple_id, "-p", password, PATH]
            bad = [i for i, v in enumerate(args) if v is None]
            if bad:
                log_info(f"AltStore install: refusing to spawn AltServer; None args at positions={bad} args={args!r}")

                def _bad_args_dialog():
                    global Failmsg
                    Failmsg = "Internal error: missing value when launching AltServer. Check logs for details."
                    try:
                        dialog2 = FailDialog(self)
                        dialog2.run()
                        dialog2.destroy()
                    except Exception:
                        pass
                    try:
                        self.destroy()
                    except Exception:
                        pass
                    return False

                GLib.idle_add(_bad_args_dialog)
                return

            log_info(f"AltStore install: starting AltServer with udid={UDID!r} ipa={str(PATH)!r}")
            if os.path.isdir(f"{os.environ['HOME']}/.adi"):
                rmtree(f"{os.environ['HOME']}/.adi")

            # Set up environment with anisette server
            env = os.environ.copy()
            env["ALTSERVER_ANISETTE_SERVER"] = "http://127.0.0.1:6969"
            env["AVAHI_COMPAT_NOWARN"] = "1"

            # Only use netmuxd socket for network installs.
            if transport == "network":
                env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
                log_info("AltStore install env: using netmuxd socket (network device)")
            else:
                env.pop("USBMUXD_SOCKET_ADDRESS", None)
                log_info("AltStore install env: using default usbmuxd socket (USB device)")

            # Append AltServer install output into the shared application log.
            os.makedirs(altheapath, exist_ok=True)
            self._install_log_fp = open(_log_path(), "ab", buffering=0)

            InsAltStore = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=self._install_log_fp,
                stderr=self._install_log_fp,
                env=env
            )
        else:
            global Failmsg
            Failmsg = "iOS 15.0 or later is required."
            dialog2 = FailDialog(self)
            dialog2.run()
            dialog2.destroy()
            self.destroy()

    def install_process(self):
        global InsAltStore
        self._installing = True
        # Poll in the GTK main loop instead of busy-looping.
        # Note: _install_poll() is a GLib timeout callback and must return
        # True to continue polling and False to stop scheduling further polls.
        self._two_factor_time = 0
        self._install_log_path = getattr(self, "_install_log_path", _log_path())
        self._install_log_offset = getattr(self, "_install_log_offset", 0)

        # Poll in the GTK main loop instead of busy-looping.
        GLib.timeout_add(250, self._install_poll)
        return False

    def _tail_lines(self, path: str, n: int) -> str:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                block_size = 4096
                data = b""
                pos = end
                while pos > 0 and data.count(b"\n") <= n:
                    pos = max(0, pos - block_size)
                    f.seek(pos)
                    data = f.read(end - pos) + data
                    end = pos
                return b"\n".join(data.splitlines()[-n:]).decode(errors="replace")
        except FileNotFoundError:
            return ""
        except OSError:
            return ""

    def _read_log_text(self) -> str:
        try:
            with open(self._install_log_path, "rb") as f:
                try:
                    f.seek(int(self._install_log_offset), os.SEEK_SET)
                except Exception:
                    f.seek(0, os.SEEK_SET)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except OSError:
            return ""

    def _send_to_installer(self, payload: bytes) -> None:
        try:
            if InsAltStore is None:
                return
            if InsAltStore.stdin is None:
                return
            InsAltStore.stdin.write(payload)
            InsAltStore.stdin.flush()
        except Exception:
            pass

    def _install_poll(self):
        if not getattr(self, "_installing", False):
            return False

        global InsAltStore

        # If the installer process exited unexpectedly, stop polling.
        try:
            if InsAltStore is None or InsAltStore.poll() is not None:
                self._installing = False
                return False
        except Exception:
            self._installing = False
            return False

        log_text = self._read_log_text()
        if not log_text:
            return True

        if "Could not" in log_text:
            try:
                InsAltStore.terminate()
            except Exception:
                pass
            self._installing = False
            global Failmsg
            Failmsg = self._tail_lines(self._install_log_path, 6)
            dialog2 = FailDialog(self)
            dialog2.run()
            dialog2.destroy()
            self.destroy()
            return False

        if "Are you sure you want to continue?" in log_text and self._warn_time == 0:
            global Warnmsg
            # Use the installer output from this run (since offset).
            Warnmsg = "\n".join(log_text.splitlines()[-8:])
            dialog1 = WarningDialog(self)
            response1 = dialog1.run()
            if response1 == Gtk.ResponseType.OK:
                dialog1.destroy()
                self._send_to_installer(b"\n")
                self._warn_time = 1
                return True
            if response1 == Gtk.ResponseType.CANCEL:
                dialog1.destroy()
                try:
                    os.system(f"pkill -TERM -P {InsAltStore.pid}")
                except Exception:
                    pass
                self._warn_time = 1
                self.cancel()
                self._installing = False
                return False

            dialog1.destroy()
            self._warn_time = 1
            return True

        if "Enter two factor code" in log_text and self._two_factor_time == 0:
            dialog = VerificationDialog(self)
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                vercode = dialog.entry2.get_text() + "\n"
                self._send_to_installer(vercode.encode())
                self._two_factor_time = 1
                dialog.destroy()
                return True

            if response == Gtk.ResponseType.CANCEL:
                self._two_factor_time = 1
                try:
                    os.system(f"pkill -TERM -P {InsAltStore.pid}")
                except Exception:
                    pass
                self.cancel()
                dialog.destroy()
                self.destroy()
                self._installing = False
                return False

            dialog.destroy()
            self._two_factor_time = 1
            return True

        if "Notify: Installation Succeeded" in log_text:
            self._installing = False
            self.success()
            self.destroy()
            return False

        return True

    def success(self):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Success!",
        )
        dialog.format_secondary_text("Operation completed")
        dialog.run()
        dialog.destroy()

    def cancel(self):
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Cancelled",
        )
        dialog.format_secondary_text("Operation cancelled by user")
        dialog.run()
        dialog.destroy()

    def do_pulse(self, user_data):
        self.entry.progress_pulse()
        return True

    def on_icon_toggled(self, widget, icon, event):
        global icon_name
        if icon_name == "changes-prevent-symbolic":
            icon_name = "changes-allow-symbolic"
            self.entry.set_visibility(True)
        elif icon_name == "changes-allow-symbolic":
            icon_name = "changes-prevent-symbolic"
            self.entry.set_visibility(False)
        self.entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, icon_name)


class DeviceListWindow(Handy.Window):
    def __init__(self):
        super().__init__(title="Connected Devices")
        self.set_default_size(500, 450)  # Increased height slightly for new buttons
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_border_width(10)

        self.handle = Handy.WindowHandle()
        self.add(self.handle)

        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.handle.add(self.vbox)

        # Header
        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "Device Manager"
        self.vbox.pack_start(self.hb, False, True, 0)

        # Info Label
        info_lbl = Gtk.Label()
        info_lbl.set_markup(
            "<i>To enable Wi-Fi detection on Linux without iTunes/Finder:\nConnect via USB and click 'Enable WiFi Sync'.</i>"
        )
        info_lbl.set_justify(Gtk.Justification.CENTER)
        info_lbl.set_margin_top(5)
        info_lbl.set_margin_bottom(5)
        self.vbox.pack_start(info_lbl, False, False, 0)

        # Scrolled Window for the List
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.vbox.pack_start(self.scrolled, True, True, 0)

        # The List Box
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.scrolled.add(self.listbox)

        # Button Box for Actions
        self.btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.btn_box.set_halign(Gtk.Align.CENTER)
        self.btn_box.set_margin_top(10)

        # Button: Enable WiFi Sync
        self.enable_wifi_btn = Gtk.Button(label="Enable WiFi Sync")
        self.enable_wifi_btn.connect("clicked", self.on_enable_wifi_clicked)

        # Button: Refresh List
        self.refresh_btn = Gtk.Button(label="Refresh List")
        self.refresh_btn.connect("clicked", self.on_refresh_clicked)

        self.btn_box.pack_start(self.enable_wifi_btn, True, True, 0)
        self.btn_box.pack_start(self.refresh_btn, True, True, 0)

        self.vbox.pack_start(self.btn_box, False, False, 0)

        # Populate initial data
        self.populate_devices()

    def on_refresh_clicked(self, widget):
        # Remove all existing items
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        # Reload
        self.populate_devices()

    def on_enable_wifi_clicked(self, widget):
        """
        Attempts to enable 'Sync over WiFi' using pymobiledevice3.
        Updated syntax: --state on --udid <UDID>
        """

        # 1. Get the UDID of the connected USB device
        udid = ""
        try:
            usb_output = subprocess.check_output(
                ["idevice_id", "-l"], stderr=subprocess.DEVNULL
            )
            usb_devices = [
                u.strip() for u in usb_output.decode().splitlines() if u.strip()
            ]
            if usb_devices:
                udid = usb_devices[0]
            else:
                # Try network connection with proper environment
                env = os.environ.copy()
                env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
                net_output = subprocess.check_output(
                    ["idevice_id", "-n", "-l"],
                    env=env,
                    stderr=subprocess.DEVNULL,
                )
                net_devices = [
                    u.strip() for u in net_output.decode().splitlines() if u.strip()
                ]
                if net_devices:
                    udid = net_devices[0]
        except subprocess.CalledProcessError:
            pass

        if not udid:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="No Device Found",
            )
            dialog.format_secondary_text(
                "Could not find a connected device UDID to enable WiFi Sync.\n"
                "Please ensure your iPhone is connected via USB and appears in the list above."
            )
            dialog.run()
            dialog.destroy()
            return

        # 2. Construct the command based on the Help Output
        # Syntax: python -m pymobiledevice3 lockdown wifi-connections --state on --udid <UDID>
        cmd = [
            sys.executable,
            "-m",
            "pymobiledevice3",
            "lockdown",
            "wifi-connections",
            "--state",
            "on",
            "--udid",
            udid,
        ]

        # Show a quick dialog so the user knows something is happening
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.NONE,
            text="Enabling WiFi Sync...",
        )
        dialog.format_secondary_text(f"Sending command to device: {udid}")
        dialog.show()

        # Allow the UI to update
        while Gtk.events_pending():
            Gtk.main_iteration()

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()

        dialog.destroy()  # Close the "Enabling..." dialog

        if process.returncode == 0:
            # Success: Refresh list to show results
            self.on_refresh_clicked(None)

            success_dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Success!",
            )
            success_dialog.format_secondary_text(
                "WiFi Sync enabled successfully.\n"
                "You may need to unplug the USB cable for it to switch to Wi-Fi mode."
            )
            success_dialog.run()
            success_dialog.destroy()
        else:
            # Error handling
            err_msg = stderr.decode().strip() if stderr else "Unknown error"

            error_dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="Failed",
            )
            error_dialog.format_secondary_text(
                f"Could not enable WiFi Sync.\n\n"
                f"Details: {err_msg}\n\n"
                f"Command used: {' '.join(cmd)}"
            )
            error_dialog.run()
            error_dialog.destroy()

    def populate_devices(self):
        # 1. Check USB
        try:
            usb_output = subprocess.check_output(
                "idevice_id -l", shell=True, stderr=subprocess.DEVNULL
            )
            usb_devices = [
                u.strip() for u in usb_output.decode().splitlines() if u.strip()
            ]
        except subprocess.CalledProcessError:
            usb_devices = []

        # 2. Check Wi-Fi (Network) via system usbmuxd
        try:
            env = os.environ.copy()
            env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
            wifi_output = subprocess.check_output(
                ["idevice_id", "-n", "-l"],
                env=env,
                stderr=subprocess.DEVNULL,
            )
            wifi_devices = [
                w.strip() for w in wifi_output.decode().splitlines() if w.strip()
            ]
        except subprocess.CalledProcessError:
            wifi_devices = []

        # Strip "(Network)" or "(USB)" suffix from wifi devices for comparison
        def strip_suffix(device):
            return device.replace(" (Network)", "").replace(" (USB)", "").strip()

        # Filter out devices that are already connected via USB (prioritize USB)
        wifi_devices = [d for d in wifi_devices if strip_suffix(d) not in usb_devices]

        # --- Add USB Section ---
        row = Handy.ActionRow()
        row.set_title("USB Devices")
        row.set_subtitle("Wired Connection")
        row.set_activatable(False)
        self.listbox.add(row)

        if not usb_devices:
            row_sub = Handy.ActionRow()
            row_sub.set_title("No USB devices found.")
            row_sub.set_activatable(False)
            self.listbox.add(row_sub)
        else:
            for udid in usb_devices:
                row_dev = Handy.ActionRow()
                row_dev.set_title(udid)
                row_dev.set_subtitle("Connected via USB")
                row_dev.set_activatable(False)
                self.listbox.add(row_dev)

        # Separator
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(10)
        sep.set_margin_bottom(10)
        self.listbox.add(sep)

        # --- Add Wi-Fi Section ---
        row_wifi = Handy.ActionRow()
        row_wifi.set_title("Wi-Fi Devices")
        row_wifi.set_subtitle("Wireless Connection")
        row_wifi.set_activatable(False)
        self.listbox.add(row_wifi)

        if not wifi_devices:
            row_sub = Handy.ActionRow()
            row_sub.set_title("No Wi-Fi devices found.")
            row_sub.set_subtitle("Ensure 'Sync over WiFi' is enabled.")
            row_sub.set_activatable(False)
            self.listbox.add(row_sub)
        else:
            for udid in wifi_devices:
                row_dev = Handy.ActionRow()
                row_dev.set_title(udid)
                row_dev.set_subtitle("Connected via Wi-Fi")
                row_dev.set_activatable(False)
                self.listbox.add(row_dev)

        self.show_all()


class PairWindow(Handy.Window):
    def __init__(self):
        super().__init__(title="Pair your device")
        self.present()
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_resizable(False)
        self.set_border_width(20)

        self.handle = Handy.WindowHandle()
        self.add(self.handle)

        self.hbox = Gtk.Box(spacing=5, orientation=Gtk.Orientation.VERTICAL)
        self.handle.add(self.hbox)

        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "Pair your device"
        self.hbox.pack_start(self.hb, False, True, 0)

        pixbuf = Gtk.IconTheme.get_default().load_icon(
            "phone-apple-iphone-symbolic", 48, 0
        )
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.show()
        image.set_margin_top(5)
        self.hbox.pack_start(image, True, True, 0)

        lbl1 = Gtk.Label(
            label="Please make sure your device is connected to the computer.\nPress 'Pair' to pair your device."
        )
        lbl1.set_property("margin_left", 15)
        lbl1.set_property("margin_right", 15)
        lbl1.set_margin_top(5)
        lbl1.set_justify(Gtk.Justification.CENTER)
        self.hbox.pack_start(lbl1, False, False, 0)

        button = Gtk.Button(label="Pair")
        button.connect("clicked", self.on_info_clicked)
        button.set_property("margin_left", 150)
        button.set_property("margin_right", 150)
        self.hbox.pack_start(button, False, False, 10)

        # Window already contains the Handy.WindowHandle with self.hbox.

    def on_info_clicked(self, widget):
        # Pairing must be done over USB using the default usbmuxd socket.
        device = get_connected_device()
        udid = device.get("udid", "")
        transport = device.get("transport", "none")

        if not udid:
            dlg = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="No device detected.",
            )
            dlg.format_secondary_text(
                "Connect your iPhone via USB, unlock it, and tap 'Trust' when prompted."
            )
            dlg.run()
            dlg.destroy()
            return

        if transport != "usb":
            dlg = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Pairing requires a USB connection.",
            )
            dlg.format_secondary_text(
                "Connect your iPhone via USB to pair (Wi-Fi devices cannot be paired here)."
            )
            dlg.run()
            dlg.destroy()
            return

        # Disable button while pairing.
        try:
            widget.set_sensitive(False)
        except Exception:
            pass

        def _show_error(title: str, details: str):
            dlg = Gtk.MessageDialog(
                transient_for=self,
                flags=0,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=title,
            )
            if details:
                dlg.format_secondary_text(details)
            dlg.run()
            dlg.destroy()
            try:
                widget.set_sensitive(True)
            except Exception:
                pass
            return False

        def _worker():
            log_info(f"PairWindow: running idevicepair pair for udid={udid!r}")
            try:
                proc = subprocess.run(
                    ["idevicepair", "-u", udid, "pair"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=12,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                GLib.idle_add(
                    lambda: _show_error(
                        "Pairing timed out",
                        "Unlock your phone and accept the Trust prompt, then try again.",
                    )
                )
                return

            out = (proc.stdout or b"").decode(errors="replace")
            log_info(f"PairWindow: idevicepair pair rc={proc.returncode} output_tail={out[-300:]!r}")

            if proc.returncode != 0:
                # Helpful hint for the common case.
                hint = "Unlock your phone and tap 'Trust'."
                if out:
                    hint = (out.strip() + "\n\n" + hint).strip()
                GLib.idle_add(lambda: _show_error("Pairing failed", hint))
                return

            # Validate immediately.
            try:
                v = subprocess.run(
                    ["idevicepair", "-u", udid, "validate"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=5,
                    check=False,
                )
                vout = (v.stdout or b"").decode(errors="replace")
                log_info(f"PairWindow: idevicepair validate rc={v.returncode} output_tail={vout[-300:]!r}")
            except Exception:
                pass

            def _success():
                try:
                    self.destroy()
                except Exception:
                    pass
                try:
                    install_queue_manager.ensure_window()
                except Exception:
                    pass
                # Continue original flow.
                global login_or_file_chooser
                global PATH
                if login_or_file_chooser == "file_chooser":
                    win2 = FileChooserWindow()
                else:
                    PATH = f"{(altheapath)}/AltStore.ipa"
                    win1()
                global ipa_path_exists
                if ipa_path_exists == True:
                    PATH = win2.PATHFILE
                    win1()
                    ipa_path_exists = False
                login_or_file_chooser = "login"
                return False

            GLib.idle_add(_success)

        threading.Thread(target=_worker, daemon=True).start()


class FileChooserWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="File chooser")
        box = Gtk.Box()
        self.add(box)

        dialog = Gtk.FileChooserDialog(
            title="Please choose a file", parent=self, action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,
            Gtk.ResponseType.OK,
        )

        self.add_filters(dialog)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            self.PATHFILE = dialog.get_filename()
            global ipa_path_exists
            ipa_path_exists = True
        elif response == Gtk.ResponseType.CANCEL:
            self.destroy()

        dialog.destroy()

    def add_filters(self, dialog):
        filter_ipa = Gtk.FileFilter()
        filter_ipa.set_name("IPA files")
        filter_ipa.add_pattern("*.ipa")
        dialog.add_filter(filter_ipa)

        filter_any = Gtk.FileFilter()
        filter_any.set_name("Any files")
        filter_any.add_pattern("*")
        dialog.add_filter(filter_any)


class VerificationDialog(Gtk.Dialog):
    def __init__(self, parent):
        if not savedcheck:
            super().__init__(title="Verification code", transient_for=parent, flags=0)
        else:
            super().__init__(title="Verification code", flags=0)
        self.present()
        self.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK,
            Gtk.ResponseType.OK,
        )
        self.set_resizable(True)
        self.set_border_width(10)

        labelhelp = Gtk.Label(label="Enter the verification \ncode on your device: ")
        labelhelp.set_justify(Gtk.Justification.CENTER)

        self.entry2 = Gtk.Entry()

        box = self.get_content_area()
        box.add(labelhelp)
        box.add(self.entry2)
        self.show_all()


class WarningDialog(Gtk.Dialog):
    def __init__(self, parent):
        global Warnmsg
        super().__init__(title="Warning", transient_for=parent, flags=0)
        self.present()
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        self.set_resizable(False)
        self.set_border_width(10)

        labelhelp = Gtk.Label(label="Are you sure you want to continue?")
        labelhelp.set_justify(Gtk.Justification.CENTER)

        labelhelp1 = Gtk.Label(label=Warnmsg)
        labelhelp1.set_justify(Gtk.Justification.CENTER)
        labelhelp1.set_line_wrap(True)
        labelhelp1.set_max_width_chars(48)
        labelhelp1.set_selectable(True)

        box = self.get_content_area()
        box.add(labelhelp)
        box.add(labelhelp1)
        self.show_all()


class FailDialog(Gtk.Dialog):
    def __init__(self, parent):
        global Failmsg
        super().__init__(title="Fail", transient_for=parent, flags=0)
        self.present()
        self.add_buttons(Gtk.STOCK_OK, Gtk.ResponseType.OK)
        self.set_resizable(False)
        self.set_border_width(10)

        labelhelp = Gtk.Label(label="AltServer has failed.")
        labelhelp.set_justify(Gtk.Justification.CENTER)

        labelhelp1 = Gtk.Label(label=Failmsg)
        labelhelp1.set_justify(Gtk.Justification.CENTER)
        labelhelp1.set_line_wrap(True)
        labelhelp1.set_max_width_chars(48)
        labelhelp1.set_selectable(True)

        box = self.get_content_area()
        box.add(labelhelp)
        box.add(labelhelp1)
        self.show_all()


class Oops(Handy.Window):
    def __init__(self, markup_text, pixbuf_icon):
        super().__init__(title="Error")
        self.present()
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_resizable(False)
        self.set_size_request(450, 100)
        self.set_border_width(10)

        handle = Handy.WindowHandle()
        self.add(handle)
        box = Gtk.VBox()
        vb = Gtk.VBox(spacing=0, orientation=Gtk.Orientation.VERTICAL)

        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "Error"
        vb.pack_start(self.hb, False, True, 0)

        pixbuf = Gtk.IconTheme.get_default().load_icon(pixbuf_icon, 48, 0)
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.show()
        image.set_margin_top(10)
        vb.pack_start(image, True, True, 0)

        lbl1 = Gtk.Label()
        lbl1.set_justify(Gtk.Justification.CENTER)
        lbl1.set_markup(markup_text)
        lbl1.set_property("margin_left", 15)
        lbl1.set_property("margin_right", 15)
        lbl1.set_margin_top(10)

        button = Gtk.Button(label="OK")
        button.set_property("margin_left", 125)
        button.set_property("margin_right", 125)
        button.connect("clicked", self.on_info_clicked2)

        handle.add(vb)
        vb.pack_start(lbl1, expand=False, fill=True, padding=0)
        vb.pack_start(button, False, False, 10)
        box.add(vb)
        self.add(box)
        self.show_all()

    def on_info_clicked2(self, widget):
        quitit()


class LogWindow(Handy.Window):
    def __init__(self):
        super().__init__(title="Logs")
        self.present()
        self.set_default_size(800, 500)
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_border_width(10)

        self.handle = Handy.WindowHandle()
        self.add(self.handle)

        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.handle.add(self.vbox)

        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "Logs"
        self.vbox.pack_start(self.hb, False, True, 0)

        self._follow_source_id = None
        self._follow_pos = 0

        follow_btn = Gtk.ToggleButton(label="Follow")
        follow_btn.connect("toggled", self.on_follow_toggled)
        self.hb.pack_end(follow_btn)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        self.hb.pack_end(refresh_btn)

        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_vexpand(True)
        self.vbox.pack_start(self.scrolled, True, True, 0)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_monospace(True)
        self.buffer = self.textview.get_buffer()
        self.scrolled.add(self.textview)

        self.footer = Gtk.Label(label="")
        self.footer.set_halign(Gtk.Align.START)
        self.footer.set_selectable(True)
        self.vbox.pack_start(self.footer, False, False, 0)

        self.refresh()
        self.show_all()

    def _read_tail(self, path, max_bytes=200_000):
        try:
            if not os.path.exists(path):
                return ""
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = max(0, size - max_bytes)
                f.seek(start, os.SEEK_SET)
                data = f.read()
            # If we started in the middle, drop the first partial line.
            if start > 0:
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1 :]
            return data.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def refresh(self):
        path = _log_path()
        text = self._read_tail(path)
        if not text:
            text = "(no logs yet)"
        self.buffer.set_text(text)
        self.footer.set_text(f"Log file: {path}")

        # Move follow position to the current end of file.
        try:
            self._follow_pos = os.path.getsize(path)
        except Exception:
            self._follow_pos = 0

        # Scroll to bottom.
        try:
            end_iter = self.buffer.get_end_iter()
            self.textview.scroll_to_iter(end_iter, 0.0, True, 0.0, 1.0)
        except Exception:
            pass

    def on_follow_toggled(self, btn):
        enabled = bool(btn.get_active())
        if enabled:
            # Start following from the current end after a refresh.
            self.refresh()
            if self._follow_source_id is None:
                self._follow_source_id = GLib.timeout_add(500, self._follow_tick)
        else:
            if self._follow_source_id is not None:
                try:
                    GLib.source_remove(self._follow_source_id)
                except Exception:
                    pass
            self._follow_source_id = None

    def _append_text(self, text: str):
        if not text:
            return
        try:
            # Keep the buffer from growing without bound.
            max_chars = 1_000_000
            cur_chars = self.buffer.get_char_count()
            if cur_chars > max_chars:
                start_iter = self.buffer.get_start_iter()
                trim_iter = self.buffer.get_iter_at_offset(cur_chars - max_chars)
                self.buffer.delete(start_iter, trim_iter)

            end_iter = self.buffer.get_end_iter()
            self.buffer.insert(end_iter, text)
            end_iter = self.buffer.get_end_iter()
            self.textview.scroll_to_iter(end_iter, 0.0, True, 0.0, 1.0)
        except Exception:
            pass

    def _follow_tick(self):
        path = _log_path()
        try:
            if not os.path.exists(path):
                return True

            size = os.path.getsize(path)
            # If the file was truncated/rotated, restart from 0.
            if size < self._follow_pos:
                self._follow_pos = 0

            if size == self._follow_pos:
                return True

            with open(path, "rb") as f:
                f.seek(self._follow_pos, os.SEEK_SET)
                data = f.read()
                self._follow_pos = f.tell()

            self._append_text(data.decode("utf-8", errors="replace"))
            return True
        except Exception:
            return True


class SettingsWindow(Handy.Window):
    def __init__(self):
        super().__init__(title="Settings")
        self.present()
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_resizable(False)
        self.set_border_width(10)

        self.handle = Handy.WindowHandle()
        self.add(self.handle)

        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.handle.add(self.vbox)

        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "Settings"
        self.vbox.pack_start(self.hb, False, True, 0)

        row = Handy.ActionRow()
        row.set_title("Start in system tray only")
        row.set_subtitle("If enabled, althea will start without opening the main window.")

        self.tray_only_switch = Gtk.Switch()
        self.tray_only_switch.set_valign(Gtk.Align.CENTER)
        self.tray_only_switch.set_active(SETTINGS.get("startup_mode") == "tray_only")
        self.tray_only_switch.connect("notify::active", self.on_tray_only_toggled)
        row.add(self.tray_only_switch)

        self.vbox.pack_start(row, False, False, 0)

        # Services
        services_lbl = Gtk.Label()
        services_lbl.set_markup("<b>Services</b>")
        services_lbl.set_halign(Gtk.Align.START)
        services_lbl.set_margin_top(10)
        self.vbox.pack_start(services_lbl, False, False, 0)

        self.row_anisette = Handy.ActionRow()
        self.row_anisette.set_title("anisette-server")
        self.btn_anisette = Gtk.Button(label="Restart")
        self.btn_anisette.connect("clicked", self.on_restart_anisette)
        self.row_anisette.add(self.btn_anisette)
        self.vbox.pack_start(self.row_anisette, False, False, 0)

        self.row_netmuxd = Handy.ActionRow()
        self.row_netmuxd.set_title("netmuxd")
        self.btn_netmuxd = Gtk.Button(label="Restart")
        self.btn_netmuxd.connect("clicked", self.on_restart_netmuxd)
        self.row_netmuxd.add(self.btn_netmuxd)
        self.vbox.pack_start(self.row_netmuxd, False, False, 0)

        self.row_altserver = Handy.ActionRow()
        self.row_altserver.set_title("AltServer")
        self.btn_altserver = Gtk.Button(label="Restart")
        self.btn_altserver.connect("clicked", self.on_restart_altserver)
        self.row_altserver.add(self.btn_altserver)
        self.vbox.pack_start(self.row_altserver, False, False, 0)

        self.row_lockdownd = Handy.ActionRow()
        self.row_lockdownd.set_title("lockdownd")
        self.btn_lockdownd = Gtk.Button(label="Restart")
        self.btn_lockdownd.connect("clicked", self.on_restart_lockdownd)
        self.row_lockdownd.add(self.btn_lockdownd)
        self.vbox.pack_start(self.row_lockdownd, False, False, 0)

        logs_row = Handy.ActionRow()
        logs_row.set_title("Logs")
        logs_row.set_subtitle("View timestamped application logs")
        logs_btn = Gtk.Button(label="View")
        logs_btn.connect("clicked", lambda _b: openwindow(LogWindow))
        logs_row.add(logs_btn)
        self.vbox.pack_start(logs_row, False, False, 0)

        self.status_label = Gtk.Label(label="")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_line_wrap(True)
        self.status_label.set_selectable(True)
        self.status_label.set_margin_top(10)
        self.vbox.pack_start(self.status_label, False, False, 0)

        self.refresh_statuses()
        self.show_all()

    def on_tray_only_toggled(self, switch, _param):
        global SETTINGS
        tray_only = bool(switch.get_active())
        SETTINGS["startup_mode"] = "tray_only" if tray_only else "window_and_tray"
        save_settings(SETTINGS)

    def refresh_statuses(self):
        try:
            if is_anisette_accessible(timeout=0.5):
                self.row_anisette.set_subtitle("Accessible on http://127.0.0.1:6969")
            else:
                self.row_anisette.set_subtitle("Not accessible")

            if is_netmuxd_ready(timeout=0.35):
                self.row_netmuxd.set_subtitle("Ready (USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015)")
            else:
                self.row_netmuxd.set_subtitle("Not ready")

            if is_altserver_running():
                self.row_altserver.set_subtitle("Running")
            else:
                self.row_altserver.set_subtitle("Not running")

            if _is_usbmuxd_responsive(timeout_s=1.5):
                self.row_lockdownd.set_subtitle("usbmuxd responsive")
            else:
                self.row_lockdownd.set_subtitle("usbmuxd not responding")
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.status_label.set_text(text)
        except Exception:
            pass

    def _restart_with_feedback(
        self,
        *,
        name,
        restart_fn,
        check_fn,
        ok_text,
        row,
        button,
        timeout_s=6.0,
    ):
        def _ui_start():
            button.set_sensitive(False)
            row.set_subtitle("Restarting…")
            self._set_status(f"Restarting {name}…")
            return False

        GLib.idle_add(_ui_start)

        def _work():
            try:
                restart_fn()

                # Poll for readiness so the user gets a clear "reconnected" signal.
                deadline = GLib.get_monotonic_time() + int(timeout_s * 1_000_000)
                ready = False
                while GLib.get_monotonic_time() < deadline:
                    try:
                        if check_fn():
                            ready = True
                            break
                    except Exception:
                        pass
                    sleep(0.25)

                def _ui_done():
                    button.set_sensitive(True)
                    if ready:
                        row.set_subtitle(ok_text)
                        self._set_status(f"{name} is ready.")
                    else:
                        row.set_subtitle("Restarted, but still not reachable")
                        self._set_status(f"{name} did not become ready (timed out).")
                    return False

                GLib.idle_add(_ui_done)
            except Exception as e:
                def _ui_err():
                    button.set_sensitive(True)
                    row.set_subtitle("Restart failed")
                    self._set_status(f"Failed to restart {name}: {e}")
                    return False

                GLib.idle_add(_ui_err)

        threading.Thread(target=_work, daemon=True).start()

    def _run_in_thread(self, fn):
        def _wrap():
            try:
                fn()
            finally:
                GLib.idle_add(self.refresh_statuses)

        threading.Thread(target=_wrap, daemon=True).start()

    def on_restart_anisette(self, _btn):
        self._restart_with_feedback(
            name="anisette-server",
            restart_fn=restart_anisette_server,
            check_fn=lambda: is_anisette_accessible(timeout=0.5),
            ok_text="Accessible on http://127.0.0.1:6969",
            row=self.row_anisette,
            button=self.btn_anisette,
        )

    def on_restart_netmuxd(self, _btn):
        self._restart_with_feedback(
            name="netmuxd",
            restart_fn=restart_netmuxd,
            check_fn=lambda: is_netmuxd_ready(timeout=0.35),
            ok_text="Ready (USBMUXD_SOCKET_ADDRESS=127.0.0.1:27015)",
            row=self.row_netmuxd,
            button=self.btn_netmuxd,
        )

    def on_restart_altserver(self, _btn):
        self._restart_with_feedback(
            name="AltServer",
            restart_fn=restart_altserver_process,
            check_fn=is_altserver_running,
            ok_text="Running",
            row=self.row_altserver,
            button=self.btn_altserver,
        )

    def on_restart_lockdownd(self, _btn):
        self._restart_with_feedback(
            name="lockdownd",
            restart_fn=restart_lockdownd_service,
            check_fn=lambda: _is_usbmuxd_responsive(timeout_s=1.5),
            ok_text="usbmuxd responsive",
            row=self.row_lockdownd,
            button=self.btn_lockdownd,
            timeout_s=8.0,
        )


# -----------------------------------------------------------------------------


# Main function
def main():
    GLib.set_prgname("althea")
    global altheapath
    if not os.path.exists(altheapath):
        os.mkdir(altheapath)

    setup_logging()
    log_info("althea starting")

    global SETTINGS
    SETTINGS = load_settings()
    log_info(f"Settings loaded: startup_mode={SETTINGS.get('startup_mode')}")

    # Check if althea is already running
    if is_process_running("main.py", ignore_pid=os.getpid()):
        # If already running, just show the main window instead of starting new processes
        print("althea is already running. Showing main window...")
        openwindow(MainWindow)
        Gtk.main()
        return

    # Try to create a tray indicator if AppIndicator is available.
    try:
        global indicator
        indicator = appindicator.Indicator.new(
            "althea-tray-icon",
            resource_path("resources/1.png"),
            appindicator.IndicatorCategory.APPLICATION_STATUS,
        )
        indicator.set_status(appindicator.IndicatorStatus.ACTIVE)
        indicator.set_menu(menu())
        indicator.set_status(appindicator.IndicatorStatus.PASSIVE)
    except Exception:
        indicator = None

    if connectioncheck():
        openwindow(SplashScreen)
    else:
        markup_text = "althea is unable to connect to the Internet.\nPlease connect to the Internet and restart althea."
        pixbuf_icon = "network-wireless-no-route-symbolic"
        Oops(markup_text, pixbuf_icon)
    Handy.init()
    Gtk.main()


class MainWindow(Handy.Window):
    def __init__(self):
        super().__init__(title="althea")
        self.set_default_size(400, 500)
        self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)
        self.set_border_width(10)

        self.handle = Handy.WindowHandle()
        self.add(self.handle)

        self.vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.handle.add(self.vbox)

        # Header
        self.hb = Handy.HeaderBar()
        self.hb.set_show_close_button(True)
        self.hb.props.title = "althea"
        self.vbox.pack_start(self.hb, False, True, 0)

        # Logo
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
            resource_path("resources/3.png"), 64, 64
        )
        image = Gtk.Image.new_from_pixbuf(pixbuf)
        image.set_margin_top(10)
        self.vbox.pack_start(image, False, False, 0)

        # Main buttons container
        self.grid = Gtk.Grid()
        self.grid.set_row_spacing(10)
        self.grid.set_column_spacing(10)
        self.grid.set_margin_top(20)
        self.grid.set_margin_bottom(20)
        self.grid.set_margin_start(20)
        self.grid.set_margin_end(20)
        self.vbox.pack_start(self.grid, True, True, 0)

        # Create buttons matching the tray menu
        self.create_main_buttons()

        # Restart Services button
        restart_btn = Gtk.Button(label="Restart AltServer")
        restart_btn.connect("clicked", restart_altserver)
        restart_btn.set_margin_top(10)
        self.vbox.pack_start(restart_btn, False, False, 0)

        # Quit button
        quit_btn = Gtk.Button(label="Quit althea")
        quit_btn.connect("clicked", lambda x: quitit())
        self.vbox.pack_start(quit_btn, False, False, 0)

    def create_main_buttons(self):
        # Define the buttons and their callbacks
        buttons = [
            ("List Devices", lambda x: openwindow(DeviceListWindow)),
            ("About althea", on_abtdlg),
            ("Settings", lambda x: openwindow(SettingsWindow)),
            ("View Logs", lambda x: openwindow(LogWindow)),
            ("Install AltStore", altstoreinstall),
            ("Install an IPA file", altserverfile),
            ("Pair", lambda x: openwindow(PairWindow)),
        ]

        # Create a grid of buttons
        row = 0
        col = 0
        for label, callback in buttons:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", callback)
            btn.set_hexpand(True)
            self.grid.attach(btn, col, row, 1, 1)

            # Move to next position (2 buttons per row)
            col += 1
            if col > 1:  # 2 columns
                col = 0
                row += 1


if __name__ == "__main__":
    main()

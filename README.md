# althea
<img src="https://github.com/vyvir/althea/blob/main/resources/screenshot.png" alt="althea screenshot">

althea is a GUI for AltServer-Linux that allows to easily sideload apps onto an iPhone, an iPad, or an iPod Touch. It supports x86_64, aarch64, and armv7.

This app is in a very early state, so if you're experiencing issues or want to help, you can create a [pull request](https://github.com/vyvir/althea/pulls), [report an issue](https://github.com/vyvir/althea/issues), or join [the Discord server](https://discord.gg/DZwRbyXq5Z).

## Instructions

### Dependencies

Ubuntu:
```
sudo apt install software-properties-common
```

```
sudo add-apt-repository universe -y
```

```
sudo apt-get install binutils python3-pip python3-requests python3-keyring git gir1.2-appindicator3-0.1 usbmuxd libimobiledevice6 libimobiledevice-utils wget curl libavahi-compat-libdnssd-dev zlib1g-dev unzip usbutils libhandy-1-dev gir1.2-notify-0.7 psmisc
```

Fedora:
```
sudo dnf install binutils python3-pip python3-requests python3-keyring git libappindicator-gtk3 usbmuxd libimobiledevice-devel libimobiledevice-utils wget curl avahi-compat-libdns_sd-devel dnf-plugins-core unzip usbutils psmisc libhandy1-devel
```
Arch Linux:
```
sudo pacman -S binutils wget curl git python-pip python-requests python-gobject python-keyring libappindicator-gtk3 usbmuxd libimobiledevice avahi zlib unzip usbutils psmisc libhandy
```

OpenSUSE:
```
sudo zypper in binutils wget curl git python3-pip python3-requests python3-keyring python3-gobject-Gdk libhandy-devel libappindicator3-1 typelib-1_0-AppIndicator3-0_1 imobiledevice-tools libdns_sd libnotify-devel psmisc
```

### Running althea

Once the dependencies are installed, run the following commands:
```
git clone https://github.com/vyvir/althea
```

```
cd althea
```

```
python3 main.py
```

Tip: use a local venv with uv to keep system packages untouched:
```bash
uv venv .venv
uv pip install -r requirements.txt
source .venv/bin/activate
python main.py
```

That's it! Have fun with althea!

## Wi-Fi refresh / network mode

althea can use devices connected over USB and (when available) over Wi-Fi. On Linux, Wi‑Fi support depends on `libimobiledevice` being able to see your iPhone via network discovery.

### Verify Wi‑Fi discovery works

1. Ensure your iPhone and Linux PC are on the same LAN.
2. Pair once over USB and accept the Trust prompt.
3. Unplug USB and run:

```bash
idevice_id -n -l
```

If this prints your device UDID, Wi‑Fi connectivity is available and althea can use it.

### If `idevice_id -n -l` shows nothing

- Ensure `usbmuxd` is running: `systemctl status usbmuxd`
- Ensure mDNS discovery works: `avahi-browse -rt _apple-mobdev2._tcp`
- Ensure your firewall allows mDNS (UDP 5353) and iOS device services on your LAN.

Some iOS setups require enabling “Sync with this iPhone over Wi‑Fi” once using macOS Finder or Windows iTunes. After that, network discovery on Linux typically starts returning the UDID.

If you use Docker/VM networking, make sure your LAN interface is preferred for routes to your phone (Docker bridge routes can interfere with discovery/connectivity).

### Run althea with a systemd user service (optional)

Use this only if you really need a user service. If it breaks your launcher or tray, use the desktop autostart below instead. Replace `/path/to/althea` with the absolute path to your cloned repo.

One-time setup:
```bash
mkdir -p ~/.config/systemd/user
# import your current session env so GTK can reach the display/dbus
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
# check what display backend you use (prints x11 or wayland)
echo "$XDG_SESSION_TYPE"
cat > ~/.config/systemd/user/althea.service <<'EOF'
[Unit]
Description=Althea AltServer tray
After=graphical-session.target
Wants=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/path/to/althea/.venv/bin/python /path/to/althea/main.py  # or /usr/bin/python if you skip the venv
WorkingDirectory=/path/to/althea
Restart=on-failure
# carry the session env needed by GTK and dbus
PassEnvironment=DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
Environment=XDG_RUNTIME_DIR=/run/user/%U
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus
# set your display backend; if the check above prints "wayland", use wayland here or drop the line
Environment=GDK_BACKEND=x11
Environment=XAUTHORITY=%h/.Xauthority

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now althea.service
```

If the service crashes your launcher/tray: ensure you are logged in (no lingering), re-run `dbus-update-activation-environment --systemd --all`, drop `Environment=GDK_BACKEND=x11` on Wayland, or prefer the desktop autostart method below.

Optional: keep running after logout (enable lingering):
```bash
loginctl enable-linger "$(whoami)"
```

Useful commands:
```bash
systemctl --user status althea.service
journalctl --user -u althea.service -f
systemctl --user stop althea.service
systemctl --user restart althea.service
```

### Run althea on desktop login (autostart)

Starts when you log into your desktop session.

One-time setup:
```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/althea.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=althea
Exec=/path/to/althea/.venv/bin/python /path/to/althea/main.py
X-GNOME-Autostart-enabled=true
EOF
```
Add OnlyShowIn or other keys if your DE requires them.

Test it quickly (Wayland/X11):
```bash
install -D ~/.config/autostart/althea.desktop ~/.local/share/applications/althea.desktop
gtk-launch althea
```

Or just run it directly:
```bash
/usr/bin/python /path/to/althea/main.py
```

Disable autostart:
```bash
rm -f ~/.config/autostart/althea.desktop
```

## FAQ

<b>Fedora 41 shows the following error:</b>

`ERROR: Device returned unhandled error code -5`

You can downgrade crypto policies to the previous Fedora version:

`sudo update-crypto-policies --set FEDORA40`

## Credits

althea made by [vyvir](https://github.com/vyvir)

AltServer-Linux made by [NyaMisty](https://github.com/NyaMisty)

Provision by [Dadoum](https://github.com/Dadoum)

Artwork by [Nebula](https://github.com/itsnebulalol)

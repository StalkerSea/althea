# Run althea on desktop login (autostart)

This uses a .desktop file for your GUI session. It only starts when you log into a desktop environment.

## One-time setup
1) Ensure the path to main.py is correct for your system (currently /home/luazu/iOS/althea/main.py).
2) Create the autostart entry:

```bash
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/althea.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=althea
Exec=/usr/bin/python /home/luazu/iOS/althea/main.py
X-GNOME-Autostart-enabled=true
EOF
```

If your distro/DE needs OnlyShowIn or similar keys, add them as needed.

## Test it now
Run once in the foreground to confirm it launches correctly:
```bash
/usr/bin/python /home/luazu/iOS/althea/main.py
```

## Disable autostart
Remove the desktop entry:
```bash
rm -f ~/.config/autostart/althea.desktop
```

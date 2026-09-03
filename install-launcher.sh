#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APPLICATIONS_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APPLICATIONS_DIR/zxplayer.desktop"

mkdir -p "$APPLICATIONS_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=ZXPlayer
Comment=ZX Spectrum cassette player
Exec=/usr/bin/python3 "$APP_DIR/player.py"
Path=$APP_DIR
Icon=$APP_DIR/zxplayer-icon.png
Terminal=false
StartupNotify=false
Categories=Game;AudioVideo;Player;
Keywords=ZX Spectrum;Cassette;Tape;
EOF
chmod +x "$DESKTOP_FILE"

if [ -d "$HOME/Desktop" ]; then
    cp "$DESKTOP_FILE" "$HOME/Desktop/ZXPlayer.desktop"
    chmod +x "$HOME/Desktop/ZXPlayer.desktop"
    if command -v gio >/dev/null 2>&1; then
        gio set "$HOME/Desktop/ZXPlayer.desktop" metadata::trusted true >/dev/null 2>&1 || true
    fi
fi

if [ "${1:-}" = "--autostart" ]; then
    mkdir -p "$HOME/.config/autostart"
    cp "$DESKTOP_FILE" "$HOME/.config/autostart/zxplayer.desktop"
    echo "ZXPlayer will also start automatically after desktop login."
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

echo "ZXPlayer is now available from the application menu and desktop."

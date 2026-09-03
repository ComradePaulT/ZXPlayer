#!/bin/sh
set -eu

rm -f "$HOME/.local/share/applications/zxplayer.desktop"
rm -f "$HOME/Desktop/ZXPlayer.desktop"
rm -f "$HOME/.config/autostart/zxplayer.desktop"
echo "ZXPlayer launchers removed. The application and collection were not deleted."

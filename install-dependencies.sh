#!/bin/sh
set -eu

sudo apt update
sudo apt install -y python3-pygame python3-numpy fuse-emulator-utils

echo "ZXPlayer dependencies are installed."

#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

sudo apt update
sudo apt install -y python3-pygame python3-numpy fuse-emulator-utils

echo "ZXPlayer dependencies are installed."

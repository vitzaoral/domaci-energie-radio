#!/bin/bash
# Sestaví a nainstaluje rtl_433 25.12 (dekodér 277 Apator Metra E-RM 30 je od verze 25.12).
# Builds and installs rtl_433 25.12 (decoder 277 was added in 25.12).
# Raspberry Pi OS / Debian: sudo bash scripts/build-rtl433.sh   (Pi Zero 2 W asi 20–30 min)
set -euo pipefail
VERSION="${RTL433_VERSION:-25.12}"
apt-get update
apt-get install -y build-essential cmake pkg-config libusb-1.0-0-dev librtlsdr-dev rtl-sdr curl
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
curl -fsSL "https://github.com/merbanan/rtl_433/archive/refs/tags/${VERSION}.tar.gz" | tar xz
cd "rtl_433-${VERSION}" && mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DENABLE_OPENSSL=OFF -DENABLE_SOAPYSDR=OFF
nice -n 10 make -j2
make install && ldconfig
rtl_433 -V 2>&1 | head -1
rtl_433 -R help 2>&1 | grep -i "apator metra" || { echo "dekodér 277 chybí" >&2; exit 1; }

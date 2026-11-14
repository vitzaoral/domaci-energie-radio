#!/bin/bash
# Sestaví zigbee2mqtt i s node_modules pro Raspberry Pi (linux/arm64, Node 22) v Dockeru
# na jiném počítači. Na Pi Zero 2 W kompilace trvá dlouho a plná zátěž s USB hubem
# napájeným z Pi shodila napájení.
#   bash scripts/build-z2m-arm64.sh 2.14.3 ./out
# Na Pi pak: sudo tar xzf z2m-2.14.3-arm64.tgz -C /opt && sudo chown -R zigbee2mqtt: /opt/zigbee2mqtt
set -euo pipefail
VERSION=${1:?verze zigbee2mqtt, např. 2.14.3}
OUT=$(mkdir -p "${2:-.}" && cd "${2:-.}" && pwd)
docker run --rm --platform linux/arm64 -v "$OUT:/out" -e VERSION="$VERSION" node:22-bookworm bash -c '
set -euo pipefail
apt-get update -q >/dev/null && apt-get install -y -q libsystemd-dev >/dev/null
git clone -q --depth 1 --branch "$VERSION" https://github.com/Koenkk/zigbee2mqtt.git /opt/zigbee2mqtt
cd /opt/zigbee2mqtt
corepack enable
COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm install --frozen-lockfile > /tmp/pnpm.log 2>&1 || { tail -30 /tmp/pnpm.log; exit 1; }
pnpm run build > /tmp/build.log 2>&1 || { tail -20 /tmp/build.log; exit 1; }
test -f dist/controller.js
tar czf "/out/z2m-$VERSION-arm64.tgz" -C /opt zigbee2mqtt
'
ls -lh "$OUT/z2m-$VERSION-arm64.tgz"

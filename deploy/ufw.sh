#!/usr/bin/env bash
# Opens port 8004 to the home network only. Never the internet.
# Usage: deploy/ufw.sh [subnet]   (defaults to coatl's: 192.168.0.0/24)
set -euo pipefail

SUBNET="${1:-192.168.0.0/24}"
sudo ufw allow from "$SUBNET" to any port 8004 proto tcp comment 'karaoke (LAN only)'
sudo ufw status | grep 8004

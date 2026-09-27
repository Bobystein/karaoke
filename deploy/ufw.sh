#!/usr/bin/env bash
# Abre el puerto 8004 solo a la red de la casa. Nada de internet.
# Uso: deploy/ufw.sh [subred]   (por omision la de coatl: 192.168.0.0/24)
set -euo pipefail

SUBRED="${1:-192.168.0.0/24}"
sudo ufw allow from "$SUBRED" to any port 8004 proto tcp comment 'karaoke (solo LAN)'
sudo ufw status | grep 8004

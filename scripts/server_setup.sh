#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu server (EC2 t3.small). Safe to run twice.
# Usage: bash scripts/server_setup.sh      (run as your normal user, not root)
set -euo pipefail

SWAP_FILE=/swapfile
SWAP_SIZE=2G

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (e.g. ubuntu), not root. It uses sudo itself." >&2
  exit 1
fi

echo "==> Swap"
if swapon --show=NAME --noheadings | grep -q .; then
  echo "Swap already active, skipping."
else
  if [ ! -f "$SWAP_FILE" ]; then
    sudo fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
    sudo chmod 600 "$SWAP_FILE"
    sudo mkswap "$SWAP_FILE"
  fi
  sudo swapon "$SWAP_FILE"
fi
if ! grep -qs "^$SWAP_FILE " /etc/fstab; then
  echo "$SWAP_FILE none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi
# Prefer RAM; use swap only under pressure.
echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/99-swappiness.conf

echo "==> Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker and Compose already installed, skipping."
else
  # Official method: https://docs.docker.com/engine/install/ubuntu/
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl git
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker

if id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  echo "$USER is already in the docker group."
else
  sudo usermod -aG docker "$USER"
  echo "Added $USER to the docker group."
fi

echo
echo "==> Done. Next steps"
echo "1. Log out and back in (so the docker group applies), then check: docker ps"
echo "2. Clone the repo:   git clone <your-repo-url> ~/kodera && cd ~/kodera"
echo "3. Create .env:      cp .env.example .env && chmod 600 .env && nano .env"
echo "4. First start:      bash scripts/deploy.sh"
echo "5. Check:            curl -i https://YOUR_DOMAIN/health"
free -h

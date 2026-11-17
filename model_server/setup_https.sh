#!/usr/bin/env bash
# Direct HTTPS for the GPU gateway, instead of the ngrok tunnel (fewer hops = faster answers).
#
#   bash setup_https.sh                # on the GPU server, as root, after bootstrap_e2e.sh
#
# Installs Caddy (single binary from its GitHub release, no apt), which gets a free Let's Encrypt
# certificate for <ip-with-dashes>.sslip.io and forwards HTTPS to the gateway on 127.0.0.1:PORT.
# The gateway still checks the API key on every model request. Then set, in the app's .env:
#   LLM_BASE_URL / STT_BASE_URL / TTS_BASE_URL = https://<ip-with-dashes>.sslip.io/v1
set -euo pipefail
PORT=${PORT:-$(grep -E '^GATEWAY_PORT=' "$HOME/kodera-gpu/Kodera/model_server/.env" 2>/dev/null | cut -d= -f2)}
PORT=${PORT:-8000}
IP=${IP:-$(curl -fsS4 https://api.ipify.org)}
HOST=${HOST:-${IP//./-}.sslip.io}

if ! command -v caddy >/dev/null; then
  tmp=$(mktemp -d)
  # Read the release info from a file: piping curl into "grep -m1" makes curl fail (exit 23).
  curl -fsSL -o "$tmp/release.json" https://api.github.com/repos/caddyserver/caddy/releases/latest
  V=$(grep '"tag_name"' "$tmp/release.json" | head -1 | cut -d'"' -f4)
  [ -n "$V" ] || { echo "Could not read the latest Caddy version" >&2; exit 1; }
  echo "Installing Caddy $V"
  curl -fsSL -o "$tmp/caddy.tgz" "https://github.com/caddyserver/caddy/releases/download/$V/caddy_${V#v}_linux_amd64.tar.gz"
  tar -xzf "$tmp/caddy.tgz" -C "$tmp" caddy
  install -m 755 "$tmp/caddy" /usr/local/bin/caddy
  rm -rf "$tmp"
fi

mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
# Kodera GPU gateway over HTTPS. flush_interval -1 streams LLM tokens without buffering.
$HOST {
	reverse_proxy 127.0.0.1:$PORT {
		flush_interval -1
	}
}
EOF

cat > /etc/systemd/system/caddy.service <<EOF
[Unit]
Description=Caddy HTTPS for the Kodera GPU gateway
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile
Restart=always
Environment=HOME=/root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now caddy
systemctl restart caddy
for _ in $(seq 1 30); do
  if curl -fsS -m 5 "https://$HOST/health" >/dev/null 2>&1; then
    echo "HTTPS is up: https://$HOST  (health: $(curl -fsS -m 5 "https://$HOST/health"))"
    echo "In the app's .env set LLM_BASE_URL, STT_BASE_URL and TTS_BASE_URL to https://$HOST/v1"
    exit 0
  fi
  sleep 2
done
echo "Caddy started but https://$HOST/health is not answering yet: journalctl -u caddy -n 30" >&2
exit 1

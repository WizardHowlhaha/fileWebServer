#!/usr/bin/env bash
set -euo pipefail

REMOTE="${1:?usage: ./deploy.sh <host>}"
FIREWALL_CIDR="${FILEWEB_FIREWALL_CIDR:-}"
APP_DIR="/srv/fileweb/app"
RELEASE_DIR="/tmp/fileweb-release"

echo "Uploading FileWeb to ${REMOTE}..."
tar -czf - --exclude='__pycache__' server.py static requirements.txt README.md fileweb.service deploy.sh \
  | ssh "${REMOTE}" "rm -rf '${RELEASE_DIR}' && mkdir -p '${RELEASE_DIR}' && tar -xzf - -C '${RELEASE_DIR}'"

ssh "${REMOTE}" "FILEWEB_FIREWALL_CIDR='${FIREWALL_CIDR}' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail

dnf install -y nodejs >/dev/null
id fileweb >/dev/null 2>&1 || useradd --system --home-dir /var/lib/fileweb --shell /sbin/nologin fileweb
install -d -o root -g root -m 755 /srv/fileweb /srv/fileweb/app /opt/fileweb
install -d -o fileweb -g fileweb -m 750 /srv/fileweb/data
install -d -o fileweb -g fileweb -m 700 /var/lib/fileweb /var/lib/fileweb/agent-home
install -d -o fileweb -g fileweb -m 700 /var/lib/fileweb/profile
install -d -o root -g root -m 755 /etc/fileweb
install -d -o root -g root -m 755 /opt/fileweb/runtime

data_fingerprint() {
  (
    cd /srv/fileweb/data
    find . -type d -printf 'D\0%P\0' | sort -z
    find . -type l -printf 'L\0%P\0%l\0' | sort -z
    find . -type f -print0 | sort -z | while IFS= read -r -d '' item; do
      printf 'F\0%s\0' "$item"
      sha256sum -- "$item"
    done
  ) | sha256sum | cut -d' ' -f1
}

if [ "/srv/fileweb/data" = "/srv/fileweb/app" ]; then
  echo "Refusing deployment: application directory must not be the data directory" >&2
  exit 1
fi

rm -rf /srv/fileweb/app.new
install -d -o root -g root -m 755 /srv/fileweb/app.new
cp -a /tmp/fileweb-release/server.py /tmp/fileweb-release/static /tmp/fileweb-release/README.md /srv/fileweb/app.new/
chown -R root:root /srv/fileweb/app.new
chmod 755 /srv/fileweb/app.new/server.py

service_was_active=0
if systemctl is-active --quiet fileweb.service; then service_was_active=1; fi
restart_on_failure() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$service_was_active" -eq 1 ]; then
    systemctl start fileweb.service 2>/dev/null || true
  fi
  exit "$status"
}
trap restart_on_failure EXIT
systemctl stop fileweb.service 2>/dev/null || true
data_fingerprint_before=$(data_fingerprint)
rm -rf /srv/fileweb/app.old
if [ -e /srv/fileweb/app ]; then mv /srv/fileweb/app /srv/fileweb/app.old; fi
mv -T /srv/fileweb/app.new /srv/fileweb/app
rm -rf /srv/fileweb/app.old

if [ ! -s /etc/fileweb/admin-password ]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > /etc/fileweb/admin-password
fi
chmod 600 /etc/fileweb/admin-password
chown fileweb:fileweb /etc/fileweb/admin-password

if [ ! -x /opt/fileweb/runtime/node_modules/.bin/codex ]; then
  npm install --prefix /opt/fileweb/runtime --no-fund --no-audit --omit=dev @openai/codex@0.154.0 >/dev/null
fi
chown -R root:root /opt/fileweb/runtime
chmod -R a+rX /opt/fileweb/runtime

if [ -r /root/.codex/auth.json ]; then
  install -o fileweb -g fileweb -m 600 /root/.codex/auth.json /var/lib/fileweb/agent-home/auth.json
fi
if [ -r /root/.codex/config.toml ]; then
  install -o fileweb -g fileweb -m 600 /root/.codex/config.toml /var/lib/fileweb/agent-home/config.toml
fi

install -o root -g root -m 644 /tmp/fileweb-release/fileweb.service /etc/systemd/system/fileweb.service
restorecon -RF /srv/fileweb /opt/fileweb /var/lib/fileweb /etc/fileweb /etc/systemd/system/fileweb.service 2>/dev/null || true
systemctl daemon-reload
if [ -n "${FILEWEB_FIREWALL_CIDR}" ]; then
  firewall-cmd --permanent --add-rich-rule="rule family=\"ipv4\" source address=\"${FILEWEB_FIREWALL_CIDR}\" port port=\"8787\" protocol=\"tcp\" accept" >/dev/null || true
  firewall-cmd --reload >/dev/null
fi
systemctl enable --now fileweb.service
systemctl is-active --quiet fileweb.service
health_ready=0
for _ in {1..20}; do
  if curl --fail --silent --max-time 1 http://127.0.0.1:8787/healthz >/dev/null; then
    health_ready=1
    break
  fi
  sleep 0.5
done
if [ "$health_ready" -ne 1 ]; then
  echo "FileWeb did not pass its health check after restart" >&2
  exit 1
fi
data_fingerprint_after=$(data_fingerprint)
if [ "$data_fingerprint_before" != "$data_fingerprint_after" ]; then
  echo "Refusing to report success: data directory changed during deployment" >&2
  exit 1
fi
rm -rf /tmp/fileweb-release
trap - EXIT
echo "FileWeb is running on port 8787"
REMOTE_SCRIPT

echo "Deployment complete."
echo "Access the service on port 8787 through the target host address."
echo "Read the generated password with:"
echo "  ssh ${REMOTE} 'cat /etc/fileweb/admin-password'"

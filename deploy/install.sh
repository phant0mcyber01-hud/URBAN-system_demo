#!/usr/bin/env bash
# Installs the URBAN platform (and the Edge unit program) on Ubuntu 24.04 under /opt/urban as user `urban`.
# Run as root. Does not start anything: see deploy/README.md for the configuration steps.
set -euo pipefail

APP=/opt/urban
REPO=${URBAN_REPO:-https://github.com/phant0mcyber01-hud/URBAN-system_demo.git}

apt-get update
apt-get install -y python3 python3-venv python3-pip git nginx nodejs npm sqlite3

# the clone comes first: useradd must not create the home directory, git refuses a non-empty one
if [ ! -d "$APP/.git" ]; then
  git clone "$REPO" "$APP"
fi
id -u urban >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$APP" --shell /usr/sbin/nologin urban
chown -R urban:urban "$APP"
cd "$APP"

sudo -u urban python3 -m venv .venv
sudo -u urban .venv/bin/pip install --upgrade pip
sudo -u urban .venv/bin/pip install -e "backend[edge]"
(cd web && sudo -u urban npm ci && sudo -u urban npm run build)

sudo -u urban mkdir -p data
chmod 700 data
[ -f .env ] || sudo -u urban cp deploy/env.example .env
chmod 600 .env

install -m 644 deploy/urban.service /etc/systemd/system/urban.service
install -m 644 deploy/urban-edge@.service /etc/systemd/system/urban-edge@.service
mkdir -p /etc/urban/pki && chown -R urban:urban /etc/urban && chmod 700 /etc/urban/pki
install -m 644 deploy/nginx-urban.conf /etc/nginx/sites-available/urban
ln -sf ../sites-available/urban /etc/nginx/sites-enabled/urban
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/backups/urban && chown urban:urban /var/backups/urban
systemctl daemon-reload
systemctl enable urban

# firewall: only administrators on 22, operators on 443, devices on 8443
# ufw default deny incoming && ufw allow from <admin net> to any port 22 && ufw allow 443 && ufw allow 8443 && ufw enable

cat <<EOF

URBAN installed in $APP. Next:
  1. edit $APP/.env (DEVICE_NAME, URBAN_SOURCE=field)
  2. put the map data in $APP/data/out (python -m urban.pipeline here or on a workstation)
  3. sudo -u urban $APP/.venv/bin/python -m urban.provision --host <DEVICE_NAME>; move data/pki/ca.key off the server
  4. TLS certificate for nginx (certbot --nginx or your CA), then: nginx -t && systemctl reload nginx
  5. systemctl start urban && journalctl -u urban -f   (the first start prints the admin password)
EOF

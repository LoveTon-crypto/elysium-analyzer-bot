#!/bin/bash
# Installs/updates the bot on the server. Run from /root/elysium-analyzer-bot.
set -e
cd /root/elysium-analyzer-bot
if [ ! -x venv/bin/python ]; then
  echo ">>> Creating Python environment..."
  apt-get install -y -qq python3-venv >/dev/null 2>&1 || true
  python3 -m venv venv
fi
[ -f .env ] || { cp .env.example .env; echo "⚠️  Created .env — put BOT_TOKEN in it: nano /root/elysium-analyzer-bot/.env"; }
echo ">>> Installing libraries..."
apt-get install -y -qq libcairo2 >/dev/null 2>&1 || true   # SVG token pictures
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
cp elys-analyzer.service /etc/systemd/system/elys-analyzer.service
systemctl daemon-reload
systemctl enable -q elys-analyzer
cat > /usr/local/bin/elys-analyzer-update <<'UPD'
#!/bin/bash
# Download the latest version from GitHub and restart the bot
set -e
cd /root/elysium-analyzer-bot
git fetch -q origin main
git reset -q --hard origin/main
exec bash deploy.sh
UPD
chmod +x /usr/local/bin/elys-analyzer-update
echo ">>> Restarting the bot..."
systemctl restart elys-analyzer
sleep 8
if systemctl is-active --quiet elys-analyzer; then
  echo "✅ Bot is running. Version: $(git log -1 --format='%h — %s' 2>/dev/null)"
else
  echo "❌ Bot didn't start, last log lines:"
  journalctl -u elys-analyzer -n 25 --no-pager -o cat
fi

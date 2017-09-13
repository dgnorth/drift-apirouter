#!/bin/bash
echo "Installing crontab job for apirouter-conf:"
if crontab -l -u ubuntu | grep -q 'apirouter-conf'; then
  echo "  crontab already configured"
else
  echo "  updating crontab"
  sudo crontab -l -u ubuntu | { cat; echo "* * * * * /usr/local/bin/apirouter-conf 2>&1 | logger -t drift-apirouter"; } | crontab - -u ubuntu
fi

echo "Preparing nginx.conf"
sudo chown ubuntu /etc/nginx/nginx.conf
echo "# Truncated. apirouter-conf will generate this file in a moment." > /etc/nginx/nginx.conf
/usr/local/bin/apirouter-conf
sudo nginx -s reload

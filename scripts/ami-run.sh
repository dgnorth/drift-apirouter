#!/bin/bash
apt-get install nginx -y -q
echo "Create and change ownership of static file folder."
mkdir /usr/share/nginx/api-router
chown ubuntu /usr/share/nginx/api-router

echo "Installing crontab job for apirouter-conf:"
if crontab -l -u ubuntu | grep -q 'apirouter-conf'; then
  echo "  crontab already configured"
else
  venv=`cat /etc/opt/drift-apirouter/venv`
  echo "  updating crontab, using apirouter-conf from virtualenv ${venv}."
  crontab -l -u ubuntu | { cat; echo "* * * * * ${venv}/bin/apirouter-conf 2>&1 | logger -t drift-apirouter"; } | crontab - -u ubuntu
fi

echo "Preparing nginx.conf"
chown ubuntu /etc/nginx/nginx.conf
echo "# Truncated. apirouter-conf will generate this file in a moment." > /etc/nginx/nginx.conf
sudo -u ubuntu ${venv}/bin/apirouter-conf
nginx -s reload

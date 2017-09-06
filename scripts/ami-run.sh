#!/bin/bash
if crontab -l -u ubuntu | grep -q 'apirouter-conf'; then
  echo "crontab already configured"
else
  echo "updating crontab"
  sudo crontab -l -u ubuntu | { cat; echo "* * * * * apirouter-conf 2>&1 | logger -t drift-apirouter"; } | crontab - -u ubuntu
fi

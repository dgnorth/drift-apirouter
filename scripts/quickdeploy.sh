#!/bin/bash

# The following section is identical in driftapp-packer.sh and quickdeploy.sh
export servicefullname=${service}-${version}
echo "----------------- Extracting ${servicefullname}.tar -----------------"
export approot=/etc/opt/${service}
echo "--> Untar into ${approot} and change owner to ubuntu and fix up permissions"
tar -C /etc/opt -xvf ~/${servicefullname}.tar
rm -rf ${approot}
mv /etc/opt/${servicefullname} ${approot}
chown -R ubuntu:root ${approot}

echo "----------------- Create virtualenv and install dependencies -----------------"
cd ${approot}
if [ -z "${SKIP_PIP}" ]; then
    echo "Running pipenv install"
    pipenv install --deploy --verbose
fi

export VIRTUALENV=`pipenv --venv`
echo ${VIRTUALENV} >> ${approot}/venv

echo "---------------- Run apirouter-conf to generate a new nginx config -------------"
pipenv run apirouter-conf
echo "---------------- Reload nginx -------------"
sudo nginx -s reload

# -*- coding: utf-8 -*-
"""
Nginx Configuration Generator

OSX note: The Nginx config requires the 'realip' module.
Use 'brew install nginx-full --with-realip' to install it.
See https://github.com/Homebrew/homebrew-nginx and https://homebrew-nginx.marcqualie.com/
"""
import os
import os.path
import sys
import logging
import subprocess
import json
import time

import click
from jinja2 import Environment, PackageLoader
from driftconfig.util import get_drift_config
from apirouter.awstargets import get_ec2_targets_for_tier, get_api_endpoints_for_tier, get_name_server


log = logging.getLogger(__name__)


# Platform specifics
if sys.platform.startswith("linux"):
    platform = {
        'etc': '/etc',
        'pid': '/run/nginx.pid',
        'log': '/var/log',
        'root': '/usr/share/nginx',
        'nginx_config': '/etc/nginx/nginx.conf',
        'nameserver': get_name_server(),
    }
elif sys.platform == 'darwin':
    platform = {
        'etc': '/usr/local/etc',
        'pid': '/usr/local/var/run/nginx.pid',
        'log': '/usr/local/var/log',
        'root': '/usr/local/share/nginx',
        'nginx_config': '/usr/local/etc/nginx/nginx.conf',
        'nameserver': get_name_server(),
    }
else:
    platform = {}

platform['os'] = sys.platform

HEALTHCHECK_TIMEOUT = 1.0  # Timeout for target health check ping.


def _prepare_info(tier_name, check_health=True):
    conf = get_drift_config(tier_name=tier_name)
    ts = conf.table_store

    # Map tenant to product, and include only active tenants on active products
    # for this tier.
    tenant_map = {}
    tenant_names = ts.get_table('tenant-names')
    tenants = ts.get_table('tenants').find({'tier_name': tier_name, 'state': 'active'})
    for tenant in tenants:
        tenant_master = tenant_names.get({'tenant_name': tenant['tenant_name']})
        product = tenant_names.get_foreign_row(tenant_master, 'products')
        if product['state'] == 'active':
            tenant_map[tenant['tenant_name']] = product

    # Make a product map that includes all its tenants, for convenience
    product_map = {}
    for product in ts.get_table('products').find():
        product_name = product['product_name']
        tenants = ts.get_table('tenant-names').find({'product_name': product_name})
        if tenants:
            product_map[product_name] = product
            product['tenants'] = [tenant['tenant_name'] for tenant in tenants]

    deployables = ts.get_table('deployables').find({'tier_name': tier_name})
    deployables = {d['deployable_name']: d for d in deployables}  # Turn into a dict

    # Prepare routes for EC2 targets and API gateway endpoints.
    ec2_targets = get_ec2_targets_for_tier(tier_name=tier_name, check_health=check_health)
    api_endpoints = get_api_endpoints_for_tier(tier_name=tier_name, check_health=check_health)

    # Make sure the same deployable is not deployed both as an EC2 and an api gateway.
    common = set(ec2_targets) & set(api_endpoints)
    if common:
        log.warning(
            "Deployable(s) found both as EC2 target and API Gateway: %s\n"
            "The EC2 target will be selected!",
            ' '.join(common)
        )

    routes = {}
    for route in ts.get_table('routing').find():
        deployable_name = route['deployable_name']
        deployable = ts.get_table('deployables').get(
            {'tier_name': tier_name, 'deployable_name': deployable_name})
        if deployable is not None:
            routes[deployable_name] = route.copy()
            routes[deployable_name]['api'] = route.get('api', deployable_name)
            routes[deployable_name]['ec2_targets'] = ec2_targets.get(deployable_name, [])
            routes[deployable_name]['api_endpoint'] = api_endpoints.get(deployable_name)
            routes[deployable_name]['deployable'] = deployable

    # Example of product and custom key:
    '''
    {
        "api_key_name": "dg-superkaiju-1210a98c",
        "create_date": "2017-01-17T11:11:55.099094Z",
        "custom_data": "nonnib@directivegames.com",
        "in_use": true,
        "key_type": "product",
        "product_name": "dg-superkaiju"
    },

    {
        "api_key_name": "dg-nonnib-04ceccfb",
        "create_date": "2017-01-18T14:52:41.331130Z",
        "custom_data": "nonnib@directivegames.com",
        "in_use": true,
        "key_type": "custom",
        "product_name": "dg-superkaiju"
    }
    '''

    # Example of product key rules:
    '''
    product_name: dg-superkaiju
    assignment_order: 0-6
    is_active: true

    rule_name          version_patterns  rule_type  status_code  ...
    ------------------------------------------------------------------------------------------------
    upgrade-client-1.6 ["1.6.0","1.6.2"] reject     404          response_body: {"action": "upgrade_client"}
    downtime-message   []                reject     403          response_body: {"message": "The server is down"}
**  redirect_to_test   ["0.0.1"]         redirect   (302)        tenant_name: dg-themachines-test
    upgrade-client-old ["1.3.*","1.4."]   reject     404          response_body: {"action": "upgrade_client"}
    pass               ["1.6.3","1.6.4"] pass
    redirect-to-nonni  ["1.6.5"]         redirect                tenant_name: dg-nonnib-devnorth
    reject             []                reject     403          response_body: {"message": "Bugger off!"},


    inactive:
    {
        "assignment_order": 0,
        "is_active": false,
        "match_type": "partial",
        "product_name": "dg-superkaiju",
        "response_body": {
            "action": "upgrade_client"
        },
        "rule_name": "upgrade-client-1.6",
        "rule_type": "reject",
        "status_code": 404,
        "version_patterns": [
            "1.6.0",
            "1.6.1",
            "1.6.2"
        ]
    },
    '''
    api_keys = {}
    for api_key in ts.get_table('api-keys').find({'tier_name': tier_name}):
        if api_key['in_use'] and api_key['key_type'] == 'custom':
            api_keys[api_key['api_key_name']] = ''

    # This should come from the "new" nginx config table:
    nginx = ts.get_table('nginx').get({'tier_name': tier_name})

    ret = {
        'conf': conf,
        'tenants': tenant_map,
        'products': product_map,
        'routes': routes,
        'nginx': nginx,
        'plat': platform,
    }

    return ret


def _generate_status(data):
    # Make a pretty summary of services, routes, upstream servers and products.
    deployables = []

    for name, route in data['routes'].items():
        service = {
            'name': name,
            'api': route['api'],
            'requires_api_key': route['requires_api_key'],
            'is_active': route['deployable']['is_active'],
            'upstream_servers': [
                [
                    {
                        'address': "{}:{}".format(target['private_ip_address'], target['tags']['api-port']),
                        'status': target['tags']['api-status'],
                        'health': target.get('health_status'),
                        'version': target['tags'].get('drift:manifest:version'),
                        ##'tags': target['tags'],
                    }
                ]
                for target in route['ec2_targets']
            ],
        }
        if not service['is_active'] and 'reason_inactive' in route['deployable']:
            service['reason_inactive'] = route['deployable']['reason_inactive']

        ep = route['api_endpoint']
        if ep:
            service['api_gateway'] = {
                'url': ep['url'],
                'health': ep['message'] if ep['health_status'] == 'error' else 'ok',
            }
        else:
            service['api_gateway'] = None

        deployables.append(service)

    status = {
        'deployables': deployables,
        'products': [
            {
                'product_name': product['product_name'],
                'organization_name': product['organization_name'],
                'state': product['state'],
                'deployables': product['deployables'],
                'tenants': product['tenants'],
            }
            for product in data['products'].values()
        ],
    }

    return json.dumps(status, indent=4, default=str)


def generate_nginx_config(tier_name, check_health=True):
    data = _prepare_info(tier_name=tier_name, check_health=check_health)
    env = Environment(loader=PackageLoader('apirouter', ''))
    env.filters['jsonify'] = lambda ob: json.dumps(ob, indent=4)
    ret = {
        'config': env.get_template('nginx.conf.jinja').render(**data),
        'data': data,
        'status': _generate_status(data),
    }

    return ret


def write_status_doc(status):
    """Write 'status' to a json file which gets served at /api-router."""
    status_folder = os.path.join(platform['root'], 'api-router')
    if not os.path.exists(status_folder):
        os.makedirs(status_folder)
    with open(os.path.join(platform['root'], 'api-router', 'status.json'), 'w') as f:
        f.write(status)


def apply_nginx_config(nginx_config, skip_if_same=True):
    """Apply the Nginx config on the local machine and trigger a reload."""
    if os.path.exists(platform['nginx_config']):
        with open(platform['nginx_config'], 'r') as f:
            if nginx_config['config'] == f.read() and skip_if_same:
                return "skipped"

    with open(platform['nginx_config'], 'w') as f:
        f.write(nginx_config['config'])
    ret = subprocess.call(['sudo', 'nginx', '-t'])
    if ret != 0:
        return ret
    ret = subprocess.call(['sudo', 'nginx', '-s', 'reload'])
    time.sleep(1)
    return ret


@click.command()
@click.option('--preview', '-p', is_flag=True, help='Preview only.')
@click.option('--log-level', '-l', default='WARNING', help='Logging level.')
@click.option('--skip-healthcheck', '-s', is_flag=True, help='Skip health check.')
def cli(preview, log_level, skip_healthcheck):
    logging.basicConfig(level=log_level)
    print("Configure Drift API Router.")
    nginx_config = generate_nginx_config(
        tier_name=os.environ['DRIFT_TIER'],
        check_health=not skip_healthcheck,
    )

    if preview:
        print("Nginx configuration file:")
        print(nginx_config['config'])
        print("Status file:")
        print(nginx_config['status'])
        return

    if 'status' in nginx_config:
        write_status_doc(nginx_config['status'])

    ret = apply_nginx_config(nginx_config)
    if ret == "skipped":
        print("No change detected.")
    else:
        print("New config applied.")


if __name__ == '__main__':
    logging.basicConfig(level='WARNING')
    nginx_config = generate_nginx_config(tier_name=os.environ['DRIFT_TIER'])
    print(nginx_config['config'])
    subset = nginx_config['data'].copy()
    del subset['conf']
    print(subset)

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

import boto3
import requests
import requests.exceptions

from jinja2 import Environment, PackageLoader
from driftconfig.util import get_drift_config

log = logging.getLogger(__name__)

# Platform specifics
if sys.platform.startswith("linux"):
    platform = {
        'etc': '/etc',
        'pid': '/run/nginx.pid',
        'log': '/var/log',
        'root': '/usr/share/nginx',
        'nginx_config': '/etc/nginx/nginx.conf',
    }
elif sys.platform == 'darwin':
    platform = {
        'etc': '/usr/local/etc',
        'pid': '/usr/local/var/run/nginx.pid',
        'log': '/usr/local/var/log',
        'root': '/usr/local/share/nginx',
        'nginx_config': '/usr/local/etc/nginx/nginx.conf',
    }
else:
    platform = {}

platform['os'] = sys.platform

HEALTHCHECK_TIMEOUT = 1.0  # Timeout for target health check ping.


def _prepare_info(tier_name):
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

    # Prepare routes (or api forwarding)
    api_targets = get_api_targets(conf, deployables)

    # Run health check on targets
    nginx = ts.get_table('nginx').get({'tier_name': tier_name})
    healthy_targets = _healthcheck_targets(api_targets, nginx)

    routes = {}

    for route in ts.get_table('routing').find():
        deployable_name = route['deployable_name']
        routes[deployable_name] = route.copy()
        routes[deployable_name].setdefault('api', deployable_name)  # Makes it easier for the template code.
        routes[deployable_name]['targets'] = api_targets.get(deployable_name, [])
        routes[deployable_name]['healthy_targets'] = healthy_targets.get(deployable_name, [])
        routes[deployable_name]['deployable'] = ts.get_table('deployables').get(
            {'tier_name': tier_name, 'deployable_name': deployable_name})


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


def _healthcheck_targets(api_targets, nginx=None):
    """Pings targets in 'api_targets' for health and removes a new map of 'healhty_targets' as a result."""
    nginx = nginx or {}
    if not nginx.get('healthcheck_targets', True):
        log.info("Not running health checks on targets, as configured.")
        return

    healthcheck_timeout = nginx.get('healthcheck_timeout', HEALTHCHECK_TIMEOUT)
    healthcheck_port = nginx.get('healthcheck_port', 8080)
    healthy_targets = {}

    for api_target_name, targets in api_targets.items():
        healthy_targets[api_target_name] = []
        for target in targets[:]:
            # Ping the target for health. The assumption is that the target runs a plain
            # http server on port 8080 and responds to / with a 200.
            healthy = False
            try:
                ret = requests.get(
                    "http://{}:{}/".format(target['private_ip_address'], healthcheck_port),
                    timeout=healthcheck_timeout
                )
            except Exception as e:
                log.warning("Target %s[%s]: Healthcheck failed: %s.", api_target_name, target['private_ip_address'], e)
                target['health_status'] = str(e)
            else:
                if ret.status_code != 200:
                    log.warning(
                        "EC2 instance %s[%s]: Healthcheck response failure: %s: %s.",
                        api_target_name, target['private_ip_address'], ret.status_code, ret.text
                    )
                    target['health_status'] = u"{}: {}".format(ret.status_code, ret.text)
                else:
                    healthy = True

            if healthy:
                target['health_status'] = 'ok'
                healthy_targets[api_target_name].append(target)

    return healthy_targets


def get_api_targets(conf, deployables):
    """Return a dict where key is deployable name and value is list of targets."""
    # Note, only AWS targets supported here.
    return get_api_targets_from_aws(conf, deployables)


def get_api_targets_from_aws(conf, deployables):

    # Enumerate EC2's that match the entries in 'deployables' and are tagged as 'api-target'.
    if 'aws' not in conf.tier:
        raise RuntimeError("'aws' section missing from tier configuration.")

    ec2 = boto3.resource('ec2', region_name=conf.tier['aws']['region'])
    filters = {
        'instance-state-name': 'running',
        'tag:tier': conf.tier['tier_name'],
    }
    ec2_instances = list(ec2.instances.filter(Filters=filterize(filters)))

    # If the instances are part of an autoscaling group, make sure they are healthy and in service.
    autoscaling = boto3.client('autoscaling', region_name=conf.tier['aws']['region'])
    auto_ec2s = autoscaling.describe_auto_scaling_instances(InstanceIds=[ec2.instance_id for ec2 in ec2_instances])
    auto_ec2s = {auto_ec2['InstanceId']: auto_ec2 for auto_ec2 in auto_ec2s['AutoScalingInstances']}
    # auto_ec2s is a dict with instance id as key, and value is a dict with LifecycleState and HealthStatus key.

    api_targets = {}
    for ec2 in ec2_instances:

        # Analyse the EC2 instances and see if they are supposed to be a target of the api router.
        tags = fold_tags(ec2.tags)
        api_status = tags.get('api-status')
        api_target = tags.get('api-target')
        api_port = tags.get('api-port')
        name = tags.get('Name')

        # Check if instance is being "scaled in" by autoscaling group.
        if ec2.instance_id in auto_ec2s:
            if auto_ec2s[ec2.instance_id]['LifecycleState'].startswith('Terminating'):
                log.info("EC2 instance %s[%s] terminating. Marking it as 'backup' to drain connections.", name, ec2.instance_id[:7])
                tags['api-param'] = 'backup'  # This will enable connection draining in Nginx.

        if not any([api_status, api_target, api_port]):
            log.info("EC2 instance %s[%s] not in rotation, as it's not configured as api-target.", name, ec2.instance_id[:7])
            continue

        if any([api_status, api_target, api_port]) and not all([api_status, api_target, api_port]):
            log.warning("EC2 instance %s[%s] must define all api tags, not just some: %s.", name, ec2.instance_id[:7], tags)
            continue

        if not unicode(api_port).isnumeric():
            log.warning("EC2 instance %s[%s] has bogus 'api-port' tag: %s.", name, ec2.instance_id[:7], api_port)
            continue

        deployable = deployables.get(api_target)
        if not deployable:
            log.warning("EC2 instance %s[%s]: No deployable defined for api-target '%s'.", name, ec2.instance_id[:7], api_target)
            continue

        if api_status not in ['online', 'online2']:
            log.info("EC2 instance %s[%s] not in rotation, api-status tag is '%s'.", name, ec2.instance_id[:7], api_status)
            continue

        target = {
            'name': name,
            'image_id': ec2.image_id,
            'instance_id': ec2.id,
            'instance_type': ec2.instance_type,
            'launch_time': ec2.launch_time.isoformat() + 'Z',
            'placement': ec2.placement,
            'private_ip_address': ec2.private_ip_address,
            'public_ip_address': ec2.public_ip_address,
            'state_name': ec2.state['Name'],
            'state_transition_reason': ec2.state_transition_reason,
            'subnet_id': ec2.subnet_id,
            'tags': tags,
            'vpc_id': ec2.vpc_id,
            'comment': "{} [{}] [{}]".format(name, ec2.instance_type, ec2.placement['AvailabilityZone']),
        }

        api_targets.setdefault(api_target, []).append(target)

    return api_targets


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
                        'health': target['health_status'],
                        'version': target['tags'].get('drift:manifest:version'),
                        ##'tags': target['tags'],
                    }
                ]
                for target in route['targets']
            ]
        }
        if not service['is_active'] and 'reason_inactive' in route['deployable']:
            service['reason_inactive'] = route['deployable']['reason_inactive']

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

    return json.dumps(status, indent=4)


def generate_nginx_config(tier_name):
    data = _prepare_info(tier_name=tier_name)
    env = Environment(loader=PackageLoader('apirouter', ''))
    env.filters['jsonify'] = lambda ob: json.dumps(ob, indent=4)
    ret = {
        'config': env.get_template('nginx.conf.jinja').render(**data),
        'data': data,
        'status': _generate_status(data),
    }

    return ret


def apply_nginx_config(nginx_config, skip_if_same=True):
    """Apply the Nginx config on the local machine and trigger a reload."""
    if 'status' in nginx_config:
        status_folder = os.path.join(platform['root'], 'api-router')
        if not os.path.exists(status_folder):
            os.makedirs(status_folder)
        with open(os.path.join(platform['root'], 'api-router', 'status.json'), 'w') as f:
            f.write(nginx_config['status'])

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


def filterize(d):
    """
    Return dictionary 'd' as a boto3 "filters" object by unfolding it to a list of
    dict with 'Name' and 'Values' entries.
    """
    return [{'Name': k, 'Values': [v]} for k, v in d.items()]


def fold_tags(tags, key_name=None, value_name=None):
    """Fold boto3 resource tags array into a dictionary."""
    return {tag['Key']: tag['Value'] for tag in tags}


def cli():
    logging.basicConfig(level='WARNING')
    nginx_config = generate_nginx_config(tier_name=os.environ['DRIFT_TIER'])
    ret = apply_nginx_config(nginx_config)
    if ret == "skipped":
        print "No change detected."
    else:
        print "New config applied."


if __name__ == '__main__':
    logging.basicConfig(level='WARNING')
    nginx_config = generate_nginx_config(tier_name=os.environ['DRIFT_TIER'])
    from drift.utils import pretty
    print pretty(nginx_config['config'], lexer='nginx')
    subset = nginx_config['data'].copy()
    del subset['conf']
    print pretty(subset)

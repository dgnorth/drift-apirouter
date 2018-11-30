"""
AWS Resource Enumerators

Utility to find Drift targets running in the AWS Cloud.
"""
import os
import socket
import logging
import re

import boto3
import requests
from requests.utils import urlparse

from driftconfig.util import get_drift_config


log = logging.getLogger(__name__)


HEALTHCHECK_TIMEOUT = 1.0  # Timeout for target health check ping.
HEALTHCHECK_PORT = 8080  # HTTP server port on targets.


def _get_ec2_targets_from_aws(tier_name):

    conf = get_drift_config(tier_name=tier_name)
    deployables = conf.table_store.get_table('deployables').find({'tier_name': tier_name})
    deployables = {d['deployable_name']: d for d in deployables}  # Turn into a dict

    # Enumerate EC2's that match the entries in 'deployables' and are tagged as 'api-target'.
    if 'aws' not in conf.tier:
        raise RuntimeError("'aws' section missing from tier configuration.")

    ec2_client = boto3.resource('ec2', region_name=conf.tier['aws']['region'])
    filters = {
        'instance-state-name': 'running',
        'tag:tier': conf.tier['tier_name'],
    }
    def filterize(d):
        """
        Return dictionary 'd' as a boto3 "filters" object by unfolding it to a list of
        dict with 'Name' and 'Values' entries.
        """
        return [{'Name': k, 'Values': [v]} for k, v in d.items()]

    log.info("Fetch EC2 instances that match %s", filterize(filters))
    ec2_instances = list(ec2_client.instances.filter(Filters=filterize(filters)))

    # If the instances are part of an autoscaling group, make sure they are healthy and in service.
    region_name = conf.tier['aws']['region']
    vpc = _get_vpc_for_tier(region_name=region_name, tier_name=tier_name)
    autoscaling = boto3.client('autoscaling', region_name=region_name)
    auto_ec2s = autoscaling.describe_auto_scaling_instances(
        InstanceIds=[ec2.instance_id for ec2 in ec2_instances]
    )
    auto_ec2s = {auto_ec2['InstanceId']: auto_ec2 for auto_ec2 in auto_ec2s['AutoScalingInstances']}
    # auto_ec2s is a dict with instance id as key, and value is a dict with LifecycleState and HealthStatus key.

    api_targets = {}
    for ec2 in ec2_instances:
        # Analyse the EC2 instances and see if they are supposed to be a target of the api router.

        def fold_tags(tags, key_name=None, value_name=None):
            """Fold boto3 resource tags array into a dictionary."""
            return {tag['Key']: tag['Value'] for tag in tags}

        tags = fold_tags(ec2.tags)
        api_status = tags.get('api-status')
        api_target = tags.get('api-target')
        api_port = tags.get('api-port')
        name = tags.get('Name')

        # Check if instance is being scaled in or out by autoscaling group.
        if ec2.instance_id in auto_ec2s:
            lifecycle_state = auto_ec2s[ec2.instance_id]['LifecycleState']

            if lifecycle_state.startswith('Terminating'):
                # The instances are normally equipped with a lifecycle hook that specifies
                # 2 minute timeout before they are terminated. The 'LivecycleState' value
                # is "Terminating:Waiting" during this transition.
                # The timeout can be extended by by calling record_lifecycle_action_heartbeat()
                # on the lifecycle hook. Example:
                # boto3.client('autoscaling).record_lifecycle_action_heartbeat(...)
                #
                # To gracefully drain the connections, the instance is marked as 'backup'. This
                # simply removes the instance from the round robin load balancing.
                log.info("EC2 instance %s[%s] terminating. Marking it as 'backup' to drain connections.", name, ec2.instance_id[:7])
                tags['api-param'] = 'backup'  # This will enable connection draining in Nginx.
            elif lifecycle_state != 'InService':
                log.warning("EC2 instance %s[%s] not in service yet: %s", name, ec2.instance_id[:7], lifecycle_state)
                continue

        if ec2.vpc_id != vpc['VpcId']:
            log.warning("EC2 instance %s[%s] not in correct VPC. Fix tier tag!", name, ec2.instance_id[:7])
            continue

        if not any([api_status, api_target, api_port]):
            log.info("EC2 instance %s[%s] not in rotation, as it's not configured as api-target.", name, ec2.instance_id[:7])
            continue

        if any([api_status, api_target, api_port]) and not all([api_status, api_target, api_port]):
            log.warning("EC2 instance %s[%s] must define all api tags, not just some: %s.", name, ec2.instance_id[:7], tags)
            continue

        if not api_port.isnumeric():
            log.warning("EC2 instance %s[%s] has bogus 'api-port' tag: %s.", name, ec2.instance_id[:7], api_port)
            continue

        deployable = deployables.get(api_target)
        if not deployable:
            log.warning("EC2 instance %s[%s]: No deployable defined for api-target '%s'.", name, ec2.instance_id[:7], api_target)
            continue

        if api_status not in ['online', 'online2']:
            log.info("EC2 instance %s[%s] not in rotation, api-status tag is '%s'.", name, ec2.instance_id[:7], api_status)
            continue

        log.info(
            "EC2 instance %s[%s] in rotation. [%s:%s:%s]",
            name, ec2.instance_id[:7], api_target, api_status, api_port
        )

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


def _healthcheck_targets(api_targets):
    """Pings targets in 'api_targets' for health and removes a new map of 'healhty_targets' as a result."""
    healthy_targets = {}

    for api_target_name, targets in api_targets.items():
        healthy_targets[api_target_name] = []
        for target in targets[:]:
            log.info("Checking health of %s", target['private_ip_address'])
            # Ping the target for health. The assumption is that the target runs a plain
            # http server on port 8080 and responds to / with a 200.

            # HACK: There is no uniform healtch check path on targets. Trying out a few until
            # this gets cleaned up:
            # Try /healthcheck
            url = 'http://{}:{}/healthcheck'.format(target['private_ip_address'], HEALTHCHECK_PORT)
            status, message = _check_url(url)
            if status != 200:
                # Try /
                url = 'http://{}:{}/'.format(target['private_ip_address'], HEALTHCHECK_PORT)
                status, message = _check_url(url)

            if status != 200:
                log.warning(
                    "Target %s[%s]: Healthcheck failed: %s.",
                    api_target_name,
                    url,
                    message,
                )
                target['health_status'] = message
            else:
                target['health_status'] = 'ok'
                healthy_targets[api_target_name].append(target)

    return healthy_targets


def _get_vpc_for_tier(region_name, tier_name):
    """
    Returns first VPC that matches tag:tier='tier_name' or None if none found.
    The response format can be seen here:
    https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ec2.html#EC2.Client.describe_vpcs
    """
    log.info("Getting VPC for tier %s", tier_name)
    client = boto3.client('ec2', region_name=region_name)
    vpcs = client.describe_vpcs(Filters=[{'Name': 'tag:tier', 'Values': [tier_name]}])
    if vpcs['Vpcs']:
        return vpcs['Vpcs'][0]


# See "Invoking Your Private API Using Endpoint-Specific Public DNS Hostnames" here:
# https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-apis.html#apigateway-private-api-test-invoke-url


def _get_public_api_gw_url(region_name, tier_name, stage_name=None):
    """
    Return a public URL for the API Gateway on the tier specified by 'tier_name'.
    The VPC must have an endpoint for 'com.amazonaws.eu-west-1.execute-api' service configured.
    """
    log.info("Get public API Gateway URl for tier %s", tier_name)
    stage_name = stage_name or 'main'
    vpc = _get_vpc_for_tier(region_name, tier_name)
    if not vpc:
        return

    client = boto3.client('ec2', region_name=region_name)
    query = [
        {'Name': 'service-name', 'Values': ['com.amazonaws.eu-west-1.execute-api']},
        {'Name': 'vpc-id', 'Values': [vpc['VpcId']]}
    ]
    endpoints = client.describe_vpc_endpoints(Filters=query)
    if endpoints['VpcEndpoints']:
        vpc_ep = endpoints['VpcEndpoints'][0]
        url = 'https://{vpce_id}.execute-api.{region}.vpce.amazonaws.com/{stage}'.format(
            vpce_id=vpc_ep['VpcEndpointId'],
            region=region_name,
            stage=stage_name,
        )
        return url


def _get_api_endpoints(region_name, tier_name, deployable_names, stage_name=None):
    # Returns info on AWS API Gateway endpoints that match 'deployable_names'.
    stage_name = stage_name or 'main'
    client = boto3.client('apigateway', region_name=region_name)
    api_names = {
        '{}-{}'.format(tier_name, deployable_name): deployable_name
        for deployable_name in deployable_names
    }
    endpoints = []

    log.info("Get API endpoints for tier %s", tier_name)
    for api in client.get_rest_apis(limit=500)['items']:
        if api['name'] in api_names:
            # Make sure this API got the expected stage name
            for stage in client.get_stages(restApiId=api['id'])['item']:
                if stage['stageName'] == stage_name:
                    break
            else:
                log.warning("Found API Gateway '%s' but no stage named '%s'.", api['name'], stage_name)
                continue

            url = 'https://{id}.execute-api.{region_name}.amazonaws.com/{stage_name}'.format(
                id=api['id'], region_name=region_name, stage_name=stage_name
                )

            ep = {
                'deployable_name': api_names[api['name']],
                'url': url,
                'api': api,
            }
            endpoints.append(ep)

    return endpoints


def _check_url(url, headers=None, timeout=None):
    headers = headers or {}
    timeout = timeout or HEALTHCHECK_TIMEOUT

    # Do a HEAD request on 'url' and catch DNS errors in particular.
    try:
        socket.gethostbyname(urlparse(url).hostname)
    except socket.gaierror:  # Get Address Info Error
        # Assume DNS problem
        return 'error', 'DNS lookup failed'

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        return resp.status_code, resp.text
    except Exception as e:
        if 'timed out' in str(e):
            return 'error', 'Timeout'
        else:
            return 'error', str(e)


def _do_api_gw_health_check(url, public_url=None):
    """
    Do health check on API Gateway endpoints.
    Does http request on 'url and returns a tuple of 'status' and 'message' as result.
    If 'status' is not 'error' then the url seems fine.
    """
    timeout = 7.0  # In case the lambda is sleeping we give it ample time.
    status, message = _check_url(url, {}, timeout=timeout)
    if message == 'DNSLookupError' and public_url:
        status, message = _check_url(
            public_url,
            headers={'Host': urlparse(url).hostname},
            timeout=timeout
        )
    return status, message


def get_name_server():
    """Return IP address of private name server."""
    with open('/etc/resolv.conf') as f:
        s = re.search(r"nameserver[\W](.*)\s", f.read())
        if s:
            return s.groups()[0]


def get_api_endpoints_for_tier(tier_name, check_health=False, public_url=None):
    """
    Returns a dict of API Gateway endpoints for all deployables in tier 'tier_name' and
    the health status if 'health_check' is set.
    If 'public_url' is set the health check will try to use the public api gw endpoint
    to ping the target.

    Abridged example of response:

    {
        "drift-base": {
            "deployable_name": "drift-base",
            "url": "https://spliffdnkgen.execute-api.eu-west-1.amazonaws.com/main",
            "api": {
                "id": "spliffdnkgen",
                "name": "DEVNORTH-drift-base",
                "createdDate": datetime.datetime(2018, 10, 24, 13, 21, 59, tzinfo=tzlocal()),
                "apiKeySource": "HEADER",
                "endpointConfiguration": {
                    "types": [
                        "PRIVATE"
                    ]
                },
                "policy": "..."
            },
            "health_status": "error",
            "message": "DNS lookup failed"
        }
    }

    Note, if endpoint health is good 'health_status' is the status code (usually 200).

    """
    conf = get_drift_config(tier_name=tier_name)
    deployables = conf.table_store.get_table('deployables').find({'tier_name': tier_name})
    deployable_names = [deployable['deployable_name'] for deployable in deployables]
    endpoints = _get_api_endpoints(
        region_name=conf.tier['aws']['region'],
        tier_name=conf.tier['tier_name'],
        deployable_names=deployable_names,
    )

    if check_health:
        for ep in endpoints:
            status, message = _do_api_gw_health_check(ep['url'], public_url)
            ep['health_status'] = status
            ep['message'] = message

    return {ep['deployable_name']: ep for ep in endpoints}


def get_ec2_targets_for_tier(tier_name, check_health=False):
    """
    Returns a dict of EC2 instances for all deployables in tier 'tier_name' that are tagged
    as targets.
    Health is checked if 'check_health' is set.


    Abridged example of response:

    {
        "drift-base":
        [
            {
                "name": "DEVNORTH-drift-base-auto",
                "image_id": "ami-09378736dddbd9368",
                "instance_id": "i-0a436fc2e66a39f35",
                "instance_type": "t2.small",
                "launch_time": "2018-10-16T13:05:14+00:00Z",
                "private_ip_address": "10.50.2.139",
                "state_name": "running",
                "subnet_id": "subnet-8544def2",
                "tags": {
                    "api-status": "online",
                    "api-target": "drift-base",
                    "service-name": "drift-base",
                    "Name": "DEVNORTH-drift-base-auto",
                    "api-port": "10080",
                    "tier": "DEVNORTH"
                },
                "vpc_id": "vpc-6458d901",
                "comment": "DEVNORTH-drift-base-auto [t2.small] [eu-west-1b]",
                "health_status": "ok"
            }
        ]
    }
    """
    ec2_targets = _get_ec2_targets_from_aws(tier_name=tier_name)
    if check_health:
        _healthcheck_targets(ec2_targets)

    return ec2_targets


def _dump():
    tier_name = os.environ['DRIFT_TIER']

    conf = get_drift_config(tier_name=tier_name)
    public_url = _get_public_api_gw_url(
        region_name=conf.tier['aws']['region'],
        tier_name=conf.tier['tier_name']
    )
    if public_url:
        print("VPC has public endpoint for execute-api service calls:")
        print(public_url)

    endpoints = get_api_endpoints_for_tier(tier_name, check_health=True, public_url=public_url)
    ec2_targets = get_ec2_targets_for_tier(tier_name, check_health=True)

    print("AWS API Gateways:")
    for ep in endpoints.values():
        print("\t{}:\t{}\t[{}] {}".format(ep['deployable_name'], ep['url'], ep['health_status'], ep['message']))

    print("AWS EC2 Targets:")
    for deployable_name, targets in ec2_targets.items():
        if not targets:
            print("\t{}: (no targets registered or running)".format(deployable_name))
        else:
            print("\t{}:".format(deployable_name))
            for target in targets:
                print("\t\t{}:{}\t[{}]".format(
                    target['private_ip_address'], target['tags']['api-port'], target['health_status']))


if __name__ == '__main__':
    logging.basicConfig(level='INFO')
    _dump()

# -*- coding: utf-8 -*-
import time
import unittest
import json
import requests
import logging
import httplib
import mock

from driftconfig.testhelpers import create_test_domain, TIER_NAME


from apirouter import nginxconf


HOST = 'localhost'
PORT = 8080         # todo: do not hardcode, get this from nginx config
REDIR_PORT = PORT + 1

REQUEST_HOST = 'just.a.test'
HOST_HEADER = {'Host': REQUEST_HOST}


# Some patching
def get_api_targets(tier_name, region_name):
    tags = {'api-status': 'online', 'api-target': 'nginxtest', 'api-port': 555}
    targets = [
        {'name': 'test instance', 'instance_id': 'test-1', 'private_ip_address': '10.0.0.1', 'instance_type': 't2.small', 'tags': tags, 'placement': {'AvailabilityZone': 'test-zone-1a'}, 'comment': "DEVNORTH-drift-base [t2.small] [eu-west-1b]"},
        {'name': 'test instance', 'instance_id': 'test-2', 'private_ip_address': '10.0.0.2', 'instance_type': 't2.small', 'tags': tags, 'placement': {'AvailabilityZone': 'test-zone-1a'}, 'comment': "DEVNORTH-drift-base [t2.small] [eu-west-1b]"},
    ]
    return {'nginxtest': targets}


class TestNginxConfig(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.patchers = [
            mock.patch('apirouter.nginxconf.get_api_targets', get_api_targets),
        ]

        for patcher in cls.patchers:
            patcher.start()

        # Create config with two deployables. The first one will have targets available (see
        # get_api_targets() above). The second one has not target but is used to test keyless
        # api access.
        config_size = {
            'num_org': 6152,
            'num_tiers': 2,
            'num_deployables': 4,
            'num_products': 2,
            'num_tenants': 2,
        }

        import driftconfig.relib
        driftconfig.relib.CHECK_INTEGRITY = []

        t = time.time()
        ts = create_test_domain(config_size)
        print "CREATING TEST CONFIG", time.time() - t

        if 1:
            from driftconfig.relib import create_backend

            #t = time.time()
            #create_backend('file://~/.drift/testts').save_table_store(ts, run_integrity_check=False, file_format='json')
            #print "WRITING json", time.time() - t

            t = time.time()
            create_backend('file://~/.drift/testts').save_table_store(ts, run_integrity_check=False, file_format='pickle')
            print "WRITING pickle", time.time() - t

        tier_name = ts.get_table('tiers').find()[0]['tier_name']
        api_1 = ts.get_table('deployables').find()[0]['deployable_name']
        api_2 = ts.get_table('deployables').find()[1]['deployable_name']

        routing = ts.get_table('routing')
        routing.add({
            'tier_name': tier_name,
            'deployable_name': api_1,
            'requires_api_key': True,
        })

        routing.add({
            'tier_name': tier_name,
            'deployable_name': api_2,
            'requires_api_key': False,
        })

        nginx_config = nginxconf.generate_nginx_config(tier_name)
        nginxconf.apply_nginx_config(nginx_config)

        cls.ts = ts
        cls.keyless_api = api_2

    @classmethod
    def tearDownClass(cls):
        for patcher in cls.patchers:
            patcher.stop()

    def get(self, uri, **kw):
        headers = kw.setdefault('headers', {})
        headers.setdefault('Accept', 'application/json')
        url = 'http://{}:{}{}'.format(HOST, PORT, uri)
        ret = requests.get(url, **kw)
        self.assertEqual(ret.headers['Content-type'], headers['Accept'])
        return ret

    def test_https_redirect(self):
        # http requests are redirected to https
        path_query_fragment = '/some/path?some=arg'  # Note, leaving fragment out on purpose!
        http_url = 'http://{}:{}{}'.format(HOST, REDIR_PORT, path_query_fragment)
        https_url = 'https://{}:{}{}'.format(REQUEST_HOST, PORT, path_query_fragment)
        ret = requests.get(http_url, headers=HOST_HEADER, allow_redirects=False)
        self.assertEqual(ret.status_code, httplib.MOVED_PERMANENTLY)  # 301
        self.assertEqual(ret.headers['Location'], https_url)

    def test_api_key_missing(self):
        ret = self.get('/testing-key-missing/some-path')
        self.assertEqual(ret.status_code, httplib.FORBIDDEN)  # 403
        self.assertEqual(ret.json()['error']['code'], 'api_key_missing')

    def test_api_router_endpoint(self):
        ret = self.get('/api-router')
        ret.raise_for_status()
        self.assertTrue(ret.json())

    def test_not_found(self):
        ret = self.get('/api-router/not/found')
        self.assertEqual(ret.status_code, httplib.NOT_FOUND)  # 404
        ret.json()

    def test_healthcheck(self):
        ret = self.get('/healthcheck')
        ret.raise_for_status()
        ret.json()  # Make sure it's a json response.

    def test_keyless_api(self):
        ret = self.get('/' + self.keyless_api)
        self.assertEqual(ret.status_code, httplib.SERVICE_UNAVAILABLE)  # 503
        ret.json()


if __name__ == '__main__':
    # logging.basicConfig(level='WARNING')
    unittest.main()

# -*- coding: utf-8 -*-
import time
import unittest
import requests
import httplib
import mock
from wsgiref.util import setup_testing_defaults
from wsgiref.simple_server import make_server
import threading
import json

from driftconfig.testhelpers import create_test_domain


from apirouter import nginxconf

# Note: For OSX, install nginx like this:
# brew install nginx-full --with-realip --with-headers-more-module


HOST = 'localhost'
HTTP_HOST = 'something.com'
PORT = 8080         # todo: do not hardcode, get this from nginx config
REDIR_PORT = PORT + 1

UPSTREAM_SERVER_PORT = 8098

REQUEST_HOST = 'just.a.test'
HOST_HEADER = {'Host': REQUEST_HOST}


# A relatively simple WSGI application. It's going to print out the
# environment dictionary after being updated by setup_testing_defaults
def simple_app(environ, start_response):
    setup_testing_defaults(environ)
    status = '200 OK'
    headers = [('Content-type', 'application/json')]
    start_response(status, headers)
    ret = json.dumps({"test_target": "ok"}, indent=4)
    return ret


class TestNginxConfig(unittest.TestCase):


    # Some patching
    @classmethod
    def get_api_targets(cls, tier_name, region_name):
        tags = {'api-status': 'online', 'api-target': cls.deployable_1, 'api-port': UPSTREAM_SERVER_PORT}
        targets = [
            {
                'name': 'test instance',
                'instance_id': 'test-{}'.format(i),
                'private_ip_address': '127.0.0.1',
                'instance_type': 't2.small',
                'tags': tags,
                'placement': {'AvailabilityZone': 'test-zone-1a'}, 'comment': "SOMETIER-drift-base [t2.small] [eu-west-1b]"
            }
            for i in xrange(3)
        ]

        return {cls.deployable_1: targets}


    @classmethod
    def setUpClass(cls):

        # Run echo server
        print "serving at port", UPSTREAM_SERVER_PORT
        cls.httpd = make_server('', UPSTREAM_SERVER_PORT, simple_app)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever)
        cls.server_thread.start()

        cls.patchers = [
            mock.patch('apirouter.nginxconf.get_api_targets', cls.get_api_targets),
        ]

        for patcher in cls.patchers:
            patcher.start()

        # Create config with two deployables. The first one will have targets available (see
        # get_api_targets() above). The second one has not target but is used to test keyless
        # api access.
        config_size = {
            'num_org': 5,
            'num_tiers': 2,
            'num_deployables': 4,
            'num_products': 2,
            'num_tenants': 2,
        }

        import driftconfig.relib
        driftconfig.relib.CHECK_INTEGRITY = []

        t = time.time()
        ts = create_test_domain(config_size)
        cls.ts = ts

        t = time.time() - t
        if t > 0.100:
            print "Warning: create_test_domain() took %.1f seconds." % t

        if 0:
            from driftconfig.relib import create_backend
            from driftconfig.backends import ZipEncoded

            # t = time.time()
            # create_backend('file://~/.drift/testts').save_table_store(ts, run_integrity_check=False, file_format='json')
            # print "WRITING json", time.time() - t

            t = time.time()
            backend = create_backend('file://~/.drift/testts')
            for b in backend, ZipEncoded(backend):
                b.save_table_store(ts, run_integrity_check=False, file_format='pickle')

            print "WRITING pickle", time.time() - t

        # Extract names from config. This way there's no need to assume how the names are generated
        # by create_test_domain() function.
        cls.tier_name = ts.get_table('tiers').find()[0]['tier_name']
        cls.product_name = ts.get_table('products').find()[0]['product_name']
        cls.tenant_name_1 = ts.get_table('tenant-names').find({'product_name': cls.product_name})[0]['tenant_name']
        cls.tenant_name_2 = ts.get_table('tenant-names').find({'product_name': cls.product_name})[1]['tenant_name']

        # Add api router specific config data:
        cls.deployable_1 = ts.get_table('deployables').find()[0]['deployable_name']
        cls.deployable_2 = ts.get_table('deployables').find()[1]['deployable_name']
        cls.api_1 = cls.deployable_1 + '_custom'  # The route prefix name is not neccessary the same as the deployable name.
        cls.api_2 = cls.deployable_2

        # Generate 'routing' data
        routing = ts.get_table('routing')
        routing.add({
            'deployable_name': cls.deployable_1,
            'requires_api_key': True,
            'api': cls.api_1,
        })

        routing.add({
            'deployable_name': cls.deployable_2,
            'requires_api_key': False,
            # Ommit the 'api'. The 'deployable_name' will be used as the api prefix.
        })

        # Generate 'api-keys' and 'api-key-rules' data
        cls.product_api_key = cls.product_name + '-99999999'
        cls.custom_api_key = 'nginx-unittester'

        api_keys = ts.get_table('api-keys')
        api_keys.add({
            'api_key_name': cls.product_api_key,
            'product_name': cls.product_name,
            'key_type': 'product',
        })
        api_keys.add({
            'api_key_name': cls.custom_api_key,
            'product_name': cls.product_name,
            'key_type': 'custom',
        })

        nginx_config = nginxconf.generate_nginx_config(cls.tier_name)
        nginxconf.apply_nginx_config(nginx_config)

        cls.key_api = '/' + cls.api_1
        cls.keyless_api = '/' + cls.api_2

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

        for patcher in cls.patchers:
            patcher.stop()

    def get(self, uri, api_key=None, version=None, status_code=None, tenant_name=None, check_accept=True, **kw):
        headers = kw.setdefault('headers', {})
        if api_key == 'product':
            headers['drift-api-key'] = self.product_api_key
        elif api_key == 'custom':
            headers['drift-api-key'] = self.custom_api_key

        if version is not None:
            headers['drift-api-key'] += ':' + version

        if tenant_name is not None:
            headers['Host'] = '{}.{}'.format(tenant_name, HTTP_HOST)

        headers.setdefault('Accept', 'application/json')
        url = 'http://{}:{}{}'.format(HOST, PORT, uri)
        ret = requests.get(url, allow_redirects=False, **kw)

        if check_accept:
            self.assertEqual(ret.headers.get('Content-Type'), headers['Accept'])

            # Assert a properly formatted json response
            if headers['Accept'] == 'application/json':
                ret.json()

        # Assert proper status code.
        if status_code is None:
            ret.raise_for_status()
        elif status_code == 'ignore':
            pass
        else:
            self.assertEqual(status_code, ret.status_code)

        return ret

    def kget(self, *args, **kw):
        """Same as get() but with an api key."""
        return self.get(*args, api_key='custom', **kw)

    def test_https_redirect(self):
        # http requests are redirected to https
        path_query_fragment = '/some/path?some=arg'  # Note, leaving fragment out on purpose!
        http_url = 'http://{}:{}{}'.format(HOST, REDIR_PORT, path_query_fragment)
        https_url = 'https://{}:{}{}'.format(REQUEST_HOST, PORT, path_query_fragment)
        ret = requests.get(http_url, headers=HOST_HEADER, allow_redirects=False)
        self.assertEqual(ret.status_code, httplib.MOVED_PERMANENTLY)  # 301
        self.assertEqual(ret.headers['Location'], https_url)

    def test_api_key_missing(self):
        ret = self.get('/testing-key-missing/some-path', status_code=httplib.FORBIDDEN)
        self.assertEqual(ret.json()['error']['code'], 'api_key_missing')

    def test_api_router_endpoint(self):
        ret = self.get('/api-router/')

    def test_not_found(self):
        self.get('/api-router/not/found', status_code=httplib.NOT_FOUND)

    def test_healthcheck(self):
        self.get('/healthcheck')

    def test_apirouter_request_endpoint(self):
        # This endpoint returns some introspected information. There is no key
        # required.
        self.get('/api-router/request')

    def test_name_mapping(self):
        # Make sure the tenant name can be mapped to a product.
        for tenant in self.ts.get_table('tenant-names').find():
            # Skip over cls.product_name because it has all kinds of api rules associated with it.
            if tenant['product_name'] == self.product_name:
                continue
            ret = self.get('/api-router/request', tenant_name=tenant['tenant_name'])
            self.assertEqual(ret.json()['product_name'], tenant['product_name'])

    # NOTE!!!!!!!!!! This will be moved into the flask stack!!!!!!
    @classmethod
    def _add_rules(cls):
        """The location of this function is for convenience as the test function comes right after it."""

        # Make a few rules to test various cases:
        # Rule 1: reject clients 1.6.0 and 1.6.2 and ask them to upgrade.
        # Rule 2: redirect client 1.6.5 to another tenant.
        # Rule 3: always let client 1.6.6 pass through.
        # Rule 4: reject all clients with message "server is down".
        rules = [
            ('upgrade-client-1.6', ["1.6.0", "1.6.2"], 'reject', [404, {"action": "upgrade_client"}]),
            ('redirect-to-new-tenant', ["1.6.5"], 'redirect', cls.tenant_name_2),
            ('always-pass', ["1.6.6"], 'pass', cls.tenant_name_1),
            ('downtime-message', [], 'reject', [503, {"message": "The server is down for maintenance."}]),
        ]

        api_key_rules = cls.ts.get_table('api-key-rules')

        for i, (rule_name, version_pattern, rule_type, custom) in enumerate(rules):
            row = api_key_rules.add({
                'product_name': cls.product_name,
                'rule_name': rule_name,
                'assignment_order': i,
                'version_patterns': version_pattern,
                'rule_type': rule_type,
            })
            if rule_type == 'reject':
                row['reject'] = {'status_code': custom[0], 'response_body': custom[1]}
            elif rule_type == 'redirect':
                row['redirect'] = {'tenant_name': custom}

            row['response_header'] = {'Test-Rule-Name': rule_name}  # To test response headers

    def test_api_key(self):
        # First, test keyless endpoint, with and without a key
        ret = self.get(
            self.keyless_api,
            api_key='product', version='1.6.6',
            tenant_name=self.tenant_name_1,
            status_code=503,
        )
        # This one passes through but will hit 503 because there is no upstream server
        # for 'self.key_api'.
        self.assertIn("No targets registered", ret.json()['message'])

        ret = self.get(
            self.keyless_api,
            tenant_name=self.tenant_name_1,
            status_code=503,
        )
        self.assertIn("No targets registered", ret.json()['message'])

        # Now test endpoint which requires a key, using a valid key, invalid key and no key
        ret = self.get(
            self.key_api + '_mangled',
            api_key='product', version='1.6.6',
            tenant_name=self.tenant_name_1,
            status_code=200,
        )
        self.assertIn("test_target", ret.json())

        ret = self.get(
            self.key_api,
            tenant_name=self.tenant_name_1,
            status_code=403,
        )
        self.assertDictContainsSubset({"code": "api_key_missing"}, ret.json()['error'])

        ret = self.get(
            self.key_api,
            headers={'drift-api-key': 'totally bogus key'},
            tenant_name=self.tenant_name_1,
            status_code=403,
        )
        self.assertDictContainsSubset({"code": "api_key_missing"}, ret.json()['error'])


if __name__ == '__main__':
    # logging.basicConfig(level='WARNING')
    unittest.main()

# -*- coding: utf-8 -*-
import sys
import os
import os.path
import time
import unittest
import requests
import http.client
import mock
from wsgiref.util import setup_testing_defaults
import subprocess
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
    ret = json.dumps({"test_target": "ok"}, indent=4).encode('utf-8')
    req = '{REQUEST_METHOD} http://{HTTP_HOST}{REQUEST_URI}{QUERY_STRING}'.format(**environ)
    print("UWSGI request: {} (key={}).".format(req, environ.get('HTTP_DRIFT_API_KEY')))
    return [ret]


class TestNginxConfig(unittest.TestCase):

    # Some patching
    @classmethod
    def get_ec2_targets_for_tier(cls, tier_name, check_health=False):
        tags = {
            'api-status': 'online',
            'api-target': cls.deployable_1,
            'api-port': UPSTREAM_SERVER_PORT,
            'api-param': 'weight=100',
        }
        targets = [
            {
                'name': 'test instance',
                'instance_id': 'test-{}'.format(i),
                'private_ip_address': '127.0.0.1',
                'instance_type': 't2.small',
                'tags': tags,
                'placement': {'AvailabilityZone': 'test-zone-1a'}, 'comment': "SOMETIER-drift-base [t2.small] [eu-west-1b]"
            }
            for i in range(3)
        ]

        return {cls.deployable_1: targets}

    @classmethod
    def get_api_endpoints_for_tier(cls, tier_name, check_health=False, public_url=None):
        return {}

    @classmethod
    def setUpClass(cls):

        import driftconfig.relib
        driftconfig.relib.CHECK_INTEGRITY = []

        # Create config with two deployables. The first one will have targets available (see
        # get_ec2_targets_for_tier() above). The second one has not target but is used to test keyless
        # api access.
        config_size = {
            'num_org': 5,
            'num_tiers': 2,
            'num_deployables': 4,
            'num_products': 2,
            'num_tenants': 2,
        }

        t = time.time()
        ts = create_test_domain(config_size)
        t = time.time() - t
        if t > 0.100:
            print("Warning: create_test_domain() took %.1f seconds." % t)

        cls.ts = ts
        cls.patchers = [
            mock.patch('apirouter.nginxconf.get_ec2_targets_for_tier', cls.get_ec2_targets_for_tier, ts),
            mock.patch('apirouter.nginxconf.get_api_endpoints_for_tier', cls.get_api_endpoints_for_tier, ts),
        ]

        for patcher in cls.patchers:
            patcher.start()

        # Toggling the status of all tenants from 'initializing' to 'active'. This is normally
        # done by the tenant provisioning logic but we don't want to run that if we don't
        # need to.
        for tenant in ts.get_table('tenants').find():
            tenant['state'] = 'active'

        # Extract names from config. This way there's no need to assume how the names are generated
        # by create_test_domain() function.
        cls.tier_name = ts.get_table('tiers').find()[0]['tier_name']
        cls.product_name = ts.get_table('products').find()[0]['product_name']
        cls.tenant_name_1 = ts.get_table('tenant-names').find({'product_name': cls.product_name})[0]['tenant_name']
        cls.tenant_name_2 = ts.get_table('tenant-names').find({'product_name': cls.product_name})[1]['tenant_name']

        # Add api router specific config data:
        deployables = ts.get_table('deployables').find({'tier_name': cls.tier_name})
        cls.deployable_1 = deployables[0]['deployable_name']
        cls.deployable_2 = deployables[1]['deployable_name']
        cls.deployable_3 = deployables[2]['deployable_name']
        cls.api_1 = cls.deployable_1 + '_custom'  # The route prefix name is not neccessarily the same as the deployable name.
        cls.api_2 = cls.deployable_2
        cls.api_3 = cls.deployable_3

        # Generate 'routing' data
        routing = ts.get_table('routing')

        routing.add({
            'tier_name': cls.tier_name,
            'deployable_name': cls.deployable_1,
            'requires_api_key': True,
            'api': cls.api_1,
        })

        routing.add({
            'tier_name': cls.tier_name,
            'deployable_name': cls.deployable_2,
            'requires_api_key': False,
            # Ommit the 'api'. The 'deployable_name' will be used as the api prefix.
        })

        routing.add({
            'tier_name': cls.tier_name,
            'deployable_name': cls.deployable_3,
            'requires_api_key': False,
            # Ommit the 'api'. The 'deployable_name' will be used as the api prefix.
        })
        # Make deployable 3 inactive.
        deployables[2]['is_active'] = False
        deployables[2]['reason_inactive'] = "Testing inactive."

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
            'key_type': 'custom',
        })
        # Generate 'nginx' data
        nginx = ts.get_table('nginx')
        nginx.add({
            'tier_name': cls.tier_name,
            # 'user': getpass.getuser(),
            'api_key_passthrough': [
                {'key_name': 'drift-api-key', 'key_value': '^LetMeIn:.*:8888$', 'product_name': cls.product_name},
                {'key_name': 'magic-api-key', 'key_value': '^LetMeInAsWell:.*:1234$', 'product_name': cls.product_name},
                {'key_name': 'drift-api-key', 'key_value': '^LockMeOut:.*:8888$', 'product_name': 'a-different-product'},
            ],
            'worker_rlimit_nofile': 100,
            'worker_connections': 100,
            'healthcheck_targets': True,
            'healthcheck_port': 8901,
        })

        # Run uwsgi echo server.
        uwsgi_exe = _find_executable('uwsgi')
        if not uwsgi_exe:
            raise RuntimeError("Can't continue without uwsgi executable.")

        cmd = [
            'uwsgi',
            '--socket', ':{}'.format(UPSTREAM_SERVER_PORT),
            '--http', ':8901',  # For the health check endpoint. Note, can't use default 8080 port because of nginx.
            '--stats', '127.0.0.1:9191',
            '--wsgi-file', __file__,
            '--callable', 'simple_app',
            '--processes', '1',
            '--threads', '1',
        ]
        cls.uwsgi = subprocess.Popen(cmd)

        nginx_config = nginxconf.generate_nginx_config(cls.tier_name)
        if 0:
            print("Using nginx.conf:")
            # Pretty print config
            try:
                import pygments
                lexerob = pygments.lexers.get_lexer_by_name('nginx')
                formatter = pygments.formatters.get_formatter_by_name('console256', style='tango')
                print(pygments.highlight(nginx_config['config'], lexerob, formatter))
            except ImportError:
                print(nginx_config['config'])

        ret = nginxconf.apply_nginx_config(nginx_config, skip_if_same=False)
        if ret != 0:
            raise RuntimeError("Failed to set up test, apply_nginx_config() returned with {}.".format(ret))

        # Generate a mock status.json file for /api-router/ endpoint
        path = os.path.join(nginx_config['data']['plat']['root'], 'api-router')
        if not os.path.exists(path):
            os.mkdir(path)
        statusfile = os.path.join(path, 'status.json')
        if not os.path.exists(statusfile):
            with open(statusfile, 'w') as f:
                f.write('{"hi": "there"}\n')

        cls.nginx_config = nginx_config
        cls.key_api = '/' + cls.api_1
        cls.keyless_api = '/' + cls.api_2
        cls.inactive_api = '/' + cls.api_3

    @classmethod
    def tearDownClass(cls):
        cls.uwsgi.terminate()

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
                try:
                    ret.json()
                except Exception as e:
                    print("Badly formatted json or no json at all:")
                    print("The error: ", e)
                    print("The url:", url)
                    print("The ret.raw: ", repr(ret.raw))
                    print("The ret.text:", repr(ret.text))
                    print("The request headers: ", kw)
                    print("The headers: ", ret.headers)
                    raise

        # Assert proper status code.
        if status_code is None:
            ret.raise_for_status()
        elif status_code == 'ignore':
            pass
        else:
            msg = "{} != {}: {}Header: {}".format(status_code, ret.status_code, ret.text, headers)
            self.assertEqual(status_code, ret.status_code, msg=msg)

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
        self.assertEqual(ret.status_code, http.client.MOVED_PERMANENTLY)  # 301
        self.assertEqual(ret.headers['Location'], https_url)

    def test_api_key_missing(self):
        ret = self.get('/testing-key-missing/some-path', status_code=http.client.FORBIDDEN)
        self.assertEqual(ret.json()['error']['code'], 'api_key_error')
        self.assertIn("API key not found.", ret.json()['error']['description'])

    def test_api_router_endpoint(self):
        self.get('/api-router/')

    def test_not_found(self):
        self.get('/api-router/not/found', status_code=http.client.NOT_FOUND)

    def test_healthcheck(self):
        self.get('/healthcheck')

    def test_apirouter_request_endpoint(self):
        # This endpoint returns some introspected information. There is no key
        # required.
        self.get('/api-router/request')

    def test_name_mapping(self):
        # Make sure the tenant name can be mapped to a product.
        for tenant in self.ts.get_table('tenant-names').find({'tier_name': self.tier_name}):
            # Skip over cls.product_name because it has all kinds of api rules associated with it.
            if tenant['product_name'] == self.product_name:
                continue
            ret = self.get('/api-router/request', tenant_name=tenant['tenant_name'])
            self.assertEqual(ret.json()['product_name'], tenant['product_name'])

    def test_inactive_deployables(self):
        # Deployable marked as 'is_active=False' should respond with a 503 and a custom message.
        # versionless key.
        ret = self.get(
            self.inactive_api,
            status_code=503,
        )
        self.assertIn("Service Unavailable. Testing inactive.", ret.json()['message'])

    def test_keyless_endpoints(self):
        # First, test keyless endpoint, with and without a key. Test versioned and
        # versionless key.
        ret = self.get(
            self.keyless_api,
            api_key='product', version=None,
            tenant_name=self.tenant_name_1,
            status_code=503,
        )
        # This one passes through but will hit 503 because there is no upstream server
        # for 'self.key_api'.
        self.assertIn("No targets registered", ret.json()['message'])

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

    def test_api_key_check(self):
        # Now test endpoint which requires a key, using a valid key, invalid key and no key
        ret = self.get(
            self.key_api,
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
        self.assertEqual(ret.json()['error']['code'], 'api_key_error')
        self.assertIn("API key not found.", ret.json()['error']['description'])

        ret = self.get(
            self.key_api,
            headers={'drift-api-key': 'totally bogus key'},
            tenant_name=self.tenant_name_1,
            status_code=403,
        )
        self.assertEqual(ret.json()['error']['code'], 'api_key_error')
        self.assertIn("API key not found.", ret.json()['error']['description'])

    def test_custom_key_access(self):
        self.kget(
            self.key_api,
            status_code=200,
        )

    def test_api_key_passthrough(self):
        # Test passthrough
        ret = self.get(
            self.key_api,
            headers={'drift-api-key': 'LetMeIn:1.2.3:8888'},
            tenant_name=self.tenant_name_1,
            status_code=200,
        )
        self.assertIn("test_target", ret.json())

        ret = self.get(
            self.key_api,
            headers={'magic-api-key': 'LetMeInAsWell:1.2.3:1234'},
            tenant_name=self.tenant_name_1,
            status_code=200,
        )
        self.assertIn("test_target", ret.json())

        # Test mismatched products
        ret = self.get(
            self.key_api,
            headers={'drift-api-key': 'LockMeOut:1.2.3:8888'},
            tenant_name=self.tenant_name_1,
            status_code=403,
        )
        self.assertEqual(ret.json()['error']['code'], 'api_key_error')
        self.assertIn("API key is for", ret.json()['error']['description'])

        # Test bad key and requires key flags in the nginx config itself.
        ret = self.get('/api-router/request')
        self.assertEqual(ret.json()['bad_key_and_requires_key'], 'true:false')

        # Note: we
        ret = self.get(
            '/api-router/request',
            api_key='product',
            version='1.6.6',
            tenant_name=self.tenant_name_1
        )
        self.assertEqual(ret.json()['bad_key_and_requires_key'], 'false:false')


def _find_executable(executable, path=None):
    """Find if 'executable' can be run. Looks for it in 'path'
    (string that lists directories separated by 'os.pathsep';
    defaults to os.environ['PATH']). Checks for all executable
    extensions. Returns full path or None if no command is found.
    """
    # https://gist.github.com/4368898
    # Public domain code by anatoly techtonik <techtonik@gmail.com>
    # AKA Linux `which` and Windows `where`
    if path is None:
        path = os.environ['PATH']
    paths = path.split(os.pathsep)
    extlist = ['']
    if os.name == 'os2':
        (base, ext) = os.path.splitext(executable)
        # executable files on OS/2 can have an arbitrary extension, but
        # .exe is automatically appended if no dot is present in the name
        if not ext:
            executable = executable + ".exe"
    elif sys.platform == 'win32':
        pathext = os.environ['PATHEXT'].lower().split(os.pathsep)
        (base, ext) = os.path.splitext(executable)
        if ext.lower() not in pathext:
            extlist = pathext
    for ext in extlist:
        execname = executable + ext
        if os.path.isfile(execname):
            return execname
        else:
            for p in paths:
                f = os.path.join(p, execname)
                if os.path.isfile(f):
                    return f
    else:
        return None


if __name__ == '__main__':
    # logging.basicConfig(level='WARNING')
    unittest.main()

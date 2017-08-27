from setuptools import setup, find_packages
from setuptools.command.install import install
import pip
import sys
import pkg_resources
import subprocess
import os


class CustomInstallCommand(install):
    def run(self):
        ret = install.run(self)

        if sys.platform.startswith("linux"):
            print "Running custom install steps for Linux platform"
            setup_script = pkg_resources.resource_filename(__name__, "scripts/setup")
            print "Executing ", setup_script
            subprocess.call(setup_script)
        elif sys.platform.startswith("darwin"):
            print "Note! Custom install steps not defined for OSX."
        else:
            print "Note! Custom install steps not defined for platform ", sys.platform


setup(
    name="drift-apirouter",
    version='0.1.0',
    license='MIT',
    author="Directive Games North",
    author_email="info@directivegames.com",
    description="Routes HTTP requests to Drift Web Services.",
    packages=find_packages(
        exclude=["*.tests", "*.tests.*", "tests.*", "tests"]
    ),
    include_package_data=True,

    install_requires=[
        str(i.req)
        for i in pip.req.parse_requirements(pkg_resources.resource_filename(__name__, "requirements.txt"), session=pip.download.PipSession())
        if i.req
    ],

    entry_points='''
        [console_scripts]
        apirouter-conf=apirouter.nginxconf:cli
    ''',

    cmdclass={
        'install': CustomInstallCommand,
    },

    classifiers=[
        'Drift :: Tag :: Core',
        'Drift :: Tag :: Apirouter',
        'Environment :: Web Environment',
        'Framework :: Drift',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Application Frameworks',
        'Topic :: Software Development :: Libraries :: Python Modules',
    ],

)

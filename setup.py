from setuptools import setup, find_packages


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

    entry_points='''
        [console_scripts]
        apirouter-conf=apirouter.nginxconf:cli
    ''',

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

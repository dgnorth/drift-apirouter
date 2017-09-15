from setuptools import setup, find_packages
import pip


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
        for i in pip.req.parse_requirements("requirements.txt", session=pip.download.PipSession())
        if i.req
    ],

    tests_require=[
        'uwsgi',
    ]

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

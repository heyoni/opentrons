# Inspired by:
# https://hynek.me/articles/sharing-your-labor-of-love-pypi-quick-and-dirty/
import codecs
import os
from setuptools import setup, find_packages


HERE = os.path.abspath(os.path.dirname(__file__))


def get_version():
    with open(os.path.join(HERE, 'version')) as version:
        return version.readline().strip()


VERSION = get_version()

DISTNAME = 'update'
LICENSE = 'Apache 2.0'
AUTHOR = "Opentrons"
EMAIL = "engineering@opentrons.com"
URL = "https://github.com/OpenTrons/opentrons"
DOWNLOAD_URL = ''
CLASSIFIERS = [
    'Development Status :: 5 - Production/Stable',
    'Environment :: Console',
    'Operating System :: OS Independent',
    'Intended Audience :: Science/Research',
    'Programming Language :: Python',
    'Programming Language :: Python :: 3',
    'Programming Language :: Python :: 3.6',
    'Topic :: Scientific/Engineering',
]
KEYWORDS = ["robots", "automation", "lab"]
DESCRIPTION = (
    "A server to update software and firmware on Opentrons robots")
PACKAGES = find_packages(where='.', exclude=["tests.*", "tests"])
INSTALL_REQUIRES = ['aiohttp==2.3.8']


def read(*parts):
    """
    Build an absolute path from *parts* and and return the contents of the
    resulting file.  Assume UTF-8 encoding.
    """
    with codecs.open(os.path.join(HERE, *parts), "rb", "utf-8") as f:
        return f.read()


# def write_version_file(filename='opentrons/_version.py'):
#     contents = """
# # This file is generated during setup--do not edit manually
#
# __version__ = '{version}'
# """
#     with open(filename, 'w') as version_file:
#         version_file.write(contents.format(version=VERSION))


if __name__ == "__main__":
    # write_version_file()

    setup(
        python_requires='>=3.6',
        name=DISTNAME,
        description=DESCRIPTION,
        license=LICENSE,
        url=URL,
        version=VERSION,
        author=AUTHOR,
        author_email=EMAIL,
        maintainer=AUTHOR,
        maintainer_email=EMAIL,
        keywords=KEYWORDS,
        long_description=read("README.rst"),
        packages=PACKAGES,
        zip_safe=False,
        classifiers=CLASSIFIERS,
        install_requires=INSTALL_REQUIRES,
        setup_requires=['pytest-runner'],
        tests_require=['pytest'],
        include_package_data=True
    )

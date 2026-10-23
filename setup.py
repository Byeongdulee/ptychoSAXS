from setuptools import find_packages, setup

setup(
    name='ptychoSAXS',
    version='0.3.0',
    author='Byeongdu Lee',
    author_email='blee@anl.gov',
    # find_packages, not a hand-written list: gui/, tools/, debug/ and
    # analysis/ used to be reachable only because the repo root happened to
    # land on sys.path, which is why ten modules mutated sys.path at import
    # time and why the GUI had to be started from inside gui/.
    packages=find_packages(
        include=["ptychosaxs*", "gui*"]
    ),
    package_data={"gui": ["ui/*.ui", "ini/*.ini", "*.json"]},
    url='#',
    license='LICENSE.txt',
    description='12ID-C ptycho-SAXS DAQ/Controller tool',
    python_requires=">=3.10",
    # py12inifunc used to be required here. Its .ini format now lives in
    # gui/beamline_ini.py: the package was imported behind a try/except that
    # fell back to a no-op stub, so an environment missing it lost every
    # setting the GUI tried to persist, silently.
    install_requires=[
        "pyqudis",
    ],
    # Hardware libraries, optional on purpose. pihexapod is only needed for
    # fly scans (the wave generator has no EPICS interface); step motion goes
    # through the EPICS motor records and needs nothing but pyepics. Keeping
    # these required meant the package would not install in an environment
    # that only ever drives the IOC.
    extras_require={
        "hexapod": ["pihexapod"],
        "acs": ["acspy"],
        "mcp": ["mcp"],
        "dev": ["pytest", "pytest-timeout"],
    },
)

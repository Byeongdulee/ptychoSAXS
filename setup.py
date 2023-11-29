from setuptools import setup

setup(
   name='ptychoSAXS',
   version='0.1.1',
   author='Byeongdu Lee',
   author_email='blee@anl.gov',
   packages=['ptychosaxs'],
   url='#',
   license='LICENSE.txt',
   description='12ID-C ptycho-SAXS DAQ/Controller tool',
   install_requires=[
       "acspy",
       "pyqudis",
       "pihexapod",
   ],
)
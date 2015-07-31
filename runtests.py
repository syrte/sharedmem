import sharedmem
import sys
import os

from numpy.testing import Tester
sys.path.insert(0, os.path.abspath('.'))

from sys import argv

tester = Tester()
result = tester.test(extra_argv=['-w', 'tests'] + argv[1:])
if len(result.errors) > 0:
    raise Exception("Test Failed")

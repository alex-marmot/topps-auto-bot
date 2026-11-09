import multiprocessing

# Python 3.14 removed stdlib distutils. Import setuptools so its bundled
# compatibility module is available to UC in the frozen process.
import setuptools

from topps_auto_bot.__main__ import main

multiprocessing.freeze_support()
raise SystemExit(main())

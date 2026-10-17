"""Unit tests. Run: python3 -m unittest discover -s tests -t .

Importing tests.fakes (done here) installs a fake `decky` module and puts
py_modules on sys.path, so vpn_deck can be imported without Decky.
"""

from tests import fakes as _fakes  # noqa: F401

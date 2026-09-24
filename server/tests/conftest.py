"""
Test-only environment setup, evaluated before any test module imports
api.py (which needs LESPHOTOS_SECRET at import time - see auth.load_secret)
or auth.py (which needs LESPHOTOS_DATA_DIR resolvable). Neither value needs
to be real: these tests never open auth.sqlite3 or sign a real cookie, they
just need the module-level guards in api.py/auth.py to pass.
"""

import os

os.environ.setdefault("LESPHOTOS_SECRET", "test-secret-not-for-production-use")
os.environ.setdefault("LESPHOTOS_DATA_DIR", "/tmp/lesphotos-pytest-data")

"""Enter Bloomkeeper with a controlled import path and bundled CA trust roots."""

import os
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
os.environ['SSL_CERT_FILE'] = str(root / 'push_vendor/certifi/cacert.pem')
runpy.run_path(str(root / 'collector.py'), run_name='__main__')

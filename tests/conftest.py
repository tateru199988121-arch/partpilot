import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RAW = os.path.join(ROOT, "data", "sample")

from pathlib import Path
from scripts.make_sample_data import HEADERS, make_sample_data

if any(not (Path(RAW) / filename).exists() for filename in HEADERS):
    make_sample_data(Path(RAW))

"""Check the shared local simulator contract without importing GPU/model libraries."""
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = ROOT / 'requirements-sim.txt'


def expected_versions():
    return dict(line.split('==') for line in CONSTRAINTS.read_text().splitlines()
                if line and not line.startswith('#'))


def require_local_sim():
    expected = expected_versions()
    actual = {}
    for name in expected:
        try: actual[name] = version(name)
        except PackageNotFoundError: actual[name] = 'missing'
    if actual != expected:
        raise RuntimeError(
            'Local LIBERO simulation versions differ: ' + json.dumps(actual) +
            f'. Use {ROOT}/.venv/bin/python; restore with '
            f'uv pip install --python {ROOT}/.venv/bin/python -r {ROOT}/requirements.txt')
    return actual


if __name__ == '__main__':
    actual = require_local_sim()
    import mujoco
    import numpy as np
    option = mujoco.MjvOption()
    option.geomgroup[:] = [0, 1, 0, 0, 0, 0]
    if option.geomgroup.tolist() != [0, 1, 0, 0, 0, 0]:
        raise RuntimeError('MuJoCo/NumPy array assignment failed')
    print(json.dumps({'python': sys.executable, 'versions': actual, 'binding_check': 'ok'}, indent=2))

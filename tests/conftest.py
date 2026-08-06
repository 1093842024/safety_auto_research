"""Project test session setup.

Environment accommodation (NOT a product change):

The managed test venv ships pandas 3.0, where pyarrow-backed strings are the
*default* string dtype. In this particular pyarrow 25 / numpy 2 build,
constructing an ``ArrowStringArray`` from a sequence segfaults deep inside the
C layer (e.g. ``pd.read_csv`` on any CSV with a string column). That crash is a
pandas/pyarrow/numpy interop bug in the environment, unrelated to the project
code under test.

Forcing ``future.infer_string = False`` restores the pandas 2.x default (plain
``object``-dtype strings), which sidesteps the segfault and matches the dtype
behavior the suites were written against. Imported early so the option is set
before any module-under-test builds a DataFrame.
"""

import pandas as pd

try:
    pd.set_option("future.infer_string", False)
except Exception:  # pragma: no cover - older pandas without the option
    pass

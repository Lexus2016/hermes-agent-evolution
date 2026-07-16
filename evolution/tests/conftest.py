# -*- coding: utf-8 -*-
"""Pytest path setup for the ``evolution`` workspace test suite.

The modules under test live in ``evolution/lib/``.  The test files use two
import conventions:

* flat  — ``from root_cause_diagnosis import ...`` / ``from feasibility_checker
  import ...`` (module directly on ``sys.path``);
* package — ``from lib.recheck_classifier import ...`` /
  ``from lib.intra_task_compression import ...`` (``lib`` as a namespace
  package).

To make the committed suite runnable from a clean checkout with a plain
``pytest evolution/tests`` (no ``PYTHONPATH`` juggling), inject both the
``evolution/`` directory (enables ``lib.<module>``) and ``evolution/lib/``
(enables the flat ``<module>``) onto ``sys.path``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_EVOLUTION_DIR = _TESTS_DIR.parent
_LIB_DIR = _EVOLUTION_DIR / "lib"

for _p in (_EVOLUTION_DIR, _LIB_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

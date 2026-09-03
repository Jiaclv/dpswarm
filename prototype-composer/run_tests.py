#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from tests.test_scenarios import ScenarioTests  # noqa: E402

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(ScenarioTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\nSUMMARY: {total - failed}/{total} passed")
    sys.exit(0 if result.wasSuccessful() else 1)

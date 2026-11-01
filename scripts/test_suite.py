"""Run unittest with an explicit machine-readable result and reliable exit status."""

import json
from pathlib import Path
import sys
import unittest


if __name__ == "__main__":
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(
        unittest.defaultTestLoader.discover("tests")
    )
    Path("artifacts").mkdir(exist_ok=True)
    report = dict(
        tests=result.testsRun,
        failures=len(result.failures),
        errors=len(result.errors),
        skipped=[reason for _, reason in result.skipped],
        success=result.wasSuccessful(),
    )
    Path("artifacts/tests.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    raise SystemExit(0 if result.wasSuccessful() else 1)

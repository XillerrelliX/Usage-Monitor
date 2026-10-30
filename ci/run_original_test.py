"""Run one unchanged original test with its normal class/case fixtures."""
from __future__ import annotations

import argparse
from pathlib import Path
import unittest

from common import write_json
from run_python_tests import canonical_host_python, flatten

TARGET = ('test_windows_package.WindowsPackageBehaviorTests.'
          'test_even_a_hash_approved_malformed_archive_cannot_escape_extraction')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    host = canonical_host_python()
    discovered = list(flatten(unittest.defaultTestLoader.discover(str(args.source_root / 'tests'))))
    identities = [test.id() for test in discovered]
    selected = [test for test in discovered if test.id() == TARGET]
    if len(selected) != 1:
        raise RuntimeError('The exact original retry method was not discovered once')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    success = (result.wasSuccessful() and result.testsRun == 1 and not result.skipped and
               not result.expectedFailures and not result.unexpectedSuccesses)
    write_json(args.output, {'status': 'passed' if success else 'failed', 'target': TARGET,
        'hostPython': host, 'discoveredIds': identities, 'executedThisRun': result.testsRun,
        'passed': 1 if success else 0, 'skipped': [{'id': test.id(), 'reason': reason} for test, reason in result.skipped],
        'failed': [{'id': test.id(), 'traceback': trace} for test, trace in result.failures],
        'errors': [{'id': test.id(), 'traceback': trace} for test, trace in result.errors]})
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())

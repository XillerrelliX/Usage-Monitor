"""Run complete discovery and retain every test identity, including skips."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import unittest

from common import write_json


WINDOWS_METHODS = {
    'test_existing_out_ancestor_dashboard_source_acl_unchanged',
    'test_missing_out_inherits_ancestor_across_new_meter_and_completion',
    'test_atomic_replacement_inherits_parent_grants_without_changing_parent',
    'test_second_writer_preserves_original_access_and_watcher_completes',
    'test_legacy_three_principal_scope_is_preserved_by_new_writers',
    'test_repeated_begin_preserves_existing_scope_and_lock_acl',
    'test_normal_windows_meter_paths_never_call_security_sdk_or_chmod',
    'test_windows_launcher_selects_available_python_and_preserves_unicode_args',
    'test_no_system_python_custom_runtime_first_install_and_dashboard_config_reuse',
    'test_windows_setup_then_refresh_launchers_keep_settings_and_verified_ledger',
}


def flatten(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def canonical_host_python():
    """Expose the actual host executable to fixtures that validate local paths."""
    reported = sys.executable
    resolved = str(Path(reported).resolve(strict=True))
    sys.executable = resolved
    return {'reported': reported, 'resolved': resolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--minimum-tests', type=int, default=351)
    args = parser.parse_args()
    host_python = canonical_host_python()
    suite = unittest.defaultTestLoader.discover(str(args.source_root / 'tests'))
    identities = [test.id() for test in flatten(suite)]
    document = {'discovered': len(identities), 'discoveredIds': identities, 'cases': [], 'hostPython': host_python}
    write_json(args.output, document)

    class Result(unittest.TextTestResult):
        def stopTest(self, test):
            identity = test.id()
            failed = {case.id() for case, _ in self.failures + self.errors}
            skipped = dict((case.id(), reason) for case, reason in self.skipped)
            status = ('skipped' if identity in skipped else 'failed' if identity in failed or
                      any(item.startswith(identity + ' (') for item in failed) else 'passed')
            document['cases'].append({'id': identity, 'status': status,
                                      **({'reason': skipped[identity]} if identity in skipped else {})})
            write_json(args.output, document)
            super().stopTest(test)

    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    unexpected_skips = []
    for test, reason in result.skipped:
        identity = test.id()
        permitted = (os.name != 'nt' and (
            identity.startswith('test_windows_package.WindowsPackageBehaviorTests.') or
            identity.rsplit('.', 1)[-1] in WINDOWS_METHODS))
        if not permitted:
            unexpected_skips.append({'id': identity, 'reason': reason})
    document.update(executed=result.testsRun, passed=sum(item['status'] == 'passed' for item in document['cases']),
                    skipped=[{'id': test.id(), 'reason': reason} for test, reason in result.skipped],
                    failed=[{'id': test.id(), 'traceback': detail} for test, detail in result.failures],
                    errors=[{'id': test.id(), 'traceback': detail} for test, detail in result.errors],
                    unexpectedSkips=unexpected_skips)
    complete = (len(identities) >= args.minimum_tests and len(set(identities)) == len(identities)
                and result.testsRun == len(identities) and not unexpected_skips
                and result.wasSuccessful() and not result.expectedFailures and not result.unexpectedSuccesses)
    document['status'] = 'passed' if complete else 'failed'
    write_json(args.output, document)
    print(json.dumps({key: document[key] for key in ('status', 'discovered', 'executed', 'passed', 'skipped', 'unexpectedSkips')}))
    return 0 if complete else 1


if __name__ == '__main__':
    raise SystemExit(main())

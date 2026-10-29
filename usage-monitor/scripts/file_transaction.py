"""Restore local report/config files if a later refresh operation fails."""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def preserve_files(paths):
    """Snapshot only explicit files; never remove folders or unrelated contents."""
    directory = Path(tempfile.mkdtemp(prefix='codex-usage-rollback-'))
    originals, keep_recovery = [], False
    try:
        for number, target in enumerate(dict.fromkeys(Path(path).resolve() for path in paths)):
            if target.exists() and not target.is_file():
                raise ValueError('A dashboard/config destination is not a file: '+str(target))
            backup = directory/str(number)
            existed = target.is_file()
            originals.append((target, backup, existed))
            if existed:
                shutil.copy2(target, backup)
        try:
            yield
        except BaseException:
            failures = []
            for target, backup, existed in reversed(originals):
                try:
                    if existed:
                        if target.is_file() and target.read_bytes() == backup.read_bytes() and target.stat().st_mtime_ns == backup.stat().st_mtime_ns:
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        # Restoring by copy avoids a failed publication rename hook.
                        shutil.copy2(backup, target)
                    elif target.is_file():
                        target.unlink()
                except OSError:
                    failures.append(str(target))
            if failures:
                keep_recovery = True
                raise OSError('Refresh failed and some original files could not be restored: '+', '.join(failures)+
                              '. Original recovery files remain in '+str(directory))
            raise
    finally:
        if not keep_recovery:
            # Every backup is an explicitly created plain file; no recursive delete.
            for _, backup, _ in originals:
                backup.unlink(missing_ok=True)
            directory.rmdir()

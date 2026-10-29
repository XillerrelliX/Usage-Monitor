"""Small, local-only machine settings; never part of a portable export."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _profile(platform=None, environ=None, home=None):
    return (sys.platform if platform is None else platform,
            os.environ if environ is None else environ,
            Path.home() if home is None else Path(home))


def default_settings_file(platform=None, environ=None, home=None):
    platform, environ, home = _profile(platform, environ, home)
    if platform == 'win32':
        base = Path(environ.get('LOCALAPPDATA') or home/'AppData/Local')
    elif platform == 'darwin':
        base = home/'Library/Application Support'
    else:
        base = Path(environ.get('XDG_CONFIG_HOME') or home/'.config')
    return base/'codex-local-usage/settings.json'


def default_dashboard_directory(platform=None, environ=None, home=None):
    platform, environ, home = _profile(platform, environ, home)
    if platform == 'win32':
        base = Path(environ.get('LOCALAPPDATA') or home/'AppData/Local')
    elif platform == 'darwin':
        base = home/'Library/Application Support'
    else:
        base = Path(environ.get('XDG_DATA_HOME') or home/'.local/share')
    return base/'codex-local-usage/dashboard'


def default_runtime_directory(platform=None, environ=None, home=None):
    return default_dashboard_directory(platform, environ, home).parent/'runtime'


def _validate(settings):
    required = {'schemaVersion', 'database', 'namesDatabases', 'projectState',
                'outputDirectory', 'cacheFile'}
    if not isinstance(settings, dict):
        raise ValueError('The local setup file must contain a settings object.')
    version = settings.get('schemaVersion')
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unsupported local setup version; expected schemaVersion 1 or 2.')
    if version == 2:
        required.add('offline')
    if set(settings) != required:
        raise ValueError('The local setup file has missing or unsupported settings.')

    def absolute(value, optional=False):
        return (optional and value is None) or (
            isinstance(value, str) and bool(value) and Path(value).is_absolute())

    if not all(absolute(settings[key]) for key in ('database', 'outputDirectory')):
        raise ValueError('Saved database and dashboard paths must be absolute.')
    if not all(absolute(settings[key], True) for key in ('projectState', 'cacheFile')):
        raise ValueError('Saved project/cache paths must be absolute or null.')
    names = settings['namesDatabases']
    if names is not None and (not isinstance(names, list) or not names or
                              not all(absolute(value) for value in names)):
        raise ValueError('Saved sidebar databases must be an absolute path list or null.')
    if version == 2:
        offline = settings['offline']
        if not isinstance(offline, dict) or set(offline) != {'codexHome', 'timezone', 'engine', 'runtimeDirectory'}:
            raise ValueError('Invalid saved offline setup settings.')
        if not all(absolute(offline[key]) for key in ('codexHome', 'engine', 'runtimeDirectory')):
            raise ValueError('Saved offline paths must be absolute.')
        if offline['timezone'] is not None and (not isinstance(offline['timezone'], str) or not offline['timezone'].strip()):
            raise ValueError('Saved offline timezone must be a nonempty string or null.')
    return settings


def load_settings(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return _validate(json.loads(path.read_text(encoding='utf-8')))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid local setup file '+str(path)+': '+str(exc)+
                         ' Original bytes were preserved. Move it aside to set up again, '
                         'or select another file with --config PATH.') from exc


def _read_pointer(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != {'schemaVersion', 'settingsFile'} or
                type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or
                not isinstance(value['settingsFile'], str) or
                not Path(value['settingsFile']).is_absolute()):
            raise ValueError('Expected a version 1 pointer with an absolute settingsFile.')
        return Path(value['settingsFile'])
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Invalid dashboard setup pointer '+str(path)+': '+str(exc)+
                         ' Original bytes were preserved. Move it aside to set up again.') from exc


def resolve_settings_file(root, explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    pointer = Path(root)/'.usage-setup.json'
    if not pointer.exists():
        return default_settings_file().expanduser().resolve()
    settings_file = _read_pointer(pointer)
    if not settings_file.is_file():
        raise ValueError('This dashboard\'s saved setup file is unavailable: '+str(settings_file)+
                         '. Select this machine\'s settings with --config PATH, '
                         'or move .usage-setup.json aside to set up again.')
    return settings_file


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.usage-settings-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_dashboard_pointer(out, settings_file):
    """Remember an isolated setup for generic launchers, outside exported source."""
    path = Path(out)/'.usage-setup.json'
    settings_file = Path(settings_file).expanduser().resolve()
    if load_settings(settings_file) is None:
        raise ValueError('Dashboard setup must reference an existing, validated local settings file.')
    previous = _read_pointer(path) if path.exists() else None
    if previous == settings_file:
        return 'reused'
    _write_json(path, {'schemaVersion': 1, 'settingsFile': str(settings_file)})
    return 'created' if previous is None else 'updated'


def save_interpreter_hint(directory, executable):
    """Let shell launchers reuse private Python before a JSON reader is available."""
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ValueError('The current Python interpreter is unavailable.')
    path = Path(directory)/'.usage-python'
    payload = str(executable)+'\n'
    if path.is_file() and path.read_text(encoding='utf-8') == payload:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.usage-python-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_settings(path, settings):
    """Commit only validated settings; unchanged runs perform no settings write."""
    path = Path(path)
    _validate(settings)
    previous = load_settings(path)
    if previous == settings:
        return 'reused'
    _write_json(path, settings)
    return 'created' if previous is None else 'updated'

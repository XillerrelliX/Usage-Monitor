"""Measure explicitly selected turns with the verified native snapshot adapter.

This module never parses rollout JSON. It hashes fixed raw byte prefixes and
accepts only the native adapter's numeric telemetry and narrow lineage schema.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
COUNTERS = ('inputTokens', 'cachedInputTokens', 'outputTokens',
            'reasoningOutputTokens', 'totalTokens')
MAX_COUNTER = (1 << 64) - 1
SAFE_ID = re.compile(r'^[A-Za-z0-9_./:-]{1,200}$')
SAFE_CODE = re.compile(r'^query_[a-z0-9_]{1,100}$')
SAFE_PATH = re.compile(r'^/?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$')
ALLOWED_KINDS = {'spawn_agent', 'followup_task', 'send_message_to_agent', 'send_input',
                 'send_message', 'collaboration.spawn_agent',
                 'collaboration.followup_task', 'collaboration.send_message'}


class MeterError(ValueError):
    """A static, non-sensitive failure code, safe to show in compact stdout."""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def canonical_id(value):
    try:
        result = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise MeterError('query_invalid_session_id') from None
    if value.lower() != result:
        raise MeterError('query_invalid_session_id')
    return result


def safe_id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise MeterError('query_invalid_identifier')
    return value


def local_path(value):
    path = Path(value).expanduser().absolute()
    if str(path).startswith(('\\\\', '//')):
        raise MeterError('query_local_path_required')
    # Do not follow source/output aliases or Windows reparse points.
    for part in (path, *path.parents):
        if part.exists():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise MeterError('query_reparse_path')
    return path.resolve()


def private(path):
    """New POSIX query objects are private; Windows inherits its selected parent ACL."""
    if os.name != 'nt':
        path = Path(path)
        path.chmod(0o700 if path.is_dir() else 0o600)


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MeterError('query_duplicate_json_field')
            result[key] = value
        return result
    try:
        return json.loads(Path(path).read_bytes(), object_pairs_hook=unique)
    except (OSError, ValueError, UnicodeError):
        raise MeterError('query_invalid_state') from None


def atomic(path, data):
    """Publish atomically; preserve shared parents and inherit Windows permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.query-', dir=path.parent)
    temporary = Path(name)
    try:
        private(temporary)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(directory, *, create=False):
    directory = local_path(directory)
    created = False
    if create:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
            created = True
        except FileExistsError:
            pass
    if not directory.is_dir():
        raise MeterError('query_invalid_state')
    if created:
        private(directory)
    path = directory / '.lock'
    if create:
        try:
            with path.open('xb') as initial:
                private(path)
                initial.write(b'\0')
                initial.flush()
                os.fsync(initial.fileno())
        except FileExistsError:
            pass
    # Existing locks are opened without writing bytes or changing permissions.
    with path.open('r+b') as stream:
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MeterError('query_meter_busy') from None
        try:
            # A Windows byte lock also blocks reads of this byte by another
            # process. Validate only after acquiring it, so contention is busy.
            if stream.read(1) != b'\0':
                raise MeterError('query_invalid_state')
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def hash_prefix(path, size):
    digest, remaining = hashlib.sha256(), size
    with Path(path).open('rb') as stream:
        while remaining:
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise MeterError('query_source_truncated')
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def counters(value):
    if not isinstance(value, dict) or set(value) != set(COUNTERS):
        raise MeterError('query_invalid_counters')
    if any(type(value[key]) is not int or not 0 <= value[key] <= MAX_COUNTER for key in COUNTERS):
        raise MeterError('query_invalid_counters')
    return {key: value[key] for key in COUNTERS}


def timestamp(value, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise MeterError('query_invalid_timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None:
            raise ValueError()
    except ValueError:
        raise MeterError('query_invalid_timestamp') from None
    return value


def validate_snapshot(value, session_id, turn_id, size):
    required = {'schemaVersion', 'sessionId', 'parentSessionId', 'agentPath', 'turnId',
                'turnStatus', 'startedAt', 'completedAt', 'beforeTurn', 'throughTurn',
                'readBytes', 'completeLines', 'trailingPartial', 'delegationCalls'}
    if not isinstance(value, dict) or not required <= set(value):
        raise MeterError('query_invalid_snapshot')
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
        raise MeterError('query_invalid_snapshot')
    if canonical_id(value['sessionId']) != session_id:
        raise MeterError('query_session_mismatch')
    selected = safe_id(value['turnId'])
    if turn_id is not None and selected != turn_id:
        raise MeterError('query_turn_mismatch')
    if value['turnStatus'] not in {'active', 'completed', 'interrupted', 'failed'}:
        raise MeterError('query_invalid_snapshot')
    if type(value['readBytes']) is not int or value['readBytes'] != size:
        raise MeterError('query_read_limit_mismatch')
    if type(value['completeLines']) is not int or value['completeLines'] < 0 or type(value['trailingPartial']) is not bool:
        raise MeterError('query_invalid_snapshot')
    parent = value['parentSessionId']
    if parent is not None:
        parent = canonical_id(parent)
    agent = value['agentPath']
    if agent is not None and (not isinstance(agent, str) or len(agent) > 512 or not SAFE_PATH.fullmatch(agent)):
        raise MeterError('query_invalid_lineage')
    before, through = counters(value['beforeTurn']), counters(value['throughTurn'])
    if any(through[key] < before[key] for key in COUNTERS):
        raise MeterError('query_counter_regression')
    completed = timestamp(value['completedAt'], optional=True)
    if (value['turnStatus'] != 'active') != (completed is not None):
        raise MeterError('query_completion_mismatch')
    calls = value['delegationCalls']
    if not isinstance(calls, list) or len(calls) > 10000:
        raise MeterError('query_invalid_delegations')
    filtered, seen = [], set()
    for call in calls:
        if not isinstance(call, dict) or set(call) != {'id', 'kind'}:
            raise MeterError('query_invalid_delegations')
        ident, kind = safe_id(call['id']), call['kind']
        if ident in seen or kind not in ALLOWED_KINDS:
            raise MeterError('query_invalid_delegations')
        filtered.append({'id': ident, 'kind': kind})
        seen.add(ident)
    return dict(schemaVersion=1, sessionId=session_id, parentSessionId=parent,
                agentPath=agent, turnId=selected, turnStatus=value['turnStatus'],
                startedAt=timestamp(value['startedAt']), completedAt=completed,
                beforeTurn=before, throughTurn=through, readBytes=size,
                completeLines=value['completeLines'], trailingPartial=value['trailingPartial'],
                delegationCalls=filtered)


def query_snapshot(engine, path, session_id, turn_id=None, previous=None, timeout=60):
    """Bracket exactly the same fixed bytes read by the native executable."""
    path = local_path(path)
    if not path.is_file():
        raise MeterError('query_source_unavailable')
    if previous and hash_prefix(path, previous['size']) != previous['sha256']:
        raise MeterError('query_historical_update')
    size = path.stat().st_size
    before = hash_prefix(path, size)
    command = [str(engine), '--query-snapshot', str(path), '--byte-limit', str(size)]
    if turn_id is not None:
        command.extend(['--turn-id', safe_id(turn_id)])
    allowed = {'PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR',
               'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA'}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    options = {}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.run(command, capture_output=True, timeout=timeout, env=environment, **options)
    except (OSError, subprocess.TimeoutExpired):
        raise MeterError('query_engine_failed') from None
    if before != hash_prefix(path, size):
        raise MeterError('query_concurrent_source_update')
    if process.returncode:
        code = None
        try:
            error = json.loads(process.stderr)
            code = error.get('errorCode')
        except (ValueError, TypeError, AttributeError):
            pass
        if isinstance(code, str) and SAFE_CODE.fullmatch(code):
            raise MeterError(code)
        raise MeterError('query_engine_failed')
    if len(process.stdout) > 1024 * 1024:
        raise MeterError('query_invalid_snapshot')
    try:
        value = json.loads(process.stdout)
    except (ValueError, UnicodeError):
        raise MeterError('query_invalid_snapshot') from None
    result = validate_snapshot(value, session_id, turn_id, size)
    return result, {'size': size, 'sha256': before}


def find_source(home, session_id, explicit=None):
    session_id = canonical_id(session_id)
    home = local_path(home)
    if not home.is_dir():
        raise MeterError('query_source_home_unavailable')
    suffix = '-' + session_id + '.jsonl'
    if explicit:
        source = local_path(explicit)
        if not source.is_relative_to(home) or not source.name.lower().endswith(suffix):
            raise MeterError('query_source_scope_mismatch')
        matches = [source] if source.is_file() else []
    else:
        matches = []
        for name in ('sessions', 'archived_sessions'):
            root = home / name
            if root.is_dir():
                for directory, dirs, files in os.walk(root, followlinks=False):
                    # Reject junctions too, before os.walk could descend into them.
                    for name in dirs:
                        local_path(Path(directory) / name)
                    for name in files:
                        if name.lower().endswith(suffix):
                            matches.append(local_path(Path(directory) / name))
    if len(matches) != 1:
        raise MeterError('query_ambiguous_source' if matches else 'query_source_not_found')
    return matches[0]


def delta(turn):
    return {key: turn['snapshot']['throughTurn'][key] - turn['baseline'][key] for key in COUNTERS}


def aggregate(state):
    valid = [turn for turn in state['turns'] if not turn.get('error')]
    subtotal = {key: sum(delta(turn)[key] for turn in valid) for key in COUNTERS} if valid else None
    # Parent scope includes the exact parent turn, never timestamps or forks.
    # Parent lineage and an operator's dispatch claim do not prove which child
    # turn a particular call caused. The native schema currently has no such
    # causal evidence; all manual registrations remain captured-turn claims.
    registrations = {(t['parent']['sessionId'], t['parent']['turnId'], t['parent']['dispatchCallId'])
                     for t in state['turns'] if t.get('parent') and t.get('ownershipVerified') is True}
    unverified = sum(bool(t.get('parent')) and t.get('ownershipVerified') is not True for t in state['turns'])
    unaccounted = sum((t['sessionId'], t['turnId'], call['id']) not in registrations
                      for t in valid for call in t['snapshot']['delegationCalls'])
    completed = sum(t['snapshot']['turnStatus'] == 'completed' for t in valid)
    partial_sources = sum(t['snapshot']['trailingPartial'] for t in valid)
    warnings = (['query_agent_ownership_unverified'] if unverified else []) + (['query_trailing_partial'] if partial_sources else [])
    errors = sorted(set(state.get('errors', []) + [t['error'] for t in state['turns'] if t.get('error')]))
    failures = any(t['snapshot']['turnStatus'] in {'interrupted', 'failed'} for t in valid)
    if failures:
        errors.append('query_turn_not_completed')
    captured_complete = bool(valid) and len(valid) == len(state['turns']) and completed == len(valid) and not partial_sources
    is_complete = captured_complete and not unverified and not unaccounted and not errors
    if is_complete:
        status = 'complete'
    elif not valid:
        status = 'unavailable'
    elif unaccounted or unverified or errors or failures or state.get('terminal'):
        status = 'partial'
    else:
        status = 'provisional'
    return {'schemaVersion': 1, 'queryId': state['queryId'], 'status': status,
            'final': is_complete, 'terminal': bool(state.get('terminal') or is_complete or failures or errors or (captured_complete and unverified)),
            'coverage': state['coverage'], 'createdAt': state['createdAt'], 'updatedAt': now(),
            'cutoff': state['sampledAt'], 'counters': subtotal if is_complete else None,
            'counterText': {k: str(v) for k, v in subtotal.items()} if is_complete else None,
            'subtotal': subtotal, 'subtotalText': {k: str(v) for k, v in subtotal.items()} if subtotal else None,
            'registeredTurns': len(state['turns']), 'completedTurns': completed,
            'unaccountedDispatches': unaccounted, 'errors': sorted(set(errors)),
            'unverifiedRegistrations': unverified, 'ownershipVerified': not unverified and not unaccounted,
            'partialSources': partial_sources, 'warnings': warnings, 'subtotalScope': 'captured-turns',
            'modelCalls': 0, 'networkUsed': False,
            'turns': [{'sessionId': t['sessionId'], 'turnId': t['turnId'],
                       'status': t['snapshot']['turnStatus'], 'error': t.get('error'),
                       'ownershipVerified': not bool(t.get('parent')),
                       'ownershipStatus': 'claimed' if t.get('parent') else 'root-selection',
                       'counters': delta(t) if not t.get('error') else None} for t in state['turns']]}


VIEW = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Usage Monitor · Query</title><style>*{box-sizing:border-box}body{background:#0b1526;color:#ecf6f4;font:17px system-ui;max-width:780px;margin:8vh auto;padding:28px;overflow-wrap:anywhere}h1{font-size:30px}#total{font-size:clamp(26px,6vw,50px);color:#86e8c1;font-weight:650}dt{color:#a9bbce}dd{margin:0 0 16px}small{color:#a9bbce}</style><h1>Consumption of this query</h1><p id="status">Waiting for local measurement…</p><p id="total">—</p><dl id="details"></dl><small id="scope"></small><script>function draw(){const r=window.USAGE_QUERY_METER;if(!r)return;document.getElementById('status').textContent=r.status==='complete'?'Completed':r.status==='pending'?'Waiting for local measurement…':r.status==='provisional'?'Provisional · closing response may be pending':r.status==='partial'?'Partial · complete total unavailable':'Measurement unavailable';const c=r.counterText||r.subtotalText;document.getElementById('total').textContent=c?BigInt(c.totalTokens).toLocaleString('en-US')+' tokens'+(r.final?'':' · captured-turn subtotal'):'—';const d=document.getElementById('details');d.replaceChildren();if(c)for(const [key,label]of [['inputTokens','Input'],['cachedInputTokens','Cached input (included in input)'],['outputTokens','Output'],['reasoningOutputTokens','Reasoning (included in output)']]){const t=document.createElement('dt'),v=document.createElement('dd');t.textContent=label;v.textContent=BigInt(c[key]).toLocaleString('en-US');d.append(t,v)}document.getElementById('scope').textContent=(r.coverage==='checkpoint-only'?'Checkpoint interval · request start excluded':'Selected turn from its beginning')+' · '+r.completedTurns+'/'+r.registeredTurns+' completed turns · cutoff '+r.cutoff+(r.unaccountedDispatches?' · '+r.unaccountedDispatches+' unassigned agent calls':'')+(r.unverifiedRegistrations?' · '+r.unverifiedRegistrations+' claimed agent turns · request ownership unverified':'')+(r.partialSources?' · incomplete source tail':'')+(r.errors.length?' · '+r.errors.join(', '):'')}function poll(){const s=document.createElement('script');s.src='query-usage/latest.js?t='+Date.now();s.onload=()=>{draw();s.remove()};s.onerror=()=>s.remove();document.head.append(s)}poll();setInterval(poll,2000);</script></html>'''


class Meter:
    def __init__(self, out, engine=None):
        self.out = local_path(out)
        self.directory = self.out / 'query-usage'
        self.engine = local_path(engine) if engine else None
        if self.out.is_relative_to(PACKAGE_ROOT):
            raise MeterError('query_keep_state_outside_package')

    def state_path(self, query_id):
        return self.directory / canonical_id(query_id) / 'state.json'

    def load(self, query_id=None):
        if query_id is None:
            pointer = read_json(self.directory / 'active.json')
            query_id = pointer.get('queryId') if isinstance(pointer, dict) else None
        state = read_json(self.state_path(query_id))
        if not isinstance(state, dict) or state.get('schemaVersion') != 1 or state.get('queryId') != query_id or not isinstance(state.get('turns'), list) or not state['turns']:
            raise MeterError('query_invalid_state')
        # Evidence is immutable and chained; its head binds the current state.
        head = state.get('evidenceHead')
        if not isinstance(head, dict) or set(head) != {'id', 'sha256'}:
            raise MeterError('query_invalid_state')
        current, seen, event = head, set(), None
        while current:
            ident = canonical_id(current['id'])
            if ident in seen or len(seen) > 100000:
                raise MeterError('query_invalid_evidence_chain')
            seen.add(ident)
            proof = self.directory / query_id / 'evidence' / (ident + '.json')
            if hashlib.sha256(proof.read_bytes()).hexdigest() != current['sha256']:
                raise MeterError('query_evidence_changed')
            record = read_json(proof)
            if event is None:
                event = record
            current = record.get('previous')
        if event.get('state') != {key: value for key, value in state.items() if key != 'evidenceHead'}:
            raise MeterError('query_state_changed')
        if self.engine and str(self.engine) != state['engine']:
            raise MeterError('query_engine_mismatch')
        return state

    def write(self, path, data):
        atomic(path, data)

    @contextmanager
    def scope_lock(self, create=False):
        with locked(self.directory, create=create):
            yield

    def save(self, state, action, activate=False):
        folder = self.directory / state['queryId']
        evidence = folder / 'evidence'
        try:
            evidence.mkdir(parents=True)
            private(evidence)
        except FileExistsError:
            pass
        event_id = str(uuid.uuid4())
        result = aggregate(state)
        state['terminal'] = result['terminal']
        payload = encode({'schemaVersion': 1, 'id': event_id, 'action': action, 'at': now(),
                          'previous': state.get('evidenceHead'),
                          'state': {key: value for key, value in state.items() if key != 'evidenceHead'}})
        path = evidence / (event_id + '.json')
        with path.open('xb') as stream:
            private(path)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        state['evidenceHead'] = {'id': event_id, 'sha256': hashlib.sha256(payload).hexdigest()}
        result.update(resultPath=str(folder / 'result.json'), viewPath=str(folder / 'index.html'),
                      latestViewPath=str(self.out / 'query-usage.html'))
        self.write(folder / 'state.json', encode(state))
        self.write(folder / 'result.json', encode(result))
        self.write(folder / 'result.js', b'window.USAGE_QUERY_METER=' + encode(result) + b';\n')
        self.write(folder / 'index.html', VIEW.replace('query-usage/latest.js', 'result.js').encode('utf-8'))
        if activate:
            self.write(self.directory / 'active.json', encode({'schemaVersion': 1, 'queryId': state['queryId']}))
        active = read_json(self.directory / 'active.json') if (self.directory / 'active.json').exists() else None
        if not active or active.get('queryId') == state['queryId']:
            self.write(self.directory / 'latest.json', encode(result))
            self.write(self.directory / 'latest.js', b'window.USAGE_QUERY_METER=' + encode(result) + b';\n')
            self.write(self.out / 'query-usage.html', VIEW.encode('utf-8'))
        return result

    def begin(self, home, session_id, turn_id=None, checkpoint_only=False, source=None):
        if not self.engine or not self.engine.is_file():
            raise MeterError('query_engine_required')
        home = local_path(home)
        if self.out.is_relative_to(home):
            raise MeterError('query_keep_state_outside_source')
        with self.scope_lock(create=True):
            if (self.directory / 'active.json').exists() and not self.load().get('terminal'):
                raise MeterError('query_measurement_already_active')
            session_id = canonical_id(session_id)
            path = find_source(home, session_id, source)
            snapshot, proof = query_snapshot(self.engine, path, session_id, turn_id)
            if turn_id is None and snapshot['turnStatus'] != 'active':
                raise MeterError('query_current_turn_unavailable')
            state = {'schemaVersion': 1, 'queryId': str(uuid.uuid4()), 'createdAt': now(),
                     'sampledAt': now(), 'engine': str(self.engine), 'home': str(home),
                     'engineProof': {'size': self.engine.stat().st_size,
                                     'sha256': hash_prefix(self.engine, self.engine.stat().st_size)},
                     'coverage': 'checkpoint-only' if checkpoint_only else 'turn-start',
                     'terminal': False, 'errors': [], 'turns': [self.make_turn(path, snapshot, proof,
                        snapshot['throughTurn'] if checkpoint_only else snapshot['beforeTurn'])]}
            return self.save(state, 'begin', activate=True)

    @staticmethod
    def make_turn(path, snapshot, proof, baseline, parent=None):
        return {'sessionId': snapshot['sessionId'], 'turnId': snapshot['turnId'],
                'source': str(path), 'baseline': baseline, 'baselineProof': proof,
                'proof': proof, 'snapshot': snapshot, 'parent': parent, 'error': None,
                'ownershipVerified': parent is None, 'ownershipStatus': 'claimed' if parent else 'root-selection'}

    def sample_turns(self, state, deadline=None):
        engine = self.engine or local_path(state['engine'])
        proof = state['engineProof']
        try:
            valid_engine = engine.stat().st_size == proof['size'] and hash_prefix(engine, proof['size']) == proof['sha256']
        except OSError:
            valid_engine = False
        if not valid_engine:
            for turn in state['turns']:
                turn['error'] = 'query_engine_changed'
            state['sampledAt'] = now()
            return
        for turn in state['turns']:
            if turn.get('error'):
                continue  # A disproved historical prefix cannot become trusted again.
            try:
                remaining = 60 if deadline is None else min(60, deadline - time.monotonic())
                if remaining <= 0:
                    state['terminal'] = True
                    state['errors'] = sorted(set(state['errors'] + ['query_watch_timeout']))
                    break
                snapshot, proof = query_snapshot(engine, turn['source'], turn['sessionId'], turn['turnId'], turn['proof'], remaining)
                old = turn['snapshot']
                if any(snapshot[key] != old[key] for key in ('sessionId', 'turnId', 'parentSessionId', 'agentPath', 'startedAt', 'beforeTurn')):
                    raise MeterError('query_historical_update')
                if any(snapshot['throughTurn'][key] < old['throughTurn'][key] for key in COUNTERS):
                    raise MeterError('query_counter_regression')
                if old['turnStatus'] != 'active' and snapshot['turnStatus'] != old['turnStatus']:
                    raise MeterError('query_completion_changed')
                if old['turnStatus'] != 'active' and snapshot['completedAt'] != old['completedAt']:
                    raise MeterError('query_completion_changed')
                turn.update(snapshot=snapshot, proof=proof)
            except (MeterError, OSError) as exc:
                turn['error'] = str(exc) if isinstance(exc, MeterError) else 'query_source_unavailable'
        state['sampledAt'] = now()

    def sample(self, query_id=None, stop_reason=None, deadline=None):
        with self.scope_lock():
            state = self.load(query_id)
            self.sample_turns(state, deadline)
            if stop_reason:
                state['terminal'] = True
                state['errors'] = sorted(set(state['errors'] + [stop_reason]))
            return self.save(state, 'sample')

    def status(self, query_id=None):
        with self.scope_lock():
            state = self.load(query_id)
            result = aggregate(state)
            result.update(resultPath=str(self.directory / state['queryId'] / 'result.json'),
                          viewPath=str(self.directory / state['queryId'] / 'index.html'),
                          latestViewPath=str(self.out / 'query-usage.html'))
            # No source access and no watcher launch from status.
            return result

    def register(self, session_id, turn_id, parent_session_id, parent_turn_id, dispatch_call_id, query_id=None, source=None):
        with self.scope_lock():
            state = self.load(query_id)
            if state.get('terminal'):
                raise MeterError('query_measurement_closed')
            session_id, parent_session_id = canonical_id(session_id), canonical_id(parent_session_id)
            turn_id, parent_turn_id, dispatch_call_id = safe_id(turn_id), safe_id(parent_turn_id), safe_id(dispatch_call_id)
            if any(t['sessionId'] == session_id and t['turnId'] == turn_id for t in state['turns']):
                raise MeterError('query_duplicate_turn')
            parent = next((t for t in state['turns'] if t['sessionId'] == parent_session_id and t['turnId'] == parent_turn_id), None)
            if parent is None or parent.get('error'):
                raise MeterError('query_parent_not_registered')
            self.sample_turns(state)
            if parent.get('error'):
                self.save(state, 'registration-failed')
                raise MeterError('query_parent_not_verified')
            if dispatch_call_id not in {c['id'] for c in parent['snapshot']['delegationCalls']}:
                raise MeterError('query_dispatch_not_observed')
            assignment = {'sessionId': parent_session_id, 'turnId': parent_turn_id, 'dispatchCallId': dispatch_call_id}
            if any(t.get('parent') == assignment for t in state['turns']):
                raise MeterError('query_duplicate_dispatch')
            path = find_source(state['home'], session_id, source)
            snapshot, proof = query_snapshot(self.engine or state['engine'], path, session_id, turn_id)
            if session_id == parent_session_id or snapshot['parentSessionId'] != parent_session_id:
                raise MeterError('query_lineage_mismatch')
            if not snapshot['agentPath']:
                raise MeterError('query_lineage_unavailable')
            # Every ancestor is already registered, ending at this query's root.
            ancestor = parent
            seen = {session_id}
            while ancestor:
                if ancestor['sessionId'] in seen:
                    raise MeterError('query_lineage_cycle')
                seen.add(ancestor['sessionId'])
                link = ancestor.get('parent')
                ancestor = next((t for t in state['turns'] if link and t['sessionId'] == link['sessionId'] and t['turnId'] == link['turnId']), None)
            state['turns'].append(self.make_turn(path, snapshot, proof, snapshot['beforeTurn'], assignment))
            return self.save(state, 'register')

    def watch_signature(self, query_id):
        """Only a recheck trigger: stat is never evidence of completion or counters."""
        with self.scope_lock():
            state = self.load(query_id)
            signature = []
            for turn in state['turns']:
                try:
                    info = local_path(turn['source']).stat()
                    revision = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)
                except (OSError, MeterError):
                    revision = None
                signature.append((turn['sessionId'], turn['turnId'], turn.get('error'), revision))
            try:
                info = (self.engine or Path(state['engine'])).stat()
                engine_revision = (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_dev)
            except OSError:
                engine_revision = None
            return engine_revision, tuple(signature)

    def watch(self, query_id=None, timeout=3600, interval=2):
        if not 1 <= timeout <= 3600 or not 0.1 <= interval <= 30:
            raise MeterError('query_invalid_watch_bounds')
        deadline = time.monotonic() + timeout
        target = None
        previous_signature = None
        try:
            while True:
                try:
                    if target is None:
                        target = self.status(query_id)['queryId']
                    signature = self.watch_signature(target)
                    result = self.sample(target, deadline=deadline) if signature != previous_signature else self.status(target)
                    previous_signature = signature
                    if result['final'] or result['terminal'] or result['errors']:
                        return result
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return self.sample(target, 'query_watch_timeout')
                    time.sleep(min(interval, remaining))
                except MeterError as exc:
                    if str(exc) != 'query_meter_busy':
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # The lock may still be held, so no state may be written
                        # without it. Never pretend that this timeout completed.
                        raise MeterError('query_watch_timeout') from None
                    time.sleep(min(interval, remaining))
        except KeyboardInterrupt:
            return self.sample(target, 'query_watch_interrupted')

    def spawn_watch(self, query_id, timeout, interval):
        # -c bridges the bundled Python's isolated ._pth without changing it.
        bridge = 'import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_path(sys.argv.pop(1),run_name="__main__")'
        command = [sys.executable, '-B', '-X', 'utf8', '-c', bridge, str(Path(__file__).parent),
                   str(Path(__file__).resolve()), 'watch', '--out', str(self.out), '--query-id', query_id,
                   '--timeout', str(timeout), '--interval', str(interval)]
        # Preserve the verified Mac launcher's startup isolation in its hidden
        # observer; user sitecustomize/PYTHONPATH must not become executable.
        if sys.platform == 'darwin' and sys.flags.isolated:
            command[1:1] = ['-I', '-S']
        if self.engine:
            command.extend(['--engine', str(self.engine)])
        options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
                   'close_fds': True}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            options['start_new_session'] = True
        try:
            subprocess.Popen(command, **options)
        except OSError:
            raise MeterError('query_watch_launch_failed') from None


def compact(result):
    return {key: value for key, value in result.items() if key != 'turns'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('command', choices=('begin', 'sample', 'status', 'register', 'watch'))
    for name in ('out', 'engine', 'config', 'codex-home', 'source'):
        parser.add_argument('--' + name, type=Path)
    for name in ('thread-id', 'turn-id', 'query-id', 'parent-session-id', 'parent-turn-id', 'dispatch-call-id'):
        parser.add_argument('--' + name)
    coverage = parser.add_mutually_exclusive_group()
    coverage.add_argument('--from-turn-start', action='store_true', help='Default: native-verified beforeTurn baseline.')
    coverage.add_argument('--checkpoint-only', action='store_true', help='Explicit interval starting at this checkpoint.')
    parser.add_argument('--detach', '--watch', dest='detach', action='store_true', help='Start a bounded hidden local watcher after begin, or detach watch.')
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.timeout <= 3600 or not 0.1 <= args.interval <= 30:
            raise MeterError('query_invalid_watch_bounds')
        from local_setup import load_settings, default_settings_file
        config = local_path(args.config) if args.config else default_settings_file().parent.parent / 'usage-monitor' / 'settings.json'
        saved = load_settings(config) or {}
        out = args.out or saved.get('outputDirectory') or config.parent / 'dashboard'
        engine = args.engine
        if not engine:
            from windows_package import verify_bundle
            from prebuilt_runtime import resolve_prebuilt
            verify_bundle(PACKAGE_ROOT)
            engine = resolve_prebuilt(PACKAGE_ROOT, PACKAGE_ROOT / 'runtime')['engine']
        meter = Meter(out, engine)
        if args.command == 'begin':
            home = args.codex_home or saved.get('offline', {}).get('codexHome') or os.environ.get('CODEX_HOME') or Path.home() / '.codex'
            thread = args.thread_id or os.environ.get('CODEX_THREAD_ID')
            if not home or not thread:
                raise MeterError('query_explicit_home_and_thread_required')
            result = meter.begin(home, thread, args.turn_id, args.checkpoint_only, args.source)
        elif args.command == 'register':
            if not all((args.thread_id, args.turn_id, args.parent_session_id, args.parent_turn_id, args.dispatch_call_id)):
                raise MeterError('query_registration_fields_required')
            result = meter.register(args.thread_id, args.turn_id, args.parent_session_id, args.parent_turn_id,
                                    args.dispatch_call_id, args.query_id, args.source)
        elif args.command == 'watch' and not args.detach:
            result = meter.watch(args.query_id, args.timeout, args.interval)
        elif args.command == 'sample':
            result = meter.sample(args.query_id)
        else:
            result = meter.status(args.query_id)
        if args.detach and args.command in {'begin', 'watch'} and not result['terminal']:
            meter.spawn_watch(result['queryId'], args.timeout, args.interval)
        print(json.dumps(compact(result), ensure_ascii=True, separators=(',', ':')))
        return 0 if result['status'] == 'complete' else 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        code = str(exc) if isinstance(exc, MeterError) else 'query_local_operation_failed'
        print(json.dumps({'schemaVersion': 1, 'status': 'unavailable', 'final': False,
                          'errorCode': code, 'modelCalls': 0, 'networkUsed': False}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

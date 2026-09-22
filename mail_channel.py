"""Durable local mailbox for the existing Claude desktop session.

This module transports messages only; it does not wake or invoke a model.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from receiver_order import register_and_sort

BASE = Path(__file__).resolve().parent
MAIL = BASE / 'mail'
RECEIVER_STATE = BASE / 'state' / 'receiver-order'
CLAIMS = '.claims.json'


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked():
    for folder in ('inbox', 'outbox', 'archive'):
        (MAIL / folder).mkdir(parents=True, exist_ok=True)
    with (MAIL / '.transport.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def valid_name(name):
    if (not name or Path(name).name != name
            or Path(name).suffix not in {'.md', '.json'}):
        raise ValueError('Expected a mailbox Markdown/JSON filename, not a path')


def safe_file(folder, name):
    valid_name(name)
    path = MAIL / folder / name
    if path.is_symlink():
        raise ValueError('Mailbox symlinks are not supported')
    return path


def notify_claude(subject, body, request_id, needs_reply=True, reply_to='-'):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', request_id):
        raise ValueError('request_id must contain 1–80 letters, digits, underscores or hyphens')
    if not subject.strip() or '\n' in subject or '\r' in subject or len(subject) > 300:
        raise ValueError('Subject must be a single line, maximum 300 characters')
    if len(body) > 100000:
        raise ValueError('Put large materials in an attachment and include its absolute path')
    if reply_to != '-':
        valid_name(reply_to)
    if re.search(r'github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}|-----BEGIN .*PRIVATE KEY-----', subject + body):
        raise ValueError('Do not put access tokens or private keys in mailbox messages')
    body = body.replace('\r\n', '\n').replace('\r', '\n')
    digest = hashlib.sha256(json.dumps([subject, body, needs_reply, reply_to], ensure_ascii=False).encode()).hexdigest()
    with locked():
        index_path = MAIL / '.outgoing-index.json'
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        if request_id in index:
            record = index[request_id]
            if record['digest'] != digest:
                raise ValueError('request_id already used with another message')
        else:
            now = datetime.now(timezone.utc)
            slug = re.sub(r'[^a-z0-9]+', '-', subject.lower()).strip('-')[:32] or 'message'
            name = f'{now:%Y-%m-%dT%H-%M-%SZ}--{slug}--{request_id}.md'
            # JSON strings are valid YAML scalars; this prevents header injection.
            header = {'id': request_id, 'from': 'codex', 'to': 'claude', 'subject': subject,
                      'created_utc': now.isoformat(timespec='seconds').replace('+00:00', 'Z'),
                      'reply_to': reply_to, 'needs_reply': bool(needs_reply)}
            content = '---\n' + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in header.items()) + '\n---\n\n' + body + '\n'
            record = {'name': name, 'digest': digest, 'content': content}
            index[request_id] = record
            # Commit the stable filename before delivery. An identical retry can
            # repair an interrupted write without creating a duplicate message.
            atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2))
        incoming = safe_file('inbox', record['name'])
        archived = safe_file('archive', record['name'])
        if not incoming.exists() and not archived.exists():
            atomic_write(incoming, record['content'])
        return {'name': record['name'], 'path': str(archived if archived.exists() else incoming),
                'archived': archived.exists(), 'delivery': 'file-written; model wakeup is separate'}


def read_replies(limit=20, max_chars=100000):
    """Read complete messages within a total budget; never archive implicitly."""
    with locked():
        result = []
        used = 0
        paths = [safe_file('outbox', path.name)
                 for path in (MAIL / 'outbox').glob('*.md')]
        records = [{'name': path.name, 'sha256': _digest(path)} for path in paths]
        ordered = register_and_sort(RECEIVER_STATE, 'outbox', records, time.time())
        files = [safe_file('outbox', item['name']) for item in ordered]
        for path in files[:max(1, min(limit, 100))]:
            path = safe_file('outbox', path.name)
            body = path.read_text(encoding='utf-8')
            if used + len(body) > max(1, min(max_chars, 200000)):
                return {'messages': result, 'remaining': len(files)-len(result), 'next_name': path.name}
            result.append({'name': path.name, 'text': body,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            used += len(body)
        return {'messages': result, 'remaining': len(files)-len(result)}


def read_reply(name, offset=0, max_chars=20000):
    with locked():
        path = safe_file('outbox', name)
        raw = path.read_bytes()
        register_and_sort(RECEIVER_STATE, 'outbox',
                          [{'name': name, 'sha256': hashlib.sha256(raw).hexdigest()}],
                          time.time())
        text = raw.decode('utf-8')
        offset = max(0, offset)
        end = offset + max(1, min(max_chars, 100000))
        return {'name': name, 'text': text[offset:end], 'sha256': hashlib.sha256(raw).hexdigest(),
                'total_chars': len(text), 'next_offset': end if end < len(text) else None}


def archive_reply(name, expected_sha256):
    with locked():
        source = safe_file('outbox', name)
        target = safe_file('archive', name)
        current = source if source.exists() else target
        if hashlib.sha256(current.read_bytes()).hexdigest() != expected_sha256:
            raise ValueError('Message changed since reading; read it again before acknowledging')
        if source.exists():
            if target.exists():
                raise ValueError('Archive filename collision; refusing to overwrite')
            os.replace(source, target)
        return {'name': name, 'archived': True, 'path': str(target)}


def _digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _claim_key(name, sha256):
    return f'{name}:{sha256}'


def claim_reply(name, expected_sha256, actor, ttl_seconds=1800, now=None):
    """Atomically claim and read one exact letter version.

    Both interactive Codex and the background Mailroom use this API.  Reading
    without a claim is still available for inspection, but it must not be used
    to produce an answer once Mailroom is enabled.
    """
    if not actor or len(actor) > 120:
        raise ValueError('actor is required and must be at most 120 characters')
    now = time.time() if now is None else float(now)
    ttl_seconds = max(30, min(int(ttl_seconds), 7200))
    with locked():
        source = safe_file('outbox', name)
        if not source.exists():
            raise FileNotFoundError(name)
        current_sha = _digest(source)
        if current_sha != expected_sha256:
            raise ValueError('Message changed before claim; read the new version')
        claims_path = MAIL / CLAIMS
        claims = json.loads(claims_path.read_text()) if claims_path.exists() else {}
        key = _claim_key(name, expected_sha256)
        existing = claims.get(key)
        if existing and existing.get('expires_at', 0) > now:
            return {'claimed': False, 'name': name, 'sha256': expected_sha256,
                    'actor': existing['actor'], 'expires_at': existing['expires_at']}
        token = str(uuid.uuid4())
        claims[key] = {'name': name, 'sha256': expected_sha256, 'actor': actor,
                       'token': token, 'state': 'observed', 'claimed_at': now,
                       'expires_at': now + ttl_seconds}
        atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
        return {'claimed': True, 'name': name, 'sha256': expected_sha256,
                'actor': actor, 'token': token, 'state': 'observed',
                'expires_at': now + ttl_seconds, 'text': source.read_text(encoding='utf-8')}


def complete_claim(name, expected_sha256, token, *, disposition, outcome_ref,
                   authority_ids=None, now=None):
    """Commit a durable outcome, receipt and archive under one transport lock."""
    allowed = {'replied', 'escalated', 'duplicate', 'superseded'}
    if disposition not in allowed:
        raise ValueError(f'Unknown disposition: {disposition}')
    if not outcome_ref:
        raise ValueError('outcome_ref is required before a claim can be completed')
    authority_ids = list(authority_ids or [])
    if any(not re.fullmatch(r'(?:D|F)(?:-CODEX)?-[0-9]{4}', x) for x in authority_ids):
        raise ValueError('authority_ids must contain canonical D-*/F-* identifiers')
    now = time.time() if now is None else float(now)
    with locked():
        claims_path = MAIL / CLAIMS
        claims = json.loads(claims_path.read_text()) if claims_path.exists() else {}
        key = _claim_key(name, expected_sha256)
        claim = claims.get(key)
        if not claim or claim.get('token') != token:
            raise ValueError('Claim token is missing or belongs to another reader')
        source = safe_file('outbox', name)
        target = safe_file('archive', name)
        current = source if source.exists() else target
        if not current.exists() or _digest(current) != expected_sha256:
            raise ValueError('Message changed or disappeared before completion')
        receipts = MAIL / 'receipts'
        receipts.mkdir(parents=True, exist_ok=True)
        receipt_name = hashlib.sha256(key.encode()).hexdigest()[:24] + '.json'
        receipt_path = receipts / receipt_name
        receipt = {'schema_version': 1, 'name': name, 'sha256': expected_sha256,
                   'actor': claim['actor'], 'state': 'outcome_durable',
                   'disposition': disposition, 'outcome_ref': outcome_ref,
                   'authority_ids': authority_ids, 'observed_at': claim['claimed_at'],
                   'completed_at': now}
        atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
        if source.exists():
            if target.exists():
                raise ValueError('Archive filename collision; refusing to overwrite')
            os.replace(source, target)
        receipt['state'] = 'archived'
        receipt['archived_at'] = now
        atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
        claims.pop(key, None)
        atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
        return {'name': name, 'archived': True, 'disposition': disposition,
                'receipt': str(receipt_path), 'outcome_ref': outcome_ref}


def release_claim(name, expected_sha256, token):
    """Release a claim after a known pre-output failure; never releases another actor."""
    with locked():
        claims_path = MAIL / CLAIMS
        claims = json.loads(claims_path.read_text()) if claims_path.exists() else {}
        key = _claim_key(name, expected_sha256)
        claim = claims.get(key)
        if not claim or claim.get('token') != token:
            return {'released': False}
        claims.pop(key)
        atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
        return {'released': True}


def _состояние_доставки(name, sha256):
    """Что известно о доставке этой точной версии, или None, если учёта нет.

    Учёт ведёт owner_watch рядом с почтой. Если файла учёта нет вовсе —
    возвращаем None и НЕ запрещаем подтверждение: так ведут себя сценарии без
    mailroom, и ломать их здесь нечем. Это названная граница, не забывчивость.
    """
    учёт = MAIL.parent / 'state' / 'mailroom' / 'owner-delivery.json'
    if not учёт.exists():
        return None
    try:
        доставки = json.loads(учёт.read_text()).get('deliveries', {})
    except (ValueError, OSError):
        return None
    return доставки.get('%s:%s' % (name, sha256))


def acknowledge_owner_escalation(name, expected_sha256):
    """Acknowledge that the interactive task presented one escalation to the owner.

    Подтвердить можно только то, что доставлено. 21.09.2026 эта функция унесла
    в архив семь эскалаций, которых доставщик даже не отправлял: нить владельца
    была в архиве, записи стояли в `pending`, а подтверждение о доставке ничего
    не знало. Кандидаты берутся из очереди, файлов там не осталось — и семь
    сообщений навсегда замерли в состоянии, неотличимом от потерянных.
    """
    with locked():
        (MAIL / 'owner-archive').mkdir(parents=True, exist_ok=True)
        source = safe_file('owner-outbox', name)
        target = safe_file('owner-archive', name)
        current = source if source.exists() else target
        if not current.exists() or _digest(current) != expected_sha256:
            raise ValueError('Escalation changed or disappeared before acknowledgment')
        учёт = _состояние_доставки(name, expected_sha256)
        if учёт is not None and учёт.get('state') not in ('delivered', 'overdue',
                                                          'acknowledged'):
            raise ValueError(
                'Эскалацию %s никто не доставлял (состояние %r) — подтверждать '
                'нечего. Письмо остаётся в очереди.' % (name, учёт.get('state')))
        if source.exists():
            if target.exists():
                raise ValueError('Owner archive filename collision')
            os.replace(source, target)
        return {'name': name, 'acknowledged': True, 'path': str(target)}

"""Durable local mailbox for the existing Claude desktop session.

This module transports messages only; it does not wake or invoke a model.

Оба ящика обслуживаются одной транзакционной логикой: ``outbox`` — письма к
Codex, ``inbox`` — письма к Claude.  Ящик передаётся параметром, умолчание
``outbox`` дословно сохраняет прежний путь: ключи захвата, имена квитанций и
папка архива для него не изменились, старые данные читаются без миграции.
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

MAILBOXES = ('outbox', 'inbox')
# Куда ящик архивирует. Писать можно только в свою папку.
ARCHIVE_WRITE = {'outbox': 'archive', 'inbox': 'inbox-archive'}
# Откуда ящик читает архив. До 22.09.2026 архив был общий: старый адрес
# остаётся читаемым, но новым письмам inbox он больше не адресуется.
ARCHIVE_READ = {'outbox': ('archive',), 'inbox': ('inbox-archive', 'archive')}
# Учёт исходящих ведётся по ящику доставки; имя для inbox прежнее.
OUTGOING_INDEX = {'inbox': '.outgoing-index.json', 'outbox': '.outgoing-index-outbox.json'}
PREPARED = 'prepared'


def _mailbox(mailbox):
    """Проверить ящик ДО любого обращения к диску и вернуть его."""
    if mailbox not in MAILBOXES:
        raise ValueError('Unknown mailbox: %r; expected one of %s'
                         % (mailbox, ', '.join(MAILBOXES)))
    return mailbox


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


def create_exclusive(path, text):
    """Создать файл один раз. Существующий не перезаписывается никогда."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return True


@contextmanager
def locked():
    for folder in ('inbox', 'outbox', 'archive', 'inbox-archive'):
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


def _archive_target(mailbox, name):
    """Единственная папка, куда этот ящик архивирует."""
    return safe_file(ARCHIVE_WRITE[mailbox], name)


def _archived(mailbox, name, expected_sha256=None):
    """Путь, по которому это письмо уже лежит в архиве, или None.

    В своей папке имя решает: оно принадлежит этому ящику. В общей папке,
    куда архивировали до 22.09.2026, одно и то же имя может принадлежать
    письму другого ящика — поэтому там признаком служит содержимое, а не имя.
    Письмо здесь всюду опознаётся парой (имя, sha256); архив не исключение.
    """
    folders = ARCHIVE_READ[mailbox]
    primary = safe_file(folders[0], name)
    if primary.exists():
        return primary
    for folder in folders[1:]:
        path = safe_file(folder, name)
        if (path.exists() and expected_sha256 is not None
                and _digest(path) == expected_sha256):
            return path
    return None


def _deliver(mailbox, sender, recipient, subject, body, request_id, needs_reply, reply_to):
    _mailbox(mailbox)
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
        index_path = MAIL / OUTGOING_INDEX[mailbox]
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
            header = {'id': request_id, 'from': sender, 'to': recipient, 'subject': subject,
                      'created_utc': now.isoformat(timespec='seconds').replace('+00:00', 'Z'),
                      'reply_to': reply_to, 'needs_reply': bool(needs_reply)}
            content = '---\n' + '\n'.join(f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in header.items()) + '\n---\n\n' + body + '\n'
            record = {'name': name, 'digest': digest, 'content': content}
            index[request_id] = record
            # Commit the stable filename before delivery. An identical retry can
            # repair an interrupted write without creating a duplicate message.
            atomic_write(index_path, json.dumps(index, ensure_ascii=False, indent=2))
        delivered = safe_file(mailbox, record['name'])
        archived = _archived(mailbox, record['name'],
                             hashlib.sha256(record['content'].encode()).hexdigest())
        if not delivered.exists() and archived is None:
            atomic_write(delivered, record['content'])
        return {'name': record['name'], 'path': str(archived if archived else delivered),
                'archived': archived is not None,
                'delivery': 'file-written; model wakeup is separate'}


def notify_claude(subject, body, request_id, needs_reply=True, reply_to='-'):
    return _deliver('inbox', 'codex', 'claude', subject, body, request_id,
                    needs_reply, reply_to)


def notify_codex(subject, body, request_id, needs_reply=True, reply_to='-'):
    """Зеркальная доставка: ответ от Claude к Codex ложится в outbox."""
    return _deliver('outbox', 'claude', 'codex', subject, body, request_id,
                    needs_reply, reply_to)


def local_delivery_record(name, expected_sha256, request_id, *, mailbox='outbox'):
    """Запись НАШЕГО журнала исходящих об этом точном письме, или None.

    Заявление письма о себе ничего не доказывает: признак автомата в
    идентификаторе может поставить любой доверенный отправитель, и письмо,
    которое ждало ответа, молча уедет в архив как «автоответ». Доказывает
    только след транспорта — журнал того обработчика, который письмо создал,
    и совпадение имени и полного SHA содержимого.
    """
    _mailbox(mailbox)
    if not request_id or not name:
        return None
    index_path = MAIL / OUTGOING_INDEX[mailbox]
    if not index_path.exists():
        return None
    try:
        index = json.loads(index_path.read_text())
    except (ValueError, OSError):
        return None
    record = index.get(request_id)
    if not isinstance(record, dict) or record.get('name') != name:
        return None
    содержимое = record.get('content')
    if not isinstance(содержимое, str):
        return None
    if hashlib.sha256(содержимое.encode('utf-8')).hexdigest() != expected_sha256:
        return None
    return record


def read_replies(limit=20, max_chars=100000, *, mailbox='outbox'):
    """Read complete messages within a total budget; never archive implicitly."""
    _mailbox(mailbox)
    with locked():
        result = []
        used = 0
        paths = [safe_file(mailbox, path.name)
                 for path in (MAIL / mailbox).glob('*.md')]
        records = [{'name': path.name, 'sha256': _digest(path)} for path in paths]
        ordered = register_and_sort(RECEIVER_STATE, mailbox, records, time.time())
        files = [safe_file(mailbox, item['name']) for item in ordered]
        for path in files[:max(1, min(limit, 100))]:
            path = safe_file(mailbox, path.name)
            body = path.read_text(encoding='utf-8')
            if used + len(body) > max(1, min(max_chars, 200000)):
                return {'messages': result, 'remaining': len(files)-len(result), 'next_name': path.name}
            result.append({'name': path.name, 'text': body,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            used += len(body)
        return {'messages': result, 'remaining': len(files)-len(result)}


def list_headers(limit=200, head_bytes=2048, *, mailbox='outbox'):
    """Имена в порядке приёмника вместе с шапкой, без чтения тел.

    Нужно, чтобы выбрать письмо по признаку шапки, не втягивая в бюджет
    чтения все тела: иначе письмо своей нити может навсегда остаться за
    пределом, потому что чужие письма впереди оказались большими.
    """
    _mailbox(mailbox)
    with locked():
        paths = [safe_file(mailbox, p.name) for p in (MAIL / mailbox).glob('*.md')]
        records = [{'name': p.name, 'sha256': _digest(p)} for p in paths]
        ordered = register_and_sort(RECEIVER_STATE, mailbox, records, time.time())
        out = []
        for item in ordered[:max(1, min(limit, 1000))]:
            path = safe_file(mailbox, item['name'])
            head = path.read_bytes()[:max(256, min(head_bytes, 65536))]
            out.append({'name': item['name'], 'sha256': item['sha256'],
                        'head': head.decode('utf-8', 'replace')})
        return out


def read_reply(name, offset=0, max_chars=20000, *, mailbox='outbox'):
    _mailbox(mailbox)
    with locked():
        path = safe_file(mailbox, name)
        raw = path.read_bytes()
        register_and_sort(RECEIVER_STATE, mailbox,
                          [{'name': name, 'sha256': hashlib.sha256(raw).hexdigest()}],
                          time.time())
        text = raw.decode('utf-8')
        offset = max(0, offset)
        end = offset + max(1, min(max_chars, 100000))
        return {'name': name, 'text': text[offset:end], 'sha256': hashlib.sha256(raw).hexdigest(),
                'total_chars': len(text), 'next_offset': end if end < len(text) else None}


def archive_reply(name, expected_sha256, *, mailbox='outbox'):
    _mailbox(mailbox)
    with locked():
        source = safe_file(mailbox, name)
        target = _archive_target(mailbox, name)
        archived = _archived(mailbox, name, expected_sha256)
        current = source if source.exists() else archived
        if current is None:
            raise FileNotFoundError(name)
        if _digest(current) != expected_sha256:
            raise ValueError('Message changed since reading; read it again before acknowledging')
        if source.exists():
            if target.exists():
                raise ValueError('Archive filename collision; refusing to overwrite')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            return {'name': name, 'archived': True, 'path': str(target)}
        return {'name': name, 'archived': True, 'path': str(archived)}


def _digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _claim_key(mailbox, name, sha256):
    """Ключ захвата. Для outbox — прежнее представление, без префикса ящика.

    Совместимость здесь не удобство, а условие: старые захваты, квитанции и
    архивные записи outbox должны читаться без миграции, а имя квитанции
    считается от этого самого ключа.
    """
    _mailbox(mailbox)
    if mailbox == 'outbox':
        return f'{name}:{sha256}'
    return f'{mailbox}:{name}:{sha256}'


def claim_reply(name, expected_sha256, actor, ttl_seconds=1800, now=None, *, mailbox='outbox'):
    """Atomically claim and read one exact letter version.

    Both interactive Codex and the background Mailroom use this API.  Reading
    without a claim is still available for inspection, but it must not be used
    to produce an answer once Mailroom is enabled.
    """
    _mailbox(mailbox)
    if not actor or len(actor) > 120:
        raise ValueError('actor is required and must be at most 120 characters')
    now = time.time() if now is None else float(now)
    ttl_seconds = max(30, min(int(ttl_seconds), 7200))
    with locked():
        source = safe_file(mailbox, name)
        if not source.exists():
            raise FileNotFoundError(name)
        current_sha = _digest(source)
        if current_sha != expected_sha256:
            raise ValueError('Message changed before claim; read the new version')
        claims_path = MAIL / CLAIMS
        claims = json.loads(claims_path.read_text()) if claims_path.exists() else {}
        key = _claim_key(mailbox, name, expected_sha256)
        existing = claims.get(key)
        if existing and existing.get('expires_at', 0) > now:
            return {'claimed': False, 'name': name, 'sha256': expected_sha256,
                    'mailbox': mailbox, 'actor': existing['actor'],
                    'expires_at': existing['expires_at']}
        token = str(uuid.uuid4())
        claims[key] = {'name': name, 'sha256': expected_sha256, 'mailbox': mailbox,
                       'actor': actor, 'token': token, 'state': 'observed',
                       'claimed_at': now, 'expires_at': now + ttl_seconds}
        atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
        return {'claimed': True, 'name': name, 'sha256': expected_sha256,
                'mailbox': mailbox, 'actor': actor, 'token': token, 'state': 'observed',
                'expires_at': now + ttl_seconds, 'text': source.read_text(encoding='utf-8')}


def _prepared_path(key):
    """Имя подготовленного ответа — полный SHA-256 ключа исходного письма.

    Полный, а не усечённый: 16 hex оставляют 64 бита, и цена ошибки здесь —
    чужой ответ, зафиксированный как свой.
    """
    return MAIL / PREPARED / (hashlib.sha256(key.encode()).hexdigest() + '.json')


def _outcome_digest(record):
    material = {k: record.get(k) for k in
                ('source_key', 'mailbox', 'name', 'source_sha256', 'payload')}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _verify_prepared(record, key, mailbox, name, expected_sha256):
    if (record.get('source_key') != key or record.get('mailbox') != mailbox
            or record.get('name') != name
            or record.get('source_sha256') != expected_sha256):
        raise ValueError('Prepared outcome belongs to another letter version; refusing to reuse it')
    if record.get('outcome_sha256') != _outcome_digest(record):
        raise ValueError('Prepared outcome failed its content check; refusing to reuse it')
    return record


def load_prepared_outcome(name, expected_sha256, *, mailbox='outbox'):
    """Вернуть уже подготовленный ответ на это точное письмо, или None.

    Расхождение метаданных или содержимого — отказ, а не молчаливый успех.
    """
    _mailbox(mailbox)
    key = _claim_key(mailbox, name, expected_sha256)
    path = _prepared_path(key)
    if not path.exists():
        return None
    return _verify_prepared(json.loads(path.read_text()), key, mailbox, name, expected_sha256)


def prepare_outcome(name, expected_sha256, token, *, payload, mailbox='outbox', now=None):
    """Зафиксировать ответ под общим замком, огородив его действующим захватом.

    Ответ создаётся ровно один раз и никогда не перезаписывается. Повтор
    сверяет уже созданный по полному ключу исходного письма, метаданным и SHA
    содержимого и возвращает СОХРАНЁННЫЙ ответ: работник, потерявший захват,
    ни записать, ни подменить его не может.
    """
    _mailbox(mailbox)
    if payload is None:
        raise ValueError('payload is required before an outcome can be prepared')
    now = time.time() if now is None else float(now)
    with locked():
        claims_path = MAIL / CLAIMS
        claims = json.loads(claims_path.read_text()) if claims_path.exists() else {}
        key = _claim_key(mailbox, name, expected_sha256)
        claim = claims.get(key)
        if not claim or claim.get('token') != token:
            raise ValueError('Claim token is missing or belongs to another reader')
        if float(claim.get('expires_at', 0)) <= now:
            raise ValueError('Claim expired before the answer was prepared; another reader may have taken over')
        source = safe_file(mailbox, name)
        current = source if source.exists() else _archived(mailbox, name, expected_sha256)
        if current is None or _digest(current) != expected_sha256:
            raise ValueError('Message changed or disappeared before the answer was prepared')
        path = _prepared_path(key)
        record = {'schema_version': 1, 'source_key': key, 'mailbox': mailbox,
                  'name': name, 'source_sha256': expected_sha256,
                  'payload': payload, 'prepared_at': now}
        record['outcome_sha256'] = _outcome_digest(record)
        reused = not create_exclusive(path, json.dumps(record, ensure_ascii=False, indent=2))
        if reused:
            record = _verify_prepared(json.loads(path.read_text()), key, mailbox,
                                      name, expected_sha256)
        claim['prepared_ref'] = str(path)
        claim['prepared_sha256'] = record['outcome_sha256']
        claim['state'] = 'outcome_prepared'
        atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
        return {'name': name, 'mailbox': mailbox, 'path': str(path), 'reused': reused,
                'outcome_sha256': record['outcome_sha256'], 'payload': record['payload']}


def receipt_path(name, expected_sha256, *, mailbox='outbox'):
    """Где лежит (или лежала бы) квитанция об этом точном письме."""
    ключ = _claim_key(mailbox, name, expected_sha256)
    return MAIL / 'receipts' / (hashlib.sha256(ключ.encode()).hexdigest()[:24] + '.json')


def complete_claim(name, expected_sha256, token, *, disposition, outcome_ref,
                   authority_ids=None, now=None, mailbox='outbox', archive=True):
    """Commit a durable outcome, receipt and archive under one transport lock.

    ``archive=False`` фиксирует исход, НЕ вынося письмо из ящика. Так
    обрабатывается чужая нить в общем ящике: письмо остаётся там, где его
    ищет настоящий адресат, а мы помним, что уже его разобрали. Квитанция
    по-прежнему пишется под тем же замком — теряется не она, а только право
    трогать чужое письмо."""
    _mailbox(mailbox)
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
        key = _claim_key(mailbox, name, expected_sha256)
        claim = claims.get(key)
        if not claim or claim.get('token') != token:
            raise ValueError('Claim token is missing or belongs to another reader')
        if float(claim.get('expires_at', 0)) <= now:
            raise ValueError('Claim expired before completion; another reader may have taken over')
        # Подготовленный ответ и место, где исход материализовался, — разные
        # вещи: первый огорожен захватом, второй указывает на письмо или
        # эскалацию. Для зеркала первый обязателен, иначе фиксировать нечего.
        if mailbox != 'outbox' and not claim.get('prepared_ref'):
            raise ValueError('No prepared outcome is fenced under this claim; prepare it first')
        source = safe_file(mailbox, name)
        target = _archive_target(mailbox, name)
        archived = _archived(mailbox, name, expected_sha256)
        current = source if source.exists() else archived
        if current is None or _digest(current) != expected_sha256:
            raise ValueError('Message changed or disappeared before completion')
        receipts = MAIL / 'receipts'
        receipts.mkdir(parents=True, exist_ok=True)
        receipt_name = hashlib.sha256(key.encode()).hexdigest()[:24] + '.json'
        receipt_path = receipts / receipt_name   # локальный путь, не функция выше
        receipt = {'schema_version': 1, 'name': name, 'sha256': expected_sha256,
                   'source_mailbox': mailbox, 'actor': claim['actor'],
                   'state': 'outcome_durable', 'disposition': disposition,
                   'outcome_ref': outcome_ref, 'prepared_ref': claim.get('prepared_ref'),
                   'prepared_sha256': claim.get('prepared_sha256'),
                   'authority_ids': authority_ids, 'observed_at': claim['claimed_at'],
                   'completed_at': now}
        atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
        if not archive:
            receipt['state'] = 'outcome_durable_in_place'
            receipt['left_in_mailbox'] = True
            atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
            claims.pop(key, None)
            atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
            return {'name': name, 'mailbox': mailbox, 'archived': False,
                    'disposition': disposition, 'receipt': str(receipt_path),
                    'outcome_ref': outcome_ref}
        if source.exists():
            if target.exists():
                raise ValueError('Archive filename collision; refusing to overwrite')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        receipt['state'] = 'archived'
        receipt['archived_at'] = now
        atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
        claims.pop(key, None)
        atomic_write(claims_path, json.dumps(claims, ensure_ascii=False, indent=2))
        return {'name': name, 'mailbox': mailbox, 'archived': True,
                'disposition': disposition, 'receipt': str(receipt_path),
                'outcome_ref': outcome_ref}


def release_claim(name, expected_sha256, token, *, mailbox='outbox'):
    """Release a claim after a known pre-output failure; never releases another actor."""
    _mailbox(mailbox)
    with locked():
        claims_path = MAIL / CLAIMS
        claims = json.loads(claims_path.read_text()) if claims_path.exists() else {}
        key = _claim_key(mailbox, name, expected_sha256)
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

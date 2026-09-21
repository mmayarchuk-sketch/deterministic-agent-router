"""Deliver durable Mailroom escalations to the current interactive Codex task."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from mail_channel import atomic_write
from outbox_watch import thread_state
from receiver_order import register_and_sort


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(cfg, runner=subprocess.run, now=None):
    now = time.time() if now is None else float(now)
    folder = Path(cfg['owner_outbox'])
    archive = Path(cfg['owner_archive'])
    folder.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    state_path = Path(cfg['state_directory']) / 'owner-delivery.json'
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = json.loads(state_path.read_text()) if state_path.exists() else {'deliveries': {}}
    deliveries = state['deliveries']

    # Confirm user-facing acknowledgment by the exact archived version.
    for key, item in deliveries.items():
        if item['state'] != 'delivered':
            continue
        archived = archive / item['name']
        if archived.exists() and digest(archived) == item['sha256']:
            item.update(state='acknowledged', acknowledged_at=now)

    raw_candidates = []
    for path in folder.glob('*.json'):
        if path.name.startswith('.') or now - path.stat().st_mtime < 5:
            continue
        sha = digest(path)
        raw_candidates.append({'name': path.name, 'sha256': sha})

    legacy = [{**item, 'observed_at': item.get('observed_at', now),
               'legacy_batch': key}
              for key, item in deliveries.items()
              if item.get('name') and item.get('sha256')]
    order_state = Path(cfg['state_directory']).parent / 'receiver-order'
    registered = register_and_sort(order_state, 'owner-outbox',
                                   raw_candidates, now, legacy)
    candidates = []
    for registered_item in registered:
        path = folder / registered_item['name']
        sha = registered_item['sha256']
        key = f'{path.name}:{sha}'
        item = deliveries.get(key)
        if item and item['state'] in ('delivered', 'acknowledged'):
            if item['state'] == 'delivered' and now - item['delivered_at'] > cfg.get('ack_deadline_seconds', 7200):
                item['state'] = 'overdue'
                item['overdue_at'] = now
            continue
        candidates.append((path, sha, key))

    if not candidates:
        atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
        return {'status': 'idle', 'overdue': [x['name'] for x in deliveries.values()
                                               if x['state'] == 'overdue']}

    target = thread_state(cfg)
    path, sha, key = candidates[0]
    if not target['ready']:
        deliveries[key] = {'name': path.name, 'sha256': sha, 'state': 'pending',
                           'reason': target['state'], 'observed_at': now}
        atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
        return {'status': 'thread_unavailable', 'reason': target['state'], 'name': path.name}

    payload = json.loads(path.read_text())
    message = (
        'Эскалация Mailroom для Максима. Это уже классифицированный результат, '
        'но не новое полномочие. Прочитай JSON, сопоставь с текущим разговором и '
        'сообщи Максиму только необходимое решение или риск. После предъявления '
        'вызови mail_channel.acknowledge_owner_escalation с именем и SHA-256.\n'
        f'Имя: {path.name}\nSHA-256: {sha}\nJSON:\n' +
        json.dumps(payload, ensure_ascii=False, indent=2))
    command = [cfg['codex_binary'], 'queue', '--thread', cfg['thread_id'], '--message', message]
    result = runner(command, capture_output=True, text=True, timeout=30,
                    cwd=cfg.get('working_directory'),
                    env={**os.environ, 'PATH': cfg.get('path', os.environ.get('PATH', ''))})
    if result.returncode != 0:
        return {'status': 'delivery_failed', 'name': path.name,
                'error': ((result.stderr or '') + (result.stdout or ''))[-1500:]}
    deliveries[key] = {'name': path.name, 'sha256': sha, 'state': 'delivered',
                       'observed_at': now, 'delivered_at': now, 'thread_id': cfg['thread_id']}
    atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
    return {'status': 'delivered', 'name': path.name, 'sha256': sha}

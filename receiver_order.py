"""Receiver-owned registration order for local mailbox files.

Sender timestamps and filenames are metadata, not evidence of arrival order.
This registry assigns an immutable local timestamp and monotonic sequence to
each observed (mailbox, filename, content hash) version.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile


STATE_NAME = 'receive-order.json'
LOCK_NAME = 'receive-order.lock'


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _utc(seconds):
    return datetime.fromtimestamp(float(seconds), timezone.utc).isoformat(
        timespec='microseconds').replace('+00:00', 'Z')


def _key(channel, item):
    return json.dumps([channel, item['name'], item['sha256']], ensure_ascii=False,
                      separators=(',', ':'))


def register_and_sort(state_directory, channel, items, now, legacy_items=()):
    """Register exact file versions and return them in receiver order."""
    state_dir = Path(state_directory)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = state_dir / STATE_NAME
    with (state_dir / LOCK_NAME).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = (json.loads(state_path.read_text()) if state_path.exists()
                 else {'schema_version': 1, 'next_receive_sequence': 1,
                       'next_observation_batch': 1, 'entries': {}})
        state.setdefault('next_receive_sequence', 1)
        state.setdefault('next_observation_batch', 1)
        entries = state.setdefault('entries', {})

        def add_group(group, source, observed_at):
            unknown = [dict(item) for item in group if _key(channel, item) not in entries]
            if not unknown:
                return
            batch = state['next_observation_batch']
            state['next_observation_batch'] += 1
            # Polling cannot reveal causal order inside one observation. Hash
            # order is deterministic and does not treat sender time as truth.
            unknown.sort(key=lambda item: (item['sha256'], item['name']))
            for item in unknown:
                seq = state['next_receive_sequence']
                state['next_receive_sequence'] += 1
                entries[_key(channel, item)] = {
                    'channel': channel,
                    'name': item['name'],
                    'sha256': item['sha256'],
                    'received_at_utc': _utc(observed_at),
                    'receive_sequence': seq,
                    'observation_batch': batch,
                    'order_within_batch': 'unknown',
                    'registration_source': source,
                }

        groups = {}
        for item in legacy_items:
            groups.setdefault((float(item['observed_at']), item.get('legacy_batch')), []).append(item)
        for (observed_at, _), group in sorted(groups.items(), key=lambda pair: pair[0][0]):
            add_group(group, 'legacy_receiver_state', observed_at)
        add_group(items, 'receiver_first_seen', now)
        _atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))

        result = []
        for item in items:
            enriched = dict(item)
            enriched.update(entries[_key(channel, item)])
            result.append(enriched)
        return sorted(result, key=lambda item: item['receive_sequence'])

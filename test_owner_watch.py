from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

import owner_watch


class OwnerWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        sessions = root / '.codex/sessions/2026/09/20'
        sessions.mkdir(parents=True)
        (sessions / 'rollout-live-thread.jsonl').write_text('{}')
        self.cfg = {'owner_outbox': str(root / 'owner-outbox'),
                    'owner_archive': str(root / 'owner-archive'),
                    'state_directory': str(root / 'state'), 'thread_id': 'live-thread',
                    'codex_home': str(root / '.codex'), 'validate_thread': True,
                    'codex_binary': '/test/codex', 'working_directory': str(root),
                    'ack_deadline_seconds': 100}
        Path(self.cfg['owner_outbox']).mkdir()
        self.sender = Mock(return_value=subprocess.CompletedProcess([], 0, 'queued', ''))

    def escalation(self):
        p = Path(self.cfg['owner_outbox']) / 'one.json'
        p.write_text(json.dumps({'for_human': True, 'body': 'Choose'}))
        p.touch()
        return p

    def test_deliver_once_then_acknowledge_exact_archive(self):
        p = self.escalation()
        result = owner_watch.run(self.cfg, self.sender, now=p.stat().st_mtime + 6)
        self.assertEqual(result['status'], 'delivered')
        owner_watch.run(self.cfg, self.sender, now=p.stat().st_mtime + 20)
        self.sender.assert_called_once()
        archive = Path(self.cfg['owner_archive']) / p.name
        archive.parent.mkdir(exist_ok=True)
        p.replace(archive)
        result = owner_watch.run(self.cfg, self.sender, now=archive.stat().st_mtime + 30)
        self.assertEqual(result['status'], 'idle')
        state = json.loads((Path(self.cfg['state_directory']) / 'owner-delivery.json').read_text())
        self.assertEqual(next(iter(state['deliveries'].values()))['state'], 'acknowledged')

    def test_stale_thread_never_queues(self):
        p = self.escalation()
        archived = Path(self.cfg['codex_home']) / 'archived_sessions'
        archived.mkdir()
        (archived / 'rollout-live-thread.jsonl').write_text('{}')
        result = owner_watch.run(self.cfg, self.sender, now=p.stat().st_mtime + 6)
        self.assertEqual(result['status'], 'thread_unavailable')
        self.sender.assert_not_called()

    def test_unacknowledged_delivery_becomes_overdue_without_duplicate(self):
        p = self.escalation()
        t = p.stat().st_mtime + 6
        owner_watch.run(self.cfg, self.sender, now=t)
        result = owner_watch.run(self.cfg, self.sender, now=t + 101)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['overdue'], ['one.json'])
        self.sender.assert_called_once()


if __name__ == '__main__':
    unittest.main()

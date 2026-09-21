from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import mail_channel as m


class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(m, 'MAIL', Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        state = patch.object(m, 'RECEIVER_STATE', Path(self.tmp.name) / 'state')
        state.start()
        self.addCleanup(state.stop)

    def test_deduplicate_and_repair_pending_delivery(self):
        first = m.notify_claude('Review', 'Body', 'test-001')
        second = m.notify_claude('Review', 'Body', 'test-001')
        self.assertEqual(first['name'], second['name'])
        Path(first['path']).unlink()
        third = m.notify_claude('Review', 'Body', 'test-001')
        self.assertEqual(first['name'], third['name'])
        self.assertTrue(Path(third['path']).exists())
        with self.assertRaises(ValueError):
            m.notify_claude('Review', 'Changed', 'test-001')

    def test_archive_requires_exact_read_version(self):
        with m.locked():
            path = m.MAIL / 'outbox' / 'reply.md'
            path.write_text('First')
        message = m.read_replies()['messages'][0]
        self.assertTrue(path.exists())
        path.write_text('Changed')
        with self.assertRaises(ValueError):
            m.archive_reply('reply.md', message['sha256'])
        message = m.read_reply('reply.md')
        m.archive_reply('reply.md', message['sha256'])
        self.assertFalse(path.exists())
        self.assertEqual(m.read_replies()['messages'], [])
        self.assertTrue(m.archive_reply('reply.md', message['sha256'])['archived'])

    def test_read_order_comes_from_receiver_not_sender_filename(self):
        with m.locked():
            future = m.MAIL / 'outbox' / '2099-first.md'
            future.write_text('first')
        self.assertEqual(m.read_replies()['messages'][0]['name'], future.name)
        with m.locked():
            past = m.MAIL / 'outbox' / '2000-second.md'
            past.write_text('second')
        self.assertEqual([x['name'] for x in m.read_replies()['messages']],
                         [future.name, past.name])

    def test_traversal_and_symlinks_refused(self):
        with self.assertRaises(ValueError):
            m.read_reply('../config.md')
        with m.locked():
            (m.MAIL / 'outbox' / 'link.md').symlink_to('/etc/hosts')
        with self.assertRaises(ValueError):
            m.read_replies()

    def test_owner_json_escalation_can_be_acknowledged_by_exact_sha(self):
        with m.locked():
            folder = m.MAIL / 'owner-outbox'
            folder.mkdir()
            p = folder / 'escalation.json'
            p.write_text('{"for_human": true}')
        sha = m._digest(p)
        done = m.acknowledge_owner_escalation(p.name, sha)
        self.assertTrue(done['acknowledged'])
        self.assertTrue((m.MAIL / 'owner-archive' / p.name).exists())

    def test_budget_does_not_silently_consume_large_mail(self):
        with m.locked():
            (m.MAIL / 'outbox' / 'large.md').write_text('a' * 100)
        self.assertEqual(m.read_replies(max_chars=10)['remaining'], 1)
        first = m.read_reply('large.md', max_chars=20)
        self.assertEqual(first['next_offset'], 20)
        self.assertTrue((m.MAIL / 'outbox' / 'large.md').exists())

    def test_one_exact_version_has_one_active_claim(self):
        with m.locked():
            p = m.MAIL / 'outbox' / 'one.md'
            p.write_text('hello')
        sha = m.read_reply('one.md')['sha256']
        first = m.claim_reply('one.md', sha, 'mailroom', now=100)
        second = m.claim_reply('one.md', sha, 'interactive-astra', now=101)
        self.assertTrue(first['claimed'])
        self.assertFalse(second['claimed'])
        self.assertEqual(second['actor'], 'mailroom')

    def test_expired_claim_can_be_recovered(self):
        with m.locked():
            p = m.MAIL / 'outbox' / 'one.md'
            p.write_text('hello')
        sha = m.read_reply('one.md')['sha256']
        first = m.claim_reply('one.md', sha, 'dead-worker', ttl_seconds=30, now=100)
        recovered = m.claim_reply('one.md', sha, 'mailroom', now=131)
        self.assertNotEqual(first['token'], recovered['token'])
        self.assertTrue(recovered['claimed'])

    def test_completion_requires_durable_outcome_and_writes_receipt(self):
        with m.locked():
            p = m.MAIL / 'outbox' / 'one.md'
            p.write_text('hello')
        sha = m.read_reply('one.md')['sha256']
        claim = m.claim_reply('one.md', sha, 'mailroom', now=100)
        with self.assertRaises(ValueError):
            m.complete_claim('one.md', sha, claim['token'], disposition='replied',
                             outcome_ref='', now=101)
        done = m.complete_claim('one.md', sha, claim['token'], disposition='escalated',
                                outcome_ref='owner-outbox/escalation.json',
                                authority_ids=['D-CODEX-0007'], now=102)
        self.assertTrue(Path(done['receipt']).exists())
        self.assertFalse(p.exists())
        self.assertTrue((m.MAIL / 'archive' / 'one.md').exists())
        receipt = __import__('json').loads(Path(done['receipt']).read_text())
        self.assertEqual(receipt['state'], 'archived')
        self.assertEqual(receipt['disposition'], 'escalated')

    def test_wrong_actor_cannot_complete_or_release_claim(self):
        with m.locked():
            p = m.MAIL / 'outbox' / 'one.md'
            p.write_text('hello')
        sha = m.read_reply('one.md')['sha256']
        claim = m.claim_reply('one.md', sha, 'mailroom')
        with self.assertRaises(ValueError):
            m.complete_claim('one.md', sha, 'wrong-token', disposition='replied',
                             outcome_ref='x')
        self.assertFalse(m.release_claim('one.md', sha, 'wrong-token')['released'])
        self.assertTrue(m.release_claim('one.md', sha, claim['token'])['released'])


if __name__ == '__main__':
    unittest.main()

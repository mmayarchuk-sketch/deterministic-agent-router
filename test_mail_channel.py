from pathlib import Path
import hashlib
import json
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

    # --- Зеркало входящих: ящик как параметр, старый путь без миграции ---

    def letter(self, mailbox, name='one.md', text='hello'):
        with m.locked():
            path = m.MAIL / mailbox / name
            path.write_text(text)
        return path, m._digest(path)

    def test_claim_key_keeps_legacy_shape_for_outbox_and_namespaces_inbox(self):
        self.assertEqual(m._claim_key('outbox', 'a.md', 'sha'), 'a.md:sha')
        self.assertEqual(m._claim_key('inbox', 'a.md', 'sha'), 'inbox:a.md:sha')
        with self.assertRaises(ValueError):
            m._claim_key('elsewhere', 'a.md', 'sha')

    def test_same_name_in_both_mailboxes_does_not_claim_each_other(self):
        _, out_sha = self.letter('outbox')
        _, in_sha = self.letter('inbox')
        first = m.claim_reply('one.md', out_sha, 'codex-mailroom', now=100)
        second = m.claim_reply('one.md', in_sha, 'mirror', now=100, mailbox='inbox')
        self.assertTrue(first['claimed'])
        self.assertTrue(second['claimed'])
        self.assertNotEqual(first['token'], second['token'])
        claims = json.loads((m.MAIL / m.CLAIMS).read_text())
        self.assertEqual(sorted(claims), sorted(['one.md:' + out_sha,
                                                 'inbox:one.md:' + in_sha]))

    def test_receiver_order_is_separate_per_mailbox(self):
        self.letter('outbox', '2099-first.md', 'first')
        self.assertEqual(m.read_replies()['messages'][0]['name'], '2099-first.md')
        self.letter('inbox', 'incoming.md', 'incoming')
        m.read_replies(mailbox='inbox')
        self.letter('outbox', '2000-second.md', 'second')
        self.assertEqual([x['name'] for x in m.read_replies()['messages']],
                         ['2099-first.md', '2000-second.md'])
        state = json.loads((m.RECEIVER_STATE / 'receive-order.json').read_text())
        channels = sorted({e['channel'] for e in state['entries'].values()})
        self.assertEqual(channels, ['inbox', 'outbox'])

    def test_archives_are_separate_and_legacy_archive_still_recognised(self):
        _, out_sha = self.letter('outbox')
        _, in_sha = self.letter('inbox')
        m.archive_reply('one.md', out_sha)
        m.archive_reply('one.md', in_sha, mailbox='inbox')
        self.assertTrue((m.MAIL / 'archive' / 'one.md').exists())
        self.assertTrue((m.MAIL / 'inbox-archive' / 'one.md').exists())
        # Письмо, заархивированное по-старому в общую папку, считается
        # заархивированным и не доставляется повторно.
        legacy = m.MAIL / 'archive' / 'old.md'
        legacy.write_text('legacy')
        self.assertIsNone(m._archived('inbox', 'old.md'))
        self.assertIsNotNone(m._archived('inbox', 'old.md', m._digest(legacy)))
        again = m.archive_reply('old.md', m._digest(legacy), mailbox='inbox')
        self.assertEqual(again['path'], str(legacy))

    def test_receipt_name_and_body_separate_the_mailboxes(self):
        _, out_sha = self.letter('outbox')
        _, in_sha = self.letter('inbox')
        out_claim = m.claim_reply('one.md', out_sha, 'codex-mailroom', now=100)
        in_claim = m.claim_reply('one.md', in_sha, 'mirror', now=100, mailbox='inbox')
        m.prepare_outcome('one.md', in_sha, in_claim['token'], mailbox='inbox',
                          payload={'action': 'reply'}, now=100)
        out_done = m.complete_claim('one.md', out_sha, out_claim['token'],
                                    disposition='replied', outcome_ref='x', now=101)
        in_done = m.complete_claim('one.md', in_sha, in_claim['token'], mailbox='inbox',
                                   disposition='replied', outcome_ref='y', now=101)
        self.assertNotEqual(out_done['receipt'], in_done['receipt'])
        self.assertEqual(json.loads(Path(out_done['receipt']).read_text())['source_mailbox'],
                         'outbox')
        incoming = json.loads(Path(in_done['receipt']).read_text())
        self.assertEqual(incoming['source_mailbox'], 'inbox')
        self.assertTrue(incoming['prepared_sha256'])

    def test_legacy_outbox_claim_and_receipt_need_no_migration(self):
        path, sha = self.letter('outbox')
        # Захват, записанный прежней версией: ключ без ящика, поля mailbox нет.
        (m.MAIL / m.CLAIMS).write_text(json.dumps({
            f'one.md:{sha}': {'name': 'one.md', 'sha256': sha, 'actor': 'codex-mailroom',
                              'token': 'legacy-token', 'state': 'observed',
                              'claimed_at': 100, 'expires_at': 100000}}))
        done = m.complete_claim('one.md', sha, 'legacy-token', disposition='replied',
                                outcome_ref='legacy', now=200)
        expected = hashlib.sha256(f'one.md:{sha}'.encode()).hexdigest()[:24] + '.json'
        self.assertEqual(Path(done['receipt']).name, expected)
        self.assertTrue((m.MAIL / 'archive' / 'one.md').exists())

    def test_unknown_mailbox_is_refused_before_touching_disk(self):
        missing = Path(self.tmp.name) / 'never-created'
        with patch.object(m, 'MAIL', missing):
            calls = [
                lambda: m.read_replies(mailbox='elsewhere'),
                lambda: m.read_reply('a.md', mailbox='elsewhere'),
                lambda: m.archive_reply('a.md', 'sha', mailbox='elsewhere'),
                lambda: m.claim_reply('a.md', 'sha', 'actor', mailbox='elsewhere'),
                lambda: m.complete_claim('a.md', 'sha', 't', disposition='replied',
                                         outcome_ref='x', mailbox='elsewhere'),
                lambda: m.release_claim('a.md', 'sha', 't', mailbox='elsewhere'),
                lambda: m.prepare_outcome('a.md', 'sha', 't', payload={}, mailbox='elsewhere'),
                lambda: m.load_prepared_outcome('a.md', 'sha', mailbox='elsewhere'),
            ]
            for call in calls:
                with self.assertRaises(ValueError):
                    call()
            self.assertFalse(missing.exists())

    def test_prepared_outcome_is_written_once_and_never_overwritten(self):
        _, sha = self.letter('inbox')
        claim = m.claim_reply('one.md', sha, 'mirror', now=100, mailbox='inbox')
        first = m.prepare_outcome('one.md', sha, claim['token'], mailbox='inbox',
                                  payload={'body': 'stable'}, now=100)
        self.assertFalse(first['reused'])
        second = m.prepare_outcome('one.md', sha, claim['token'], mailbox='inbox',
                                   payload={'body': 'stochastic'}, now=101)
        self.assertTrue(second['reused'])
        self.assertEqual(second['payload'], {'body': 'stable'})
        self.assertEqual(m.load_prepared_outcome('one.md', sha, mailbox='inbox')['payload'],
                         {'body': 'stable'})
        self.assertEqual(Path(first['path']).name,
                         hashlib.sha256(f'inbox:one.md:{sha}'.encode()).hexdigest() + '.json')

    def test_prepared_outcome_refuses_foreign_or_corrupted_record(self):
        _, sha = self.letter('inbox')
        claim = m.claim_reply('one.md', sha, 'mirror', now=100, mailbox='inbox')
        prepared = m.prepare_outcome('one.md', sha, claim['token'], mailbox='inbox',
                                     payload={'body': 'stable'}, now=100)
        record = json.loads(Path(prepared['path']).read_text())
        record['payload'] = {'body': 'substituted'}
        Path(prepared['path']).write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            m.load_prepared_outcome('one.md', sha, mailbox='inbox')
        record['source_key'] = 'inbox:other.md:' + sha
        record['outcome_sha256'] = m._outcome_digest(record)
        Path(prepared['path']).write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            m.load_prepared_outcome('one.md', sha, mailbox='inbox')

    def test_expired_or_handed_over_claim_can_neither_prepare_nor_complete(self):
        _, sha = self.letter('inbox')
        old = m.claim_reply('one.md', sha, 'dead-worker', ttl_seconds=30, now=100,
                            mailbox='inbox')
        with self.assertRaises(ValueError):
            m.prepare_outcome('one.md', sha, old['token'], mailbox='inbox',
                              payload={'body': 'late'}, now=200)
        with self.assertRaises(ValueError):
            m.complete_claim('one.md', sha, old['token'], mailbox='inbox',
                             disposition='replied', outcome_ref='x', now=200)
        fresh = m.claim_reply('one.md', sha, 'mirror', now=200, mailbox='inbox')
        self.assertTrue(fresh['claimed'])
        with self.assertRaises(ValueError):
            m.prepare_outcome('one.md', sha, old['token'], mailbox='inbox',
                              payload={'body': 'late'}, now=201)
        self.assertTrue((m.MAIL / 'inbox' / 'one.md').exists())

    def test_mirror_completion_requires_a_fenced_prepared_outcome(self):
        _, sha = self.letter('inbox')
        claim = m.claim_reply('one.md', sha, 'mirror', now=100, mailbox='inbox')
        with self.assertRaises(ValueError):
            m.complete_claim('one.md', sha, claim['token'], mailbox='inbox',
                             disposition='replied', outcome_ref='x', now=101)
        self.assertTrue((m.MAIL / 'inbox' / 'one.md').exists())
        m.prepare_outcome('one.md', sha, claim['token'], mailbox='inbox',
                          payload={'body': 'answer'}, now=101)
        done = m.complete_claim('one.md', sha, claim['token'], mailbox='inbox',
                                disposition='replied', outcome_ref='x', now=102)
        self.assertTrue((m.MAIL / 'inbox-archive' / 'one.md').exists())
        self.assertTrue(Path(done['receipt']).exists())

    def test_source_changed_after_claim_commits_nothing(self):
        path, sha = self.letter('inbox')
        claim = m.claim_reply('one.md', sha, 'mirror', now=100, mailbox='inbox')
        path.write_text('the sender edited the letter')
        with self.assertRaises(ValueError):
            m.prepare_outcome('one.md', sha, claim['token'], mailbox='inbox',
                              payload={'body': 'answer'}, now=101)
        with self.assertRaises(ValueError):
            m.complete_claim('one.md', sha, claim['token'], mailbox='inbox',
                             disposition='replied', outcome_ref='x', now=101)
        self.assertEqual(list((m.MAIL / 'inbox-archive').glob('*.md')), [])

    def test_mirror_delivery_writes_to_outbox_with_reversed_header(self):
        sent = m.notify_codex('Answer', 'Body', 'mirror-' + 'a' * 64, reply_to='one.md')
        self.assertTrue(Path(sent['path']).parent.name == 'outbox')
        text = Path(sent['path']).read_text()
        self.assertIn('from: "claude"', text)
        self.assertIn('to: "codex"', text)
        repeat = m.notify_codex('Answer', 'Body', 'mirror-' + 'a' * 64, reply_to='one.md')
        self.assertEqual(sent['name'], repeat['name'])
        self.assertEqual(len(list((m.MAIL / 'outbox').glob('*.md'))), 1)
        self.assertEqual(list((m.MAIL / 'inbox').glob('*.md')), [])


if __name__ == '__main__':
    unittest.main()

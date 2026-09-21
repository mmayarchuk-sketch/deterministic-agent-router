from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import mail_channel as channel
import mailroom


class MailroomTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = patch.object(channel, 'MAIL', self.root / 'mail')
        p.start()
        self.addCleanup(p.stop)
        receiver_state = patch.object(channel, 'RECEIVER_STATE', self.root / 'receiver-state')
        receiver_state.start()
        self.addCleanup(receiver_state.stop)
        with channel.locked():
            pass
        self.digest = self.root / 'DIGEST.md'
        self.digest.write_text('Decision D-CODEX-0007 permits Mailroom.')
        self.policy = self.root / 'policy.json'
        self.policy.write_text(Path(mailroom.POLICY).read_text())
        self.cfg = {'actor': 'mailroom', 'memory_digests': [str(self.digest)],
                    'policy': str(self.policy), 'lease_seconds': 60,
                    'state_directory': str(self.root / 'state')}

    def letter(self, name='one.md'):
        with channel.locked():
            p = channel.MAIL / 'outbox' / name
            p.write_text('Question')

    def test_technical_reply_is_delivered_and_archived_once(self):
        self.letter()
        outcome = {'action': 'reply', 'kind': 'technical_analysis', 'subject': 'Answer',
                   'body': 'Read-only finding.', 'authority_ids': [],
                   'requires_external_action': False, 'reason': 'Technical only'}
        first = mailroom.run(self.cfg, classifier=lambda _: outcome, now=100)
        second = mailroom.run(self.cfg, classifier=lambda _: outcome, now=101)
        self.assertEqual(first['status'], 'replied')
        self.assertEqual(second['status'], 'idle')
        self.assertEqual(len(list((channel.MAIL / 'inbox').glob('*.md'))), 1)
        self.assertEqual(len(list((channel.MAIL / 'receipts').glob('*.json'))), 1)

    def test_owner_question_is_escalated_not_replied(self):
        self.letter()
        outcome = {'action': 'escalate', 'kind': 'owner_decision', 'subject': 'Need owner',
                   'body': 'Choose A or B.', 'authority_ids': [],
                   'requires_external_action': False, 'reason': 'New decision'}
        result = mailroom.run(self.cfg, classifier=lambda _: outcome, now=100)
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual(len(list((channel.MAIL / 'owner-outbox').glob('*.json'))), 1)
        self.assertEqual(len(list((channel.MAIL / 'inbox').glob('*.md'))), 0)

    def test_unknown_authority_is_rejected_and_letter_remains(self):
        self.letter()
        outcome = {'action': 'reply', 'kind': 'authorized_action', 'subject': 'Go',
                   'body': 'Authorized.', 'authority_ids': ['D-CODEX-9999'],
                   'requires_external_action': True, 'reason': 'Claimed authority'}
        with self.assertRaises(ValueError):
            mailroom.run(self.cfg, classifier=lambda _: outcome, now=100)
        self.assertTrue((channel.MAIL / 'outbox' / 'one.md').exists())

    def test_second_reader_cannot_take_live_lease(self):
        self.letter()
        msg = channel.read_reply('one.md')
        channel.claim_reply('one.md', msg['sha256'], 'interactive-astra', now=100)
        result = mailroom.run(self.cfg, classifier=lambda _: None, now=101)
        self.assertEqual(result['status'], 'claimed_elsewhere')
        self.assertEqual(result['actor'], 'interactive-astra')

    def test_shadow_mode_writes_candidate_but_touches_no_mail(self):
        self.letter()
        self.cfg['mode'] = 'shadow'
        outcome = {'action': 'reply', 'kind': 'technical_analysis', 'subject': 'Candidate',
                   'body': 'Would answer this.', 'authority_ids': [],
                   'requires_external_action': False, 'reason': 'Technical only'}
        result = mailroom.run(self.cfg, classifier=lambda _: outcome, now=100)
        self.assertEqual(result['status'], 'shadowed')
        self.assertTrue(Path(result['candidate']).exists())
        self.assertTrue((channel.MAIL / 'outbox' / 'one.md').exists())
        self.assertEqual(list((channel.MAIL / 'inbox').glob('*.md')), [])
        self.assertEqual(list((channel.MAIL / 'archive').glob('*.md')), [])
        again = mailroom.run(self.cfg, classifier=lambda _: None, now=101)
        self.assertEqual(again['status'], 'shadowed')

    def test_crash_after_reply_reuses_same_outcome_without_duplicate(self):
        self.letter()
        outcome = {'action': 'reply', 'kind': 'technical_analysis', 'subject': 'Answer',
                   'body': 'Stable answer.', 'authority_ids': [],
                   'requires_external_action': False, 'reason': 'Technical only'}
        real_complete = channel.complete_claim
        with patch.object(channel, 'complete_claim', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                mailroom.run(self.cfg, classifier=lambda _: outcome, now=100)
        self.assertEqual(len(list((channel.MAIL / 'inbox').glob('*.md'))), 1)
        changed = dict(outcome, body='A different stochastic answer.')
        result = mailroom.run(self.cfg, classifier=lambda _: changed, now=101)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(len(list((channel.MAIL / 'inbox').glob('*.md'))), 1)
        sent = next((channel.MAIL / 'inbox').glob('*.md')).read_text()
        self.assertIn('Stable answer.', sent)
        self.assertNotIn('different stochastic', sent)


if __name__ == '__main__':
    unittest.main()

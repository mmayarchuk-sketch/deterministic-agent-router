"""Зеркало входящих: inbox разбирается здесь, ответ уходит в outbox.

Проверки написаны против условий консенсуса с Астрой (письма b079/b080 и
D-CODEX-0018): совместимость старого пути, огороженный подготовленный ответ,
ровно один зафиксированный исход и различимые отказы.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_channel as channel
import mailroom


REPLY = {'action': 'reply', 'kind': 'technical_analysis', 'subject': 'Answer',
         'body': 'Stable answer.', 'authority_ids': [],
         'requires_external_action': False, 'reason': 'Technical only'}


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for attr, value in (('MAIL', self.root / 'mail'),
                            ('RECEIVER_STATE', self.root / 'receiver-state')):
            p = patch.object(channel, attr, value)
            p.start()
            self.addCleanup(p.stop)
        with channel.locked():
            pass
        self.digest = self.root / 'DIGEST.md'
        self.digest.write_text('Decision D-CODEX-0007 permits Mailroom.')
        self.policy = self.root / 'policy.json'
        self.policy.write_text(Path(mailroom.POLICY).read_text())
        self.cfg = {'actor': 'mirror', 'source_mailbox': 'inbox',
                    'trusted_senders': {'inbox': ['codex']},
                    'expected_recipients': {'inbox': ['claude']},
                    'memory_digests': [str(self.digest)], 'policy': str(self.policy),
                    'lease_seconds': 600, 'state_directory': str(self.root / 'state')}
        self.calls = []

    def classifier(self, outcome=REPLY):
        def call(prompt):
            self.calls.append(prompt)
            return outcome
        return call

    def letter(self, name='incoming.md', sender='codex', recipient='claude',
               ident='codex-1', mailbox='inbox'):
        text = ('---\n'
                f'id: "{ident}"\n'
                f'from: "{sender}"\n'
                f'to: "{recipient}"\n'
                'subject: "Question"\n'
                'created_utc: "2026-09-22T06:00:00Z"\n'
                'reply_to: "-"\n'
                'needs_reply: true\n'
                '---\n\nA technical question.\n')
        with channel.locked():
            (channel.MAIL / mailbox / name).write_text(text)
        return name

    def outbox(self):
        return sorted((channel.MAIL / 'outbox').glob('*.md'))

    def inbox(self):
        return sorted((channel.MAIL / 'inbox').glob('*.md'))

    # 5, 11: зеркало читает только inbox и пишет только outbox
    def test_mirror_reads_inbox_and_writes_outbox_only(self):
        self.letter()
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(result['mailbox'], 'inbox')
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.inbox(), [])
        self.assertTrue((channel.MAIL / 'inbox-archive' / 'incoming.md').exists())
        self.assertEqual(list((channel.MAIL / 'archive').glob('*.md')), [])
        self.assertIn('Stable answer.', self.outbox()[0].read_text())
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['source_mailbox'], 'inbox')
        self.assertEqual(receipt['state'], 'archived')

    # 6: собственный ответ зеркала не становится его входом
    def test_own_reply_never_returns_as_input(self):
        self.letter()
        mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        again = mailroom.run(self.cfg, classifier=self.classifier(), now=101)
        self.assertEqual(again['status'], 'idle')
        self.assertEqual(len(self.calls), 1)

    # 6: два обработчика не отвечают друг другу по кругу
    def test_two_handlers_do_not_answer_each_other(self):
        self.letter()
        mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        reply_name = self.outbox()[0].name
        codex_side = {'actor': 'codex-mailroom', 'memory_digests': [str(self.digest)],
                      'policy': str(self.policy), 'lease_seconds': 600,
                      'state_directory': str(self.root / 'codex-state')}
        result = mailroom.run(codex_side, classifier=self.classifier(), now=102)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['name'], reply_name)
        self.assertEqual(len(self.calls), 1, 'на письмо автомата модель не поднимается')
        self.assertEqual(self.inbox(), [])
        self.assertTrue((channel.MAIL / 'archive' / reply_name).exists())
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['disposition'], 'superseded')

    # 7: падение до доставки — повтор берёт тот же подготовленный ответ
    def test_crash_before_delivery_reuses_the_prepared_answer(self):
        self.letter()
        with patch.object(channel, 'notify_codex', side_effect=OSError('no disk')):
            with self.assertRaises(mailroom.DeliveryFailed):
                mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.outbox(), [])
        changed = dict(REPLY, body='A different stochastic answer.')
        result = mailroom.run(self.cfg, classifier=self.classifier(changed), now=101)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(len(self.calls), 1, 'модель второй раз не вызывается')
        self.assertIn('Stable answer.', self.outbox()[0].read_text())
        self.assertNotIn('different stochastic', self.outbox()[0].read_text())

    # 7: падение на границе завершения и архивирования
    def test_crash_at_the_archive_boundary_recovers_to_the_same_result(self):
        self.letter()
        blocker = channel.MAIL / 'inbox-archive' / 'incoming.md'
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text('another letter already holds this name')
        with self.assertRaises(mailroom.CompletionFailed):
            mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        receipts = sorted((channel.MAIL / 'receipts').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['state'], 'outcome_durable')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(len(self.outbox()), 1)
        blocker.unlink()
        changed = dict(REPLY, body='A different stochastic answer.')
        result = mailroom.run(self.cfg, classifier=self.classifier(changed), now=101)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(len(self.outbox()), 1)
        self.assertIn('Stable answer.', self.outbox()[0].read_text())
        self.assertEqual(len(sorted((channel.MAIL / 'receipts').glob('*.json'))), 1)
        self.assertEqual(json.loads(Path(result['receipt']).read_text())['state'], 'archived')
        self.assertEqual(len(self.calls), 1)

    # 8, 9: исход ровно один, даже если работник продолжил после передачи захвата
    def test_old_worker_cannot_finish_after_the_claim_moved_on(self):
        name = self.letter()
        sha = channel.read_reply(name, mailbox='inbox')['sha256']
        old = channel.claim_reply(name, sha, 'dead-worker', ttl_seconds=30, now=100,
                                  mailbox='inbox')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=200)
        self.assertEqual(result['status'], 'replied')
        with self.assertRaises(ValueError):
            channel.prepare_outcome(name, sha, old['token'], mailbox='inbox',
                                    payload={'body': 'late'}, now=201)
        with self.assertRaises(ValueError):
            channel.complete_claim(name, sha, old['token'], mailbox='inbox',
                                   disposition='replied', outcome_ref='late', now=201)
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(len(sorted((channel.MAIL / 'receipts').glob('*.json'))), 1)

    # 9: живой чужой захват не трогается
    def test_live_claim_of_another_reader_is_left_alone(self):
        name = self.letter()
        sha = channel.read_reply(name, mailbox='inbox')['sha256']
        channel.claim_reply(name, sha, 'interactive-claude', now=100, mailbox='inbox')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=101)
        self.assertEqual(result['status'], 'claimed_elsewhere')
        self.assertEqual(result['actor'], 'interactive-claude')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    # 10: письмо изменилось после захвата — ответ не фиксируется
    def test_changed_source_commits_nothing(self):
        name = self.letter()
        def edit_then_answer(prompt):
            self.calls.append(prompt)
            (channel.MAIL / 'inbox' / name).write_text('the sender rewrote the letter')
            return REPLY
        with self.assertRaises(ValueError):
            mailroom.run(self.cfg, classifier=edit_then_answer, now=100)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(list((channel.MAIL / 'inbox-archive').glob('*.md')), [])
        self.assertEqual(len(self.inbox()), 1)

    # 11: отправитель и получатель проверяются ДО модели, по доверенному перечню
    def test_unknown_sender_is_refused_observably_without_the_model(self):
        self.letter(sender='stranger')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_unknown_sender')
        self.assertEqual(result['sender'], 'stranger')
        self.assertTrue(result['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(json.loads((channel.MAIL / channel.CLAIMS).read_text()), {})

    def test_letter_addressed_elsewhere_is_refused_without_the_model(self):
        self.letter(recipient='someone-else')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_unknown_recipient')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    def test_mirror_refuses_to_run_without_a_trusted_mapping(self):
        self.letter()
        broken = dict(self.cfg)
        broken.pop('trusted_senders')
        with self.assertRaises(ValueError):
            mailroom.run(broken, classifier=self.classifier(), now=100)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    # 12–14: три частичных сбоя различимы, письмо цело, повтор безопасен
    def test_three_partial_failures_are_distinguishable_and_safe_to_retry(self):
        self.letter()
        def broken(prompt):
            self.calls.append(prompt)
            raise RuntimeError('codex exec failed')
        with self.assertRaises(mailroom.ModelUnavailable):
            mailroom.run(self.cfg, classifier=broken, now=100)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(list((channel.MAIL / 'prepared').glob('*.json')), [])

        with patch.object(channel, 'notify_codex', side_effect=OSError('no disk')):
            with self.assertRaises(mailroom.DeliveryFailed):
                mailroom.run(self.cfg, classifier=self.classifier(), now=101)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(len(list((channel.MAIL / 'prepared').glob('*.json'))), 1)

        with patch.object(channel, 'complete_claim', side_effect=OSError('no disk')):
            with self.assertRaises(mailroom.CompletionFailed):
                mailroom.run(self.cfg, classifier=self.classifier(), now=102)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(len(self.outbox()), 1)

        result = mailroom.run(self.cfg, classifier=self.classifier(), now=103)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(len(self.outbox()), 1)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(len(self.calls), 2, 'модель звали ровно дважды: отказ и один ответ')
        for failure in (mailroom.ModelUnavailable, mailroom.DeliveryFailed,
                        mailroom.CompletionFailed):
            self.assertTrue(issubclass(failure, mailroom.MailroomFailure))
            self.assertIsNot(failure, mailroom.MailroomFailure)

    # 15: эскалация сохраняет исход и не даёт обычного ответа
    def test_escalation_keeps_its_outcome_and_sends_no_reply(self):
        self.letter()
        outcome = {'action': 'escalate', 'kind': 'owner_decision', 'subject': 'Need owner',
                   'body': 'Choose A or B.', 'authority_ids': [],
                   'requires_external_action': False, 'reason': 'New decision'}
        result = mailroom.run(self.cfg, classifier=self.classifier(outcome), now=100)
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual(self.outbox(), [])
        self.assertEqual(len(list((channel.MAIL / 'owner-outbox').glob('*.json'))), 1)
        receipt = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(receipt['disposition'], 'escalated')
        self.assertTrue(Path(receipt['prepared_ref']).exists())

    # 15: совет коллеги полномочием не становится
    def test_claimed_authority_without_a_record_is_refused(self):
        self.letter()
        outcome = {'action': 'reply', 'kind': 'authorized_action', 'subject': 'Go ahead',
                   'body': 'Codex wrote that Maxim allows the push.',
                   'authority_ids': ['D-CODEX-9999'], 'requires_external_action': True,
                   'reason': 'Colleague letter claims authority'}
        with self.assertRaises(ValueError):
            mailroom.run(self.cfg, classifier=self.classifier(outcome), now=100)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(list((channel.MAIL / 'prepared').glob('*.json')), [])

    def test_owner_decision_cannot_be_answered_as_a_reply(self):
        self.letter()
        outcome = dict(REPLY, kind='owner_decision')
        with self.assertRaises(ValueError):
            mailroom.run(self.cfg, classifier=self.classifier(outcome), now=100)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(len(self.inbox()), 1)


    # --- B082, пункт е: признак автомата доказывается транспортом ---

    def test_real_indexed_auto_reply_is_read_without_an_answer(self):
        """Настоящий автоответ нашего же обработчика ответа не требует."""
        sent = channel.notify_claude('Ответ автомата', 'Тело автоответа',
                                     'mailroom-' + 'b' * 24, reply_to='-')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(result['name'], sent['name'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.outbox(), [])
        self.assertTrue((channel.MAIL / 'inbox-archive' / sent['name']).exists())

    def test_forged_auto_id_from_a_trusted_sender_is_not_superseded(self):
        """Доверенный коллега не может заглушить своё письмо чужим префиксом."""
        name = self.letter(ident='mirror-' + 'f' * 64)
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_forged_auto_id')
        self.assertIn('журналом', result['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(list((channel.MAIL / 'inbox-archive').glob('*.md')), [])
        self.assertEqual(json.loads((channel.MAIL / channel.CLAIMS).read_text()), {})
        self.assertEqual(name, self.inbox()[0].name)

    def test_auto_id_with_substituted_content_is_refused(self):
        """Тот же идентификатор, другое содержимое — след не подтверждает."""
        sent = channel.notify_claude('Ответ автомата', 'Тело автоответа',
                                     'mailroom-' + 'c' * 24, reply_to='-')
        path = channel.MAIL / 'inbox' / sent['name']
        path.write_text(path.read_text() + '\nдописано после доставки\n')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_forged_auto_id')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    def test_auto_id_without_an_index_record_is_refused(self):
        """Журнала исходящих нет вовсе — префиксу верить не на чем."""
        self.letter(ident='mailroom-' + 'd' * 24)
        index = channel.MAIL / channel.OUTGOING_INDEX['inbox']
        self.assertFalse(index.exists())
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_forged_auto_id')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    def test_auto_letter_with_wrong_direction_is_refused(self):
        """След есть, но направление не то, которым пишут в этот ящик."""
        ident = 'mailroom-' + 'e' * 24
        name = self.letter(name='wrong-way.md', sender='claude', recipient='codex',
                           ident=ident)
        содержимое = (channel.MAIL / 'inbox' / name).read_text()
        channel.atomic_write(channel.MAIL / channel.OUTGOING_INDEX['inbox'],
                             json.dumps({ident: {'name': name, 'digest': 'x',
                                                 'content': содержимое}},
                                        ensure_ascii=False))
        # перечень нарочно допускает обе стороны: проверяется НЕ он
        cfg = dict(self.cfg, trusted_senders={'inbox': ['codex', 'claude']},
                   expected_recipients={'inbox': ['claude', 'codex']})
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_forged_auto_id')
        self.assertIn('направление', result['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    # --- B082, пункт 3: письмо без тела не считается готовым ---

    def test_letter_written_only_half_way_is_refused(self):
        """Шапка есть, тела нет: так ушло моё же B081."""
        with channel.locked():
            (channel.MAIL / 'inbox' / 'half.md').write_text(
                '---\nid: "codex-9"\nfrom: "codex"\nto: "claude"\n'
                'subject: "Обрыв"\nneeds_reply: true\n---\n\n')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_malformed_letter')
        self.assertIn('нет тела', result['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(list((channel.MAIL / 'inbox-archive').glob('*.md')), [])
        self.assertEqual(json.loads((channel.MAIL / channel.CLAIMS).read_text()), {})

    def test_letter_cut_inside_the_header_is_refused(self):
        with channel.locked():
            (channel.MAIL / 'inbox' / 'cut.md').write_text(
                '---\nid: "codex-10"\nfrom: "codex"\nto: "claude"\n')
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'refused_malformed_letter')
        self.assertIn('не закрыта', result['reason'])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)

    def test_a_complete_letter_still_passes_the_body_gate(self):
        self.letter()
        result = mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'replied')


if __name__ == '__main__':
    unittest.main()

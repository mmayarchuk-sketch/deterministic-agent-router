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


    # --- canary: проход можно ограничить поимённо ---

    def test_only_names_takes_exactly_the_named_letter(self):
        первое = self.letter(name='aaa-other.md', ident='codex-other')
        канарейка = self.letter(name='zzz-canary.md', ident='codex-canary')
        # без ограничения проход взял бы письмо по порядку приёмника
        порядок = [x['name'] for x in
                   channel.read_replies(mailbox='inbox')['messages']]
        self.assertEqual(порядок[0], первое)
        cfg = dict(self.cfg, only_names=[канарейка])
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(result['name'], канарейка)
        self.assertTrue((channel.MAIL / 'inbox' / первое).exists())
        self.assertTrue((channel.MAIL / 'inbox-archive' / канарейка).exists())
        self.assertEqual(len(self.calls), 1)

    def test_only_names_that_is_absent_touches_nothing(self):
        имя = self.letter()
        cfg = dict(self.cfg, only_names=['no-such-letter.md'])
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['scope'], 'only_names')
        self.assertEqual(self.calls, [])
        self.assertTrue((channel.MAIL / 'inbox' / имя).exists())
        self.assertEqual(self.outbox(), [])

    def test_only_names_cannot_point_outside_the_mailbox(self):
        self.letter()
        cfg = dict(self.cfg, only_names=['../mailroom.json', '/etc/hosts'])
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.inbox()), 1)


    def test_empty_only_names_means_nothing_not_everything(self):
        имя = self.letter()
        cfg = dict(self.cfg, only_names=[])
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['scope'], 'only_names')
        self.assertEqual(self.calls, [])
        self.assertTrue((channel.MAIL / 'inbox' / имя).exists())


    def test_a_repeated_shadow_pass_says_that_it_reused_the_candidate(self):
        self.letter()
        cfg = dict(self.cfg, mode='shadow')
        первый = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(первый['status'], 'shadowed')
        self.assertFalse(первый['reused'])
        self.assertTrue(первый['model_called'])
        второй = mailroom.run(cfg, classifier=self.classifier(), now=101)
        self.assertTrue(второй['reused'])
        self.assertFalse(второй['model_called'])
        self.assertEqual(len(self.calls), 1)

    def test_refresh_asks_the_model_again_instead_of_reusing(self):
        self.letter()
        cfg = dict(self.cfg, mode='shadow')
        mailroom.run(cfg, classifier=self.classifier(), now=100)
        другой = dict(REPLY, body='Ответ после правки памяти.')
        свежий = mailroom.run(dict(cfg, refresh=True),
                              classifier=self.classifier(другой), now=101)
        self.assertFalse(свежий['reused'])
        self.assertTrue(свежий['model_called'])
        self.assertEqual(len(self.calls), 2)
        кандидат = json.loads(Path(свежий['candidate']).read_text())
        self.assertIn('после правки памяти', кандидат['outcome']['body'])

    def test_production_reuse_of_a_fenced_answer_is_visible_too(self):
        self.letter()
        with patch.object(channel, 'complete_claim', side_effect=OSError('disk')):
            with self.assertRaises(mailroom.CompletionFailed):
                mailroom.run(self.cfg, classifier=self.classifier(), now=100)
        второй = mailroom.run(self.cfg, classifier=self.classifier(), now=101)
        self.assertEqual(второй['status'], 'replied')
        self.assertTrue(второй['reused'])
        self.assertFalse(второй['model_called'])


    def test_a_directory_of_records_is_read_as_trusted_memory(self):
        папка = self.root / 'records'
        папка.mkdir()
        (папка / 'D-CODEX-0013.json').write_text(
            json.dumps({'decision_id': 'D-CODEX-0013',
                        'decision': 'Проверять почту каждую минуту.'},
                       ensure_ascii=False), encoding='utf-8')
        текст = mailroom.прочитать_каталог(папка)
        self.assertIn('D-CODEX-0013', текст)
        self.assertIn('каждую минуту', текст)
        self.assertIn('1 из 1', текст)
        self.assertNotIn('НЕПОЛНЫЙ', текст)
        # запись, которую видно, становится известным полномочием
        self.assertIn('D-CODEX-0013', mailroom.authority_ids(текст))

    def test_a_truncated_corpus_says_so_instead_of_pretending(self):
        папка = self.root / 'big'
        папка.mkdir()
        for i in range(3):
            (папка / ('F-000%d.json' % i)).write_text('x' * 40, encoding='utf-8')
        текст = mailroom.прочитать_каталог(папка, предел=50)
        self.assertIn('НЕПОЛНЫЙ', текст)
        self.assertIn('1 из 3', текст)

    def test_the_pass_can_take_its_memory_from_a_directory(self):
        папка = self.root / 'records2'
        папка.mkdir()
        (папка / 'D-CODEX-0007.json').write_text(
            json.dumps({'decision_id': 'D-CODEX-0007',
                        'decision': 'Mailroom разрешён.'}, ensure_ascii=False),
            encoding='utf-8')
        self.letter()
        cfg = dict(self.cfg, memory_digests=[str(папка)])
        outcome = dict(REPLY, kind='authorized_action',
                       authority_ids=['D-CODEX-0007'])
        result = mailroom.run(cfg, classifier=self.classifier(outcome), now=100)
        self.assertEqual(result['status'], 'replied')
        self.assertIn('D-CODEX-0007', self.calls[0])


    # --- расписание: проход ограничен своей нитью ---

    def test_the_pass_takes_only_letters_from_our_own_thread(self):
        чужое = self.letter(name='aaa-foreign.md', ident='codex-a-1')
        with channel.locked():
            путь = channel.MAIL / 'inbox' / 'aaa-foreign.md'
            путь.write_text(путь.read_text().replace('reply_to: "-"',
                            'reply_to: "2026-09-22T00-00-00Z--chuzhoe--claude_a_i1.md"'))
        моё = self.letter(name='zzz-mine.md', ident='codex-b-1')
        with channel.locked():
            путь = channel.MAIL / 'inbox' / 'zzz-mine.md'
            путь.write_text(путь.read_text().replace('reply_to: "-"',
                            'reply_to: "2026-09-22T10-29-14Z--otchyot--b086.md"'))
        cfg = dict(self.cfg, only_reply_to_pattern=r'--b\d{3}\.md$')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'replied')
        self.assertEqual(result['name'], 'zzz-mine.md')
        self.assertTrue((channel.MAIL / 'inbox' / чужое).exists(),
                        'чужая нить не тронута')
        self.assertEqual(len(self.calls), 1)

    def test_no_letter_of_our_thread_means_idle_not_someone_elses(self):
        чужое = self.letter(name='aaa-foreign.md', ident='codex-a-1')
        with channel.locked():
            путь = channel.MAIL / 'inbox' / 'aaa-foreign.md'
            путь.write_text(путь.read_text().replace('reply_to: "-"',
                            'reply_to: "2026-09-22T00-00-00Z--chuzhoe--claude_a_i1.md"'))
        cfg = dict(self.cfg, only_reply_to_pattern=r'--b\d{3}\.md$')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['scope'], 'thread')
        self.assertEqual(self.calls, [])
        self.assertTrue((channel.MAIL / 'inbox' / чужое).exists())

    def test_headers_are_listed_without_reading_bodies(self):
        self.letter(name='one.md', ident='codex-1')
        with channel.locked():
            (channel.MAIL / 'inbox' / 'huge.md').write_text(
                '---\nid: "codex-2"\nfrom: "codex"\nto: "claude"\n'
                'subject: "Большое"\nreply_to: "--b086.md"\n---\n\n' + 'x' * 300000)
        записи = channel.list_headers(mailbox='inbox')
        имена = [z['name'] for z in записи]
        self.assertIn('huge.md', имена)
        большое = [z for z in записи if z['name'] == 'huge.md'][0]
        self.assertLess(len(большое['head']), 5000, 'тело не втягивается')
        self.assertIn('--b086.md', большое['head'])


    def test_a_lookalike_thread_slug_is_not_our_thread(self):
        """Ровно тот случай 22.09: чужая тема «B0: …» попала под подстроку."""
        чужое = self.letter(name='foreign-b0.md', ident='codex-a-9')
        with channel.locked():
            путь = channel.MAIL / 'inbox' / 'foreign-b0.md'
            путь.write_text(путь.read_text().replace(
                'reply_to: "-"',
                'reply_to: "2026-09-21T15-46-21Z--b0-scenariy-1--mail-206.md"'))
        cfg = dict(self.cfg, only_reply_to_pattern=r'--b\d{3}\.md$')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['scope'], 'thread')
        self.assertEqual(self.calls, [])
        self.assertTrue((channel.MAIL / 'inbox' / чужое).exists())
        self.assertEqual(self.outbox(), [])

    def test_a_letter_that_asks_for_no_reply_is_left_where_it_lies(self):
        имя = self.letter(name='no-reply.md', ident='codex-b-2')
        with channel.locked():
            путь = channel.MAIL / 'inbox' / 'no-reply.md'
            путь.write_text(путь.read_text()
                            .replace('reply_to: "-"', 'reply_to: "x--b086.md"')
                            .replace('needs_reply: true', 'needs_reply: false'))
        cfg = dict(self.cfg, only_reply_to_pattern=r'--b\d{3}\.md$')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(self.calls, [])
        self.assertTrue((channel.MAIL / 'inbox' / имя).exists())
        self.assertEqual(list((channel.MAIL / 'inbox-archive').glob('*.md')), [])


    # --- общий ящик: чужая нить разбирается, но остаётся на месте ---

    def чужое_письмо(self, name='foreign.md', ident='codex-a-5', создано='2026-09-22T10:00:00Z'):
        имя = self.letter(name=name, ident=ident)
        with channel.locked():
            путь = channel.MAIL / 'inbox' / name
            путь.write_text(путь.read_text()
                            .replace('reply_to: "-"', 'reply_to: "x--claude_a_i7.md"')
                            .replace('created_utc: "2026-09-22T06:00:00Z"',
                                     'created_utc: "%s"' % создано))
        return имя

    def test_a_foreign_thread_is_answered_but_never_taken_out_of_the_mailbox(self):
        имя = self.чужое_письмо()
        cfg = dict(self.cfg, own_thread_pattern=r'--b\d{3}\.md$')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'replied')
        self.assertFalse(result['own_thread'])
        self.assertFalse(result['archived'])
        self.assertTrue((channel.MAIL / 'inbox' / имя).exists(),
                        'чужое письмо обязано остаться там, где его ищет адресат')
        self.assertEqual(list((channel.MAIL / 'inbox-archive').glob('*.md')), [])
        квитанция = json.loads(Path(result['receipt']).read_text())
        self.assertEqual(квитанция['state'], 'outcome_durable_in_place')
        self.assertTrue(квитанция['left_in_mailbox'])
        self.assertEqual(len(self.outbox()), 1)

    def test_a_letter_already_receipted_is_not_taken_again(self):
        self.чужое_письмо()
        cfg = dict(self.cfg, own_thread_pattern=r'--b\d{3}\.md$')
        mailroom.run(cfg, classifier=self.classifier(), now=100)
        второй = mailroom.run(cfg, classifier=self.classifier(), now=101)
        self.assertEqual(второй['status'], 'idle')
        self.assertEqual(второй['scope'], 'mailbox-shared')
        self.assertEqual(len(self.calls), 1, 'модель второй раз не поднимается')
        self.assertEqual(len(self.outbox()), 1)

    def test_our_own_thread_is_still_archived_normally(self):
        имя = self.letter(name='mine.md', ident='codex-b-7')
        with channel.locked():
            путь = channel.MAIL / 'inbox' / 'mine.md'
            путь.write_text(путь.read_text().replace('reply_to: "-"',
                                                     'reply_to: "x--b086.md"'))
        cfg = dict(self.cfg, own_thread_pattern=r'--b\d{3}\.md$')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertTrue(result['own_thread'])
        self.assertTrue(result['archived'])
        self.assertFalse((channel.MAIL / 'inbox' / имя).exists())
        self.assertTrue((channel.MAIL / 'inbox-archive' / имя).exists())

    def test_the_old_backlog_is_left_alone_until_asked_for(self):
        старое = self.чужое_письмо(name='old.md', ident='codex-a-6',
                                   создано='2026-09-20T10:00:00Z')
        cfg = dict(self.cfg, own_thread_pattern=r'--b\d{3}\.md$',
                   process_from_utc='2026-09-22T00:00:00Z')
        result = mailroom.run(cfg, classifier=self.classifier(), now=100)
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(self.calls, [])
        self.assertTrue((channel.MAIL / 'inbox' / старое).exists())
        # отсечку убрали — то же письмо берётся
        свежий = mailroom.run(dict(cfg, process_from_utc=''),
                              classifier=self.classifier(), now=101)
        self.assertEqual(свежий['status'], 'replied')


if __name__ == '__main__':
    unittest.main()

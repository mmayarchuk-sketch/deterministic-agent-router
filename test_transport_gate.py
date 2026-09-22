"""Ворота транспорта: письмо берётся только по доказанному сигналу адресату.

Условия приёмки — из письма Астры `codex-b-b101-final-transport-gate-20260922-v1`
(вариант В без обхода по времени).
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mail_channel as channel
import mailroom
import transport_gate

НИТЬ = '01a0gate-0000-0000-0000-000000000001'


class ВоротаTests(unittest.TestCase):
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
        self.дом = self.root / 'codex-home'
        (self.дом / 'sessions').mkdir(parents=True)
        (self.дом / 'archived_sessions').mkdir(parents=True)
        self.оживить(НИТЬ)
        self.состояние = self.root / 'watcher-state'
        self.состояние.mkdir()
        self.записать_состояние({'batches': []})
        self.настройка = self.root / 'outbox-watch.json'
        self.настройка.write_text(json.dumps(
            {'thread_id': НИТЬ, 'state_directory': str(self.состояние),
             'codex_home': str(self.дом)}, ensure_ascii=False))
        self.cfg = {'actor': 'codex-mailroom', 'state_directory': str(self.root / 'state'),
                    'owner_outbox': str(channel.MAIL / 'owner-outbox'),
                    'delivery_gate': {'watcher_config': str(self.настройка),
                                      'codex_home': str(self.дом)}}
        self.всплытия = []

    # --- стенд ---

    def оживить(self, нить):
        (self.дом / 'sessions' / ('rollout-%s.jsonl' % нить)).write_text('{}')

    def похоронить(self, нить):
        (self.дом / 'sessions' / ('rollout-%s.jsonl' % нить)).unlink(missing_ok=True)
        (self.дом / 'archived_sessions' / ('rollout-%s.jsonl' % нить)).write_text('{}')

    def записать_состояние(self, d):
        (self.состояние / 'watch.json').write_text(json.dumps(d, ensure_ascii=False))

    def письмо(self, name='one.md', text='Вопрос'):
        with channel.locked():
            (channel.MAIL / 'outbox' / name).write_text(text)
        return name, channel._digest(channel.MAIL / 'outbox' / name)

    def сигнал(self, name, sha, *, status='queued', thread=None):
        d = json.loads((self.состояние / 'watch.json').read_text())
        d['batches'].append({'id': 'b%d' % len(d['batches']), 'status': status,
                             'thread_id': thread or НИТЬ, 'attempt_id': 'a1',
                             'files': [{'name': name, 'sha256': sha}]})
        self.записать_состояние(d)

    def звонарь(self, *a, **kw):
        self.всплытия.append(a[0] if a else None)
        class Ответ:
            returncode = 0
            stderr = ''
        return Ответ()

    # --- точная версия ---

    def test_signal_opens_the_gate_only_for_the_exact_version(self):
        имя, sha = self.письмо()
        self.assertFalse(transport_gate.открыты(имя, sha, self.cfg)['ok'])
        self.сигнал(имя, sha)
        открыто = transport_gate.открыты(имя, sha, self.cfg)
        self.assertTrue(открыто['ok'])
        self.assertEqual(открыто['thread_id'], НИТЬ)
        self.assertTrue(открыто['batch'] and открыто['attempt'])
        # другой sha того же имени — другая версия, допуска нет
        другой = transport_gate.открыты(имя, 'f' * 64, self.cfg)
        self.assertFalse(другой['ok'])
        self.assertEqual(другой['reason'], 'version_not_signalled')
        # другое имя — вовсе без сигнала
        self.assertEqual(transport_gate.открыты('другое.md', sha, self.cfg)['reason'],
                         'no_signal')

    def test_only_a_successful_queue_counts_as_delivered(self):
        имя, sha = self.письмо()
        for статус in ('pending', 'sending', 'retry', 'uncertain', 'obsolete'):
            self.записать_состояние({'batches': []})
            self.сигнал(имя, sha, status=статус)
            ответ = transport_gate.открыты(имя, sha, self.cfg)
            self.assertFalse(ответ['ok'], 'статус %s не доставка' % статус)
            self.assertEqual(ответ['reason'], 'version_not_signalled')

    def test_a_signal_into_another_thread_does_not_count(self):
        имя, sha = self.письмо()
        self.оживить('01a0gate-0000-0000-0000-000000000002')
        self.сигнал(имя, sha, thread='01a0gate-0000-0000-0000-000000000002')
        self.assertFalse(transport_gate.открыты(имя, sha, self.cfg)['ok'])

    # --- состояние доставщика ---

    def test_missing_or_corrupt_state_fails_closed(self):
        имя, sha = self.письмо()
        (self.состояние / 'watch.json').unlink()
        self.assertEqual(transport_gate.открыты(имя, sha, self.cfg)['reason'], 'no_state')
        (self.состояние / 'watch.json').write_text('{это не json')
        self.assertEqual(transport_gate.открыты(имя, sha, self.cfg)['reason'], 'corrupt_state')
        (self.состояние / 'watch.json').write_text('{"batches": "не список"}')
        self.assertEqual(transport_gate.открыты(имя, sha, self.cfg)['reason'], 'corrupt_state')
        (self.состояние / 'watch.json').write_text('{"batches": [1, "мусор", null]}')
        self.assertFalse(transport_gate.открыты(имя, sha, self.cfg)['ok'])

    def test_archived_thread_fails_closed(self):
        имя, sha = self.письмо()
        self.сигнал(имя, sha)
        self.похоронить(НИТЬ)
        ответ = transport_gate.открыты(имя, sha, self.cfg)
        self.assertFalse(ответ['ok'])
        self.assertEqual(ответ['reason'], 'thread_archived')

    # --- тревога: три прохода, один инцидент, два пути ---

    def test_three_passes_raise_one_incident_on_two_paths(self):
        имя, sha = self.письмо()
        self.похоронить(НИТЬ)
        инциденты = []
        for момент in (100, 160, 220):
            ответ = transport_gate.открыты(имя, sha, self.cfg)
            учёт = transport_gate.проход(self.cfg, ответ, name=имя, sha256=sha,
                                         now=момент, runner=self.звонарь)
            инциденты.append(учёт['incident'])
        self.assertIsNone(инциденты[0])
        self.assertIsNone(инциденты[1])
        инцидент = инциденты[2]
        self.assertIsNotNone(инцидент, 'после трёх подряд объявляем аварию')
        self.assertTrue(инцидент['durable']['ok'], 'долговечный путь')
        self.assertTrue(инцидент['native']['ok'], 'всплывание')
        self.assertEqual(len(self.всплытия), 1)
        файлы = list((channel.MAIL / 'owner-outbox').glob('transport-*.json'))
        self.assertEqual(len(файлы), 1)
        запись = json.loads(файлы[0].read_text())
        self.assertEqual(запись['kind'], 'transport_failed')
        self.assertTrue(запись['for_human'])
        # ещё проходы в пределах получаса не плодят ни записи, ни всплытия
        for момент in (280, 340, 400):
            transport_gate.проход(self.cfg, transport_gate.открыты(имя, sha, self.cfg),
                                  name=имя, sha256=sha, now=момент, runner=self.звонарь)
        self.assertEqual(len(list((channel.MAIL / 'owner-outbox').glob('transport-*.json'))), 1)
        self.assertEqual(len(self.всплытия), 1)

    def test_one_broken_path_does_not_cancel_the_other(self):
        имя, sha = self.письмо()
        self.похоронить(НИТЬ)
        def сломанный_звонарь(*a, **kw):
            raise OSError('osascript недоступен')
        for момент in (100, 160, 220):
            учёт = transport_gate.проход(self.cfg, transport_gate.открыты(имя, sha, self.cfg),
                                         name=имя, sha256=sha, now=момент,
                                         runner=сломанный_звонарь)
        self.assertFalse(учёт['incident']['native']['ok'])
        self.assertTrue(учёт['incident']['durable']['ok'],
                        'отказ всплывания не отменяет долговечную запись')
        self.assertEqual(len(list((channel.MAIL / 'owner-outbox').glob('transport-*.json'))), 1)

    def test_recovery_closes_the_incident_only_after_a_real_signal(self):
        имя, sha = self.письмо()
        self.похоронить(НИТЬ)
        for момент in (100, 160, 220):
            transport_gate.проход(self.cfg, transport_gate.открыты(имя, sha, self.cfg),
                                  name=имя, sha256=sha, now=момент, runner=self.звонарь)
        # нить ожила, но сигнала об этой версии всё ещё нет — ворота закрыты
        self.оживить(НИТЬ)
        (self.дом / 'archived_sessions' / ('rollout-%s.jsonl' % НИТЬ)).unlink()
        self.assertFalse(transport_gate.открыты(имя, sha, self.cfg)['ok'])
        # и только после пересланного сигнала точной версии — открыты
        self.сигнал(имя, sha)
        ответ = transport_gate.открыты(имя, sha, self.cfg)
        self.assertTrue(ответ['ok'])
        учёт = transport_gate.проход(self.cfg, ответ, now=300, runner=self.звонарь)
        self.assertTrue(учёт['closed'], 'инцидент закрывается фактом сигнала')

    # --- поведение прохода целиком ---

    def test_mailroom_does_not_claim_without_a_signal(self):
        имя, sha = self.письмо()
        cfg = dict(self.cfg, eligible={'names': [имя], 'reply_to': []},
                   memory_digests=[], policy=str(mailroom.POLICY))
        вызовы = []
        итог = mailroom.run(cfg, classifier=lambda p: вызовы.append(p), now=100)
        self.assertEqual(итог['status'], 'blocked_transport')
        self.assertEqual(итог['reason'], 'no_signal')
        self.assertEqual(вызовы, [], 'модель не поднимается')
        self.assertTrue((channel.MAIL / 'outbox' / имя).exists(), 'письмо на месте')
        захваты = channel.MAIL / channel.CLAIMS
        self.assertTrue(not захваты.exists() or json.loads(захваты.read_text()) == {},
                        'захвата нет')

    def test_mailroom_proceeds_once_the_signal_exists(self):
        имя, sha = self.письмо()
        self.сигнал(имя, sha)
        исход = {'action': 'reply', 'kind': 'technical_analysis', 'subject': 'Ответ',
                 'body': 'Разбор.', 'authority_ids': [], 'requires_external_action': False,
                 'reason': 'Только техника'}
        cfg = dict(self.cfg, eligible={'names': [имя], 'reply_to': []},
                   memory_digests=[], policy=str(mailroom.POLICY))
        итог = mailroom.run(cfg, classifier=lambda _: исход, now=100)
        self.assertEqual(итог['status'], 'replied')


if __name__ == '__main__':
    unittest.main()

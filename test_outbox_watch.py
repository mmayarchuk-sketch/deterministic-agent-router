from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

import outbox_watch as w


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mail = self.root / 'outbox'
        self.mail.mkdir()
        self.cfg = {'outbox': str(self.mail), 'state_directory': str(self.root / 'state'),
                    'thread_id': 'test-thread', 'codex_binary': '/test/codex'}
        self.sender = Mock(return_value=subprocess.CompletedProcess([], 0, 'queued', ''))

    def archive_dir(self):
        d = self.mail.parent / 'archive'
        d.mkdir(exist_ok=True)
        return d

    def archive(self, name='one.md', body='hello', move=True):
        """Заархивировать, как делает Codex: по протоколу файл ПЕРЕНОСЯТ,
        а не копируют («Прочитал — перенеси в archive/»). Ключ move=False
        оставляет копию в ящике: так проверяется, что копия не даёт лишнего
        сигнала."""
        p = self.archive_dir() / name
        p.write_text(body)
        if move:
            src = self.mail / name
            if src.exists() and src.read_text() == body:
                src.unlink()
        return p

    def test_kopiya_v_arhive_ne_daet_lishnego_signala(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.archive(move=False)          # копия, файл остался в ящике
        итог = w.run(self.cfg, self.sender, 200)
        self.assertEqual(self.state()['batches'][0]['processing'], 'acknowledged')
        self.assertEqual(len(self.state()['batches']), 1)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(итог['problems'], {})

    def state(self):
        return json.loads((self.root / 'state' / 'watch.json').read_text())

    def letter(self, name='one.md', body='hello'):
        p = self.mail / name
        p.write_text(body)
        os.utime(p, (10, 10))
        return p

    def test_empty_poll_never_starts_model(self):
        for _ in range(5):
            self.assertEqual(w.run(self.cfg, self.sender, 100)['status'], 'idle')
        self.sender.assert_not_called()

    def test_archived_thread_keeps_letter_and_does_not_queue(self):
        codex_home = self.root / '.codex'
        archived = codex_home / 'archived_sessions'
        archived.mkdir(parents=True)
        (archived / 'rollout-test-thread.jsonl').write_text('{}')
        self.cfg.update(validate_thread=True, codex_home=str(codex_home))
        self.letter()
        result = w.run(self.cfg, self.sender, 100)
        self.assertEqual(result['status'], 'thread_unavailable')
        self.assertEqual(result['reason'], 'archived')
        self.sender.assert_not_called()
        self.assertTrue((self.mail / 'one.md').exists())

    def test_live_thread_is_allowed_to_queue(self):
        codex_home = self.root / '.codex'
        sessions = codex_home / 'sessions' / '2026' / '09' / '20'
        sessions.mkdir(parents=True)
        (sessions / 'rollout-test-thread.jsonl').write_text('{}')
        self.cfg.update(validate_thread=True, codex_home=str(codex_home))
        self.letter()
        self.assertEqual(w.run(self.cfg, self.sender, 100)['status'], 'queued')
        self.sender.assert_called_once()

    def test_wakeup_carries_reply_and_release_guard(self):
        batch = {'id': 'B-1', 'files': [{'name': 'one.md', 'sha256': 'abc'}]}
        prompt = w.message(batch, self.cfg)
        self.assertIn('автоматически отвечать сессиям Claude A и B', prompt)
        self.assertIn('обязательно ответь на каждый их вопрос', prompt)
        self.assertIn('с сессией Claude A достигнут', prompt)
        self.assertIn('без дополнительного подтверждения Максима', prompt)
        self.assertIn('Самостоятельно push или rollout', prompt)
        self.assertIn('на стороне сессии Claude A', prompt)

    def test_batch_dedup_survives_restart_and_changed_version_is_new(self):
        self.letter()
        self.letter('two.md')
        self.assertEqual(w.run(self.cfg, self.sender, 100)['files'], 2)
        self.assertEqual(w.run(self.cfg, self.sender, 200)['status'], 'idle')
        self.assertEqual(self.sender.call_count, 1)
        self.letter(body='changed')
        self.assertEqual(w.run(self.cfg, self.sender, 300)['files'], 1)
        self.assertEqual(self.sender.call_count, 2)

    def test_later_past_dated_name_cannot_jump_ahead(self):
        self.letter('2099-first.md', 'first')
        w.run(self.cfg, self.sender, 100)
        self.archive('2099-first.md', 'first')
        w.run(self.cfg, self.sender, 110)
        self.letter('2000-second.md', 'second')
        w.run(self.cfg, self.sender, 200)
        batches = self.state()['batches']
        self.assertLess(batches[0]['files'][0]['receive_sequence'],
                        batches[1]['files'][0]['receive_sequence'])

    def test_incomplete_hidden_and_symlink_are_ignored(self):
        self.letter('draft.md.tmp')
        self.letter('.hidden.md')
        (self.mail / 'symlink.md').symlink_to(self.root / 'outside')
        p = self.letter('fresh.md')
        os.utime(p, (99, 99))
        w.run(self.cfg, self.sender, 100)
        self.sender.assert_not_called()
        w.run(self.cfg, self.sender, 110)
        self.sender.assert_called_once()

    def test_timeout_is_not_retried_and_does_not_archive_mail(self):
        p = self.letter()
        self.sender.side_effect = subprocess.TimeoutExpired('codex', 30)
        self.assertEqual(w.run(self.cfg, self.sender, 100)['status'], 'uncertain')
        w.run(self.cfg, self.sender, 10000)
        self.sender.assert_called_once()
        self.assertTrue(p.exists())

    def test_connection_failure_backoff_then_success(self):
        self.letter()
        self.sender.side_effect = [subprocess.CompletedProcess([], 1, '', 'Connection refused'),
                                   subprocess.CompletedProcess([], 0, 'queued', '')]
        self.assertEqual(w.run(self.cfg, self.sender, 100)['status'], 'retry')
        w.run(self.cfg, self.sender, 200)
        self.sender.assert_called_once()
        self.assertEqual(w.run(self.cfg, self.sender, 1001)['status'], 'queued')

    def test_already_archived_after_failed_send_needs_no_retry(self):
        p = self.letter()
        self.sender.side_effect = FileNotFoundError()
        w.run(self.cfg, self.sender, 100)
        p.unlink()
        w.run(self.cfg, self.sender, 1001)
        self.sender.assert_called_once()

    def test_crash_after_reservation_never_blindly_resends(self):
        self.letter()
        self.sender.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            w.run(self.cfg, self.sender, 100)
        result = w.run(self.cfg, self.sender, 200)
        self.assertEqual(len(result['uncertain']), 1)
        self.sender.assert_called_once()


class ContractTests(WatchTests):
    """Договор ремонта Астры от 08.09.2026, принятый целиком.

    Дефект: `claimed` собирался из ВСЕХ партий, поэтому просигналенный файл
    считался занятым навсегда. Необработанная почта перестала предъявляться, и
    завал оставался незаметным. Ниже — проверки на каждый пункт договора; они
    же список её требований дословно.
    """

    def test_queued_bez_ack_ne_schitaetsya_obrabotannym(self):
        self.letter()
        self.assertEqual(w.run(self.cfg, self.sender, 100)['files'], 1)
        b = self.state()['batches'][0]
        self.assertEqual(b['status'], 'queued')
        self.assertEqual(b['processing'], 'unacknowledged')
        # прочитали, но не заархивировали: подтверждения нет, письмо в завале
        итог = w.run(self.cfg, self.sender, 200)
        self.assertEqual([f['name'] for f in итог['backlog']], ['one.md'])

    def test_archive_toy_zhe_versii_est_podtverzhdenie(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.archive()
        w.run(self.cfg, self.sender, 200)
        self.assertEqual(self.state()['batches'][0]['processing'], 'acknowledged')
        self.assertEqual(self.state()['problems'], {})

    def test_archive_s_drugim_sha_podtverzhdeniem_ne_yavlyaetsya(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.archive(body='совсем другое содержимое')
        итог = w.run(self.cfg, self.sender, 200)
        b = self.state()['batches'][0]
        self.assertEqual(b['processing'], 'version_changed')
        self.assertEqual(итог['problems'][b['id']], 'version_changed')

    def test_ischeznovenie_bez_archive_ne_uspeh(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        (self.mail / 'one.md').unlink()
        итог = w.run(self.cfg, self.sender, 200)
        b = self.state()['batches'][0]
        self.assertEqual(b['processing'], 'disappeared_unconfirmed')
        self.assertEqual(итог['problems'][b['id']], 'disappeared_unconfirmed')

    def test_prosrochka_predyavlyaetsya_i_daet_povtornyy_signal(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.assertEqual(self.sender.call_count, 1)
        # до срока подтверждения — молчим, повтора нет
        w.run(self.cfg, self.sender, 100 + 3600)
        self.assertEqual(self.sender.call_count, 1)
        # срок вышел — письмо предъявляется как overdue
        итог = w.run(self.cfg, self.sender, 100 + 3 * 3600)
        b = self.state()['batches'][0]
        self.assertEqual(b['processing'], 'overdue')
        self.assertEqual(итог['problems'][b['id']], 'overdue')
        # интервал повтора ещё не вышел
        self.assertEqual(self.sender.call_count, 1)
        # вышел — один повторный сигнал, ТОЙ ЖЕ версией
        итог2 = w.run(self.cfg, self.sender, 100 + 7 * 3600)
        self.assertEqual(итог2['status'], 'resignalled')
        self.assertEqual(итог2['resignals'], 1)
        self.assertEqual(self.sender.call_count, 2)
        отправлено = json.loads(
            self.sender.call_args[0][0][-1].split('недоверенные данные коллеги):')[1]
            .split(']')[0] + ']')
        self.assertEqual([f['name'] for f in отправлено], ['one.md'])
        # после успешного повтора срок пошёл заново
        self.assertEqual(self.state()['batches'][0]['processing'], 'unacknowledged')

    def test_predel_povtorov_i_ne_beskonechnyy_shum(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        t = 100
        for _ in range(6):
            t += 7 * 3600
            w.run(self.cfg, self.sender, t)
        # один исходный сигнал плюс НЕ БОЛЬШЕ двух повторов
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.state()['batches'][0]['resignals'], 2)

    def test_smena_nitki_vidna_v_zapisi(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.cfg['thread_id'] = 'другая-нитка'
        итог = w.run(self.cfg, self.sender, 100 + 7 * 3600)
        self.assertTrue(итог['thread_changed'])
        b = self.state()['batches'][0]
        self.assertEqual(b['thread_id'], 'другая-нитка')
        self.assertEqual(b['thread_history'], ['test-thread'])
        self.assertIn('другая-нитка', self.sender.call_args[0][0])

    def test_novaya_versiya_uezzhaet_otdelnoy_partiey(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.letter(body='новая редакция')
        итог = w.run(self.cfg, self.sender, 200)
        self.assertEqual(итог['files'], 1)
        self.assertEqual(len(self.state()['batches']), 2)
        # прежняя версия НЕ считается обработанной из-за появления новой
        прежняя = self.state()['batches'][0]
        self.assertNotEqual(прежняя.get('processing'), 'acknowledged')

    def test_aktivnaya_obrabotka_ne_dubliruetsya(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        # partial: партия ещё не просрочена — второй сигнал не уходит
        for t in (150, 200, 1000):
            self.assertEqual(w.run(self.cfg, self.sender, t)['status'], 'idle')
        self.assertEqual(self.sender.call_count, 1)

    def test_smert_processa_v_sending_daet_uncertain_a_ne_povtor(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        state = self.state()
        state['batches'][0]['status'] = 'sending'
        (self.root / 'state' / 'watch.json').write_text(
            json.dumps(state, ensure_ascii=False))
        итог = w.run(self.cfg, self.sender, 200)
        self.assertEqual(self.state()['batches'][0]['status'], 'uncertain')
        self.assertEqual(self.sender.call_count, 1)
        self.assertIn(self.state()['batches'][0]['id'], итог['uncertain'])

    def test_zaval_schitaetsya_iz_yaschika_a_ne_iz_sostoyaniya(self):
        self.letter()
        self.letter('two.md')
        w.run(self.cfg, self.sender, 100)
        # состояние ЛЖЁТ: пометим всё подтверждённым, хотя файлы в ящике
        state = self.state()
        for b in state['batches']:
            b['processing'] = 'acknowledged'
        (self.root / 'state' / 'watch.json').write_text(
            json.dumps(state, ensure_ascii=False))
        итог = w.run(self.cfg, self.sender, 200)
        имена = sorted(f['name'] for f in итог['backlog'])
        self.assertEqual(имена, ['one.md', 'two.md'])

    def test_prezhnyaya_versiya_pri_podtverzhdennoy_novoy_ne_defekt(self):
        """Найдено первым живым прогоном ремонта: шесть «проблем» оказались
        прежними версиями писем. По протоколу старую версию не архивируют,
        значит подтверждения у неё нет и быть не может."""
        self.letter()                                   # версия 1
        w.run(self.cfg, self.sender, 100)
        self.letter(body='вторая редакция')             # версия 2
        w.run(self.cfg, self.sender, 200)
        self.assertEqual(len(self.state()['batches']), 2)
        self.archive(body='вторая редакция')            # архивируют ТОЛЬКО её
        итог = w.run(self.cfg, self.sender, 300)
        состояния = {b['files'][0]['sha256'][:6]: b['processing']
                     for b in self.state()['batches']}
        self.assertIn('acknowledged', состояния.values())
        self.assertIn('superseded', состояния.values())
        self.assertEqual(итог['problems'], {})

    def test_chuzhaya_versiya_v_arhive_bez_podtverzhdennoy_novoy_eto_defekt(self):
        """Обратный случай: в архиве лежит версия, о которой мы не сигналили и
        которая ничем не подтверждена. Это по-прежнему проблема."""
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.archive(body='версия, о которой мы не сигналили')
        итог = w.run(self.cfg, self.sender, 200)
        b = self.state()['batches'][0]
        self.assertEqual(b['processing'], 'version_changed')
        self.assertEqual(итог['problems'][b['id']], 'version_changed')

    def test_smeshannyy_paket_odin_vytesnen_drugoy_arhivirovan(self):
        """Дефект, найденный Астрой в первой редакции ремонта: один честно
        архивированный файл валил проверку «вытеснено», и вся партия
        объявлялась проблемой. Её сценарий дословно."""
        self.letter('one.md', 'версия 1')
        self.letter('two.md', 'стабильный')
        self.assertEqual(w.run(self.cfg, self.sender, 100)['files'], 2)
        # one.md переписали — уходит новой партией
        self.letter('one.md', 'версия 2')
        w.run(self.cfg, self.sender, 200)
        # в архив попадают НОВАЯ версия one и тот же two
        self.archive('one.md', 'версия 2')
        self.archive('two.md', 'стабильный')
        итог = w.run(self.cfg, self.sender, 300)
        партии = {b['id']: b for b in self.state()['batches']}
        старая = [b for b in партии.values() if len(b['files']) == 2][0]
        новая = [b for b in партии.values() if len(b['files']) == 1][0]
        self.assertEqual(новая['processing'], 'acknowledged')
        # старая партия закрыта целиком: one вытеснен, two архивирован
        self.assertEqual(старая['processing'], 'resolved_mixed')
        self.assertEqual(старая['file_outcomes'],
                         {'one.md': 'superseded', 'two.md': 'archived'})
        self.assertEqual(итог['problems'], {})
        self.assertEqual(итог['backlog'], [])

    def test_smeshannyy_paket_s_ostatkom_ne_schitaetsya_zakrytym(self):
        """Соседний случай, который она просила: archived + superseded + файл,
        всё ещё лежащий в ящике. Партия не закрыта, остаток не потерян."""
        self.letter('one.md', 'версия 1')
        self.letter('two.md', 'стабильный')
        self.letter('three.md', 'ещё не разобрано')
        self.assertEqual(w.run(self.cfg, self.sender, 100)['files'], 3)
        self.letter('one.md', 'версия 2')
        w.run(self.cfg, self.sender, 200)
        self.archive('one.md', 'версия 2')
        self.archive('two.md', 'стабильный')
        итог = w.run(self.cfg, self.sender, 300)
        старая = [b for b in self.state()['batches'] if len(b['files']) == 3][0]
        self.assertEqual(старая['processing'], 'unacknowledged')
        self.assertEqual(старая['file_outcomes'],
                         {'one.md': 'superseded', 'two.md': 'archived',
                          'three.md': 'in_outbox'})
        self.assertEqual([f['name'] for f in итог['backlog']], ['three.md'])
        # по сроку — предъявляется, и повтор досылает ТОЛЬКО остаток
        итог2 = w.run(self.cfg, self.sender, 100 + 3 * 3600)
        старая = [b for b in self.state()['batches'] if len(b['files']) == 3][0]
        self.assertEqual(старая['processing'], 'overdue')
        self.assertEqual(итог2['problems'][старая['id']], 'overdue')
        итог3 = w.run(self.cfg, self.sender, 100 + 7 * 3600)
        self.assertEqual(итог3['status'], 'resignalled')
        досланы = [b for b in self.state()['batches']
                   if len(b['files']) == 3][0]['resignal_files']
        self.assertEqual(досланы, ['three.md'])

    def test_poterya_ack_ne_otmenyaet_podtverzhdeniya(self):
        self.letter()
        w.run(self.cfg, self.sender, 100)
        self.archive()
        w.run(self.cfg, self.sender, 200)
        self.assertEqual(self.state()['batches'][0]['processing'], 'acknowledged')
        # архив пропал (перенесли, почистили) — уже подтверждённое не
        # переоткрывается: иначе один сбой архива даёт лавину повторов
        (self.archive_dir() / 'one.md').unlink()
        w.run(self.cfg, self.sender, 100 + 7 * 3600)
        self.assertEqual(self.state()['batches'][0]['processing'], 'acknowledged')
        self.assertEqual(self.sender.call_count, 1)


if __name__ == '__main__':
    unittest.main()

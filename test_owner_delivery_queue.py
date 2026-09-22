# -*- coding: utf-8 -*-
"""Недоставленная эскалация обязана остаться доставляемой.

Что случилось 21.09.2026. Нить владельца ушла в архив, семь эскалаций
остались в состоянии `pending`. Но их файлы к тому времени уже перенесли в
архив вызовом `acknowledge_owner_escalation` — который подтверждает «я
предъявил владельцу», НИЧЕГО не зная о том, была ли доставка. Кандидаты
доставщик берёт только из папки очереди, файлов там больше нет, и семь
сообщений остались навсегда в состоянии «ожидает доставки», которое ничем
не отличается от потерянных.

Здесь два требования, и оба проверяются поломкой:
  * подтвердить недоставленное нельзя — отказ, файл остаётся в очереди;
  * запись `pending`, чей файл почему-то оказался в архиве, видна как
    требующая разбора, а не как «ожидает».
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ЗДЕСЬ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ЗДЕСЬ)

import mail_channel  # noqa: E402
import owner_watch  # noqa: E402


def сумма(путь):
    return hashlib.sha256(Path(путь).read_bytes()).hexdigest()


class ОчередьВладельцаTest(unittest.TestCase):

    def setUp(self):
        self.дом = Path(tempfile.mkdtemp(prefix='test-owner-queue-'))
        self.почта = self.дом / 'mail'
        (self.почта / 'owner-outbox').mkdir(parents=True)
        (self.почта / 'owner-archive').mkdir(parents=True)
        (self.дом / 'state' / 'mailroom').mkdir(parents=True)
        self.прежняя_почта = mail_channel.MAIL
        mail_channel.MAIL = self.почта

        self.письмо = self.почта / 'owner-outbox' / 'mailroom-test0001.json'
        self.письмо.write_text(json.dumps({'action': 'escalate',
                                           'subject': 'проба'}, ensure_ascii=False),
                               encoding='utf-8')
        self.sha = сумма(self.письмо)
        self.cfg = {
            'owner_outbox': str(self.почта / 'owner-outbox'),
            'owner_archive': str(self.почта / 'owner-archive'),
            'state_directory': str(self.дом / 'state' / 'mailroom'),
            'validate_thread': True,
            'thread_id': 'нет-такой-нити',
            'codex_home': str(self.дом / 'codex'),
            'codex_binary': '/bin/echo',
        }
        # Файл старше пяти секунд, иначе доставщик его намеренно пропускает.
        древность = os.stat(self.письмо).st_mtime - 60
        os.utime(self.письмо, (древность, древность))

    def tearDown(self):
        mail_channel.MAIL = self.прежняя_почта
        shutil.rmtree(self.дом, ignore_errors=True)

    def _состояние(self):
        п = Path(self.cfg['state_directory']) / 'owner-delivery.json'
        return json.loads(п.read_text())['deliveries'] if п.exists() else {}

    def test_недоступная_нить_оставляет_письмо_в_очереди(self):
        итог = owner_watch.run(self.cfg)
        self.assertEqual('thread_unavailable', итог['status'])
        self.assertTrue(self.письмо.exists(), 'письмо обязано остаться в очереди')
        состояния = {v['state'] for v in self._состояние().values()}
        self.assertEqual({'pending'}, состояния)

    def test_подтвердить_недоставленное_нельзя(self):
        """Тот самый случай: подтверждение унесло в архив то, что не отправляли."""
        owner_watch.run(self.cfg)                      # → pending
        with self.assertRaises(ValueError) as отказ:
            mail_channel.acknowledge_owner_escalation('mailroom-test0001.json', self.sha)
        self.assertIn('не доставлял', str(отказ.exception))
        self.assertTrue(self.письмо.exists(),
                        'после отказа письмо остаётся в очереди, а не в архиве')

    def test_доставленное_подтвердить_можно(self):
        """Штатный путь не сломан: доставленное подтверждается и уходит в архив."""
        состояние = Path(self.cfg['state_directory']) / 'owner-delivery.json'
        ключ = 'mailroom-test0001.json:%s' % self.sha
        состояние.write_text(json.dumps(
            {'deliveries': {ключ: {'name': 'mailroom-test0001.json',
                                   'sha256': self.sha, 'state': 'delivered',
                                   'observed_at': 1.0, 'delivered_at': 1.0}}},
            ensure_ascii=False), encoding='utf-8')
        итог = mail_channel.acknowledge_owner_escalation('mailroom-test0001.json', self.sha)
        self.assertTrue(итог['acknowledged'])
        self.assertFalse(self.письмо.exists())
        self.assertTrue((self.почта / 'owner-archive' / 'mailroom-test0001.json').exists())

    def test_потерянная_запись_видна_как_требующая_разбора(self):
        """Файл в архиве, а состояние pending — это дефект, и он должен быть назван."""
        owner_watch.run(self.cfg)                      # → pending
        shutil.move(str(self.письмо),
                    str(self.почта / 'owner-archive' / 'mailroom-test0001.json'))
        итог = owner_watch.run(self.cfg)
        self.assertIn('unresolved', итог, 'потерянные записи обязаны быть перечислены')
        self.assertEqual(['mailroom-test0001.json'], итог['unresolved'])
        self.assertNotEqual('idle', итог['status'],
                            'тишина при потерянном сообщении недопустима')


if __name__ == '__main__':
    unittest.main(verbosity=2)

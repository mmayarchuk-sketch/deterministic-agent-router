# -*- coding: utf-8 -*-
"""Проверки замка единственного писателя.

Замок стал частью порядка работы двух агентов в одном дереве, а значит ручной
проверки ему мало: сломается молча — и оба начнут писать одновременно.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ЗДЕСЬ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ЗДЕСЬ)

import worktree_lock  # noqa: E402


class ЗамокTest(unittest.TestCase):

    def setUp(self):
        # Замок берётся рядом с модулем, поэтому на время проверок уводим его
        # во временный каталог: рабочее дерево трогать нельзя.
        self.каталог = tempfile.mkdtemp(prefix='test-worktree-lock-')
        self.прежний = worktree_lock.ЗАМОК
        worktree_lock.ЗАМОК = os.path.join(self.каталог, '.worktree.lock')

    def tearDown(self):
        worktree_lock.ЗАМОК = self.прежний
        shutil.rmtree(self.каталог, ignore_errors=True)

    def test_первый_захват_удаётся_и_пишет_кто_и_зачем(self):
        self.assertEqual(0, worktree_lock.взять('claude-b', 'правка канала'))
        with open(worktree_lock.ЗАМОК, encoding='utf-8') as f:
            запись = json.load(f)
        self.assertEqual('claude-b', запись['кто'])
        self.assertEqual('правка канала', запись['зачем'])
        self.assertIn('взят_в', запись)

    def test_второму_писателю_отказано(self):
        worktree_lock.взять('claude-b', 'правка канала')
        self.assertEqual(1, worktree_lock.взять('codex-astra', 'своя правка'))
        # Отказ не должен затирать чужую запись.
        with open(worktree_lock.ЗАМОК, encoding='utf-8') as f:
            self.assertEqual('claude-b', json.load(f)['кто'])

    def test_чужую_отдачу_не_принимает(self):
        worktree_lock.взять('claude-b', 'правка канала')
        self.assertEqual(1, worktree_lock.отдать('codex-astra', силой=False))
        self.assertTrue(os.path.exists(worktree_lock.ЗАМОК))

    def test_владелец_отдаёт_и_дерево_освобождается(self):
        worktree_lock.взять('claude-b', 'правка канала')
        self.assertEqual(0, worktree_lock.отдать('claude-b', силой=False))
        self.assertFalse(os.path.exists(worktree_lock.ЗАМОК))
        self.assertEqual(0, worktree_lock.взять('codex-astra', 'теперь можно'))

    def test_силой_снимает_чужой_и_говорит_об_этом(self):
        worktree_lock.взять('claude-b', 'правка канала')
        self.assertEqual(0, worktree_lock.отдать('codex-astra', силой=True))
        self.assertFalse(os.path.exists(worktree_lock.ЗАМОК))

    def test_битый_замок_не_выдаётся_за_свободное_дерево(self):
        with open(worktree_lock.ЗАМОК, 'w', encoding='utf-8') as f:
            f.write('{это не json')
        # Главное: битый файл не читается как «свободно» (код 0).
        self.assertEqual(2, worktree_lock.статус())
        # И не позволяет второму писателю зайти поверх.
        self.assertEqual(1, worktree_lock.взять('codex-astra', 'поверх битого'))

    def test_отдавать_нечего_когда_замка_нет(self):
        self.assertEqual(0, worktree_lock.отдать('claude-b', силой=False))


if __name__ == '__main__':
    unittest.main(verbosity=0)

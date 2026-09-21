from pathlib import Path
import json
import tempfile
import unittest

from receiver_order import register_and_sort


class ReceiverOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_sender_filename_does_not_reorder_later_arrival(self):
        future = {'name': '2099-01-01--first.md', 'sha256': 'f' * 64}
        past = {'name': '2000-01-01--second.md', 'sha256': 'a' * 64}
        first = register_and_sort(self.state, 'outbox', [future], 100)
        both = register_and_sort(self.state, 'outbox', [past, future], 200)
        self.assertLess(first[0]['receive_sequence'], both[1]['receive_sequence'])
        self.assertEqual([x['name'] for x in both], [future['name'], past['name']])

    def test_restart_is_idempotent_and_changed_sha_is_new_version(self):
        one = {'name': 'one.md', 'sha256': '1' * 64}
        seq = register_and_sort(self.state, 'outbox', [one], 100)[0]['receive_sequence']
        again = register_and_sort(self.state, 'outbox', [one], 200)[0]
        changed = register_and_sort(
            self.state, 'outbox', [{'name': 'one.md', 'sha256': '2' * 64}], 300)[0]
        self.assertEqual(again['receive_sequence'], seq)
        self.assertGreater(changed['receive_sequence'], seq)

    def test_legacy_receiver_time_wins_over_sender_name(self):
        legacy = [
            {'name': '2099-late-name.md', 'sha256': '1' * 64,
             'observed_at': 10, 'legacy_batch': 'old-1'},
            {'name': '1999-early-name.md', 'sha256': '2' * 64,
             'observed_at': 20, 'legacy_batch': 'old-2'},
        ]
        items = [{k: v for k, v in x.items() if k in ('name', 'sha256')} for x in legacy]
        ordered = register_and_sort(self.state, 'outbox', items, 30, legacy)
        self.assertEqual([x['name'] for x in ordered],
                         ['2099-late-name.md', '1999-early-name.md'])
        state = json.loads((self.state / 'receive-order.json').read_text())
        self.assertEqual(len(state['entries']), 2)


if __name__ == '__main__':
    unittest.main()

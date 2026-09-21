import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import dispatcher as d


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = patch.object(d, 'STATE', Path(self.tmp.name) / 'state')
        self.state.start()
        self.addCleanup(self.state.stop)
        self.launch = patch.object(d, 'ensure_worker')
        self.launch.start()
        self.addCleanup(self.launch.stop)

    def submit(self, key='a', **kw):
        return d.submit('Test', 'Read context', request_key=key, **kw)

    def test_retry_deduplication_and_payload_conflict(self):
        first = self.submit()
        self.assertEqual(first['id'], self.submit()['id'])
        with self.assertRaises(ValueError):
            d.submit('Changed', 'Other', request_key='a')
        self.assertEqual(len(d.list_jobs()), 1)

    def test_cancel_queued_is_terminal(self):
        job = self.submit()
        self.assertEqual(d.cancel_job(job['id'])['status'], 'cancelled')
        self.assertEqual(d.get_result(job['id'])['text'], '')

    def test_continue_only_owned_completed_session(self):
        job = self.submit()
        with self.assertRaises(ValueError):
            self.submit('b', resume_job_id=job['id'])
        d.finish(job['id'], 'completed')
        child = self.submit('b', resume_job_id=job['id'])
        self.assertEqual(child['session_id'], job['session_id'])
        with self.assertRaises(ValueError):
            self.submit('c', resume_job_id=job['id'])

    def test_project_and_timeout_validation(self):
        with self.assertRaises(ValueError):
            self.submit(project='/tmp')
        with self.assertRaises(ValueError):
            self.submit(timeout_seconds=999999)

    def test_missing_executable_fails_durably(self):
        job = self.submit()
        cfg = d.config()
        cfg['claude_binary'] = '/not/a/real/executable'
        with patch.object(d, 'config', return_value=cfg):
            d.run_job(job)
        self.assertEqual(d.get_job(job['id'])['status'], 'failed')

    def test_fake_executor_success_and_failure(self):
        exe = Path(self.tmp.name) / 'fake-claude'
        exe.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"result":"verified","is_error":false}\\n\'\n')
        exe.chmod(0o700)
        cfg = d.config()
        cfg['claude_binary'] = str(exe)
        job = self.submit()
        with patch.object(d, 'config', return_value=cfg):
            d.run_job(job)
        self.assertEqual(d.get_result(job['id'])['text'], 'verified')
        exe.write_text('#!/bin/sh\ncat >/dev/null\nprintf \'{"result":"denied","is_error":true}\\n\'\n')
        job = self.submit('b')
        with patch.object(d, 'config', return_value=cfg):
            d.run_job(job)
        self.assertEqual(d.get_job(job['id'])['status'], 'failed')

    def test_running_cancel_and_timeout_stop_process(self):
        exe = Path(self.tmp.name) / 'slow-claude'
        exe.write_text('#!/bin/sh\ncat >/dev/null\nsleep 60\n')
        exe.chmod(0o700)
        cfg = d.config()
        cfg['claude_binary'] = str(exe)
        for key, cancel in [('cancel', True), ('timeout', False)]:
            job = self.submit(key)
            with d.db() as c:
                c.execute("UPDATE jobs SET status='running',cancel_requested=? WHERE id=?", (int(cancel), job['id']))
            job['timeout_seconds'] = 0
            with patch.object(d, 'config', return_value=cfg):
                d.run_job(job)
            self.assertEqual(d.get_job(job['id'])['status'], 'cancelled' if cancel else 'failed')


if __name__ == '__main__':
    unittest.main()

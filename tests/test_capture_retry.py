from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_memory.capture import CaptureConflict, CaptureUncertain
from agent_memory.cli import _capture
from agent_memory.config import load_config
from tests.test_memory_system import make_home


def arguments(identity='request-a', **changes):
    values = dict(kind='decision', title='Synthetic choice', body='Use PostgreSQL.', source='user',
                  confidence='high', scope='project:demo', sensitivity='private', valid_from=None,
                  review_after=None, no_sync=True, operation_id=identity)
    values.update(changes)
    return argparse.Namespace(**values)


def worker(home, queue):
    try:
        queue.put(str(_capture(load_config(Path(home)), arguments())))
    except Exception as exc:
        queue.put(type(exc).__name__)


def crash_worker(home, phase):
    import agent_memory.capture as module
    real_create, real_receipt = module.atomic_create_text, module._write_receipt
    def create(path, content, **kwargs):
        if path.suffix == '.md' and phase == 'before-card':
            os._exit(17)
        real_create(path, content, **kwargs)
        if path.suffix == '.md' and phase == 'after-card':
            os._exit(17)
    def receipt(path, value, **kwargs):
        if value['state'] == 'completed' and phase == 'before-completion':
            os._exit(17)
        real_receipt(path, value, **kwargs)
    module.atomic_create_text, module._write_receipt = create, receipt
    _capture(load_config(Path(home)), arguments(phase))


class CaptureRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / 'source'
        source.mkdir()
        self.config = load_config(make_home(self.root, source))
        self.home = self.config.home

    def receipts(self):
        return list((self.home / 'capture-operations').glob('*.json'))

    def test_retry_conflicts_and_intentional_separate_saves(self):
        first = _capture(self.config, arguments())
        with patch('agent_memory.cli.utc_now', return_value='2026-12-01T00:00:00Z'):
            self.assertEqual(_capture(self.config, arguments()), first)
        for key, value in dict(body='Use SQLite.', title='Other', source='assistant', kind='fact',
                               confidence='low', scope='global', sensitivity='shareable',
                               valid_from='2026-01-01', review_after='2027-01-01').items():
            with self.subTest(key=key), self.assertRaises(CaptureConflict):
                _capture(self.config, arguments(**{key: value}))
        for identity in ('request-b', None, None):
            self.assertNotEqual(_capture(self.config, arguments(identity)), first)
        self.assertEqual(len(list((self.home / 'vault/inbox').glob('*.md'))), 4)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.receipts()[0].stat().st_mode), 0o600)
        self.assertNotIn('PostgreSQL', self.receipts()[0].read_text())
        self.assertNotIn('request-a', self.receipts()[0].read_text())

    def test_concurrent_processes_create_one_card(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        processes = [context.Process(target=worker, args=(str(self.home), queue)) for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join()
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=3) for _ in processes]
        queue.close()
        self.assertEqual(len(set(results)), 1, results)
        self.assertTrue(Path(results[0]).is_file())
        self.assertEqual(len(list((self.home / 'vault/inbox').glob('*.md'))), 1)

    def test_interruption_before_card_does_not_blindly_recreate(self):
        with patch('agent_memory.capture.atomic_create_text', side_effect=KeyboardInterrupt):
            # Interrupt card creation only; receipt uses the same helper.
            with self.assertRaises(KeyboardInterrupt):
                _capture(self.config, arguments())
        # No durable intent existed, so a fresh attempt is safe.
        self.assertEqual(self.receipts(), [])
        import agent_memory.capture as module
        real = module.atomic_create_text
        def interrupted(path, content, **kwargs):
            if path.suffix == '.md':
                raise KeyboardInterrupt
            return real(path, content, **kwargs)
        with patch.object(module, 'atomic_create_text', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                _capture(self.config, arguments())
        with self.assertRaises(CaptureUncertain):
            _capture(self.config, arguments())
        self.assertEqual(list((self.home / 'vault/inbox').glob('*.md')), [])

    def test_interruption_after_card_and_before_receipt_completion_recovers(self):
        import agent_memory.capture as module
        real = module._write_receipt
        def interrupted(path, receipt, **kwargs):
            if receipt['state'] == 'completed':
                raise KeyboardInterrupt
            return real(path, receipt, **kwargs)
        with patch.object(module, '_write_receipt', interrupted):
            with self.assertRaises(KeyboardInterrupt):
                _capture(self.config, arguments())
        created = list((self.home / 'vault/inbox').glob('*.md'))
        self.assertEqual(len(created), 1)
        self.assertEqual(_capture(self.config, arguments()), created[0])
        self.assertEqual(json.loads(self.receipts()[0].read_text())['state'], 'completed')

    def test_abrupt_process_exit_releases_lock_and_preserves_uncertainty(self):
        context = multiprocessing.get_context('spawn')
        for phase in ('before-card', 'after-card', 'before-completion'):
            with self.subTest(phase=phase):
                process = context.Process(target=crash_worker, args=(str(self.home), phase))
                process.start()
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join()
                self.assertEqual(process.exitcode, 17)
                before = list((self.home / 'vault/inbox').glob('*.md'))
                if phase == 'before-card':
                    with self.assertRaises(CaptureUncertain):
                        _capture(self.config, arguments(phase))
                else:
                    self.assertTrue(_capture(self.config, arguments(phase)).is_file())
                self.assertEqual(list((self.home / 'vault/inbox').glob('*.md')), before)

    def test_promotion_index_rebuild_and_erasure(self):
        first = _capture(self.config, arguments())
        reviewed = self.home / 'vault/decisions/reviewed.md'
        reviewed.parent.mkdir()
        first.rename(reviewed)
        reviewed.write_text(reviewed.read_text().replace('status: inbox', 'status: reviewed'))
        # A normal YAML rewrite may quote an unchanged identity.
        import re
        reviewed.write_text(re.sub(r'capture_operation: "?([a-f0-9]+)"?', r"capture_operation:    '\1'", reviewed.read_text()))
        shutil.rmtree(self.config.database.parent)
        self.assertEqual(_capture(self.config, arguments()), reviewed)
        reviewed.unlink()
        with self.assertRaises(CaptureUncertain):
            _capture(self.config, arguments())
        self.assertEqual(list((self.home / 'vault').rglob('*.md')), [])

    def test_duplicate_identity_and_modified_pending_card_are_uncertain(self):
        first = _capture(self.config, arguments())
        duplicate = first.with_name('duplicate.md')
        duplicate.write_text(first.read_text())
        with self.assertRaises(CaptureUncertain):
            _capture(self.config, arguments())
        duplicate.unlink()
        first.write_text(first.read_text() + 'Changed after interruption.\n')
        with self.assertRaises(CaptureUncertain):
            _capture(self.config, arguments())

    def test_invalid_ids_and_symlink_receipts_fail_without_card(self):
        for identity in ('', 'x\n', 'x\x7f', '가' * 100):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                _capture(self.config, arguments(identity))
        directory = self.home / 'capture-operations'
        outside = self.root / 'outside'
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            _capture(self.config, arguments())
        self.assertEqual(list(outside.iterdir()), [])

    def test_numeric_identity_and_corrupt_receipt_fail_closed(self):
        with patch('agent_memory.capture.uuid.uuid4', return_value=SimpleNamespace(hex='1' * 32)):
            first = _capture(self.config, arguments())
        self.assertEqual(_capture(self.config, arguments()), first)
        self.receipts()[0].write_text('{invalid receipt')
        with self.assertRaises(CaptureUncertain):
            _capture(self.config, arguments())
        self.assertEqual(list((self.home / 'vault/inbox').glob('*.md')), [first])

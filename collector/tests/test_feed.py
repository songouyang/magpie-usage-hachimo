import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest

from magpie_usage.feed import FileFeedWriter, encode_slot, safe_path
from magpie_usage.protocol import FEED_SIZE, SLOT_SIZE, FEED_SCHEMA, latest, decode_slots, checksum
from magpie_usage.projection import project, duration
from magpie_usage.state import CollectionState
from test_collection import payloads, AT

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('plugin', ROOT/'tools/plugin.py')
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.path = self.base/'feed.txt'
        self.writer = FileFeedWriter(self.path)
        self.state = CollectionState()
        self.state.succeed(payloads(), AT, 10)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def read(self):
        return self.path.read_text(encoding='ascii')

    def test_inode_and_size_survive_updates_and_restart(self):
        before = self.path.stat()
        for index in range(4):
            self.state.succeed(payloads(), AT + index * 15, 10 + index * 15)
            receipt = self.writer.write(self.state)
            self.assertEqual(receipt['sequence'], index + 1)
            self.assertEqual(receipt['slot'], index % 2)
            self.assertEqual(self.path.stat().st_ino, before.st_ino)
            self.assertEqual(self.path.stat().st_size, FEED_SIZE)
            self.assertEqual(plugin.snapshot(self.read()), {**project(self.state.snapshot), 'successAt': (AT+index*15)*1000, 'failed': False})
        self.writer.close()
        self.writer = FileFeedWriter(self.path)
        self.state.fail('offline')
        self.assertEqual(self.writer.write(self.state)['sequence'], 5)
        self.assertEqual(self.path.stat().st_ino, before.st_ino)

    def test_duplicate_is_not_written(self):
        self.writer.write(self.state)
        before = self.path.stat().st_mtime_ns
        self.assertFalse(self.writer.write(self.state)['written'])
        self.assertEqual(before, self.path.stat().st_mtime_ns)

    def test_concurrent_readers_always_find_an_integral_sample(self):
        self.writer.write(self.state)
        start = Barrier(4)

        def read_many():
            start.wait()
            for _ in range(200):
                raw = self.path.read_text(encoding='ascii')
                record = latest(raw)
                self.assertIsNotNone(record)
                view = plugin.snapshot(raw)
                self.assertGreaterEqual(view['successAt'], AT * 1000)
                self.assertLessEqual(view['successAt'], (AT + 60 * 15) * 1000)
                self.assertEqual(len(view['pages']), 4)

        with ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(read_many) for _ in range(3)]
            start.wait()
            for index in range(1, 61):
                self.state.succeed(payloads(), AT + index * 15, 10 + index * 15)
                self.writer.write(self.state)
            for reader in readers:
                reader.result()

    def test_failure_retains_last_success_then_recovery_advances_time(self):
        self.writer.write(self.state)
        first = plugin.snapshot(self.read())
        self.state.fail('offline')
        self.writer.write(self.state)
        failed = plugin.snapshot(self.read())
        self.assertTrue(failed['failed'])
        self.assertEqual(first['successAt'], failed['successAt'])
        self.assertEqual(first['pages'], failed['pages'])
        self.state.succeed(payloads(), AT+80, 90)
        self.writer.write(self.state)
        self.assertFalse(plugin.snapshot(self.read())['failed'])
        self.assertEqual(plugin.snapshot(self.read())['successAt'], (AT+80)*1000)

    def test_every_torn_byte_boundary_keeps_a_complete_slot(self):
        self.writer.write(self.state)
        before = self.read().encode('ascii')
        self.state.succeed(payloads(), AT+15, 25)
        value = {'schema': FEED_SCHEMA, 'snapshot': self.state.snapshot, 'failed': False}
        new = encode_slot(2, value)
        for boundary in range(SLOT_SIZE+1):
            raw = before[:SLOT_SIZE] + new[:boundary] + before[SLOT_SIZE+boundary:]
            slots = decode_slots(raw.decode('ascii'))
            self.assertTrue(slots, boundary)
            result = plugin.snapshot(raw.decode('ascii'))
            self.assertIn(result['successAt'], (AT*1000, (AT+15)*1000), boundary)

    def test_bad_checksum_and_bad_slot_fall_back_to_other_slot(self):
        self.writer.write(self.state)
        old = self.read()
        self.state.fail('offline')
        self.writer.write(self.state)
        raw = self.read()
        record = json.loads(raw[SLOT_SIZE:])
        record['sequence'] = 999999
        bad = json.dumps(record).ljust(SLOT_SIZE)
        self.assertEqual(plugin.snapshot(raw[:SLOT_SIZE]+bad), plugin.snapshot(old))
        self.assertEqual(plugin.snapshot(raw[:SLOT_SIZE]+'?'*SLOT_SIZE), plugin.snapshot(old))

    def test_no_data_and_invalid_input_are_waiting(self):
        for raw in (None, '', '?'*FEED_SIZE, ' '*FEED_SIZE):
            result = plugin.snapshot(raw)
            self.assertEqual(result['successAt'], 0)
            self.assertEqual(result['updated'], '数据更新：—')
            self.assertTrue(all(page['hero']=='—' for page in result['pages']))
        self.writer.write(CollectionState())
        self.assertEqual(plugin.snapshot(self.read())['successAt'], 0)

    def test_cross_process_lock_prevents_second_collector(self):
        code = 'from magpie_usage.feed import FileFeedWriter; FileFeedWriter(__import__("sys").argv[1])'
        result = subprocess.run([sys.executable, '-c', code, str(self.path)], env={**os.environ, 'PYTHONPATH': str(ROOT/'collector')}, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('已有采集器'.encode(), result.stderr)

    def test_oversize_rejected_without_modifying_file(self):
        self.writer.write(self.state)
        before = self.read()
        self.state.snapshot['extra'] = 'x'*5000
        with self.assertRaises(ValueError):
            self.writer.write(self.state)
        self.assertEqual(before, self.read())

    def test_replacement_detected_and_new_file_not_overwritten(self):
        self.writer.write(self.state)
        self.path.rename(self.base/'old.txt')
        self.path.write_text('replacement')
        with self.assertRaises(ValueError):
            self.writer.write(self.state)
        self.assertEqual(self.path.read_text(), 'replacement')

    def test_symlink_hardlink_corrupt_file_and_manager_paths_rejected(self):
        self.writer.write(self.state)
        link = self.base/'link.txt'
        link.symlink_to(self.path)
        with self.assertRaises(ValueError): FileFeedWriter(link)
        hard = self.base/'hard.txt'
        os.link(self.path, hard)
        with self.assertRaises(ValueError): FileFeedWriter(hard)
        self.assertEqual(len(self.read()), FEED_SIZE)
        bad = self.base/'bad.txt'
        for raw in ('damaged', '?'*FEED_SIZE):
            bad.write_text(raw)
            with self.assertRaises(ValueError): FileFeedWriter(bad)
            self.assertEqual(bad.read_text(), raw)
        for root in (Path.home()/'.claw-pet/components', Path.home()/'Library/Application Support/com.petmanager.desktop'):
            with self.assertRaises(ValueError): safe_path(root/'feed.txt')

    def test_projection_and_checksum_reference_values(self):
        self.assertEqual(checksum(1, 'test'), '838d2e24')
        for value in (None, {}, self.state.snapshot, {'latest_model':'W'*63, 'success_permille':333}):
            self.assertEqual(project(value), plugin.project(value))
        self.assertEqual(duration(3271), '3.27 s')
        self.assertEqual(duration(999), '999 ms')
        self.assertEqual(duration(None), '—')


class PackageTests(unittest.TestCase):
    def test_confirmed_ui_and_bindings_preserved(self):
        base = ROOT/'package-python/magpie-usage-hachimo'
        # Baselines from the previously accepted 1.0.3 UI, without old source trees.
        for name, expected in (
            ('runtime/widget.json', 'ceb6c35d1cbbd32a36a1cbc0979160e22407aad387231409f3098e3581ae5afc'),
            ('buttons.json', '629ac85cb5b9f4ade48d35688bd3a37649d36dcf59e138745c38a706b79ed92d'),
        ):
            self.assertEqual(hashlib.sha256((base/name).read_bytes()).hexdigest(), expected)
        current = json.loads((base/'runtime/widget.json').read_text())
        self.assertEqual(len(current['vars']), 8)
        self.assertEqual(current['tick'], [])

    def test_all_bound_actions_resolve_and_pages_wrap(self):
        base = ROOT/'package-python/magpie-usage-hachimo'
        app = json.loads((base/'pc/application.json').read_text())
        buttons = json.loads((base/'buttons.json').read_text())
        plan = app['content'][0]
        for button in buttons:
            flow = app['actions'][button['action']].removeprefix('flow:')
            self.assertIn(flow, plan['workflows'])
        for page in range(4):
            for flow, increment in (('previous', 3), ('next', 1)):
                expression = plan['workflows'][flow][0]['value']
                self.assertEqual(expression['op'], 'mod')
                addition, count = expression['args']
                self.assertEqual(addition, {'op': 'add', 'args': [{'ref': '/sources/nav/page'}, increment]})
                self.assertEqual(count, 4)
                self.assertEqual((page + increment) % count, (page + (1 if flow == 'next' else -1)) % 4)
        for page in range(4):
            action = ('overview', 'requests', 'sessions', 'cache')[page]
            flow = app['actions']['host.' + action].removeprefix('flow:')
            self.assertEqual(plan['workflows'][flow][0]['value'], page)

    def test_collector_has_no_manager_writer_or_serial_dependency(self):
        for name in ('feed.py', '__main__.py'):
            source = (ROOT/'collector/magpie_usage'/name).read_text()
            for forbidden in ('CompositionWriter', 'manager_version', 'package_hash', 'serial.Serial', 'composition-v2-state.json'):
                self.assertNotIn(forbidden, source)


if __name__=='__main__': unittest.main()

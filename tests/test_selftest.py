from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from omhc import cli, selftest


class TestSelfTest(unittest.TestCase):
    def test_isolated_pipeline_checks_both_directions_without_launching_harnesses(self):
        original_run = subprocess.run
        launches = []
        def execute(argv, **kwargs):
            launches.append(argv)
            return original_run(argv, **kwargs)
        with tempfile.TemporaryDirectory() as home:
            sentinel = Path(home) / 'settings.json'
            sentinel.write_text('Preserve caller state')
            out = io.StringIO()
            with mock.patch.dict(os.environ, {'HOME': home, 'OMHC_OFF': '1'}), \
                    mock.patch.object(selftest.subprocess, 'run', side_effect=execute):
                code = cli.main(['doctor', '--self-test', '--json'], home=home, out=out)
            result = json.loads(out.getvalue())
            self.assertEqual(code, 0, result)
            self.assertTrue(result['isolated'])
            self.assertEqual(result['model_receipt'], 'unverified')
            self.assertEqual(len(result['checks']), 2)
            self.assertTrue(all(r['ok'] for r in result['checks']), result)
            self.assertEqual(list(Path(home).iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(), 'Preserve caller state')
        self.assertTrue(launches)
        self.assertTrue(all(argv[0] == '/bin/sh' or Path(argv[0]).name == 'omhc' for argv in launches))

    def test_a_failed_probe_is_not_reported_as_pass(self):
        with mock.patch.object(selftest.subprocess, 'run', side_effect=subprocess.TimeoutExpired('omhc', 15)):
            result = selftest.run()
        self.assertFalse(result['ok'])
        self.assertTrue(any(r['ok'] is False for r in result['checks']))

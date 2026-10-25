from __future__ import annotations

import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from omhc import brief, cli, diagnostics, due, gate, ledger
from ._repo import TempRepo, codex_user_row

NOW = 1800000000


def snapshot(t):
    root = Path(t.home).parent
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


class TestExplain(unittest.TestCase):
    def setUp(self):
        self.t = TempRepo()
        self.addCleanup(self.t.close)

    def plant(self, **kwargs):
        return self.t.plant_codex(ledger_home=self.t.home, when=NOW, **kwargs)

    def explain(self, **kwargs):
        return diagnostics.explain(self.t.root, 'claude-code', home=self.t.home, now=NOW, **kwargs)

    def test_preview_explains_a_candidate_without_mutating_or_consuming_it(self):
        self.plant(human='Keep the source request')
        before = snapshot(self.t)
        result = self.explain()
        self.assertEqual(result['reason'], 'preview_ready')
        self.assertIn('GOAL  Keep the source request', result['preview'])
        self.assertTrue(any(e['reason'] == 'included' for e in result['events']))
        self.assertEqual(snapshot(self.t), before)
        body = brief.compute(my_harness='claude-code', my_session_id='target',
                             repo_root=self.t.root, home=self.t.home, now=NOW)
        self.assertIn('Keep the source request', body)

    def test_same_harness_and_no_history_have_different_explanations(self):
        self.assertEqual(self.explain()['reason'], 'no_candidates')
        self.plant()
        result = diagnostics.explain(self.t.root, 'codex-cli', home=self.t.home, now=NOW)
        self.assertEqual(result['reason'], 'same_harness')
        self.assertIn('native resume', ' '.join(result['actions']))

    def test_off_marker_is_observed_without_modification(self):
        self.plant()
        Path(self.t.state).mkdir(parents=True, exist_ok=True)
        (Path(self.t.state) / 'off').touch()
        before = snapshot(self.t)
        self.assertEqual(self.explain()['reason'], 'off')
        rows = diagnostics.sessions(self.t.root, 'claude-code', home=self.t.home, now=NOW)['sessions']
        self.assertTrue(all(r['reason'] == 'off' for r in rows))
        self.assertEqual(snapshot(self.t), before)

    def test_old_and_headless_sessions_report_the_selection_reason(self):
        self.plant(session_id='old')
        self.assertEqual(diagnostics.explain(self.t.root, 'claude-code', home=self.t.home,
                         now=NOW + due.MAX_AGE_SECONDS + 1)['reason'], 'too_old')
        self.plant(session_id='headless', meta_extra={'source': 'exec'})
        result = self.explain()
        self.assertTrue(any(e['session'] == 'headless' and e['reason'] == 'excluded_by_adapter'
                            for e in result['events'] if 'session' in e))

    def test_delivery_and_empty_resume_are_reported_using_the_real_offset_guard(self):
        path = self.plant()
        brief.compute(my_harness='claude-code', my_session_id='first', repo_root=self.t.root,
                      home=self.t.home, now=NOW)
        delivered = self.explain(session='first')
        self.assertEqual(delivered['reason'], 'already_delivered')
        self.assertIs(delivered['gate_claimed'], True)
        due.mark_reopened(self.t.state, 'cx1', 'codex-cli', NOW)
        self.assertEqual(self.explain()['reason'], 'no_new_human_turn')
        with open(path, 'a') as fh:
            fh.write(json.dumps(codex_user_row('New actual instruction', ordinal=10)) + '\n')
        self.assertEqual(self.explain()['reason'], 'preview_ready')

    def test_claimed_gate_blocks_a_new_source_for_the_same_receiver(self):
        self.plant()
        gate.claim(self.t.state, 'claude-code', 'target')
        before = snapshot(self.t)
        result = self.explain(session='target')
        self.assertEqual(result['reason'], 'gate_claimed')
        self.assertEqual(result['outcome'], 'nothing')
        self.assertEqual(snapshot(self.t), before)

    def test_missing_source_and_read_errors_do_not_write_failure_logs(self):
        path = self.plant()
        os.unlink(path)
        before = snapshot(self.t)
        self.assertEqual(self.explain()['reason'], 'missing_source')
        self.assertEqual(snapshot(self.t), before)
        self.plant()
        before = snapshot(self.t)
        with mock.patch.object(brief, '_ref_for', side_effect=ValueError('bad source')):
            self.assertEqual(self.explain()['outcome'], 'error')
        self.assertEqual(snapshot(self.t), before)

    def test_trace_preserves_selection_order_and_reopen_boundaries(self):
        self.plant(session_id='a')
        first = due.due(self.t.key, 'claude-code', '', NOW, home=self.t.home)[0]
        due.mark_delivered(self.t.state, first, to_harness='claude-code', epoch=NOW)
        self.plant(session_id='b')
        self.plant(session_id='a')
        due.mark_reopened(self.t.state, 'a', 'codex-cli', NOW)
        trace = []
        plain = due.due(self.t.key, 'claude-code', '', NOW, home=self.t.home)
        observed = due.due(self.t.key, 'claude-code', '', NOW, home=self.t.home, trace=trace.append)
        self.assertEqual(plain, observed)
        self.assertEqual([r.session_id for r in observed], ['a', 'b'])
        self.assertTrue(any(e['reason'] == 'reopened_boundary' for e in trace))

    def test_sessions_are_deduplicated_in_ledger_order_and_human_goals_only(self):
        self.plant(session_id='a', human='First human goal')
        self.plant(session_id='b', human='Second human goal')
        self.plant(session_id='a', human='First human goal')
        before = snapshot(self.t)
        result = diagnostics.sessions(self.t.root, 'claude-code', home=self.t.home, now=NOW)
        self.assertEqual([r['session'] for r in result['sessions']], ['a', 'b'])
        self.assertEqual(result['sessions'][0]['goal'], 'First human goal')
        self.assertEqual(snapshot(self.t), before)

    def test_session_cap_does_not_label_the_last_included_session_as_skipped(self):
        for i in range(4):
            self.plant(session_id='source-' + str(i))
        result = diagnostics.sessions(self.t.root, 'claude-code', home=self.t.home, now=NOW)
        rows = result['sessions']
        self.assertEqual([r['reason'] for r in rows[:3]], ['included'] * 3)
        self.assertEqual(rows[3]['reason'], 'outside_selection_window')

    def test_runnable_project_hook_does_not_prompt_a_duplicate_global_install(self):
        inst = mock.Mock()
        inst.hooks_status.return_value = (False, 'Global hooks are missing')
        inst.hook_is_installed.return_value = True
        with mock.patch.object(diagnostics.adapters, 'get', return_value=inst):
            status = diagnostics._hooks('example', self.t.home, self.t.root)
        self.assertIsNone(status['ok'])
        self.assertIn('runnable repo hook', status['detail'])

    def test_refused_roots_have_json_errors(self):
        previous = os.getcwd()
        os.chdir('/')
        self.addCleanup(os.chdir, previous)
        for command in ('explain', 'sessions'):
            out = io.StringIO()
            code = cli.main([command, '--harness', 'claude-code', '--json'], home=self.t.home, out=out)
            self.assertEqual(code, 1)
            self.assertIn('error', json.loads(out.getvalue()))

    def test_cli_json_diagnostics_are_read_only(self):
        self.plant()
        previous = os.getcwd()
        os.chdir(self.t.repo)
        self.addCleanup(os.chdir, previous)
        before = snapshot(self.t)
        for command in ('explain', 'sessions'):
            out = io.StringIO()
            self.assertEqual(cli.main([command, '--harness', 'claude-code', '--json'],
                                      home=self.t.home, out=out), 0)
            json.loads(out.getvalue())
        self.assertEqual(snapshot(self.t), before)

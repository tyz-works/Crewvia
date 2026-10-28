#!/usr/bin/env python3
"""validate_agy.py と check_contamination.py の判定 (claude / agy は呼ばない)。

実行: python3 bench/review-bench/tests/test_validators.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(KIT)), 'scripts')
SCHEMA = os.path.join(KIT, 'schema.json')
GOOD = {'findings': []}


def run(script, *args):
    return subprocess.run([sys.executable, os.path.join(KIT, script), *args], capture_output=True, text=True)


class ValidateAgy(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, 'raw')
            with open(raw, 'w') as f:
                f.write(text)
            return run('validate_agy.py', '--raw', raw, '--schema', SCHEMA, '--scripts', SCRIPTS)

    def test_ok(self):
        r = self.check(json.dumps({'status': 'SUCCESS', 'denied_actions': [], 'structured_output': GOOD}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), GOOD)

    def test_rejects(self):
        cases = {
            'status': {'status': 'ERROR', 'structured_output': GOOD},
            'denied_actions': {'status': 'SUCCESS', 'denied_actions': [{'x': 1}], 'structured_output': GOOD},
            'no_structured_output': {'status': 'SUCCESS'},
            'schema_invalid': {'status': 'SUCCESS', 'structured_output': {'findings': 'x'}},
        }
        for code, doc in cases.items():
            r = self.check(json.dumps(doc))
            self.assertEqual(r.returncode, 1, code)
            self.assertEqual(r.stdout, '', code)
            self.assertIn(code, r.stderr)

    def test_rejects_garbage_and_multiple_docs(self):
        ok = json.dumps({'status': 'SUCCESS', 'structured_output': GOOD})
        for text in ('', 'not json', ok + ok):
            r = self.check(text)
            self.assertEqual(r.returncode, 1, text)
            self.assertEqual(r.stdout, '', text)


class Contamination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clone = os.path.join(self.tmp.name, 'clone')
        self.diff = os.path.join(self.tmp.name, 'diff')
        os.makedirs(self.clone)
        os.makedirs(self.diff)
        key = ''.join('-' if c in '/._' else c for c in os.path.realpath(self.clone))
        self.projects = os.path.join(self.tmp.name, 'projects')
        self.sess = os.path.join(self.projects, key)
        os.makedirs(self.sess)

    def read(self, path):
        return json.dumps({'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': {'file_path': path}}]}})

    def check(self, *lines):
        with open(os.path.join(self.sess, 's.jsonl'), 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return run('check_contamination.py', '--clone', self.clone, '--diff-dir', self.diff, '--projects-dir', self.projects)

    def test_clean(self):
        self.assertEqual(self.check(self.read(os.path.join(self.clone, 'a'))).returncode, 0)

    def test_outside_read(self):
        r = self.check(self.read('/etc/passwd'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('/etc/passwd', r.stdout)

    def test_truncated_only_is_inconclusive(self):
        self.assertEqual(self.check('{"message": {"content": [{"type": "tool_').returncode, 3)

    def test_malformed_line_among_valid_is_inconclusive(self):
        r = self.check(self.read(os.path.join(self.clone, 'a')), '{"broken')
        self.assertEqual(r.returncode, 3)

    def test_zero_audited_is_inconclusive(self):
        self.assertEqual(self.check('{"type": "summary"}').returncode, 3)

    def test_outside_read_wins_over_malformed(self):
        self.assertEqual(self.check(self.read('/etc/passwd'), '{"broken').returncode, 1)

    def test_no_session_file(self):
        os.remove(os.path.join(self.sess, 's.jsonl')) if os.path.exists(os.path.join(self.sess, 's.jsonl')) else None
        r = run('check_contamination.py', '--clone', self.clone, '--diff-dir', self.diff, '--projects-dir', self.projects)
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()

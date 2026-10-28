#!/usr/bin/env python3
"""本番 lib の extract-agy (bench が呼ぶ検証) と check_contamination.py の判定 (claude / agy は呼ばない)。

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
SID = '11111111-2222-3333-4444-555555555555'


def run(script, *args):
    return subprocess.run([sys.executable, os.path.join(KIT, script), *args], capture_output=True, text=True)


class ExtractAgy(unittest.TestCase):
    """bench は agy 出力の検証を再実装せず、本番 lib の extract-agy を呼ぶ。その呼び口が bench の schema で働くこと。"""
    def check(self, text):
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, 'raw')
            with open(raw, 'w') as f:
                f.write(text)
            return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'lib_review_engine.py'), 'extract-agy',
                                   '--out', raw, '--schema', SCHEMA], capture_output=True, text=True)

    def test_ok(self):
        r = self.check(json.dumps({'status': 'SUCCESS', 'denied_actions': [], 'structured_output': GOOD}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), GOOD)

    def test_rejects(self):
        for text in ('', 'not json', json.dumps({'status': 'ERROR', 'structured_output': GOOD}),
                     json.dumps({'status': 'SUCCESS', 'denied_actions': [1], 'structured_output': GOOD}),
                     json.dumps({'status': 'SUCCESS', 'structured_output': {'findings': 'x'}})):
            r = self.check(text)
            self.assertEqual(r.returncode, 1, text)
            self.assertEqual(r.stdout, '', text)

    def test_bench_has_no_validator_of_its_own(self):
        self.assertFalse(os.path.exists(os.path.join(KIT, 'validate_agy.py')))
        with open(os.path.join(KIT, 'run.sh'), encoding='utf-8') as f:
            self.assertIn('extract-agy', f.read())


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
        with open(os.path.join(self.sess, SID + '.jsonl'), 'w') as f:
            f.write('\n'.join(lines) + '\n')
        return run('check_contamination.py', '--clone', self.clone, '--diff-dir', self.diff, '--session-id', SID,
                   '--projects-dir', self.projects)

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

    def tool(self, name, inp):
        return json.dumps({'message': {'content': [{'type': 'tool_use', 'name': name, 'input': inp}]}})

    def test_read_without_path_is_inconclusive(self):
        self.assertEqual(self.check(self.read(os.path.join(self.clone, 'a')), self.tool('Read', {})).returncode, 3)

    def test_read_non_string_path_is_inconclusive(self):
        self.assertEqual(self.check(self.read(os.path.join(self.clone, 'a')), self.tool('Read', {'file_path': 5})).returncode, 3)

    def test_read_non_dict_input_is_inconclusive(self):
        self.assertEqual(self.check(self.read(os.path.join(self.clone, 'a')), self.tool('Read', 'x')).returncode, 3)

    def test_grep_without_path_is_cwd(self):
        self.assertEqual(self.check(self.tool('Grep', {'pattern': 'x'})).returncode, 0)

    def test_escaping_patterns_are_outside(self):
        for name, inp in (('Glob', {'pattern': '/etc/*'}), ('Glob', {'pattern': '../x/**'}), ('Glob', {'pattern': 'a/../../b'}),
                          ('Glob', {'pattern': '~/.ssh/*'}), ('Grep', {'pattern': 'x', 'glob': '/etc/**'}),
                          ('Grep', {'pattern': '/etc/passwd'}), ('Grep', {'pattern': 'x', 'glob': '../*'})):
            r = self.check(self.tool(name, inp))
            self.assertEqual(r.returncode, 1, (name, inp))

    def test_inside_patterns_are_fine(self):
        for name, inp in (('Glob', {'pattern': '**/*.py'}), ('Grep', {'pattern': 'foo\\.\\.bar', 'glob': '*.sh'}),
                          ('Glob', {'pattern': 'scripts/lib_*.py'})):
            self.assertEqual(self.check(self.tool(name, inp)).returncode, 0, (name, inp))

    def test_no_session_file(self):
        r = run('check_contamination.py', '--clone', self.clone, '--diff-dir', self.diff, '--session-id', SID,
                '--projects-dir', self.projects)
        self.assertEqual(r.returncode, 2)

    def test_only_the_named_session_is_read(self):
        # 別の session (外を読んでいる) が同じ dir にあっても、指定した UUID の 1 ファイルだけを見る
        other = os.path.join(self.sess, '99999999-2222-3333-4444-555555555555.jsonl')
        with open(other, 'w') as f:
            f.write(self.read('/etc/passwd') + '\n')
        self.assertEqual(self.check(self.read(os.path.join(self.clone, 'a'))).returncode, 0)

    def test_bad_session_id(self):
        r = run('check_contamination.py', '--clone', self.clone, '--diff-dir', self.diff, '--session-id', '../x',
                '--projects-dir', self.projects)
        self.assertEqual(r.returncode, 2)


# 型の一般論として残してよい語 (事例ではなく、どのレビューでも言える一般的な確認項目)
GENERIC = {'重複キー', 'kill-after'}


class GenChecklist(unittest.TestCase):
    def gen(self, *args):
        return subprocess.run(['bash', os.path.join(KIT, 'gen_checklist.sh'), *args], capture_output=True, text=True)

    def test_no_graded_pr_or_ground_truth_text(self):
        r = self.gen()
        self.assertEqual(r.returncode, 0, r.stderr)
        for n in ('312', '313', '314'):
            self.assertNotIn('#' + n, r.stdout)
        with open(os.path.join(KIT, 'ground-truth.tsv'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                summary = line.split('\t')[1].strip()
                if summary in GENERIC:
                    continue
                self.assertNotIn(summary, r.stdout)
        self.assertIn('型 1', r.stdout)  # 型の一般論は残る

    def test_unreadable_source_fails(self):
        self.assertNotEqual(self.gen('/nonexistent/x.md').returncode, 0)

    def test_empty_output_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'c.md')
            with open(p, 'w') as f:
                f.write('see #313\n')
            self.assertNotEqual(self.gen(p).returncode, 0)


if __name__ == '__main__':
    unittest.main()

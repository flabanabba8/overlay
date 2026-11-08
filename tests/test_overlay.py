import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('overlay_fixture', root / '__init__.py', submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
core = sys.modules['overlay_fixture.core']
ORIGINAL = ''.join(f'line {i}\n' for i in range(1, 21))


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); base = Path(self.tmp.name)
        self.proj = base / 'proj'; self.proj.mkdir(); (self.proj / 'app.py').write_text(ORIGINAL)
        self.o = core.Overlays(base / 'data'); self.id = self.o.start(str(self.proj))['overlay_id']

    def tearDown(self): self.tmp.cleanup()

    def write(self, rel, text):
        r = self.o.rewrite('write_file', {'path': str(self.proj / rel)}); self.assertEqual(r['action'], 'modify')
        Path(r['args']['path']).parent.mkdir(parents=True, exist_ok=True); Path(r['args']['path']).write_text(text); return r['args']['path']

    def test_tool_paths_are_redirected_and_real_files_untouched(self):
        sh = self.write('app.py', ORIGINAL.replace('line 2\n', 'LINE TWO\n'))
        self.assertEqual((self.proj / 'app.py').read_text(), ORIGINAL); self.assertNotEqual(sh, str(self.proj / 'app.py'))
        again = self.o.rewrite('read_file', {'path': 'app.py'})['args']['path']; self.assertEqual(again, sh); self.assertIn('LINE TWO', Path(again).read_text())

    def test_paths_outside_the_overlay_and_other_tools_are_ignored(self):
        self.assertIsNone(self.o.rewrite('write_file', {'path': '/tmp/elsewhere.txt'})); self.assertIsNone(self.o.rewrite('terminal', {'command': 'ls'}))
        self.assertIsNone(self.o.rewrite('write_file', {'path': str(self.proj / '..' / 'x' / 'y')}))

    def test_diff_has_numbered_hunks_and_new_files(self):
        self.write('app.py', ORIGINAL.replace('line 2\n', 'LINE TWO\n').replace('line 18\n', 'LINE 18\nextra\n')); self.write('new/mod.py', 'x = 1\n')
        d = self.o.diff(self.id); self.assertEqual(d['changed_files'], 2); app = next(f for f in d['files'] if f['file'] == 'app.py')
        self.assertEqual([h['hunk'] for h in app['hunks']], [0, 1]); self.assertIn('+LINE TWO', app['hunks'][0]['diff'])
        self.assertTrue(next(f for f in d['files'] if f['file'] == 'new/mod.py')['new_file'])

    def test_apply_only_the_accepted_hunks(self):
        self.write('app.py', ORIGINAL.replace('line 2\n', 'LINE TWO\n').replace('line 18\n', 'LINE 18\n').replace('line 10\n', 'BAD EDIT\n'))
        r = self.o.apply(self.id, {'app.py': [0, 2]}); text = (self.proj / 'app.py').read_text()
        self.assertIn('LINE TWO', text); self.assertIn('LINE 18', text); self.assertIn('line 10\n', text); self.assertNotIn('BAD EDIT', text)
        self.assertEqual(r['applied'][0]['hunks'], [0, 2]); self.assertEqual(r['rejected'][0]['hunks'], [1])
        self.assertEqual(self.o.status()['active'], [])

    def test_apply_all_creates_new_files_and_unlisted_files_are_rejected(self):
        self.write('app.py', ORIGINAL + 'tail\n'); self.write('keep/out.txt', 'hello\n'); self.write('skip.txt', 'no\n')
        self.o.apply(self.id, {'keep/out.txt': 'all'})
        self.assertEqual((self.proj / 'keep' / 'out.txt').read_text(), 'hello\n'); self.assertFalse((self.proj / 'skip.txt').exists()); self.assertEqual((self.proj / 'app.py').read_text(), ORIGINAL)

    def test_real_file_changed_meanwhile_is_not_overwritten(self):
        self.write('app.py', ORIGINAL.replace('line 2\n', 'LINE TWO\n')); (self.proj / 'app.py').write_text('someone else edited this\n')
        d = self.o.diff(self.id); self.assertTrue(d['files'][0]['conflict'])
        r = self.o.apply(self.id, {'app.py': 'all'}); self.assertEqual(r['refused'][0]['file'], 'app.py'); self.assertEqual((self.proj / 'app.py').read_text(), 'someone else edited this\n')

    def test_discard_and_validation(self):
        self.write('app.py', 'x\n'); self.o.discard(self.id); self.assertEqual((self.proj / 'app.py').read_text(), ORIGINAL)
        with self.assertRaises(ValueError): self.o.diff('../../etc')
        with self.assertRaises(ValueError): self.o.start('/'); 
        with self.assertRaises(ValueError): self.o.start('relative')
        i2 = self.o.start(str(self.proj))['overlay_id']
        with self.assertRaises(ValueError): self.o.start(str(self.proj))
        with self.assertRaises(ValueError): self.o.apply(i2, {'nope.py': 'all'})

    def test_results_show_real_paths_and_terminal_writes_ask(self):
        sh = self.write('app.py', 'x\n'); self.assertEqual(self.o.unmap(f'wrote {sh} ok'), f'wrote {self.proj}/app.py ok')
        self.assertEqual(self.o.terminal_guard('sed -i s/a/b/ app.py')['action'], 'approve'); self.assertEqual(self.o.terminal_guard('echo hi > out.txt')['action'], 'approve')
        self.assertIsNone(self.o.terminal_guard('python -m pytest -q 2>&1 > /dev/null')); self.assertIsNone(self.o.terminal_guard('ls -la'))

    def test_symlink_escape_is_not_shadowed_into_a_real_write(self):
        outside = Path(self.tmp.name) / 'outside.txt'; outside.write_text('secret'); (self.proj / 'link').symlink_to(outside)
        self.assertIsNone(self.o.rewrite('write_file', {'path': str(self.proj / 'link')}))

    def test_apply_tool_needs_approval_through_the_hook(self):
        d = module.on_pre_tool_call(tool_name='overlay_apply', args={}); self.assertEqual(d['action'], 'approve')


if __name__ == '__main__': unittest.main()

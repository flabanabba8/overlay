"""Copy-on-write shadow of a project folder for the agent's file tools, with per-hunk review and a checked apply."""
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

REWRITE_TOOLS = {'read_file', 'write_file', 'patch'}
ID = re.compile(r'^[0-9a-f]{12}$')
MAX_FILE = 2 * 1024 * 1024
MAX_HUNKS = 300
TERMINAL_WRITE = re.compile(r'(>>?\s*[^\s&|]|\bsed\s+(-[a-zA-Z]*i|--in-place)|\btee\b|\bmv\b|\bcp\b|\brm\b|\bgit\s+(checkout|restore|reset|apply|stash)\b|\bpatch\b|\btruncate\b|\bdd\b|\binstall\s)', re.I)


def sha(data): return hashlib.sha256(data).hexdigest()


def real(path): return os.path.realpath(os.path.expanduser(path))


def inside(root, path): return path == root or path.startswith(root.rstrip('/') + '/')


class Overlays:
    def __init__(self, data_root):
        self.root = Path(data_root); self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def folder(self, oid):
        if not isinstance(oid, str) or not ID.match(oid) or not (self.root / oid / 'meta.json').is_file(): raise ValueError('Unknown overlay')
        return self.root / oid

    def meta(self, oid): return json.loads((self.folder(oid) / 'meta.json').read_text(encoding='utf-8'))

    def save_meta(self, oid, meta):
        f = self.root / oid / 'meta.json'; tmp = f.with_suffix('.tmp'); tmp.write_text(json.dumps(meta), encoding='utf-8'); os.replace(tmp, f)

    def active(self):
        out = []
        for d in sorted(self.root.iterdir()):
            f = d / 'meta.json'
            if f.is_file():
                m = json.loads(f.read_text(encoding='utf-8'))
                if m.get('state') == 'active': out.append((d.name, m))
        return out

    def start(self, root):
        r = Path(root).expanduser()
        if not r.is_absolute() or not r.is_dir(): raise ValueError('root must be an absolute existing folder')
        r = real(str(r))
        if r in ('/', real('~')): raise ValueError('Choose a project folder, not the filesystem or home root')
        for oid, m in self.active():
            if m['root'] == r: raise ValueError(f'An overlay is already active for this folder: {oid}')
        oid = uuid.uuid4().hex[:12]; (self.root / oid / 'files').mkdir(parents=True, mode=0o700)
        self.save_meta(oid, {'root': r, 'state': 'active', 'created': time.time(), 'baseline': {}})
        return {'overlay_id': oid, 'root': r, 'notice': 'Agent read_file, write_file and patch calls inside this folder now go to a shadow copy. Terminal commands are not captured.'}

    def shadow_path(self, oid, m, path): return str(self.root / oid / 'files' / os.path.relpath(path, m['root']))

    def rewrite(self, tool_name, args):
        """Return modified args for a file tool call inside an active overlay, or None."""
        if tool_name not in REWRITE_TOOLS or not isinstance(args, dict) or not isinstance(args.get('path'), str): return None
        for oid, m in self.active():
            p = args['path']; full = real(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(m['root'], p))
            if not inside(m['root'], full): continue
            rel = os.path.relpath(full, m['root']); sh = Path(self.shadow_path(oid, m, full)); sh.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if rel not in m['baseline']:
                if os.path.isfile(full):
                    if os.path.getsize(full) > MAX_FILE: return {'action': 'block', 'message': f'Overlay: {rel} is larger than 2 MB and cannot be shadowed.'}
                    data = Path(full).read_bytes(); shutil.copy2(full, sh); m['baseline'][rel] = sha(data)
                else: m['baseline'][rel] = 'absent'
                self.save_meta(oid, m)
            return {'action': 'modify', 'args': {'path': str(sh)}}
        return None

    def terminal_guard(self, command):
        if not isinstance(command, str): return None
        command = re.sub(r'\d?>\s*/dev/null|\d>&\d', ' ', command)
        if not TERMINAL_WRITE.search(command): return None
        for oid, m in self.active():
            if True:
                return {'action': 'approve', 'message': f'Overlay is active for {m["root"]}: terminal writes go straight to disk and cannot be reviewed. Confirm this command is not editing project files.', 'rule_key': 'overlay-terminal'}
        return None

    def unmap(self, text):
        if not isinstance(text, str): return None
        out = text
        for oid, m in self.active():
            prefix = str(self.root / oid / 'files')
            if prefix in out: out = out.replace(prefix, m['root'])
        return out if out != text else None

    def files(self, oid):
        m = self.meta(oid); base = self.root / oid / 'files'; out = []
        for rel, baseline in sorted(m['baseline'].items()):
            sh = base / rel; realp = Path(m['root']) / rel
            old = realp.read_bytes() if realp.is_file() else b''; new = sh.read_bytes() if sh.is_file() else None
            if new is None or (baseline != 'absent' and sha(new) == baseline) or (baseline == 'absent' and not sh.is_file()): continue
            out.append({'rel': rel, 'new_file': baseline == 'absent', 'baseline': baseline, 'real_now': sha(old) if realp.is_file() else 'absent', 'old': old, 'new': new})
        return out

    @staticmethod
    def split(old, new):
        try: a, b = old.decode('utf-8').splitlines(keepends=True), new.decode('utf-8').splitlines(keepends=True)
        except UnicodeDecodeError: return None, None, None
        ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]
        return a, b, ops

    def diff(self, oid):
        out = []
        for f in self.files(oid):
            a, b, ops = self.split(f['old'], f['new'])
            if ops is None: out.append({'file': f['rel'], 'binary': True, 'new_file': f['new_file'], 'hunks': [{'hunk': 0, 'summary': 'binary or non-UTF-8 file: accept or reject as a whole'}]}); continue
            hunks = []
            for tag, i1, i2, j1, j2 in ops:
                if tag == 'equal': continue
                text = [f'@@ old {i1 + 1},{i2 - i1} new {j1 + 1},{j2 - j1} @@'] + [('-' + x.rstrip('\n'))[:200] for x in a[i1:i2][:12]] + [('+' + x.rstrip('\n'))[:200] for x in b[j1:j2][:12]]
                hunks.append({'hunk': len(hunks), 'removed': i2 - i1, 'added': j2 - j1, 'diff': '\n'.join(text)})
            out.append({'file': f['rel'], 'new_file': f['new_file'], 'conflict': f['baseline'] != 'absent' and f['real_now'] != f['baseline'] or (f['baseline'] == 'absent' and f['real_now'] != 'absent'), 'hunks': hunks[:MAX_HUNKS]})
        return {'overlay_id': oid, 'files': out, 'changed_files': len(out), 'hunks': sum(len(f['hunks']) for f in out)}

    def apply(self, oid, accept):
        if not isinstance(accept, dict): raise ValueError('accept maps a file to "all" or a list of hunk numbers')
        m = self.meta(oid); done, rejected, refused = [], [], []
        changed = {f['rel']: f for f in self.files(oid)}
        for rel in accept:
            if rel not in changed: raise ValueError(f'No pending change for {rel}')
        for rel, f in changed.items():
            choice = accept.get(rel, [])
            if f['baseline'] != 'absent' and f['real_now'] != f['baseline'] or (f['baseline'] == 'absent' and f['real_now'] != 'absent'):
                refused.append({'file': rel, 'reason': 'the real file changed since the overlay copied it; nothing applied for this file'}); continue
            a, b, ops = self.split(f['old'], f['new'])
            realp = Path(m['root']) / rel
            if ops is None:
                if choice == 'all': realp.parent.mkdir(parents=True, exist_ok=True); realp.write_bytes(f['new']); done.append({'file': rel, 'hunks': 'whole file'})
                else: rejected.append({'file': rel, 'hunks': 'whole file'})
                continue
            nonequal = [i for i, o in enumerate(ops) if o[0] != 'equal']; take = set(nonequal) if choice == 'all' else {nonequal[h] for h in choice if isinstance(h, int) and 0 <= h < len(nonequal)}
            if not isinstance(choice, (list, str)) or (isinstance(choice, str) and choice != 'all'): raise ValueError('accept values must be "all" or a list of hunk numbers')
            out = []
            for i, (tag, i1, i2, j1, j2) in enumerate(ops):
                out += b[j1:j2] if (tag != 'equal' and i in take) else a[i1:i2]
            if take:
                realp.parent.mkdir(parents=True, exist_ok=True); tmp = realp.with_name(realp.name + '.overlay-tmp'); tmp.write_text(''.join(out), encoding='utf-8'); os.replace(tmp, realp)
                done.append({'file': rel, 'hunks': sorted(nonequal.index(i) for i in take)})
            rej = [n for n, i in enumerate(nonequal) if i not in take]
            if rej: rejected.append({'file': rel, 'hunks': rej})
        m['state'] = 'applied'; self.save_meta(oid, m); shutil.rmtree(self.root / oid / 'files', ignore_errors=True)
        return {'applied': done, 'rejected': rejected, 'refused': refused, 'notice': 'The overlay is closed; rejected hunks are gone. The agent can be told what was rejected.'}

    def discard(self, oid):
        m = self.meta(oid); m['state'] = 'discarded'; self.save_meta(oid, m); shutil.rmtree(self.root / oid / 'files', ignore_errors=True); return {'discarded': oid}

    def status(self):
        return {'active': [{'overlay_id': oid, 'root': m['root'], 'files_touched': len(m['baseline'])} for oid, m in self.active()]}

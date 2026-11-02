"""
Game Model Exporter - replace one of the game's models with your own .blend.
Pick (1) your model and (2) the game's .blend.bin to replace; the rest is automatic.

Double-click to run (needs Python 3 for Windows + Blender).
Keep this file next to zigspec.py, gamemodel_blender.py and the 'specs' folder.
"""
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import zigspec  # noqa: E402

BACKEND = os.path.join(HERE, 'gamemodel_blender.py')
SPEC_DIR = os.path.join(HERE, 'specs')
APP = 'Game Model Exporter'
NO_PRESET = '(none)'
CFG_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~/.config'), 'GameModelExporter')
CFG = os.path.join(CFG_DIR, 'settings.json')
USER_SPEC_DIR = os.path.join(CFG_DIR, 'specs')        # where "Add spec" keeps the copy of the game's spec file


def spec_files():
    out = []
    for d in (SPEC_DIR, USER_SPEC_DIR):
        out += [p for p in sorted(glob.glob(os.path.join(d, '*.zig'))) if p not in out]
    return out
AUTO = '(from file name)'


def find_blender():
    exe = 'blender.exe' if os.name == 'nt' else 'blender'
    cands = [shutil.which('blender') or '']
    if os.name == 'nt':
        for root in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')):
            cands += glob.glob(os.path.join(root, 'Blender Foundation', 'Blender*', exe))
            cands += glob.glob(os.path.join(root, 'Steam', 'steamapps', 'common', 'Blender', exe))
        cands += glob.glob(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Blender*', '**', exe),
                           recursive=True)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'blendfile\shell\open\command') as k:
                m = re.match(r'"([^"]+)"|(\S+)', winreg.QueryValue(k, None))
                if m: cands.append(m.group(1) or m.group(2))
        except Exception:
            pass
    elif sys.platform == 'darwin':
        cands.append('/Applications/Blender.app/Contents/MacOS/Blender')

    def ver(p):
        n = re.findall(r'(\d+)\.(\d+)', p)
        return tuple(map(int, n[-1])) if n else (0, 0)
    found = sorted({c for c in cands if c and os.path.isfile(c)}, key=ver, reverse=True)
    return found[0] if found else ''


def guess_model(path, models):
    """Model name from a .blend file name, tolerant of copies/edits:
    'bike-4stroke.blend', 'Bike-4stroke_edited.blend', 'bike-4stroke (1).blend', 'my bike-4stroke v2.blend'."""
    import difflib
    stem = zigspec.model_from_filename(path)
    if stem in models: return stem
    low = {m.lower(): m for m in models}
    s = stem.lower()
    if s in low: return low[s]
    inside = [m for k, m in low.items() if k in s]                 # model name contained in file name
    if inside: return max(inside, key=len)
    norm = lambda x: re.sub(r'[^a-z0-9]', '', x.lower())
    hit = difflib.get_close_matches(norm(stem), [norm(m) for m in models], 1, 0.75)
    if hit: return next(m for m in models if norm(m) == hit[0])
    return stem


def load_cfg():
    try:
        with open(CFG) as f: return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG, 'w') as f: json.dump(d, f, indent=2)
    except Exception:
        pass


def run_lines(cmd, on_line, proc_holder=None):
    """Run a command, call on_line for each output line; returns exit code."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                         errors='replace', creationflags=0x08000000 if os.name == 'nt' else 0)
    if proc_holder is not None: proc_holder.append(p)
    for line in p.stdout: on_line(line.rstrip())
    return p.wait()


def game_model_name(path):
    """'.../bike-4stroke.blend.bin' or '.../bike-4stroke.blend.bin.orig' -> 'bike-4stroke'"""
    b = os.path.basename(path)
    if b.endswith('.orig'): b = b[:-5]
    return zigspec.model_from_filename(b)


FINISH_KEYS = {'Satin - soft sheen (recommended)': 'satin', 'Matte - no shine': 'matte',
               'Glossy - shiny plastic': 'gloss', 'From my Blender materials': 'blend'}
FINISH_LABELS = list(FINISH_KEYS)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry('1000x900')
        self.minsize(820, 600)
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.proc, self.cancel, self.busy = None, False, False
        self.specs, self.spec, self.spec_path = {}, None, None
        self.game_ok = False

        st = ttk.Style(self)
        for th in ('vista', 'clam'):
            if th in st.theme_names(): st.theme_use(th); break
        st.configure('Big.TButton', padding=(16, 7))
        st.configure('Hint.TLabel', foreground='#666')
        st.configure('Warn.TLabel', foreground='#b35c00')
        st.configure('Good.TLabel', foreground='#2e7d32')
        st.configure('Bad.TLabel', foreground='#c0392b')
        st.configure('Step.TLabelframe.Label', font=('Segoe UI', 10, 'bold'))
        st.configure('Treeview', rowheight=22)

        top = ttk.Frame(self, padding=(10, 10, 10, 0)); top.pack(fill='x')
        ttk.Label(top, text='Blender').pack(side='left')
        self.blender = tk.StringVar(value=self.cfg.get('blender') or find_blender())
        ttk.Entry(top, textvariable=self.blender).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(top, text='Browse…', command=self.pick_blender).pack(side='left')
        ttk.Button(top, text='Auto-detect', command=lambda: self.blender.set(find_blender())).pack(side='left', padx=(4, 0))

        nb = ttk.Notebook(self); nb.pack(fill='both', expand=True, padx=10, pady=(8, 6))
        self.nb = nb
        nb.add(self.build_export(nb), text='  Replace a game model  ')
        nb.add(self.build_spec_tab(nb), text='  Spec: models & nodes  ')
        nb.add(self.build_inspect(nb), text='  Inspect / Convert  ')
        self.status = tk.StringVar(value='Ready.')
        ttk.Label(self, textvariable=self.status, style='Hint.TLabel', padding=(12, 0, 12, 8)).pack(fill='x')
        self.load_specs()
        self.on_inputs()
        self.protocol('WM_DELETE_WINDOW', self.on_close)
        self.after(80, self.pump)

    def pick_blender(self):
        p = filedialog.askopenfilename(title='Locate Blender', filetypes=[('Blender', 'blender*.exe blender'), ('All', '*')])
        if p: self.blender.set(p)

    # ------------------------------------------------------------ specs (picked automatically)
    def load_specs(self):
        paths = spec_files()
        paths += [p for p in self.cfg.get('extra_specs', []) if os.path.isfile(p) and p not in paths]
        for p in paths:
            try: self.specs[p] = zigspec.ZigSpec.load(p)
            except Exception as e: self.status.set('Could not read %s: %s' % (os.path.basename(p), e))
        if self.specs:
            self.spec_path = next(iter(self.specs)); self.spec = self.specs[self.spec_path]
        self.fill_spec_tab()

    def spec_for_file(self, path):
        """The spec for a game file. When a game update gave the files a new version number but the same layout
        (any change to the game's lists of names changes the number), a matching spec is made automatically."""
        h = zigspec.sniff_header(path)
        if not h: return None
        sp = next((p for p, s in self.specs.items() if s.schema_hash == h), None)
        if sp: return sp
        for p in spec_files():
            if p not in self.specs:
                try: self.specs[p] = zigspec.ZigSpec.load(p)
                except Exception: pass
        sp = next((p for p, s in self.specs.items() if s.schema_hash == h), None)
        if sp: return sp
        cands = sorted(self.specs, key=lambda p: ('legacy' in os.path.basename(p).lower(),
                                                  'auto-' in os.path.basename(p).lower()))
        self.status.set('The game was updated (file version %s) - checking whether its files still read the same way…' % h)
        self.update_idletasks()
        try:
            out = zigspec.derive_spec(path, cands, USER_SPEC_DIR)
        except Exception as e:
            self.status.set("Couldn't check the updated game files: %s" % e); return None
        if not out:
            self.status.set('The updated game files (version %s) are laid out differently - a new spec is needed.' % h)
            return None
        self.specs[out] = zigspec.ZigSpec.load(out); self.fill_spec_tab()
        self.status.set('The game was updated (file version %s). Its files are laid out exactly as before, so the tool '
                        'made a matching spec by itself (%s).' % (h, os.path.basename(out)))
        return out

    def add_spec(self):
        p = filedialog.askopenfilename(title='Choose BlendMeshSpec.zig', filetypes=[('Zig spec', '*.zig')])
        if not p: return
        try: self.specs[p] = zigspec.ZigSpec.load(p)
        except Exception as e:
            messagebox.showerror(APP, 'That file is not a usable spec:\n%s' % e); return
        try:                                   # keep a copy so the map editor and the Blender add-on find it too
            os.makedirs(USER_SPEC_DIR, exist_ok=True)
            q = os.path.join(USER_SPEC_DIR, os.path.basename(p))
            if os.path.abspath(q) != os.path.abspath(p):
                shutil.copyfile(p, q); self.specs.pop(p, None); self.specs[q] = zigspec.ZigSpec.load(q); p = q
        except OSError:
            extra = self.cfg.setdefault('extra_specs', [])
            if p not in extra: extra.append(p)
        self.on_inputs()

    # ------------------------------------------------------------ main tab
    def build_export(self, parent):
        f = ttk.Frame(parent, padding=12)

        s1 = ttk.LabelFrame(f, text=' 1.  Your model ', style='Step.TLabelframe', padding=10); s1.pack(fill='x')
        self.blend = tk.StringVar(value=self.cfg.get('blend', ''))
        r = ttk.Frame(s1); r.pack(fill='x')
        ttk.Entry(r, textvariable=self.blend).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Browse…', command=self.pick_blend).pack(side='left', padx=(6, 0))
        self.import_btn = ttk.Button(r, text='Import MX Bikes / MX Sim bike…', command=self.import_bike)
        self.import_btn.pack(side='left', padx=(6, 0))
        self.shop_btn = ttk.Button(r, text='Paint & parts…', command=self.open_workshop)
        self.shop_btn.pack(side='left', padx=(6, 0))
        self.blend_info = ttk.Label(s1, text='', style='Hint.TLabel', wraplength=880); self.blend_info.pack(anchor='w', pady=(4, 0))
        r = ttk.Frame(s1); r.pack(fill='x', pady=(6, 0))
        ttk.Label(r, text='Line-up preset').pack(side='left')
        self.preset = tk.StringVar(value=self.cfg.get('preset', NO_PRESET))
        self.preset_box = ttk.Combobox(r, textvariable=self.preset, state='readonly', width=46,
                                       postcommand=self.fill_presets)
        self.preset_box.pack(side='left', padx=6)
        ttk.Label(r, text='used when your model has no saved line-up yet (save presets in Edit model)',
                  style='Hint.TLabel').pack(side='left')
        self.fill_presets()

        s2 = ttk.LabelFrame(f, text=' 2.  Game model to replace ', style='Step.TLabelframe', padding=10)
        s2.pack(fill='x', pady=(10, 0))
        self.game = tk.StringVar(value=self.cfg.get('game', ''))
        r = ttk.Frame(s2); r.pack(fill='x')
        ttk.Entry(r, textvariable=self.game).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Browse…', command=self.pick_game).pack(side='left', padx=(6, 0))
        self.game_info = ttk.Label(s2, text='', style='Hint.TLabel', wraplength=880); self.game_info.pack(anchor='w', pady=(4, 0))
        r = ttk.Frame(s2); r.pack(fill='x', pady=(6, 0))
        self.save_copy = tk.BooleanVar(value=self.cfg.get('save_copy', False))
        ttk.Checkbutton(r, text="Don't replace it - save the result to:", variable=self.save_copy,
                        command=self.on_inputs).pack(side='left')
        self.copy_path = tk.StringVar(value=self.cfg.get('copy_path', ''))
        self.copy_entry = ttk.Entry(r, textvariable=self.copy_path); self.copy_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.copy_btn = ttk.Button(r, text='Save as…', command=self.pick_copy); self.copy_btn.pack(side='left')
        for v in (self.blend, self.game, self.copy_path): v.trace_add('write', lambda *a: self.on_inputs())

        op = ttk.LabelFrame(f, text='Options', padding=8)
        self.opt = {}
        for i, (key, text, default) in enumerate((
                ('pivots', "Keep the game's pivot points (wheels spin on the game's axles)", True),
                ('mirror', 'Mirror my model left <-> right (e.g. exhaust on the wrong side)', False),
                ('split', 'Split my objects into loose parts and sort each one', True),
                ('mods', 'Apply modifiers', True),
                ('defer', 'Leave Lattice / Armature to the game', True),
                ('subdiv', 'Leave Subdivision to the game (off = applied, like the original bike)', False),
                ('double', 'Give open (thin) parts a back side - the game hides back faces', True),
                ('json', 'Also write _blend.json (debug)', False),
                ('force', 'Write even if checks fail (game will likely crash)', False))):
            v = tk.BooleanVar(value=self.cfg.get('opt_' + key, default)); self.opt[key] = v
            ttk.Checkbutton(op, text=text, variable=v).grid(row=i // 2, column=i % 2, sticky='w', padx=(0, 24), pady=1)
        fr = ttk.Frame(op); fr.grid(row=99, column=0, columnspan=2, sticky='w', pady=(4, 0))
        ttk.Label(fr, text='Finish of my parts:').pack(side='left')
        self.finish = tk.StringVar(value=self.cfg.get('finish', FINISH_LABELS[0]))
        ttk.Combobox(fr, textvariable=self.finish, values=FINISH_LABELS, state='readonly', width=44).pack(side='left', padx=6)

        tx = ttk.LabelFrame(f, text='Textures', padding=8)
        self.bring_tex = tk.BooleanVar(value=self.cfg.get('bring_tex', False))
        r = ttk.Frame(tx); r.pack(fill='x')
        ttk.Checkbutton(r, text="Bring my textures - packed into one image that replaces the game's",
                        variable=self.bring_tex, command=self.on_inputs).pack(side='left')
        self.tex_slot = tk.StringVar(value=self.cfg.get('tex_slot', ''))
        self.tex_slot_box = ttk.Combobox(r, textvariable=self.tex_slot, state='readonly', width=30)
        self.tex_slot_box.pack(side='left', padx=6)
        self.tex_slot_box.bind('<<ComboboxSelected>>', lambda e: (self.tex_file.set(''), self.on_inputs()))
        r = ttk.Frame(tx); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Texture file on disk:').pack(side='left')
        self.tex_file = tk.StringVar(value=self.cfg.get('tex_file', ''))
        self.tex_entry = ttk.Entry(r, textvariable=self.tex_file); self.tex_entry.pack(side='left', fill='x', expand=True, padx=6)
        self.tex_btn = ttk.Button(r, text='Browse…', command=self.pick_tex); self.tex_btn.pack(side='left')
        r = ttk.Frame(tx); r.pack(fill='x', pady=(4, 0))
        for key, text, default in (('bake_n', "Bake my normal maps into the colours (the bike has no normal-map slot)", True),
                                   ('alpha', 'Keep transparency (alpha)', False)):
            v = tk.BooleanVar(value=self.cfg.get('opt_' + key, default)); self.opt[key] = v
            ttk.Checkbutton(r, text=text, variable=v).pack(side='left', padx=(0, 18))
        self.tex_info = ttk.Label(tx, text='', style='Hint.TLabel', wraplength=880); self.tex_info.pack(anchor='w', pady=(4, 0))
        self.tex_file.trace_add('write', lambda *a: self.on_inputs())

        act = ttk.Frame(f)
        self.align_btn = ttk.Button(act, text='Edit model (3D)…', command=self.align)
        self.align_btn.pack(side='left')
        self.review_btn = ttk.Button(act, text='Review matching…', command=self.review)
        self.review_btn.pack(side='left', padx=(6, 0))
        self.check_btn = ttk.Button(act, text='Check', command=lambda: self.start(True)); self.check_btn.pack(side='left', padx=6)
        self.go = ttk.Button(act, text='Export', style='Big.TButton', command=lambda: self.start(False))
        self.go.pack(side='left')
        self.stop = ttk.Button(act, text='Cancel', command=self.cancel_run, state='disabled'); self.stop.pack(side='left', padx=6)
        ttk.Button(act, text='Map editor (heightmap studio)…', command=self.map_editor).pack(side='right')
        self.prog = ttk.Progressbar(act, mode='determinate'); self.prog.pack(side='left', fill='x', expand=True, padx=(6, 0))

        self.log = tk.Text(f, height=10, wrap='word', font=('Consolas', 9), state='disabled', relief='flat',
                           background='#1e1e1e', foreground='#d4d4d4', padx=6, pady=4)
        for tag, col in (('err', '#f48771'), ('ok', '#89d185'), ('warn', '#e2c08d'), ('dim', '#8a8a8a')):
            self.log.tag_configure(tag, foreground=col)
        op.pack(fill='x', pady=(10, 0))
        tx.pack(fill='x', pady=(10, 0))
        act.pack(fill='x', pady=(12, 0))
        self.log.pack(fill='both', expand=True, pady=(8, 0))
        return f

    def pick_blend(self):
        p = filedialog.askopenfilename(title='Your model', filetypes=[('Blender files', '*.blend'), ('All', '*')])
        if p: self.blend.set(os.path.normpath(p))

    # ------------------------------------------------------------ line-up presets
    def fill_presets(self):
        import lineup_presets
        names = [NO_PRESET] + [p['name'] for p in lineup_presets.list_presets()]
        self.preset_box['values'] = names
        if self.preset.get() not in names: self.preset.set(NO_PRESET)

    def preset_path(self):
        import lineup_presets
        n = self.preset.get()
        p = lineup_presets.find(n) if n and n != NO_PRESET else None
        return p['path'] if p else None

    def import_info(self, blend=None):
        p = (blend or self.blend.get().strip()) + '.import.json'
        try:
            with open(p) as fh: return json.load(fh)
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------ importing MX Bikes / MX Simulator bikes
    def choose(self, title, text, items):
        """A small list dialog; returns the chosen item or None."""
        d = tk.Toplevel(self); d.title(title); d.transient(self); d.grab_set()
        ttk.Label(d, text=text, wraplength=420, justify='left', padding=(10, 10, 10, 4)).pack(anchor='w')
        lb = tk.Listbox(d, height=min(14, max(4, len(items))), width=60, activestyle='dotbox')
        for it in items: lb.insert('end', it)
        lb.pack(fill='both', expand=True, padx=10); lb.selection_set(0)
        out = {}
        def ok(_e=None):
            s = lb.curselection()
            if s: out['v'] = items[s[0]]
            d.destroy()
        lb.bind('<Double-1>', ok)
        b = ttk.Frame(d, padding=10); b.pack(fill='x')
        ttk.Button(b, text='OK', command=ok).pack(side='right')
        ttk.Button(b, text='Cancel', command=d.destroy).pack(side='right', padx=6)
        self.wait_window(d)
        return out.get('v')

    def import_bike(self):
        """An MX Bikes model.edf or an MX Simulator .saf -> a .blend with the bike assembled, ready to line up."""
        blender = self.blender.get().strip()
        if not blender or not os.path.isfile(blender):
            messagebox.showerror(APP, 'Set the path to blender.exe first.'); return
        p = filedialog.askopenfilename(title='Import a bike', filetypes=[
            ('MX Bikes / MX Simulator bikes', '*.edf *.saf'), ('MX Bikes model', '*.edf'),
            ('MX Simulator bike', '*.saf'), ('All', '*')])
        if not p: return
        try:
            import numpy  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            messagebox.showerror(APP, 'Importing bikes needs numpy and pillow (pip install numpy pillow).'); return
        import bike_import
        kw, family = {}, None
        try:
            if p.lower().endswith('.saf'):
                family = 'MX Simulator'
                bikes = bike_import.mxs_bikes(p)
                if not bikes:
                    messagebox.showerror(APP, 'No MX Simulator bike models (.jm) in %s - it may be a track or gear.'
                                         % os.path.basename(p)); return
                b = bikes[0]
                if len(bikes) > 1:
                    k = self.choose('Import a bike', '%s holds %d bikes. Which one?' % (os.path.basename(p), len(bikes)),
                                    [x['key'] for x in bikes])
                    if not k: return
                    b = next(x for x in bikes if x['key'] == k)
                skins = [sk for sk in b['skins'] if not sk[3]]
                if len(skins) > 1:
                    sk = self.choose('Import a bike', 'Which skin (texture)?', [sk[0] for sk in skins])
                    if not sk: return
                    kw['skin'] = sk
                kw['bike_key'] = b['key']
                self.on_inputs()
                t = self.template()
                if not (self.game_ok and t):
                    messagebox.showerror(APP, 'Step 2 first: pick the game bike to replace. MX Simulator bikes come '
                                              'as separate parts, and they are put together around the game bike\'s '
                                              'axles and handlebars.'); return
                with open(t, 'rb') as fh: kw['boxes'] = bike_import.template_boxes(self.spec.decode(fh.read()))
            else:
                family = 'MX Bikes'
        except Exception as e:
            messagebox.showerror(APP, 'Could not read %s:\n%s' % (os.path.basename(p), e)); return
        self.set_busy(True); self.import_btn.configure(state='disabled')
        self.status.set('Importing %s…' % os.path.basename(p))
        self.write_log('▶ importing %s (%s)' % (os.path.basename(p), family))

        def work():
            try:
                if family == 'MX Simulator':
                    parts, tex, info = bike_import.load_mxs(p, **kw)
                else:
                    parts, tex, info = bike_import.load_edf(p)
                name = info['key'] if family == 'MX Bikes' else '%s %s' % (info.get('model'), info.get('skin') or '')
                name = ' '.join(name.split())
                folder = bike_import.import_folder(name.strip())
                jp = bike_import.write_parts(folder, parts, tex)
                out = os.path.join(folder, re.sub(r'[^A-Za-z0-9_.-]+', '_', name.strip()) + '.blend')
                cmd = [blender, '-b', '--factory-startup', '--python-exit-code', '1', '--python', BACKEND, '--',
                       '--build-parts', jp, '--save-blend', out]
                lines = []
                code = run_lines(cmd, lines.append)
                if not os.path.isfile(out):
                    self.q.put(('log', '  ✘ Blender could not build the model:', 'err'))
                    for line in lines[-12:]: self.q.put(('log', '    ' + line, 'err'))
                    self.q.put(('finish', 'Import failed.')); return
                with open(out + '.import.json', 'w') as fh:
                    json.dump({'family': family, 'source': p, 'bike': info.get('key'), 'skin': info.get('skin'),
                               'notes': info.get('notes', [])}, fh, indent=1)
                for n in info.get('notes', []): self.q.put(('log', '  · ' + n, 'dim'))
                self.q.put(('log', '  ✔ %s - %d parts, saved as %s' % (info.get('key'), len(parts), out), 'ok'))
                self.q.put(('call', lambda: self.after_import(out, family)))
                self.q.put(('finish', 'Imported %s.' % info.get('key')))
            except Exception as e:
                self.q.put(('log', '  ✘ %s' % e, 'err')); self.q.put(('finish', 'Import failed.'))
            finally:
                self.q.put(('call', lambda: self.import_btn.configure(state='normal')))
        threading.Thread(target=work, daemon=True).start()

    def open_workshop(self, blend=None):
        """Paint / parts live in Edit model (3D): open it on its Paint tab."""
        b = self.blend.get().strip()
        if not b or not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(b)), 'parts.json')):
            messagebox.showinfo(APP, 'Painting and parts work on bikes brought in with "Import MX Bikes / MX Sim bike…" - '
                                     'import one first (or pick one from Documents\\GameModelExporter\\imports).')
            return
        self.align(start_mode='paint')

    def after_import(self, blend, family):
        import lineup_presets
        self.blend.set(os.path.normpath(blend))
        self.fill_presets()
        pr = lineup_presets.best_for(family, self.model() if self.game_ok else None)
        self.write_log('  paint it, or delete / add parts: "Paint & parts…" (or the Paint tab in Edit model)', 'dim')
        if pr:
            self.preset.set(pr['name'])
            self.write_log('  using your line-up preset "%s" - open Edit model to check it' % pr['name'], 'ok')
        else:
            self.preset.set(NO_PRESET)
            self.write_log('  next: Edit model → Auto align, adjust, then "Save as line-up preset" - every %s bike '
                           'after this one lines up the same way' % family, 'dim')

    def pick_game(self):
        p = filedialog.askopenfilename(title='Game model to replace',
                                       filetypes=[('Game model', '*.blend.bin *.bin *.orig'), ('All', '*')])
        if p:
            if p.endswith('.orig'): p = p[:-5]          # the backup was picked: target the real file
            self.game.set(os.path.normpath(p))

    def pick_tex(self):
        p = filedialog.askopenfilename(title="The game's texture file (%s)" % os.path.basename(self.tex_slot.get() or 'png'),
                                       filetypes=[('Images', '*.png'), ('All', '*')])
        if p: self.tex_file.set(os.path.normpath(p))

    def find_texture_file(self, slot):
        """Where the game keeps a texture slot on disk: relative to the game model file, else searched nearby."""
        g = self.game.get().strip()
        if not g or not slot: return None
        base = os.path.dirname(g)
        cand = os.path.normpath(os.path.join(base, slot))
        if os.path.isfile(cand) or os.path.isfile(cand + '.orig'): return cand
        tail = [x for x in slot.replace('\\', '/').split('/') if x not in ('', '.', '..')]
        d = base
        for _ in range(4):
            for root, dirs, files in os.walk(d):
                if root[len(d):].count(os.sep) > 3: dirs[:] = []; continue
                if tail[-1] in files and root.replace('\\', '/').endswith('/'.join(tail[:-1])):
                    return os.path.join(root, tail[-1])
            nd = os.path.dirname(d)
            if nd == d: break
            d = nd
        return None

    def pick_copy(self):
        m = self.model() or 'model'
        p = filedialog.asksaveasfilename(title='Save the result as', defaultextension='.bin',
                                         initialfile=m + '.blend.bin', filetypes=[('Game model', '*.bin')])
        if p: self.copy_path.set(os.path.normpath(p)); self.save_copy.set(True); self.on_inputs()

    # --- derived state
    def model(self):
        g = self.game.get().strip()
        return game_model_name(g) if g else None

    def template(self):
        """The game's original: the .orig backup if we already replaced it once, else the file itself."""
        g = self.game.get().strip()
        if not g: return None
        return zigspec.original_of(g)

    def out_path(self):
        return self.copy_path.get().strip() if self.save_copy.get() else self.game.get().strip()

    def map_path(self, blend=None):
        return (blend or self.blend.get().strip()) + '.gamemap.json'

    def refresh_files(self):
        self.on_inputs()

    def on_inputs(self):
        if not hasattr(self, 'game_info'): return
        state = 'normal' if self.save_copy.get() else 'disabled'
        self.copy_entry.configure(state=state); self.copy_btn.configure(state=state)
        b = self.blend.get().strip()
        if not b:
            self.blend_info.configure(text='Any .blend - objects and materials can be named anything.', style='Hint.TLabel')
        elif not os.path.isfile(b):
            self.blend_info.configure(text='File not found.', style='Bad.TLabel')
        else:
            mp = os.path.isfile(self.map_path(b))
            self.blend_info.configure(text='✔ ' + ('matching reviewed and saved' if mp else 'parts will be matched automatically'),
                                      style='Good.TLabel')
        self.game_ok = False
        g = self.game.get().strip()
        t = self.template()
        if not g:
            self.game_info.configure(text="Pick the game's own file, e.g. bike-4stroke.blend.bin. It is the template "
                                          "for positions, pivots and mount points.", style='Hint.TLabel'); return
        if not t:
            self.game_info.configure(text='File not found.', style='Bad.TLabel'); return
        h = zigspec.sniff_header(t)
        if not h:
            self.game_info.configure(text='Not a game model file (no BinarySerializer header).', style='Bad.TLabel'); return
        sp = self.spec_for_file(t)
        if sp is None:
            self.game_info.configure(text='Game file version %s - no spec for it, and its layout differs from every '
                                          'spec the tool has. Ask the dev for the matching BlendMeshSpec.zig and add '
                                          'it on the Spec tab.' % h, style='Bad.TLabel'); return
        if self.spec_path != sp:
            self.spec_path, self.spec = sp, self.specs[sp]; self.fill_spec_tab()
        m = self.model()
        if m not in self.spec.models:
            self.game_info.configure(text='"%s" is not one of the game\'s models (%s).' % (
                m, ', '.join(sorted(self.spec.models))), style='Bad.TLabel'); return
        self.game_ok = True
        self.update_textures()
        where = ('result saved to the file above' if self.save_copy.get() else
                 'this file is replaced; the original is kept as %s.orig' % os.path.basename(g))
        self.game_info.configure(text='✔ %s · %d game parts · version %s · %s%s' % (
            m, len(self.spec.models[m]), h, where, '' if t == g else '  (template: the .orig backup)'), style='Good.TLabel')

    def update_textures(self):
        if not self.spec: return
        slots = self.spec.textures or []
        self.tex_slot_box['values'] = slots
        if self.tex_slot.get() not in slots:
            self.tex_slot.set(next((t for t in slots if 'crashpad' in t), slots[0] if slots else ''))
        on = self.bring_tex.get()
        for w in (self.tex_entry, self.tex_btn): w.configure(state='normal' if on else 'disabled')
        self.tex_slot_box.configure(state='readonly' if on else 'disabled')
        if not on:
            self.tex_info.configure(text='Off: the game can only load its own textures, so yours are left off and '
                                         'each material shows its colour. Or use the game\'s materials in Edit model.',
                                    style='Hint.TLabel'); return
        if not self.tex_file.get().strip():
            f = self.find_texture_file(self.tex_slot.get())
            if f: self.tex_file.set(f); return
        f = self.tex_file.get().strip()
        if f and (os.path.isfile(f) or os.path.isfile(f + '.orig')):
            self.tex_info.configure(text='✔ your textures (and colours) will be packed into %s - the original is kept as '
                                         '.orig, and yours is written at the same size. Anything else in the game using '
                                         'this texture will show yours too.' % f, style='Good.TLabel')
        else:
            self.tex_info.configure(text="Couldn't find %s next to the game files. Use Browse to pick it (it's in the "
                                         "game's data folder)." % self.tex_slot.get(), style='Bad.TLabel')

    def texture_args(self, writing):
        if not self.bring_tex.get() or not self.tex_slot.get(): return []
        a = ['--texture-slot', self.tex_slot.get()]
        if not self.opt['bake_n'].get(): a.append('--no-bake-normals')
        if self.opt['alpha'].get(): a.append('--keep-alpha')
        if writing and self.tex_file.get().strip(): a += ['--texture-file', self.tex_file.get().strip()]
        return a

    # --- running
    def write_log(self, text, tag=None):
        self.log.configure(state='normal'); self.log.insert('end', text + '\n', tag)
        self.log.see('end'); self.log.configure(state='disabled')

    def preflight(self, need_out=True):
        blender = self.blender.get().strip()
        if not blender or not os.path.isfile(blender):
            messagebox.showerror(APP, 'Set the path to blender.exe first.'); return None
        if not os.path.isfile(self.blend.get().strip()):
            messagebox.showerror(APP, 'Step 1: choose your model (.blend).'); return None
        self.on_inputs()
        if not self.game_ok:
            messagebox.showerror(APP, 'Step 2: choose the game model to replace.\n\n' + self.game_info.cget('text')); return None
        if need_out and self.bring_tex.get() and not (os.path.isfile(self.tex_file.get().strip()) or
                                                     os.path.isfile(self.tex_file.get().strip() + '.orig')):
            messagebox.showerror(APP, 'Textures: pick the game texture file to replace (or untick "Bring my textures").')
            return None
        if need_out and self.save_copy.get() and not self.copy_path.get().strip():
            messagebox.showerror(APP, 'Choose where to save the result (or untick "Don\'t replace it").'); return None
        return blender

    def base_cmd(self, blender):
        o = {k: v.get() for k, v in self.opt.items()}
        cmd = [blender, '-b', self.blend.get().strip(), '--factory-startup', '--python-exit-code', '1',
               '--python', BACKEND, '--', '--spec', self.spec_path, '--model', self.model(),
               '--auto', '--mapping', self.map_path(), '--reference', self.template()]
        if not o['pivots']: cmd += ['--pivots', 'yours']
        if not o['split']: cmd.append('--whole-objects')
        if not o['mods']: cmd.append('--no-modifiers')
        if not o['defer']: cmd.append('--bake-deform')
        if not o['double']: cmd.append('--single-sided')
        if o['subdiv']: cmd.append('--defer-subdiv')
        if o['mirror']: cmd.append('--mirror')
        cmd += ['--finish', FINISH_KEYS.get(self.finish.get(), 'satin')]
        pp = self.preset_path()
        if pp: cmd += ['--preset', pp]
        return cmd, o

    def set_busy(self, b):
        self.busy, self.cancel = b, False
        for w in (self.go, self.check_btn, self.review_btn, self.align_btn): w.configure(state='disabled' if b else 'normal')
        self.stop.configure(state='normal' if b else 'disabled')
        if b: self.prog['value'] = 0

    def cancel_run(self):
        self.cancel = True
        if self.proc and self.proc.poll() is None: self.proc.kill()

    def start(self, check_only):
        blender = self.preflight(need_out=not check_only)
        if not blender: return
        self.persist()
        cmd, o = self.base_cmd(blender)
        out = self.out_path()
        cmd += ['--export', out]
        cmd += self.texture_args(writing=not check_only)
        if check_only: cmd.append('--check')
        else:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            if o['json']: cmd += ['--json', os.path.splitext(out)[0].replace('.blend', '_blend') + '.json']
            if o['force']: cmd.append('--force')
        self.set_busy(True)
        self.write_log('▶ %s  →  replaces %s' % (os.path.basename(self.blend.get()), self.model()))
        threading.Thread(target=self.worker, args=(cmd, check_only), daemon=True).start()

    def worker(self, cmd, check_only):
        state = {'tail': [], 'result': None, 'nerr': 0}

        def on_line(line):
            m = re.match(r'PROGRESS (\d+) (\d+) ', line)
            if m:
                self.q.put(('prog', int(m.group(1)) / int(m.group(2)) * 100)); return
            kind = line.split(' ', 1)[0]; msg = line[len(kind) + 1:]
            if kind == 'ERROR': state['nerr'] += 1; self.q.put(('log', '  ✘ ' + msg, 'err'))
            elif kind == 'WARN': self.q.put(('log', '  ⚠ ' + msg, 'warn'))
            elif kind == 'INFO' and not msg.startswith('   '): self.q.put(('log', '  ' + msg, 'dim'))
            elif kind in ('DONE', 'CHECKED', 'FAILED'): state['result'] = (kind, msg)
            elif kind != 'INFO': state['tail'] = (state['tail'] + [line])[-20:]
        holder = []
        try:
            t = threading.Thread(target=lambda: run_lines(cmd, on_line, holder)); t.start()
            while t.is_alive():
                if holder: self.proc = holder[0]
                t.join(0.1)
        except OSError as e:
            self.q.put(('log', '  could not start Blender: %s' % e, 'err'))
        res = state['result']
        if res and res[0] == 'DONE':
            self.q.put(('log', '  ✔ ' + res[1], 'ok')); msg = 'Exported - start the game to see your model.'
        elif res and res[0] == 'CHECKED':
            good = res[1].startswith('ok')
            self.q.put(('log', '  ✔ ready to export' if good else '  ✘ %d problem(s)' % state['nerr'], 'ok' if good else 'err'))
            msg = 'Check passed.' if good else 'Check found problems.'
        elif res and res[0] == 'FAILED':
            self.q.put(('log', '  ' + res[1], 'err')); msg = 'Nothing written.'
        elif self.cancel:
            msg = 'Cancelled.'
        else:
            self.q.put(('log', '  ✘ Blender failed. Last output:', 'err'))
            for line in state['tail']:
                if line.strip(): self.q.put(('log', '    ' + line, 'err'))
            msg = 'Blender error.'
        self.q.put(('prog', 100 if not self.cancel else 0))
        self.q.put(('finish', msg))

    # ------------------------------------------------------------ 3D alignment
    def align(self, start_mode='lineup'):
        missing = []
        for mod, pkg in (('numpy', 'numpy'), ('PIL.ImageTk', 'pillow'), ('OpenGL.GL', 'PyOpenGL'),
                         ('pyopengltk', 'pyopengltk')):
            try: __import__(mod)
            except ImportError: missing.append(pkg)
        if missing and messagebox.askyesno(APP, 'The 3D editor needs %s (free libraries) to draw your model on '
                                                'your graphics card, like Blender\'s viewport.\nInstall now? (runs "pip install %s")'
                                                % (' and '.join(missing), ' '.join(missing))):
            py = sys.executable
            if os.path.basename(py).lower() == 'pythonw.exe': py = os.path.join(os.path.dirname(py), 'python.exe')
            self.write_log('▶ installing %s…' % ', '.join(missing)); self.update_idletasks()
            r = subprocess.run([py, '-m', 'pip', 'install', '--user'] + missing, capture_output=True, text=True,
                               creationflags=0x08000000 if os.name == 'nt' else 0)
            self.write_log('  ✔ installed' if r.returncode == 0 else '  ✘ could not install - the editor shows dots '
                           'instead of a shaded model', 'ok' if r.returncode == 0 else 'warn')
            import importlib, site
            try: site.addsitedir(site.getusersitepackages())
            except Exception: pass
            importlib.invalidate_caches()
            import align_editor as ae
            try:
                import numpy as _np; ae.np = _np
            except ImportError: pass
            try:
                from PIL import Image as _I, ImageDraw as _D, ImageTk as _T
                ae.Image, ae.ImageDraw, ae.ImageTk = _I, _D, _T
            except ImportError: pass
        blender = self.preflight(need_out=False)
        if not blender: return
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), 'gme_preview_%d.json' % os.getpid())
        cmd, _ = self.base_cmd(blender)
        cmd += ['--preview', tmp, '--export', self.out_path() or self.game.get().strip()]
        cmd += self.texture_args(writing=False)          # the in-game view shows your packed textures
        self.set_busy(True)
        blend, model = self.blend.get().strip(), self.model()
        self.status.set('Preparing the 3D view of %s…' % os.path.basename(blend))
        self.write_log('▶ preparing 3D alignment for %s → %s' % (os.path.basename(blend), model))

        def work():
            lines = []
            run_lines(cmd, lines.append)
            try:
                with open(tmp) as fh: data = json.load(fh)
            except Exception:
                self.q.put(('log', '  ✘ could not prepare the 3D view:', 'err'))
                for line in lines[-15:]: self.q.put(('log', '    ' + line, 'err'))
                self.q.put(('finish', 'Failed.')); return
            for line in lines:
                if line.startswith('INFO') and not line.startswith('INFO    '):
                    self.q.put(('log', '  ' + line[5:], 'dim'))
            self.q.put(('finish', 'Opened the 3D alignment view.'))
            import align_editor
            self.q.put(('call', lambda: align_editor.AlignEditor(self, blend, model, data, self.map_path(blend),
                                                                 start_mode=start_mode)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ map editor
    def ensure_libs(self, mods, why):
        """Offer to pip-install missing libraries; returns True when they import."""
        import importlib, site
        missing = []
        for mod, pkg in mods:
            try: importlib.import_module(mod)
            except ImportError: missing.append(pkg)
        if not missing: return True
        if not messagebox.askyesno(APP, '%s needs %s (free libraries).\nInstall now? (runs "pip install %s")'
                                   % (why, ' and '.join(missing), ' '.join(missing))):
            return False
        py = sys.executable
        if os.path.basename(py).lower() == 'pythonw.exe': py = os.path.join(os.path.dirname(py), 'python.exe')
        r = subprocess.run([py, '-m', 'pip', 'install', '--user'] + missing, capture_output=True, text=True,
                           creationflags=0x08000000 if os.name == 'nt' else 0)
        try: site.addsitedir(site.getusersitepackages())
        except Exception: pass
        importlib.invalidate_caches()
        if r.returncode != 0:
            messagebox.showerror(APP, "Couldn't install %s:\n%s" % (' '.join(missing), (r.stderr or '')[-600:])); return False
        return True

    def heightmap_studio(self):
        if not self.ensure_libs((('numpy', 'numpy'), ('PIL.ImageTk', 'pillow')), 'The heightmap studio'): return
        self.ensure_libs((('OpenGL', 'PyOpenGL'), ('pyopengltk', 'pyopengltk')), "The studio's 3D view")
        import hm_studio
        hm_studio.Studio(self, on_send=self._studio_to_map)

    def _studio_to_map(self, proj, H, info):
        """'Send to map editor' from a studio opened here: open the map editor (if needed) and add it."""
        import map_editor as ME
        eds = [w for w in self.winfo_children() if isinstance(w, ME.MapEditor)]
        if eds and eds[0].t is not None:
            return eds[0].receive_studio(proj, H, info)
        if not eds:
            self.map_editor()
            eds = [w for w in self.winfo_children() if isinstance(w, ME.MapEditor)]
            if not eds: return 'Open the map editor first.'
        ed = eds[0]

        def later():
            if ed.t is None: self.after(400, later)
            else: ed.receive_studio(proj, H, info)
        later()
        return 'Opening the map editor - it is placed in the middle of its view when the map has loaded.'

    def map_editor(self):
        if not self.ensure_libs((('numpy', 'numpy'), ('PIL.ImageTk', 'pillow')), 'The map editor'): return
        g = self.game.get().strip()
        mp = os.path.join(os.path.dirname(g), 'map.blend.bin') if g else ''
        if not os.path.isfile(mp):
            mp = filedialog.askopenfilename(title="The game's map (content\\meshes\\map.blend.bin)",
                                            filetypes=[('Game map', 'map.blend.bin'), ('All', '*.*')])
            if not mp: return
        if os.path.getsize(mp) == 0 or not zigspec.sniff_header(mp):
            bk = mp + '.orig'
            if os.path.isfile(bk) and os.path.getsize(bk) > 0 and zigspec.sniff_header(bk):
                if messagebox.askyesno(APP, "The game's map file (map.blend.bin) is empty or damaged - that is what "
                                       "makes the game stop with \"EndOfStream\".\n\nPut the backup of the original "
                                       "(map.blend.bin.orig) back?"):
                    try:
                        import world_editor
                        world_editor.safe_copy(bk, mp)
                    except OSError as e:
                        messagebox.showerror(APP, "Couldn't put it back:\n%s" % e); return
                else:
                    return
            else:
                messagebox.showerror(APP, "The game's map file (map.blend.bin) is empty or damaged - that is what "
                                     "makes the game stop with \"EndOfStream\".\n\nIn Steam: right-click Senders "
                                     "Playtest > Properties > Installed Files > \"Verify integrity of game files\". "
                                     "That downloads the original map again; your map work is kept in the editor.")
                return
        meshes = os.path.dirname(os.path.abspath(mp))
        root = os.path.dirname(os.path.dirname(meshes))
        spec = None
        try:
            spec = self.spec_for_file(zigspec.original_of(mp) or mp)
        except Exception:
            pass
        if not spec:
            messagebox.showerror(APP, "No spec file matches this map file. Add the game's BlendMeshSpec.zig on the Spec tab (see README: SPEC FILE).")
            return
        self.ensure_libs((('OpenGL', 'PyOpenGL'), ('pyopengltk', 'pyopengltk'), ('tkinterdnd2', 'tkinterdnd2')),
                         "The map editor's 3D view and drag-and-drop")
        import world_editor
        for w in self.winfo_children():
            if isinstance(w, world_editor.WorldEditor):
                w.lift(); return
        world_editor.WorldEditor(self, mp, spec, root if os.path.isdir(os.path.join(root, 'content')) else None)

    # ------------------------------------------------------------ matching review
    def review(self, remap=False):
        blender = self.preflight(need_out=False)
        if not blender: return
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), 'gme_scan_%d.json' % os.getpid())
        cmd, _ = self.base_cmd(blender)
        cmd += ['--scan', tmp, '--export', self.out_path() or self.game.get().strip()]
        if remap: cmd.append('--remap')
        self.set_busy(True)
        blend, model = self.blend.get().strip(), self.model()
        self.status.set('Looking at %s…' % os.path.basename(blend))
        self.write_log('▶ matching %s → %s' % (os.path.basename(blend), model))

        def work():
            lines = []
            run_lines(cmd, lines.append)
            try:
                with open(tmp) as fh: data = json.load(fh)
            except Exception:
                self.q.put(('log', '  ✘ could not scan the file:', 'err'))
                for line in lines[-15:]: self.q.put(('log', '    ' + line, 'err'))
                self.q.put(('finish', 'Scan failed.')); return
            self.q.put(('finish', 'Scanned %s.' % os.path.basename(blend)))
            self.q.put(('call', lambda: MappingEditor(self, blend, model, data)))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ spec tab
    def build_spec_tab(self, parent):
        f = ttk.Frame(parent, padding=10)
        r = ttk.Frame(f); r.pack(fill='x')
        self.spec_label = tk.StringVar()
        ttk.Label(r, textvariable=self.spec_label, style='Hint.TLabel').pack(side='left')
        ttk.Button(r, text='Add spec…', command=self.add_spec).pack(side='right')
        pw = ttk.PanedWindow(f, orient='horizontal'); pw.pack(fill='both', expand=True, pady=(6, 0))
        left = ttk.Frame(pw); right = ttk.Frame(pw)
        pw.add(left, weight=1); pw.add(right, weight=3)
        self.model_list = ttk.Treeview(left, columns=('n',), show='tree headings', selectmode='browse')
        self.model_list.heading('#0', text='Model (BlendId)', anchor='w'); self.model_list.heading('n', text='Nodes')
        self.model_list.column('#0', width=170); self.model_list.column('n', width=60, anchor='e')
        self.model_list.pack(fill='both', expand=True)
        self.model_list.bind('<<TreeviewSelect>>', lambda e: self.show_model_nodes())
        self.node_list = ttk.Treeview(right, columns=('i',), show='tree headings')
        self.node_list.heading('#0', text='Node name', anchor='w'); self.node_list.heading('i', text='#')
        self.node_list.column('i', width=50, anchor='e')
        sb = ttk.Scrollbar(right, orient='vertical', command=self.node_list.yview)
        self.node_list.configure(yscrollcommand=sb.set)
        self.node_list.pack(side='left', fill='both', expand=True); sb.pack(side='left', fill='y')
        tf = ttk.LabelFrame(f, text='Textures the game knows (TextureId)', padding=6)
        tf.pack(fill='x', pady=(8, 0))
        self.tex_text = tk.Text(tf, height=5, wrap='none', font=('Consolas', 9), relief='flat')
        self.tex_text.pack(fill='x')
        return f

    def fill_spec_tab(self):
        if not hasattr(self, 'model_list') or not self.spec: return
        self.spec_label.set('%s (version %s) - picked automatically from the game file. Each game model must '
                            'contain exactly these parts.' % (os.path.basename(self.spec_path), self.spec.schema_hash))
        self.model_list.delete(*self.model_list.get_children())
        for m, nodes in sorted(self.spec.models.items(), key=lambda kv: kv[0].lower()):
            self.model_list.insert('', 'end', iid=m, text=m, values=(len(nodes),))
        self.tex_text.configure(state='normal'); self.tex_text.delete('1.0', 'end')
        self.tex_text.insert('end', '\n'.join(self.spec.textures or ['(spec has no TextureId list)']))
        self.tex_text.configure(state='disabled')
        cur = self.model() if hasattr(self, 'game') else None
        first = cur if cur in self.spec.models else 'bike-4stroke' if 'bike-4stroke' in self.spec.models else next(iter(self.spec.models), None)
        if first: self.model_list.selection_set(first); self.show_model_nodes()

    def show_model_nodes(self):
        sel = self.model_list.selection()
        if not sel: return
        self.node_list.delete(*self.node_list.get_children())
        for i, n in enumerate(self.spec.models[sel[0]]):
            self.node_list.insert('', 'end', text=n, values=(i,))

    # ------------------------------------------------------------ inspect tab
    def build_inspect(self, parent):
        f = ttk.Frame(parent, padding=10)
        row = ttk.Frame(f); row.pack(fill='x')
        ttk.Button(row, text='Open .bin / .json…', command=self.open_inspect).pack(side='left')
        self.conv_btn = ttk.Button(row, text='Save as…', command=self.convert, state='disabled')
        self.conv_btn.pack(side='left', padx=6)
        self.insp_label = tk.StringVar(value='Open a .blend.bin or _blend.json to see what is inside and check it.')
        ttk.Label(row, textvariable=self.insp_label, style='Hint.TLabel').pack(side='left', padx=6)
        pw = ttk.PanedWindow(f, orient='vertical'); pw.pack(fill='both', expand=True, pady=(8, 0))
        tf = ttk.Frame(pw); pw.add(tf, weight=4)
        self.tree = ttk.Treeview(tf, columns=('type', 'verts', 'polys', 'material'))
        for c, w, t in (('#0', 330, 'Node'), ('type', 80, 'Type'), ('verts', 80, 'Vertices'),
                        ('polys', 80, 'Polygons'), ('material', 300, 'Material')):
            self.tree.heading(c, text=t, anchor='w')
            self.tree.column(c, width=w, anchor='w' if c in ('#0', 'material', 'type') else 'e')
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True); sb.pack(side='left', fill='y')
        self.tree.tag_configure('bad', foreground='#c0392b')
        self.check_text = tk.Text(pw, height=5, wrap='word', font=('Consolas', 9), relief='flat',
                                  background='#1e1e1e', foreground='#d4d4d4', padx=6, pady=4)
        for tag, col in (('err', '#f48771'), ('ok', '#89d185'), ('warn', '#e2c08d')):
            self.check_text.tag_configure(tag, foreground=col)
        pw.add(self.check_text, weight=1)
        self.doc = None
        return f

    def open_inspect(self):
        p = filedialog.askopenfilename(title='Open model', filetypes=[('Game model', '*.bin *.json *.orig'), ('All', '*')])
        if not p: return
        self.status.set('Reading %s…' % os.path.basename(p)); self.update_idletasks()
        try:
            with open(p, 'rb') as fh: data = fh.read()
            if data.startswith(b'BinarySerializer:'):
                h = zigspec.sniff_header(data)
                sp = next((s for s in self.specs.values() if s.schema_hash == h), None)
                if sp is None:
                    raise ValueError('schema %s - none of the loaded spec files matches.' % h)
                self.doc, self.doc_spec = sp.decode(data), sp
            else:
                self.doc, self.doc_spec = json.loads(data.decode('utf-8')), None
            self.doc_path = p
        except Exception as e:
            messagebox.showerror(APP, 'Could not read file:\n%s' % e); self.status.set('Ready.'); return
        self.fill_tree()
        self.conv_btn.configure(state='normal', text='Save as .json…' if self.doc_spec else 'Save as .bin…')
        self.status.set('Loaded %s' % p)

    def fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        d, ids = self.doc, {}
        meshes = [n['name'] for n in d['nodes'] if n.get('mesh')]
        model = zigspec.model_from_filename(self.doc_path.replace('.orig', ''))
        spec = self.doc_spec or self.spec
        expected = set(spec.models.get(model, [])) if spec else set()
        tv = 0
        for n in d['nodes']:
            parent = ''
            if '.material.' in n['name']:
                c = [b for b in meshes if n['name'].startswith(b + '.material.')]
                if c: parent = ids.get(max(c, key=len), '')
            m, mat = n.get('mesh'), n.get('material')
            if m: tv += len(m['positions'])
            mt = ''
            if mat:
                c = mat['color']
                mt = 'rgb(%.2f %.2f %.2f) r%.2f m%.2f' % (c['x'], c['y'], c['z'], mat['roughness'], mat['metallic'])
                if mat.get('diffuse_texture'): mt += '  ' + mat['diffuse_texture']
            if n.get('armature'): mt = '%d bones, %d frames' % (len(n['armature']['bones']), len(n['armature']['animation']))
            label = n['name'][len(self.tree.item(parent, 'text')) + 1:] if parent else n['name']
            ids[n['name']] = self.tree.insert(parent, 'end', text=label, values=(
                n['type'], len(m['positions']) if m else '', len(m['polygons']) if m else '', mt),
                tags=('bad',) if expected and n['name'] not in expected else ())
        self.insp_label.set('{:,} nodes · {:,} vertices · {} fps · schema {}'.format(
            len(d['nodes']), tv, d['framerate'], self.doc_spec.schema_hash if self.doc_spec else 'json'))
        self.check_text.configure(state='normal'); self.check_text.delete('1.0', 'end')
        if spec:
            errs, warns = zigspec.check_model(spec, model, d)
            if not errs and not warns: self.check_text.insert('end', '✔ matches the spec for "%s"\n' % model, 'ok')
            for e in errs: self.check_text.insert('end', '✘ ' + e + '\n', 'err')
            for w in warns: self.check_text.insert('end', '⚠ ' + w + '\n', 'warn')
        self.check_text.configure(state='disabled')

    def convert(self):
        if not self.doc: return
        base = os.path.splitext(self.doc_path)[0]
        if self.doc_spec:
            p = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('JSON', '*.json')],
                                             initialfile=os.path.basename(base.replace('.blend', '_blend')) + '.json')
            if p:
                with open(p, 'w') as fh: json.dump(self.doc, fh, indent=4)
        else:
            p = filedialog.asksaveasfilename(defaultextension='.bin', filetypes=[('Game model', '*.bin')],
                                             initialfile=os.path.basename(base.replace('_blend', '.blend')) + '.bin')
            if p:
                try: data = self.spec.encode(self.doc)
                except Exception as e:
                    messagebox.showerror(APP, 'Could not encode:\n%s' % e); return
                with open(p, 'wb') as fh: fh.write(data)
        if p: self.status.set('Saved %s' % p)

    # ------------------------------------------------------------ plumbing
    def pump(self):
        try:
            while True:
                kind, *a = self.q.get_nowait()
                if kind == 'log': self.write_log(a[0], a[1])
                elif kind == 'prog': self.prog['value'] = a[0]
                elif kind == 'status': self.status.set(a[0])
                elif kind == 'call': a[0]()
                elif kind == 'finish':
                    self.status.set(a[0]); self.write_log(a[0] + '\n', 'dim'); self.set_busy(False); self.on_inputs()
        except queue.Empty:
            pass
        self.after(80, self.pump)

    def persist(self):
        self.cfg.update(blender=self.blender.get(), blend=self.blend.get(), game=self.game.get(),
                        save_copy=self.save_copy.get(), copy_path=self.copy_path.get(), preset=self.preset.get(),
                        bring_tex=self.bring_tex.get(), finish=self.finish.get(), tex_slot=self.tex_slot.get(), tex_file=self.tex_file.get(),
                        **{'opt_' + k: v.get() for k, v in self.opt.items()})
        save_cfg(self.cfg)

    def on_close(self):
        self.persist(); self.cancel_run(); self.destroy()


class MappingEditor(tk.Toplevel):
    """Shows which of your parts became which game part, and lets you change it."""

    def __init__(self, app, blend, model, data):
        super().__init__(app)
        self.app, self.blend, self.model = app, blend, model
        self.scan, self.mapping = data['scan'], data['mapping']
        self.objs = {o['name']: o for o in self.scan['objects']}
        self.title('Matching: %s → %s' % (os.path.basename(blend), model))
        self.geometry('980x680'); self.transient(app)

        top = ttk.Frame(self, padding=10); top.pack(fill='x')
        src = 'reviewed earlier' if self.mapping.get('saved') else 'worked out automatically'
        ref = data.get('reference')
        ttk.Label(top, text='Matching %s. Template: %s' % (src, ref or 'NONE - set the game models folder'),
                  style='Warn.TLabel' if not ref else 'Hint.TLabel').pack(anchor='w')
        notes = tk.Text(top, height=4, wrap='word', font=('Segoe UI', 9), relief='flat', background='#f4f4f4')
        notes.insert('end', '\n'.join(self.mapping.get('notes') or ['(no notes)']))
        notes.configure(state='disabled'); notes.pack(fill='x', pady=(4, 0))

        mid = ttk.Frame(self, padding=(10, 0)); mid.pack(fill='both', expand=True)
        side = ttk.Frame(mid); side.pack(side='right', fill='y', padx=(8, 0))
        self.tree = ttk.Treeview(mid, columns=('parts', 'how'))
        for c, t, w in (('#0', 'Game part', 260), ('parts', 'Your parts', 300), ('how', 'Why', 300)):
            self.tree.heading(c, text=t, anchor='w'); self.tree.column(c, width=w, anchor='w')
        self.tree.tag_configure('empty', foreground='#999'); self.tree.tag_configure('mine', foreground='#1f5fbf')
        self.tree.tag_configure('missing', foreground='#c0392b')
        sb = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview); self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True); sb.pack(side='left', fill='y')
        for text, cmd in (('Put object(s) here…', self.assign), ('Leave empty', self.clear_unit),
                          ('Use the original', self.use_original), ('Match again', self.rematch)):
            ttk.Button(side, text=text, command=cmd, width=20).pack(fill='x', pady=2)
        ttk.Label(side, text='Select a game part,\nthen choose what\ngoes in it.', style='Hint.TLabel').pack(anchor='w', pady=(10, 0))

        bot = ttk.Frame(self, padding=10); bot.pack(fill='x')
        self.pivots = tk.BooleanVar(value=self.mapping.get('pivots', 'original') == 'original')
        ttk.Checkbutton(bot, text="Keep the game's pivot points", variable=self.pivots).pack(side='left')
        self.unused = tk.StringVar(); ttk.Label(bot, textvariable=self.unused, style='Hint.TLabel').pack(side='left', padx=12)
        ttk.Button(bot, text='Cancel', command=self.destroy).pack(side='right')
        ttk.Button(bot, text='Save matching', style='Big.TButton', command=self.save).pack(side='right', padx=6)
        self.fill()

    def units(self): return self.mapping['units']

    def unit(self, base): return next(u for u in self.units() if u['base'] == base)

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        for u in self.units():
            if u['kind'] == 'mesh' and u.get('keep_original'):
                self.tree.insert('', 'end', iid=u['base'], text=u['base'], values=("the game's original", u['how']),
                                 tags=('empty',))
                continue
            if u['kind'] == 'mesh':
                srcs = [x for s in u['splits'] for x in s['sources']]
                if srcs:
                    whole = sorted({x[0] for x in srcs if len(x) < 3 or x[2] is None or x[2] < 0})
                    parts = {}
                    for x in srcs:
                        if len(x) > 2 and x[2] is not None and x[2] >= 0: parts.setdefault(x[0], set()).add(x[2])
                    txt = ', '.join(['%s (whole)' % w for w in whole] +
                                    ['%s: %d part%s' % (o, len(p), 's' if len(p) > 1 else '') for o, p in sorted(parts.items())])
                    tag = 'mine' if u.get('set_by_you') else ''
                else:
                    txt, tag = '— empty —', 'empty'
                iid = self.tree.insert('', 'end', iid=u['base'], text=u['base'], values=(txt, u['how']), tags=(tag,))
                for s in u['splits']:
                    if s['sources']:
                        self.tree.insert(iid, 'end', text='  material: ' + (s['node'][len(u['base']) + 10:] or 'main')
                                         if s['node'] != u['base'] else '  material: main', values=('', s['how']))
            else:
                missing = u['how'].startswith('MISSING')
                self.tree.insert('', 'end', iid=u['base'], text=u['base'],
                                 values=(u.get('source') or ('— none —' if missing else 'original kept'), u['how']),
                                 tags=('missing',) if missing else ('mine',) if u.get('source') else ('empty',))
        used = {x[0] for u in self.units() if u['kind'] == 'mesh' for s in u['splits'] for x in s['sources']}
        used |= {u['source'] for u in self.units() if u['kind'] != 'mesh' and u.get('source')}
        unused = [o for o in self.objs if o not in used]
        self.mapping['unused'] = unused
        self.unused.set('Not exported: ' + (', '.join(unused) if unused else 'nothing'))

    def selected(self):
        sel = self.tree.selection()
        if not sel: messagebox.showinfo(APP, 'Select a game part first.', parent=self); return None
        iid = sel[0]
        while self.tree.parent(iid): iid = self.tree.parent(iid)
        return self.unit(iid)

    def assign(self):
        u = self.selected()
        if not u: return
        want = 'MESH' if u['kind'] == 'mesh' else (u.get('type') or None)
        names = [o['name'] for o in self.scan['objects'] if (want is None or o['type'] == want)]
        if not names:
            messagebox.showinfo(APP, 'No %s objects in this file.' % (want or ''), parent=self); return
        dlg = tk.Toplevel(self); dlg.title('Put into %s' % u['base']); dlg.transient(self); dlg.grab_set()
        ttk.Label(dlg, text='Choose your object(s) for %s.\nThey are taken out of any other part.' % u['base'],
                  padding=8).pack(anchor='w')
        lb = tk.Listbox(dlg, selectmode='extended' if u['kind'] == 'mesh' else 'browse', height=min(16, len(names)),
                        width=40)
        for n in names: lb.insert('end', n)
        lb.pack(fill='both', expand=True, padx=8)

        def ok():
            chosen = [names[i] for i in lb.curselection()]
            dlg.destroy()
            if not chosen: return
            if u['kind'] != 'mesh':
                for v in self.units():
                    if v['kind'] != 'mesh' and v.get('source') == chosen[0]: v['source'] = None; v['how'] = 'from original game file'
                u['source'] = chosen[0]; u['how'] = 'set by you'; self.fill(); return
            for v in self.units():          # take the objects out of every other part
                if v['kind'] != 'mesh': continue
                for s in v['splits']:
                    s['sources'] = [x for x in s['sources'] if x[0] not in chosen]
            for s in u['splits']: s['sources'] = []
            u['keep_original'] = False
            k = 1
            for name in chosen:
                slots = sorted([sl for sl in self.objs[name].get('slots') or [{'index': 0, 'polys': 1}]
                                if sl.get('polys', 1)], key=lambda sl: -sl.get('polys', 0))
                for j, sl in enumerate(slots):
                    if j == 0 and not u['splits'][0]['sources']: target = u['splits'][0]
                    elif k < len(u['splits']): target = u['splits'][k]; k += 1
                    else: target = u['splits'][0]
                    target['sources'].append([name, sl['index'], -1])
            for s in u['splits']:
                s['how'] = ', '.join('%s/%s' % (x[0], (self.objs[x[0]].get('slots') or [{}])[x[1]].get('material')
                                                if self.objs[x[0]].get('slots') else '') for x in s['sources']) or 'empty'
            u['how'] = 'set by you'; u['set_by_you'] = True; u['objects'] = chosen
            self.fill()
        ttk.Button(dlg, text='OK', command=ok).pack(side='right', padx=8, pady=8)
        ttk.Button(dlg, text='Cancel', command=dlg.destroy).pack(side='right', pady=8)

    def clear_unit(self):
        u = self.selected()
        if not u: return
        if u['kind'] == 'mesh':
            for s in u['splits']: s['sources'] = []; s['how'] = 'empty'
            u['how'] = 'left empty by you'; u['set_by_you'] = True; u['keep_original'] = False
        else:
            u['source'] = None; u['how'] = 'from original game file'
        self.fill()

    def use_original(self):
        u = self.selected()
        if not u: return
        if u['kind'] == 'mesh':
            for s in u['splits']: s['sources'] = []
            u['keep_original'] = True; u['how'] = 'game original kept'; self.fill(); return
        u['source'] = None; u['how'] = 'from original game file'; self.fill()

    def rematch(self):
        if not messagebox.askyesno(APP, 'Throw away changes and match automatically again?', parent=self): return
        try: os.remove(self.app.map_path(self.blend))
        except OSError: pass
        self.app.on_inputs()
        self.destroy(); self.app.review(remap=True)

    def save(self):
        self.mapping['pivots'] = 'original' if self.pivots.get() else 'yours'
        self.mapping.pop('saved', None)
        with open(self.app.map_path(self.blend), 'w') as fh: json.dump(self.mapping, fh, indent=1)
        self.app.write_log('  ✔ matching saved for %s - Export will use it' % os.path.basename(self.blend), 'ok')
        self.app.refresh_files(); self.destroy()


if __name__ == '__main__':
    App().mainloop()

"""
map_editor.py - Map editor for the Game Model Exporter: lay your own heightmap tracks onto the game's terrain.

The game's terrain lives in content/meshes/map.blend.bin (node "Heightmap.Base"): a 0.5 m base heightmap,
detail "height clips" (the game's own tracks are 12.5 cm clips), rock/tree biome maps and a surface map.
A heightmap you add here becomes the same thing: heights blended into the base map, a new 12.5 cm detail
clip, dirt surface underneath and rocks/trees cleared. The game's file is only read; the result is saved as
a new file (Install copies it into the game with a backup of the original).
"""
import json
import math
import os
import re
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

import terrain_io as T
import zigspec

PALETTE = {'sand': (190, 145, 100), 'gravel': (138, 126, 110), 'dirt': (180, 145, 109), 'rock': (169, 158, 132),
           'highway': (110, 104, 97), 'path': (118, 92, 68), 'forest_floor': (125, 89, 60), 'grass': (82, 91, 40)}
PAL = np.array([PALETTE[m] for m in T.MATERIALS], np.float32)
VOID = np.array((34, 52, 76), np.float32)
# The game's layer names don't say what they look like: its two tracks are painted with 'gravel' (track 1)
# and 'path' (track 2); its 'dirt' layer is a light tan ground.
SURFACES = [('Track dirt - like game track 1', 'gravel'), ('Track dirt - like game track 2', 'path'),
            ('Light dirt', 'dirt'), ('Sand', 'sand'), ('Rock', 'rock'), ('Forest floor', 'forest_floor'),
            ('Grass', 'grass'), ('Tarmac', 'highway'), ('(keep the surface)', None),
            ('(painted in the Heightmap Studio)', '__studio__')]
SURF_LABEL = {v: k for k, v in SURFACES}
SURF_VALUE = {k: v for k, v in SURFACES}


def load_heightmap(path, with_levels=False):
    """Greyscale heights 0..1 (rows = image top to bottom). 16-bit images keep their full precision.
    with_levels: also return how many grey levels the file can hold (256 or 65536)."""
    im = Image.open(path)
    levels = 65536
    if im.mode in ('I;16', 'I;16L', 'I;16B', 'I;16N'):
        a = np.asarray(im, dtype=np.uint16).astype(np.float32) / 65535.0
    elif im.mode == 'I':
        a = np.asarray(im, dtype=np.float32)
        a = a / 65535.0 if a.min() >= 0 and a.max() <= 65535 else (a - a.min()) / max(float(np.ptp(a)), 1e-9)
    elif im.mode == 'F':
        a = np.asarray(im, dtype=np.float32)
        lo, hi = float(a.min()), float(a.max())
        a = (a - lo) / (hi - lo) if hi > lo else np.zeros_like(a)
    else:
        if im.mode not in ('L',): im = im.convert('L')
        a = np.asarray(im, dtype=np.float32) / 255.0; levels = 256
    return (a, levels) if with_levels else a


def save_heightmap16(path, a):
    """0..1 heights -> 16-bit greyscale PNG."""
    q = np.round(np.clip(np.nan_to_num(a, nan=0.0), 0, 1) * 65535).astype(np.uint16)
    q = np.ascontiguousarray(q.astype('<u2'))
    try:
        im = Image.fromarray(q)                          # Pillow picks 16-bit greyscale for uint16
    except (TypeError, ValueError):
        im = Image.frombytes('I;16', (q.shape[1], q.shape[0]), q.tobytes())
    if im.mode != 'I;16': im = Image.frombytes('I;16', (q.shape[1], q.shape[0]), q.tobytes())
    im.save(path)


def placement_file(img_path):
    return os.path.splitext(img_path)[0] + '.placement.json'


def last_ride_position(game_root):
    """Where the rider was when the game last closed (read from game_state.zon), or None."""
    try:
        txt = open(os.path.join(game_root, 'game_state.zon'), encoding='utf-8', errors='replace').read()
        m = re.search(r'\.sim_offset\s*=\s*\.\{\s*\.x\s*=\s*([-\d.e]+),\s*\.y\s*=\s*([-\d.e]+),\s*\.z\s*=\s*([-\d.e]+)', txt)
        return (float(m.group(1)), float(m.group(3))) if m else None
    except (OSError, TypeError):
        return None


class Track:
    def __init__(self, path, img, cx, cz):
        self.path, self.name = path, os.path.splitext(os.path.basename(path))[0]
        h, w = img.shape
        self.pl = T.Placement(img, cx, cz, width=round(w * 0.25, 1), length=round(h * 0.25, 1), rot=0.0,
                              height_range=10.0, edge=10.0, material='gravel', clear_biomes=True)
        self.lock = True              # keep the image's proportions when resizing
        self.levels = 65536           # grey levels of the file (256 for 8-bit)
        # 'world': black/white = the game map's own lowest/highest height (default)
        # 'ground': sit on the terrain (+ offset)  /  'absolute': black at a height
        self.mode = 'world'
        self.offset = 0.0
        self.abs_y = 0.0


class MapEditor(tk.Toplevel):
    def __init__(self, app, map_path, spec_path, game_root=None):
        super().__init__(app)
        self.app, self.map_path, self.spec_path, self.game_root = app, map_path, spec_path, game_root
        self.title('Map editor - %s' % map_path)
        self.geometry('1450x880'); self.minsize(1000, 640)
        self.tf = None; self.t = None; self.g = None
        self.tracks = []; self.sel = None
        self.cx = self.cz = 0.0; self.mpp = 4.0
        self._img = None; self._busy = False; self._drag = None; self._scale_job = None
        self.ride = last_ride_position(game_root) if game_root else None
        self.build()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(50, self.load)

    # ------------------------------------------------------------------ layout
    def build(self):
        right = ttk.Frame(self, padding=(8, 4), width=410); right.pack(side='right', fill='y'); right.pack_propagate(False)
        ttk.Label(right, text='Your tracks', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.tlist = ttk.Treeview(right, columns=('size',), show='tree headings', height=2, selectmode='browse')
        self.tlist.heading('#0', text='Heightmap', anchor='w'); self.tlist.heading('size', text='size (m)')
        self.tlist.column('#0', width=200); self.tlist.column('size', width=110, anchor='e')
        self.tlist.pack(fill='x'); self.tlist.bind('<<TreeviewSelect>>', self.on_select)
        r = ttk.Frame(right); r.pack(fill='x', pady=(4, 4))
        ttk.Button(r, text='Add heightmap…', command=self.add_track).pack(side='left')
        ttk.Button(r, text='Remove', command=self.remove_track).pack(side='left', padx=4)
        ttk.Button(r, text='Reload', command=self.reload_track).pack(side='left')
        r = ttk.Frame(right); r.pack(fill='x', pady=(0, 4))
        ttk.Button(r, text='Start from the game\'s ground here…', command=self.grab_ground).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Heightmap Studio…', command=self.open_studio).pack(side='left', fill='x', expand=True, padx=(4, 0))

        self.fv = {}
        box = ttk.LabelFrame(right, text='Placement (metres, degrees)', padding=(6, 3)); box.pack(fill='x')
        for i, (key, lab) in enumerate((('cx', 'X'), ('cz', 'Z'), ('width', 'Width'), ('length', 'Length'),
                                        ('rot', 'Rotation'), ('mppx', 'm / pixel'))):
            r_, c_ = divmod(i, 2)
            ttk.Label(box, text=lab).grid(row=r_, column=c_ * 2, sticky='w', pady=1, padx=(0 if c_ == 0 else 8, 0))
            v = tk.StringVar(); en = ttk.Entry(box, textvariable=v, width=8, justify='right'); en.grid(row=r_, column=c_ * 2 + 1, padx=2)
            cmd = self.set_mppx if key == 'mppx' else self.read_fields
            en.bind('<Return>', lambda ev, cmd=cmd: cmd()); en.bind('<FocusOut>', lambda ev, cmd=cmd: cmd())
            self.fv[key] = v
        r = ttk.Frame(box); r.grid(row=3, column=0, columnspan=4, sticky='we', pady=(4, 0))
        self.lockv = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text='Keep proportions', variable=self.lockv, command=self.read_fields).pack(side='left')
        for t_, f_ in (('×2', 2.0), ('×1.1', 1.1), ('×0.9', 0.9), ('½', 0.5)):
            ttk.Button(r, text=t_, width=4, command=lambda f_=f_: self.scale_by(f_)).pack(side='right', padx=(1, 0))
        self.scale = tk.DoubleVar(value=0.0)
        ttk.Scale(box, from_=-4.0, to=1.5, variable=self.scale, command=self.on_scale).grid(
            row=4, column=0, columnspan=4, sticky='we', pady=(4, 0))
        self._scale_lock = False
        ttk.Label(box, text='Click / drag the map to place it. Squares resize, the circle turns it (Shift: stretch '
                  '/ snap 15°).', wraplength=380,
                  foreground='#666').grid(row=5, column=0, columnspan=4, sticky='w', pady=(4, 0))

        box = ttk.LabelFrame(right, text='Heights', padding=(6, 3)); box.pack(fill='x', pady=(4, 0))
        self.mode = tk.StringVar(value='world')
        ttk.Radiobutton(box, text="Same scale as the game's map", value='world',
                        variable=self.mode, command=self.read_fields).grid(row=0, column=0, columnspan=3, sticky='w')
        self.world_lbl = ttk.Label(box, text='', foreground='#666', wraplength=380)
        self.world_lbl.grid(row=4, column=0, columnspan=3, sticky='w')
        ttk.Radiobutton(box, text='Sit on the ground, raised by', value='ground', variable=self.mode,
                        command=self.read_fields).grid(row=1, column=0, columnspan=2, sticky='w')
        self.fv['offset'] = tk.StringVar(); e = ttk.Entry(box, textvariable=self.fv['offset'], width=8, justify='right')
        e.grid(row=1, column=2, sticky='w'); e.bind('<Return>', lambda ev: self.read_fields()); e.bind('<FocusOut>', lambda ev: self.read_fields())
        ttk.Radiobutton(box, text='Black is at height (Y)', value='absolute', variable=self.mode,
                        command=self.read_fields).grid(row=2, column=0, columnspan=2, sticky='w')
        self.fv['abs_y'] = tk.StringVar(); e = ttk.Entry(box, textvariable=self.fv['abs_y'], width=8, justify='right')
        e.grid(row=2, column=2, sticky='w'); e.bind('<Return>', lambda ev: self.read_fields()); e.bind('<FocusOut>', lambda ev: self.read_fields())
        ttk.Label(box, text='   with black → white spanning').grid(row=3, column=0, columnspan=2, sticky='w')
        self.fv['height_range'] = tk.StringVar()
        self.range_entry = e = ttk.Entry(box, textvariable=self.fv['height_range'], width=8, justify='right')
        e.grid(row=3, column=2, sticky='w')
        e.bind('<Return>', lambda ev: self.read_fields()); e.bind('<FocusOut>', lambda ev: self.read_fields())
        ttk.Label(box, text='Blend into the ground over').grid(row=5, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.fv['edge'] = tk.StringVar(); e = ttk.Entry(box, textvariable=self.fv['edge'], width=8, justify='right')
        e.grid(row=5, column=2, sticky='w', pady=(4, 0)); e.bind('<Return>', lambda ev: self.read_fields()); e.bind('<FocusOut>', lambda ev: self.read_fields())

        box = ttk.LabelFrame(right, text='Surface', padding=(6, 3)); box.pack(fill='x', pady=(4, 0))
        self.surface = tk.StringVar(value=SURFACES[0][0])
        cb = ttk.Combobox(box, textvariable=self.surface, values=[k for k, v in SURFACES], state='readonly', width=32); cb.pack(anchor='w')
        cb.bind('<<ComboboxSelected>>', lambda e: self.read_fields())
        self.clear = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text='Clear rocks and trees on the track', variable=self.clear,
                        command=self.read_fields).pack(anchor='w', pady=(4, 0))
        self.only_changed = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text='Surface only where I changed the ground',
                        variable=self.only_changed, command=self.read_fields).pack(anchor='w')

        self.nav = ttk.Frame(right); self.nav.pack(fill='x', pady=(4, 0))

        bot = ttk.Frame(right); bot.pack(side='bottom', fill='x')
        self.status = tk.StringVar(value='Loading the map…')
        ttk.Label(bot, textvariable=self.status, wraplength=390, foreground='#444').pack(anchor='w', pady=(0, 4))
        self.prog = ttk.Progressbar(bot, mode='determinate'); self.prog.pack(fill='x', pady=(0, 4))
        ttk.Button(bot, text='Save new map as…', command=self.save).pack(fill='x')
        r = ttk.Frame(bot); r.pack(fill='x', pady=(4, 0))
        ttk.Button(r, text='Install a saved map…', command=self.install).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Restore original', command=self.restore).pack(side='left', fill='x', expand=True, padx=(4, 0))

        mid = ttk.Frame(self); mid.pack(side='left', fill='both', expand=True)
        self.info = ttk.Label(mid, text='', anchor='w', padding=(8, 2)); self.info.pack(side='bottom', fill='x')
        self.canvas = tk.Canvas(mid, background='#22262c', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        c = self.canvas
        c.bind('<Configure>', lambda e: self.render())
        c.bind('<ButtonPress-1>', self.on_down); c.bind('<B1-Motion>', self.on_drag); c.bind('<ButtonRelease-1>', self.on_up)
        for b in ('<ButtonPress-2>', '<ButtonPress-3>'): c.bind(b, self.on_pan_down)
        for b in ('<B2-Motion>', '<B3-Motion>'): c.bind(b, self.on_pan)
        for b in ('<ButtonRelease-2>', '<ButtonRelease-3>'): c.bind(b, lambda e: self.render())
        c.bind('<MouseWheel>', lambda e: self.wheel(e, e.delta > 0))
        c.bind('<Control-Button-4>', lambda e: self.scale_by(1.1)); c.bind('<Control-Button-5>', lambda e: self.scale_by(1 / 1.1))
        c.bind('<Button-4>', lambda e: self.zoom(1 / 1.25, e.x, e.y)); c.bind('<Button-5>', lambda e: self.zoom(1.25, e.x, e.y))
        c.bind('<Motion>', self.on_motion)

    # ------------------------------------------------------------------ loading
    def load(self):
        def work():
            try:
                spec = zigspec.ZigSpec.load(self.spec_path)
                tf = T.TerrainFile.open(self.map_path, spec)
                if not tf.terrains: raise ValueError('no terrain found in this map file')
                self.after(0, lambda: self.loaded(tf))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: (messagebox.showerror('Map editor', "Couldn't read the map:\n%s" % msg, parent=self),
                                       self.status.set("Couldn't read the map.")))
        threading.Thread(target=work, daemon=True).start()

    def load_palette(self):
        """Average colour of each surface layer's texture, read live from the game (nothing is copied)."""
        global PAL
        if not self.game_root: return
        d = os.path.join(self.game_root, 'content', 'terrain-tiling')
        pal = PAL.copy(); n = 0
        for i, m in enumerate(T.MATERIALS):
            f = os.path.join(d, '%s_diffuse.generated.png' % m)
            try:
                im = Image.open(f); im.draft('RGB', (64, 64)); im = im.convert('RGB').resize((32, 32))
                pal[i] = np.asarray(im, np.float32).reshape(-1, 3).mean(0) * 1.15; n += 1
            except Exception:
                pass
        if n: PAL = pal

    def loaded(self, tf):
        self.load_palette()
        self.tf, self.t = tf, tf.terrains[0]
        self.g = T.Grid(self.t)
        g = self.g
        self.cx, self.cz = (g.x0 + g.x1) / 2, (g.z0 + g.z1) / 2
        W = max(400, self.canvas.winfo_width()); H = max(400, self.canvas.winfo_height())
        self.mpp = max((g.x1 - g.x0) / W, (g.z1 - g.z0) / H) * 1.02
        for w in self.nav.winfo_children(): w.destroy()
        ttk.Label(self.nav, text='Go to:').pack(side='left')
        ttk.Button(self.nav, text='Whole map', command=self.fit_all).pack(side='left', padx=(4, 0))
        for i, c in enumerate(self.t['height_clips']):
            b = c['bounds']
            ttk.Button(self.nav, text='Track %d' % (i + 1),
                       command=lambda b=b: self.goto((b['min'][0] + b['max'][0]) / 2, (b['min'][2] + b['max'][2]) / 2, 0.6)
                       ).pack(side='left', padx=(4, 0))
        if self.ride:
            ttk.Button(self.nav, text='Last ride', command=lambda: self.goto(self.ride[0], self.ride[1], 0.6)).pack(side='left', padx=(4, 0))
        self.status.set('Map: %.1f x %.1f km, %d x %d heights (%.2f m), %d detail clips. Add a heightmap to start.'
                        % ((g.x1 - g.x0) / 1000, (g.z1 - g.z0) / 1000, g.rx, g.ry, g.dx, len(self.t['height_clips'])))
        self.render()

    # ------------------------------------------------------------------ tracks
    def add_track(self):
        if self.t is None: return
        p = filedialog.askopenfilename(parent=self, title='Heightmap image (greyscale; 16-bit PNG is best)',
                                       filetypes=[('Images', '*.png *.tif *.tiff *.jpg *.jpeg *.bmp *.pgm'), ('All', '*.*')])
        if not p: return
        try:
            img, levels = load_heightmap(p, with_levels=True)
        except Exception as e:
            messagebox.showerror('Map editor', "Couldn't read the image:\n%s" % e, parent=self); return
        tr = Track(p, img, self.cx, self.cz); tr.levels = levels
        tr.offset = 0.0
        self.read_placement(tr)
        self.add_to_list(tr)
        self.status.set('%s: %d x %d pixels. Click the map where it should go, then set its size.%s'
                        % (tr.name, img.shape[1], img.shape[0], self.precision_note(tr)))

    def add_to_list(self, tr):
        self.tracks.append(tr)
        iid = str(id(tr)); tr.iid = iid
        self.tlist.insert('', 'end', iid=iid, text=tr.name, values=('%g x %g' % (tr.pl.width, tr.pl.length),))
        self.tlist.selection_set(iid); self.sel = tr
        self.refresh_ground(tr); self.show_fields(); self.render()

    # ------------------------------------------------------------------ heightmap studio
    def open_studio(self):
        """Build / edit the selected heightmap (or a 256 m square of the game's ground in the middle of the
        view) in the Heightmap Studio; "Send to map editor" there brings it back here."""
        if self.t is None: return
        if hasattr(self.app, 'ensure_libs'):
            self.app.ensure_libs((('OpenGL', 'PyOpenGL'), ('pyopengltk', 'pyopengltk')), "The studio's 3D view")
        import hm_core as HC, hm_studio as HS
        tr = self.sel
        if tr is not None:
            pl = tr.pl; size = max(pl.width, pl.length); cx, cz, rot = pl.cx, pl.cz, pl.rot
        else:
            size, cx, cz, rot = 256.0, self.cx, self.cz, 0.0
        n = 1025 if size <= 600 else 2049
        p = HC.Project(n, size / (n - 1))
        sq = T.Placement(np.zeros((n, n), np.float32), cx, cz, size, size, rot)
        self.status.set('Reading the ground for the studio…'); self.update_idletasks()
        h = T.ground_heights(self.t, sq, n, n)
        lo, hi = T.world_range(self.t)
        h = np.where(np.isnan(h), lo, h)
        if tr is not None:                               # what this heightmap makes of it now
            c, s_ = np.cos(np.radians(rot)), np.sin(np.radians(rot))
            u = (np.arange(n) / (n - 1) - 0.5) * size
            U, V = np.meshgrid(u, u)
            X = cx + U * c - V * s_; Z = cz + U * s_ + V * c
            self.refresh_ground(tr)
            new, w = tr.pl.sample(X, Z)
            h = np.where(w > 0, h * (1 - w) + new * w, h)
        p.base_img = h.astype(np.float32); p.terrain['kind'] = 'ground'
        p.world = [lo, hi]; p.export = {'mode': 'world', 'lo': lo, 'hi': hi}
        p.placement = {'cx': cx, 'cz': cz, 'rot': rot, 'size': size}
        target = tr

        def send(proj, H, info):
            return self.receive_studio(proj, H, info, target)
        HS.Studio(self, project=p, on_send=send, title='map %.0f, %.0f' % (cx, cz))
        self.status.set('Heightmap Studio opened. Press "Send to map editor" there to bring the result back.')

    def receive_studio(self, proj, H, info, target=None):
        import hm_core as HC
        lo, hi = T.world_range(self.t)
        pl0 = proj.placement
        if pl0 is None:                                  # a studio project of its own: put it in the view
            pl0 = {'cx': self.cx, 'cz': self.cz, 'rot': 0.0, 'size': proj.size}
        d = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'GameModelExporter', 'studio')
        os.makedirs(d, exist_ok=True)
        if target is not None and target.path.startswith(d): path = target.path
        else:
            k = 1
            while os.path.exists(os.path.join(d, 'studio_%d.png' % k)): k += 1
            path = os.path.join(d, 'studio_%d.png' % k)
        world = proj.world is not None
        if world:
            a = (H - lo) / (hi - lo)
        else:
            a = (H - float(H.min())) / max(float(np.ptp(H)), 1e-3)
        save_heightmap16(path, a)
        Image.fromarray((info['surface'].astype(np.int16) + 1).astype(np.uint8), 'L').save(os.path.splitext(path)[0] + '.surface.png')
        if target is not None and target in self.tracks:
            tr = target
        else:
            tr = Track(path, a.astype(np.float32), pl0['cx'], pl0['cz'])
            tr.name = 'studio %s' % os.path.splitext(os.path.basename(path))[0].split('_')[-1]
        tr.path = path
        pl = tr.pl
        pl.img = np.round(np.clip(a, 0, 1) * 65535).astype(np.float32) / 65535
        pl.cx, pl.cz, pl.rot = pl0['cx'], pl0['cz'], pl0['rot']
        pl.width = pl.length = pl0['size']
        pl.surface = info['surface'].copy(); pl.material = None; pl.paint_changed = False
        pl.edge = 4.0 if world else max(pl.edge, 8.0)
        tr.levels = 65536
        if world: tr.mode = 'world'
        else:
            tr.mode = 'ground'; tr.custom_range = float(np.ptp(H)) or 1.0; tr.offset = 0.0
        self.write_placement(tr)
        if tr not in self.tracks: self.add_to_list(tr)
        else:
            self.tlist.item(tr.iid, text=tr.name, values=('%g x %g' % (pl.width, pl.length),))
            self.refresh_ground(tr); self.show_fields()
        self.sel = tr; self.tlist.selection_set(tr.iid)
        self.goto(pl.cx, pl.cz, max(0.2, pl0['size'] / 700))
        self.status.set('Studio heightmap placed (%s). Its surface comes from the studio; trees and rocks are cleared '
                        'where the ground changed or got a surface. Save new map as… to write it.' % tr.name)
        self.lift()
        return 'Sent to the map editor as "%s".' % tr.name

    def precision_note(self, tr):
        lo, hi = T.world_range(self.t)
        if tr.mode == 'world' and tr.levels <= 256:
            return (' Note: it is an 8-bit image - on the map\'s scale each grey step is %.1f m, so slopes come '
                    'out as stairs. Save it as a 16-bit PNG.' % ((hi - lo) / 255))
        return ''

    def read_placement(self, tr):
        """A heightmap saved by 'Start from the game's ground' remembers where it goes."""
        try:
            with open(placement_file(tr.path)) as f: d = json.load(f)
        except (OSError, ValueError):
            return False
        pl = tr.pl
        for k in ('cx', 'cz', 'width', 'length', 'rot', 'edge'):
            if k in d: setattr(pl, k, float(d[k]))
        tr.mode = d.get('mode', 'world'); pl.material = d.get('material', pl.material)
        pl.paint_changed = bool(d.get('paint_changed', False))
        if tr.mode != 'world' and d.get('height_range'): tr.custom_range = float(d['height_range'])
        sp = os.path.splitext(tr.path)[0] + '.surface.png'
        if d.get('surface') and os.path.exists(sp):
            pl.surface = (np.asarray(Image.open(sp), np.int16) - 1).astype(np.int8); pl.material = None
        return True

    def write_placement(self, tr):
        pl = tr.pl
        d = {k: getattr(pl, k) for k in ('cx', 'cz', 'width', 'length', 'rot', 'edge')}
        d.update(mode=tr.mode, material=pl.material, paint_changed=pl.paint_changed,
                 surface=getattr(pl, 'surface', None) is not None, height_range=pl.height_range, note='Map editor placement for %s' % os.path.basename(tr.path))
        try:
            with open(placement_file(tr.path), 'w') as f: json.dump(d, f, indent=1)
        except OSError:
            pass

    def reload_track(self):
        """Read the selected heightmap's image again (after editing it elsewhere); placement is kept."""
        tr = self.sel
        if not tr: return
        try:
            img, tr.levels = load_heightmap(tr.path, with_levels=True)
        except Exception as e:
            messagebox.showerror('Map editor', "Couldn't read the image:\n%s" % e, parent=self); return
        tr.pl.img = np.asarray(img, np.float32)
        self.refresh_ground(tr); self.show_fields(); self.render()
        self.status.set('Reloaded %s (%d x %d pixels).%s' % (tr.name, img.shape[1], img.shape[0], self.precision_note(tr)))

    def grab_ground(self):
        """Save the game's own ground under the selected box (or a 200 m square in the middle of the view)
        as a 16-bit heightmap on the map's scale, to edit in any image editor and bring back."""
        if self.t is None: return
        src = self.sel
        if src:
            cx, cz, w, l, rot = src.pl.cx, src.pl.cz, src.pl.width, src.pl.length, src.pl.rot
            mppx = w / src.pl.img.shape[1]
        else:
            cx, cz, w, l, rot, mppx = self.cx, self.cz, 200.0, 200.0, 0.0, 0.25
        nx = max(2, int(round(w / mppx))); ny = max(2, int(round(l / mppx)))
        if nx * ny > 64e6:
            messagebox.showinfo('Map editor', 'That area is too big for one image (%d x %d pixels) - make the box '
                                'smaller or use more metres per pixel.' % (nx, ny), parent=self); return
        p = filedialog.asksaveasfilename(parent=self, title="Save the game's ground here as a heightmap",
                                         defaultextension='.png', initialfile='ground_%d_%d.png' % (cx, cz),
                                         filetypes=[('16-bit PNG', '*.png')])
        if not p: return
        if self.game_root and os.path.abspath(p).lower().startswith(os.path.abspath(self.game_root).lower()):
            messagebox.showinfo('Map editor', "Please save it outside the game's folder.", parent=self); return
        tmp = T.Placement(np.zeros((ny, nx), np.float32), cx, cz, w, l, rot)
        self.status.set('Reading the ground…'); self.update_idletasks()
        h = T.ground_heights(self.t, tmp, nx, ny)
        lo, hi = T.world_range(self.t)
        a = (h - lo) / (hi - lo)
        save_heightmap16(p, a)
        tr = Track(p, np.nan_to_num(a, nan=0.0).astype(np.float32), cx, cz)
        tr.pl.width, tr.pl.length, tr.pl.rot = w, l, rot
        tr.mode = 'world'; tr.pl.paint_changed = True
        if src: tr.pl.material, tr.pl.edge = src.pl.material, src.pl.edge
        self.write_placement(tr)
        self.add_to_list(tr)
        self.status.set('Saved %s (%d x %d pixels, %.3g m per pixel) - it is the ground exactly as it is now, on the '
                        "map's own scale. Edit it in any image editor (keep it 16-bit greyscale), save, then press "
                        'Reload. Unchanged parts stay as they are.' % (os.path.basename(p), nx, ny, mppx))

    def remove_track(self):
        if not self.sel: return
        self.tlist.delete(self.sel.iid); self.tracks.remove(self.sel); self.sel = None; self.render()

    def on_select(self, _e=None):
        s = self.tlist.selection()
        self.sel = next((t for t in self.tracks if t.iid in s), None)
        self.show_fields(); self.render()

    def refresh_ground(self, tr):
        """'Sit on the ground': base height follows the terrain under the track."""
        pl = tr.pl
        if tr.mode == 'world':
            lo, hi = T.world_range(self.t)
            pl.base_y, pl.height_range = lo, hi - lo
        elif tr.mode == 'ground':
            if getattr(tr, 'custom_range', None): pl.height_range = tr.custom_range
            pl.base_y = T.ground_level(self.t, pl) + tr.offset - pl.height_range * float(np.median(pl.img))
        else:
            if getattr(tr, 'custom_range', None): pl.height_range = tr.custom_range
            pl.base_y = tr.abs_y

    def show_fields(self):
        tr = self.sel
        if not tr:
            for v in self.fv.values(): v.set('')
            return
        pl = tr.pl
        for k in ('cx', 'cz', 'width', 'length', 'rot', 'height_range', 'edge'):
            self.fv[k].set('%g' % round(getattr(pl, k), 2))
        self.fv['offset'].set('%g' % tr.offset); self.fv['abs_y'].set('%g' % round(tr.abs_y, 2))
        self.mode.set(tr.mode); self.surface.set(SURF_LABEL['__studio__'] if getattr(pl, 'surface', None) is not None
                         else SURF_LABEL.get(pl.material, SURFACES[0][0])); self.clear.set(pl.clear_biomes)
        self.only_changed.set(pl.paint_changed)
        lo, hi = T.world_range(self.t)
        img = pl.img
        self.world_lbl.config(text='   black %.1f m, white %.1f m (map\'s lowest/highest); image %.1f to %.1f m'
                              % (lo, hi, lo + float(img.min()) * (hi - lo), lo + float(img.max()) * (hi - lo))
                              if tr.mode == 'world' else '')
        self.range_entry.config(state='disabled' if tr.mode == 'world' else 'normal')
        self.lockv.set(tr.lock)
        self.fv['mppx'].set('%.4g' % (pl.width / pl.img.shape[1]))
        self._scale_lock = True; self.scale.set(math.log2(max(pl.width / pl.img.shape[1], 1e-4))); self._scale_lock = False

    def read_fields(self):
        tr = self.sel
        if not tr: return
        pl = tr.pl
        def num(k, cur):
            try: return float(self.fv[k].get())
            except ValueError: return cur
        pl.cx, pl.cz = num('cx', pl.cx), num('cz', pl.cz)
        tr.lock = self.lockv.get()
        w_new, l_new = max(1.0, num('width', pl.width)), max(1.0, num('length', pl.length))
        if tr.lock and abs(w_new - pl.width) > 1e-6: l_new = pl.length * w_new / pl.width
        elif tr.lock and abs(l_new - pl.length) > 1e-6: w_new = pl.width * l_new / pl.length
        pl.width, pl.length = w_new, l_new
        pl.rot = num('rot', pl.rot) % 360
        new_mode = self.mode.get()
        if tr.mode != 'world' and new_mode != 'world':
            tr.custom_range = num('height_range', pl.height_range)
        elif new_mode != 'world' and not getattr(tr, 'custom_range', None):
            tr.custom_range = 10.0
        pl.edge = max(0.0, num('edge', pl.edge))
        tr.offset = num('offset', tr.offset); tr.abs_y = num('abs_y', tr.abs_y); tr.mode = new_mode
        v = SURF_VALUE.get(self.surface.get(), 'gravel')
        if v != '__studio__':
            pl.material = v; pl.surface = None             # a surface picked here replaces the studio's painting
        elif getattr(pl, 'surface', None) is None:
            pl.material = 'gravel'
        pl.clear_biomes = self.clear.get(); pl.paint_changed = self.only_changed.get()
        self.refresh_ground(tr)
        self.tlist.item(tr.iid, values=('%g x %g' % (round(pl.width, 1), round(pl.length, 1)),))
        self.show_fields(); self.render()

    # ---- sizing
    def resize(self, width, length, final=True):
        tr = self.sel
        if not tr: return
        tr.pl.width, tr.pl.length = max(1.0, width), max(1.0, length)
        self.tlist.item(tr.iid, values=('%g x %g' % (round(tr.pl.width, 1), round(tr.pl.length, 1)),))
        if final: self.refresh_ground(tr); self.show_fields()
        else:
            for k in ('width', 'length'): self.fv[k].set('%g' % round(getattr(tr.pl, k), 2))
        self.render()

    def scale_by(self, f):
        if self.sel: self.resize(self.sel.pl.width * f, self.sel.pl.length * f)

    def set_mppx(self):
        tr = self.sel
        if not tr: return
        try: m = float(self.fv['mppx'].get())
        except ValueError: return
        if m > 0:
            h, w = tr.pl.img.shape
            self.resize(w * m, h * m)

    def on_scale(self, _v=None):
        if self._scale_lock or not self.sel: return
        m = 2 ** float(self.scale.get())                 # slider: metres per pixel, log scale (1/16 .. 2.8)
        pl = self.sel.pl; h, w = pl.img.shape
        k = m / (pl.width / w)
        self.resize(pl.width * k, pl.length * k, final=False)
        if self._scale_job: self.after_cancel(self._scale_job)
        self._scale_job = self.after(300, lambda: (self.refresh_ground(self.sel), self.show_fields()) if self.sel else None)

    # ------------------------------------------------------------------ view
    def fit_all(self):
        g = self.g
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.cx, self.cz = (g.x0 + g.x1) / 2, (g.z0 + g.z1) / 2
        self.mpp = max((g.x1 - g.x0) / W, (g.z1 - g.z0) / H) * 1.02; self.render()

    def goto(self, x, z, mpp):
        self.cx, self.cz, self.mpp = x, z, mpp; self.render()

    def to_world(self, px, py):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        return self.cx + (px - W / 2) * self.mpp, self.cz + (py - H / 2) * self.mpp

    def to_screen(self, x, z):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        return W / 2 + (x - self.cx) / self.mpp, H / 2 + (z - self.cz) / self.mpp

    def wheel(self, e, up):
        if e.state & 0x4 and self.sel:                       # Ctrl + wheel: resize the selected track
            self.scale_by(1.1 if up else 1 / 1.1)
        else:
            self.zoom(1 / 1.25 if up else 1.25, e.x, e.y)

    def zoom(self, f, px, py):
        x, z = self.to_world(px, py)
        self.mpp = min(max(self.mpp * f, 0.05), 40.0)
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.cx, self.cz = x - (px - W / 2) * self.mpp, z - (py - H / 2) * self.mpp
        self.render()

    def sample_view(self, W, H):
        """Heights and surface for every pixel (nearest grid cell), with your tracks applied on top."""
        g = self.g
        xs = self.cx + (np.arange(W) - W / 2) * self.mpp; zs = self.cz + (np.arange(H) - H / 2) * self.mpp
        ix = np.clip(np.round((xs - g.x0) / g.dx).astype(np.int64), 0, g.rx - 1)
        iz = np.clip(np.round((zs - g.z0) / g.dz).astype(np.int64), 0, g.ry - 1)
        inside = ((xs >= g.x0) & (xs <= g.x1))[None, :] & ((zs >= g.z0) & (zs <= g.z1))[:, None]
        h = g.base[np.ix_(iz, ix)].astype(np.float32)
        if self.mpp < g.dx * 1.5:                         # zoomed in: use the game's detail clips too
            Z, X = np.meshgrid(zs, xs, indexing='ij')
            for c in self.t['height_clips']:
                ox0, oz0, odx, odz, A = g.clip_grid(c)
                ry, rx = A.shape
                fx = (X - ox0) / odx; fz = (Z - oz0) / odz
                m = (fx >= 0) & (fx <= rx - 1) & (fz >= 0) & (fz <= ry - 1)
                if m.any(): h[m] = A[np.round(fz[m]).astype(np.int64), np.round(fx[m]).astype(np.int64)]
        mw = self.t['material_weights']
        mat = T.shown_layer(*(mw[k].reshape(g.ry, g.rx)[np.ix_(iz, ix)] for k in ('first', 'second', 'blend')))
        void = (h <= -999) | ~inside
        Z = X = None
        for tr in self.tracks:
            bx0, bz0, bx1, bz1 = tr.pl.bbox()
            c0 = max(0, int((bx0 - xs[0]) / self.mpp) - 1); c1 = min(W, int((bx1 - xs[0]) / self.mpp) + 2)
            r0 = max(0, int((bz0 - zs[0]) / self.mpp) - 1); r1 = min(H, int((bz1 - zs[0]) / self.mpp) + 2)
            if c0 >= c1 or r0 >= r1: continue
            Zs, Xs = np.meshgrid(zs[r0:r1], xs[c0:c1], indexing='ij')
            new, w = tr.pl.sample(Xs, Zs)
            sub = h[r0:r1, c0:c1]
            h[r0:r1, c0:c1] = np.where(w > 0, sub * (1 - w) + new * w, sub)
            if getattr(tr.pl, 'surface', None) is not None:
                ms = mat[r0:r1, c0:c1]; sm = tr.pl.sample_surface(Xs, Zs); m_ = (w > 0.5) & (sm >= 0); ms[m_] = sm[m_]
            elif tr.pl.material:
                ms = mat[r0:r1, c0:c1]; ms[w > 0.5] = T.MATERIALS.index(tr.pl.material)
        return h, mat, void

    def render(self):
        if self.g is None: return
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        if W < 20 or H < 20: return
        h, mat, void = self.sample_view(W, H)
        hv = np.where(void, np.nan, h)
        gx = np.zeros_like(h); gz = np.zeros_like(h)
        gx[:, 1:-1] = (np.nan_to_num(hv[:, 2:]) - np.nan_to_num(hv[:, :-2])) / (2 * self.mpp)
        gz[1:-1, :] = (np.nan_to_num(hv[2:, :]) - np.nan_to_num(hv[:-2, :])) / (2 * self.mpp)
        n = np.stack([-gx, np.ones_like(h), -gz], -1); n /= np.linalg.norm(n, axis=-1, keepdims=True)
        L = np.array([-0.5, 0.75, -0.45]); L /= np.linalg.norm(L)
        shade = np.clip(n @ L, 0, 1) * 0.75 + 0.35
        col = PAL[np.clip(mat, 0, 7)] * shade[..., None]
        col[void] = VOID
        img = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8))
        self._img = ImageTk.PhotoImage(img)
        c = self.canvas
        c.delete('all'); c.create_image(0, 0, anchor='nw', image=self._img)
        # the game's tracks (detail clips)
        for i, cl in enumerate(self.t['height_clips']):
            b = cl['bounds']
            x0, y0 = self.to_screen(b['min'][0], b['min'][2]); x1, y1 = self.to_screen(b['max'][0], b['max'][2])
            c.create_rectangle(x0, y0, x1, y1, outline='#56d1ff', dash=(5, 3))
            c.create_text(x0 + 4, y0 + 4, anchor='nw', text='game track %d' % (i + 1), fill='#56d1ff')
        # your tracks
        g = self.g
        for tr in self.tracks:
            pts = [self.to_screen(x, z) for x, z in tr.pl.corners()]
            sel = tr is self.sel
            bx0, bz0, bx1, bz1 = tr.pl.bbox()
            off = bx0 < g.x0 or bz0 < g.z0 or bx1 > g.x1 or bz1 > g.z1
            tr.off_map = off
            c.create_polygon(*[v for p in pts for v in p], outline='#ff4040' if off else ('#ffd60a' if sel else '#e0b84a'),
                             fill='', width=3 if sel else 1.5)
            if off:
                cx_, cy_ = self.to_screen(tr.pl.cx, tr.pl.cz)
                c.create_text(cx_, cy_ + 16, text='goes off the map', fill='#ff6060', font=('Segoe UI', 9, 'bold'))
            # the image's top edge is drawn thicker so its orientation is clear
            c.create_line(*pts[0], *pts[1], fill='#ffffff', width=3 if sel else 2)
            cx, cy = self.to_screen(tr.pl.cx, tr.pl.cz)
            c.create_text(cx, cy, text=tr.name, fill='#ffd60a' if sel else '#e0b84a', font=('Segoe UI', 9, 'bold'))
        if self.sel in self.tracks:
            hs = self.handles(self.sel)
            nx, ny = hs['n'][:2]; rx_, ry_ = hs['rot'][:2]
            c.create_line(nx, ny, rx_, ry_, fill='#ffd60a', width=2)
            c.create_oval(rx_ - 7, ry_ - 7, rx_ + 7, ry_ + 7, fill='#ffd60a', outline='#3a3000')
            for name, (sx, sy, u, v) in hs.items():
                if name == 'rot': continue
                c.create_rectangle(sx - 6, sy - 6, sx + 6, sy + 6, fill='#ffffff' if len(name) == 2 else '#ffd60a',
                                   outline='#3a3000')
            ccx, ccy = self.to_screen(self.sel.pl.cx, self.sel.pl.cz)
            c.create_text(ccx, ccy + 16 + (16 if getattr(self.sel, 'off_map', False) else 0),
                          text='%g x %g m' % (round(self.sel.pl.width, 1), round(self.sel.pl.length, 1)),
                          fill='#ffd60a', font=('Segoe UI', 9, 'bold'))
        if self.ride:
            x, y = self.to_screen(*self.ride)
            c.create_oval(x - 6, y - 6, x + 6, y + 6, outline='#ff5a5a', width=2)
            c.create_text(x + 9, y, anchor='w', text='my last ride', fill='#ff8080')
        # scale bar
        bar = 10 ** math.floor(math.log10(max(self.mpp * 120, 1e-3)))
        for m in (1, 2, 5):
            if bar * m / self.mpp > 70: bar *= m; break
        px = bar / self.mpp
        c.create_line(16, H - 18, 16 + px, H - 18, fill='white', width=3)
        c.create_text(16 + px / 2, H - 30, text=('%g m' % bar) if bar < 1000 else ('%g km' % (bar / 1000)), fill='white')

    # ------------------------------------------------------------------ mouse
    def handles(self, tr):
        """Screen positions of the resize / rotate handles: {name: (sx, sy, ux, uz)} (u = local unit offset)."""
        pl = tr.pl; out = {}
        c, s = math.cos(math.radians(pl.rot)), math.sin(math.radians(pl.rot))
        for name, (u, v) in (('nw', (-.5, -.5)), ('ne', (.5, -.5)), ('se', (.5, .5)), ('sw', (-.5, .5)),
                             ('n', (0, -.5)), ('e', (.5, 0)), ('s', (0, .5)), ('w', (-.5, 0))):
            lx, lz = u * pl.width, v * pl.length
            out[name] = self.to_screen(pl.cx + lx * c - lz * s, pl.cz + lx * s + lz * c) + (u, v)
        # rotate handle: beyond the top edge (the image's top)
        nx, ny = out['n'][:2]; cx, cy = self.to_screen(pl.cx, pl.cz)
        d = math.hypot(nx - cx, ny - cy) or 1
        out['rot'] = (nx + (nx - cx) / d * 28, ny + (ny - cy) / d * 28, 0, 0)
        return out

    def handle_at(self, px, py):
        if not self.sel: return None
        for name, (sx, sy, u, v) in self.handles(self.sel).items():
            if abs(px - sx) <= 8 and abs(py - sy) <= 8: return name
        return None

    def track_at(self, x, z):
        for tr in reversed(self.tracks):
            lx, lz = tr.pl.local(np.array([x]), np.array([z]))
            if abs(lx[0]) <= tr.pl.width / 2 and abs(lz[0]) <= tr.pl.length / 2: return tr
        return None

    def on_down(self, e):
        if self.g is None: return
        h = self.handle_at(e.x, e.y)
        if h:
            pl = self.sel.pl
            self._drag = ('handle', h, pl.width, pl.length, pl.rot)
            return
        x, z = self.to_world(e.x, e.y)
        hit = self.track_at(x, z)
        if hit is not None:
            if hit is not self.sel:
                self.tlist.selection_set(hit.iid); self.sel = hit; self.show_fields()
            self._drag = (x, z, hit.pl.cx, hit.pl.cz)
        elif self.sel is not None:                         # click empty map: move the selected track here
            self.sel.pl.cx, self.sel.pl.cz = x, z
            self.refresh_ground(self.sel); self.show_fields(); self.render()
            self._drag = (x, z, x, z)

    def on_drag(self, e):
        if not self._drag or self.sel is None: return
        x, z = self.to_world(e.x, e.y)
        if self._drag[0] == 'handle':
            _, h, w0, l0, r0 = self._drag
            pl = self.sel.pl; shift = bool(e.state & 0x1)
            if h == 'rot':
                ang = math.degrees(math.atan2(x - pl.cx, -(z - pl.cz)))
                if shift: ang = round(ang / 15) * 15
                pl.rot = ang % 360
                self.fv['rot'].set('%g' % round(pl.rot, 1)); self.render(); return
            lx, lz = pl.local(np.array([x]), np.array([z])); lx, lz = abs(float(lx[0])), abs(float(lz[0]))
            lock = self.sel.lock and not shift
            if len(h) == 2:                                    # corner
                if lock:
                    k = max(2 * lx / w0, 2 * lz / l0); self.resize(w0 * k, l0 * k, final=False)
                else: self.resize(2 * lx, 2 * lz, final=False)
            elif h in ('e', 'w'):
                k = 2 * lx / w0; self.resize(w0 * k, l0 * k if lock else l0, final=False)
            else:
                k = 2 * lz / l0; self.resize(w0 * k if lock else w0, l0 * k, final=False)
            return
        x0, z0, cx0, cz0 = self._drag
        self.sel.pl.cx, self.sel.pl.cz = cx0 + x - x0, cz0 + z - z0
        self.render()

    def on_up(self, e):
        if self._drag and self._drag[0] == 'handle' and self.sel is not None:
            self.refresh_ground(self.sel); self.show_fields(); self.render(); self._drag = None; return
        if self._drag and self.sel is not None:
            self.refresh_ground(self.sel); self.show_fields(); self.render()
        self._drag = None

    def on_pan_down(self, e):
        self._pan = (e.x, e.y, self.cx, self.cz)

    def on_pan(self, e):
        x0, y0, cx, cz = self._pan
        self.cx, self.cz = cx - (e.x - x0) * self.mpp, cz - (e.y - y0) * self.mpp
        self.render()

    def on_motion(self, e):
        if self.g is None: return
        x, z = self.to_world(e.x, e.y)
        g = self.g
        if g.x0 <= x <= g.x1 and g.z0 <= z <= g.z1:
            hh = float(g.height(np.array([x]), np.array([z]))[0])
            k = int(round((z - g.z0) / g.dz)) * g.rx + int(round((x - g.x0) / g.dx)); mw = self.t['material_weights']
            m = T.MATERIALS[int(T.shown_layer(mw['first'][k], mw['second'][k], mw['blend'][k]))]
            self.info.configure(text='X %.1f   Z %.1f   height %s   surface %s   (%.2f m per pixel)'
                                % (x, z, 'void' if hh <= -999 else '%.2f m' % hh, m, self.mpp))
        else:
            self.info.configure(text='X %.1f   Z %.1f   (outside the map)' % (x, z))

    # ------------------------------------------------------------------ saving
    def save(self):
        if self.t is None or self._busy: return
        if not self.tracks:
            messagebox.showinfo('Map editor', 'Add a heightmap first.', parent=self); return
        off = [tr.name for tr in self.tracks if getattr(tr, 'off_map', False)]
        if off:
            messagebox.showerror('Map editor', '%s goes off the edge of the map (red box) - move it or make it '
                                 'smaller first.' % ', '.join(off), parent=self); return
        out = filedialog.asksaveasfilename(parent=self, title='Save the new map as', defaultextension='.bin',
                                           initialfile='map.blend.bin', filetypes=[('Game map', '*.bin')])
        if not out: return
        if os.path.abspath(out) == os.path.abspath(self.map_path):
            messagebox.showerror('Map editor', "Save it somewhere else first - then use Install, which keeps a "
                                 "backup of the game's map.", parent=self); return
        self._busy = True
        logs = []
        def prog(done, total): self.after(0, lambda: self.prog.configure(value=100.0 * done / max(total, 1)))
        def work():
            try:
                t = self.t
                for tr in self.tracks:
                    self.refresh_ground(tr)
                    T.apply_placement(t, tr.pl, log=logs.append)
                self.tf.save(out, {t['node']: t}, progress=prog)
                spec = self.tf.spec
                self.tf.close()
                tf = T.TerrainFile.open(self.map_path, spec)            # fresh copy of the game's map for more edits
                chk = T.TerrainFile.open(out, spec); n = len(chk.terrains[0]['height_clips']); chk.close()
                self.after(0, lambda: self.saved(tf, out, n, logs))
            except Exception as e:
                msg = str(e)
                try:                                                   # start again from the game's map
                    spec = self.tf.spec; self.tf.close(); tf = T.TerrainFile.open(self.map_path, spec)
                except Exception:
                    tf = None
                def fail():
                    self._busy = False
                    if tf is not None: self.tf, self.t = tf, tf.terrains[0]; self.g = T.Grid(self.t); self.render()
                    self.status.set("Couldn't save."); messagebox.showerror('Map editor', "Couldn't save:\n%s" % msg, parent=self)
                self.after(0, fail)
        self.status.set('Saving…'); threading.Thread(target=work, daemon=True).start()

    def saved(self, tf, out, n, logs):
        self._busy = False
        self.tf, self.t = tf, tf.terrains[0]; self.g = T.Grid(self.t)
        self.last_saved = out
        warns = [l[5:] for l in logs if l.startswith('WARN')]
        self.status.set('Saved %s (checked: it reads back with %d detail clips). Use "Install a saved map" to put it '
                        'in the game.%s' % (os.path.basename(out), n, ('\n⚠ ' + '\n⚠ '.join(warns)) if warns else ''))
        self.prog.configure(value=0); self.render()

    def install(self):
        if not self.game_root:
            messagebox.showinfo('Map editor', "Copy your saved map over the game's content\\meshes\\map.blend.bin "
                                "(keep a copy of the original).", parent=self); return
        src = filedialog.askopenfilename(parent=self, title='The map you saved', initialfile=getattr(self, 'last_saved', ''),
                                         filetypes=[('Game map', '*.bin')])
        if not src: return
        dst = self.map_path
        if not messagebox.askyesno('Map editor', 'Replace the game\'s map with\n%s ?\n\nThe original is kept as '
                                   'map.blend.bin.orig - "Restore the original map" puts it back.' % src, parent=self):
            return
        try:
            if not os.path.exists(dst + '.orig'): shutil.copy2(dst, dst + '.orig')
            if self.tf: self.tf.close()
            shutil.copy2(src, dst)
            self.status.set('Installed. Start the game to ride it. (Steam "verify files" also restores the original.)')
            self.load()
        except OSError as e:
            messagebox.showerror('Map editor', "Couldn't install:\n%s" % e, parent=self)

    def restore(self):
        dst = self.map_path
        if not os.path.exists(dst + '.orig'):
            messagebox.showinfo('Map editor', "The game's map hasn't been replaced (no map.blend.bin.orig).", parent=self); return
        if not messagebox.askyesno('Map editor', "Put the game's original map back?", parent=self): return
        try:
            if self.tf: self.tf.close()
            shutil.copy2(dst + '.orig', dst); self.status.set('Original map restored.'); self.load()
        except OSError as e:
            messagebox.showerror('Map editor', "Couldn't restore:\n%s" % e, parent=self)

    def close(self):
        try:
            if self.tf: self.tf.close()
        except Exception: pass
        self.destroy()

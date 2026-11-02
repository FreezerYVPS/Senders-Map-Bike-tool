"""
world_editor.py - the map editor and the heightmap studio in one window: the game's whole map, edited in
place with every studio tool (terrain, tracks, jumps, ruts, bumps, sculpting, surface painting, layers) and
a live 3D view of the spot you work on with the surrounding ground around it.

Edits live in "work areas" - squares of the real map at studio detail (default 256 m at 25 cm per pixel).
One is made automatically where you start editing, it grows when a track or sculpt reaches past it (up to
2049 pixels a side), and you can have as many as you like. Each area starts as the game's own ground.
"Save new map as…" writes every area into a copy of the game's map (heights, a 12.5 cm detail clip, the
surfaces, rocks / trees cleared where the ground changed); "Install" puts that copy in the game, keeping
the original. The game's files are only read. Your work is kept between sessions (autosaved workspace).
"""
import copy
import io
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image

import hm_core as C
import mxb_io
import mxs_io
import hm_studio as HS
import map_editor as ME
import terrain_io as T
import zigspec

MAX_N = 2049


def app_dir():
    d = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'GameModelExporter')
    os.makedirs(d, exist_ok=True)
    return d


def _settings_path():
    return os.path.join(app_dir(), 'settings.json')


def get_setting(app, key, default=None):
    cfg = getattr(app, 'cfg', None)
    if isinstance(cfg, dict) and cfg.get(key): return cfg[key]
    try:
        with open(_settings_path()) as f: return json.load(f).get(key) or default
    except Exception:
        return default


def set_setting(app, key, value):
    cfg = getattr(app, 'cfg', None)
    if isinstance(cfg, dict): cfg[key] = value             # the launcher writes it back when it closes, too
    try:
        try:
            with open(_settings_path()) as f: d = json.load(f)
        except Exception:
            d = {}
        d[key] = value
        with open(_settings_path() + '.tmp', 'w') as f: json.dump(d, f, indent=2)
        os.replace(_settings_path() + '.tmp', _settings_path())
    except OSError:
        pass


def default_save_dir():
    home = os.path.expanduser('~')
    for d in (os.path.join(home, 'Downloads'), os.path.join(home, 'Documents'), home):
        if os.path.isdir(d): return d
    return home


def log_error(text):
    import time as _t
    try:
        with open(os.path.join(app_dir(), 'studio_errors.log'), 'a') as f:
            f.write('[%s] %s\n' % (_t.strftime('%Y-%m-%d %H:%M:%S'), text))
    except OSError:
        pass


def safe_copy(src, dst):
    """Copy src over dst so that dst is never left half-written: copy to a temporary file next to it, check
    its size, then swap it in (an error leaves dst exactly as it was)."""
    part = dst + '.part'
    try:
        shutil.copyfile(src, part)
        if os.path.getsize(part) != os.path.getsize(src):
            raise OSError('the copy came out incomplete (disk full?)')
        os.replace(part, dst)
    finally:
        if os.path.exists(part):
            try: os.remove(part)
            except OSError: pass


class WorldEditor(HS.Studio):
    def __init__(self, app, map_path, spec_path, game_root=None):
        self.app, self.map_path, self.spec_path, self.game_root = app, map_path, spec_path, game_root
        self.tf = self.t = self.g = None
        self.areas = []; self.area = None
        self.ride = ME.last_ride_position(game_root) if game_root else None
        self._bg = None; self._bg_key = None; self._ctx_job = None; self._ws_job = None
        self.ws_path = os.path.join(app_dir(), 'map_workspace.hmworld')
        super().__init__(app, project=C.Project(65, 1.0))
        self.title('Map editor - %s' % map_path)
        self.after(80, self.load_map)
        self.after(150, self.setup_dnd)
        self.bind('<Return>', self.key_confirm, add='+'); self.bind('<KP_Enter>', self.key_confirm, add='+')
        self.bind('<Key-r>', self.key_turn, add='+'); self.bind('<Key-R>', self.key_turn, add='+')
        self.bind('<Escape>', self.key_escape, add='+')

    # ------------------------------------------------------------------ layout
    def build(self):
        super().build()
        for k in ('new',): self.tb[k].pack_forget()
        self.tb['open'].config(text='Open work…'); self.tb['save'].config(text='Save work'); self.tb['saveas'].config(text='Save work as…')
        self.tb['savemap'] = ttk.Button(self.tb['saveas'].master, text='Save map for the game', command=self.save_map)
        self.tb['savemap'].pack(side='left', padx=(6, 3), after=self.tb['saveas']); self.tb['savemap']._label = 'Save map for the game'
        self.send_btn.pack_forget()
        f = self.make_panel('Map', 0); self.panels['Map'] = f
        self.nb.select(0)
        self.build_map(f)

    def build_map(self, f):
        self.section(f, 'Work areas (edited parts of the map)')
        self.atree = ttk.Treeview(f, columns=('size',), show='tree headings', height=5, selectmode='browse')
        self.atree.heading('#0', text='Area'); self.atree.heading('size', text='size')
        self.atree.column('#0', width=230); self.atree.column('size', width=150, anchor='e')
        self.atree.pack(fill='x'); self.atree.bind('<<TreeviewSelect>>', self.on_area_pick)
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Button(r, text='New area here', command=lambda: self.new_area(self.vx, self.vz)).pack(side='left')
        ttk.Button(r, text='Size…', command=self.change_size).pack(side='left', padx=3)
        ttk.Button(r, text='Rename…', command=self.rename_area).pack(side='left')
        ttk.Button(r, text='Remove', command=self.remove_area).pack(side='left', padx=3)
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        self.allow_overlap = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text='Areas may overlap', variable=self.allow_overlap).pack(side='left')
        ttk.Button(r, text='Bring to front', command=lambda: self.restack(True)).pack(side='left', padx=(8, 0))
        ttk.Button(r, text='Send to back', command=lambda: self.restack(False)).pack(side='left', padx=3)
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        self.place_btn = ttk.Button(r, text='Move this area…', command=self.confirm_placement); self.place_btn.pack(side='left')
        ttk.Button(r, text='Turn 90°', command=self.turn_area).pack(side='left', padx=3)
        self.clear_all_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text='Clear rocks & trees under it all', variable=self.clear_all_var,
                        command=self.clear_all_changed).pack(side='left', padx=(6, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Track surface (material):').pack(side='left')
        self.area_surf = tk.StringVar(value='')
        cb = ttk.Combobox(r, textvariable=self.area_surf, values=[ME.SURFACES[k][0] for k in range(8)] + ["(keep the game's ground)"], state='readonly', width=30)
        cb.pack(side='left', padx=4); cb.bind('<<ComboboxSelected>>', lambda e: self.set_area_surface(self.area_surf.get()))
        self.hint(f, 'Placing: drag the orange box, R or Turn 90° turns it, Enter confirms, Esc stops moving. '
                     'Overlapping areas: the one in front (lower in the list) wins.')
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Imported track: raise (+) / sink (-)').pack(side='left')
        self.imp_rise = tk.StringVar(value='0')
        ttk.Entry(r, textvariable=self.imp_rise, width=6, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='m').pack(side='left')
        self.imp_edge = tk.StringVar(value='pad')
        ttk.Radiobutton(r, text='level pad', value='pad', variable=self.imp_edge).pack(side='left', padx=(8, 0))
        ttk.Radiobutton(r, text='fade in over', value='fade', variable=self.imp_edge).pack(side='left', padx=(4, 0))
        self.imp_blend = tk.StringVar(value='25')
        ttk.Entry(r, textvariable=self.imp_blend, width=4, justify='right').pack(side='left', padx=2)
        ttk.Label(r, text='m').pack(side='left')
        ttk.Button(r, text='Apply', command=self.apply_import_settings).pack(side='left', padx=(6, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='New areas:').pack(side='left')
        self.area_size = tk.StringVar(value='256'); self.area_px = tk.StringVar(value='0.25')
        ttk.Combobox(r, textvariable=self.area_size, values=['128', '256', '384', '512', '768', '1024'], width=5).pack(side='left', padx=3)
        ttk.Label(r, text='m at').pack(side='left')
        ttk.Combobox(r, textvariable=self.area_px, values=['0.125', '0.25', '0.5'], width=5).pack(side='left', padx=3)
        ttk.Label(r, text='m / pixel').pack(side='left')
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Button(r, text='Import a heightmap image as an area…', command=self.import_area).pack(side='left')
        self.section(f, 'MX Bikes / MX Simulator tracks (drag a .pkz / .saf onto the map, or pick one)')
        r = ttk.Frame(f); r.pack(fill='x')
        self.mxb_dir = get_setting(self.app, 'mxb_tracks_dir') or mxb_io.default_tracks_dir()
        self.mxb_pick = tk.StringVar()
        self.mxb_cb = ttk.Combobox(r, textvariable=self.mxb_pick, state='readonly', width=34)
        self.mxb_cb.pack(side='left', fill='x', expand=True)
        self.mxb_cb.bind('<ButtonPress-1>', lambda e: self.fill_mxb(), add='+')
        ttk.Button(r, text='Import', command=self.import_mxb_pick).pack(side='left', padx=(4, 0))
        ttk.Button(r, text='Other…', command=lambda: self.import_mxb(None)).pack(side='left', padx=(4, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        ttk.Label(r, text='Tracks folder:').pack(side='left')
        self.mxb_dir_var = tk.StringVar(value=self.mxb_dir or '(not found - pick it)')
        ttk.Label(r, textvariable=self.mxb_dir_var, foreground='#555', width=1).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(r, text='Change…', command=self.pick_mxb_dir).pack(side='left')
        ttk.Button(r, text='Default', command=lambda: self.pick_mxb_dir(default=True)).pack(side='left', padx=(3, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        ttk.Label(r, text='MX Simulator folder:').pack(side='left')
        self.mxs_dir = get_setting(self.app, 'mxs_dir') or next(iter(mxs_io.default_dirs()), None)
        self.mxs_dir_var = tk.StringVar(value=self.mxs_dir or '(not found - pick it)')
        ttk.Label(r, textvariable=self.mxs_dir_var, foreground='#555', width=1).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(r, text='Change…', command=self.pick_mxs_dir).pack(side='left')
        ttk.Button(r, text='Default', command=lambda: self.pick_mxs_dir(default=True)).pack(side='left', padx=(3, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        ttk.Button(r, text='Unpack a .saf…', command=self.unpack_saf).pack(side='left')
        ttk.Label(r, text='(not needed to import - .saf tracks are read directly)', foreground='#666').pack(side='left', padx=4)
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Blend areas into the map over').pack(side='left')
        self.area_edge = tk.StringVar(value='6'); ttk.Entry(r, textvariable=self.area_edge, width=5, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='m').pack(side='left')
        self.hint(f, 'Just start editing anywhere (Track, Jumps, Ruts, Sculpt, Surface tabs): an area of the real '
                     'ground is made there, and grows when you work past its edge. Click an area to switch to it. '
                     'Right- or middle-drag moves the view, the wheel zooms.')
        self.section(f, 'Whole map')
        r = ttk.Frame(f); r.pack(fill='x')
        self.flat = {'on': False, 'keep': [0, 1], 'level': None, 'blend': 40.0, 'clear': False}
        self.flat_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='Flatten the whole map', variable=self.flat_on).pack(side='left')
        ttk.Label(r, text='  at').pack(side='left')
        self.flat_level_var = tk.StringVar(value='auto')
        ttk.Entry(r, textvariable=self.flat_level_var, width=6, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='m, easing in over').pack(side='left')
        self.flat_blend_var = tk.StringVar(value='40')
        ttk.Entry(r, textvariable=self.flat_blend_var, width=4, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='m').pack(side='left')
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        self.flat_keep_box = ttk.Frame(r); self.flat_keep_box.pack(side='left')
        self.flat_clear = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='no trees / rocks on it', variable=self.flat_clear).pack(side='left', padx=(8, 0))
        ttk.Button(r, text='Apply', command=self.apply_flat).pack(side='right')
        self.hint(f, 'Flatten: everything except the game tracks you keep becomes one flat plane ("auto" = the '
                     'height of those tracks) - easy to place tracks on. Your areas sit on it. Un-tick and Apply to '
                     'get the game\'s ground back.')
        self.section(f, 'Go to')
        self.nav = ttk.Frame(f); self.nav.pack(fill='x')
        self.section(f, 'The game')
        self.prog = ttk.Progressbar(f, mode='determinate'); self.prog.pack(fill='x', pady=(0, 4))
        self.save_dir = get_setting(self.app, 'map_save_dir') or default_save_dir()
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Label(r, text='Maps are saved in:').pack(side='left')
        self.save_dir_var = tk.StringVar(value=self.save_dir)
        ttk.Label(r, textvariable=self.save_dir_var, foreground='#555', width=1).pack(side='left', fill='x', expand=True, padx=4)
        ttk.Button(r, text='Change…', command=self.pick_save_dir).pack(side='left')
        ttk.Button(r, text='Open', command=lambda: self.open_folder(self.save_dir)).pack(side='left', padx=(3, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        b1 = ttk.Button(r, text='Save map (map.blend.bin)', command=self.save_map); b1.pack(side='left', fill='x', expand=True)
        b2 = ttk.Button(r, text='Save map as…', command=lambda: self.save_map(ask=True))
        b2.pack(side='left', fill='x', expand=True, padx=(4, 0))
        self.save_btns = [b1, b2]
        for b in self.save_btns: b._label = b.cget('text')
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Button(r, text='Install a saved map…', command=self.install).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Restore original', command=self.restore).pack(side='left', fill='x', expand=True, padx=(4, 0))
        self.hint(f, "The game's map is only read. \"Save work\" (top) only keeps your editing for next time; "
                     '"Save map" writes the game file (map.blend.bin) into the folder above, Install puts it in the '
                     'game (the original is kept as map.blend.bin.orig).')

    # ------------------------------------------------------------------ map
    def load_map(self):
        self.status.set('Loading the map…')

        def work():
            try:
                spec = zigspec.ZigSpec.load(self.spec_path)
                tf = self.open_terrain(spec)
                if not tf.terrains: raise ValueError('no terrain found in this map file')
                self.after(0, lambda: self.map_loaded(tf))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda: (messagebox.showerror('Map editor', "Couldn't read the map:\n%s" % msg, parent=self),
                                       self.status.set("Couldn't read the map.")))
        threading.Thread(target=work, daemon=True).start()

    def map_loaded(self, tf):
        first = self.tf is None
        ME.MapEditor.load_palette(self)
        self.tf, self.t = tf, tf.terrains[0]; self.g = T.Grid(self.t); self._bg_key = None
        g = self.g
        self.fill_nav()
        if first:
            self.fit()
            if os.path.exists(self.ws_path):
                try:
                    with zipfile.ZipFile(self.ws_path) as z: n = len(json.loads(z.read('workspace.json'))['areas'])
                except Exception:
                    n = 0
                if n and messagebox.askyesno('Map editor', 'Carry on with your %d work area%s from last time?'
                                             % (n, 's' if n > 1 else ''), parent=self):
                    self.open_workspace(self.ws_path)
        self.status.set('Map: %.1f x %.1f km. Start editing anywhere - pick a tab on the right.' % ((g.x1 - g.x0) / 1000, (g.z1 - g.z0) / 1000))
        self.render()

    def world_range(self):
        return T.world_range(self.t)

    def _shown_layers(self):
        """The surface the game shows in every cell of the map (worked out once)."""
        if getattr(self, '_layers_t', None) is not self.t:
            g = self.g; mw = self.t['material_weights']
            self._layers = T.shown_layer(*(mw[k].reshape(g.ry, g.rx) for k in ('first', 'second', 'blend'))).astype(np.int8)
            self._layers_t = self.t
        return self._layers

    def world_image(self, W, H):
        """The map under the view (heights shaded, the game's surfaces), as a PIL image."""
        g = self.g
        if abs(self.view_rot) > 1e-9:
            return self._world_image_turned(W, H)
        xs = self.vx + (np.arange(W) - W / 2) * self.mpp; zs = self.vz - (np.arange(H) - H / 2) * self.mpp
        ix = np.clip(np.round((xs - g.x0) / g.dx).astype(np.int64), 0, g.rx - 1)
        iz = np.clip(np.round((zs - g.z0) / g.dz).astype(np.int64), 0, g.ry - 1)
        inside = ((xs >= g.x0) & (xs <= g.x1))[None, :] & ((zs >= g.z0) & (zs <= g.z1))[:, None]
        h = g.base[np.ix_(iz, ix)].astype(np.float32)
        if self.mpp < g.dx * 1.5:
            for c in self.t['height_clips']:                 # detail clips: rows / columns are separable
                ox0, oz0, odx, odz, A = g.clip_grid(c)
                ry, rx = A.shape
                fx = (xs - ox0) / odx; fz = (zs - oz0) / odz
                cx = np.nonzero((fx >= 0) & (fx <= rx - 1))[0]; cz = np.nonzero((fz >= 0) & (fz <= ry - 1))[0]
                if not len(cx) or not len(cz): continue
                h[np.ix_(cz, cx)] = A[np.ix_(np.round(fz[cz]).astype(np.int64), np.round(fx[cx]).astype(np.int64))]
        mat = self._shown_layers()[np.ix_(iz, ix)]
        void = (h <= -999) | ~inside
        hv = np.where(void, np.float32(0), h)
        gx = np.zeros_like(h); gz = np.zeros_like(h)
        gx[:, 1:-1] = (hv[:, 2:] - hv[:, :-2]) * np.float32(1 / (2 * self.mpp))
        gz[1:-1, :] = (hv[2:, :] - hv[:-2, :]) * np.float32(1 / (2 * self.mpp))
        L = np.array([-0.5, 0.75, -0.45], np.float32); L /= np.linalg.norm(L)
        sh = np.clip((-gx * L[0] + L[1] - gz * L[2]) / np.sqrt(gx * gx + 1 + gz * gz), 0, 1) * np.float32(0.75) + np.float32(0.35)
        col = ME.PAL.astype(np.float32)[np.clip(mat, 0, 7)] * sh[..., None]
        col[void] = ME.VOID
        return Image.fromarray(np.clip(col, 0, 255).astype(np.uint8))

    def _world_image_turned(self, W, H):
        """world_image for a turned view: every screen pixel's own spot on the map."""
        g = self.g
        c, s_ = math.cos(self.view_rot), math.sin(self.view_rot)
        a = ((np.arange(W, dtype=np.float32) + 0.5) - W / 2) * self.mpp
        b = -((np.arange(H, dtype=np.float32) + 0.5) - H / 2) * self.mpp
        X = self.vx + a[None, :] * c + b[:, None] * s_
        Z = self.vz - a[None, :] * s_ + b[:, None] * c
        ix = np.clip(np.round((X - g.x0) / g.dx).astype(np.int64), 0, g.rx - 1)
        iz = np.clip(np.round((Z - g.z0) / g.dz).astype(np.int64), 0, g.ry - 1)
        inside = (X >= g.x0) & (X <= g.x1) & (Z >= g.z0) & (Z <= g.z1)
        h = g.base[iz, ix].astype(np.float32)
        if self.mpp < g.dx * 1.5:
            for cl in self.t['height_clips']:
                ox0, oz0, odx, odz, A = g.clip_grid(cl)
                ry, rx = A.shape
                fx = (X - ox0) / odx; fz = (Z - oz0) / odz
                m = (fx >= 0) & (fx <= rx - 1) & (fz >= 0) & (fz <= ry - 1)
                if m.any(): h[m] = A[np.round(fz[m]).astype(np.int64), np.round(fx[m]).astype(np.int64)]
        mat = self._shown_layers()[iz, ix]
        void = (h <= -999) | ~inside
        hv = np.where(void, np.float32(0), h)
        ga = np.zeros_like(h); gb = np.zeros_like(h)               # slopes along the screen's x and up
        ga[:, 1:-1] = (hv[:, 2:] - hv[:, :-2]) * np.float32(1 / (2 * self.mpp))
        gb[1:-1, :] = -(hv[2:, :] - hv[:-2, :]) * np.float32(1 / (2 * self.mpp))
        gx = ga * c + gb * s_; gz = -ga * s_ + gb * c               # back to the map's x / z
        L = np.array([-0.5, 0.75, -0.45], np.float32); L /= np.linalg.norm(L)
        sh = np.clip((-gx * L[0] + L[1] - gz * L[2]) / np.sqrt(gx * gx + 1 + gz * gz), 0, 1) * np.float32(0.75) + np.float32(0.35)
        col = ME.PAL.astype(np.float32)[np.clip(mat, 0, 7)] * sh[..., None]
        col[void] = ME.VOID
        return Image.fromarray(np.clip(col, 0, 255).astype(np.uint8))

    # ------------------------------------------------------------------ coordinates: view in world metres
    def origin(self):
        if self.area is None: return 0.0, 0.0
        pl = self.p.placement; return pl['x0'], pl['z0']

    def vs_origin(self):                        # area-local (0, 0) in world metres (the view's space)
        return self.origin()

    def edit3d(self):
        return self.area is not None and super().edit3d()

    def to_world(self, cx, cy):
        return self.c2vs(cx, cy)

    def to_screen(self, x, z):
        return self.vs2c(x, z)

    def fit(self):
        if self.g is None: return
        g = self.g; W = max(200, self.canvas.winfo_width()); H = max(200, self.canvas.winfo_height())
        self.vx, self.vz = (g.x0 + g.x1) / 2, (g.z0 + g.z1) / 2
        self.mpp = max((g.x1 - g.x0) / W, (g.z1 - g.z0) / H) * 1.02; self.render()

    def goto(self, x, z, mpp):
        self.vx, self.vz, self.mpp = x, z, mpp; self.render()

    def wheel(self, e, up):
        x, z = self.to_world(e.x, e.y)
        self.mpp *= 1 / 1.2 if up else 1.2
        self.mpp = min(max(self.mpp, 0.02), 40.0)
        nx, nz = self.to_world(e.x, e.y); self.vx += x - nx; self.vz += z - nz
        self.request_render()

    # ------------------------------------------------------------------ areas
    def area_rect(self, a):
        pl = a['p'].placement; return pl['x0'], pl['z0'], pl['x0'] + a['p'].size, pl['z0'] + a['p'].size

    def area_at(self, x, z):
        for a in ([self.area] if self.area else []) + [b for b in self.areas if b is not self.area]:
            x0, z0, x1, z1 = self.area_rect(a)
            if x0 <= x <= x1 and z0 <= z <= z1: return a
        return None

    def ground(self, x0, z0, n, px):
        """The game's ground (base map + detail clips) on a studio grid."""
        size = (n - 1) * px
        sq = T.Placement(np.zeros((2, 2), np.float32), x0 + size / 2, z0 + size / 2, size, size, 0.0)
        h = T.ground_heights(self.t, sq, n, n)
        lo, hi = self.world_range()
        return np.where(np.isnan(h) | (h < lo - 1), lo, h).astype(np.float32)

    def ground_mat(self, x0, z0, n, px):
        """The game's surface (layer index) on a studio grid."""
        g = self.g; mw = self.t['material_weights']
        xs = x0 + np.arange(n) * px; zs = z0 + np.arange(n) * px
        ix = np.clip(np.round((xs - g.x0) / g.dx).astype(np.int64), 0, g.rx - 1)
        iz = np.clip(np.round((zs - g.z0) / g.dz).astype(np.int64), 0, g.ry - 1)
        return T.shown_layer(*(mw[k].reshape(g.ry, g.rx)[np.ix_(iz, ix)] for k in ('first', 'second', 'blend'))).astype(np.int8)

    def area_gmat(self, a):
        p = a['p']
        if a.get('gmat') is None or a['gmat'].shape != (p.n, p.n) or a.get('gkey') != (p.placement['x0'], p.placement['z0']):
            a['gmat'] = self.ground_mat(p.placement['x0'], p.placement['z0'], p.n, p.px)
            a['gkey'] = (p.placement['x0'], p.placement['z0'])
        return a['gmat']

    def ground_bio(self, x0, z0, n, px):
        """The game's boulder and tree-height maps (0..255) on a studio grid."""
        g = self.g
        xs = x0 + np.arange(n) * px; zs = z0 + np.arange(n) * px
        ix = np.clip(np.round((xs - g.x0) / g.dx).astype(np.int64), 0, g.rx - 1)
        iz = np.clip(np.round((zs - g.z0) / g.dz).astype(np.int64), 0, g.ry - 1)
        out = []
        for m in self.t['biome_height'][:2]:
            rx, ry = m['resolution']
            if (rx, ry) != (g.rx, g.ry):
                jx = np.clip(np.round((xs - g.x0) / (g.x1 - g.x0) * (rx - 1)).astype(np.int64), 0, rx - 1)
                jz = np.clip(np.round((zs - g.z0) / (g.z1 - g.z0) * (ry - 1)).astype(np.int64), 0, ry - 1)
                out.append(m['map'].reshape(ry, rx)[np.ix_(jz, jx)].astype(np.int16))
            else:
                out.append(m['map'].reshape(ry, rx)[np.ix_(iz, ix)].astype(np.int16))
        return out[0], out[1]

    def area_gbio(self, a):
        p = a['p']
        if a.get('gbio') is None or a['gbio'][0].shape != (p.n, p.n) or a.get('bkey') != (p.placement['x0'], p.placement['z0']):
            a['gbio'] = self.ground_bio(p.placement['x0'], p.placement['z0'], p.n, p.px)
            a['bkey'] = (p.placement['x0'], p.placement['z0'])
        return a['gbio']

    def nature_maps(self, win):
        """What will grow in the active area after saving: your painting, else the game's own (cleared when the
        area clears everything, or where the ground was given a surface)."""
        if self.area is None or self.t is None: return super().nature_maps(win)
        grk, gcan = self.area_gbio(self.area)
        p = self.p
        cleared = bool(p.terrain.get('clear_all')) | (self.info['surface'][win] >= 0)
        can = np.where(p.canopy[win] >= 0, p.canopy[win], np.where(cleared, 0, gcan[win]))
        rk = np.where(p.rocks[win] >= 0, p.rocks[win], np.where(cleared, 0, grk[win]))
        return can, rk

    def shade_surface(self, win):
        s = self.info['surface'][win]
        if self.area is None or self.t is None: return s
        return np.where(s >= 0, s, self.area_gmat(self.area)[win])

    def palette(self):
        return ME.PAL

    def ground_track_direction(self, x, z, radius=12.0):
        """Direction / width of the game's track painted in the ground around local x, z (its track-dirt
        cells, 'gravel' / 'path'): the main axis of those cells."""
        if self.area is None or self.t is None: return None
        gm = self.area_gmat(self.area); px = self.p.px
        bw = C.brush_window(self.p.n, px, x, z, radius)
        if bw is None: return None
        win, q = bw
        m = (q < 1) & np.isin(gm[win], (1, 5))
        if m.sum() < 20: return None
        zz, xx = np.nonzero(m)
        P = np.stack([xx, zz], 1).astype(np.float64) * px; P -= P.mean(0)
        w, v = np.linalg.eigh(P.T @ P / len(P))
        main = v[:, 1]
        if w[1] < 1.5 * w[0]: return None                  # no clear direction (a wide open patch)
        heading = math.degrees(math.atan2(main[1], main[0])) % 180
        width = float(np.clip(4 * math.sqrt(max(w[0], 0)), 4, 14))
        return heading, width

    def make_area(self, x0, z0, n, px, name=None):
        lo, hi = self.world_range()
        p = C.Project(n, px)
        p.base_img = self.ground(x0, z0, n, px)
        p.terrain['kind'] = 'ground'; p.terrain['on_ground'] = True
        p.world = [lo, hi]; p.export = {'mode': 'world', 'lo': lo, 'hi': hi}
        p.placement = {'x0': x0, 'z0': z0, 'size': p.size}
        for k, v in self.presets.get('templates', {}).items(): p.templates.setdefault(k, v)
        a = {'name': name or 'Area %d' % (len(self.areas) + 1), 'p': p, 'hist': HS.History(self), 'pil': None}
        return a

    def clamp_origin(self, x0, z0, size):
        g = self.g
        x0 = min(max(x0, g.x0), max(g.x0, g.x1 - size)); z0 = min(max(z0, g.z0), max(g.z0, g.z1 - size))
        return x0, z0

    def new_area(self, cx, cz, size=None, px=None):
        if self.g is None: return None
        try:
            size = size or float(self.area_size.get()); px = px or float(self.area_px.get())
        except ValueError:
            size, px = 256.0, 0.25
        n = int(round(size / px)) + 1
        if n > MAX_N:
            px = size / (MAX_N - 1); n = MAX_N
        size = (n - 1) * px
        x0, z0 = self.clamp_origin(cx - size / 2, cz - size / 2, size)
        other = self.overlapping(x0, z0, size)
        if other is not None:                       # areas never overlap: use / grow the one that's there
            if other is not self.area: self.activate(other)
            if self.grow_to(cx, cz) is not None: return other
            self.status.set('That is next to "%s", which can\'t grow any further at its detail - pick a spot further '
                            'away for a new area.' % other['name'])
            return None
        self.status.set('Reading the ground for a new work area…'); self.update_idletasks()
        a = self.make_area(x0, z0, n, px)
        self.areas.append(a); self.refresh_areas(); self.activate(a)
        self.status.set('New work area: %.0f x %.0f m of the game\'s ground at %.3g m per pixel.' % (size, size, px))
        return a

    def overlapping(self, x0, z0, size, skip=None):
        if getattr(self, 'allow_overlap', None) is not None and self.allow_overlap.get(): return None
        for a in self.areas:
            if a is skip: continue
            ax0, az0, ax1, az1 = self.area_rect(a)
            if x0 < ax1 - 1e-6 and x0 + size > ax0 + 1e-6 and z0 < az1 - 1e-6 and z0 + size > az0 + 1e-6: return a
        return None

    def activate(self, a):
        if a is self.area: return
        if self.area is not None: self.area['pil'] = self.pil
        self.placing = False
        if hasattr(self, 'place_btn'): self.place_btn.config(text='Move this area…')
        self.area = a; self.p = a['p']; self.hist = a['hist']
        if hasattr(self, 'clear_all_var'): self.clear_all_var.set(bool(a['p'].terrain.get('clear_all', True)))
        if hasattr(self, 'area_surf'): self.area_surf.set(a['p'].terrain.get('surface_all', ''))
        self.load_import_settings()
        self.H = self.info = self.rgb = None; self.pil = None
        self.sel = None; self.sel_pts = set()
        if self.gl is not None: self.gl.H = None
        self.refresh_areas()
        self.recompute(now=True)
        self.schedule_context()

    def on_area_pick(self, e=None):
        s = self.atree.selection()
        if not s: return
        a = next((b for b in self.areas if str(id(b)) == s[0]), None)
        if a is not None and a is not self.area:
            self.activate(a)
            x0, z0, x1, z1 = self.area_rect(a); self.goto((x0 + x1) / 2, (z0 + z1) / 2, max(0.2, (x1 - x0) / 650))

    def refresh_areas(self):
        t = self.atree
        t.delete(*t.get_children())
        for a in self.areas:
            p = a['p']
            t.insert('', 'end', iid=str(id(a)), text=('▶ ' if a is self.area else '   ') + a['name'],
                     values=('%.0f m, %.3g m/px' % (p.size, p.px),))
        if self.area is not None and t.exists(str(id(self.area))): t.selection_set(str(id(self.area)))

    def rename_area(self):
        if self.area is None: return
        n = simpledialog.askstring('Area', 'Name:', initialvalue=self.area['name'], parent=self)
        if n: self.area['name'] = n; self.refresh_areas(); self.autosave_ws()

    def remove_area(self):
        a = self.area
        if a is None: return
        if not messagebox.askyesno('Map editor', 'Remove "%s" and everything made in it?' % a['name'], parent=self): return
        self.areas.remove(a); self.area = None
        self.p = C.Project(65, 1.0); self.hist = HS.History(self); self.H = self.info = self.rgb = self.pil = None
        if self.areas: self.activate(self.areas[-1])
        self.refresh_areas(); self.refresh_all(); self.render(); self.autosave_ws()

    def reframe(self, x0, z0, n, why=''):
        """Move / resize the active area to a new square (same detail), keeping everything made in it."""
        a = self.area; p = self.p; px = p.px
        # keep the old pixels on the new grid
        ox, oz = p.placement['x0'], p.placement['z0']
        kx = int(round((x0 - ox) / px)); kz = int(round((z0 - oz) / px))
        x0, z0 = ox + kx * px, oz + kz * px
        self.hist.push_full()
        q = C.Project(n, px); q.set_state(p.state()); q.n, q.px = n, px
        q.base_img = self.ground(x0, z0, n, px)
        # paste the old arrays (and the old terrain, which may be an imported image) at their place
        r0, c0 = -kz, -kx
        rs0, cs0 = max(0, r0), max(0, c0); rs1, cs1 = min(n, r0 + p.n), min(n, c0 + p.n)
        src = (slice(rs0 - r0, rs1 - r0), slice(cs0 - c0, cs1 - c0)); dst = (slice(rs0, rs1), slice(cs0, cs1))
        for name in C.PAINT_ARRAYS:
            arr = getattr(q, name); arr[dst] = getattr(p, name)[src]
        if p.terrain.get('kind') == 'image' and p.base_img is not None:
            q.base_img[dst] = p.base_img[src]
        dx, dz = ox - x0, oz - z0                           # move the drawn things with the new corner
        for tr in q.tracks:
            for pt in tr['points']: pt[0] += dx; pt[1] += dz
        for rt in q.ruts:
            for pt in rt['points']: pt[0] += dx; pt[1] += dz
        for b in q.bumps:
            if 'x' in b: b['x'] += dx; b['z'] += dz
        for j in q.jumps:
            if j.get('track', 0) < 0: j['x'] += dx; j['z'] += dz
        q.placement = {'x0': x0, 'z0': z0, 'size': q.size}
        p.__dict__.update(q.__dict__); p._cache = {}
        # keep the view still
        self.H = self.info = self.rgb = self.pil = None
        if self.gl is not None: self.gl.H = None
        self.refresh_areas(); self.recompute(now=True); self.schedule_context()
        if why: self.status.set(why)
        return dx, dz

    def grow_to(self, x, z, margin=30.0):
        """Grow the active area so world point (x, z) is inside it. Returns the local shift or None."""
        x0, z0, x1, z1 = self.area_rect(self.area)
        nx0, nz0 = min(x0, x - margin), min(z0, z - margin)
        nx1, nz1 = max(x1, x + margin), max(z1, z + margin)
        size = max(nx1 - nx0, nz1 - nz0)
        px = self.p.px
        n = int(math.ceil(size / px)) + 1
        if n > MAX_N: return None
        size = (n - 1) * px
        nx0, nz0 = self.clamp_origin(nx0 if nx1 - nx0 >= size else nx0 - (size - (nx1 - nx0)) / 2,
                                     nz0 if nz1 - nz0 >= size else nz0 - (size - (nz1 - nz0)) / 2, size)
        if self.overlapping(nx0, nz0, size, skip=self.area) is not None: return None
        return self.reframe(nx0, nz0, n, 'The work area grew to %.0f x %.0f m.' % (size, size))

    def change_size(self):
        if self.area is None:
            self.status.set('Pick or make a work area first.'); return
        size = simpledialog.askfloat('Work area', 'Size of this area (metres, at %.3g m per pixel; up to %.0f m):'
                                     % (self.p.px, (MAX_N - 1) * self.p.px), initialvalue=round(self.p.size),
                                     minvalue=16, maxvalue=(MAX_N - 1) * self.p.px, parent=self)
        if not size: return
        n = int(round(size / self.p.px)) + 1
        x0, z0, x1, z1 = self.area_rect(self.area)
        cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
        s = (n - 1) * self.p.px
        nx0, nz0 = self.clamp_origin(cx - s / 2, cz - s / 2, s)
        if self.overlapping(nx0, nz0, s, skip=self.area) is not None:
            self.status.set('That size would overlap another work area.'); return
        self.reframe(nx0, nz0, n, 'Work area is now %.0f x %.0f m.' % (s, s))

    def import_area(self, path=None):
        """A heightmap image becomes a new work area (on the map's scale, or sitting on the ground)."""
        if self.g is None: return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title='Heightmap image (16-bit PNG is best)',
                                              filetypes=[('Images', '*.png *.tif *.tiff *.jpg *.bmp *.pgm'), ('All', '*.*')])
        if not path: return
        if mxb_io.is_track(path) or mxs_io.is_track(path): return self.import_mxb(path)
        try:
            img, levels = ME.load_heightmap(path, with_levels=True)
        except Exception as e:
            messagebox.showerror('Map editor', "Couldn't read it:\n%s" % e, parent=self); return
        d = tk.Toplevel(self); d.title('Import heightmap'); d.transient(self); d.grab_set()
        mppx = tk.StringVar(value='0.25'); mode = tk.StringVar(value='world'); rng = tk.StringVar(value='10')
        lo, hi = self.world_range()
        ttk.Label(d, text='%s: %d x %d pixels' % (os.path.basename(path), img.shape[1], img.shape[0])).grid(row=0, column=0, columnspan=3, sticky='w', padx=8, pady=6)
        ttk.Label(d, text='Metres per pixel').grid(row=1, column=0, sticky='w', padx=8)
        ttk.Entry(d, textvariable=mppx, width=7).grid(row=1, column=1, sticky='w')
        ttk.Radiobutton(d, text="The game map's scale (black = %.1f m, white = %.1f m)" % (lo, hi), value='world',
                        variable=mode).grid(row=2, column=0, columnspan=3, sticky='w', padx=8)
        ttk.Radiobutton(d, text='Sitting on the ground, black → white =', value='ground', variable=mode).grid(row=3, column=0, sticky='w', padx=8)
        ttk.Entry(d, textvariable=rng, width=7).grid(row=3, column=1, sticky='w'); ttk.Label(d, text='m').grid(row=3, column=2, sticky='w')
        if levels <= 256:
            ttk.Label(d, text='8-bit image: on the map scale every grey step is %.1f m - a 16-bit PNG is much better.'
                      % ((hi - lo) / 255), foreground='#a33', wraplength=420).grid(row=4, column=0, columnspan=3, padx=8, sticky='w')
        res = {}

        def ok():
            try: res.update(mppx=float(mppx.get()), mode=mode.get(), rng=float(rng.get()))
            except ValueError: return
            d.destroy()
        ttk.Button(d, text='Import', command=ok).grid(row=5, column=0, columnspan=3, pady=8)
        self.wait_window(d)
        if not res: return
        mp = res['mppx']; ih, iw = img.shape
        if res['mode'] == 'world':
            hm, mode = lo + img * (hi - lo), 'absolute'
        else:
            hm, mode = img * res['rng'], 'relative'
        self.area_from_heights(os.path.splitext(os.path.basename(path))[0], hm.astype(np.float32), (iw - 1) * mp,
                               (ih - 1) * mp, blend=8.0, mode=mode, note=os.path.basename(path), place=False)

    # ------------------------------------------------------------------ MX Bikes tracks
    def pick_mxb_dir(self, default=False):
        if default:
            d = mxb_io.default_tracks_dir()
            set_setting(self.app, 'mxb_tracks_dir', '')
        else:
            d = filedialog.askdirectory(parent=self, title="MX Bikes tracks folder (usually Documents\\PiBoSo\\MX Bikes\\mods\\tracks)",
                                        initialdir=self.mxb_dir or os.path.expanduser('~'), mustexist=True)
            if not d: return
            d = os.path.normpath(d); set_setting(self.app, 'mxb_tracks_dir', d)
        self.mxb_dir = d
        self.mxb_dir_var.set(d or '(not found - pick it)')
        self.fill_mxb(); self.mxb_pick.set('')
        self.status.set('MX Bikes tracks folder: %s (%d tracks)' % (d, len(self.mxb_tracks)) if d else
                        'No MX Bikes tracks folder found - use Change… to pick it.')

    def choose(self, title, text, options):
        """A small window with a list to pick from; returns the picked option or None."""
        d = tk.Toplevel(self); d.title(title); d.transient(self); d.grab_set()
        ttk.Label(d, text=text).pack(anchor='w', padx=8, pady=(8, 4))
        lb = tk.Listbox(d, height=min(12, len(options)), width=40); lb.pack(padx=8, fill='both', expand=True)
        for o in options: lb.insert('end', o)
        lb.selection_set(0)
        res = []
        def ok(*_):
            sel = lb.curselection()
            if sel: res.append(options[sel[0]])
            d.destroy()
        lb.bind('<Double-Button-1>', ok)
        ttk.Button(d, text='OK', command=ok).pack(pady=8)
        self.wait_window(d)
        return res[0] if res else None

    def pick_mxs_dir(self, default=False):
        if default:
            d = next(iter(mxs_io.default_dirs()), None); set_setting(self.app, 'mxs_dir', '')
        else:
            d = filedialog.askdirectory(parent=self, title='MX Simulator folder (where its .saf tracks are, usually '
                                        'C:\\Program Files (x86)\\MX Simulator)',
                                        initialdir=self.mxs_dir or os.path.expanduser('~'), mustexist=True)
            if not d: return
            d = os.path.normpath(d); set_setting(self.app, 'mxs_dir', d)
        self.mxs_dir = d
        self.mxs_dir_var.set(d or '(not found - pick it)')
        self.fill_mxb(); self.mxb_pick.set('')
        n = sum(1 for t in self.mxb_tracks if '(MX Simulator' in t[0])
        self.status.set('MX Simulator folder: %s (%d tracks)' % (d, n) if d else 'No MX Simulator folder found - use Change… to pick it.')

    def unpack_saf(self):
        src = filedialog.askopenfilename(parent=self, title='MX Simulator .saf to unpack', initialdir=self.mxs_dir or None,
                                         filetypes=[('MX Simulator archive', '*.saf'), ('All', '*.*')])
        if not src: return
        dest = filedialog.askdirectory(parent=self, title='Unpack it into which folder?',
                                       initialdir=os.path.dirname(src))
        if not dest: return
        if self.in_game_folder(os.path.join(dest, 'x')):
            messagebox.showerror('Map editor', "Pick a folder outside Senders' game folder.", parent=self); return
        self.status.set('Unpacking %s…' % os.path.basename(src)); self.update_idletasks()
        try:
            n = mxs_io.extract(src, dest)
        except Exception as e:
            messagebox.showerror('Map editor', "Couldn't unpack it:\n%s" % e, parent=self); return
        self.status.set('Unpacked %d files from %s into %s' % (n, os.path.basename(src), dest))

    def fill_mxb(self):
        self.mxb_tracks = (mxb_io.list_tracks(self.mxb_dir) if self.mxb_dir else []) + \
            (mxs_io.list_tracks(self.mxs_dir) if getattr(self, 'mxs_dir', None) else [])
        self.mxb_cb['values'] = [n + ('' if ok else '  (protected)') for n, p, ok in self.mxb_tracks] or \
            ['(no tracks found - use Other…)']

    def import_mxb_pick(self):
        if not getattr(self, 'mxb_tracks', None): self.fill_mxb()
        k = self.mxb_cb.current()
        if k < 0 or k >= len(self.mxb_tracks):
            self.status.set('Pick a track in the list first.'); return
        self.import_mxb(self.mxb_tracks[k][1])

    def import_mxb(self, path=None):
        """An MX Bikes or MX Simulator track's terrain becomes a new work area, at its real size."""
        if self.g is None: return
        if path is None:
            path = filedialog.askopenfilename(parent=self, title='MX Bikes track (.pkz / .trh) or MX Simulator track (.saf / terrain.hf)',
                                              initialdir=self.mxb_dir or self.mxs_dir or None,
                                              filetypes=[('Tracks', '*.pkz *.trh *.saf *.hf'), ('MX Bikes track', '*.pkz *.trh'),
                                                         ('MX Simulator track', '*.saf *.hf'), ('All', '*.*')])
            if not path: return
        self.status.set('Reading %s…' % os.path.basename(path)); self.update_idletasks()
        mxs = mxs_io.is_track(path)
        try:
            if mxs:
                which = None
                if path.lower().endswith('.saf'):
                    tr = mxs_io.saf_tracks(path)
                    if len(tr) > 1:
                        which = self.choose('Which track?', 'This .saf holds several tracks:', [x.strip('/') for x in tr])
                        if which is None: return
                        which = tr[[x.strip('/') for x in tr].index(which)]
                t = mxs_io.load(path, which)
            else:
                t = mxb_io.load(path)
        except mxs_io.NotATrack as e:
            messagebox.showinfo('Map editor', str(e), parent=self); self.status.set(str(e)); return
        except mxb_io.ProtectedTrack as e:
            messagebox.showinfo('Map editor', str(e) + '\n\nUnprotected tracks (and unpacked track folders) work.', parent=self)
            self.status.set(str(e)); return
        except Exception as e:
            messagebox.showerror('Map editor', "Couldn't read that track:\n%s" % e, parent=self); return
        h = t['heights']
        d = tk.Toplevel(self); d.title('Import %s' % t['name']); d.transient(self); d.grab_set()
        rot = tk.StringVar(value='0'); mir = tk.BooleanVar(value=False); raise_m = tk.StringVar(value='0')
        ttk.Label(d, text='%s: %.0f x %.0f m, %d x %d heights (%.2f m each), %.1f m from lowest to highest.'
                  % (t['name'], t['size_x'], t['size_z'], h.shape[1], h.shape[0], t['size_x'] / (h.shape[1] - 1), t['range']),
                  wraplength=460).grid(row=0, column=0, columnspan=3, sticky='w', padx=8, pady=6)
        surf = tk.StringVar(value=ME.SURFACES[0][0])
        sf = ttk.LabelFrame(d, text=' Track surface (material) - the whole track becomes this ')
        sf.grid(row=1, column=0, columnspan=3, sticky='we', padx=8, pady=(0, 6))
        ttk.Combobox(sf, textvariable=surf, values=[ME.SURFACES[k][0] for k in range(8)] + ["(keep the game's ground)"],
                     state='readonly', width=36).pack(side='left', padx=6, pady=6)
        ttk.Label(sf, text='(change it later in the Map tab)', foreground='#666').pack(side='left', padx=4)
        ttk.Label(d, text='Turn it').grid(row=2, column=0, sticky='w', padx=8)
        ttk.Combobox(d, textvariable=rot, values=['0', '90', '180', '270'], width=5, state='readonly').grid(row=2, column=1, sticky='w')
        ttk.Checkbutton(d, text='Mirror it', variable=mir).grid(row=2, column=2, sticky='w')
        ttk.Label(d, text='It sits on the ground where you are looking. Raise it by (m)').grid(row=3, column=0, columnspan=2, sticky='w', padx=8)
        ttk.Entry(d, textvariable=raise_m, width=6).grid(row=3, column=2, sticky='w')
        scale = tk.StringVar(value='100')
        sr = ttk.Frame(d); sr.grid(row=9, column=0, columnspan=3, sticky='w', padx=8, pady=(4, 0))
        ttk.Label(sr, text='Size').pack(side='left')
        ttk.Entry(sr, textvariable=scale, width=5, justify='right').pack(side='left', padx=3)
        ttk.Label(sr, text='%  (100 = true size; heights scale too)', foreground='#666').pack(side='left')
        if mxs:
            ttk.Button(sr, text='Match the MX Sim bike (104%)', command=lambda: scale.set('104')).pack(side='left', padx=(6, 0))
            ttk.Label(d, text="MX Simulator's bikes have true-size wheels (0.70 / 0.665 m); Senders' bike is about 4% bigger "
                      "(0.725 / 0.69 m). 104% keeps jumps the same size next to the bike as in MX Simulator.",
                      foreground='#666', wraplength=460).grid(row=11, column=0, columnspan=3, sticky='w', padx=8)
        ttk.Label(d, text='It keeps its exact shape on a level pad; around its edges it meets the map over 25 m. '
                  'You can drag it into place afterwards (Enter confirms).', foreground='#666', wraplength=460).grid(row=4, column=0, columnspan=3, sticky='w', padx=8)
        pv = ttk.Frame(d); pv.grid(row=5, column=0, columnspan=3, padx=8, pady=(6, 0), sticky='w')
        c1 = tk.Canvas(pv, width=200, height=200, highlightthickness=0, background='#20242a'); c1.pack(side='left')
        imgs = {}
        if t.get('map_png'):
            try:
                from PIL import ImageTk
                mp = Image.open(io.BytesIO(t['map_png'])).convert('RGBA'); mp.thumbnail((200, 200))
                bg = Image.new('RGBA', mp.size, (32, 36, 42, 255)); bg.alpha_composite(mp)
                imgs['map'] = ImageTk.PhotoImage(bg, master=d)
                c2 = tk.Canvas(pv, width=200, height=200, highlightthickness=0, background='#20242a'); c2.pack(side='left', padx=(8, 0))
                c2.create_image(100, 100, image=imgs['map'])
                ttk.Label(d, text='Left: how it will sit in Senders (seen from above). Right: the track\'s own map. If '
                          'they are mirror images, tick "Mirror it".', foreground='#666', wraplength=460).grid(
                    row=6, column=0, columnspan=3, sticky='w', padx=8)
            except Exception:
                pass
        if t.get('cropped'):
            ttk.Label(d, text='Only the part around the track is taken (%.0f x %.0f m of the whole %.0f x %.0f m terrain).'
                      % (t['size_x'], t['size_z'], t['full_size'][0], t['full_size'][1]), foreground='#666',
                      wraplength=460).grid(row=7, column=0, columnspan=3, sticky='w', padx=8)

        def preview(*_):
            try:
                from PIL import ImageTk
                hh, _sw = mxb_io.orient(h, int(rot.get()), mir.get())
                step = max(1, max(hh.shape) // 200)
                sm = hh[::step, ::step]
                rgb = C.shade(sm, step * t['size_x'] / max(h.shape[1] - 1, 1))
                im = Image.fromarray(rgb[::-1])                 # z up the screen, like the map view
                im.thumbnail((200, 200)); imgs['h'] = ImageTk.PhotoImage(im, master=d)
                c1.delete('all'); c1.create_image(100, 100, image=imgs['h'])
            except Exception:
                pass
        rot.trace_add('write', preview); mir.trace_add('write', preview); preview()
        res = {}

        def ok():
            try: res.update(rot=int(rot.get()), mir=mir.get(), up=float(raise_m.get() or 0), surf=surf.get(),
                            scale=max(10.0, min(400.0, float(scale.get() or 100))) / 100.0)
            except ValueError: return
            d.destroy()
        ttk.Button(d, text='Import', command=ok).grid(row=12, column=0, columnspan=3, pady=8)
        d.bind('<Return>', lambda e: ok())
        self.wait_window(d)
        if not res: return
        h, swap = mxb_io.orient(h, res['rot'], res['mir'])
        wm, lm = (t['size_z'], t['size_x']) if swap else (t['size_x'], t['size_z'])
        k = res.get('scale', 1.0)
        if abs(k - 1.0) > 1e-6:
            h = h * k; wm *= k; lm *= k
        self.area_from_heights(t['name'], h, wm, lm, rise=res['up'], blend=25.0, flat=True, source='mxs' if mxs else 'mxb',
                               note='%s track %s (%.0f x %.0f m)' % ('MX Simulator' if mxs else 'MX Bikes', t['name'], wm, lm))
        if self.area is not None and self.area['name'] == t['name'] and res.get('surf'):
            msg = self.status.get(); self.set_area_surface(res['surf']); self.area_surf.set(res['surf']); self.status.set(msg)
        if self.area is not None and self.area['name'] == t['name']:
            self.p.terrain['import']['handed'] = 2

    def area_from_heights(self, name, h, wm, lm, rise=0.0, blend=15.0, note='', mode='relative', place=True, flat=False,
                          source=''):
        """A new work area holding heights h (metres, rows = z) spanning wm x lm metres, centred in the view,
        sitting on the ground (median to median, mode 'relative') or at its own heights ('absolute'), blended
        into the ground at its edges. The heights are kept with the area so it can be moved / turned later."""
        ih, iw = h.shape
        mp = min(wm / (iw - 1), lm / (ih - 1))
        size = max(wm, lm) + 2 * blend
        px = max(mp, size / (MAX_N - 1), 0.125)
        n = int(math.ceil(size / px)) + 1
        size = (n - 1) * px
        x0, z0 = self.clamp_origin(self.vx - size / 2, self.vz - size / 2, size)
        other = self.overlapping(x0, z0, size)
        if other is not None:
            messagebox.showinfo('Map editor', 'It would overlap the work area "%s" - move the view somewhere free '
                                'first (or remove that area).' % other['name'], parent=self); return
        self.status.set('Building the area…'); self.update_idletasks()
        a = self.make_area(x0, z0, n, px, name=name)
        p = a['p']
        p.import_src = np.ascontiguousarray(h, np.float32)
        p.terrain['import'] = {'wm': float(wm), 'lm': float(lm), 'rise': float(rise), 'blend': float(blend), 'mode': mode,
                               'flat': bool(flat), 'source': source}
        p.base_img = self.compose_base(p)
        p.terrain['kind'] = 'image'; p.terrain['image'] = name
        self.areas.append(a); self.refresh_areas(); self.activate(a)
        self.goto(x0 + size / 2, z0 + size / 2, max(0.2, size / 700))
        if place:
            self.start_placing(a)
            self.status.set('Imported %s. Drag it to where it should go (Map tab), turn it if needed, then press '
                            '"Confirm placement" - that clears the rocks and trees under it.' % (note or name))
        else:
            self.status.set('Imported %s as a %.0f m work area (%.3g m per pixel) - all the tools work on it now.'
                            % (note or name, size, px))

    def restack(self, front):
        if self.area is None: return
        self.areas.remove(self.area)
        if front: self.areas.append(self.area)
        else: self.areas.insert(0, self.area)
        self.refresh_areas(); self.render(); self.autosave_ws()
        self.status.set('"%s" is now %s - where areas overlap, the one in front wins.'
                        % (self.area['name'], 'in front' if front else 'at the back'))

    def apply_import_settings(self):
        if self.area is None or not self.p.terrain.get('import'):
            self.status.set('This area has no imported track or heightmap.'); return
        try: rise = float(self.imp_rise.get()); blend = max(0.5, float(self.imp_blend.get()))
        except ValueError:
            self.status.set('Raise / sink and the fade distance must be numbers.'); return
        imp = self.p.terrain['import']
        self.hist.push_full()
        imp['rise'] = rise; imp['flat'] = self.imp_edge.get() == 'pad'; imp['blend'] = blend
        self.refresh_import_base()
        self.status.set('Imported track %s by %.2f m, %s.' % ('raised' if rise >= 0 else 'sunk', abs(rise),
                        'on a level pad' if imp['flat'] else 'fading into the map over %.0f m' % blend))

    def load_import_settings(self):
        imp = self.p.terrain.get('import') if self.area is not None else None
        if not imp or not hasattr(self, 'imp_rise'): return
        self.imp_rise.set('%g' % imp.get('rise', 0.0)); self.imp_edge.set('pad' if imp.get('flat') else 'fade')
        self.imp_blend.set('%g' % imp.get('blend', 25.0))

    def refresh_import_base(self):
        """Rebuild the active area's terrain from its imported track (after sinking, erasing...)."""
        p = self.p
        if not p.terrain.get('import'): return
        p.base_img = self.compose_base(p)
        p._cache.pop('base_key', None); self.area['objmask'] = None
        self._mask_dirty = True; self.recompute(now=True); self.autosave_ws()

    def extra_sculpt_tools(self):
        return ['Erase import', 'Restore import']

    def after_sculpt(self, name):
        if name == 'impkeep': self.refresh_import_base()

    def after_undo(self):
        if self.area is not None and self.p.terrain.get('import'):
            self.p.base_img = self.compose_base(self.p); self.area['objmask'] = None
        super().after_undo()

    def draw_nature(self, rgb, win):
        super().draw_nature(rgb, win)
        if self.area is not None and self.p.terrain.get('import') and self.tab() == 'Sculpt':
            k = (1 - np.clip(self.p.impkeep[win], 0, 1))[..., None] * 0.55     # erased parts of the import: blue
            if k.max() > 0: rgb[:] = (rgb * (1 - k) + np.array([60, 120, 230]) * k).astype(np.uint8)

    def compose_base(self, p):
        """The area's terrain: the game's ground where it is now, with the imported heights (if any) on it."""
        x0, z0 = p.placement['x0'], p.placement['z0']
        gk = (x0, z0, p.n, p.px, getattr(self, '_map_version', 0))
        gc = getattr(p, '_ground', None)
        if gc is None or gc[0] != gk:
            p._ground = gc = (gk, self.ground(x0, z0, p.n, p.px))
        G = gc[1]
        imp = p.terrain.get('import'); h = getattr(p, 'import_src', None)
        if not imp or h is None: return G
        ih, iw = h.shape; wm, lm, size = imp['wm'], imp['lm'], p.size
        X, Z = p.coords()
        u = (X - (size - wm) / 2) / max(wm, 1e-6); v = (Z - (size - lm) / 2) / max(lm, 1e-6)
        inside = (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
        val = C.bilinear(h.astype(np.float32), np.clip(u, 0, 1) * (iw - 1), np.clip(v, 0, 1) * (ih - 1))
        if imp.get('mode') == 'absolute':
            shift = imp.get('rise', 0.0)
        else:
            shift = float(np.median(G[inside])) - float(np.median(h)) + imp.get('rise', 0.0)
        hgt = val + shift
        imp['shift'] = float(shift)                    # the track's objects are lifted the same way
        bl = max(imp.get('blend', 10.0), 0.1)
        if imp.get('flat'):
            # exactly the track's own heights on a level pad; it meets the surrounding ground only outside its
            # edges (the edge heights carried outwards, eased into the ground over the blend distance)
            ox = np.maximum(np.maximum(-u * wm, (u - 1) * wm), 0); oz = np.maximum(np.maximum(-v * lm, (v - 1) * lm), 0)
            dout = np.hypot(ox, oz)
            wgt = 1 - C.smoothstep(dout / bl)
        else:
            dd = np.minimum(np.minimum(u * wm, (1 - u) * wm), np.minimum(v * lm, (1 - v) * lm))
            wgt = np.where(inside, C.smoothstep(dd / bl), 0)
        keep = getattr(p, 'impkeep', None)
        if keep is not None and keep.shape == G.shape: wgt = wgt * np.clip(keep, 0, 1)   # erased: the ground
        return (G * (1 - wgt) + hgt * wgt).astype(np.float32)

    # ------------------------------------------------------------------ moving / turning / placing an area
    def start_placing(self, a=None):
        if a is not None and a is not self.area: self.activate(a)
        if self.area is None: self.status.set('Pick an area first.'); return
        self.placing = True
        self.nb.select(0)
        self.place_btn.config(text='Confirm placement ✔'); self.render()

    def confirm_placement(self):
        if self.area is None: return
        if not getattr(self, 'placing', False):
            self.start_placing(); self.status.set('Drag the area on the map to move it, then press "Confirm placement".'); return
        self.placing = False
        self.place_btn.config(text='Move this area…')
        self.p.terrain['clear_all'] = bool(self.clear_all_var.get())
        self.render(); self.autosave_ws()
        self.status.set('Placed. %s Save new map as… writes it into the game\'s map.' %
                        ('Rocks and trees under the whole area will be cleared.' if self.p.terrain['clear_all']
                         else 'Rocks and trees are cleared only where the ground changed or got a surface.'))

    def _typing(self):
        return isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox, tk.Spinbox))

    def key_confirm(self, e=None):
        if getattr(self, 'placing', False) and not self._typing():
            self.confirm_placement(); return 'break'

    def key_turn(self, e=None):
        if getattr(self, 'placing', False) and not self._typing():
            self.turn_area(); return 'break'

    def key_escape(self, e=None):
        if getattr(self, 'placing', False):
            self.placing = False; self.place_btn.config(text='Move this area…'); self.render()
            self.status.set('Stopped moving (the area stays where it is now).')

    def footprint(self, p=None):
        """Mask of the imported track (its own rectangle) in the area, or the whole area when nothing was imported."""
        p = p or self.p
        imp = p.terrain.get('import')
        if not imp: return np.ones((p.n, p.n), bool)
        X, Z = p.coords(); size = p.size
        return (np.abs(X - size / 2) <= imp['wm'] / 2) & (np.abs(Z - size / 2) <= imp['lm'] / 2)

    def set_area_surface(self, label):
        """Give the whole imported track (the whole area if nothing was imported) one of the game's surfaces."""
        if self.area is None: self.status.set('Pick an area first.'); return
        if label.startswith('(keep'): val = -2
        else:
            v = dict(ME.SURFACES).get(label)
            if v is None or v == '__studio__': return
            val = C.SURFACES.index(v)
        full = (slice(0, self.p.n), slice(0, self.p.n))
        self.hist.push_array('surf', full, self.p.surf.copy())
        m = self.footprint()
        self.p.surf[m] = val
        self.p.terrain['surface_all'] = label
        self.recompute(now=True)
        self.status.set('The whole track is now "%s" (paint over it in the Surface tab to change parts).' % label)

    def reposition(self, x0, z0):
        """Move the active area: everything in it moves along, the terrain is re-seated on the ground there."""
        p = self.p; size = p.size
        x0, z0 = self.clamp_origin(x0, z0, size)
        other = self.overlapping(x0, z0, size, skip=self.area)
        if other is not None:
            self.status.set('It would overlap "%s" - put it somewhere else.' % other['name']); self.render(); return False
        self.hist.push_full()
        p.placement = {'x0': float(x0), 'z0': float(z0), 'size': size}
        p.base_img = self.compose_base(p) if p.terrain.get('kind') in ('image', 'ground', 'generated') else p.base_img
        p._cache = {}; self.area['gmat'] = None; self.area['gbio'] = None; self.area['objmask'] = None
        self.H = self.info = self.rgb = self.pil = None
        if self.gl is not None: self.gl.H = None
        self.recompute(now=True); self.schedule_context(); self.refresh_areas()
        return True

    def turn_area(self):
        """Turn the active area 90 degrees clockwise (as seen on the map), with everything in it."""
        if self.area is None: return
        p = self.p; S = p.size
        self.hist.push_full()
        for name in C.PAINT_ARRAYS:
            setattr(p, name, np.ascontiguousarray(np.rot90(getattr(p, name), 1)))   # clockwise with z up the screen
        imp = p.terrain.get('import')
        if imp and getattr(p, 'import_src', None) is not None:
            p.import_src = np.ascontiguousarray(np.rot90(p.import_src, 1)); imp['wm'], imp['lm'] = imp['lm'], imp['wm']
            if p.terrain.get('objects'): p.terrain['objects']['rot'] = (p.terrain['objects']['rot'] + 90) % 360
            self.area['objmask'] = None
        elif p.terrain.get('kind') == 'image' and p.base_img is not None:
            p.base_img = np.ascontiguousarray(np.rot90(p.base_img, 1))
        tf = lambda x, z: (z, S - x)
        for tr in p.tracks:
            for pt in tr['points']: pt[0], pt[1] = tf(pt[0], pt[1])
        for rt in p.ruts:
            for pt in rt['points']: pt[0], pt[1] = tf(pt[0], pt[1])
        for b in p.bumps:
            if 'x' in b: b['x'], b['z'] = tf(b['x'], b['z'])
        for j in p.jumps:
            if j.get('track', 0) < 0:
                j['x'], j['z'] = tf(j['x'], j['z']); j['heading'] = (j.get('heading', 0) - 90) % 360
        if imp: p.base_img = self.compose_base(p)
        p._cache = {}; self.area['gmat'] = None; self.area['gbio'] = None
        self.H = self.info = self.rgb = self.pil = None
        if self.gl is not None: self.gl.H = None
        self.recompute(now=True); self.schedule_context()
        self.status.set('Turned 90°.')

    # ------------------------------------------------------------------ drag and drop from Explorer
    def setup_dnd(self):
        try:
            import tkinterdnd2
            tkinterdnd2.TkinterDnD._require(self._root())
            for w in (self.canvas, self):
                w.drop_target_register(tkinterdnd2.DND_FILES)
                w.dnd_bind('<<Drop>>', self.on_drop)
            if self.gl is not None:
                try:
                    self.gl.drop_target_register(tkinterdnd2.DND_FILES); self.gl.dnd_bind('<<Drop>>', self.on_drop)
                except Exception:
                    pass
            self.dnd_ok = True
        except Exception as e:
            self.dnd_ok = False
            print('drag and drop unavailable:', e)

    def on_drop(self, e):
        try: files = self.tk.splitlist(e.data)
        except Exception: files = [e.data]
        for f in files[:1]:
            if mxb_io.is_track(f) or mxs_io.is_track(f): self.after(10, lambda f=f: self.import_mxb(f))
            elif f.lower().endswith(('.png', '.tif', '.tiff', '.jpg', '.jpeg', '.bmp', '.pgm')): self.after(10, lambda f=f: self.import_area(f))
            elif f.lower().endswith('.hmworld'): self.after(10, lambda f=f: self.open_workspace(f))
            else: self.status.set("That file isn't an MX Bikes / MX Simulator track, a heightmap image or saved map work.")
        return getattr(e, 'action', 'copy')

    # ------------------------------------------------------------------ evaluation / views
    def recompute(self, now=False):
        if self.area is None:
            self.render(); return
        super().recompute(now)

    def _evaluate(self):
        if self.area is None: return
        super()._evaluate()
        self.area['pil'] = self.pil
        self.autosave_ws()

    def push_gl(self, throttle=False, win=None):
        if self.area is None: return
        super().push_gl(throttle, win)

    def schedule_context(self):
        if self._ctx_job: self.after_cancel(self._ctx_job)
        self._ctx_job = self.after(300, self.make_context)

    def make_context(self):
        """The ground around the active area for the 3D view (3x its size, coarser)."""
        self._ctx_job = None
        if self.gl is None or self.area is None or self.g is None: return
        x0, z0, x1, z1 = self.area_rect(self.area); s = x1 - x0
        cn = 385
        cx0, cz0, cs = x0 - s, z0 - s, 3 * s
        X, Z = np.meshgrid(cx0 + np.arange(cn) * cs / (cn - 1), cz0 + np.arange(cn) * cs / (cn - 1))
        h = self.g.height(X, Z)
        lo, hi = self.world_range()
        h = np.where(h <= -999, lo, h).astype(np.float32)
        g = self.g; mw = self.t['material_weights']
        ix = np.clip(np.round((X - g.x0) / g.dx).astype(np.int64), 0, g.rx - 1)
        iz = np.clip(np.round((Z - g.z0) / g.dz).astype(np.int64), 0, g.ry - 1)
        k = iz * g.rx + ix
        mat = T.shown_layer(mw['first'][k], mw['second'][k], mw['blend'][k]).astype(np.int8)
        rgb = C.shade(h, cs / (cn - 1), surface=mat, slope_tint=False, palette=ME.PAL)
        self.gl.set_context(h, rgb, -s, -s, cs)

    def compose(self, bx0, by0, bx1, by1):
        """The picture for the screen box (bx0, by0)-(bx1, by1): the game's map with the work areas on it."""
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        key = (round(self.vx, 4), round(self.vz, 4), round(self.mpp, 6), W, H, round(self.view_rot, 6))
        if key != self._bg_key:
            self._bg = self.world_image(W, H); self._bg_key = key
        img = self._bg.crop((bx0, by0, bx1, by1)) if (bx0, by0, bx1, by1) != (0, 0, W, H) else self._bg.copy()
        mv = self._drag['off'] if (self._drag and self._drag.get('kind') == 'move_area') else (0.0, 0.0)
        for a in [b for b in self.areas if b is not self.area] + ([self.area] if self.area else []):
            pil = self.pil if a is self.area else a.get('pil')
            if pil is None: continue
            x0, z0, x1, z1 = self.area_rect(a)
            if a is self.area: x0, z0, x1, z1 = x0 + mv[0], z0 + mv[1], x1 + mv[0], z1 + mv[1]
            corners = [self.to_screen(x, z) for x, z in ((x0, z0), (x1, z0), (x1, z1), (x0, z1))]
            ax0 = max(bx0, int(math.floor(min(p[0] for p in corners)))); ax1 = min(bx1, int(math.ceil(max(p[0] for p in corners))))
            ay0 = max(by0, int(math.floor(min(p[1] for p in corners)))); ay1 = min(by1, int(math.ceil(max(p[1] for p in corners))))
            if ax1 - ax0 < 1 or ay1 - ay0 < 1: continue
            px = a['p'].px
            data = self.affine((ax0, ay0), 1 / px, 1 / px, x0, z0)
            rs = Image.BILINEAR if self.mpp > px else Image.NEAREST
            reg = pil.transform((ax1 - ax0, ay1 - ay0), Image.AFFINE, data, resample=rs)
            mask = None
            if abs(self.view_rot) > 1e-9:                           # a turned area doesn't fill its box
                mask = Image.new('L', pil.size, 255).transform(reg.size, Image.AFFINE, data, resample=Image.NEAREST, fillcolor=0)
            if a is self.area and self.p.reference.get('visible') and self.p.reference.get('path'):
                try:
                    if self.ref_img is None or self.ref_img[0] != self.p.reference['path']:
                        self.ref_img = (self.p.reference['path'], Image.open(self.p.reference['path']).convert('RGB'))
                    ri = self.ref_img[1]; sxr, syr = ri.width / self.p.size, ri.height / self.p.size
                    r2 = ri.transform(reg.size, Image.AFFINE, self.affine((ax0, ay0), sxr, syr, x0, z0), resample=Image.BILINEAR)
                    reg = Image.blend(reg, r2, float(self.p.reference.get('opacity', 0.5)))
                except Exception:
                    pass
            if mask is not None:
                img.paste(reg, (ax0 - bx0, ay0 - by0), mask); continue
            img.paste(reg, (ax0 - bx0, ay0 - by0))
        return img

    def patch_render(self, win):
        if self._drag and self._drag.get('kind') == 'move_area': return False
        return super().patch_render(win)

    def render(self):
        c = self.canvas
        if not c.winfo_ismapped(): return
        W, H = c.winfo_width(), c.winfo_height()
        if W < 4 or H < 4: return
        self._cancel_render()
        if self.g is None:
            c.delete('all'); c.create_text(W / 2, H / 2, text='Loading the map…', fill='#ccc'); return
        mv = self._drag['off'] if (self._drag and self._drag.get('kind') == 'move_area') else (0.0, 0.0)
        img = self.compose(0, 0, W, H)
        self._shown = img
        from PIL import ImageTk
        self._tk = ImageTk.PhotoImage(img)
        c.delete('all'); c.create_image(0, 0, image=self._tk, anchor='nw')
        for i, cl in enumerate(self.t['height_clips']):
            b = cl['bounds']
            pts = self.poly_of(b['min'][0], b['min'][2], b['max'][0], b['max'][2])
            c.create_polygon(*pts, outline='#56d1ff', fill='', dash=(5, 3))
            tx, ty = self.to_screen(b['min'][0], b['max'][2])
            c.create_text(tx + 4, ty + 4, anchor='nw', text='game track %d' % (i + 1), fill='#56d1ff')
        for a in self.areas:
            x0, z0, x1, z1 = self.area_rect(a)
            on = a is self.area
            if on: x0, z0, x1, z1 = x0 + mv[0], z0 + mv[1], x1 + mv[0], z1 + mv[1]
            p0 = self.to_screen(x0, z1)                                        # the area's north-west corner
            placing = on and getattr(self, 'placing', False)
            c.create_polygon(*self.poly_of(x0, z0, x1, z1), fill='', outline='#ff8a2a' if placing else ('#ffd60a' if on else '#c8a848'),
                             width=3 if placing else (2 if on else 1), dash=() if on else (4, 3))
            lab = a['name'] + ('   - drag to move, then Confirm placement' if placing else '') + \
                ('   (rocks & trees cleared)' if a['p'].terrain.get('clear_all') and not placing else '')
            c.create_text(p0[0] + 4, p0[1] - 2, anchor='sw', text=lab, fill='#ff8a2a' if placing else ('#ffd60a' if on else '#c8a848'))
        if self.ride:
            x, y = self.to_screen(*self.ride)
            c.create_oval(x - 6, y - 6, x + 6, y + 6, outline='#ff5a5a', width=2)
            c.create_text(x + 9, y, anchor='w', text='my last ride', fill='#ff8080')
        if self.area is not None:
            self.draw_overlays(c, W, H)
        else:
            self._scale_bar(c, W, H)
        self.draw_compass(c, W, H)
        self._rendered()

    def _scale_bar(self, c, W, H):
        L = 10 ** math.floor(math.log10(max(self.mpp * 120, 1e-3)))
        for m_ in (5, 2, 1):
            if m_ * L / self.mpp < 160: L *= m_; break
        c.create_line(14, H - 18, 14 + L / self.mpp, H - 18, fill='#fff', width=2)
        c.create_text(14 + L / self.mpp / 2, H - 28, text=('%g m' % L) if L < 1000 else '%g km' % (L / 1000), fill='#fff')

    def on_motion(self, e):
        x, z = self.to_world(e.x, e.y)
        a = self.area
        if a is not None:
            x0, z0, x1, z1 = self.area_rect(a)
            if x0 <= x <= x1 and z0 <= z <= z1 and self.H is not None:
                super().on_motion(e)
                self.cursor_lbl.config(text='x %.1f  z %.1f m   ' % (x, z) + self.cursor_lbl.cget('text').split('m   ', 1)[-1])
                return
        if self.g is not None:
            h = float(self.g.height(np.array([x]), np.array([z]))[0])
            self.cursor_lbl.config(text='x %.1f  z %.1f m   height %s' % (x, z, ('%.2f m' % h) if h > -999 else '(no ground)'))
        self.canvas.delete('brush')
        if self.tab() in ('Sculpt', 'Surface', 'Nature'):
            r = (self.brush['radius'] if self.tab() == 'Sculpt' else self.paint_radius()) / self.mpp
            self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline='#fff', tags='brush')

    # ------------------------------------------------------------------ mouse: make / pick / grow areas
    def on_down(self, e):
        if self.g is None: return
        t = self.tab(); x, z = self.to_world(e.x, e.y)
        hit = self.area_at(x, z)
        if getattr(self, 'placing', False) and self.area is not None and hit is self.area:
            self._drag = {'kind': 'move_area', 'start': (x, z), 'off': (0.0, 0.0)}; return
        if t in ('Map', 'Terrain', 'Layers'):
            if hit is not None and hit is not self.area: self.activate(hit); return
            self.on_pan_start(e); self._drag = {'kind': 'pan'}; return
        if hit is not None and hit is not self.area:
            self.activate(hit)
        elif hit is None:
            grow = self.area is not None and t in ('Track', 'Ruts') and self._near_area(x, z, 250)
            grow = grow or (self.area is not None and t in ('Sculpt', 'Surface', 'Nature', 'Jumps') and self._near_area(x, z, 40))
            if grow and self.grow_to(x, z) is not None:
                pass
            else:
                if self.area is not None and t == 'Track' and self.sel and self.sel[0] == 'point':
                    self.status.set('The work area can\'t grow that far (max %.0f m at this detail) - a new area is '
                                    'started there.' % ((MAX_N - 1) * self.p.px))
                self.new_area(x, z)
            if self.area is None or not self._near_area(x, z, 0): return
        super().on_down(e)

    def on_up(self, e):
        d = self._drag
        if d and d.get('kind') == 'move_area':
            self._drag = None
            dx, dz = d['off']
            if abs(dx) > 1e-6 or abs(dz) > 1e-6:
                x0, z0, x1, z1 = self.area_rect(self.area)
                px = self.p.px
                self.reposition(x0 + round(dx / px) * px, z0 + round(dz / px) * px)
            self.render(); return
        super().on_up(e)

    def clear_all_changed(self):
        if self.area is not None:
            self.p.terrain['clear_all'] = bool(self.clear_all_var.get()); self.autosave_ws()

    def _near_area(self, x, z, d):
        x0, z0, x1, z1 = self.area_rect(self.area)
        return x0 - d <= x <= x1 + d and z0 - d <= z <= z1 + d

    def on_drag(self, e):
        d = self._drag
        if d and d.get('kind') == 'move_area':
            x, z = self.to_world(e.x, e.y); d['off'] = (x - d['start'][0], z - d['start'][1]); self.request_render(); return
        if d and d.get('kind') == 'point' and self.area is not None:
            x, z = self.to_world(e.x, e.y)
            if not self._near_area(x, z, 0) and self._near_area(x, z, 250):
                tr_j = (d['track'], d['j'])
                if self.grow_to(x, z) is not None:
                    self._drag = d
        super().on_drag(e)

    # ------------------------------------------------------------------ terrain tab in the map
    def terrain_changed(self, d, key, val):
        if self.area is None: self.status.set('Make or pick a work area first (Map tab).'); return
        self.hist.push_state(); d[key] = val; self.p.terrain['kind'] = 'generated'; self.p.terrain['on_ground'] = True
        self.recompute()

    def apply_terrain_dict(self, d):
        if self.area is None: return
        self.hist.push_state(); self.p.terrain.update(d); self.p.terrain['kind'] = 'generated'; self.p.terrain['on_ground'] = True
        self.tform.load(self.p.terrain); self.recompute()

    def new_seed(self):
        if self.area is None: return
        super().new_seed(); self.p.terrain['on_ground'] = True; self.recompute()

    def use_generated(self):
        if self.area is None: return
        self.hist.push_state(); self.p.terrain['kind'] = 'generated'; self.p.terrain['on_ground'] = True; self.recompute()

    def import_base(self):
        self.import_area()

    def refresh_all(self):
        super().refresh_all()
        if self.area is None: return
        self.size_lbl.config(text='This work area: %.0f x %.0f m at %.3g m per pixel (Map tab: Size…)' % (self.p.size, self.p.size, self.p.px))
        kind = self.p.terrain.get('kind')
        self.terrain_kind.config(text={'generated': "Using: the game's ground with generated hills added (they fade out "
                                                    "towards the area's edges).",
                                       'image': 'Using: the imported image %s.' % self.p.terrain.get('image', ''),
                                       'ground': "Using: the game's own ground."}.get(kind, ''))

    # ------------------------------------------------------------------ workspace (all areas)
    def autosave_ws(self):
        if self._ws_job: self.after_cancel(self._ws_job)
        self._ws_job = self.after(3000, lambda: self.save_workspace(self.ws_path, quiet=True))

    def save_workspace(self, path, quiet=False):
        self._ws_job = None
        if self._drag: self._ws_job = self.after(1500, lambda: self.save_workspace(path, quiet)); return
        try:
            tmp = path + '.tmp'
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
                info = {'map': self.map_path, 'areas': [], 'flat': getattr(self, 'flat', None)}
                for k, a in enumerate(self.areas):
                    bio = io.BytesIO(); a['p'].save(bio)
                    z.writestr('area_%d.hmproj' % k, bio.getvalue())
                    info['areas'].append({'name': a['name'], 'file': 'area_%d.hmproj' % k, 'active': a is self.area})
                z.writestr('workspace.json', json.dumps(info, indent=1))
            os.replace(tmp, path)
            if not quiet: self.status.set('Kept your map work for next time (%s). This is not the game map - use '
                                          '"Save map for the game" for that.' % path)
        except OSError as e:
            if not quiet: messagebox.showerror('Map editor', "Couldn't save:\n%s" % e, parent=self)

    def open_workspace(self, path):
        try:
            with zipfile.ZipFile(path) as z:
                info = json.loads(z.read('workspace.json'))
                areas = []
                for d in info['areas']:
                    p = C.Project.open(io.BytesIO(z.read(d['file'])))
                    p.terrain.pop('objects', None)         # track objects are switched off for now
                    areas.append(({'name': d['name'], 'p': p, 'hist': HS.History(self), 'pil': None}, d.get('active')))
        except Exception as e:
            messagebox.showerror('Map editor', "Couldn't open it:\n%s" % e, parent=self); return
        self.areas = [a for a, _ in areas]; self.area = None
        fl = info.get('flat')
        if fl and fl != self.flat:                     # the map was flattened then: flatten it again first
            self.flat = fl
            self.flat_on.set(bool(fl.get('on'))); self.flat_blend_var.set('%g' % fl.get('blend', 40))
            self.flat_level_var.set('auto' if fl.get('level') is None else '%g' % fl['level'])
            self.flat_clear.set(bool(fl.get('clear')))
            self.after(100, lambda: self.load_map_keep(reseat=True))
        fixed = self.repair_mxb_imports()
        unflipped = self.unflip_upright_imports()
        act = next((a for a, on in areas if on), self.areas[-1] if self.areas else None)
        # every area's picture for the overview
        for a in self.areas:
            if a is act: continue
            H, info_ = a['p'].evaluate()
            sf = info_['surface']; sf = np.where(sf >= 0, sf, self.area_gmat(a))
            a['pil'] = Image.fromarray(C.shade(H, a['p'].px, surface=sf, palette=ME.PAL))
        if act is not None: self.activate(act)
        self.refresh_areas(); self.render()
        if unflipped:
            self.status.set('Turned %s back the right way round (the previous version turned MX Bikes imports over - '
                            'the map view is the right way round now instead). Everything you made in it turned along.'
                            % ', '.join(unflipped))
            self.autosave_ws()
        if fixed:
            self.status.set('Repaired the heights of %s (imported by the previous version, which read MX Bikes '
                            'heights wrongly - that made the walls).' % ', '.join(fixed))
            self.autosave_ws()

    def unflip_upright_imports(self):
        """Areas imported by the version that turned MX Bikes tracks over (z): mirror the whole area back, with
        everything made in it, so the track is in the game's own orientation again."""
        done = []
        for a in self.areas:
            p = a['p']; imp = p.terrain.get('import')
            if not imp or not imp.pop('upright', False): continue
            S = p.size
            for name in C.PAINT_ARRAYS:
                setattr(p, name, np.ascontiguousarray(getattr(p, name)[::-1]))
            if getattr(p, 'import_src', None) is not None: p.import_src = np.ascontiguousarray(p.import_src[::-1])
            if p.base_img is not None: p.base_img = np.ascontiguousarray(p.base_img[::-1])
            for tr in p.tracks:
                for pt in tr['points']:
                    pt[1] = S - pt[1]
                    if len(pt) > 3: pt[3] = -pt[3]                       # banking leans the other way
            for rt in p.ruts:
                for pt in rt['points']: pt[1] = S - pt[1]
                if rt.get('berm_side') in ('left', 'right'): rt['berm_side'] = 'right' if rt['berm_side'] == 'left' else 'left'
            for b in p.bumps:
                if 'z' in b: b['z'] = S - b['z']
            for j in p.jumps:
                j['offset'] = -float(j.get('offset', 0) or 0); j['angle'] = -float(j.get('angle', 0) or 0)
                if j.get('track', 0) < 0:
                    j['z'] = S - j['z']; j['heading'] = (-float(j.get('heading', 0))) % 360
            imp['handed'] = 2
            p._cache = {}; a['gmat'] = None; a['gbio'] = None; a['objmask'] = None; a['pil'] = None
            try: p.base_img = self.compose_base(p)
            except Exception: pass
            done.append(a['name'])
        return done

    def repair_mxb_imports(self):
        """MX Bikes tracks imported by an earlier version were read as unsigned heights (walls, lost hills)."""
        fixed = []
        for a in self.areas:
            p = a['p']; imp = p.terrain.get('import'); h = getattr(p, 'import_src', None)
            if not imp or h is None or not imp.get('flat') or imp.get('source'): continue
            h2, changed = mxb_io.unwrap_metres(h)
            imp['source'] = 'mxb'
            if not changed: continue
            p.import_src = np.ascontiguousarray(h2, np.float32)
            try:
                p.base_img = self.compose_base(p); p._cache = {}; a['pil'] = None
            except Exception:
                continue
            fixed.append(a['name'])
        return fixed

    def open_project(self):
        path = filedialog.askopenfilename(parent=self, title='Open map work', filetypes=[('Map work', '*.hmworld'), ('All', '*.*')])
        if path: self.open_workspace(path)

    def save_project(self, ask=False):
        if ask:
            path = filedialog.asksaveasfilename(parent=self, title='Save map work as', defaultextension='.hmworld',
                                                filetypes=[('Map work', '*.hmworld')])
            if not path: return
            self.save_workspace(path)
        else:
            self.save_workspace(self.ws_path)

    def update_title(self):
        pass

    # ------------------------------------------------------------------ writing the game map
    def pick_save_dir(self):
        d = filedialog.askdirectory(parent=self, title='Folder to save maps in', initialdir=self.save_dir, mustexist=True)
        if not d: return
        d = os.path.normpath(d)
        if self.in_game_folder(os.path.join(d, 'map.blend.bin')):
            messagebox.showerror('Map editor', "Pick a folder outside the game's folder.", parent=self); return
        self.save_dir = d; self.save_dir_var.set(d); set_setting(self.app, 'map_save_dir', d)

    def open_folder(self, d, select=None):
        try:
            if sys.platform.startswith('win'):
                if select: subprocess.Popen(['explorer', '/select,', os.path.normpath(select)])
                else: os.startfile(d)
            else:
                subprocess.Popen(['xdg-open' if sys.platform.startswith('linux') else 'open', d])
        except Exception as e:
            self.status.set("Couldn't open the folder: %s" % e)

    def save_map(self, ask=False):
        if self._busy:
            self.status.set('Still working on the last save…'); return
        if self.t is None:
            self.status.set('The map is still loading.'); return
        if not self.areas and not getattr(self, 'flat', {}).get('on'):
            messagebox.showinfo('Map editor', 'Nothing to save yet - edit the map first.', parent=self); return
        if ask or not os.path.isdir(self.save_dir):
            out = filedialog.asksaveasfilename(parent=self, title='Save the new map as', defaultextension='.bin',
                                               initialdir=self.save_dir if os.path.isdir(self.save_dir) else None,
                                               initialfile='map.blend.bin', filetypes=[('Game map', '*.bin')])
            if not out: return
            out = os.path.normpath(out)
        else:
            out = os.path.join(self.save_dir, 'map.blend.bin')
        if self.in_game_folder(out):
            messagebox.showerror('Map editor', "Save it outside the game's folder (Documents, Desktop…) - then use "
                                 "\"Install a saved map\", which keeps a backup of the game's map and only replaces it "
                                 "once the new one has been checked.", parent=self); return
        try: edge = max(0.0, float(self.area_edge.get()))
        except ValueError: edge = 6.0
        self.status.set('Building the areas…'); self.update_idletasks()
        lo, hi = self.world_range()
        places = []
        for a in self.areas:
            p = a['p']; H, info = p.evaluate()
            l2, h2 = min(lo, float(H.min())), max(hi, float(H.max()))
            x0, z0 = p.placement['x0'], p.placement['z0']
            pl = T.Placement(((H - l2) / (h2 - l2)).astype(np.float32), x0 + p.size / 2, z0 + p.size / 2, p.size, p.size, 0.0,
                             height_range=h2 - l2, base_y=l2, edge=edge, material=None, clear_biomes=True,
                             clip_res=min(max(p.px / 2, 0.125), 0.5), surface=info['surface'])
            pl.allow_outside = True
            pl.clear_all = bool(p.terrain.get('clear_all'))
            if (p.canopy >= 0).any(): pl.canopy = p.canopy.copy()
            if (p.rocks >= 0).any(): pl.rocks = p.rocks.copy()
            if (p.smix >= 0).any(): pl.smix = p.smix.copy()
            places.append(pl)
        self._busy = True; logs = []

        def prog(done, total): self.after(0, lambda: self.prog.configure(value=100.0 * done / max(total, 1)))

        def work():
            try:
                t = self.t
                spec = self.tf.spec
                for pl in places: T.apply_placement(t, pl, log=logs.append)
                part = out + '.part'
                self.after(0, lambda: self.status.set('Writing the map…'))
                self.tf.save(part, {t['node']: t}, progress=prog)
                chk = T.TerrainFile.open(part, spec); n = len(chk.terrains[0]['height_clips']); chk.close(); del chk
                try: os.remove(out + '.objects.png')       # left by the old track-objects feature
                except OSError: pass
                for side in ('.ground.json',):              # left by the removed ground-texture feature
                    try: os.remove(out + side)
                    except OSError: pass
                os.replace(part, out)                      # only a checked, complete file gets the real name
                self.tf.close()
                tf = self.open_terrain(spec)
                self.after(0, lambda: self.map_saved(tf, out, n, logs))
            except Exception as e:
                import traceback as _tb
                msg = str(e) or type(e).__name__
                log_error('Saving the map to %s failed:\n%s' % (out, _tb.format_exc()))
                try: os.remove(out + '.part')
                except OSError: pass
                try:
                    spec = self.tf.spec; self.tf.close(); tf = self.open_terrain(spec)
                except Exception:
                    tf = None

                def fail():
                    self._busy = False; self.set_saving(False)
                    try:
                        alive = bool(self.winfo_exists())
                    except tk.TclError:
                        alive = False
                    if not alive:
                        if tf is not None: tf.close()
                        return
                    if tf is not None: self.tf, self.t = tf, tf.terrains[0]; self.g = T.Grid(self.t)
                    self.prog.configure(value=0)
                    self.status.set("Couldn't save the map - nothing was written. %s" % msg)
                    messagebox.showerror('Map editor', "Couldn't save the map (nothing was written to\n%s):\n\n%s\n\n"
                                         "Details are in studio_errors.log." % (out, msg), parent=self)
                    if getattr(self, '_close_after_save', False): self._close_after_save = False
                self.after(0, fail)
        self._close_after_save = False; self.set_saving(True)
        self.status.set('Saving the map to %s… (takes a while - keep the editor open until it says Saved)' % out)
        threading.Thread(target=work, daemon=True).start()

    def set_saving(self, on):
        for b in getattr(self, 'save_btns', []) + ([self.tb['savemap']] if 'savemap' in getattr(self, 'tb', {}) else []):
            try: b.configure(state='disabled' if on else 'normal', text=b._label + (' (saving…)' if on else ''))
            except (tk.TclError, AttributeError): pass

    def map_saved(self, tf, out, n, logs):
        self._busy = False; self.set_saving(False)
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            tf.close(); return
        if getattr(self, '_close_after_save', False):
            self.tf = tf; self.last_saved = out
            messagebox.showinfo('Map editor', 'Map saved:\n%s' % out, parent=self)
            self.close(); return
        self.tf, self.t = tf, tf.terrains[0]; self.g = T.Grid(self.t)
        self.last_saved = out
        warns = [l[5:] for l in logs if l.startswith('WARN')]
        import time as _t
        try: mb = os.path.getsize(out) / 1e6
        except OSError: mb = 0
        self.status.set('Saved the map to %s at %s (%.0f MB, checked: %d detail clips). Use "Install a saved map" to put '
                        'it in the game.%s' % (out, _t.strftime('%H:%M:%S'), mb, n, ('  ⚠ ' + '  ⚠ '.join(warns)) if warns else ''))
        self.prog.configure(value=0); self._bg_key = None; self.render()
        if messagebox.askyesno('Map editor', 'Map saved:\n%s\n(%.0f MB, %s)\n\nInstall it in the game now? (the original '
                               'is kept as map.blend.bin.orig)' % (out, mb, _t.strftime('%H:%M:%S')), parent=self):
            self.install(out)

    def install(self, src=None):
        if not self.game_root:
            messagebox.showinfo('Map editor', "Copy your saved map over the game's content\\meshes\\map.blend.bin "
                                "(keep a copy of the original).", parent=self); return
        asked = src is not None
        if src is None:
            last = getattr(self, 'last_saved', '') or os.path.join(self.save_dir, 'map.blend.bin')
            src = filedialog.askopenfilename(parent=self, title='The map you saved', initialdir=os.path.dirname(last),
                                             initialfile=os.path.basename(last), filetypes=[('Game map', '*.bin')])
        if not src: return
        dst = self.map_path
        if not asked and not messagebox.askyesno('Map editor', "Replace the game's map with\n%s ?\n\nThe original is kept as "
                                   'map.blend.bin.orig - "Restore original" puts it back.' % src, parent=self):
            return
        bad = self.check_map_file(src)
        if bad:
            messagebox.showerror('Map editor', "Not installed - %s\n\nThe game's map was left as it is." % bad, parent=self); return
        try:
            if os.path.getsize(dst) > 0 and (not os.path.exists(dst + '.orig') or zigspec.stale_backup(dst)
                                             or os.path.getsize(dst + '.orig') == 0):
                safe_copy(dst, dst + '.orig')
            if self.tf: self.tf.close()
            self.tf = None
            tex = self.slot_path()
            if tex and os.path.exists(tex + '.orig'):      # undo the old track-objects feature: the backpack logo back
                safe_copy(tex + '.orig', tex)
            self.restore_textures()
            safe_copy(src, dst)
            self.status.set('Installed. Start the game to ride it. (Steam "verify files" also restores the original.)')
            self.load_map_keep()
        except OSError as e:
            messagebox.showerror('Map editor', "Couldn't install (the game's map was left as it was):\n%s\n\n"
                                 "Is the game still running?" % e, parent=self)
            if self.tf is None: self.load_map_keep()

    def restore_textures(self):
        """Put back game textures an earlier version of the tool replaced (Sand ground, backpack logo)."""
        if not self.game_root: return
        d = os.path.join(self.game_root, 'content', 'terrain-tiling')
        names = [os.path.join(d, 'sand_%s.generated.%s' % (k, e)) for k, e in
                 (('diffuse', 'png'), ('diffuse', 'jpg'), ('height', 'png'), ('material', 'png'), ('normal', 'png'))]
        for f in names:
            if os.path.exists(f + '.orig'): safe_copy(f + '.orig', f)

    def slot_path(self):
        """The backpack logo texture an older version replaced with the track objects' textures."""
        if not self.game_root: return None
        p = os.path.join(self.game_root, 'content', 'model-textures', 'logo-transparent.png')
        return p if os.path.exists(p) else None

    def in_game_folder(self, path):
        def norm(p): return os.path.normcase(os.path.realpath(os.path.abspath(p)))
        p = norm(path)
        roots = [norm(os.path.dirname(self.map_path))] + ([norm(self.game_root)] if self.game_root else [])
        return any(p == r or p.startswith(r.rstrip('\\/') + os.sep) for r in roots)

    def check_map_file(self, path):
        """None if path is a complete map file of this game version, else why not."""
        try:
            if os.path.getsize(path) == 0: return 'that file is empty (a save that didn\'t finish?).'
            spec = zigspec.ZigSpec.load(self.spec_path)
            if zigspec.sniff_header(path) != spec.schema_hash:
                return "that file is for a different game version than the game's current map."
            tf = T.TerrainFile.open(path, spec); ok = bool(tf.terrains); tf.close(); del tf
            return None if ok else 'that file has no terrain in it.'
        except Exception as e:
            return "that file doesn't read as a complete map (%s)." % e

    def load_map_keep(self, reseat=False):
        """Read the map again (after install / restore / flattening) - your areas stay."""
        def work():
            try:
                tf = self.open_terrain(zigspec.ZigSpec.load(self.spec_path))
                self.after(0, lambda: self.map_loaded_keep(tf, reseat))
            except Exception as e:
                msg = str(e); self.after(0, lambda: self.status.set("Couldn't read the map: %s" % msg))
        threading.Thread(target=work, daemon=True).start()

    def map_loaded_keep(self, tf, reseat=False):
        try:
            if not self.winfo_exists(): tf.close(); return
        except tk.TclError:
            tf.close(); return
        self.tf, self.t = tf, tf.terrains[0]; self.g = T.Grid(self.t); self._bg_key = None
        self.fill_nav()
        if reseat: self.reseat_all()
        self.render()

    # ------------------------------------------------------------------ the map as the editor reads it
    def source_path(self):
        """The game's own map (its .orig backup once a saved map is installed): every save starts from it, so
        earlier saves are never built on twice."""
        return zigspec.original_of(self.map_path) or self.map_path

    def open_terrain(self, spec):
        tf = T.TerrainFile.open(self.source_path(), spec)
        t = tf.terrains[0]
        self.game_tracks = len(t['height_clips'])
        self._map_version = getattr(self, '_map_version', 0) + 1
        fl = getattr(self, 'flat', None) or {}
        if fl.get('on'):
            self.flat_level = T.flatten_map(t, keep=fl.get('keep', [0, 1]), level=fl.get('level'),
                                            blend=fl.get('blend', 40.0), clear=fl.get('clear', False), log=lambda m: None)
        return tf

    def fill_nav(self):
        for w in self.nav.winfo_children(): w.destroy()
        ttk.Button(self.nav, text='Whole map', command=self.fit).pack(side='left')
        for i, c in enumerate(self.t['height_clips']):
            b = c['bounds']
            ttk.Button(self.nav, text='Track %d' % (i + 1), command=lambda b=b: self.goto(
                (b['min'][0] + b['max'][0]) / 2, (b['min'][2] + b['max'][2]) / 2, 0.6)).pack(side='left', padx=(4, 0))
        if self.ride:
            ttk.Button(self.nav, text='Last ride', command=lambda: self.goto(self.ride[0], self.ride[1], 0.6)).pack(side='left', padx=(4, 0))
        # which game tracks to keep when flattening
        for w in self.flat_keep_box.winfo_children(): w.destroy()
        ttk.Label(self.flat_keep_box, text='Keep game tracks:').pack(side='left')
        fl = self.flat
        self.flat_keep_vars = []
        for i in range(getattr(self, 'game_tracks', 0)):
            v = tk.BooleanVar(value=i in fl.get('keep', [0, 1]))
            ttk.Checkbutton(self.flat_keep_box, text=str(i + 1), variable=v).pack(side='left', padx=(4, 0))
            self.flat_keep_vars.append(v)

    def reseat_all(self):
        """Every area takes the ground again (after the map's ground changed, e.g. flattened)."""
        for a in self.areas:
            p = a['p']
            p.base_img = self.compose_base(p) if p.terrain.get('import') else self.ground(p.placement['x0'], p.placement['z0'], p.n, p.px)
            p._cache = {}; a['gmat'] = None; a['gbio'] = None; a['pil'] = None
        cur = self.area; self.area = None
        for a in self.areas:
            if a is cur: continue
            H, info_ = a['p'].evaluate()
            sf = info_['surface']; sf = np.where(sf >= 0, sf, self.area_gmat(a))
            a['pil'] = Image.fromarray(C.shade(H, a['p'].px, surface=sf, palette=ME.PAL))
        self.H = self.info = self.rgb = self.pil = None
        if self.gl is not None: self.gl.H = None
        if cur is not None: self.area = None; self.activate(cur)
        self.refresh_areas(); self.autosave_ws()

    def apply_flat(self):
        try:
            lvl = self.flat_level_var.get().strip()
            level = float(lvl) if lvl and not lvl.startswith('auto') else None
            blend = max(1.0, float(self.flat_blend_var.get()))
        except ValueError:
            self.status.set('Flatten: the level / blend must be numbers.'); return
        self.flat = {'on': bool(self.flat_on.get()), 'keep': [i for i, v in enumerate(self.flat_keep_vars) if v.get()],
                     'level': level, 'blend': blend, 'clear': bool(self.flat_clear.get())}
        self.status.set('Flattening the map…' if self.flat['on'] else 'Putting the game\'s ground back…')
        self.load_map_keep(reseat=True)
        self.after(200, self.autosave_ws)

    def restore(self):
        dst = self.map_path
        if not os.path.exists(dst + '.orig'):
            messagebox.showinfo('Map editor', "The game's map hasn't been replaced (no map.blend.bin.orig).", parent=self); return
        if zigspec.stale_backup(dst):
            messagebox.showinfo('Map editor', 'map.blend.bin.orig is from an older game build - the game has been updated '
                                'since, so its current map already is the original.', parent=self); return
        if not messagebox.askyesno('Map editor', "Put the game's original map back?", parent=self): return
        try:
            if self.tf: self.tf.close()
            self.tf = None
            safe_copy(dst + '.orig', dst)
            tex = self.slot_path()
            if tex and os.path.exists(tex + '.orig'): safe_copy(tex + '.orig', tex)
            self.restore_textures()
            self.status.set('Original map restored.'); self.load_map_keep()
        except OSError as e:
            messagebox.showerror('Map editor', "Couldn't restore:\n%s" % e, parent=self)

    def close(self):
        if self._busy:
            if not messagebox.askyesno('Map editor', 'The map is still being saved (see the bar in the Map tab).\n\n'
                                       'Close the editor as soon as it is done?\n(No = keep it open)', parent=self):
                return
            self._close_after_save = True
            self.status.set('Will close when the save is done…')
            return
        try:
            if self.areas: self.save_workspace(self.ws_path, quiet=True)
        except Exception:
            pass
        try:
            if self.tf: self.tf.close()
        except Exception:
            pass
        self.destroy()

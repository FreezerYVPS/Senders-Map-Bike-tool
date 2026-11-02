"""
hm_studio.py - Heightmap Studio: build a track heightmap inside the tool.

  Terrain   generate (seeded presets), import an image, or start from the game's own ground
  Track     pen tool: click to add points, drag to move, click the line to insert, Shift = 45 degree steps,
            right-click an end for 45/90/180 turns; width, banking, cut, smoothing, start funnel
  Jumps     template library (and your own templates), drop onto a track, drag along it (Ctrl = copy,
            Shift = length), snapping and repeating
  Ruts      pen or freehand ruts, multi-rut berms; bumps: stamp, scatter, braking bumps
  Sculpt    Raise, Lower, Smooth, Flatten, Flatten slope, Ramp, Noise, Erode, Clone, Terrace, Erase,
            Mask / Unmask - on the terrain (under the tracks) or the final heightmap
  Surface   paint the game's ground layers (tracks get theirs automatically) with a set of brush tips
  Nature    place / paint trees, bushes, grass clumps and rocks (and clear them)
  Layers    show / hide, rename, group, reorder, delete
  3D view, reference image, undo / redo, presets, 16-bit export, Send to map editor.
"""
import copy
import json
import math
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image, ImageTk

import hm_core as C

TABS = ['Terrain', 'Track', 'Jumps', 'Ruts', 'Sculpt', 'Surface', 'Nature', 'Layers']
SURF_CHOICES = [C.SURF_NAMES[s] for s in C.SURFACES]
SURF_BY_NAME = {C.SURF_NAMES[s]: s for s in C.SURFACES}


def presets_path():
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    d = os.path.join(base, 'GameModelExporter'); os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'heightmap_presets.json')


def load_presets():
    try:
        with open(presets_path()) as f: return json.load(f)
    except (OSError, ValueError):
        return {}


def save_presets(d):
    try:
        with open(presets_path(), 'w') as f: json.dump(d, f, indent=1)
    except OSError:
        pass


# ======================================================================= small form builder
class Form:
    """Rows of labelled fields bound to a dict. kinds: float, int, bool, choice (values=[...])."""

    def __init__(self, master, specs, on_change, cols=2):
        self.f = ttk.Frame(master); self.specs = specs; self.on_change = on_change
        self.vars = {}; self.d = None; self._loading = False
        for i, sp in enumerate(specs):
            key, label, kind = sp[:3]; extra = sp[3] if len(sp) > 3 else {}
            r, c = divmod(i, cols)
            if kind == 'bool':
                v = tk.BooleanVar()
                ttk.Checkbutton(self.f, text=label, variable=v, command=lambda k=key: self._changed(k)).grid(
                    row=r, column=c * 2, columnspan=2, sticky='w', padx=(0 if c == 0 else 8, 0), pady=1)
            else:
                ttk.Label(self.f, text=label).grid(row=r, column=c * 2, sticky='w', padx=(0 if c == 0 else 8, 0), pady=1)
                if kind == 'choice':
                    v = tk.StringVar()
                    w = ttk.Combobox(self.f, textvariable=v, values=extra.get('values', []), state='readonly',
                                     width=extra.get('width', 11))
                    w.bind('<<ComboboxSelected>>', lambda e, k=key: self._changed(k))
                else:
                    v = tk.StringVar()
                    w = ttk.Entry(self.f, textvariable=v, width=extra.get('width', 7), justify='right')
                    w.bind('<Return>', lambda e, k=key: self._changed(k))
                    w.bind('<FocusOut>', lambda e, k=key: self._changed(k))
                    st = extra.get('step', 1 if kind == 'int' else 0.1)
                    w.bind('<Up>', lambda e, k=key, st=st: self._nudge(k, st))
                    w.bind('<Down>', lambda e, k=key, st=st: self._nudge(k, -st))
                w.grid(row=r, column=c * 2 + 1, sticky='w', padx=2, pady=1)
            self.vars[key] = (v, kind, extra)

    def grid(self, **kw): self.f.grid(**kw); return self

    def pack(self, **kw): self.f.pack(**kw); return self

    def load(self, d):
        self.d = d; self._loading = True
        for key, (v, kind, extra) in self.vars.items():
            val = None if d is None else d.get(key)
            if kind == 'bool': v.set(bool(val))
            elif kind == 'choice':
                show = extra.get('show', lambda x: x)
                v.set('' if val is None else show(val))
            else:
                v.set('' if val is None else ('%d' % val if kind == 'int' else '%.4g' % val))
        self._loading = False

    def _value(self, key):
        v, kind, extra = self.vars[key]
        if kind == 'bool': return bool(v.get())
        if kind == 'choice':
            back = extra.get('back', lambda x: x)
            return back(v.get())
        try:
            x = float(v.get())
        except ValueError:
            return None
        if 'min' in extra: x = max(extra['min'], x)
        if 'max' in extra: x = min(extra['max'], x)
        return int(round(x)) if kind == 'int' else x

    def _nudge(self, key, st):
        v = self._value(key)
        if v is None: return
        self.vars[key][0].set('%.4g' % (v + st)); self._changed(key)
        return 'break'

    def _changed(self, key):
        if self._loading or self.d is None: return
        val = self._value(key)
        if val is None or self.d.get(key) == val: return
        self.on_change(self.d, key, val)


# ======================================================================= undo
class History:
    def __init__(self, studio, cap=40):
        self.s = studio; self.undo_ = []; self.redo_ = []; self.cap = cap

    def push_state(self):
        self.undo_.append(('state', self.s.p.state())); self.redo_.clear(); self._trim()

    def push_array(self, name, win, old):
        self.undo_.append(('arr', name, win, old)); self.redo_.clear(); self._trim()

    def push_arrays(self, items):
        """Several arrays changed by one stroke: [(name, win, old values)]."""
        self.undo_.append(('arrs', items)); self.redo_.clear(); self._trim()

    def push_full(self):
        p = self.s.p
        self.undo_.append(('full', copy.deepcopy(p.state()), {k: (None if getattr(p, k) is None else getattr(p, k).copy())
                                                             for k in C.PAINT_ARRAYS + ('base_img',)}))
        self.redo_.clear(); self._trim()

    def _trim(self):
        while len(self.undo_) > self.cap: self.undo_.pop(0)

    def _swap(self, e):
        p = self.s.p
        if e[0] == 'state':
            cur = ('state', p.state()); p.set_state(e[1]); return cur
        if e[0] == 'arr':
            a = getattr(p, e[1]); cur = ('arr', e[1], e[2], a[e[2]].copy()); a[e[2]] = e[3]; return cur
        if e[0] == 'arrs':
            back = []
            for name, win, old in e[1]:
                a = getattr(p, name); back.append((name, win, a[win].copy())); a[win] = old
            return ('arrs', back)
        cur = ('full', copy.deepcopy(p.state()), {k: (None if getattr(p, k) is None else getattr(p, k).copy())
                                                  for k in C.PAINT_ARRAYS + ('base_img',)})
        if e[1].get('n') != p.n:
            p.n = e[1]['n']
        p.px = e[1].get('px', p.px)
        p.set_state(e[1])
        for k, v in e[2].items(): setattr(p, k, v)
        p._cache.clear()
        return cur

    def undo(self):
        if not self.undo_: return False
        self.redo_.append(self._swap(self.undo_.pop())); return True

    def redo(self):
        if not self.redo_: return False
        self.undo_.append(self._swap(self.redo_.pop())); return True


# ======================================================================= brush tips panel
class BrushPanel:
    """A palette of brush tips (thumbnails, like a paint program's brush list) and the brush settings.
    Several panels can share one settings dict (the Surface and Nature tabs do)."""

    COLS = 4

    def __init__(self, master, settings, on_change=None):
        self.b = settings; self.on_change = on_change
        self.f = ttk.Frame(master)
        n = len(C.BRUSH_TIPS); rows = (n + self.COLS - 1) // self.COLS
        self.cw, self.ch = 104, 70
        self.cv = tk.Canvas(self.f, width=self.COLS * self.cw, height=rows * self.ch, background='#15171b',
                            highlightthickness=0, cursor='hand2')
        self.cv.pack(anchor='w')
        self.cv.bind('<ButtonPress-1>', self.pick)
        self.imgs = []
        for k, tip in enumerate(C.BRUSH_TIPS):
            a = C.tip_preview({'tip': tip}, 48)
            ph = ImageTk.PhotoImage(Image.fromarray(a, 'L'), master=self.cv)
            self.imgs.append(ph)
            r, c = divmod(k, self.COLS)
            x, y = c * self.cw + self.cw / 2, r * self.ch
            self.cv.create_image(x, y + 28, image=ph)
            self.cv.create_text(x, y + self.ch - 7, text=tip, fill='#cfd3d8', font=('Segoe UI', 7))
        self.help = ttk.Label(self.f, text='', foreground='#666'); self.help.pack(anchor='w')
        self.form = Form(self.f, [('radius', 'Size - radius (m)', 'float', {'step': 0.5, 'min': 0.1}),
                                  ('opacity', 'Opacity (0-1)', 'float', {'step': 0.05, 'min': 0.02, 'max': 1}),
                                  ('density', 'Density (0-1)', 'float', {'step': 0.05, 'min': 0, 'max': 1}),
                                  ('scale', 'Texture scale', 'float', {'step': 0.1, 'min': 0.1, 'max': 10}),
                                  ('hardness', 'Hardness (soft)', 'float', {'step': 0.05, 'min': 0, 'max': 0.95}),
                                  ('spacing', 'Dab spacing', 'float', {'step': 0.05, 'min': 0.05, 'max': 3}),
                                  ('follow', 'Texture follows the stroke', 'bool'),
                                  ('angle', 'Fixed angle (°)', 'float', {'step': 15})], self._changed)
        self.form.pack(fill='x', pady=(4, 0))
        self.load()

    def pack(self, **kw): self.f.pack(**kw); return self

    def _changed(self, d, k, v):
        d[k] = v
        if self.on_change: self.on_change()

    def pick(self, e):
        c = int(e.x // self.cw); r = int(e.y // self.ch)
        k = r * self.COLS + c
        if 0 <= c < self.COLS and 0 <= k < len(C.BRUSH_TIPS):
            self.b['tip'] = C.BRUSH_TIPS[k]; self.load()
            if self.on_change: self.on_change()

    def load(self):
        self.form.load(self.b)
        self.cv.delete('sel')
        if self.b.get('tip') in C.BRUSH_TIPS:
            k = C.BRUSH_TIPS.index(self.b['tip']); r, c = divmod(k, self.COLS)
            self.cv.create_rectangle(c * self.cw + 2, r * self.ch + 2, (c + 1) * self.cw - 2, (r + 1) * self.ch - 2,
                                     outline='#ffd24a', width=2, tags='sel')
        self.help.config(text='%s: %s' % (self.b.get('tip'), C.BRUSH_TIP_HELP.get(self.b.get('tip'), '')))


# ======================================================================= the studio window
class Studio(tk.Toplevel):
    def __init__(self, master, project=None, on_send=None, title='', path=None):
        super().__init__(master)
        self.p = project or C.Project()
        self.on_send = on_send; self.path = path; self.title_hint = title
        self.hist = History(self)
        self.presets = load_presets()
        for k, v in self.presets.get('templates', {}).items(): self.p.templates.setdefault(k, v)
        self.H = None; self.info = None; self.rgb = None; self.pil = None
        self.vx = self.vz = self.p.size / 2; self.mpp = 1.0
        self.sel = None                     # ('track', i) / ('point', i, j) / ('jump', k) / ('rut', k) / ('bump', k)
        self.sel_pts = set()
        self._drag = None; self._eval_job = None; self._gl_job = None; self._busy = False; self._mask_dirty = False
        self._render_job = None; self._drawn = None; self._shifted = (0.0, 0.0); self._gl_win = None
        self.view_rot = 0.0                 # the 2D view's turn (radians): up on screen = the 3D camera's facing
        self.ref_img = None
        self.geometry('1500x900'); self.minsize(1100, 700)
        self._root().report_callback_exception = self._report_error
        self.build()
        self.update_title()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(60, lambda: (self.fit(), self.recompute(now=True)))

    # ------------------------------------------------------------------ layout
    def build(self):
        top = ttk.Frame(self, padding=(6, 4)); top.pack(side='top', fill='x')
        self.tb = {}
        for key, txt, cmd in (('new', 'New…', self.new_project), ('open', 'Open…', self.open_project),
                              ('save', 'Save', self.save_project), ('saveas', 'Save as…', lambda: self.save_project(True))):
            self.tb[key] = ttk.Button(top, text=txt, command=cmd); self.tb[key].pack(side='left', padx=(0, 3))
        ttk.Separator(top, orient='vertical').pack(side='left', fill='y', padx=6)
        self.autosave = tk.BooleanVar(value=True); self._autosave_job = None
        ttk.Checkbutton(top, text='Autosave', variable=self.autosave).pack(side='left', padx=(0, 6))
        ttk.Button(top, text='↶ Undo', command=self.undo).pack(side='left')
        ttk.Button(top, text='↷ Redo', command=self.redo).pack(side='left', padx=(3, 0))
        ttk.Separator(top, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Button(top, text='Export heightmap…', command=self.export_dialog).pack(side='left')
        ttk.Button(top, text='Export surface mask…', command=self.export_surface).pack(side='left', padx=(3, 0))
        self.send_btn = ttk.Button(top, text='Send to map editor ➜', command=self.send)
        self.send_btn.pack(side='left', padx=(8, 0))
        top = ttk.Frame(self, padding=(6, 0, 6, 4)); top.pack(side='top', fill='x')
        ttk.Label(top, text='View:').pack(side='left', padx=(0, 4))
        self.viewmode = tk.StringVar(value='both')
        for t, v in (('2D', '2d'), ('3D', '3d'), ('2D + 3D', 'both')):
            ttk.Radiobutton(top, text=t, value=v, variable=self.viewmode, command=self.layout_views).pack(side='left')
        ttk.Label(top, text='  3D height ×').pack(side='left')
        self.exag = tk.DoubleVar(value=1.0)
        ttk.Spinbox(top, from_=0.5, to=5, increment=0.25, textvariable=self.exag, width=5,
                    command=self.gl_settings).pack(side='left')
        self.slope = tk.BooleanVar(value=True); self.grid3d = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='slope shading', variable=self.slope, command=self.gl_settings).pack(side='left', padx=(6, 0))
        ttk.Checkbutton(top, text='10 m grid', variable=self.grid3d, command=self.gl_settings).pack(side='left', padx=(6, 0))

        self.status = tk.StringVar(value='')
        bar = ttk.Frame(self); bar.pack(side='bottom', fill='x')
        ttk.Label(bar, textvariable=self.status, padding=(8, 2), anchor='w').pack(side='left', fill='x', expand=True)
        self.cursor_lbl = ttk.Label(bar, text='', padding=(8, 2)); self.cursor_lbl.pack(side='right')

        right = ttk.Frame(self, width=500, padding=(6, 4)); right.pack(side='right', fill='y'); right.pack_propagate(False)
        try: ttk.Style(self).configure('TNotebook.Tab', padding=(5, 2))        # all the tabs fit on one row
        except tk.TclError: pass
        self.nb = ttk.Notebook(right); self.nb.pack(fill='both', expand=True)
        self.panels = {}
        for t in TABS:
            self.panels[t] = self.make_panel(t)
        self.bind_all('<MouseWheel>', self.panel_wheel, add='+')
        self.bind_all('<Button-4>', lambda e: self.panel_wheel(e, -1), add='+')
        self.bind_all('<Button-5>', lambda e: self.panel_wheel(e, 1), add='+')
        self.nb.bind('<<NotebookTabChanged>>', lambda e: (self.render(), self.update_cursor_hint(), self.reload_brush_panels()))
        self.build_terrain(self.panels['Terrain']); self.build_track(self.panels['Track'])
        self.build_jumps(self.panels['Jumps']); self.build_ruts(self.panels['Ruts'])
        self.build_sculpt(self.panels['Sculpt']); self.build_surface(self.panels['Surface'])
        self.build_nature(self.panels['Nature']); self.build_layers(self.panels['Layers'])

        self.views = ttk.Frame(self); self.views.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(self.views, background='#20242a', highlightthickness=0, cursor='crosshair')
        c = self.canvas
        c.bind('<Configure>', lambda e: self.render())
        c.bind('<ButtonPress-1>', self.on_down); c.bind('<B1-Motion>', self.on_drag); c.bind('<ButtonRelease-1>', self.on_up)
        c.bind('<ButtonPress-3>', self.on_right); c.bind('<B3-Motion>', self.on_pan); c.bind('<ButtonRelease-3>', self.on_pan_end)
        c.bind('<ButtonPress-2>', self.on_pan_start); c.bind('<B2-Motion>', self.on_pan); c.bind('<ButtonRelease-2>', self.on_pan_end)
        c.bind('<Motion>', self.on_motion); c.bind('<Leave>', lambda e: c.delete('brush'))
        c.bind('<Control-ButtonPress-3>', self.on_turn_start); c.bind('<Control-B3-Motion>', self.on_turn)
        c.bind('<Control-ButtonRelease-3>', lambda e: setattr(self, '_turn', None))
        for k, d in (('q', -15), ('e', 15)):
            self.bind('<Key-%s>' % k, lambda ev, d=d: None if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Spinbox,
                                                                                            ttk.Combobox)) else self.turn_view(d), add='+')
        c.bind('<MouseWheel>', lambda e: self.wheel(e, e.delta > 0))
        c.bind('<Button-4>', lambda e: self.wheel(e, True)); c.bind('<Button-5>', lambda e: self.wheel(e, False))
        self.bind('<Control-z>', lambda e: self.undo()); self.bind('<Control-y>', lambda e: self.redo())
        self.bind('<Control-Z>', lambda e: self.redo()); self.bind('<Control-s>', lambda e: self.save_project())
        self.bind('<Delete>', self.on_delete); self.bind('<BackSpace>', self.on_delete)
        self.bind('<Escape>', lambda e: self.select(None))
        self.gl = None
        try:
            import hm_gl
            self.gl = hm_gl.TerrainGL(self.views, width=400, height=400)
            self.gl.owner = self
            self.gl.yaw = -math.pi / 2                     # facing north, like the 2D map (up = north)
            self.gl.bind('<Shift-Button-1>', self.gl_click)
            self.after(2500, self.check_gl)
        except Exception as e:                               # no OpenGL: 2D only
            self.gl = None; self.gl_error = str(e)
        self.layout_views()

    def gl_click(self, e):
        """Shift+click in the 3D view (Jumps tab): put the picked jump there."""
        if self.tab() == 'Jumps' and self.gl is not None:
            pt = self.gl.ground_at(e.x, e.y)
            if pt is not None: self.place_jump(*pt)
            return 'break'

    # ---- editing directly in the 3D view: the same tools as on the map, at the spot under the mouse
    EDIT3D = ('Track', 'Jumps', 'Ruts', 'Sculpt', 'Surface', 'Nature')

    def edit3d(self):
        return self.tab() in self.EDIT3D and self.H is not None

    def gl_edit(self, what, pt, e):
        """A mouse press / drag / release on the 3D terrain at area-local point pt: handled like the same action on
        the 2D map (the map turns the point into its own screen position)."""
        import types
        if pt is None:
            if what == 'up' and self._drag is not None:
                d = getattr(self, '_last3d', None)
                if d is not None: pt = d
            if pt is None: return
        self._last3d = pt
        cx, cy = self.w2c(*pt)
        ev = types.SimpleNamespace(x=cx, y=cy, state=e.state, x_root=e.x_root, y_root=e.y_root, widget=self.canvas)
        if what == 'down': self.on_down(ev)
        elif what == 'drag': self.on_drag(ev)
        else: self.on_up(ev)
        if self.gl is not None: self.gl.request()

    def gl_hover(self, pt, e):
        if pt is not None and self.H is not None:
            x, z = pt
            self.cursor_lbl.config(text='x %.1f  z %.1f m   height %.2f m   (3D)' % (
                x, z, float(self.H[min(self.p.n - 1, int(round(z / self.p.px))), min(self.p.n - 1, int(round(x / self.p.px)))])))

    def overlay3d(self, cursor):
        """What the 3D view draws on the terrain: tracks, their points, jumps, ruts and the brush ring."""
        t = self.tab()
        lines, points = [], []
        for i, tr in enumerate(self.p.tracks):
            cur = self.curve_of(i)
            active = i == self.cur_track_index()
            if cur is not None and len(cur[0]) > 1:
                Cc = cur[0][::max(1, int(len(cur[0]) / 1500))]
                lines.append((Cc, (1.0, 0.82, 0.29, 0.95) if active else (0.79, 0.65, 0.25, 0.6), 2.5 if active else 1.5))
            if t == 'Track' or (active and t == 'Jumps'):
                for j, q in enumerate(tr['points']):
                    on = self.sel is not None and self.sel[0] == 'point' and self.sel[1] == i and self.sel[2] == j
                    points.append(((float(q[0]), float(q[1])), (1.0, 0.31, 0.19, 1) if on else (1, 1, 1, 0.95), 9 if on else 7))
        for jp in getattr(self.p, 'jumps', []):
            try:
                poly = self.jump_poly(jp)
                if poly is not None and len(poly) > 2:
                    P = np.asarray(poly, np.float64).reshape(-1, 2); P = np.vstack([P, P[:1]])
                    lines.append((P, (0.35, 0.85, 1.0, 0.9), 1.5))
            except Exception:
                pass
        for r in getattr(self.p, 'ruts', []):
            pts = r.get('points') or []
            if len(pts) > 1: lines.append((np.asarray(pts, np.float64), (0.9, 0.55, 1.0, 0.8), 1.5))
        if cursor is not None and t in ('Sculpt', 'Surface', 'Nature'):
            rad = self.brush['radius'] if t == 'Sculpt' else self.paint_radius()
            a = np.linspace(0, 2 * np.pi, 64)
            lines.append((np.column_stack([cursor[0] + rad * np.cos(a), cursor[1] + rad * np.sin(a)]), (1, 1, 1, 0.95), 2))
        return {'lines': lines, 'points': points}

    def layout_views(self):
        m = self.viewmode.get()
        self.canvas.pack_forget()
        if self.gl is not None: self.gl.pack_forget()
        if m in ('2d', 'both') or self.gl is None: self.canvas.pack(side='left', fill='both', expand=True)
        if self.gl is not None and m in ('3d', 'both'):
            self.gl.pack(side='left', fill='both', expand=True)
            self.after(50, self.push_gl)

    def check_gl(self):
        """Say so when the 3D view could not start (instead of an empty panel)."""
        g = self.gl
        if g is None or g.ready or self.viewmode.get() == '2d': return
        if not g.winfo_ismapped(): self.after(2000, self.check_gl); return
        msg = '3D view could not start: %s' % (getattr(g, 'failed', None) or 'no OpenGL 3 context from the graphics driver')
        self.status.set(msg)
        try:
            tk.Label(g, text=msg + '\n\nThe 2D map still works. Details are in studio_errors.log.', bg='#20242a', fg='#ddd',
                     wraplength=380).place(relx=0.5, rely=0.5, anchor='center')
            with open(os.path.join(os.path.dirname(presets_path()), 'studio_errors.log'), 'a') as f: f.write(msg + '\n')
        except Exception:
            pass

    def gl_settings(self):
        if self.gl is None: return
        try: self.gl.exag = float(self.exag.get())
        except (tk.TclError, ValueError): pass
        self.gl.slope = self.slope.get(); self.gl.grid = self.grid3d.get(); self.gl.request()

    # ------------------------------------------------------------------ panels
    def make_panel(self, text, index='end'):
        """A notebook tab whose contents scroll when the window is too short to show them all."""
        outer = ttk.Frame(self.nb)
        cv = tk.Canvas(outer, highlightthickness=0, borderwidth=0, yscrollincrement=20)
        try: cv.configure(background=ttk.Style(self).lookup('TFrame', 'background') or cv.cget('background'))
        except tk.TclError: pass
        sb = ttk.Scrollbar(outer, orient='vertical', command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        inner = ttk.Frame(cv, padding=(6, 6))
        win = cv.create_window(0, 0, window=inner, anchor='nw')

        def sync(_e=None):
            cv.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight()))
            if inner.winfo_reqheight() > cv.winfo_height() > 1:
                if not sb.winfo_ismapped(): sb.pack(side='right', fill='y', before=cv)
            elif sb.winfo_ismapped():
                sb.pack_forget(); cv.yview_moveto(0)
        inner.bind('<Configure>', sync)
        cv.bind('<Configure>', lambda e: (cv.itemconfigure(win, width=e.width), sync()))
        cv.pack(side='left', fill='both', expand=True)
        outer._scroll_canvas = cv; outer._inner = inner
        if index == 'end': self.nb.add(outer, text=text)
        else: self.nb.insert(index, outer, text=text)
        self._scroll_panels = getattr(self, '_scroll_panels', []) + [(outer, cv, inner)]
        return inner

    def panel_wheel(self, e, step=None):
        """The mouse wheel over a tab's controls scrolls the tab (lists, combo boxes and the map keep theirs)."""
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return
        if w is None or isinstance(w, (tk.Listbox, tk.Text, ttk.Treeview, ttk.Combobox, ttk.Spinbox, tk.Spinbox)):
            return
        for outer, cv, inner in getattr(self, '_scroll_panels', []):
            x = w
            while x is not None and x is not outer:
                x = getattr(x, 'master', None)
            if x is None: continue
            if inner.winfo_reqheight() <= cv.winfo_height(): return
            if step is None: step = -1 if e.delta > 0 else 1
            cv.yview_scroll(step * 3, 'units')
            return

    def section(self, parent, text):
        ttk.Label(parent, text=text, font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(8, 2))

    def hint(self, parent, text):
        ttk.Label(parent, text=text, foreground='#666', wraplength=440, justify='left').pack(anchor='w', pady=(2, 0))

    def preset_menu(self, parent, kind, get, apply):
        mb = ttk.Menubutton(parent, text='Presets ▾'); m = tk.Menu(mb, tearoff=False); mb['menu'] = m

        def fill():
            m.delete(0, 'end')
            m.add_command(label='Save current as…', command=lambda: self._save_preset(kind, get()))
            items = self.presets.get(kind, {})
            if items: m.add_separator()
            for name in sorted(items):
                m.add_command(label=name, command=lambda n=name: apply(copy.deepcopy(self.presets[kind][n])))
            if items:
                m.add_separator()
                dm = tk.Menu(m, tearoff=False)
                for name in sorted(items): dm.add_command(label=name, command=lambda n=name: self._del_preset(kind, n))
                m.add_cascade(label='Delete', menu=dm)
        mb.bind('<ButtonPress-1>', lambda e: fill(), add='+')
        return mb

    def _save_preset(self, kind, d):
        if d is None: return
        name = simpledialog.askstring('Preset', 'Name for this preset:', parent=self)
        if not name: return
        self.presets.setdefault(kind, {})[name] = copy.deepcopy(d); save_presets(self.presets)

    def _del_preset(self, kind, name):
        self.presets.get(kind, {}).pop(name, None); save_presets(self.presets)

    # ---- terrain
    def build_terrain(self, f):
        self.section(f, 'Size')
        r = ttk.Frame(f); r.pack(anchor='w')
        self.size_lbl = ttk.Label(r, text=''); self.size_lbl.pack(side='left')
        ttk.Button(r, text='Change…', command=self.change_size).pack(side='left', padx=6)
        self.section(f, 'Generated terrain')
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Label(r, text='Preset').pack(side='left')
        self.tpreset = tk.StringVar()
        cb = ttk.Combobox(r, textvariable=self.tpreset, values=list(C.TERRAIN_PRESETS), state='readonly', width=16)
        cb.pack(side='left', padx=4); cb.bind('<<ComboboxSelected>>', lambda e: self.apply_terrain_preset())
        ttk.Button(r, text='New seed', command=self.new_seed).pack(side='left')
        self.tform = Form(f, [('seed', 'Seed', 'int'), ('amplitude', 'Height (m)', 'float', {'step': 0.5}),
                              ('size', 'Feature size (m)', 'float', {'step': 5, 'min': 2}),
                              ('roughness', 'Roughness', 'float', {'step': 0.05, 'min': 0.05, 'max': 0.95}),
                              ('octaves', 'Detail levels', 'int', {'min': 1, 'max': 9}),
                              ('tilt', 'Slope (°)', 'float', {'step': 0.5}),
                              ('tilt_dir', 'Slope dir. (°)', 'float', {'step': 15}),
                              ('ridged', 'Ridged', 'bool')], self.terrain_changed).pack(fill='x', pady=(4, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(6, 0))
        self.preset_menu(r, 'terrain', lambda: {k: v for k, v in self.p.terrain.items() if k != 'kind'},
                         self.apply_terrain_dict).pack(side='left')
        ttk.Button(r, text='Use generated terrain', command=self.use_generated).pack(side='left', padx=4)
        self.section(f, 'Or start from an image')
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Button(r, text='Import heightmap…', command=self.import_base).pack(side='left')
        self.terrain_kind = ttk.Label(f, text='', foreground='#555', wraplength=360); self.terrain_kind.pack(anchor='w', pady=(4, 0))
        self.section(f, 'Reference image (only shown, never exported)')
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Button(r, text='Load…', command=self.load_reference).pack(side='left')
        self.ref_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text='show', variable=self.ref_on, command=self.ref_changed).pack(side='left', padx=6)
        ttk.Label(r, text='opacity').pack(side='left')
        self.ref_op = tk.DoubleVar(value=0.5)
        ttk.Scale(r, from_=0.05, to=1, variable=self.ref_op, command=lambda v: self.ref_changed()).pack(side='left', fill='x', expand=True)
        self.hint(f, 'Right- or middle-drag moves the view, the wheel zooms - in every tab.')

    def terrain_changed(self, d, key, val):
        self.hist.push_state(); d[key] = val; self.p.terrain['kind'] = 'generated'; self.recompute()

    def apply_terrain_dict(self, d):
        self.hist.push_state(); self.p.terrain.update(d); self.p.terrain['kind'] = 'generated'
        self.tform.load(self.p.terrain); self.recompute()

    def apply_terrain_preset(self):
        name = self.tpreset.get()
        d = dict(C.TERRAIN_PRESETS[name]); d.setdefault('tilt', 0.0); d['preset'] = name
        self.apply_terrain_dict(d)

    def new_seed(self):
        self.hist.push_state(); self.p.terrain['seed'] = int(np.random.randint(1, 99999)); self.p.terrain['kind'] = 'generated'
        self.tform.load(self.p.terrain); self.recompute()

    def use_generated(self):
        self.hist.push_state(); self.p.terrain['kind'] = 'generated'; self.recompute()

    def import_base(self):
        path = filedialog.askopenfilename(parent=self, title='Heightmap image (16-bit PNG is best)',
                                          filetypes=[('Images', '*.png *.tif *.tiff *.jpg *.bmp *.pgm'), ('All', '*.*')])
        if not path: return
        try:
            a = C.load_image_heights(path)
        except Exception as e:
            messagebox.showerror('Heightmap studio', "Couldn't read it:\n%s" % e, parent=self); return
        rng = simpledialog.askfloat('Heights', 'How many metres does black → white span?', initialvalue=20.0,
                                    minvalue=0.01, parent=self)
        if rng is None: return
        self.hist.push_full()
        self.p.base_img = (C.resample(a, self.p.n) * rng).astype(np.float32)
        self.p.terrain['kind'] = 'image'; self.p.terrain['image'] = os.path.basename(path)
        self.p._cache.clear(); self.recompute()

    def load_reference(self):
        path = filedialog.askopenfilename(parent=self, title='Reference image (map, sketch, satellite photo)',
                                          filetypes=[('Images', '*.png *.jpg *.jpeg *.bmp *.tif'), ('All', '*.*')])
        if not path: return
        self.p.reference.update(path=path, visible=True); self.ref_on.set(True); self.ref_img = None; self.render()

    def ref_changed(self):
        self.p.reference['visible'] = self.ref_on.get(); self.p.reference['opacity'] = float(self.ref_op.get())
        self.render()

    def change_size(self):
        d = tk.Toplevel(self); d.title('Size'); d.transient(self); d.grab_set()
        n = tk.StringVar(value=str(self.p.n)); px = tk.StringVar(value='%g' % self.p.px)
        ttk.Label(d, text='Pixels per side').grid(row=0, column=0, sticky='w', padx=8, pady=4)
        ttk.Combobox(d, textvariable=n, values=['513', '1025', '2049'], width=8).grid(row=0, column=1, padx=8)
        ttk.Label(d, text='Metres per pixel').grid(row=1, column=0, sticky='w', padx=8, pady=4)
        ttk.Combobox(d, textvariable=px, values=['0.125', '0.25', '0.5', '1'], width=8).grid(row=1, column=1, padx=8)
        lbl = ttk.Label(d, text='', foreground='#555'); lbl.grid(row=2, column=0, columnspan=2, padx=8)

        def upd(*a):
            try: lbl.config(text='= %.0f x %.0f m' % (((int(n.get()) - 1) * float(px.get()),) * 2))
            except ValueError: pass
        n.trace_add('write', upd); px.trace_add('write', upd); upd()

        def ok():
            try: nn, pp = int(n.get()), float(px.get())
            except ValueError: return
            d.destroy()
            if nn * nn > 4300000 and not messagebox.askyesno('Size', 'That is a big map - editing gets slower. Continue?', parent=self): return
            self.hist.push_full()
            q = self.p.resized(nn, pp); self.p.__dict__.update(q.__dict__); self.p._cache.clear()
            self.fit(); self.recompute(now=True)
        ttk.Button(d, text='OK', command=ok).grid(row=3, column=0, columnspan=2, pady=8)

    # ---- track
    def build_track(self, f):
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Label(r, text='Track').pack(side='left')
        self.track_pick = tk.StringVar()
        self.track_cb = ttk.Combobox(r, textvariable=self.track_pick, state='readonly', width=14)
        self.track_cb.pack(side='left', padx=4); self.track_cb.bind('<<ComboboxSelected>>', lambda e: self.pick_track())
        ttk.Button(r, text='New track', command=self.new_track).pack(side='left')
        ttk.Button(r, text='Delete', command=self.delete_track).pack(side='left', padx=(3, 0))
        self.hint(f, 'Click to add points (the line follows them), drag a point to move it, click on the line to '
                     'insert one. Shift: 45° steps. Shift+click a 2nd point to select a stretch (for banking). '
                     'Right-click an end point for turns. Delete removes the selected point.')
        self.section(f, 'Selected point(s)')
        self.pform = Form(f, [('width', 'Width (m)', 'float', {'step': 0.5, 'min': 1}),
                              ('bank', 'Banking (°)', 'float', {'step': 1, 'min': -60, 'max': 60})],
                          self.point_changed).pack(fill='x')
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        ttk.Label(r, text='Lane gap for turns (m)').pack(side='left')
        self.lane_gap = tk.StringVar(value='4'); ttk.Entry(r, textvariable=self.lane_gap, width=6, justify='right').pack(side='left', padx=4)
        self.section(f, 'Track settings')
        self.tsform = Form(f, [('cut', 'Cut depth (m)', 'float', {'step': 0.05}),
                               ('smooth', 'Smooth height (m)', 'float', {'step': 1, 'min': 0}),
                               ('follow', 'Follow ground (0-1)', 'float', {'step': 0.1, 'min': 0, 'max': 1}),
                               ('falloff', 'Blend edge (m)', 'float', {'step': 0.5, 'min': 0.1}),
                               ('concavity', 'Edges raised (m)', 'float', {'step': 0.1}),
                               ('surface', 'Surface', 'choice', {'values': SURF_CHOICES + ['(none)'], 'width': 12,
                                                                 'show': lambda s: C.SURF_NAMES.get(s, '(none)'),
                                                                 'back': lambda v: SURF_BY_NAME.get(v, '')}),
                               ('closed', 'Closed loop', 'bool'), ('funnel_len', 'Start funnel (m)', 'float', {'step': 1, 'min': 0}),
                               ('funnel_width', 'Funnel width (m)', 'float', {'step': 1, 'min': 1})],
                           self.track_setting_changed).pack(fill='x')
        r = ttk.Frame(f); r.pack(fill='x', pady=(6, 0))
        self.preset_menu(r, 'track', lambda: (None if self.cur_track() is None else
                                              {k: self.cur_track()[k] for k in C.TRACK_SETTINGS}),
                         self.apply_track_preset).pack(side='left')
        self.hint(f, 'The start funnel widens the first metres of the track into a start straight.')

    def cur_track_index(self):
        if not self.p.tracks: return None
        if self.sel and self.sel[0] in ('track', 'point') and self.sel[1] < len(self.p.tracks): return self.sel[1]
        i = self.track_cb.current()
        return i if 0 <= i < len(self.p.tracks) else len(self.p.tracks) - 1

    def cur_track(self):
        i = self.cur_track_index()
        return None if i is None else self.p.tracks[i]

    def refresh_track_list(self):
        names = [t['name'] for t in self.p.tracks]
        self.track_cb['values'] = names
        i = self.cur_track_index()
        if i is not None: self.track_cb.current(i)
        else: self.track_pick.set('')
        tr = self.cur_track(); self.tsform.load(tr)
        pt = None
        if self.sel and self.sel[0] == 'point' and tr is not None and self.sel[2] < len(tr['points']):
            q = tr['points'][self.sel[2]]; pt = {'width': q[2], 'bank': q[3]}
        self.pform.load(pt)

    def pick_track(self):
        i = self.track_cb.current(); self.sel = ('track', i); self.sel_pts = set(); self.refresh_track_list(); self.render()

    def new_track(self, points=None):
        self.hist.push_state()
        d = dict(name='Track %d' % (len(self.p.tracks) + 1), points=points or [], visible=True, **copy.deepcopy(C.TRACK_SETTINGS))
        self.p.tracks.append(d); self.sel = ('track', len(self.p.tracks) - 1); self.sel_pts = set()
        self.refresh_track_list(); self.refresh_layers(); self.render()
        self.status.set('New track: click on the map to lay out its points.')

    def delete_track(self):
        i = self.cur_track_index()
        if i is None: return
        self.hist.push_state(); self.p.tracks.pop(i)
        self.p.jumps = [j for j in self.p.jumps if j['track'] != i]
        for j in self.p.jumps:
            if j['track'] > i: j['track'] -= 1
        self.p.bumps = [b for b in self.p.bumps if not (b['kind'] == 'braking' and b['track'] == i)]
        for b in self.p.bumps:
            if b['kind'] == 'braking' and b['track'] > i: b['track'] -= 1
        self.sel = None; self.p._cache.clear(); self.refresh_all(); self.recompute()

    def point_changed(self, d, key, val):
        tr = self.cur_track()
        if tr is None or not self.sel or self.sel[0] != 'point': return
        self.hist.push_state()
        idx = {'width': 2, 'bank': 3}[key]
        for j in (self.sel_pts or {self.sel[2]}) | {self.sel[2]}:
            if j < len(tr['points']): tr['points'][j][idx] = val
        d[key] = val; self.recompute()

    def track_setting_changed(self, d, key, val):
        self.hist.push_state(); d[key] = val; self.recompute()

    def apply_track_preset(self, d):
        tr = self.cur_track()
        if tr is None: return
        self.hist.push_state(); tr.update(d); self.tsform.load(tr); self.recompute()

    # ---- jumps
    def build_jumps(self, f):
        self.section(f, 'Library (drag one onto the map, or pick it and click where it goes)')
        r = ttk.Frame(f); r.pack(fill='x')
        self.tpl_list = tk.Listbox(r, height=8, exportselection=False); self.tpl_list.pack(side='left', fill='x', expand=True)
        self.tpl_list.bind('<<ListboxSelect>>', lambda e: self.draw_tpl_preview())
        # drag and drop: remember the template pressed on, and keep the list from changing its selection while
        # the mouse is outside it (Tk's listbox would otherwise select whatever item it drags past)
        self.tpl_list.bind('<ButtonPress-1>', self.tpl_press, add='+')
        self.tpl_list.bind('<B1-Motion>', self.tpl_motion)
        self.tpl_list.bind('<ButtonRelease-1>', self.tpl_drop)
        self._tpl_drag = None
        self.tpl_prev = tk.Canvas(r, width=150, height=110, background='#2a2e35', highlightthickness=0)
        self.tpl_prev.pack(side='left', padx=(6, 0))
        r = ttk.Frame(f); r.pack(fill='x', pady=(3, 0))
        ttk.Button(r, text='New template…', command=lambda: self.edit_template(None)).pack(side='left')
        ttk.Button(r, text='Edit…', command=lambda: self.edit_template(self.tpl_name())).pack(side='left', padx=3)
        ttk.Button(r, text='Delete', command=self.delete_template).pack(side='left')
        self.section(f, 'Selected jump')
        self.jform = Form(f, [('template', 'Shape', 'choice', {'values': [], 'width': 12}),
                              ('s', 'Position (m)', 'float', {'step': 0.5, 'min': 0}),
                              ('length', 'Length (m)', 'float', {'step': 0.5, 'min': 1}),
                              ('height', 'Height (m)', 'float', {'step': 0.1}),
                              ('width', 'Width (0 = track)', 'float', {'step': 0.5, 'min': 0}),
                              ('offset', 'Sideways (m)', 'float', {'step': 0.25}),
                              ('angle', 'Angle (°)', 'float', {'step': 5}),
                              ('edge', 'Side blend (m)', 'float', {'step': 0.25, 'min': 0}),
                              ('heading', 'Direction (°)', 'float', {'step': 15})], self.jump_changed).pack(fill='x')
        self.section(f, "This jump's shape (drag the points - it changes as you drag)")
        self.jshape = tk.Canvas(f, width=440, height=130, background='#2a2e35', highlightthickness=0)
        self.jshape.pack(anchor='w')
        self.jshape.bind('<ButtonPress-1>', self.jshape_down); self.jshape.bind('<B1-Motion>', self.jshape_drag)
        self.jshape.bind('<ButtonRelease-1>', self.jshape_up)
        self.jshape.bind('<Double-1>', self.jshape_add); self.jshape.bind('<Button-3>', self.jshape_remove)
        self._jsk = None
        r = ttk.Frame(f); r.pack(fill='x', pady=(3, 0))
        ttk.Button(r, text="Back to the template's shape", command=self.jshape_reset).pack(side='left')
        ttk.Button(r, text='Save as template (shape + sizes)…', command=self.jshape_save).pack(side='left', padx=4)
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Snap every').pack(side='left')
        self.snap = tk.StringVar(value='0'); ttk.Entry(r, textvariable=self.snap, width=5, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='m').pack(side='left')
        self.preset_menu(r, 'jump', lambda: (None if self.cur_jump() is None else
                                             {k: v for k, v in self.cur_jump().items() if k in C.JUMP_DEFAULTS}),
                         self.apply_jump_preset).pack(side='right')
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Repeat').pack(side='left')
        self.rep_n = tk.StringVar(value='3'); ttk.Entry(r, textvariable=self.rep_n, width=4, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='times every').pack(side='left')
        self.rep_d = tk.StringVar(value='30'); ttk.Entry(r, textvariable=self.rep_d, width=5, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='m').pack(side='left')
        ttk.Button(r, text='Repeat', command=self.repeat_jump).pack(side='left', padx=4)
        self.hint(f, 'Drop jumps on one of your tracks (they follow it) or anywhere else - e.g. on the game\'s own '
                     'track, where they line up with it. On the map: drag a jump to move it, Ctrl+drag copies it, '
                     'Shift+drag changes its length; a free jump\'s round handle turns it (Direction).')
        self.refresh_templates()

    def tpl_names(self):
        return list(C.BUILTIN_TEMPLATES) + [k for k in self.p.templates if k not in C.BUILTIN_TEMPLATES]

    def refresh_templates(self):
        names = self.tpl_names()
        cur = self.tpl_name()
        self.tpl_list.delete(0, 'end')
        for n in names: self.tpl_list.insert('end', n + ('  (yours)' if n in self.p.templates else ''))
        k = names.index(cur) if cur in names else 0
        self.tpl_list.selection_clear(0, 'end'); self.tpl_list.selection_set(k)
        self.jform.vars['template'][0].set(self.jform.vars['template'][0].get())
        for w in self.jform.f.grid_slaves():
            if isinstance(w, ttk.Combobox): w['values'] = names
        self.draw_tpl_preview()

    def tpl_name(self):
        s = self.tpl_list.curselection() if hasattr(self, 'tpl_list') else ()
        names = self.tpl_names()
        return names[s[0]] if s and s[0] < len(names) else names[0]

    def draw_tpl_preview(self, cv=None, tpl=None):
        cv = cv or self.tpl_prev
        cv.delete('all')
        W, H = int(cv['width']), int(cv['height'])
        prof = self.p.template(tpl or self.tpl_name())
        u = np.linspace(0, 1, 120); h = C.template_profile(prof, u)
        lo = min(0.0, float(h.min())); hi = max(1.0, float(h.max()))
        pts = []
        for a, b in zip(u, h): pts += [8 + a * (W - 16), H - 10 - (b - lo) / (hi - lo) * (H - 24)]
        cv.create_line(8, H - 10 - (0 - lo) / (hi - lo) * (H - 24), W - 8, H - 10 - (0 - lo) / (hi - lo) * (H - 24), fill='#556')
        cv.create_line(*pts, fill='#e8c060', width=2, smooth=True)
        cv.create_text(W / 2, 8, text='direction of travel →', fill='#889', font=('Segoe UI', 7))

    def tpl_press(self, e):
        k = self.tpl_list.nearest(e.y)
        names = self.tpl_names()
        self._tpl_drag = names[k] if 0 <= k < len(names) else None

    def tpl_motion(self, e):
        if self._tpl_drag is None: return
        inside = 0 <= e.x < self.tpl_list.winfo_width() and 0 <= e.y < self.tpl_list.winfo_height()
        if inside: return                                   # normal listbox behaviour inside the list
        self.tpl_list.config(cursor='hand2')
        self.status.set('Drop "%s" onto a track (in the 2D map or the 3D view).' % self._tpl_drag)
        return 'break'

    def tpl_drop(self, e):
        """Released the mouse after dragging a template out of the list: drop it where the pointer is."""
        tpl, self._tpl_drag = self._tpl_drag, None
        self.tpl_list.config(cursor='')
        if tpl is None: return
        names = self.tpl_names()
        if tpl in names:                                   # show the dragged one as the picked template
            self.tpl_list.selection_clear(0, 'end'); self.tpl_list.selection_set(names.index(tpl)); self.draw_tpl_preview()
        w = self.winfo_containing(e.x_root, e.y_root)
        if w is self.canvas:
            x = e.x_root - self.canvas.winfo_rootx(); y = e.y_root - self.canvas.winfo_rooty()
            self.place_jump(*self.c2w(x, y), tpl=tpl)
        elif self.gl is not None and w is self.gl and getattr(self.gl, 'ready', False):
            pt = self.gl.ground_at(e.x_root - self.gl.winfo_rootx(), e.y_root - self.gl.winfo_rooty())
            if pt is not None: self.place_jump(*pt, tpl=tpl)
            else: self.status.set('Drop it onto the ground.')

    def cur_jump(self):
        if self.sel and self.sel[0] == 'jump' and self.sel[1] < len(self.p.jumps): return self.p.jumps[self.sel[1]]
        return None

    def jump_changed(self, d, key, val):
        self.hist.push_state(); d[key] = val
        if key == 'template':
            d.pop('profile', None)
            self.apply_template_specs(d, only_saved=True); self.jform.load(d)
        self.draw_jump_shape(); self.recompute()

    def apply_template_specs(self, j, only_saved=False):
        """Sizes for a jump of template j['template']: the sizes saved with your template, or sensible ones."""
        if not only_saved: self._jump_sizes(j)
        sp = self.p.template_specs(j['template'])
        if sp:
            for k, v in sp.items():
                if k == 'width' and not v and j.get('track', 0) < 0: continue     # free jumps need a width
                if k in C.JUMP_SPEC_KEYS: j[k] = float(v)

    # ---- the selected jump's own shape, edited live
    def _js_xy(self, u, h):
        W, H = int(self.jshape['width']), int(self.jshape['height'])
        return 14 + u * (W - 28), H - 22 - h * (H - 44)

    def _js_uh(self, x, y):
        W, H = int(self.jshape['width']), int(self.jshape['height'])
        return min(1.0, max(0.0, (x - 14) / (W - 28))), max(-1.0, min(1.4, (H - 22 - y) / (H - 44)))

    def _js_points(self, j):
        return C.profile_points(self.p.jump_shape(j))

    def draw_jump_shape(self):
        cv = getattr(self, 'jshape', None)
        if cv is None: return
        cv.delete('all')
        j = self.cur_jump()
        W = int(cv['width'])
        if j is None:
            cv.create_text(W / 2, 65, text='Pick a jump on the map to shape it here', fill='#889'); return
        pts = self._js_points(j)
        cv.create_line(*self._js_xy(0, 0), *self._js_xy(1, 0), fill='#556')
        cv.create_line(*self._js_xy(0, 1), *self._js_xy(1, 1), fill='#3a4048', dash=(3, 3))
        u = np.linspace(0, 1, 220); h = C.template_profile(pts, u)
        cv.create_line(*[v for a, b in zip(u, h) for v in self._js_xy(a, b)], fill='#e8c060', width=2)
        for a, b in pts:
            x, y = self._js_xy(a, b); cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill='#fff', outline='#222')
        cv.create_text(8, 8, anchor='nw', fill='#bbc', font=('Segoe UI', 8),
                       text='%s%s   %.1f m long, %.2f m high   - travel →' % (j['template'], '  (edited)' if j.get('profile') else '',
                                                                          j['length'], j['height']))
        cv.create_text(W - 8, int(cv['height']) - 6, anchor='se', fill='#889', font=('Segoe UI', 7),
                       text='double-click adds a point, right-click removes one')

    def _js_near(self, j, e):
        for k, (a, b) in enumerate(self._js_points(j)):
            x, y = self._js_xy(a, b)
            if abs(x - e.x) < 8 and abs(y - e.y) < 8: return k
        return None

    def jshape_down(self, e):
        j = self.cur_jump()
        if j is None: return
        k = self._js_near(j, e)
        self._jsk = None if k is None else {'k': k, 'pushed': False}

    def jshape_drag(self, e):
        j = self.cur_jump(); d = self._jsk
        if j is None or d is None: return
        if not d['pushed']: self.hist.push_state(); d['pushed'] = True
        pts = self._js_points(j)
        u, h = self._js_uh(e.x, e.y)
        k = d['k']
        if k == 0: u = 0.0
        elif k == len(pts) - 1: u = 1.0
        else: u = min(max(u, pts[k - 1][0] + 0.002), pts[k + 1][0] - 0.002)
        pts[k] = [u, h]
        j['profile'] = pts
        self.draw_jump_shape(); self._live_eval()

    def jshape_up(self, e):
        if self._jsk and self._jsk['pushed']: self.recompute()
        self._jsk = None

    def jshape_add(self, e):
        j = self.cur_jump()
        if j is None: return
        self.hist.push_state()
        pts = self._js_points(j); pts.append(list(self._js_uh(e.x, e.y))); pts.sort(key=lambda q: q[0])
        j['profile'] = pts; self.draw_jump_shape(); self.recompute()

    def jshape_remove(self, e):
        j = self.cur_jump()
        if j is None: return
        k = self._js_near(j, e); pts = self._js_points(j)
        if k is None or len(pts) <= 2: return
        self.hist.push_state(); pts.pop(k); j['profile'] = pts; self.draw_jump_shape(); self.recompute()

    def jshape_reset(self):
        j = self.cur_jump()
        if j is None or 'profile' not in j: return
        self.hist.push_state(); j.pop('profile', None); self.draw_jump_shape(); self.recompute()

    def jshape_save(self):
        j = self.cur_jump()
        if j is None:
            self.status.set('Pick a jump first - its shape and sizes become the template.'); return
        base = j['template'] if j['template'] not in C.BUILTIN_TEMPLATES else j['template'] + ' (mine)'
        n = simpledialog.askstring('Save template', 'Name for the new template (it keeps this shape and the length, '
                                   'height, width, side blend, angle and sideways of this jump):', initialvalue=base, parent=self)
        if not n: return
        n = n.strip()
        if n in C.BUILTIN_TEMPLATES: n += ' (mine)'
        tpl = {'points': [tuple(q) for q in self._js_points(j)],
               'specs': {k: float(j.get(k, C.JUMP_DEFAULTS.get(k, 0)) or 0) for k in C.JUMP_SPEC_KEYS}}
        self.hist.push_state()
        self.p.templates[n] = tpl
        self.presets.setdefault('templates', {})[n] = tpl; save_presets(self.presets)
        j['template'] = n; j.pop('profile', None)
        self.refresh_templates()
        names = self.tpl_names(); self.tpl_list.selection_clear(0, 'end'); self.tpl_list.selection_set(names.index(n))
        self.draw_tpl_preview(); self.jform.load(j); self.draw_jump_shape(); self.recompute()
        self.status.set('Saved the template "%s" with its sizes - new jumps made from it start with them.' % n)

    def apply_jump_preset(self, d):
        j = self.cur_jump()
        if j is None: return
        self.hist.push_state(); j.update(d); self.jform.load(j); self.recompute()

    def nearest_track(self, x, z, max_d=None):
        """(track index, distance along, sideways distance) of the nearest track centre line."""
        best = None
        for i, tr in enumerate(self.p.tracks):
            cur = self.curve_of(i)
            if cur is None: continue
            Cc, T, s = cur[:3]
            d = np.hypot(Cc[:, 0] - x, Cc[:, 1] - z); k = int(np.argmin(d))
            if best is None or d[k] < best[2]: best = (i, float(s[k]), float(d[k]))
        if best is None or (max_d is not None and best[2] > max_d): return None
        return best

    def snap_s(self, s):
        try: st = float(self.snap.get())
        except ValueError: st = 0
        return round(s / st) * st if st > 0 else s

    def ground_track_direction(self, x, z):
        """(heading degrees, width m) of a track painted in the ground at x, z, or None. The studio alone has
        no ground tracks; the map editor looks at the game's surface there."""
        return None

    def place_jump(self, x, z, tpl=None):
        nt = self.nearest_track(x, z, max_d=max(12.0, 15 * self.mpp))
        self.hist.push_state()
        if nt is None:                                   # anywhere: e.g. on the game's own track
            gd = self.ground_track_direction(x, z)
            j = dict(copy.deepcopy(C.JUMP_DEFAULTS), track=-1, s=0.0, x=float(x), z=float(z), visible=True,
                     name='Jump %d' % (len(self.p.jumps) + 1))
            j['template'] = tpl or self.tpl_name()
            if gd is not None: j['heading'], j['width'] = gd
            else: j['width'] = 8.0
            self.apply_template_specs(j)
            self.p.jumps.append(j); self.select(('jump', len(self.p.jumps) - 1)); self.refresh_layers(); self.recompute()
            self.status.set('Added a %s %s. Drag it to move it, drag its round handle to turn it, Shift+drag sets its '
                            'length, Ctrl+drag copies it.' % (j['template'], 'lined up with the track there' if gd else
                                                              '(no track there - turn it with its round handle)'))
            return
        j = dict(copy.deepcopy(C.JUMP_DEFAULTS), track=nt[0], s=self.snap_s(nt[1]), visible=True,
                 name='Jump %d' % (len(self.p.jumps) + 1))
        j['template'] = tpl or self.tpl_name()
        self.apply_template_specs(j)
        self.p.jumps.append(j); self.select(('jump', len(self.p.jumps) - 1)); self.refresh_layers(); self.recompute()
        self.status.set('Added a %s to %s at %.0f m - drag it along the track, Ctrl+drag copies, Shift+drag sets its length.'
                        % (j['template'], self.p.tracks[nt[0]]['name'], j['s']))

    @staticmethod
    def _jump_sizes(j):
        t = j['template']
        if t in ('Whoops',): j.update(length=24, height=0.6)
        if t in ('Rhythm (3)',): j.update(length=20, height=1.2)
        if t in ('Roller',): j.update(length=10, height=1.0)
        if t in ('Double', 'Triple'): j.update(length=30 if t == 'Double' else 40, height=2.5)

    def repeat_jump(self):
        j = self.cur_jump()
        if j is None: return
        try: n, d = int(self.rep_n.get()), float(self.rep_d.get())
        except ValueError: return
        self.hist.push_state()
        for k in range(1, n + 1):
            q = copy.deepcopy(j); q['s'] = j['s'] + k * d; q['name'] = 'Jump %d' % (len(self.p.jumps) + 1)
            if j.get('track', 0) < 0:
                a = math.radians(j.get('heading', 0)); q['x'] = j['x'] + k * d * math.cos(a); q['z'] = j['z'] + k * d * math.sin(a)
            self.p.jumps.append(q)
        self.refresh_layers(); self.recompute()

    def edit_template(self, name):
        TemplateEditor(self, name)

    def delete_template(self):
        n = self.tpl_name()
        if n not in self.p.templates:
            messagebox.showinfo('Templates', 'The built-in templates stay - you can delete your own.', parent=self); return
        self.p.templates.pop(n, None); self.presets.setdefault('templates', {}).pop(n, None); save_presets(self.presets)
        self.refresh_templates()

    # ---- ruts and bumps
    def build_ruts(self, f):
        self.section(f, 'Tool')
        self.rtool = tk.StringVar(value='rut_pen')
        g = ttk.Frame(f); g.pack(fill='x')
        for i, (t, v) in enumerate((('Rut (pen)', 'rut_pen'), ('Rut (freehand)', 'rut_free'), ('Bump', 'stamp'),
                                    ('Scatter bumps', 'scatter'), ('Braking bumps', 'braking'), ('Select', 'select'))):
            ttk.Radiobutton(g, text=t, value=v, variable=self.rtool, command=self.update_cursor_hint).grid(
                row=i // 3, column=i % 3, sticky='w', padx=(0, 8))
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Button(r, text='New rut', command=lambda: (self.select(None), self.status.set('Click to start a new rut.'))).pack(side='left')
        self.hint(f, 'Pen: click points along the corner (Esc finishes, drag points to adjust). Freehand: drag. '
                     'Braking bumps go on the track before the point you click.')
        self.section(f, 'Rut / berm')
        self.rform = Form(f, [('count', 'Ruts side by side', 'int', {'min': 1, 'max': 6}),
                              ('spacing', 'Spacing (m)', 'float', {'step': 0.1, 'min': 0.2}),
                              ('depth', 'Depth (m)', 'float', {'step': 0.05}),
                              ('width', 'Width (m)', 'float', {'step': 0.05, 'min': 0.1}),
                              ('sharpness', 'Sharpness', 'float', {'step': 0.25, 'min': 0.3}),
                              ('berm', 'Berm height (m)', 'float', {'step': 0.1}),
                              ('berm_width', 'Berm width (m)', 'float', {'step': 0.1, 'min': 0.1}),
                              ('berm_side', 'Berm side', 'choice', {'values': ['auto', 'left', 'right', 'both'], 'width': 7})],
                          self.rut_changed).pack(fill='x')
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        self.preset_menu(r, 'rut', lambda: self._rut_params(), self.apply_rut_preset).pack(side='left')
        self.section(f, 'Bumps')
        self.bform = Form(f, [('height', 'Height (m)', 'float', {'step': 0.05}),
                              ('radius', 'Radius (m)', 'float', {'step': 0.1, 'min': 0.1}),
                              ('area', 'Scatter radius (m)', 'float', {'step': 1, 'min': 0.5}),
                              ('count', 'Scatter count', 'int', {'min': 1}),
                              ('size_min', 'Size from (m)', 'float', {'step': 0.1, 'min': 0.1}),
                              ('size_max', 'Size to (m)', 'float', {'step': 0.1, 'min': 0.1}),
                              ('length', 'Braking length (m)', 'float', {'step': 1, 'min': 1}),
                              ('spacing', 'Row spacing (m)', 'float', {'step': 0.1, 'min': 0.3}),
                              ('seed', 'Seed', 'int')], self.bump_changed).pack(fill='x')
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        self.preset_menu(r, 'bump', lambda: self._bump_params(), self.apply_bump_preset).pack(side='left')
        self.new_rut = copy.deepcopy(C.RUT_DEFAULTS); self.new_bump = copy.deepcopy(C.BUMP_DEFAULTS)
        self.rform.load(self.new_rut); self.bform.load(self.new_bump)

    def cur_rut(self):
        if self.sel and self.sel[0] == 'rut' and self.sel[1] < len(self.p.ruts): return self.p.ruts[self.sel[1]]
        return None

    def cur_bump(self):
        if self.sel and self.sel[0] == 'bump' and self.sel[1] < len(self.p.bumps): return self.p.bumps[self.sel[1]]
        return None

    def _rut_params(self):
        d = self.cur_rut() or self.new_rut
        return {k: d[k] for k in C.RUT_DEFAULTS}

    def _bump_params(self):
        d = self.cur_bump() or self.new_bump
        return {k: d[k] for k in C.BUMP_DEFAULTS if k not in ('kind', 'track', 's')}

    def rut_changed(self, d, key, val):
        if d is self.new_rut: d[key] = val; return
        self.hist.push_state(); d[key] = val; self.recompute()

    def bump_changed(self, d, key, val):
        if d is self.new_bump: d[key] = val; return
        self.hist.push_state(); d[key] = val; self.recompute()

    def apply_rut_preset(self, d):
        tgt = self.cur_rut() or self.new_rut
        if tgt is not self.new_rut: self.hist.push_state()
        tgt.update(d); self.rform.load(tgt); self.recompute()

    def apply_bump_preset(self, d):
        tgt = self.cur_bump() or self.new_bump
        if tgt is not self.new_bump: self.hist.push_state()
        tgt.update(d); self.bform.load(tgt); self.recompute()

    # ---- sculpt
    def build_sculpt(self, f):
        self.section(f, 'Tool')
        self.stool = tk.StringVar(value='Raise')
        g = ttk.Frame(f); g.pack(fill='x')
        for i, t in enumerate(C.SCULPT_TOOLS + ['Mask', 'Unmask'] + self.extra_sculpt_tools()):
            ttk.Radiobutton(g, text=t, value=t, variable=self.stool, command=self.update_cursor_hint).grid(
                row=i // 3, column=i % 3, sticky='w', padx=(0, 8))
        self.smode = tk.StringVar(value='final')
        r = ttk.Frame(f); r.pack(fill='x', pady=(6, 0))
        ttk.Radiobutton(r, text='Final heightmap', value='final', variable=self.smode).pack(side='left')
        ttk.Radiobutton(r, text='Terrain only (under the tracks)', value='terrain', variable=self.smode).pack(side='left', padx=(8, 0))
        self.brush = {'radius': 6.0, 'strength': 0.5, 'hardness': 0.3, 'step': 1.0, 'seed': 7, 'ramp_width': 8.0}
        self.section(f, 'Brush')
        Form(f, [('radius', 'Radius (m)', 'float', {'step': 0.5, 'min': 0.25}),
                 ('strength', 'Strength', 'float', {'step': 0.05, 'min': 0.01, 'max': 2}),
                 ('hardness', 'Hardness', 'float', {'step': 0.05, 'min': 0, 'max': 0.95}),
                 ('step', 'Terrace step (m)', 'float', {'step': 0.1, 'min': 0.05}),
                 ('seed', 'Noise seed', 'int'), ('ramp_width', 'Ramp width (m)', 'float', {'step': 0.5, 'min': 0.5})],
             lambda d, k, v: d.__setitem__(k, v)).pack(fill='x').load(self.brush)
        self.hint(f, 'Drag to sculpt. "[" and "]" change the radius. Clone: Alt+click the spot to copy from, then '
                     'paint. Ramp: drag from the bottom to the top of the ramp. Flatten keeps the height where you start. '
                     'Mask hides the tracks, jumps, ruts and bumps where painted (Unmask brings them back). '
                     'Terrain-only sculpting goes under the tracks, so they follow it.')
        self.bind('<bracketleft>', lambda e: self._brush_size(1 / 1.15)); self.bind('<bracketright>', lambda e: self._brush_size(1.15))
        self.show_mask = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='Show the mask (red)', variable=self.show_mask, command=lambda: self.reshade()).pack(anchor='w', pady=(6, 0))

    def extra_sculpt_tools(self):
        return []

    def after_sculpt(self, name):
        pass

    def _brush_size(self, k):
        if self.tab() in ('Surface', 'Nature'):
            self.pbrush['radius'] = max(0.1, self.pbrush['radius'] * k); self.reload_brush_panels()
        else:
            self.brush['radius'] = max(0.25, self.brush['radius'] * k)
        self.render()

    def reload_brush_panels(self):
        for pnl in (getattr(self, 'surf_panel', None), getattr(self, 'nat_panel', None)):
            if pnl is not None: pnl.load()

    def paint_radius(self):
        """Radius of the cursor circle / dab in the Surface and Nature tabs."""
        if self.tab() == 'Nature' and self.nat_mode.get() == 'place':
            try: return max(0.25, float(self.nat_spot.get()))
            except ValueError: return 3.0
        return float(self.pbrush['radius'])

    # ---- surface
    def build_surface(self, f):
        self.pbrush = dict(C.BRUSH_DEFAULTS)
        self.section(f, "The game's ground layers")
        self.paint_layer = tk.StringVar(value=SURF_CHOICES[1])
        g = ttk.Frame(f); g.pack(fill='x')
        for i, name in enumerate(SURF_CHOICES + ['Automatic (erase painting)', "Keep the game's ground"]):
            ttk.Radiobutton(g, text=name, value=name, variable=self.paint_layer).grid(row=i // 2, column=i % 2, sticky='w', padx=(0, 10))
        r = ttk.Frame(f); r.pack(fill='x', pady=(6, 0))
        ttk.Label(r, text='Mix in').pack(side='left')
        self.mix_layer = tk.StringVar(value='(nothing)')
        ttk.Combobox(r, textvariable=self.mix_layer, values=['(nothing)'] + SURF_CHOICES, state='readonly',
                     width=18).pack(side='left', padx=4)
        ttk.Label(r, text='at').pack(side='left')
        self.mix_amt = tk.IntVar(value=20)
        ttk.Spinbox(r, from_=1, to=100, increment=5, textvariable=self.mix_amt, width=4).pack(side='left', padx=3)
        ttk.Label(r, text='%').pack(side='left')
        self.hint(f, 'Mix in: every spot you paint gets the layer above blended with this one (the game blends two '
                     'surfaces per 0.5 m cell) - a tint just there, e.g. Light dirt with 20% Grass on one track, '
                     'Track dirt 1 with 30% Track dirt 2 on another.')
        self.section(f, 'Brush')
        self.surf_panel = BrushPanel(f, self.pbrush, self.render).pack(fill='x')
        self.hint(f, 'Pick a tip, then drag on the map. Lower opacity / density paints sparse cover: e.g. Grass with '
                     'the Hard round, then Light dirt with Splotches or Speckle at 0.3-0.5 for grass with dirt '
                     'patches. Mixed cells are written as the game\'s own blend of two layers. "[" and "]" change '
                     'the size. Tracks get their own surface automatically (Track tab).')

    # ---- nature: trees, bushes, grass clumps, rocks
    def build_nature(self, f):
        self.section(f, 'What to put there')
        self.nat_item = tk.StringVar(value=C.NATURE_NAMES[0])
        g = ttk.Frame(f); g.pack(fill='x')
        for i, name in enumerate(C.NATURE_NAMES):
            ttk.Radiobutton(g, text=name, value=name, variable=self.nat_item, command=self.nature_item_changed).grid(
                row=i // 2, column=i % 2, sticky='w', padx=(0, 10))
        r = ttk.Frame(f); r.pack(fill='x', pady=(4, 0))
        ttk.Label(r, text='Size level (0-255)').pack(side='left')
        self.nat_level = tk.StringVar(value='255')
        ttk.Entry(r, textvariable=self.nat_level, width=5, justify='right').pack(side='left', padx=3)
        ttk.Label(r, text='  Spot size (m)').pack(side='left')
        self.nat_spot = tk.StringVar(value='3')
        ttk.Entry(r, textvariable=self.nat_spot, width=5, justify='right').pack(side='left', padx=3)
        self.nat_ground = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='Also set the ground they need (forest floor / grass)',
                        variable=self.nat_ground).pack(anchor='w', pady=(2, 0))
        self.section(f, 'How')
        self.nat_mode = tk.StringVar(value='place')
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Radiobutton(r, text='Place one at a time (click)', value='place', variable=self.nat_mode,
                        command=self.render).pack(side='left')
        ttk.Radiobutton(r, text='Paint with the brush (drag)', value='paint', variable=self.nat_mode,
                        command=self.render).pack(side='left', padx=(10, 0))
        self.show_nature = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text='Show trees and rocks on the map', variable=self.show_nature,
                        command=lambda: (setattr(self, '_mask_dirty', True), self.reshade())).pack(anchor='w')
        self.section(f, 'Brush (for painting)')
        self.nat_panel = BrushPanel(f, self.pbrush, self.render).pack(fill='x')
        self.hint(f, 'The game grows its own trees, bushes, grass clumps and rocks where the map lets them: a spot '
                     'gets one or a few (which kind and exactly where is the game\'s choice). Size level: how tall '
                     'the woods may grow (255 = like the game\'s deep forest; 30-100 like its forest edges) or how '
                     'big the boulders are. "Bushes only" keeps your ground and gives each bush just the speck of '
                     'forest floor the game needs to grow it (brush Density = how many); its low level leaves no room '
                     'for trees. Ctrl+click / Ctrl+drag puts a spot back to automatic. Clear stops trees '
                     'and boulders; meadow grass grows on any Grass ground - paint another ground to remove it.')
        self.nature_item_changed()

    def nature_item_changed(self):
        it = C.nature_item(self.nat_item.get())
        lvl = it[2] if it[2] is not None else it[3]
        self.nat_level.set('' if lvl is None or lvl < 0 else str(lvl))
        self.nat_spot.set('%g' % it[4])

    # ---- layers
    def build_layers(self, f):
        self.ltree = ttk.Treeview(f, columns=('vis', 'group'), show='tree headings', height=18, selectmode='browse')
        self.ltree.heading('#0', text='Layer'); self.ltree.heading('vis', text='Shown'); self.ltree.heading('group', text='Group')
        self.ltree.column('#0', width=170); self.ltree.column('vis', width=50, anchor='center'); self.ltree.column('group', width=90)
        self.ltree.pack(fill='both', expand=True)
        self.ltree.bind('<<TreeviewSelect>>', self.layer_select)
        self.ltree.bind('<Double-1>', lambda e: self.layer_rename())
        g = ttk.Frame(f); g.pack(fill='x', pady=(4, 0))
        for i, (t, cmd) in enumerate((('Show / hide', self.layer_toggle), ('Rename…', self.layer_rename),
                                      ('Group…', self.layer_group), ('Up', lambda: self.layer_move(-1)),
                                      ('Down', lambda: self.layer_move(1)), ('Delete', self.layer_delete))):
            ttk.Button(g, text=t, command=cmd).grid(row=i // 3, column=i % 3, sticky='we', padx=1, pady=1)
        self.hint(f, 'Every track, jump, rut and bump is its own layer: change or remove it any time. '
                     'Hiding a group hides everything in it.')

    def layer_items(self):
        out = []
        for k, (lst, name) in enumerate(((self.p.tracks, 'track'), (self.p.jumps, 'jump'), (self.p.ruts, 'rut'),
                                          (self.p.bumps, 'bump'))):
            for i, d in enumerate(lst): out.append((name, i, d))
        return out

    def refresh_layers(self):
        t = self.ltree
        sel = t.selection()
        t.delete(*t.get_children())
        groups = {}
        for kind, i, d in self.layer_items():
            g = d.get('group', '')
            parent = ''
            if g:
                if g not in groups:
                    groups[g] = t.insert('', 'end', iid='g:' + g, text='▸ ' + g, values=('', ''), open=True)
                parent = groups[g]
            nm = d.get('name') or '%s %d' % (kind, i + 1)
            if kind == 'jump': nm += '  (%s)' % d.get('template')
            t.insert(parent, 'end', iid='%s:%d' % (kind, i), text=nm, values=('✓' if d.get('visible', True) else '–', g))
        if sel and t.exists(sel[0]): t.selection_set(sel[0])

    def _layer_target(self):
        s = self.ltree.selection()
        if not s: return None, None
        k, _, i = s[0].partition(':')
        if k == 'g': return 'group', i
        lst = {'track': self.p.tracks, 'jump': self.p.jumps, 'rut': self.p.ruts, 'bump': self.p.bumps}[k]
        return k, int(i)

    def _layer_dict(self, k, i):
        return {'track': self.p.tracks, 'jump': self.p.jumps, 'rut': self.p.ruts, 'bump': self.p.bumps}[k][i]

    def layer_select(self, e=None):
        k, i = self._layer_target()
        if k in ('track', 'jump', 'rut', 'bump'):
            self.sel = (k, i); self.sel_pts = set(); self.load_selection_forms(); self.render()

    def layer_toggle(self):
        k, i = self._layer_target()
        if k is None: return
        self.hist.push_state()
        if k == 'group':
            items = [d for _, _, d in self.layer_items() if d.get('group') == i]
            v = not all(d.get('visible', True) for d in items)
            for d in items: d['visible'] = v
        else:
            d = self._layer_dict(k, i); d['visible'] = not d.get('visible', True)
        self.refresh_layers(); self.recompute()

    def layer_rename(self):
        k, i = self._layer_target()
        if k in (None, 'group'): return
        d = self._layer_dict(k, i)
        n = simpledialog.askstring('Rename', 'Name:', initialvalue=d.get('name', ''), parent=self)
        if n:
            self.hist.push_state(); d['name'] = n; self.refresh_layers(); self.refresh_track_list()

    def layer_group(self):
        k, i = self._layer_target()
        if k in (None, 'group'): return
        d = self._layer_dict(k, i)
        n = simpledialog.askstring('Group', 'Group name (empty = no group):', initialvalue=d.get('group', ''), parent=self)
        if n is None: return
        self.hist.push_state(); d['group'] = n.strip(); self.refresh_layers()

    def layer_move(self, dk):
        k, i = self._layer_target()
        if k in (None, 'group'): return
        lst = {'track': self.p.tracks, 'jump': self.p.jumps, 'rut': self.p.ruts, 'bump': self.p.bumps}[k]
        j = i + dk
        if not 0 <= j < len(lst): return
        self.hist.push_state()
        lst[i], lst[j] = lst[j], lst[i]
        if k == 'track':                                # keep jumps / braking bumps on their tracks
            for q in self.p.jumps + [b for b in self.p.bumps if b['kind'] == 'braking']:
                q['track'] = j if q['track'] == i else (i if q['track'] == j else q['track'])
        self.p._cache.clear(); self.refresh_layers(); self.ltree.selection_set('%s:%d' % (k, j)); self.recompute()

    def layer_delete(self):
        k, i = self._layer_target()
        if k in (None, 'group'): return
        if k == 'track':
            self.sel = ('track', i); self.delete_track(); return
        self.hist.push_state()
        {'jump': self.p.jumps, 'rut': self.p.ruts, 'bump': self.p.bumps}[k].pop(i)
        self.sel = None; self.p._cache.clear(); self.refresh_layers(); self.recompute()

    def refresh_all(self):
        self.tform.load(self.p.terrain); self.refresh_track_list(); self.refresh_layers(); self.refresh_templates()
        self.load_selection_forms()
        self.size_lbl.config(text='%d x %d pixels, %.3g m each = %.0f x %.0f m' % (self.p.n, self.p.n, self.p.px, self.p.size, self.p.size))
        kind = self.p.terrain.get('kind')
        self.terrain_kind.config(text={'generated': 'Using: the generated terrain.',
                                       'image': 'Using: the imported image %s.' % self.p.terrain.get('image', ''),
                                       'ground': "Using: the game's own ground from the map editor."}.get(kind, ''))
        self.send_btn.config(state='normal' if self.on_send else 'disabled')

    def load_selection_forms(self):
        self.jform.load(self.cur_jump()); self.draw_jump_shape()
        r = self.cur_rut(); self.rform.load(r or self.new_rut)
        b = self.cur_bump(); self.bform.load(b or self.new_bump)
        self.refresh_track_list()

    def select(self, s):
        self.sel = s; self.sel_pts = set() if not s or s[0] != 'point' else self.sel_pts
        self.load_selection_forms(); self.render()

    # ------------------------------------------------------------------ evaluation and drawing
    def recompute(self, now=False):
        if self._eval_job: self.after_cancel(self._eval_job); self._eval_job = None
        if now: self._evaluate()
        else: self._eval_job = self.after(40, self._evaluate)

    def _evaluate(self):
        self._eval_job = None
        old = self.H; olds = None if self.info is None else self.info['surface']
        try:
            self.H, self.info = self.p.evaluate()
        except Exception as e:
            self.status.set('Could not build the heightmap: %s' % e); raise
        win = None
        if old is not None and old.shape == self.H.shape and self.rgb is not None and not self._mask_dirty:
            ch = (old != self.H) | (olds != self.info['surface'])
            rows = np.nonzero(ch.any(1))[0]
            if len(rows) == 0:
                self.render(); self.refresh_all(); return
            cols = np.nonzero(ch.any(0))[0]
            if (rows[-1] - rows[0]) * (cols[-1] - cols[0]) < 0.5 * self.H.size:
                win = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
        self._mask_dirty = False
        if self.path and self.autosave.get() and self.hist.undo_:
            if self._autosave_job: self.after_cancel(self._autosave_job)
            self._autosave_job = self.after(2500, self._autosave)
        self.reshade(win)
        if win is not None: self.push_gl()
        self.refresh_all()

    def reshade(self, win=None):
        if self.H is None: return
        n = self.p.n
        if win is None or getattr(self, '_hrange', None) is None:
            full = (slice(0, n), slice(0, n))
            self._hrange = tuple(np.percentile(self.H, [1, 99])) if self.H.size else (0, 1)
            self.rgb = C.shade(self.H, self.p.px, surface=self.shade_surface(full), palette=self.palette(),
                               hrange=self._hrange)
            self.draw_nature(self.rgb, full)
            if self.show_mask_on(): self._tint_mask(self.rgb, self.p.mask)
            self.pil = Image.fromarray(self.rgb)
            win = None
        else:
            r0, r1 = max(0, win[0].start - 1), min(n, win[0].stop + 1)          # slopes change 1 pixel around
            c0, c1 = max(0, win[1].start - 1), min(n, win[1].stop + 1)
            q0, q1, k0, k1 = max(0, r0 - 1), min(n, r1 + 1), max(0, c0 - 1), min(n, c1 + 1)   # + their neighbours
            sub = C.shade(self.H[q0:q1, k0:k1], self.p.px, surface=self.shade_surface((slice(q0, q1), slice(k0, k1))),
                          palette=self.palette(), hrange=self._hrange)[r0 - q0:r1 - q0, c0 - k0:c1 - k0]
            sub = np.ascontiguousarray(sub)
            self.draw_nature(sub, (slice(r0, r1), slice(c0, c1)))
            if self.show_mask_on(): self._tint_mask(sub, self.p.mask[r0:r1, c0:c1])
            self.rgb[r0:r1, c0:c1] = sub
            self.pil.paste(Image.fromarray(sub), (c0, r0))
            if self.patch_render((slice(r0, r1), slice(c0, c1))):
                self.push_gl(True, (slice(r0, r1), slice(c0, c1))); return
            win = (slice(r0, r1), slice(c0, c1))
        self.render(); self.push_gl(win is not None, win)

    def shade_surface(self, win):
        return self.info['surface'][win]

    def palette(self):
        return None

    def nature_maps(self, win):
        """(tree height, boulders) 0..255 that will be in effect inside a window."""
        c = self.p.canopy[win]; r = self.p.rocks[win]
        return np.where(c >= 0, c, 0), np.where(r >= 0, r, 0)

    def draw_nature(self, rgb, win):
        sm = self.p.smix[win]
        if (sm >= 0).any():
            pal = self.palette()
            C.mix_rgb(rgb, sm, (lambda k: C.SURF_RGB[C.SURFACES[k]]) if pal is None else
                      (lambda k: tuple(pal[k])))
        if not (getattr(self, 'show_nature', None) is not None and self.show_nature.get()): return
        can, rk = self.nature_maps(win)
        surf = self.shade_surface(win)
        if not ((can > 0).any() or (rk > 0).any() or (surf == C.SURFACES.index('rock')).any()): return
        iz, ix = np.mgrid[win[0], win[1]]
        C.nature_tint(rgb, ix * self.p.px, iz * self.p.px, can, rk, surf, self.p.px)

    def show_mask_on(self):
        return getattr(self, 'show_mask', None) is not None and self.show_mask.get()

    @staticmethod
    def _tint_mask(rgb, m):
        k = (np.clip(m, 0, 1) * 0.45)[..., None]
        if k.max() > 0: rgb[:] = (rgb * (1 - k) + np.array([220, 40, 40]) * k).astype(np.uint8)

    def push_gl(self, throttle=False, win=None):
        """Send the heights / colours to the 3D view - only the changed window when there is one."""
        if self.gl is None or self.H is None or self.viewmode.get() == '2d': return
        if win is not None:
            g = self._gl_win
            self._gl_win = win if g is None else (slice(min(g[0].start, win[0].start), max(g[0].stop, win[0].stop)),
                                                  slice(min(g[1].start, win[1].start), max(g[1].stop, win[1].stop)))
        if throttle:
            if self._gl_job: return
            self._gl_job = self.after(60, lambda: (setattr(self, '_gl_job', None), self.push_gl(False)))
            return
        w, self._gl_win = self._gl_win, None
        if w is not None and hasattr(self.gl, 'update_window') and self.gl.update_window(self.H, self.rgb, self.p.size, w):
            return
        self.gl.set_data(self.H, self.rgb, self.p.size); self.gl_settings()

    def fit(self):
        W = max(200, self.canvas.winfo_width()); Hh = max(200, self.canvas.winfo_height())
        self.vx = self.vz = self.p.size / 2; self.mpp = self.p.size / min(W, Hh) * 1.04; self.render()

    # ---- view space (vx / vz) <-> screen, with the view turned by view_rot
    def vs2c(self, x, z):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        c, s_ = math.cos(self.view_rot), math.sin(self.view_rot)
        dx, dz = x - self.vx, z - self.vz
        return W / 2 + (dx * c - dz * s_) / self.mpp, H / 2 - (dx * s_ + dz * c) / self.mpp

    def c2vs(self, cx, cy):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        c, s_ = math.cos(self.view_rot), math.sin(self.view_rot)
        a, b = (cx - W / 2) * self.mpp, -(cy - H / 2) * self.mpp
        return self.vx + a * c + b * s_, self.vz - a * s_ + b * c

    def vs_origin(self):
        """Where area-local (0, 0) is in view space (the map editor puts areas on the world)."""
        return 0.0, 0.0

    def w2c(self, x, z):
        ox, oz = self.vs_origin(); return self.vs2c(x + ox, z + oz)

    def c2w(self, cx, cy):
        ox, oz = self.vs_origin(); x, z = self.c2vs(cx, cy); return x - ox, z - oz

    def affine(self, box, sx, sy, ox=0.0, oz=0.0):
        """PIL AFFINE data: a pixel of the screen box -> ((x - ox) * sx, (z - oz) * sy) of view-space point x, z."""
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        c, s_ = math.cos(self.view_rot), math.sin(self.view_rot); m = self.mpp
        bx0, by0 = box[0] + 0.5, box[1] + 0.5
        X0 = self.vx + (bx0 - W / 2) * m * c - (by0 - H / 2) * m * s_
        Z0 = self.vz - (bx0 - W / 2) * m * s_ - (by0 - H / 2) * m * c
        return ((m * c) * sx, (-m * s_) * sx, (X0 - ox) * sx, (-m * s_) * sy, (-m * c) * sy, (Z0 - oz) * sy)

    def poly_of(self, x0, z0, x1, z1):
        """Screen corners of a view-space rectangle (for outlines that turn with the view)."""
        return [v for p in ((x0, z0), (x1, z0), (x1, z1), (x0, z1)) for v in self.vs2c(*p)]

    # ---- turning the 2D view; the 3D view turns with it (and the other way round)
    def set_rot(self, rot, from3d=False):
        self.view_rot = (rot + math.pi) % (2 * math.pi) - math.pi
        if not from3d and self.gl is not None:
            self.gl.yaw = math.atan2(-math.cos(self.view_rot), -math.sin(self.view_rot)); self.gl.request()
        self.request_render()

    def gl_turned(self, yaw):
        self.set_rot(math.atan2(-math.cos(yaw), -math.sin(yaw)), from3d=True)

    def turn_view(self, deg):
        self.set_rot(self.view_rot + math.radians(deg))

    def north_up(self):
        self.set_rot(0.0)

    def on_turn_start(self, e):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        self._turn = (math.atan2(e.y - H / 2, e.x - W / 2), self.view_rot)

    def on_turn(self, e):
        t = getattr(self, '_turn', None)
        if not t: return
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        a = math.atan2(e.y - H / 2, e.x - W / 2)
        self.set_rot(t[1] - (a - t[0]))

    def curve_of(self, i):
        tr = self.p.tracks[i]
        key = json.dumps([tr['points'], tr.get('closed'), tr.get('funnel_len'), tr.get('funnel_width')])
        ck = ('disp', i)
        if self.p._cache.get(ck, (None,))[0] != key:
            self.p._cache[ck] = (key, self.p.track_curve(tr, spacing=max(0.5, self.p.px)))
        return self.p._cache[ck][1]

    def compose(self, bx0, by0, bx1, by1):
        """The picture for the screen box (bx0, by0)-(bx1, by1) at the current view."""
        px = self.p.px
        size = (bx1 - bx0, by1 - by0)
        ox, oz = self.vs_origin()
        img = self.pil.transform(size, Image.AFFINE, self.affine((bx0, by0), 1 / px, 1 / px, ox, oz),
                                 resample=Image.BILINEAR if self.mpp > px else Image.NEAREST, fillcolor=(32, 36, 42))
        ref = self.p.reference
        if ref.get('visible') and ref.get('path'):
            try:
                if self.ref_img is None or self.ref_img[0] != ref['path']:
                    self.ref_img = (ref['path'], Image.open(ref['path']).convert('RGB'))
                ri = self.ref_img[1]
                sx, sy = ri.width / self.p.size, ri.height / self.p.size
                r = ri.transform(size, Image.AFFINE, self.affine((bx0, by0), sx, sy, ox, oz), resample=Image.BILINEAR,
                                 fillcolor=(32, 36, 42))
                img = Image.blend(img, r, float(ref.get('opacity', 0.5)))
            except Exception:
                pass
        return img

    def render(self):
        c = self.canvas
        if self.pil is None or not c.winfo_ismapped(): return
        W, H = c.winfo_width(), c.winfo_height()
        if W < 4 or H < 4: return
        self._cancel_render()
        img = self.compose(0, 0, W, H)
        self._shown = img
        self._tk = ImageTk.PhotoImage(img)
        c.delete('all')
        c.create_image(0, 0, image=self._tk, anchor='nw')
        ox, oz = self.vs_origin()
        c.create_polygon(*self.poly_of(ox, oz, ox + self.p.size, oz + self.p.size), outline='#9aa', fill='')
        self.draw_overlays(c, W, H)
        self.draw_compass(c, W, H)
        self._rendered()

    def draw_compass(self, c, W, H):
        """North arrow (click: north up) - shows how the view is turned."""
        x, y, r = W - 30, 30, 16
        s_, co = math.sin(self.view_rot), math.cos(self.view_rot)
        dx, dy = -s_ * r, -co * r                         # +z (north) on screen
        c.create_oval(x - r - 4, y - r - 4, x + r + 4, y + r + 4, fill='#2a2e35', outline='#888', tags=('compass',))
        c.create_line(x - dx * 0.7, y - dy * 0.7, x + dx, y + dy, fill='#ff5a5a', width=3, arrow='last', tags=('compass',))
        c.create_text(x + dx * 1.45, y + dy * 1.45, text='N', fill='#ffd0d0', font=('Segoe UI', 8, 'bold'), tags=('compass',))
        c.tag_bind('compass', '<Button-1>', lambda e: (self.north_up(), 'break')[1])

    # ---- smooth views: draw only what changed, slide the picture while panning, merge quick redraws
    def _rendered(self):
        self._drawn = (self.vx, self.vz, self.mpp, self.canvas.winfo_width(), self.canvas.winfo_height(), self.view_rot)
        self._shifted = (0.0, 0.0)

    def _cancel_render(self):
        if self._render_job:
            try: self.after_cancel(self._render_job)
            except Exception: pass
            self._render_job = None

    def request_render(self, delay=1):
        """Redraw soon - many requests in a row (mouse moves, wheel turns) make one redraw."""
        if self._render_job: return
        self._render_job = self.after(max(1, int(delay)), self._do_render)

    def _do_render(self):
        self._render_job = None
        self.render()

    def quick_shift(self):
        """Pan: slide what is on screen to the new view at once; the full redraw follows a moment later."""
        d = self._drawn
        if d is None or abs(d[2] - self.mpp) > 1e-12 or abs(d[5] - self.view_rot) > 1e-12:
            self.request_render(); return
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        sx, sy = self.vs2c(d[0], d[1]); dx, dy = sx - W / 2, sy - H / 2
        self.canvas.move('all', dx - self._shifted[0], dy - self._shifted[1])
        self._shifted = (dx, dy)
        self.request_render(70)

    def win_rect(self, win):
        """A window of the heightmap (rows = z, cols = x) as an area-local rectangle (x0, z0, x1, z1)."""
        px = self.p.px
        return win[1].start * px, win[0].start * px, win[1].stop * px, win[0].stop * px

    def screen_box(self, x0, z0, x1, z1, pad=2):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        pts = [self.w2c(x, z) for x, z in ((x0, z0), (x1, z0), (x1, z1), (x0, z1))]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bx0, bx1 = max(0, int(math.floor(min(xs))) - pad), min(W, int(math.ceil(max(xs))) + pad)
        by0, by1 = max(0, int(math.floor(min(ys))) - pad), min(H, int(math.ceil(max(ys))) + pad)
        return (bx0, by0, bx1, by1) if bx1 > bx0 and by1 > by0 else None

    def patch_render(self, win):
        """Redraw just the part of the view a brush dab changed. False = a full redraw is needed."""
        c = self.canvas
        d = self._drawn
        if (getattr(self, '_tk', None) is None or getattr(self, '_shown', None) is None or d is None or self._render_job
                or d != (self.vx, self.vz, self.mpp, c.winfo_width(), c.winfo_height(), self.view_rot)
                or self._shifted != (0.0, 0.0)):
            return False
        box = self.screen_box(*self.win_rect(win))
        if box is None: return True
        sub = self.compose(*box)
        self._shown.paste(sub, box[:2])
        patch = ImageTk.PhotoImage(sub)
        c.tk.call(str(self._tk), 'copy', str(patch), '-to', box[0], box[1])
        self._patch_keep = patch
        return True

    def draw_overlays(self, c, W, H):
        tab = self.tab()
        # tracks
        for i, tr in enumerate(self.p.tracks):
            cur = self.curve_of(i)
            active = i == self.cur_track_index()
            if cur is not None:
                Cc = cur[0][::max(1, int(len(cur[0]) / 1500))]
                pts = [v for x, z in Cc for v in self.w2c(x, z)]
                if len(pts) >= 4:
                    c.create_line(*pts, fill='#ffd24a' if active else '#c9a640', width=2 if active else 1,
                                  dash=() if tr.get('visible', True) else (4, 3))
            if tab == 'Track' or (active and tab in ('Jumps',)):
                for j, q in enumerate(tr['points']):
                    sx, sy = self.w2c(q[0], q[1])
                    on = self.sel and self.sel[0] == 'point' and self.sel[1] == i and self.sel[2] == j
                    inr = active and j in self.sel_pts
                    r = 5 if (j == 0 or j == len(tr['points']) - 1) else 4
                    c.create_rectangle(sx - r, sy - r, sx + r, sy + r, fill='#ff5030' if on else ('#ffb050' if inr else '#ffffff'),
                                       outline='#202020')
                    if j == 0: c.create_text(sx + 9, sy - 9, text='start', fill='#fff', font=('Segoe UI', 8), anchor='w')
        # jumps
        for k, jp in enumerate(self.p.jumps):
            poly = self.jump_poly(jp)
            if poly is None: continue
            on = self.sel == ('jump', k)
            c.create_polygon(*[v for x, z in poly for v in self.w2c(x, z)], outline='#ff5030' if on else '#7fe0ff',
                             fill='', width=2 if on else 1, dash=() if jp.get('visible', True) else (3, 3))
            if jp.get('track', 0) < 0 and tab == 'Jumps':
                hx, hz = self.jump_handle(jp); sx, sy = self.w2c(hx, hz)
                c.create_oval(sx - 6, sy - 6, sx + 6, sy + 6, fill='#7fe0ff' if not on else '#ff5030', outline='#103040')
                c.create_line(*self.w2c(jp['x'], jp['z']), sx, sy, fill='#7fe0ff', arrow='last')
            if self.mpp < 0.6 or on:
                mx, mz = np.mean(poly, 0); sx, sy = self.w2c(mx, mz)
                c.create_text(sx, sy, text=jp['template'] + (' (edited)' if jp.get('profile') else ''), fill='#bff', font=('Segoe UI', 8))
        # ruts
        for k, rt in enumerate(self.p.ruts):
            pts = [v for x, z in (q[:2] for q in rt['points']) for v in self.w2c(x, z)]
            on = self.sel == ('rut', k)
            if len(pts) >= 4: c.create_line(*pts, fill='#ff7ad9' if on else '#d070c0', width=2 if on else 1, smooth=True)
            if tab == 'Ruts':
                for q in rt['points']:
                    sx, sy = self.w2c(q[0], q[1])
                    c.create_oval(sx - 3, sy - 3, sx + 3, sy + 3, fill='#ff7ad9' if on else '#fff', outline='#202020')
        for k, b in enumerate(self.p.bumps):
            on = self.sel == ('bump', k)
            if b['kind'] == 'braking':
                cur = self.curve_of(b['track']) if b['track'] < len(self.p.tracks) else None
                if cur is None: continue
                Cc, T, s = cur[:3]
                a = [np.interp(v, s, Cc[:, 0]) for v in (b['s'], b['s'] + b['length'])]
                z_ = [np.interp(v, s, Cc[:, 1]) for v in (b['s'], b['s'] + b['length'])]
                p0 = self.w2c(a[0], z_[0]); p1 = self.w2c(a[1], z_[1])
                c.create_line(*p0, *p1, fill='#ff5030' if on else '#a0ff80', width=5, stipple='gray50')
            else:
                r = (b['area'] if b['kind'] == 'scatter' else b['radius']) / self.mpp
                sx, sy = self.w2c(b['x'], b['z'])
                c.create_oval(sx - r, sy - r, sx + r, sy + r, outline='#ff5030' if on else '#a0ff80',
                              dash=(3, 2) if b['kind'] == 'scatter' else ())
        # scale bar
        L = 10 ** math.floor(math.log10(max(self.mpp * 120, 1e-3)))
        for m_ in (5, 2, 1):
            if m_ * L / self.mpp < 160: L *= m_; break
        c.create_line(14, H - 18, 14 + L / self.mpp, H - 18, fill='#fff', width=2)
        c.create_text(14 + L / self.mpp / 2, H - 28, text='%g m' % L, fill='#fff')
        if self._drag and self._drag.get('kind') == 'ramp' and self._drag.get('b'):
            a = self.w2c(*self._drag['a']); b_ = self.w2c(*self._drag['b'])
            c.create_line(*a, *b_, fill='#fff', width=2, arrow='last')

    def jump_poly(self, jp):
        i = jp['track']
        if i < 0:
            a = math.radians(jp.get('heading', 0)); L = jp['length'] / 2; W = (jp.get('width') or 8.0) / 2
            ca, sa = math.cos(a), math.sin(a); o = jp.get('offset', 0)
            pts = [(-L, -W + o), (L, -W + o), (L, W + o), (-L, W + o)]
            return np.array([(jp['x'] + u * ca - v * sa, jp['z'] + u * sa + v * ca) for u, v in pts])
        if i >= len(self.p.tracks): return None
        cur = self.curve_of(i)
        if cur is None: return None
        Cc, T, s, w = cur[:4]
        L = jp['length']; ss = np.linspace(jp['s'] - L / 2, jp['s'] + L / 2, 9)
        ss = ss[(ss >= 0) & (ss <= s[-1])]
        if len(ss) < 2: return None
        hw = (jp.get('width') or float(np.interp(jp['s'], s, w))) / 2
        X = np.interp(ss, s, Cc[:, 0]); Z = np.interp(ss, s, Cc[:, 1])
        TX = np.interp(ss, s, T[:, 0]); TZ = np.interp(ss, s, T[:, 1])
        o = jp.get('offset', 0)
        # sideways unit (perpendicular to travel) = (-tz, tx) -> matches the core's "lat" sign
        nx_, nz_ = -TZ, TX
        a = [(x + nx_k * (o - hw), z + nz_k * (o - hw)) for x, z, nx_k, nz_k in zip(X, Z, nx_, nz_)]
        b = [(x + nx_k * (o + hw), z + nz_k * (o + hw)) for x, z, nx_k, nz_k in zip(X, Z, nx_, nz_)]
        return np.array(a + b[::-1])

    def tab(self):
        try: return self.nb.tab(self.nb.select(), 'text')
        except tk.TclError: return 'Terrain'

    def update_cursor_hint(self):
        t = self.tab()
        tips = {'Terrain': 'Terrain: change the settings on the right; right-drag moves the view.',
                'Track': 'Track: click to add points, drag to move, click the line to insert.',
                'Jumps': 'Jumps: drag a template from the list onto a track (or select it and click the track).',
                'Ruts': {'rut_pen': 'Rut: click points along the line; Esc finishes.',
                                 'rut_free': 'Rut: drag to draw it.', 'stamp': 'Bump: click to place one.',
                                 'scatter': 'Scatter: click the middle of the area.', 'braking': 'Braking bumps: click the track where they start.',
                                 'select': 'Select: click a rut or bump to edit it.'}.get(self.rtool.get(), ''),
                'Sculpt': 'Sculpt (%s): drag to paint.' % self.stool.get(),
                'Surface': 'Surface: drag to paint with the picked brush tip.',
                'Nature': 'Nature: click to place, or drag to paint (Ctrl = back to automatic).',
                'Layers': 'Layers: pick one in the list to edit it.'}
        self.status.set(tips.get(t, ''))

    # ------------------------------------------------------------------ mouse
    def on_motion(self, e):
        x, z = self.c2w(e.x, e.y)
        if self.H is not None and 0 <= x <= self.p.size and 0 <= z <= self.p.size:
            i, j = int(round(z / self.p.px)), int(round(x / self.p.px))
            h = self.H[i, j]
            gz, gx = (self.H[min(i + 1, self.p.n - 1), j] - self.H[max(i - 1, 0), j]) / (2 * self.p.px), \
                     (self.H[i, min(j + 1, self.p.n - 1)] - self.H[i, max(j - 1, 0)]) / (2 * self.p.px)
            self.cursor_lbl.config(text='x %.1f  z %.1f m   height %.2f m   slope %.0f°' % (x, z, h, math.degrees(math.atan(math.hypot(gx, gz)))))
        c = self.canvas; c.delete('brush')
        t = self.tab()
        if t in ('Sculpt', 'Surface', 'Nature'):
            r = (self.brush['radius'] if t == 'Sculpt' else self.paint_radius()) / self.mpp
            c.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline='#fff', tags='brush')

    def wheel(self, e, up):
        x, z = self.c2w(e.x, e.y)
        self.mpp *= 1 / 1.2 if up else 1.2
        self.mpp = min(max(self.mpp, self.p.px / 8), self.p.size / 100)
        nx, nz = self.c2w(e.x, e.y); self.vx += x - nx; self.vz += z - nz
        self.request_render()

    def on_pan_start(self, e): self._pan = (e.x, e.y, self.vx, self.vz, False)

    def on_pan(self, e):
        if not getattr(self, '_pan', None): return
        sx, sy, vx, vz, _ = self._pan
        c, s_ = math.cos(self.view_rot), math.sin(self.view_rot)
        a, b = (e.x - sx) * self.mpp, -(e.y - sy) * self.mpp
        self.vx = vx - (a * c + b * s_); self.vz = vz - (-a * s_ + b * c)
        self._pan = (sx, sy, vx, vz, True); self.quick_shift()

    def on_pan_end(self, e):
        p = getattr(self, '_pan', None); self._pan = None
        if p and p[4]: self.render()
        if p and not p[4] and self.tab() == 'Track': self.track_menu(e)

    def on_right(self, e):
        self.on_pan_start(e)

    def hit_point(self, sx, sy, r=8):
        i = self.cur_track_index()
        order = [i] + [k for k in range(len(self.p.tracks)) if k != i] if i is not None else []
        for k in order:
            for j, q in enumerate(self.p.tracks[k]['points']):
                px_, py_ = self.w2c(q[0], q[1])
                if abs(px_ - sx) <= r and abs(py_ - sy) <= r: return k, j
        return None

    def hit_curve(self, sx, sy, r=7):
        x, z = self.c2w(sx, sy)
        best = None
        for i in range(len(self.p.tracks)):
            cur = self.curve_of(i)
            if cur is None: continue
            Cc, T, s, w, bank = cur
            d = np.hypot(Cc[:, 0] - x, Cc[:, 1] - z); k = int(np.argmin(d))
            if d[k] / self.mpp <= r and (best is None or d[k] < best[2]): best = (i, k, d[k])
        return best

    def on_down(self, e):
        t = self.tab(); x, z = self.c2w(e.x, e.y)
        self._drag = None
        if t == 'Track': self.track_down(e, x, z)
        elif t == 'Jumps': self.jump_down(e, x, z)
        elif t == 'Ruts': self.rut_down(e, x, z)
        elif t in ('Sculpt', 'Surface', 'Nature'): self.sculpt_down(e, x, z)
        else: self.on_pan_start(e); self._drag = {'kind': 'pan'}

    def on_drag(self, e):
        d = self._drag
        if not d: return
        x, z = self.c2w(e.x, e.y)
        k = d['kind']
        if k == 'pan': self.on_pan(e)
        elif k == 'point': self.track_drag(e, x, z)
        elif k == 'jump': self.jump_drag(e, x, z)
        elif k in ('rut_pt',): self.rut_pt_drag(x, z)
        elif k == 'free': self.free_drag(x, z)
        elif k == 'sculpt': self.sculpt_drag(x, z)
        elif k == 'ramp': d['b'] = (x, z); self.request_render()

    def on_up(self, e):
        d = self._drag; self._drag = None
        if not d: return
        k = d['kind']
        if k == 'pan':
            moved = bool(getattr(self, '_pan', None) and self._pan[4]); self._pan = None
            if moved: self.render()
        elif k in ('point', 'jump', 'rut_pt'):
            if d.get('moved'): self.p._cache.pop(('tf', d.get('track')), None); self.recompute()
        elif k == 'free': self.free_end(d)
        elif k == 'sculpt': self.sculpt_end(d)
        elif k == 'ramp':
            if d.get('b'):
                arr = self.sculpt_array()
                old = arr.copy()
                r = C.ramp(arr, self.H, self.p.px, d['a'], d['b'], float(self.brush['ramp_width']), float(self.brush['hardness']))
                if r: self.hist.push_array(self.sculpt_name(), r[0], old[r[0]])
            self.recompute()

    def on_delete(self, e=None):
        if isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox)): return
        s = self.sel
        if not s: return
        self.hist.push_state()
        if s[0] == 'point':
            tr = self.p.tracks[s[1]]
            for j in sorted((self.sel_pts | {s[2]}), reverse=True):
                if j < len(tr['points']): tr['points'].pop(j)
            self.sel = ('track', s[1]); self.sel_pts = set()
        elif s[0] == 'track': self.hist.undo_.pop(); self.delete_track(); return
        elif s[0] == 'jump': self.p.jumps.pop(s[1]); self.sel = None
        elif s[0] == 'rut': self.p.ruts.pop(s[1]); self.sel = None
        elif s[0] == 'bump': self.p.bumps.pop(s[1]); self.sel = None
        self.p._cache.clear(); self.refresh_layers(); self.recompute()

    # ---- track mouse
    def _snap45(self, x, z, ref):
        dx, dz = x - ref[0], z - ref[1]
        L = math.hypot(dx, dz); a = round(math.atan2(dz, dx) / (math.pi / 4)) * (math.pi / 4)
        return ref[0] + L * math.cos(a), ref[1] + L * math.sin(a)

    def track_down(self, e, x, z):
        shift = e.state & 0x1
        hp = self.hit_point(e.x, e.y)
        if hp:
            i, j = hp
            if shift and self.sel and self.sel[0] == 'point' and self.sel[1] == i:
                a, b = sorted((self.sel[2], j)); self.sel_pts = set(range(a, b + 1))
                self.refresh_track_list(); self.render(); return
            self.sel = ('point', i, j); self.sel_pts = set(); self.refresh_track_list()
            self.hist.push_state(); self._drag = {'kind': 'point', 'track': i, 'j': j}
            self.render(); return
        hc = self.hit_curve(e.x, e.y)
        if hc and len(self.p.tracks[hc[0]]['points']) >= 2:
            i, k, _ = hc
            tr = self.p.tracks[i]
            pts = np.array([q[:2] for q in tr['points']])
            if tr.get('closed'): pts_c = np.vstack([pts, pts[:1]])
            else: pts_c = pts
            # project to the segment between consecutive points nearest to the click
            segd = []
            for a in range(len(pts_c) - 1):
                p0, p1 = pts_c[a], pts_c[a + 1]; dv = p1 - p0; L2 = max(dv @ dv, 1e-9)
                tt = np.clip(((x - p0[0]) * dv[0] + (z - p0[1]) * dv[1]) / L2, 0, 1)
                segd.append(math.hypot(p0[0] + tt * dv[0] - x, p0[1] + tt * dv[1] - z))
            a = int(np.argmin(segd))
            self.hist.push_state()
            w = (tr['points'][a][2] + tr['points'][(a + 1) % len(tr['points'])][2]) / 2
            tr['points'].insert(a + 1, [x, z, w, 0.0])
            self.sel = ('point', i, a + 1); self.sel_pts = set()
            self._drag = {'kind': 'point', 'track': i, 'j': a + 1, 'moved': True}
            self.refresh_track_list(); self.render(); return
        # add a point to the end of the current track (a new one if there is none)
        if not self.p.tracks: self.new_track()
        i = self.cur_track_index(); tr = self.p.tracks[i]
        self.hist.push_state()
        at_start = self.sel and self.sel[0] == 'point' and self.sel[1] == i and self.sel[2] == 0 and len(tr['points']) > 1
        if shift and tr['points']:
            ref = tr['points'][0] if at_start else tr['points'][-1]
            x, z = self._snap45(x, z, ref)
        w = tr['points'][-1][2] if tr['points'] else C.TRACK_DEFAULTS['width']
        if at_start:
            tr['points'].insert(0, [x, z, w, 0.0]); j = 0
        else:
            tr['points'].append([x, z, w, 0.0]); j = len(tr['points']) - 1
        self.sel = ('point', i, j); self.sel_pts = set()
        self._drag = {'kind': 'point', 'track': i, 'j': j, 'moved': True}
        self.refresh_track_list(); self.render()
        if len(tr['points']) >= 2: self.recompute()

    def track_drag(self, e, x, z):
        d = self._drag; tr = self.p.tracks[d['track']]; j = d['j']
        if e.state & 0x1 and len(tr['points']) > 1:
            ref = tr['points'][j - 1] if j > 0 else tr['points'][j + 1]
            x, z = self._snap45(x, z, ref)
        tr['points'][j][0] = min(max(x, 0), self.p.size); tr['points'][j][1] = min(max(z, 0), self.p.size)
        d['moved'] = True; self.request_render()

    def track_menu(self, e):
        hp = self.hit_point(e.x, e.y)
        if not hp: return
        i, j = hp
        tr = self.p.tracks[i]
        if j not in (0, len(tr['points']) - 1) or len(tr['points']) < 2: return
        m = tk.Menu(self, tearoff=False)
        for ang in (45, 90, 180):
            for side, sg in (('left', -1), ('right', 1)):
                m.add_command(label='Add a %d° turn to the %s' % (ang, side), command=lambda a=ang, s=sg: self.add_turn(i, j, a, s))
        m.tk_popup(e.x_root, e.y_root)

    def add_turn(self, i, j, ang, side):
        tr = self.p.tracks[i]; P = tr['points']
        end = P[j]; prev = P[j - 1] if j > 0 else P[j + 1]
        dx, dz = end[0] - prev[0], end[1] - prev[1]; L = math.hypot(dx, dz) or 1
        tx, tz = dx / L, dz / L
        w = end[2]
        try: gap = float(self.lane_gap.get())
        except ValueError: gap = 4.0
        radius = (w + gap) / 2 if ang == 180 else max(w * 1.2, 6.0)
        nx_, nz_ = -tz * side, tx * side          # towards the turn
        cx, cz = end[0] + nx_ * radius, end[1] + nz_ * radius
        a0 = math.atan2(end[1] - cz, end[0] - cx)
        steps = max(2, int(ang / 30))
        new = []
        cross = tx * nz_ - tz * nx_
        for k in range(1, steps + 1):
            a = a0 + math.radians(ang) * k / steps * (1 if cross > 0 else -1)
            new.append([cx + radius * math.cos(a), cz + radius * math.sin(a), w, 0.0])
        # a short straight after the turn
        lx, lz = new[-1][0] - (new[-2][0] if len(new) > 1 else end[0]), new[-1][1] - (new[-2][1] if len(new) > 1 else end[1])
        ll = math.hypot(lx, lz) or 1
        new.append([new[-1][0] + lx / ll * radius, new[-1][1] + lz / ll * radius, w, 0.0])
        self.hist.push_state()
        if j == len(P) - 1: P.extend(new)
        else:
            for q in new: P.insert(0, q)
        self.recompute()

    # ---- jumps mouse
    def jump_handle(self, jp):
        """The round 'turn' handle of a free jump: just past its far end."""
        a = math.radians(jp.get('heading', 0)); r = jp['length'] / 2 + 2.5 * max(self.mpp, 0.3)
        return jp['x'] + r * math.cos(a), jp['z'] + r * math.sin(a)

    def jump_down(self, e, x, z):
        for k, jp in enumerate(self.p.jumps):
            if jp.get('track', 0) < 0:
                hx, hz = self.jump_handle(jp)
                if math.hypot(hx - x, hz - z) <= 8 * self.mpp:
                    self.hist.push_state(); self.select(('jump', k))
                    self._drag = {'kind': 'jump', 'k': k, 'rotate': True}; return
        hit = None
        for k, jp in enumerate(self.p.jumps):
            poly = self.jump_poly(jp)
            if poly is None: continue
            if self._inside(poly, x, z): hit = k
        if hit is None:
            self.place_jump(x, z); return
        self.hist.push_state()
        if e.state & 0x4:                              # Ctrl: copy
            q = copy.deepcopy(self.p.jumps[hit]); q['name'] = 'Jump %d' % (len(self.p.jumps) + 1)
            self.p.jumps.append(q); hit = len(self.p.jumps) - 1; self.refresh_layers()
        self.select(('jump', hit))
        jp = self.p.jumps[hit]
        if jp.get('track', 0) < 0:
            self._drag = {'kind': 'jump', 'k': hit, 'shift': bool(e.state & 0x1), 'free': True, 'start': (x, z),
                          'xz0': (jp['x'], jp['z']), 'L0': jp['length']}
            return
        nt = self.nearest_track(x, z)
        self._drag = {'kind': 'jump', 'k': hit, 'shift': bool(e.state & 0x1), 's0': jp['s'], 'L0': jp['length'],
                      'grab': (nt[1] if nt else jp['s'])}

    @staticmethod
    def _inside(poly, x, z):
        n = len(poly); inside = False
        for a in range(n):
            x1, z1 = poly[a]; x2, z2 = poly[(a + 1) % n]
            if (z1 > z) != (z2 > z) and x < (x2 - x1) * (z - z1) / (z2 - z1 + 1e-12) + x1: inside = not inside
        return inside

    def jump_drag(self, e, x, z):
        d = self._drag; jp = self.p.jumps[d['k']]
        if d.get('rotate'):
            ang = math.degrees(math.atan2(z - jp['z'], x - jp['x']))
            if e.state & 0x1: ang = round(ang / 15) * 15
            jp['heading'] = ang % 360
            d['moved'] = True; self.jform.load(jp); self.request_render(); self._live_eval(); return
        if d.get('free'):
            a = math.radians(jp.get('heading', 0))
            dx, dz = x - d['start'][0], z - d['start'][1]
            if d['shift']:
                jp['length'] = max(1.0, d['L0'] + 2 * (dx * math.cos(a) + dz * math.sin(a)))
            else:
                jp['x'], jp['z'] = d['xz0'][0] + dx, d['xz0'][1] + dz
            d['moved'] = True; self.jform.load(jp); self.request_render(); self._live_eval(); return
        cur = self.curve_of(jp['track'])
        if cur is None: return
        Cc, T, s = cur[:3]
        k = int(np.argmin(np.hypot(Cc[:, 0] - x, Cc[:, 1] - z))); sv = float(s[k])
        if d['shift']:
            jp['length'] = max(1.0, d['L0'] + 2 * abs(sv - d['s0']) - 2 * abs(d['grab'] - d['s0']))
        else:
            jp['s'] = self.snap_s(min(max(d['s0'] + sv - d['grab'], 0), float(s[-1])))
        d['moved'] = True; self.jform.load(jp); self.draw_jump_shape(); self.request_render()
        self._live_eval()

    def _live_eval(self):
        if self._eval_job: return
        self._eval_job = self.after(60, self._evaluate)

    # ---- ruts & bumps mouse
    def rut_down(self, e, x, z):
        tool = self.rtool.get()
        # grab a rut point of the selected rut?
        r = self.cur_rut()
        if r is not None:
            for j, q in enumerate(r['points']):
                sx, sy = self.w2c(q[0], q[1])
                if abs(sx - e.x) <= 6 and abs(sy - e.y) <= 6:
                    self.hist.push_state(); self._drag = {'kind': 'rut_pt', 'j': j}; return
        if tool == 'select':
            self.select(self.pick_feature(x, z)); return
        if tool == 'rut_pen':
            self.hist.push_state()
            if r is None:
                d = dict(copy.deepcopy(self.new_rut), points=[[x, z]], visible=True, name='Rut %d' % (len(self.p.ruts) + 1))
                self.p.ruts.append(d); self.sel = ('rut', len(self.p.ruts) - 1); self.refresh_layers()
            else:
                r['points'].append([x, z])
            self.load_selection_forms(); self.recompute(); return
        if tool == 'rut_free':
            self._drag = {'kind': 'free', 'pts': [(x, z)]}; return
        b = dict(copy.deepcopy(self.new_bump), x=x, z=z, visible=True, name='Bumps %d' % (len(self.p.bumps) + 1))
        b['kind'] = tool
        if tool == 'braking':
            nt = self.nearest_track(x, z, max_d=20)
            if nt is None: self.status.set('Click on a track for braking bumps.'); return
            b['track'], b['s'] = nt[0], nt[1]
        self.hist.push_state(); self.p.bumps.append(b); self.sel = ('bump', len(self.p.bumps) - 1)
        self.refresh_layers(); self.load_selection_forms(); self.recompute()

    def pick_feature(self, x, z):
        best = None
        for k, rt in enumerate(self.p.ruts):
            for q in rt['points']:
                d = math.hypot(q[0] - x, q[1] - z)
                if best is None or d < best[0]: best = (d, ('rut', k))
        for k, b in enumerate(self.p.bumps):
            if b['kind'] == 'braking': continue
            d = max(0.0, math.hypot(b['x'] - x, b['z'] - z) - (b['area'] if b['kind'] == 'scatter' else b['radius']))
            if best is None or d < best[0]: best = (d, ('bump', k))
        return best[1] if best and best[0] < 12 * self.mpp + 1 else None

    def rut_pt_drag(self, x, z):
        r = self.cur_rut()
        if r is None: return
        r['points'][self._drag['j']] = [x, z]; self._drag['moved'] = True; self.request_render()

    def free_drag(self, x, z):
        pts = self._drag['pts']
        if math.hypot(x - pts[-1][0], z - pts[-1][1]) >= max(0.5, self.p.px * 2):
            pts.append((x, z))
            a = self.w2c(*pts[-2]); b = self.w2c(*pts[-1])
            self.canvas.create_line(*a, *b, fill='#ff7ad9', width=2)

    def free_end(self, d):
        pts = rdp(np.array(d['pts']), 0.25)
        if len(pts) < 2: return
        self.hist.push_state()
        r = dict(copy.deepcopy(self.new_rut), points=[list(map(float, q)) for q in pts], visible=True,
                 name='Rut %d' % (len(self.p.ruts) + 1))
        self.p.ruts.append(r); self.sel = ('rut', len(self.p.ruts) - 1)
        self.refresh_layers(); self.load_selection_forms(); self.recompute()

    # ---- sculpt / paint mouse
    def sculpt_name(self):
        t = self.stool.get()
        if t in ('Mask', 'Unmask'): return 'mask'
        if t in ('Erase import', 'Restore import'): return 'impkeep'
        return 'tsculpt' if self.smode.get() == 'terrain' else 'fsculpt'

    def sculpt_array(self):
        return getattr(self.p, self.sculpt_name())

    def sculpt_down(self, e, x, z):
        if self.H is None: return
        if self.tab() in ('Surface', 'Nature'):
            self._drag = {'kind': 'sculpt', 'paint': self.tab(), 'old': {k: getattr(self.p, k).copy() for k in ('surf', 'canopy', 'rocks', 'smix')},
                          'win': None, 'last': None, 'rng': np.random.default_rng(), 'erase': bool(e.state & 0x4),
                          'angle': 0.0}
            self.sculpt_drag(x, z); return
        tool = self.stool.get()
        alt = 0x20000 if os.name == 'nt' else 0x8                     # Alt (Windows) / Mod1 (Linux)
        if tool == 'Clone' and (e.state & alt):                         # Alt: the spot to copy from
            self.clone_src = (x, z); self.status.set('Clone source set - now paint where it should go.'); return
        if tool == 'Ramp':
            self._drag = {'kind': 'ramp', 'a': (x, z), 'b': None}; return
        arr = self.sculpt_array()
        ctx = {'h0': float(self.H[min(self.p.n - 1, max(0, int(z / self.p.px))), min(self.p.n - 1, max(0, int(x / self.p.px)))]),
               'step': float(self.brush['step']), 'seed': int(self.brush['seed']), 'src': getattr(self, 'clone_src', None),
               'start': (x, z)}
        if tool == 'Clone' and ctx['src'] is None:
            self.status.set('Clone: Alt+click the spot to copy from first.'); return
        self._drag = {'kind': 'sculpt', 'paint': False, 'old': arr.copy(), 'win': None, 'ctx': ctx, 'last': None}
        self.sculpt_drag(x, z)

    def sculpt_drag(self, x, z):
        d = self._drag
        if d['paint']:
            rad = self.paint_radius()
            place = d['paint'] == 'Nature' and self.nat_mode.get() == 'place'
            gap = rad * (2.2 if place else max(0.05, float(self.pbrush.get('spacing', 0.3))))
        else:
            rad = float(self.brush['radius']); gap = rad * 0.25
        if d['last'] is not None:
            dist = math.hypot(x - d['last'][0], z - d['last'][1])
            if dist < gap: return
            d['angle'] = math.atan2(z - d['last'][1], x - d['last'][0])
        d['last'] = (x, z)
        if d['paint']:
            win = self.paint_dab(d, x, z, rad)
            if win is None: return
        else:
            tool = self.stool.get()
            arr = self.sculpt_array()
            if tool in ('Mask', 'Unmask', 'Erase import', 'Restore import'):
                bw = C.brush_window(self.p.n, self.p.px, x, z, rad)
                if bw is None: return
                win, q = bw
                f = C.falloff(q, float(self.brush['hardness'])) * float(self.brush['strength'])
                arr[win] = np.clip(arr[win] + (f if tool in ('Mask', 'Restore import') else -f), 0, 1)
            else:
                r = C.sculpt_dab(tool, arr, self.H, self.p.px, x, z, rad, float(self.brush['strength']),
                                 float(self.brush['hardness']), d['ctx'])
                if r is None: return
                win, dd = r
                self.H[win] += dd
        d['win'] = win if d['win'] is None else (slice(min(d['win'][0].start, win[0].start), max(d['win'][0].stop, win[0].stop)),
                                                  slice(min(d['win'][1].start, win[1].start), max(d['win'][1].stop, win[1].stop)))
        self.reshade(win)

    def _auto_surface(self, win):
        """The automatic surface (from the tracks) inside a window."""
        out = np.full((win[0].stop - win[0].start, win[1].stop - win[1].start), -1, np.int8)
        keep = 1 - np.clip(self.p.mask[win], 0, 1)
        for i, tr in enumerate(self.p.tracks):
            if tr.get('visible', True) and tr.get('surface'):
                m = (self.info['ti'][win] == i) & (self.info['tw'][win] * keep > 0.5)
                out[m] = C.SURFACES.index(tr['surface'])
        return out

    def sculpt_end(self, d):
        if d['win'] is None: return
        if d['paint']:
            self.hist.push_arrays([(k, d['win'], d['old'][k][d['win']].copy()) for k in ('surf', 'canopy', 'rocks', 'smix')])
        else:
            self.hist.push_array(self.sculpt_name(), d['win'], d['old'][d['win']].copy())
            if self.sculpt_name() == 'impkeep': self.after_sculpt('impkeep'); return
        self.recompute()

    def paint_dab(self, d, x, z, rad):
        """One dab of the Surface / Nature brush (or one placed spot). Returns the changed window."""
        p = self.p
        bw = C.brush_window(p.n, p.px, x, z, rad)
        if bw is None: return None
        win, q = bw
        place = d['paint'] == 'Nature' and self.nat_mode.get() == 'place'
        if place:
            m = q < 1
        else:
            iz, ix = np.mgrid[win[0], win[1]]
            X = ix * p.px; Z = iz * p.px
            pat = C.brush_tip(self.pbrush, X, Z, X - x, Z - z, rad, p.px, d['rng'], d['angle'])
            m = pat * float(self.pbrush.get('opacity', 1.0)) > C.pixel_threshold(win)
        if not m.any(): return win
        if d['paint'] == 'Surface':
            lay = self.paint_layer.get()
            val = -1 if lay.startswith('Automatic') else (-2 if lay.startswith('Keep') else C.SURFACES.index(SURF_BY_NAME[lay]))
            p.surf[win][m] = val
            mx = SURF_BY_NAME.get(self.mix_layer.get()) if val >= 0 else None
            amt = int(round(float(self.mix_amt.get()) * 2.55)) if mx else 0
            if mx and 0 < amt and C.SURFACES.index(mx) != val:
                p.smix[win][m] = C.SURFACES.index(mx) * 256 + min(amt, 255)
            else:
                p.smix[win][m] = -1
        else:
            it = C.nature_item(self.nat_item.get())
            name, surf, can, rk = it[:4]
            try: lvl = int(float(self.nat_level.get()))
            except ValueError: lvl = None
            if lvl is not None: lvl = max(0, min(255, lvl))
            if d.get('erase'):
                p.canopy[win][m] = -1; p.rocks[win][m] = -1
            else:
                if can is not None: p.canopy[win][m] = can if (can <= 0 or lvl is None or rk is not None) else lvl
                if rk is not None: p.rocks[win][m] = rk if (rk <= 0 or lvl is None or can is not None) else lvl
                if surf == 'specks':                   # bushes only: a tiny forest-floor patch per bush
                    if place:
                        sel = m & (q * rad <= C.SPECK_R)
                    else:
                        dd, live = C.bush_spots(X, Z, float(self.pbrush.get('density', 0.5)))
                        sel = m & live & (dd <= C.SPECK_R)
                    p.surf[win][sel] = C.SURFACES.index('forest_floor')
                elif surf is not None and (self.nat_ground.get() or surf == 'rock'):
                    p.surf[win][m] = C.SURFACES.index(surf)
        s2 = self.info['surface'][win]
        auto = self._auto_surface(win)
        s2[:] = np.where(p.surf[win] >= 0, p.surf[win], np.where(p.surf[win] == -2, -1, auto))
        return win

    # ------------------------------------------------------------------ undo / files
    def undo(self):
        if self.hist.undo(): self.p._cache.pop('base_key', None); self.after_undo()

    def redo(self):
        if self.hist.redo(): self.p._cache.pop('base_key', None); self.after_undo()

    def after_undo(self):
        self._mask_dirty = True
        for k in list(self.p._cache):
            if isinstance(k, tuple) and k[0] in ('tf', 'rf', 'disp'): self.p._cache.pop(k)
        if self.sel:
            lst = {'track': self.p.tracks, 'point': self.p.tracks, 'jump': self.p.jumps, 'rut': self.p.ruts, 'bump': self.p.bumps}[self.sel[0]]
            if self.sel[1] >= len(lst): self.sel = None
        self.recompute(now=True)

    def _autosave(self):
        self._autosave_job = None
        if self._drag: self._autosave_job = self.after(1500, self._autosave); return
        try:
            self.p.save(self.path)
        except OSError:
            pass

    def _report_error(self, exc, val, tb):
        """Errors are shown (and written to studio_errors.log next to the presets) instead of failing silently."""
        import traceback
        txt = ''.join(traceback.format_exception(exc, val, tb))
        try:
            with open(os.path.join(os.path.dirname(presets_path()), 'studio_errors.log'), 'a') as f: f.write(txt + '\n')
        except OSError:
            pass
        print(txt)
        try:
            messagebox.showerror('Map editor', 'Something went wrong (details are in studio_errors.log in %s):\n\n%s'
                                 % (os.path.dirname(presets_path()), txt[-900:]), parent=self)
        except Exception:
            pass

    def update_title(self):
        nm = os.path.basename(self.path) if self.path else 'untitled'
        self.title('Heightmap Studio - %s%s' % (nm, ('  [%s]' % self.title_hint) if self.title_hint else ''))

    def new_project(self):
        d = tk.Toplevel(self); d.title('New heightmap'); d.transient(self); d.grab_set()
        n = tk.StringVar(value='1025'); px = tk.StringVar(value='0.25'); pre = tk.StringVar(value='Rolling hills')
        for r_, (lab, var, vals) in enumerate((('Pixels per side', n, ['513', '1025', '2049']),
                                               ('Metres per pixel', px, ['0.125', '0.25', '0.5', '1']),
                                               ('Terrain', pre, list(C.TERRAIN_PRESETS)))):
            ttk.Label(d, text=lab).grid(row=r_, column=0, sticky='w', padx=8, pady=4)
            ttk.Combobox(d, textvariable=var, values=vals, width=14).grid(row=r_, column=1, padx=8)

        def ok():
            try: nn, pp = int(n.get()), float(px.get())
            except ValueError: return
            d.destroy()
            self.hist.push_full()
            q = C.Project(nn, pp); t = dict(C.TERRAIN_PRESETS.get(pre.get(), {})); t.setdefault('tilt', 0.0)
            q.terrain.update(t); q.terrain['preset'] = pre.get()
            q.templates = self.p.templates; q.placement = None
            self.p.__dict__.update(q.__dict__); self.path = None; self.update_title(); self.sel = None
            self.fit(); self.recompute(now=True)
        ttk.Button(d, text='Create', command=ok).grid(row=3, column=0, columnspan=2, pady=8)

    def open_project(self):
        path = filedialog.askopenfilename(parent=self, title='Open heightmap project',
                                          filetypes=[('Heightmap project', '*.hmproj'), ('All', '*.*')])
        if not path: return
        try:
            q = C.Project.open(path)
        except Exception as e:
            messagebox.showerror('Heightmap studio', "Couldn't open it:\n%s" % e, parent=self); return
        self.hist.push_full()
        self.p.__dict__.update(q.__dict__); self.path = path; self.update_title(); self.sel = None
        self.fit(); self.recompute(now=True)

    def save_project(self, ask=False):
        if ask or not self.path:
            p = filedialog.asksaveasfilename(parent=self, title='Save heightmap project', defaultextension='.hmproj',
                                             filetypes=[('Heightmap project', '*.hmproj')])
            if not p: return
            self.path = p; self.update_title()
        try:
            self.p.save(self.path); self.status.set('Saved %s' % os.path.basename(self.path))
        except OSError as e:
            messagebox.showerror('Heightmap studio', "Couldn't save:\n%s" % e, parent=self)

    def export_dialog(self):
        d = tk.Toplevel(self); d.title('Export heightmap'); d.transient(self); d.grab_set()
        mode = tk.StringVar(value=self.p.export.get('mode', 'auto') if (self.p.export.get('mode') != 'world' or self.p.world) else 'auto')
        lo = tk.StringVar(value='%g' % self.p.export.get('lo', 0)); hi = tk.StringVar(value='%g' % self.p.export.get('hi', 50))
        ttk.Label(d, text='16-bit greyscale PNG, %d x %d pixels' % (self.p.n, self.p.n)).grid(row=0, column=0, columnspan=4, sticky='w', padx=8, pady=6)
        ttk.Radiobutton(d, text='Black = lowest, white = highest point of this heightmap (%.2f to %.2f m)'
                        % (float(self.H.min()), float(self.H.max())), value='auto', variable=mode).grid(row=1, column=0, columnspan=4, sticky='w', padx=8)
        rb = ttk.Radiobutton(d, text="The game map's scale%s" % (' (%.1f to %.1f m)' % tuple(self.p.world) if self.p.world else ' (only for ground from the map editor)'),
                             value='world', variable=mode)
        rb.grid(row=2, column=0, columnspan=4, sticky='w', padx=8)
        if not self.p.world: rb.state(['disabled'])
        ttk.Radiobutton(d, text='Fixed: black =', value='fixed', variable=mode).grid(row=3, column=0, sticky='w', padx=8)
        ttk.Entry(d, textvariable=lo, width=7).grid(row=3, column=1); ttk.Label(d, text='m, white =').grid(row=3, column=2)
        ttk.Entry(d, textvariable=hi, width=7).grid(row=3, column=3, padx=(0, 8))

        def ok():
            self.p.export = {'mode': mode.get(), 'lo': float(lo.get() or 0), 'hi': float(hi.get() or 1)}
            d.destroy()
            path = filedialog.asksaveasfilename(parent=self, title='Export heightmap', defaultextension='.png',
                                                filetypes=[('16-bit PNG', '*.png')])
            if not path: return
            a, b = C.export_heightmap(self.p, path, self.H)
            with open(os.path.splitext(path)[0] + '.heights.json', 'w') as f:
                json.dump({'black_m': a, 'white_m': b, 'metres_per_pixel': self.p.px, 'size_m': self.p.size}, f, indent=1)
            self.status.set('Exported %s (black = %.2f m, white = %.2f m; written next to it in .heights.json)'
                            % (os.path.basename(path), a, b))
        ttk.Button(d, text='Export…', command=ok).grid(row=4, column=0, columnspan=4, pady=8)

    def export_surface(self):
        path = filedialog.asksaveasfilename(parent=self, title='Export surface / texture mask', defaultextension='.png',
                                            filetypes=[('PNG with transparency', '*.png')])
        if not path: return
        C.export_surface(self.p, path, self.info)
        self.status.set('Exported %s (transparent where the ground is left alone) and %s'
                        % (os.path.basename(path), os.path.basename(os.path.splitext(path)[0] + '.index.png')))

    def send(self):
        if not self.on_send: return
        try:
            msg = self.on_send(self.p, self.H, self.info)
        except Exception as e:
            messagebox.showerror('Heightmap studio', "Couldn't send it:\n%s" % e, parent=self); return
        if msg: self.status.set(msg)

    def close(self):
        if self.hist.undo_ and messagebox.askyesno('Heightmap studio', 'Save the project before closing?', parent=self):
            self.save_project()
        self.destroy()


def rdp(P, eps):
    """Ramer-Douglas-Peucker line simplification."""
    if len(P) < 3: return P
    a, b = P[0], P[-1]; ab = b - a; L = np.linalg.norm(ab)
    d = np.abs(ab[0] * (P[:, 1] - a[1]) - ab[1] * (P[:, 0] - a[0])) / L if L > 1e-9 else np.linalg.norm(P - a, axis=1)
    k = int(np.argmax(d))
    if d[k] > eps:
        return np.vstack([rdp(P[:k + 1], eps)[:-1], rdp(P[k:], eps)])
    return np.vstack([a, b])


class TemplateEditor(tk.Toplevel):
    """Draw a jump profile: drag points, double-click to add, right-click to remove."""

    def __init__(self, studio, name):
        super().__init__(studio); self.s = studio
        self.title('Jump template'); self.transient(studio)
        src = studio.p.template(name) if name else [(0, 0), (0.3, 1), (0.7, 1), (1, 0)]
        self.pts = C.profile_points(src)
        # the sizes kept with the template: its own saved ones, else the selected jump's, else the defaults
        j = studio.cur_jump()
        self.specs = {k: float(C.JUMP_DEFAULTS.get(k, 0)) for k in C.JUMP_SPEC_KEYS}
        if j is not None: self.specs.update({k: float(j.get(k, 0) or 0) for k in C.JUMP_SPEC_KEYS})
        self.specs.update(studio.p.template_specs(name) or {} if name else {})
        self.name = tk.StringVar(value=(name + ' (mine)') if name and name not in studio.p.templates else (name or 'My jump'))
        r = ttk.Frame(self, padding=6); r.pack(fill='x')
        ttk.Label(r, text='Name').pack(side='left'); ttk.Entry(r, textvariable=self.name, width=24).pack(side='left', padx=4)
        self.cv = tk.Canvas(self, width=640, height=280, background='#2a2e35', highlightthickness=0); self.cv.pack(padx=6)
        ttk.Label(self, text='Left = start of the jump (direction of travel →). Height 1 = the jump\'s Height setting. '
                  'Drag points, double-click to add one, right-click to remove one.', foreground='#666',
                  wraplength=620).pack(padx=6, anchor='w')
        sf = ttk.LabelFrame(self, text=' Sizes kept with the template (new jumps start with these) ', padding=6)
        sf.pack(fill='x', padx=6, pady=(6, 0))
        self.sform = Form(sf, [('length', 'Length (m)', 'float', {'step': 0.5, 'min': 1}), ('height', 'Height (m)', 'float', {'step': 0.1}),
                  ('width', 'Width (0 = track)', 'float', {'step': 0.5, 'min': 0}), ('edge', 'Side blend (m)', 'float', {'step': 0.25, 'min': 0}),
                  ('angle', 'Angle (°)', 'float', {'step': 5}), ('offset', 'Sideways (m)', 'float', {'step': 0.25})],
             lambda d, k, v: d.__setitem__(k, v), cols=3).pack(fill='x')
        self.sform.load(self.specs)
        b = ttk.Frame(self, padding=6); b.pack(fill='x')
        ttk.Button(b, text='Save template', command=self.save).pack(side='right')
        self.cv.bind('<ButtonPress-1>', self.down); self.cv.bind('<B1-Motion>', self.drag)
        self.cv.bind('<Double-1>', self.add); self.cv.bind('<Button-3>', self.remove)
        self.k = None; self.draw()

    def xy(self, u, h): return 20 + u * 600, 230 - h * 180

    def uh(self, x, y): return min(1, max(0, (x - 20) / 600)), max(-0.5, min(1.2, (230 - y) / 180))

    def draw(self):
        cv = self.cv; cv.delete('all')
        cv.create_line(*self.xy(0, 0), *self.xy(1, 0), fill='#556'); cv.create_line(*self.xy(0, 1), *self.xy(1, 1), fill='#334', dash=(3, 3))
        self.pts.sort(key=lambda q: q[0])
        u = np.linspace(0, 1, 200); h = C.template_profile(self.pts, u)
        cv.create_line(*[v for a, b in zip(u, h) for v in self.xy(a, b)], fill='#e8c060', width=2)
        for a, b_ in self.pts:
            x, y = self.xy(a, b_); cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill='#fff')

    def near(self, e):
        for k, (a, b) in enumerate(self.pts):
            x, y = self.xy(a, b)
            if abs(x - e.x) < 8 and abs(y - e.y) < 8: return k
        return None

    def down(self, e): self.k = self.near(e)

    def drag(self, e):
        if self.k is None: return
        self.pts[self.k] = list(self.uh(e.x, e.y))
        if self.k == 0: self.pts[0][0] = 0.0
        self.draw()
        self.k = min(range(len(self.pts)), key=lambda i: abs(self.xy(*self.pts[i])[0] - e.x) + abs(self.xy(*self.pts[i])[1] - e.y))

    def add(self, e): self.pts.append(list(self.uh(e.x, e.y))); self.draw()

    def remove(self, e):
        k = self.near(e)
        if k is not None and len(self.pts) > 2: self.pts.pop(k); self.draw()

    def save(self):
        n = self.name.get().strip()
        if not n: return
        if n in C.BUILTIN_TEMPLATES: n += ' (mine)'
        self.pts.sort(key=lambda q: q[0])
        for k in C.JUMP_SPEC_KEYS: self.sform._changed(k)       # commit a size still being typed
        self.s.p.templates[n] = {'points': [tuple(q) for q in self.pts], 'specs': dict(self.specs)}
        self.s.presets.setdefault('templates', {})[n] = self.s.p.templates[n]; save_presets(self.s.presets)
        self.s.refresh_templates()
        names = self.s.tpl_names(); self.s.tpl_list.selection_clear(0, 'end'); self.s.tpl_list.selection_set(names.index(n))
        self.s.draw_tpl_preview(); self.s.recompute(); self.destroy()


def open_studio(master, **kw):
    return Studio(master, **kw)


if __name__ == '__main__':
    root = tk.Tk(); root.withdraw()
    s = Studio(root); s.protocol('WM_DELETE_WINDOW', root.destroy)
    root.mainloop()

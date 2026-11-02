"""
workshop.py - painting and parts for an imported bike, inside Edit model (its "Paint" and "Parts" tabs).
Works like a racing game's livery editor:

    Tools (keys):  Move V · Colour bucket B · Eyedropper I     Add a shape / logo (PNG) / text where you clicked
    On the bike:   drag a design to slide it anywhere over the bike · corner squares size it · blue knob turns it
                   wheel = size the selected design · Shift+wheel = turn it · Ctrl+wheel = zoom the view
                   drag empty space = turn the view · right-drag = move the view · click = pick a spot
    Colour:        the colour square on the right changes the selected design at once; the eyedropper takes a
                   colour from the bike; the bucket paints the piece you click in the current colour
    Keys:          arrows nudge · [ ] turn · + - size · Ctrl+D duplicate · Delete · Ctrl+Z / Ctrl+Y
"""
import colorsys
import copy
import json
import math
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
from PIL import Image, ImageTk

import livery as LV

PREV = 1024                       # the texture size the views work at (the build uses the full size)
QUICK = ['#ffffff', '#111111', '#d62828', '#f77f00', '#fcbf49', '#2a9d8f', '#1d4ed8', '#7b2cbf', '#e5e5e5',
         '#6b7280', '#00a651', '#ff4fd8']
HINT_SEL = 'Drag it anywhere on the bike · corners = size · blue knob = turn · wheel = size · Shift+wheel = turn'


def font_list():
    out = {}
    for d in (os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'), '/usr/share/fonts'):
        if not os.path.isdir(d): continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(('.ttf', '.otf')): out.setdefault(os.path.splitext(f)[0], os.path.join(root, f))
    return dict(sorted(out.items(), key=lambda kv: kv[0].lower()))


def is_import(blend):
    return bool(blend) and os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(blend)), 'parts.json'))


class Cam:
    def __init__(self):
        self.yaw, self.pitch, self.zoom, self.pan, self.center, self.depth = -90.0, 8.0, 300.0, [0.0, 0.0], [0, 0.6, 0], 10.0

    def basis(self):
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        fwd = (math.cos(p) * math.sin(y), -math.sin(p), math.cos(p) * math.cos(y))
        right = (math.cos(y), 0.0, -math.sin(y))
        up = (fwd[1] * right[2] - fwd[2] * right[1], fwd[2] * right[0] - fwd[0] * right[2],
              fwd[0] * right[1] - fwd[1] * right[0])
        return right, up, fwd

    def ray(self, sx, sy, W, H):
        right, up, fwd = (np.asarray(v) for v in self.basis())
        a = (sx - W / 2) / self.zoom + self.pan[0]; b = -(sy - H / 2) / self.zoom + self.pan[1]
        o = np.asarray(self.center, np.float64) + right * a + up * b - fwd * self.depth
        return o, fwd

    def project(self, P, W, H):
        right, up, _ = (np.asarray(v) for v in self.basis())
        q = np.asarray(P, np.float64) - np.asarray(self.center)
        return W / 2 + (q @ right - self.pan[0]) * self.zoom, H / 2 - (q @ up - self.pan[1]) * self.zoom


# ==================================================================== colour picker (always open, changes at once)
class ColorPicker(ttk.Frame):
    W, H = 236, 120

    def __init__(self, master, on_change):
        super().__init__(master)
        self.on_change = on_change
        self.h, self.s, self.v = 0.0, 0.8, 0.85
        self.sv = tk.Canvas(self, width=self.W, height=self.H, highlightthickness=0, cursor='tcross')
        self.sv.pack(anchor='w')
        self.hue = tk.Canvas(self, width=self.W, height=14, highlightthickness=0, cursor='sb_h_double_arrow')
        self.hue.pack(anchor='w', pady=(3, 0))
        r = ttk.Frame(self); r.pack(fill='x', pady=(3, 0))
        self.sw = tk.Label(r, width=5, relief='solid', borderwidth=1); self.sw.pack(side='left')
        self.hex = tk.StringVar()
        e = ttk.Entry(r, textvariable=self.hex, width=9); e.pack(side='left', padx=4)
        e.bind('<Return>', lambda ev: self.typed())
        ttk.Label(r, text='hex', foreground='#777').pack(side='left')
        self.qf = ttk.Frame(self); self.qf.pack(fill='x', pady=(3, 0))
        for i, c in enumerate(QUICK):
            b = tk.Label(self.qf, background=c, width=2, relief='raised', borderwidth=1, cursor='hand2')
            b.grid(row=0, column=i, padx=1); b.bind('<Button-1>', lambda e, c=c: self.set(c, notify=True, final=True))
        ttk.Label(self, text='Recent', foreground='#777').pack(anchor='w')
        self.rf = ttk.Frame(self); self.rf.pack(fill='x')
        self.recent = []
        a = np.linspace(0, 1, self.W)[None, :]
        hsv = np.stack([a.repeat(14, 0), np.ones((14, self.W)), np.ones((14, self.W))], -1)
        self._hue_img = ImageTk.PhotoImage(Image.fromarray(self._hsv2rgb(hsv)))
        self.hue.create_image(0, 0, image=self._hue_img, anchor='nw')
        for c, f in ((self.sv, self.sv_mouse), (self.hue, self.hue_mouse)):
            c.bind('<ButtonPress-1>', lambda e, f=f: f(e, False)); c.bind('<B1-Motion>', lambda e, f=f: f(e, False))
            c.bind('<ButtonRelease-1>', lambda e, f=f: f(e, True))
        self.draw()

    @staticmethod
    def _hsv2rgb(hsv):
        h, s, v = hsv[..., 0] * 6, hsv[..., 1], hsv[..., 2]
        i = np.floor(h).astype(int) % 6; f = h - np.floor(h)
        p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
        r = np.choose(i, [v, q, p, p, t, v]); g = np.choose(i, [t, v, v, q, p, p]); b = np.choose(i, [p, p, t, v, v, q])
        return (np.stack([r, g, b], -1) * 255).astype(np.uint8)

    def color(self):
        r, g, b = colorsys.hsv_to_rgb(self.h, self.s, self.v)
        return '#%02x%02x%02x' % (int(r * 255 + .5), int(g * 255 + .5), int(b * 255 + .5))

    def draw(self):
        x = np.linspace(0, 1, self.W)[None, :].repeat(self.H, 0); y = np.linspace(1, 0, self.H)[:, None].repeat(self.W, 1)
        hsv = np.stack([np.full_like(x, self.h), x, y], -1)
        self._sv_img = ImageTk.PhotoImage(Image.fromarray(self._hsv2rgb(hsv)))
        self.sv.delete('all'); self.sv.create_image(0, 0, image=self._sv_img, anchor='nw')
        cx, cy = self.s * (self.W - 1), (1 - self.v) * (self.H - 1)
        self.sv.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, outline='#000', width=2)
        self.sv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline='#fff', width=1)
        self.hue.delete('m'); hx = self.h * (self.W - 1)
        self.hue.create_rectangle(hx - 2, 0, hx + 2, 13, outline='#000', width=2, tags='m')
        c = self.color(); self.sw.configure(background=c); self.hex.set(c)

    def set(self, hexc, notify=False, final=False):
        try:
            r, g, b = (int(hexc.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4))
        except ValueError:
            return
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s > 0.001 and v > 0.001: self.h = h
        self.s, self.v = s, v
        self.draw()
        if notify: self.notify(final)

    def notify(self, final):
        c = self.color()
        if final and c not in self.recent[:1]:
            self.recent = [c] + [x for x in self.recent if x != c][:9]
            for w in self.rf.winfo_children(): w.destroy()
            for i, rc in enumerate(self.recent):
                b = tk.Label(self.rf, background=rc, width=2, relief='raised', borderwidth=1, cursor='hand2')
                b.grid(row=0, column=i, padx=1); b.bind('<Button-1>', lambda e, rc=rc: self.set(rc, True, True))
        self.on_change(c, final)

    def sv_mouse(self, e, final):
        self.s = min(1, max(0, e.x / (self.W - 1))); self.v = min(1, max(0, 1 - e.y / (self.H - 1)))
        self.draw(); self.notify(final)

    def hue_mouse(self, e, final):
        self.h = min(0.999, max(0, e.x / (self.W - 1))); self.draw(); self.notify(final)

    def typed(self):
        t = self.hex.get().strip()
        if not t.startswith('#'): t = '#' + t
        if len(t) == 7: self.set(t, True, True)


# ==================================================================== the panel
class PaintPanel(ttk.Frame):
    """Paint / Parts for an imported bike. host: the window (for dialogs and keys), app: the main window."""

    def __init__(self, master, host, app, blend, page='paint'):
        super().__init__(master)
        self.host, self.app, self.blend = host, app, blend
        self.folder = os.path.dirname(os.path.abspath(blend))
        try:
            with open(blend + '.import.json') as f: self.info = json.load(f)
        except (OSError, ValueError):
            self.info = {}
        self.bike = LV.Bike(self.folder)
        self.proj = self.bike.project
        self.mats = self.bike.materials()
        self.mat = self.mats[0] if self.mats else None
        self.page = page
        self.active = False
        self.dirty = False                   # changes not yet built into the .blend
        self.cam = Cam()
        self.spot = None; self.picked = []; self.sel = None
        self.hist, self.redo_ = [], []
        self._masks, self._base_prev, self._wire, self._prev, self._below = {}, {}, {}, {}, {}
        self._comp_jobs = {}; self._save_job = None
        self._v2 = {'zoom': None, 'pan': [0.0, 0.0]}
        self._d3 = None
        self.fonts = font_list()
        self.tool = tk.StringVar(value='move')
        self.cur_color = '#d62828'
        self.build_ui()
        self.bind_keys()
        self.after(150, self.start)

    # ================================================================ layout
    def build_ui(self):
        top = ttk.Frame(self, padding=(6, 4)); top.pack(fill='x')
        self.toolbar = top
        self.tools = ttk.Frame(top); self.tools.pack(side='left')
        for val, txt in (('move', '✥ Move  (V)'), ('bucket', '◉ Colour bucket  (B)'), ('eyedrop', '✎ Eyedropper  (I)')):
            ttk.Radiobutton(self.tools, text=txt, value=val, variable=self.tool, style='Toolbutton',
                            command=self.tool_changed).pack(side='left', padx=1)
        ttk.Separator(self.tools, orient='vertical').pack(side='left', fill='y', padx=6)
        mb = ttk.Menubutton(self.tools, text='＋ Shape ▾'); m = tk.Menu(mb, tearoff=False)
        for s in LV.SHAPES: m.add_command(label=s, command=lambda s=s: self.add_layer('shape', shape=s))
        mb['menu'] = m; mb.pack(side='left', padx=1)
        ttk.Button(self.tools, text='＋ Logo / PNG…', command=self.add_image).pack(side='left', padx=1)
        ttk.Button(self.tools, text='＋ Text…', command=self.add_text).pack(side='left', padx=1)
        ttk.Separator(self.tools, orient='vertical').pack(side='left', fill='y', padx=6)
        ttk.Label(self.tools, text='Texture').pack(side='left')
        self.mat_var = tk.StringVar(value=self.mat or '')
        self.mat_cb = ttk.Combobox(self.tools, textvariable=self.mat_var, values=self.mats, state='readonly', width=14)
        self.mat_cb.pack(side='left', padx=3); self.mat_cb.bind('<<ComboboxSelected>>', lambda e: self.set_mat(self.mat_var.get()))
        mb = ttk.Menubutton(self.tools, text='Start from ▾'); m = tk.Menu(mb, tearoff=False)
        m.add_command(label='An MX Bikes paint (.pnt)…', command=self.use_pnt)
        m.add_command(label='One plain colour (the current colour)', command=self.plain_texture)
        m.add_command(label="The bike's own texture", command=self.own_texture)
        mb['menu'] = m; mb.pack(side='left', padx=1)
        right = ttk.Frame(top); right.pack(side='right')
        ttk.Button(right, text='Undo', command=self.undo, width=6).pack(side='left')
        ttk.Button(right, text='Redo', command=self.redo, width=6).pack(side='left', padx=(2, 8))
        self.apply_btn = ttk.Button(right, text='Apply to the model', command=self.build_bike)
        self.apply_btn.pack(side='left')
        self.status = tk.StringVar(value='Click the bike to pick a spot.')
        sb = ttk.Label(self, textvariable=self.status, foreground='#555', padding=(8, 0)); sb.pack(fill='x')

        body = ttk.Frame(self); body.pack(fill='both', expand=True)
        self.body = body
        self.left = ttk.Frame(body, padding=6, width=270); self.left.pack(side='left', fill='y'); self.left.pack_propagate(False)
        self.right = ttk.Frame(body, padding=6, width=262); self.right.pack(side='right', fill='y'); self.right.pack_propagate(False)
        self.paint_left = ttk.Frame(self.left); self.parts_left = ttk.Frame(self.left)
        self.build_paint_left(self.paint_left); self.build_parts_left(self.parts_left)
        self.build_props(self.right)

        views = ttk.PanedWindow(body, orient='horizontal'); views.pack(side='left', fill='both', expand=True)
        self.views = views
        self.v3 = ttk.Frame(views); self.v2f = ttk.Frame(views)
        views.add(self.v3, weight=3); views.add(self.v2f, weight=1)
        self.gl = None
        try:
            import workshop_gl
            self.gl = workshop_gl.WorkshopGL(self.v3, self)
            self.gl.pack(fill='both', expand=True)
            g = self.gl
            g.bind('<ButtonPress-1>', self.v3_down); g.bind('<B1-Motion>', self.v3_drag); g.bind('<ButtonRelease-1>', self.v3_up)
            g.bind('<Motion>', self.v3_hover)
            for b in ('<ButtonPress-2>', '<ButtonPress-3>'): g.bind(b, self.v3_pan0)
            for b in ('<B2-Motion>', '<B3-Motion>'): g.bind(b, self.v3_pan)
            g.bind('<MouseWheel>', lambda e: self.v3_wheel(e, 1 if e.delta > 0 else -1))
            g.bind('<Button-4>', lambda e: self.v3_wheel(e, 1)); g.bind('<Button-5>', lambda e: self.v3_wheel(e, -1))
        except Exception as e:
            ttk.Label(self.v3, text='3D view unavailable (%s).\nInstall PyOpenGL and pyopengltk, or paint on the '
                                    'texture on the right.' % e, wraplength=500).pack(expand=True)
        vb = ttk.Frame(self.v3); vb.pack(side='bottom', fill='x')
        for t, y, p in (('Left', -90, 8), ('Right', 90, 8), ('Front', 0, 8), ('Back', 180, 8), ('Top', -90, 89)):
            ttk.Button(vb, text=t, width=6, command=lambda y=y, p=p: self.set_view(y, p)).pack(side='left', padx=1)
        ttk.Button(vb, text='Fit', width=5, command=self.fit3d).pack(side='left', padx=1)
        bar = ttk.Frame(self.v2f); bar.pack(side='top', fill='x')
        ttk.Label(bar, text='The texture', font=('Segoe UI', 9, 'bold')).pack(side='left')
        self.show_wire = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='layout lines', variable=self.show_wire, command=self.draw2d).pack(side='left', padx=6)
        ttk.Button(bar, text='Fit', command=self.fit2d, width=5).pack(side='right')
        self.c2 = tk.Canvas(self.v2f, background='#2a2c30', highlightthickness=0, cursor='crosshair')
        self.c2.pack(fill='both', expand=True)
        self.c2.bind('<Configure>', lambda e: self.draw2d())
        self.c2.bind('<ButtonPress-1>', self.v2_down); self.c2.bind('<B1-Motion>', self.v2_drag)
        self.c2.bind('<ButtonRelease-1>', self.v2_up)
        for b in ('<ButtonPress-2>', '<ButtonPress-3>'): self.c2.bind(b, self.v2_pan0)
        for b in ('<B2-Motion>', '<B3-Motion>'): self.c2.bind(b, self.v2_pan)
        self.c2.bind('<MouseWheel>', lambda e: self.v2_zoomby(1.15 if e.delta > 0 else 1 / 1.15, e.x, e.y))
        self.c2.bind('<Button-4>', lambda e: self.v2_zoomby(1.15, e.x, e.y))
        self.c2.bind('<Button-5>', lambda e: self.v2_zoomby(1 / 1.15, e.x, e.y))
        self.after(300, self.setup_dnd)
        self.after(400, self._sash)
        self.set_page(self.page)

    def _sash(self):
        try:
            w = self.views.winfo_width()
            if w > 100: self.views.sashpos(0, int(w * 0.74))
            else: self.after(300, self._sash)
        except tk.TclError:
            pass

    def build_paint_left(self, f):
        ttk.Label(f, text='Designs (top = in front)', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.ltree = ttk.Treeview(f, columns=('vis',), show='tree', height=18, selectmode='browse')
        self.ltree.column('#0', width=200); self.ltree.column('vis', width=40, anchor='center')
        self.ltree.pack(fill='both', expand=True)
        self.ltree.bind('<<TreeviewSelect>>', self.on_layer_pick)
        self.ltree.bind('<Double-1>', lambda e: self.toggle_vis())
        g = ttk.Frame(f); g.pack(fill='x', pady=(4, 0))
        for i, (t, c) in enumerate((('▲ Forward', lambda: self.move_layer(1)), ('▼ Back', lambda: self.move_layer(-1)),
                                    ('Duplicate', self.dup_layer), ('Copy to other side', self.other_side),
                                    ('Show / hide', self.toggle_vis), ('Delete', self.del_layer))):
            ttk.Button(g, text=t, command=c).grid(row=i // 2, column=i % 2, sticky='ew', padx=1, pady=1)
        g.columnconfigure(0, weight=1); g.columnconfigure(1, weight=1)
        r = ttk.LabelFrame(f, text='Clicking / bucket picks', padding=4); r.pack(fill='x', pady=(8, 0))
        self.pick_kind = tk.StringVar(value='p')
        ttk.Radiobutton(r, text='a whole piece', value='p', variable=self.pick_kind).pack(anchor='w')
        ttk.Radiobutton(r, text='one panel (the patch of texture)', value='u', variable=self.pick_kind).pack(anchor='w')
        self.bucket_detail = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="Bucket keeps the texture's detail", variable=self.bucket_detail).pack(anchor='w')

    def build_parts_left(self, f):
        ttk.Label(f, text='Click pieces on the bike (Shift+click for more).', font=('Segoe UI', 9, 'bold'),
                  wraplength=250).pack(anchor='w')
        self.pick_lbl = ttk.Label(f, text='Nothing picked.', foreground='#666'); self.pick_lbl.pack(anchor='w', pady=(2, 4))
        r = ttk.Frame(f); r.pack(fill='x')
        ttk.Button(r, text='Delete picked pieces', command=self.delete_picked).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Clear', command=self.clear_picked).pack(side='left', padx=(4, 0))
        ttk.Label(f, text='Deleted pieces', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 2))
        self.dlist = tk.Listbox(f, height=6, selectmode='extended'); self.dlist.pack(fill='x')
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        ttk.Button(r, text='Restore selected', command=self.restore_sel).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Restore all', command=self.restore_all).pack(side='left', padx=(4, 0))
        ttk.Label(f, text='Added parts', font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(12, 2))
        self.alist = tk.Listbox(f, height=5, exportselection=False); self.alist.pack(fill='x')
        self.alist.bind('<<ListboxSelect>>', lambda e: self.fill_added())
        r = ttk.Frame(f); r.pack(fill='x', pady=(2, 0))
        ttk.Button(r, text='Add a part…', command=self.add_part).pack(side='left', fill='x', expand=True)
        ttk.Button(r, text='Remove', command=self.remove_part).pack(side='left', padx=(4, 0))
        self.af = ttk.Frame(f); self.af.pack(fill='x', pady=(6, 0))
        ttk.Label(self.af, text='Moves with').grid(row=0, column=0, sticky='w')
        self.a_unit = tk.StringVar()
        self.a_unit_cb = ttk.Combobox(self.af, textvariable=self.a_unit, state='readonly', width=18,
                                      values=['frame', 'handlebars.top', 'handlebars.bottom', 'swingarm',
                                              '(automatic, by where it is)'])
        self.a_unit_cb.grid(row=0, column=1, columnspan=3, sticky='ew', pady=1)
        self.a_unit_cb.bind('<<ComboboxSelected>>', lambda e: self.added_changed())
        self.a_vars = {}
        rows = [('Move X cm', 't', 0, 1.0), ('Move Y cm', 't', 1, 1.0), ('Move Z cm', 't', 2, 1.0),
                ('Turn X°', 'r', 0, 5.0), ('Turn Y°', 'r', 1, 5.0), ('Turn Z°', 'r', 2, 5.0), ('Size %', 's', 0, 2.0)]
        for i, (lab, k, ax, st) in enumerate(rows, start=1):
            ttk.Label(self.af, text=lab).grid(row=i, column=0, sticky='w')
            ttk.Button(self.af, text='−', width=3, command=lambda k=k, ax=ax, st=st: self.nudge_added(k, ax, -st)).grid(row=i, column=1)
            v = tk.StringVar(); e = ttk.Entry(self.af, textvariable=v, width=7, justify='right'); e.grid(row=i, column=2, padx=2)
            e.bind('<Return>', lambda ev, k=k, ax=ax, v=v: self.typed_added(k, ax, v))
            e.bind('<FocusOut>', lambda ev, k=k, ax=ax, v=v: self.typed_added(k, ax, v))
            ttk.Button(self.af, text='+', width=3, command=lambda k=k, ax=ax, st=st: self.nudge_added(k, ax, st)).grid(row=i, column=3)
            self.a_vars[(k, ax)] = v

    def build_props(self, f):
        ttk.Label(f, text='Colour', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.picker = ColorPicker(f, self.color_changed); self.picker.pack(anchor='w')
        self.picker.set(self.cur_color)
        ttk.Separator(f).pack(fill='x', pady=6)
        ttk.Label(f, text='Selected design', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        self.p_info = ttk.Label(f, text='', foreground='#666', wraplength=245); self.p_info.pack(anchor='w')
        self.pv, self.pw = {}, {}
        holder = ttk.Frame(f); holder.pack(fill='x')

        def row(label):
            r = ttk.Frame(holder); ttk.Label(r, text=label, width=10).pack(side='left'); return r
        r = row('Name'); self.pv['name'] = tk.StringVar()
        e = ttk.Entry(r, textvariable=self.pv['name']); e.pack(side='left', fill='x', expand=True)
        e.bind('<Return>', lambda ev: self.set_prop('name', self.pv['name'].get()))
        e.bind('<FocusOut>', lambda ev: self.set_prop('name', self.pv['name'].get())); self.pw['name'] = r
        for key, label, lo, hi in (('opacity', 'Opacity %', 0, 100), ('size', 'Size %', 0.3, 80), ('w', 'Width %', 0.2, 100),
                                   ('h', 'Height %', 0.2, 100), ('rot', 'Turn °', -180, 180), ('outline', 'Outline', 0, 45)):
            r = row(label); v = tk.DoubleVar(); self.pv[key] = v
            s = ttk.Scale(r, from_=lo, to=hi, variable=v, command=lambda val, k=key: self.slide(k))
            s.pack(side='left', fill='x', expand=True)
            s.bind('<ButtonPress-1>', lambda e: self.snapshot()); s.bind('<ButtonRelease-1>', lambda e: self.save_soon())
            e = ttk.Entry(r, width=5, justify='right'); e.pack(side='left', padx=(3, 0))
            e.bind('<Return>', lambda ev, k=key, e=e: self.typed(k, e.get())); self.pw[key] = r; self.pw[key + '_e'] = e
        r = ttk.Frame(holder)
        self.pv['tint'] = tk.BooleanVar()
        ttk.Checkbutton(r, text="Keep the texture's detail (tint)", variable=self.pv['tint'],
                        command=lambda: self.set_prop('blend', 'tint' if self.pv['tint'].get() else 'normal')).pack(anchor='w')
        self.pv['clip'] = tk.BooleanVar()
        ttk.Checkbutton(r, text='Stay on its piece', variable=self.pv['clip'], command=self.toggle_clip).pack(anchor='w')
        self.pv['flip'] = tk.BooleanVar()
        ttk.Checkbutton(r, text='Mirror it', variable=self.pv['flip'],
                        command=lambda: self.set_prop('flip', self.pv['flip'].get())).pack(anchor='w')
        self.pw['opts'] = r
        r = row('Shape'); self.pv['shape'] = tk.StringVar()
        cb = ttk.Combobox(r, textvariable=self.pv['shape'], values=LV.SHAPES, state='readonly')
        cb.pack(side='left', fill='x', expand=True); cb.bind('<<ComboboxSelected>>', lambda e: self.set_prop('shape', self.pv['shape'].get()))
        self.pw['shape'] = r
        r = row('Text'); self.pv['text'] = tk.StringVar()
        e = ttk.Entry(r, textvariable=self.pv['text']); e.pack(side='left', fill='x', expand=True)
        e.bind('<Return>', lambda ev: self.set_prop('text', self.pv['text'].get()))
        e.bind('<FocusOut>', lambda ev: self.set_prop('text', self.pv['text'].get())); self.pw['text'] = r
        r = row('Font'); self.pv['font'] = tk.StringVar()
        cb = ttk.Combobox(r, textvariable=self.pv['font'], values=['(standard)'] + list(self.fonts), state='readonly')
        cb.pack(side='left', fill='x', expand=True)
        cb.bind('<<ComboboxSelected>>', lambda e: self.set_prop('font', self.fonts.get(self.pv['font'].get(), '')))
        self.pw['font'] = r
        r = ttk.Frame(holder)
        ttk.Button(r, text='Change image…', command=self.change_image).pack(side='left')
        self.pv['recolor'] = tk.BooleanVar()
        ttk.Checkbutton(r, text='One colour', variable=self.pv['recolor'],
                        command=lambda: self.set_prop('recolor', self.pv['recolor'].get())).pack(side='left', padx=6)
        self.pw['image'] = r
        r = ttk.Frame(holder)
        self.fill_lbl = ttk.Label(r, text='', foreground='#666', wraplength=245); self.fill_lbl.pack(anchor='w')
        r2 = ttk.Frame(r); r2.pack(fill='x')
        ttk.Button(r2, text='+ picked piece', command=lambda: self.fill_targets(True)).pack(side='left')
        ttk.Button(r2, text='− picked piece', command=lambda: self.fill_targets(False)).pack(side='left', padx=4)
        self.pv['whole'] = tk.BooleanVar()
        ttk.Checkbutton(r, text='The whole texture', variable=self.pv['whole'],
                        command=lambda: self.set_prop('whole', self.pv['whole'].get())).pack(anchor='w')
        self.pw['fill'] = r

    def set_page(self, page):
        self.page = page
        for w in (self.paint_left, self.parts_left): w.pack_forget()
        (self.paint_left if page == 'paint' else self.parts_left).pack(fill='both', expand=True)
        if page == 'paint':
            self.tools.pack(side='left')
            if not self.right.winfo_ismapped(): self.right.pack(side='right', fill='y', before=self.views)
        else:
            self.tools.pack_forget(); self.right.pack_forget()
        self.status.set('Click the bike to pick a spot, then add a shape, logo or text - or pick the colour bucket.'
                        if page == 'paint' else 'Click pieces of the bike to delete them, or add a part.')
        self.update_sel3d(); self.fill_props()
        if self.gl: self.gl.request()

    def setup_dnd(self):
        try:
            import tkinterdnd2
            tkinterdnd2.TkinterDnD._require(self._root())
            for w in [x for x in (self.gl, self.c2) if x is not None]:
                w.drop_target_register(tkinterdnd2.DND_FILES); w.dnd_bind('<<Drop>>', self.on_drop)
        except Exception:
            pass

    def on_drop(self, e):
        try: files = self.tk.splitlist(e.data)
        except Exception: files = [e.data]
        f = files[0] if files else ''
        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tga')):
            if self.gl is not None and e.widget is self.gl:
                x, y = e.x_root - self.gl.winfo_rootx(), e.y_root - self.gl.winfo_rooty()
                h = self.hit(x, y)
                if h is not None: self.spot = h
            self.after(10, lambda: self.add_image(f))
        return getattr(e, 'action', 'copy')

    # ================================================================ keys (only while this panel is shown)
    def bind_keys(self):
        def k(f):
            def g(e):
                if not self.active or isinstance(self.focus_get(), (tk.Entry, ttk.Entry, ttk.Combobox)): return
                f(e); return 'break'
            return g
        H = self.host
        H.bind('<Control-z>', k(lambda e: self.undo()), add='+'); H.bind('<Control-y>', k(lambda e: self.redo()), add='+')
        H.bind('<Delete>', k(self.key_delete), add='+')
        H.bind('<Control-d>', k(lambda e: self.dup_layer()), add='+')
        for key, tool in (('v', 'move'), ('b', 'bucket'), ('i', 'eyedrop')):
            H.bind('<Key-%s>' % key, k(lambda e, t=tool: (self.tool.set(t), self.tool_changed())), add='+')
        for key, dx, dy in (('Left', -1, 0), ('Right', 1, 0), ('Up', 0, -1), ('Down', 0, 1)):
            H.bind('<%s>' % key, k(lambda e, dx=dx, dy=dy: self.nudge_layer(dx, dy, e)), add='+')
        H.bind('<bracketleft>', k(lambda e: self.turn_layer(-5 if not e.state & 1 else -15)), add='+')
        H.bind('<bracketright>', k(lambda e: self.turn_layer(5 if not e.state & 1 else 15)), add='+')
        for key, f_ in (('plus', 1.05), ('equal', 1.05), ('minus', 1 / 1.05)):
            H.bind('<%s>' % key, k(lambda e, f_=f_: self.scale_layer(f_)), add='+')
        H.bind('<Escape>', k(lambda e: self.deselect()), add='+')

    # ================================================================ start / meshes
    def start(self):
        self.status.set('Reading the bike…'); self.update_idletasks()
        self.bike.pieces()
        P = np.concatenate([p['P'] for p in self.bike.parts]) if self.bike.parts else np.zeros((1, 3))
        lo, hi = P.min(0), P.max(0)
        self.cam.center = list((lo + hi) / 2); self.cam.depth = float(np.linalg.norm(hi - lo)) * 2 + 1
        self._size = float(np.linalg.norm(hi - lo))
        self.after(80, self.fit3d)
        self.push_meshes()
        for m in self.mats: self.refresh_tex(m, now=True)
        self.fill_layers(); self.fill_parts_tab(); self.fill_props(); self.fit2d()
        self.status.set('Click the bike to pick a spot, then add a shape, logo or text - or pick the colour bucket.')

    def fit3d(self):
        W = max(200, self.v3.winfo_width()); H = max(200, self.v3.winfo_height())
        self.cam.zoom = 0.85 * min(W, H) / max(getattr(self, '_size', 2.0), 1e-6); self.cam.pan = [0.0, 0.0]
        if self.gl: self.gl.request()

    def set_view(self, yaw, pitch):
        self.cam.yaw, self.cam.pitch = yaw, pitch
        if self.gl: self.gl.request()

    def push_meshes(self):
        if not self.gl: return
        ms = []
        for i, p in enumerate(self.bike.parts):
            keep = ~self.bike.deleted_mask(i)
            ms.append({'P': p['P'], 'N': p['N'], 'U': p['U'], 'F': p['F'][keep], 'mat': p['mat'],
                       'sel': np.zeros(len(p['P']), np.float32)})
        self.gl.set_meshes(ms)
        self.update_sel3d()

    def update_sel3d(self):
        if not self.gl: return
        if self.page == 'parts': keys = list(self.picked)
        else:
            keys = [self.spot['key']] if (self.spot is not None and getattr(self, '_flash', 0) > 0) else []
            L = self.layer()
            if L is not None and L['type'] == 'fill' and getattr(self, '_flash', 0) > 0: keys += list(L.get('targets', []))
        sels = []
        for i, p in enumerate(self.bike.parts):
            s = np.zeros(len(p['P']), np.float32)
            for k in keys:
                try: j, tris = self.bike.piece_tris(k)
                except (ValueError, IndexError): continue
                if j == i: s[np.unique(p['F'][tris])] = 1
            sels.append(s)
        self.gl.set_selection(sels)

    def flash(self):
        self._flash = 1; self.update_sel3d()
        self.after(600, lambda: (setattr(self, '_flash', 0), self.update_sel3d()))

    def spot_point(self):
        return None if (self.spot is None or self.page != 'paint') else self.spot['point']

    def gl_failed(self, why):
        self.status.set('3D view unavailable (%s) - paint on the texture view.' % why[:80])

    # ================================================================ the selected design's handles on the bike
    def gizmo(self):
        """Screen box, corner handles and turn knob of the selected design (3D view), or None."""
        L = self.layer()
        if L is None or L['type'] == 'fill' or self.page != 'paint' or not self.gl: return None
        mat = self.layer_mat(L)
        key = json.dumps([mat, L['x'], L['y'], L['w'], L['h'], L.get('rot'), L.get('flip')], default=LV._plain)
        if getattr(self, '_gz_key', None) != key:
            self._gz_key, self._gz = key, self.bike.decal_frame(mat, L)
        fr = self._gz
        if fr is None: return None
        c, t, s = fr
        W, H = self.gl.winfo_width(), self.gl.winfo_height()
        pts = np.array([c - t + s, c + t + s, c + t - s, c - t - s, c + s, c])
        sx, sy = self.cam.project(pts, W, H)
        poly = list(zip(sx[:4].tolist(), sy[:4].tolist()))
        cx, cy = float(sx[5]), float(sy[5]); tx, ty = float(sx[4]), float(sy[4])
        d = math.hypot(tx - cx, ty - cy) or 1.0
        knob = (tx + (tx - cx) / d * 26, ty + (ty - cy) / d * 26)
        return {'poly': poly, 'handles': poly, 'knob': knob, 'knob_from': (tx, ty), 'centre': (cx, cy)}

    # ================================================================ textures
    def base_prev(self, mat):
        if mat not in self._base_prev:
            b = self.bike.base_texture(mat)
            s = PREV / max(b.size)
            self._base_prev[mat] = b.resize((max(1, int(b.width * s)), max(1, int(b.height * s))), Image.BILINEAR)
        return self._base_prev[mat]

    def composite(self, mat):
        """The painted preview. The designs under the selected one are kept from last time (they don't change
        while you drag it), so moving / sizing a design only redraws it and what is above it."""
        Ls = self.proj['layers'].get(mat, [])
        k = next((i for i, L in enumerate(Ls) if L['id'] == self.sel), len(Ls))
        key = (json.dumps(Ls[:k], default=LV._plain), id(self.base_prev(mat)))
        c = self._below.get(mat)
        if c is not None and c[0] == key:
            img = c[1].copy()
        else:
            img = self.base_prev(mat).copy(); self._draw(img, Ls[:k], mat)
            self._below[mat] = (key, img.copy())
        self._draw(img, Ls[k:], mat)
        return img

    def _draw(self, img, Ls, mat):
        for L in Ls:
            try: LV.draw_layer(img, L, self.bike, mat, self._masks)
            except Exception as e: self.status.set('Could not draw "%s": %s' % (L.get('name'), e))

    def refresh_tex(self, mat=None, now=False):
        mat = mat or self.mat
        if mat is None: return
        if not now:
            if self._comp_jobs.get(mat): return
            self._comp_jobs[mat] = self.after(16, lambda: self.refresh_tex(mat, True)); return
        job = self._comp_jobs.pop(mat, None)
        if job:
            try: self.after_cancel(job)
            except Exception: pass
        img = self.composite(mat)
        self._prev[mat] = img
        if self.gl: self.gl.set_texture(mat, img)
        if mat == self.mat: self.draw2d()

    def set_mat(self, mat):
        if mat not in self.mats: return
        self.mat = mat; self.mat_var.set(mat)
        self.fill_layers(); self.fill_props(); self.fit2d()

    def use_pnt(self):
        import pnt_io
        src = self.info.get('source') or ''
        items = pnt_io.find_paints(src) if src.lower().endswith('.edf') else []
        labels = [l for l, _, _ in items] + ['Other .pnt file…']
        pick = self.app.choose('MX Bikes paint', 'Paints for %s (protected ones can\'t be used):' %
                               (self.info.get('bike') or 'this bike'), labels)
        if not pick: return
        if pick == labels[-1]:
            path = filedialog.askopenfilename(parent=self.host, title='MX Bikes paint', filetypes=[('MX Bikes paint', '*.pnt')])
            if not path: return
        else:
            path = items[labels.index(pick)][1]
        try:
            idx = pnt_io.index(path)
        except pnt_io.Protected as e:
            messagebox.showinfo('MX Bikes paint', '%s\n\nPick another paint.' % e, parent=self.host); return
        except Exception as e:
            messagebox.showerror('MX Bikes paint', 'Could not read it:\n%s' % e, parent=self.host); return
        cols = pnt_io.colour_textures(idx)
        if not cols:
            messagebox.showerror('MX Bikes paint', 'That paint has no colour texture.', parent=self.host); return
        names = [t['name'] for t in cols]
        target = self.mat
        t = next((t for t in cols if t['name'].lower() == (target or '').lower()), None)
        if t is None:
            hit = next(((t, m) for t in cols for m in self.mats if m.lower() == t['name'].lower()), None)
            if hit: t, target = hit
        if t is None:
            ch = self.app.choose('MX Bikes paint', 'This paint is for "%s" - not a texture of this model (%s). It may '
                                 'be for another version of the bike, and then it won\'t line up. Put which one on "%s" '
                                 'anyway?' % (', '.join(names), ', '.join(self.mats), target), names)
            if not ch: return
            t = cols[names.index(ch)]
        self.snapshot()
        self.proj['base'][target] = {'pnt': path, 'tex': t['name'], 'label': idx['name']}
        self._base_prev.pop(target, None)
        self.status.set('Reading the paint…'); self.update_idletasks()
        self.changed(target)
        self.status.set('Using the paint "%s" on %s.' % (idx['name'], target))

    def plain_texture(self):
        if self.mat is None: return
        size = list(self.bike.base_texture(self.mat).size) if self.bike.has_texture(self.mat) else [2048, 2048]
        self.snapshot()
        self.proj['base'][self.mat] = {'blank': True, 'size': size, 'color': self.cur_color}
        self._base_prev.pop(self.mat, None); self.changed()

    def own_texture(self):
        if self.mat in self.proj['base']:
            self.snapshot(); self.proj['base'].pop(self.mat, None); self._base_prev.pop(self.mat, None); self.changed()

    # ================================================================ colour
    def color_changed(self, c, final):
        self.cur_color = c
        L = self.layer()
        if L is not None:
            if not getattr(self, '_color_snap', False): self.snapshot(); self._color_snap = True
            L['color'] = c; self.dirty = True
            if L['type'] == 'image' and not L.get('recolor'):        # a logo takes the colour as one colour
                L['recolor'] = True; self.pv['recolor'].set(True)
            self.refresh_tex(self.layer_mat(L))
            if final: self._color_snap = False; self.save_soon()
        elif final:
            self.status.set('Colour %s - the bucket and new designs use it.' % c)

    def sample(self, h):
        """Colour of the painted bike at a hit."""
        img = self._prev.get(h['mat'])
        if img is None: return None
        x = min(img.width - 1, max(0, int(h['img'][0] * img.width))); y = min(img.height - 1, max(0, int(h['img'][1] * img.height)))
        r, g, b = img.getpixel((x, y))[:3]
        return '#%02x%02x%02x' % (r, g, b)

    def bucket(self, h):
        key = h['key']; mat = h['mat']
        Ls = self.layers(mat)
        L = next((L for L in reversed(Ls) if L['type'] == 'fill' and L.get('targets') == [key]), None)
        self.snapshot()
        if L is None:
            L = LV.new_layer('fill', targets=[key], color=self.cur_color, blend='tint' if self.bucket_detail.get() else 'normal')
            L['name'] = 'Colour %s' % ('piece' if key[0] == 'p' else 'panel')
            k = next((i for i, x in enumerate(Ls) if x['type'] != 'fill'), len(Ls))   # under the designs
            Ls.insert(k, L)
        else:
            L['color'] = self.cur_color; L['blend'] = 'tint' if self.bucket_detail.get() else 'normal'
        if mat != self.mat: self.set_mat(mat)
        self.changed(mat); self.fill_layers()
        self.status.set('Painted the %s %s. Click more pieces, or pick another colour.' % (
            'piece' if key[0] == 'p' else 'panel', self.cur_color))

    def tool_changed(self):
        t = self.tool.get()
        if self.gl: self.gl.configure(cursor={'move': 'arrow', 'bucket': 'spraycan', 'eyedrop': 'tcross'}.get(t, 'arrow'))
        self.status.set({'move': 'Move: drag designs on the bike; click to pick a spot.',
                         'bucket': 'Colour bucket: click a piece of the bike to paint it the current colour.',
                         'eyedrop': 'Eyedropper: click the bike (or the texture) to take its colour.'}[t])

    # ================================================================ layers
    def layers(self, mat=None):
        return self.proj['layers'].setdefault(mat or self.mat, [])

    def layer(self):
        if self.sel is None: return None
        for m, Ls in self.proj['layers'].items():
            for L in Ls:
                if L['id'] == self.sel: return L
        return None

    def layer_mat(self, L):
        return next((m for m, Ls in self.proj['layers'].items() if L in Ls), self.mat)

    def select(self, L):
        self.sel = L['id'] if L else None
        if L is not None:
            m = self.layer_mat(L)
            if m != self.mat: self.set_mat(m)
            if L.get('color') and L['type'] != 'image': self.picker.set(L['color'])
            self.status.set(HINT_SEL if L['type'] != 'fill' else 'Colour: change it with the colour square.')
        self.fill_layers(); self.fill_props(); self.draw2d(); self.update_sel3d()
        if self.gl: self.gl.request()

    def deselect(self):
        self.select(None)

    def fill_layers(self):
        self.ltree.delete(*self.ltree.get_children())
        for L in reversed(self.layers()):
            icon = {'shape': '◆', 'image': '▣', 'text': 'T', 'fill': '●'}.get(L['type'], '·')
            self.ltree.insert('', 'end', iid=L['id'], text='%s  %s' % (icon, L['name']),
                              values=('' if L.get('visible', True) else 'hidden',))
        if self.sel and self.ltree.exists(self.sel):
            self._quiet = True; self.ltree.selection_set(self.sel); self.ltree.see(self.sel); self._quiet = False

    def on_layer_pick(self, _e=None):
        if getattr(self, '_quiet', False): return
        s = self.ltree.selection()
        L = next((L for L in self.layers() if s and L['id'] == s[0]), None)
        if (L['id'] if L else None) != self.sel: self.select(L)

    def spot_xy(self):
        if self.spot and self.spot['mat'] == self.mat: return self.spot['img']
        return self.c2t(self.c2.winfo_width() / 2, self.c2.winfo_height() / 2)

    def add_layer(self, kind, **kw):
        if self.mat is None: return
        if self.spot and self.spot['mat'] != self.mat: self.set_mat(self.spot['mat'])
        x, y = self.spot_xy()
        kw.setdefault('color', self.cur_color)
        L = LV.new_layer(kind, x=float(x), y=float(y), **kw)
        if kind == 'shape': L['name'] = kw.get('shape', 'Shape')
        if kind == 'shape' and kw.get('shape') == 'Stripe': L.update(w=0.25, h=0.02)
        if self.spot and self.spot['mat'] == self.mat and kind != 'fill': L['clip'] = [self.spot['key']]
        self.snapshot()
        self.layers().append(L)
        self.changed(); self.select(L)
        if not self.spot: self.status.set('Tip: click the bike first to put new designs there. ' + HINT_SEL)
        return L

    def add_image(self, path=None):
        p = path or filedialog.askopenfilename(parent=self.host, title='Logo or image', filetypes=[
            ('Images', '*.png *.jpg *.jpeg *.webp *.bmp *.tga'), ('All', '*')])
        if not p: return
        try:
            im = Image.open(p); w, h = im.size
        except Exception as e:
            messagebox.showerror('Add a logo', 'Could not open it:\n%s' % e, parent=self.host); return
        rel = LV.copy_into(self.folder, 'decals', p)
        ww = 0.1; hh = ww * h / max(1, w)
        self.add_layer('image', image=rel, w=ww, h=hh, name=os.path.splitext(os.path.basename(p))[0], color='#ffffff')

    def add_text(self):
        t = simpledialog.askstring('Add text', 'Text:', parent=self.host, initialvalue='')
        if not t: return
        self.add_layer('text', text=t, name='"%s"' % t[:20], w=0.03 * max(3, len(t)), h=0.045)

    def move_layer(self, d):
        L = self.layer()
        if L is None: return
        Ls = self.layers(self.layer_mat(L)); i = Ls.index(L); j = i + d
        if not 0 <= j < len(Ls): return
        self.snapshot(); Ls[i], Ls[j] = Ls[j], Ls[i]; self.changed(); self.fill_layers()

    def dup_layer(self):
        L = self.layer()
        if L is None: return
        M = copy.deepcopy(L); M['id'] = LV.new_layer('shape')['id']; M['name'] = L['name'] + ' copy'
        M['x'] = min(1.0, L['x'] + 0.01); M['y'] = min(1.0, L['y'] + 0.01)
        self.snapshot(); Ls = self.layers(self.layer_mat(L)); Ls.insert(Ls.index(L) + 1, M)
        self.changed(); self.select(M)

    def other_side(self):
        L = self.layer()
        if L is None or L['type'] == 'fill':
            self.status.set('Select a shape, logo or text first.'); return
        mat = self.layer_mat(L)
        M = self.bike.mirror_layer(mat, L)
        if M is None:
            self.status.set("Couldn't find the matching spot on the other side (is the design on a piece with a twin?)"); return
        tm = M.pop('_mat', mat)
        self.snapshot(); self.layers(tm).append(M)
        self.changed(tm); self.select(M)
        self.status.set('Copied to the other side - fine-tune it if the two sides differ.')

    def toggle_vis(self):
        L = self.layer()
        if L is None: return
        self.snapshot(); L['visible'] = not L.get('visible', True); self.changed(self.layer_mat(L)); self.fill_layers()

    def del_layer(self):
        L = self.layer()
        if L is None: return
        m = self.layer_mat(L)
        self.snapshot(); self.layers(m).remove(L); self.sel = None
        self.changed(m); self.select(None)

    def key_delete(self, e):
        if self.page == 'parts': self.delete_picked()
        else: self.del_layer()

    def nudge_layer(self, dx, dy, e):
        L = self.layer()
        if L is None or L['type'] == 'fill': return
        st = 0.01 if e.state & 1 else 0.002
        self.snapshot(); L['x'] += dx * st; L['y'] += dy * st; self.changed(self.layer_mat(L)); self.fill_props()

    def turn_layer(self, deg):
        L = self.layer()
        if L is None or L['type'] == 'fill': return
        self.snapshot(); L['rot'] = (L.get('rot', 0) + deg + 180) % 360 - 180; self.changed(self.layer_mat(L)); self.fill_props()

    def scale_layer(self, f, snap=True):
        L = self.layer()
        if L is None or L['type'] == 'fill': return
        if snap: self.snapshot()
        L['w'] = max(0.002, min(2.0, L['w'] * f)); L['h'] = max(0.002, min(2.0, L['h'] * f))
        self.changed(self.layer_mat(L)); self.fill_props()

    # ================================================================ properties
    def fill_props(self):
        L = self.layer() if self.page == 'paint' else None
        for k, w in self.pw.items():
            if not k.endswith('_e'): w.pack_forget()
        if L is None:
            self.p_info.configure(text='Click a design on the bike or in the list. With nothing selected, the colour '
                                       'square sets the colour for the bucket and new designs.')
            return
        t = L['type']
        show = ['name', 'opacity']
        if t != 'fill': show += ['size', 'w', 'h', 'rot', 'opts']
        if t == 'shape': show += ['shape', 'outline']
        if t == 'text': show += ['text', 'font']
        if t == 'image': show += ['image']
        if t == 'fill': show += ['opts', 'fill']
        for k in ('name', 'opacity', 'size', 'w', 'h', 'rot', 'outline', 'opts', 'shape', 'text', 'font', 'image', 'fill'):
            if k in show: self.pw[k].pack(fill='x', pady=1)
        self.p_info.configure(text={'shape': 'Shape', 'image': 'Logo / image', 'text': 'Text',
                                    'fill': 'Colour of pieces'}[t] + ' (on %s)' % self.layer_mat(L))
        self._loading = True
        self.pv['name'].set(L['name']); self.pv['opacity'].set(L.get('opacity', 1) * 100)
        self.pv['size'].set(L.get('w', 0.1) * 100)
        for k in ('w', 'h'): self.pv[k].set(L.get(k, 0.1) * 100)
        self.pv['rot'].set(L.get('rot', 0)); self.pv['outline'].set(float(L.get('outline') or 0) * 100)
        for k in ('opacity', 'size', 'w', 'h', 'rot', 'outline'):
            e = self.pw[k + '_e']; e.delete(0, 'end'); e.insert(0, '%.1f' % self.pv[k].get())
        self.pv['tint'].set(L.get('blend') == 'tint'); self.pv['clip'].set(bool(L.get('clip')))
        self.pv['flip'].set(bool(L.get('flip'))); self.pv['shape'].set(L.get('shape', ''))
        self.pv['text'].set(L.get('text', '')); self.pv['recolor'].set(bool(L.get('recolor')))
        fn = next((n for n, p in self.fonts.items() if p == L.get('font')), '(standard)'); self.pv['font'].set(fn)
        self.pv['whole'].set(bool(L.get('whole')))
        if t == 'fill':
            self.fill_lbl.configure(text='%d piece(s) / panel(s). Click another piece on the bike and press '
                                         '"+ picked piece" to add it (or use the bucket).' % len(L.get('targets', [])))
        self._loading = False

    def set_prop(self, k, v, record=True):
        L = self.layer()
        if L is None or getattr(self, '_loading', False): return
        if L.get(k) == v: return
        if record: self.snapshot()
        L[k] = v
        if k == 'name': self.fill_layers()
        self.changed(self.layer_mat(L))

    def slide(self, k):
        if getattr(self, '_loading', False): return
        L = self.layer()
        if L is None: return
        v = self.pv[k].get()
        self.apply_value(L, k, v)
        e = self.pw[k + '_e']; e.delete(0, 'end'); e.insert(0, '%.1f' % v)

    def apply_value(self, L, k, v):
        if k == 'opacity': L['opacity'] = max(0.0, min(1.0, v / 100))
        elif k == 'size':
            f = max(0.002, v / 100) / max(1e-6, L['w']); L['w'] *= f; L['h'] = max(0.002, L['h'] * f)
            self._loading = True; self.pv['w'].set(L['w'] * 100); self.pv['h'].set(L['h'] * 100); self._loading = False
        elif k in ('w', 'h'): L[k] = max(0.002, v / 100)
        elif k == 'rot': L['rot'] = v
        elif k == 'outline': L['outline'] = max(0.0, v / 100)
        self.changed(self.layer_mat(L))

    def typed(self, k, txt):
        try: v = float(txt.replace(',', '.'))
        except ValueError: return
        L = self.layer()
        if L is None: return
        self.snapshot(); self.apply_value(L, k, v); self.fill_props()

    def toggle_clip(self):
        L = self.layer()
        if L is None: return
        if self.pv['clip'].get():
            hit = self.bike.surface_at(self.layer_mat(L), L['x'], L['y'])
            if hit is None:
                self.pv['clip'].set(False); self.status.set('Move the design onto a piece first.'); return
            self.set_prop('clip', [self.bike.key_at(hit[0], hit[1], self.pick_kind.get())])
        else:
            self.set_prop('clip', [])

    def change_image(self):
        L = self.layer()
        if L is None or L['type'] != 'image': return
        p = filedialog.askopenfilename(parent=self.host, title='Logo or image', filetypes=[
            ('Images', '*.png *.jpg *.jpeg *.webp *.bmp *.tga'), ('All', '*')])
        if p: self.set_prop('image', LV.copy_into(self.folder, 'decals', p))

    def fill_targets(self, add):
        L = self.layer()
        if L is None or L['type'] != 'fill' or not self.spot: return
        k = self.spot['key']; t = list(L.get('targets', []))
        if add and k not in t:
            if self.bike.piece_mat(k) != self.layer_mat(L):
                self.status.set('That piece uses another texture (%s) - use the bucket on it.' % self.bike.piece_mat(k)); return
            t.append(k)
        elif not add and k in t: t.remove(k)
        self.set_prop('targets', t); self.fill_props(); self.update_sel3d()

    # ================================================================ history / saving
    def snapshot(self):
        self.hist.append(json.dumps(self.proj, default=LV._plain)); self.redo_.clear()
        if len(self.hist) > 100: del self.hist[0]

    def _restore(self, s):
        d = json.loads(s)
        self.proj.clear(); self.proj.update(d)
        self._base_prev.clear(); self._below.clear()
        self.bike.sync_added(); self.push_meshes(); self.mats_all()
        for m in self.mats: self.refresh_tex(m, now=True)
        if self.layer() is None: self.sel = None
        self.fill_layers(); self.fill_props(); self.fill_parts_tab(); self.save_soon(); self.dirty = True
        if self.gl: self.gl.request()

    def undo(self):
        if not self.hist: return
        self.redo_.append(json.dumps(self.proj, default=LV._plain)); self._restore(self.hist.pop())

    def redo(self):
        if not self.redo_: return
        self.hist.append(json.dumps(self.proj, default=LV._plain)); self._restore(self.redo_.pop())

    def mats_all(self):
        self.mats = self.bike.materials()
        self.mat_cb.configure(values=self.mats)
        return self.mats

    def changed(self, mat=None):
        self.dirty = True
        self.refresh_tex(mat or self.mat); self.save_soon()
        if self.gl: self.gl.request()

    def save_soon(self):
        if self._save_job: self.after_cancel(self._save_job)
        self._save_job = self.after(800, self.save_now)

    def save_now(self):
        if self._save_job:
            try: self.after_cancel(self._save_job)
            except Exception: pass
        self._save_job = None
        try: LV.save_project(self.folder, self.proj)
        except Exception as e: self.status.set('Could not save the paint job: %s' % e)

    # ================================================================ 3D view mouse
    def hit(self, sx, sy):
        o, d = self.cam.ray(sx, sy, self.gl.winfo_width(), self.gl.winfo_height())
        h = self.bike.ray_hit(o, np.asarray(d, np.float64))
        if h is not None: h['key'] = self.bike.key_at(h['part'], h['tri'], self.pick_kind.get())
        return h

    def gizmo_part(self, x, y):
        gz = self.gizmo()
        if gz is None: return None
        if math.hypot(x - gz['knob'][0], y - gz['knob'][1]) <= 11: return 'rot'
        for hx, hy in gz['handles']:
            if abs(x - hx) <= 8 and abs(y - hy) <= 8: return 'size'
        if _inside(gz['poly'], x, y): return 'move'
        return None

    def v3_hover(self, e):
        if self.page != 'paint' or self.tool.get() != 'move' or self._d3: return
        part = self.gizmo_part(e.x, e.y)
        self.gl.configure(cursor={'rot': 'exchange', 'size': 'sizing', 'move': 'fleur'}.get(part, 'arrow'))

    def v3_down(self, e):
        self.gl.focus_set()
        st = {'x': e.x, 'y': e.y, 'yaw': self.cam.yaw, 'pitch': self.cam.pitch, 'moved': False,
              'shift': bool(e.state & 0x1), 'mode': 'view'}
        if self.page == 'paint' and self.tool.get() == 'move':
            part = self.gizmo_part(e.x, e.y)
            if part is None:
                h = self.hit(e.x, e.y)
                if h is not None:
                    L = self.bike.layer_at(h['mat'], h['img'][0], h['img'][1], self.layers(h['mat']))
                    if L is not None:
                        self.select(L); part = 'move'
            if part is not None:
                L = self.layer(); gz = self.gizmo()
                st.update(mode=part, L0=copy.deepcopy(L))
                if gz is not None:
                    st['c'] = gz['centre']; st['a0'] = math.atan2(e.y - gz['centre'][1], e.x - gz['centre'][0])
                    st['r0'] = math.hypot(e.x - gz['centre'][0], e.y - gz['centre'][1]) or 1.0
                    p = gz['poly']
                    cr = (p[1][0] - p[0][0]) * (p[3][1] - p[0][1]) - (p[1][1] - p[0][1]) * (p[3][0] - p[0][0])
                    st['flip_s'] = -1 if cr > 0 else 1
                elif part != 'move':
                    st['mode'] = 'view'
                self.snapshot()
        self._d3 = st

    def v3_drag(self, e):
        st = self._d3
        if not st: return
        if abs(e.x - st['x']) + abs(e.y - st['y']) > 3: st['moved'] = True
        if not st['moved']: return
        L = self.layer()
        if st['mode'] == 'move' and L is not None:
            h = self.hit(e.x, e.y)
            if h is not None:
                if h['mat'] != self.layer_mat(L):                          # onto a part with another texture
                    old = self.layer_mat(L); self.layers(old).remove(L); self.layers(h['mat']).append(L)
                    self.refresh_tex(old); self.set_mat(h['mat'])
                L['x'], L['y'] = float(h['img'][0]), float(h['img'][1])
                if L.get('clip'): L['clip'] = [self.bike.key_at(h['part'], h['tri'], L['clip'][0][0])]
                self.dirty = True; self.refresh_tex(self.layer_mat(L)); self.gl.request()
            return
        if st['mode'] == 'size' and L is not None:
            r = math.hypot(e.x - st['c'][0], e.y - st['c'][1]) / st['r0']
            o = st['L0']; L['w'] = max(0.002, o['w'] * r); L['h'] = max(0.002, o['h'] * r)
            self.dirty = True; self.refresh_tex(self.layer_mat(L)); self.gl.request(); return
        if st['mode'] == 'rot' and L is not None:
            a = math.atan2(e.y - st['c'][1], e.x - st['c'][0])
            v = st['L0'].get('rot', 0) + math.degrees(a - st['a0']) * st['flip_s']
            if e.state & 0x1: v = round(v / 15) * 15
            L['rot'] = (v + 180) % 360 - 180
            self.dirty = True; self.refresh_tex(self.layer_mat(L)); self.gl.request(); return
        self.cam.yaw = st['yaw'] + (e.x - st['x']) * 0.4
        self.cam.pitch = max(-89.0, min(89.0, st['pitch'] + (e.y - st['y']) * 0.4))
        self.gl.request()

    def v3_up(self, e):
        st = self._d3; self._d3 = None
        if not st: return
        if st['mode'] != 'view':
            if st['moved']: self.fill_props(); self.save_soon()
            elif self.hist: self.hist.pop()
            return
        if st['moved']: return
        h = self.hit(e.x, e.y)
        if self.page == 'parts':
            if h is None:
                if not st['shift']: self.picked = []
            else:
                k = 'p%d:%d' % (h['part'], int(self.bike.pieces()[h['part']][h['tri']]))
                if st['shift']: self.picked = [p for p in self.picked if p != k] if k in self.picked else self.picked + [k]
                else: self.picked = [k]
            self.fill_parts_tab(); self.update_sel3d(); self.gl.request(); return
        tool = self.tool.get()
        if h is None:
            if tool == 'move': self.spot = None; self.deselect()
            return
        if tool == 'eyedrop':
            c = self.sample(h)
            if c:
                self.picker.set(c, notify=True, final=True)
                self.status.set('Took the colour %s%s.' % (c, ' - the selected design has it now' if self.layer() else ''))
            return
        if tool == 'bucket':
            self.spot = h; self.bucket(h); self.flash(); return
        self.spot = h
        if self.layer() is not None: self.sel = None; self.fill_props(); self.fill_layers()
        if h['mat'] != self.mat: self.set_mat(h['mat'])
        self.flash(); self.draw2d(); self.gl.request()
        self.status.set('Picked a %s on %s - add a shape / logo / text here, or use the bucket.' % (
            'piece' if h['key'][0] == 'p' else 'panel', self.bike.parts[h['part']]['name']))

    def v3_pan0(self, e):
        self._p3 = (e.x, e.y, list(self.cam.pan))

    def v3_pan(self, e):
        x0, y0, p0 = self._p3
        self.cam.pan = [p0[0] - (e.x - x0) / self.cam.zoom, p0[1] + (e.y - y0) / self.cam.zoom]
        self.gl.request()

    def v3_wheel(self, e, d):
        L = self.layer()
        if L is not None and L['type'] != 'fill' and self.page == 'paint' and not (e.state & 0x4):
            if not getattr(self, '_wheel_snap', False):
                self.snapshot(); self._wheel_snap = True
                self.after(700, lambda: setattr(self, '_wheel_snap', False))
            if e.state & 0x1: L['rot'] = (L.get('rot', 0) + 5 * d + 180) % 360 - 180
            else: L['w'] = max(0.002, min(2.0, L['w'] * (1.06 if d > 0 else 1 / 1.06))); L['h'] = max(0.002, min(2.0, L['h'] * (1.06 if d > 0 else 1 / 1.06)))
            self.changed(self.layer_mat(L)); self.fill_props(); self.gl.request(); return
        f = 1.15 if d > 0 else 1 / 1.15
        W, H = self.gl.winfo_width(), self.gl.winfo_height()
        ax = (e.x - W / 2) / self.cam.zoom + self.cam.pan[0]; ay = -(e.y - H / 2) / self.cam.zoom + self.cam.pan[1]
        self.cam.zoom *= f
        self.cam.pan = [ax - (e.x - W / 2) / self.cam.zoom, ay + (e.y - H / 2) / self.cam.zoom]
        self.gl.request()

    # ================================================================ 2D texture view
    def tex_size(self):
        return self.base_prev(self.mat).size if self.mat else (PREV, PREV)

    def fit2d(self):
        W, H = max(50, self.c2.winfo_width()), max(50, self.c2.winfo_height())
        tw, th = self.tex_size()
        self._v2['zoom'] = 0.95 * min(W / tw, H / th)
        self._v2['pan'] = [(W - tw * self._v2['zoom']) / 2, (H - th * self._v2['zoom']) / 2]
        self.draw2d()

    def t2c(self, x, y):
        tw, th = self.tex_size(); z = self._v2['zoom']
        return self._v2['pan'][0] + x * tw * z, self._v2['pan'][1] + y * th * z

    def c2t(self, cx, cy):
        tw, th = self.tex_size(); z = self._v2['zoom'] or 1
        return (cx - self._v2['pan'][0]) / (tw * z), (cy - self._v2['pan'][1]) / (th * z)

    def draw2d(self):
        c = self.c2
        if self.mat is None or self._v2['zoom'] is None: return
        W, H = c.winfo_width(), c.winfo_height()
        if W < 4 or H < 4: return
        img = self._prev.get(self.mat)
        if img is None: return
        tw, th = img.size; z = self._v2['zoom']; px, py = self._v2['pan']
        x0, y0 = max(0, -px / z), max(0, -py / z)
        x1, y1 = min(tw, (W - px) / z), min(th, (H - py) / z)
        c.delete('all')
        if x1 > x0 and y1 > y0:
            view = img
            if self.show_wire.get():
                if self.mat not in self._wire: self._wire[self.mat] = self.bike.uv_wire(self.mat, img.size)
                view = Image.alpha_composite(img, self._wire[self.mat])
            crop = view.crop((int(x0), int(y0), int(math.ceil(x1)), int(math.ceil(y1))))
            sw, sh = max(1, int(crop.width * z)), max(1, int(crop.height * z))
            crop = crop.resize((sw, sh), Image.NEAREST if z > 2 else Image.BILINEAR).convert('RGB')
            self._tk2 = ImageTk.PhotoImage(crop)
            c.create_image(px + int(x0) * z, py + int(y0) * z, image=self._tk2, anchor='nw')
        c.create_rectangle(*self.t2c(0, 0), *self.t2c(1, 1), outline='#777')
        if self.spot and self.spot['mat'] == self.mat and self.page == 'paint':
            sx, sy = self.t2c(*self.spot['img'])
            c.create_oval(sx - 6, sy - 6, sx + 6, sy + 6, outline='#4cff7a', width=2)
        L = self.layer()
        if L is not None and L['type'] != 'fill' and self.layer_mat(L) == self.mat:
            pts = self.layer_corners(L)
            c.create_polygon(*[v for p in pts for v in p], outline='#ffd60a', fill='', width=2, dash=(5, 3))
            for p in pts: c.create_rectangle(p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4, fill='#ffd60a', outline='#000')
            k = self.rot_knob(L)
            c.create_line(*self.mid_top(L), *k, fill='#ffd60a')
            c.create_oval(k[0] - 5, k[1] - 5, k[0] + 5, k[1] + 5, fill='#4cc3ff', outline='#000')

    def layer_axes(self, L):
        tw, th = self.tex_size(); z = self._v2['zoom']
        cx, cy = self.t2c(L['x'], L['y'])
        a = math.radians(L.get('rot', 0))
        hw, hh = L['w'] * tw * z / 2, L['h'] * tw * z / 2
        return (cx, cy), (math.cos(a) * hw, -math.sin(a) * hw), (math.sin(a) * hh, math.cos(a) * hh)

    def layer_corners(self, L):
        (cx, cy), ux, uy = self.layer_axes(L)
        return [(cx + sx * ux[0] + sy * uy[0], cy + sx * ux[1] + sy * uy[1]) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]

    def mid_top(self, L):
        (cx, cy), ux, uy = self.layer_axes(L); return cx - uy[0], cy - uy[1]

    def rot_knob(self, L):
        (cx, cy), ux, uy = self.layer_axes(L)
        n = math.hypot(*uy) or 1
        return cx - uy[0] - uy[0] / n * 20, cy - uy[1] - uy[1] / n * 20

    def v2_down(self, e):
        if self.tool.get() == 'eyedrop' and self.mat:
            x, y = self.c2t(e.x, e.y)
            img = self._prev.get(self.mat)
            if img is not None and 0 <= x < 1 and 0 <= y < 1:
                r, g, b = img.getpixel((int(x * img.width), int(y * img.height)))[:3]
                self.picker.set('#%02x%02x%02x' % (r, g, b), notify=True, final=True)
            self._d2 = None; return
        L = self.layer(); mode = None
        if L is not None and L['type'] != 'fill' and self.layer_mat(L) == self.mat:
            k = self.rot_knob(L)
            if math.hypot(e.x - k[0], e.y - k[1]) <= 8: mode = 'rot'
            elif any(abs(e.x - p[0]) <= 6 and abs(e.y - p[1]) <= 6 for p in self.layer_corners(L)): mode = 'size'
        if mode is None:
            x, y = self.c2t(e.x, e.y)
            hit = self.bike.layer_at(self.mat, x, y, self.layers()) if self.mat else None
            if hit is not None:
                if hit is not L: self.select(hit)
                mode = 'move'
        if mode is None:
            self._d2 = None; return
        self.snapshot()
        self._d2 = {'mode': mode, 'start': (e.x, e.y), 'L': copy.deepcopy(self.layer())}

    def v2_drag(self, e):
        d = getattr(self, '_d2', None); L = self.layer()
        if not d or L is None: return
        o = d['L']; tw, th = self.tex_size(); z = self._v2['zoom']
        if d['mode'] == 'move':
            L['x'] = o['x'] + (e.x - d['start'][0]) / (tw * z); L['y'] = o['y'] + (e.y - d['start'][1]) / (th * z)
        elif d['mode'] == 'rot':
            cx, cy = self.t2c(L['x'], L['y'])
            v = math.degrees(math.atan2(-(e.y - cy), e.x - cx)) - 90
            if e.state & 0x1: v = round(v / 15) * 15
            L['rot'] = (v + 180) % 360 - 180
        elif d['mode'] == 'size':
            (cx, cy), _, _ = self.layer_axes(o)
            (sx, sy) = d['start']
            r = math.hypot(e.x - cx, e.y - cy) / max(1e-6, math.hypot(sx - cx, sy - cy))
            L['w'], L['h'] = max(0.002, o['w'] * r), max(0.002, o['h'] * r)
        self.dirty = True; self.refresh_tex(self.layer_mat(L))
        if self.gl: self.gl.request()

    def v2_up(self, e):
        if getattr(self, '_d2', None):
            self._d2 = None; self.fill_props(); self.save_soon()

    def v2_pan0(self, e):
        self._p2 = (e.x, e.y, list(self._v2['pan']))

    def v2_pan(self, e):
        x0, y0, p0 = self._p2
        self._v2['pan'] = [p0[0] + e.x - x0, p0[1] + e.y - y0]; self.draw2d()

    def v2_zoomby(self, f, x, y):
        z = self._v2['zoom']; nz = max(0.05, min(40, z * f))
        px, py = self._v2['pan']
        self._v2['pan'] = [x - (x - px) * nz / z, y - (y - py) * nz / z]; self._v2['zoom'] = nz; self.draw2d()

    # ================================================================ parts
    def fill_parts_tab(self):
        n = len(self.picked)
        self.pick_lbl.configure(text='%d piece%s picked.' % (n, '' if n == 1 else 's') if n else 'Nothing picked.')
        self.dlist.delete(0, 'end')
        for k in self.proj.get('deleted', []):
            i = int(k[1:].split(':')[0])
            nm = self.bike.parts[i]['name'] if i < len(self.bike.parts) else '?'
            self.dlist.insert('end', '%s - piece %s' % (nm, k.split(':')[1]))
        sel = self.alist.curselection()
        self.alist.delete(0, 'end')
        for a in self.proj.get('added', []):
            self.alist.insert('end', '%s  (%s)' % (a.get('name'), a.get('unit') or 'automatic'))
        if sel and sel[0] < self.alist.size(): self.alist.selection_set(sel[0])
        self.fill_added()

    def delete_picked(self):
        if not self.picked: self.status.set('Pick pieces on the bike first.'); return
        self.snapshot()
        for k in self.picked:
            if k not in self.proj['deleted']: self.proj['deleted'].append(k)
        self.picked = []; self.dirty = True
        self.push_meshes(); self.fill_parts_tab(); self.save_soon(); self._wire.clear(); self.draw2d()

    def clear_picked(self):
        self.picked = []; self.fill_parts_tab(); self.update_sel3d()

    def restore_sel(self):
        idx = self.dlist.curselection()
        if not idx: return
        self.snapshot()
        keys = [self.proj['deleted'][i] for i in idx]
        self.proj['deleted'] = [k for k in self.proj['deleted'] if k not in keys]; self.dirty = True
        self.push_meshes(); self.fill_parts_tab(); self.save_soon(); self._wire.clear(); self.draw2d()

    def restore_all(self):
        if not self.proj['deleted']: return
        self.snapshot(); self.proj['deleted'] = []; self.dirty = True
        self.push_meshes(); self.fill_parts_tab(); self.save_soon(); self._wire.clear(); self.draw2d()

    def add_part(self):
        p = filedialog.askopenfilename(parent=self.host, title='Add a part', filetypes=[
            ('Models', '*.obj *.jm *.edf'), ('Wavefront OBJ', '*.obj'), ('MX Simulator model', '*.jm'),
            ('MX Bikes model', '*.edf'), ('All', '*')])
        if not p: return
        units, up = 'm', 'y'
        if p.lower().endswith('.obj'):
            u = self.app.choose('Add a part', 'What units is %s in?' % os.path.basename(p),
                                ['metres (Blender default)', 'centimetres', 'millimetres', 'feet', 'inches'])
            if not u: return
            units = {'metres': 'm', 'centimetres': 'cm', 'millimetres': 'mm', 'feet': 'ft', 'inches': 'in'}[u.split()[0]]
            ax = self.app.choose('Add a part', 'Which way is up in the file?', ['Y up (OBJ export default)', 'Z up'])
            if not ax: return
            up = 'z' if ax.startswith('Z') else 'y'
        unit = self.app.choose('Add a part', 'Which part of the bike does it move with?',
                               ['frame', 'handlebars.top (steering)', 'handlebars.bottom (fork lowers)', 'swingarm',
                                '(automatic, by where it is)'])
        if not unit: return
        unit = None if unit.startswith('(') else unit.split()[0]
        near = [float(v) for v in self.spot['point']] if self.spot else None
        self.snapshot()
        try:
            a = LV.add_part(self.folder, self.proj, p, unit=unit, units=units, up=up, near=near)
        except Exception as e:
            self.hist.pop(); messagebox.showerror('Add a part', 'Could not read it:\n%s' % e, parent=self.host); return
        self.dirty = True
        self.bike.sync_added(); self.mats_all()
        if a.get('mat'): self.refresh_tex(a['mat'], now=True)
        self.push_meshes(); self.fill_parts_tab(); self.save_soon()
        self.alist.selection_clear(0, 'end'); self.alist.selection_set('end'); self.fill_added()
        self.status.set('Added %s%s - move it with the boxes on the left.' % (a['name'], ' at the picked spot' if near else ''))

    def remove_part(self):
        i = self.alist.curselection()
        if not i: return
        self.snapshot(); self.proj['added'].pop(i[0]); self.dirty = True
        self.proj['deleted'] = [k for k in self.proj['deleted'] if int(k[1:].split(':')[0]) < self.bike.n_orig]
        self.bike.sync_added(); self.push_meshes(); self.fill_parts_tab(); self.save_soon()

    def cur_added(self):
        i = self.alist.curselection()
        return self.proj['added'][i[0]] if i and i[0] < len(self.proj['added']) else None

    def fill_added(self):
        a = self.cur_added()
        for w in self.af.winfo_children():
            try: w.configure(state=('readonly' if a else 'disabled') if isinstance(w, ttk.Combobox) else ('normal' if a else 'disabled'))
            except tk.TclError: pass
        if not a:
            for v in self.a_vars.values(): v.set('')
            return
        self.a_unit.set(a.get('unit') or '(automatic, by where it is)')
        for ax in range(3):
            self.a_vars[('t', ax)].set('%.1f' % (a['t'][ax] * 100)); self.a_vars[('r', ax)].set('%.1f' % a['r'][ax])
        self.a_vars[('s', 0)].set('%.1f' % (a.get('s', 1) * 100))

    def added_changed(self):
        a = self.cur_added()
        if a is None: return
        self.snapshot(); u = self.a_unit.get(); a['unit'] = None if u.startswith('(') else u
        self.dirty = True; self.fill_parts_tab(); self.save_soon()

    def nudge_added(self, k, ax, st):
        a = self.cur_added()
        if a is None: return
        self.snapshot()
        if k == 't': a['t'][ax] += st / 100
        elif k == 'r': a['r'][ax] = (a['r'][ax] + st + 180) % 360 - 180
        else: a['s'] = max(0.01, a.get('s', 1) * (1 + st / 100))
        self.dirty = True; self.bike.sync_added(); self.push_meshes(); self.fill_added(); self.save_soon()

    def typed_added(self, k, ax, var):
        a = self.cur_added()
        if a is None: return
        try: v = float(var.get().replace(',', '.'))
        except ValueError: return
        old = a['t'][ax] * 100 if k == 't' else (a['r'][ax] if k == 'r' else a.get('s', 1) * 100)
        if abs(old - v) < 1e-9: return
        self.snapshot()
        if k == 't': a['t'][ax] = v / 100
        elif k == 'r': a['r'][ax] = v
        else: a['s'] = max(0.01, v / 100)
        self.dirty = True; self.bike.sync_added(); self.push_meshes(); self.save_soon()

    # ================================================================ applying to the model (.blend)
    def build_bike(self, done=None):
        """Paint the textures at full size and rebuild the .blend; done(ok) is called afterwards."""
        blender = self.app.blender.get().strip() if hasattr(self.app, 'blender') else ''
        if not blender or not os.path.isfile(blender):
            messagebox.showerror('Apply', 'Set the path to blender.exe in the main window first.', parent=self.host); return
        self.save_now()
        self.apply_btn.configure(state='disabled')
        self.status.set('Applying - painting the textures at full size and rebuilding the model…')
        log = getattr(self.app, 'write_log', None)

        def work():
            try:
                lines = []
                jp = LV.build(self.bike, log=lines.append)
                import subprocess
                gm = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gamemodel_blender.py')
                cmd = [blender, '-b', '--factory-startup', '--python-exit-code', '1', '--python', gm, '--',
                       '--build-parts', jp, '--save-blend', self.blend]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
                ok = r.returncode == 0 and 'DONE saved' in r.stdout
                msg = ('Applied to %s.' % os.path.basename(self.blend) if ok else
                       'Blender could not rebuild the bike:\n' + '\n'.join((r.stdout + r.stderr).splitlines()[-10:]))
            except Exception as e:
                ok, msg, lines = False, 'Applying failed: %s' % e, []
            self.after(0, lambda: self._built(ok, msg, lines, log, done))
        threading.Thread(target=work, daemon=True).start()

    def _built(self, ok, msg, lines, log, done):
        self.apply_btn.configure(state='normal'); self.status.set(msg.splitlines()[0])
        if ok: self.dirty = False
        if log:
            for l in lines: log('  ' + (l[5:] if l.startswith('INFO ') else l), 'dim')
            log(('  ✔ ' if ok else '  ✘ ') + msg.splitlines()[0], 'ok' if ok else 'err')
        if not ok: messagebox.showerror('Apply', msg, parent=self.host)
        if done: done(ok)
        elif ok and hasattr(self.host, 'after_apply'): self.host.after_apply()


def _inside(poly, x, y):
    n = len(poly); c = False
    for i in range(n):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1: c = not c
    return c


class Workshop(tk.Toplevel):
    """The paint / parts panel in a window of its own (Edit model normally hosts it)."""

    def __init__(self, app, blend, page='paint'):
        super().__init__(app)
        self.title('Paint & parts - %s' % os.path.basename(blend)); self.geometry('1560x920')
        self.panel = PaintPanel(self, self, app, blend, page); self.panel.pack(fill='both', expand=True)
        self.panel.active = True
        self.protocol('WM_DELETE_WINDOW', lambda: (self.panel.save_now(), self.destroy()))

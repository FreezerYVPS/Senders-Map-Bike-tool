"""
align_editor.py - 3D editor for the Game Model Exporter: line your parts up with the game's original
and give pieces the game's materials.

View    left-drag orbit · right/middle-drag pan · wheel zoom · 1/2/3/4 side/front/top/back
Select  click a piece · Shift+click add/remove · Shift+drag box-select · click empty space = clear
        Shift/Ctrl+click in the part list to pick several game parts
Move    Ctrl+drag slides the selection · arrows / PgUp / PgDn nudge · [ ] turn · + / - scale
Saved into <your.blend>.gamemap.json, which Export uses.
"""
import json
import math
import os
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import numpy as np
except ImportError:
    np = None
try:
    from PIL import Image, ImageDraw, ImageTk
except ImportError:
    Image = None

import automap

PALETTE = [(230, 126, 34), (52, 152, 219), (46, 204, 113), (231, 76, 60), (155, 89, 182), (241, 196, 15),
           (26, 188, 156), (236, 112, 160), (255, 99, 72), (72, 126, 176), (211, 84, 0), (39, 174, 96),
           (142, 68, 173), (22, 160, 133), (243, 156, 18)]
GHOST = (125, 135, 150)
KEPT = (215, 218, 224)
UNASSIGNED = (110, 70, 70)
SELECT = (255, 214, 10)
BG = (24, 26, 30)
WHOLE = '(whole model)'
NONE = '(not exported)'
OWN = '(my own material)'


def _rgb255(c):
    return tuple(int(max(0, min(1, x)) * 255) for x in c[:3])


class AlignEditor(tk.Toplevel):
    def __init__(self, app, blend, model, data, map_path):
        super().__init__(app)
        self.app, self.blend, self.model, self.map_path = app, blend, model, map_path
        self.mapping = data['mapping']
        self.mapping.setdefault('adjust', {}); self.mapping.setdefault('piece_adjust', {})
        self.mapping.setdefault('paint', {})
        self.pieces = data['pieces']
        self.parts = data['parts']
        self.helpers = data.get('helpers', [])
        self.catalog = data.get('catalog', [])
        self.cat = {c['id']: c for c in self.catalog}
        self.model_pivot = data.get('model_pivot') or [0, 0, 0]
        self.dirty = False
        self.title('Edit model: %s → %s' % (os.path.basename(blend), model))
        self.geometry('1320x860'); self.minsize(1050, 660)
        self.units = [u['base'] for u in self.mapping['units'] if u['kind'] == 'mesh']
        self.color = {u: PALETTE[i % len(PALETTE)] for i, u in enumerate(self.units)}
        for p in self.pieces:
            p['key'] = automap.piece_key(p['obj'], p['island'])
            if np is not None:
                p['Vn'] = np.asarray(p['V'], dtype=np.float64).reshape(-1, 3)
                p['Fn'] = np.asarray(p['F'], dtype=np.int64).reshape(-1, 3)
        self.ref = {u: (np.asarray(d['ref'], dtype=np.float64).reshape(-1, 3) if np is not None else d['ref'])
                    for u, d in self.parts.items()}
        self.sel_parts = [WHOLE]
        self.sel_pieces = set()
        self.mode = tk.StringVar(value='align')
        self.yaw, self.pitch, self.zoom, self.pan = -90.0, 0.0, None, [0.0, 0.0]
        self.show_ref = tk.BooleanVar(value=False)
        self.ref_on_top = tk.BooleanVar(value=False)
        self.only_sel = tk.BooleanVar(value=False)
        self.show_help = tk.BooleanVar(value=True)
        self.solid = tk.BooleanVar(value=Image is not None and np is not None)
        self.step_mm = tk.StringVar(value='5 mm'); self.step_deg = tk.StringVar(value='1°')
        self._img = None; self._idbuf = None; self._mat_cache = {}
        self.samples = self.load_samples(data.get('samples'))
        self.glmesh_path = data.get('glmesh')
        self.game_root = data.get('game_root')       # the game's render settings are read from here, live
        self.ref_unit = {sp['node']: u['base'] for u in self.mapping['units'] if u['kind'] == 'mesh'
                         for sp in u['splits']}
        self._box = None
        self.ingame = tk.BooleanVar(value=self.samples is not None or bool(self.glmesh_path))
        self.build()
        self.fill_parts(); self.fill_materials(); self.update_selection_ui()
        self.bind_keys()
        self.after(100, self.fit_view)
        self.protocol('WM_DELETE_WINDOW', self.close)

    # ================================================================ layout
    def build(self):
        left = ttk.Frame(self, padding=8); left.pack(side='left', fill='y')
        ttk.Label(left, text='Game parts', font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        ttk.Label(left, text='Shift/Ctrl+click to pick several', foreground='#666').pack(anchor='w')
        self.plist = ttk.Treeview(left, columns=('n',), show='tree headings', height=14, selectmode='extended')
        self.plist.heading('#0', text='Part', anchor='w'); self.plist.heading('n', text='pieces')
        self.plist.column('#0', width=245); self.plist.column('n', width=50, anchor='e')
        self.plist.pack(fill='y', expand=True)
        self.plist.bind('<<TreeviewSelect>>', self.on_part_select)
        box = ttk.LabelFrame(left, text='Selected pieces', padding=6); box.pack(fill='x', pady=(8, 0))
        self.pick_lbl = ttk.Label(box, text='', wraplength=270, justify='left'); self.pick_lbl.pack(anchor='w')
        r = ttk.Frame(box); r.pack(fill='x', pady=(4, 0))
        ttk.Button(r, text='Whole object', command=self.sel_whole_object).pack(side='left')
        ttk.Button(r, text='Whole part', command=self.sel_whole_part).pack(side='left', padx=3)
        ttk.Button(r, text='Clear', command=self.clear_pieces).pack(side='left')
        ttk.Label(box, text='Move them to game part:').pack(anchor='w', pady=(6, 0))
        self.pick_unit = tk.StringVar()
        self.pick_box = ttk.Combobox(box, textvariable=self.pick_unit, state='disabled', values=self.units + [NONE], width=32)
        self.pick_box.pack(fill='x'); self.pick_box.bind('<<ComboboxSelected>>', self.reassign)

        right = ttk.Frame(self, padding=8, width=300); right.pack(side='right', fill='y'); right.pack_propagate(False)
        mt = ttk.Frame(right); mt.pack(fill='x')
        ttk.Radiobutton(mt, text='Align', value='align', variable=self.mode, command=self.on_mode).pack(side='left')
        ttk.Radiobutton(mt, text='Materials', value='materials', variable=self.mode, command=self.on_mode).pack(side='left', padx=10)
        self.panel_align = self.build_align_panel(right)
        self.panel_mat = self.build_material_panel(right)
        vf = ttk.LabelFrame(right, text='View', padding=6); vf.pack(side='bottom', fill='x', pady=(8, 0))
        r = ttk.Frame(vf); r.pack(fill='x')
        for t, y, p in (('Side', -90, 0), ('Front', 0, 0), ('Top', -90, 89.9), ('Back', 180, 0)):
            ttk.Button(r, text=t, width=6, command=lambda y=y, p=p: self.set_view(y, p)).pack(side='left', padx=1)
        ttk.Checkbutton(vf, text='As in game (textures, full detail)', variable=self.ingame, command=self.render,
                        state='normal' if (self.samples is not None or self.glmesh_path) else 'disabled').pack(anchor='w')
        ttk.Checkbutton(vf, text='Solid model (shaded)', variable=self.solid, command=self.render,
                        state='normal' if (Image is not None and np is not None) else 'disabled').pack(anchor='w')
        ttk.Checkbutton(vf, text='Show original (see-through)', variable=self.show_ref, command=self.render).pack(anchor='w')
        ttk.Checkbutton(vf, text='Original dots on top', variable=self.ref_on_top, command=self.render).pack(anchor='w')
        ttk.Checkbutton(vf, text='Only the selected parts', variable=self.only_sel, command=self.render).pack(anchor='w')
        ttk.Checkbutton(vf, text='Mount points (yellow)', variable=self.show_help, command=self.render).pack(anchor='w')
        bot = ttk.Frame(right); bot.pack(side='bottom', fill='x', pady=(8, 0))
        ttk.Button(bot, text='Save', style='Big.TButton', command=self.save).pack(side='left', fill='x', expand=True)
        ttk.Button(bot, text='Close', command=self.close).pack(side='left', padx=(4, 0))
        self.panel_align.pack(fill='both', expand=True, pady=(8, 0))

        self.view_host = ttk.Frame(self); self.view_host.pack(side='left', fill='both', expand=True)
        self.legend = tk.Label(self.view_host, text='', anchor='e', background='#2a2c30', foreground='#9aa0a6',
                               font=('Segoe UI', 9), padx=8)
        self.legend.pack(side='top', fill='x')
        self.glv = None
        if self.glmesh_path and np is not None:
            try:
                import gl_view
                self.glv = gl_view.GLView(self.view_host, self, self.glmesh_path)
                self.glv.pack(fill='both', expand=True)
                self.glv.configure(cursor='crosshair')
                self.canvas = self.glv
            except Exception as e:                     # libraries missing: software view
                print('GL view not used:', e)
                self.glv = None
        if self.glv is None:
            self.make_canvas()
        self.bind_view(self.canvas)

    def make_canvas(self):
        self.canvas = tk.Canvas(self.view_host, background='#%02x%02x%02x' % BG, highlightthickness=0, cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda e: self.render())

    def gl_failed(self, why):
        """OpenGL couldn't start on this PC: switch to the software view."""
        if self.glv is None: return
        try: self.glv.destroy()
        except Exception: pass
        self.glv = None
        self.make_canvas(); self.bind_view(self.canvas)
        self.legend.configure(text='(3D card view unavailable: %s - using the software view)' % why[:80])
        self.after(50, self.render)

    def bind_view(self, w):
        self.canvas.bind('<ButtonPress-1>', self.on_down)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_up)
        for b in ('<ButtonPress-2>', '<ButtonPress-3>'): self.canvas.bind(b, self.on_pan_down)
        for b in ('<B2-Motion>', '<B3-Motion>'): self.canvas.bind(b, self.on_pan)
        for b in ('<ButtonRelease-2>', '<ButtonRelease-3>'): self.canvas.bind(b, lambda e: self.render())
        self.canvas.bind('<MouseWheel>', lambda e: self.zoom_by(1.15 if e.delta > 0 else 1 / 1.15, e.x, e.y))
        self.canvas.bind('<Button-4>', lambda e: self.zoom_by(1.15, e.x, e.y))
        self.canvas.bind('<Button-5>', lambda e: self.zoom_by(1 / 1.15, e.x, e.y))

    def build_align_panel(self, parent):
        f = ttk.Frame(parent)
        self.tgt_lbl = ttk.Label(f, text='', font=('Segoe UI', 10, 'bold'), wraplength=280); self.tgt_lbl.pack(anchor='w')
        self.tgt_hint = ttk.Label(f, text='', foreground='#666', wraplength=280, justify='left'); self.tgt_hint.pack(anchor='w', pady=(0, 6))
        self.fields, self.entries = {}, []
        grid = ttk.Frame(f); grid.pack(fill='x')
        rows = [('Move X (side)', 't', 0), ('Move Y (up)', 't', 1), ('Move Z (forward)', 't', 2),
                ('Turn X (pitch)', 'r', 0), ('Turn Y (yaw)', 'r', 1), ('Turn Z (roll)', 'r', 2), ('Scale %', 's', 0)]
        for i, (lab, key, ax) in enumerate(rows):
            ttk.Label(grid, text=lab).grid(row=i, column=0, sticky='w', pady=1)
            ttk.Button(grid, text='−', width=3, command=lambda k=key, a=ax: self.nudge(k, a, -1)).grid(row=i, column=1)
            v = tk.StringVar(); e = ttk.Entry(grid, textvariable=v, width=8, justify='right'); e.grid(row=i, column=2, padx=2)
            e.bind('<Return>', lambda ev, k=key, a=ax, vv=v: self.typed(k, a, vv))
            e.bind('<FocusOut>', lambda ev, k=key, a=ax, vv=v: self.typed(k, a, vv))
            ttk.Button(grid, text='+', width=3, command=lambda k=key, a=ax: self.nudge(k, a, +1)).grid(row=i, column=3)
            self.fields[(key, ax)] = v; self.entries.append(e)
        st = ttk.Frame(f); st.pack(fill='x', pady=(6, 0))
        ttk.Label(st, text='Step').pack(side='left')
        ttk.Combobox(st, textvariable=self.step_mm, values=['1 mm', '5 mm', '1 cm', '5 cm', '20 cm'], width=6,
                     state='readonly').pack(side='left', padx=4)
        ttk.Combobox(st, textvariable=self.step_deg, values=['0.1°', '1°', '5°', '15°', '90°'], width=5,
                     state='readonly').pack(side='left')
        self.keep_var = tk.BooleanVar(value=False)
        self.keep_chk = ttk.Checkbutton(f, text="Use the game's original for this part", variable=self.keep_var,
                                        command=self.toggle_keep)
        self.keep_chk.pack(anchor='w', pady=(8, 0))
        self.snap_btns = [ttk.Button(f, text='Snap to original (position)', command=lambda: self.snap(False)),
                          ttk.Button(f, text='Snap to original (position + size)', command=lambda: self.snap(True))]
        for b in self.snap_btns: b.pack(fill='x', pady=(4, 0))
        ttk.Button(f, text='Reset selection', command=self.reset).pack(fill='x', pady=(4, 0))
        return f

    def build_material_panel(self, parent):
        f = ttk.Frame(parent)
        ttk.Label(f, text="The game's materials", font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        ttk.Label(f, text='Select pieces in the view (click, Shift+click, Shift+drag), then pick a material and '
                          'press Apply.', foreground='#666', wraplength=280, justify='left').pack(anchor='w', pady=(0, 6))
        self.mlist = ttk.Treeview(f, columns=('n',), show='tree headings', height=12, selectmode='browse')
        self.mlist.heading('#0', text='Material', anchor='w'); self.mlist.heading('n', text='pieces')
        self.mlist.column('#0', width=210); self.mlist.column('n', width=50, anchor='e')
        self.mlist.pack(fill='both', expand=True)
        self.mlist.bind('<Double-1>', lambda e: self.apply_material())
        self.mat_info = ttk.Label(f, text='', foreground='#666', wraplength=280, justify='left'); self.mat_info.pack(anchor='w')
        self.mlist.bind('<<TreeviewSelect>>', self.on_mat_select)
        ttk.Button(f, text='Apply to selected pieces', command=self.apply_material).pack(fill='x', pady=(6, 0))
        ttk.Button(f, text='Select pieces with this material', command=self.select_by_material).pack(fill='x', pady=(4, 0))
        self.color_by = tk.StringVar(value='material')
        ttk.Label(f, text='Colour the view by:').pack(anchor='w', pady=(8, 0))
        ttk.Radiobutton(f, text='material', value='material', variable=self.color_by, command=self.render).pack(anchor='w')
        ttk.Radiobutton(f, text='game part', value='part', variable=self.color_by, command=self.render).pack(anchor='w')
        return f

    def on_mode(self):
        if self.mode.get() == 'align':
            self.panel_mat.pack_forget(); self.panel_align.pack(fill='both', expand=True, pady=(8, 0))
        else:
            self.panel_align.pack_forget(); self.panel_mat.pack(fill='both', expand=True, pady=(8, 0))
        self.render()

    # ================================================================ lists
    def unit(self, name):
        return next((u for u in self.mapping['units'] if u['base'] == name), {})

    def fill_parts(self):
        sel = list(self.sel_parts)
        self.plist.delete(*self.plist.get_children())
        counts = {}
        for p in self.pieces: counts[p['unit']] = counts.get(p['unit'], 0) + 1
        self.plist.insert('', 'end', iid=WHOLE, text=WHOLE, values=(len(self.pieces),))
        self.plist.tag_configure('kept', foreground='#8a8a8a')
        for u in self.units:
            adj = '' if automap.is_identity(self.mapping['adjust'].get(u)) else '  ✎'
            if self.unit(u).get('keep_original'):
                self.plist.insert('', 'end', iid=u, text='□ %s  (game original)' % u, values=('-',), tags=('kept',))
            else:
                tag = 'c_' + u; self.plist.tag_configure(tag, foreground='#%02x%02x%02x' % self.color[u])
                self.plist.insert('', 'end', iid=u, text='■ ' + u + adj, values=(counts.get(u, 0),), tags=(tag,))
        if counts.get(None):
            self.plist.insert('', 'end', iid=NONE, text=NONE, values=(counts[None],))
        keep = [s for s in sel if self.plist.exists(s)] or [WHOLE]
        self._suppress = True
        self.plist.selection_set(keep)
        self._suppress = False

    def fill_materials(self):
        sel = self.mlist.selection()
        self.mlist.delete(*self.mlist.get_children())
        counts = {}
        for p in self.pieces:
            g = self.mapping['paint'].get(p['key'], OWN); counts[g] = counts.get(g, 0) + 1
        self.mlist.insert('', 'end', iid=OWN, text='  ' + OWN, values=(counts.get(OWN, 0),))
        for c in self.catalog:
            col = '#%02x%02x%02x' % _rgb255(c['color'])
            tag = 'm_' + c['id']; self.mlist.tag_configure(tag, foreground=col if sum(_rgb255(c['color'])) > 120 else '#555')
            label = '■ %s%s' % (c['id'], '  (texture)' if c.get('texture') else '')
            self.mlist.insert('', 'end', iid=c['id'], text=label, values=(counts.get(c['id'], 0),), tags=(tag,))
        if sel and self.mlist.exists(sel[0]): self.mlist.selection_set(sel[0])

    def on_mat_select(self, _e=None):
        s = self.mlist.selection()
        if not s or s[0] == OWN:
            self.mat_info.configure(text='Your own material from the .blend (and your texture, if "bring my '
                                         'textures" is on).'); return
        c = self.cat[s[0]]
        self.mat_info.configure(text='roughness %.2f · metal %.2f%s\nused on: %s' % (
            c['roughness'], c['metallic'], ' · texture %s' % os.path.basename(c['texture']) if c.get('texture') else '',
            ', '.join(sorted({u.split('.material.')[0] for u in c['used_by']}))))

    # ================================================================ selection
    def targets(self):
        """('pieces', [ids]) or ('parts', [keys])"""
        if self.sel_pieces: return 'pieces', sorted(self.sel_pieces)
        keys = ['__all__' if s == WHOLE else s for s in self.sel_parts if s != NONE]
        return 'parts', keys or ['__all__']

    def on_part_select(self, _e=None):
        if getattr(self, '_suppress', False): return
        sel = list(self.plist.selection())
        if not sel or sorted(sel) == sorted(self.sel_parts): return     # programmatic refresh, not a click
        self.sel_parts = sel
        self.sel_pieces.clear()
        self.update_selection_ui(); self.render()

    def update_selection_ui(self):
        kind, items = self.targets()
        if kind == 'pieces':
            ps = [self.pieces[i] for i in items]
            if len(ps) == 1:
                p = ps[0]
                nm = p['obj'] if p['island'] < 0 else '%s, loose part %d' % (p['obj'], p['island'])
                txt = '%s\n%s vertices · in %s\nmaterial: %s' % (nm, format(p['n'], ','), p['unit'] or NONE,
                                                                  self.mapping['paint'].get(p['key'], p['material'] or '-'))
            else:
                units = sorted({p['unit'] or NONE for p in ps})
                txt = '%d pieces · %s vertices\nin %s' % (len(ps), format(sum(p['n'] for p in ps), ','),
                                                          ', '.join(units[:4]) + (' …' if len(units) > 4 else ''))
            self.pick_lbl.configure(text=txt)
            us = {p['unit'] for p in ps}
            self.pick_unit.set((next(iter(us)) or NONE) if len(us) == 1 else '')
            self.pick_box.configure(state='readonly')
            self.tgt_lbl.configure(text='Adjusting: %d selected piece%s' % (len(ps), 's' if len(ps) > 1 else ''))
            self.tgt_hint.configure(text='Moves/turns/scales the pieces themselves (around their centre). The boxes '
                                         'only take steps here; type into them to move by that amount.')
            for e in self.entries: e.delete(0, 'end')
            for b in self.snap_btns: b.configure(state='disabled')
            self.keep_chk.configure(state='disabled')
        else:
            self.pick_lbl.configure(text='Click a piece in the view (Shift+click / Shift+drag for more).')
            self.pick_box.configure(state='disabled'); self.pick_unit.set('')
            names = ['whole model' if k == '__all__' else k for k in items]
            self.tgt_lbl.configure(text='Adjusting: ' + (', '.join(names) if len(names) <= 3 else '%d parts' % len(names)))
            self.tgt_hint.configure(text="Moves, turns and scales around each part's pivot (axle, hinge…).")
            a = self.mapping['adjust'].get(items[0]) or {'t': [0, 0, 0], 'r': [0, 0, 0], 's': 1.0}
            for ax in range(3):
                self.fields[('t', ax)].set('%.2f' % (a['t'][ax] * 100)); self.fields[('r', ax)].set('%.1f' % a['r'][ax])
            self.fields[('s', 0)].set('%.1f' % (a['s'] * 100))
            for b in self.snap_btns: b.configure(state='normal')
            one = len(items) == 1 and items[0] in self.units
            self.keep_chk.configure(state='normal' if one else 'disabled')
            self.keep_var.set(bool(one and self.unit(items[0]).get('keep_original')))

    def clear_pieces(self):
        self.sel_pieces.clear(); self.update_selection_ui(); self.render()

    def sel_whole_object(self):
        objs = {self.pieces[i]['obj'] for i in self.sel_pieces}
        self.sel_pieces |= {i for i, p in enumerate(self.pieces) if p['obj'] in objs}
        self.update_selection_ui(); self.render()

    def sel_whole_part(self):
        if self.sel_pieces: units = {self.pieces[i]['unit'] for i in self.sel_pieces}
        else: units = {s for s in self.sel_parts if s != WHOLE}
        self.sel_pieces = {i for i, p in enumerate(self.pieces) if p['unit'] in units}
        self.update_selection_ui(); self.render()

    # ================================================================ geometry
    def part_matrix(self, unit):
        adj = self.mapping['adjust']
        A = automap.adjust_matrix(adj.get('__all__'))
        if unit is not None: A = automap.mmul(automap.adjust_matrix(adj.get(unit)), A)
        return A

    def piece_matrix(self, p, with_piece=True):
        M = self.part_matrix(p['unit'])
        P = self.mapping['piece_adjust'].get(p['key']) if with_piece else None
        return automap.mat4_mul(P, M) if P else M

    def verts(self, p, with_piece=True):
        M = self.piece_matrix(p, with_piece)
        if np is not None:
            Mn = np.asarray(M); return p['Vn'] @ Mn[:3, :3].T + Mn[:3, 3]
        return [automap.xform(M, v) for v in p['V']]

    def bbox(self, P):
        if np is not None:
            P = np.asarray(P); return P.min(0).tolist(), P.max(0).tolist()
        xs, ys, zs = zip(*P); return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]

    def concat(self, arrs):
        arrs = [a for a in arrs if len(a)]
        if not arrs: return None
        return np.concatenate(arrs) if np is not None else [q for a in arrs for q in a]

    # ================================================================ edits
    def steps(self):
        mm = {'1 mm': 0.001, '5 mm': 0.005, '1 cm': 0.01, '5 cm': 0.05, '20 cm': 0.2}[self.step_mm.get()]
        return mm, float(self.step_deg.get().rstrip('°'))

    def part_adj(self, key):
        a = self.mapping['adjust'].get(key)
        if a is None:
            pv = self.model_pivot if key == '__all__' else ((self.parts.get(key) or {}).get('pivot') or self.centroid_of_unit(key))
            a = {'t': [0.0, 0.0, 0.0], 'r': [0.0, 0.0, 0.0], 's': 1.0, 'pivot': list(pv)}
            self.mapping['adjust'][key] = a
        return a

    def centroid_of_unit(self, unit):
        P = self.concat([self.verts(p, False) for p in self.pieces if p['unit'] == unit])
        if P is None: return [0, 0, 0]
        b = self.bbox(P); return [(b[0][i] + b[1][i]) / 2 for i in range(3)]

    def apply_delta(self, t=(0, 0, 0), r=(0, 0, 0), s=1.0):
        kind, items = self.targets()
        if kind == 'pieces':
            P = self.concat([self.verts(self.pieces[i]) for i in items])
            if P is None: return
            b = self.bbox(P); pv = [(b[0][i] + b[1][i]) / 2 for i in range(3)]
            D = automap.delta_matrix(t, r, s, pv)
            for i in items:
                k = self.pieces[i]['key']
                old = self.mapping['piece_adjust'].get(k) or [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
                self.mapping['piece_adjust'][k] = automap.mat4_mul(D, old)
        else:
            for k in items:
                a = self.part_adj(k)
                a['t'] = [a['t'][i] + t[i] for i in range(3)]
                a['r'] = [(a['r'][i] + r[i] + 180) % 360 - 180 for i in range(3)]
                a['s'] = max(0.01, a['s'] * s)
        self.dirty = True; self._idbuf = None
        self.update_selection_ui(); self.render()

    def nudge(self, key, ax, sign, mult=1.0):
        mm, deg = self.steps()
        if key == 't': t = [0, 0, 0]; t[ax] = sign * mm * mult; self.apply_delta(t=t)
        elif key == 'r': r = [0, 0, 0]; r[ax] = sign * deg * mult; self.apply_delta(r=r)
        else: self.apply_delta(s=1 + sign * 0.01 * mult)

    def typed(self, key, ax, var):
        txt = var.get().strip().replace(',', '.')
        if not txt: return
        try: val = float(txt)
        except ValueError: self.update_selection_ui(); return
        kind, items = self.targets()
        if kind == 'pieces':                      # pieces: the typed amount is a step
            if key == 't': t = [0, 0, 0]; t[ax] = val / 100; self.apply_delta(t=t)
            elif key == 'r': r = [0, 0, 0]; r[ax] = val; self.apply_delta(r=r)
            else: self.apply_delta(s=max(0.01, val / 100))
            return
        changed = False
        for k in items:
            a = self.part_adj(k)
            if key == 't' and abs(a['t'][ax] - val / 100) > 1e-9: a['t'][ax] = val / 100; changed = True
            elif key == 'r' and abs(a['r'][ax] - val) > 1e-9: a['r'][ax] = val; changed = True
            elif key == 's' and abs(a['s'] - val / 100) > 1e-9: a['s'] = max(0.01, val / 100); changed = True
        if changed:
            self.dirty = True; self._idbuf = None; self.update_selection_ui(); self.render()

    def reset(self):
        kind, items = self.targets()
        if kind == 'pieces':
            for i in items: self.mapping['piece_adjust'].pop(self.pieces[i]['key'], None)
        else:
            for k in items: self.mapping['adjust'].pop(k, None)
        self.dirty = True; self._idbuf = None; self.fill_parts(); self.update_selection_ui(); self.render()

    def snap(self, size):
        kind, items = self.targets()
        if kind != 'parts': return
        for k in items:
            unit = None if k == '__all__' else k
            user = self.concat([self.verts(p, False) for p in self.pieces if unit is None or p['unit'] == unit])
            refp = self.concat(list(self.ref.values())) if unit is None else self.ref.get(unit)
            if user is None or refp is None or len(refp) == 0: continue
            if unit is not None:                   # without this part's own adjustment
                A = automap.adjust_matrix(self.mapping['adjust'].get('__all__'))
                user = self.concat([(p['Vn'] @ np.asarray(A)[:3, :3].T + np.asarray(A)[:3, 3]) if np is not None
                                    else [automap.xform(A, v) for v in p['V']] for p in self.pieces if p['unit'] == unit])
            bu, br = self.bbox(user), self.bbox(refp)
            a = self.part_adj(k); a['t'] = [0.0, 0.0, 0.0]
            if size:
                du, dr = math.dist(bu[0], bu[1]), math.dist(br[0], br[1])
                if du > 1e-9: a['s'] = dr / du
            cu = [(bu[0][i] + bu[1][i]) / 2 for i in range(3)]
            moved = automap.xform(automap.adjust_matrix(a), cu) if unit is not None else \
                automap.xform(automap.adjust_matrix(a), cu)
            cr = [(br[0][i] + br[1][i]) / 2 for i in range(3)]
            a['t'] = [cr[i] - moved[i] for i in range(3)]
        self.dirty = True; self._idbuf = None; self.fill_parts(); self.update_selection_ui(); self.render()

    def toggle_keep(self):
        kind, items = self.targets()
        if kind != 'parts' or len(items) != 1 or items[0] not in self.units: return
        name = items[0]; u = self.unit(name)
        if self.keep_var.get():
            mine = [i for i, p in enumerate(self.pieces) if p['unit'] == name]
            if mine and not messagebox.askyesno('Edit model', '%d of your pieces are in %s. They will not be '
                                                'exported (move them to another part first if you need them). Use the '
                                                "game's original?" % (len(mine), name), parent=self):
                self.keep_var.set(False); return
            self.move_pieces(mine, None)
            u['keep_original'] = True
            for sp in u['splits']: sp['sources'] = []
            u['how'] = 'game original kept'
        else:
            u['keep_original'] = False; u['how'] = 'set by you'
        self.dirty = True; self.fill_parts(); self.render()

    def move_pieces(self, ids, new):
        triples = {(s[0], s[1], s[2]) for i in ids for s in self.pieces[i]['sources']}
        for u in self.mapping['units']:
            if u['kind'] != 'mesh': continue
            for sp in u['splits']:
                sp['sources'] = [s for s in sp['sources'] if (s[0], s[1], s[2] if len(s) > 2 else -1) not in triples]
        if new is not None:
            u = self.unit(new); u['keep_original'] = False
            u['splits'][0]['sources'].extend([list(t) for t in sorted(triples)])
            u['how'] = 'set by you'; u['set_by_you'] = True
        used = {s[0] for u in self.mapping['units'] if u['kind'] == 'mesh' for sp in u['splits'] for s in sp['sources']}
        self.mapping['unused'] = sorted({o for o in self.mapping.get('unused', []) if o not in used} |
                                        {self.pieces[i]['obj'] for i in ids if self.pieces[i]['obj'] not in used})
        for i in ids: self.pieces[i]['unit'] = new

    def reassign(self, _e=None):
        if not self.sel_pieces: return
        new = self.pick_unit.get(); new = None if new in (NONE, '') else new
        self.move_pieces(sorted(self.sel_pieces), new)
        self.dirty = True; self.fill_parts(); self.update_selection_ui(); self.render()

    def apply_material(self):
        s = self.mlist.selection()
        if not s: messagebox.showinfo('Edit model', 'Pick a material in the list first.', parent=self); return
        if not self.sel_pieces:
            messagebox.showinfo('Edit model', 'Select pieces in the view first (click, Shift+click or Shift+drag).',
                                parent=self); return
        for i in self.sel_pieces:
            k = self.pieces[i]['key']
            if s[0] == OWN: self.mapping['paint'].pop(k, None)
            else: self.mapping['paint'][k] = s[0]
        self.dirty = True; self.fill_materials(); self.update_selection_ui(); self.render()

    def select_by_material(self):
        s = self.mlist.selection()
        if not s: return
        self.sel_pieces = {i for i, p in enumerate(self.pieces) if self.mapping['paint'].get(p['key'], OWN) == s[0]}
        self.update_selection_ui(); self.render()

    # ================================================================ camera
    def basis(self):
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        fwd = (math.cos(p) * math.sin(y), -math.sin(p), math.cos(p) * math.cos(y))
        right = (math.cos(y), 0.0, -math.sin(y))
        up = (fwd[1] * right[2] - fwd[2] * right[1], fwd[2] * right[0] - fwd[0] * right[2],
              fwd[0] * right[1] - fwd[1] * right[0])
        return right, up, fwd

    def set_view(self, yaw, pitch):
        self.yaw, self.pitch = yaw, pitch; self._idbuf = None; self.render()

    def fit_view(self):
        P = self.concat(list(self.ref.values()))
        if P is None: P = self.concat([self.verts(p) for p in self.pieces])
        if P is None: return
        b = self.bbox(P)
        self.center = [(b[0][i] + b[1][i]) / 2 for i in range(3)]
        W = max(200, self.canvas.winfo_width()); H = max(200, self.canvas.winfo_height())
        self.zoom = 0.8 * min(W, H) / max(math.dist(b[0], b[1]), 1e-6)
        self.pan = [0.0, 0.0]; self._idbuf = None; self.render()

    def project_np(self, P):
        right, up, fwd = self.basis()
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        Q = P - np.asarray(self.center)
        return (W / 2 + (Q @ np.asarray(right) - self.pan[0]) * self.zoom,
                H / 2 - (Q @ np.asarray(up) - self.pan[1]) * self.zoom, Q @ np.asarray(fwd))

    def project_py(self, pts):
        right, up, fwd = self.basis()
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        cx, cy, cz = self.center; z = self.zoom; px, py = self.pan
        return [(W / 2 + ((x - cx) * right[0] + (y - cy) * right[1] + (q - cz) * right[2] - px) * z,
                 H / 2 - ((x - cx) * up[0] + (y - cy) * up[1] + (q - cz) * up[2] - py) * z,
                 (x - cx) * fwd[0] + (y - cy) * fwd[1] + (q - cz) * fwd[2]) for x, y, q in pts]

    # ================================================================ colours
    def piece_color(self, i, p):
        if self.mode.get() == 'materials' and self.color_by.get() == 'material':
            g = self.mapping['paint'].get(p['key'])
            col = _rgb255(self.cat[g]['color']) if g in self.cat else _rgb255(p.get('color') or [0.7, 0.7, 0.7])
            col = tuple(max(c, 40) for c in col)              # keep near-black materials visible
        else:
            col = self.color.get(p['unit'], UNASSIGNED)
        if i in self.sel_pieces:
            col = tuple(int(0.35 * c + 0.65 * s) for c, s in zip(col, SELECT))
        return col

    def piece_color_plain(self, i, p):
        if self.mode.get() == 'materials' and self.color_by.get() == 'material':
            g = self.mapping['paint'].get(p['key'])
            col = _rgb255(self.cat[g]['color']) if g in self.cat else _rgb255(p.get('color') or [0.7, 0.7, 0.7])
            return tuple(max(c, 40) for c in col)
        return self.color.get(p['unit'], UNASSIGNED)

    def ghost_visible(self, unit):
        if self.only_sel.get() and not self.sel_pieces and WHOLE not in self.sel_parts and unit not in self.sel_parts:
            return False
        return True

    def overlay_pivots(self):
        kind, items = self.targets()
        out = []
        if kind == 'parts' and self.mode.get() == 'align':
            for k in items[:6]:
                a = self.mapping['adjust'].get(k)
                pv = (a or {}).get('pivot') or (self.model_pivot if k == '__all__' else (self.parts.get(k) or {}).get('pivot'))
                if pv: out.append(pv)
        return out

    def legend_text(self):
        n = '   ·   %d piece(s) selected' % len(self.sel_pieces) if self.sel_pieces else ''
        if self.use_ingame():
            t = 'as the game will draw it (your textures / colours)   ·   blue-grey = original   ·   yellow = selected'
        elif self.mode.get() == 'materials' and self.color_by.get() == 'material':
            t = 'colours = game material (yours where not set)   ·   yellow = selected'
        else:
            t = 'colours = your parts by game part   ·   blue-grey = original   ·   solid = kept game parts'
        return t + n

    def visible(self, p):
        if not self.only_sel.get(): return True
        if self.sel_pieces: return True
        return WHOLE in self.sel_parts or p['unit'] in self.sel_parts

    def dim(self, i, p):
        if self.sel_pieces: return 1.0 if i in self.sel_pieces else 0.55
        if WHOLE in self.sel_parts: return 1.0
        return 1.0 if p['unit'] in self.sel_parts else 0.4

    # ================================================================ rendering
    def load_samples(self, path):
        if np is None or Image is None or not path: return None
        try:
            z = np.load(path)
            I = z['I']
            starts = np.searchsorted(I, np.arange(len(self.pieces) + 1))
            return {'P': z['P'].astype(np.float32), 'N': z['N'].astype(np.float32), 'C': z['C'],
                    'slices': [(int(starts[i]), int(starts[i + 1])) for i in range(len(self.pieces))],
                    'spacing': float(z['spacing'])}
        except Exception:
            return None

    def use_ingame(self):
        return ((self.samples is not None or getattr(self, 'glv', None) is not None) and self.ingame.get()
                and not (self.mode.get() == 'materials' and self.color_by.get() == 'part'))

    def render(self, fast=False):
        if self.zoom is None: return
        if self.glv is not None:
            self.legend.configure(text=self.legend_text())
            self._idbuf = None; self.glv.request(); return
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        if W < 10 or H < 10: return
        if self.use_ingame() and not fast:
            self.render_ingame(W, H)
        elif self.solid.get() and Image is not None and np is not None and not fast:
            self.render_solid(W, H)
        elif np is not None:
            self.render_points_np(W, H)
        else:
            self.render_points_py(W, H, fast)
        self.draw_overlay(W, H)

    def ghost_layers(self):
        out = []
        if not self.show_ref.get(): return out
        for u, R in self.ref.items():
            if len(R) == 0: continue
            kept = self.unit(u).get('keep_original')
            if self.only_sel.get() and not self.sel_pieces and WHOLE not in self.sel_parts and u not in self.sel_parts:
                continue
            out.append((R, KEPT if kept else GHOST, 1.0 if kept else 0.7, 2 if kept else 1))
        return out

    def splat(self, img, P, cols, size):
        sx, sy, dz = self.project_np(P)
        H, W = img.shape[:2]
        xi, yi = sx.astype(np.int32), sy.astype(np.int32)
        ok = (xi >= 0) & (xi < W - 1) & (yi >= 0) & (yi < H - 1)
        order = np.argsort(-dz[ok], kind='stable')
        xi, yi, C = xi[ok][order], yi[ok][order], cols[ok][order]
        img[yi, xi] = C
        if size > 1:
            img[yi, xi + 1] = C; img[yi + 1, xi] = C; img[yi + 1, xi + 1] = C
        return xi, yi, ok, order

    def render_points_np(self, W, H):
        img = np.empty((H, W, 3), np.uint8); img[:] = BG
        for R, col, dm, sz in self.ghost_layers():
            self.splat(img, R, np.tile(np.array([int(c * dm) for c in col], np.uint8), (len(R), 1)), sz)
        Ps, Cs, Is = [], [], []
        for i, p in enumerate(self.pieces):
            if not self.visible(p) or not len(p['Vn']): continue
            V = self.verts(p); Ps.append(V)
            c = np.array(self.piece_color(i, p), np.float32) * self.dim(i, p)
            Cs.append(np.tile(c.astype(np.uint8), (len(V), 1))); Is.append(np.full(len(V), i, np.int32))
        if Ps:
            P, C, I = np.concatenate(Ps), np.concatenate(Cs), np.concatenate(Is)
            xi, yi, ok, order = self.splat(img, P, C, 2)
            self._pick = (xi, yi, I[ok][order])
        else:
            self._pick = None
        self._idbuf = None
        self.show_image(Image.fromarray(img) if Image is not None else img)

    def render_points_py(self, W, H, fast):
        buf = bytearray(bytes(BG) * (W * H))
        items = []
        for R, col, dm, sz in self.ghost_layers():
            pts = R[::4] if fast else R
            for sx, sy, dz in self.project_py(pts): items.append((dz, int(sx), int(sy), tuple(int(c * dm) for c in col), -1, sz))
        for i, p in enumerate(self.pieces):
            if not self.visible(p): continue
            col = tuple(int(c * self.dim(i, p)) for c in self.piece_color(i, p))
            pts = self.verts(p)
            if fast: pts = pts[::4]
            for sx, sy, dz in self.project_py(pts): items.append((dz, int(sx), int(sy), col, i, 2))
        items.sort(key=lambda t: -t[0])
        pick = []
        for dz, x, y, col, pid, sz in items:
            if not (0 <= x < W - 1 and 0 <= y < H - 1): continue
            rgb = bytes(col); o = (y * W + x) * 3; buf[o:o + 3] = rgb
            if sz > 1:
                buf[o + 3:o + 6] = rgb; o2 = o + W * 3; buf[o2:o2 + 3] = rgb; buf[o2 + 3:o2 + 6] = rgb
            if pid >= 0: pick.append((x, y, pid))
        self._pick = pick; self._idbuf = None
        self._img = tk.PhotoImage(width=W, height=H, data=b'P6 %d %d 255\n' % (W, H) + bytes(buf), format='PPM')
        self.canvas.delete('img'); self.canvas.create_image(0, 0, anchor='nw', image=self._img, tags='img')
        self.canvas.tag_lower('img')

    def gather_faces(self):
        """All visible triangles: screen coords (n,3,2), depth (n,), colour (n,3), piece id (n,)"""
        right, up, fwd = self.basis()
        Fs, Ds, Cs, Is, Ns = [], [], [], [], []
        allV, off = [], 0
        for i, p in enumerate(self.pieces):
            if not self.visible(p) or not len(p['Fn']): continue
            V = self.verts(p)
            allV.append(V)
            Fs.append(p['Fn'] + off); off += len(V)
            Cs.append(np.tile(np.array(self.piece_color(i, p), np.float32) * self.dim(i, p), (len(p['Fn']), 1)))
            Is.append(np.full(len(p['Fn']), i, np.int32))
        if not Fs: return None
        V = np.concatenate(allV); F = np.concatenate(Fs); C = np.concatenate(Cs); I = np.concatenate(Is)
        sx, sy, dz = self.project_np(V)
        tri = V[F]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        light = np.asarray(fwd) * 0.85 + np.asarray(up) * 0.35
        light /= np.linalg.norm(light)
        shade = 0.30 + 0.70 * np.abs(n @ light)
        C = np.clip(C * shade[:, None], 0, 255).astype(np.uint8)
        XY = np.stack([sx[F], sy[F]], -1)
        D = dz[F].mean(1)
        order = np.argsort(-D, kind='stable')
        return XY[order], C[order], I[order]

    def render_solid(self, W, H):
        img = Image.new('RGB', (W, H), BG)
        arr = np.asarray(img).copy()
        behind = [g for g in self.ghost_layers()] if not self.ref_on_top.get() else []
        for R, col, dm, sz in behind:
            self.splat(arr, R, np.tile(np.array([int(c * dm) for c in col], np.uint8), (len(R), 1)), sz)
        img = Image.fromarray(arr)
        dr = ImageDraw.Draw(img)
        g = self.gather_faces()
        self._faces = g
        if g is not None:
            XY, C, I = g
            xy = XY.reshape(len(XY), 6).tolist(); cl = [tuple(c) for c in C.tolist()]
            poly = dr.polygon
            for pts, c in zip(xy, cl):
                poly(pts, fill=c)
        if self.ref_on_top.get():
            arr = np.asarray(img).copy()
            for R, col, dm, sz in self.ghost_layers():
                self.splat(arr, R, np.tile(np.array([int(c * dm) for c in col], np.uint8), (len(R), 1)), 1)
            img = Image.fromarray(arr)
        self._pick = None; self._idbuf = None
        self.show_image(img)

    def render_ingame(self, W, H):
        """Your model at full detail with the colours/texture the game gets, smooth lit, depth buffered."""
        S = self.samples
        right, up, fwd = map(np.asarray, self.basis())
        img = np.empty((H, W, 3), np.uint8); img[:] = BG
        if not self.ref_on_top.get():
            for R, col, dm, sz in self.ghost_layers():
                self.splat(img, R, np.tile(np.array([int(c * dm) for c in col], np.uint8), (len(R), 1)), sz)
        Ps, Ns, Cs = [], [], []
        light = fwd * -0.55 + up * 0.75 + right * 0.35; light /= np.linalg.norm(light)
        for i, p in enumerate(self.pieces):
            a, e = S['slices'][i]
            if e <= a or not self.visible(p): continue
            M = np.asarray(self.piece_matrix(p))
            R3 = M[:3, :3]
            Ps.append(S['P'][a:e] @ R3.T + M[:3, 3])
            n = S['N'][a:e] @ np.linalg.inv(R3).T
            Ns.append(n)
            g = self.mapping['paint'].get(p['key'])
            if g in self.cat:                                 # a game material you picked: its flat colour
                c = np.tile(np.array(_rgb255(self.cat[g]['color']), np.float32), (e - a, 1))
            else:
                c = S['C'][a:e].astype(np.float32)
            if i in self.sel_pieces:
                c = c * 0.45 + np.array(SELECT, np.float32) * 0.55
            dm = self.dim(i, p)
            if dm != 1.0: c = c * dm
            Cs.append(c)
        if Ps:
            P = np.concatenate(Ps); N = np.concatenate(Ns); C = np.concatenate(Cs)
            N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
            Q = P - np.asarray(self.center, np.float32)
            sx = W / 2 + (Q @ right - self.pan[0]) * self.zoom
            sy = H / 2 - (Q @ up - self.pan[1]) * self.zoom
            dz = Q @ fwd
            facing = N @ fwd
            N = np.where(facing[:, None] > 0, -N, N)          # show the side facing you (two-sided)
            sh = 0.38 + 0.62 * np.clip(N @ light, 0, 1)
            C = np.clip(C * sh[:, None], 0, 255).astype(np.uint8)
            # draw at a resolution the samples cover solidly (zoomed in -> smaller buffer, scaled up after)
            px_per = S['spacing'] * self.zoom
            f = min(1.0, 1.0 / max(px_per * 0.9, 1e-6))
            w2, h2 = max(8, int(W * f)), max(8, int(H * f))
            fx, fy = w2 / W, h2 / H
            xi, yi = (sx * fx).astype(np.int32), (sy * fy).astype(np.int32)
            ok = (xi >= 0) & (yi >= 0) & (xi < w2 - 1) & (yi < h2 - 1)
            xi, yi, dz, C = xi[ok], yi[ok], dz[ok], C[ok]
            order = np.argsort(-dz, kind='stable')               # far first, near last (near wins)
            xi, yi, C, dz = xi[order], yi[order], C[order], dz[order]
            buf = np.zeros((h2, w2, 3), np.uint8); zb = np.full((h2, w2), np.inf, np.float32)
            buf[yi, xi] = C; zb[yi, xi] = dz
            # pinholes: an empty pixel (or one clearly behind its neighbours) takes the nearest neighbour
            for _ in range(2):
                nb = [(0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, -1), (1, -1), (-1, 1)]
                Zs = np.stack([np.roll(zb, d, (0, 1)) for d in nb])
                k = np.argmin(Zs, 0); zmin = np.take_along_axis(Zs, k[None], 0)[0]
                filled = np.isfinite(Zs).sum(0)
                thr = S['spacing'] * 3
                fix = (filled >= 5) & (~np.isfinite(zb) | (zb > zmin + thr))
                if not fix.any(): break
                Cs = np.stack([np.roll(buf, d, (0, 1)) for d in nb])
                yy, xx = np.nonzero(fix)
                buf[yy, xx] = Cs[k[yy, xx], yy, xx]; zb[yy, xx] = zmin[yy, xx]
            mask = np.isfinite(zb)
            if f < 1.0:
                big = np.asarray(Image.fromarray(buf).resize((W, H), Image.BILINEAR))
                m = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((W, H), Image.NEAREST)) > 0
                img[m] = big[m]
            else:
                img[mask] = buf[mask]
        if self.ref_on_top.get():
            for R, col, dm, sz in self.ghost_layers():
                self.splat(img, R, np.tile(np.array([int(c * dm) for c in col], np.uint8), (len(R), 1)), 1)
        self._faces = self.gather_faces()                     # picking still uses the light mesh
        self._pick = None; self._idbuf = None
        self.show_image(Image.fromarray(img))

    def id_buffer(self):
        """Piece id under each pixel (solid mode), drawn on demand for clicks / box selection."""
        if self._idbuf is not None: return self._idbuf
        if self.glv is not None:
            self._idbuf = self.glv.pick_buffer(); return self._idbuf
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        g = getattr(self, '_faces', None)
        img = Image.new('RGB', (W, H), (0, 0, 0))
        if g is not None:
            XY, C, I = g
            dr = ImageDraw.Draw(img)
            ids = (I + 1).tolist()
            for pts, k in zip(XY.reshape(len(XY), 6).tolist(), ids):
                dr.polygon(pts, fill=(k & 255, (k >> 8) & 255, (k >> 16) & 255))
        a = np.asarray(img).astype(np.int64)
        self._idbuf = (a[..., 0] | (a[..., 1] << 8) | (a[..., 2] << 16)) - 1
        return self._idbuf

    def show_image(self, img):
        if Image is not None and not isinstance(img, np.ndarray if np is not None else type(None)):
            self._img = ImageTk.PhotoImage(img)
        else:
            H, W = img.shape[:2]
            self._img = tk.PhotoImage(width=W, height=H, data=b'P6 %d %d 255\n' % (W, H) + img.tobytes(), format='PPM')
        self.canvas.delete('img'); self.canvas.create_image(0, 0, anchor='nw', image=self._img, tags='img')
        self.canvas.tag_lower('img')

    def draw_overlay(self, W, H):
        self.canvas.delete('ov')
        if self.show_help.get() and self.helpers:
            pts = [h['pos'] for h in self.helpers]
            pr = (list(zip(*self.project_np(np.asarray(pts))[:2])) if np is not None
                  else [(a, b) for a, b, _ in self.project_py(pts)])
            for x, y in pr:
                self.canvas.create_polygon(x, y - 4, x + 4, y, x, y + 4, x - 4, y, fill='#e8c547', outline='', tags='ov')
        kind, items = self.targets()
        if kind == 'parts' and self.mode.get() == 'align':
            for k in items[:6]:
                a = self.mapping['adjust'].get(k)
                pv = (a or {}).get('pivot') or (self.model_pivot if k == '__all__' else (self.parts.get(k) or {}).get('pivot'))
                if not pv: continue
                x, y = ((self.project_np(np.asarray([pv]))[0][0], self.project_np(np.asarray([pv]))[1][0]) if np is not None
                        else self.project_py([pv])[0][:2])
                self.canvas.create_oval(x - 6, y - 6, x + 6, y + 6, outline='#ffffff', width=2, tags='ov')
                self.canvas.create_text(x + 9, y - 9, text='pivot', fill='#ffffff', anchor='w', tags='ov')
        right, up, fwd = self.basis()
        ox, oy, L = 40, H - 40, 26
        for vec, col, name in (((1, 0, 0), '#e74c3c', 'X'), ((0, 1, 0), '#2ecc71', 'Y'), ((0, 0, 1), '#3498db', 'Z')):
            dx = sum(vec[i] * right[i] for i in range(3)) * L; dy = -sum(vec[i] * up[i] for i in range(3)) * L
            self.canvas.create_line(ox, oy, ox + dx, oy + dy, fill=col, width=2, tags='ov')
            self.canvas.create_text(ox + dx * 1.25, oy + dy * 1.25, text=name, fill=col, tags='ov')
        if self.use_ingame():
            legend = 'as the game will draw it (your textures / colours) · grey dots = original · yellow = selected'
        else:
            legend = ('colours = game material (yours where not set) · yellow = selected'
                      if self.mode.get() == 'materials' and self.color_by.get() == 'material'
                      else 'grey dots = original · light grey = kept game parts · colours = your parts by game part')
        self.canvas.create_text(W - 10, 12, anchor='ne', fill='#9aa0a6', tags='ov', text=legend)
        if self.sel_pieces:
            self.canvas.create_text(W - 10, 30, anchor='ne', fill='#ffd60a', tags='ov',
                                    text='%d piece(s) selected' % len(self.sel_pieces))

    # ================================================================ mouse / keys
    def on_down(self, e):
        self._down = (e.x, e.y, self.yaw, self.pitch); self._moved = False; self._last = (e.x, e.y)
        self._ctrl = bool(e.state & 0x4); self._shift = bool(e.state & 0x1)

    def on_drag(self, e):
        x0, y0, yaw0, pitch0 = self._down
        if abs(e.x - x0) + abs(e.y - y0) > 3: self._moved = True
        if not self._moved: return
        if self._shift:                                      # box select
            if self.glv is not None:
                self._box = (x0, y0, e.x, e.y); self.glv.request(); return
            self.canvas.delete('box')
            self.canvas.create_rectangle(x0, y0, e.x, e.y, outline='#ffd60a', dash=(4, 2), tags='box')
            return
        if self._ctrl:                                       # slide the selection in the view plane
            right, up, _ = self.basis()
            dx, dy = e.x - self._last[0], e.y - self._last[1]; self._last = (e.x, e.y)
            t = [(dx * right[i] - dy * up[i]) / self.zoom for i in range(3)]
            kind, items = self.targets()
            if kind == 'pieces':
                D = automap.delta_matrix(t)
                for i in items:
                    k = self.pieces[i]['key']
                    old = self.mapping['piece_adjust'].get(k) or [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
                    self.mapping['piece_adjust'][k] = automap.mat4_mul(D, old)
            else:
                for k in items:
                    a = self.part_adj(k); a['t'] = [a['t'][i] + t[i] for i in range(3)]
            self.dirty = True; self.render(fast=True); return
        self.yaw = yaw0 + (e.x - x0) * 0.4
        self.pitch = max(-89.9, min(89.9, pitch0 + (e.y - y0) * 0.4))
        self.render(fast=True)

    def on_up(self, e):
        x0, y0 = self._down[:2]
        if self._moved and self._shift:
            self._box = None
            if self.glv is None: self.canvas.delete('box')
            self.box_select(min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y))
        elif not self._moved:
            self.click_select(e.x, e.y, self._shift)
        else:
            self._idbuf = None; self.update_selection_ui(); self.render()

    def ids_in(self, x0, y0, x1, y1):
        if self.glv is not None or ((self.solid.get() or self.use_ingame()) and Image is not None and np is not None):
            buf = self.id_buffer()
            H, W = buf.shape
            sub = buf[max(0, y0):min(H, y1 + 1), max(0, x0):min(W, x1 + 1)]
            return set(np.unique(sub[sub >= 0]).tolist())
        if self._pick is None: return set()
        if np is not None:
            xi, yi, ids = self._pick
            m = (xi >= x0) & (xi <= x1) & (yi >= y0) & (yi <= y1)
            return set(np.unique(ids[m]).tolist())
        return {pid for px, py, pid in self._pick if x0 <= px <= x1 and y0 <= py <= y1}

    def click_select(self, x, y, add):
        hit = None
        if self.solid.get() and Image is not None and np is not None:
            buf = self.id_buffer()
            if 0 <= y < buf.shape[0] and 0 <= x < buf.shape[1]:
                win = buf[max(0, y - 2):y + 3, max(0, x - 2):x + 3]
                v = win[win >= 0]
                if len(v): hit = int(np.bincount(v).argmax())
        else:
            ids = self.ids_in(x - 6, y - 6, x + 6, y + 6)
            if ids:
                if np is not None:
                    xi, yi, pid = self._pick
                    m = np.isin(pid, list(ids)); d = (xi[m] - x) ** 2 + (yi[m] - y) ** 2
                    hit = int(pid[m][int(np.argmin(d))])
                else:
                    hit = min(((px - x) ** 2 + (py - y) ** 2, pid) for px, py, pid in self._pick if pid in ids)[1]
        if hit is None:
            if not add: self.sel_pieces.clear()
        elif add:
            self.sel_pieces ^= {hit}
        else:
            self.sel_pieces = {hit}
        self.update_selection_ui(); self.render()

    def box_select(self, x0, y0, x1, y1):
        self.sel_pieces |= self.ids_in(int(x0), int(y0), int(x1), int(y1))
        self.update_selection_ui(); self.render()

    def on_pan_down(self, e):
        self._pan0 = (e.x, e.y, list(self.pan))

    def on_pan(self, e):
        x0, y0, p0 = self._pan0
        self.pan = [p0[0] - (e.x - x0) / self.zoom, p0[1] + (e.y - y0) / self.zoom]
        self._idbuf = None; self.render(fast=True)

    def zoom_by(self, f, x, y):
        W, H = self.canvas.winfo_width(), self.canvas.winfo_height()
        wx = (x - W / 2) / self.zoom + self.pan[0]; wy = -(y - H / 2) / self.zoom + self.pan[1]
        self.zoom *= f
        self.pan = [wx - (x - W / 2) / self.zoom, wy + (y - H / 2) / self.zoom]
        self._idbuf = None; self.render()

    def bind_keys(self):
        def move(dr, du, dd):
            def f(e):
                if isinstance(self.focus_get(), (ttk.Entry, tk.Entry)): return
                mult = 10 if e.state & 0x1 else 1
                mm, _ = self.steps(); right, up, fwd = self.basis()
                self.apply_delta(t=[(dr * right[i] + du * up[i] + dd * fwd[i]) * mm * mult for i in range(3)])
            return f

        def turn(sign):
            def f(e):
                _, deg = self.steps(); _, _, fwd = self.basis()
                ax = max(range(3), key=lambda i: abs(fwd[i])); r = [0, 0, 0]
                r[ax] = sign * deg * (10 if e.state & 0x1 else 1) * (1 if fwd[ax] > 0 else -1)
                self.apply_delta(r=r)
            return f
        for key, args in (('<Left>', (-1, 0, 0)), ('<Right>', (1, 0, 0)), ('<Up>', (0, 1, 0)), ('<Down>', (0, -1, 0)),
                          ('<Prior>', (0, 0, 1)), ('<Next>', (0, 0, -1))):
            self.bind(key, move(*args)); self.bind('<Shift-%s' % key[1:], move(*args))
        self.bind('<bracketleft>', turn(-1)); self.bind('<bracketright>', turn(1))
        self.bind('<plus>', lambda e: self.nudge('s', 0, 1)); self.bind('<equal>', lambda e: self.nudge('s', 0, 1))
        self.bind('<minus>', lambda e: self.nudge('s', 0, -1))
        self.bind('<Escape>', lambda e: self.clear_pieces())
        self.bind('1', lambda e: self.set_view(-90, 0)); self.bind('2', lambda e: self.set_view(0, 0))
        self.bind('3', lambda e: self.set_view(-90, 89.9)); self.bind('4', lambda e: self.set_view(180, 0))

    # ================================================================ save
    def save(self):
        for k in [k for k, a in self.mapping['adjust'].items() if automap.is_identity(a)]: self.mapping['adjust'].pop(k)
        I = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        for k in [k for k, M in self.mapping['piece_adjust'].items()
                  if all(abs(M[i][j] - I[i][j]) < 1e-12 for i in range(4) for j in range(4))]:
            self.mapping['piece_adjust'].pop(k)
        self.mapping.pop('saved', None)
        with open(self.map_path, 'w') as fh: json.dump(self.mapping, fh, indent=1)
        self.dirty = False
        self.app.write_log('  ✔ edits saved for %s - Export will use them' % os.path.basename(self.blend), 'ok')
        self.app.on_inputs()
        messagebox.showinfo('Edit model', 'Saved. Click Export in the main window to write the game file.', parent=self)

    def close(self):
        if self.dirty and messagebox.askyesno('Edit model', 'Save your changes before closing?', parent=self):
            self.save()
        self.destroy()

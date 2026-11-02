"""
livery.py - the engine of the Bike workshop: an imported bike's parts and textures, its loose pieces, and the
paint job on top (layers of shapes, logos, text and piece colours, like a racing game's livery editor).

Everything lives in the bike's import folder (Documents\\GameModelExporter\\imports\\<bike>):
    parts.npz / parts.json     the imported geometry and textures (never changed)
    workshop.json              the paint job, deleted pieces, added parts (this module's project)
    decals\\                    copies of the logos / images you used
    added\\                     parts you added (.obj / .jm / .edf), converted
    built\\                     what the .blend is made from: parts.npz/json + painted textures
Texture positions are fractions of the texture: x right, y down (image rows), sizes as fractions of its width.
"""
import copy
import json
import math
import os
import re
import shutil
import time

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

PROJECT = 'workshop.json'

SHAPES = ['Rectangle', 'Rounded box', 'Circle', 'Ring', 'Triangle', 'Diamond', 'Star', 'Hexagon', 'Chevron',
          'Arrow', 'Stripe', 'Half circle', 'Lightning', 'Number plate']


def _star(n=5, inner=0.45):
    pts = []
    for i in range(2 * n):
        r = 1.0 if i % 2 == 0 else inner
        a = math.pi / 2 + i * math.pi / n
        pts.append((r * math.cos(a), -r * math.sin(a)))
    return pts


POLY = {      # shapes as polygons in a -1..1 box (x right, y down)
    'Triangle': [(0, -1), (1, 1), (-1, 1)],
    'Diamond': [(0, -1), (1, 0), (0, 1), (-1, 0)],
    'Star': _star(),
    'Hexagon': [(math.cos(a), math.sin(a)) for a in np.radians(np.arange(0, 360, 60))],
    'Chevron': [(-1, -1), (0, 0.1), (1, -1), (1, -0.1), (0, 1), (-1, -0.1)],
    'Arrow': [(-1, -0.35), (0.25, -0.35), (0.25, -1), (1, 0), (0.25, 1), (0.25, 0.35), (-1, 0.35)],
    'Lightning': [(-0.2, -1), (0.6, -1), (0.15, -0.15), (0.65, -0.15), (-0.35, 1), (-0.05, 0.15), (-0.55, 0.15)],
}


def new_layer(kind, **kw):
    L = {'id': '%x' % int(time.time() * 1e6), 'type': kind, 'name': kw.pop('name', kind.title()), 'visible': True,
         'x': 0.5, 'y': 0.5, 'w': 0.1, 'h': 0.1, 'rot': 0.0, 'color': '#d62828', 'opacity': 1.0, 'blend': 'normal',
         'flip': False, 'clip': []}
    if kind == 'shape': L.update(shape='Rectangle', outline=0.0)
    if kind == 'image': L.update(image='', recolor=False, color='#ffffff')
    if kind == 'text': L.update(text='TEXT', font='', color='#ffffff', w=0.2, h=0.06)
    if kind == 'fill': L.update(targets=[], blend='tint', name='Colour')
    L.update(kw)
    return L


def hex_rgb(c):
    c = (c or '#ffffff').lstrip('#')
    try: return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError: return (255, 255, 255)


# ================================================================== the bike: parts, pieces, picking
class Bike:
    """The imported bike: parts (with any added ones), their loose pieces and textures."""

    def __init__(self, folder, project=None):
        self.folder = folder
        with open(os.path.join(folder, 'parts.json')) as f: self.meta = json.load(f)
        arr = np.load(os.path.join(folder, 'parts.npz'))
        self.parts = []
        for i, p in enumerate(self.meta['parts']):
            self.parts.append(dict(p, P=arr['P%d' % i].astype(np.float64), N=arr['N%d' % i].astype(np.float64),
                                   U=arr['U%d' % i].astype(np.float64), F=arr['F%d' % i].astype(np.int64), added=None))
        self.n_orig = len(self.parts)
        self.images = dict(self.meta.get('images') or {})          # material -> png (in folder)
        self.project = project or load_project(folder)
        self._tex = {}
        self.sync_added()

    # ---- added parts
    def sync_added(self):
        del self.parts[self.n_orig:]
        for k, a in enumerate(self.project.get('added', [])):
            try:
                z = np.load(os.path.join(self.folder, a['file']))
            except OSError:
                continue
            P = z['P'].astype(np.float64)
            R = rot_xyz(a.get('r', [0, 0, 0])) * float(a.get('s', 1.0))
            c = P.mean(0) if len(P) else np.zeros(3)
            P = (P - c) @ R.T + np.asarray(a.get('t', [0, 0, 0]), np.float64)
            N = z['N'].astype(np.float64) @ rot_xyz(a.get('r', [0, 0, 0])).T
            mat = a.get('mat') or ('added_%d' % k)
            if a.get('tex'): self.images.setdefault(mat, a['tex'])
            self.parts.append({'name': a.get('name', 'added %d' % (k + 1)), 'group': 'added', 'unit': a.get('unit'),
                               'mat': mat, 'P': P, 'N': N, 'U': z['U'].astype(np.float64), 'F': z['F'].astype(np.int64),
                               'added': k})
        if getattr(self, '_pieces', None) is not None: self._pieces = self._pieces[:self.n_orig]
        if getattr(self, '_patches', None) is not None: self._patches = self._patches[:self.n_orig]

    # ---- loose pieces (parts welded by position, then split where they don't touch)
    def pieces(self):
        """[(part index, tri -> piece number, number of pieces)]; piece keys are 'p<part>:<piece>'."""
        out = getattr(self, '_pieces', None) or []
        for p in self.parts[len(out):]:
            out.append(_components(p['P'], p['F']))
        self._pieces = out
        return out

    def patches(self):
        """Like pieces(), but split along texture seams too (each patch of the texture): keys 'u<part>:<n>'."""
        out = getattr(self, '_patches', None) or []
        for p in self.parts[len(out):]:
            out.append(_components(p['P'], p['F'], weld=False))
        self._patches = out
        return out

    def piece_tris(self, key):
        i, c = (int(v) for v in key[1:].split(':'))
        lab = (self.patches() if key[0] == 'u' else self.pieces())[i]
        return i, np.nonzero(lab == c)[0]

    def deleted_mask(self, i):
        """Triangles of part i that are deleted."""
        lab = self.pieces()[i]
        dels = [int(k.split(':')[1]) for k in self.project.get('deleted', []) if k.startswith('p%d:' % i)]
        if not dels: return np.zeros(len(lab), bool)
        return np.isin(lab, dels) if dels else np.zeros(len(lab), bool)

    def piece_mat(self, key):
        try: return self.parts[int(key[1:].split(':')[0])]['mat']
        except (ValueError, IndexError): return None

    def key_at(self, part, tri, kind='p'):
        lab = (self.patches() if kind == 'u' else self.pieces())[part]
        return '%s%d:%d' % (kind, part, int(lab[tri]))

    def materials(self):
        """Materials in use (that have, or can get, a texture), main skin first."""
        cnt = {}
        for p in self.parts: cnt[p['mat']] = cnt.get(p['mat'], 0) + len(p['F'])
        return sorted(cnt, key=lambda m: -cnt[m])

    # ---- textures
    def base_texture(self, mat):
        """The texture under the paint job (an MX Bikes paint if one is chosen), PIL RGBA."""
        b = (self.project.get('base') or {}).get(mat)
        key = (mat, json.dumps(b, sort_keys=True))
        if key in self._tex: return self._tex[key]
        img = None
        if b and b.get('pnt'):
            try:
                import pnt_io
                idx = pnt_io.index(b['pnt'])
                t = next((t for t in idx['textures'] if t['name'] == b.get('tex')), None) or pnt_io.colour_textures(idx)[0]
                img = Image.fromarray(pnt_io.texture(b['pnt'], t), 'RGBA')
            except Exception:
                img = None
        elif b and b.get('blank'):
            img = Image.new('RGBA', tuple(b.get('size', (2048, 2048))), hex_rgb(b.get('color', '#c8c8c8')) + (255,))
        if img is None and self.images.get(mat):
            img = Image.open(os.path.join(self.folder, self.images[mat])).convert('RGBA')
        if img is None:
            img = Image.new('RGBA', (2048, 2048), (200, 200, 200, 255))
        self._tex[key] = img
        return img

    def has_texture(self, mat):
        return bool(self.images.get(mat)) or bool((self.project.get('base') or {}).get(mat))

    # ---- UV footprints of pieces (for colouring a piece, and keeping decals on their piece)
    def piece_mask(self, keys, size):
        """'L' image of size (w, h): 255 where the pieces' triangles lie in the texture."""
        W, H = size
        m = Image.new('L', (W, H), 0); dr = ImageDraw.Draw(m)
        for key in keys:
            try: i, tris = self.piece_tris(key)
            except (ValueError, IndexError): continue
            p = self.parts[i]
            uv = p['U'][p['F'][tris]]                                   # (n, 3, 2)
            xy = np.stack([uv[..., 0] * W, (1 - uv[..., 1]) * H], -1)
            for t in xy.reshape(len(xy), 6).tolist():
                dr.polygon(t, fill=255)
        if keys: m = m.filter(ImageFilter.MaxFilter(3))                   # cover the seams
        return m

    def uv_wire(self, mat, size, color=(255, 255, 255, 70)):
        """Transparent image with the UV layout of the material's pieces drawn on it."""
        W, H = size
        img = Image.new('RGBA', (W, H), (0, 0, 0, 0)); dr = ImageDraw.Draw(img)
        for i, p in enumerate(self.parts):
            if p['mat'] != mat: continue
            keep = ~self.deleted_mask(i)
            uv = p['U'][p['F'][keep]]
            xy = np.stack([uv[..., 0] * W, (1 - uv[..., 1]) * H], -1)
            step = max(1, len(xy) // 60000)
            for t in xy[::step].reshape(-1, 6).tolist():
                dr.polygon(t, outline=color)
        return img

    # ---- picking: what is under a ray from the 3D view
    def ray_hit(self, o, d):
        """(part, triangle, point, uv) of the nearest visible triangle hit by the ray o + t d, or None."""
        best = None
        for i, p in enumerate(self.parts):
            F = p['F']; keep = ~self.deleted_mask(i)
            if not keep.any(): continue
            idx = np.nonzero(keep)[0]
            A, B, C = p['P'][F[idx, 0]], p['P'][F[idx, 1]], p['P'][F[idx, 2]]
            e1, e2 = B - A, C - A
            pv = np.cross(d, e2); det = (e1 * pv).sum(1)
            ok = np.abs(det) > 1e-12
            inv = np.zeros_like(det); inv[ok] = 1 / det[ok]
            tv = o - A; u = (tv * pv).sum(1) * inv
            qv = np.cross(tv, e1); v = (d * qv).sum(1) * inv; t = (e2 * qv).sum(1) * inv
            hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6)
            if not hit.any(): continue
            k = np.nonzero(hit)[0][np.argmin(t[hit])]
            if best is None or t[k] < best[0]:
                tri = idx[k]; w = np.array([1 - u[k] - v[k], u[k], v[k]])
                uv = (p['U'][F[tri]] * w[:, None]).sum(0)
                best = (t[k], i, int(tri), o + t[k] * d, uv)
        if best is None: return None
        _, i, tri, pt, uv = best
        lab = self.pieces()[i]
        return {'part': i, 'tri': tri, 'point': pt, 'uv': uv, 'piece': 'p%d:%d' % (i, int(lab[tri])),
                'mat': self.parts[i]['mat'], 'img': (float(uv[0]), float(1 - uv[1]))}

    # ---- texture <-> surface (for "copy to the other side")
    def _frame_at(self, i, tri):
        p = self.parts[i]; F = p['F'][tri]
        A, B, C = p['P'][F]; ua, ub, uc = (np.array([u[0], 1 - u[1]]) for u in p['U'][F])
        E = np.stack([B - A, C - A], 1)                 # 3x2: 3D edges
        D = np.stack([ub - ua, uc - ua], 1)             # 2x2: image edges
        return A, ua, E, D

    def surface_at(self, mat, x, y):
        """(part, tri, 3D point) under texture point (x, y) of a material, or None."""
        for i, p in enumerate(self.parts):
            if p['mat'] != mat: continue
            F = p['F']; keep = ~self.deleted_mask(i)
            U = np.stack([p['U'][:, 0], 1 - p['U'][:, 1]], 1)
            a, b, c = U[F[:, 0]], U[F[:, 1]], U[F[:, 2]]
            v0, v1 = b - a, c - a; v2 = np.array([x, y]) - a
            den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
            ok = keep & (np.abs(den) > 1e-14)
            s = np.where(ok, (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / np.where(ok, den, 1), -1)
            t = np.where(ok, (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / np.where(ok, den, 1), -1)
            inside = ok & (s >= -1e-6) & (t >= -1e-6) & (s + t <= 1 + 1e-6)
            if inside.any():
                k = int(np.nonzero(inside)[0][0])
                A, B, C = p['P'][F[k]]
                return i, k, A + s[k] * (B - A) + t[k] * (C - A)
        return None

    def decal_frame(self, mat, L):
        """(centre, half-width axis, half-height axis) of a design on the bike's surface (3D), or None."""
        src = self.surface_at(mat, L['x'], L['y'])
        if src is None: return None
        i, tri, pt = src
        A, ua, E, D = self._frame_at(i, tri)
        Dinv = np.linalg.pinv(D)
        W, H = self.base_texture(mat).size
        a = W / float(H)
        th = math.radians(L.get('rot', 0))
        ex = np.array([math.cos(th), -math.sin(th) * a]) * L['w'] / 2 * (-1 if L.get('flip') else 1)
        up = np.array([-math.sin(th), -math.cos(th) * a]) * L['h'] / 2
        return pt, E @ (Dinv @ ex), E @ (Dinv @ up)

    def layer_at(self, mat, x, y, layers):
        """The top design (not a colour fill) covering texture point (x, y)."""
        W, H = self.base_texture(mat).size
        for L in reversed(layers):
            if L['type'] == 'fill' or not L.get('visible', True): continue
            dx, dy = (x - L['x']) * W, (y - L['y']) * H
            th = math.radians(L.get('rot', 0))
            lx = dx * math.cos(th) - dy * math.sin(th); ly = dx * math.sin(th) + dy * math.cos(th)
            if abs(lx) <= L['w'] * W / 2 and abs(ly) <= L['h'] * W / 2: return L
        return None

    def mirror_layer(self, mat, L):
        """A copy of a layer for the bike's other side (mirrored across x = 0, reading the right way round)."""
        src = self.surface_at(mat, L['x'], L['y'])
        if src is None: return None
        i, tri, pt = src
        A, ua, E, D = self._frame_at(i, tri)
        Dinv = np.linalg.pinv(D)
        th = math.radians(L['rot'])
        ex = np.array([math.cos(th), -math.sin(th)]) * (-1 if L.get('flip') else 1)
        up = np.array([-math.sin(th), -math.cos(th)])
        t3, s3 = E @ (Dinv @ ex), E @ (Dinv @ up)                  # decal axes on the surface (3D)
        R = np.diag([-1.0, 1.0, -1.0])                             # seen from the other side: turn about y
        tgt, t3, s3 = pt * np.array([-1.0, 1.0, 1.0]), R @ t3, R @ s3
        n_src = np.cross(E[:, 0], E[:, 1])
        best = None
        for j, p in enumerate(self.parts):
            F = p['F']; keep = ~self.deleted_mask(j)
            cen = p['P'][F].mean(1)
            d = np.linalg.norm(cen - tgt, axis=1); d[~keep] = 1e9
            nrm = np.cross(p['P'][F[:, 1]] - p['P'][F[:, 0]], p['P'][F[:, 2]] - p['P'][F[:, 0]])
            same = np.sign(nrm[:, 0]) == -np.sign(n_src[0]) if abs(n_src[0]) > 1e-12 else np.ones(len(F), bool)
            d[~same] += 0.05
            k = int(np.argmin(d))
            if best is None or d[k] < best[0]: best = (d[k], j, k)
        if best is None or best[0] > 0.08: return None
        _, j, k = best
        A2, ua2, E2, D2 = self._frame_at(j, k)
        J = D2 @ np.linalg.pinv(E2)                                 # 3D -> image near the target
        uv = ua2 + J @ (tgt - A2)
        exd, upd = J @ t3, J @ s3
        if np.linalg.norm(upd) < 1e-9 or np.linalg.norm(exd) < 1e-9: return None
        thd = math.atan2(-upd[0], -upd[1])
        xd = np.array([math.cos(thd), -math.sin(thd)])
        M = copy.deepcopy(L)
        M.update(id='%x' % int(time.time() * 1e6), name=L['name'] + ' (other side)', x=float(uv[0]), y=float(uv[1]),
                 rot=math.degrees(thd), flip=bool(np.dot(exd, xd) < 0), w=L['w'] * float(np.linalg.norm(exd)),
                 h=L['h'] * float(np.linalg.norm(upd)), clip=[])
        if L.get('clip'):
            lab = self.pieces()[j]; M['clip'] = ['p%d:%d' % (j, int(lab[k]))]
        if self.parts[j]['mat'] != mat: M['_mat'] = self.parts[j]['mat']
        return M


def _components(P, F, weld=True):
    """Triangle -> loose-piece number (weld: vertices at the same spot count as joined)."""
    if not len(F): return np.zeros(0, np.int64)
    if weld:
        q = np.round(P / 1e-5).astype(np.int64)
        _, weld = np.unique(q, axis=0, return_inverse=True)
        weld = weld.reshape(-1)
    else:
        weld = np.arange(len(P))
    parent = np.arange(weld.max() + 1)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    W = weld[F]
    for a, b, c in W.tolist():
        ra, rb, rc = find(a), find(b), find(c)
        if ra != rb: parent[rb] = ra
        if ra != rc: parent[find(rc)] = find(ra)
    roots = np.array([find(a) for a in W[:, 0].tolist()])
    _, lab = np.unique(roots, return_inverse=True)
    return lab.reshape(-1)


def rot_xyz(r):
    rx, ry, rz = (math.radians(v) for v in r)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


# ================================================================== the project file
def load_project(folder):
    p = os.path.join(folder, PROJECT)
    try:
        with open(p) as f: d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault('version', 1); d.setdefault('layers', {}); d.setdefault('base', {})
    d.setdefault('deleted', []); d.setdefault('added', [])
    return d


def _plain(o):
    if isinstance(o, np.generic): return o.item()
    if isinstance(o, np.ndarray): return o.tolist()
    raise TypeError('%r can not be saved' % type(o))


def save_project(folder, project):
    p = os.path.join(folder, PROJECT)
    tmp = p + '.tmp'
    with open(tmp, 'w') as f: json.dump(project, f, indent=1, default=_plain)
    os.replace(tmp, p)


def copy_into(folder, sub, path):
    """Copy a file the user picked into the bike folder (so the project keeps working); returns its relative path."""
    d = os.path.join(folder, sub); os.makedirs(d, exist_ok=True)
    base = re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.basename(path))
    out = os.path.join(d, base); k = 1
    while os.path.exists(out) and not _same_file(out, path):
        stem, ext = os.path.splitext(base); out = os.path.join(d, '%s_%d%s' % (stem, k, ext)); k += 1
    if not os.path.exists(out): shutil.copy2(path, out)
    return os.path.relpath(out, folder).replace('\\', '/')


def _same_file(a, b):
    try: return os.path.getsize(a) == os.path.getsize(b) and open(a, 'rb').read() == open(b, 'rb').read()
    except OSError: return False


# ================================================================== drawing the layers
def _font(path, size):
    for f in ([path] if path else []) + ['arialbd.ttf', 'arial.ttf', 'DejaVuSans-Bold.ttf', 'DejaVuSans.ttf']:
        try: return ImageFont.truetype(f, size)
        except (OSError, ValueError): continue
    return ImageFont.load_default()


def shape_mask(shape, pw, ph, outline=0.0, ss=3):
    """'L' mask of a shape filling pw x ph pixels (outline > 0: only a border that thick, as a share of the size)."""
    W, H = max(1, pw * ss), max(1, ph * ss)
    m = Image.new('L', (W, H), 0); dr = ImageDraw.Draw(m)
    box = (0, 0, W - 1, H - 1)

    def poly(pts):
        return [((x + 1) / 2 * (W - 1), (y + 1) / 2 * (H - 1)) for x, y in pts]
    if shape in ('Rectangle', 'Stripe'):
        dr.rectangle(box, fill=255)
    elif shape == 'Rounded box':
        dr.rounded_rectangle(box, radius=min(W, H) * 0.25, fill=255)
    elif shape == 'Number plate':
        dr.rounded_rectangle(box, radius=min(W, H) * 0.12, fill=255)
    elif shape in ('Circle', 'Ring'):
        dr.ellipse(box, fill=255)
        if shape == 'Ring' and not outline: outline = 0.18
    elif shape == 'Half circle':
        dr.pieslice((0, 0, W - 1, 2 * H - 1), 180, 360, fill=255)
    elif shape in POLY:
        dr.polygon(poly(POLY[shape]), fill=255)
    else:
        dr.rectangle(box, fill=255)
    if outline and outline > 0:
        k = max(1, int(round(min(W, H) * outline)))
        inner = m.filter(ImageFilter.MinFilter(2 * (k // 2) + 1)) if k < 60 else m.resize(
            (max(1, W - 2 * k), max(1, H - 2 * k)))
        if inner.size != m.size:
            pad = Image.new('L', m.size, 0); pad.paste(inner, (k, k)); inner = pad
        m = ImageChops.subtract(m, inner)
    return m.resize((max(1, pw), max(1, ph)), Image.BOX)


def layer_rgba(L, W, folder, scale_w):
    """The layer as an RGBA image (before turning), and its size in pixels, for a texture W pixels wide."""
    pw = max(1, int(round(L['w'] * scale_w))); ph = max(1, int(round(L['h'] * scale_w)))
    col = hex_rgb(L.get('color'))
    if L['type'] == 'shape':
        m = shape_mask(L.get('shape', 'Rectangle'), pw, ph, float(L.get('outline') or 0))
        img = Image.new('RGBA', (pw, ph), col + (0,)); img.putalpha(m)
    elif L['type'] == 'image':
        try:
            src = Image.open(os.path.join(folder, L['image'])).convert('RGBA')
        except (OSError, KeyError):
            src = Image.new('RGBA', (8, 8), (255, 0, 255, 255))
        img = src.resize((pw, ph), Image.LANCZOS)
        if L.get('recolor'):
            a = img.getchannel('A'); img = Image.new('RGBA', img.size, col + (0,)); img.putalpha(a)
    elif L['type'] == 'text':
        f = _font(L.get('font'), 160)
        txt = L.get('text') or ' '
        bb = ImageDraw.Draw(Image.new('L', (8, 8))).textbbox((0, 0), txt, font=f)
        tm = Image.new('L', (max(1, bb[2] - bb[0] + 8), max(1, bb[3] - bb[1] + 8)), 0)
        ImageDraw.Draw(tm).text((4 - bb[0], 4 - bb[1]), txt, font=f, fill=255)
        m = tm.resize((pw, ph), Image.LANCZOS)
        img = Image.new('RGBA', (pw, ph), col + (0,)); img.putalpha(m)
    else:
        return None
    if L.get('flip'): img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return img


def _blend_region(canvas, over, x0, y0, mode):
    """Put RGBA 'over' onto canvas at (x0, y0): normal, or tint (keeps the texture's shading and detail)."""
    W, H = canvas.size
    ax0, ay0 = max(0, x0), max(0, y0); ax1, ay1 = min(W, x0 + over.width), min(H, y0 + over.height)
    if ax1 <= ax0 or ay1 <= ay0: return
    o = over.crop((ax0 - x0, ay0 - y0, ax1 - x0, ay1 - y0))
    if mode == 'tint':
        base = np.asarray(canvas.crop((ax0, ay0, ax1, ay1))).astype(np.float32)
        ov = np.asarray(o).astype(np.float32)
        lum = (base[..., 0] * 0.3 + base[..., 1] * 0.59 + base[..., 2] * 0.11) / 255.0
        shade = np.clip(lum / max(0.35, float(np.percentile(lum, 70)) if lum.size else 0.5), 0, 1.35)[..., None]
        tinted = np.clip(ov[..., :3] * shade, 0, 255)
        a = ov[..., 3:4] / 255.0
        out = base.copy(); out[..., :3] = base[..., :3] * (1 - a) + tinted * a
        canvas.paste(Image.fromarray(out.astype(np.uint8), 'RGBA'), (ax0, ay0))
    else:
        region = canvas.crop((ax0, ay0, ax1, ay1))
        canvas.paste(Image.alpha_composite(region, o), (ax0, ay0))


def draw_layer(canvas, L, bike, mat, masks=None):
    """Draw one layer onto canvas (PIL RGBA, the texture at some size)."""
    if not L.get('visible', True) or float(L.get('opacity', 1)) <= 0: return
    W, H = canvas.size
    op = float(L.get('opacity', 1.0))
    masks = masks if masks is not None else {}

    def mask_for(keys):
        k = (tuple(sorted(keys)), W, H)
        if k not in masks: masks[k] = bike.piece_mask(keys, (W, H))
        return masks[k]
    if L['type'] == 'fill':
        keys = [k for k in L.get('targets', []) if bike.piece_mat(k) == mat] if L.get('targets') else []
        if not keys and not L.get('whole'): return
        m = mask_for(keys) if keys else Image.new('L', (W, H), 255)
        col = Image.new('RGBA', (W, H), hex_rgb(L.get('color')) + (0,))
        col.putalpha(m.point(lambda v: int(v * op)))
        _blend_region(canvas, col, 0, 0, L.get('blend', 'tint'))
        return
    img = layer_rgba(L, W, bike.folder, W)
    if img is None: return
    if abs(L.get('rot', 0)) > 1e-6: img = img.rotate(L['rot'], resample=Image.BICUBIC, expand=True)
    if op < 1: img.putalpha(img.getchannel('A').point(lambda v: int(v * op)))
    x0 = int(round(L['x'] * W - img.width / 2)); y0 = int(round(L['y'] * H - img.height / 2))
    if L.get('clip'):
        m = mask_for(L['clip']).crop((x0, y0, x0 + img.width, y0 + img.height))
        img.putalpha(ImageChops.multiply(img.getchannel('A'), m))
    _blend_region(canvas, img, x0, y0, L.get('blend', 'normal'))


def composite(bike, mat, size=None, layers=None, masks=None):
    """The painted texture of a material (PIL RGBA) at a size (default: the texture's own)."""
    base = bike.base_texture(mat)
    img = base.copy() if size is None or tuple(size) == base.size else base.resize(tuple(size), Image.BILINEAR)
    for L in (layers if layers is not None else bike.project.get('layers', {}).get(mat, [])):
        draw_layer(img, L, bike, mat, masks)
    return img


# ================================================================== adding parts from files
def read_part_file(path, units='m', up='y'):
    """(P, N, U, F, texture path or None) of a model file: .obj, MX Simulator .jm, MX Bikes .edf (all its parts)."""
    low = path.lower()
    tex = None
    if low.endswith('.obj'):
        P, N, U, F, tex = _read_obj(path)
    elif low.endswith('.jm'):
        import bike_import
        P, N, U, F = bike_import.read_jm(open(path, 'rb').read())
        P, N, F = bike_import.mxs_to_senders(P, N, F)
        units, up = 'm', 'y'
    elif low.endswith('.edf'):
        import edf_io
        parts, texs = edf_io.parts(path)
        if not parts: raise ValueError('no geometry in %s' % os.path.basename(path))
        Ps, Ns, Us, Fs, b = [], [], [], [], 0
        for p in parts:
            Ps.append(p['P']); Ns.append(p['N']); Us.append(np.column_stack([p['UV'][:, 0], 1 - p['UV'][:, 1]]))
            Fs.append(p['F'] + b); b += len(p['P'])
        P, N, U, F = (np.concatenate(Ps), np.concatenate(Ns), np.concatenate(Us), np.concatenate(Fs))
        if texs:
            k = max(texs, key=lambda m: texs[m].shape[0] * texs[m].shape[1])
            tex = ('array', texs[k])
        units, up = 'm', 'y'
    else:
        raise ValueError('use an .obj, MX Simulator .jm or MX Bikes .edf file')
    P = np.asarray(P, np.float64) * {'m': 1.0, 'cm': 0.01, 'mm': 0.001, 'ft': 0.3048, 'in': 0.0254}.get(units, 1.0)
    N = np.asarray(N, np.float64)
    if up == 'z':                                   # Blender / 3ds Max style: z up, -y forward
        P = np.column_stack([P[:, 0], P[:, 2], -P[:, 1]]); N = np.column_stack([N[:, 0], N[:, 2], -N[:, 1]])
    return P, N, np.asarray(U, np.float64), np.asarray(F, np.int64), tex


def _read_obj(path):
    V, T, Nn, faces, tex = [], [], [], [], None
    mtl, cur = {}, None
    folder = os.path.dirname(os.path.abspath(path))
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.split()
            if not s: continue
            if s[0] == 'v': V.append([float(x) for x in s[1:4]])
            elif s[0] == 'vt': T.append([float(x) for x in s[1:3]] + [0.0] * (2 - len(s[1:3])))
            elif s[0] == 'vn': Nn.append([float(x) for x in s[1:4]])
            elif s[0] == 'f':
                idx = []
                for w in s[1:]:
                    a = (w.split('/') + ['', ''])[:3]
                    idx.append(tuple(int(x) if x else 0 for x in a))
                for k in range(1, len(idx) - 1): faces.append((idx[0], idx[k], idx[k + 1]))
            elif s[0] == 'mtllib':
                mp = os.path.join(folder, line.split(None, 1)[1].strip())
                try:
                    with open(mp, encoding='utf-8', errors='replace') as mf:
                        name = None
                        for ml in mf:
                            t = ml.split()
                            if t and t[0] == 'newmtl': name = ml.split(None, 1)[1].strip()
                            elif t and t[0] == 'map_Kd' and name: mtl[name] = os.path.join(os.path.dirname(mp), ml.split(None, 1)[1].strip().split()[-1])
                except OSError:
                    pass
            elif s[0] == 'usemtl':
                cur = line.split(None, 1)[1].strip()
                if tex is None and mtl.get(cur) and os.path.isfile(mtl[cur]): tex = mtl[cur]
    if not faces: raise ValueError('no faces in %s' % os.path.basename(path))
    V = np.array(V, np.float64); T = np.array(T or [[0, 0]], np.float64); Nn = np.array(Nn or [[0, 1, 0]], np.float64)
    key, P, U, N, F = {}, [], [], [], []
    for tri in faces:
        out = []
        for vi, ti, ni in tri:
            k = (vi, ti, ni)
            if k not in key:
                key[k] = len(P)
                P.append(V[vi - 1 if vi > 0 else vi]); U.append(T[ti - 1 if ti > 0 else ti] if ti else [0, 0])
                N.append(Nn[ni - 1 if ni > 0 else ni] if ni else [0, 0, 0])
            out.append(key[k])
        F.append(out)
    P, U, N, F = np.array(P), np.array(U)[:, :2], np.array(N), np.array(F, np.int64)
    if not np.abs(N).sum(1).all():                  # missing normals: face normals
        fn = np.cross(P[F[:, 1]] - P[F[:, 0]], P[F[:, 2]] - P[F[:, 0]])
        N = np.zeros_like(P)
        for c in range(3): np.add.at(N, F[:, c], fn)
        N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
    return P, N, U, F, tex


def add_part(folder, project, path, name=None, unit=None, units='m', up='y', near=None):
    """Convert a model file into the bike folder and add it to the project. Returns the new entry."""
    P, N, U, F, tex = read_part_file(path, units, up)
    d = os.path.join(folder, 'added'); os.makedirs(d, exist_ok=True)
    stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', os.path.splitext(os.path.basename(path))[0]) or 'part'
    k = 1
    while os.path.exists(os.path.join(d, '%s_%d.npz' % (stem, k))): k += 1
    fn = 'added/%s_%d.npz' % (stem, k)
    np.savez(os.path.join(folder, fn), P=P.astype(np.float32), N=N.astype(np.float32), U=U.astype(np.float32),
             F=F.astype(np.int32))
    entry = {'file': fn, 'name': name or os.path.basename(path), 'unit': unit,
             't': [float(v) for v in (near if near is not None else P.mean(0))], 'r': [0.0, 0.0, 0.0], 's': 1.0}
    if tex is not None:
        tn = 'added/%s_%d.png' % (stem, k)
        if isinstance(tex, tuple): Image.fromarray(tex[1]).save(os.path.join(folder, tn))
        else: Image.open(tex).convert('RGBA').save(os.path.join(folder, tn))
        entry['tex'] = tn; entry['mat'] = 'added_%s_%d' % (stem, k)
    else:
        entry['mat'] = 'added_%s_%d' % (stem, k)
    project.setdefault('added', []).append(entry)
    return entry


# ================================================================== building what the .blend is made from
def build(bike, log=print):
    """Write built/parts.npz + parts.json + painted textures; returns the json path."""
    folder = bike.folder; out = os.path.join(folder, 'built'); os.makedirs(out, exist_ok=True)
    arrays, meta, n_del = {}, [], 0
    for i, p in enumerate(bike.parts):
        keep = ~bike.deleted_mask(i)
        n_del += int((~keep).sum())
        F = p['F'][keep]
        if not len(F): continue
        used, inv = np.unique(F, return_inverse=True)
        k = len(meta)
        arrays['P%d' % k] = p['P'][used].astype(np.float32); arrays['N%d' % k] = p['N'][used].astype(np.float32)
        arrays['U%d' % k] = p['U'][used].astype(np.float32); arrays['F%d' % k] = inv.reshape(-1, 3).astype(np.int32)
        meta.append({'name': p['name'], 'group': p.get('group', ''), 'mat': p['mat'], 'unit': p.get('unit')})
    np.savez(os.path.join(out, 'parts.npz'), **arrays)
    images = {}
    for mat in bike.materials():
        if not bike.has_texture(mat) and not bike.project.get('layers', {}).get(mat): continue
        t0 = time.time()
        img = composite(bike, mat)
        fn = re.sub(r'[^A-Za-z0-9_.-]+', '_', mat) + '.png'
        img.save(os.path.join(out, fn), optimize=False, compress_level=3)
        images[mat] = fn
        log('INFO painted %s (%dx%d, %d layers, %.1f s)' % (mat, img.width, img.height,
                                                            len(bike.project.get('layers', {}).get(mat, [])), time.time() - t0))
    jp = os.path.join(out, 'parts.json')
    with open(jp, 'w') as f: json.dump({'parts': meta, 'images': images}, f, indent=1)
    if n_del: log('INFO %d triangles of deleted pieces left out' % n_del)
    return jp

"""
hm_core.py - the heightmap studio's engine (no user interface): a project is a square heightmap built
non-destructively from

    terrain (generated / imported / the game's own ground) + terrain sculpting
    -> track paths carved in (width, cut, banking, elevation smoothing, start funnel)
    -> jumps on the tracks (profiles from a template library)
    -> ruts / berms and bumps
    -> final sculpting
with a mask that hides the track features where painted, and a surface map (which of the game's ground
layers goes where).  Everything is in metres; x = image columns (left -> right), z = image rows (top -> down).
Only numpy is needed.
"""
import copy
import io
import json
import math
import os
import zipfile

import numpy as np

SURFACES = ['sand', 'gravel', 'dirt', 'rock', 'highway', 'path', 'forest_floor', 'grass']   # the game's layers
SURF_NAMES = {'gravel': 'Track dirt 1', 'path': 'Track dirt 2', 'dirt': 'Light dirt', 'sand': 'Sand',
              'rock': 'Rock', 'forest_floor': 'Forest floor', 'grass': 'Grass', 'highway': 'Tarmac'}
def mix_rgb(rgb, smix, surf_rgb_of):
    """Show a mixed-in second surface: blend each pixel's colour towards that surface's by its amount."""
    m = smix >= 0
    if not m.any(): return
    lay = (smix[m] >> 8).astype(np.int64); amt = (smix[m] & 255).astype(np.float32)[:, None] / 255.0
    cols = np.array([surf_rgb_of(k) for k in range(len(SURFACES))], np.float32)[lay]
    rgb[m, :3] = (rgb[m, :3] * (1 - amt) + cols * amt).astype(np.uint8)


SURF_RGB = {'sand': (167, 126, 86), 'gravel': (120, 110, 96), 'dirt': (157, 126, 95), 'rock': (147, 137, 115),
            'highway': (96, 90, 84), 'path': (103, 80, 59), 'forest_floor': (109, 77, 52), 'grass': (71, 79, 35)}


# =================================================================== small helpers
def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _box1(a, r, axis):
    if r < 1: return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim; pad[axis] = (r + 1, r)
    p = np.pad(a, pad, mode='edge')
    c = np.cumsum(p, axis=axis, dtype=np.float64)
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def blur(a, radius_px):
    """Gaussian-like blur (three box passes), edges extended."""
    r = int(round(radius_px / 1.7))
    if r < 1: return a.astype(np.float32, copy=True)
    out = a.astype(np.float32)
    for _ in range(3):
        out = _box1(_box1(out, r, 0), r, 1)
    return out


def blur1d(v, radius):
    r = int(round(radius / 1.7))
    if r < 1 or len(v) < 3: return np.asarray(v, np.float32)
    out = np.asarray(v, np.float32)
    for _ in range(3): out = _box1(out, min(r, len(v)), 0)
    return out


def bilinear(A, fx, fz):
    """A sampled at fractional (col, row) positions, clamped to the edges."""
    ry, rx = A.shape
    fx = np.clip(fx, 0, rx - 1.0001); fz = np.clip(fz, 0, ry - 1.0001)
    ix = fx.astype(np.int64); iz = fz.astype(np.int64); tx = fx - ix; tz = fz - iz
    a = A[iz, ix]; b = A[iz, ix + 1]; c = A[iz + 1, ix]; d = A[iz + 1, ix + 1]
    return (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz


def _hash(ix, iz, seed):
    h = (ix.astype(np.int64) * 374761393 + iz.astype(np.int64) * 668265263 + int(seed) * 1442695041) & 0xffffffff
    h = ((h ^ (h >> 13)) * 1274126177) & 0xffffffff
    h = h ^ (h >> 16)
    return (h & 0xffffff).astype(np.float32) / float(0xffffff)


def value_noise(x, z, seed):
    """Smooth value noise in -1..1 at real coordinates (one unit = one lattice cell)."""
    ix = np.floor(x); iz = np.floor(z)
    tx = x - ix; tz = z - iz
    tx = tx * tx * tx * (tx * (tx * 6 - 15) + 10); tz = tz * tz * tz * (tz * (tz * 6 - 15) + 10)
    ix = ix.astype(np.int64); iz = iz.astype(np.int64)
    a = _hash(ix, iz, seed); b = _hash(ix + 1, iz, seed); c = _hash(ix, iz + 1, seed); d = _hash(ix + 1, iz + 1, seed)
    return ((a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz) * 2 - 1


def fbm(x, z, seed, size, octaves=5, persistence=0.5, lacunarity=2.0, ridged=False):
    """Fractal noise in metres: features about `size` metres across, roughly -1..1."""
    total = np.zeros(np.broadcast(x, z).shape, np.float32); amp = 1.0; f = 1.0 / max(size, 1e-3); norm = 0.0
    for o in range(int(octaves)):
        n = value_noise(x * f + 17.3 * o, z * f - 9.1 * o, seed + 101 * o)
        if ridged: n = 1 - 2 * np.abs(n)
        total += n * amp; norm += amp
        amp *= persistence; f *= lacunarity
    return total / max(norm, 1e-6)


# =================================================================== splines
def catmull(points, closed=False, spacing=0.5):
    """Points [(x, z), ...] -> evenly spaced samples along a centripetal Catmull-Rom curve.
    Returns (P (n,2), T unit tangents (n,2), s distance along (n,), k: fractional index of the source point
    each sample came from (n,) - used to interpolate per-point values)."""
    P = np.asarray(points, np.float64)
    if len(P) < 2: return None
    if closed: P = np.vstack([P, P[:1]])
    ext = np.vstack([P[:1] * 2 - P[1:2] if not closed else P[-2:-1], P,
                     P[-1:] * 2 - P[-2:-1] if not closed else P[1:2]])
    out, ks = [], []
    for i in range(len(P) - 1):
        p0, p1, p2, p3 = ext[i], ext[i + 1], ext[i + 2], ext[i + 3]
        seg = float(np.linalg.norm(p2 - p1))
        n = max(2, int(math.ceil(seg / (spacing * 0.5))))
        t = np.linspace(0, 1, n, endpoint=False)[:, None]

        def tj(ti, a, b): return ti + max(float(np.linalg.norm(b - a)) ** 0.5, 1e-6)
        t0 = 0.0; t1 = tj(t0, p0, p1); t2 = tj(t1, p1, p2); t3 = tj(t2, p2, p3)
        tt = t1 + t * (t2 - t1)
        a1 = (t1 - tt) / (t1 - t0) * p0 + (tt - t0) / (t1 - t0) * p1
        a2 = (t2 - tt) / (t2 - t1) * p1 + (tt - t1) / (t2 - t1) * p2
        a3 = (t3 - tt) / (t3 - t2) * p2 + (tt - t2) / (t3 - t2) * p3
        b1 = (t2 - tt) / (t2 - t0) * a1 + (tt - t0) / (t2 - t0) * a2
        b2 = (t3 - tt) / (t3 - t1) * a2 + (tt - t1) / (t3 - t1) * a3
        out.append((t2 - tt) / (t2 - t1) * b1 + (tt - t1) / (t2 - t1) * b2)
        ks.append(i + t[:, 0])
    out.append(P[-1:]); ks.append(np.array([len(P) - 1.0]))
    C = np.vstack(out); K = np.concatenate(ks)
    d = np.linalg.norm(np.diff(C, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(d)])
    # resample evenly by distance
    L = s[-1]
    if L < 1e-6: return None
    n = max(2, int(math.ceil(L / spacing)) + 1)
    sn = np.linspace(0, L, n)
    C2 = np.stack([np.interp(sn, s, C[:, 0]), np.interp(sn, s, C[:, 1])], 1)
    K2 = np.interp(sn, s, K)
    T = np.gradient(C2, axis=0); T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-9)
    if closed: K2 = np.mod(K2, len(P) - 1)
    return C2, T, sn, K2


def interp_point_values(K, vals, closed=False):
    vals = np.asarray(vals, np.float64)
    if closed: vals = np.concatenate([vals, vals[:1]])
    return np.interp(K, np.arange(len(vals)), vals)


def path_field(C, s, grid, reach, chunk=24):
    """Nearest point on a sampled path for every pixel within `reach` metres.
    grid: (x0, z0, px, nx, nz). Returns dict of full-size arrays d (distance, inf = far), sp (distance
    along), lat (signed sideways offset: + = right of travel when looking down the image)."""
    x0, z0, px, nx, nz = grid
    d = np.full((nz, nx), np.inf, np.float32); sp = np.zeros((nz, nx), np.float32); lat = np.zeros((nz, nx), np.float32)
    n = len(C)
    for a in range(0, n - 1, chunk):
        b = min(n - 1, a + chunk)
        A = C[a:b]; B = C[a + 1:b + 1]; SA = s[a:b]
        lo = np.minimum(A.min(0), B.min(0)) - reach; hi = np.maximum(A.max(0), B.max(0)) + reach
        c0 = max(0, int((lo[0] - x0) / px)); c1 = min(nx, int((hi[0] - x0) / px) + 2)
        r0 = max(0, int((lo[1] - z0) / px)); r1 = min(nz, int((hi[1] - z0) / px) + 2)
        if c0 >= c1 or r0 >= r1: continue
        X = (x0 + np.arange(c0, c1) * px)[None, :]; Z = (z0 + np.arange(r0, r1) * px)[:, None]
        bd = d[r0:r1, c0:c1]; bs = sp[r0:r1, c0:c1]; bl = lat[r0:r1, c0:c1]
        for k in range(len(A)):
            ax, az = A[k]; dx, dz = B[k] - A[k]
            L2 = dx * dx + dz * dz
            if L2 < 1e-12: continue
            px_ = X - ax; pz_ = Z - az
            t = np.clip((px_ * dx + pz_ * dz) / L2, 0, 1)
            qx = px_ - t * dx; qz = pz_ - t * dz
            dist = np.sqrt(qx * qx + qz * qz)
            m = dist < bd
            if not m.any(): continue
            L = math.sqrt(L2)
            cr = (dx * pz_ - dz * px_) / L                         # signed sideways distance
            bd[m] = dist[m]; bs[m] = (SA[k] + t * L)[m]
            bl[m] = np.broadcast_to(cr, dist.shape)[m]
    return {'d': d, 's': sp, 'lat': lat}


# =================================================================== jump templates
BUILTIN_TEMPLATES = {
    'Tabletop':   [(0, 0), (0.08, 0.15), (0.28, 1), (0.34, 1), (0.66, 1), (0.72, 0.95), (1, 0)],
    'Double':     [(0, 0), (0.1, 0.25), (0.27, 1), (0.3, 0.8), (0.36, 0), (0.56, 0), (0.62, 1), (0.66, 0.97), (1, 0)],
    'Triple':     [(0, 0), (0.08, 0.3), (0.2, 1), (0.23, 0.7), (0.28, 0), (0.6, 0), (0.64, 1), (0.68, 0.96), (1, 0)],
    'Step-up':    [(0, 0), (0.1, 0.2), (0.3, 0.75), (0.33, 0.6), (0.38, 0), (0.48, 0), (0.55, 1), (0.85, 1), (1, 0)],
    'Step-down':  [(0, 0), (0.2, 1), (0.45, 1), (0.48, 0.85), (0.6, 0.45), (1, 0)],
    'Kicker':     [(0, 0), (0.5, 0.35), (0.85, 1), (0.88, 0.9), (1, 0)],
    'Roller':     [(0, 0), (0.25, 0.6), (0.5, 1), (0.75, 0.6), (1, 0)],
    'Whoops':     'whoops',
    'Rhythm (3)': 'rhythm',
    'Berm wall':  [(0, 0), (0.3, 0.4), (0.7, 0.9), (1, 1)],
    'Drop-off':   [(0, 1), (0.5, 1), (0.55, 0.6), (1, 0)],
    'Gap (pit)':  [(0, 0), (0.15, -0.3), (0.5, -1), (0.85, -0.3), (1, 0)],
}


def template_profile(tpl, u):
    """Height 0..1 of a jump template at u in 0..1 (array)."""
    u = np.clip(u, 0, 1)
    if tpl == 'whoops' or tpl == 'rhythm':
        k = 6 if tpl == 'whoops' else 3
        return np.sin(np.pi * u * k) ** 2
    P = np.asarray(tpl, np.float64)
    o = np.argsort(P[:, 0]); P = P[o]
    # monotone cubic (Fritsch-Carlson) through the points: smooth, no overshoot
    x, y = P[:, 0], P[:, 1]
    if len(x) < 2: return np.zeros_like(u)
    h = np.diff(x); h[h == 0] = 1e-9
    dl = np.diff(y) / h
    m = np.zeros_like(y)
    with np.errstate(divide='ignore', invalid='ignore'):
        m[1:-1] = np.where(dl[:-1] * dl[1:] > 0, 2 / (1 / np.where(dl[:-1] == 0, 1e-9, dl[:-1]) + 1 / np.where(dl[1:] == 0, 1e-9, dl[1:])), 0)
    m[0] = dl[0]; m[-1] = dl[-1]
    i = np.clip(np.searchsorted(x, u) - 1, 0, len(x) - 2)
    t = (u - x[i]) / h[i]
    t2, t3 = t * t, t * t * t
    return ((2 * t3 - 3 * t2 + 1) * y[i] + (t3 - 2 * t2 + t) * h[i] * m[i] + (-2 * t3 + 3 * t2) * y[i + 1]
            + (t3 - t2) * h[i] * m[i + 1])


# =================================================================== project
TERRAIN_PRESETS = {
    'Flat pad':      dict(amplitude=0.4, size=60, octaves=3, roughness=0.45, ridged=False),
    'Gentle slope':  dict(amplitude=2.0, size=150, octaves=3, roughness=0.4, ridged=False, tilt=4.0),
    'Rolling hills': dict(amplitude=8.0, size=140, octaves=4, roughness=0.5, ridged=False),
    'Rough':         dict(amplitude=12.0, size=90, octaves=6, roughness=0.55, ridged=False),
    'Ridges':        dict(amplitude=14.0, size=110, octaves=5, roughness=0.5, ridged=True),
}

TRACK_DEFAULTS = dict(width=8.0, bank=0.0)
TRACK_SETTINGS = dict(cut=0.3, smooth=12.0, falloff=6.0, concavity=0.0, follow=1.0, surface='gravel',
                      funnel_len=0.0, funnel_width=20.0, closed=False)
JUMP_DEFAULTS = dict(template='Tabletop', length=18.0, height=2.0, width=0.0, offset=0.0, edge=1.5, angle=0.0, heading=0.0)
RUT_DEFAULTS = dict(count=1, spacing=0.9, depth=0.25, width=0.45, berm=0.6, berm_side='auto', sharpness=1.5,
                    berm_width=1.6)
BUMP_DEFAULTS = dict(kind='stamp', radius=1.2, height=0.3, count=40, size_min=0.6, size_max=1.4, seed=1,
                     spacing=1.2, length=20.0, track=-1, s=0.0, area=8.0)


PAINT_ARRAYS = ('tsculpt', 'fsculpt', 'mask', 'surf', 'canopy', 'rocks', 'impkeep', 'smix')   # per-pixel layers of a project
JUMP_SPEC_KEYS = ('length', 'height', 'width', 'edge', 'angle', 'offset')       # kept with a saved template


def template_points(t):
    """A template as stored (points, a 'whoops' / 'rhythm' name, or {'points': ..., 'specs': ...}) -> profile."""
    if isinstance(t, dict): return t.get('points') or [(0, 0), (1, 0)]
    return t


def profile_points(prof, n=25):
    """Editable points for any profile (the sine ones are sampled)."""
    prof = template_points(prof)
    if isinstance(prof, str):
        k = 6 if prof == 'whoops' else 3
        u = np.linspace(0, 1, 8 * k + 1)
        return [[float(a), float(b)] for a, b in zip(u, template_profile(prof, u))]
    return [[float(a), float(b)] for a, b in sorted(prof, key=lambda q: q[0])]


class Project:
    def __init__(self, n=1025, px=0.25):
        self.n = int(n); self.px = float(px)
        self.terrain = dict(kind='generated', preset='Rolling hills', seed=1, **TERRAIN_PRESETS['Rolling hills'])
        self.terrain.setdefault('tilt', 0.0)
        self.terrain['tilt_dir'] = 0.0
        self.base_img = None                # imported / grabbed terrain (metres), n x n
        self.import_src = None              # an imported heightmap (metres) kept to re-seat it when moved
        self.tracks = []                    # dicts: name, points [[x, z, width, bank]], settings, visible
        self.jumps = []                     # dicts: track, s, + JUMP_DEFAULTS, name, visible
        self.ruts = []                      # dicts: points [[x, z]], + RUT_DEFAULTS, name, visible
        self.bumps = []                     # dicts: x, z, + BUMP_DEFAULTS, name, visible
        self.groups = {}                    # layer id -> group name (display only)
        self.templates = {}                 # user jump templates: name -> points
        self.tsculpt = np.zeros((self.n, self.n), np.float32)    # terrain sculpting (under the tracks)
        self.fsculpt = np.zeros((self.n, self.n), np.float32)    # final sculpting (on top of everything)
        self.mask = np.zeros((self.n, self.n), np.float32)       # 1 = track features hidden here
        self.surf = np.full((self.n, self.n), -1, np.int8)       # painted surface (-1 = automatic)
        self.canopy = np.full((self.n, self.n), -1, np.int16)    # painted tree height 0..255 (-1 = automatic)
        self.rocks = np.full((self.n, self.n), -1, np.int16)     # painted boulders 0..255 (-1 = automatic)
        self.impkeep = np.ones((self.n, self.n), np.float32)     # 1 = imported track kept, 0 = erased there
        self.smix = np.full((self.n, self.n), -1, np.int16)      # a 2nd surface mixed in: layer*256 + amount (-1 = none)
        self.reference = dict(path='', opacity=0.5, visible=False)
        self.placement = None               # where it sits in the game's map (from the map editor), or None
        self.world = None                   # (lo, hi) the game map's height scale, when the terrain came from it
        self.export = dict(mode='auto', lo=0.0, hi=1.0)
        self._cache = {}

    # ---------------------------------------------------------- geometry
    @property
    def size(self): return (self.n - 1) * self.px

    def grid(self, n=None):
        n = n or self.n
        px = self.size / (n - 1)
        return (0.0, 0.0, px, n, n)

    def coords(self, n=None):
        x0, z0, px, nx, nz = self.grid(n)
        return np.meshgrid(x0 + np.arange(nx) * px, z0 + np.arange(nz) * px)

    # ---------------------------------------------------------- terrain
    def make_base(self):
        t = self.terrain
        if t['kind'] in ('image', 'ground') and self.base_img is not None:
            return self.base_img.astype(np.float32)
        X, Z = self.coords()
        on_ground = self.base_img is not None and t.get('on_ground')
        h = fbm(X, Z, int(t.get('seed', 1)), float(t['size']), int(t['octaves']), float(t['roughness']),
                ridged=bool(t.get('ridged'))) * float(t['amplitude'])
        tilt = float(t.get('tilt', 0.0))
        if tilt:
            a = math.radians(float(t.get('tilt_dir', 0.0)))
            h = h + ((X - self.size / 2) * math.cos(a) + (Z - self.size / 2) * math.sin(a)) * math.tan(math.radians(tilt))
        if on_ground:                   # added to the game's ground, fading out towards the area's edges
            d = np.minimum(np.minimum(X, self.size - X), np.minimum(Z, self.size - Z))
            h = self.base_img + (h - float(np.mean(h))) * smoothstep(d / max(self.size * 0.15, 1.0))
        return h.astype(np.float32)

    def base(self):
        key = ('base', json.dumps(self.terrain, sort_keys=True), id(self.base_img))
        if self._cache.get('base_key') != key:
            self._cache['base_key'] = key; self._cache['base'] = self.make_base()
        return self._cache['base']

    # ---------------------------------------------------------- tracks
    def track_curve(self, tr, spacing=None):
        pts = [p[:2] for p in tr['points']]
        if len(pts) < 2: return None
        c = catmull(pts, tr.get('closed', False), spacing or max(0.25, self.px))
        if c is None: return None
        C, T, s, K = c
        w = interp_point_values(K, [p[2] for p in tr['points']], tr.get('closed', False))
        bank = interp_point_values(K, [p[3] for p in tr['points']], tr.get('closed', False))
        fl = float(tr.get('funnel_len', 0) or 0)
        if fl > 0:
            fw = float(tr.get('funnel_width', 20))
            k = smoothstep(s / fl)
            w = fw * (1 - k) + w * k
        return C, T, s, w, bank

    def track_field(self, i, tr):
        geo = json.dumps([tr['points'], tr.get('closed'), tr.get('falloff'), tr.get('funnel_len'),
                          tr.get('funnel_width'), self.n, self.px])
        ck = ('tf', i)
        if self._cache.get(ck, (None,))[0] != geo:
            cur = self.track_curve(tr)
            if cur is None: self._cache[ck] = (geo, None); return None
            C, T, s, w, bank = cur
            reach = float(w.max()) / 2 + float(tr.get('falloff', 6)) + 1
            f = path_field(C, s, self.grid(), reach)
            self._cache[ck] = (geo, (f, cur))
        return self._cache[ck][1]

    def apply_track(self, H, tr, i, info):
        tf = self.track_field(i, tr)
        if tf is None: return H
        f, (C, T, s, w, bank) = tf
        near = np.isfinite(f['d'])
        if not near.any(): return H
        # elevation along the track: the ground under it, smoothed along its length
        g = self.grid()
        ground = bilinear(H, C[:, 0] / g[2], C[:, 1] / g[2])
        sm = float(tr.get('smooth', 12.0))
        if sm > 0:
            r = sm / max(s[1] - s[0], 1e-3)
            if tr.get('closed') and len(ground) > 4:            # a loop: smooth across the start / finish
                el = blur1d(np.concatenate([ground, ground, ground]), r)[len(ground):2 * len(ground)]
            else:
                el = blur1d(ground, r)
        else:
            el = ground
        fol = float(tr.get('follow', 1.0))
        el = el * fol + float(np.mean(ground)) * (1 - fol)
        el = el - float(tr.get('cut', 0.3))
        sp = f['s'][near]; lat = f['lat'][near]; d = f['d'][near]
        e = np.interp(sp, s, el); hw = np.interp(sp, s, w) / 2; bk = np.interp(sp, s, bank)
        prof = e + lat * np.tan(np.radians(bk)) + float(tr.get('concavity', 0.0)) * (np.clip(lat / np.maximum(hw, 0.1), -1, 1) ** 2)
        fo = max(float(tr.get('falloff', 6.0)), 1e-3)
        wt = np.where(d <= hw, 1.0, smoothstep(1 - (d - hw) / fo))
        old = H[near]
        H = H.copy()
        H[near] = old * (1 - wt) + prof * wt
        # remember for jumps, surface and the views
        W = np.zeros_like(H); W[near] = wt
        S = np.full_like(H, -1); S[near] = sp; L = np.zeros_like(H); L[near] = lat
        inside = W > info['tw']
        info['tw'] = np.maximum(info['tw'], W)
        info['ts'][inside] = S[inside]; info['tl'][inside] = L[inside]; info['ti'][inside] = i
        info['curves'][i] = (C, T, s, w, bank)
        return H

    # ---------------------------------------------------------- jumps
    def template(self, name):
        if name in self.templates: return template_points(self.templates[name])
        return BUILTIN_TEMPLATES.get(name, BUILTIN_TEMPLATES['Tabletop'])

    def template_specs(self, name):
        """The sizes saved with one of your templates (length, height, width...), or None."""
        t = self.templates.get(name)
        return dict(t['specs']) if isinstance(t, dict) and t.get('specs') else None

    def jump_shape(self, jp):
        """A jump's profile: its own edited shape if it has one, else its template's."""
        return jp.get('profile') or self.template(jp['template'])

    def free_jump_delta(self, jp):
        """A jump placed anywhere (not on one of your tracks): centre x, z, direction of travel 'heading'
        (degrees, 0 = +x / right, 90 = +z / down the image), length, height, width."""
        L = max(float(jp['length']), 0.5); W = float(jp.get('width') or 0) or 8.0; e = max(float(jp.get('edge', 1.5)), 1e-3)
        cx, cz = float(jp['x']), float(jp['z']); a = math.radians(float(jp.get('heading', 0)))
        R = math.hypot(L / 2, W / 2 + e) + 1
        c0 = max(0, int((cx - R) / self.px)); c1 = min(self.n, int((cx + R) / self.px) + 2)
        r0 = max(0, int((cz - R) / self.px)); r1 = min(self.n, int((cz + R) / self.px) + 2)
        if c0 >= c1 or r0 >= r1: return None
        X = (np.arange(c0, c1) * self.px - cx)[None, :]; Z = (np.arange(r0, r1) * self.px - cz)[:, None]
        along = X * math.cos(a) + Z * math.sin(a); side = -X * math.sin(a) + Z * math.cos(a) - float(jp.get('offset', 0))
        u = (along + L / 2 + side * math.tan(math.radians(float(jp.get('angle', 0))))) / L
        inside = (u > -0.01) & (u < 1.01) & (np.abs(side) < W / 2 + e)
        prof = template_profile(self.jump_shape(jp), u) * float(jp['height'])
        fade = np.where(np.abs(side) <= W / 2, 1.0, smoothstep(1 - (np.abs(side) - W / 2) / e))
        D = np.where(inside, prof * fade, 0).astype(np.float32)
        return (slice(r0, r1), slice(c0, c1)), D

    def jump_delta(self, jp, info):
        if int(jp.get('track', 0)) < 0: return self.free_jump_delta(jp)
        i = int(jp.get('track', 0))
        if i not in info['curves']: return None
        C, T, s, w, bank = info['curves'][i]
        s0 = float(jp['s']); Lj = max(float(jp['length']), 0.5)
        width = float(jp.get('width') or 0) or float(np.interp(s0, s, w))
        # work in a window around the jump only
        k0, k1 = np.searchsorted(s, [s0 - Lj / 2 - 2, s0 + Lj / 2 + 2])
        if k1 <= k0: return None
        pad = width / 2 + float(jp.get('edge', 1.5)) + abs(float(jp.get('offset', 0))) + 3 + Lj * abs(math.tan(math.radians(float(jp.get('angle', 0)))))
        P = C[k0:k1 + 1]
        c0 = max(0, int((P[:, 0].min() - pad) / self.px)); c1 = min(self.n, int((P[:, 0].max() + pad) / self.px) + 2)
        r0 = max(0, int((P[:, 1].min() - pad) / self.px)); r1 = min(self.n, int((P[:, 1].max() + pad) / self.px) + 2)
        if c0 >= c1 or r0 >= r1: return None
        win = (slice(r0, r1), slice(c0, c1))
        sel = info['ti'][win] == i
        if not sel.any(): return None
        S = info['ts'][win]; L = info['tl'][win]
        lat = L - float(jp.get('offset', 0))
        u = (S - (s0 - Lj / 2) + lat * math.tan(math.radians(float(jp.get('angle', 0))))) / Lj
        m = sel & (u > -0.01) & (u < 1.01) & (np.abs(lat) < width / 2 + float(jp.get('edge', 1.5)))
        if not m.any(): return None
        prof = template_profile(self.jump_shape(jp), u[m]) * float(jp['height'])
        e = max(float(jp.get('edge', 1.5)), 1e-3)
        side = np.where(np.abs(lat[m]) <= width / 2, 1.0, smoothstep(1 - (np.abs(lat[m]) - width / 2) / e))
        D = np.zeros(S.shape, np.float32)
        D[m] = prof * side * info['tw'][win][m]
        return win, D

    # ---------------------------------------------------------- ruts
    def rut_delta(self, k, rt):
        pts = [p[:2] for p in rt['points']]
        if len(pts) < 2: return None
        geo = json.dumps([rt['points'], rt['count'], rt['spacing'], rt['width'], rt['berm_width'], self.n, self.px])
        ck = ('rf', k)
        if self._cache.get(ck, (None,))[0] != geo:
            c = catmull(pts, False, max(0.15, self.px * 0.7))
            if c is None: return None
            C, T, s, K = c
            n = int(rt['count']); sp = float(rt['spacing'])
            reach = (n - 1) * sp / 2 + float(rt['width']) + float(rt['berm_width']) + 1
            self._cache[ck] = (geo, (path_field(C, s, self.grid(), reach), C, T, s))
        cached = self._cache[ck][1]
        if cached is None: return None
        f, C, T, s = cached
        near = np.isfinite(f['d'])
        lat = f['lat'][near]
        n = int(rt['count']); sp = float(rt['spacing']); hw = float(rt['width']) / 2
        sh = max(float(rt['sharpness']), 0.3)
        D = np.zeros(lat.shape, np.float32)
        offs = (np.arange(n) - (n - 1) / 2) * sp
        for o in offs:
            x = np.abs(lat - o) / max(hw, 0.05)
            D -= float(rt['depth']) * np.clip(1 - x * x, 0, 1) ** sh
        # end taper (ruts fade in / out over 2 m)
        sv = f['s'][near]; Ltot = s[-1]
        D *= smoothstep(np.minimum(sv, Ltot - sv) / 2.0)
        bh = float(rt['berm'])
        if bh:
            side = rt.get('berm_side', 'auto')
            if side == 'auto':                      # outside of the corner: opposite to the way it turns
                cross = np.sum(T[:-1, 0] * T[1:, 1] - T[:-1, 1] * T[1:, 0])
                side = 'left' if cross > 0 else 'right'
            edge = (n - 1) * sp / 2 + hw
            bw = max(float(rt['berm_width']), 0.1)
            sides = {'left': [-1], 'right': [1], 'both': [-1, 1]}.get(side, [1])
            for sg in sides:
                y = (lat * sg - edge) / bw                          # 0 at the rut edge -> 1 at berm outer edge
                D += bh * np.where((y > -0.3) & (y < 1), np.abs(np.sin(np.pi * np.clip((y + 0.3) / 1.3, 0, 1))) ** 1.5, 0) \
                    * smoothstep(np.minimum(sv, Ltot - sv) / 3.0)
        out = np.zeros((self.n, self.n), np.float32); out[near] = D
        return out

    # ---------------------------------------------------------- bumps
    def bump_delta(self, b, info):
        rng = np.random.default_rng(int(b.get('seed', 1)))
        X, Z = None, None
        pts = []
        if b['kind'] == 'stamp':
            pts = [(float(b['x']), float(b['z']), float(b['radius']), float(b['height']))]
        elif b['kind'] == 'scatter':
            for _ in range(int(b['count'])):
                r = float(b['area']) * math.sqrt(rng.random()); a = rng.random() * 2 * math.pi
                sz = rng.uniform(float(b['size_min']), float(b['size_max']))
                pts.append((float(b['x']) + r * math.cos(a), float(b['z']) + r * math.sin(a), sz,
                            float(b['height']) * rng.uniform(0.6, 1.0)))
        elif b['kind'] == 'braking':                # rows across the track, getting bigger towards the corner
            i = int(b.get('track', 0))
            if i not in info['curves']: return None
            C, T, s, w, bank = info['curves'][i]
            s0 = float(b['s']); L = float(b['length']); spc = max(float(b['spacing']), 0.3)
            for k, sv in enumerate(np.arange(s0, s0 + L, spc)):
                if sv < 0 or sv > s[-1]: continue
                cx = np.interp(sv, s, C[:, 0]); cz = np.interp(sv, s, C[:, 1])
                tx = np.interp(sv, s, T[:, 0]); tz = np.interp(sv, s, T[:, 1])
                hw = np.interp(sv, s, w) / 2
                grow = 0.5 + 0.5 * (sv - s0) / max(L, 1e-3)
                for o in np.arange(-hw * 0.85, hw * 0.85 + 1e-6, max(float(b['size_min']), 0.4)):
                    j = rng.uniform(-0.15, 0.15) * spc
                    pts.append((cx + tz * -o + tx * j, cz + tx * o + tz * j, spc * 0.55,
                                float(b['height']) * grow * rng.uniform(0.8, 1.0)))
        if not pts: return None
        D = np.zeros((self.n, self.n), np.float32)
        px = self.px
        for (x, z, r, h) in pts:
            c0 = max(0, int((x - r) / px)); c1 = min(self.n, int((x + r) / px) + 2)
            r0 = max(0, int((z - r) / px)); r1 = min(self.n, int((z + r) / px) + 2)
            if c0 >= c1 or r0 >= r1: continue
            xx = (np.arange(c0, c1) * px - x)[None, :]; zz = (np.arange(r0, r1) * px - z)[:, None]
            q = np.sqrt(xx * xx + zz * zz) / max(r, 0.05)
            D[r0:r1, c0:c1] += h * np.where(q < 1, 0.5 + 0.5 * np.cos(np.pi * q), 0)
        return D

    # ---------------------------------------------------------- evaluate
    def evaluate(self, upto=None):
        """The finished heightmap (metres) and info: track weight 'tw', surface 'surface' (index, -1 = keep),
        per-pixel track id / distance along / sideways, and the base (terrain + terrain sculpt) 'terrain'."""
        B = self.base() + self.tsculpt
        info = {'tw': np.zeros_like(B), 'ts': np.zeros_like(B), 'tl': np.zeros_like(B),
                'ti': np.full(B.shape, -1, np.int32), 'curves': {}, 'terrain': B}
        H = B
        for i, tr in enumerate(self.tracks):
            if tr.get('visible', True): H = self.apply_track(H, tr, i, info)
        feat = np.zeros_like(B)
        for jp in self.jumps:
            if jp.get('visible', True):
                r = self.jump_delta(jp, info)
                if r is not None: feat[r[0]] += r[1]
        for k, rt in enumerate(self.ruts):
            if rt.get('visible', True):
                D = self.rut_delta(k, rt)
                if D is not None: feat += D
        for b in self.bumps:
            if b.get('visible', True):
                D = self.bump_delta(b, info)
                if D is not None: feat += D
        layered = H + feat
        keep = 1 - np.clip(self.mask, 0, 1)
        out = B + (layered - B) * keep + self.fsculpt
        # surface: tracks get their surface where they are more than half there, then painting
        surf = np.full(B.shape, -1, np.int8)
        for i, tr in enumerate(self.tracks):
            if tr.get('visible', True) and tr.get('surface'):
                m = (info['ti'] == i) & (info['tw'] * keep > 0.5)
                surf[m] = SURFACES.index(tr['surface'])
        painted = self.surf >= 0
        surf[painted] = self.surf[painted]
        surf[self.surf == -2] = -1                          # painted "keep the game's ground"
        info['surface'] = surf
        return out.astype(np.float32), info

    # ---------------------------------------------------------- vector state for undo / save
    def state(self):
        return copy.deepcopy(dict(terrain=self.terrain, tracks=self.tracks, jumps=self.jumps, ruts=self.ruts,
                                  bumps=self.bumps, groups=self.groups, templates=self.templates,
                                  reference=self.reference, placement=self.placement, world=self.world,
                                  export=self.export, n=self.n, px=self.px))

    def set_state(self, st):
        st = copy.deepcopy(st)
        for k in ('terrain', 'tracks', 'jumps', 'ruts', 'bumps', 'groups', 'templates', 'reference',
                  'placement', 'world', 'export'):
            if k in st: setattr(self, k, st[k])

    # ---------------------------------------------------------- save / open (.hmproj = zip)
    def save(self, path):
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('project.json', json.dumps(self.state(), indent=1))
            for name in PAINT_ARRAYS + (('base_img',) if self.base_img is not None else ()) \
                    + (('import_src',) if getattr(self, 'import_src', None) is not None else ()):
                bio = io.BytesIO(); np.save(bio, getattr(self, name)); z.writestr(name + '.npy', bio.getvalue())

    @classmethod
    def open(cls, path):
        with zipfile.ZipFile(path) as z:
            st = json.loads(z.read('project.json'))
            p = cls(st.get('n', 1025), st.get('px', 0.25))
            p.set_state(st)
            for name in PAINT_ARRAYS + ('base_img', 'import_src'):
                if name + '.npy' in z.namelist():
                    setattr(p, name, np.load(io.BytesIO(z.read(name + '.npy'))))
            for name, dt, v in (('canopy', np.int16, -1), ('rocks', np.int16, -1), ('impkeep', np.float32, 1), ('smix', np.int16, -1)):
                a = getattr(p, name)
                if a.shape != (p.n, p.n): setattr(p, name, np.full((p.n, p.n), v, dt))
        return p

    def resized(self, n, px):
        """A copy at another resolution / size (arrays resampled)."""
        q = Project(n, px); q.set_state(self.state()); q.n, q.px = int(n), float(px)
        X, Z = np.meshgrid(np.arange(n) * px / self.px, np.arange(n) * px / self.px)
        for name in ('tsculpt', 'fsculpt', 'mask', 'impkeep'):
            setattr(q, name, bilinear(getattr(self, name), X, Z).astype(np.float32))
        iz, ix = np.clip(np.round(Z).astype(int), 0, self.n - 1), np.clip(np.round(X).astype(int), 0, self.n - 1)
        for name in ('surf', 'canopy', 'rocks', 'smix'): setattr(q, name, getattr(self, name)[iz, ix])
        if self.base_img is not None: q.base_img = bilinear(self.base_img, X, Z).astype(np.float32)
        return q


# =================================================================== sculpting
SCULPT_TOOLS = ['Raise', 'Lower', 'Smooth', 'Flatten', 'Flatten slope', 'Ramp', 'Noise', 'Erode', 'Clone',
                'Terrace', 'Erase']


def brush_window(n, px, x, z, radius):
    c0 = max(0, int((x - radius) / px)); c1 = min(n, int((x + radius) / px) + 2)
    r0 = max(0, int((z - radius) / px)); r1 = min(n, int((z + radius) / px) + 2)
    if c0 >= c1 or r0 >= r1: return None
    xx = (np.arange(c0, c1) * px - x)[None, :]; zz = (np.arange(r0, r1) * px - z)[:, None]
    q = np.sqrt(xx * xx + zz * zz) / max(radius, 1e-3)
    return (slice(r0, r1), slice(c0, c1)), q


def falloff(q, hardness):
    """1 in the middle -> 0 at the brush edge; hardness 0 (soft) .. 1 (hard)."""
    inner = np.clip(hardness, 0, 0.98)
    return np.where(q < 1, smoothstep((1 - q) / (1 - inner)), 0).astype(np.float32)


def sculpt_dab(tool, delta, H, px, x, z, radius, strength, hardness, ctx):
    """One brush dab: changes `delta` (the sculpt layer) using the current finished heights H.
    ctx carries stroke state (start height, ramp ends, clone offset, seed...). Returns the changed window."""
    n = H.shape[0]
    bw = brush_window(n, px, x, z, radius)
    if bw is None: return None
    win, q = bw
    f = falloff(q, hardness) * float(strength)
    h = H[win]
    if tool == 'Raise': d = f * 0.25
    elif tool == 'Lower': d = -f * 0.25
    elif tool == 'Smooth':
        pad = int(radius / px) + 4
        r0, c0 = max(0, win[0].start - pad), max(0, win[1].start - pad)
        big = H[r0:win[0].stop + pad, c0:win[1].stop + pad]
        bl = blur(big, max(1.5, radius / px * 0.25))
        t = bl[win[0].start - r0:win[0].stop - r0, win[1].start - c0:win[1].stop - c0]
        d = (t - h) * np.clip(f, 0, 1)
    elif tool == 'Flatten':
        d = (ctx['h0'] - h) * np.clip(f * 0.5, 0, 1)
    elif tool == 'Flatten slope':
        # least-squares plane through the brush area, bumps pulled onto it
        zz, xx = np.nonzero(q < 1)
        if len(zz) < 3: return None
        A = np.stack([xx, zz, np.ones_like(xx)], 1).astype(np.float64)
        coef, *_ = np.linalg.lstsq(A, h[zz, xx].astype(np.float64), rcond=None)
        Zi, Xi = np.mgrid[0:h.shape[0], 0:h.shape[1]]
        plane = coef[0] * Xi + coef[1] * Zi + coef[2]
        d = (plane - h) * np.clip(f * 0.5, 0, 1)
    elif tool == 'Noise':
        Zi, Xi = np.mgrid[win[0], win[1]]
        d = fbm(Xi * px, Zi * px, ctx.get('seed', 7), max(radius * 0.4, 1.0), 3) * f * 0.15
    elif tool == 'Terrace':
        step = max(float(ctx.get('step', 1.0)), 0.05)
        t = np.round(h / step) * step
        d = (t - h) * np.clip(f * 0.5, 0, 1)
    elif tool == 'Erase':
        d = -delta[win] * np.clip(f * 0.5, 0, 1)
    elif tool == 'Clone':
        if ctx.get('src') is None: return None
        ox, oz = ctx['src'][0] - ctx['start'][0], ctx['src'][1] - ctx['start'][1]
        Zi, Xi = np.mgrid[win[0], win[1]]
        src = bilinear(H, Xi + ox / px, Zi + oz / px)
        d = (src - h) * np.clip(f * 0.5, 0, 1)
    elif tool == 'Erode':
        area = (win[0].stop - win[0].start) * (win[1].stop - win[1].start)
        e = erode(H, win, n_drops=int(np.clip(area * 0.25 * strength, 50, 6000)),
                  seed=ctx.setdefault('seed2', 1) + ctx.setdefault('k', 0))
        ctx['k'] += 1
        d = e[win] * np.clip(falloff(q, hardness) * 1.5, 0, 1)
    else:
        return None
    delta[win] += d.astype(np.float32)
    return win, d


def ramp(delta, H, px, a, b, width, hardness=0.5):
    """Straight ramp from point a to b (heights taken there), width metres, blended at its sides."""
    n = H.shape[0]
    (ax, az), (bx, bz) = a, b
    L = math.hypot(bx - ax, bz - az)
    if L < 1e-3: return None
    ha = float(bilinear(H, np.array([ax / px]), np.array([az / px]))[0])
    hb = float(bilinear(H, np.array([bx / px]), np.array([bz / px]))[0])
    R = width / 2 + 1
    c0 = max(0, int((min(ax, bx) - R) / px)); c1 = min(n, int((max(ax, bx) + R) / px) + 2)
    r0 = max(0, int((min(az, bz) - R) / px)); r1 = min(n, int((max(az, bz) + R) / px) + 2)
    X = (np.arange(c0, c1) * px)[None, :]; Z = (np.arange(r0, r1) * px)[:, None]
    dx, dz = (bx - ax) / L, (bz - az) / L
    t = ((X - ax) * dx + (Z - az) * dz) / L
    side = np.abs((X - ax) * -dz + (Z - az) * dx)
    m = (t >= 0) & (t <= 1)
    w = np.where(m, falloff(side / (width / 2), hardness), 0)
    target = ha + (hb - ha) * np.clip(t, 0, 1)
    win = (slice(r0, r1), slice(c0, c1))
    d = (target - H[win]) * w
    delta[win] += d.astype(np.float32)
    return win, d


def erode(H, win, n_drops=400, seed=1, steps=40, inertia=0.05, capacity=4.0, erode_rate=0.3,
          deposit_rate=0.3, evaporate=0.02, gravity=4.0, radius_pad=6):
    """Hydraulic erosion by simulated raindrops (many at once) inside window `win` of H.
    Returns the height change as a full-size array (only around the window is non-zero)."""
    n = H.shape[0]
    r0 = max(0, win[0].start - radius_pad); r1 = min(n, win[0].stop + radius_pad)
    c0 = max(0, win[1].start - radius_pad); c1 = min(n, win[1].stop + radius_pad)
    A = H[r0:r1, c0:c1].astype(np.float64).copy()
    A0 = A.copy()
    hh, ww = A.shape
    if hh < 4 or ww < 4: return np.zeros_like(H)
    rng = np.random.default_rng(seed)
    px_ = rng.uniform(win[1].start - c0, win[1].stop - c0 - 1, n_drops)
    pz_ = rng.uniform(win[0].start - r0, win[0].stop - r0 - 1, n_drops)
    dx = np.zeros(n_drops); dz = np.zeros(n_drops)
    vel = np.ones(n_drops); water = np.ones(n_drops); sed = np.zeros(n_drops)
    alive = np.ones(n_drops, bool)

    def grad_h(x, z):
        ix = np.clip(x.astype(int), 0, ww - 2); iz = np.clip(z.astype(int), 0, hh - 2)
        tx = x - ix; tz = z - iz
        a = A[iz, ix]; b = A[iz, ix + 1]; c = A[iz + 1, ix]; d = A[iz + 1, ix + 1]
        gx = (b - a) * (1 - tz) + (d - c) * tz; gz = (c - a) * (1 - tx) + (d - b) * tx
        h = (a * (1 - tx) + b * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz
        return gx, gz, h, ix, iz, tx, tz

    def spread(ix, iz, tx, tz, amt):
        np.add.at(A, (iz, ix), amt * (1 - tx) * (1 - tz)); np.add.at(A, (iz, ix + 1), amt * tx * (1 - tz))
        np.add.at(A, (iz + 1, ix), amt * (1 - tx) * tz); np.add.at(A, (iz + 1, ix + 1), amt * tx * tz)

    for _ in range(steps):
        if not alive.any(): break
        k = np.nonzero(alive)[0]
        gx, gz, h0, ix, iz, tx, tz = grad_h(px_[k], pz_[k])
        dx[k] = dx[k] * inertia - gx * (1 - inertia); dz[k] = dz[k] * inertia - gz * (1 - inertia)
        L = np.sqrt(dx[k] ** 2 + dz[k] ** 2)
        dead = L < 1e-9
        L[dead] = 1
        dx[k] /= L; dz[k] /= L
        nx_ = px_[k] + dx[k]; nz_ = pz_[k] + dz[k]
        out = dead | (nx_ < 0) | (nx_ >= ww - 1) | (nz_ < 0) | (nz_ >= hh - 1)
        _, _, h1, *_ = grad_h(np.clip(nx_, 0, ww - 1.001), np.clip(nz_, 0, hh - 1.001))
        dh = h1 - h0
        cap = np.maximum(-dh, 0.01) * vel[k] * water[k] * capacity
        dep = (sed[k] > cap) | (dh > 0)
        amt_dep = np.where(dh > 0, np.minimum(dh, sed[k]), (sed[k] - cap) * deposit_rate)
        amt_dep = np.where(dep, amt_dep, 0)
        amt_ero = np.where(~dep, np.minimum((cap - sed[k]) * erode_rate, -dh), 0)
        amt_ero = np.maximum(amt_ero, 0)
        spread(ix, iz, tx, tz, amt_dep - amt_ero)
        sed[k] += amt_ero - amt_dep
        vel[k] = np.sqrt(np.maximum(vel[k] ** 2 + (-dh) * gravity, 0))
        water[k] *= (1 - evaporate)
        px_[k] = nx_; pz_[k] = nz_
        alive[k[out]] = False
    D = np.zeros(H.shape, np.float32)
    D[r0:r1, c0:c1] = (A - A0).astype(np.float32)
    return D


# =================================================================== import / export
def load_image_heights(path):
    """A greyscale image as 0..1 (16-bit kept)."""
    from PIL import Image
    im = Image.open(path)
    if im.mode in ('I;16', 'I;16L', 'I;16B', 'I;16N'):
        return np.asarray(im, np.uint16).astype(np.float32) / 65535.0
    if im.mode == 'I':
        a = np.asarray(im, np.float32)
        return a / 65535.0 if a.max() <= 65535 and a.min() >= 0 else (a - a.min()) / max(float(np.ptp(a)), 1e-9)
    if im.mode == 'F':
        a = np.asarray(im, np.float32); return (a - a.min()) / max(float(np.ptp(a)), 1e-9)
    return np.asarray(im.convert('L'), np.float32) / 255.0


def resample(a, n):
    if a.shape == (n, n): return a.astype(np.float32)
    X, Z = np.meshgrid(np.linspace(0, a.shape[1] - 1, n), np.linspace(0, a.shape[0] - 1, n))
    return bilinear(a.astype(np.float32), X, Z).astype(np.float32)


def save_png16(path, a01):
    from PIL import Image
    q = np.ascontiguousarray(np.round(np.clip(np.nan_to_num(a01), 0, 1) * 65535).astype('<u2'))
    Image.frombytes('I;16', (q.shape[1], q.shape[0]), q.tobytes()).save(path)


def export_range(p, H):
    e = p.export or {}
    if e.get('mode') == 'world' and p.world: return float(p.world[0]), float(p.world[1])
    if e.get('mode') == 'fixed': return float(e['lo']), float(e['hi'])
    lo, hi = float(H.min()), float(H.max())
    if hi - lo < 1e-3: hi = lo + 1
    return lo, hi


def export_heightmap(p, path, H=None):
    if H is None: H, _ = p.evaluate()
    lo, hi = export_range(p, H)
    save_png16(path, (H - lo) / (hi - lo))
    return lo, hi


def export_surface(p, path, info=None, rgba=True):
    """Surface map: transparent where the game's ground stays as it is, the layer's colour elsewhere
    (a texture mask). The layer index is stored in the red channel's low bits too? - no: a separate
    .surface.png of indices + 1 is written for the map editor."""
    from PIL import Image
    if info is None: _, info = p.evaluate()
    s = info['surface']
    rgb = np.zeros(s.shape + (4,), np.uint8)
    for i, name in enumerate(SURFACES):
        m = s == i
        rgb[m, :3] = SURF_RGB[name]; rgb[m, 3] = 255
    Image.fromarray(rgb, 'RGBA').save(path)
    Image.fromarray((s.astype(np.int16) + 1).astype(np.uint8), 'L').save(os.path.splitext(path)[0] + '.index.png')


def shade(H, px, exag=1.0, surface=None, tw=None, slope_tint=True, palette=None, hrange=None):
    """RGB hillshade preview: ground colour from the surface, light from the north-west.
    hrange: (low, high) heights for the colour ramp - give the whole map's when shading a part of it."""
    gz, gx = np.gradient(H.astype(np.float32) * exag, px)
    nrm = 1 / np.sqrt(gx * gx + gz * gz + 1)
    l = np.clip((-gx * -0.55 + -gz * -0.55 + 0.63) * nrm, 0, 1)
    l = 0.35 + 0.75 * l
    base = np.empty(H.shape + (3,), np.float32)
    lo, hi = hrange if hrange is not None else (np.percentile(H, [1, 99]) if H.size else (0, 1))
    t = np.clip((H - lo) / max(hi - lo, 1e-3), 0, 1)[..., None]
    base[:] = np.array([96, 112, 58], np.float32) * (1 - t) + np.array([150, 140, 100], np.float32) * t
    if surface is not None:
        for i, name in enumerate(SURFACES):
            m = surface == i
            if m.any(): base[m] = SURF_RGB[name] if palette is None else palette[i]
    if slope_tint:
        sl = np.degrees(np.arctan(np.sqrt(gx * gx + gz * gz) / max(exag, 1e-6)))
        k = np.clip((sl - 30) / 25, 0, 1)[..., None] * 0.55
        base = base * (1 - k) + np.array([200, 70, 50], np.float32) * k
    return np.clip(base * l[..., None], 0, 255).astype(np.uint8)


# =================================================================== paint brushes (brush tips)
# Each tip is a pattern 0..1 inside the brush circle.  Painting a categorical layer (surface, trees, rocks)
# turns the pattern into pixels with a fixed per-pixel threshold: a pixel is painted where
# pattern * opacity > threshold.  Repeated strokes therefore never go past the opacity (sparse stays sparse),
# while tips with random parts (spatter, clumps...) still vary from dab to dab.
BRUSH_TIPS = ['Hard round', 'Soft round', 'Rough edge', 'Speckle', 'Stipple', 'Spatter', 'Clumps', 'Splotches',
              'Grunge', 'Chalk', 'Bristles', 'Dry brush', 'Cross-hatch', 'Leaves']
BRUSH_TIP_HELP = {
    'Hard round': 'solid circle', 'Soft round': 'thins out towards the edge', 'Rough edge': 'solid with a ragged edge',
    'Speckle': 'fine random dots', 'Stipple': 'evenly spread dots (Scale = dot size)',
    'Spatter': 'random blobs, bigger and smaller', 'Clumps': 'tufts of small dots (grass clumps)',
    'Splotches': 'cloudy patches (Scale = patch size)', 'Grunge': 'broken, gritty texture',
    'Chalk': 'streaky texture along the stroke', 'Bristles': 'parallel bristle lines along the stroke',
    'Dry brush': 'bristles that break up', 'Cross-hatch': 'criss-cross lines', 'Leaves': 'scattered leaf shapes'}
BRUSH_DEFAULTS = dict(tip='Hard round', radius=4.0, opacity=1.0, density=0.5, scale=1.0, hardness=0.5,
                      angle=0.0, follow=True, spacing=0.3)


def _rot(xx, zz, ang):
    c, s = math.cos(ang), math.sin(ang)
    return xx * c + zz * s, -xx * s + zz * c


def _dots(shape, px, x0, z0, centres, radii):
    """Discs at (x, z) local metres of the window (origin at its first pixel)."""
    out = np.zeros(shape, np.float32)
    H, W = shape
    for (cx, cz), r in zip(centres, radii):
        r = max(r, px * 0.5)
        c0 = max(0, int((cx - r) / px)); c1 = min(W, int((cx + r) / px) + 2)
        r0 = max(0, int((cz - r) / px)); r1 = min(H, int((cz + r) / px) + 2)
        if c0 >= c1 or r0 >= r1: continue
        X = (np.arange(c0, c1) * px - cx)[None, :]; Z = (np.arange(r0, r1) * px - cz)[:, None]
        out[r0:r1, c0:c1] = np.maximum(out[r0:r1, c0:c1], (X * X + Z * Z <= r * r).astype(np.float32))
    return out


def brush_tip(b, X, Z, xx, zz, R, px, rng, stroke_angle=0.0):
    """The pattern (0..1, window shaped) of one dab.  X, Z world metres of the window's pixels, xx, zz the same
    relative to the dab centre, R the radius, rng a numpy Generator for this dab."""
    tip = b.get('tip', 'Hard round')
    q = np.sqrt(xx * xx + zz * zz) / max(R, 1e-3)
    disc = (q < 1).astype(np.float32)
    dens = float(np.clip(b.get('density', 0.5), 0.0, 1.0))
    sc = max(float(b.get('scale', 1.0)), 0.05)
    ang = stroke_angle if b.get('follow', True) else math.radians(float(b.get('angle', 0.0)))
    seed = int(rng.integers(1 << 30))
    H, W = xx.shape
    ox = float(xx[0, 0]); oz = float(zz[0, 0])                 # window origin relative to the centre
    if tip == 'Hard round':
        return disc
    if tip == 'Soft round':
        return falloff(q, float(b.get('hardness', 0.5)))
    if tip == 'Rough edge':
        n = fbm(X, Z, 11, max(R * 0.35 * sc, px * 2), 3)
        return (q < 0.82 + 0.3 * n).astype(np.float32)
    if tip == 'Speckle':                                      # fixed dots in the world: strokes don't fill them in
        g = max(px, 0.12 * sc)
        return disc * (_hash(np.floor(X / g).astype(np.int64), np.floor(Z / g).astype(np.int64), 61)
                       < 0.04 + 0.6 * dens * dens).astype(np.float32)
    if tip == 'Stipple':
        r = 0.25 * sc
        n = int(np.clip(dens * math.pi * R * R / max((2.2 * r) ** 2, 1e-4), 1, 4000))
        a = rng.random(n) * 2 * math.pi; d = R * np.sqrt(rng.random(n))
        return disc * _dots(xx.shape, px, 0, 0, np.stack([d * np.cos(a) - ox, d * np.sin(a) - oz], 1), np.full(n, r))
    if tip == 'Spatter':
        n = int(4 + dens * 30)
        a = rng.random(n) * 2 * math.pi; d = R * np.abs(rng.normal(0, 0.5, n)).clip(0, 1)
        rad = R * sc * rng.uniform(0.03, 0.22, n) * (1 - 0.6 * d / R)
        return _dots(xx.shape, px, 0, 0, np.stack([d * np.cos(a) - ox, d * np.sin(a) - oz], 1), rad)
    if tip in ('Clumps', 'Leaves'):
        k = int(2 + dens * 10)
        a = rng.random(k) * 2 * math.pi; d = R * 0.85 * np.sqrt(rng.random(k))
        cs, rs = [], []
        for cx, cz in zip(d * np.cos(a), d * np.sin(a)):
            m = int(5 + dens * 12)
            aa = rng.random(m) * 2 * math.pi; dd = 0.8 * sc * np.sqrt(rng.random(m))
            for u, v in zip(cx + dd * np.cos(aa), cz + dd * np.sin(aa)): cs.append((u - ox, v - oz))
            rs += list(rng.uniform(0.1, 0.24, m) * sc)
        out = _dots(xx.shape, px, 0, 0, cs, rs) if cs else np.zeros_like(disc)
        if tip == 'Leaves':                                   # stretch: elongated dabs at a random angle
            u, v = _rot(xx, zz, float(rng.random() * math.pi))
            out = out * (np.abs(np.sin(u / max(0.2 * sc, px) + v * 0.3)) > 0.25)
        return out * disc
    if tip == 'Splotches':
        n = fbm(X, Z, 23, max(R * 0.5 * sc, px * 3), 4) * 0.5 + 0.5
        return disc * (n > 1 - dens * 0.9 - 0.05).astype(np.float32) * falloff(q, 0.6) ** 0.3
    if tip == 'Grunge':
        n1 = fbm(X, Z, 31, max(0.6 * sc, px * 2), 4, ridged=True)
        n2 = fbm(X, Z, 37, max(2.5 * sc, px * 4), 3)
        return disc * ((n1 * 0.6 + n2 * 0.4) > 0.55 - dens).astype(np.float32)
    u, v = _rot(X, Z, ang)                                    # u along the stroke, v across it (world aligned)
    if tip == 'Chalk':
        n = value_noise(u / max(1.6 * sc, px * 4), v / max(0.12 * sc, px), 41) * 0.65 + \
            value_noise(u / max(0.5 * sc, px * 2), v / max(0.06 * sc, px), 43) * 0.35
        return disc * (n > 0.15 - dens * 0.9).astype(np.float32)
    if tip in ('Bristles', 'Dry brush'):
        uu, vv = _rot(xx, zz, ang)
        spacing = max(0.18 * sc, px * 1.5)
        lane = np.floor((vv + 1000 * spacing) / spacing)
        on = _hash(lane.astype(np.int64), np.zeros_like(lane, np.int64), seed % 1000) < 0.35 + 0.6 * dens
        width = np.abs(((vv / spacing) % 1.0) - 0.5) < 0.32
        out = disc * (on & width).astype(np.float32)
        if tip == 'Dry brush':
            br = value_noise(u / max(0.7 * sc, px * 2), lane * 3.1, 47)
            out = out * (br > -0.1 - dens * 0.6) * (q < 0.95 - 0.25 * np.abs(vv / max(R, 1e-3)))
        return out.astype(np.float32)
    if tip == 'Cross-hatch':
        sp = max(0.6 * sc, px * 3); wdt = 0.12 + 0.35 * dens
        a = (np.abs(((u + v) / sp) % 1.0 - 0.5) < wdt / 2) | (np.abs(((u - v) / sp) % 1.0 - 0.5) < wdt / 2)
        return disc * a.astype(np.float32)
    return disc


def pixel_threshold(win, seed=5):
    """A fixed random number per area pixel (0..1) for dithering opacity."""
    iz, ix = np.mgrid[win[0], win[1]]
    return _hash(ix, iz, seed)


def tip_preview(b, size=56):
    """A small white-on-black picture of a brush tip (like a brush palette) as a uint8 array, drawn as a
    4 m brush so textures look like they do on the map."""
    bb = dict(BRUSH_DEFAULTS, **b); bb['follow'] = False; bb['angle'] = 20.0
    R = 4.0; px = 2.2 * R / size
    c = (np.arange(size) + 0.5) * px - 1.1 * R
    xx, zz = np.meshgrid(c, c)
    m = brush_tip(bb, xx + 40.0, zz + 40.0, xx, zz, R, px, np.random.default_rng(3), 0.35)
    return (np.clip(m, 0, 1) * 235 + 20 * (m > 0)).astype(np.uint8)


# =================================================================== trees, shrubs and rocks
# The game grows its trees, grass clumps and boulders itself.  What the map stores is where they may grow:
#   - the ground layer picks the biome: Forest floor = forest (trees, bushes, forest grass), Grass = plains
#     (meadow grass clumps), Rock = rocky (small rocks);
#   - the "canopy" map (0..255) is how tall the woods may be there: big trees need a high canopy, low values
#     only leave room for bushes / small trees / forest grass, 0 = nothing tall;
#   - the "rock" map (0..255) is where boulders lie (bigger values = bigger rocks).
# The reference values are the game's own: deep forest 255, forest edges 30..100, meadows 6.
NATURE = [
    # name, surface (None = leave), canopy (None = leave), rocks (None = leave), spot radius m, preview
    ('Big trees (forest)', 'forest_floor', 255, None, 3.0, 'tree'),
    ('Small trees & bushes', 'forest_floor', 60, None, 2.5, 'bush'),
    ('Bushes & forest grass', 'forest_floor', 22, None, 2.0, 'bush'),
    ('Bushes only (keeps the ground)', 'specks', 14, None, 1.0, 'bush'),
    ('Meadow grass clumps', 'grass', 6, None, 3.0, 'grass'),
    ('Large boulders', None, None, 255, 3.0, 'rock'),
    ('Small boulders', None, None, 110, 2.0, 'rock'),
    ('Small rocks (rocky ground)', 'rock', None, None, 2.0, 'pebble'),
    ('Clear - no trees or rocks', None, 0, 0, 3.0, 'clear'),
    ('Back to automatic (erase)', None, -1, -1, 3.0, 'auto'),
]
NATURE_NAMES = [n[0] for n in NATURE]
BUSH_CELL = 2.0          # 'Bushes only': at most one bush spot per 2 x 2 m
SPECK_R = 0.3            # the tiny patch of forest floor a bush needs (about one 0.5 m ground cell)


def bush_spots(X, Z, density):
    """For 'Bushes only' painting: distance (m) of each point to its 2 m cell's bush spot, and whether that
    cell gets a bush (more of them with a higher density)."""
    cx = np.floor(X / BUSH_CELL).astype(np.int64); cz = np.floor(Z / BUSH_CELL).astype(np.int64)
    jx = (cx + 0.2 + 0.6 * _hash(cx, cz, 171)) * BUSH_CELL
    jz = (cz + 0.2 + 0.6 * _hash(cx, cz, 173)) * BUSH_CELL
    live = _hash(cx, cz, 179) < 0.1 + 0.85 * float(np.clip(density, 0, 1))
    return np.hypot(X - jx, Z - jz), live


def nature_item(name):
    for it in NATURE:
        if it[0] == name: return it
    return NATURE[0]


def nature_tint(rgb, X, Z, canopy, rocks, surf, px):
    """Draw what grows where onto a shaded picture (in place): tree crowns on forest floor (bigger with a
    higher canopy), grass tufts on grass with canopy, boulders where the rock map is set, pebbles on rock."""
    if rgb.size == 0: return rgb
    canopy = np.asarray(canopy); rocks = np.asarray(rocks); surf = np.asarray(surf)
    forest = (surf == SURFACES.index('forest_floor')) & (canopy > 0)
    low = forest & (canopy <= 40)
    if low.any():                                  # bushes: a round shrub around every bit of low forest
        halo = (blur(low.astype(np.float32), max(1.0, 0.7 / px)) > 0.06) & (canopy > 0)
        zz, xx = np.nonzero(halo)
        k = (0.8 + 0.25 * _hash(np.floor(X[zz, xx] / 0.25).astype(np.int64), np.floor(Z[zz, xx] / 0.25).astype(np.int64), 7))[:, None]
        rgb[zz, xx] = np.clip(np.array([88, 122, 46], np.float32) * k, 0, 255).astype(np.uint8)
    if forest.any():
        cell = 3.2
        zz, xx = np.nonzero(forest)
        x = X[zz, xx]; z = Z[zz, xx]
        cx = np.floor(x / cell); cz = np.floor(z / cell)
        best = np.full(len(x), 9.0, np.float32); val = canopy[zz, xx].astype(np.float32)
        r = 0.45 + 2.1 * np.clip(val / 255.0, 0, 1)                      # crown radius (m)
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                kx = cx + dx; kz = cz + dz
                jx = (kx + 0.15 + 0.7 * _hash(kx.astype(np.int64), kz.astype(np.int64), 71)) * cell
                jz = (kz + 0.15 + 0.7 * _hash(kx.astype(np.int64), kz.astype(np.int64), 73)) * cell
                live = _hash(kx.astype(np.int64), kz.astype(np.int64), 79) < np.where(val > 120, 0.75, 0.55)
                d = np.hypot(x - jx, z - jz) / r
                best = np.where(live & (d < best), d, best)
        crown = best < 1
        col = np.array([34, 62, 30], np.float32)
        shade_ = (0.75 + 0.45 * np.clip(1 - best, 0, 1))[:, None]
        sel = crown
        rgb[zz[sel], xx[sel]] = np.clip(col * shade_[sel], 0, 255).astype(np.uint8)
    grass = (surf == SURFACES.index('grass')) & (canopy > 0)
    if grass.any():
        zz, xx = np.nonzero(grass)
        t = _hash(np.floor(X[zz, xx] / 0.6).astype(np.int64), np.floor(Z[zz, xx] / 0.6).astype(np.int64), 83) < 0.22
        rgb[zz[t], xx[t]] = (np.asarray(rgb[zz[t], xx[t]], np.float32) * 0.72 + np.array([40, 70, 20]) * 0.28).astype(np.uint8)
    rk = rocks > 0
    if rk.any():
        cell = 4.0
        zz, xx = np.nonzero(rk)
        x = X[zz, xx]; z = Z[zz, xx]; val = rocks[zz, xx].astype(np.float32)
        cx = np.floor(x / cell); cz = np.floor(z / cell)
        jx = (cx + 0.2 + 0.6 * _hash(cx.astype(np.int64), cz.astype(np.int64), 91)) * cell
        jz = (cz + 0.2 + 0.6 * _hash(cx.astype(np.int64), cz.astype(np.int64), 93)) * cell
        live = _hash(cx.astype(np.int64), cz.astype(np.int64), 97) < 0.45
        r = 0.4 + 1.6 * np.clip(val / 255.0, 0, 1)
        d = np.hypot(x - jx, (z - jz) * 1.25) / r
        on = live & (d < 1)
        rgb[zz[on], xx[on]] = (np.array([150, 148, 140], np.float32) * (0.7 + 0.4 * (1 - d[on]))[:, None]).clip(0, 255).astype(np.uint8)
    peb = surf == SURFACES.index('rock')
    if peb.any():
        zz, xx = np.nonzero(peb)
        t = _hash(np.floor(X[zz, xx] / 0.5).astype(np.int64), np.floor(Z[zz, xx] / 0.5).astype(np.int64), 101) < 0.12
        rgb[zz[t], xx[t]] = np.array([175, 172, 165], np.uint8)
    return rgb

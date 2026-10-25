"""
automap.py - work out which objects / material slots of an arbitrary Blender scene
become which of the game's required nodes.

Inputs
  scan      : description of the user's scene (written by gamemodel_blender.scan_scene)
  expected  : the model's node names, in order, from BlendMeshSpec.zig
  reference : the game's original file for this model (decoded), or None

Output: a "mapping" dict (also saved as <file>.gamemap.json so it can be reviewed/edited):
  {
    "model": "bike-4stroke",
    "fit": {"scale": s, "offset": [x,y,z], "applied": bool},     # game space
    "pivots": "original" | "yours",
    "units": [
      {"base": "back_tire", "kind": "mesh", "objects": ["RearWheel"], "how": "...", "score": 0.83,
       "splits": [{"node": "back_tire", "sources": [["RearWheel", 0]], "how": "..."}, ...]},
      {"base": "mount.dab.source", "kind": "object", "type": "EMPTY",
       "source": null | "MyEmpty", "how": "from original"}
    ],
    "unused": ["Camera", "Light"]
  }

Matching signals: name similarity, object type, position and size on the model (after
normalizing both models to the same overall size), and material name/colour/roughness/metal.
"""
import difflib
import math
import re

SYN = {'wheel': 'tire', 'tyre': 'tire', 'tires': 'tire', 'rear': 'back', 'chassis': 'frame', 'body': 'frame',
       'handlebar': 'handlebars', 'bars': 'handlebars', 'bar': 'handlebars', 'shock': 'suspension',
       'damper': 'suspension', 'fairing': 'plastic', 'fairings': 'plastic', 'mat': '', 'material': '',
       'object': '', 'mesh': '', 'geo': '', 'obj': ''}


def tokens(s):
    s = re.sub(r'\.\d{3}$', '', s or '')
    s = re.sub(r'([a-z])([A-Z])', r'\1 \2', s).lower()
    out = []
    for t in re.split(r'[^a-z0-9]+', s):
        t = SYN.get(t, t)
        if t and not t.isdigit(): out.append(t)
    return out


def name_sim(a, b):
    if a is None or b is None: return 0.0
    if a.lower() == b.lower(): return 1.0
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb: return 0.0
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    sub = 0.8 if (sa <= sb or sb <= sa) else 0.0
    seq = difflib.SequenceMatcher(None, ' '.join(ta), ' '.join(tb)).ratio()
    return max(jac, sub, seq * 0.85)


# ---------------------------------------------------------------- small vector math (game space)
def qmat(q):
    x, y, z, w = q['x'], q['y'], q['z'], q['w']
    return [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]


def trs(n):
    r = qmat(n['rotation']); s = n['scale']; p = n['position']
    sc = (s['x'], s['y'], s['z'])
    m = [[r[i][j] * sc[j] for j in range(3)] + [(p['x'], p['y'], p['z'])[i]] for i in range(3)]
    return m + [[0, 0, 0, 1]]


def mmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def xform(m, v):
    return [sum(m[i][k] * v[k] for k in range(3)) + m[i][3] for i in range(3)]


def bbox_of(points):
    xs, ys, zs = zip(*points)
    return [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]


def union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes: return None
    return ([min(b[0][i] for b in boxes) for i in range(3)], [max(b[1][i] for b in boxes) for i in range(3)])


def center(b): return [(b[0][i] + b[1][i]) / 2 for i in range(3)]
def extent(b): return [b[1][i] - b[0][i] for i in range(3)]
def dist(a, b): return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def iou(a, b):
    inter = 1.0
    for i in range(3):
        lo, hi = max(a[0][i], b[0][i]), min(a[1][i], b[1][i])
        if hi <= lo: return 0.0
        inter *= hi - lo
    va = max(1e-12, math.prod(extent(a))); vb = max(1e-12, math.prod(extent(b)))
    return inter / (va + vb - inter)


# ---------------------------------------------------------------- reference model
def reference_info(ref):
    """Per-node world matrix, world bbox (meshes) and material, in game space."""
    by = {n['name']: n for n in ref['nodes']}
    world = {}

    def w(name):
        if name in world: return world[name]
        n = by[name]
        m = trs(n)
        if n.get('parent') in by: m = mmul(w(n['parent']), m)
        world[name] = m
        return m
    info = {}
    for n in ref['nodes']:
        W = w(n['name'])
        box = None
        me = n.get('mesh')
        if me and me['positions']:
            lb = bbox_of([(p['x'], p['y'], p['z']) for p in me['positions']])
            corners = [[lb[i >> k & 1][k] for k in range(3)] for i in range(8)]
            box = bbox_of([xform(W, c) for c in corners])
        info[n['name']] = {'type': n['type'], 'world': W, 'box': box, 'pos': [W[0][3], W[1][3], W[2][3]],
                           'material': n.get('material'), 'parent': n.get('parent')}
    return info


def group_units(expected, ref_info, scan):
    """Group node names into units: a mesh base + its '.material.' splits, or a single node."""
    mesh_like = set()
    for name in expected:
        if ref_info and name in ref_info:
            if ref_info[name]['type'] == 'MESH': mesh_like.add(name)
        elif '.material.' in name:
            mesh_like.add(name)
    units, index = [], {}
    for name in expected:
        base = None
        if name in mesh_like and '.material.' in name:
            cands = [b for b in index if name.startswith(b + '.material.') and units[index[b]]['kind'] == 'mesh']
            if cands: base = max(cands, key=len)
        if base is not None:
            units[index[base]]['splits'].append(name); continue
        kind = 'mesh' if name in mesh_like or any(e.startswith(name + '.material.') for e in expected) else 'object'
        if ref_info and name in ref_info and ref_info[name]['type'] != 'MESH': kind = 'object'
        index[name] = len(units)
        units.append({'base': name, 'kind': kind, 'splits': [name] if kind == 'mesh' else [],
                      'type': ref_info[name]['type'] if ref_info and name in ref_info else None})
    return units


def split_material_name(base, node):
    return node[len(base) + len('.material.'):] if node != base else None


# ---------------------------------------------------------------- wheels
def _shape(points, box=None):
    """Disc test on a point cloud (game space): returns dict(center, radius, axle, round, thin) or None.
    box: exact bounding box of all vertices - a wheel is point-symmetric, so its box centre is the axle."""
    import numpy as np
    P = np.asarray(points, dtype=np.float64)
    if len(P) < 8: return None
    c = P.mean(0)
    _, _, vt = np.linalg.svd(P - c, full_matrices=False)
    proj = (P - c) @ vt.T
    lo, hi = proj.min(0), proj.max(0)
    rng = hi - lo
    order = np.argsort(-rng)
    r1, r2, r3 = rng[order]
    if r1 <= 1e-9: return None
    mid = (lo + hi) / 2
    cen = c + vt.T @ mid
    axle = vt[order[2]]
    if box is not None: cen = (np.asarray(box[0]) + np.asarray(box[1])) / 2
    return {'center': cen.tolist(), 'radius': float(r1 + r2) / 4, 'axle': axle.tolist(),
            'round': float(1 - abs(r1 - r2) / r1), 'thin': float(r3 / r2) if r2 > 0 else 1.0}


def _is_wheel(sh):
    return sh is not None and sh['round'] > 0.85 and sh['thin'] < 0.6 and abs(sh['axle'][1]) < 0.5


def _wheel_pair(items):
    """items: [(key, shape)] -> best (a, b) wheel pair or None."""
    cand = [(k, sh) for k, sh in items if _is_wheel(sh)]
    best, bs = None, -1e9
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            (ka, a), (kb, b) = cand[i], cand[j]
            sep = [b['center'][k] - a['center'][k] for k in range(3)]
            d = math.sqrt(sum(x * x for x in sep))
            if d < 1.5 * max(a['radius'], b['radius']): continue
            par = abs(sum(a['axle'][k] * b['axle'][k] for k in range(3)))
            perp = abs(sum(sep[k] / d * a['axle'][k] for k in range(3)))
            if par < 0.8 or perp > 0.4: continue
            sc = a['round'] + b['round'] - abs(math.log(a['radius'] / b['radius'])) - perp \
                + (a['radius'] + b['radius']) / d
            if sc > bs: best, bs = (ka, kb), sc
    return best


def _front_by_width(fr_c, rr_c, pts):
    """Handlebars are the widest thing above the axles: the end where the widest slice is, is the front."""
    import numpy as np
    if len(pts) < 20: return None
    a, b = np.array(rr_c), np.array(fr_c)
    ax = b - a; ax[1] = 0; L = np.linalg.norm(ax)
    if L < 1e-9: return None
    ax /= L; lat = np.array([-ax[2], 0, ax[0]])
    P = np.asarray(pts)
    P = P[P[:, 1] > max(a[1], b[1])]                     # above the axles
    if len(P) < 20: return None
    t = (P - a) @ ax / L; w = P @ lat
    widths = []
    for i in range(-2, 12):
        m = (t >= i / 10) & (t < (i + 1) / 10)
        if m.sum() >= 5: widths.append((float(np.ptp(w[m])), (i + 0.5) / 10))
    if len(widths) < 3: return None
    widths.sort(reverse=True)
    top_w, top_t = widths[0]
    if top_w < 1.15 * widths[len(widths) // 2][0]: return None     # no clear handlebar
    return top_t > 0.5                                    # True: "b" (fr_c) really is the front


def _front_of(pair, shapes, names, other_pts):
    a, b = pair
    ta, tb = set(tokens(names[a])), set(tokens(names[b]))
    if ('front' in ta and 'front' not in tb) or ('back' in tb and 'back' not in ta): return a, b, 'name'
    if ('front' in tb and 'front' not in ta) or ('back' in ta and 'back' not in tb): return b, a, 'name'
    r = _front_by_width(shapes[a]['center'], shapes[b]['center'], other_pts)
    if r is not None:
        return (a, b, 'handlebars') if r else (b, a, 'handlebars')
    return (a, b, 'facing') if shapes[a]['center'][2] > shapes[b]['center'][2] else (b, a, 'facing')


def apply_fit(f, p):
    """Similarity transform in game space: p' = s * Ry(theta) * p + t   (or p' = m * p + t)"""
    if 'm' in f:
        m = f['m']
        return [sum(m[i][k] * p[k] for k in range(3)) + f['offset'][i] for i in range(3)]
    c, s_ = math.cos(f['theta']), math.sin(f['theta'])
    x, y, z = p
    return [f['scale'] * (c * x + s_ * z) + f['offset'][0], f['scale'] * y + f['offset'][1],
            f['scale'] * (-s_ * x + c * z) + f['offset'][2]]


ORIENTS = [  # 90-degree turns to try, in game space (x right, y up, z forward)
    ('as is', [[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
    ('was lying on its back/front (Z-up/Y-up mix-up)', [[1, 0, 0], [0, 0, -1], [0, 1, 0]]),
    ('was lying on its back/front (Z-up/Y-up mix-up)', [[1, 0, 0], [0, 0, 1], [0, -1, 0]]),
    ('was lying on its side', [[0, -1, 0], [1, 0, 0], [0, 0, 1]]),
    ('was lying on its side', [[0, 1, 0], [-1, 0, 0], [0, 0, 1]]),
    ('was upside down', [[1, 0, 0], [0, -1, 0], [0, 0, -1]]),
]


def _ry(s, t):
    c, n = math.cos(t), math.sin(t)
    return [[s * c, 0, s * n], [0, s, 0], [-s * n, 0, s * c]]


def _rot_pts(O, pts):
    return [[O[i][0] * p[0] + O[i][1] * p[1] + O[i][2] * p[2] for i in range(3)] for p in pts]


def _rot_box(O, box):
    if not box: return box
    corners = _rot_pts(O, [[box[a][0], box[b][1], box[c][2]] for a in (0, 1) for b in (0, 1) for c in (0, 1)])
    return bbox_of(corners)


def is_variant(name):
    t = set(tokens(name))
    return bool(t & {'style', 'variant', 'alt', 'lod'})


class _Voxels:
    """Which original part occupies which bit of space."""
    def __init__(self, unit_points, h):
        import numpy as np
        self.h, self.np = h, np
        votes = {}
        for ui, P in unit_points.items():
            if not len(P): continue
            keys = self.key(np.floor(np.asarray(P) / h).astype(np.int64))
            uk, cnt = np.unique(keys, return_counts=True)
            for key, c in zip(uk.tolist(), cnt.tolist()):
                d = votes.setdefault(key, {}); d[ui] = d.get(ui, 0) + c
        self.map = {k: max(d, key=d.get) for k, d in votes.items()}

    @staticmethod
    def key(k):
        # exact, collision-free cell id (cells within +-2^20 of the origin)
        k = k + (1 << 20)
        return (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]

    def vote(self, pts):
        np = self.np
        k = np.floor(np.asarray(pts) / self.h).astype(np.int64)
        out = {}
        for dx, dy, dz in [(0, 0, 0)] + [(x, y, z) for x in (-1, 0, 1) for y in (-1, 0, 1) for z in (-1, 0, 1)
                                         if (x, y, z) != (0, 0, 0)]:
            keys = self.key(k + np.array([dx, dy, dz])).tolist()
            for key in keys:
                u = self.map.get(key)
                if u is not None: out[u] = out.get(u, 0) + (3 if (dx, dy, dz) == (0, 0, 0) else 1)
            if out and (dx, dy, dz) == (0, 0, 0) and sum(out.values()) > len(keys): break
        return out


# ---------------------------------------------------------------- registration (no wheels to go by)
def _rotations24():
    import itertools
    out = []
    for perm in itertools.permutations(range(3)):
        for sg in itertools.product((1, -1), repeat=3):
            m = [[0, 0, 0] for _ in range(3)]
            for i in range(3): m[i][perm[i]] = sg[i]
            det = (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                   + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
            if det == 1: out.append(m)
    return out


def _turn_name(O):
    I = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    if O == I: return 'as is'
    if O[1] == [0, 1, 0]: return 'facing another way'
    if O[1] == [0, -1, 0]: return 'upside down'
    return 'lying on its side or back'


def register(U, R, max_u=2500, max_r=6000):
    """Similarity transform (rotation, uniform scale, move) that lays point cloud U onto R.
    Tries all 24 right-angle turns with a box fit, keeps the best overlap, then refines (trimmed ICP)."""
    import numpy as np
    if len(U) < 10 or len(R) < 10: return None
    Us = U[:: max(1, len(U) // max_u)]; Rs = R[:: max(1, len(R) // max_r)]
    rmin, rmax = Rs.min(0), Rs.max(0); rc = (rmin + rmax) / 2; rd = np.linalg.norm(rmax - rmin)
    h = rd / 40
    def keys(P):
        k = np.floor(P / h).astype(np.int64) + (1 << 20)
        return (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]
    rk = keys(Rs)
    rk_dil = np.unique(np.concatenate([keys(Rs + np.array([dx, dy, dz]) * h) for dx in (-1, 0, 1)
                                       for dy in (-1, 0, 1) for dz in (-1, 0, 1)]))
    rk_u = np.unique(rk)
    best = None
    for O in _rotations24():
        O = np.asarray(O, float)
        P = Us @ O.T
        pmin, pmax = P.min(0), P.max(0); pd = np.linalg.norm(pmax - pmin)
        if pd < 1e-12: continue
        sc = rd / pd
        t = rc - sc * (pmin + pmax) / 2
        Q = P * sc + t
        a = np.isin(keys(Q), rk_dil).mean()
        b = np.isin(rk_u, np.unique(keys(Q))).mean()
        score = a * b
        if best is None or score > best[0]: best = (score, O, sc, t)
    _, O, sc, t = best
    M = sc * O
    # symmetric trimmed ICP with scale (Umeyama): your points -> nearest original, and original -> nearest yours
    try:
        from mathutils.kdtree import KDTree          # inside Blender: fast nearest-point lookups
    except ImportError:
        KDTree = None

    def nearest(A, B):
        if KDTree is not None:
            kd = KDTree(len(B))
            for i, p in enumerate(B.tolist()): kd.insert(p, i)
            kd.balance()
            res = [kd.find(p) for p in A.tolist()]
            return np.array([r[1] for r in res], np.int64), np.array([r[2] for r in res]) ** 2
        idx = np.empty(len(A), np.int64); dd = np.empty(len(A))
        for i in range(0, len(A), 400):
            D = ((A[i:i + 400, None, :] - B[None, :, :]) ** 2).sum(-1)
            j = D.argmin(1); idx[i:i + 400] = j; dd[i:i + 400] = D[np.arange(len(j)), j]
        return idx, dd
    Rq = Rs[:: max(1, len(Rs) // 2500)]
    for it in range(40):
        P = Us @ M.T + t
        i1, d1 = nearest(P, Rs)                 # yours -> original
        k1 = d1 <= np.percentile(d1, 80)
        i2, d2 = nearest(Rq, P)                 # original -> yours
        k2 = d2 <= np.percentile(d2, 70)
        X = np.concatenate([Us[k1], Us[i2[k2]]]); Y = np.concatenate([Rs[i1[k1]], Rq[k2]])
        mx, my = X.mean(0), Y.mean(0)
        Xc, Yc = X - mx, Y - my
        Cxy = Yc.T @ Xc / len(X)
        Uu, S, Vt = np.linalg.svd(Cxy)
        Dm = np.eye(3)
        if np.linalg.det(Uu) * np.linalg.det(Vt) < 0: Dm[2, 2] = -1
        Rot = Uu @ Dm @ Vt
        c = np.trace(np.diag(S) @ Dm) / max((Xc ** 2).sum() / len(X), 1e-12)
        if not (0.5 * sc <= c <= 2.0 * sc): break              # runaway scale: keep the previous fit
        Mn = c * Rot; tn = my - Mn @ mx
        done = np.abs(Mn - M).max() < 1e-7 and np.abs(tn - t).max() < 1e-7 * rd
        M, t = Mn, tn
        if done: break
    P = Us @ M.T + t
    overlap = float(np.isin(keys(P), rk_dil).mean())
    scale = float(np.cbrt(abs(np.linalg.det(M))))
    return M, t, {'scale': scale, 'overlap': overlap, 'turn': _turn_name(O.astype(int).tolist())}


# ---------------------------------------------------------------- main
def automap(scan, model, expected, reference=None, pivots='original', merge_extras=True, fit='auto',
            split_parts=True):
    import numpy as np
    ref = reference_info(reference) if reference else None
    units = group_units(expected, ref, scan)
    objs = {o['name']: o for o in scan['objects']}
    notes = []
    mesh_units = [u for u in units if u['kind'] == 'mesh']
    by_base = {u['base']: u for u in mesh_units}
    rpts = reference_points(reference, ref, max_pts=6000) if ref else {}
    upts = {u['base']: [p for s in u['splits'] for p in rpts.get(s, [])] for u in mesh_units}

    # pieces = loose parts of your objects (or whole objects)
    pieces = []
    for o in scan['objects']:
        if o['type'] != 'MESH': continue
        parts = o.get('islands') if split_parts else None
        if parts:
            for isl in parts:
                pieces.append({'obj': o['name'], 'island': isl['id'], 'points': isl['points'], 'box': isl.get('box'),
                               'polys': isl['polys'], 'slots': isl['slots'], 'n': isl['n']})
        elif o.get('points'):
            pieces.append({'obj': o['name'], 'island': -1, 'points': o['points'], 'polys': o.get('polys', 0),
                           'slots': {str(sl['index']): sl['polys'] for sl in o.get('slots', [])}, 'n': len(o['points'])})
    for pc in pieces:
        pc['label'] = pc['obj'] if pc['island'] < 0 else '%s part %d' % (pc['obj'], pc['island'])

    # ---- the game's wheels are always kept: your models don't have wheels
    keep = set()
    if ref:
        rshapes = {b: _shape(p, ref[b]['box']) for b, p in upts.items() if len(p) >= 8 and not is_variant(b)
                   and b in ref and ref[b]['box']}
        rp = _wheel_pair(list(rshapes.items()))
        if rp: keep |= set(rp)
    keep |= {u['base'] for u in mesh_units if set(tokens(u['base'])) & {'tire', 'wheel'}}
    for u in mesh_units:
        u['keep_original'] = u['base'] in keep
    if keep: notes.append('kept the game\'s own %s' % ' and '.join(sorted(keep)))

    # ---- line your model up with the original (minus the parts kept from the game)
    fitd = {'scale': 1.0, 'theta': 0.0, 'offset': [0.0, 0.0, 0.0], 'applied': False, 'method': 'none'}
    wheel_of, wheel_check = {}, None
    allu = [p for pc in pieces for p in pc['points']]
    if ref and allu:
        target = [p for b, pts in upts.items() if b not in keep and not is_variant(b) for p in pts]
        if not target: target = [p for pts in upts.values() for p in pts]
        res = register(np.asarray(allu), np.asarray(target))
        if res:
            M, t, info = res
            fitd = {'m': M.tolist(), 'offset': t.tolist(), 'applied': True, 'method': 'registration',
                    'scale': info['scale'], 'theta': 0.0}
            if info['turn'] != 'as is': notes.append('your model was %s - turned to match the game\'s' % info['turn'])
            notes.append('lined up with the original by shape: scale x%.3g, %d%% of your surface lies on the '
                         'original\'s (check in Align parts)' % (info['scale'], round(100 * info['overlap'])))
    fit_internal = fitd if fitd['applied'] else None
    rb = union([i['box'] for i in ref.values() if i['box']]) if ref else None
    ub = bbox_of(allu) if allu else None
    size = dist(*rb) if rb else (dist(*ub) if ub else 1.0)

    # ---- every other piece goes to the original part occupying the same space
    assign = dict(wheel_of)
    if ref:
        targets = {b: p for b, p in upts.items() if p and not is_variant(b)}
        vox = _Voxels(targets, size / 45.0)
        cent = {b: np.mean(np.asarray(p), 0) for b, p in targets.items() if b not in keep}
        dropped_kept = 0
        for i, pc in enumerate(pieces):
            if i in assign: continue
            P = [apply_fit(fit_internal, p) for p in pc['points']] if fit_internal else pc['points']
            v = vox.vote(P)
            if v and max(v, key=v.get) in keep:
                dropped_kept += 1; continue          # sits where a kept game part (wheel) is: left out
            v = {b: n for b, n in v.items() if b not in keep}
            if v:
                assign[i] = max(v, key=v.get)
            elif cent:
                c = np.mean(np.asarray(P), 0)
                assign[i] = min(cent, key=lambda b: float(np.linalg.norm(cent[b] - c)))
        if dropped_kept:
            notes.append('%d of your pieces sit where the game\'s kept parts (wheels) are - left out' % dropped_kept)
    else:
        for i, pc in enumerate(pieces):
            best = max(mesh_units, key=lambda u: name_sim(pc['obj'], u['base']), default=None)
            if best and name_sim(pc['obj'], best['base']) >= 0.5: assign[i] = best['base']

    if wheel_check:
        uf, rf, ur, rr, s_, ushapes, rshapes = wheel_check
        for ui_, ub, lbl in ((uf, rf, 'front'), (ur, rr, 'rear')):
            sh = ushapes[ui_]; c = np.array(sh['center']); axv = np.array(sh['axle']); R = sh['radius']
            P = []
            for i, b in assign.items():          # everything on this wheel's ring (tyre, knobs, rim)
                if b != ub: continue
                Q = np.asarray(pieces[i]['points']) - c
                ad = Q @ axv; rd = np.linalg.norm(Q - np.outer(ad, axv), axis=1)
                if rd.max() <= 1.6 * R and np.abs(ad).max() <= 0.6 * R: P.append(Q)
            if not P: continue
            P = np.concatenate(P)
            Ru = float(np.max(np.linalg.norm(P - np.outer(P @ axv, axv), axis=1))) * s_
            rsh = rshapes[ub]; Q = np.asarray(upts[ub]) - np.asarray(ref[ub]['pos']); rax = np.array(rsh['axle'])
            Rr = float(np.max(np.linalg.norm(Q - np.outer(Q @ rax, rax), axis=1)))
            if abs(Ru / Rr - 1) > 0.06:
                notes.append('your %s wheel is %d%% %s than the game\'s after scaling - it may float or sink '
                             '(radius %.3f vs %.3f)' % (lbl, abs(Ru / Rr - 1) * 100, 'larger' if Ru > Rr else 'smaller', Ru, Rr))

    # exact object names always win (models that already use the game's names)
    for i, pc in enumerate(pieces):
        if pc['obj'] in by_base: assign[i] = pc['obj']

    # ---- per part: group pieces by material, biggest material -> main node, others -> closest look
    for u in mesh_units:
        if u.get('keep_original'):
            u['objects'] = []; u['pieces'] = 0; u['score'] = 1.0
            u['splits'] = [{'node': sp, 'sources': [], 'how': 'game original'} for sp in u['splits']]
            u['how'] = 'game original kept'
            continue
        mine = [i for i, b in assign.items() if b == u['base']]
        u['objects'] = sorted({pieces[i]['obj'] for i in mine})
        u['pieces'] = len(mine)
        groups = {}
        for i in mine:
            pc = pieces[i]
            for slot, n in pc['slots'].items():
                if not n: continue
                sl = (objs[pc['obj']].get('slots') or [{}])[int(slot)] if objs[pc['obj']].get('slots') else {}
                key = sl.get('material')
                g = groups.setdefault(key, {'polys': 0, 'src': [], 'slot': sl})
                g['polys'] += n; g['src'].append([pc['obj'], int(slot), pc['island']])
        splits = u['splits']
        src = {s: [] for s in splits}; how = {s: [] for s in splits}
        ordered = sorted(groups.items(), key=lambda kv: -kv[1]['polys'])
        for gi, (mat, g) in enumerate(ordered):
            if gi == 0 and not (mat is None and any(s.endswith('.material.no_material') for s in splits)):
                best, why = splits[0], 'main material'
            else:
                best, bs, why = splits[0], -9.0, 'main material'
                for s in splits[1:]:
                    mname = split_material_name(u['base'], s)
                    if mname == 'no_material':
                        sc = 1.0 if mat is None else -1.0
                    else:
                        rmat = ref[s]['material'] if ref and s in ref else None
                        sc = 0.3 * name_sim(mat, mname) + 0.7 * (prop_sim(g['slot'], rmat) if rmat else 0)
                        if src[s]: sc -= 0.5
                    if sc > bs: best, bs, why = s, sc, 'closest look'
            src[best].extend(g['src'])
            how[best].append('%s (%s, %d faces)' % (mat or 'no material', why, g['polys']))
        u['splits'] = [{'node': s, 'sources': src[s], 'how': '; '.join(how[s]) if src[s] else 'empty'}
                       for s in splits]
        for s in splits:
            mats = [h.split(' (')[0] for h in how[s]]
            if len(mats) > 1:
                notes.append('%s: %s will all show as "%s" (the game part has fewer material slots than you use)'
                             % (u['base'], ', '.join(mats[1:]), mats[0]))
        if mine:
            kinds = {('wheel' if i in wheel_of else 'position') for i in mine}
            ol = u['objects']
            names_txt = ', '.join(ol[:4]) + (' +%d more objects' % (len(ol) - 4) if len(ol) > 4 else '')
            u['how'] = '%d part%s from %s (%s)' % (len(mine), 's' if len(mine) > 1 else '', names_txt,
                                                    '/'.join(sorted(kinds)))
        else:
            u['how'] = ('style variant - left empty' if is_variant(u['base']) else
                        'none of your parts is here - left empty')
        u['score'] = 1.0 if mine else 0.0

    used = {pieces[i]['obj'] for i in assign}
    # ---- single-object nodes (mount points, camera, lattices, curves): keep the game's unless named the same
    for u in units:
        if u['kind'] != 'object': continue
        hit = next((o for o in scan['objects'] if o['name'] == u['base'] and o['name'] not in used
                    and (not u['type'] or o['type'] == u['type'])), None)
        if hit:
            u['source'] = hit['name']; used.add(hit['name']); u['how'] = 'same name'
        else:
            u['source'] = None
            u['how'] = 'from original game file' if ref and u['base'] in ref else 'MISSING - no original to copy from'
        u['score'] = 1.0 if hit else 0.0

    unused = [o['name'] for o in scan['objects'] if o['name'] not in used]
    return {'model': model, 'fit': fitd, 'pivots': pivots, 'units': units, 'unused': unused, 'notes': notes,
            'split_parts': split_parts}


_RP = {}


def reference_points(reference, ref, max_pts=1500):
    """World-space (game) vertex samples of every mesh node of the original, cached."""
    key = id(reference)
    if key in _RP: return _RP[key]
    out = {}
    for n in reference['nodes']:
        me = n.get('mesh')
        if not me or not me['positions']: continue
        W = ref[n['name']]['world']
        step = max(1, len(me['positions']) // max_pts)
        out[n['name']] = [xform(W, (p['x'], p['y'], p['z'])) for p in me['positions'][::step]]
    _RP[key] = out
    return out


def prop_sim(slot, rmat):
    c, rc = slot.get('color') or [0.8, 0.8, 0.8, 1], rmat['color']
    d = math.sqrt((c[0] - rc['x']) ** 2 + (c[1] - rc['y']) ** 2 + (c[2] - rc['z']) ** 2)
    d += 0.5 * abs(slot.get('roughness', 0.5) - rmat['roughness']) + 0.5 * abs(slot.get('metallic', 0) - rmat['metallic'])
    return math.exp(-d / 0.12)


def summary(mapping):
    """Human-readable lines describing a mapping."""
    out = ['INFO ' + n for n in mapping.get('notes', [])]
    for u in mapping['units']:
        if u['kind'] == 'mesh' and u.get('keep_original'):
            out.append('INFO %s  <-  the game\'s original' % u['base'])
        elif u['kind'] == 'mesh':
            tag = 'INFO ' if u.get('objects') or 'variant' in u['how'] else 'WARN '
            out.append(tag + '%s  <-  %s' % (u['base'], u['how']))
            if u.get('objects') and len(u['splits']) > 1:
                for s in u['splits']:
                    if s['sources']: out.append('INFO    %s  <-  %s' % (s['node'], s['how']))
        elif u['how'].startswith('MISSING'):
            out.append('ERROR %s  <-  %s' % (u['base'], u['how']))
        elif u.get('source'):
            out.append('INFO %s  <-  %s' % (u['base'], u['source']))
    n_orig = sum(1 for u in mapping['units'] if u['kind'] != 'mesh' and not u.get('source') and not u['how'].startswith('MISSING'))
    if n_orig: out.append('INFO %d mount points / helpers kept from the original game file' % n_orig)
    un = mapping['unused']
    if un: out.append('INFO not used: ' + ', '.join(un[:12]) + (' +%d more' % (len(un) - 12) if len(un) > 12 else ''))
    return out


# ---------------------------------------------------------------- manual adjustments (alignment editor)
# mapping['adjust'] = {'__all__': A, '<game part>': A, ...}
#   A = {'t': [x, y, z], 'r': [rx, ry, rz] (degrees), 's': scale, 'pivot': [x, y, z]}   (game space)
# A part's final placement = A_part( A_all( p ) ); each rotates/scales about its own pivot.
def _rot3(r):
    rx, ry, rz = (math.radians(a) for a in r)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    Ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    Rz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    mm = lambda a, b: [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mm(Rz, mm(Ry, Rx))


def adjust_matrix(a):
    """4x4 (row-major nested lists) for one adjustment, game space."""
    if not a: return [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    R = _rot3(a.get('r', [0, 0, 0])); s = a.get('s', 1.0); t = a.get('t', [0, 0, 0]); pv = a.get('pivot', [0, 0, 0])
    M = [[R[i][j] * s for j in range(3)] for i in range(3)]
    tr = [pv[i] + t[i] - sum(M[i][k] * pv[k] for k in range(3)) for i in range(3)]
    return [M[0] + [tr[0]], M[1] + [tr[1]], M[2] + [tr[2]], [0, 0, 0, 1]]


def part_matrix(mapping, part):
    """Combined adjustment for a game part: A_part @ A_all."""
    adj = mapping.get('adjust') or {}
    return mmul(adjust_matrix(adj.get(part)), adjust_matrix(adj.get('__all__')))


def is_identity(a):
    return (not a) or (all(abs(x) < 1e-12 for x in a.get('t', [0, 0, 0]) + a.get('r', [0, 0, 0]))
                       and abs(a.get('s', 1.0) - 1) < 1e-12)


# ---------------------------------------------------------------- per-piece moves and game materials
def piece_key(obj, island):
    return '%s|%d' % (obj, -1 if island is None else island)


def game_material_catalog(reference):
    """Distinct materials of the original model: [{'id', 'material', 'used_by': [...]}], named after the
    game's material slots (e.g. 'Chrome', 'Seat'); a part's unnamed main material is '<part> (main)'."""
    by_key, order = {}, []
    for n in reference['nodes']:
        m = n.get('material')
        if not m or not n.get('mesh'): continue
        c = m['color']
        key = (round(c['x'], 3), round(c['y'], 3), round(c['z'], 3), round(m['roughness'], 3),
               round(m['metallic'], 3), m.get('diffuse_texture'), m.get('normal_texture'))
        named = '.material.' in n['name']
        name = n['name'].split('.material.', 1)[1] if named else '%s (main)' % n['name']
        if key not in by_key:
            by_key[key] = {'id': name, 'material': m, 'used_by': [n['name']], 'named': named}; order.append(key)
        else:
            e = by_key[key]; e['used_by'].append(n['name'])
            if named and not e['named']: e['id'], e['named'] = name, True
    out, seen = [], set()
    for k in order:
        e = by_key[k]
        if e['id'] in seen: e['id'] = '%s (%s)' % (e['id'], e['used_by'][0])
        seen.add(e['id'])
        out.append({'id': e['id'], 'material': e['material'], 'used_by': e['used_by']})
    return out


def apply_paint(mapping, catalog, log=None):
    """Route pieces painted with a game material into suitable material nodes of their game part.
    Returns a modified copy of the mapping; nodes that carry a game material get 'material_override'."""
    import copy
    mp = copy.deepcopy(mapping)
    paint = mp.get('paint') or {}
    if not paint: return mp
    cat = {c['id']: c for c in catalog}
    for u in mp['units']:
        if u['kind'] != 'mesh' or u.get('keep_original'): continue
        groups = {}
        for sp in u['splits']:
            keep = []
            for s in sp['sources']:
                gid = paint.get(piece_key(s[0], s[2] if len(s) > 2 else -1))
                if gid in cat: groups.setdefault(gid, []).append(s)
                else: keep.append(s)
            sp['sources'] = keep
        if not groups: continue
        base = u['base']
        taken = {}
        free = [sp for sp in u['splits'] if not sp['sources'] and not sp['node'].endswith('.material.no_material')]
        for gid in sorted(groups, key=lambda g: -len(groups[g])):
            name_match = next((sp for sp in u['splits'] if sp['node'] == '%s.material.%s' % (base, gid)
                               and sp['node'] not in taken), None)
            target = name_match
            if target is None and free:
                target = next((sp for sp in free if sp['node'] not in taken), None)
            if target is None:
                target = u['splits'][0]
                if log: log('WARN %s: more materials than the game part has slots - "%s" shares "%s"'
                            % (base, gid, target['node']))
            if target in free: free.remove(target)
            target['sources'].extend(groups[gid])
            if target['node'] not in taken:
                target['material_override'] = cat[gid]['material']
                taken[target['node']] = gid
            target['how'] = 'game material: ' + taken[target['node']]
    return mp


def mat4_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def delta_matrix(t=(0, 0, 0), r=(0, 0, 0), s=1.0, pivot=(0, 0, 0)):
    return adjust_matrix({'t': list(t), 'r': list(r), 's': s, 'pivot': list(pivot)})

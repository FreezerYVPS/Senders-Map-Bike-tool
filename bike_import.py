"""
bike_import.py - bring an MX Bikes (.edf) or MX Simulator (.saf / .jm) bike into the exporter.

The bike is read into plain parts (metres, Senders axes: x side, y up, z forward), assembled, written to a
parts folder (parts.npz + parts.json + textures as PNG) and turned into a .blend by Blender (gamemodel_blender
--build-parts). That .blend then goes through the normal exporter, so matching, the 3D editor and line-up
presets all work on it.

MX Simulator: a bike is several .jm models named <bike>_<part>-<model>.jm (frame, fork_upper, fork_lower,
swingarm) plus skins <bike>-<model>-<skin>.png. MX Simulator gives every bike the same chassis (its physics
uses one generic geometry), and each part is modelled around its own physics body, so the parts are put
together on that chassis: the swingarm on the rear axle, the fork bottoms on the front axle, the upper fork
on the same steering axis, the frame on the chassis - here measured against Senders' own bike, which sits at
true size. JM is in feet with z pointing backwards; MX Simulator's world is the mirror image of Senders',
so (x, y, z) feet -> (x, y, -z) * 0.3048 m.
Files the author scrambled (.scram) are protected and are not unscrambled.

MX Bikes: see edf_io. The steering and suspension blocks are modelled upright; they are tilted to the
steering angle around the steering head.
"""
import io
import json
import os
import re

import numpy as np

import edf_io
import mxs_io

FT = 0.3048
PART_KINDS = ('frame', 'fork_upper', 'fork_lower', 'swingarm', 'front_wheel', 'rear_wheel')

# MX Simulator's generic chassis, in each part's own coordinates (feet, MX Simulator axes), measured on the
# stock models: where the axles are, and the steering axis direction (top end leaning back).
MXS_FRONT_AXLE_IN_FORK_LOWER = np.array([0.0, -0.70, -0.42])
MXS_REAR_AXLE_IN_SWINGARM = np.array([0.0, -0.31, 1.03])
MXS_STEER_AXIS_UP = np.array([0.0, 0.906, 0.423])
MXB_RAKE_DEG = 26.0
# MX Bikes models stand on y = 0. Standard motocross tyres: 80/100-21 front, 100/90-19 rear (radius, metres) -
# the fork is slid along the steering axis / the swingarm swung about its pivot so the axles sit at these heights.
MXB_FRONT_RADIUS = 0.347
MXB_REAR_RADIUS = 0.330
# which game part each imported group goes to
UNIT_OF = {'frame': 'frame', 'fork_upper': 'handlebars.top', 'fork_lower': 'handlebars.bottom', 'swingarm': 'swingarm',
           'chassis': 'frame', 'steer': 'handlebars.top', 'fsusp': 'handlebars.bottom', 'rsusp': 'swingarm'}


class ImportError_(ValueError):
    pass


# ------------------------------------------------------------------ JM (MX Simulator models)
def read_jm(data):
    """(P, N, UV, F) of a JM1/JM2 model (feet, MX Simulator axes)."""
    txt = data.decode('latin1')
    lines = txt.split('\n')
    head = lines[0].strip()
    if head not in ('JM1', 'JM2'): raise ImportError_('not a JM model')
    nb, start = 0, 1
    if head == 'JM2':
        nb = int(lines[1].strip()); start = 3
    tok = np.array(' '.join(lines[start:]).split())
    W = 8 + nb
    i = 0; Ps, Ns, Us, Fs = [], [], [], []; base = 0
    while i < len(tok):
        i += 5
        nv, nt = int(tok[i]), int(tok[i + 1]); i += 2
        V = tok[i:i + nv * W].astype(np.float64).reshape(nv, W); i += nv * W
        F = tok[i:i + nt * 3].astype(np.int64).reshape(nt, 3); i += nt * 3
        Ps.append(V[:, 0:3]); Ns.append(V[:, 3:6]); Us.append(V[:, 6:8]); Fs.append(F + base); base += nv
    if not Ps: raise ImportError_('the JM model is empty')
    return np.vstack(Ps), np.vstack(Ns), np.vstack(Us), np.vstack(Fs)


def mxs_to_senders(P, N, F):
    """Feet, MX Simulator axes -> metres, Senders axes (a mirror: triangle order flips too)."""
    P = P * np.array([FT, FT, -FT]); N = N * np.array([1.0, 1.0, -1.0])
    return P, N, F[:, ::-1].copy()


_JM_RE = re.compile(r'^(?P<bike>.+?)_(?P<part>frame|fork_upper|fork_lower|swingarm|front_wheel|rear_wheel)-(?P<model>.+)\.jm(?P<scram>\.scram)?$', re.I)
_PNG_RE = re.compile(r'^(?P<bike>.+?)-(?P<model>.+?)(?:-(?P<skin>.+?))?(?P<kind>_norm|_spec)?\.png(?P<scram>\.scram)?$', re.I)


def mxs_bikes(path):
    """The bikes in an MX Simulator .saf (or folder of .jm files): [{'key', 'bike', 'model', 'parts':
    {kind: (name, entry)}, 'scrambled': [kinds], 'skins': [(skin, name, entry, scrambled)]}]."""
    if os.path.isdir(path):
        entries = []
        for root, _, files in os.walk(path):
            for f in files:
                p = os.path.join(root, f); entries.append((os.path.relpath(p, path).replace('\\', '/'), p, None))
        reader = lambda e: open(e[1], 'rb').read()
    else:
        entries = mxs_io.saf_index(path)
        reader = lambda e: mxs_io.saf_read(path, e)
    bikes = {}
    for e in entries:
        base = e[0].split('/')[-1]
        m = _JM_RE.match(base)
        if m:
            b = bikes.setdefault((m['bike'], m['model']), {'bike': m['bike'], 'model': m['model'], 'parts': {},
                                                            'scrambled': [], 'skins': []})
            if m['scram']: b['scrambled'].append(m['part'].lower())
            else: b['parts'][m['part'].lower()] = e
    for e in entries:
        base = e[0].split('/')[-1]
        m = _PNG_RE.match(base)
        if not m or m['kind']: continue
        for (bk, md), b in bikes.items():
            if m['bike'] == bk and (m['model'] == md or base.startswith('%s-%s' % (bk, md))):
                skin = base[len('%s-%s' % (bk, md)):].lstrip('-')
                skin = re.sub(r'\.png(\.scram)?$', '', skin, flags=re.I) or md
                b['skins'].append((skin, base, e, bool(m['scram'])))
    out = []
    for (bk, md), b in sorted(bikes.items()):
        b['key'] = '%s (%s)' % (md, bk); b['reader'] = reader
        out.append(b)
    return out


# ------------------------------------------------------------------ the game's bike: where its parts are
def _qmat(q):
    x, y, z, w = q['x'], q['y'], q['z'], q['w']
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def template_boxes(doc):
    """{part base name: (min, max)} of the game bike's parts in world space (metres, game axes)."""
    nodes = {n['name']: n for n in doc['nodes']}

    def world(n, depth=0):
        T = np.eye(4)
        T[:3, :3] = _qmat(n['rotation']) * np.array([n['scale'][k] or 1.0 for k in 'xyz'])
        T[:3, 3] = [n['position'][k] for k in 'xyz']
        p = n.get('parent')
        return (world(nodes[p], depth + 1) @ T) if p in nodes and depth < 32 else T
    out = {}
    for name, n in nodes.items():
        me = n.get('mesh')
        if not me or not me.get('positions'): continue
        P = np.array([[v['x'], v['y'], v['z']] for v in me['positions']])
        M = world(n); W = P @ M[:3, :3].T + M[:3, 3]
        base = name.split('.material.')[0]
        lo, hi = W.min(0), W.max(0)
        if base in out: lo, hi = np.minimum(out[base][0], lo), np.maximum(out[base][1], hi)
        out[base] = (lo, hi)
    return out


def _center(b): return (b[0] + b[1]) / 2


def _union(boxes, names):
    got = [boxes[n] for n in boxes if any(n == k or n.startswith(k + '.') for k in names)]
    if not got: return None
    return np.min([g[0] for g in got], 0), np.max([g[1] for g in got], 0)


def assemble_mxs(parts, boxes):
    """Move MX Simulator parts (already in metres / Senders axes, each around its own origin) onto the
    chassis, measured against the game bike's boxes. parts: {kind: dict(P, ...)}. Returns notes."""
    notes = []
    conv = lambda v: v * np.array([FT, FT, -FT])
    front = boxes.get('front_tire'); rear = boxes.get('back_tire')
    if front is None or rear is None:
        notes.append("the game bike's wheels weren't found - the parts are left where they are"); return notes
    fa, ra = _center(front), _center(rear)
    axis = conv(MXS_STEER_AXIS_UP); axis /= np.linalg.norm(axis)
    off = {}
    if 'swingarm' in parts: off['swingarm'] = ra - conv(MXS_REAR_AXLE_IN_SWINGARM)
    if 'fork_lower' in parts: off['fork_lower'] = fa - conv(MXS_FRONT_AXLE_IN_FORK_LOWER)
    if 'fork_upper' in parts:
        base = off.get('fork_lower', fa - conv(MXS_FRONT_AXLE_IN_FORK_LOWER))
        bars = _union(boxes, ['handlebars.top', 'handlebars.bottom'])
        top = parts['fork_upper']['P'][:, 1].max()
        if bars is not None:
            d = (bars[1][1] - top - base[1]) / max(axis[1], 1e-6)
        else:
            d = 0.25
        off['fork_upper'] = base + d * axis
    if 'frame' in parts:
        fr = _union(boxes, ['frame'])
        P = parts['frame']['P']
        if fr is not None:
            c = _center(fr); lo, hi = P.min(0), P.max(0)
            off['frame'] = np.array([c[0] - (lo[0] + hi[0]) / 2, fr[1][1] - hi[1], c[2] - (lo[2] + hi[2]) / 2])
    if 'front_wheel' in parts: off['front_wheel'] = fa
    if 'rear_wheel' in parts: off['rear_wheel'] = ra
    for k, o in off.items():
        parts[k]['P'] = parts[k]['P'] + o
    notes.append('parts put together on the chassis (axles on the game bike\'s axles) - check them in '
                 '"Edit model (3D)" once and save a line-up preset; every MX Simulator bike shares this chassis')
    return notes


def load_mxs(path, bike_key=None, skin=None, boxes=None, log=None):
    """(parts list, textures {name: RGBA}, info) for one bike of an MX Simulator .saf / folder."""
    from PIL import Image
    bikes = mxs_bikes(path)
    if not bikes: raise ImportError_('no MX Simulator bike models (.jm) in %s' % os.path.basename(path))
    b = next((x for x in bikes if x['key'] == bike_key), bikes[0]) if bike_key else bikes[0]
    if not b['parts']:
        raise ImportError_('every model file of %s is scrambled (protected by its author) - it can\'t be '
                           'imported' % b['key'])
    raw = {}
    for kind, e in b['parts'].items():
        P, N, UV, F = read_jm(b['reader'](e))
        P, N, F = mxs_to_senders(P, N, F)
        raw[kind] = {'P': P, 'N': N, 'UV': UV, 'F': F}
    notes = []
    if b['scrambled']:
        notes.append('scrambled (protected) parts left out: %s' % ', '.join(sorted(set(b['scrambled']))))
    if boxes is not None: notes += assemble_mxs(raw, boxes)
    tex = {}; tex_name = None
    skins = [s for s in b['skins'] if not s[3]]
    pick = next((s for s in skins if s[0] == skin), skins[0] if skins else None)
    if pick is not None:
        try:
            im = Image.open(io.BytesIO(b['reader'](pick[2]))).convert('RGBA')
            tex_name = 'skin'; tex[tex_name] = np.asarray(im)
        except Exception as e:
            notes.append("the skin %s couldn't be read (%s)" % (pick[1], e))
    elif b['skins']:
        notes.append('every skin of this bike is scrambled (protected) - the parts come in untextured')
    parts = [{'name': k, 'group': k, 'mat': tex_name, 'uv_flip': False, **v} for k, v in raw.items()]
    return parts, tex, {'key': b['key'], 'bike': b['bike'], 'model': b['model'], 'notes': notes,
                        'skin': pick[0] if pick else None, 'family': 'MX Simulator'}


# ------------------------------------------------------------------ MX Bikes
def _rot_x(deg):
    a = np.radians(deg); c, s = np.cos(a), np.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def load_edf(path, rake=MXB_RAKE_DEG, log=None):
    parts, tex = edf_io.parts(path, log=log)
    if not parts: raise ImportError_('no geometry in %s' % os.path.basename(path))
    labels = {p['group'] for p in parts}
    offs, _ = edf_io.assembly_offsets(path, list(labels))
    steer = next((o for l, o in offs.items() if l.lower().startswith('steer')), None)
    notes = []
    if steer is not None and rake:
        R = _rot_x(-rake)                                   # tilt the top back (towards -z)
        for p in parts:
            g = p['group'].lower()
            if g.startswith(('steer', 'fsusp')):
                p['P'] = (p['P'] - steer) @ R.T + steer
                p['N'] = p['N'] @ R.T
        notes.append('steering and front suspension tilted to %g degrees around the steering head' % rake)
        _seat_mxb(parts, R @ np.array([0.0, -1.0, 0.0]), notes)
    for p in parts: p['uv_flip'] = True
    name = os.path.basename(os.path.dirname(os.path.abspath(path))) if os.path.basename(path).lower() == 'model.edf' \
        else os.path.splitext(os.path.basename(path))[0]
    return parts, tex, {'key': name, 'notes': notes, 'family': 'MX Bikes'}


def _group_pts(parts, prefix):
    sel = [p for p in parts if p['group'].lower().startswith(prefix)]
    return sel, (np.concatenate([p['P'] for p in sel]) if sel else None)


def _seat_mxb(parts, down, notes):
    """Put the front and rear axles at tyre height, as the bike stands at runtime."""
    import autoalign
    fs, P = _group_pts(parts, 'fsusp')
    e = autoalign.ends(P) if P is not None else None
    if e is not None:
        axle = e[0] if e[0][1] < e[1][1] else e[1]
        k = (axle[1] - MXB_FRONT_RADIUS) / max(0.2, -down[1])
        if abs(k) < 0.6:
            for p in fs: p['P'] = p['P'] + k * down
            notes.append('front suspension slid %.0f cm along the fork so the front axle is at tyre height' % (k * 100))
    rs, P = _group_pts(parts, 'rsusp')
    e = autoalign.ends(P) if P is not None else None
    if e is not None:
        axle, piv = (e[0], e[1]) if e[0][2] < e[1][2] else (e[1], e[0])
        v = axle[1:] - piv[1:]; L = float(np.linalg.norm(v))
        dy = MXB_REAR_RADIUS - piv[1]
        if L > 0.2 and abs(dy) < L:
            w = np.array([dy, -np.sqrt(L * L - dy * dy)])
            a = np.degrees(np.arctan2(w[1], w[0]) - np.arctan2(v[1], v[0]))
            a = (a + 180) % 360 - 180
            if abs(a) < 35:
                R = _rot_x(a)
                for p in rs:
                    p['P'] = (p['P'] - piv) @ R.T + piv; p['N'] = p['N'] @ R.T
                notes.append('swingarm swung %.1f degrees about its pivot so the rear axle is at tyre height' % a)


# ------------------------------------------------------------------ writing the parts for Blender
def write_parts(folder, parts, tex):
    """parts.npz + parts.json + <texture>.png in folder; returns the json path."""
    from PIL import Image
    os.makedirs(folder, exist_ok=True)
    arrays, meta = {}, []
    for i, p in enumerate(parts):
        uv = p['UV'].copy()
        if p.get('uv_flip'): uv[:, 1] = 1.0 - uv[:, 1]
        arrays['P%d' % i] = p['P'].astype(np.float32); arrays['N%d' % i] = p['N'].astype(np.float32)
        arrays['U%d' % i] = uv.astype(np.float32); arrays['F%d' % i] = p['F'].astype(np.int32)
        g = p.get('group', '')
        meta.append({'name': p['name'], 'group': g, 'mat': p.get('mat'),
                     'unit': UNIT_OF.get(g.lower()) or next((u for k, u in UNIT_OF.items() if g.lower().startswith(k)), None)})
    np.savez(os.path.join(folder, 'parts.npz'), **arrays)
    images = {}
    for k, a in tex.items():
        fn = re.sub(r'[^A-Za-z0-9_.-]+', '_', k) + '.png'
        Image.fromarray(np.ascontiguousarray(a), 'RGBA').save(os.path.join(folder, fn))
        images[k] = fn
    jp = os.path.join(folder, 'parts.json')
    with open(jp, 'w') as f: json.dump({'parts': meta, 'images': images}, f, indent=1)
    return jp


def import_folder(source):
    """Where an imported bike's files go: Documents\\GameModelExporter\\imports\\<name>."""
    base = os.path.join(os.path.expanduser('~'), 'Documents', 'GameModelExporter', 'imports')
    name = re.sub(r'[^A-Za-z0-9 _.()-]+', '_', source).strip() or 'bike'
    return os.path.join(base, name)

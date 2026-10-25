"""
objects_build.py - turn MX Bikes track objects (read by mxb_objects) into objects in Senders' map.

Senders' models can only use the textures the game knows (a fixed list).  So all the objects' textures are
packed into one texture atlas, which is installed in place of one of those textures (logo-transparent.png - the
small logo on the backpack, which therefore shows a bit of the atlas instead).  Every object triangle gets its
texture coordinates moved into its texture's spot in the atlas; triangles whose texture repeats (tiles) are cut
along the tile edges first, so each piece maps into one copy of the texture.

    objs = load(path)                                    # read once (cached)
    parts = [placed(objs, rot, mirror, sx, sz, offset, ...)]
    atlas, nodes = build(parts, spec, atlas_size)        # atlas: RGBA image (top row first), nodes: bytes, count
"""
import math
import os
import struct

import numpy as np

import mxb_objects as MO
import zigspec

TEXTURE_SLOT = '../model-textures/logo-transparent.png'      # the game texture the atlas replaces
SLOT_FILE = 'logo-transparent.png'
GROUND_AREA = 20000.0          # m2 of flat ground: a material this big is ground / backdrop, not an object
NODE_PREFIX = 'GME.Objects.'
PAD = 8                        # pixels of wrapped texture around each atlas entry (no bleeding)

_cache = {}


def load(path):
    """The objects of an MX Bikes track (cached while the file is unchanged)."""
    key = os.path.abspath(path)
    try: mt = os.path.getmtime(path)
    except OSError: mt = None
    c = _cache.get(key)
    if c is not None and c[0] == mt: return c[1]
    d = MO.load(path)
    _cache.clear(); _cache[key] = (mt, d)                     # one track at a time (they are big)
    return d


def ground_materials(d):
    """Materials that are really ground / backdrop planes (huge flat areas), left out by default."""
    P = d['P'].astype(np.float64); I = d['I'].astype(np.int64)
    A, B, C = P[I[:, 0]], P[I[:, 1]], P[I[:, 2]]
    hz = np.abs((B[:, 0] - A[:, 0]) * (C[:, 2] - A[:, 2]) - (B[:, 2] - A[:, 2]) * (C[:, 0] - A[:, 0])) / 2
    tot = np.bincount(d['tri_mat'], weights=hz, minlength=len(d['materials']))
    return {k for k in range(len(tot)) if tot[k] > GROUND_AREA}


def summary(d):
    g = ground_materials(d)
    keep = ~np.isin(d['tri_mat'], list(g)) if g else np.ones(len(d['tri_mat']), bool)
    used = set(d['tri_mat'][keep].tolist())
    texs = {d['materials'][k]['tex'] for k in used if d['materials'][k]['texture'] is not None}
    return {'triangles': int(keep.sum()), 'textures': len(texs), 'ground': len(g)}


def placed(d, rot=0, mirror=False, sx=None, sz=None, offset=(0.0, 0.0, 0.0), skip_ground=True, key=''):
    """The track's objects turned / mirrored like its heightmap (mxb_io.orient) and moved by offset (x, y, z):
    {'P', 'N', 'UV', 'I', 'tri_mat', 'd', 'key'} in Senders' world."""
    P = d['P'].astype(np.float64).copy(); N = d['N'].astype(np.float64).copy()
    I = d['I'].astype(np.int64).copy(); tm = d['tri_mat'].copy()
    if skip_ground:
        g = ground_materials(d)
        if g:
            keep = ~np.isin(tm, list(g)); I = I[keep]; tm = tm[keep]
    W, L = float(sx), float(sz)
    # same frame as the heightmap (mxb_io) and Senders' map - both games' worlds have the same handedness
    if mirror:
        P[:, 0] = W - P[:, 0]; N[:, 0] = -N[:, 0]
        I = I[:, [0, 2, 1]]                                   # mirrored: faces keep facing out
    for _ in range((int(rot) // 90) % 4):                     # clockwise on the map (z up the screen): (x, z) -> (z, W - x)
        x, z = P[:, 0].copy(), P[:, 2].copy()
        P[:, 0], P[:, 2] = z, W - x
        nx, nz = N[:, 0].copy(), N[:, 2].copy()
        N[:, 0], N[:, 2] = nz, -nx
        W, L = L, W
    P += np.asarray(offset, np.float64)
    return {'P': P, 'N': N, 'UV': d['UV'].astype(np.float64), 'I': I, 'tri_mat': tm, 'd': d, 'key': key}


# ---------------------------------------------------------------- texture coordinates: one tile per piece
def _clip(poly, axis, k, keep_below):
    """Sutherland-Hodgman: the part of a polygon (list of vertex arrays [x y z nx ny nz u v]) with
    uv[axis] <= k (keep_below) or >= k."""
    out = []
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        ia = (a[6 + axis] <= k) if keep_below else (a[6 + axis] >= k)
        ib = (b[6 + axis] <= k) if keep_below else (b[6 + axis] >= k)
        if ia: out.append(a)
        if ia != ib:
            t = (k - a[6 + axis]) / (b[6 + axis] - a[6 + axis])
            out.append(a + (b - a) * t)
    return out


def _split_tri(v, maxcells=256):
    """Pieces of one triangle (3 x 8 array) that each lie in one uv tile: [(cell u, cell v, polygon)]."""
    u0, u1 = math.floor(v[:, 6].min() + 1e-6), math.ceil(v[:, 6].max() - 1e-6)
    w0, w1 = math.floor(v[:, 7].min() + 1e-6), math.ceil(v[:, 7].max() - 1e-6)
    if (u1 - u0) * (w1 - w0) > maxcells: return None
    pieces = []
    rest = [r for r in v]
    for cu in range(u0, u1):
        col = _clip(rest, 0, cu + 1, True) if cu < u1 - 1 else rest
        rest = _clip(rest, 0, cu + 1, False) if cu < u1 - 1 else []
        rest2 = col
        for cv in range(w0, w1):
            cell = _clip(rest2, 1, cv + 1, True) if cv < w1 - 1 else rest2
            rest2 = _clip(rest2, 1, cv + 1, False) if cv < w1 - 1 else []
            if len(cell) >= 3: pieces.append((cu, cv, cell))
            if len(rest2) < 3: break
        if len(rest) < 3: break
    return pieces


def tiles(part):
    """Every triangle as corner arrays with uv inside one tile: (V (T,3,8) [pos, normal, local uv], tri material)."""
    P, N, UV, I, tm = part['P'], part['N'], part['UV'], part['I'], part['tri_mat']
    V = np.concatenate([P[I], N[I], UV[I]], axis=2)              # (T, 3, 8)
    u = V[:, :, 6]; w = V[:, :, 7]
    cu = np.floor(u.min(1) + 1e-6); cw = np.floor(w.min(1) + 1e-6)
    single = (u.max(1) - cu <= 1 + 1e-4) & (w.max(1) - cw <= 1 + 1e-4)
    out = V[single].copy()
    out[:, :, 6] -= cu[single, None]; out[:, :, 7] -= cw[single, None]
    mats = [tm[single]]
    extra, extra_m = [], []
    for t in np.nonzero(~single)[0]:
        pcs = _split_tri(V[t])
        if pcs is None:                                           # absurdly repeated: squeeze into one tile
            q = V[t].copy()
            for ax in (6, 7):
                lo, hi = q[:, ax].min(), q[:, ax].max()
                q[:, ax] = (q[:, ax] - lo) / max(hi - lo, 1e-9)
            extra.append(q); extra_m.append(tm[t]); continue
        for cu_, cv_, poly in pcs:
            poly = np.array(poly)
            poly[:, 6] -= cu_; poly[:, 7] -= cv_
            for j in range(1, len(poly) - 1):
                extra.append(np.stack([poly[0], poly[j], poly[j + 1]])); extra_m.append(tm[t])
    if extra:
        out = np.concatenate([out, np.array(extra)]); mats.append(np.array(extra_m, tm.dtype))
    out[:, :, 6:8] = np.clip(out[:, :, 6:8], 0.0, 1.0)
    return out, np.concatenate(mats)


# ---------------------------------------------------------------- the atlas
def _texture_list(parts):
    """Unique textures used: [(key, part d, texture dict or None, colour)] and per part a map material -> index."""
    entries, index, maps = [], {}, []
    for part in parts:
        d = part['d']; m = {}
        for k in sorted(set(part['tri_mat'].tolist())):
            mt = d['materials'][k]
            t = mt['texture']
            key = (part['key'], 'tex', mt['tex']) if t is not None else ('colour',) + tuple(round(c, 3) for c in mt['color'])
            if key not in index:
                index[key] = len(entries)
                entries.append((key, d, t, mt['color']))
            m[k] = index[key]
        maps.append(m)
    return entries, maps


def _pack(sizes, A):
    """Shelf packing of (w, h) boxes in an A x A square; [(x, y)] or None."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0]))
    pos = [None] * len(sizes)
    x = y = shelf = 0
    for i in order:
        w, h = sizes[i]
        if w > A or h > A: return None
        if x + w > A:
            y += shelf; x = 0; shelf = 0
        if y + h > A: return None
        pos[i] = (x, y); x += w; shelf = max(shelf, h)
    return pos


def build_atlas(entries, A=8192, progress=None):
    """(RGBA atlas (A, A, 4) uint8, top row first; rects [(x, y, w, h)] of each texture's own pixels)."""
    from PIL import Image
    base = []
    for key, d, t, col in entries:
        base.append((t['w'], t['h']) if t is not None else (8, 8))
    f = 1.0
    while True:
        sizes = []
        for (w, h), e in zip(base, entries):
            if e[2] is None:
                sizes.append((8 + 2 * PAD, 8 + 2 * PAD)); continue
            s = min(f, A / 2 / max(w, h))                         # nothing takes more than a quarter
            sizes.append((max(8, int(w * s)) + 2 * PAD, max(8, int(h * s)) + 2 * PAD))
        pos = _pack(sizes, A)
        if pos is not None: break
        f *= 0.85
        if f < 1 / 64: raise ValueError('too many textures for the atlas')
    atlas = np.zeros((A, A, 4), np.uint8)
    rects = []
    for i, ((key, d, t, col), (w, h), (x, y)) in enumerate(zip(entries, sizes, pos)):
        iw, ih = w - 2 * PAD, h - 2 * PAD
        if t is None:
            px = np.zeros((ih, iw, 4), np.uint8)
            px[..., :3] = np.clip(np.array(col[:3]) * 255, 0, 255).astype(np.uint8); px[..., 3] = 255
        else:
            raw = MO.texture(d['bytes'], t)[::-1]                 # stored bottom row first
            if raw.shape[1] != iw or raw.shape[0] != ih:
                px = np.asarray(Image.fromarray(np.ascontiguousarray(raw), 'RGBA').resize((iw, ih), Image.LANCZOS))
            else:
                px = raw
            raw = None
            if t['alpha'] == 0: px = px.copy(); px[..., 3] = 255   # opaque textures: no holes from stray alpha
        atlas[y:y + h, x:x + w] = np.pad(px, ((PAD, PAD), (PAD, PAD), (0, 0)), mode='wrap')
        rects.append((x + PAD, y + PAD, iw, ih))
        if progress: progress(i + 1, len(entries))
    return atlas, rects


# ---------------------------------------------------------------- Senders map nodes
def _node(spec, name, V, two_sided):
    """One map node (bytes) from corner arrays V (T, 3, 8): positions, normals, atlas uv."""
    if two_sided is not None and two_sided.any():
        back = V[two_sided][:, ::-1].copy(); back[:, :, 3:6] *= -1
        V = np.concatenate([V, back])
    flat = V.reshape(-1, 8)
    # share identical corners
    q = np.round(flat, 5)
    uniq, inv = np.unique(q, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    n = len(uniq); T = len(V)
    pos = np.zeros((n, 4), '<f4'); pos[:, :3] = uniq[:, 0:3]; pos[:, 3] = 1
    nrm = np.zeros((n, 4), '<f4'); nrm[:, :3] = uniq[:, 3:6]
    ln = np.linalg.norm(nrm[:, :3], axis=1, keepdims=True); nrm[:, :3] /= np.maximum(ln, 1e-9)
    uv = uniq[:, 6:8].astype('<f4')
    tri = inv.reshape(T, 3).astype('<u4')
    polys = np.empty(T, dtype=[('n', '<u8'), ('i', '<u4', (3,))]); polys['n'] = 3; polys['i'] = tri
    node = {'name': name, 'type': 'MESH', 'parent': None,
            'position': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0}, 'rotation': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0},
            'scale': {'x': 1.0, 'y': 1.0, 'z': 1.0, 'w': 0.0},
            'material': {'color': {'x': 1.0, 'y': 1.0, 'z': 1.0, 'w': 1.0}, 'roughness': 0.9, 'metallic': 0.0,
                         'diffuse_texture': TEXTURE_SLOT, 'normal_texture': None, 'roughness_metallic_texture': None,
                         'baked_material_texture': None},
            'mesh': {'polygons': zigspec.RawSlice(T, polys.tobytes()),
                     'positions': zigspec.RawSlice(n, pos.tobytes()),
                     'uvs': zigspec.RawSlice(n, uv.tobytes()),
                     'normals': zigspec.RawSlice(n, nrm.tobytes()),
                     'bone_indices': zigspec.RawSlice(n, np.full((n, 3), -1, 'i1').tobytes()),
                     'bone_weights': zigspec.RawSlice(n, np.zeros((n, 3), '<f4').tobytes()),
                     'vertex_groups': [], 'subdiv_levels': 0, 'costume': None, 'terrain': None},
            'linked_mesh': None, 'armature': None, 'curve': None}
    if not spec.has_field('Mesh', 'normals'): node['mesh'].pop('normals')
    out = []
    spec._enc(('ref', 'Node'), node, out, 'Node')
    return b''.join(out), n, T


def self_test(spec):
    """The packed mesh arrays must come out exactly as the spec's own encoder writes them."""
    V = np.array([[[0, 0, 0, 0, 1, 0, 0.1, 0.2], [1, 0, 0, 0, 1, 0, 0.3, 0.4], [0, 0, 1, 0, 1, 0, 0.5, 0.6]]], np.float64)
    raw, n, T = _node(spec, 'GME.test', V, None)
    pos = [{'x': float(a), 'y': float(b), 'z': float(c), 'w': 1.0} for a, b, c in V[0, :, :3]]
    plain = {'name': 'GME.test', 'type': 'MESH', 'parent': None,
             'position': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0}, 'rotation': {'x': 0.0, 'y': 0.0, 'z': 0.0, 'w': 1.0},
             'scale': {'x': 1.0, 'y': 1.0, 'z': 1.0, 'w': 0.0},
             'material': {'color': {'x': 1.0, 'y': 1.0, 'z': 1.0, 'w': 1.0}, 'roughness': 0.9, 'metallic': 0.0,
                          'diffuse_texture': TEXTURE_SLOT, 'normal_texture': None, 'roughness_metallic_texture': None,
                          'baked_material_texture': None}, 'linked_mesh': None, 'armature': None, 'curve': None}
    # the shared-corner order is np.unique's: rebuild it the same way
    flat = np.round(V.reshape(-1, 8), 5); uniq, inv = np.unique(flat, axis=0, return_inverse=True)
    plain['mesh'] = {'polygons': [list(map(int, inv.reshape(-1)))],
                     'positions': [{'x': float(r[0]), 'y': float(r[1]), 'z': float(r[2]), 'w': 1.0} for r in uniq],
                     'uvs': [{'x': float(r[6]), 'y': float(r[7])} for r in uniq],
                     'normals': [{'x': float(r[3]), 'y': float(r[4]), 'z': float(r[5]), 'w': 0.0} for r in uniq],
                     'bone_indices': [[-1, -1, -1]] * len(uniq), 'bone_weights': [[0.0, 0.0, 0.0]] * len(uniq),
                     'vertex_groups': [], 'subdiv_levels': 0, 'costume': None, 'terrain': None}
    if not spec.has_field('Mesh', 'normals'): plain['mesh'].pop('normals')
    out = []
    spec._enc(('ref', 'Node'), plain, out, 'Node')
    if b''.join(out) != raw:
        raise ValueError("this game version stores models differently - objects can't be written safely")


def build(parts, spec, A=8192, progress=None, log=None):
    """(atlas RGBA top-row-first, (node bytes, node count)) for placed parts."""
    self_test(spec)
    entries, maps = _texture_list(parts)
    atlas, rects = build_atlas(entries, A, progress=(lambda i, n: progress('textures', i, n)) if progress else None)
    blobs = []; count = 0; tv = tt = 0
    for pi, (part, m) in enumerate(zip(parts, maps)):
        V, mats = tiles(part)
        mats_l = mats.tolist()
        if not m: continue
        idx = np.array([m.get(k, 0) for k in range(max(m) + 1)])
        R = np.array(rects, np.float64)[idx[mats]]                 # (T, 4) x, y, w, h of each triangle's texture
        lu = V[:, :, 6]; lv = V[:, :, 7]
        V[:, :, 6] = (R[:, None, 0] + lu * R[:, None, 2]) / A
        V[:, :, 7] = (R[:, None, 1] + (1 - lv) * R[:, None, 3]) / A
        two = np.array([part['d']['materials'][k]['two_sided'] for k in mats_l], bool) if len(mats_l) else np.zeros(0, bool)
        # one node per ~60k triangles (keeps each model a sensible size)
        CH = 60000
        for s in range(0, len(V), CH):
            name = '%s%d.%d' % (NODE_PREFIX, pi + 1, s // CH + 1)
            b, n, T = _node(spec, name, V[s:s + CH], two[s:s + CH])
            blobs.append(b); count += 1; tv += n; tt += T
        if progress: progress('objects', pi + 1, len(parts))
    if log: log('INFO objects: %d models, %d vertices, %d triangles, %d textures' % (count, tv, tt, len(entries)))
    return atlas, (b''.join(blobs), count)


def save_png(path, atlas):
    from PIL import Image
    Image.fromarray(atlas, 'RGBA').save(path, format='PNG', compress_level=1)


# ---------------------------------------------------------------- into an existing model of the map
# The game only accepts the models its map was built with (it looks every model up by name and stops with
# "Types and nodes don't match" otherwise). So the objects go INTO one of those models: its own shape is
# kept (textured with a patch of its own colour in the atlas) and the objects are added to its mesh.
HOSTS = ['Cube', 'Ref.Ext.001', 'Base', 'Column.001']      # plain (unrotated) ones first


def host_candidates(tf, spec):
    """Names of the map's plain mesh models (no terrain) that can carry the objects, preferred first."""
    out = []
    for n in tf.nodes:
        nm = n.get('name', '')
        if not nm or nm.startswith('Heightmap') or 'end' not in n: continue
        if n['end'] - n['start'] > 50_000_000: continue
        try:
            nd = decode_node(tf, spec, n)
        except Exception:
            continue
        if nd.get('type') == 'MESH' and nd.get('mesh') and not nd['mesh'].get('terrain') and not nd['mesh'].get('costume'):
            out.append(nm)
    out.sort(key=lambda s: (HOSTS.index(s) if s in HOSTS else len(HOSTS), s))
    return out


def decode_node(tf, spec, n):
    spec._b = memoryview(tf.data); spec._p = n['start']
    return spec._dec(('ref', 'Node'))


def _qmat(q):
    x, y, z, w = q['x'], q['y'], q['z'], q['w']
    s = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / s, y / s, z / s, w / s
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _arr(lst, keys):
    return np.array([[d[k] for k in keys] for d in lst], np.float64) if lst else np.zeros((0, len(keys)))


def _pack_polys(polys):
    """[[i, j, k, ...]] -> spec bytes of []const []const u32."""
    out = bytearray()
    for p in polys:
        out += struct.pack('<Q', len(p)); out += np.asarray(p, '<u4').tobytes()
    return bytes(out)


def build_into(parts, spec, host, A=8192, progress=None, log=None):
    """(atlas, encoded host node) - the objects added to the map's model `host` (a decoded node)."""
    entries, maps = _texture_list(parts)
    mat = host.get('material') or {}
    col = mat.get('color') or {'x': 0.8, 'y': 0.8, 'z': 0.8, 'w': 1.0}
    entries.append((('host',), None, None, (col['x'], col['y'], col['z'])))      # the model's own colour
    atlas, rects = build_atlas(entries, A, progress=(lambda i, n: progress('textures', i, n)) if progress else None)
    me = host['mesh']
    has_n = spec.has_field('Mesh', 'normals')
    P0 = _arr(me['positions'], 'xyz'); U0 = _arr(me['uvs'], 'xy')
    N0 = _arr(me['normals'], 'xyz') if has_n else np.zeros_like(P0)
    hx, hy, hw, hh = rects[-1]
    U0[:] = [(hx + hw / 2) / A, (hy + hh / 2) / A]                                 # its own shape: its colour patch
    polys = [list(p) for p in me['polygons']]
    # the model's transform: world = pos + R (scale * local)
    R = _qmat(host['rotation']); pos = np.array([host['position'][k] for k in 'xyz'])
    sc = np.array([host['scale'][k] for k in 'xyz']); sc[np.abs(sc) < 1e-9] = 1.0
    Ps, Ns, Us, tris = [P0], [N0], [U0], []
    base = len(P0); tv = tt = 0
    for pi, (part, m) in enumerate(zip(parts, maps)):
        if not m: continue
        V, mats = tiles(part)
        idx = np.array([m.get(k, 0) for k in range(max(m) + 1)])
        Rr = np.array(rects, np.float64)[idx[mats]]
        lu = V[:, :, 6]; lv = V[:, :, 7]
        V[:, :, 6] = (Rr[:, None, 0] + lu * Rr[:, None, 2]) / A
        V[:, :, 7] = (Rr[:, None, 1] + (1 - lv) * Rr[:, None, 3]) / A
        two = np.array([part['d']['materials'][k]['two_sided'] for k in mats.tolist()], bool)
        if two.any():
            back = V[two][:, ::-1].copy(); back[:, :, 3:6] *= -1
            V = np.concatenate([V, back])
        flat = V.reshape(-1, 8)
        uniq, inv = np.unique(np.round(flat, 5), axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        loc = ((uniq[:, 0:3] - pos) @ R) / sc                                    # into the model's own space
        nl = (uniq[:, 3:6] @ R) * sc; nl /= np.maximum(np.linalg.norm(nl, axis=1, keepdims=True), 1e-9)
        Ps.append(loc); Ns.append(nl); Us.append(uniq[:, 6:8])
        tris.append(inv.reshape(-1, 3) + base); base += len(uniq)
        tv += len(uniq); tt += len(V)
        if progress: progress('objects', pi + 1, len(parts))
    P = np.concatenate(Ps); N = np.concatenate(Ns); U = np.concatenate(Us); n = len(P)
    pos4 = np.zeros((n, 4), '<f4'); pos4[:, :3] = P; pos4[:, 3] = 1
    nrm4 = np.zeros((n, 4), '<f4'); nrm4[:, :3] = N
    T = np.concatenate(tris) if tris else np.zeros((0, 3), np.int64)
    tri_rec = np.empty(len(T), dtype=[('n', '<u8'), ('i', '<u4', (3,))]); tri_rec['n'] = 3; tri_rec['i'] = T
    bi = np.full((n, 3), -1, 'i1'); bw = np.zeros((n, 3), '<f4')
    old_n = len(P0)
    if old_n:                                                                    # keep the model's own skinning
        bi[:old_n] = np.array(me['bone_indices'], 'i1').reshape(-1, 3)
        bw[:old_n] = np.array(me['bone_weights'], '<f4').reshape(-1, 3)
    node = dict(host)
    node['material'] = dict(mat or {'roughness': 1.0, 'metallic': 0.0, 'normal_texture': None,
                                    'roughness_metallic_texture': None, 'baked_material_texture': None})
    node['material']['color'] = {'x': 1.0, 'y': 1.0, 'z': 1.0, 'w': 1.0}
    node['material']['diffuse_texture'] = TEXTURE_SLOT
    node['material'].setdefault('roughness', 1.0); node['material'].setdefault('metallic', 0.0)
    for k in ('normal_texture', 'roughness_metallic_texture', 'baked_material_texture'): node['material'][k] = None
    mesh = dict(me)
    mesh['polygons'] = zigspec.RawSlice(len(polys) + len(T), _pack_polys(polys) + tri_rec.tobytes())
    mesh['positions'] = zigspec.RawSlice(n, pos4.tobytes())
    mesh['uvs'] = zigspec.RawSlice(n, U.astype('<f4').tobytes())
    if has_n: mesh['normals'] = zigspec.RawSlice(n, nrm4.tobytes())
    mesh['bone_indices'] = zigspec.RawSlice(n, bi.tobytes())
    mesh['bone_weights'] = zigspec.RawSlice(n, bw.tobytes())
    node['mesh'] = mesh
    out = []
    spec._enc(('ref', 'Node'), node, out, 'Node')
    data = b''.join(out)
    # read it back: it must decode as one model with everything in it
    import terrain_io
    ln = struct.unpack_from('<Q', data, 0)[0]
    if terrain_io.Walker(spec, data).skip(('ref', 'Node'), 0) != len(data) or data[8:8 + ln].decode() != host['name']:
        raise ValueError("the objects' model didn't read back correctly - not saved")
    if log: log('INFO objects: %d vertices, %d triangles, %d textures, inside the map model "%s"'
                % (tv, tt, len(entries) - 1, host['name']))
    return atlas, data

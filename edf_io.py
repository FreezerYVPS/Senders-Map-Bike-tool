"""
edf_io.py - read an MX Bikes / PiBoSo model (.edf) into plain parts (read-only).

File: "EDF\\0", then geometry blocks. Each block: bounds (6 f32), setup records (u32 n, n x 56 bytes), a vertex
pool (u32 n; positions 3f, uv 2f, uv2 2f, colour 4f, normal 3f, tangent 4f - each as a separate array),
triangles (u32 n, 3n u32), meshes (u32 n; name[100], flags, matrix 16f, bind matrix 16f, u32, child list,
draw ranges [setup, material, first tri, tri count, first vertex, vertex count], 2 u32, optional payloads,
bounds 6f) and - in the first block only - the material table (name, texture with raw-DEFLATE pixels, maps,
layers). A trailing "EXT" table names extra blocks (steer, fsusp, rsusp: the parts that steer / spring).
Matrices are local to the parent mesh (column vectors: translation in the last column). EDF axes are the same
as Senders': x to the side, y up, z forward, metres.

Only unprotected files can be read: a bike packed in an encrypted .pkz can't be (and isn't) opened.
"""
import hashlib
import os
import struct
import zlib

import numpy as np


class EDFError(ValueError):
    pass


class _R:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def need(self, n):
        if n < 0 or self.p + n > len(self.b): raise EDFError('the file ends early at 0x%x' % self.p)

    def take(self, n):
        self.need(n); v = self.b[self.p:self.p + n]; self.p += n; return v

    def u32(self):
        self.need(4); v = struct.unpack_from('<I', self.b, self.p)[0]; self.p += 4; return v

    def arr(self, dtype, n):
        size = np.dtype(dtype).itemsize * n
        self.need(size); a = np.frombuffer(self.b, dtype, n, self.p); self.p += size; return a

    def count(self, what, item, limit=100_000_000):
        n = self.u32()
        if n > limit or n * item > len(self.b) - self.p: raise EDFError('bad %s count %d at 0x%x' % (what, n, self.p - 4))
        return n


def _name(raw, fallback):
    s = bytes(raw).split(b'\0', 1)[0].decode('utf-8', 'replace').strip()
    return s or fallback


def _image(r, name, w, h, digest):
    n = r.u32()
    if n == 0: return None
    if n < 8 or n > len(r.b) - r.p: raise EDFError('texture %s: bad size' % name)
    fmt = r.u32(); r.u32()
    comp = r.take(n - 8)
    return {'name': name, 'fmt': fmt, 'w': w, 'h': h, 'digest': bytes(digest), 'data': comp}


def _named_texture(r, fallback, animated):
    name = _name(r.take(100), fallback)
    w, h = r.u32(), r.u32()
    digest = r.take(16)
    frames = [r.take(16) for _ in range(r.count('frames', 16, 100000))] if animated else []
    img = _image(r, name, w, h, digest)
    for i, fd in enumerate(frames): _image(r, '%s_%d' % (name, i), w, h, fd)
    return img


def _maps(r, owner):
    mode = r.u32()
    out = {}
    if mode == 0: return out
    if mode not in (1, 2): raise EDFError('material %s: unknown map mode %d' % (owner, mode))
    if r.u32():
        r.take(12)
        if r.u32(): out['reflection'] = _named_texture(r, owner + '_r', mode == 2)
        if r.u32(): out['environment'] = _named_texture(r, owner + '_env', False)
    if r.u32(): r.take(4)
    if r.u32(): out['specular'] = _named_texture(r, owner + '_s', mode == 2)
    if r.u32():
        r.take(4); out['normal'] = _named_texture(r, owner + '_n', True)
    if mode == 2:
        if r.u32(): r.take(12)
        r.u32()
    return out


def _material(r, i):
    name = _name(r.take(100), 'material%d' % i)
    r.u32()
    w, h = r.u32(), r.u32()
    digest = r.take(16)
    frames = [r.take(16) for _ in range(r.count('frames', 16, 100000))]
    base = _image(r, name, w, h, digest)
    for k, fd in enumerate(frames): _image(r, '%s_%d' % (name, k), w, h, fd)
    maps = _maps(r, name)
    for k in range(r.count('layers', 56, 100000)):                   # paint layers: skipped
        r.take(56); ln = _name(r.take(100), '%s_layer%d' % (name, k)); r.u32()
        lw, lh = r.u32(), r.u32(); ld = r.take(16)
        lf = [r.take(16) for _ in range(r.count('frames', 16, 100000))]
        _image(r, ln, lw, lh, ld)
        for q, fd in enumerate(lf): _image(r, ln, lw, lh, fd)
        _maps(r, ln)
    return {'name': name, 'base': base, 'maps': maps}


def _mesh(r, bi, mi):
    name = _name(r.take(100), 'block%d_mesh%d' % (bi, mi))
    flags = r.u32()
    matrix = r.arr('<f4', 16).reshape(4, 4).astype(np.float64)
    r.arr('<f4', 16)
    r.u32()
    children = r.arr('<u4', r.count('children', 4, 1_000_000)).tolist()
    draws = r.arr('<u4', 6 * r.count('draws', 24, 1_000_000)).reshape(-1, 6)
    post = r.arr('<u4', 2)
    if flags & 0x2000: r.take(int(post[0]) * 0x7C)
    if flags & 0x10000:
        for _ in range(r.count('morph tracks', 4, 1_000_000)):
            nf = r.count('morph frames', 4, 1_000_000)
            r.take(nf * int(post[0]) * 40 + nf * 4)
    if flags & ~(0x1000 | 0x2000 | 0x10000): raise EDFError('mesh %s uses unknown flags 0x%x' % (name, flags))
    r.arr('<f4', 6)
    return {'name': name, 'flags': flags, 'matrix': matrix, 'children': children, 'draws': draws}


def _block(r, bi, with_materials):
    start = r.p
    r.arr('<f4', 6)
    r.take(56 * r.count('setups', 56, 1_000_000))
    nv = r.count('vertices', 72)
    P = r.arr('<f4', nv * 3).reshape(nv, 3)
    UV = r.arr('<f4', nv * 2).reshape(nv, 2)
    r.arr('<f4', nv * 2); r.arr('<f4', nv * 4)
    N = r.arr('<f4', nv * 3).reshape(nv, 3)
    r.arr('<f4', nv * 4)
    nt = r.count('triangles', 12)
    I = r.arr('<u4', nt * 3).reshape(nt, 3)
    if nt and int(I.max()) >= nv: raise EDFError('block %d: triangle index out of range' % bi)
    meshes = [_mesh(r, bi, k) for k in range(r.count('meshes', 276, 1_000_000))]
    mats = [_material(r, k) for k in range(r.count('materials', 144, 1_000_000))] if with_materials else []
    return {'start': start, 'label': 'block%d' % bi, 'P': P, 'UV': UV, 'N': N, 'I': I, 'meshes': meshes, 'materials': mats}


def read(path):
    """{'blocks': [...], 'warnings': [...]} of an .edf file."""
    with open(path, 'rb') as f: b = f.read()
    if b[:4] != b'EDF\0':
        raise EDFError("%s isn't an MX Bikes model (.edf) - or it is protected" % os.path.basename(path))
    names = {}
    body = b
    if len(b) >= 8 and b[-8:-4] == b'EXT\0':
        size = struct.unpack_from('<I', b, len(b) - 4)[0]
        s = len(b) - size
        if 4 <= s < len(b) - 8:
            try:
                r = _R(b, s); r.u32()
                for k in range(r.count('names', 104, 1_000_000)):
                    nm = _name(r.take(100), 'ext%d' % k); names[r.u32()] = nm
                body = b[:s]
            except EDFError:
                names = {}
    r = _R(body, 4)
    blocks, warns = [], []
    while r.p < len(body):
        p0 = r.p
        try:
            blocks.append(_block(r, len(blocks), not blocks))
        except EDFError as e:
            if not blocks: raise
            warns.append('stopped at 0x%x: %s' % (p0, e)); break
        if r.p <= p0: raise EDFError('no progress')
    for bl in blocks:
        if bl['start'] in names: bl['label'] = names[bl['start']]
    return {'blocks': blocks, 'warnings': warns}


def texture_rgba(tex):
    """RGBA uint8 (rows top first as stored) of an EDF texture, or None."""
    if tex is None: return None
    try:
        raw = zlib.decompress(tex['data'], -15)
    except zlib.error:
        return None
    if hashlib.md5(raw).digest() != tex['digest']: return None
    w, h, n = tex['w'], tex['h'], tex['w'] * tex['h']
    if len(raw) == n * 4: a = np.frombuffer(raw, np.uint8).reshape(h, w, 4)
    elif len(raw) == n * 3: a = np.dstack([np.frombuffer(raw, np.uint8).reshape(h, w, 3), np.full((h, w), 255, np.uint8)])
    elif len(raw) == n: g = np.frombuffer(raw, np.uint8).reshape(h, w); a = np.dstack([g, g, g, np.full_like(g, 255)])
    else: return None
    return a


# ------------------------------------------------------------------ where the moving parts sit (MX Bikes)
# The steering, front-suspension and rear-suspension blocks are placed by the bike's physics; their resting
# spots come from the bike's .geom when it is there, else MX Bikes' standard attachment points.
DEFAULT_POINTS = {'chassis_steer': (0.0, 0.9912, 0.3372), 'chassis_rsusp_min': (0.0, 0.47, -0.17),
                  'chassis_rsusp_max': (0.0, 0.47, -0.17), 'front_upper': (0.0, -0.0818, 0.0213),
                  'front_lower': (0.0, 0.0779, 0.0212)}


def assembly_offsets(path, labels):
    v = dict(DEFAULT_POINTS); src = None
    folder = os.path.dirname(os.path.abspath(path))
    cands = [os.path.splitext(path)[0] + '.geom'] + sorted(os.path.join(folder, f) for f in os.listdir(folder)
                                                          if f.lower().endswith('.geom'))
    g = next((c for c in cands if os.path.isfile(c)), None)
    if g:
        for line in open(g, encoding='utf-8', errors='ignore'):
            k, sep, val = line.partition('=')
            k = k.strip().lower()
            if sep and k in v:
                try:
                    t = tuple(float(x) for x in val.split(',')[:3])
                    if len(t) == 3: v[k] = t
                except ValueError:
                    pass
        src = os.path.basename(g)
    steer = np.array(v['chassis_steer'])
    rsusp = (np.array(v['chassis_rsusp_min']) + np.array(v['chassis_rsusp_max'])) / 2
    fsusp = steer + np.array(v['front_upper']) - np.array(v['front_lower'])
    out = {}
    for lab in labels:
        l = lab.lower()
        for kind, off in (('steer', steer), ('fsusp', fsusp), ('rsusp', rsusp)):
            if l.startswith(kind): out[lab] = off
    return out, src


def parts(path, log=None):
    """The model as parts: [{'name', 'P' (n,3 metres, Senders axes), 'N', 'UV', 'F', 'mat'}] and the textures
    {material name: RGBA array}. Only the most detailed geometry (the first blocks with meshes) is used."""
    d = read(path)
    blocks = d['blocks']
    mats = blocks[0]['materials'] if blocks else []
    offs, src = assembly_offsets(path, [b['label'] for b in blocks])
    if log:
        for w in d['warnings']: log('WARN ' + w)
        if any(b['label'] != 'block%d' % i for i, b in enumerate(blocks)):
            log('INFO moving parts placed %s' % ('from ' + src if src else "at MX Bikes' standard attachment points"))
    out = []
    seen_main = False
    for bi, bl in enumerate(blocks):
        named = bl['label'] != 'block%d' % bi
        if not named:
            if seen_main: continue                       # further unnamed blocks: lower-detail copies (LODs)
            seen_main = True
        base_off = offs.get(bl['label'], np.zeros(3))
        meshes = bl['meshes']
        parent = {}
        for pi, m in enumerate(meshes):
            for c in m['children']:
                if c < len(meshes) and c != pi and c not in parent: parent[c] = pi

        def world(k, depth=0):                           # column vectors: v' = M v (translation in column 3)
            M = meshes[k]['matrix']
            if k in parent and depth < 64: return world(parent[k], depth + 1) @ M
            return M
        for k, m in enumerate(meshes):
            draws = m['draws']
            if not len(draws): continue
            M = world(k)
            for di, dr in enumerate(draws):
                _, mat, t0, tn = (int(x) for x in dr[:4])
                F = bl['I'][t0:t0 + tn]
                if not len(F): continue
                used, inv = np.unique(F, return_inverse=True)
                P = bl['P'][used].astype(np.float64)
                P = P @ M[:3, :3].T + M[:3, 3] + base_off
                N = bl['N'][used].astype(np.float64) @ M[:3, :3].T
                N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)
                out.append({'name': m['name'] if len(draws) == 1 else '%s.%d' % (m['name'], di),
                            'group': bl['label'] if named else 'chassis',
                            'P': P, 'N': N, 'UV': bl['UV'][used].astype(np.float64),
                            'F': inv.reshape(-1, 3).astype(np.int64),
                            'mat': mats[mat]['name'] if mat < len(mats) else None})
    tex = {}
    for m in mats:
        a = texture_rgba(m['base'])
        if a is not None: tex[m['name']] = a
    return out, tex

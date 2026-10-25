"""
mxb_objects.py - read the objects (fences, banners, tents, trees...) of an MX Bikes track, with their textures
(read-only).  Used by the map editor to bring a track's objects into Senders' map.

An MX Bikes track's <name>.map (format "MP2") holds, after its header:
    one record per material (56 bytes; offset 12 of each = 1 for two-sided foliage),
    the object geometry: u32 [count, 0, 0, vertices], positions (3 f32), uv (2 f32), 32 bytes, normals (3 f32),
        16 bytes per vertex, then u32 triangles and 3 u32 indices each,
    a bounding-volume tree whose leaves carry the draw batches (material, first triangle, triangles, first vertex,
        vertices),
    u32 material count, then per material: name (100 bytes), u32 alpha mode, u32 width, u32 height, 16 bytes,
        u32 0, u32 size, then `size` bytes (4 bytes, raw-deflate RGBA pixels, u32 "has normal map"), u32;
        with a normal map: 5 more u32/f32 and a second image (name, width, height, 16 bytes, u32 0, u32 size, data).
World units are metres: x across, y up, z along the track; the terrain (.trh) spans x 0..size_x, z 0..size_z and
its heights are 0..range (as mxb_io reads them), the same frame as the objects.
"""
import re
import struct
import zipfile
import zlib

import numpy as np


class NoObjects(Exception):
    pass


def _name(b, o):
    return bytes(b[o:o + 100]).split(b'\0')[0].decode('latin1')


def _read_map_bytes(path):
    """The .map file of a track (.pkz / folder / .map) as bytes."""
    import os, glob
    low = path.lower()
    if low.endswith('.map'):
        with open(path, 'rb') as f: return f.read()
    if os.path.isdir(path):
        maps = sorted(glob.glob(os.path.join(path, '*.map')) + glob.glob(os.path.join(path, '*', '*.map')))
        if not maps: raise NoObjects('no .map in this folder')
        with open(maps[0], 'rb') as f: return f.read()
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise NoObjects('the track is protected (encrypted) by its author')
    with z:
        names = [i for i in z.infolist() if i.filename.lower().endswith('.map')]
        if not names: raise NoObjects('no .map in %s' % os.path.basename(path))
        names.sort(key=lambda i: -i.file_size)
        return z.read(names[0])


def parse(b):
    """Geometry, per-triangle material and material / texture tables of an MP2 .map (bytes). Textures are not
    decoded here (see texture()); each carries the byte range of its compressed pixels."""
    if b[:4] != b'MP2\x00': raise NoObjects('not an MX Bikes map (MP2) file')
    nmat = struct.unpack_from('<I', b, 8)[0]
    if not 0 < nmat < 65536: raise NoObjects('unexpected material count %d' % nmat)
    mats = []
    for k in range(nmat):                      # 56 bytes each from offset 12
        r = struct.unpack_from('<I6f4I3I', b, 12 + 56 * k)
        mats.append({'two_sided': r[0] == 1, 'color': r[1:4], 'tex': r[11] - 1})     # texture index is 1-based
    o = 12 + 56 * nmat
    n = struct.unpack_from('<I', b, o)[0]; o += 4
    if not 0 < n < 50_000_000: raise NoObjects('no object geometry found')
    P = np.frombuffer(b, np.float32, n * 3, o).reshape(n, 3); o += n * 12
    UV = np.frombuffer(b, np.float32, n * 2, o).reshape(n, 2); o += n * 8
    o += n * 32
    N = np.frombuffer(b, np.float32, n * 3, o).reshape(n, 3); o += n * 12
    o += n * 16
    m = struct.unpack_from('<I', b, o)[0]; o += 4
    I = np.frombuffer(b, np.uint32, m * 3, o).reshape(m, 3); o += m * 12
    if m == 0 or int(I.max()) != n - 1: raise NoObjects('object geometry did not read correctly')
    tree = o
    need = max([mt['tex'] + 1 for mt in mats] + [0])
    if need == 0: raise NoObjects('the objects have no textures')
    texs, table = _textures(b, tree, need)
    tri_mat = _batches(b, tree, table, n, m, nmat, I)
    for mt in mats:
        t = texs[mt['tex']] if 0 <= mt['tex'] < len(texs) else None
        mt['texture'] = t
        mt['two_sided'] = mt['two_sided'] or (t is not None and t['alpha'] != 0)
    return {'P': P, 'UV': UV, 'N': N, 'I': I, 'tri_mat': tri_mat, 'materials': mats, 'textures': texs}


_NAME = re.compile(rb'[\x20-\x7e]{1,99}\x00')


def _main_entry(b, o):
    """A texture-table entry at o (name, alpha, width, height, 16 bytes, 0, size, 0, data), or None."""
    nm = bytes(b[o:o + 100])
    name = nm.split(b'\0')[0]
    if not name or any(nm[len(name):]) or not all(32 <= c < 127 for c in name): return None
    alpha, w, h = struct.unpack_from('<3I', b, o + 100)
    z1, size, z2 = struct.unpack_from('<3I', b, o + 128)
    if alpha > 7 or not (0 < w <= 16384 and 0 < h <= 16384) or z1 or z2 or not 8 < size < len(b) - o: return None
    try:
        zlib.decompressobj(-15).decompress(bytes(b[o + 144:o + 144 + min(size - 4, 4096)]), 64)
    except zlib.error:
        return None
    return {'name': name.decode('latin1'), 'alpha': alpha, 'w': w, 'h': h, 'data': (o + 144, o + 140 + size),
            'next': o + 140 + size}


def _textures(b, start, count):
    """The texture table after the geometry: u32 count, then entries (each may carry extra maps - normal,
    reflection - which are skipped by searching for the next entry)."""
    o = start
    while True:
        o = b.find(b'\x00' * 20, o)
        if o < 0: raise NoObjects('could not find the texture table')
        # back up to the start of the name in front of the zeros
        s = o
        while s > start and 32 <= b[s - 1] < 127 and o - s < 99: s -= 1
        if s < o:
            e = _main_entry(b, s)
            if e is not None: break
        o += 20
    table = s - 4
    out = [e]
    o = e['next']
    while len(out) < count:
        k = b.find(b'\x00' * 8, o)
        found = None
        while k >= 0 and k - o < 400_000_000:
            s = k
            while s > o and 32 <= b[s - 1] < 127 and k - s < 99: s -= 1
            if s < k:
                found = _main_entry(b, s)
                if found is not None: break
            k = b.find(b'\x00' * 8, k + 8)
        if found is None: break
        out.append(found); o = found['next']
    return out, table


def _batches(b, start, end, n, m, nmat, I):
    """Material of every triangle, from the draw batches (material, first tri, tris, first vertex, vertices)."""
    W = np.frombuffer(b, np.uint32, (end - start) // 4, start)
    flat = I.ravel()
    tri_mat = np.full(m, -1, np.int32)
    for k in range(len(W) - 4):
        mat, ts, tc, vs, vc = (int(x) for x in W[k:k + 5])
        if mat >= nmat or tc == 0 or vc == 0 or ts + tc > m or vs + vc > n: continue
        seg = flat[3 * ts:3 * (ts + tc)]
        if int(seg.min()) == vs and int(seg.max()) == vs + vc - 1:
            tri_mat[ts:ts + tc] = mat
    if (tri_mat < 0).mean() > 0.02: raise NoObjects('could not tell which texture goes where')
    tri_mat[tri_mat < 0] = 0
    return tri_mat


def texture(b, mt):
    """RGBA pixels (h, w, 4) of a material."""
    a, e = mt['data']
    raw = zlib.decompressobj(-15).decompress(bytes(b[a:e]), mt['w'] * mt['h'] * 4)
    if len(raw) != mt['w'] * mt['h'] * 4: raise NoObjects('texture %s did not unpack' % mt['name'])
    return np.frombuffer(raw, np.uint8).reshape(mt['h'], mt['w'], 4)


def load(path):
    """{'P', 'UV', 'N', 'I', 'tri_mat', 'materials', 'bytes'} of a track's objects."""
    b = _read_map_bytes(path)
    d = parse(b)
    d['bytes'] = b
    return d

"""
terrain_io.py - read and write the game's terrain inside map.blend.bin, using the BlendMeshSpec.zig spec.

The map file is the same format as the bike (header line + nodes), but one or more mesh nodes carry a
`terrain` (Terrain struct): a base heightmap, local height clips, two biome maps and a material map.
The file is ~700 MB, so nodes are walked with a fast skipper and only the terrain is loaded (as numpy).
Writing splices a newly encoded terrain into a copy of the file; every other byte stays identical.

    tf = TerrainFile.open('map.blend.bin', spec)
    t = tf.terrains[0]          # Terrain dict with numpy maps
    tf.save('new_map.blend.bin', {t['node']: t})
"""
import struct

import numpy as np

import zigspec


class Walker:
    """Byte-offset walker over spec types, skipping bulky data without building Python objects."""

    def __init__(self, spec, data):
        self.s, self.b = spec, data
        self._size = {}

    def fixed_size(self, t):
        """Byte size of a fixed-size type, or None."""
        key = repr(t)
        if key in self._size: return self._size[key]
        s = self.s
        r = s.resolve(t)
        size = None
        if r[0] == 'ref':
            size = 1 if r[1] == 'bool' else (int(zigspec._PRIM.match(r[1]).group(2)) + 7) // 8
        elif r[0] == 'enum':
            size = (int(zigspec._PRIM.match(r[1]).group(2)) + 7) // 8
        elif r[0] == 'struct' and r[1]:
            size = (s.bits(r) + 7) // 8
        elif r[0] == 'array':
            sub = self.fixed_size(r[2]); size = None if sub is None else sub * r[1]
        elif r[0] == 'struct':
            subs = [self.fixed_size(f[1]) for f in r[2]]
            size = None if any(x is None for x in subs) else sum(subs)
        self._size[key] = size
        return size

    def skip(self, t, p):
        """Offset just past a value of type t starting at p."""
        fs = self.fixed_size(t)
        if fs is not None: return p + fs
        s, b = self.s, self.b
        r = s.resolve(t)
        if r[0] == 'opt':
            tag = b[p]
            if tag == 0: return p + 1
            if tag != 1: raise zigspec.SpecError('bad optional tag %d at 0x%x' % (tag, p))
            return self.skip(r[1], p + 1)
        if r[0] == 'slice':
            n = struct.unpack_from('<Q', b, p)[0]; p += 8
            es = self.fixed_size(r[1])
            if es is not None: return p + n * es
            er = s.resolve(r[1])
            if er[0] == 'slice' and self.fixed_size(er[1]) is not None:      # [][]T: e.g. polygons
                ies = self.fixed_size(er[1])
                unpack = struct.unpack_from
                for _ in range(n):
                    p += 8 + unpack('<Q', b, p)[0] * ies
                return p
            for _ in range(n): p = self.skip(r[1], p)
            return p
        if r[0] == 'array':
            for _ in range(r[1]): p = self.skip(r[2], p)
            return p
        if r[0] == 'struct':
            for f in r[2]: p = self.skip(f[1], p)
            return p
        raise zigspec.SpecError('cannot skip %r' % (r,))

    def field_offsets(self, struct_name, p):
        """{field: (start, end)} of a (non-packed) struct starting at p."""
        r = self.s.resolve(('ref', struct_name))
        out = {}
        for f in r[2]:
            e = self.skip(f[1], p); out[f[0]] = (p, e, f[1]); p = e
        return out, p


def _map2d(b, p, dtype):
    n = struct.unpack_from('<Q', b, p)[0]; p += 8
    a = np.frombuffer(b, dtype, n, p).copy(); p += n * np.dtype(dtype).itemsize
    rx, ry, px, py = struct.unpack_from('<4i', b, p); p += 16
    return {'map': a, 'resolution': (rx, ry), 'padding': (px, py)}, p


def _bounds(b, p):
    v = struct.unpack_from('<8f', b, p)
    return {'min': v[0:4], 'max': v[4:8]}, p + 32


BLEND = np.dtype([('v', '<u2')])      # MaterialBlend packed: first:u4 | second:u4 | blend:u8 (LSB first)


def decode_terrain(b, p):
    t = {}
    t['bounds'], p = _bounds(b, p)
    t['base_height'], p = _map2d(b, p, '<f2')
    n = struct.unpack_from('<Q', b, p)[0]; p += 8
    clips = []
    for _ in range(n):
        c = {}
        c['bounds'], p = _bounds(b, p)
        c['height'], p = _map2d(b, p, '<f2')
        clips.append(c)
    t['height_clips'] = clips
    t['biome_height'] = []
    for _ in range(2):
        m, p = _map2d(b, p, 'u1'); t['biome_height'].append(m)
    m, p = _map2d(b, p, '<u2')
    m['first'] = (m['map'] & 0xF).astype(np.uint8)
    m['second'] = ((m['map'] >> 4) & 0xF).astype(np.uint8)
    m['blend'] = (m['map'] >> 8).astype(np.uint8)
    t['material_weights'] = m
    return t, p


def encode_terrain(t, f=None):
    """Encode a terrain; writes to file object f (streaming, low memory) or returns bytes."""
    import io
    out = f if f is not None else io.BytesIO()
    w = out.write
    def bounds(bd): w(struct.pack('<8f', *bd['min'], *bd['max']))
    def map2d(m, dtype):
        a = np.ascontiguousarray(m['map'], dtype=dtype)
        w(struct.pack('<Q', a.size)); w(memoryview(a).cast('B'))
        w(struct.pack('<4i', *m['resolution'], *m['padding']))
    bounds(t['bounds'])
    map2d(t['base_height'], '<f2')
    w(struct.pack('<Q', len(t['height_clips'])))
    for c in t['height_clips']:
        bounds(c['bounds']); map2d(c['height'], '<f2')
    for m in t['biome_height']: map2d(m, 'u1')
    mw = t['material_weights']
    if 'first' in mw:
        mw = dict(mw, map=(mw['first'].astype(np.uint16) & 0xF) | ((mw['second'].astype(np.uint16) & 0xF) << 4)
                  | (mw['blend'].astype(np.uint16) << 8))
    map2d(mw, '<u2')
    return None if f is not None else out.getvalue()


MATERIALS = ['sand', 'gravel', 'dirt', 'rock', 'highway', 'path', 'forest_floor', 'grass']


def shown_layer(first, second, blend):
    """The layer the game shows for cells, decoded as the game does: weight[first] = 255 - blend, then
    weight[second] = blend (overwriting it when first == second); all-zero weights show layer 0, sand."""
    first, second, blend = (np.asarray(a).astype(np.int64) for a in (first, second, blend))
    dom = np.where(255 - blend >= blend, first, second)
    return np.where(first == second, np.where(blend > 0, first, 0), dom)


class TerrainFile:
    """A map .blend.bin opened for terrain access (the file itself is never modified)."""

    def __init__(self, path, spec, data, nodes, terrains):
        self.path, self.spec, self.data, self.nodes, self.terrains = path, spec, data, nodes, terrains

    @classmethod
    def open(cls, path, spec, progress=None):
        import mmap
        f = open(path, 'rb')                                   # mapped read-only: the file is never changed
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        if data[:len(spec.header)] != spec.header:
            raise zigspec.SpecError('this map file is a different version than the spec')
        w = Walker(spec, data)
        root = spec.resolve(('ref', spec.root))
        p = len(spec.header)
        nodes, terrains = [], []
        for fname, ftype, _, _ in root[2]:
            fr = spec.resolve(ftype)
            if fname == 'nodes' and fr[0] == 'slice':
                count_at = p
                n = struct.unpack_from('<Q', data, p)[0]; p += 8
                node_t = spec.resolve(fr[1])
                for i in range(n):
                    start = p
                    info = {'index': i, 'start': start}
                    for nf, nt, _, _ in node_t[2]:
                        if nf == 'name':
                            ln = struct.unpack_from('<Q', data, p)[0]
                            info['name'] = bytes(data[p + 8:p + 8 + ln]).decode('utf-8', 'replace')
                        if nf == 'mesh':
                            if data[p] == 1:
                                q = p + 1
                                mesh_t = spec.resolve(spec.resolve(nt)[1])
                                for mf, mt, _, _ in mesh_t[2]:
                                    if mf == 'terrain':
                                        if data[q] == 1:
                                            t, end = decode_terrain(data, q + 1)
                                            t.update(node=info.get('name'), span=(q + 1, end))
                                            terrains.append(t)
                                        q = w.skip(mt, q)
                                    else:
                                        q = w.skip(mt, q)
                                p = q
                                continue
                        p = w.skip(nt, p)
                    info['end'] = p
                    nodes.append(info)
                    if progress: progress(i + 1, n)
            else:
                p = w.skip(ftype, p)
        if p != len(data):
            raise zigspec.SpecError('%d bytes left over: map file does not match the spec' % (len(data) - p))
        tf = cls(path, spec, data, nodes, terrains)
        tf._file = f
        tf.count_at = locals().get('count_at')
        tf.nodes_end = p if tf.count_at is not None and root[2][-1][0] == 'nodes' else None
        return tf

    TOOL_PREFIX = 'GME.'          # nodes this tool adds to the map (objects); replaced on every save

    def save(self, out_path, changed, progress=None, extra_nodes=None, replace_nodes=None):
        """Write a copy of the map with the given terrains ({node name: terrain}) replaced (streamed).
        extra_nodes: encoded nodes (bytes, count) to add; nodes added by an earlier save (named GME.*) are
        dropped first, so saving again replaces them."""
        cuts = [((t['span']), ('terrain', t['node'])) for t in self.terrains if t['node'] in changed]
        old = [n for n in self.nodes if n.get('name', '').startswith(self.TOOL_PREFIX) and 'end' in n]
        if (extra_nodes or old) and self.nodes_end is None:
            raise ValueError("this map file's layout doesn't allow adding objects")
        cuts += [((n['start'], n['end']), ('drop', None)) for n in old]
        rep = replace_nodes or {}
        cuts += [((n['start'], n['end']), ('replace', rep[n['name']])) for n in self.nodes
                 if n.get('name') in rep and 'end' in n]
        cuts.sort()
        last = 0
        CH = 64 << 20
        with open(out_path, 'wb') as f:
            def copy(a, b):
                for s in range(a, b, CH):
                    f.write(self.data[s:min(b, s + CH)])
                    if progress: progress(min(b, s + CH), len(self.data))
            for (a, b), (kind, node) in cuts:
                copy(last, a)
                if kind == 'terrain': encode_terrain(changed[node], f)
                elif kind == 'replace': f.write(node)
                last = b
            copy(last, len(self.data))
            if extra_nodes or old:
                add = extra_nodes[1] if extra_nodes else 0
                if extra_nodes: f.write(extra_nodes[0])
                n0 = struct.unpack_from('<Q', self.data, self.count_at)[0]
                f.seek(self.count_at); f.write(struct.pack('<Q', n0 - len(old) + add))

    def close(self):
        """Release the map file (needed on Windows before the file can be replaced)."""
        for t in self.terrains:                                 # nothing may still point into the mapping
            pass
        try: self.data.close()
        except Exception: pass
        try: self._file.close()
        except Exception: pass


# ======================================================================= editing
class Grid:
    """World <-> grid helpers for a terrain (x = columns, z = rows, cell = (max-min)/(res-1))."""

    def __init__(self, t):
        self.t = t
        B = t['bounds']
        self.x0, self.z0, self.x1, self.z1 = B['min'][0], B['min'][2], B['max'][0], B['max'][2]
        self.rx, self.ry = t['base_height']['resolution']
        self.dx = (self.x1 - self.x0) / (self.rx - 1); self.dz = (self.z1 - self.z0) / (self.ry - 1)
        self.base = t['base_height']['map'].reshape(self.ry, self.rx)

    def clip_grid(self, c):
        cb = c['bounds']; rx, ry = c['height']['resolution']
        return (cb['min'][0], cb['min'][2], (cb['max'][0] - cb['min'][0]) / (rx - 1),
                (cb['max'][2] - cb['min'][2]) / (ry - 1), c['height']['map'].reshape(ry, rx))

    @staticmethod
    def _bilinear(A, fx, fz):
        ry, rx = A.shape
        fx = np.clip(fx, 0, rx - 1.001); fz = np.clip(fz, 0, ry - 1.001)
        ix = fx.astype(np.int64); iz = fz.astype(np.int64); tx = fx - ix; tz = fz - iz
        a = A[iz, ix].astype(np.float32); b_ = A[iz, ix + 1].astype(np.float32)
        c = A[iz + 1, ix].astype(np.float32); d = A[iz + 1, ix + 1].astype(np.float32)
        return (a * (1 - tx) + b_ * tx) * (1 - tz) + (c * (1 - tx) + d * tx) * tz

    def height(self, X, Z):
        """Current terrain height at world points (detail clips where they exist, else the base map)."""
        h = self._bilinear(self.base, (X - self.x0) / self.dx, (Z - self.z0) / self.dz)
        for c in self.t['height_clips']:
            cx0, cz0, cdx, cdz, A = self.clip_grid(c)
            ry, rx = A.shape
            fx = (X - cx0) / cdx; fz = (Z - cz0) / cdz
            inside = (fx >= 0) & (fx <= rx - 1) & (fz >= 0) & (fz <= ry - 1)
            if inside.any(): h = np.where(inside, self._bilinear(A, fx, fz), h)
        return h


def _smooth(x):
    x = np.clip(x, 0.0, 1.0); return x * x * (3 - 2 * x)


class Placement:
    """A heightmap image laid on the terrain: centre (cx, cz), size (width along its x, length along its z),
    rotation (degrees, clockwise seen from above), heights = base_y + image * height_range, edge blend (m)."""

    def __init__(self, img, cx, cz, width, length, rot=0.0, height_range=10.0, base_y=0.0, edge=10.0,
                 material='gravel', clear_biomes=True, clip_res=0.125, paint_changed=False, surface=None):
        self.img = np.asarray(img, np.float32)            # rows = along length (z), cols = along width (x), 0..1
        self.cx, self.cz, self.width, self.length, self.rot = cx, cz, width, length, rot
        self.height_range, self.base_y, self.edge = height_range, base_y, edge
        self.material, self.clear_biomes, self.clip_res = material, clear_biomes, clip_res
        self.paint_changed = paint_changed               # surface only where the heights were changed
        # optional per-pixel surface map (same size as img): the game's layer index, -1 = leave the ground's
        self.surface = None if surface is None else np.asarray(surface, np.int8)
        # optional per-pixel tree height (canopy) / boulder maps 0..255, -1 = leave the game's (after clearing)
        self.canopy = None
        self.rocks = None

    def sample_map(self, A, X, Z):
        lx, lz = self.local(X, Z)
        H, W = A.shape
        c = np.clip(np.round((lx / self.width + 0.5) * (W - 1)), 0, W - 1).astype(np.int64)
        r = np.clip(np.round((lz / self.length + 0.5) * (H - 1)), 0, H - 1).astype(np.int64)
        return A[r, c]

    def sample_surface(self, X, Z):
        return self.sample_map(self.surface, X, Z)

    def corners(self):
        c, s = np.cos(np.radians(self.rot)), np.sin(np.radians(self.rot))
        out = []
        for u, v in ((-.5, -.5), (.5, -.5), (.5, .5), (-.5, .5)):
            lx, lz = u * self.width, v * self.length
            out.append((self.cx + lx * c - lz * s, self.cz + lx * s + lz * c))
        return out

    def local(self, X, Z):
        c, s = np.cos(np.radians(self.rot)), np.sin(np.radians(self.rot))
        dx, dz = X - self.cx, Z - self.cz
        return dx * c + dz * s, -dx * s + dz * c             # metres along width / length

    def sample(self, X, Z):
        """(new height, weight 0..1) at world points."""
        lx, lz = self.local(X, Z)
        u = lx / self.width + 0.5; v = lz / self.length + 0.5
        H, W = self.img.shape
        val = Grid._bilinear(self.img, u * (W - 1), v * (H - 1))
        d = np.minimum(np.minimum(lx + self.width / 2, self.width / 2 - lx),
                       np.minimum(lz + self.length / 2, self.length / 2 - lz))          # metres inside the edge
        w = _smooth(d / self.edge) if self.edge > 0 else (d >= 0).astype(np.float32)
        w = np.where(d < 0, 0.0, w)
        return self.base_y + val * self.height_range, w

    def bbox(self):
        cs = np.array(self.corners())
        return cs[:, 0].min(), cs[:, 1].min(), cs[:, 0].max(), cs[:, 1].max()


def ground_level(t, pl, n=64):
    """Median terrain height under a placement (for 'sit on the ground')."""
    g = Grid(t)
    u = np.linspace(-.45, .45, n)
    U, V = np.meshgrid(u * pl.width, u * pl.length)
    c, s = np.cos(np.radians(pl.rot)), np.sin(np.radians(pl.rot))
    X = pl.cx + U * c - V * s; Z = pl.cz + U * s + V * c
    h = g.height(X, Z)
    h = h[h > -999]
    return float(np.median(h)) if h.size else 0.0


def world_range(t):
    """(lowest, highest) height of the game's map - its own black -> white heightmap range."""
    b = t['bounds']
    return float(b['min'][1]), float(b['max'][1])


def ground_heights(t, pl, nx, ny, rows=256):
    """The terrain's current heights under a placement as an ny x nx array (rows along its length, like the
    image), void (outside the land) as NaN."""
    g = Grid(t)
    c, s = np.cos(np.radians(pl.rot)), np.sin(np.radians(pl.rot))
    lx = (np.arange(nx) / max(nx - 1, 1) - 0.5) * pl.width
    out = np.empty((ny, nx), np.float32)
    for r0 in range(0, ny, rows):
        r1 = min(ny, r0 + rows)
        lz = (np.arange(r0, r1) / max(ny - 1, 1) - 0.5) * pl.length
        U, V = np.meshgrid(lx, lz)
        h = g.height(pl.cx + U * c - V * s, pl.cz + U * s + V * c)
        out[r0:r1] = np.where(h > -999, h, np.nan)
    return out


def mixed_cells(pl, X, Z, g, w, old, f0, s0, b0, k=3):
    """Where a game cell (0.5 m) holds several painted layers (speckles, sparse grass...), give it the two
    most common ones and blend them by how much of the cell each covers - the game's own two-layer blend."""
    S = pl.surface
    if S is None: return
    cell = min(g.dx, g.dz)
    if cell <= max(pl.width / max(S.shape[1] - 1, 1), pl.length / max(S.shape[0] - 1, 1)) * 1.2: return  # not finer
    offs = (np.arange(k) + 0.5) / k - 0.5
    counts = np.zeros((len(MATERIALS),) + X.shape, np.int16)
    any_set = np.zeros(X.shape, bool)
    for ox in offs:
        for oz in offs:
            v = pl.sample_surface(X + ox * g.dx, Z + oz * g.dz).astype(np.int64)
            any_set |= v >= 0
            v = np.where(v >= 0, v, old)
            for m_ in range(len(MATERIALS)): counts[m_] += (v == m_)
    total = k * k
    top = np.argsort(-counts, axis=0, kind='stable')
    a = top[0]; b = top[1]
    ca = np.take_along_axis(counts, a[None], 0)[0]; cb = np.take_along_axis(counts, b[None], 0)[0]
    mix = any_set & (w >= 0.999) & (cb > 0)
    if not mix.any(): return
    f0[mix] = a[mix]; s0[mix] = b[mix]
    b0[mix] = np.round(255.0 * cb[mix] / np.maximum(ca[mix] + cb[mix], 1)).astype(np.uint8)


def apply_placement(t, pl, log=print):
    """Edit terrain t in place: base heights, a new detail clip, dirt surface, cleared rocks/trees."""
    g = Grid(t)
    bx0, bz0, bx1, bz1 = pl.bbox()
    if getattr(pl, 'allow_outside', False):            # work areas: just leave out what lies past the map
        bx0, bz0, bx1, bz1 = max(bx0, g.x0), max(bz0, g.z0), min(bx1, g.x1), min(bz1, g.z1)
        if bx0 >= bx1 or bz0 >= bz1: return
    if bx0 < g.x0 or bz0 < g.z0 or bx1 > g.x1 or bz1 > g.z1:
        raise ValueError('the track goes outside the map (%.0f..%.0f, %.0f..%.0f)' % (g.x0, g.x1, g.z0, g.z1))
    # ---- old heights for the detail clip (taken before the base map changes)
    res = pl.clip_res
    nx = int(np.ceil((bx1 - bx0) / res)) + 1; nz = int(np.ceil((bz1 - bz0) / res)) + 1
    cx1, cz1 = bx0 + (nx - 1) * res, bz0 + (nz - 1) * res
    clip_h = np.empty((nz, nx), np.float32)
    void = 0
    for r0 in range(0, nz, 512):                       # in strips (bounded memory)
        r1 = min(nz, r0 + 512)
        Z, X = np.meshgrid(bz0 + np.arange(r0, r1) * res, bx0 + np.arange(nx) * res, indexing='ij')
        old = g.height(X, Z); new, w = pl.sample(X, Z)
        void += int(((old <= -999) & (w > 0)).sum())
        clip_h[r0:r1] = np.where(w > 0, old * (1 - w) + new * w, old)
    if void: log('WARN part of the track is over the void (no ground) at the map edge')
    # ---- existing detail clips that overlap: blend the track into them too
    for c in t['height_clips']:
        ox0, oz0, odx, odz, A = g.clip_grid(c)
        ry, rx = A.shape
        ix0 = max(0, int((bx0 - ox0) / odx)); ix1 = min(rx, int((bx1 - ox0) / odx) + 2)
        iz0 = max(0, int((bz0 - oz0) / odz)); iz1 = min(ry, int((bz1 - oz0) / odz) + 2)
        if ix0 >= ix1 or iz0 >= iz1: continue
        Z, X = np.meshgrid(oz0 + np.arange(iz0, iz1) * odz, ox0 + np.arange(ix0, ix1) * odx, indexing='ij')
        new, w = pl.sample(X, Z)
        sub = A[iz0:iz1, ix0:ix1].astype(np.float32)
        A[iz0:iz1, ix0:ix1] = np.where(w > 0, sub * (1 - w) + new * w, sub).astype(np.float16)
        log('INFO the track overlaps one of the game\'s tracks - blended into it')
    # ---- base map, surface and biomes
    ix0 = max(0, int((bx0 - g.x0) / g.dx)); ix1 = min(g.rx, int((bx1 - g.x0) / g.dx) + 2)
    iz0 = max(0, int((bz0 - g.z0) / g.dz)); iz1 = min(g.ry, int((bz1 - g.z0) / g.dz) + 2)
    Z, X = np.meshgrid(g.z0 + np.arange(iz0, iz1) * g.dz, g.x0 + np.arange(ix0, ix1) * g.dx, indexing='ij')
    new, w = pl.sample(X, Z)
    sub = g.base[iz0:iz1, ix0:ix1].astype(np.float32)
    g.base[iz0:iz1, ix0:ix1] = np.where(w > 0, sub * (1 - w) + new * w, sub).astype(np.float16)
    if getattr(pl, 'paint_changed', False):
        # an edited copy of the game's own ground: paint / clear only where the shape was changed
        ch = (np.abs(new - sub) > 0.05) & (w > 0)
        for _ in range(4):                               # grow ~2 m so the edge of the change is covered too
            d = ch.copy()
            d[1:] |= ch[:-1]; d[:-1] |= ch[1:]; d[:, 1:] |= ch[:, :-1]; d[:, :-1] |= ch[:, 1:]
            ch = d
        w = np.where(ch, w, 0.0)
    mw = t['material_weights']
    F = mw['first'].reshape(g.ry, g.rx); S = mw['second'].reshape(g.ry, g.rx); Bl = mw['blend'].reshape(g.ry, g.rx)
    surf_map = getattr(pl, 'surface', None)
    if surf_map is not None:
        mi = pl.sample_surface(X, Z).astype(np.int64)    # -1 = leave the game's surface
        changed = np.abs(new - sub) > 0.05
        clear_w = np.where((mi >= 0) | changed, w, 0.0)
        if getattr(pl, 'clear_all', False): clear_w = w     # a placed track: clear its whole footprint
    else:
        mi = None if pl.material is None else MATERIALS.index(pl.material)
        clear_w = w
    if mi is not None:
        f0, s0, b0 = F[iz0:iz1, ix0:ix1], S[iz0:iz1, ix0:ix1], Bl[iz0:iz1, ix0:ix1]
        # The game weighs each cell as  w[first] = 255 - blend,  then  w[second] = blend  (in that order).
        # first == second therefore leaves every weight at 0 and the game shows layer 0 (sand) - so the
        # second layer must always differ.  blend runs 0..255 (255 = all second).
        cov = (w > 0) & (np.asarray(mi) >= 0)
        mi = np.broadcast_to(np.maximum(np.asarray(mi), 0), w.shape)
        old = np.where(b0 < 128, f0, s0)                 # what the cell shows now
        spare = np.where(s0 != mi, s0, (mi + 1) % len(MATERIALS)).astype(np.uint8)
        sec = np.where(old != mi, old, spare).astype(np.uint8)
        bl = np.where(old != mi, np.round((1 - w) * 255), 0).astype(np.uint8)
        bl[w >= 0.999] = 0
        f0[cov] = mi[cov]; s0[cov] = sec[cov]; b0[cov] = bl[cov]
        if surf_map is not None:
            mixed_cells(pl, X, Z, g, w, old, f0, s0, b0)
    if pl.clear_biomes:
        for m in t['biome_height']:
            A = m['map'].reshape(g.ry, g.rx); sub = A[iz0:iz1, ix0:ix1].astype(np.float32)
            A[iz0:iz1, ix0:ix1] = np.round(sub * (1 - clear_w)).astype(np.uint8)
    # painted trees / boulders (after clearing): biome map 0 = boulders, 1 = canopy (how tall the woods grow)
    for k, arr in ((0, getattr(pl, 'rocks', None)), (1, getattr(pl, 'canopy', None))):
        if arr is None: continue
        v = pl.sample_map(arr, X, Z).astype(np.int64)
        m = (v >= 0) & (w > 0.5)
        if not m.any(): continue
        A = t['biome_height'][k]['map'].reshape(g.ry, g.rx)
        sub = A[iz0:iz1, ix0:ix1]; sub[m] = np.clip(v[m], 0, 255).astype(np.uint8)
    # ---- the new detail clip + bounds
    B = t['bounds']
    lo, hi = float(np.nanmin(clip_h)), float(np.nanmax(clip_h))
    ymin, ymax = min(B['min'][1], lo), max(B['max'][1], hi)
    B['min'] = (B['min'][0], ymin, B['min'][2], B['min'][3]); B['max'] = (B['max'][0], ymax, B['max'][2], B['max'][3])
    for c in t['height_clips']:
        c['bounds']['min'] = (c['bounds']['min'][0], ymin) + tuple(c['bounds']['min'][2:])
        c['bounds']['max'] = (c['bounds']['max'][0], ymax) + tuple(c['bounds']['max'][2:])
    t['height_clips'].append({'bounds': {'min': (bx0, ymin, bz0, B['min'][3]), 'max': (cx1, ymax, cz1, B['max'][3])},
                              'height': {'map': clip_h.astype(np.float16).ravel(), 'resolution': (nx, nz), 'padding': (0, 0)}})
    log('INFO track placed: %.0f x %.0f m, heights %.1f..%.1f m, detail %dx%d (%.3f m)' %
        (pl.width, pl.length, lo, hi, nx, nz, res))
    return t


def flatten_map(t, keep=(0, 1), level=None, blend=40.0, clear=False, log=print):
    """Make the whole map one flat plane at `level` (m), except around the game tracks (detail clips) whose
    numbers are in `keep` (0-based): there the ground stays as it is, and it eases into the plane over `blend`
    metres. Detail clips not kept are removed (their tracks would float above the plane). Void (no land) stays
    void. clear: also no trees / rocks on the flattened land. Returns the level used."""
    g = Grid(t)
    clips = t['height_clips']
    kept = [clips[i] for i in sorted(set(keep)) if 0 <= i < len(clips)]
    rects = [(c['bounds']['min'][0], c['bounds']['min'][2], c['bounds']['max'][0], c['bounds']['max'][2]) for c in kept]
    B = g.base
    if level is None:
        vals = []
        for x0, z0, x1, z1 in rects:
            i0, i1 = max(0, int((z0 - g.z0) / g.dz)), min(g.ry, int((z1 - g.z0) / g.dz) + 1)
            j0, j1 = max(0, int((x0 - g.x0) / g.dx)), min(g.rx, int((x1 - g.x0) / g.dx) + 1)
            sub = B[i0:i1, j0:j1].astype(np.float32); sub = sub[sub > -999]
            if sub.size: vals.append(sub[::max(1, sub.size // 200000)])
        if vals: level = float(np.median(np.concatenate(vals)))
        else:
            land = B[::8, ::8].astype(np.float32); land = land[land > -999]
            level = float(np.median(land)) if land.size else 0.0
    bl = max(float(blend), 0.1)
    xs = g.x0 + np.arange(g.rx) * g.dx
    bio = [m['map'].reshape(g.ry, g.rx) for m in t['biome_height']] if clear else []
    for r0 in range(0, g.ry, 256):
        r1 = min(g.ry, r0 + 256)
        zs = g.z0 + np.arange(r0, r1) * g.dz
        d = np.full((r1 - r0, g.rx), np.inf, np.float32)
        for x0, z0, x1, z1 in rects:
            dx = np.maximum(np.maximum(x0 - xs, xs - x1), 0)[None, :]
            dz = np.maximum(np.maximum(z0 - zs, zs - z1), 0)[:, None]
            d = np.minimum(d, np.hypot(dx, dz))
        w = _smooth(d / bl) if rects else np.ones_like(d)
        sub = B[r0:r1].astype(np.float32)
        land = sub > -999
        B[r0:r1] = np.where(land, sub * (1 - w) + level * w, sub).astype(np.float16)
        for A in bio:
            s2 = A[r0:r1].astype(np.float32)
            A[r0:r1] = np.where(land, np.round(s2 * (1 - w)), s2).astype(np.uint8)
    t['height_clips'] = kept
    bd = t['bounds']
    bd['min'] = (bd['min'][0], min(bd['min'][1], level), bd['min'][2], bd['min'][3])
    bd['max'] = (bd['max'][0], max(bd['max'][1], level), bd['max'][2], bd['max'][3])
    log('INFO flattened the map at %.2f m, keeping %d game track(s)' % (level, len(kept)))
    return level

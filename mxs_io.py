"""
mxs_io.py - read the terrain of an MX Simulator track (read-only), straight from its .saf or an unpacked folder.

.saf (MX Simulator's archive): a text header of lines "<size> <path>\\n" (one per file, e.g.
"2750686 GunnisonR/terrain.png"), ended by a line "-\\n"; then every file's bytes, back to back in header order.
Only the few files needed are read out of it (nothing is unpacked to disk).

A track folder holds:
    terrain.hf   text "<dimension> <spacing> <lowest> <highest>", e.g. "10 0.750000 -6.058815 103.204445"
    terrain.png  square greyscale height image (16-bit), (2^(dimension+1) + 1) pixels a side
                 row r = z / spacing, column c = x / spacing, height = lowest + grey * (highest - lowest)
    timing_gates the starting gate and checkpoints ([x y z] positions) - used to find where the track is
    map.png      the in-game minimap (shown in the import window to compare)
Units are FEET (spectator sprites are ~6 high, gate slots 4 apart, trees 40-100) - converted to metres. Rows are turned over on import so the track keeps
its real shape in Senders (whose map runs the other way up) - the import window shows both to compare.
"""
import gzip
import io
import os
import re

import numpy as np


FT = 0.3048                    # MX Simulator's world unit (a foot) in metres


class NotATrack(Exception):
    pass


# ------------------------------------------------------------------ .saf
def saf_index(path):
    """[(name, offset, size)] of the files in a .saf archive."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        head = b''
        while True:
            chunk = f.read(1 << 16)
            if not chunk: raise ValueError('not an MX Simulator .saf archive (no end of its file list)')
            head += chunk
            k = head.find(b'\n-\n') if not head.startswith(b'-\n') else -1
            if head.startswith(b'-\n'): end = 2; break
            if k >= 0: end = k + 3; break
            if len(head) > 64 << 20: raise ValueError('not an MX Simulator .saf archive')
    out, off = [], end
    for line in head[:end - 2].split(b'\n'):
        if not line: continue
        m = re.match(rb'^(\d+) (.+)$', line)
        if not m: raise ValueError('not an MX Simulator .saf archive (bad file list line %r)' % line[:60])
        n = int(m.group(1))
        out.append((m.group(2).decode('utf-8', 'replace').replace('\\', '/'), off, n)); off += n
    if off != size:
        raise ValueError('the .saf archive is incomplete or damaged (%d bytes listed, %d there)' % (off, size))
    return out


def saf_read(path, entry):
    name, off, n = entry
    with open(path, 'rb') as f:
        f.seek(off); data = f.read(n)
    if len(data) != n: raise ValueError('%s is cut short in the archive' % name)
    return data


def _hf_prefix(name):
    """The folder prefix of a terrain.hf / terrain.hf.gz path, or None."""
    low = name.lower()
    for suf in ('terrain.hf', 'terrain.hf.gz'):
        if low.endswith(suf) and (len(low) == len(suf) or low[-len(suf) - 1] == '/'): return name[:-len(suf)]
    return None


def _has_hf(d):
    return os.path.exists(os.path.join(d, 'terrain.hf')) or os.path.exists(os.path.join(d, 'terrain.hf.gz'))


def saf_tracks(path):
    """The track folders inside a .saf (those holding terrain.hf): [folder prefix ('' = top level)]."""
    out = []
    for name, _, _ in saf_index(path):
        pre = _hf_prefix(name)
        if pre is not None: out.append(pre)
    return out


def extract(path, dest, log=None):
    """Unpack a whole .saf into folder dest (for anyone who wants the files). Returns how many were written."""
    k = 0
    dest_abs = os.path.abspath(dest)
    with open(path, 'rb') as f:
        for name, off, n in saf_index(path):
            target = os.path.abspath(os.path.join(dest, *[p for p in name.split('/') if p not in ('', '.', '..')]))
            if not target.startswith(dest_abs + os.sep): continue          # never outside dest
            os.makedirs(os.path.dirname(target), exist_ok=True)
            f.seek(off)
            with open(target, 'wb') as o:
                left = n
                while left:
                    b = f.read(min(left, 1 << 22)); o.write(b); left -= len(b)
                    if not b: break
            k += 1
            if log: log(name)
    return k


# ------------------------------------------------------------------ a track's files, wherever they are
class _Source:
    """Reads a track's files from a .saf (one track folder in it) or from a folder on disk."""

    def __init__(self, path, which=None):
        self.path = path
        if os.path.isdir(path):
            if not _has_hf(path):
                subs = [d for d in sorted(os.listdir(path)) if _has_hf(os.path.join(path, d))]
                if not subs: raise NotATrack('no MX Simulator terrain (terrain.hf) in this folder')
                path = os.path.join(path, which or subs[0])
            self.kind, self.dir = 'dir', path
            self.name = os.path.basename(path.rstrip('\\/'))
        elif path.lower().endswith(('.hf', '.hf.gz')):
            self.kind, self.dir = 'dir', os.path.dirname(path)
            self.name = os.path.basename(self.dir.rstrip('\\/')) or 'track'
        else:
            idx = saf_index(path)
            tracks = [p for p in (_hf_prefix(n) for n, _, _ in idx) if p is not None]
            if not tracks:
                raise NotATrack('%s is not a track (a bike, gear or other add-on - no terrain in it)'
                                % os.path.basename(path))
            pre = which if which is not None else tracks[0]
            self.kind, self.prefix = 'saf', pre
            self.files = {n[len(pre):].lower(): (n, o, s) for n, o, s in idx if n.startswith(pre)}
            base = os.path.splitext(os.path.basename(path))[0]
            self.name = pre.strip('/').split('/')[-1] if pre.strip('/') else base
            self.tracks = tracks

    def read(self, name, need=True):
        """A track file's bytes; tracks may also store it gzipped (<name>.gz)."""
        b = self._read(name, False)
        if b is None:
            b = self._read(name + '.gz', False)
            if b is not None: b = gzip.decompress(b)
        if b is None and need: raise NotATrack('the track has no %s' % name)
        return b

    def _read(self, name, need=True):
        if self.kind == 'dir':
            p = os.path.join(self.dir, name)
            if not os.path.exists(p):
                if need: raise NotATrack('the track has no %s' % name)
                return None
            with open(p, 'rb') as f: return f.read()
        e = self.files.get(name.lower())
        if e is None:
            if need: raise NotATrack('the track has no %s' % name)
            return None
        return saf_read(self.path, e)


def parse_hf(text):
    v = text.split()
    if len(v) < 4: raise ValueError('terrain.hf should hold 4 numbers, it has: %r' % text[:60])
    dim, res, lo, hi = int(float(v[0])), float(v[1]), float(v[2]), float(v[3])
    if not (res > 0 and hi >= lo): raise ValueError('unexpected terrain.hf values: %r' % text[:60])
    return dim, res, lo, hi


def _track_points(text):
    """[x, z] of the starting gate and checkpoints in a timing_gates file."""
    pts = []
    for line in text.splitlines():
        m = re.findall(r'\[\s*([-\d.eE]+)\s+([-\d.eE]+)\s+([-\d.eE]+)\s*\]', line)
        if m: pts.append((float(m[0][0]), float(m[0][2])))      # the first [x y z] of a line is the position
    return np.array(pts, np.float64).reshape(-1, 2)


def load(path, which=None, crop=True, margin=60.0):
    """{'name', 'heights' (rows = z, metres above the lowest point), 'size_x', 'size_z', 'range', 'map_png',
    'cropped', 'full_size', 'tracks'} of an MX Simulator track (.saf, track folder or its terrain.hf).
    crop: keep only the part around the track (its gates and checkpoints, plus margin metres).
    All sizes returned are in metres."""
    from PIL import Image
    src = _Source(path, which)
    dim, res, lo, hi = parse_hf(src.read('terrain.hf').decode('ascii', 'replace'))
    im = Image.open(io.BytesIO(src.read('terrain.png')))
    if im.mode in ('I;16', 'I;16L', 'I;16B', 'I;16N'):
        g = np.asarray(im, np.uint16).astype(np.float32) / 65535.0
    elif im.mode == 'I':
        g = np.asarray(im, np.float32) / 65535.0
    else:
        g = np.asarray(im.convert('L'), np.float32) / 255.0
    h = g * ((hi - lo) * FT)                            # metres above the lowest point
    full = ((h.shape[1] - 1) * res * FT, (h.shape[0] - 1) * res * FT)
    cropped = False
    tg = src.read('timing_gates', need=False)
    if crop and tg:
        P = _track_points(tg.decode('ascii', 'replace'))
        if len(P) >= 2:
            x0, z0 = P.min(0) - margin / FT; x1, z1 = P.max(0) + margin / FT
            c0, c1 = max(0, int(np.floor(x0 / res))), min(h.shape[1] - 1, int(np.ceil(x1 / res)))
            r0, r1 = max(0, int(np.floor(z0 / res))), min(h.shape[0] - 1, int(np.ceil(z1 / res)))
            if c1 - c0 >= 16 and r1 - r0 >= 16 and (c1 - c0 < h.shape[1] - 2 or r1 - r0 < h.shape[0] - 2):
                h = h[r0:r1 + 1, c0:c1 + 1]; cropped = True
    # MX Simulator's world is seen from above with z running DOWN its map (its minimap matches terrain.png as
    # it is); Senders' with z running UP. Turning the rows over keeps the track's real shape (not a mirror image).
    h = np.ascontiguousarray((h - h.min())[::-1], np.float32)
    return {'name': src.name, 'heights': h, 'size_x': (h.shape[1] - 1) * res * FT, 'size_z': (h.shape[0] - 1) * res * FT,
            'range': float(h.max()), 'map_png': src.read('map.png', need=False), 'cropped': cropped,
            'full_size': full, 'tracks': getattr(src, 'tracks', None), 'spacing': res * FT}


def is_track(path):
    p = path.lower()
    if p.endswith('.saf'): return True
    if p.endswith(('terrain.hf', 'terrain.hf.gz')): return True
    return os.path.isdir(path) and _has_hf(path)


def list_tracks(folder):
    """[(name, path, readable)] for the MX Simulator tracks in a folder: .saf archives holding a track, and
    unpacked track folders."""
    out = []
    if not folder or not os.path.isdir(folder): return out
    for f in sorted(os.listdir(folder)):
        p = os.path.join(folder, f)
        if f.lower().endswith('.saf'):
            try:
                if saf_tracks(p): out.append((os.path.splitext(f)[0] + ' (MX Simulator)', p, True))
            except (OSError, ValueError):
                pass
        elif os.path.isdir(p) and _has_hf(p):
            out.append((f + ' (MX Simulator folder)', p, True))
    return out


def default_dirs():
    out = []
    for d in (os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'MX Simulator'),
              os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'MX Simulator')):
        if os.path.isdir(d) and d not in out: out.append(d)
    return out

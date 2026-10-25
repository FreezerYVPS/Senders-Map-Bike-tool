"""
mxb_io.py - read the terrain heightmap of an MX Bikes track (read-only).

An MX Bikes track (.pkz = a zip archive, or an unpacked track folder) holds <name>.trh, the terrain:
    "TRH\\0", u32 columns (nx), u32 rows (nz),
    nx * nz little-endian 16-bit heights (row by row) - SIGNED (-32768..32767 = lowest..highest),
    f32 size x (m), f32 height range (m), f32 size z (m), then more data (not needed here).
Height in metres = (value + 32768) / 65535 * height range (0 = the lowest point).
(Read as unsigned numbers instead, everything below the middle height "wraps" to the top: tall walls and
cliffs where the ground crosses the middle, and the real hills lost - so the reader checks which reading is
smooth and uses that one.) Tracks whose .pkz is encrypted by their author can't be
read (and aren't).
"""
import glob
import os
import struct
import zipfile

import numpy as np


class ProtectedTrack(Exception):
    pass


def default_tracks_dir():
    for base in (os.path.join(os.path.expanduser('~'), 'Documents'), os.path.join(os.path.expanduser('~'), 'OneDrive', 'Documents')):
        d = os.path.join(base, 'PiBoSo', 'MX Bikes', 'mods', 'tracks')
        if os.path.isdir(d): return d
    return None


def _parse(head, data_reader):
    if head[:4] != b'TRH\x00':
        raise ValueError('not an MX Bikes terrain (.trh) file')
    nx, nz = struct.unpack('<II', head[4:12])
    if not (2 <= nx <= 16385 and 2 <= nz <= 16385):
        raise ValueError('unexpected terrain size %d x %d' % (nx, nz))
    raw = data_reader(2 * nx * nz + 12)
    if len(raw) < 2 * nx * nz + 12:
        raise ValueError('the terrain file is cut short')
    sx, hr, sz = struct.unpack('<3f', raw[2 * nx * nz:2 * nx * nz + 12])
    if not (sx > 0 and sz > 0 and hr >= 0):
        raise ValueError('unexpected terrain dimensions')
    s = np.frombuffer(raw[:2 * nx * nz], '<i2').reshape(nz, nx).astype(np.int32) + 32768
    u = np.frombuffer(raw[:2 * nx * nz], '<u2').reshape(nz, nx).astype(np.int32)
    a = s if _cliffs(s) <= _cliffs(u) else u
    return a.astype(np.float32) * (hr / 65535.0), float(sx), float(sz), float(hr)


def _cliffs(a):
    """How many neighbouring heights differ by more than a quarter of the whole range."""
    return int((np.abs(np.diff(a, axis=0)) > 16384).sum() + (np.abs(np.diff(a, axis=1)) > 16384).sum())


def unwrap_metres(h):
    """Repair heights that were read the old (unsigned) way: h in metres, 0..range."""
    h = np.asarray(h, np.float32)
    hr = float(h.max() - h.min()) if h.size else 0.0
    if hr <= 0: return h, False
    u = np.rint((h - h.min()) / hr * 65535).astype(np.int32)
    s = np.where(u >= 32768, u - 32768, u + 32768)
    if _cliffs(s) * 4 >= _cliffs(u): return h, False
    return (s.astype(np.float32) * (hr / 65535.0)), True


def _read_stream(f):
    head = f.read(12)
    return _parse(head, lambda n: f.read(n))


def track_name(path):
    b = os.path.basename(path.rstrip('\\/'))
    for ext in ('.pkz', '.trh', '.zip'):
        if b.lower().endswith(ext): return b[:-len(ext)]
    return b


def load(path):
    """{'name', 'heights' (rows = z, metres), 'size_x', 'size_z', 'range'} from a .pkz, a track folder or a .trh."""
    if os.path.isdir(path):
        trh = sorted(glob.glob(os.path.join(path, '*.trh')) + glob.glob(os.path.join(path, '*', '*.trh')))
        if not trh: raise ValueError('no terrain (.trh) in this folder')
        return load(trh[0])
    low = path.lower()
    if low.endswith('.trh'):
        with open(path, 'rb') as f: h, sx, sz, hr = _read_stream(f)
    else:
        try:
            z = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise ProtectedTrack('%s is packed with protection (encrypted) by its author, so its heightmap '
                                 "can't be read." % os.path.basename(path))
        with z:
            names = [i for i in z.infolist() if i.filename.lower().endswith('.trh')]
            if not names: raise ValueError('no terrain (.trh) in %s' % os.path.basename(path))
            names.sort(key=lambda i: -i.file_size)
            try:
                with z.open(names[0]) as f: h, sx, sz, hr = _read_stream(f)
            except RuntimeError as e:                 # password-protected member
                raise ProtectedTrack('%s is protected by its author: %s' % (os.path.basename(path), e))
    # rows = z, the same frame as the track's objects and as Senders' map (both games' worlds share their handedness)
    return {'name': track_name(path), 'heights': h, 'size_x': sx, 'size_z': sz, 'range': hr}


def is_track(path):
    p = path.lower()
    return p.endswith(('.pkz', '.trh')) or (os.path.isdir(path) and bool(glob.glob(os.path.join(path, '*.trh'))
                                                                          + glob.glob(os.path.join(path, '*', '*.trh'))))


def list_tracks(folder):
    """[(name, path, readable)] for the MX Bikes tracks in a folder (and its sub-folders)."""
    out = []
    if not folder or not os.path.isdir(folder): return out
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith('.pkz'):
                p = os.path.join(root, f)
                ok = zipfile.is_zipfile(p)
                out.append((track_name(p), p, ok))
        for d in list(dirs):
            p = os.path.join(root, d)
            if glob.glob(os.path.join(p, '*.trh')):
                out.append((d + ' (folder)', p, True)); dirs.remove(d)
    out.sort(key=lambda t: t[0].lower())
    return out


def orient(h, rot=0, mirror=False):
    """Turn (90 degree steps, clockwise as seen on the map) / mirror a heightmap; returns (h, swapped sizes?)."""
    if mirror: h = h[:, ::-1]
    k = (rot // 90) % 4
    if k: h = np.rot90(h, k)                  # clockwise as seen on the map (z up the screen)
    return np.ascontiguousarray(h), k % 2 == 1

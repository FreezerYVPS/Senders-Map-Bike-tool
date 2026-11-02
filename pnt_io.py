"""
pnt_io.py - read MX Bikes paint files (.pnt), read-only.

An unprotected .pnt is:  "PNT\\0", the paint's name (100 bytes, from offset 4), then at 0x68 a texture count and
per texture: name[100] (the bike material it paints, e.g. "crf"; "<name>_n" / "<name>_s" are its normal and
specular maps), width, height (u32), MD5 of the pixels (16 bytes), u32 size, u32 format, u32, then size-8 bytes of
raw DEFLATE holding width*height RGBA (or RGB / grey) pixels - the same texture storage as in .edf models.
Paints saved by newer paint tools are encrypted (no "PNT" header): those are protected by their authors and are
not read.
"""
import hashlib
import os
import struct
import zlib

import numpy as np


class PNTError(Exception):
    pass


class Protected(PNTError):
    pass


def is_protected(path):
    with open(path, 'rb') as f: return f.read(4) != b'PNT\0'


def _name(raw):
    return bytes(raw).split(b'\0', 1)[0].decode('utf-8', 'replace').strip()


def index(path):
    """{'name', 'textures': [{'name', 'w', 'h', 'offset', 'size', 'digest'}]} without decompressing anything."""
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        head = f.read(0x6c)
        if head[:4] != b'PNT\0':
            raise Protected('%s is a protected (encrypted) paint - only its author\'s tools can open it'
                            % os.path.basename(path))
        out = {'name': _name(head[4:0x68]) or os.path.splitext(os.path.basename(path))[0], 'textures': []}
        n = struct.unpack_from('<I', head, 0x68)[0]
        if n > 64: raise PNTError('not a paint file (%d textures?)' % n)
        p = 0x6c
        for _ in range(n):
            f.seek(p); h = f.read(140)
            if len(h) < 140: raise PNTError('the paint file is cut short')
            name = _name(h[:100]); w, ht = struct.unpack_from('<II', h, 100); dig = h[108:124]
            sz = struct.unpack_from('<I', h, 124)[0]
            if sz < 8 or p + 128 + sz > size or not (0 < w <= 16384 and 0 < ht <= 16384):
                raise PNTError('the paint file is damaged (texture %s)' % name)
            out['textures'].append({'name': name, 'w': w, 'h': ht, 'offset': p + 136, 'size': sz - 8, 'digest': dig})
            p += 128 + sz
    return out


def texture(path, tex):
    """RGBA uint8 array (rows top first) of one texture from index()."""
    with open(path, 'rb') as f:
        f.seek(tex['offset']); comp = f.read(tex['size'])
    try:
        raw = zlib.decompress(comp, -15)
    except zlib.error as e:
        raise PNTError('texture %s could not be unpacked (%s)' % (tex['name'], e))
    if hashlib.md5(raw).digest() != tex['digest']: raise PNTError('texture %s is damaged' % tex['name'])
    w, h = tex['w'], tex['h']; n = w * h
    if len(raw) == n * 4: return np.frombuffer(raw, np.uint8).reshape(h, w, 4).copy()
    if len(raw) == n * 3:
        return np.dstack([np.frombuffer(raw, np.uint8).reshape(h, w, 3), np.full((h, w), 255, np.uint8)])
    if len(raw) == n:
        g = np.frombuffer(raw, np.uint8).reshape(h, w); return np.dstack([g, g, g, np.full_like(g, 255)])
    raise PNTError('texture %s has an unknown pixel format' % tex['name'])


def colour_textures(idx):
    """The paint's colour textures (not its _n normal / _s specular / _r reflection maps)."""
    return [t for t in idx['textures']
            if not t['name'].lower().endswith(('_n', '_s', '_r', '_nm', '_spec', '_norm', '_ref', '_env'))]


def find_paints(source):
    """Paints for the MX Bikes bike a model came from: [(label, path, protected)]. source = its model.edf."""
    out, seen = [], set()
    folder = os.path.dirname(os.path.abspath(source))
    bike = os.path.basename(folder)
    cands = [os.path.join(folder, 'paints')]
    # Documents\PiBoSo\MX Bikes\paints\<bike>  (mods\bikes\<bike>\model.edf -> ..\..\..\paints\<bike>)
    root = os.path.dirname(os.path.dirname(os.path.dirname(folder)))
    cands.append(os.path.join(root, 'paints', bike))
    for d in cands:
        if not os.path.isdir(d): continue
        for f in sorted(os.listdir(d), key=str.lower):
            p = os.path.join(d, f)
            if f.lower().endswith('.pnt') and p not in seen:
                seen.add(p)
                try: prot = is_protected(p)
                except OSError: continue
                out.append((os.path.splitext(f)[0] + ('  (protected)' if prot else ''), p, prot))
    return out

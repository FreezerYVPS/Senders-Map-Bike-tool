"""
zigspec.py - read the game's generated BlendMeshSpec.zig and (de)serialize
.blend.bin files exactly as that spec describes. Pure Python, no Blender needed.

    spec = ZigSpec.load('BlendMeshSpec.zig')
    data = spec.encode(model_dict)       # -> bytes, starting with spec.header
    model = spec.decode(data)            # -> dict
    spec.models['bike-4stroke']          # -> list of node names the game expects
    spec.textures                        # -> list of texture paths the game knows

Binary rules (verified byte-for-byte against bike-4stroke.blend.bin):
    header line, then the root struct's fields in declaration order, no padding,
    little-endian.  []T -> u64 count + items   ([]const u8 is a UTF-8 string)
    ?T -> u8 0|1 + T      [N]T -> N items      struct -> fields in order
    packed struct -> its bits, LSB first, ceil(bits/8) bytes
    enum(uN) -> tag value in ceil(N/8) bytes   iN/uN/fN -> ceil(N/8) bytes
"""
import os
import re
import struct

# ------------------------------------------------------------------ parsing
_TOK = re.compile(r'''
    (?P<ws>\s+|//[^\n]*)
  | (?P<qid>@"(?:[^"\\]|\\.)*")
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<num>-?(?:0x[0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][-+]?\d+)?))
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<p>[{}()\[\],;:=?.&*!|])
''', re.X)


def _unescape(s):
    out, i = bytearray(), 0
    esc = {'n': b'\n', 't': b'\t', 'r': b'\r', '\\': b'\\', '"': b'"', "'": b"'"}
    while i < len(s):
        c = s[i]
        if c == '\\':
            n = s[i + 1]
            if n == 'x':
                out.append(int(s[i + 2:i + 4], 16)); i += 4; continue
            if n == 'u':
                j = s.index('}', i); out += chr(int(s[i + 3:j], 16)).encode(); i = j + 1; continue
            out += esc[n]; i += 2; continue
        out += c.encode(); i += 1
    return out.decode('utf-8')


def _tokenize(src):
    toks, pos = [], 0
    while pos < len(src):
        m = _TOK.match(src, pos)
        if not m: raise SyntaxError('cannot parse .zig near: %r' % src[pos:pos + 40])
        pos = m.end()
        k = m.lastgroup
        if k == 'ws': continue
        v = m.group(k)
        if k == 'qid': toks.append(('id', _unescape(v[2:-1])))
        elif k == 'str': toks.append(('str', _unescape(v[1:-1])))
        else: toks.append((k, v))
    toks.append(('eof', ''))
    return toks


class _P:
    def __init__(self, toks): self.t, self.i = toks, 0
    def peek(self, k=0): return self.t[self.i + k]
    def next(self): tok = self.t[self.i]; self.i += 1; return tok

    def eat(self, v):
        tok = self.next()
        if tok[1] != v: raise SyntaxError('expected %r, got %r' % (v, tok[1]))
        return tok

    def accept(self, v):
        if self.peek()[1] == v and self.peek()[0] != 'str': self.i += 1; return True
        return False

    # ---- types
    def type_expr(self):
        if self.accept('?'): return ('opt', self.type_expr())
        if self.accept('*'):
            self.accept('const'); return self.type_expr()
        if self.accept('['):
            if self.accept(']'):
                const = self.accept('const'); return ('slice', self.type_expr(), const)
            n = self.next()[1]; self.eat(']')
            return ('array', int(n.replace('_', ''), 0), self.type_expr())
        packed = self.accept('packed')
        self.accept('extern')
        if self.accept('struct'): return self.struct_body(packed)
        if self.accept('enum'): return self.enum_body()
        if self.accept('union'): return self.union_body()
        tok = self.next()
        if tok[0] != 'id': raise SyntaxError('bad type %r' % (tok,))
        return ('ref', tok[1])

    def struct_body(self, packed):
        self.eat('{'); fields = []
        while not self.accept('}'):
            if self.peek()[1] in ('pub', 'const', 'fn'):
                self.skip_decl(); continue
            name = self.next()[1]; self.eat(':')
            t = self.type_expr()
            default, has_default = None, False
            if self.accept('='):
                default, has_default = self.value(), True
            fields.append((name, t, default, has_default))
            self.accept(',')
        return ('struct', packed, fields)

    def enum_body(self):
        tag = None
        if self.accept('('): tag = self.next()[1]; self.eat(')')
        self.eat('{'); members, n = [], 0
        while not self.accept('}'):
            name = self.next()[1]
            if self.accept('='): n = int(self.next()[1].replace('_', ''), 0)
            members.append((name, n)); n += 1
            self.accept(',')
        if tag is None:
            tag = 'u%d' % max(1, (len(members) - 1).bit_length())
        return ('enum', tag, members)

    def union_body(self):
        if self.accept('('):
            while not self.accept(')'): self.next()
        self.eat('{'); members = []
        while not self.accept('}'):
            name = self.next()[1]
            t = None
            if self.accept(':'): t = self.type_expr()
            members.append((name, t)); self.accept(',')
        return ('union', members)

    def skip_decl(self):
        depth = 0
        while True:
            tok = self.next()[1]
            if tok in '{([': depth += 1
            elif tok in '})]': depth -= 1
            elif tok == ';' and depth == 0: return

    # ---- values (defaults / constants)
    def value(self):
        tok = self.peek()
        if tok[0] == 'str': self.next(); return tok[1]
        if tok[0] == 'num':
            self.next(); s = tok[1].replace('_', '')
            return float(s) if ('.' in s or 'e' in s.lower()) and not s.startswith('0x') else int(s, 0)
        if self.accept('&'): return self.value()
        if self.accept('.'):
            if self.accept('{'):
                items, named = [], {}
                while not self.accept('}'):
                    if self.peek()[1] == '.' and self.peek(1)[0] == 'id' and self.peek(2)[1] == '=':
                        self.next(); k = self.next()[1]; self.eat('='); named[k] = self.value()
                    else:
                        items.append(self.value())
                    self.accept(',')
                return named if named else items
            return ('enumlit', self.next()[1])
        if tok[1] in ('null', 'undefined'): self.next(); return None
        if tok[1] == 'true': self.next(); return True
        if tok[1] == 'false': self.next(); return False
        # anything else: skip a balanced expression
        depth, out = 0, []
        while True:
            t = self.peek()[1]
            if depth == 0 and t in (',', '}', ';'): break
            if t in '{([': depth += 1
            if t in '})]': depth -= 1
            out.append(self.next()[1])
        return ' '.join(out)


# ------------------------------------------------------------------ spec
_PRIM = re.compile(r'^([iuf])(\d+)$')


class RawSlice:
    """A slice whose items are already packed (little-endian, the spec's layout) - for big arrays."""

    def __init__(self, count, data):
        self.count = int(count); self.data = bytes(data)

    def __len__(self): return self.count


class SpecError(Exception):
    pass


class ZigSpec:
    def __init__(self, src, path=None):
        self.path = path
        self.types, self.consts = {}, {}
        p = _P(_tokenize(src))
        while p.peek()[0] != 'eof':
            if p.accept('pub'): continue
            if p.accept('const'):
                name = p.next()[1]
                if p.accept(':'): p.type_expr()
                p.eat('=')
                tok = p.peek()
                if tok[0] in ('str', 'num') or tok[1] in ('.', '&'):
                    self.consts[name] = p.value()
                else:
                    self.types[name] = p.type_expr()
                p.accept(';')
            else:
                p.skip_decl()
        hdr = self.consts.get('binary_header')
        if not hdr: raise SpecError('no binary_header constant in spec file')
        self.header = hdr.encode('utf-8')
        parts = hdr.strip().split(':')
        if len(parts) != 3: raise SpecError('unexpected header %r' % hdr)
        self.schema_hash = parts[1]
        self.root = parts[2].split('.')[-1]
        if self.root not in self.types: raise SpecError('root type %s not defined' % self.root)
        self.models = {}
        for name, t in self.types.items():
            m = re.match(r'^(.*)\.blend\.generated\.Nodes$', name)
            if m and t[0] == 'enum':
                self.models[m.group(1)] = [n for n, _ in sorted(t[2], key=lambda x: x[1])]
        tex = self.types.get('TextureId')
        self.textures = [n for n, _ in sorted(tex[2], key=lambda x: x[1])] if tex and tex[0] == 'enum' else None
        self._flat = {}

    @classmethod
    def load(cls, path):
        with open(path, encoding='utf-8') as f:
            return cls(f.read(), path)

    # ---- type helpers
    def resolve(self, t):
        seen = 0
        while t[0] == 'ref' and not _PRIM.match(t[1]) and t[1] not in ('bool',):
            if t[1] not in self.types: raise SpecError('unknown type %s' % t[1])
            t = self.types[t[1]]; seen += 1
            if seen > 50: raise SpecError('type alias loop')
        return t

    def has_field(self, struct_name, field):
        t = self.resolve(('ref', struct_name))
        return t[0] == 'struct' and any(f[0] == field for f in t[2])

    def bits(self, t):
        t = self.resolve(t)
        if t[0] == 'ref':
            if t[1] == 'bool': return 1
            return int(_PRIM.match(t[1]).group(2))
        if t[0] == 'enum': return int(_PRIM.match(t[1]).group(2))
        if t[0] == 'struct' and t[1]: return sum(self.bits(f[1]) for f in t[2])
        if t[0] == 'array': return t[1] * self.bits(t[2])
        raise SpecError('type has no fixed bit size: %r' % (t,))

    @staticmethod
    def _prim_fmt(name):
        if name == 'bool': return 'B'
        k, n = _PRIM.match(name).groups(); n = int(n)
        nb = (n + 7) // 8
        if k == 'f': return {2: 'e', 4: 'f', 8: 'd'}[nb]
        fmt = {1: 'b', 2: 'h', 4: 'i', 8: 'q'}.get(nb)
        if fmt is None: return None
        return fmt.upper() if k == 'u' else fmt

    def flat(self, t):
        """(fmt_chars, to_tuple, from_tuple) for fixed-size byte-aligned types, else None."""
        key = repr(t)
        if key in self._flat: return self._flat[key]
        r = self._make_flat(t)
        self._flat[key] = r
        return r

    def _make_flat(self, t):
        t = self.resolve(t)
        if t[0] == 'ref':
            n = t[1]
            if n != 'bool' and int(_PRIM.match(n).group(2)) % 8: return None
            f = self._prim_fmt(n)
            if f is None: return None
            if n == 'bool': return f, lambda v: (1 if v else 0,), lambda it: bool(next(it))
            if n[0] == 'f': return f, lambda v: (float(v),), lambda it: next(it)
            return f, lambda v: (int(v),), lambda it: next(it)
        if t[0] == 'array':
            sub = self.flat(t[2])
            if not sub: return None
            n, (sf, st, sb) = t[1], sub
            def to(v, n=n, st=st):
                out = ()
                for x in v: out += st(x)
                return out
            return sf * n, to, lambda it, n=n, sb=sb: [sb(it) for _ in range(n)]
        if t[0] == 'struct' and t[1]:
            subs = [(name, self.flat(ft), d, hd) for name, ft, d, hd in t[2]]
            if any(s[1] is None for s in subs): return None
            fmt = ''.join(s[1][0] for s in subs)
            def to(v, subs=subs):
                out = ()
                for name, (sf, st, sb), d, hd in subs:
                    if name in v: x = v[name]
                    elif hd: x = d
                    else: raise SpecError('missing field %r' % name)
                    out += st(x)
                return out
            def back(it, subs=subs):
                return {name: sb(it) for name, (sf, st, sb), d, hd in subs}
            return fmt, to, back
        return None

    # ---- encoding
    def encode(self, value):
        out = [self.header]
        self._enc(('ref', self.root), value, out, self.root)
        return b''.join(out)

    def _enc(self, t, v, out, path):
        t = self.resolve(t)
        kind = t[0]
        if kind == 'ref':
            fmt = self._prim_fmt(t[1])
            if t[1][0] == 'f': out.append(struct.pack('<' + fmt, float(v)))
            else:
                nb = (int(_PRIM.match(t[1]).group(2)) + 7) // 8 if t[1] != 'bool' else 1
                out.append(int(v if t[1] != 'bool' else bool(v)).to_bytes(nb, 'little', signed=t[1][0] == 'i'))
        elif kind == 'opt':
            if v is None: out.append(b'\0')
            else: out.append(b'\1'); self._enc(t[1], v, out, path)
        elif kind == 'slice':
            if isinstance(v, RawSlice):                 # pre-packed items (big meshes): count + bytes
                out.append(struct.pack('<Q', v.count)); out.append(v.data); return
            el = self.resolve(t[1])
            if el == ('ref', 'u8'):
                b = v.encode('utf-8') if isinstance(v, str) else bytes(v)
                out.append(struct.pack('<Q', len(b))); out.append(b); return
            out.append(struct.pack('<Q', len(v)))
            self._enc_items(t[1], v, out, path)
        elif kind == 'array':
            if len(v) != t[1]: raise SpecError('%s: expected %d items, got %d' % (path, t[1], len(v)))
            self._enc_items(t[2], v, out, path)
        elif kind == 'enum':
            val = self._enum_val(t, v, path)
            nb = (int(_PRIM.match(t[1]).group(2)) + 7) // 8
            out.append(val.to_bytes(nb, 'little', signed=t[1][0] == 'i'))
        elif kind == 'struct' and t[1]:
            fl = self.flat(t)
            if fl:
                out.append(struct.pack('<' + fl[0], *fl[1](v)))
            else:
                bits = self.bits(t)
                out.append(self._pack_bits(t, v).to_bytes((bits + 7) // 8, 'little'))
        elif kind == 'struct':
            for name, ft, d, hd in t[2]:
                if name in v: x = v[name]
                elif hd: x = d
                elif self.resolve(ft)[0] == 'opt': x = None
                else: raise SpecError('%s: missing field "%s"' % (path, name))
                self._enc(ft, x, out, path + '.' + name)
        else:
            raise SpecError('%s: cannot serialize %s' % (path, kind))

    def _enc_items(self, et, items, out, path):
        fl = self.flat(et)
        if fl:
            fmt, to, _ = fl
            if len(set(fmt)) == 1:
                flat = []
                for x in items: flat.extend(to(x))
                out.append(struct.pack('<%d%s' % (len(flat), fmt[0]), *flat))
            else:
                s = struct.Struct('<' + fmt)
                out.append(b''.join(s.pack(*to(x)) for x in items))
        else:
            for i, x in enumerate(items):
                self._enc(et, x, out, '%s[%d]' % (path, i))

    def _enum_val(self, t, v, path):
        if isinstance(v, tuple) and v and v[0] == 'enumlit': v = v[1]
        if isinstance(v, int): return v
        for n, val in t[2]:
            if n == v: return val
        raise SpecError('%s: %r is not one of %s' % (path, v, [n for n, _ in t[2]]))

    def _pack_bits(self, t, v):
        acc, shift = 0, 0
        for name, ft, d, hd in t[2]:
            x = v.get(name, d) if isinstance(v, dict) else v
            ftr = self.resolve(ft); nb = self.bits(ft)
            if ftr[0] == 'struct': raw = self._pack_bits(ftr, x)
            elif ftr[0] == 'enum': raw = self._enum_val(ftr, x, name)
            elif ftr[0] == 'ref' and ftr[1][0] == 'f':
                raw = int.from_bytes(struct.pack('<' + self._prim_fmt(ftr[1]), x), 'little')
            else: raw = int(x)
            acc |= (raw & ((1 << nb) - 1)) << shift; shift += nb
        return acc

    # ---- decoding
    def decode(self, data):
        if not data.startswith(self.header):
            nl = data.find(b'\n')
            raise SpecError('file header %r does not match this spec (%r)'
                            % (data[:nl].decode('latin-1'), self.header.decode().strip()))
        self._b = memoryview(data); self._p = len(self.header)
        v = self._dec(('ref', self.root))
        if self._p != len(data):
            raise SpecError('%d bytes left over: file does not match this spec' % (len(data) - self._p))
        return v

    def _take(self, n):
        if self._p + n > len(self._b):
            raise SpecError('unexpected end of file at 0x%x: file does not match this spec' % self._p)
        b = self._b[self._p:self._p + n]; self._p += n; return b

    def _dec(self, t):
        t = self.resolve(t)
        kind = t[0]
        if kind == 'ref':
            if t[1] == 'bool': return bool(self._take(1)[0])
            nb = (int(_PRIM.match(t[1]).group(2)) + 7) // 8
            if t[1][0] == 'f': return struct.unpack('<' + self._prim_fmt(t[1]), self._take(nb))[0]
            return int.from_bytes(self._take(nb), 'little', signed=t[1][0] == 'i')
        if kind == 'opt':
            tag = self._take(1)[0]
            if tag == 0: return None
            if tag != 1: raise SpecError('bad optional tag %d at 0x%x' % (tag, self._p - 1))
            return self._dec(t[1])
        if kind in ('slice', 'array'):
            if kind == 'slice':
                n = struct.unpack('<Q', self._take(8))[0]
                if n > len(self._b): raise SpecError('implausible length %d at 0x%x' % (n, self._p - 8))
                et = t[1]
                if self.resolve(et) == ('ref', 'u8'):
                    b = bytes(self._take(n))
                    return b.decode('utf-8') if self._is_string(t) else list(b)
            else:
                n, et = t[1], t[2]
            fl = self.flat(et)
            if fl:
                fmt, _, back = fl
                vals = struct.unpack('<' + fmt * n if len(set(fmt)) > 1 else '<%d%s' % (n * len(fmt), fmt[:1] or 'x'),
                                     self._take(struct.calcsize('<' + fmt) * n)) if n else ()
                it = iter(vals)
                return [back(it) for _ in range(n)]
            return [self._dec(et) for _ in range(n)]
        if kind == 'enum':
            nb = (int(_PRIM.match(t[1]).group(2)) + 7) // 8
            val = int.from_bytes(self._take(nb), 'little')
            return next((n for n, x in t[2] if x == val), val)
        if kind == 'struct' and t[1]:
            fl = self.flat(t)
            if fl:
                return fl[2](iter(struct.unpack('<' + fl[0], self._take(struct.calcsize('<' + fl[0])))))
            nbits = self.bits(t)
            return self._unpack_bits(t, int.from_bytes(self._take((nbits + 7) // 8), 'little'))
        if kind == 'struct':
            return {name: self._dec(ft) for name, ft, d, hd in t[2]}
        raise SpecError('cannot deserialize %s' % kind)

    @staticmethod
    def _is_string(t):
        return len(t) > 2 and t[2]      # []const u8 = text, []u8 = raw byte map

    def _unpack_bits(self, t, acc):
        out = {}
        for name, ft, d, hd in t[2]:
            ftr = self.resolve(ft); nb = self.bits(ft)
            raw = acc & ((1 << nb) - 1); acc >>= nb
            if ftr[0] == 'struct': out[name] = self._unpack_bits(ftr, raw)
            elif ftr[0] == 'enum': out[name] = next((n for n, x in ftr[2] if x == raw), raw)
            elif ftr[0] == 'ref' and ftr[1][0] == 'f':
                out[name] = struct.unpack('<' + self._prim_fmt(ftr[1]), raw.to_bytes(nb // 8, 'little'))[0]
            else: out[name] = raw
        return out

    # ---- convenience
    def describe(self):
        return '%s  (schema %s)' % (self.root, self.schema_hash)


def sniff_header(path_or_bytes):
    """Return the 16-hex schema hash from a .bin file (or None)."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        head = bytes(path_or_bytes[:200])
    else:
        with open(path_or_bytes, 'rb') as f: head = f.read(200)
    m = re.match(rb'BinarySerializer:([0-9a-fA-F]+):', head)
    return m.group(1).decode() if m else None


def stale_backup(path):
    """True when path + '.orig' is a backup from an older game build: the game has been updated since
    (Steam put a new original in place, in a different file version), so the current file is the original now."""
    b = path + '.orig'
    if not (os.path.isfile(path) and os.path.isfile(b)): return False
    hb, hp = sniff_header(b), sniff_header(path)
    return bool(hb and hp and hb != hp)


def original_of(path):
    """The game's original for a game file: its .orig backup if we replaced it, unless that backup is from an
    older game build."""
    if os.path.isfile(path + '.orig') and not stale_backup(path): return path + '.orig'
    return path if os.path.isfile(path) else None


# ------------------------------------------------------------------ validation
def check_model(spec, model, doc, reference=None):
    """
    Compare an exported document against what the game expects.
    Returns (errors, warnings) - lists of strings.
      model     : BlendId name, e.g. 'bike-4stroke' (from the .blend file name)
      reference : optional decoded copy of the file being replaced, to compare node types
    """
    errors, warns = [], []
    names = [n['name'] for n in doc['nodes']]
    dup = sorted({n for n in names if names.count(n) > 1})
    for n in dup: errors.append('duplicate node name "%s"' % n)
    expected = spec.models.get(model)
    if expected is None:
        warns.append('"%s" is not one of the game\'s models (%s). The game only loads models it was '
                     'compiled with, so a new model needs the dev to regenerate the spec.'
                     % (model, ', '.join(sorted(spec.models))))
    else:
        have = set(names)
        for n in expected:
            if n not in have: errors.append('missing node "%s" (the game requires it)' % n)
        for n in names:
            if n not in expected: errors.append('extra node "%s" is not in %s\'s node list' % (n, model))
        common = [n for n in names if n in expected]
        if common != [n for n in expected if n in have]:
            errors.append('node order differs from the spec')
    if spec.textures is not None:
        for n in doc['nodes']:
            m = n.get('material') or {}
            for k in ('diffuse_texture', 'normal_texture', 'roughness_metallic_texture', 'baked_material_texture'):
                p = m.get(k)
                if p and p not in spec.textures:
                    errors.append('%s: %s "%s" is not in the game\'s TextureId list' % (n['name'], k, p))
    for n in doc['nodes']:
        if n.get('parent') and n['parent'] not in names:
            warns.append('%s: parent "%s" is not exported' % (n['name'], n['parent']))
        me = n.get('mesh')
        if me is not None and (not me.get('positions') or not me.get('polygons')):
            errors.append('%s: mesh has no %s (the game refuses this)' % (
                n['name'], 'positions' if not me.get('positions') else 'polygons'))
        if me and me.get('bone_indices'):
            if any(b > 127 for row in me['bone_indices'] for b in row):
                errors.append('%s: bone index above 127 (stored as int8)' % n['name'])
    if reference is not None:
        rt = {n['name']: n['type'] for n in reference['nodes']}
        for n in doc['nodes']:
            if n['name'] in rt and rt[n['name']] != n['type']:
                errors.append('%s: type changed %s -> %s compared to the file being replaced'
                              % (n['name'], rt[n['name']], n['type']))
    return errors, warns


def model_from_filename(path):
    """'C:/x/bike-4stroke.blend' or 'bike-4stroke.blend.bin' -> 'bike-4stroke'"""
    import os
    b = os.path.basename(path)
    for suf in ('.blend.bin', '_blend.json', '.blend'):
        if b.endswith(suf): return b[:-len(suf)]
    return os.path.splitext(b)[0]

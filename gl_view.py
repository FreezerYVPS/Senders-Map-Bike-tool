"""
gl_view.py - GPU (OpenGL) viewport for the alignment editor: your model at full detail with its packed
texture or game colours, the original bike's real meshes, studio lighting and anti-aliasing - drawn by the
graphics card like Blender's viewport. Needs: pip install pyopengltk PyOpenGL (and numpy).
The editor falls back to its software view if any of this is missing or fails.
"""
import math
import sys

import numpy as np
from OpenGL import GL
from pyopengltk import OpenGLFrame

PER_ROW = 512          # pieces per row of the state texture (8 texels each)

VS = """
#version 130
in vec3 aPos; in vec3 aNrm; in vec2 aUV; in vec3 aCol; in float aTex; in float aPid; in vec2 aRM;
uniform mat4 uVP; uniform mat4 uM; uniform int uRef; uniform sampler2D uState;
out vec3 vN; out vec2 vUV; out vec3 vCol; out float vTex; out vec4 vOvr; out vec2 vSel; out vec3 vW; out vec3 vRM;
flat out int vPid;
void main() {
    mat4 M = uM; vec4 ovr = vec4(0.0); vec4 fl = vec4(0.0, 1.0, 1.0, 0.0); vec4 orm = vec4(0.0);
    int i = int(aPid + 0.5);
    if (uRef == 0) {
        int x = (i % 512) * 8; int y = i / 512;
        vec4 r0 = texelFetch(uState, ivec2(x, y), 0);
        vec4 r1 = texelFetch(uState, ivec2(x + 1, y), 0);
        vec4 r2 = texelFetch(uState, ivec2(x + 2, y), 0);
        vec4 r3 = texelFetch(uState, ivec2(x + 3, y), 0);
        M = transpose(mat4(r0, r1, r2, r3));
        ovr = texelFetch(uState, ivec2(x + 4, y), 0);
        fl = texelFetch(uState, ivec2(x + 5, y), 0);
        orm = texelFetch(uState, ivec2(x + 6, y), 0);
    }
    vec4 w = M * vec4(aPos, 1.0);
    gl_Position = uVP * w;
    if (fl.z < 0.5) gl_Position = vec4(3.0, 3.0, 3.0, 1.0);      // hidden piece
    vN = mat3(M) * aNrm; vUV = aUV; vCol = aCol; vTex = aTex; vOvr = ovr; vSel = fl.xy; vPid = i; vW = w.xyz;
    vRM = orm.z > 0.5 ? vec3(orm.xy, 1.0) : vec3(aRM, 0.0);      // a game material you picked overrides
}
"""

FS = """
#version 130
in vec3 vN; in vec2 vUV; in vec3 vCol; in float vTex; in vec4 vOvr; in vec2 vSel; in vec3 vW; in vec3 vRM;
flat in int vPid;
uniform sampler2D uAtlas; uniform int uUseTex; uniform int uIdPass; uniform int uGhost; uniform int uGame;
uniform vec3 uGhostCol; uniform float uAlpha; uniform vec3 uR; uniform vec3 uU; uniform vec3 uF;
uniform vec3 uSunDir; uniform vec3 uSunCol; uniform vec3 uSkyCol; uniform float uSkyMul; uniform float uRadScale;
uniform float uExposure; uniform vec3 uSlope; uniform vec3 uOffset; uniform vec3 uPower; uniform float uSat;
out vec4 frag;
const float PI = 3.14159265;

vec3 sky(vec3 d) {                          // stand-in for the game's sky probe: blue sky, sunlit dirt/grass below
    float h = clamp(d.y, -1.0, 1.0);
    vec3 up = mix(vec3(0.78, 0.86, 0.95), vec3(0.32, 0.50, 0.85), pow(max(h, 0.0), 0.55));
    vec3 dn = mix(vec3(0.42, 0.40, 0.30), vec3(0.20, 0.17, 0.11), clamp(-h * 2.0, 0.0, 1.0));
    return (h >= 0.0 ? up : dn) * uSkyCol;
}

vec3 agx(vec3 c) {                          // AgX, as the game's tonemap pass (public AgX constants)
    mat3 inset = mat3(0.84247905, 0.042328242, 0.042375654, 0.0784336, 0.87846863, 0.0784336,
                      0.079223745, 0.07916613, 0.87914294);
    mat3 outset = mat3(1.196879, -0.05289685, -0.052971635, -0.09802088, 1.1519032, -0.09804345,
                       -0.09902974, -0.098961174, 1.1510737);
    const float lo = -12.47393, hi = 4.026069;
    c = inset * max(c, vec3(1e-10));
    c = (clamp(log2(c), lo, hi) - lo) / (hi - lo);
    vec3 x2 = c * c, x4 = x2 * x2;
    c = 15.5 * x4 * x2 - 40.14 * x4 * c + 31.96 * x4 - 6.868 * x2 * c + 0.4298 * x2 + 0.1191 * c - 0.00232;
    c = clamp(outset * c, 0.0, 1.0);
    float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
    c = pow(max(c * uSlope + uOffset, 0.0), uPower);                // the game's "look" (Render.zon)
    float l2 = dot(c, vec3(0.2126, 0.7152, 0.0722));
    return max(vec3(l2) + (c - vec3(l2)) * uSat, 0.0);
}

void main() {
    if (uIdPass == 1) {
        int k = vPid + 1;
        frag = vec4(float(k & 255) / 255.0, float((k >> 8) & 255) / 255.0, float((k >> 16) & 255) / 255.0, 1.0);
        return;
    }
    vec3 base = vCol;
    if (uUseTex == 1 && vTex > 0.5) base = texture(uAtlas, vUV).rgb * vCol;
    if (vOvr.a > 0.5) base = vOvr.rgb;
    if (uGhost == 1) base = uGhostCol;
    vec3 n = normalize(vN);
    vec3 V = -uF;
    if (dot(n, V) < 0.0) n = -n;
    vec3 c;
    if (uGame == 1 && uGhost == 0) {
        // ---- the game's material response (main_render + lighting pass) ----
        vec3 albedo = pow(clamp(base, 0.0, 1.0), vec3(2.2));
        float r = clamp(vRM.x, 0.0, 1.0);
        float m = (vRM.y >= 0.5 ? 1.0 : 0.0) * (1.0 - r);           // metallic is on/off, faded by roughness
        vec3 F0 = mix(vec3(0.04), albedo, m);
        vec3 L = normalize(uSunDir);
        float NL = max(dot(n, L), 0.0);
        vec3 kd = (vec3(1.0) - F0 * (1.0 - r)) * (1.0 - m);
        vec3 sun = uSunCol * 3.2;
        vec3 diff = albedo * kd * sun * NL / PI;
        float pw = mix(768.0, 48.0, r);
        vec3 H = normalize(L + V);
        vec3 Fs = mix(F0, F0 + (1.0 - F0) * pow(1.0 - clamp(dot(L, H), 0.0, 1.0), 5.0), vec3(1.0 - r));
        vec3 spec = sun * Fs * (pow(max(dot(n, H), 0.0), pw) * (pw + 2.0) / (2.0 * PI)) * NL * (1.0 - r);
        vec3 F = F0 + (1.0 - F0) * pow(1.0 - max(mix(dot(n, V), 1.0, r), 0.0), 5.0);
        vec3 irr = mix(sky(vec3(0.0, -1.0, 0.0)), sky(vec3(0.0, 1.0, 0.0)), n.y * 0.5 + 0.5) * uRadScale * uSkyMul;
        vec3 R = reflect(-V, n);
        vec3 refl = mix(sky(R) * uRadScale * uSkyMul * 1.6, irr, r * r);
        vec3 amb = (vec3(1.0) - F) * (1.0 - m) * irr * albedo + refl * F;
        c = agx((diff + spec + amb) * uExposure);
    } else {
        // ---- studio view (colours by game part, the original's ghost) ----
        vec3 b = pow(clamp(base, 0.0, 1.0), vec3(2.2));
        vec3 L1 = normalize(-0.45 * uR + 0.75 * uU - 0.55 * uF);
        vec3 L2 = normalize(0.70 * uR + 0.15 * uU - 0.40 * uF);
        vec3 L3 = normalize(0.10 * uR + 0.30 * uU + 0.90 * uF);
        float hemi = mix(0.10, 0.28, dot(n, uU) * 0.5 + 0.5);
        float d = hemi + 0.80 * max(dot(n, L1), 0.0) + 0.30 * max(dot(n, L2), 0.0) + 0.25 * max(dot(n, L3), 0.0);
        vec3 Hs = normalize(L1 + V);
        float s = pow(max(dot(n, Hs), 0.0), 48.0) * 0.18;
        c = pow(b * d + vec3(s), vec3(1.0 / 2.2));
    }
    if (vSel.x > 0.5) c = mix(c, vec3(1.0, 0.84, 0.04), 0.45);
    c *= vSel.y;
    frag = vec4(c, uAlpha);
}
"""

ATTRS = ('aPos', 'aNrm', 'aUV', 'aCol', 'aTex', 'aPid', 'aRM')


def render_settings(game_root):
    """The game's own lighting / tonemap settings, read live from <game>/design/Render.zon (never copied).
    Falls back to neutral values when the game isn't found."""
    import os, re
    s = {'light_color': (1.0, 0.95, 0.85), 'skybox_color': (1.0, 1.0, 1.0), 'skybox_radiance_scale': 1.5,
         'skylight_multiplier': 0.5, 'agx_look_slope': (1.0, 1.0, 1.0), 'agx_look_offset': (0.0, 0.0, 0.0),
         'agx_look_power': (1.0, 1.0, 1.0), 'agx_look_saturation': 1.0, 'key': 0.3, 'found': False}
    try:
        txt = open(os.path.join(game_root, 'design', 'Render.zon'), encoding='utf-8', errors='replace').read()
    except (OSError, TypeError):
        return s
    for k in list(s):
        m = re.search(r'\.' + k + r'\s*=\s*\.\{\s*\.x\s*=\s*([-\d.e]+)\s*,\s*\.y\s*=\s*([-\d.e]+)\s*,\s*\.z\s*=\s*([-\d.e]+)', txt)
        if m: s[k] = tuple(float(v) for v in m.groups()); continue
        m = re.search(r'\.' + k + r'\s*=\s*([-\d.e]+)', txt)
        if m: s[k] = float(m.group(1))
    s['found'] = True
    return s


def available():
    """True if the GPU view's libraries import (the driver itself is only tested when the view opens)."""
    return True


class GLView(OpenGLFrame):
    def tkMap(self, evt):
        # Tk 9 (Python 3.14) creates the native window lazily: make sure it exists before the GL context
        try: self.update_idletasks()
        except Exception: pass
        try:
            super().tkMap(evt)
        except Exception as e:                  # no GL context: report instead of failing silently
            self.failed = '%s: %s' % (type(e).__name__, e)
            print('GL view unavailable:', self.failed, file=sys.stderr)

    def __init__(self, master, editor, mesh_path, **kw):
        super().__init__(master, **kw)
        self.ed = editor
        self.mesh_path = mesh_path
        self.ready = False
        self.failed = None
        self._pending = None
        self._fbo = None
        self.width = self.height = 1
        self.unbind('<Configure>')
        self.bind('<Configure>', self._on_resize, add='+')

    # ------------------------------------------------------------ setup
    def initgl(self):
        if self.ready or self.failed: return
        try:
            self._gl_setup()
            self.ready = True
        except Exception as e:                  # no usable OpenGL 3: the editor switches to its software view
            self.failed = '%s: %s' % (type(e).__name__, e)
            print('GL view unavailable:', self.failed, file=sys.stderr)
            self.after_idle(lambda: self.ed.gl_failed(self.failed))

    def _on_resize(self, e):
        self.width, self.height = max(1, e.width), max(1, e.height)
        self._free_fbo()
        self.request()

    def _gl_setup(self):
        ver = GL.glGetString(GL.GL_VERSION) or b'0'
        major = int(ver.split(b'.')[0].split()[-1]) if ver[:1].isdigit() else 0
        if major < 3: raise RuntimeError('OpenGL 3 needed, this driver has %s' % ver.decode(errors='replace'))
        self.prog = self._program(VS, FS)
        self.loc = {n: GL.glGetUniformLocation(self.prog, n) for n in
                    ('uVP', 'uM', 'uRef', 'uState', 'uAtlas', 'uUseTex', 'uIdPass', 'uGhost', 'uGhostCol', 'uAlpha',
                     'uR', 'uU', 'uF', 'uGame', 'uSunDir', 'uSunCol', 'uSkyCol', 'uSkyMul', 'uRadScale', 'uExposure',
                     'uSlope', 'uOffset', 'uPower', 'uSat')}
        self.rs = render_settings(getattr(self.ed, 'game_root', None))
        z = np.load(self.mesh_path)
        self.has_mine = 'P' in z.files
        self.mine = self._upload(z['P'], z['N'], z['U'], z['C'], z['K'], z['I'].astype(np.float32), z['T'],
                                 z['RM'] if 'RM' in z.files else np.tile(np.float32([0.55, 0.0]), (len(z['P']), 1))) \
            if self.has_mine else None
        self.atlas = None
        if 'atlas' in z.files:
            a = np.ascontiguousarray(z['atlas'])
            self.atlas = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.atlas)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, a.shape[1], a.shape[0], 0, GL.GL_RGBA,
                            GL.GL_UNSIGNED_BYTE, a)
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        # the original's meshes, one draw range per node
        self.ref = None; self.ref_ranges = []
        if 'rP' in z.files:
            rT = z['rT']; rI = z['rI']
            self.ref = self._upload(z['rP'], z['rN'], np.zeros((len(z['rP']), 2), np.float32), z['rC'],
                                    np.zeros(len(z['rP']), np.uint8), np.zeros(len(z['rP']), np.float32), rT,
                                    z['rRM'] if 'rRM' in z.files else np.tile(np.float32([1.0, 0.0]), (len(z['rP']), 1)))
            tri_node = rI[rT[::3]]
            names = [str(s) for s in z['rNames']]
            starts = np.searchsorted(tri_node, np.arange(len(names) + 1))
            self.ref_ranges = [(names[k], int(starts[k]) * 3, int(starts[k + 1] - starts[k]) * 3)
                               for k in range(len(names)) if starts[k + 1] > starts[k]]
        pts = [z['P']] if self.has_mine else []
        if 'rP' in z.files: pts.append(z['rP'])
        allp = np.concatenate(pts) if pts else np.zeros((1, 3), np.float32)
        self.depth_range = float(np.linalg.norm(allp.max(0) - allp.min(0))) * 2 + 1e-3
        self.state_tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.state_tex)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        self.samples = 4

    def _program(self, vs, fs):
        def sh(kind, src):
            s = GL.glCreateShader(kind); GL.glShaderSource(s, src); GL.glCompileShader(s)
            if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
                raise RuntimeError(GL.glGetShaderInfoLog(s).decode(errors='replace'))
            return s
        p = GL.glCreateProgram()
        GL.glAttachShader(p, sh(GL.GL_VERTEX_SHADER, vs)); GL.glAttachShader(p, sh(GL.GL_FRAGMENT_SHADER, fs))
        for i, n in enumerate(ATTRS): GL.glBindAttribLocation(p, i, n)
        GL.glBindFragDataLocation(p, 0, 'frag')
        GL.glLinkProgram(p)
        if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(p).decode(errors='replace'))
        return p

    def _upload(self, P, N, U, C, K, I, T, RM):
        def buf(a, target=GL.GL_ARRAY_BUFFER):
            a = np.ascontiguousarray(a); b = GL.glGenBuffers(1)
            GL.glBindBuffer(target, b); GL.glBufferData(target, a.nbytes, a, GL.GL_STATIC_DRAW)
            return b
        return {'bufs': [(buf(P.astype(np.float32)), 3, GL.GL_FLOAT, False),
                         (buf(N.astype(np.float32)), 3, GL.GL_FLOAT, False),
                         (buf(U.astype(np.float32)), 2, GL.GL_FLOAT, False),
                         (buf(C.astype(np.uint8)), 3, GL.GL_UNSIGNED_BYTE, True),
                         (buf(K.astype(np.float32)), 1, GL.GL_FLOAT, False),
                         (buf(I.astype(np.float32)), 1, GL.GL_FLOAT, False),
                         (buf(RM.astype(np.float32)), 2, GL.GL_FLOAT, False)],
                'ibo': buf(T.astype(np.uint32), GL.GL_ELEMENT_ARRAY_BUFFER), 'n': int(len(T))}

    def _bind_mesh(self, m):
        for i, (b, size, typ, norm) in enumerate(m['bufs']):
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, b)
            GL.glEnableVertexAttribArray(i)
            GL.glVertexAttribPointer(i, size, typ, GL.GL_TRUE if norm else GL.GL_FALSE, 0, None)
        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, m['ibo'])

    # ------------------------------------------------------------ frame buffers (anti-aliasing)
    def _free_fbo(self):
        if self._fbo and self.ready:
            try:
                self.tkMakeCurrent()
                GL.glDeleteFramebuffers(1, [self._fbo[0]]); GL.glDeleteRenderbuffers(2, self._fbo[1:3])
            except Exception:
                pass
        self._fbo = None

    def _make_fbo(self, W, H, samples):
        fb = GL.glGenFramebuffers(1); GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
        rc, rd = GL.glGenRenderbuffers(2)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rc)
        GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, samples, GL.GL_RGBA8, W, H)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_RENDERBUFFER, rc)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rd)
        GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, samples, GL.GL_DEPTH_COMPONENT24, W, H)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, rd)
        ok = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) == GL.GL_FRAMEBUFFER_COMPLETE
        return (fb, rc, rd) if ok else None

    # ------------------------------------------------------------ drawing
    def request(self):
        """Redraw soon (several requests in a row become one frame)."""
        if self._pending is None and self.winfo_ismapped():
            self._pending = self.after_idle(self._frame)

    def _frame(self):
        self._pending = None
        if self.ready:
            self._display()

    def _vp(self, W, H):
        ed = self.ed
        right, up, fwd = (np.asarray(v, np.float64) for v in ed.basis())
        c = np.asarray(ed.center, np.float64)
        sx, sy, D = 2 * ed.zoom / W, 2 * ed.zoom / H, self.depth_range
        M = np.zeros((4, 4))
        M[0, :3] = right * sx; M[0, 3] = -(right @ c) * sx - ed.pan[0] * sx
        M[1, :3] = up * sy;    M[1, 3] = -(up @ c) * sy - ed.pan[1] * sy
        M[2, :3] = fwd / D;    M[2, 3] = -(fwd @ c) / D
        M[3, 3] = 1
        return M, right, up, fwd

    def _piece_state(self):
        """Per-piece matrix, colour override and flags for the vertex shader."""
        ed = self.ed
        n = len(ed.pieces)
        rows = max(1, (n + PER_ROW - 1) // PER_ROW)
        S = np.zeros((rows, PER_ROW * 8, 4), np.float32)
        textured = ed.use_ingame()
        for i, p in enumerate(ed.pieces):
            y, x = divmod(i, PER_ROW); x *= 8
            S[y, x:x + 4] = np.asarray(ed.piece_matrix(p), np.float32)
            if textured:
                g = ed.mapping['paint'].get(p['key'])
                if g in ed.cat:
                    S[y, x + 4] = [*(c / 255.0 for c in _rgb(ed.cat[g]['color'])), 1]
                    S[y, x + 6] = [ed.cat[g].get('roughness', 0.5), ed.cat[g].get('metallic', 0.0), 1, 0]
            else:
                S[y, x + 4] = [*(c / 255.0 for c in ed.piece_color_plain(i, p)), 1]
            S[y, x + 5] = [1.0 if i in ed.sel_pieces else 0.0, ed.dim(i, p), 1.0 if ed.visible(p) else 0.0, 0]
        return S

    def _draw_scene(self, W, H, id_pass=False):
        ed = self.ed
        VP, right, up, fwd = self._vp(W, H)
        GL.glUseProgram(self.prog)
        L = self.loc
        GL.glUniformMatrix4fv(L['uVP'], 1, GL.GL_TRUE, VP.astype(np.float32))
        GL.glUniformMatrix4fv(L['uM'], 1, GL.GL_TRUE, np.eye(4, dtype=np.float32))
        for k, v in (('uR', right), ('uU', up), ('uF', fwd)): GL.glUniform3f(L[k], *map(float, v))
        GL.glUniform1i(L['uIdPass'], 1 if id_pass else 0)
        GL.glUniform1i(L['uGhost'], 0); GL.glUniform1f(L['uAlpha'], 1.0)
        GL.glActiveTexture(GL.GL_TEXTURE0); GL.glBindTexture(GL.GL_TEXTURE_2D, self.state_tex)
        S = self._piece_state()
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA32F, S.shape[1], S.shape[0], 0, GL.GL_RGBA, GL.GL_FLOAT, S)
        GL.glUniform1i(L['uState'], 0)
        GL.glActiveTexture(GL.GL_TEXTURE1); GL.glBindTexture(GL.GL_TEXTURE_2D, self.atlas or 0)
        GL.glUniform1i(L['uAtlas'], 1)
        game = ed.use_ingame()
        GL.glUniform1i(L['uUseTex'], 1 if (self.atlas and game) else 0)
        GL.glUniform1i(L['uGame'], 1 if game else 0)
        rs = self.rs
        sun = np.array([0.35, 0.78, 0.52]); sun /= np.linalg.norm(sun)     # high sun, in the bike's world
        GL.glUniform3f(L['uSunDir'], *map(float, sun))
        GL.glUniform3f(L['uSunCol'], *map(float, rs['light_color'][:3]))
        GL.glUniform3f(L['uSkyCol'], *map(float, rs['skybox_color'][:3]))
        GL.glUniform1f(L['uSkyMul'], float(rs['skylight_multiplier']))
        GL.glUniform1f(L['uRadScale'], float(rs['skybox_radiance_scale']))
        GL.glUniform1f(L['uExposure'], float(rs['key']) / 0.3 * 0.62 * getattr(ed, 'exposure', 1.0))
        GL.glUniform3f(L['uSlope'], *map(float, rs['agx_look_slope'][:3]))
        GL.glUniform3f(L['uOffset'], *map(float, rs['agx_look_offset'][:3]))
        GL.glUniform3f(L['uPower'], *map(float, rs['agx_look_power'][:3]))
        GL.glUniform1f(L['uSat'], float(rs['agx_look_saturation']))
        GL.glEnable(GL.GL_DEPTH_TEST); GL.glDepthFunc(GL.GL_LEQUAL); GL.glDisable(GL.GL_CULL_FACE)
        # your model
        if self.mine:
            GL.glUniform1i(L['uRef'], 0)
            self._bind_mesh(self.mine)
            GL.glDrawElements(GL.GL_TRIANGLES, self.mine['n'], GL.GL_UNSIGNED_INT, None)
        if id_pass or not self.ref: return
        # the original: kept parts solid in their own colours, the rest as a see-through ghost
        GL.glUniform1i(L['uRef'], 1)
        self._bind_mesh(self.ref)
        GL.glDisableVertexAttribArray(4); GL.glVertexAttrib1f(4, 0.0)
        GL.glDisableVertexAttribArray(5); GL.glVertexAttrib1f(5, 0.0)
        GL.glUniform1i(L['uGame'], 1 if game else 0)
        ghosts = []
        for name, start, count in self.ref_ranges:
            unit = ed.ref_unit.get(name)
            kept = unit is not None and ed.unit(unit).get('keep_original')
            if kept:
                GL.glDrawElements(GL.GL_TRIANGLES, count, GL.GL_UNSIGNED_INT, GL.ctypes.c_void_p(start * 4))
            elif ed.show_ref.get() and ed.ghost_visible(unit):
                ghosts.append((start, count))
        if ghosts:
            on_top = ed.ref_on_top.get()
            GL.glUniform1i(L['uGhost'], 1); GL.glUniform3f(L['uGhostCol'], 0.62, 0.68, 0.78)
            GL.glUniform1f(L['uAlpha'], 0.55 if on_top else 0.32)
            GL.glEnable(GL.GL_BLEND); GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
            GL.glDepthMask(GL.GL_FALSE)
            if on_top: GL.glDisable(GL.GL_DEPTH_TEST)
            for start, count in ghosts:
                GL.glDrawElements(GL.GL_TRIANGLES, count, GL.GL_UNSIGNED_INT, GL.ctypes.c_void_p(start * 4))
            GL.glDepthMask(GL.GL_TRUE); GL.glDisable(GL.GL_BLEND); GL.glEnable(GL.GL_DEPTH_TEST)
            GL.glUniform1i(L['uGhost'], 0); GL.glUniform1f(L['uAlpha'], 1.0)
        for i in range(7): GL.glEnableVertexAttribArray(i)

    def _background(self):
        GL.glUseProgram(0)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glMatrixMode(GL.GL_PROJECTION); GL.glLoadIdentity()
        GL.glMatrixMode(GL.GL_MODELVIEW); GL.glLoadIdentity()
        GL.glBegin(GL.GL_QUADS)                  # Blender-style vertical gradient
        GL.glColor3f(0.165, 0.170, 0.180); GL.glVertex2f(-1, -1); GL.glVertex2f(1, -1)
        GL.glColor3f(0.270, 0.280, 0.300); GL.glVertex2f(1, 1); GL.glVertex2f(-1, 1)
        GL.glEnd()

    def _overlay(self, W, H):
        ed = self.ed
        GL.glUseProgram(0)
        for i in range(7): GL.glDisableVertexAttribArray(i)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glMatrixMode(GL.GL_PROJECTION); GL.glLoadIdentity(); GL.glOrtho(0, W, H, 0, -1, 1)
        GL.glMatrixMode(GL.GL_MODELVIEW); GL.glLoadIdentity()
        GL.glEnable(GL.GL_BLEND); GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glEnable(GL.GL_POINT_SMOOTH); GL.glEnable(GL.GL_LINE_SMOOTH)
        if ed.show_help.get() and ed.helpers:
            sx, sy, _ = ed.project_np(np.asarray([h['pos'] for h in ed.helpers], np.float64))
            GL.glPointSize(7); GL.glColor3f(0.91, 0.77, 0.28)
            GL.glBegin(GL.GL_POINTS)
            for x, y in zip(sx, sy): GL.glVertex2f(x, y)
            GL.glEnd()
        for pv in ed.overlay_pivots():
            sx, sy, _ = ed.project_np(np.asarray([pv], np.float64))
            GL.glLineWidth(2); GL.glColor3f(1, 1, 1)
            GL.glBegin(GL.GL_LINE_LOOP)
            for k in range(24):
                a = k * math.pi / 12; GL.glVertex2f(sx[0] + 7 * math.cos(a), sy[0] + 7 * math.sin(a))
            GL.glEnd()
            GL.glBegin(GL.GL_LINES)
            GL.glVertex2f(sx[0] - 11, sy[0]); GL.glVertex2f(sx[0] + 11, sy[0])
            GL.glVertex2f(sx[0], sy[0] - 11); GL.glVertex2f(sx[0], sy[0] + 11)
            GL.glEnd()
        right, up, fwd = ed.basis()                 # axis gizmo
        ox, oy, Lg = 44, H - 44, 28
        GL.glLineWidth(2.5)
        GL.glBegin(GL.GL_LINES)
        for vec, col in (((1, 0, 0), (0.91, 0.30, 0.24)), ((0, 1, 0), (0.18, 0.80, 0.44)), ((0, 0, 1), (0.20, 0.60, 0.86))):
            dx = sum(vec[i] * right[i] for i in range(3)) * Lg; dy = -sum(vec[i] * up[i] for i in range(3)) * Lg
            GL.glColor3f(*col); GL.glVertex2f(ox, oy); GL.glVertex2f(ox + dx, oy + dy)
        GL.glEnd()
        box = getattr(ed, '_box', None)
        if box:
            x0, y0, x1, y1 = box
            GL.glLineWidth(1.5); GL.glColor4f(1.0, 0.84, 0.04, 1.0)
            GL.glEnable(GL.GL_LINE_STIPPLE); GL.glLineStipple(1, 0xF0F0)
            GL.glBegin(GL.GL_LINE_LOOP)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)): GL.glVertex2f(x, y)
            GL.glEnd(); GL.glDisable(GL.GL_LINE_STIPPLE)
        GL.glDisable(GL.GL_BLEND)

    def redraw(self):
        if not self.ready or self.ed.zoom is None: return
        W, H = self.winfo_width(), self.winfo_height()
        if W < 2 or H < 2: return
        if self._fbo is None and self.samples:
            try:
                self._fbo = self._make_fbo(W, H, self.samples) or False
            except Exception:
                self._fbo = False
            if not self._fbo: self.samples = 0                         # no anti-aliasing on this driver
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        if self._fbo:
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo[0])
        GL.glViewport(0, 0, W, H)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        self._background()
        GL.glClear(GL.GL_DEPTH_BUFFER_BIT)
        self._draw_scene(W, H)
        if self._fbo:
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._fbo[0])
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, 0)
            GL.glBlitFramebuffer(0, 0, W, H, 0, 0, W, H, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        self._overlay(W, H)

    # ------------------------------------------------------------ picking
    def pick_buffer(self):
        """Piece id under every pixel (-1 = none), H x W, top row first."""
        W, H = self.winfo_width(), self.winfo_height()
        if not self.ready: return np.full((H, W), -1, np.int64)
        self.tkMakeCurrent()
        fb = GL.glGenFramebuffers(1); GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
        rc, rd = GL.glGenRenderbuffers(2)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rc); GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_RGBA8, W, H)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_RENDERBUFFER, rc)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rd)
        GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_DEPTH_COMPONENT24, W, H)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, rd)
        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0, 0, 0, 1); GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        self._draw_scene(W, H, id_pass=True)
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        raw = GL.glReadPixels(0, 0, W, H, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glDeleteFramebuffers(1, [fb]); GL.glDeleteRenderbuffers(2, [rc, rd])
        a = np.frombuffer(raw, np.uint8).reshape(H, W, 4)[::-1].astype(np.int64)
        return (a[..., 0] | (a[..., 1] << 8) | (a[..., 2] << 16)) - 1

    def grab_frame(self):
        """The current frame as an RGB array (for tests / screenshots)."""
        W, H = self.winfo_width(), self.winfo_height()
        self.tkMakeCurrent(); self.redraw()
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        GL.glReadBuffer(GL.GL_BACK)
        raw = GL.glReadPixels(0, 0, W, H, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        self.tkSwapBuffers()
        return np.frombuffer(raw, np.uint8).reshape(H, W, 3)[::-1]


def _rgb(c):
    return tuple(int(max(0, min(1, x)) * 255) for x in c[:3])

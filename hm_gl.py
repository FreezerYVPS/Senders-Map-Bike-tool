"""
hm_gl.py - the heightmap studio's live 3D view (OpenGL via pyopengltk): the heightmap as a lit terrain
mesh with the surface colours, optional slope shading and 10 m grid, and a display-only height
exaggeration. Left-drag orbits, right/middle-drag pans, wheel zooms, double-click centres on that spot.
"""
import math
import sys

import numpy as np
from OpenGL import GL
from pyopengltk import OpenGLFrame

VS = """
#version 130
in vec2 aUV;
uniform sampler2D uH; uniform mat4 uVP; uniform float uSize; uniform float uExag; uniform float uTexel;
uniform float uMid; uniform vec2 uOrigin;
out vec2 vUV; out vec3 vN; out vec3 vW;
void main() {
    float h = (texture(uH, aUV).r - uMid) * uExag;
    float hl = texture(uH, aUV - vec2(uTexel, 0.0)).r, hr = texture(uH, aUV + vec2(uTexel, 0.0)).r;
    float hd = texture(uH, aUV - vec2(0.0, uTexel)).r, hu = texture(uH, aUV + vec2(0.0, uTexel)).r;
    float step = uTexel * uSize * 2.0;
    vN = normalize(vec3(-(hr - hl) * uExag / step, 1.0, -(hu - hd) * uExag / step));
    vec3 w = vec3(uOrigin.x + aUV.x * uSize, h, uOrigin.y + aUV.y * uSize);
    vW = w; vUV = aUV;
    gl_Position = uVP * vec4(w, 1.0);
}
"""

FS = """
#version 130
in vec2 vUV; in vec3 vN; in vec3 vW;
uniform sampler2D uC; uniform int uSlope; uniform int uGrid; uniform float uExag; uniform vec3 uEye;
uniform vec4 uClip; uniform int uUseClip;
out vec4 frag;
void main() {
    if (uUseClip == 1 && vW.x > uClip.x && vW.x < uClip.z && vW.z > uClip.y && vW.z < uClip.w) discard;
    vec3 n = normalize(vN);
    vec3 col = pow(texture(uC, vUV).rgb, vec3(2.2));
    if (uSlope == 1) {
        float sl = degrees(acos(clamp(normalize(vec3(n.x * uExag, n.y, n.z * uExag)).y, 0.0, 1.0)));
        float sl2 = degrees(atan(length(n.xz) / max(n.y, 1e-4) / max(uExag, 1e-4)));
        float k = clamp((sl2 - 25.0) / 25.0, 0.0, 1.0);
        col = mix(col, vec3(0.75, 0.12, 0.06), k * 0.6);
    }
    vec3 sun = normalize(vec3(-0.45, 0.8, -0.35));
    float d = max(dot(n, sun), 0.0);
    float sky = 0.5 + 0.5 * n.y;
    vec3 c = col * (vec3(1.0, 0.96, 0.88) * d * 1.25 + vec3(0.45, 0.55, 0.7) * sky * 0.55);
    if (uGrid == 1) {
        vec2 g = abs(fract(vW.xz / 10.0 + 0.5) - 0.5) / fwidth(vW.xz / 10.0);
        float line = 1.0 - min(min(g.x, g.y), 1.0);
        c = mix(c, vec3(0.9), line * 0.25);
    }
    float fog = clamp(length(vW - uEye) / 2500.0, 0.0, 0.6);
    c = mix(c, vec3(0.62, 0.70, 0.80), fog);
    frag = vec4(pow(c, vec3(1.0 / 2.2)), 1.0);
}
"""


class TerrainGL(OpenGLFrame):
    def tkMap(self, evt):
        # Tk 9 (Python 3.14) creates the native window lazily: make sure it exists before the GL context
        try: self.update_idletasks()
        except Exception: pass
        try:
            super().tkMap(evt)
        except Exception as e:                  # no GL context: report instead of failing silently
            self.failed = '%s: %s' % (type(e).__name__, e)
            print('GL view unavailable:', self.failed, file=sys.stderr)

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.ready = False; self.failed = None
        self._pending = None; self._fbo = None
        self.H = None; self.rgb = None; self.size = 1.0; self._dirty_h = self._dirty_c = False
        self.mesh = None; self.mesh_m = 0
        self.yaw, self.pitch, self.dist = -0.6, 0.55, 300.0
        self.target = np.array([0.0, 0.0, 0.0]); self.exag = 1.0; self.slope = True; self.grid = False
        self.mid = 0.0
        self.mirror = True          # the game's world is shown like the game shows it (a mirror of plain GL)
        self.ctx = None; self._dirty_ctx = False; self.cmesh = None   # surrounding ground (heights, rgb, x0, z0, size)
        self.unbind('<Configure>')
        self.bind('<Configure>', self._on_resize, add='+')
        self.bind('<ButtonPress-1>', self._down); self.bind('<B1-Motion>', self._orbit)
        for b in ('<ButtonPress-2>', '<ButtonPress-3>'): self.bind(b, self._down)
        for b in ('<B2-Motion>', '<B3-Motion>'): self.bind(b, self._pan)
        self.bind('<MouseWheel>', lambda e: self.zoom(0.88 if e.delta > 0 else 1 / 0.88))
        self.bind('<Button-4>', lambda e: self.zoom(0.88)); self.bind('<Button-5>', lambda e: self.zoom(1 / 0.88))
        self.bind('<Double-Button-1>', self._focus)
        self._last = (0, 0)

    # ------------------------------------------------------------ data
    def set_data(self, H, rgb, size, reset_view=False):
        first = self.H is None or reset_view or abs(size - self.size) > 1e-6
        self.H = np.ascontiguousarray(H, np.float32); self.rgb = np.ascontiguousarray(rgb[..., :3], np.uint8)
        self.size = float(size)
        if first:
            self.mid = float(np.median(self.H))
            self.target = np.array([self.size / 2, 0.0, self.size / 2]); self.dist = self.size * 1.1
        self._dirty_h = self._dirty_c = True
        self.request()

    def set_context(self, Hc, rgbc, x0, z0, size):
        """The ground around the edited area (coordinates relative to the area's corner), drawn around it."""
        self.ctx = (np.ascontiguousarray(Hc, np.float32), np.ascontiguousarray(rgbc[..., :3], np.uint8), float(x0), float(z0), float(size))
        self._dirty_ctx = True; self.request()

    def update_region(self, H, rgb):
        self.H = H; self.rgb = rgb; self._dirty_h = self._dirty_c = True; self.request()

    # ------------------------------------------------------------ setup
    def initgl(self):
        if self.ready or self.failed: return
        try:
            ver = GL.glGetString(GL.GL_VERSION) or b'0'
            major = int(ver.split(b'.')[0].split()[-1]) if ver[:1].isdigit() else 0
            if major < 3: raise RuntimeError('OpenGL 3 needed')
            self.prog = self._program(VS, FS)
            self.loc = {n: GL.glGetUniformLocation(self.prog, n) for n in
                        ('uH', 'uVP', 'uSize', 'uExag', 'uTexel', 'uC', 'uSlope', 'uGrid', 'uEye', 'uMid', 'uOrigin', 'uClip', 'uUseClip')}
            self.cth, self.ctc = GL.glGenTextures(2)
            for t in (self.cth, self.ctc):
                GL.glBindTexture(GL.GL_TEXTURE_2D, t)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            self.th, self.tc = GL.glGenTextures(2)
            for t in (self.th, self.tc):
                GL.glBindTexture(GL.GL_TEXTURE_2D, t)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
                GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            self.ready = True
        except Exception as e:
            self.failed = '%s: %s' % (type(e).__name__, e)
            print('3D view unavailable:', self.failed, file=sys.stderr)

    def _program(self, vs, fs):
        def sh(kind, src):
            s = GL.glCreateShader(kind); GL.glShaderSource(s, src); GL.glCompileShader(s)
            if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
                raise RuntimeError(GL.glGetShaderInfoLog(s).decode(errors='replace'))
            return s
        p = GL.glCreateProgram()
        GL.glAttachShader(p, sh(GL.GL_VERTEX_SHADER, vs)); GL.glAttachShader(p, sh(GL.GL_FRAGMENT_SHADER, fs))
        GL.glBindAttribLocation(p, 0, 'aUV'); GL.glBindFragDataLocation(p, 0, 'frag')
        GL.glLinkProgram(p)
        if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(p).decode(errors='replace'))
        return p

    def _make_mesh(self, m, keep=False):
        u = np.linspace(0, 1, m, dtype=np.float32)
        U, V = np.meshgrid(u, u)
        uv = np.ascontiguousarray(np.stack([U.ravel(), V.ravel()], 1))
        i = np.arange(m * m, dtype=np.uint32).reshape(m, m)
        a = i[:-1, :-1].ravel(); b = i[:-1, 1:].ravel(); c = i[1:, :-1].ravel(); d = i[1:, 1:].ravel()
        T = np.ascontiguousarray(np.stack([a, c, b, b, c, d], 1).ravel())
        if keep:
            vb, ib = GL.glGenBuffers(2)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vb); GL.glBufferData(GL.GL_ARRAY_BUFFER, uv.nbytes, uv, GL.GL_STATIC_DRAW)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ib); GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, T.nbytes, T, GL.GL_STATIC_DRAW)
            return (vb, ib, len(T))
        if self.mesh:
            GL.glDeleteBuffers(2, [self.mesh[0], self.mesh[1]])
        vb, ib = GL.glGenBuffers(2)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vb); GL.glBufferData(GL.GL_ARRAY_BUFFER, uv.nbytes, uv, GL.GL_STATIC_DRAW)
        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ib); GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, T.nbytes, T, GL.GL_STATIC_DRAW)
        self.mesh = (vb, ib, len(T)); self.mesh_m = m

    # ------------------------------------------------------------ view
    def _on_resize(self, e):
        self.width, self.height = max(1, e.width), max(1, e.height)
        self._free_fbo(); self.request()

    def request(self):
        if self._pending is None:
            try:
                if self.winfo_ismapped(): self._pending = self.after_idle(self._frame)
            except Exception:
                pass

    def _frame(self):
        self._pending = None
        if self.ready: self._display()

    def eye(self):
        cp = math.cos(self.pitch)
        d = np.array([math.cos(self.yaw) * cp, math.sin(self.pitch), math.sin(self.yaw) * cp])
        return self.target + d * self.dist

    def _matrix(self, W, H):
        eye = self.eye(); f = self.target - eye; f /= np.linalg.norm(f)
        r = np.cross(f, [0, 1, 0]); r /= max(np.linalg.norm(r), 1e-9); u = np.cross(r, f)
        V = np.eye(4); V[0, :3] = r; V[1, :3] = u; V[2, :3] = -f; V[:3, 3] = -V[:3, :3] @ eye
        near = max(self.dist * 0.01, 0.1); far = self.dist * 4 + self.size * 3
        t = 1 / math.tan(math.radians(50) / 2); a = W / max(H, 1)
        P = np.zeros((4, 4)); P[0, 0] = (-t if self.mirror else t) / a; P[1, 1] = t; P[2, 2] = (far + near) / (near - far)
        P[2, 3] = 2 * far * near / (near - far); P[3, 2] = -1
        return P @ V, eye

    def _down(self, e): self._last = (e.x, e.y)

    def _orbit(self, e):
        dx, dy = e.x - self._last[0], e.y - self._last[1]; self._last = (e.x, e.y)
        if self.mirror: dx = -dx
        self.yaw += dx * 0.008; self.pitch = min(1.55, max(0.03, self.pitch + dy * 0.006)); self.request()

    def _pan(self, e):
        dx, dy = e.x - self._last[0], e.y - self._last[1]; self._last = (e.x, e.y)
        if self.mirror: dx = -dx
        k = self.dist * 0.0018
        f = np.array([-math.cos(self.yaw), 0, -math.sin(self.yaw)]); r = np.array([math.sin(self.yaw), 0, -math.cos(self.yaw)])
        self.target = self.target - r * dx * k + f * dy * k
        self.request()

    def zoom(self, f):
        self.dist = min(self.size * 4, max(2.0, self.dist * f)); self.request()

    def ground_at(self, sx, sy):
        """(x, z) on the terrain under screen point (sx, sy) of this view, or None."""
        if self.H is None: return None
        W, H = self.winfo_width(), self.winfo_height()
        M, eye = self._matrix(W, H)
        Mi = np.linalg.inv(M)
        x = 2 * sx / W - 1; y = 1 - 2 * sy / H
        a = Mi @ [x, y, -1, 1]; b = Mi @ [x, y, 1, 1]; a = a[:3] / a[3]; b = b[:3] / b[3]
        d = (b - a); d /= np.linalg.norm(d)
        n = self.H.shape[0]; px = self.size / (n - 1)
        for t in np.linspace(0, self.dist * 4, 1600):
            p = a + d * t
            if 0 <= p[0] <= self.size and 0 <= p[2] <= self.size:
                if p[1] <= (self.H[int(p[2] / px), int(p[0] / px)] - self.mid) * self.exag: return float(p[0]), float(p[2])
        return None

    def _focus(self, e):
        if self.H is None: return
        W, H = self.winfo_width(), self.winfo_height()
        M, eye = self._matrix(W, H)
        Mi = np.linalg.inv(M)
        x = 2 * e.x / W - 1; y = 1 - 2 * e.y / H
        a = Mi @ [x, y, -1, 1]; b = Mi @ [x, y, 1, 1]; a = a[:3] / a[3]; b = b[:3] / b[3]
        d = (b - a); d /= np.linalg.norm(d)
        n = self.H.shape[0]; px = self.size / (n - 1)
        for t in np.linspace(0, self.dist * 4, 800):           # march the view ray until it hits the ground
            p = a + d * t
            if 0 <= p[0] <= self.size and 0 <= p[2] <= self.size:
                h = (self.H[int(p[2] / px), int(p[0] / px)] - self.mid) * self.exag
                if p[1] <= h:
                    self.target = np.array([p[0], h, p[2]]); self.request(); return

    def look_at(self, x, z):
        if self.H is None: return
        n = self.H.shape[0]; px = self.size / (n - 1)
        h = (float(self.H[min(n - 1, max(0, int(z / px))), min(n - 1, max(0, int(x / px)))]) - self.mid) * self.exag
        self.target = np.array([x, h, z]); self.dist = min(self.dist, 120.0); self.request()

    # ------------------------------------------------------------ drawing
    def _free_fbo(self):
        if self._fbo and self.ready:
            try:
                self.tkMakeCurrent()
                GL.glDeleteFramebuffers(1, [self._fbo[0]]); GL.glDeleteRenderbuffers(2, self._fbo[1:3])
            except Exception:
                pass
        self._fbo = None

    def _make_fbo(self, W, H, samples=4):
        fb = GL.glGenFramebuffers(1); GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fb)
        rc, rd = GL.glGenRenderbuffers(2)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rc)
        GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, samples, GL.GL_RGBA8, W, H)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_RENDERBUFFER, rc)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, rd)
        GL.glRenderbufferStorageMultisample(GL.GL_RENDERBUFFER, samples, GL.GL_DEPTH_COMPONENT24, W, H)
        GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_ATTACHMENT, GL.GL_RENDERBUFFER, rd)
        ok = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) == GL.GL_FRAMEBUFFER_COMPLETE
        return (fb, rc, rd) if ok else False

    def redraw(self):
        if not self.ready: return
        W, Hh = self.winfo_width(), self.winfo_height()
        if W < 2 or Hh < 2: return
        if self._fbo is None:
            try: self._fbo = self._make_fbo(W, Hh)
            except Exception: self._fbo = False
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        if self._fbo: GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo[0])
        GL.glViewport(0, 0, W, Hh)
        GL.glClearColor(0.62, 0.70, 0.80, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if self.H is not None:
            n = self.H.shape[0]
            m = min(n, 1025)
            if self.mesh is None or self.mesh_m != m: self._make_mesh(m)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            if self._dirty_h:
                GL.glBindTexture(GL.GL_TEXTURE_2D, self.th)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_R32F, n, n, 0, GL.GL_RED, GL.GL_FLOAT, self.H)
                self._dirty_h = False
            if self._dirty_c:
                GL.glBindTexture(GL.GL_TEXTURE_2D, self.tc)
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, n, n, 0, GL.GL_RGB, GL.GL_UNSIGNED_BYTE, self.rgb)
                self._dirty_c = False
            M, eye = self._matrix(W, Hh)
            GL.glUseProgram(self.prog); L = self.loc
            GL.glUniformMatrix4fv(L['uVP'], 1, GL.GL_TRUE, M.astype(np.float32))
            GL.glUniform1f(L['uExag'], self.exag); GL.glUniform1f(L['uMid'], self.mid)
            GL.glUniform1i(L['uSlope'], 1 if self.slope else 0); GL.glUniform1i(L['uGrid'], 1 if self.grid else 0)
            GL.glUniform3f(L['uEye'], *map(float, eye))
            GL.glEnable(GL.GL_DEPTH_TEST); GL.glDisable(GL.GL_CULL_FACE)

            def draw(th, tc, mesh, mm, x0, z0, size, clip):
                GL.glUniform1f(L['uSize'], size); GL.glUniform2f(L['uOrigin'], x0, z0)
                GL.glUniform1f(L['uTexel'], 1.0 / max(mm - 1, 1))
                GL.glUniform1i(L['uUseClip'], 1 if clip else 0)
                if clip: GL.glUniform4f(L['uClip'], 0.0, 0.0, self.size, self.size)
                GL.glActiveTexture(GL.GL_TEXTURE0); GL.glBindTexture(GL.GL_TEXTURE_2D, th); GL.glUniform1i(L['uH'], 0)
                GL.glActiveTexture(GL.GL_TEXTURE1); GL.glBindTexture(GL.GL_TEXTURE_2D, tc); GL.glUniform1i(L['uC'], 1)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, mesh[0]); GL.glEnableVertexAttribArray(0)
                GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, mesh[1])
                GL.glDrawElements(GL.GL_TRIANGLES, mesh[2], GL.GL_UNSIGNED_INT, None)
            draw(self.th, self.tc, self.mesh, m, 0.0, 0.0, self.size, False)
            if self.ctx is not None:
                Hc, rgbc, cx0, cz0, csz = self.ctx
                cn = Hc.shape[0]
                if self._dirty_ctx:
                    GL.glBindTexture(GL.GL_TEXTURE_2D, self.cth)
                    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_R32F, cn, cn, 0, GL.GL_RED, GL.GL_FLOAT, Hc)
                    GL.glBindTexture(GL.GL_TEXTURE_2D, self.ctc)
                    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8, cn, cn, 0, GL.GL_RGB, GL.GL_UNSIGNED_BYTE, rgbc)
                    if self.cmesh is None or self.cmesh[3] != cn:
                        mm_ = self._make_mesh(cn, keep=True); self.cmesh = mm_ + (cn,)
                    self._dirty_ctx = False
                draw(self.cth, self.ctc, self.cmesh, cn, cx0, cz0, csz, True)
            GL.glDisableVertexAttribArray(0); GL.glUseProgram(0); GL.glActiveTexture(GL.GL_TEXTURE0)
        if self._fbo:
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._fbo[0]); GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, 0)
            GL.glBlitFramebuffer(0, 0, W, Hh, 0, 0, W, Hh, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)

    def grab_frame(self):
        W, H = self.winfo_width(), self.winfo_height()
        self.tkMakeCurrent(); self.redraw()
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1); GL.glReadBuffer(GL.GL_BACK)
        raw = GL.glReadPixels(0, 0, W, H, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        self.tkSwapBuffers()
        return np.frombuffer(raw, np.uint8).reshape(H, W, 3)[::-1]

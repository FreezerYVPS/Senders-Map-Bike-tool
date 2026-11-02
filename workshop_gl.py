"""
workshop_gl.py - the Bike workshop's 3D view: the imported bike with its painted textures, drawn by the graphics
card (needs PyOpenGL + pyopengltk, like the 3D editor). Orbit / pan / zoom; picking is done by the workshop
(a ray against the triangles), so this only draws.
"""
import math
import sys

import numpy as np
from OpenGL import GL
from pyopengltk import OpenGLFrame

VS = """
#version 130
in vec3 aPos; in vec3 aNrm; in vec2 aUV; in float aSel;
uniform mat4 uVP;
out vec3 vN; out vec2 vUV; out float vSel;
void main() { gl_Position = uVP * vec4(aPos, 1.0); vN = aNrm; vUV = aUV; vSel = aSel; }
"""
FS = """
#version 130
in vec3 vN; in vec2 vUV; in float vSel;
uniform sampler2D uTex; uniform int uUseTex; uniform vec3 uCol; uniform vec3 uR; uniform vec3 uU; uniform vec3 uF;
out vec4 frag;
void main() {
    vec3 base = uUseTex == 1 ? texture(uTex, vUV).rgb : uCol;
    vec3 b = pow(clamp(base, 0.0, 1.0), vec3(2.2));
    vec3 n = normalize(vN); vec3 V = -uF;
    if (dot(n, V) < 0.0) n = -n;
    vec3 L1 = normalize(-0.45 * uR + 0.75 * uU - 0.55 * uF);
    vec3 L2 = normalize(0.70 * uR + 0.15 * uU - 0.40 * uF);
    float d = mix(0.22, 0.40, dot(n, uU) * 0.5 + 0.5) + 0.75 * max(dot(n, L1), 0.0) + 0.30 * max(dot(n, L2), 0.0);
    float s = pow(max(dot(n, normalize(L1 + V)), 0.0), 40.0) * 0.15;
    vec3 c = pow(b * d + vec3(s), vec3(1.0 / 2.2));
    if (vSel > 0.5) c = mix(c, vec3(1.0, 0.84, 0.04), 0.35 + 0.15 * sin(gl_FragCoord.x * 0.35 + gl_FragCoord.y * 0.35));
    frag = vec4(c, 1.0);
}
"""


class WorkshopGL(OpenGLFrame):
    def tkMap(self, evt):
        try: self.update_idletasks()
        except Exception: pass
        try:
            super().tkMap(evt)
        except Exception as e:
            self.failed = '%s: %s' % (type(e).__name__, e)
            print('workshop 3D view unavailable:', self.failed, file=sys.stderr)

    def __init__(self, master, owner, **kw):
        super().__init__(master, **kw)
        self.ws = owner
        self.ready = False; self.failed = None; self._pending = None
        self.meshes = []; self.textures = {}; self._todo_tex = {}; self._todo_mesh = None
        self.unbind('<Configure>')
        self.bind('<Configure>', lambda e: self.request(), add='+')

    def initgl(self):
        if self.ready or self.failed: return
        try:
            ver = GL.glGetString(GL.GL_VERSION) or b'0'
            major = int(ver.split(b'.')[0].split()[-1]) if ver[:1].isdigit() else 0
            if major < 3: raise RuntimeError('OpenGL 3 needed (this PC has %s)' % ver.decode(errors='replace'))

            def sh(kind, src):
                s = GL.glCreateShader(kind); GL.glShaderSource(s, src); GL.glCompileShader(s)
                if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS): raise RuntimeError(GL.glGetShaderInfoLog(s))
                return s
            p = GL.glCreateProgram()
            GL.glAttachShader(p, sh(GL.GL_VERTEX_SHADER, VS)); GL.glAttachShader(p, sh(GL.GL_FRAGMENT_SHADER, FS))
            for i, a in enumerate(('aPos', 'aNrm', 'aUV', 'aSel')): GL.glBindAttribLocation(p, i, a)
            GL.glBindFragDataLocation(p, 0, 'frag'); GL.glLinkProgram(p)
            if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS): raise RuntimeError(GL.glGetProgramInfoLog(p))
            self.prog = p
            self.loc = {n: GL.glGetUniformLocation(p, n) for n in ('uVP', 'uTex', 'uUseTex', 'uCol', 'uR', 'uU', 'uF')}
            self.ready = True
        except Exception as e:
            self.failed = '%s: %s' % (type(e).__name__, e)
            print('workshop 3D view unavailable:', self.failed, file=sys.stderr)
            self.after_idle(lambda: self.ws.gl_failed(self.failed))

    # ------------------------------------------------------------ data (uploaded at the next frame)
    def set_meshes(self, meshes):
        """[{'P', 'N', 'U', 'F', 'mat', 'sel' (per vertex 0/1)}]"""
        self._todo_mesh = meshes; self.request()

    def set_selection(self, sels):
        """Per mesh: per-vertex 0/1 highlight."""
        self._todo_sel = sels; self.request()

    def set_texture(self, mat, img):
        self._todo_tex[mat] = img; self.request()

    def _upload(self):
        if self._todo_mesh is not None:
            for m in self.meshes:
                GL.glDeleteBuffers(len(m['bufs']), [b for b, _ in m['bufs']]); GL.glDeleteBuffers(1, [m['ibo']])
            self.meshes = []
            for m in self._todo_mesh:
                bufs = []
                for a, n in ((m['P'], 3), (m['N'], 3), (m['U'], 2), (m['sel'], 1)):
                    a = np.ascontiguousarray(a, np.float32); b = GL.glGenBuffers(1)
                    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, b); GL.glBufferData(GL.GL_ARRAY_BUFFER, a.nbytes, a, GL.GL_DYNAMIC_DRAW)
                    bufs.append((b, n))
                T = np.ascontiguousarray(m['F'], np.uint32).ravel(); ib = GL.glGenBuffers(1)
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, ib); GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, T.nbytes, T, GL.GL_STATIC_DRAW)
                self.meshes.append({'bufs': bufs, 'ibo': ib, 'n': len(T), 'mat': m['mat'], 'nv': len(m['P'])})
            self._todo_mesh = None
        sels = getattr(self, '_todo_sel', None)
        if sels is not None:
            for m, s in zip(self.meshes, sels):
                if s is None or len(s) != m['nv']: continue
                a = np.ascontiguousarray(s, np.float32)
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, m['bufs'][3][0]); GL.glBufferSubData(GL.GL_ARRAY_BUFFER, 0, a.nbytes, a)
            self._todo_sel = None
        for mat, img in list(self._todo_tex.items()):
            a = np.ascontiguousarray(np.asarray(img.convert('RGBA'), np.uint8))
            t = self.textures.get(mat)
            if t is None:
                t = GL.glGenTextures(1); self.textures[mat] = t
            GL.glBindTexture(GL.GL_TEXTURE_2D, t)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            # image rows run top first; UV v runs up: flip so v = 1 is the top row
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, a.shape[1], a.shape[0], 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE,
                            np.ascontiguousarray(a[::-1]))
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        self._todo_tex.clear()

    # ------------------------------------------------------------ drawing
    def request(self):
        if self._pending is None:
            try:
                if self.winfo_ismapped(): self._pending = self.after_idle(self._frame)
            except Exception:
                pass

    def _frame(self):
        self._pending = None
        if self.ready: self._display()

    def redraw(self):
        if not self.ready: return
        W, H = self.winfo_width(), self.winfo_height()
        if W < 2 or H < 2: return
        self._upload()
        GL.glViewport(0, 0, W, H)
        GL.glClearColor(0.20, 0.22, 0.26, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        cam = self.ws.cam
        right, up, fwd = (np.asarray(v, np.float64) for v in cam.basis())
        c = np.asarray(cam.center, np.float64)
        sx, sy, D = 2 * cam.zoom / W, 2 * cam.zoom / H, cam.depth
        M = np.zeros((4, 4))
        M[0, :3] = right * sx; M[0, 3] = -(right @ c) * sx - cam.pan[0] * sx
        M[1, :3] = up * sy; M[1, 3] = -(up @ c) * sy - cam.pan[1] * sy
        M[2, :3] = fwd / D; M[2, 3] = -(fwd @ c) / D
        M[3, 3] = 1
        GL.glUseProgram(self.prog); L = self.loc
        GL.glUniformMatrix4fv(L['uVP'], 1, GL.GL_TRUE, M.astype(np.float32))
        for k, v in (('uR', right), ('uU', up), ('uF', fwd)): GL.glUniform3f(L[k], *map(float, v))
        GL.glEnable(GL.GL_DEPTH_TEST); GL.glDepthFunc(GL.GL_LEQUAL); GL.glDisable(GL.GL_CULL_FACE)
        GL.glActiveTexture(GL.GL_TEXTURE0); GL.glUniform1i(L['uTex'], 0)
        for m in self.meshes:
            t = self.textures.get(m['mat'])
            GL.glUniform1i(L['uUseTex'], 1 if t else 0)
            GL.glUniform3f(L['uCol'], 0.75, 0.75, 0.75)
            GL.glBindTexture(GL.GL_TEXTURE_2D, t or 0)
            for i, (b, n) in enumerate(m['bufs']):
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, b); GL.glEnableVertexAttribArray(i)
                GL.glVertexAttribPointer(i, n, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, m['ibo'])
            GL.glDrawElements(GL.GL_TRIANGLES, m['n'], GL.GL_UNSIGNED_INT, None)
        for i in range(4): GL.glDisableVertexAttribArray(i)
        GL.glUseProgram(0)
        # 2D overlay: the picked spot and the selected design's box / handles
        spot = self.ws.spot_point()
        gz = self.ws.gizmo() if hasattr(self.ws, 'gizmo') else None
        if spot is None and gz is None: return
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glMatrixMode(GL.GL_PROJECTION); GL.glLoadIdentity(); GL.glOrtho(0, W, H, 0, -1, 1)
        GL.glMatrixMode(GL.GL_MODELVIEW); GL.glLoadIdentity()
        GL.glEnable(GL.GL_LINE_SMOOTH)
        if spot is not None and gz is None:
            q = spot - c
            x = W / 2 + (q @ right - cam.pan[0]) * cam.zoom; y = H / 2 - (q @ up - cam.pan[1]) * cam.zoom
            GL.glLineWidth(2); GL.glColor3f(0.3, 1.0, 0.5)
            GL.glBegin(GL.GL_LINE_LOOP)
            for k in range(20):
                a = k * math.pi / 10; GL.glVertex2f(x + 7 * math.cos(a), y + 7 * math.sin(a))
            GL.glEnd()
        if gz is not None:
            GL.glLineWidth(2); GL.glColor3f(1.0, 0.84, 0.04)
            GL.glEnable(GL.GL_LINE_STIPPLE); GL.glLineStipple(1, 0xF0F0)
            GL.glBegin(GL.GL_LINE_LOOP)
            for x, y in gz['poly']: GL.glVertex2f(x, y)
            GL.glEnd(); GL.glDisable(GL.GL_LINE_STIPPLE)
            GL.glBegin(GL.GL_LINES); GL.glVertex2f(*gz['knob_from']); GL.glVertex2f(*gz['knob']); GL.glEnd()
            for x, y in gz['handles']:
                GL.glColor3f(0, 0, 0); GL.glBegin(GL.GL_QUADS)
                for dx, dy in ((-6, -6), (6, -6), (6, 6), (-6, 6)): GL.glVertex2f(x + dx, y + dy)
                GL.glEnd(); GL.glColor3f(1.0, 0.84, 0.04); GL.glBegin(GL.GL_QUADS)
                for dx, dy in ((-5, -5), (5, -5), (5, 5), (-5, 5)): GL.glVertex2f(x + dx, y + dy)
                GL.glEnd()
            x, y = gz['knob']
            GL.glColor3f(0.3, 0.76, 1.0); GL.glBegin(GL.GL_POLYGON)
            for k in range(20):
                a = k * math.pi / 10; GL.glVertex2f(x + 7 * math.cos(a), y + 7 * math.sin(a))
            GL.glEnd()

    def grab(self):
        W, H = self.winfo_width(), self.winfo_height()
        self.tkMakeCurrent(); GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        raw = GL.glReadPixels(0, 0, W, H, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        return np.frombuffer(raw, np.uint8).reshape(H, W, 3)[::-1]

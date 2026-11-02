"""
autoalign.py - "Auto align" for the bike line-up editor.

Finds four things on your model and on the game's bike, and moves your parts so they meet:
    axles      the front axle (where the fork meets the front tyre) and the rear axle (where the swingarm
               meets the rear tyre): found from your wheels, or - if your model has none (imported MX Simulator /
               MX Bikes bikes) - from the bottom of the fork and the back end of the swingarm
    foot pegs  the widest points low down, just behind and below the swingarm pivot
    handlebars the two ends of the bars (the widest points of the handlebar part)

Then (all in game space: x side, y up, z forward):
    1. the whole model is scaled / pitched / moved so both axles sit on the game's axles
    2. the frame (and everything that rides with it) slides so the pegs sit on the game's peg mounts
    3. the handlebars slide so the grips sit on the game's handlebar mounts
    4. the fork lowers turn about the front axle to point at the (moved) fork tops
    5. the swingarm turns about the rear axle to point at the (moved) swingarm pivot
Only numbers are used (points of your parts, and the game bike's mount points / part positions).
"""
import math

import numpy as np

BODY_SKIP = ('handlebars', 'swingarm', 'front_tire', 'back_tire')


def _ang(v):                                   # angle of a (y, z) direction, as rotated by a turn about x
    return math.atan2(v[1], v[0])


def _yz(p):
    return np.array([p[1], p[2]], np.float64)


def ends(P, frac=0.12):
    """The two ends of a long part along its main direction in the side view: (low end, high end, direction)."""
    P = np.asarray(P, np.float64)
    if len(P) < 6: return None
    Q = P[:, 1:3]; c = Q.mean(0)
    _, _, vt = np.linalg.svd(Q - c, full_matrices=False)
    u = vt[0]
    t = (Q - c) @ u
    lo, hi = np.quantile(t, frac), np.quantile(t, 1 - frac)
    a, b = P[t <= lo].mean(0), P[t >= hi].mean(0)
    xm = float(P[:, 0].min() + P[:, 0].max()) / 2; a[0] = b[0] = xm
    return a, b, u


def wheel_centres(shapes):
    """shapes: [(key, automap._shape dict)] -> (front centre, rear centre) or None."""
    import automap
    pair = automap._wheel_pair(shapes)
    if not pair: return None
    d = dict(shapes)
    a, b = np.array(d[pair[0]]['center']), np.array(d[pair[1]]['center'])
    return (a, b) if a[2] > b[2] else (b, a)


def grips(P):
    """Both handlebar ends (left, right) of the handlebar part's points: the outer fifth of its width."""
    P = np.asarray(P, np.float64)
    if len(P) < 10: return None
    xc = float(P[:, 0].min() + P[:, 0].max()) / 2; X = P[:, 0] - xc
    w = max(-X.min(), X.max())
    if w < 0.12: return None
    L, R = P[X <= -0.8 * w], P[X >= 0.8 * w]
    if len(L) < 2 or len(R) < 2: return None
    return L.mean(0), R.mean(0)


def pegs(P, pivot, rear_axle):
    """Foot pegs: the widest points of the body in a box below / around the swingarm pivot."""
    P = np.asarray(P, np.float64)
    if len(P) < 20 or pivot is None: return None
    lo_y = (rear_axle[1] - 0.12) if rear_axle is not None else pivot[1] - 0.3
    m = (P[:, 1] > lo_y) & (P[:, 1] < pivot[1] + 0.08) & (P[:, 2] > pivot[2] - 0.32) & (P[:, 2] < pivot[2] + 0.12)
    Q = P[m]
    if len(Q) < 10: return None
    xc = float(np.median(P[:, 0])); X = Q[:, 0] - xc
    out = []
    for side in (-1, 1):
        S = Q[X * side > 0.10]
        if len(S) < 2: return None
        d = np.abs(S[:, 0] - xc)
        out.append(S[d >= np.quantile(d, 0.97)].mean(0))     # the peg tips
    return out[0], out[1]


# ---------------------------------------------------------------- the model
def model_features(by_unit, wheel_shapes):
    """by_unit: {unit or None: (n,3) points as now placed}; wheel_shapes: [(key, shape)] of every piece.
    -> dict of points (numpy, game space) and notes."""
    f, notes = {}, []
    w = wheel_centres(wheel_shapes)
    fork = by_unit.get('handlebars.bottom'); arm = by_unit.get('swingarm')
    fe = ends(fork) if fork is not None and len(fork) >= 6 else None
    se = ends(arm) if arm is not None and len(arm) >= 6 else None
    if fe is not None:
        lo, hi = (fe[0], fe[1]) if fe[0][1] < fe[1][1] else (fe[1], fe[0])
        f['fork_axle'], f['fork_top'] = lo, hi
    if se is not None:
        back, front = (se[0], se[1]) if se[0][2] < se[1][2] else (se[1], se[0])
        f['arm_axle'], f['arm_pivot'] = back, front
    if w is not None:
        f['front_axle'], f['rear_axle'] = w
        f['axles_from'] = 'wheels'
        notes.append('axles: from your wheels')
    elif 'fork_axle' in f and 'arm_axle' in f:
        f['front_axle'], f['rear_axle'] = f['fork_axle'], f['arm_axle']
        f['axles_from'] = 'ends'
        notes.append('axles: from the bottom of the fork and the back of the swingarm (no wheels on your model), '
                     "matched to the same spots on the game's fork and swingarm")
    bars = by_unit.get('handlebars.top')
    g = grips(bars) if bars is not None else None
    if g is None:                                   # the widest thing high up
        allp = np.concatenate([p for p in by_unit.values() if p is not None and len(p)]) if by_unit else None
        if allp is not None and len(allp):
            top = allp[allp[:, 1] > np.quantile(allp[:, 1], 0.8)]
            g = grips(top)
    if g is not None: f['bars'] = g
    body = [p for u, p in by_unit.items() if u is not None and p is not None and len(p) and not u.startswith(BODY_SKIP)]
    if body:
        pg = pegs(np.concatenate(body), f.get('arm_pivot'), f.get('rear_axle'))
        if pg is not None: f['pegs'] = pg
    return f, notes


# ---------------------------------------------------------------- the game's bike
def _helper(helpers, *names):
    by = {h['name']: h['pos'] for h in helpers}
    for n in names:
        if n in by: return np.array(by[n], np.float64)
    return None


def game_features(parts, helpers):
    f = {}
    for u, k in (('front_tire', 'front_axle'), ('back_tire', 'rear_axle')):
        R = np.asarray((parts.get(u) or {}).get('ref') or [], np.float64).reshape(-1, 3)
        if len(R): f[k] = (R.min(0) + R.max(0)) / 2
    ref = lambda u: np.asarray((parts.get(u) or {}).get('ref') or [], np.float64).reshape(-1, 3)
    e = ends(ref('handlebars.bottom'))                       # found the same way as on your model
    if e is not None: f['fork_axle'], f['fork_top'] = (e[0], e[1]) if e[0][1] < e[1][1] else (e[1], e[0])
    e = ends(ref('swingarm'))
    if e is not None: f['arm_axle'], f['arm_end_front'] = (e[0], e[1]) if e[0][2] < e[1][2] else (e[1], e[0])
    R = ref('handlebars.top')
    g = grips(R) if len(R) else None
    if g is not None: f['bars'] = g
    for k, names in (('bars', (('mount.handlebars.capsule.end', 'mount.rider_handlebar.left', 'mount.left_handle'),
                                ('mount.handlebars.capsule.start', 'mount.rider_handlebar.right', 'mount.right_handle'))),
                     ('pegs', (('mount.rider_peg.left', 'mount.left_foot_bottom'),
                               ('mount.rider_peg.right', 'mount.right_foot_bottom')))):
        if k in f: continue
        l, r = _helper(helpers, *names[0]), _helper(helpers, *names[1])
        if l is not None and r is not None:
            f[k] = (l, r) if l[0] < r[0] else (r, l)
    piv = (parts.get('swingarm') or {}).get('pivot')
    if piv: f['arm_pivot'] = np.array(piv, np.float64)
    return f


# ---------------------------------------------------------------- solving
def _mid(pair):
    return (np.asarray(pair[0]) + np.asarray(pair[1])) / 2


def _adj(t=(0, 0, 0), rx=0.0, s=1.0, pivot=(0, 0, 0)):
    return {'t': [float(v) for v in t], 'r': [float(rx), 0.0, 0.0], 's': float(s), 'pivot': [float(v) for v in pivot]}


def _apply(a, p):
    """automap.adjust_matrix(a) applied to one point (turns about x only)."""
    th = math.radians(a['r'][0]); c, s_ = math.cos(th), math.sin(th)
    pv = np.asarray(a['pivot']); q = np.asarray(p, np.float64) - pv
    q = a['s'] * np.array([q[0], c * q[1] - s_ * q[2], s_ * q[1] + c * q[2]])
    return pv + np.asarray(a['t']) + q


def solve(m, g, units, wheel_units=()):
    """m: model_features (before any adjustment), g: game_features, units: the game part names.
    -> (adjust dict {key: A}, report lines, marks [(model point, game point, label)])."""
    adj, rep, marks = {}, [], []
    if 'front_axle' not in m or 'rear_axle' not in m or 'front_axle' not in g or 'rear_axle' not in g:
        missing = [k for k in ('front_axle', 'rear_axle') if k not in m]
        raise ValueError('could not find your %s - assign your fork lowers to handlebars.bottom and your swingarm '
                         'to swingarm (or keep wheels on the model), then try again'
                         % ' and '.join(k.replace('_', ' ') for k in missing) if missing else
                         "the game bike's wheels are missing from the reference")
    ends_ok = 'fork_axle' in g and 'arm_axle' in g
    if m.get('axles_from') == 'ends' and not ends_ok:
        raise ValueError("the game bike's fork or swingarm is missing from the reference")
    Fm, Rm = np.asarray(m['front_axle']), np.asarray(m['rear_axle'])
    Fg, Rg = ((np.asarray(g['fork_axle']), np.asarray(g['arm_axle'])) if m.get('axles_from') == 'ends'
              else (np.asarray(g['front_axle']), np.asarray(g['rear_axle'])))
    # whole model: one size and pitch for the best overall match of axles (counted twice), pegs and grips;
    # each part is then put exactly on its spot below
    src, dst, w = [_yz(Fm), _yz(Rm)], [_yz(Fg), _yz(Rg)], [2.0, 2.0]
    for k in ('pegs', 'bars'):
        if k in m and k in g: src.append(_yz(_mid(m[k]))); dst.append(_yz(_mid(g[k]))); w.append(1.0)
    src, dst, w = np.array(src), np.array(dst), np.array(w)[:, None]
    cs, cd = (src * w).sum(0) / w.sum(), (dst * w).sum(0) / w.sum()
    a, b = src - cs, dst - cd
    th_r = math.atan2(float((w[:, 0] * (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])).sum()),
                      float((w[:, 0] * (a * b).sum(1)).sum()))
    c_, s_ = math.cos(th_r), math.sin(th_r)
    Ra = np.stack([c_ * a[:, 0] - s_ * a[:, 1], s_ * a[:, 0] + c_ * a[:, 1]], 1)
    s = float((w[:, 0] * (Ra * b).sum(1)).sum() / max(1e-12, (w[:, 0] * (a * a).sum(1)).sum()))
    th = math.degrees(th_r)
    xm = (Fm[0] + Rm[0]) / 2
    A = _adj(t=[-xm, cd[0] - cs[0], cd[1] - cs[1]], rx=th, s=s, pivot=[xm, cs[0], cs[1]])   # wheels on x = 0
    adj['__all__'] = A
    dm, dg = _yz(Fm) - _yz(Rm), _yz(Fg) - _yz(Rg)
    rep.append('whole model: scale %.1f%%, pitch %+.1f° (wheelbase %.3f m → %.3f m in the game)'
               % (s * 100, th, np.linalg.norm(dm), np.linalg.norm(dg)))
    T = {k: (tuple(_apply(A, p) for p in v) if isinstance(v, tuple) else _apply(A, v)) for k, v in m.items()
         if not isinstance(v, str) and v is not None}

    body = [u for u in units if not u.startswith(BODY_SKIP)]
    d_body = np.zeros(3)
    if 'pegs' in T and 'pegs' in g:
        d_body = _mid(g['pegs']) - _mid(T['pegs']); d_body[0] = 0
        for u in body: adj[u] = _adj(t=d_body)
        marks += [(T['pegs'][i] + d_body, g['pegs'][i], 'peg') for i in (0, 1)]
        rep.append('frame and body: moved %.1f cm up, %.1f cm forward so the foot pegs sit on the game\'s'
                   % (d_body[1] * 100, d_body[2] * 100))
        wid = abs(T['pegs'][1][0] - T['pegs'][0][0]); gw = abs(g['pegs'][1][0] - g['pegs'][0][0])
        if abs(wid - gw) > 0.06: rep.append('   (your pegs are %.0f cm apart, the game\'s %.0f cm)' % (wid * 100, gw * 100))
    else:
        rep.append('foot pegs: not found on your model%s - the frame stays where the axles put it'
                   % ('' if 'pegs' in g else " (or the game bike's peg mounts are missing)"))

    d_bars = np.zeros(3)
    if 'bars' in T and 'bars' in g:
        d_bars = _mid(g['bars']) - _mid(T['bars']); d_bars[0] = 0
        for u in units:
            if u.startswith('handlebars') and u != 'handlebars.bottom': adj[u] = _adj(t=d_bars)
        marks += [(T['bars'][i] + d_bars, g['bars'][i], 'grip') for i in (0, 1)]
        rep.append('handlebars: moved %.1f cm up, %.1f cm forward so the grips sit on the game\'s'
                   % (d_bars[1] * 100, d_bars[2] * 100))
        wid = abs(T['bars'][1][0] - T['bars'][0][0]); gw = abs(g['bars'][1][0] - g['bars'][0][0])
        if abs(wid - gw) > 0.08: rep.append('   (your bars are %.0f cm wide end to end, the game\'s grips %.0f cm apart)'
                                            % (wid * 100, gw * 100))
    else:
        rep.append('handlebars: not found - left where they are')

    # the fork lowers / swingarm: their ends onto the game's same ends (the axle spots), pointing at what they hang from
    Fe = np.asarray(g['fork_axle']) if 'fork_axle' in g else Fg
    Re = np.asarray(g['arm_axle']) if 'arm_axle' in g else Rg
    if 'fork_axle' in T and 'handlebars.bottom' in units:
        a0, top = T['fork_axle'], T['fork_top'] + d_bars
        rx = math.degrees(_ang(_yz(top) - _yz(Fe)) - _ang(_yz(T['fork_top']) - _yz(a0)))
        adj['handlebars.bottom'] = _adj(t=Fe - a0, rx=rx, pivot=a0); adj['handlebars.bottom']['t'][0] = 0.0
        rep.append('fork lowers: on the front axle, turned %+.1f° to meet the fork tops' % rx)
    if 'arm_axle' in T and 'swingarm' in units:
        a0, piv = T['arm_axle'], T['arm_pivot'] + d_body
        rx = math.degrees(_ang(_yz(piv) - _yz(Re)) - _ang(_yz(T['arm_pivot']) - _yz(a0)))
        L0, L1 = np.linalg.norm(_yz(T['arm_pivot']) - _yz(a0)), np.linalg.norm(_yz(piv) - _yz(Re))
        sc = float(np.clip(L1 / max(1e-6, L0), 0.85, 1.2))
        adj['swingarm'] = _adj(t=Re - a0, rx=rx, s=sc, pivot=a0); adj['swingarm']['t'][0] = 0.0
        if abs(sc - 1) > 0.01: rep.append('   (swingarm sized %.0f%% so it reaches from the axle to its pivot)' % (sc * 100))
        rep.append('swingarm: on the rear axle, turned %+.1f° to meet its pivot on the frame' % rx)
        if 'arm_pivot' in g:
            off = np.linalg.norm(_yz(piv) - _yz(g['arm_pivot']))
            if off > 0.05: rep.append("   (its pivot ends up %.0f cm from the game's swingarm pivot)" % (off * 100))
    for u, k in (('front_tire', 'front_axle'), ('back_tire', 'rear_axle')):
        if u in units and k in T and u in wheel_units:
            adj[u] = _adj(t=np.asarray(g[k]) - T[k])
    # where your axles end up after every step (on the part that carries them)
    def final(p, unit):
        q = _apply(A, p)
        return _apply(adj[unit], q) if unit in adj else q
    fu = 'front_tire' if m.get('axles_from') == 'wheels' and 'front_tire' in adj else 'handlebars.bottom'
    ru = 'back_tire' if m.get('axles_from') == 'wheels' and 'back_tire' in adj else 'swingarm'
    if m.get('axles_from') == 'ends':
        marks += [(final(m['fork_axle'], fu), Fe, 'front axle'), (final(m['arm_axle'], ru), Re, 'rear axle')]
    else:
        marks += [(final(m['front_axle'], fu if fu.endswith('tire') else '-'), Fg, 'front axle'),
                  (final(m['rear_axle'], ru if ru.endswith('tire') else '-'), Rg, 'rear axle')]
        if 'fork_axle' in m: marks.append((final(m['fork_axle'], 'handlebars.bottom'), Fe, 'fork end'))
        if 'arm_axle' in m: marks.append((final(m['arm_axle'], 'swingarm'), Re, 'swingarm end'))
    return adj, rep, marks

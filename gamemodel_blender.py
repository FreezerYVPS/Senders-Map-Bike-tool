"""
gamemodel_blender.py - Blender side of the Game Model Exporter.

Exports a Blender scene to the game's .blend.bin using the dev's BlendMeshSpec.zig
as the source of truth (binary layout, node names, node order, texture list).

  Headless (what the GUI runs):
    blender -b model.blend --python gamemodel_blender.py -- \
        --spec BlendMeshSpec.zig --export model.blend.bin [--check] [--json out.json]
        [--model NAME] [--keep-extra] [--force] [--no-modifiers] [--bake-deform] [--embed-linked]
  As an add-on: install gamemodel_addon.zip (File > Export / Import > Game Model).

Conventions recovered from the shipped files (see README):
  Blender (x,y,z) -> game (x, z, -y); transforms are matrix_local after that basis change,
  quaternion sign chosen so w <= 0; meshes split per material slot ("<obj>.material.<mat>",
  "no_material"); vertices split per (vertex, uv[, normal]) in order of first use; uv = (u, 1-v).
"""
bl_info = {
    "name": "Game Model (BlendMeshSpec) Exporter",
    "author": "reverse engineered",
    "version": (2, 0, 0),
    "blender": (3, 6, 0),
    "location": "File > Export / Import > Game Model (.blend.bin)",
    "description": "Export/import the game's BlendMeshSpec model format, validated against BlendMeshSpec.zig",
    "category": "Import-Export",
}

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    from . import zigspec            # installed as add-on package
except ImportError:
    import zigspec                   # run as script / next to it

import bpy
import mathutils
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

TEXTURE_KEYS = ('diffuse_texture', 'normal_texture', 'roughness_metallic_texture', 'baked_material_texture')
DEFER_MODIFIERS = {'LATTICE', 'ARMATURE'}      # Subdivision is applied unless defer_subdiv (the bike never uses it)


# =========================================================================
#  helpers
# =========================================================================
def _v4(x, y, z, w):
    return {'x': x, 'y': y, 'z': z, 'w': w}


def _to_game(co, w=1.0):
    return _v4(co[0], co[2], -co[1], w)


def _C():
    return mathutils.Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))


def _decompose(m):
    C = _C()
    loc, rot, sca = (C @ m @ C.transposed()).decompose()
    q = (rot.x, rot.y, rot.z, rot.w)
    if rot.w > 0:
        q = tuple(-c for c in q)
    return _v4(loc.x, loc.y, loc.z, 1), _v4(*q), _v4(sca.x, sca.y, sca.z, 0)


def _rel(path):
    p = path or ''
    if p.startswith('//'): p = p[2:]
    return p.replace('\\', '/')


def _upstream_image(sock):
    seen, stack = set(), [l.from_node for l in sock.links] if sock.is_linked else []
    while stack:
        node = stack.pop()
        if node in seen: continue
        seen.add(node)
        if node.type == 'TEX_IMAGE' and node.image:
            return node.image
        for inp in node.inputs:
            stack.extend(l.from_node for l in inp.links)
    return None


def _img(img):
    return _rel(img.filepath or img.name) if img else None


_MEAN_CACHE = {}


def _image_mean(img, ch):
    import numpy as np
    k = (img.name, ch)
    if k not in _MEAN_CACHE:
        try:
            w, h = img.size
            a = np.empty(w * h * 4, np.float32); img.pixels.foreach_get(a); a = a.reshape(-1, 4)[::7]
            _MEAN_CACHE[k] = float(a[:, :3].mean() if ch is None else a[:, ch].mean())
        except Exception:
            _MEAN_CACHE[k] = None
    return _MEAN_CACHE[k]


def _socket_value(sock, fallback):
    """A shader input's value. When a texture drives it, the slider underneath is ignored by Blender,
    so use the texture's average instead (or a neutral fallback if that can't be worked out)."""
    if not sock.is_linked: return sock.default_value
    l = sock.links[0]; node, out = l.from_node, l.from_socket
    ch = None
    if node.type in ('SEPARATE_COLOR', 'SEPRGB', 'SEPARATE_RGB'):
        ch = {'Red': 0, 'R': 0, 'Green': 1, 'G': 1, 'Blue': 2, 'B': 2}.get(out.name)
        if not node.inputs[0].is_linked: return fallback
        l = node.inputs[0].links[0]; node, out = l.from_node, l.from_socket
    if node.type == 'TEX_IMAGE' and node.image is not None:
        if out.name == 'Alpha': ch = 3
        v = _image_mean(node.image, ch)
        if v is not None: return v
    return fallback


def material_spec(mat):
    color = list(mat.diffuse_color)
    rough, metal = mat.roughness, mat.metallic
    tex = dict.fromkeys(TEXTURE_KEYS)
    nt = mat.node_tree if getattr(mat, 'use_nodes', True) else None
    if nt:
        bsdf = None
        for node in nt.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                if any(l.to_node.type == 'OUTPUT_MATERIAL' for o in node.outputs for l in o.links):
                    break
        if bsdf:
            color = list(bsdf.inputs['Base Color'].default_value)
            rough = _socket_value(bsdf.inputs['Roughness'], 0.5)
            metal = _socket_value(bsdf.inputs['Metallic'], 0.0)
            tex['diffuse_texture'] = _img(_upstream_image(bsdf.inputs['Base Color']))
            tex['normal_texture'] = _img(_upstream_image(bsdf.inputs['Normal']))
            tex['roughness_metallic_texture'] = _img(_upstream_image(bsdf.inputs['Roughness'])
                                                     or _upstream_image(bsdf.inputs['Metallic']))
        for node in nt.nodes:
            if node.type == 'TEX_IMAGE' and node.image and 'bake' in (node.label + ' ' + node.name).lower():
                tex['baked_material_texture'] = _img(node.image)
    out = {'color': _v4(*color[:4]), 'roughness': float(rough), 'metallic': float(metal)}
    out.update(tex)
    return out


# =========================================================================
#  mesh
# =========================================================================
def _armature_of(obj):
    arm = next((m.object for m in obj.modifiers if m.type == 'ARMATURE' and m.object), None)
    if arm is None and obj.parent and obj.parent.type == 'ARMATURE':
        arm = obj.parent
    return arm


def _corner_normals(me, np, nl):
    out = np.zeros(nl * 3, np.float32)
    if hasattr(me, 'corner_normals'):                 # Blender 4.1+
        me.corner_normals.foreach_get('vector', out)
    else:
        if hasattr(me, 'calc_normals_split'): me.calc_normals_split()
        me.loops.foreach_get('normal', out)
    return out.reshape(-1, 3)


def mesh_nodes(obj, depsgraph, opts, with_normals, log):
    import numpy as np
    subdiv, toggled = 0, []
    for mod in obj.modifiers:
        if mod.type == 'SUBSURF' and mod.show_viewport and opts.get('defer_subdiv'):
            subdiv = max(subdiv, mod.levels)
        disable = (not opts['apply_modifiers']) or (mod.type == 'SUBSURF' and opts.get('defer_subdiv')) or \
                  (mod.type in DEFER_MODIFIERS and opts['defer_deform'])
        if disable and mod.show_viewport:
            mod.show_viewport = False; toggled.append(mod)
    try:
        if toggled: depsgraph.update()
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh()
        try:
            return _split(obj, me, subdiv, with_normals, np, log)
        finally:
            ev.to_mesh_clear()
    finally:
        for mod in toggled: mod.show_viewport = True
        if toggled: depsgraph.update()


def _split(obj, me, subdiv, with_normals, np, log):
    nv, nl, npo = len(me.vertices), len(me.loops), len(me.polygons)
    co = np.empty(nv * 3, np.float32); me.vertices.foreach_get('co', co); co = co.reshape(-1, 3)
    lv = np.empty(nl, np.int32); me.loops.foreach_get('vertex_index', lv)
    ls = np.empty(npo, np.int32); me.polygons.foreach_get('loop_start', ls)
    lt = np.empty(npo, np.int32); me.polygons.foreach_get('loop_total', lt)
    mi = np.empty(npo, np.int32); me.polygons.foreach_get('material_index', mi)
    uvl = _render_uv(me)
    uv = np.zeros(nl * 2, np.float32)
    if uvl: uvl.data.foreach_get('uv', uv)
    elif npo: log('WARN %s has no UV map; UVs written as 0' % obj.name)
    uv = uv.reshape(-1, 2) + np.float32(0.0)
    nrm = _corner_normals(me, np, nl) + np.float32(0.0) if with_normals else None

    slots = obj.material_slots
    nslots = max(1, len(slots))
    mi = np.clip(mi, 0, nslots - 1)

    vg_names = [g.name for g in obj.vertex_groups]
    vgroups = [[(g.group, g.weight) for g in v.groups] for v in me.vertices] if vg_names else None
    bone_of_group = {}
    arm = _armature_of(obj)
    if arm is not None and vg_names:
        bones = {b.name: i for i, b in enumerate(arm.data.bones)}
        bone_of_group = {gi: bones[n] for gi, n in enumerate(vg_names) if n in bones}

    dt = [('v', '<i4'), ('u', '<u4'), ('w', '<u4')]
    if with_normals: dt += [('nx', '<u4'), ('ny', '<u4'), ('nz', '<u4')]
    out = []
    for k in range(nslots):
        polys = np.nonzero(mi == k)[0]
        if len(polys) == 0: continue
        mat = slots[k].material if k < len(slots) else None
        name = obj.name if k == 0 else '%s.material.%s' % (obj.name, mat.name if mat else 'no_material')
        loops = np.concatenate([np.arange(ls[p], ls[p] + lt[p]) for p in polys])
        key = np.empty(len(loops), dtype=dt)
        key['v'] = lv[loops]
        key['u'] = uv[loops, 0].view(np.uint32); key['w'] = uv[loops, 1].view(np.uint32)
        if with_normals:
            key['nx'] = nrm[loops, 0].view(np.uint32); key['ny'] = nrm[loops, 1].view(np.uint32)
            key['nz'] = nrm[loops, 2].view(np.uint32)
        _, first, inv = np.unique(key, return_index=True, return_inverse=True)
        order = np.argsort(first, kind='stable')
        rank = np.empty_like(order); rank[order] = np.arange(len(order))
        new_idx = rank[inv.ravel()]
        src_loop = loops[first[order]]
        src_vert = lv[src_loop]

        cuts = np.cumsum(lt[polys])[:-1]
        mesh = {
            'polygons': [a.tolist() for a in np.split(new_idx, cuts)],
            'positions': [_v4(x, z, -y, 1) for x, y, z in co[src_vert].astype(np.float64).tolist()],
            'uvs': [{'x': u, 'y': 1.0 - v} for u, v in uv[src_loop].astype(np.float64).tolist()],
        }
        if with_normals:
            mesh['normals'] = [_v4(x, z, -y, 0) for x, y, z in nrm[src_loop].astype(np.float64).tolist()]
        bidx, bwt, groups = [], [], []
        if vgroups is not None:
            pts = [([], []) for _ in vg_names]
            for ni, vi in enumerate(src_vert.tolist()):
                for gi, wgt in vgroups[vi]:
                    if gi < len(pts):
                        pts[gi][0].append(ni); pts[gi][1].append(wgt)
            if any(p[0] for p in pts):
                groups = [{'name': n, 'points': p[0], 'weights': p[1]} for n, p in zip(vg_names, pts)]
        for vi in src_vert.tolist():
            inf = []
            if bone_of_group:
                inf = sorted(((w, bone_of_group[g]) for g, w in vgroups[vi] if g in bone_of_group and w > 0),
                             reverse=True)[:3]
            s = sum(w for w, _ in inf) or 1.0
            bidx.append([b for _, b in inf] + [-1] * (3 - len(inf)))
            bwt.append([w / s for w, _ in inf] + [0.0] * (3 - len(inf)))
        mesh.update({'bone_indices': bidx, 'bone_weights': bwt, 'vertex_groups': groups,
                     'subdiv_levels': subdiv, 'costume': None, 'terrain': None})
        out.append((name, mat, mesh))
    return out


# =========================================================================
#  armature / curve
# =========================================================================
def _bone_tf(m):
    p, r, _ = _decompose(m)
    return {'position': p, 'rotation': r}


def armature_spec(obj, scene):
    bones = list(obj.data.bones)
    index = {b.name: i for i, b in enumerate(bones)}
    out_bones = []
    for b in bones:
        m = b.matrix_local if b.parent is None else b.parent.matrix_local.inverted() @ b.matrix_local
        out_bones.append({'name': b.name, 'parent': index[b.parent.name] if b.parent else None,
                          'length': b.length, 'rest': _bone_tf(m)})
    anim, markers = [], []
    act = obj.animation_data.action if obj.animation_data else None
    if act is not None:
        f0, f1 = (int(round(x)) for x in act.frame_range)
        keep = scene.frame_current
        for f in range(f0, f1 + 1):
            scene.frame_set(f)
            frame_bones = []
            for b in bones:
                pb = obj.pose.bones[b.name]
                m = pb.matrix if pb.parent is None else pb.parent.matrix.inverted() @ pb.matrix
                frame_bones.append(_bone_tf(m))
            anim.append({'frame': f, 'bones': frame_bones})
        scene.frame_set(keep)
        markers = [{'name': mk.name, 'frame': mk.frame} for mk in getattr(act, 'pose_markers', [])]
    if not markers:
        markers = [{'name': mk.name, 'frame': mk.frame} for mk in scene.timeline_markers]
    return {'bones': out_bones, 'animation': anim, 'markers': markers}


def curve_spec(obj):
    splines = []
    for s in obj.data.splines:
        if s.type == 'BEZIER':
            pts = [{'point': _to_game(p.co), 'handle_left': _to_game(p.handle_left),
                    'handle_right': _to_game(p.handle_right)} for p in s.bezier_points]
        else:
            pts = [{'point': _to_game(p.co[:3]), 'handle_left': _to_game(p.co[:3]),
                    'handle_right': _to_game(p.co[:3])} for p in s.points]
        splines.append({'bezier_points': pts})
    return {'splines': splines}


# =========================================================================
#  scene -> document
# =========================================================================
def _base_node(obj):
    pos, rot, sca = _decompose(obj.matrix_local)
    return {'type': obj.type, 'parent': obj.parent.name if obj.parent else None,
            'position': pos, 'rotation': rot, 'scale': sca,
            'material': None, 'mesh': None, 'linked_mesh': None, 'armature': None, 'curve': None}


def build_document(context, spec, model, opts, log=print):
    """Returns (doc, dropped_names). Node order follows the spec's node list for `model`."""
    scene = context.scene
    expected = spec.models.get(model)
    with_normals = spec.has_field('Mesh', 'normals')
    objs = sorted(scene.objects, key=lambda o: o.name.lower())
    if opts.get('selected_only'):
        objs = [o for o in objs if o.select_get()]
    depsgraph = context.evaluated_depsgraph_get()
    nodes, dropped = [], []
    want = set(expected) if expected else None

    def wanted(name):
        return want is None or opts.get('keep_extra') or name in want

    for i, obj in enumerate(objs):
        log('PROGRESS %d %d %s' % (i + 1, len(objs), obj.name))
        linked = obj.library is not None or (obj.data is not None and getattr(obj.data, 'library', None))
        if obj.type == 'MESH' and not (linked and not opts.get('embed_linked')):
            # skip the (possibly expensive) mesh evaluation if no split of it is wanted
            if want is not None and not opts.get('keep_extra') and \
                    not any(n == obj.name or n.startswith(obj.name + '.material.') for n in want):
                dropped.append(obj.name); continue
            for name, mat, mesh in mesh_nodes(obj, depsgraph, opts, with_normals, log):
                if not wanted(name):
                    dropped.append(name); continue
                n = {'name': name}; n.update(_base_node(obj))
                n['mesh'] = mesh
                n['material'] = material_spec(mat) if mat else None
                nodes.append(n)
            continue
        if not wanted(obj.name):
            dropped.append(obj.name); continue
        n = {'name': obj.name}; n.update(_base_node(obj))
        if obj.type == 'MESH':           # linked mesh -> reference
            lib = obj.data.library or obj.library
            n['linked_mesh'] = {'blend_path': _rel(lib.filepath), 'node_name': obj.name,
                                'mesh_name': obj.data.name}
        elif obj.type == 'ARMATURE':
            n['armature'] = armature_spec(obj, scene)
        elif obj.type == 'CURVE':
            n['curve'] = curve_spec(obj)
        nodes.append(n)

    if spec.textures:
        _map_textures(nodes, spec.textures, log)
    if expected:
        pos = {name: i for i, name in enumerate(expected)}
        nodes.sort(key=lambda n: (pos.get(n['name'], len(pos)), n['name'].lower()))
    fps = scene.render.fps / (scene.render.fps_base or 1)
    return {'framerate': int(round(fps)), 'nodes': nodes}, dropped


def _map_textures(nodes, known, log):
    """Blender stores image paths relative to wherever the .blend lives, so the same texture can show up
    as '../../model-textures/x.png'. Map such paths onto the spec's TextureId entry with the same tail."""
    def parts(p):
        return [x for x in p.replace('\\', '/').split('/') if x not in ('', '.', '..')]
    cache = {}
    for n in nodes:
        m = n.get('material')
        if not m: continue
        for k in TEXTURE_KEYS:
            p = m.get(k)
            if not p or p in known: continue
            if p not in cache:
                pp, best, score = parts(p), None, 0
                for t in known:
                    tp, s = parts(t), 0
                    while s < min(len(tp), len(pp)) and tp[-1 - s].lower() == pp[-1 - s].lower(): s += 1
                    if s > score: best, score = t, s
                    elif s == score and s: best = None if best != t else best
                cache[p] = best
                if best: log('INFO texture "%s" -> "%s"' % (p, best))
            if cache[p]: m[k] = cache[p]


def export(context, spec_path, out_path, model=None, json_path=None, check_only=False,
           force=False, log=print, **opts):
    spec = zigspec.ZigSpec.load(spec_path)
    model = model or zigspec.model_from_filename(bpy.data.filepath or out_path)
    log('INFO spec %s, model "%s"%s' % (spec.describe(), model,
        ' (%d nodes expected)' % len(spec.models[model]) if model in spec.models else ''))
    doc, dropped = build_document(context, spec, model, opts, log)
    import difflib
    missing = [n for n in spec.models.get(model, []) if n not in {d['name'] for d in doc['nodes']}]
    for n in dropped:
        hint = difflib.get_close_matches(n, missing, 1, 0.6)
        log('WARN skipped "%s" - not in %s\'s node list%s' % (
            n, model, ' (renamed? the game expects "%s")' % hint[0] if hint else ''))
    reference = None
    if out_path and os.path.exists(out_path):
        try:
            with open(out_path, 'rb') as f: data = f.read()
            if data.startswith(spec.header):
                reference = spec.decode(data)
            else:
                log('WARN the file being replaced uses schema %s, this spec is %s'
                    % (zigspec.sniff_header(data), spec.schema_hash))
        except Exception as e:
            log('WARN could not read the existing %s: %s' % (os.path.basename(out_path), e))
    errors, warns = zigspec.check_model(spec, model, doc, reference)
    for w in warns: log('WARN ' + w)
    for e in errors: log('ERROR ' + e)
    nv = sum(len(n['mesh']['positions']) for n in doc['nodes'] if n.get('mesh'))
    if check_only:
        log('CHECKED %s %d nodes, %d vertices' % ('ok' if not errors else 'fail', len(doc['nodes']), nv))
        return doc, errors
    if errors and not force:
        log('FAILED %d problem(s) - nothing written (use "write anyway" to override)' % len(errors))
        return doc, errors
    data = spec.encode(doc)
    with open(out_path, 'wb') as f: f.write(data)
    if json_path:
        with open(json_path, 'w') as f: json.dump(doc, f, indent=4)
    log('DONE %d nodes, %s vertices, %s bytes -> %s' % (len(doc['nodes']), format(nv, ','),
                                                         format(len(data), ','), out_path))
    return doc, errors


# =========================================================================
#  auto-mapped export: any scene -> the game's pieces
# =========================================================================
try:
    from . import automap
except ImportError:
    import automap


def _game_world(obj):
    C = _C()
    return C @ obj.matrix_world @ C.transposed()


def scan_scene(context, max_pts=3000, log=print, opts=None, max_islands=4000):
    """Describe the scene for automap (game space), including each mesh's loose parts."""
    import numpy as np
    dg = context.evaluated_depsgraph_get()
    opts = opts or {'apply_modifiers': True, 'defer_deform': True}
    cache = _MeshCache(dg, opts, False)
    out = []
    for obj in sorted(context.scene.objects, key=lambda o: o.name.lower()):
        W = _game_world(obj)
        o = {'name': obj.name, 'type': obj.type, 'parent': obj.parent.name if obj.parent else None,
             'pos': list(W.translation)}
        if obj.type == 'MESH':
            d = cache.get(obj)
            nv = len(d['co'])
            Wn = np.array(_C() @ obj.matrix_world)      # Blender-local -> game world
            g = d['co'].astype(np.float64) @ Wn[:3, :3].T + Wn[:3, 3]
            mi = np.clip(d['material_index'], 0, d['nslots'] - 1)
            counts = np.bincount(mi, minlength=d['nslots']) if len(mi) else np.zeros(d['nslots'], int)
            if nv:
                o['box'] = [g.min(0).tolist(), g.max(0).tolist()]
                o['points'] = g[::max(1, nv // max_pts)].tolist()
                vi, pi = d['visland'], d['pisland']
                nis = int(vi.max()) + 1 if nv else 0
                if nis <= max_islands:
                    order = np.argsort(vi, kind='stable')
                    bounds = np.searchsorted(vi[order], np.arange(nis + 1))
                    isl = []
                    for k in range(nis):
                        idx = order[bounds[k]:bounds[k + 1]]
                        pm = pi == k
                        sc = np.bincount(mi[pm], minlength=d['nslots'])
                        gi = g[idx]
                        isl.append({'id': k, 'n': int(len(idx)), 'polys': int(pm.sum()),
                                    'box': [gi.min(0).tolist(), gi.max(0).tolist()],
                                    'points': g[idx[::max(1, len(idx) // 400)]].tolist(),
                                    'slots': {str(j): int(c) for j, c in enumerate(sc) if c}})
                    o['islands'] = isl
                else:
                    log('WARN %s has %d loose parts - sorting it as one piece' % (obj.name, nis))
            o['polys'] = int(sum(counts))
            slots = []
            for i in range(max(1, len(obj.material_slots))):
                mat = obj.material_slots[i].material if i < len(obj.material_slots) else None
                sl = {'index': i, 'material': mat.name if mat else None, 'polys': int(counts[i]) if i < len(counts) else 0}
                if mat:
                    ms = material_spec(mat); c = ms['color']
                    sl.update(color=[c['x'], c['y'], c['z'], c['w']], roughness=ms['roughness'], metallic=ms['metallic'])
                slots.append(sl)
            o['slots'] = slots
        out.append(o)
    return {'objects': out}


_MIRROR = mathutils.Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))     # left <-> right (game x = Blender x)


def _fit_matrix(fit, mirror=False):
    """automap's game-space similarity transform as a Blender-space Matrix (after the optional mirror)."""
    F = _fit_matrix_raw(fit)
    return _MIRROR @ F if mirror else F        # after the fit: across the bike's centreline (x = 0)


def _fit_matrix_raw(fit):
    import math
    C = _C()
    if not fit or not fit.get('applied'): return mathutils.Matrix.Identity(4)
    if 'm' in fit:
        m = fit['m']; t = fit['offset']
        G = mathutils.Matrix(((m[0][0], m[0][1], m[0][2], t[0]), (m[1][0], m[1][1], m[1][2], t[1]),
                              (m[2][0], m[2][1], m[2][2], t[2]), (0, 0, 0, 1)))
        return C.transposed() @ G @ C
    G = (mathutils.Matrix.Translation(fit['offset']) @ mathutils.Matrix.Rotation(fit['theta'], 4, 'Y')
         @ mathutils.Matrix.Scale(fit['scale'], 4))
    return C.transposed() @ G @ C


def _ref_world_blender(ref_by, name, cache):
    """World matrix (Blender space) of a node of the original game file."""
    if name in cache: return cache[name]
    n = ref_by[name]
    C = _C()
    p, r, s = n['position'], n['rotation'], n['scale']
    G = mathutils.Matrix.LocRotScale(mathutils.Vector((p['x'], p['y'], p['z'])),
                                     mathutils.Quaternion((r['w'], r['x'], r['y'], r['z'])),
                                     mathutils.Vector((s['x'], s['y'], s['z'])))
    M = C.transposed() @ G @ C
    if n.get('parent') in ref_by:
        M = _ref_world_blender(ref_by, n['parent'], cache) @ M
    cache[name] = M
    return M


class _MeshCache:
    def __init__(self, depsgraph, opts, with_normals):
        self.dg, self.opts, self.wn, self.c = depsgraph, opts, with_normals, {}
        _SLOT_UV.clear()

    def get(self, obj):
        if obj.name in self.c: return self.c[obj.name]
        import numpy as np
        toggled = []
        for mod in obj.modifiers:
            disable = (not self.opts['apply_modifiers']) or (mod.type == 'SUBSURF' and self.opts.get('defer_subdiv')) or \
                      (mod.type in DEFER_MODIFIERS and self.opts['defer_deform'])
            if disable and mod.show_viewport: mod.show_viewport = False; toggled.append(mod)
        try:
            if toggled: self.dg.update()
            ev = obj.evaluated_get(self.dg); me = ev.to_mesh()
            try:
                nv, nl, npo = len(me.vertices), len(me.loops), len(me.polygons)
                d = {}
                co = np.empty(nv * 3, np.float32); me.vertices.foreach_get('co', co); d['co'] = co.reshape(-1, 3)
                lv = np.empty(nl, np.int32); me.loops.foreach_get('vertex_index', lv); d['lv'] = lv
                for k in ('loop_start', 'loop_total', 'material_index'):
                    a = np.empty(npo, np.int32); me.polygons.foreach_get(k, a); d[k] = a
                d['uvs'] = {}
                for layer in me.uv_layers:                       # every UV map; each material uses its own
                    uv = np.zeros(nl * 2, np.float32); layer.data.foreach_get('uv', uv)
                    d['uvs'][layer.name] = uv.reshape(-1, 2) + np.float32(0)
                rl = _render_uv(me)
                d['uv'] = d['uvs'][rl.name] if rl is not None else np.zeros((nl, 2), np.float32)
                d['nrm'] = _corner_normals(me, np, nl) if self.wn else None
                names = [g.name for g in obj.vertex_groups]
                d['groups'] = [[(names[g.group], g.weight) for g in v.groups if g.group < len(names)]
                               for v in me.vertices] if names else None
                arm = _armature_of(obj)
                d['bones'] = {b.name: i for i, b in enumerate(arm.data.bones)} if arm else {}
                d['subdiv'] = (max([m.levels for m in obj.modifiers if m.type == 'SUBSURF'] or [0])
                               if self.opts.get('defer_subdiv') else 0)     # otherwise applied into the mesh
                ev_ = np.empty(len(me.edges) * 2, np.int32); me.edges.foreach_get('vertices', ev_)
                cn = d['nrm'] if d['nrm'] is not None else _corner_normals(me, np, nl)
                d['cn'] = cn
                ext = float(np.ptp(d['co'], 0).max()) if nv else 1.0
                q = max(ext, 1e-6) * 1e-6
                pq = np.round(d['co'] / q).astype(np.int64)
                _, pfirst, pinv = np.unique(pq, axis=0, return_index=True, return_inverse=True)
                pid = pfirst[pinv.ravel()]           # weld by position: each vertex -> first vertex at that spot
                d['wv'] = pid
                if self.wn and nl:
                    # newer game builds take normals from the file instead of smoothing across shared
                    # vertices: corners of unwelded (ripped) models carry flat face normals, so smooth them
                    # over the welded spot, keeping real creases (corners > ~37 degrees off stay sharp)
                    g = pid[lv]
                    multi = np.bincount(pid, minlength=nv)[g] > 1
                    if multi.any():
                        S = np.zeros((nv, 3), np.float64); np.add.at(S, g, cn)
                        m = S[g]; m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)
                        n0 = cn / np.maximum(np.linalg.norm(cn, axis=1, keepdims=True), 1e-12)
                        ok = multi & ((n0 * m).sum(1) > 0.8)
                        cn[ok] = m[ok].astype(cn.dtype)
                # loose parts follow the welded surface (an unwelded model is not thousands of triangles)
                if nv:
                    raw = np.asarray(_islands(nv, pid[ev_.reshape(-1, 2)], np))[pid]
                    _, f2, i2 = np.unique(raw, return_index=True, return_inverse=True)
                    o2 = np.argsort(f2, kind='stable'); r2 = np.empty_like(o2); r2[o2] = np.arange(len(o2))
                    d['visland'] = r2[i2.ravel()]    # numbered by first vertex, as before for welded models
                else:
                    d['visland'] = np.zeros(0, np.int64)
                d['pisland'] = d['visland'][lv[d['loop_start']]] if npo else np.zeros(0, np.int64)
                d['nslots'] = max(1, len(obj.material_slots))
                # weld: corners at the same spot facing the same way become one game vertex (the game
                # smooths across shared vertices; ripped / unwelded models would otherwise look faceted)
                ck = np.concatenate([pid[lv][:, None], np.round(cn * 1000).astype(np.int64)], 1)
                _, cid = np.unique(ck, axis=0, return_inverse=True); d['cid'] = cid.ravel()   # + same normal
                # open parts: edges (on the welded surface) with a face on one side only
                if nl:
                    nxt = np.arange(nl) + 1
                    last = d['loop_start'] + d['loop_total'] - 1
                    nxt[last] = d['loop_start']
                    a_, b_ = pid[lv], pid[lv[nxt]]
                    e = np.sort(np.stack([a_, b_], 1), 1)
                    ek = e[:, 0] * (int(pid.max()) + 1) + e[:, 1]
                    _, einv, ecnt = np.unique(ek, return_inverse=True, return_counts=True)
                    border_loop = ecnt[einv.ravel()] == 1
                    poly_of_loop = np.repeat(np.arange(npo), d['loop_total'])
                    d['open_islands'] = set(np.unique(d['pisland'][poly_of_loop[border_loop]]).tolist())
                else:
                    d['open_islands'] = set()
            finally:
                ev.to_mesh_clear()
        finally:
            for mod in toggled: mod.show_viewport = True
            if toggled: self.dg.update()
        self.c[obj.name] = d
        return d


def _islands(nv, edges, np):
    """Loose-part id per vertex, numbered in order of each part's first vertex."""
    lab = np.arange(nv, dtype=np.int64)
    if len(edges):
        a, b = edges[:, 0].astype(np.int64), edges[:, 1].astype(np.int64)
        while True:
            la, lb = lab[a], lab[b]
            m = np.minimum(la, lb)
            if np.array_equal(la, lb): break
            np.minimum.at(lab, a, m); np.minimum.at(lab, b, m)
            for _ in range(8): lab = lab[lab]
    _, first, inv = np.unique(lab, return_index=True, return_inverse=True)
    order = np.argsort(first, kind='stable')
    rank = np.empty_like(order); rank[order] = np.arange(len(order))
    return rank[inv.ravel()]


def _render_uv(me):
    """The UV map Blender renders with (camera icon in the UV Maps list), else the selected one."""
    return next((l for l in me.uv_layers if l.active_render), None) or me.uv_layers.active


_SLOT_UV = {}


def _slot_uv_name(obj, slot):
    """Name of the UV map the material's colour image reads (a UV Map node feeding it), or None = default."""
    k = (obj.name, slot)
    if k in _SLOT_UV: return _SLOT_UV[k]
    name = None
    mat = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
    if mat is not None and mat.use_nodes and mat.node_tree:
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        img = None
        if bsdf is not None and bsdf.inputs['Base Color'].is_linked:
            seen, stack = set(), [bsdf.inputs['Base Color'].links[0].from_node]
            while stack:
                n = stack.pop()
                if n in seen: continue
                seen.add(n)
                if n.type == 'TEX_IMAGE' and n.image: img = n; break
                for i in n.inputs: stack.extend(l.from_node for l in i.links)
        node = img
        for _ in range(8):                               # follow Vector back through Mapping etc.
            if node is None or 'Vector' not in node.inputs or not node.inputs['Vector'].is_linked: break
            node = node.inputs['Vector'].links[0].from_node
            if node.type == 'UVMAP':
                name = node.uv_map or None; break
            if node.type == 'TEX_COORD': break
    _SLOT_UV[k] = name
    return name


def _slot_uv(d, obj, slot):
    name = _slot_uv_name(obj, slot)
    return d['uvs'].get(name, d['uv']) if name and 'uvs' in d else d['uv']


def _wrap_uv(uv, group):
    """Bring UVs that repeat outside 0..1 back into the tile, moving each face as a whole (per-corner wrapping
    would tear a face across the texture)."""
    import numpy as np
    uv = np.asarray(uv, np.float64)
    _, g = np.unique(group, return_inverse=True); g = g.ravel()
    cnt = np.bincount(g)
    off = np.stack([np.floor(np.bincount(g, uv[:, k]) / cnt) for k in (0, 1)], 1)
    return np.clip(uv - off[g], 0.0, 1.0)


def _build_split(sources, target_world, F, cache, with_normals, piece_mats=None, uvmap=None, double_sided=False,
                 no_uv=False):
    """Geometry of one game node from [(obj, slot, island)], expressed in target_world's space.
    piece_mats: {(obj name, island): Matrix} extra per-piece moves (Blender world space, applied last)
    uvmap: {(obj name, slot): (sx, sy, ox, oy) or ('swatch', u, v)} texture-atlas remaps"""
    import numpy as np
    P, U, N, VID, LT, GR, BI = [], [], [], [], [], [], []
    subdiv = 0
    inv = target_world.inverted()
    obj_id = {}
    for src in sources:
        obj, slot = src[0], src[1]
        island = src[2] if len(src) > 2 else -1
        si = obj_id.setdefault(obj.name, len(obj_id))
        d = cache.get(obj)
        mi = np.clip(d['material_index'], 0, d['nslots'] - 1)
        mask = mi == slot
        if island is not None and island >= 0: mask &= d['pisland'] == island
        polys = np.nonzero(mask)[0]
        if not len(polys): continue
        subdiv = max(subdiv, d['subdiv'])
        loops = np.concatenate([np.arange(d['loop_start'][p], d['loop_start'][p] + d['loop_total'][p]) for p in polys])
        PM = (piece_mats or {}).get((obj.name, -1 if island is None else island))
        M = np.array(inv @ (PM if PM is not None else mathutils.Matrix.Identity(4)) @ F @ obj.matrix_world)
        if np.linalg.det(M[:3, :3]) < 0:            # mirrored: reverse each face's corner order (keeps faces outward)
            lens = d['loop_total'][polys]
            st = np.repeat(np.concatenate([[0], np.cumsum(lens)[:-1]]), lens)
            k = np.arange(len(loops)) - st
            loops = loops[st + np.repeat(lens, lens) - 1 - k]
        vi = d['lv'][loops]
        pos = d['co'][vi].astype(np.float64) @ M[:3, :3].T + M[:3, 3]
        uv = _slot_uv(d, obj, slot)[loops]
        if no_uv: uv = np.zeros_like(uv)            # no texture on this part: UVs unused, don't split vertices
        rm = (uvmap or {}).get((obj.name, slot))
        if rm is not None:
            if rm[0] == 'swatch':
                uv = np.tile(np.array([[rm[1], rm[2]]], np.float32), (len(uv), 1))
            else:
                sx, sy, ox, oy = rm
                w = _wrap_uv(uv, np.repeat(np.arange(len(polys)), d['loop_total'][polys]))
                uv = (np.stack([ox + w[:, 0] * sx, oy + w[:, 1] * sy], 1)).astype(np.float32)
        P.append(pos); U.append(uv); LT.append(d['loop_total'][polys])
        VID.append(d['wv'][vi].astype(np.int64) + (si << 32))      # welded, like the game's own exporter
        if with_normals:
            nm = np.linalg.inv(M[:3, :3]).T
            n = d['nrm'][loops].astype(np.float64) @ nm.T
            n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
            N.append(n.astype(np.float32))
        GR.append((d, vi))
        if double_sided and d.get('open_islands'):
            # the game hides back faces: give open (thin) parts a back side with its own vertices
            op = np.isin(d['pisland'][polys], list(d['open_islands']))
            if op.any():
                lt_o = d['loop_total'][polys][op]
                starts = np.concatenate([[0], np.cumsum(d['loop_total'][polys])[:-1]])[op]
                rev = np.concatenate([s + np.arange(n - 1, -1, -1) for s, n in zip(starts.tolist(), lt_o.tolist())])
                P.append(pos[rev]); U.append(uv[rev]); LT.append(lt_o)
                VID.append(d['wv'][vi[rev]].astype(np.int64) + (si << 32) + (1 << 31))
                if with_normals: N.append(-N[-1][rev])
                GR.append((d, vi[rev]))
    if not P:
        return None
    P = np.concatenate(P); U = np.concatenate(U); VID = np.concatenate(VID); LT = np.concatenate(LT)
    dt = [('v', '<i8'), ('u', '<u4'), ('w', '<u4')]
    if with_normals:
        N = np.concatenate(N); dt += [('nx', '<u4'), ('ny', '<u4'), ('nz', '<u4')]
    key = np.empty(len(P), dtype=dt)
    key['v'] = VID; key['u'] = U[:, 0].view(np.uint32); key['w'] = U[:, 1].view(np.uint32)
    if with_normals:
        key['nx'] = N[:, 0].view(np.uint32); key['ny'] = N[:, 1].view(np.uint32); key['nz'] = N[:, 2].view(np.uint32)
    _, first, invi = np.unique(key, return_index=True, return_inverse=True)
    order = np.argsort(first, kind='stable')
    rank = np.empty_like(order); rank[order] = np.arange(len(order))
    new_idx = rank[invi.ravel()]
    src = first[order]
    mesh = {'polygons': [a.tolist() for a in np.split(new_idx, np.cumsum(LT)[:-1])],
            'positions': [_v4(x, z, -y, 1) for x, y, z in P[src].astype(np.float32).astype(np.float64).tolist()],
            'uvs': [{'x': u, 'y': 1.0 - v} for u, v in U[src].astype(np.float64).tolist()]}
    if with_normals:
        mesh['normals'] = [_v4(x, z, -y, 0) for x, y, z in N[src].astype(np.float64).tolist()]
    # vertex groups / bones, looked up through the source loop
    offs = np.cumsum([0] + [len(vi) for _, vi in GR])
    gnames, gpts = [], {}
    bidx, bwt = [], []
    for ni, li in enumerate(src.tolist()):
        k = int(np.searchsorted(offs, li, side='right')) - 1
        d, vi = GR[k]; v = int(vi[li - offs[k]])
        inf = []
        if d['groups'] is not None:
            for g, w in d['groups'][v]:
                if g not in gpts: gnames.append(g); gpts[g] = ([], [])
                gpts[g][0].append(ni); gpts[g][1].append(w)
                if g in d['bones'] and w > 0: inf.append((w, d['bones'][g]))
        inf = sorted(inf, reverse=True)[:3]; s = sum(w for w, _ in inf) or 1.0
        bidx.append([b for _, b in inf] + [-1] * (3 - len(inf)))
        bwt.append([w / s for w, _ in inf] + [0.0] * (3 - len(inf)))
    mesh.update({'bone_indices': bidx, 'bone_weights': bwt,
                 'vertex_groups': [{'name': g, 'points': gpts[g][0], 'weights': gpts[g][1]} for g in gnames],
                 'subdiv_levels': subdiv, 'costume': None, 'terrain': None})
    return mesh


def _empty_mesh(with_normals):
    """The game rejects meshes with no positions, so a game part with nothing of yours in it gets
    one tiny triangle (0.1 mm) at its pivot - present for the game, invisible in play."""
    e = 1e-4
    m = {'polygons': [[0, 1, 2]],
         'positions': [_v4(0, 0, 0, 1), _v4(e, 0, 0, 1), _v4(0, 0, e, 1)],
         'uvs': [{'x': 0.0, 'y': 0.0}] * 3,
         'bone_indices': [[-1, -1, -1]] * 3, 'bone_weights': [[0.0, 0.0, 0.0]] * 3,
         'vertex_groups': [], 'subdiv_levels': 0, 'costume': None, 'terrain': None}
    if with_normals: m['normals'] = [_v4(0, 1, 0, 0)] * 3
    return m


def _adj_blender(mapping, part):
    """Manual adjustment from the alignment editor (game space) as a Blender-space Matrix."""
    M = automap.part_matrix(mapping, part)
    C = _C()
    return C.transposed() @ mathutils.Matrix(M) @ C


def _slot_image(obj, slot):
    if slot >= len(obj.material_slots): return None, None
    mat = obj.material_slots[slot].material
    if mat is None or not mat.node_tree: return mat, None
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    return mat, (_upstream_image(bsdf.inputs['Base Color']) if bsdf else None)


def _slot_images(obj, slot):
    """(material, base colour image, normal map image) of a material slot"""
    if slot >= len(obj.material_slots): return None, None, None
    mat = obj.material_slots[slot].material
    if mat is None or not mat.node_tree: return mat, None, None
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf: return mat, None, None
    return mat, _upstream_image(bsdf.inputs['Base Color']), _upstream_image(bsdf.inputs['Normal'])


def _read_image(im, log):
    import numpy as np
    try:
        w, h = im.size
        if w and h:
            a = np.empty(w * h * 4, np.float32); im.pixels.foreach_get(a); return a.reshape(h, w, 4)
    except Exception as e:
        log('WARN could not read texture %s: %s' % (im.name, e))
    return None


def _bake_normal_shading(px, nrm, strength=0.8):
    """The bike has no normal-map slot, so bake the normal map's bumps into the colour as light and shade
    (light from above-front). Flat areas stay exactly as they were."""
    import numpy as np
    n = nrm[..., :3].astype(np.float32) * 2 - 1
    n /= np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6)
    L = np.array([-0.35, 0.45, 0.82], np.float32); L /= np.linalg.norm(L)
    shade = np.clip(n @ L, 0, None) / L[2]                  # 1.0 where the normal map is flat
    f = np.clip(1 + strength * (shade - 1), 0.25, 1.6)[..., None]
    out = px.copy(); out[..., :3] = np.clip(px[..., :3] * f, 0, 1)
    return out


FINISHES = {'satin': (0.55, 0.0), 'matte': (0.9, 0.0), 'gloss': (0.2, 0.0), 'blend': None}


def _needs_bake(mat):
    """True when the material's Base Color is more than one image plugged straight in (mixes, masks, tints,
    colour ramps, ...): then the colour Blender shows has to be rendered, not copied from the file."""
    if mat is None or not mat.use_nodes or not mat.node_tree: return False
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None or not bsdf.inputs['Base Color'].is_linked: return False
    node = bsdf.inputs['Base Color'].links[0].from_node
    if node.type != 'TEX_IMAGE': return True
    return node.inputs['Vector'].is_linked or bsdf.inputs['Base Color'].links[0].from_socket.name != 'Color'


def _bake_base_colors(context, cells, log, max_cell=1024):
    """Render each material's final base colour (exactly as its nodes compute it) into an image, using
    Blender's own baker. Fills c['px']; on failure the cell falls back to the raw image."""
    import numpy as np
    scene = context.scene
    prev_engine = scene.render.engine
    scene.render.engine = 'CYCLES'
    try:
        scene.cycles.samples = 1; scene.cycles.device = 'CPU'
        scene.cycles.use_denoising = False
    except Exception:
        pass
    targets, patched, extra = {}, [], []
    dummy = bpy.data.images.new('gme_bake_dummy', 8, 8)
    for c in cells:
        mat = c['mat']; nt = mat.node_tree
        w, h = c['image'].size if c['image'] is not None else (1024, 1024)
        w = min(max_cell * 2, max(64, 1 << int(np.floor(np.log2(max(w, 1))))))
        h = min(max_cell * 2, max(64, 1 << int(np.floor(np.log2(max(h, 1))))))
        img = bpy.data.images.new('gme_bake_' + mat.name, w, h, alpha=False)
        targets[mat.name] = img; c['bake_img'] = img
        bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
        out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None) or \
            next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if out is None: continue
        src = bsdf.inputs['Base Color'].links[0].from_socket
        old = [l.from_socket for l in out.inputs['Surface'].links]
        em = nt.nodes.new('ShaderNodeEmission'); nt.links.new(src, em.inputs['Color'])
        nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
        tn = nt.nodes.new('ShaderNodeTexImage'); tn.image = img
        for n in nt.nodes: n.select = False
        tn.select = True; nt.nodes.active = tn
        patched.append((mat, em, tn, out, old))
    objs = sorted({o for c in cells for (o, sl) in c['users']})
    done = set()
    for oname in objs:
        obj = scene.objects.get(oname)
        if obj is None or obj.type != 'MESH' or not obj.data.uv_layers: continue
        added = []
        for slot in obj.material_slots:                 # the baker wants a target in every material
            m = slot.material
            if m is not None and m.use_nodes and m.name not in targets:
                tn = m.node_tree.nodes.new('ShaderNodeTexImage'); tn.image = dummy
                m.node_tree.nodes.active = tn; added.append((m, tn))
        try:
            vis = (obj.hide_render, obj.hide_viewport)
            obj.hide_render = False; obj.hide_viewport = False
            try: obj.hide_set(False)
            except Exception: pass
            for o in context.view_layer.objects: o.select_set(False)
            obj.select_set(True); context.view_layer.objects.active = obj
            want = next((_slot_uv_name(obj, sl) for c in cells for (o, sl) in c['users']
                         if o == oname and _slot_uv_name(obj, sl)), None)
            lay = obj.data.uv_layers.get(want) if want else _render_uv(obj.data)
            if lay is not None: obj.data.uv_layers.active = lay        # the baker writes in the active UV map
            bpy.ops.object.bake(type='EMIT', margin=4, use_clear=False)
            obj.hide_render, obj.hide_viewport = vis
            done.add(oname)
        except Exception as e:
            log('WARN could not bake the colours of %s (%s) - using the raw texture' % (oname, str(e).strip()[:120]))
        for m, tn in added: m.node_tree.nodes.remove(tn)
    for mat, em, tn, out, old in patched:                  # put the materials back as they were
        nt = mat.node_tree
        nt.nodes.remove(em); nt.nodes.remove(tn)
        for s in old: nt.links.new(s, out.inputs['Surface'])
    scene.render.engine = prev_engine
    n = 0
    for c in cells:
        if all(o in done for o, _ in c['users']):
            img = c['bake_img']; w, h = img.size
            a = np.empty(w * h * 4, np.float32); img.pixels.foreach_get(a)
            c['px'] = a.reshape(h, w, 4); c['px'][..., 3] = 1.0; n += 1
    if n: log('INFO baked the final colours of %d material(s) (masks, mixes and tints included)' % n)


def _downscale(px, th, tw):
    """Area-average resize (proper filtering, so detail isn't lost to aliasing)."""
    import numpy as np
    h, w = px.shape[:2]
    if (h, w) == (th, tw): return px
    if h % th == 0 and w % tw == 0 and h >= th and w >= tw:
        return px.reshape(th, h // th, tw, w // tw, px.shape[2]).mean((1, 3))
    return _resample(px, th, tw)


def build_atlas(context, mapping, opts, log=print, max_cell=None, gutter=4):
    """Pack the textures (and plain colours) used by your parts into one colour image, plus a normal-map
    image with the same layout. Returns dict(uvmap, atlas_splits, normal_splits, color, normal) or None."""
    import numpy as np
    O = context.scene.objects
    atlas_splits, normal_splits, cells = set(), set(), {}
    for u in mapping['units']:
        if u['kind'] != 'mesh' or u.get('keep_original'): continue
        for sp in u['splits']:
            if sp.get('material_override'): continue
            srcs = [(O[s[0]], s[1]) for s in sp['sources'] if s[0] in O]
            info = [_slot_images(o, sl) for o, sl in srcs]
            if not any(im for _, im, _ in info): continue
            atlas_splits.add(sp['node'])
            if any(nm for _, _, nm in info): normal_splits.add(sp['node'])
            for (o, sl), (mat, im, nm) in zip(srcs, info):
                if im is not None and _needs_bake(mat):        # nodes change the colour: bake what Blender shows
                    key = (('bake', mat.name), nm.name if nm else None)
                else:
                    key = (('img', im.name) if im else ('col', mat.name if mat else '(none)'), nm.name if nm else None)
                cells.setdefault(key, {'image': im, 'normal': nm, 'mat': mat, 'users': set(),
                                       'bake': key[0][0] == 'bake'})['users'].add((o.name, sl))
    if not cells:
        log('INFO no image textures on your parts - nothing to pack'); return None
    for oname in sorted({o for c in cells.values() for o, _ in c['users']}):
        obj = O[oname]
        if obj.type != 'MESH': continue
        names = [l.name for l in obj.data.uv_layers]
        used = sorted({_slot_uv_name(obj, sl) or (_render_uv(obj.data).name if obj.data.uv_layers else '-')
                       for c in cells.values() for o, sl in c['users'] if o == oname})
        if len(names) > 1:
            log('INFO %s has %d UV maps (%s) - textures use %s' % (oname, len(names), ', '.join(names), ', '.join(used)))
    want_normals = bool(opts.get('normal_slot'))
    bake = (not want_normals) and opts.get('bake_normals', True)
    limit = int(opts.get('atlas_size') or 4096)
    if max_cell is None:            # biggest per-texture size that lets everything fit in limit x limit
        p2 = lambda v: 1 << max(3, int(np.floor(np.log2(max(v, 1)))))
        sizes = [tuple(c['image'].size) for c in cells.values() if c['image'] is not None and c['image'].size[0]]
        max_cell = limit
        while max_cell > 64 and sum(min(max_cell, p2(w)) * min(max_cell, p2(h)) for w, h in sizes) > 0.72 * limit * limit:
            max_cell //= 2
    to_bake = [c for c in cells.values() if c['bake']]
    if to_bake:
        _bake_base_colors(context, to_bake, log, max_cell)
    tiles = []
    for key, c in cells.items():
        px = c.get('px')
        if px is None:
            px = _read_image(c['image'], log) if c['image'] is not None else None
        if c['image'] is not None and px is None:
            log('WARN texture "%s" could not be loaded (missing file?) - using its material colour' % c['image'].name)
        if px is None:
            col = list(material_spec(c['mat'])['color'].values()) if c['mat'] else [0.8, 0.8, 0.8, 1]
            col = list(_srgb(col[:3])) + [col[3] if len(col) > 3 else 1.0]    # PNG pixels are sRGB
            px = np.tile(np.array(col, np.float32).reshape(1, 1, 4), (8, 8, 1)); swatch = True
        else:
            swatch = False
            h, w = px.shape[:2]
            tw = min(max_cell, 1 << max(3, int(np.floor(np.log2(w))))); th = min(max_cell, 1 << max(3, int(np.floor(np.log2(h)))))
            tw, th = max(8, tw - 2 * gutter), max(8, th - 2 * gutter)        # tile + its border = a power of two
            px = _downscale(px, th, tw)
        nrm = None
        if (want_normals or bake) and c['normal'] is not None and not swatch:
            n = _read_image(c['normal'], log)
            if n is not None:
                h2, w2 = n.shape[:2]; th, tw = px.shape[:2]
                nrm = n[(np.arange(th) * h2 // th)][:, (np.arange(tw) * w2 // tw)].copy()
                if opts.get('flip_green'): nrm[..., 1] = 1.0 - nrm[..., 1]
            else:
                log('WARN normal map "%s" could not be loaded' % c['normal'].name)
        if bake and nrm is not None:
            px = _bake_normal_shading(px, nrm, opts.get('bake_strength', 0.8)); nrm = None; c['baked'] = True
        tiles.append({'px': px, 'nrm': nrm, 'swatch': swatch, 'users': c['users'], 'baked': c.get('baked')})
    tiles.sort(key=lambda t: -t['px'].shape[0] * t['px'].shape[1])
    W = max(256, max(t['px'].shape[1] + 2 * gutter for t in tiles))
    W = 1 << int(np.ceil(np.log2(W)))
    total = sum((t['px'].shape[0] + 2 * gutter) * (t['px'].shape[1] + 2 * gutter) for t in tiles)
    while W * W < total and W < limit: W *= 2
    x = y = shelf = 0
    for t in tiles:
        h, w = t['px'].shape[:2]; cw, ch = w + 2 * gutter, h + 2 * gutter
        if x + cw > W: x, y, shelf = 0, y + shelf, 0
        t['x0'], t['y0'] = x + gutter, y + gutter
        x += cw; shelf = max(shelf, ch)
    H = max(8, (y + shelf + 15) // 16 * 16)
    color = np.zeros((H, W, 4), np.float32); color[..., 3] = 1
    normal = None
    if want_normals:
        normal = np.zeros((H, W, 4), np.float32); normal[..., 0:2] = 0.5; normal[..., 2:4] = 1.0     # flat
    uvmap = {}
    for t in tiles:
        h, w = t['px'].shape[:2]
        sl = (slice(t['y0'] - gutter, t['y0'] + h + gutter), slice(t['x0'] - gutter, t['x0'] + w + gutter))
        color[sl] = np.pad(t['px'], ((gutter, gutter), (gutter, gutter), (0, 0)), mode='edge')
        if t['nrm'] is not None and normal is not None:
            normal[sl] = np.pad(t['nrm'], ((gutter, gutter), (gutter, gutter), (0, 0)), mode='edge')
        for user in t['users']:
            uvmap[user] = (('swatch', (t['x0'] + w / 2) / W, (t['y0'] + h / 2) / H) if t['swatch']
                           else (w / W, h / H, t['x0'] / W, t['y0'] / H))
    if not opts.get('keep_alpha'):
        color[..., 3] = 1.0          # alpha in game textures means see-through; masks would punch holes
    if normal is not None: normal[..., 3] = 1.0
    n_img = sum(1 for t in tiles if not t['swatch'])
    n_nrm = sum(1 for t in tiles if t['nrm'] is not None)
    n_bk = sum(1 for t in tiles if t.get('baked'))
    log('INFO packed %d texture(s) + %d colour(s) into %dx%d%s%s' % (
        n_img, len(tiles) - n_img, W, H, ', with %d normal map(s)' % n_nrm if want_normals else '',
        ', %d normal map(s) baked into the colours' % n_bk if n_bk else ''))
    return {'uvmap': uvmap, 'atlas_splits': atlas_splits, 'normal_splits': normal_splits if want_normals else set(),
            'color': color, 'normal': normal if want_normals else None}


def _png_info(path):
    """(width, height, bit depth, colour type) from a PNG header, or None"""
    import struct
    try:
        with open(path, 'rb') as f: h = f.read(33)
        if h[:8] != b'\x89PNG\r\n\x1a\n' or h[12:16] != b'IHDR': return None
        w, hh, depth, ctype = struct.unpack('>IIBB', h[16:26])
        return w, hh, depth, ctype
    except OSError:
        return None


def _resample(px, H, W):
    """Area-ish resize of an (h,w,c) float image (2x2 supersampled)."""
    import numpy as np
    h, w = px.shape[:2]
    if (h, w) == (H, W): return px
    acc = 0
    for oy in (0.25, 0.75):
        for ox in (0.25, 0.75):
            ys = np.clip(((np.arange(H) + oy) * h / H).astype(np.int64), 0, h - 1)
            xs = np.clip(((np.arange(W) + ox) * w / W).astype(np.int64), 0, w - 1)
            acc = acc + px[ys][:, xs]
    return acc / 4


def write_png(px, path, log=print, backup=True):
    """Write an 8-bit PNG (same channel layout as the game's original file) at full packed resolution -
    the game loads each texture at its own size. px: (h,w,4) float, bottom row first (Blender order)."""
    import numpy as np, shutil, zlib, struct
    if backup and os.path.isfile(path) and not os.path.exists(path + '.orig'):
        shutil.copy2(path, path + '.orig')
        log('INFO original texture backed up as %s.orig' % os.path.basename(path))
    info = _png_info(path + '.orig') or _png_info(path)
    if info:
        W, H, depth, ctype = info
        ch = {0: 3, 2: 3, 4: 4, 6: 4}.get(ctype, 4)          # same alpha/no-alpha layout as the original
        H, W = px.shape[:2]
        log('INFO texture written at %dx%d (the original was %dx%d)' % (W, H, info[0], info[1]))
    else:
        H, W = px.shape[:2]; ch = 4
    a = (np.clip(px[::-1], 0, 1) * 255 + 0.5).astype(np.uint8)    # top row first for PNG
    if ch == 3: a = a[..., :3]
    elif ch == 1: a = a[..., :3].mean(-1, keepdims=True).astype(np.uint8)
    elif ch == 2: a = np.concatenate([a[..., :3].mean(-1, keepdims=True).astype(np.uint8), a[..., 3:4]], -1)
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[ch]
    raw = b''.join(b'\x00' + a[y].tobytes() for y in range(H))
    def chunk(t, d): return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, ctype, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
    with open(path, 'wb') as f: f.write(data)


def build_document_mapped(context, spec, mapping, reference, opts, log=print):
    import copy
    scene = context.scene
    if reference and mapping.get('paint'):
        mapping = automap.apply_paint(mapping, automap.game_material_catalog(reference), log)
    C_ = _C()
    piece_mats = {}
    for k, M in (mapping.get('piece_adjust') or {}).items():
        on, isl = k.rsplit('|', 1)
        piece_mats[(on, int(isl))] = C_.transposed() @ mathutils.Matrix(M) @ C_
    uvmap, atlas_splits, normal_splits = {}, set(), set()
    if opts.get('texture_slot'):
        at = build_atlas(context, mapping, opts, log)
        if at:
            uvmap, atlas_splits, normal_splits = at['uvmap'], at['atlas_splits'], at['normal_splits']
            if opts.get('texture_file'):
                write_png(at['color'], opts['texture_file'], log); log('INFO textures -> %s' % opts['texture_file'])
            if at['normal'] is not None and opts.get('normal_file'):
                write_png(at['normal'], opts['normal_file'], log); log('INFO normal maps -> %s' % opts['normal_file'])
    with_normals = spec.has_field('Mesh', 'normals')
    O = scene.objects
    F = _fit_matrix(mapping.get('fit'), mapping.get('mirror'))
    ref_by = {n['name']: n for n in reference['nodes']} if reference else {}
    wcache = {}
    cache = _MeshCache(context.evaluated_depsgraph_get(), opts, with_normals)
    original_pivots = mapping.get('pivots', 'original') == 'original'
    nodes = []

    adj_all = _adj_blender(mapping, None)

    def head_from_obj(obj, name):
        W = adj_all @ F @ obj.matrix_world
        par = ref_by[name].get('parent') if name in ref_by else None
        PW = _ref_world_blender(ref_by, par, wcache) if par in ref_by else mathutils.Matrix.Identity(4)
        pos, rot, sca = _decompose(PW.inverted() @ W)
        return {'type': obj.type, 'parent': par, 'position': pos, 'rotation': rot, 'scale': sca,
                'material': None, 'mesh': None, 'linked_mesh': None, 'armature': None, 'curve': None}

    for ui, u in enumerate(mapping['units']):
        log('PROGRESS %d %d %s' % (ui + 1, len(mapping['units']), u['base']))
        if u['kind'] == 'mesh' and u.get('keep_original') and all(sp['node'] in ref_by for sp in u['splits']):
            for sp in u['splits']:                             # the game's own part, untouched
                nodes.append(copy.deepcopy(ref_by[sp['node']]))
            continue
        if u['kind'] == 'mesh':
            Fu = _adj_blender(mapping, u['base']) @ F          # fit + your manual alignment
            objs = sorted({s[0] for sp in u['splits'] for s in sp['sources'] if s[0] in O})
            objs = [O[n] for n in objs]
            if u['base'] in ref_by and (original_pivots or not objs):
                r = ref_by[u['base']]
                TW = _ref_world_blender(ref_by, u['base'], wcache)
                head = {'type': 'MESH', 'parent': r.get('parent'), 'position': dict(r['position']),
                        'rotation': dict(r['rotation']), 'scale': dict(r['scale'])}
            elif objs:
                TW = Fu @ objs[0].matrix_world
                head = head_from_obj(objs[0], u['base']); head['type'] = 'MESH'
            else:
                TW = mathutils.Matrix.Identity(4)
                head = {'type': 'MESH', 'parent': None, 'position': _v4(0, 0, 0, 1),
                        'rotation': _v4(0, 0, 0, -1), 'scale': _v4(1, 1, 1, 0)}
            for sp in u['splits']:
                srcs = [(O[s[0]],) + tuple(s[1:]) for s in sp['sources'] if s[0] in O]
                game_tex = {os.path.basename(t).lower() for t in (spec.textures or [])}
                textured = sp['node'] in atlas_splits or any(
                    (_slot_images(s[0], s[1])[1] is not None and
                     os.path.basename(_slot_images(s[0], s[1])[1].filepath or _slot_images(s[0], s[1])[1].name).lower() in game_tex)
                    for s in srcs)
                mesh = _build_split(srcs, TW, Fu, cache, with_normals, piece_mats, uvmap,
                                    double_sided=opts.get('double_sided', True),
                                    no_uv=not textured and not sp.get('material_override')) if srcs else None
                n = {'name': sp['node'], 'linked_mesh': None, 'armature': None, 'curve': None}
                n.update(copy.deepcopy(head))
                n['mesh'] = mesh or _empty_mesh(with_normals)
                if sp.get('material_override'):
                    n['material'] = copy.deepcopy(sp['material_override'])
                elif sp['node'] in atlas_splits and srcs:
                    mats = [s[0].material_slots[s[1]].material for s in srcs if s[1] < len(s[0].material_slots)]
                    specs = [material_spec(m) for m in mats if m] or [{'color': _v4(1, 1, 1, 1), 'roughness': 0.5, 'metallic': 0.0}]
                    m_ = dict(specs[0])
                    m_['roughness'] = sum(s['roughness'] for s in specs) / len(specs)
                    m_['metallic'] = sum(s['metallic'] for s in specs) / len(specs)
                    m_.update(color=_v4(1, 1, 1, 1), diffuse_texture=opts.get('texture_slot'),
                              normal_texture=opts.get('normal_slot') if sp['node'] in normal_splits else None,
                              roughness_metallic_texture=None, baked_material_texture=None)
                    n['material'] = m_
                elif sp['node'].endswith('.material.no_material'):
                    n['material'] = None
                elif srcs and srcs[0][0].material_slots and srcs[0][1] < len(srcs[0][0].material_slots) \
                        and srcs[0][0].material_slots[srcs[0][1]].material:
                    n['material'] = material_spec(srcs[0][0].material_slots[srcs[0][1]].material)
                elif sp['node'] in ref_by:
                    n['material'] = copy.deepcopy(ref_by[sp['node']].get('material'))
                else:
                    n['material'] = None
                fin = FINISHES.get(opts.get('finish', 'satin'))
                if fin and srcs and n.get('material') and not sp.get('material_override'):
                    n['material']['roughness'], n['material']['metallic'] = fin     # your parts' finish
                nodes.append(n)
        else:
            src = O.get(u['source']) if u.get('source') else None
            if src is None:
                if u['base'] in ref_by:
                    nodes.append(copy.deepcopy(ref_by[u['base']]))
                else:
                    log('ERROR "%s" has no source and no original to copy from' % u['base'])
                continue
            n = {'name': u['base']}; n.update(head_from_obj(src, u['base']))
            if src.type == 'ARMATURE': n['armature'] = armature_spec(src, scene)
            elif src.type == 'CURVE': n['curve'] = curve_spec(src)
            nodes.append(n)
    expected = spec.models.get(mapping['model']) or []
    pos = {name: i for i, name in enumerate(expected)}
    nodes.sort(key=lambda n: (pos.get(n['name'], len(pos)), n['name'].lower()))
    if spec.textures:
        for n in nodes:
            m = n.get('material')
            if not m: continue
            for k in TEXTURE_KEYS:
                if m.get(k) and m[k] not in spec.textures:
                    _map_textures([n], spec.textures, log)
                    if m.get(k) not in spec.textures:
                        log('WARN %s: texture "%s" is not one the game knows - left off' % (n['name'], m[k]))
                        m[k] = None
    fps = scene.render.fps / (scene.render.fps_base or 1)
    return {'framerate': int(round(fps)), 'nodes': nodes}


TEMPLATE_DIR = os.path.join(_HERE, 'templates')


def _decode_any(data, spec, spec_dir):
    """Decode a game file with whichever spec matches its header (the template may be another version)."""
    if data.startswith(spec.header): return spec.decode(data)
    h = zigspec.sniff_header(data)
    for d in (spec_dir, os.path.join(_HERE, 'specs')):
        if not d or not os.path.isdir(d): continue
        for f in sorted(os.listdir(d)):
            if f.endswith('.zig'):
                s2 = zigspec.ZigSpec.load(os.path.join(d, f))
                if s2.schema_hash == h: return s2.decode(data)
    raise ValueError('schema %s - no spec file for it' % h)


def _srgb(c):
    return [round(max(0.0, min(1.0, x)) ** (1 / 2.2), 4) for x in c[:3]]


def write_preview(context, mapping, reference, opts, path, budget_tris=60000, log=print):
    """Data for the alignment editor: every piece of yours as a light triangle mesh (as placed by the
    automatic fit, before manual adjustments), its material, the original's parts and the game's materials."""
    import numpy as np
    O = context.scene.objects
    F = _fit_matrix(mapping.get('fit'), mapping.get('mirror'))
    cache = _MeshCache(context.evaluated_depsgraph_get(), opts, False)
    groups = {}
    for u in mapping['units']:
        if u['kind'] != 'mesh': continue
        for sp in u['splits']:
            for s in sp['sources']:
                isl = s[2] if len(s) > 2 and s[2] is not None else -1
                g = groups.setdefault((s[0], isl), {'unit': u['base'], 'sources': []})
                g['sources'].append(list(s[:2]) + [isl])
    for name in mapping.get('unused', []):            # meshes not exported: shown so they can be assigned
        o = O.get(name)
        if o is not None and o.type == 'MESH' and (name, -1) not in groups:
            groups[(name, -1)] = {'unit': None, 'sources': [[name, i, -1] for i in range(max(1, len(o.material_slots)))]}
    raw, full = [], []
    for (oname, isl), g in groups.items():
        obj = O.get(oname)
        if obj is None: continue
        d = cache.get(obj)
        mi = np.clip(d['material_index'], 0, d['nslots'] - 1)
        slots = sorted({s[1] for s in g['sources']})
        mask = np.isin(mi, slots)
        if isl >= 0: mask &= d['pisland'] == isl
        polys = np.nonzero(mask)[0]
        if not len(polys): continue
        W = np.array(_C() @ F @ obj.matrix_world)
        ls, lt = d['loop_start'][polys], d['loop_total'][polys]
        tris = []
        for k in range(1, int(lt.max()) - 1):                      # fan-triangulate every polygon
            m = lt > k + 1
            if not m.any(): break
            tris.append(np.stack([d['lv'][ls[m]], d['lv'][ls[m] + k], d['lv'][ls[m] + k + 1]], 1))
        T = np.concatenate(tris) if tris else np.zeros((0, 3), np.int64)
        used = np.unique(np.concatenate([T.ravel(), d['lv'][ls]]))  # only this piece's vertices
        remap = np.full(int(used.max()) + 1, -1, np.int64); remap[used] = np.arange(len(used))
        T = remap[T] if len(T) else T
        Pw = d['co'][used].astype(np.float64) @ W[:3, :3].T + W[:3, 3]
        LT = []
        for k in range(1, int(lt.max()) - 1):
            m = lt > k + 1
            if not m.any(): break
            LT.append((np.stack([ls[m], ls[m] + k, ls[m] + k + 1], 1), polys[m]))
        if LT:
            full.append((len(raw), obj, d, np.concatenate([t for t, _ in LT]), mi[np.concatenate([q for _, q in LT])], W))
        counts = np.bincount(mi[polys], minlength=d['nslots'])
        main = int(np.argmax(counts))
        mat = obj.material_slots[main].material if main < len(obj.material_slots) else None
        col = _srgb(list(material_spec(mat)['color'].values())) if mat else [0.7, 0.7, 0.7]
        raw.append((oname, isl, g, Pw, T, mat.name if mat else None, col))
    allP = np.concatenate([r[3] for r in raw]) if raw else np.zeros((1, 3))
    size = float(np.linalg.norm(allP.max(0) - allP.min(0))) or 1.0

    def cluster(Pw, T, h):
        if not len(T): return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
        k = np.floor(Pw / h).astype(np.int64) + (1 << 20)
        key = (k[:, 0] << 42) | (k[:, 1] << 21) | k[:, 2]
        _, cid, cnt = np.unique(key, return_inverse=True, return_counts=True)
        cid = cid.ravel()
        V = np.zeros((len(cnt), 3)); np.add.at(V, cid, Pw); V /= cnt[:, None]
        C = cid[T]
        ok = (C[:, 0] != C[:, 1]) & (C[:, 1] != C[:, 2]) & (C[:, 0] != C[:, 2])
        if not ok.any(): return V, np.zeros((0, 3), np.int64)
        S = np.sort(C[ok], 1)
        tk = (S[:, 0] << 42) | (S[:, 1] << 21) | S[:, 2]
        _, first = np.unique(tk, return_index=True)
        return V, S[first]
    h = size / 400
    for _ in range(4):                                     # size the simplification to the triangle budget
        n = sum(len(cluster(r[3], r[4], h)[1]) for r in raw)
        if n <= budget_tris: break
        h *= max(1.15, (n / budget_tris) ** 0.5 * 1.05)
    pieces = []
    for i, (oname, isl, g, Pw, T, mname, col) in enumerate(raw):
        V, Fc = cluster(Pw, T, h)
        if not len(Fc):                                   # tiny piece: keep at least its points
            used = np.unique(T) if len(T) else np.arange(min(len(Pw), 8))
            V = Pw[used[:: max(1, len(used) // 16)]]
        pieces.append({'id': i, 'obj': oname, 'island': isl, 'unit': g['unit'], 'n': int(len(Pw)),
                       'sources': g['sources'], 'material': mname, 'color': col,
                       'V': np.round(V, 5).tolist(), 'F': Fc.tolist()})
    parts, model_pivot, catalog = {}, None, []
    if reference:
        info = automap.reference_info(reference)
        rpts = automap.reference_points(reference, info, max_pts=2500)
        for u in mapping['units']:
            if u['kind'] != 'mesh': continue
            pts = [p for sp in u['splits'] for p in rpts.get(sp['node'], [])]
            step = max(1, len(pts) // 4000)
            parts[u['base']] = {'pivot': info[u['base']]['pos'] if u['base'] in info else None,
                                'ref': [[round(c, 5) for c in p] for p in pts[::step]]}
        helpers = [{'name': n, 'pos': i['pos'], 'type': i['type']} for n, i in info.items() if i['type'] != 'MESH']
        wheels = [info[n]['pos'] for n in ('back_tire', 'front_tire') if n in info]
        allb = automap.union([i['box'] for i in info.values() if i['box']])
        model_pivot = ([(wheels[0][i] + wheels[1][i]) / 2 for i in range(3)] if len(wheels) == 2
                       else automap.center(allb) if allb else [0, 0, 0])
        for c in automap.game_material_catalog(reference):
            m = c['material']
            catalog.append({'id': c['id'], 'color': _srgb([m['color']['x'], m['color']['y'], m['color']['z']]),
                            'roughness': m['roughness'], 'metallic': m['metallic'],
                            'texture': m.get('diffuse_texture'), 'used_by': c['used_by']})
    else:
        helpers = []
    for u in mapping['units']:
        if u['kind'] == 'mesh' and u['base'] not in parts:
            parts[u['base']] = {'pivot': None, 'ref': []}
    samples = glmesh = None
    at = None
    try:
        at = build_atlas(context, mapping, opts, lambda s: None) if opts.get('texture_slot') else None
    except Exception as e:
        log('WARN could not pack your textures for the view: %s' % e)
    try:
        glmesh = _write_glmesh(full, at, reference, os.path.splitext(path)[0] + '.mesh.npz', log, opts)
    except Exception as e:                       # the editor still works without it
        import traceback; traceback.print_exc()
        log('WARN could not prepare the 3D model view: %s' % e)
    try:
        samples = _write_samples(context, mapping, opts, full, os.path.splitext(path)[0] + '.samples.npz', log, at=at)
    except Exception as e:
        log('WARN could not prepare the in-game view: %s' % e)
    with open(path, 'w') as f:
        json.dump({'pieces': pieces, 'parts': parts, 'helpers': helpers, 'model_pivot': model_pivot,
                   'catalog': catalog, 'mapping': mapping, 'samples': samples, 'glmesh': glmesh,
                   'game_root': opts.get('game_root')}, f)
    log('DONE preview %d pieces, %d triangles' % (len(pieces), sum(len(p['F']) for p in pieces)))


def _export_rm(mat, opts):
    """(roughness, metallic) a part with this Blender material is exported with."""
    fin = FINISHES.get(opts.get('finish', 'satin'))
    if fin: return fin
    if mat is None: return (0.5, 0.0)
    s = material_spec(mat); return (s['roughness'], s['metallic'])


def _write_glmesh(full, at, reference, path, log=print, opts=None):
    """Full-detail triangle mesh for the editor's GPU view: your pieces (welded, smooth normals, UVs into the
    packed texture or a flat material colour) and the original's own meshes, all in game space."""
    import numpy as np
    uvmap = at['uvmap'] if at else {}
    Ps, Ns, Us, Cs, Ts, Ks, Is, RMs = [], [], [], [], [], [], [], []
    for pid, obj, d, Lt, slot, W in full:
        L = Lt.ravel()                                                    # corners, 3 per triangle
        P = d['co'][d['lv'][L]].astype(np.float64) @ W[:3, :3].T + W[:3, 3]
        nm = np.linalg.inv(W[:3, :3]).T
        N = d['cn'][L].astype(np.float64) @ nm.T
        N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)
        U = np.zeros((len(L), 2)); C = np.zeros((len(L), 3), np.uint8); T = np.zeros(len(L), np.uint8)
        RM = np.zeros((len(L), 2), np.float32)
        st = np.repeat(slot, 3)
        for s in np.unique(st):
            m = st == s
            rm = uvmap.get((obj.name, int(s)))
            if rm is not None:
                T[m] = 1
                if rm[0] == 'swatch':
                    U[m] = (rm[1], rm[2])
                else:
                    sx, sy, ox, oy = rm
                    uv = _wrap_uv(_slot_uv(d, obj, int(s))[L[m]].astype(np.float64), np.nonzero(m)[0] // 3)
                    U[m] = np.stack([ox + uv[:, 0] * sx, oy + uv[:, 1] * sy], 1)
            mat = obj.material_slots[int(s)].material if int(s) < len(obj.material_slots) else None
            c = _srgb(list(material_spec(mat)['color'].values())[:3]) if mat else [0.7, 0.7, 0.7]
            if rm is not None: c = [1.0, 1.0, 1.0]             # packed parts: colour white x texture, as exported
            C[m] = (np.clip(np.array(c), 0, 1) * 255 + 0.5).astype(np.uint8)
            RM[m] = _export_rm(mat, opts or {})
        # share vertices: same welded corner + same uv
        key = np.empty(len(L), [('c', '<i8'), ('u', '<f4'), ('v', '<f4')])
        key['c'] = d['cid'][L]; key['u'] = U[:, 0]; key['v'] = U[:, 1]
        _, first, inv = np.unique(key, return_index=True, return_inverse=True)
        base = sum(len(x) for x in Ps)
        Ps.append(P[first].astype(np.float32)); Ns.append(N[first].astype(np.float32))
        Us.append(U[first].astype(np.float32)); Cs.append(C[first]); Ks.append(T[first]); RMs.append(RM[first])
        Is.append(np.full(len(first), pid, np.int32)); Ts.append((inv.ravel() + base).astype(np.uint32))
    out = {}
    if Ps:
        out.update(P=np.concatenate(Ps), N=np.concatenate(Ns), U=np.concatenate(Us), C=np.concatenate(Cs),
                   K=np.concatenate(Ks), I=np.concatenate(Is), T=np.concatenate(Ts), RM=np.concatenate(RMs))
    if at is not None:
        out['atlas'] = (np.clip(at['color'], 0, 1) * 255 + 0.5).astype(np.uint8)
    # the original's meshes, in game space
    if reference:
        info = automap.reference_info(reference)
        rP, rN, rC, rT, rU, rRM, names = [], [], [], [], [], [], []
        for n in reference['nodes']:
            me = n.get('mesh')
            if not me or not me['positions'] or n['name'] not in info: continue
            Wr = np.array(info[n['name']]['world'], np.float64)
            p = np.array([(q['x'], q['y'], q['z']) for q in me['positions']], np.float64)
            p = p @ Wr[:3, :3].T + Wr[:3, 3]
            tris = [(poly[0], poly[k], poly[k + 1]) for poly in me['polygons'] for k in range(1, len(poly) - 1)]
            if not tris: continue
            t = np.array(tris, np.int64)
            fn = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
            vn = np.zeros_like(p); np.add.at(vn, t.ravel(), np.repeat(fn, 3, 0))   # game-style smooth normals
            vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
            mat = n.get('material') or {}
            col = mat.get('color') or {'x': 0.7, 'y': 0.7, 'z': 0.7}
            c = (np.clip(np.array(_srgb([col['x'], col['y'], col['z']])), 0, 1) * 255 + 0.5).astype(np.uint8)
            base = sum(len(x) for x in rP)
            rP.append(p.astype(np.float32)); rN.append(vn.astype(np.float32)); rC.append(np.tile(c, (len(p), 1)))
            rT.append((t.ravel() + base).astype(np.uint32)); rU.append(np.full(len(p), len(names), np.int32))
            rRM.append(np.tile(np.array([mat.get('roughness', 1.0), mat.get('metallic', 0.0)], np.float32), (len(p), 1)))
            names.append(n['name'])
        if rP:
            out.update(rP=np.concatenate(rP), rN=np.concatenate(rN), rC=np.concatenate(rC),
                       rT=np.concatenate(rT), rI=np.concatenate(rU), rNames=np.array(names), rRM=np.concatenate(rRM))
    np.savez(path, **out)
    log('INFO 3D view: %s triangles of yours, %s of the original' % (
        format(len(out.get('T', [])) // 3, ','), format(len(out.get('rT', [])) // 3, ',')))
    return path


def _write_samples(context, mapping, opts, full, path, log=print, budget=900000, at=None):
    """Dense coloured surface samples of every piece, coloured the way the game will draw them: the packed
    texture (when textures are brought over) or the material colour. Used by the editor's in-game view."""
    import numpy as np
    uvmap = at['uvmap'] if at else {}
    atlas = None
    if at is not None:
        atlas = (np.clip(at['color'][..., :3], 0, 1) * 255 + 0.5).astype(np.uint8)
    rng = np.random.default_rng(1)
    areas = []
    for pid, obj, d, Lt, slot, W in full:
        V = d['co'][d['lv'][Lt]].astype(np.float64) @ W[:3, :3].T + W[:3, 3]
        areas.append(0.5 * np.linalg.norm(np.cross(V[:, 1] - V[:, 0], V[:, 2] - V[:, 0]), axis=1))
    total = float(sum(a.sum() for a in areas)) or 1.0
    ntri = sum(len(a) for a in areas)
    dens = max(budget - ntri, budget // 2) / total
    P_, N_, C_, I_ = [], [], [], []
    for (pid, obj, d, Lt, slot, W), A in zip(full, areas):
        lv = d['lv'][Lt]
        V = d['co'][lv].astype(np.float64) @ W[:3, :3].T + W[:3, 3]           # (n,3,3)
        fn = np.cross(V[:, 1] - V[:, 0], V[:, 2] - V[:, 0])                  # area-weighted face normals
        u_, inv = np.unique(lv.ravel(), return_inverse=True)
        vn = np.zeros((len(u_), 3)); np.add.at(vn, inv, np.repeat(fn, 3, 0))  # smooth, like the game
        vn = vn[inv].reshape(-1, 3, 3)
        cnt = np.floor(A * dens + rng.random(len(A))).astype(np.int64)
        if ntri < budget // 2: cnt = np.maximum(cnt, 1)
        t = np.repeat(np.arange(len(A)), cnt)
        if not len(t): continue
        r1 = np.sqrt(rng.random(len(t))); r2 = rng.random(len(t))
        w = np.stack([1 - r1, r1 * (1 - r2), r1 * r2], 1)                    # uniform barycentrics
        P_.append((np.einsum('ij,ijk->ik', w, V[t])).astype(np.float32))
        n = np.einsum('ij,ijk->ik', w, vn[t]); n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        N_.append(n.astype(np.float16))
        col = np.zeros((len(t), 3), np.uint8)
        st = slot[t]
        for s in np.unique(st):
            m = st == s
            rm = uvmap.get((obj.name, int(s)))
            if rm is not None and atlas is not None:
                if rm[0] == 'swatch':
                    uv = np.tile(np.array([[rm[1], rm[2]]]), (int(m.sum()), 1))
                else:
                    sx, sy, ox, oy = rm
                    cuv = _slot_uv(d, obj, int(s))[Lt[t[m]]].astype(np.float64)                  # (k,3,2) corner uvs
                    cuv = np.clip(cuv - np.floor(cuv.mean(1, keepdims=True)), 0, 1)
                    cuv = np.stack([ox + cuv[..., 0] * sx, oy + cuv[..., 1] * sy], -1)
                    uv = np.einsum('ij,ijk->ik', w[m], cuv)
                Hh, Ww = atlas.shape[:2]
                col[m] = atlas[np.clip((uv[:, 1] * Hh).astype(np.int64), 0, Hh - 1),
                               np.clip((uv[:, 0] * Ww).astype(np.int64), 0, Ww - 1)]
            else:
                mat = obj.material_slots[int(s)].material if int(s) < len(obj.material_slots) else None
                c = _srgb(list(material_spec(mat)['color'].values())[:3]) if mat else [0.7, 0.7, 0.7]
                col[m] = (np.clip(np.array(c), 0, 1) * 255 + 0.5).astype(np.uint8)
        C_.append(col); I_.append(np.full(len(t), pid, np.int32))
    if not P_: return None
    P, N, C, I = (np.concatenate(x) for x in (P_, N_, C_, I_))
    order = np.argsort(I, kind='stable')
    np.savez(path, P=P[order], N=N[order], C=C[order], I=I[order], spacing=np.float32((total / len(P)) ** 0.5))
    log('INFO in-game view: %d surface samples%s' % (len(P), ' with your packed textures' if atlas is not None else ''))
    return path


def find_reference(spec, out_path, reference=None, log=print, model=None, spec_dir=None):
    """The game's original file for this model, used as the template:
    explicit path, the file being replaced (its .orig backup first), then the built-in templates folder."""
    cands = []
    for p in (reference, out_path):
        if not p: continue
        if zigspec.stale_backup(p):               # the game was updated since we backed it up
            log('INFO %s.orig is from an older game build - the game\'s current %s is used as the original'
                % (os.path.basename(p), os.path.basename(p)))
            cands.append(p)
        else:
            cands += [p + '.orig', p]             # a backed-up original always beats an exported file
    if model: cands.append(os.path.join(TEMPLATE_DIR, model + '.blend.bin'))
    for p in cands:
        if p and os.path.isfile(p):
            with open(p, 'rb') as f: data = f.read()
            try:
                return _decode_any(data, spec, spec_dir), p
            except Exception as e:
                log('WARN could not use %s as the original: %s' % (os.path.basename(p), e))
    return None, None


MAPPING_VERSION = 3


def fingerprint(scan):
    import hashlib
    h = hashlib.sha1()
    for o in scan['objects']:
        h.update(('%s|%s|%d|%d;' % (o['name'], o['type'], o.get('polys', 0), len(o.get('islands', [])))).encode())
    return h.hexdigest()[:16]


def _same_pieces(mp, scan):
    """Does a saved matching still describe the scene piece for piece (same objects, same loose-part count)?"""
    n_isl = {o['name']: len(o.get('islands', [])) for o in scan['objects'] if o['type'] == 'MESH'}
    used = {}
    for u in mp.get('units', []):
        for sp in u.get('splits', []):
            for s in sp.get('sources', []):
                if s[0] not in n_isl: return False
                isl = s[2] if len(s) > 2 and s[2] is not None else -1
                if isl >= n_isl[s[0]]: return False
                used.setdefault(s[0], set()).add(isl)
    return all(o in used or o in mp.get('unused', []) for o in n_isl)


def get_mapping(context, spec, model, ref, mapping_path, remap, pivots, opts, log):
    """Saved mapping if it still fits the scene, else a fresh automatic one. Returns (mapping, scan, saved?)"""
    scan = scan_scene(context, log=log, opts=opts)
    fp = fingerprint(scan)
    if mapping_path and os.path.isfile(mapping_path) and not remap:
        with open(mapping_path) as f: mp = json.load(f)
        if mp.get('version') != MAPPING_VERSION:
            log('INFO matching saved by an older version of the tool - matched again')
        elif mp.get('model') == model and mp.get('fingerprint') == fp:
            log('INFO using your reviewed matching (%s)' % os.path.basename(mapping_path))
            mp['mirror'] = bool(opts.get('mirror'))
            return mp, scan, True
        elif mp.get('model') == model and _same_pieces(mp, scan):
            # same objects and loose parts, only detail changed (e.g. Subdivision now applied): keep your edits
            mp['fingerprint'] = fp; mp['mirror'] = bool(opts.get('mirror'))
            log('INFO using your reviewed matching (%s) - mesh detail changed, pieces are the same'
                % os.path.basename(mapping_path))
            return mp, scan, True
        elif mp.get('model') == model:
            log('WARN the .blend changed since the matching was reviewed - matched again automatically')
    mp = automap.automap(scan, model, spec.models.get(model, []), ref, pivots=pivots,
                         split_parts=opts.get('split_parts', True))
    mp['fingerprint'] = fp
    mp['version'] = MAPPING_VERSION
    mp['mirror'] = bool(opts.get('mirror'))
    return mp, scan, False


def export_auto(context, spec_path, out_path, model=None, reference=None, mapping_path=None, remap=False,
                check_only=False, force=False, json_path=None, pivots='original', log=print, **opts):
    import shutil
    spec = zigspec.ZigSpec.load(spec_path)
    model = model or zigspec.model_from_filename(bpy.data.filepath or out_path)
    expected = spec.models.get(model)
    log('INFO spec %s, model "%s"%s' % (spec.describe(), model,
        ' (%d nodes expected)' % len(expected) if expected else ''))
    if expected is None:
        log('ERROR "%s" is not a model in this spec' % model); return None, ['unknown model']
    ref, ref_path = find_reference(spec, out_path, reference, log, model, os.path.dirname(spec_path))
    if ref is None:
        log('ERROR no original game file for %s to use as the template. Put the game\'s %s.blend.bin in the '
            '"Game models" folder (or in the tool\'s templates folder).' % (model, model))
        if not check_only: log('FAILED no template - nothing written')
        else: log('CHECKED fail no template')
        return None, ['no template']
    else:
        log('INFO original: %s' % ref_path)
    mapping, _, _ = get_mapping(context, spec, model, ref, mapping_path, remap, pivots, opts, log)
    for line in automap.summary(mapping): log(line)
    doc = build_document_mapped(context, spec, mapping, ref, opts, log)
    errors, warns = zigspec.check_model(spec, model, doc, ref)
    for w in warns: log('WARN ' + w)
    for e in errors: log('ERROR ' + e)
    nv = sum(len(n['mesh']['positions']) for n in doc['nodes'] if n.get('mesh'))
    ref_nv = sum(len(n['mesh']['positions']) for n in ref.get('nodes', []) if n.get('mesh')) if ref else 0
    if ref_nv:
        log('INFO detail: %s vertices (the original has %s)' % (format(nv, ','), format(ref_nv, ',')))
        if nv > 2.5 * ref_nv:
            log("WARN that's %.1fx the original's detail - it may run slowly in game (lower the Subdivision "
                'levels in Blender if so)' % (nv / ref_nv))
    if check_only:
        log('CHECKED %s %d nodes, %d vertices' % ('ok' if not errors else 'fail', len(doc['nodes']), nv))
        return doc, errors
    if errors and not force:
        log('FAILED %d problem(s) - nothing written' % len(errors)); return doc, errors
    if os.path.isfile(out_path) and ref_path == out_path and (
            not os.path.exists(out_path + '.orig') or zigspec.stale_backup(out_path)):
        shutil.copy2(out_path, out_path + '.orig')
        log('INFO original backed up as %s' % os.path.basename(out_path + '.orig'))
    data = spec.encode(doc)
    with open(out_path, 'wb') as f: f.write(data)
    if json_path:
        with open(json_path, 'w') as f: json.dump(doc, f, indent=1)
    log('DONE %d nodes, %s vertices, %s bytes -> %s' % (len(doc['nodes']), format(nv, ','),
                                                         format(len(data), ','), out_path))
    return doc, errors


# =========================================================================
#  document -> Blender (import, for viewing/editing game models)
# =========================================================================
def _from_game(v):
    return (v['x'], -v['z'], v['y'])


def _matrix(n):
    C = _C()
    p, r, s = n['position'], n['rotation'], n['scale']
    mg = mathutils.Matrix.LocRotScale(mathutils.Vector((p['x'], p['y'], p['z'])),
                                      mathutils.Quaternion((r['w'], r['x'], r['y'], r['z'])),
                                      mathutils.Vector((s['x'], s['y'], s['z'])))
    return C.transposed() @ mg @ C


def _make_material(name, m):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    c = m['color']
    bsdf.inputs['Base Color'].default_value = (c['x'], c['y'], c['z'], c['w'])
    bsdf.inputs['Roughness'].default_value = m['roughness']
    bsdf.inputs['Metallic'].default_value = m['metallic']
    mat.diffuse_color = (c['x'], c['y'], c['z'], c['w'])

    def img_node(path, label):
        node = nt.nodes.new('ShaderNodeTexImage'); node.label = label
        img = bpy.data.images.new(os.path.basename(path), 8, 8)
        img.source = 'FILE'; img.filepath = path if os.path.isabs(path) else '//' + path
        node.image = img
        return node
    if m.get('diffuse_texture'):
        nt.links.new(img_node(m['diffuse_texture'], 'diffuse').outputs['Color'], bsdf.inputs['Base Color'])
    if m.get('normal_texture'):
        nm = nt.nodes.new('ShaderNodeNormalMap'); t = img_node(m['normal_texture'], 'normal')
        nt.links.new(t.outputs['Color'], nm.inputs['Color']); nt.links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])
    if m.get('roughness_metallic_texture'):
        t = img_node(m['roughness_metallic_texture'], 'roughness_metallic')
        sep = nt.nodes.new('ShaderNodeSeparateColor')
        nt.links.new(t.outputs['Color'], sep.inputs[0])
        nt.links.new(sep.outputs[1], bsdf.inputs['Roughness']); nt.links.new(sep.outputs[2], bsdf.inputs['Metallic'])
    if m.get('baked_material_texture'):
        img_node(m['baked_material_texture'], 'baked')
    return mat


def import_document(context, doc, collection_name='GameModel'):
    coll = bpy.data.collections.new(collection_name)
    context.scene.collection.children.link(coll)
    context.scene.render.fps, context.scene.render.fps_base = doc['framerate'], 1
    mesh_names = {n['name'] for n in doc['nodes'] if n.get('mesh')}
    groups, order = {}, []
    for n in doc['nodes']:
        base = None
        if n.get('mesh') and '.material.' in n['name']:
            c = [b for b in mesh_names if n['name'].startswith(b + '.material.')]
            if c: base = max(c, key=len)
        if base is None:
            groups[n['name']] = [n]; order.append(n['name'])
        else:
            groups.setdefault(base, []).append(n)
    mats, objects = {}, {}
    for key in order:
        parts = groups[key]; head = parts[0]
        if head.get('mesh'):
            verts, faces, fmat, luv, vg, slots = [], [], [], [], {}, []
            for si, n in enumerate(parts):
                m = n['mesh']; off = len(verts)
                verts.extend(_from_game(p) for p in m['positions'])
                for poly in m['polygons']:
                    faces.append([i + off for i in poly]); fmat.append(si)
                    luv.extend((m['uvs'][i]['x'], 1.0 - m['uvs'][i]['y']) for i in poly)
                for g in m.get('vertex_groups') or []:
                    vg.setdefault(g['name'], []).extend((p + off, w) for p, w in zip(g['points'], g['weights']))
                if n.get('material'):
                    mname = key + '.base' if si == 0 else n['name'][len(key) + len('.material.'):]
                    if si == 0 or mname not in mats:
                        mats[mname] = _make_material(mname, n['material'])
                    slots.append(mats[mname])
                else:
                    slots.append(None)
            me = bpy.data.meshes.new(key)
            me.from_pydata(verts, [], faces)
            me.polygons.foreach_set('material_index', fmat)
            me.polygons.foreach_set('use_smooth', [True] * len(faces))
            me.uv_layers.new(name='UVMap').data.foreach_set('uv', [c for uv in luv for c in uv])
            for mat in slots: me.materials.append(mat)
            me.validate(clean_customdata=False)
            obj = bpy.data.objects.new(key, me)
            for gname, pts in vg.items():
                g = obj.vertex_groups.new(name=gname)
                for p, w in pts: g.add([p], w, 'REPLACE')
            if head['mesh'].get('subdiv_levels'):
                obj.modifiers.new('Subdivision', 'SUBSURF').levels = head['mesh']['subdiv_levels']
        elif head.get('curve'):
            cu = bpy.data.curves.new(key, 'CURVE'); cu.dimensions = '3D'
            for s in head['curve']['splines']:
                sp = cu.splines.new('BEZIER'); sp.bezier_points.add(len(s['bezier_points']) - 1)
                for bp, src in zip(sp.bezier_points, s['bezier_points']):
                    bp.handle_left_type = bp.handle_right_type = 'FREE'
                    bp.co = _from_game(src['point'])
                    bp.handle_left = _from_game(src['handle_left']); bp.handle_right = _from_game(src['handle_right'])
            obj = bpy.data.objects.new(key, cu)
        elif head['type'] == 'CAMERA':
            obj = bpy.data.objects.new(key, bpy.data.cameras.new(key))
        elif head['type'] == 'LATTICE':
            obj = bpy.data.objects.new(key, bpy.data.lattices.new(key))
        elif head['type'] == 'LIGHT':
            obj = bpy.data.objects.new(key, bpy.data.lights.new(key, 'POINT'))
        else:                                  # EMPTY, ARMATURE (bones not rebuilt), linked meshes, ...
            obj = bpy.data.objects.new(key, None)
            if head['type'] != 'EMPTY': obj['game_type'] = head['type']
        coll.objects.link(obj)
        obj.matrix_basis = _matrix(head)
        objects[key] = (obj, head)
    for obj, head in objects.values():
        if head.get('parent') in objects:
            obj.parent = objects[head['parent']][0]
            obj.matrix_parent_inverse = mathutils.Matrix.Identity(4)
            obj.matrix_basis = _matrix(head)
    return coll


def load_document(path, spec_paths):
    with open(path, 'rb') as f: data = f.read()
    if not data.startswith(b'BinarySerializer:'):
        return json.loads(data.decode('utf-8'))
    h = zigspec.sniff_header(data)
    for sp in spec_paths:
        s = zigspec.ZigSpec.load(sp)
        if s.schema_hash == h: return s.decode(data)
    raise ValueError('no spec file matches schema %s' % h)


# =========================================================================
#  add-on UI
# =========================================================================
def _addon_specs():
    d = os.path.join(_HERE, 'specs')
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith('.zig')) if os.path.isdir(d) else []


def _spec_items(self, context):
    items = [(p, os.path.basename(p), p) for p in _addon_specs()]
    return items or [('', 'no specs found', '')]


def _model_items(self, context):
    try:
        spec = zigspec.ZigSpec.load(self.spec_file)
        return [('__auto__', 'From .blend file name', '')] + \
               [(m, m, '%d nodes' % len(n)) for m, n in sorted(spec.models.items())]
    except Exception:
        return [('__auto__', 'From .blend file name', '')]


class EXPORT_OT_game_model(bpy.types.Operator, ExportHelper):
    """Export to the game's .blend.bin, validated against BlendMeshSpec.zig"""
    bl_idname = 'export_scene.game_model_bin'
    bl_label = 'Export Game Model'
    filename_ext = '.bin'
    filter_glob: StringProperty(default='*.bin', options={'HIDDEN'})
    spec_file: EnumProperty(name='Spec', items=_spec_items)
    model: EnumProperty(name='Model', items=_model_items)
    auto_match: BoolProperty(name='Match my pieces automatically', default=True,
                             description='Find wheels, body, swingarm... by shape and position and name them for the game')
    reference_file: StringProperty(name='Original game file', subtype='FILE_PATH',
                                   description="The game's own .blend.bin for this model (template for positions, "
                                               "pivots and mount points)")
    keep_extra: BoolProperty(name='Keep objects not in node list', default=False)
    force: BoolProperty(name='Write even if checks fail', default=False)
    apply_modifiers: BoolProperty(name='Apply modifiers', default=True)
    defer_deform: BoolProperty(name='Leave Lattice/Armature to game', default=True)
    defer_subdiv: BoolProperty(name='Leave Subdivision to game', default=False)
    mirror: BoolProperty(name='Mirror left/right', default=False)
    embed_linked: BoolProperty(name='Embed linked meshes', default=False)

    def invoke(self, context, event):
        if not self.filepath and bpy.data.filepath:
            self.filepath = bpy.data.filepath + '.bin'
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        msgs = []
        model = None if self.model == '__auto__' else self.model
        try:
          if self.auto_match:
            mp = (bpy.data.filepath + '.gamemap.json') if bpy.data.filepath else None
            _, errors = export_auto(context, self.spec_file, self.filepath, model=model, force=self.force,
                                    reference=bpy.path.abspath(self.reference_file) or None, mapping_path=mp,
                                    log=msgs.append, apply_modifiers=self.apply_modifiers,
                                    defer_deform=self.defer_deform, defer_subdiv=self.defer_subdiv, mirror=self.mirror)
          else:
            _, errors = export(context, self.spec_file, self.filepath, model=model, force=self.force,
                               log=msgs.append, keep_extra=self.keep_extra, apply_modifiers=self.apply_modifiers,
                               defer_deform=self.defer_deform, defer_subdiv=self.defer_subdiv, mirror=self.mirror, embed_linked=self.embed_linked)
        except Exception as e:
            self.report({'ERROR'}, 'Export failed: %s' % e); return {'CANCELLED'}
        for m in msgs:
            if m.startswith(('ERROR', 'WARN')): print(m)
        if errors and not self.force:
            self.report({'ERROR'}, '%d problem(s), nothing written: %s' % (len(errors), errors[0])); return {'CANCELLED'}
        self.report({'INFO'}, next((m[5:] for m in msgs if m.startswith('DONE')), 'Exported'))
        return {'FINISHED'}


class IMPORT_OT_game_model(bpy.types.Operator, ImportHelper):
    """Import a game .blend.bin (or _blend.json)"""
    bl_idname = 'import_scene.game_model_bin'
    bl_label = 'Import Game Model'
    filter_glob: StringProperty(default='*.bin;*.json', options={'HIDDEN'})

    def execute(self, context):
        try:
            import_document(context, load_document(self.filepath, _addon_specs()), os.path.basename(self.filepath))
        except Exception as e:
            self.report({'ERROR'}, 'Import failed: %s' % e); return {'CANCELLED'}
        return {'FINISHED'}


def _menu_export(self, context):
    self.layout.operator(EXPORT_OT_game_model.bl_idname, text='Game Model (.blend.bin)')


def _menu_import(self, context):
    self.layout.operator(IMPORT_OT_game_model.bl_idname, text='Game Model (.blend.bin / .json)')


_classes = (EXPORT_OT_game_model, IMPORT_OT_game_model)


def register():
    for c in _classes: bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for c in reversed(_classes): bpy.utils.unregister_class(c)


# =========================================================================
#  headless entry point
# =========================================================================
def cli(argv):
    import argparse
    ap = argparse.ArgumentParser(prog='gamemodel_blender')
    ap.add_argument('--spec', required=False)
    ap.add_argument('--export', metavar='OUT_BIN')
    ap.add_argument('--model')
    ap.add_argument('--json')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--keep-extra', action='store_true')
    ap.add_argument('--selected-only', action='store_true')
    ap.add_argument('--no-modifiers', action='store_true')
    ap.add_argument('--bake-deform', action='store_true')
    ap.add_argument('--embed-linked', action='store_true')
    ap.add_argument('--auto', action='store_true', help='auto-map pieces to the game\'s nodes')
    ap.add_argument('--reference', help='the game\'s original .blend.bin for this model')
    ap.add_argument('--mapping', help='mapping file to read/write (.gamemap.json)')
    ap.add_argument('--remap', action='store_true', help='ignore a saved mapping and auto-map again')
    ap.add_argument('--pivots', choices=('original', 'yours'), default='original')
    ap.add_argument('--whole-objects', action='store_true', help='don\'t split objects into loose parts')
    ap.add_argument('--scan', metavar='OUT_JSON', help='write scene scan + automatic mapping for review')
    ap.add_argument('--preview', metavar='OUT_JSON', help='write point samples for the alignment editor')
    ap.add_argument('--texture-slot', help='game texture path to reuse for your textures, e.g. ../model-textures/crashpad.png')
    ap.add_argument('--texture-file', help='that texture file on disk (it is overwritten; original kept as .orig)')
    ap.add_argument('--normal-slot', help='game normal-map texture path to reuse for your normal maps')
    ap.add_argument('--normal-file', help='that normal-map file on disk (overwritten; original kept as .orig)')
    ap.add_argument('--no-bake-normals', action='store_true', help="don't bake normal maps into the colour texture")
    ap.add_argument('--flip-green', action='store_true', help='flip the green channel of your normal maps')
    ap.add_argument('--keep-alpha', action='store_true', help='keep texture alpha (transparency)')
    ap.add_argument('--mirror', action='store_true', help='mirror your model left <-> right before fitting')
    ap.add_argument('--defer-subdiv', action='store_true', help='leave Subdivision modifiers to the game instead of applying them')
    ap.add_argument('--finish', choices=sorted(FINISHES), default='satin',
                    help="shine of your parts: satin (default), matte, gloss, or blend = from your materials")
    ap.add_argument('--single-sided', action='store_true', help="don't add back faces to open parts")
    ap.add_argument('--import', dest='imp')
    ap.add_argument('--save-blend')
    a = ap.parse_args(argv)
    log = lambda s: (print(s), sys.stdout.flush())
    if a.imp:
        import_document(bpy.context, load_document(a.imp, [a.spec] if a.spec else _addon_specs()),
                        os.path.basename(a.imp))
        log('DONE imported ' + a.imp)
    if a.preview:
        spec = zigspec.ZigSpec.load(a.spec)
        model = a.model or zigspec.model_from_filename(bpy.data.filepath)
        opts = {'apply_modifiers': not a.no_modifiers, 'defer_deform': not a.bake_deform, 'defer_subdiv': a.defer_subdiv, 'mirror': a.mirror,
                'split_parts': not a.whole_objects, 'texture_slot': a.texture_slot, 'keep_alpha': a.keep_alpha,
                'bake_normals': not a.no_bake_normals, 'flip_green': a.flip_green, 'finish': a.finish}
        ref, ref_path = find_reference(spec, a.export, a.reference, log, model, os.path.dirname(a.spec))
        if ref_path:                    # <game>/content/meshes/x.blend.bin -> <game> (its render settings are read live)
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(ref_path))))
            if os.path.isdir(os.path.join(root, 'design')): opts['game_root'] = root
        mp, sc, saved = get_mapping(bpy.context, spec, model, ref, a.mapping, a.remap, a.pivots, opts, log)
        mp['saved'] = saved
        write_preview(bpy.context, mp, ref, opts, a.preview, log=log)
        return 0
    if a.scan:
        spec = zigspec.ZigSpec.load(a.spec)
        model = a.model or zigspec.model_from_filename(bpy.data.filepath)
        ref, ref_path = find_reference(spec, a.export, a.reference, log, model, os.path.dirname(a.spec))
        mp, sc, saved = get_mapping(bpy.context, spec, model, ref, a.mapping, a.remap, a.pivots,
                                    {'apply_modifiers': not a.no_modifiers, 'defer_deform': not a.bake_deform, 'defer_subdiv': a.defer_subdiv, 'mirror': a.mirror,
                                     'split_parts': not a.whole_objects}, log)
        mp['saved'] = saved
        for o in sc['objects']:
            o.pop('points', None)
            o['islands'] = [{k: v for k, v in i.items() if k != 'points'} for i in o.get('islands', [])]
        with open(a.scan, 'w') as f:
            json.dump({'scan': sc, 'mapping': mp, 'reference': ref_path,
                       'ref_nodes': {n['name']: n['type'] for n in ref['nodes']} if ref else {}}, f, indent=1)
        log('DONE scanned')
        return 0
    if a.auto and (a.export or a.check):
        _, errors = export_auto(bpy.context, a.spec, a.export, model=a.model, reference=a.reference,
                                mapping_path=a.mapping, remap=a.remap, check_only=a.check, force=a.force,
                                json_path=a.json, pivots=a.pivots, log=log, split_parts=not a.whole_objects,
                                texture_slot=a.texture_slot, texture_file=None if a.check else a.texture_file,
                                normal_slot=a.normal_slot, normal_file=None if a.check else a.normal_file,
                                flip_green=a.flip_green, bake_normals=not a.no_bake_normals, finish=a.finish, keep_alpha=a.keep_alpha, double_sided=not a.single_sided,
                                apply_modifiers=not a.no_modifiers, defer_deform=not a.bake_deform, defer_subdiv=a.defer_subdiv, mirror=a.mirror)
        return 2 if errors else 0
    if a.export or a.check:
        _, errors = export(bpy.context, a.spec, a.export, model=a.model, json_path=a.json, check_only=a.check,
                           force=a.force, log=log, keep_extra=a.keep_extra, selected_only=a.selected_only,
                           apply_modifiers=not a.no_modifiers, defer_deform=not a.bake_deform, defer_subdiv=a.defer_subdiv, mirror=a.mirror,
                           embed_linked=a.embed_linked)
        if errors and (a.check or not a.force):
            return 2
    if a.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(a.save_blend))
        log('DONE saved ' + a.save_blend)
    return 0


if __name__ == '__main__':
    if '--' in sys.argv:
        try:
            code = cli(sys.argv[sys.argv.index('--') + 1:])
        except Exception as e:
            import traceback; traceback.print_exc()
            print('ERROR %s' % e); sys.stdout.flush(); code = 1
        sys.stdout.flush()
        if code: sys.exit(code)
    else:
        register()

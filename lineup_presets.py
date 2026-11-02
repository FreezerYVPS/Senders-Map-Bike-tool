"""
lineup_presets.py - saved line-ups ("presets") for the bike editor.

A preset is the line-up you made in Edit model (size/position fit, each part's move/turn/scale, per-piece moves,
game materials) saved under a name, e.g. "MX Simulator → bike-4stroke". Imported MX Simulator / MX Bikes bikes all
come in with the same part names (frame, handlebars.top, handlebars.bottom, swingarm), so one preset lines up every
bike from that game. Stored as small JSON files in %APPDATA%\\GameModelExporter\\lineup_presets.
"""
import copy
import json
import os
import re
import time


def folder():
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    d = os.path.join(base, 'GameModelExporter', 'lineup_presets')
    os.makedirs(d, exist_ok=True)
    return d


def _file(name):
    return os.path.join(folder(), (re.sub(r'[^A-Za-z0-9 _.()+-]+', '_', name).strip() or 'preset') + '.json')


def list_presets():
    """[{'name', 'path', 'family', 'model', 'saved'}], newest first."""
    out = []
    try:
        names = os.listdir(folder())
    except OSError:
        return out
    for f in names:
        if not f.lower().endswith('.json'): continue
        p = os.path.join(folder(), f)
        try:
            with open(p) as fh: d = json.load(fh)
        except (OSError, ValueError):
            continue
        out.append({'name': d.get('name') or f[:-5], 'path': p, 'family': d.get('family') or '',
                    'model': d.get('model') or '', 'saved': d.get('saved', 0)})
    out.sort(key=lambda d: -d['saved'])
    return out


def find(name):
    return next((p for p in list_presets() if p['name'] == name), None)


def best_for(family, model=None):
    """The newest preset made for this kind of import (and this game model, if one fits)."""
    ps = [p for p in list_presets() if family and p['family'] == family]
    return next((p for p in ps if p['model'] == model), None) or (ps[0] if ps else None)


def save(name, mapping, family='', note=''):
    d = {'name': name, 'family': family, 'note': note, 'saved': time.time(), 'model': mapping.get('model'),
         'fit': mapping.get('fit'), 'adjust': mapping.get('adjust') or {}, 'piece_adjust': mapping.get('piece_adjust') or {},
         'paint': mapping.get('paint') or {}, 'mirror': bool(mapping.get('mirror')),
         'keep': sorted(u['base'] for u in mapping.get('units', []) if u.get('keep_original'))}
    p = _file(name)
    with open(p, 'w') as fh: json.dump(d, fh, indent=1)
    return p


def load(path):
    with open(path) as fh: return json.load(fh)


def apply(mapping, preset, with_fit=True):
    """Put a preset's line-up onto a mapping (in place). Per-piece moves and materials only where the same pieces
    exist. Returns a short description."""
    mapping['adjust'] = copy.deepcopy(preset.get('adjust') or {})
    if with_fit and preset.get('fit'): mapping['fit'] = copy.deepcopy(preset['fit'])
    keys = {'%s|%d' % (s[0], s[2] if len(s) > 2 and s[2] is not None else -1)
            for u in mapping.get('units', []) for sp in u.get('splits', []) for s in sp.get('sources', [])}
    pa = {k: v for k, v in (preset.get('piece_adjust') or {}).items() if k in keys}
    pt = {k: v for k, v in (preset.get('paint') or {}).items() if k in keys}
    mapping['piece_adjust'] = copy.deepcopy(pa)
    mapping.setdefault('paint', {}).update(copy.deepcopy(pt))
    for u in mapping.get('units', []):
        if u.get('kind') == 'mesh' and u['base'] in (preset.get('keep') or []) and not any(
                sp.get('sources') for sp in u.get('splits', [])):
            u['keep_original'] = True
    return '%d part moves%s%s' % (len(mapping['adjust']), ', %d piece moves' % len(pa) if pa else '',
                                  ', %d materials' % len(pt) if pt else '')

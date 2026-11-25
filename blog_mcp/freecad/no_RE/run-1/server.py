#!/usr/bin/env python3
"""FreeCAD MCP server.

Drives the running FreeCAD 0.20 window through cua-input (its own menus, dialogs
and widgets only -- never a console, macro editor or script) and reads results
back by OCR of the window, so a solver can create objects, change properties,
run operations, open/save/export files and inspect computed values.

Units: FreeCAD's default length unit is the millimetre (mm); angles are degrees.
Property values may be given as plain numbers ("25") or with units ("25 mm").
"""
import difflib
import json
import sys
import time
import traceback

import numpy as np
from PIL import Image

import cua
import ocr

SCREEN = (1280, 800)

# ---- fixed screen geometry of the running FreeCAD window (1280x800) ----
MENUBAR_Y = 79
WB_COMBO = (560, 108)
WB_ITEMS = ['Arch', 'Draft', 'Drawing', 'FEM', 'Image', 'Inspection', 'Mesh Design',
            'None', 'OpenSCAD', 'Part Design', 'Part', 'Path', 'Points', 'Raytracing',
            'Reverse Engineering', 'Robot', 'Sketcher', 'Spreadsheet', 'Start',
            'Surface', 'TechDraw', 'Test Framework', 'Web']
WB_Y = [44, 64, 90, 116, 135, 155, 181, 207, 231, 253, 277, 301, 324, 346, 369, 393,
        416, 440, 465, 489, 512, 536, 556]
PART_PRIMITIVES_Y = 115
PRIM_X = 600
PRIM_Y = {'cube': 115, 'cylinder': 139, 'sphere': 164, 'cone': 189, 'torus': 211, 'tube': 235}
PART_CHECKGEO_Y = 302
PART_BOOLEAN_Y = 349
BOOL_X = 600
BOOL_Y = {'cut': 373, 'union': 395, 'fuse': 395, 'intersection': 417, 'common': 417}

TREE_BOX = (2, 232, 142, 478)     # model tree (labels column)
PROP_BOX = (2, 496, 262, 748)     # property editor (name | value)
VAL_X = 108                        # x where the value column starts
NAME_X1 = 106

PROP_NAMES = ['Length', 'Width', 'Height', 'Radius', 'Radius1', 'Radius2', 'Angle',
              'Angle1', 'Angle2', 'Angle3', 'Label', 'Placement', 'Support', 'Map Mode',
              'Attachment', 'Base', 'Box', 'Cylinder', 'Sphere', 'Cone', 'Torus', 'Tube',
              'Inner Radius', 'Outer Radius', 'Shape', 'Position', 'Axis', 'Volume',
              'Surface', 'View', 'Visibility', 'Line Color', 'Point Size', 'Transparency',
              'Deviation', 'Angular Deflection', 'Light Source', 'Bounding Box', 'Mass',
              'Center of Mass', 'Matrix', 'Expression Engine', 'Label2', 'Description',
              'ID', 'Type', 'Color', 'First Angle', 'Second Angle', 'Third Angle']


def snap_name(txt, cands=PROP_NAMES, cut=0.55):
    t = (txt or '').strip().lower()
    if not t:
        return txt
    best, bs = txt, 0.0
    for c in cands:
        r = difflib.SequenceMatcher(None, t, c.lower()).ratio()
        if r > bs:
            bs, best = r, c
    return best if bs >= cut else txt.strip()

_SHOOT = '/tmp/_mcp_shot.png'
CREATED = []          # (label, tree row index) of objects this session created


# --------------------------------------------------------------------------- #
# low level helpers
# --------------------------------------------------------------------------- #
def shoot():
    cua.screenshot(_SHOOT)
    return Image.open(_SHOOT).convert('RGB')


def ink_image():
    im = shoot()
    return 1.0 - np.asarray(im.convert('L')).astype(np.float32) / 255.0, im


def escape(times=1):
    for _ in range(times):
        cua.key('Escape')
        time.sleep(0.3)


def dismiss_dialogs():
    """Cancel a stray modal dialog (Document Recovery after a restart, errors).

    Escape cancels modal dialogs in FreeCAD; a click on the recovery dialog's
    Cancel button is tried as a fallback.
    """
    escape(2)
    return True


def _rows_with_text(reg, sat, x_from=0, th=0.35, dth=0.3, sat_cut=70):
    out = []
    for (a, b) in ocr.text_rows(reg, th):
        row = reg[a:b]
        med = np.median(row)
        d = np.abs(row - med)
        if sat is not None:
            d = d.copy()
            d[sat[a:b] > sat_cut] = 0
        out.append((a, b, d, med))
    return out


def _repaint_panel():
    """Force the Combo View to repaint (it sometimes stays blank after switches)."""
    cua.click(88, 200)
    time.sleep(0.4)
    cua.click(46, 200)
    time.sleep(0.5)


def _left_bands():
    """Text rows of the left dock (Combo View), with their y-bands and OCR text."""
    im = shoot()
    arr = 1.0 - np.asarray(im.convert('L')).astype(np.float32) / 255.0
    left = arr[150:790, 0:266]
    out = []
    for (a, b) in ocr.text_rows(left, 0.35):
        row = left[a:b]
        med = np.median(row)
        d = np.abs(row - med)
        out.append((150 + a, 150 + b, ocr.read_text(d, 0.3).strip()))
    return out


def _headers(bands):
    """Locate the 'Labels & Attributes' and 'Property | Value' header rows.

    OCR is imperfect, so the best-scoring row above a threshold wins.
    """
    bp = bt = 0.0
    prop_y = tree_y = None
    for y0, y1, txt in bands:
        t = txt.lower().replace(' ', '')
        rp = difflib.SequenceMatcher(None, t, 'propertyvalue').ratio()
        rt = difflib.SequenceMatcher(None, t, 'labelsattributes').ratio()
        if rp > bp:
            bp, prop_y = rp, y1
        if rt > bt:
            bt, tree_y = rt, y1
    if bp < 0.35:
        prop_y = None
    if bt < 0.38:
        tree_y = None
    return tree_y, prop_y


def read_tree():
    for _ in range(2):
        bands = _left_bands()
        tree_y, prop_y = _headers(bands)
        if tree_y is None or prop_y is None:
            _repaint_panel()
            continue
        rows = [(t, (a + b) // 2) for a, b, t in bands if a >= tree_y and b < prop_y]
        return [r for r in rows if r[0].strip()]
    return []


def read_props():
    im = shoot()
    arr = 1.0 - np.asarray(im.convert('L')).astype(np.float32) / 255.0
    bands = _left_bands()
    _, prop_y = _headers(bands)
    if prop_y is None:
        _repaint_panel()
        bands = _left_bands()
        _, prop_y = _headers(bands)
    if prop_y is None:
        prop_y = 490          # default header position in the Part workbench layout
    out = []
    for y0, y1, raw in bands:
        if y0 <= prop_y or y0 > 745:
            continue
        low = raw.lower()
        if ('view' in low and 'data' in low) or low.strip() in ('view', 'data', 'model', 'tasks'):
            continue
        ncol = arr[y0:y1, 3:NAME_X1]
        if float(np.median(ncol)) > 0.4:
            d = np.abs(ncol - np.median(ncol))
            name = ocr.read_text(d, 0.3) or ocr.read_text((ncol < 0.35).astype(np.float32), 0.5)
        else:
            name = ocr.read_text(ncol, 0.35)
        vcol = arr[y0:y1, VAL_X:264]
        if float(np.median(vcol)) > 0.4:
            d = np.abs(vcol - np.median(vcol))
            val = ocr.read_text(d, 0.3) or ocr.read_text((vcol < 0.35).astype(np.float32), 0.5)
        else:
            val = ocr.read_text(vcol, 0.35)
        out.append((snap_name(name).strip(), val.strip(), (y0 + y1) // 2))
    return out


def _match(target, txt):
    t = target.strip().lower()
    c = txt.strip().lower()
    best = difflib.SequenceMatcher(None, t, c).ratio()
    for cut in range(1, 5):
        if len(c) > cut:
            best = max(best, difflib.SequenceMatcher(None, t, c[cut:]).ratio())
    return best


def deselect_all():
    """Clear the tree/property selection so selected rows render normally."""
    cua.click(700, 420)
    time.sleep(0.4)


def _wait_panel(timeout=6.0):
    """Wait until the property editor shows at least one row."""
    end = time.time() + timeout
    while time.time() < end:
        if read_props():
            return True
        time.sleep(0.3)
    return False


def _label_of_selection():
    for n, v, _ in read_props():
        if n.strip().lower() == 'label':
            return v.strip()
    return ''


def _fuzzy_index(rows, label):
    best, score = None, 0.0
    for i, (txt, y) in enumerate(rows):
        sc = _match(label, txt)
        if sc > score:
            score, best = sc, i
    return best if (best is not None and score >= 0.55) else None


def _resolve(label):
    """Index of the tree row for this object Label.

    Objects the MCP created are tracked in creation order, which is the most
    reliable identifier (tree label OCR is imperfect).  Otherwise fall back to a
    fuzzy match on the tree text.
    """
    rows = read_tree()
    key = label.strip().lower()
    for lab, idx in CREATED:
        if lab.strip().lower() == key and 0 <= idx < len(rows):
            return idx
    return _fuzzy_index(rows, label)


def find_row(label):
    i = _resolve(label)
    if i is None:
        return None
    rows = read_tree()
    return rows[i][1] if i < len(rows) else None


def select_object(label):
    i = _resolve(label)
    if i is None:
        return False
    rows = read_tree()
    if i >= len(rows):
        return False
    cua.click(60, rows[i][1])
    time.sleep(0.5)
    _wait_panel()
    return True


def _collapse_expanded():
    """Collapse an expanded Placement row so all properties are visible."""
    rows = read_props()
    names = [n.strip().lower() for n, _, _ in rows]
    if not any(('axis' in n or 'position' in n) for n in names):
        return False
    for n, v, y in rows:
        if 'placement' in n.strip().lower() or _match('placement', n) >= 0.6:
            cua.double_click(140, y)      # double-click the value toggles expansion
            time.sleep(0.7)
            return True
    return False


def _name_match(prop, name):
    n = (name or '').strip().lower()
    p = prop.strip().lower()
    return n == p or p in n or _match(prop, name) >= 0.5


def _value_ok(want, got):
    w = str(want).strip().replace(' ', '')
    g = (got or '').strip().replace(' ', '')
    return bool(w) and (w in g or g.startswith(w))


def set_property(prop, value):
    """Find a property row by name, type a new value and verify it stuck."""
    _wait_panel()
    _collapse_expanded()
    for attempt in range(4):
        hit = None
        for name, val, y in read_props():
            if _name_match(prop, name):
                hit = (name, val, y)
                break
        if hit is not None:
            cua.double_click(140, hit[2])
            time.sleep(0.35)
            cua.key('ctrl', 'a')
            time.sleep(0.1)
            cua.key('Delete')
            time.sleep(0.1)
            cua.type_text(str(value))
            time.sleep(0.15)
            cua.key('Return')
            time.sleep(0.5)
            for name, val, y in read_props():
                if _name_match(prop, name):
                    if _value_ok(value, val):
                        return True
                    break
        time.sleep(0.4)
    for _ in range(3):
        cua.click(150, 600)
        time.sleep(0.2)
        cua.scroll('down', 3)
        time.sleep(0.5)
        for name, val, y in read_props():
            if _name_match(prop, name):
                cua.double_click(140, y)
                time.sleep(0.35)
                cua.key('ctrl', 'a')
                time.sleep(0.1)
                cua.key('Delete')
                time.sleep(0.1)
                cua.type_text(str(value))
                time.sleep(0.15)
                cua.key('Return')
                time.sleep(0.5)
                return True
    return False


def current_workbench():
    arr, im = ink_image()
    return ocr.read_text(arr[100:117, 505:672])


def _open_wb_popup():
    """Click the workbench combo; return True if its list popup opened."""
    before = np.asarray(shoot()).astype(np.float32)[40:560, 495:665]
    cua.click(*WB_COMBO)
    time.sleep(0.9)
    after = np.asarray(shoot()).astype(np.float32)[40:560, 495:665]
    return float(np.abs(after - before).mean()) > 5.0


def set_workbench(name):
    name_l = name.strip().lower()
    names_l = [w.lower() for w in WB_ITEMS]
    if name_l not in names_l:
        return False
    idx = names_l.index(name_l)
    for _ in range(4):
        if _open_wb_popup():
            cua.key('Home')
            time.sleep(0.25)
            for _ in range(idx):
                cua.key('Down')
                time.sleep(0.04)
            cua.key('Return')
            time.sleep(1.2)
            _repaint_panel()
            return True
        escape(1)
    return False


def _last_object_row():
    rows = [r for r in read_tree() if r[0].strip()]
    return rows[-1][1] if rows else None


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
def t_new_document():
    dismiss_dialogs()
    cua.key('ctrl', 'n')
    time.sleep(1.2)
    tree = [t for t, _ in read_tree() if t.strip()]
    return 'Created a new document. Model tree: %s' % (tree or ['(empty)'],)


def t_set_workbench(workbench):
    ok = set_workbench(workbench)
    return ('Activated workbench %r.' % workbench) if ok else ('ERROR: could not open the workbench list.')


def t_create_primitive(kind, dimensions=None, name=None):
    kind = (kind or '').strip().lower()
    if kind not in PRIM_Y:
        return 'ERROR: unknown primitive %r; choose one of %s.' % (kind, sorted(PRIM_Y))
    if not set_workbench('Part'):
        return 'ERROR: could not activate the Part workbench.'
    before = [t for t, _ in read_tree()]
    for _ in range(3):
        cua.key('alt', 'p')
        time.sleep(0.7)
        cua.click(290, PART_PRIMITIVES_Y)
        time.sleep(0.7)
        cua.click(PRIM_X, PRIM_Y[kind])
        time.sleep(1.4)
        if len([t for t, _ in read_tree()]) > len(before):
            break
    rows = read_tree()
    if rows:
        cua.click(60, rows[-1][1])
        time.sleep(0.5)
    _wait_panel()
    if name:
        CREATED.append((name, len(rows) - 1))
    results = []
    if name:
        results.append('Label=%s' % name if set_property('Label', name) else 'Label=FAILED')
    dims = dict(dimensions or {})
    for prop, val in dims.items():
        results.append('%s=%s' % (prop, val) if set_property(prop, val) else '%s=FAILED' % prop)
    # verify against the panel and retry anything that did not stick
    for _ in range(2):
        panel = {n: v for n, v, _ in read_props() if n}
        bad = []
        for prop, val in dims.items():
            got = next((v for n, v in panel.items() if _name_match(prop, n)), None)
            if got is None or not _value_ok(val, got):
                bad.append(prop)
        if not bad:
            break
        for prop in bad:
            set_property(prop, dims[prop])
    props = {n: v for n, v, _ in read_props() if n}
    return 'Created %s. Set: %s. Properties now: %s' % (kind, results or '(defaults)', props)


def t_list_objects():
    return 'Model tree: %s' % ([t for t, _ in read_tree() if t.strip()],)


def t_get_properties(object=None):
    if object:
        if not select_object(object):
            return 'ERROR: object %r not found in the model tree. Tree: %s' % (object, [t for t, _ in read_tree()])
    props = read_props()
    if not props:
        return 'No properties shown (is an object selected?).'
    return 'Properties of the selected object:\n' + '\n'.join('  %s = %s' % (n, v) for n, v, _ in props)


def t_set_property(object, property, value):
    if not select_object(object):
        return 'ERROR: object %r not found. Tree: %s' % (object, [t for t, _ in read_tree()])
    if set_property(property, value):
        props = {n: v for n, v, _ in read_props() if n}
        return 'Set %s.%s = %r. Now: %s' % (object, property, value, props)
    return 'ERROR: property %r not found on %r. Properties: %s' % (
        property, object, [n for n, _, _ in read_props()])


def t_boolean_operation(operation, base, tool, name=None):
    op = (operation or '').strip().lower()
    if op not in BOOL_Y:
        return 'ERROR: operation must be cut, union/fuse or intersection/common.'
    if not set_workbench('Part'):
        return 'ERROR: could not activate the Part workbench.'
    bi = _resolve(base)
    ti = _resolve(tool)
    rows = read_tree()
    if bi is None:
        return 'ERROR: base object %r not found. Tree: %s' % (base, [t for t, _ in rows])
    if ti is None:
        return 'ERROR: tool object %r not found. Tree: %s' % (tool, [t for t, _ in rows])
    cua.click(60, rows[bi][1])
    time.sleep(0.6)
    step = 1 if ti > bi else -1
    for _ in range(abs(ti - bi)):
        cua.key('shift', 'Down' if step > 0 else 'Up')
        time.sleep(0.15)
    cua.key('alt', 'p')
    time.sleep(0.7)
    cua.click(290, PART_BOOLEAN_Y)
    time.sleep(0.7)
    cua.click(BOOL_X, BOOL_Y[op])
    time.sleep(1.8)
    tree = [t for t, _ in read_tree() if t.strip()]
    return 'Ran Part boolean %s(%s, %s). Model tree: %s' % (op, base, tool, tree)


def t_check_geometry(object=None):
    """Report FreeCAD's computed validity for an object.

    The most reliable source is the status bar, which shows e.g.
    "Valid, internal name: Box" for the current selection.
    """
    if object and not select_object(object):
        return 'ERROR: object %r not found. Tree: %s' % (object, [t for t, _ in read_tree()])
    time.sleep(0.5)
    im = shoot()
    arr = 1.0 - np.asarray(im.convert('L')).astype(np.float32) / 255.0
    status = ocr.read_text(arr[774:790, 5:520], 0.35).strip()
    return 'FreeCAD status bar: %s' % (status or '(nothing shown - select an object first)')


def t_delete_object(object):
    if not select_object(object):
        return 'ERROR: object %r not found.' % object
    cua.key('Delete')
    time.sleep(0.9)
    return 'Deleted %r. Model tree: %s' % (object, [t for t, _ in read_tree() if t.strip()])


def _file_dialog_open():
    """A modal file dialog covers the centre with a light grey panel, hiding the
    dark-blue 3D view; detect by the fraction of blue pixels in the centre."""
    im = shoot()
    rgb = np.asarray(im).astype(np.float32)
    reg = rgb[220:620, 340:940]
    blue = ((reg[:, :, 2] > reg[:, :, 0] + 25) & (reg[:, :, 2] > reg[:, :, 1] + 25)).mean()
    return float(blue) < 0.30


def _run_file_dialog(path):
    """Wait for a file dialog, type the path into its name field, confirm."""
    for _ in range(14):
        if _file_dialog_open():
            break
        time.sleep(0.5)
    time.sleep(0.8)
    for attempt in range(2):
        cua.click(640, 589)
        time.sleep(0.5)
        cua.key('ctrl', 'a')
        time.sleep(0.15)
        cua.type_text(path)
        time.sleep(0.5)
        cua.key('Return')
        time.sleep(2.5)
        if not _file_dialog_open():
            return True
    return not _file_dialog_open()


def _open_file_dialog(shortcut, menu_index):
    """Open a File dialog via the File menu (keyboard), falling back to a shortcut."""
    if _file_dialog_open():
        return True
    cua.click(700, 420)          # focus the application window
    time.sleep(0.4)
    cua.key('alt', 'f')
    time.sleep(0.8)
    for _ in range(menu_index):
        cua.key('Down')
        time.sleep(0.12)
    cua.key('Return')
    time.sleep(1.8)
    if _file_dialog_open():
        return True
    cua.key(*shortcut)
    time.sleep(1.8)
    return _file_dialog_open()


def _type_path(path):
    cua.click(640, 589)
    time.sleep(0.5)
    cua.key('ctrl', 'a')
    time.sleep(0.15)
    cua.key('Delete')
    time.sleep(0.15)
    cua.type_text(path)
    time.sleep(0.5)
    cua.key('Return')
    time.sleep(1.5)
    cua.key('Return')          # confirm an overwrite/duplicate-path prompt if shown
    time.sleep(2.5)


def t_save_document(path):
    escape(1)
    time.sleep(0.4)
    if not _open_file_dialog(('ctrl', 'shift', 's'), 5):
        return 'ERROR: could not open the Save As dialog.'
    _type_path(path)
    return 'Save As -> %s (%s).' % (path, 'completed' if not _file_dialog_open() else 'dialog still open')


def t_open_document(path):
    escape(1)
    time.sleep(0.4)
    if not _open_file_dialog(('ctrl', 'o'), 1):
        return 'ERROR: could not open the Open dialog.'
    _type_path(path)
    tree = [t for t, _ in read_tree() if t.strip()]
    return 'Open %s (%s). Model tree: %s' % (path, 'completed' if not _file_dialog_open() else 'dialog still open', tree)


def t_export_document(path):
    escape(1)
    time.sleep(0.4)
    if not _open_file_dialog(('ctrl', 'e'), 10):
        return 'ERROR: could not open the Export dialog.'
    _type_path(path)
    return 'Export -> %s (%s).' % (path, 'completed' if not _file_dialog_open() else 'dialog still open')


TOOLS = {
    'new_document': (t_new_document, 'Create a new empty FreeCAD document (File > New).',
                     {'type': 'object', 'properties': {}}),
    'set_workbench': (t_set_workbench, 'Switch the active workbench, e.g. "Part", "Part Design", "Sketcher", "Draft", "Spreadsheet".',
                      {'type': 'object', 'properties': {'workbench': {'type': 'string'}}, 'required': ['workbench']}),
    'create_primitive': (t_create_primitive,
                         'Create a Part primitive and set its dimensions. dimensions maps FreeCAD property names to values '
                         '(mm), e.g. cube: {"Length":10,"Width":20,"Height":30}; cylinder: {"Radius":5,"Height":20,"Angle":360}; '
                         'sphere: {"Radius":5}; cone: {"Radius1":5,"Radius2":0,"Height":10}; torus: {"Radius1":10,"Radius2":2}; '
                         'tube: {"Inner Radius":3,"Outer Radius":5,"Height":10}. name sets the object Label.',
                         {'type': 'object', 'properties': {
                             'kind': {'type': 'string', 'enum': ['cube', 'cylinder', 'sphere', 'cone', 'torus', 'tube']},
                             'dimensions': {'type': 'object'},
                             'name': {'type': 'string'}}, 'required': ['kind']}),
    'list_objects': (t_list_objects, 'List the objects currently in the document (model tree labels).',
                     {'type': 'object', 'properties': {}}),
    'get_properties': (t_get_properties, 'Select an object and read its property editor (Data tab): dimensions, placement, label, etc. '
                       'Values include units exactly as FreeCAD displays them. Reads the window by OCR, so a name may be '
                       'slightly garbled; values are reliable.',
                       {'type': 'object', 'properties': {'object': {'type': 'string'}}}),
    'set_property': (t_set_property, 'Select an object and set one property in its property editor, e.g. property "Length", value 25, or '
                     '"Label", value "MyPart". Also works for nested rows such as Placement sub-properties ("Position", "Angle"). '
                     'The result text lists the panel values afterwards; if a value did not stick, call get_properties to check and set it again.',
                     {'type': 'object', 'properties': {'object': {'type': 'string'},
                                                       'property': {'type': 'string'},
                                                       'value': {'type': ['string', 'number']}},
                      'required': ['object', 'property', 'value']}),
    'boolean_operation': (t_boolean_operation, 'Combine two objects with a Part boolean: cut, union (fuse) or intersection (common).',
                          {'type': 'object', 'properties': {'operation': {'type': 'string', 'enum': ['cut', 'union', 'fuse', 'intersection', 'common']},
                                                            'base': {'type': 'string'}, 'tool': {'type': 'string'}, 'name': {'type': 'string'}},
                           'required': ['operation', 'base', 'tool']}),
    'check_geometry': (t_check_geometry, 'Select an object and run Part > Check Geometry (a computed result). Returns FreeCAD\'s summary '
                       'text; because it is read by OCR it can be imperfect — the object\'s status bar text '
                       '(\"Valid, internal name: …\") is the most reliable validity signal.',
                       {'type': 'object', 'properties': {'object': {'type': 'string'}}}),
    'delete_object': (t_delete_object, 'Delete an object from the document.',
                      {'type': 'object', 'properties': {'object': {'type': 'string'}}, 'required': ['object']}),
    'save_document': (t_save_document, 'Save the current document to a path (Save As), e.g. /tmp/part.FCStd.',
                      {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}),
    'open_document': (t_open_document, 'Open a FreeCAD document (.FCStd) from a path.',
                      {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}),
    'export_document': (t_export_document, 'Export the document to a file (File > Export). The file format is taken from the dialog\'s '
                        '\"Files of type\" filter, so pass a matching extension (.stl, .obj, .step, .iges, .brep). '
                        'The MCP cannot read the exported file back.',
                        {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']}),
}


# --------------------------------------------------------------------------- #
# MCP JSON-RPC
# --------------------------------------------------------------------------- #
def rpc_result(id_, result):
    return {'jsonrpc': '2.0', 'id': id_, 'result': result}


def rpc_error(id_, code, message):
    return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': code, 'message': message}}


def handle(msg):
    method = msg.get('method')
    id_ = msg.get('id')
    if method == 'initialize':
        return rpc_result(id_, {
            'protocolVersion': msg.get('params', {}).get('protocolVersion', '2024-11-05'),
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'freecad-mcp', 'version': '1.0'},
            'instructions': (
                'Drives a running FreeCAD 0.20 window through its own menus, dialogs and widgets '
                '(never a console or script). '
                'UNITS: lengths are millimetres (mm), angles degrees; FreeCAD default unit is mm. '
                'OBJECTS are addressed by their tree Label (see list_objects). '
                'Property values may be plain numbers ("25") or with units ("25 mm"). '
                'READING: get_properties/list_objects read the window by OCR; numeric values are '
                'reliable, some names/labels may be slightly garbled (especially the currently '
                'selected row). Treat them as best-effort and cross-check with list_objects. '
                'FILES: save_document/open_document/export_document type a path into FreeCAD\'s own '
                'file dialog; the files live in the application\'s filesystem and the MCP cannot read '
                'them back. Typical flow: create_primitive -> set_property/boolean_operation -> '
                'get_properties/check_geometry -> save_document/export_document.'
            )})
    if method == 'notifications/initialized' or method is None:
        return None
    if method == 'ping':
        return rpc_result(id_, {})
    if method == 'tools/list':
        return rpc_result(id_, {'tools': [
            {'name': n, 'description': d, 'inputSchema': s} for n, (_, d, s) in TOOLS.items()]})
    if method == 'tools/call':
        params = msg.get('params', {})
        name = params.get('name')
        args = params.get('arguments', {}) or {}
        if name not in TOOLS:
            return rpc_result(id_, {'content': [{'type': 'text', 'text': 'Unknown tool %r' % name}], 'isError': True})
        try:
            dismiss_dialogs()
            text = TOOLS[name][0](**args)
            is_err = isinstance(text, str) and text.startswith('ERROR')
        except TypeError as e:
            text = 'Bad arguments for %s: %s' % (name, e); is_err = True
        except Exception as e:
            text = 'Tool %s failed: %s\n%s' % (name, e, traceback.format_exc()); is_err = True
        return rpc_result(id_, {'content': [{'type': 'text', 'text': text}], 'isError': is_err})
    if id_ is None:
        return None
    return rpc_error(id_, -32601, 'Method not found: %s' % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            resp = handle(msg)
        except Exception:
            resp = rpc_error(msg.get('id'), -32603, traceback.format_exc())
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()

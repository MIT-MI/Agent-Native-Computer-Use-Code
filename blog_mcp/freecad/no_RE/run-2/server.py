#!/usr/bin/env python3
"""MCP server for FreeCAD 0.20 (graphical session).

The server drives the running FreeCAD window the way a person does: it picks
menu commands, fills dialogs and reads the numbers FreeCAD displays.  It never
opens FreeCAD's files, never uses the Python console and never runs scripts
inside the application.

Run it as an MCP stdio server: JSON-RPC 2.0 messages, one per line, on stdin
and stdout; diagnostics go to stderr.
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fc
import vision

SERVER_NAME = 'freecad-gui'
SERVER_VERSION = '1.0'

INSTRUCTIONS = """\
This server drives a running FreeCAD 0.20 graphical session the way a person
does: it picks the program's own menu commands, fills its dialogs, and reads
the numbers FreeCAD draws on screen.  It never opens FreeCAD's files, never
uses the Python console and never runs scripts inside the application.

Units and formats
  * Lengths are millimetres, angles degrees.  Any value field accepts a plain
    number ("30") or a value with a unit ("30 mm", "1.5 in", "45 deg").
  * Object names are the labels FreeCAD shows in the model tree ("Box",
    "Cylinder", ...).  Objects made in one document are numbered by FreeCAD,
    so the first Box you create is "Box", the next "Box001".
  * Everything happens in the document that is open.  Call new_document first,
    or open_document(path), before creating geometry.

What a session usually looks like
  1. new_document
  2. set_workbench("Part")
  3. create_primitive(kind="Box", length=30, width=20, height=10)
  4. get_property("Box", "Length")          -> "30.00 mm"
  5. set_property("Box", "Length", "45")    -> reads the value back
  6. check_geometry("Box")                  -> volume, area, face counts
  7. save_document("/tmp/part.FCStd")

Files
  save_document writes the document with FreeCAD's own Save dialog.  The path
  is a path on the machine FreeCAD runs on (for example /tmp/part.FCStd).
  The server cannot read the file back; use open_document to load it again.
  close_document(save=true) saves to the current file name first.

Behaviour, timing and limits
  * Every call takes roughly 1-4 seconds because it works the real user
    interface; a few calls take longer while FreeCAD redraws.
  * Values are read by recognising the digits FreeCAD draws, so a value is
    reported as the text shown on screen, including the unit FreeCAD appends.
    Reading is reliable for numbers but can mis-read a value that is scrolled
    out of view or covered; the tools say so instead of guessing.
  * The model tree and the property editor are driven with the keyboard, so
    the object you name is looked up by its position in the tree.
  * If a dialog or task panel is in the way, close_dialog (Escape) dismisses
    it.  app_info reports what FreeCAD is showing right now.
"""


def _obj(name, desc, props, required=()):
    return {'name': name, 'description': desc,
            'inputSchema': {'type': 'object', 'properties': props, 'required': list(required)}}


TOOLS = [
    _obj('app_info', 'Report what FreeCAD currently shows: window rectangle, active workbench, '
         'open documents, objects in the model tree and the text in the status bar.',
         {}),
    _obj('new_document', 'Create a new empty document (File > New). Any open document is kept.',
         {}),
    _obj('open_document', 'Open a .FCStd (or importable) file (File > Open).',
         {'path': {'type': 'string', 'description': 'absolute path on the machine FreeCAD runs on'}},
         ['path']),
    _obj('save_document', 'Save the active document to `path` (File > Save As...). The file is '
         'written by FreeCAD, so it stays on FreeCAD\'s machine; this tool cannot read it back.',
         {'path': {'type': 'string', 'description': 'absolute path, usually ending in .FCStd'}},
         ['path']),
    _obj('close_document', 'Close the active document (File > Close), optionally saving first.',
         {'save': {'type': 'boolean', 'description': 'save before closing (default false)'}}),
    _obj('undo', 'Undo the last modelling action (Ctrl+Z).', {}),
    _obj('redo', 'Redo the last undone action (Ctrl+Y).', {}),
    _obj('set_workbench', 'Switch the workbench selector, e.g. "Part", "Part Design", '
         '"Sketcher", "Spreadsheet", "Draft", "Mesh Design", "Start".',
         {'name': {'type': 'string', 'description': 'workbench name as shown in the selector'}},
         ['name']),
    _obj('create_primitive',
         'Create a geometric primitive (Part workbench) and set its measurements.  Only the '
         'parameters that belong to the chosen kind are used; the others are ignored.  The new '
         'object appears in the model tree as Box, Cylinder, Sphere, ...  This tool walks '
         'FreeCAD\'s Part > Primitives menu, which is the least steady part of the interface: if '
         'it reports that the primitive could not be created, simply call it again (the state is '
         'left clean, so a second or third call usually succeeds).',
         {'kind': {'type': 'string', 'description': 'Box, Cylinder, Sphere, Cone, Torus, Tube, '
                   'Ellipsoid, Prism, Wedge or Plane'},
          'length': {'type': 'number'}, 'width': {'type': 'number'},
          'height': {'type': 'number'}, 'radius': {'type': 'number'},
          'radius1': {'type': 'number'}, 'radius2': {'type': 'number'},
          'angle': {'type': 'number', 'description': 'degrees'},
          'x': {'type': 'number'}, 'y': {'type': 'number'}, 'z': {'type': 'number'}},
         ['kind']),
    _obj('list_objects', 'List the objects of the active document, in model-tree order, with the '
         'type FreeCAD reports for each.', {}),
    _obj('select_object', 'Select an object in the model tree so that its properties are shown.',
         {'name': {'type': 'string'}}, ['name']),
    _obj('get_property', 'Read one property of an object as FreeCAD displays it (for example '
         '"Length" of a Box, "Placement" or "Label").',
         {'object': {'type': 'string'}, 'property': {'type': 'string'}},
         ['object', 'property']),
    _obj('set_property', 'Set one property of an object by editing the value cell of the property '
         'editor.  Accepts plain numbers, numbers with units ("30 mm", "1 in") or text for '
         'properties such as Label.',
         {'object': {'type': 'string'}, 'property': {'type': 'string'},
          'value': {'type': 'string', 'description': 'text to type into the value cell'}},
         ['object', 'property', 'value']),
    _obj('set_placement', 'Move an object by setting its Placement position (mm).',
         {'object': {'type': 'string'}, 'x': {'type': 'number'}, 'y': {'type': 'number'},
          'z': {'type': 'number'}},
         ['object', 'x', 'y', 'z']),
    _obj('delete_object', 'Delete an object from the document.', {'name': {'type': 'string'}},
         ['name']),
    _obj('boolean', 'Boolean operation between two objects (Part workbench): op is '
         '"cut", "fuse" or "common". The result is a new object.',
         {'op': {'type': 'string'}, 'object_a': {'type': 'string'}, 'object_b': {'type': 'string'}},
         ['op', 'object_a', 'object_b']),
    _obj('check_geometry',
         'Run Part > Check Geometry on an object and return the measurements FreeCAD reports '
         '(volume, surface area, edge and face counts, and whether the shape is valid).',
         {'object': {'type': 'string'}}, ['object']),
                    _obj('set_view', 'Set the 3D view direction: Front, Top, Right, Rear, Bottom, Left, Isometric, '
         'Axonometric, Dimetric or Trimetric.', {'name': {'type': 'string'}}, ['name']),
    _obj('fit_all', 'Zoom the view so everything fits (View > Fit all, or the V then F shortcut).',
         {}),
    _obj('read_status_bar', 'Return the text FreeCAD shows in its status bar (hints and results '
         'of the last operation).', {}),
    _obj('read_task_panel', 'Return the text of the Tasks panel (the panel that appears for dialogs '
         'such as Create primitives, Check geometry or Edit parameters).', {}),
    _obj('close_dialog', 'Press Escape to dismiss whatever dialog or task panel is open.', {}),
]


# ------------------------------------------------------------------ state --
class Session(object):
    def __init__(self):
        self.objects = []      # [{'name':..., 'type':..., 'row': int}]
        self.doc = None

    def add(self, name, type_):
        self.objects.append({'name': name, 'type': type_, 'row': len(self.objects)})
        return self.objects[-1]

    def row_of(self, name):
        for o in self.objects:
            if o['name'] == name:
                return o['row']
        return None

    def type_of(self, name):
        for o in self.objects:
            if o['name'] == name:
                return o['type']
        return None

    def reset(self):
        self.objects = []


SESSION = Session()


def _num(v):
    if v is None:
        return None
    return v


# ------------------------------------------------------------------ tools --
def tool_app_info(args):
    dx, dy = fc.offset()[0]
    box = fc.offset()[1]
    info = {'window': list(box), 'offset_from_reference': [dx, dy],
            'workbench': fc.workbench_name(),
            'document_open': fc.has_document(),
            'objects': SESSION.objects,
            'status_bar': fc.status_text()}
    return info


def tool_new_document(args):
    fc.new_document()
    SESSION.reset()
    ok = fc.has_document()
    if not ok:
        raise fc.FcError('File > New did not open a document (is a modal dialog in the way? '
                         'try close_dialog)')
    return {'created': 'new document', 'objects': [], 'status_bar': fc.status_text()}


def tool_open_document(args):
    fc.open_document(args['path'])
    SESSION.reset()
    return {'opened': args['path'], 'document_open': fc.has_document(),
            'status_bar': fc.status_text(),
            'note': 'the object list is not known until objects are created or read'}


def tool_save_document(args):
    fc.save_document(args['path'])
    return {'saved_as': args['path'], 'status_bar': fc.status_text()}


def tool_close_document(args):
    fc.close_document(bool(args.get('save')))
    SESSION.reset()
    return {'closed': True}


def tool_undo(args):
    fc.ckey('ctrl', 'z')
    fc._t(1.4)
    if SESSION.objects:
        SESSION.objects.pop()
    return {'status_bar': fc.status_text(), 'objects': SESSION.objects}


def tool_redo(args):
    fc.ckey('ctrl', 'y')
    fc._t(1.4)
    return {'status_bar': fc.status_text()}


def tool_set_workbench(args):
    return {'workbench': fc.set_workbench(args['name'])}


def _fmt(v):
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


PANEL_COMBO_Y = 322      # y of the type combo of the Create-primitives panel


def _fill_primitive_panel(kind, args, cy=PANEL_COMBO_Y):
    """Fill the Create-primitives task panel and press its Create button."""
    fields = fc.PRIMITIVE_FIELDS[kind]
    values = [args.get(f.lower().replace(' ', '')) for f in fields]
    fc.cclick(fc.TASK_TYPE_COMBO[0], cy)
    fc._t(1.0)
    for _ in range(25):
        fc.ckey('Up')
        fc._t(0.05)
    fc._t(0.3)
    for _ in range(fc.PRIMITIVE_ORDER.index(kind)):
        fc.ckey('Down')
        fc._t(0.13)
    fc.ckey('Return')
    fc._t(1.2)
    fy = fc.find_task_button('Length:', x0=0, x1=60, y0=cy + 30, y1=cy + 80)
    fc.cclick(222, fy if fy is not None else cy + 53)  # first parameter field
    fc._t(0.4)
    for i, v in enumerate(values):
        if v is not None:
            fc.ckey('ctrl', 'a')
            fc._t(0.15)
            fc.ctext(_fmt(v))
            fc._t(0.25)
        if i != len(values) - 1:
            fc.ckey('Tab')
            fc._t(0.25)
    by = fc.find_task_button('Create')
    fc.cclick(85, by if by is not None else cy - 80)   # Create
    fc._t(2.5)
    by = fc.find_task_button('Close')
    fc.cclick(85, by if by is not None else cy - 80)   # Close
    fc._t(1.2)


def _open_primitive_dialog(idx):
    fc.ckey('Escape')
    fc._t(0.3)
    fc.focus()
    fc.ckey('alt', 'p')
    fc._t(1.0)
    for _ in range(idx):
        fc.ckey('Down')
        fc._t(0.5)
    fc.ckey('Return')
    fc._t(2.5)


def tool_create_primitive(args):
    """Make a Part primitive, then write its measurements through the property editor."""
    kind = args['kind']
    if kind not in fc.PRIMITIVE_FIELDS:
        raise fc.FcError('unsupported primitive %r; supported: %s'
                         % (kind, ', '.join(sorted(fc.PRIMITIVE_FIELDS))))
    fc.set_workbench('Part')
    fc.focus()
    fc.ckey('Escape')
    fc._t(1.0)
    full = 'Part::' + kind
    want = len(fc.PROPERTIES[full])
    made = False
    for _try in range(2):                           # Part > Primitives > <kind>
        if not fc.menu_until('Part', 113):
            break
        fc.ckey('Return')
        fc._t(1.6)
        fc.cclick(600, 115 + 24 * fc.PRIMITIVE_SUB[kind])
        fc._t(2.8)
        fc.select_last_object()
        if len(fc.property_rows()) == want:
            made = True
            break
        fc.ckey('Escape')
        fc._t(0.5)
    if not made:
        for _dx in fc.create_from_toolbar(kind):
            fc.select_last_object()
            if len(fc.property_rows()) == want:
                made = True
                break
            fc.ckey('ctrl', 'z')
            fc._t(1.2)
    if not made:
        raise fc.FcError('the %s could not be created (tried Part > Primitives and the Part '
                         'toolbar); the document may be in an unexpected state' % kind)
    rec = SESSION.add(kind, full)
    for field in fc.PRIMITIVE_FIELDS[kind]:
        key = field.lower().replace(' ', '')
        v = args.get(key)
        if v is not None:
            fc.set_property_value_at(full, field, _fmt(v))
    if any(args.get(k) is not None for k in ('x', 'y', 'z')):
        expr = '(%s %s %s; 0 0 0)' % (_fmt(args.get('x') or 0), _fmt(args.get('y') or 0),
                                      _fmt(args.get('z') or 0))
        fc.set_property_value_at(full, 'Placement', expr)
    fc.select_last_object()
    shown = {}
    for field in fc.PRIMITIVE_FIELDS[kind]:
        shown[field] = fc.read_property_value_at(full, field)
    return {'created': rec, 'parameters_as_shown': shown, 'status_bar': fc.status_text()}


def tool_list_objects(args):
    return {'objects': SESSION.objects, 'document_open': fc.has_document(),
            'note': 'objects created through this session'}


def tool_select_object(args):
    row = SESSION.row_of(args['name'])
    if row is None:
        raise fc.FcError('unknown object %r; this session knows: %s'
                         % (args['name'], ', '.join(o['name'] for o in SESSION.objects) or 'none'))
    fc.select_object(row)
    return {'selected': args['name'], 'status_bar': fc.status_text()}


def _kind_of(name):
    t = SESSION.type_of(name)
    if t is None:
        raise fc.FcError('unknown object %r; this session knows: %s'
                         % (name, ', '.join(o['name'] for o in SESSION.objects) or 'none'))
    return t


def _select_and_check(name):
    kind = _kind_of(name)
    fc.select_object(SESSION.row_of(name), len(SESSION.objects))
    nrows = len(fc.property_rows())
    want = len(fc.PROPERTIES.get(kind, []))
    if want and nrows != want:
        raise fc.FcError('could not select %r in the model tree (%d property rows shown, expected %d)'
                         % (name, nrows, want))
    return kind


def tool_get_property(args):
    kind = _select_and_check(args['object'])
    val = fc.read_property_value_at(kind, args['property'])
    if val is None:
        raise fc.FcError('%s has no property %r; known properties: %s'
                         % (kind, args['property'], ', '.join(fc.PROPERTIES.get(kind, []))))
    return {'object': args['object'], 'property': args['property'], 'value': val}


def tool_set_property(args):
    kind = _select_and_check(args['object'])
    val = fc.set_property_value_at(kind, args['property'], args['value'])
    return {'object': args['object'], 'property': args['property'], 'value_after': val}


def tool_set_placement(args):
    name = args['object']
    kind = _select_and_check(name)
    expr = '(%s %s %s; 0 0 0)' % (_fmt(args['x']), _fmt(args['y']), _fmt(args['z']))
    fc.set_property_value_at(kind, 'Placement', expr)
    return {'object': name, 'placement_set_to': expr,
            'value_after': fc.read_property_value_at(kind, 'Placement')}


def tool_delete_object(args):
    name = args['name']
    row = SESSION.row_of(name)
    if row is None:
        raise fc.FcError('unknown object %r' % name)
    fc.select_object(row, len(SESSION.objects))
    fc.ckey('Delete')
    fc._t(1.4)
    SESSION.objects = [o for o in SESSION.objects if o['name'] != name]
    for i, o in enumerate(SESSION.objects):
        o['row'] = i
    return {'deleted': name, 'objects': SESSION.objects}


def tool_boolean(args):
    """Boolean of two objects: select them in the tree and run the Part command."""
    op = args['op'].lower()
    sub = {'cut': 1, 'fuse': 2, 'common': 3}.get(op)
    if sub is None:
        raise fc.FcError('op must be cut, fuse or common')
    a, b = args['object_a'], args['object_b']
    ra, rb = SESSION.row_of(a), SESSION.row_of(b)
    if ra is None or rb is None:
        raise fc.FcError('unknown object; this session knows: %s'
                         % ', '.join(o['name'] for o in SESSION.objects))
    fc.set_workbench('Part')
    n = len(SESSION.objects)
    name = {'cut': 'Cut', 'fuse': 'Fusion', 'common': 'Common'}[op]
    for parent in (12, 13, 11, 14):
        fc.ckey('Escape')
        fc._t(0.3)
        # select the two objects: the last one, then extend the selection upwards
        fc.select_object(max(ra, rb), n)
        gap = abs(ra - rb)
        for _ in range(gap):
            fc.ckey('shift', 'Up')
            fc._t(0.15)
        fc._t(0.5)
        fc.focus()
        fc.ckey('alt', 'p')
        fc._t(0.9)
        for _ in range(parent):
            fc.ckey('Down')
            fc._t(0.12)
        fc.ckey('Return')
        fc._t(0.9)
        for _ in range(sub):
            fc.ckey('Down')
            fc._t(0.12)
        fc.ckey('Return')
        fc._t(2.5)
        fc.select_last_object()
        rows = len(fc.property_rows())
        if rows >= 6:
            rec = SESSION.add(name, 'Part::' + name)
            return {'created': rec, 'status_bar': fc.status_text(),
                    'property_rows_of_result': rows}
    raise fc.FcError('the boolean operation did not produce a result object')


def tool_check_geometry(args):
    name = args['object']
    _select_and_check(name)
    if fc.menu_command('Part', [11, 10, 12, 9, 13], fc.task_panel_open) is None:
        raise fc.FcError('Part > Check geometry could not be opened')
    fc._t(1.5)
    fc.cclick(64, 725)                      # Run check
    fc._t(3.5)
    img = fc.grab()
    txt = vision.read_text(img, (0, 240, 262, 720), size=12)
    fc.cclick(*fc.TASK_CLOSE)
    fc._t(1.2)
    return {'object': name, 'task_panel_text': txt, 'status_bar': fc.status_text(),
            'note': 'text is read from the Check geometry task panel; empty means the panel '
                    'reported nothing visible'}


def tool_spreadsheet_create(args):
    raise fc.FcError('not implemented yet')


def tool_spreadsheet_set_cell(args):
    raise fc.FcError('not implemented yet')


def tool_spreadsheet_get_cell(args):
    raise fc.FcError('not implemented yet')


def tool_evaluate(args):
    raise fc.FcError('not implemented yet')


def tool_set_view(args):
    names = {'front': '1', 'top': '2', 'right': '3', 'rear': '4', 'bottom': '5', 'left': '6',
             'isometric': '0', 'axonometric': '0', 'dimetric': '0', 'trimetric': '0'}
    n = names.get(args['name'].lower())
    if n is None:
        raise fc.FcError('unknown view %r; use Front, Top, Right, Rear, Bottom, Left, '
                         'Isometric, Axonometric, Dimetric or Trimetric' % args['name'])
    fc.ckey(n)
    fc._t(1.2)
    return {'view': args['name']}


def tool_fit_all(args):
    fc.ckey('V')
    fc._t(0.3)
    fc.ckey('F')
    fc._t(1.2)
    return {'fitted': True}


def tool_read_status_bar(args):
    return {'status_bar': fc.status_text()}


def tool_read_task_panel(args):
    img = fc.grab()
    return {'task_panel_text': vision.read_text(img, (0, 180, 262, 780), size=12)}


def tool_close_dialog(args):
    fc.ckey('Escape')
    fc._t(0.9)
    return {'status_bar': fc.status_text()}


HANDLERS = {
    'app_info': tool_app_info, 'new_document': tool_new_document,
    'open_document': tool_open_document, 'save_document': tool_save_document,
    'close_document': tool_close_document, 'undo': tool_undo, 'redo': tool_redo,
    'set_workbench': tool_set_workbench, 'create_primitive': tool_create_primitive,
    'list_objects': tool_list_objects, 'select_object': tool_select_object,
    'get_property': tool_get_property, 'set_property': tool_set_property,
    'set_placement': tool_set_placement, 'delete_object': tool_delete_object,
    'boolean': tool_boolean, 'check_geometry': tool_check_geometry,
    'spreadsheet_create': tool_spreadsheet_create,
    'spreadsheet_set_cell': tool_spreadsheet_set_cell,
    'spreadsheet_get_cell': tool_spreadsheet_get_cell, 'evaluate': tool_evaluate,
    'set_view': tool_set_view, 'fit_all': tool_fit_all,
    'read_status_bar': tool_read_status_bar, 'read_task_panel': tool_read_task_panel,
    'close_dialog': tool_close_dialog,
}


# ------------------------------------------------------------------- json --
def reply(mid, result=None, error=None):
    msg = {'jsonrpc': '2.0', 'id': mid}
    if error is not None:
        msg['error'] = error
    else:
        msg['result'] = result
    sys.stdout.write(json.dumps(msg) + '\n')
    sys.stdout.flush()


def handle(req):
    method = req.get('method')
    mid = req.get('id')
    params = req.get('params') or {}
    if method == 'initialize':
        return reply(mid, {'protocolVersion': params.get('protocolVersion', '2024-11-05'),
                           'capabilities': {'tools': {}},
                           'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
                           'instructions': INSTRUCTIONS})
    if method in ('notifications/initialized', 'initialized'):
        return
    if method == 'ping':
        return reply(mid, {})
    if method == 'tools/list':
        return reply(mid, {'tools': TOOLS})
    if method == 'tools/call':
        name = params.get('name')
        args = params.get('arguments') or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return reply(mid, {'content': [{'type': 'text', 'text': 'unknown tool %r' % name}],
                               'isError': True})
        try:
            out = fn(args)
            text = json.dumps(out, indent=1, ensure_ascii=False)
            return reply(mid, {'content': [{'type': 'text', 'text': text}]})
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            return reply(mid, {'content': [{'type': 'text', 'text': '%s: %s' % (type(exc).__name__, exc)}],
                               'isError': True})
    if mid is not None:
        return reply(mid, None, {'code': -32601, 'message': 'method not found: %s' % method})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            handle(req)
        except Exception:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())


if __name__ == '__main__':
    main()

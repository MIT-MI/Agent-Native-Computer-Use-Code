#!/usr/bin/env python3
"""MCP server for FreeCAD 0.20.

Speaks MCP JSON-RPC on stdin/stdout (one JSON object per line) and drives the
FreeCAD window through cua-input.  Diagnostics go to stderr.
"""
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uia                      # noqa: E402
import fc as fcmod              # noqa: E402

SERVER_NAME = 'freecad-ui'
SERVER_VERSION = '1.0.0'
PROTOCOL = '2024-11-05'

INSTRUCTIONS = """\
Drives a running FreeCAD 0.20 window the way a person would: it opens the
application's own menus, fills its dialogs and edits the property editor with
the mouse and keyboard.  It has no access to FreeCAD's Python API, so every
operation is one a user could perform by hand.  Calls are synchronous; when a
call returns, the UI has settled.

Units and values
  * Lengths are millimetres, angles degrees.  Property values are the text a
    user would type: "30", "30 mm", "1.5 in", "45 deg", "true", or an
    expression such as "10 mm + 5 mm".  Values are read back as FreeCAD
    displays them, e.g. "30.00 mm".
  * Objects are named by the *label* shown in the Model tree ("Box", "Cut001",
    "MyPart").  Labels are what set_property/get_property/delete_object expect.

Reading results
  * get_property, describe_object, list_objects and read_window_text read the
    pixels of the window with a small built-in OCR of DejaVu Sans (the UI
    font).  Digits and ordinary words are usually correct, but similar shapes
    (c/e, o/p, i/l/1, 0/O) are occasionally confused, so treat readings as
    observations.  describe_object returns both the raw OCR text and a name
    snapped onto FreeCAD's known property names.
  * There is no other feedback channel: the server cannot read FreeCAD's files,
    settings or logs (FreeCAD runs as a different user), and it cannot execute
    FreeCAD code.  A document saved with save_document/export_file lives inside
    FreeCAD's own filesystem and is only visible to FreeCAD itself; the path is
    echoed back so a later open_document can use it.

Limits and recovery
  * The window must stay visible and unobstructed; the server finds the menu bar
    itself, so moving or un-maximising the window is fine.
  * A modal dialog blocks everything else.  If a call reports that a menu or
    panel cannot be found, press_key ["Escape"] (or click the dialog button) and
    try again.
  * Calls take roughly 0.5-8 s each; creating an object costs a few seconds
    because each property is typed and the panel is read back.
  * Operations are not atomic: if one step fails the tool reports the error and
    the document keeps whatever was already applied.
"""

TOOLS = [
    {
        'name': 'new_document',
        'description': ('Create a new empty document (File > New, Ctrl+N) and make it the '
                        'active one. Returns a short confirmation; the new document appears '
                        'in the Model tree and in the tab bar.'),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'open_document',
        'description': ('Open a document (File > Open, Ctrl+O). The path is resolved inside '
                        'FreeCAD\'s own filesystem. Returns the path that was typed.'),
        'inputSchema': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': 'Absolute path of a .FCStd document.'}},
            'required': ['path']},
    },
    {
        'name': 'save_document',
        'description': ('Save the current document. Without a path it saves in place (Ctrl+S). '
                        'With a path it behaves like File > Save As... and stores the document '
                        'there inside FreeCAD\'s filesystem. Returns the path used.'),
        'inputSchema': {'type': 'object', 'properties': {
            'path': {'type': 'string', 'description': 'Optional absolute path (.FCStd).'}},
            'additionalProperties': False},
    },
    {
        'name': 'close_document',
        'description': ('Close the current document (Ctrl+W). If FreeCAD asks whether to save, '
                        'the dialog is answered with the given choice. Returns the choice used.'),
        'inputSchema': {'type': 'object', 'properties': {
            'save': {'type': 'string', 'enum': ['discard', 'save', 'cancel'],
                     'description': 'What to do with unsaved changes (default discard).'}},
            'additionalProperties': False},
    },
    {
        'name': 'export_file',
        'description': ('Export the document to a file (Ctrl+E). The format is chosen from the '
                        'extension: step/stp, iges/igs, stl, obj, brep, ply, off, amf, 3mf, '
                        'dae, dxf, dwg, iv, vrml, wrl. The file is written inside FreeCAD\'s '
                        'filesystem. Returns the filter FreeCAD used and the path.'),
        'inputSchema': {'type': 'object', 'properties': {
            'path': {'type': 'string'},
            'format': {'type': 'string', 'description': 'Optional format keyword override.'}},
            'required': ['path']},
    },
    {
        'name': 'create_primitive',
        'description': ('Add a Part primitive to the active document and set its dimensions. '
                        'Returns the object label and the properties that were set.\n'
                        'kind: box | cylinder | sphere | cone | torus | tube | prism | wedge.\n'
                        'parameters is a map of dimension name -> value (mm / degrees):\n'
                        '  box: length, width, height\n'
                        '  cylinder: radius, height, angle\n'
                        '  sphere: radius, angle1, angle2, angle3\n'
                        '  cone: radius1, radius2, height, angle\n'
                        '  torus: radius1, radius2, angle1, angle2, angle3\n'
                        '  tube: outer_radius, inner_radius, height, angle\n'
                        '  prism: polygon, circumradius, height\n'
                        '  wedge: xmin, ymin, zmin, x2min, z2min, xmax, ymax, zmax, x2max, z2max\n'
                        'The Part workbench is activated first if needed.'),
        'inputSchema': {'type': 'object', 'properties': {
            'kind': {'type': 'string'},
            'label': {'type': 'string', 'description': 'Label for the new object (optional).'},
            'parameters': {'type': 'object', 'description': 'Dimension name -> value.'}},
            'required': ['kind']},
    },
    {
        'name': 'set_property',
        'description': ('Select an object in the Model tree and set one of its properties in the '
                        'property editor (Data tab). `value` is the text a user would type, e.g. '
                        '"30 mm", "45 deg", "true", "10 mm + 5 mm". Returns the value read back '
                        'from the panel (may differ in formatting, e.g. "30.00 mm").'),
        'inputSchema': {'type': 'object', 'properties': {
            'object': {'type': 'string'}, 'property': {'type': 'string'},
            'value': {'type': 'string'}},
            'required': ['object', 'property', 'value']},
    },
    {
        'name': 'get_property',
        'description': ('Select an object and read one property from the property editor (Data '
                        'tab). Returns the displayed text including units, e.g. "30.00 mm". '
                        'Read with OCR.'),
        'inputSchema': {'type': 'object', 'properties': {
            'object': {'type': 'string'}, 'property': {'type': 'string'}},
            'required': ['object', 'property']},
    },
    {
        'name': 'describe_object',
        'description': ('Select an object and return every property the Data tab shows for it, '
                        'as a list of {name, raw_name, value}. `name` is the OCR text snapped '
                        'onto FreeCAD\'s known property names, `raw_name` is what was read. This '
                        'is the main way to inspect the state of the model.'),
        'inputSchema': {'type': 'object', 'properties': {
            'object': {'type': 'string'}}, 'required': ['object']},
    },
    {
        'name': 'list_objects',
        'description': ('List the entries of the Model tree, top to bottom: open documents and '
                        'the objects inside them (as read with OCR, so leading icon glyphs may '
                        'appear as stray characters).'),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'delete_object',
        'description': 'Select an object in the Model tree and delete it (Delete key). Returns its label.',
        'inputSchema': {'type': 'object', 'properties': {
            'object': {'type': 'string'}}, 'required': ['object']},
    },
    {
        'name': 'boolean_operation',
        'description': ('Part boolean between two objects. operation: cut (base minus tool) | '
                        'fuse (union) | common (intersection) | section | xor. The two objects '
                        'are selected in the tree (base, then Shift+arrow to the tool) and the '
                        'matching Part > Boolean entry is run. Returns operation, base, tool and '
                        'the label if one was given.'),
        'inputSchema': {'type': 'object', 'properties': {
            'operation': {'type': 'string'}, 'base': {'type': 'string'},
            'tool': {'type': 'string'}, 'label': {'type': 'string'}},
            'required': ['operation', 'base', 'tool']},
    },
    {
        'name': 'switch_workbench',
        'description': ('Activate a workbench through the selector box in the toolbar. Names as '
                        'shown there: Part, Part Design, Sketcher, Draft, Spreadsheet, Mesh '
                        'Design, TechDraw, Arch, FEM, OpenSCAD, Inspection, Points, Surface, '
                        'Start, Image, Robot, Path, Reverse Engineering, Raytracing, Web.'),
        'inputSchema': {'type': 'object', 'properties': {
            'name': {'type': 'string'}}, 'required': ['name']},
    },
    {
        'name': 'run_menu_command',
        'description': ('Run any command from FreeCAD\'s menu bar by naming its path, e.g. '
                        '["Part","Fillet..."], ["Edit","Preferences..."], ["Tools","Edit '
                        'parameters..."], ["View","Panels","Report view"]. Each entry is matched '
                        'against the text of the popup that opens, so the path must use the '
                        'words FreeCAD shows (menu_items lists them). Returns the path that ran.'),
        'inputSchema': {'type': 'object', 'properties': {
            'path': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['path']},
    },
    {
        'name': 'menu_items',
        'description': ('List the entries of a top-level menu (File, Edit, View, Tools, Macro, '
                        'Part, Measure, Windows, Help) by opening it and reading the popup. Use '
                        'it to discover the exact wording for run_menu_command.'),
        'inputSchema': {'type': 'object', 'properties': {
            'menu': {'type': 'string'}}, 'required': ['menu']},
    },
    {
        'name': 'press_key',
        'description': ('Send a key or key combination to the window, e.g. ["ctrl","z"] undo, '
                        '["ctrl","y"] redo, ["Escape"] to dismiss a dialog, ["space"] toggle '
                        'visibility, ["ctrl","shift","s"] save as, ["F10"] focus the menu bar. '
                        'Use for shortcuts that have no dedicated tool.'),
        'inputSchema': {'type': 'object', 'properties': {
            'keys': {'type': 'array', 'items': {'type': 'string'}}}, 'required': ['keys']},
    },
    {
        'name': 'read_window_text',
        'description': ('Read the text currently shown in the window: the Model tree entries, '
                        'every row of the property panel (name and displayed value) and the '
                        'status bar. Useful to confirm what an operation did.'),
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'status_bar',
        'description': 'Read the status bar at the bottom of the window (FreeCAD\'s last message).',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'dismiss_dialog',
        'description': ('If a modal dialog is open, click its button whose text matches '
                        '`button` (e.g. "OK", "Yes", "Discard", "Close", "Cancel"). Returns the '
                        'label that was clicked, or reports that no dialog was found.'),
        'inputSchema': {'type': 'object', 'properties': {
            'button': {'type': 'string'}}, 'required': ['button']},
    },
]

KIND_ITEM = {
    'box': 'Cube', 'cube': 'Cube', 'cylinder': 'Cylinder', 'sphere': 'Sphere',
    'cone': 'Cone', 'torus': 'Torus', 'tube': 'Create tube', 'prism': 'Prism',
    'wedge': 'Wedge',
}

KIND_PARAMS = {
    'box': {'length': 'Length', 'width': 'Width', 'height': 'Height'},
    'cylinder': {'radius': 'Radius', 'height': 'Height', 'angle': 'Angle'},
    'sphere': {'radius': 'Radius', 'angle1': 'Angle1', 'angle2': 'Angle2', 'angle3': 'Angle3'},
    'cone': {'radius1': 'Radius1', 'radius2': 'Radius2', 'height': 'Height', 'angle': 'Angle'},
    'torus': {'radius1': 'Radius1', 'radius2': 'Radius2', 'angle1': 'Angle1',
              'angle2': 'Angle2', 'angle3': 'Angle3'},
    'tube': {'outer_radius': 'OuterRadius', 'inner_radius': 'InnerRadius', 'height': 'Height',
             'angle': 'Angle'},
    'prism': {'polygon': 'Polygon', 'circumradius': 'Circumradius', 'height': 'Height'},
    'wedge': {'xmin': 'Xmin', 'ymin': 'Ymin', 'zmin': 'Zmin', 'x2min': 'X2min', 'z2min': 'Z2min',
              'xmax': 'Xmax', 'ymax': 'Ymax', 'zmax': 'Zmax', 'x2max': 'X2max', 'z2max': 'Z2max'},
}

BOOL_ITEM = {'cut': 'Cut', 'fuse': 'Fuse', 'union': 'Fuse', 'common': 'Common',
             'section': 'Section', 'xor': 'XOR'}

EXPORT_FILTER = {
    'step': 'STEP', 'stp': 'STEP', 'iges': 'IGES', 'igs': 'IGES', 'stl': 'STL',
    'obj': 'OBJ', 'brep': 'BREP', 'ply': 'PLY', 'off': 'OFF', 'amf': 'AMF',
    '3mf': '3MF', 'dae': 'Collada', 'dxf': 'DXF', 'dwg': 'DWG', 'iv': 'Inventor',
    'vrml': 'VRML', 'wrl': 'VRML',
}


class App:
    def __init__(self):
        self.fc = fcmod.FreeCAD()

    # ------------------------------------------------------------- helpers
    def _fill_file_dialog(self, path):
        uia.type_text(path)
        time.sleep(0.5)
        uia.key('Return')
        time.sleep(1.2)

    # ------------------------------------------------------------ handlers
    def new_document(self):
        uia.key('ctrl', 'n')
        time.sleep(1.0)
        return 'New document created; it is now the active document.'

    def open_document(self, path):
        uia.key('ctrl', 'o')
        time.sleep(1.2)
        self._fill_file_dialog(path)
        return 'opened %s' % path

    def save_document(self, path=None):
        if path:
            uia.key('ctrl', 'shift', 's')
            time.sleep(1.2)
            self._fill_file_dialog(path)
            return 'saved as %s' % path
        uia.key('ctrl', 's')
        time.sleep(1.2)
        return 'document saved'

    def close_document(self, save='discard'):
        uia.key('ctrl', 'w')
        time.sleep(1.0)
        if save != 'discard':
            self.fc.click_dialog_button({'save': 'Save', 'cancel': 'Cancel'}.get(save, 'Save'))
        else:
            self.fc.click_dialog_button('Discard')
        return 'closed (%s)' % save

    def export_file(self, path, format=None):
        import os as _os
        ext = _os.path.splitext(path)[1].lstrip('.').lower()
        keyword = format or EXPORT_FILTER.get(ext, ext.upper())
        uia.key('ctrl', 'e')
        time.sleep(1.4)
        self.fc.set_file_dialog_filter(keyword)
        self._fill_file_dialog(path)
        return {'path': path, 'filter': keyword, 'format': ext}

    def create_primitive(self, kind, label=None, parameters=None):
        kind = str(kind).lower().strip()
        if kind not in KIND_ITEM:
            raise fcmod.UiError('unknown primitive %r; known: %s'
                                % (kind, ', '.join(sorted(KIND_ITEM))))
        if self.fc.workbench() != 'Part':
            self.fc.switch_workbench('Part')
            time.sleep(0.5)
        item = KIND_ITEM[kind]
        before = [t['label'] for t in self.fc.tree_items()]
        made = False
        new_label = None
        if item in fcmod.PRIM_BUTTONS:
            self.fc.click_toolbar(item)
            after = [t['label'] for t in self.fc.tree_items()]
            made = len(after) > len(before)
            new_label = next((l for l in reversed(after) if l not in before), None)
        if not made:
            self.fc.menu_command('Part', 'Primitives', item)
            time.sleep(1.2)
        if new_label:
            self.fc.select_object(new_label)
        self.fc.show_data_tab()
        time.sleep(0.4)
        mapping = KIND_PARAMS.get(kind, {})
        applied = {}
        for key, value in (parameters or {}).items():
            prop = mapping.get(str(key).lower(), key)
            self.fc.set_property(prop, value)
            applied[prop] = value
        if label:
            self.fc.set_property('Label', label)
        return {'label': label or item, 'properties_set': applied}

    def set_property(self, object, property, value):
        self.fc.show_data_tab()
        self.fc.select_object(object)
        self.fc.set_property(property, value)
        return self.fc.read_property(property)

    def get_property(self, object, property):
        self.fc.show_data_tab()
        self.fc.select_object(object)
        return self.fc.read_property(property)

    def describe_object(self, object):
        self.fc.show_data_tab()
        sel = self.fc.select_object(object)
        return {'selected': sel, 'properties': self.fc.property_rows()}

    def list_objects(self):
        return [t['label'] for t in self.fc.tree_items()]

    def delete_object(self, object):
        self.fc.select_object(object)
        uia.key('Delete')
        time.sleep(0.6)
        return object

    def boolean_operation(self, operation, base, tool, label=None):
        op = str(operation).lower().strip()
        if op not in BOOL_ITEM:
            raise fcmod.UiError('unknown boolean %r; known: %s'
                                % (operation, ', '.join(sorted(BOOL_ITEM))))
        if self.fc.workbench() != 'Part':
            self.fc.switch_workbench('Part')
            time.sleep(0.5)
        items = [t['label'] for t in self.fc.tree_items()]
        self.fc.select_object(base)
        bi = next((i for i, l in enumerate(items) if base.lower() in l.lower()), None)
        ti = next((i for i, l in enumerate(items) if tool.lower() in l.lower()), None)
        if bi is not None and ti is not None and bi != ti:
            step = 'Down' if ti > bi else 'Up'
            for _ in range(abs(ti - bi)):
                uia.key('shift', step)
        self.fc.menu_command('Part', 'Boolean', BOOL_ITEM[op])
        time.sleep(1.2)
        if label:
            self.fc.show_data_tab()
            try:
                self.fc.set_property('Label', label)
            except fcmod.UiError:
                pass
        return {'operation': op, 'base': base, 'tool': tool, 'label': label}

    def switch_workbench(self, name):
        got = self.fc.switch_workbench(name)
        time.sleep(0.4)
        return got

    def run_menu_command(self, path):
        self.fc.menu_command(*path)
        return 'ran %s' % ' > '.join(path)

    def menu_items(self, menu):
        return self.fc.menu_items(menu)

    def press_key(self, keys):
        uia.key(*keys)
        return 'sent %s' % '+'.join(keys)

    def read_window_text(self):
        return self.fc.read_window_text()

    def status_bar(self):
        return self.fc.status_text()

    def dismiss_dialog(self, button):
        return self.fc.click_dialog_button(button)


def dispatch(app, name, args):
    handler = getattr(app, name, None)
    if handler is None:
        raise fcmod.UiError('unknown tool %r' % name)
    return handler(**args)


def main():
    app = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get('method')
        mid = msg.get('id')
        if method == 'initialize':
            result = {
                'protocolVersion': PROTOCOL,
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
                'instructions': INSTRUCTIONS,
            }
        elif method in ('notifications/initialized', 'initialized'):
            continue
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            params = msg.get('params') or {}
            name = params.get('name')
            args = params.get('arguments') or {}
            try:
                if app is None:
                    app = App()
                out = dispatch(app, name, args)
                text = out if isinstance(out, str) else json.dumps(out, indent=1, ensure_ascii=False)
                result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
            except Exception as exc:
                detail = ''.join(traceback.format_exception_only(type(exc), exc)).strip()
                result = {'content': [{'type': 'text', 'text': 'ERROR: %s' % detail}], 'isError': True}
        elif method == 'ping':
            result = {}
        else:
            if mid is None:
                continue
            sys.stdout.write(json.dumps({
                'jsonrpc': '2.0', 'id': mid,
                'error': {'code': -32601, 'message': 'method not found: %s' % method}}) + '\n')
            sys.stdout.flush()
            continue
        if mid is not None:
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': result}) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass

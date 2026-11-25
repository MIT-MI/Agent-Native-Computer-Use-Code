#!/usr/bin/env python3
"""Driver for the FreeCAD 0.20 graphical application.

Everything here works the way a person works the program: menu titles are
clicked, the arrow keys walk the menu, dialogs are filled in and Enter commits.
Nothing is read from or written to the application's own files.

The window is kept maximised, so screen coordinates are stable: the menu bar is
at y=80, the model tree occupies the left dock from y=215 down, and the property
editor below it.  Positions are derived from the window rectangle that is
measured on every call, so a differently placed window still works.
"""
import re
import time

import vision
from vision import (click, double_click, drag, grab, key, right_click, scroll,
                    type_text, window_box)

REF_ORIGIN = (0, 28)          # window rectangle when maximised on this display
SCREEN = (1280, 800)

MENU_Y = 80
MENU_X = {                    # x of each menu title, per workbench menu bar
    'File': 20, 'Edit': 56, 'View': 97, 'Tools': 140, 'Macro': 188,
    'Windows': 350, 'Help': 405,
}
WB_MENU = {                   # extra menus contributed by a workbench
    'Part': ['Part', 'Measure'],
    'PartDesign': ['Part Design', 'Measure'],
    'Sketcher': ['Sketch', 'Measure'],
    'Spreadsheet': ['Spreadsheet'],
    'Draft': ['Draft', 'Measure'],
    'Mesh': ['Meshes'],
    'Start': [],
    'TechDraw': ['TechDraw', 'Measure'],
}

# ---------------------------------------------------------------- geometry --
PROP_ROW0 = 503               # y of the first row of the property editor
PROP_STEP = 20
PROP_NAME_X = 55              # centre of the name column
PROP_VALUE_BOX = (103, 258)   # x range of the value column

TREE_ROW0 = 279               # y of the first child row of the model tree
TREE_STEP = 20
TREE_X = 73

STATUS_BOX = (0, 782, 700, 800)

# property rows of the Data tab, in display order, per object type
PROPERTIES = {
    'Part::Box': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                  'Box', 'Length', 'Width', 'Height'],
    'Part::Cylinder': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                       'Cylinder', 'Radius', 'Height', 'Angle'],
    'Part::Sphere': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                     'Sphere', 'Radius', 'Angle1', 'Angle2', 'Angle3'],
    'Part::Cone': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                   'Cone', 'Radius1', 'Radius2', 'Height', 'Angle'],
    'Part::Torus': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                    'Torus', 'Radius1', 'Radius2', 'Angle1', 'Angle2', 'Angle3'],
    'Part::Tube': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                   'Tube', 'Outer Radius', 'Inner Radius', 'Height'],
    'Part::Wedge': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                    'Wedge', 'Xmin', 'Ymin', 'Zmin', 'X2min', 'Z2min', 'Xmax', 'Ymax',
                    'Zmax', 'X2max', 'Z2max'],
    'Part::Cut': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                  'Cut', 'Base', 'Tool'],
    'Part::Fuse': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                   'Fuse', 'Shapes'],
    'Part::Common': ['Attachment', 'Support', 'Map Mode', 'Base', 'Placement', 'Label',
                     'Common', 'Shapes'],
}

# the "Create primitives" dialog: type name -> parameter fields, in tab order
PRIMITIVE_FIELDS = {
    'Plane': ['Length', 'Width'],
    'Box': ['Length', 'Width', 'Height'],
    'Cylinder': ['Radius', 'Height', 'Angle'],
    'Cone': ['Radius1', 'Radius2', 'Height', 'Angle'],
    'Sphere': ['Radius', 'Angle1', 'Angle2', 'Angle3'],
    'Ellipsoid': ['Radius1', 'Radius2', 'Radius3', 'Angle1', 'Angle2', 'Angle3'],
    'Torus': ['Radius1', 'Radius2', 'Angle1', 'Angle2', 'Angle3'],
    'Prism': ['Polygon', 'Circumradius', 'Height'],
    'Wedge': ['Xmin', 'Ymin', 'Zmin', 'X2min', 'Z2min', 'Xmax', 'Ymax', 'Zmax', 'X2max', 'Z2max'],
    'Tube': ['Outer Radius', 'Inner Radius', 'Height'],
}
PRIMITIVE_ORDER = ['Plane', 'Box', 'Cylinder', 'Cone', 'Sphere', 'Ellipsoid', 'Torus',
                   'Prism', 'Wedge', 'Helix', 'Spiral', 'Circle', 'Ellipse', 'Point',
                   'Line', 'Regular polygon']

# the workbench selector list, in display order
WORKBENCHES = ['Arch', 'Draft', 'Drawing', 'FEM', 'Image', 'Inspection', 'Mesh Design',
               '<none>', 'OpenSCAD', 'Part Design', 'Part', 'Path', 'Points', 'Raytracing',
               'Reverse Engineering', 'Robot', 'Sketcher', 'Spreadsheet', 'Start',
               'Surface', 'TechDraw', 'Test Framework', 'Web']

DLG_NAME = (640, 590)         # "File name" field of the open/save dialogs
DLG_OK = (800, 620)           # default button of the file dialogs
TASK_TYPE_COMBO = (140, 325)  # type combo of the geometric-primitive task panel
TASK_FIELD0 = (222, 378)      # first parameter field of that panel
TASK_CREATE = (152, 245)
TASK_CLOSE = (237, 245)
WORKBENCH_COMBO = (578, 110)


class FcError(RuntimeError):
    pass


def _t(sec=0.35):
    time.sleep(sec)


_offset_cache = None
MENUBAR_Y = 79                # y of the menu-bar text row when maximised
MENUBAR_X = 10                # x where the first menu title starts


def _menubar_band(a):
    """Row band of the menu bar: the first row band with a lot of dark text."""
    for y in range(45, 97):
        n = int((a[y, 8:430] < 140).sum())
        if n > 25:
            y0 = y
            while y < 97 and int((a[y, 8:430] < 140).sum()) > 25:
                y += 1
            return y0, y
    return None


def offset(refresh=False):
    """Offset of the window from the reference layout, from the menu bar.

    The menu bar is found by its dark text, so the position is recovered even
    when the window manager places the frame a few pixels away from the
    reference position.
    """
    global _offset_cache
    if _offset_cache is not None and not refresh:
        return _offset_cache
    try:
        img = grab()
        import numpy as np
        a = np.asarray(img.convert('L')).astype(int)
        band = _menubar_band(a)
        if band is None:
            raise ValueError('menu bar not found')
        cy = (band[0] + band[1]) // 2
        row = a[band[0]:band[1], :700] < 140
        xs = np.where(row.any(axis=0))[0]
        dx = int(xs.min()) - MENUBAR_X
        dy = cy - MENUBAR_Y
        _offset_cache = ((dx, dy), (dx, dy + 32, 1280 + dx, 800 + dy))
    except Exception:
        _offset_cache = ((0, 0), (0, 32, 1280, 800))
    return _offset_cache


def focus():
    """Give the main window the keyboard focus by clicking its title bar."""
    cclick(640, 45)
    _t(0.35)


def P(x, y):
    """Translate reference coordinates into screen coordinates."""
    dx, dy = offset()[0]
    return int(x + dx), int(y + dy)


def cclick(x, y):
    return click(*P(x, y))


def cdouble(x, y):
    return double_click(*P(x, y))


def ckey(*keys):
    return key(*keys)


def ctext(t):
    return type_text(t)


# ------------------------------------------------------------------ menus --
def _menu_runs(img):
    import numpy as np
    a = np.asarray(img.convert('L'))
    ink = (a[MENU_Y - 9:MENU_Y + 9, :640] < 128).any(axis=0)
    runs = []
    x = 0
    while x < len(ink):
        if ink[x]:
            x0 = x
            while x < len(ink) and ink[x]:
                x += 1
            runs.append([x0, x])
        else:
            x += 1
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < 8:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return merged


_wb_cache = None


def current_workbench():
    global _wb_cache
    if _wb_cache is None:
        _wb_cache = workbench_name()
    return _wb_cache


MENU_KEY = {'File': 'f', 'Edit': 'e', 'View': 'v', 'Tools': 't', 'Macro': 'm',
            'Windows': 'w', 'Help': 'h', 'Part': 'p', 'Part Design': 'p',
            'Spreadsheet': 's', 'Sketch': 's', 'Draft': 'd', 'Meshes': 'm',
            'TechDraw': 't'}


def menu_names(img=None):
    """[(name, x_centre)] for the menu bar currently on screen."""
    if img is None:
        img = grab()
    runs = _menu_runs(img)
    n = len(runs)
    mid = WB_MENU.get(current_workbench(), [])
    if n == 7 + len(mid):
        names = ['File', 'Edit', 'View', 'Tools', 'Macro'] + list(mid) + ['Windows', 'Help']
    else:
        names = (['File', 'Edit', 'View', 'Tools', 'Macro']
                 + ['menu%d' % i for i in range(5, n - 2)] + ['Windows', 'Help'])
    return [(names[i] if i < len(names) else 'menu%d' % i, (r[0] + r[1]) // 2)
            for i, r in enumerate(runs)]


def find_menu_entry(img, text, x0=265, x1=700, y0=30, y1=800, step=4):
    """Y centre of the menu entry whose text matches, or None."""
    best = (0.0, None)
    for y in range(y0, y1, step):
        v = vision.contains_text(img, (x0, y - 9, x1, y + 9), text, sizes=(12, 13))
        if v > best[0]:
            best = (v, y)
    return best[1] if best[0] >= 0.72 else None


def highlight_y(img=None, x=350):
    """Y of the highlighted (blue) row of an open menu popup, or None."""
    import numpy as np
    if img is None:
        img = grab()
    a = np.asarray(img.convert('RGB')).astype(int)
    col = a[:, x, :]
    blue = (col[:, 2] > 120) & (col[:, 2] - col[:, 0] > 40) & (col[:, 1] < 170)
    ys = np.where(blue)[0]
    if len(ys) == 0:
        return None
    return int((ys.min() + ys.max()) // 2)


def menu_until(top, target_y, max_steps=30, step=0.35):
    """Open a menu and press Down until the highlight sits at `target_y`."""
    focus()
    ckey('Escape')
    _t(0.3)
    ckey('alt', MENU_KEY[top])
    _t(1.0)
    for _ in range(max_steps):
        y = highlight_y()
        if y is not None and abs(y - target_y) <= 7:
            return True
        ckey('Down')
        _t(step)
    ckey('Escape')
    return False


def click_menu_item(top, text, subs=()):
    """Open a menu with its keyboard mnemonic and click the entry with `text`.

    The entry is located by reading the popup, so disabled entries and
    separators cannot shift the choice.  `subs` are sub-menu entries to walk
    into first.
    """
    focus()
    ckey('Escape')
    _t(0.3)
    ckey('alt', MENU_KEY[top])
    _t(1.0)
    for want in (text,) + tuple(subs):
        y = find_menu_entry(grab(), want)
        if y is None:
            ckey('Escape')
            return False
        cclick(400, y)
        _t(1.1)
    return True


def menu(top, index, sub=None, subindex=0):
    """Run a menu command.

    The menu is opened with its keyboard mnemonic (Alt+F for File, Alt+P for
    Part and so on) and the wanted entry is reached with the arrow keys, so no
    screen positions are involved.
    """
    focus()
    ckey('Escape')
    _t(0.25)
    k = MENU_KEY.get(top)
    if k is None:
        raise FcError('no keyboard mnemonic known for menu %r' % top)
    ckey('alt', k)
    _t(0.9)
    for _ in range(index):
        ckey('Down')
        _t(0.5)
    ckey('Return')
    _t(1.3)
    if sub is not None:
        for _ in range(subindex):
            ckey('Down')
            _t(0.12)
        ckey('Return')
        _t(1.3)


def status_text():
    img = grab()
    return vision.read_text(img, STATUS_BOX, size=12)


def workbench_name():
    """Name shown by the workbench selector ('' when it cannot be read)."""
    img = grab()
    cx, cy = P(*WORKBENCH_COMBO)
    box = (cx - 70, cy - 10, cx + 55, cy + 10)
    scored = []
    for wb in WORKBENCHES:
        scored.append((vision.contains_text(img, box, wb, sizes=(12, 13)), wb))
    scored.sort(reverse=True)
    top, second = scored[0], scored[1]
    if top[0] >= 0.85 and top[0] - second[0] >= 0.15:
        return top[1]
    return ''      # an unreadable selector is reported as unknown


def set_workbench(name):
    if name not in WORKBENCHES:
        raise FcError('unknown workbench %r; known: %s' % (name, ', '.join(WORKBENCHES)))
    global _wb_cache
    for _attempt in range(3):
        if workbench_name() == name:
            _wb_cache = name
            return name
        focus()
        cclick(*WORKBENCH_COMBO)
        _t(1.0)
        ckey('Home')
        _t(0.35)
        for _ in range(WORKBENCHES.index(name)):
            ckey('Down')
            _t(0.13)
        ckey('Return')
        _t(1.8)
    got = workbench_name()
    _wb_cache = got
    if got != name:
        raise FcError('workbench did not switch to %r (selector shows %r)' % (name, got))
    return got


def task_panel_open(img=None):
    """True when a task panel (Create primitives, Check geometry, ...) is up."""
    if img is None:
        img = grab()
    return vision.contains_text(img, (110, 236, 196, 256), 'Create', sizes=(11, 12, 13)) > 0.7


def find_type_combo(img=None, lo=300, hi=345):
    """Y of the type combo of the Create-primitives panel (None when absent).

    The combo is the only pure-white band in the dock, so it is found from the
    pixels rather than from the text; this survives the panel opening on a
    different primitive than last time and small shifts of the dock layout.
    """
    import numpy as np
    if img is None:
        img = grab()
    a = np.asarray(img.convert('L')).astype(int)
    run = []
    for y in range(lo, hi):
        if a[y, 30:250].mean() > 248:
            run.append(y)
        else:
            if len(run) >= 3:
                return (run[0] + run[-1]) // 2
            run = []
    if len(run) >= 3:
        return (run[0] + run[-1]) // 2
    return None


def find_task_button(text, x0=30, x1=140, y0=210, y1=300):
    """Y of a task-panel button (Create/Close) found by its text, or None."""
    img = grab()
    best = (0.0, None)
    for y in range(y0, y1, 3):
        v = vision.contains_text(img, (x0, y - 9, x1, y + 9), text, sizes=(11, 12))
        if v > best[0]:
            best = (v, y)
    return best[1] if best[0] >= 0.70 else None


def primitive_panel_open(img=None):
    """True when the Create-primitives panel is showing (its type combo is up)."""
    return find_type_combo(img) is not None


def file_dialog_open(img=None):
    """True when a file chooser covers the middle of the window."""
    if img is None:
        img = grab()
    import numpy as np
    a = np.asarray(img.convert('L')).astype(int)
    return a[300, 640] > 200 and a[300, 900] > 200


def menu_command(top, candidates, check, settle=1.4):
    """Run a menu entry, trying several arrow-key counts until `check` passes.

    Menu entries are reached with the arrow keys, which skip the entries that
    happen to be disabled; trying a few neighbouring positions makes the
    command robust against that.
    """
    for idx in candidates:
        ckey('Escape')
        _t(0.3)
        focus()
        k = MENU_KEY.get(top)
        if k is None:
            raise FcError('no keyboard mnemonic known for menu %r' % top)
        ckey('alt', k)
        _t(0.9)
        for _ in range(idx):
            ckey('Down')
            _t(0.12)
        ckey('Return')
        _t(settle)
        for _ in range(4):
            if check():
                return idx
            _t(0.8)
    return None


def new_document():
    focus()
    ckey('Escape')
    _t(0.3)
    ckey('ctrl', 'n')
    _t(3.0)
    if not has_document():
        focus()
        ckey('ctrl', 'n')
        _t(3.5)


def open_document(path):
    focus()
    ckey('Escape')
    _t(0.3)
    ckey('ctrl', 'o')
    _t(2.5)
    cclick(*DLG_NAME)
    _t(0.4)
    ckey('ctrl', 'a')
    ctext(path)
    _t(0.4)
    ckey('Return')
    _t(2.5)


def save_document(path):
    """Save the active document.  Ctrl+S asks for a name when it has none."""
    focus()
    ckey('Escape')
    _t(0.3)
    ckey('ctrl', 's')
    _t(2.5)
    if not file_dialog_open():
        # the document already has a file name: go through File > Save As...
        ckey('Escape')
        _t(0.3)
        menu_command('File', [5], file_dialog_open, settle=2.0)
    if not file_dialog_open():
        raise FcError('Ctrl+S did not open a save dialog (the document may already be saved; '
                      'File > Save As... could not be reached)')
    cclick(*DLG_NAME)
    _t(0.4)
    ckey('ctrl', 'a')
    ctext(path)
    _t(0.4)
    ckey('Return')
    _t(2.0)


def close_document(save=False):
    focus()
    if save:
        ckey('ctrl', 's')
        _t(1.5)
    ckey('ctrl', 'w')
    _t(1.8)
    ckey('Escape')
    _t(0.5)


# -------------------------------------------------------------- modelling --
def _prop_row_index(kind, prop):
    rows = PROPERTIES.get(kind)
    if rows is None:
        return None
    for i, r in enumerate(rows):
        if r.lower() == prop.lower():
            return i
    return None


def property_rows(img=None):
    """Y centres of the rows of the property editor, found from its grid lines.

    The property editor draws a light rule between rows; the rules are evenly
    spaced, so the row that carries a given property can be located without
    knowing how the panel happens to be laid out.
    """
    import numpy as np
    if img is None:
        img = grab()
    a = np.asarray(img.convert('L')).astype(int)
    cand = []
    for y in range(330, 780):
        m = a[y, 3:262].mean()
        if 180 < m < 222:
            cand.append(y)
    # keep only lines that repeat every 20 px
    best = []
    for start in cand:
        run = [start]
        y = start
        while True:
            nxt = [c for c in cand if abs(c - (y + PROP_STEP)) <= 1]
            if not nxt:
                break
            y = min(nxt, key=lambda c: abs(c - (run[-1] + PROP_STEP)))
            run.append(y)
        if len(run) > len(best):
            best = run
    if len(best) < 4:
        return []
    top = best[0]
    return [top - PROP_STEP // 2 + PROP_STEP * i for i in range(len(best))]


def property_row_y(index, img=None):
    rows = property_rows(img)
    if rows and index < len(rows):
        return rows[index]
    return PROP_ROW0 + PROP_STEP * index


def read_property_value(kind, prop):
    i = _prop_row_index(kind, prop)
    if i is None:
        return None
    img = grab()
    y = property_row_y(i, img)
    x0, x1 = PROP_VALUE_BOX
    dx, dy = offset()[0]
    return vision.read_text(img, (x0 + dx, y - 9 + dy, x1 + dx, y + 9 + dy), size=12,
                            candidates=vision.NUMCHARS + 'mdegkMs' + 'I' + 'n' + 'h' + 'u' + 't')


def set_property_value(kind, prop, value):
    i = _prop_row_index(kind, prop)
    if i is None:
        raise FcError('unknown property %r for %s' % (prop, kind))
    y = property_row_y(i)
    cdouble(PROP_NAME_X, y)
    _t(0.6)
    ckey('ctrl', 'a')
    _t(0.2)
    ctext(value)
    _t(0.3)
    ckey('Return')
    _t(1.2)
    return read_property_value(kind, prop)


def select_tree_row(row):
    cclick(TREE_X, TREE_ROW0 + TREE_STEP * row)
    _t(0.9)


TREE_CLICK = (100, 250)
PROP_VALUE_X = 165


MODEL_TAB_X = 31


def combo_tab_row(img=None):
    """Y of the Model/Tasks tab strip of the Combo View dock."""
    import numpy as np
    if img is None:
        img = grab()
    a = np.asarray(img.convert('L')).astype(int)
    for y in range(150, 340):
        if ((a[y, 18:52] < 130).sum() >= 6 and (a[y, 68:112] < 130).sum() >= 6
                and (a[y, 52:68] < 130).sum() == 0):
            return y
    return None


def show_model_tab():
    """Make sure the model tree (not the Tasks panel) is the visible dock page."""
    y = combo_tab_row()
    if y is None:
        return False
    cclick(MODEL_TAB_X, y)
    _t(0.6)
    return True


def has_document():
    """True when a document is open (its properties fill the property editor)."""
    if len(property_rows()) >= 6:
        return True
    show_model_tab()
    cclick(*TREE_CLICK)
    _t(0.4)
    ckey('ctrl', 'Home')
    _t(0.3)
    ckey('Down')
    _t(0.8)
    return len(property_rows()) >= 6


def select_object(index, count=1):
    """Select one object of the active document in the model tree.

    The tree is walked with the keyboard from its last row, so the position of
    the dock and the number of open documents do not matter.
    """
    cclick(*TREE_CLICK)
    _t(0.4)
    ckey('End')
    _t(0.4)
    for _ in range(max(0, count - 1 - index)):
        ckey('Up')
        _t(0.3)
    _t(0.5)


PRIMITIVE_SUB = {'Box': 0, 'Cylinder': 1, 'Sphere': 2, 'Cone': 3, 'Torus': 4, 'Tube': 5}
PART_TOOL_X = {'Box': 805, 'Cylinder': 838, 'Sphere': 871, 'Cone': 904,
               'Torus': 937, 'Tube': 970}
PART_TOOLBAR_Y = 147


def create_from_toolbar(kind):
    """Press the Part workbench toolbar button that makes a primitive."""
    x = PART_TOOL_X.get(kind)
    if x is None:
        raise FcError('no toolbar button known for %r' % kind)
    for dx in (0, 8, -8, 16, -16):
        cclick(x + dx, PART_TOOLBAR_Y)
        _t(2.4)
        yield dx


def select_last_object():
    """Select the last item of the model tree (the object just created)."""
    cclick(*TREE_CLICK)
    _t(0.4)
    ckey('End')
    _t(0.9)


def property_index(kind, prop):
    return _prop_row_index(kind, prop)


def read_property_value_at(kind, prop, img=None):
    i = _prop_row_index(kind, prop)
    if i is None:
        return None
    if img is None:
        img = grab()
    y = property_row_y(i, img)
    dx, dy = offset()[0]
    x0, x1 = PROP_VALUE_BOX
    return vision.read_text(img, (x0 + dx, y - 9 + dy, x1 + dx, y + 9 + dy), size=12,
                            candidates=vision.NUMCHARS + 'mdeg')


def set_property_value_at(kind, prop, value):
    i = _prop_row_index(kind, prop)
    if i is None:
        raise FcError('unknown property %r for %s' % (prop, kind))
    y = property_row_y(i)
    cdouble(PROP_VALUE_X, y)
    _t(0.7)
    ckey('ctrl', 'a')
    _t(0.2)
    ctext(value)
    _t(0.3)
    ckey('Return')
    _t(1.3)
    return read_property_value_at(kind, prop)

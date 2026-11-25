"""High level FreeCAD operations driven through the application's own UI.

Every operation is expressed with the application's own vocabulary (menus,
dialogs, panels, the property editor, the model tree).  Screen coordinates are
derived from the location of the main window's menu bar, which is found by
looking at the screen, so a moved or un-maximised window still works.

Menus are navigated with the keyboard: the highlighted entry is found from its
selection colour and read with the built-in OCR, which is far more reliable
than guessing pixel positions.
"""
import difflib
import re
import time

import numpy as np

import uia
from ocr import Reader, ALL_CHARS, DIGITS, WORDS, ink_mask

# Property names FreeCAD shows for Part objects.  The panel font is small and
# light, so OCR readings are snapped onto this vocabulary when they are close.
PROPERTY_VOCAB = [
    'Label', 'Placement', 'Support', 'Map Mode', 'Attachment', 'Visibility',
    'Length', 'Width', 'Height', 'Radius', 'Radius1', 'Radius2', 'Angle',
    'Angle1', 'Angle2', 'Angle3', 'OuterRadius', 'InnerRadius', 'Polygon',
    'Circumradius', 'Xmin', 'Ymin', 'Zmin', 'X2min', 'Z2min', 'Xmax', 'Ymax',
    'Zmax', 'X2max', 'Z2max', 'Base', 'Tool', 'Shapes', 'ExpressionEngine',
    'Shape', 'Display Mode', 'Line Color', 'Line Width', 'Transparency',
    'Draw Style', 'Point Color', 'Point Size', 'Bounding Box', 'Selectable',
    'Shape Color', 'Shape Material', 'Angular Deflection', 'Deviation',
    'Light Source', 'Line Material', 'B-Spline Degree', 'Maximum Length',
    'Maximum Deviation', 'Segments', 'Number Of Points', 'Trim By Parameter',
]

TREE_VOCAB = ['Application', 'Labels & Attributes', 'Description']

# Offsets below are measured from the centre of the main window's menu bar for
# the reference layout (1280x800, FreeCAD 0.20.2, DejaVu Sans 12px, Combo View
# dock 262 px wide on the left, no Report view).
TREE_TOP = 162
TREE_BOTTOM = 402
PROP_TOP = 420
TABS_Y = 660
PROP_VALUE_X = 150
PROP_VALUE_X0 = 106
PROP_VALUE_X1 = 262
TOOLBAR2_Y = 33
# Part toolbar buttons (x offsets from the window left edge, y from the menu bar)
TOOLBAR_PRIM_Y = 73
PRIM_BUTTONS = {'Cube': 803, 'Cylinder': 836, 'Sphere': 869, 'Cone': 902,
                'Torus': 935, 'Tube': 968}
# Prism and Wedge have no toolbar button in this build; they are only reachable
# through Part > Primitives, which Qt renders scrolled and unreliably.
NO_BUTTON = ('Prism', 'Wedge')

MENU_TITLES = {'File': 23, 'Edit': 51, 'View': 91, 'Tools': 127, 'Macro': 178,
               'Part': 227, 'Measure': 283, 'Windows': 341, 'Help': 409}


class UiError(RuntimeError):
    pass


def similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def norm_conf(text):
    """Fold the glyph confusions the OCR makes most often (l/i/1, r/n, 0/o)."""
    t = text.lower()
    for a, b in (('l', 'i'), ('1', 'i'), ('|', 'i'), ('r', 'n'), ('0', 'o'),
                 ('5', 's'), ('c', 'e'), ('u', 'n')):
        t = t.replace(a, b)
    return t


def name_similar(a, b):
    return max(similar(a, b), similar(norm_conf(a), norm_conf(b)))


def clean(text):
    """Drop the icon/tree-guide glyphs the OCR picks up in front of a label."""
    return re.sub(r'^[^A-Za-z0-9]+', '', text).strip()


def snap(text, vocab):
    """Snap an OCR reading onto the closest known word, when close enough."""
    best, score = text, 0.0
    for word in vocab:
        r = similar(text, word)
        if r > score:
            best, score = word, r
    return best if score >= 0.62 else text


class FreeCAD:
    def __init__(self):
        self.reader = Reader(12)
        self._origin = None
        self._wb = None
        self._menu_bar = None

    # ------------------------------------------------------------- geometry
    def origin(self, refresh=False):
        if self._origin is not None and not refresh:
            return self._origin
        rgb = np.array(uia.screenshot())
        self._origin = self._locate(rgb)
        return self._origin

    @staticmethod
    def _locate(rgb):
        """Find (window_left, menu_bar_centre_y) of the main window."""
        h, w, _ = rgb.shape
        med = np.median(rgb.astype(float), axis=1)
        bright = (med.min(axis=1) >= 228) & (med.max(axis=1) - med.min(axis=1) <= 8)
        menu_y = None
        run = 0
        for y in range(28, min(h, 300)):
            if bright[y]:
                run += 1
                if run >= 12:
                    menu_y = y - run // 2
                    break
            else:
                run = 0
        if menu_y is None:
            menu_y = 79
        band = rgb[max(0, menu_y - 6):menu_y + 6]
        colmed = np.median(band.astype(float), axis=(0, 2))
        left = 0
        for x in range(w):
            if colmed[x] >= 200:
                left = x
                break
        return left, menu_y

    def mx(self, x):
        return self.origin()[0] + x

    def my(self, y):
        return self.origin()[1] + y

    def click(self, x, y):
        uia.click(self.mx(x), self.my(y))

    def dclick(self, x, y):
        uia.double_click(self.mx(x), self.my(y))

    def shot(self):
        return np.array(uia.screenshot())

    # ---------------------------------------------------------------- menus
    MENU_ORDER = ['File', 'Edit', 'View', 'Tools', 'Macro']

    def menu_bar(self, refresh=False):
        """Titles of the menu bar with their x positions, read from the screen.

        The text itself is small and is sometimes mis-read, so only the
        *positions* are used; the order is fixed (File, Edit, View, Tools,
        Macro, <workbench menus...>, Windows, Help).
        """
        if self._menu_bar is not None and not refresh:
            return self._menu_bar
        left, menu_y = self.origin()
        rgb = self.shot()
        region = rgb[menu_y - 11:menu_y + 11, 0:470]
        rows = self.reader.read_region(region, charset=WORDS, split_gap=9)
        out = []
        for r in rows:
            out.append({'title': r['text'], 'x': (r['x0'] + r['x1']) // 2})
        out.sort(key=lambda e: e['x'])
        self._menu_bar = out
        return out

    def title_x(self, title, refresh=False):
        entries = self.menu_bar(refresh=refresh)
        if not entries:
            return MENU_TITLES.get(title)
        if title in self.MENU_ORDER:
            i = self.MENU_ORDER.index(title)
            if i < len(entries):
                return entries[i]['x']
        if title == 'Windows':
            if len(entries) >= 2 and entries[-1]['x'] - entries[-2]['x'] > 60:
                return entries[-2]['x']
            return entries[-1]['x'] - 42
        if title == 'Help':
            return entries[-1]['x']
        # a workbench menu: the first entry after the fixed ones
        if len(entries) > len(self.MENU_ORDER):
            return entries[len(self.MENU_ORDER)]['x']
        return MENU_TITLES.get(title)

    def open_menu(self, title):
        if title not in MENU_TITLES:
            raise UiError('unknown menu %r (known: %s)' % (title, ', '.join(MENU_TITLES)))
        uia.key('Escape')
        for attempt in range(3):
            x = self.title_x(title, refresh=(attempt > 0))
            if x is None:
                break
            self.click(x, 0)
            time.sleep(0.35)
            if self._menu_open():
                return
        raise UiError('could not open the %s menu (menu bar reads: %s)'
                      % (title, ', '.join(e['title'] for e in self.menu_bar())))

    def _menu_open(self):
        """A popup is open when a large light rectangle covers the left area."""
        rgb = self.shot()
        left, menu_y = self.origin()
        patch = rgb[menu_y + 40:menu_y + 200, left + 20:left + 200]
        if patch.size == 0:
            return False
        light = (patch.min(axis=2) > 200).mean()
        return bool(light > 0.75)

    def _menu_lines(self, region):
        """Read the entries inside a screen region: list of {text, x, y}."""
        x0, y0, x1, y1 = [int(v) for v in region]
        x0 = max(0, x0); y0 = max(0, y0)
        rgb = self.shot()
        x1 = min(rgb.shape[1], x1); y1 = min(rgb.shape[0], y1)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return []
        sub = rgb[y0:y1, x0:x1]
        rows = self.reader.read_region(sub, charset=WORDS, split_gap=8, max_height=15)
        groups = {}
        for r in rows:
            groups.setdefault(r['y0'] // 6, []).append(r)
        out = []
        for key in sorted(groups):
            parts = sorted(groups[key], key=lambda r: r['x0'])
            text = re.sub(r'\s+', ' ', ' '.join(p['text'] for p in parts)).strip()
            if not text:
                continue
            yc = int(np.mean([p['y0'] for p in parts] + [p['y1'] for p in parts])) + y0
            xc = min(p['x0'] for p in parts) + x0
            out.append({'text': text, 'x': xc, 'y': yc})
        return out

    def find_menu_item(self, name, region):
        for attempt in range(2):
            item = self._find_menu_item_once(name, region)
            if item is not None:
                return item
            time.sleep(0.3)
        return None

    def _find_menu_item_once(self, name, region):
        target = name.lower().replace('&', '').strip()
        best = None
        for line in self._menu_lines(region):
            text = line['text'].lower().replace('&', '').strip()
            clean = re.sub(r'[^a-z0-9 ]', '', text).strip()
            r = similar(target, clean)
            if clean.startswith(target) or target in clean:
                r = max(r, 0.9)
            if best is None or r > best[0]:
                best = (r, line)
        if best is None or best[0] < 0.55:
            return None
        return best[1]

    @staticmethod
    def _submenu_region(before, after, fallback):
        """Bounding box of the popup that appeared after clicking a menu entry."""
        b = before.astype(np.int16)
        a = after.astype(np.int16)
        d = (np.abs(a - b).sum(axis=2) > 30)
        if not d.any():
            return fallback
        colcount = d.sum(axis=0)
        cols = np.where(colcount > 40)[0]
        if len(cols) == 0:
            return fallback
        x0, x1 = int(cols.min()), int(cols.max()) + 1
        rows = d[:, x0:x1].sum(axis=1)
        ys = np.where(rows > 3)[0]
        if len(ys) == 0:
            return fallback
        return (x0, int(ys.min()), x1, int(ys.max()) + 1)

    def menu_command(self, *path):
        """Walk a menu path, e.g. menu_command('Part', 'Boolean', 'Cut').

        Each popup is located from the screen itself: the first from the menu
        title, the following ones from the area that changes when the parent
        entry is clicked.
        """
        if not path:
            raise UiError('menu path required')
        left, menu_y = self.origin()
        self.open_menu(path[0])
        region = (MENU_TITLES[path[0]] - 6, menu_y + 10,
                  MENU_TITLES[path[0]] + 330, menu_y + 740)
        for i, name in enumerate(path[1:]):
            item = self.find_menu_item(name, region)
            if item is None:
                uia.key('Escape')
                raise UiError('menu item %r not found under %s' % (name, ' > '.join(path[:i + 1])))
            before = self.shot()
            uia.click(item['x'] + 10, item['y'])
            time.sleep(0.45)
            after = self.shot()
            fallback = (max(0, MENU_TITLES[path[0]] - 6), menu_y + 10,
                        MENU_TITLES[path[0]] + 700, menu_y + 745)
            region = self._submenu_region(before, after, fallback)
            if region[2] - region[0] < 60 or region[3] - region[1] < 40:
                region = fallback
        return True

    def menu_items(self, title):
        """List the entries of a top-level menu."""
        left, menu_y = self.origin()
        self.open_menu(title)
        time.sleep(0.3)
        out = [l['text'] for l in self._menu_lines(
            (MENU_TITLES[title] - 6, menu_y + 10, MENU_TITLES[title] + 330, menu_y + 740))]
        uia.key('Escape')
        return out

    def click_toolbar(self, name):
        """Click one of the Part primitive buttons in the toolbar."""
        x = PRIM_BUTTONS.get(name)
        if x is None:
            return False
        uia.click(self.mx(x), self.my(TOOLBAR_PRIM_Y))
        time.sleep(1.0)
        return True

    # ----------------------------------------------------------- workbenches
    # Order of the entries in the workbench selector (View > Workbench and the
    # combo box in the toolbar show the same list).
    WORKBENCHES = ['Arch', 'Draft', 'Drawing', 'FEM', 'Image', 'Inspection',
                   'Mesh Design', '<none>', 'OpenSCAD', 'Part Design', 'Part',
                   'Path', 'Points', 'Raytracing', 'Reverse Engineering',
                   'Robot', 'Sketcher', 'Spreadsheet', 'Start', 'Surface',
                   'TechDraw', 'Test Framework', 'Web']

    def switch_workbench(self, name):
        """Activate a workbench through the selector combo box in the toolbar."""
        target = None
        for wb in self.WORKBENCHES:
            if wb.lower() == name.lower():
                target = wb
                break
        if target is None:
            best = max(self.WORKBENCHES, key=lambda w: similar(name, w))
            if similar(name, best) < 0.6:
                raise UiError('unknown workbench %r; known: %s'
                              % (name, ', '.join(self.WORKBENCHES)))
            target = best
        idx = self.WORKBENCHES.index(target)
        uia.key('Escape')
        self.click(600, TOOLBAR2_Y)
        time.sleep(0.6)
        uia.key('Home')
        time.sleep(0.1)
        for _ in range(idx):
            uia.key('Down')
            time.sleep(0.04)
        uia.key('Return')
        time.sleep(0.9)
        self._wb = target
        return target

    def workbench(self):
        """Name of the active workbench (tracked from our own switches, else read)."""
        if self._wb:
            return self._wb
        left, menu_y = self.origin()
        rgb = self.shot()
        region = rgb[menu_y + TOOLBAR2_Y - 11:menu_y + TOOLBAR2_Y + 11, 470:700]
        rows = self.reader.read_lines(region, charset=WORDS, gap=7)
        if not rows:
            return ''
        text = rows[0]['text']
        best = max(self.WORKBENCHES, key=lambda w: similar(text, w))
        return best if similar(text, best) >= 0.5 else text

    # ------------------------------------------------------------------- tree
    def _panel_region(self, rgb, x0, x1):
        left, menu_y = self.origin()
        h = rgb.shape[0]
        return rgb[menu_y + 120:h - 24, left + x0:left + x1], menu_y + 120

    def _name_rows(self, rgb=None):
        """Rows of the left dock, read from the property-name column."""
        rgb = rgb if rgb is not None else self.shot()
        region, y0 = self._panel_region(rgb, 2, 104)
        rows = self.reader.read_lines(region, charset=WORDS, gap=7, thr=170, max_height=15)
        for r in rows:
            r['y'] = (r['y0'] + r['y1']) // 2 + y0
        return rows

    def _header_y(self, rgb=None):
        for r in self._name_rows(rgb):
            t = r['text'].strip()
            if similar('Property', t) > 0.55 or t.lower().startswith('prop'):
                return r['y']
        return None

    def _tree_lines(self, rgb=None):
        rgb = rgb if rgb is not None else self.shot()
        hy = self._header_y(rgb)
        left, menu_y = self.origin()
        h = rgb.shape[0]
        y1 = (hy - 8) if hy else h - 200
        region = rgb[menu_y + 150:y1, left + 2:left + 250]
        rows = self.reader.read_lines(region, charset=WORDS, gap=7, max_height=15)
        out = []
        for r in rows:
            t = r['text'].strip()
            if similar('Labels', t) > 0.5 or similar('Application', t) > 0.6:
                continue
            r['y'] = (r['y0'] + r['y1']) // 2 + menu_y + 150
            out.append(r)
        return out, 0

    def tree_items(self):
        self._clear_tree_highlight()
        rows, _ = self._tree_lines()
        return [{'label': r['text'], 'y': r['y'], 'conf': r['conf']} for r in rows]

    def _clear_tree_highlight(self):
        """Click an empty spot in the tree so the selected row is not highlighted."""
        try:
            rgb0 = self.shot()
            hy0 = self._header_y(rgb0) or (rgb0.shape[0] - 300)
            uia.click(self.mx(150), hy0 - 40)
            time.sleep(0.25)
        except Exception:
            pass

    def select_object(self, label):
        self._clear_tree_highlight()
        rows, _ = self._tree_lines()
        best = None
        for r in rows:
            ratio = name_similar(label, clean(r['text']))
            if label.lower() in r['text'].lower():
                ratio = max(ratio, 0.9)
            if best is None or ratio > best[0]:
                best = (ratio, r)
        if best is None or best[0] < 0.5:
            seen = ', '.join(r['text'] for r in rows) or '(tree empty)'
            raise UiError('object %r not found in the model tree; tree shows: %s' % (label, seen))
        uia.click(self.mx(70), best[1]['y'])
        # clear the property-row highlight so the panel text reads cleanly
        try:
            rgb = self.shot()
            uia.click(self.mx(150), rgb.shape[0] - 42)
            time.sleep(0.25)
        except Exception:
            pass
        return best[1]['text']

    # -------------------------------------------------------------- property
    def _prop_lines(self, rgb=None):
        rows = self._name_rows(rgb)
        out = []
        started = False
        for r in rows:
            t = r['text'].strip()
            if not started:
                if similar('Property', t) > 0.55 or t.lower().startswith('prop'):
                    started = True
                continue
            if similar('View', t) > 0.8 or similar('Data', t) > 0.8:
                break
            out.append(r)
        return out, 0

    def _find_prop(self, name):
        rows, _ = self._prop_lines()
        best = self._score_prop(name, rows)
        if best is None or best[0] < 0.55:
            # the row we want may be painted with a selection highlight; clear it
            self._clear_prop_highlight()
            rows, _ = self._prop_lines()
            again = self._score_prop(name, rows)
            if again is not None and (best is None or again[0] > best[0]):
                best = again
        if best is None or best[0] < 0.5:
            seen = ', '.join(r['text'] for r in rows) or '(none visible)'
            raise UiError('property %r is not shown in the property editor; visible: %s'
                          % (name, seen))
        r = best[1]
        return {'name': best[2], 'raw': r['text'], 'y': r['y']}

    def _clear_prop_highlight(self):
        try:
            rows, _ = self._prop_lines()
            rgb = self.shot()
            h = rgb.shape[0]
            y = min(h - 40, max([r['y'] for r in rows] + [h - 200]) + 30)
            uia.click(self.mx(150), y)
            time.sleep(0.3)
        except Exception:
            pass

    def _score_prop(self, name, rows):
        best = None
        for r in rows:
            cand = snap(clean(r['text']), PROPERTY_VOCAB)
            ratio = name_similar(name, cand)
            if name.lower() == cand.lower():
                ratio = 1.0
            if best is None or ratio > best[0]:
                best = (ratio, r, cand)
        return best

    def show_data_tab(self):
        self._click_tab('Data')

    def show_view_tab(self):
        self._click_tab('View')

    def _click_tab(self, which):
        rgb = self.shot()
        h = rgb.shape[0]
        left, menu_y = self.origin()
        region = rgb[h - 70:h - 10, left:left + 200]
        rows = self.reader.read_region(region, charset=WORDS, split_gap=4)
        for r in rows:
            if similar(which, r['text']) > 0.6:
                uia.click(left + (r['x0'] + r['x1']) // 2, h - 70 + (r['y0'] + r['y1']) // 2)
                time.sleep(0.35)
                return True
        # fall back to the reference position
        uia.click(self.mx(76 if which == 'Data' else 22), self.my(701))
        time.sleep(0.35)
        return False

    def set_property(self, name, value):
        row = self._find_prop(name)
        uia.double_click(self.mx(PROP_VALUE_X), row['y'])
        uia.key('ctrl', 'a')
        uia.type_text(str(value))
        uia.key('Return')
        time.sleep(0.4)
        return row

    def read_property(self, name, charset=None):
        row = self._find_prop(name)
        rgb = self.shot()
        y = row['y']
        best = ('', -1.0)
        for dy in (0, -2, 2, -4, 4):
            top = y + dy - 8
            region = rgb[top:y + dy + 8, PROP_VALUE_X0:PROP_VALUE_X1]
            for line in self.reader.read_lines(region, charset=charset, gap=7):
                centre = top + (line['y0'] + line['y1']) / 2.0
                score = line['conf'] - abs(centre - y) / 40.0
                if score > best[1]:
                    best = (line['text'], score)
        return best[0]

    def property_rows(self):
        """Every property row currently displayed: name, displayed value."""
        rows, _ = self._prop_lines()
        rgb = self.shot()
        out = []
        for r in rows:
            best = ('', -1.0)
            for dy in (0, -4, 4, -2, 2):
                region = rgb[r['y'] + dy - 9:r['y'] + dy + 9, PROP_VALUE_X0:PROP_VALUE_X1]
                lines = self.reader.read_lines(region, gap=7)
                if lines and lines[0]['conf'] > best[1]:
                    best = (lines[0]['text'], lines[0]['conf'])
            out.append({'name': snap(clean(r['text']), PROPERTY_VOCAB),
                        'raw_name': r['text'],
                        'value': best[0]})
        return out

    # -------------------------------------------------------------- misc UI
    def status_text(self):
        """Text of the status bar at the very bottom of the window."""
        rgb = self.shot()
        h = rgb.shape[0]
        rows = self.reader.read_lines(rgb[h - 16:h - 2, 4:900], gap=7)
        return rows[0]['text'] if rows else ''

    def read_window_text(self):
        """Read the tree, the property panel and the status bar."""
        rgb = self.shot()
        tree, _ = self._tree_lines(rgb)
        props, _ = self._prop_lines(rgb)
        out = {'tree': [r['text'] for r in tree], 'properties': [], 'status': ''}
        for r in props:
            best = ('', -1.0)
            for dy in (0, -2, 2, -4, 4):
                region = rgb[r['y'] + dy - 8:r['y'] + dy + 8,
                             PROP_VALUE_X0:PROP_VALUE_X1]
                lines = self.reader.read_lines(region, gap=7)
                if lines and lines[0]['conf'] > best[1]:
                    best = (lines[0]['text'], lines[0]['conf'])
            out['properties'].append({'name': snap(clean(r['text']), PROPERTY_VOCAB),
                                      'value': best[0]})
        out['status'] = self.status_text()
        return out




# --------------------------------------------------------------- file dialogs
def _dialog_scan(self, y0, y1, x0, x1, gap=10):
    rgb = self.shot()
    h, w, _ = rgb.shape
    y0 = max(0, min(h - 10, y0)); y1 = max(y0 + 10, min(h, y1))
    x0 = max(0, min(w - 10, x0)); x1 = max(x0 + 10, min(w, x1))
    rows = self.reader.read_region(rgb[y0:y1, x0:x1], charset=WORDS, split_gap=gap)
    for r in rows:
        r['sx'] = r['x0'] + x0
        r['sy'] = (r['y0'] + r['y1']) // 2 + y0
    return rows


def _find_dialog_row(self, needle, y0, y1, x0, x1):
    for r in self._dialog_scan(y0, y1, x0, x1):
        if similar(needle, r['text']) > 0.55 or needle.lower() in r['text'].lower():
            return r
    return None


def set_file_dialog_filter(self, keyword):
    """Pick a file-type filter in the currently open file dialog."""
    rgb = self.shot()
    h, w, _ = rgb.shape
    row = self._find_dialog_row('Files of type', h // 2 - 200, h - 60, w // 4, 3 * w // 4)
    y = row['sy'] if row else h - 178
    uia.click(w // 2, y)
    time.sleep(0.5)
    uia.type_text(keyword)
    time.sleep(0.6)
    uia.key('Return')
    time.sleep(0.7)
    return keyword


def click_dialog_button(self, label):
    """Click a button of a modal dialog by its caption."""
    rgb = self.shot()
    h, w, _ = rgb.shape
    rows = self._dialog_scan(h // 4, h - 20, 0, w, gap=12)
    best = None
    for r in rows:
        t = r['text'].strip()
        if not t or len(t) > 14:
            continue
        score = similar(label, t)
        if label.lower() in t.lower():
            score = max(score, 0.9)
        if best is None or score > best[0]:
            best = (score, r)
    if best is None or best[0] < 0.6:
        return 'no dialog button matching %r was found' % label
    uia.click(best[1]['sx'] + 20, best[1]['sy'])
    time.sleep(0.8)
    return 'clicked %s' % best[1]['text']


FreeCAD._dialog_scan = _dialog_scan
FreeCAD._find_dialog_row = _find_dialog_row
FreeCAD.set_file_dialog_filter = set_file_dialog_filter
FreeCAD.click_dialog_button = click_dialog_button
FreeCAD._dialog_scan = _dialog_scan
FreeCAD._find_dialog_row = _find_dialog_row
FreeCAD.set_file_dialog_filter = set_file_dialog_filter
FreeCAD.click_dialog_button = click_dialog_button

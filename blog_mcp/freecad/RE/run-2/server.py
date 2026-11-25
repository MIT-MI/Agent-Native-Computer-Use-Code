#!/usr/bin/env python3
"""
FreeCAD MCP server.

Controls a *running* FreeCAD instance through FreeCAD's own embedded Python
scripting interface, executed on the application's main (GUI) thread via a
queued Qt invocation.  Nothing is done by driving the window.

The server speaks MCP JSON-RPC over stdin/stdout (newline-delimited JSON).
Diagnostics go to stderr.
"""
import sys, os, json, time, glob, subprocess, tempfile, shutil, traceback, threading

# --------------------------------------------------------------------------
# MCP plumbing
# --------------------------------------------------------------------------
SERVER_INFO = {"name": "freecad-mcp", "version": "1.0.0"}

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def log(*a):
    try:
        sys.stderr.write("[freecad-mcp] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Preamble executed *inside* FreeCAD.  Provides value conversion helpers and
# a main-thread dispatcher so document/GUI work happens on the GUI thread.
# --------------------------------------------------------------------------
PREAMBLE = r'''
import json as _json, traceback as _tb, math as _math, os as _os, time as _time
import FreeCAD as _App
try:
    import FreeCADGui as _Gui
except Exception:
    _Gui = None

def _fc_run_on_main(fn, timeout=90):
    """Run fn() on the Qt main thread; return its value.  Runs inline if we are
    already on the main thread or if there is no Qt event loop."""
    try:
        from PySide2 import QtCore
        app = QtCore.QCoreApplication.instance()
    except Exception:
        app = None
    if app is None:
        return fn()
    try:
        if QtCore.QThread.currentThread() is app.thread():
            return fn()
    except Exception:
        pass
    global _FC_RUNNER
    if '_FC_RUNNER' not in globals() or _FC_RUNNER is None:
        class _FCRunner(QtCore.QObject):
            _sig = QtCore.Signal()
            def __init__(self):
                QtCore.QObject.__init__(self)
                self.task = None
                self.done = False
                self.exc = None
                self.val = None
                self._sig.connect(self._run, QtCore.Qt.QueuedConnection)
            def _run(self):
                try:
                    self.val = self.task()
                except BaseException as e:
                    self.exc = e
                finally:
                    self.done = True
        r = _FCRunner()
        r.moveToThread(app.thread())
        _FC_RUNNER = r
    r = _FC_RUNNER
    r.task = fn
    r.done = False
    r.exc = None
    r.val = None
    r._sig.emit()
    t0 = _time.time()
    while not r.done:
        if _time.time() - t0 > timeout:
            raise RuntimeError("main-thread operation timed out after %ss "
                               "(a modal dialog may be open in FreeCAD)" % timeout)
        _time.sleep(0.01)
    if r.exc is not None:
        raise r.exc
    return r.val

# ---- value conversion -----------------------------------------------------
def _objref(o):
    try:
        return {"name": o.Name, "label": o.Label, "typeId": o.TypeId,
                "document": getattr(o, "Document", None) and o.Document.Name}
    except Exception:
        return str(o)

def _shape_summary(s):
    d = {}
    try: d["shapeType"] = s.ShapeType
    except Exception: pass
    try: d["isValid"] = bool(s.isValid())
    except Exception: pass
    try: d["volume_mm3"] = s.Volume
    except Exception: pass
    try: d["area_mm2"] = s.Area
    except Exception: pass
    try: d["length_mm"] = s.Length
    except Exception: pass
    try: d["centerOfMass"] = [s.CenterOfMass.x, s.CenterOfMass.y, s.CenterOfMass.z]
    except Exception: pass
    try:
        bb = s.BoundBox
        d["boundBox"] = {"min": [bb.XMin, bb.YMin, bb.ZMin], "max": [bb.XMax, bb.YMax, bb.ZMax],
                         "size": [bb.XLength, bb.YLength, bb.ZLength]}
    except Exception: pass
    try:
        d["counts"] = {"solids": len(s.Solids), "shells": len(s.Shells), "faces": len(s.Faces),
                       "edges": len(s.Edges), "vertices": len(s.Vertexes)}
    except Exception: pass
    return d

def _geo_val(g):
    tn = type(g).__name__
    d = {"type": tn}
    for attr, key in (("StartPoint", "start"), ("EndPoint", "end"), ("Center", "center")):
        try:
            p = getattr(g, attr)
            d[key] = [p.x, p.y, p.z]
        except Exception:
            pass
    try:
        d["radius"] = g.Radius
    except Exception:
        pass
    try:
        d["point"] = [g.X, g.Y, g.Z]
    except Exception:
        pass
    return d


def _con_val(c):
    d = {"type": c.Type}
    for attr, key in (("Value", "value"), ("First", "first"), ("FirstPos", "firstPos"),
                      ("Second", "second"), ("SecondPos", "secondPos"),
                      ("Third", "third"), ("ThirdPos", "thirdPos"), ("Name", "name")):
        try:
            d[key] = getattr(c, attr)
        except Exception:
            pass
    return d


def _val(v, depth=0):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if depth > 6:
        return str(v)
    tn = type(v).__name__
    if tn in ("LineSegment", "Line", "Circle", "ArcOfCircle", "ArcOfEllipse", "Ellipse",
              "Point", "BSplineCurve", "BezierCurve"):
        return _geo_val(v)
    if tn == "Constraint":
        return _con_val(v)
    if tn in ("Vector", "Vector2d"):
        d = {"x": v.x, "y": v.y}
        if hasattr(v, "z"):
            d["z"] = v.z
        return d
    if tn == "Rotation":
        return {"axis": [v.Axis.x, v.Axis.y, v.Axis.z], "angle_deg": _math.degrees(v.Angle),
                "yaw_pitch_roll_deg": list(v.toEuler())}
    if tn == "Placement":
        return {"position": [v.Base.x, v.Base.y, v.Base.z], "rotation": _val(v.Rotation, depth + 1)}
    if tn == "BoundBox":
        return {"min": [v.XMin, v.YMin, v.ZMin], "max": [v.XMax, v.YMax, v.ZMax]}
    if tn == "Matrix":
        return {"A11": v.A11, "A12": v.A12, "A13": v.A13, "A14": v.A14,
                "A21": v.A21, "A22": v.A22, "A23": v.A23, "A24": v.A24,
                "A31": v.A31, "A32": v.A32, "A33": v.A33, "A34": v.A34}
    if tn in ("Shape", "Solid", "Shell", "Face", "Edge", "Vertex", "Wire",
              "Compound", "CompSolid") or (hasattr(v, "ShapeType") and hasattr(v, "BoundBox")):
        return _shape_summary(v)
    if hasattr(v, "Value") and hasattr(v, "Unit") and hasattr(v, "UserString"):
        # Base.Quantity (PropertyLength / PropertyAngle / ...): numeric value in its unit
        try:
            return v.Value
        except Exception:
            return str(v)
    if hasattr(v, "TypeId") and hasattr(v, "Name"):
        return _objref(v)
    if isinstance(v, (list, tuple)):
        return [_val(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): _val(x, depth + 1) for k, x in v.items()}
    return str(v)

def _doc(name):
    if name is None or name == "":
        d = _App.ActiveDocument
    else:
        d = _App.getDocument(name)
        if d is None:
            for x in _App.listDocuments().values():
                if x.Label == name:
                    d = x
                    break
    if d is None:
        raise ValueError("document not found: %r (open: %r)" % (name, list(_App.listDocuments().keys())))
    return d

def _obj(d, name):
    o = d.getObject(name)
    if o is None:
        for x in d.Objects:
            if x.Label == name:
                o = x
                break
    if o is None:
        raise ValueError("object not found: %r in %s (objects: %r)" % (name, d.Name, [x.Name for x in d.Objects]))
    return o

def _conv(tid, value):
    short = tid.split("::")[-1]
    if short in ("PropertyVector", "PropertyVectorDistance", "PropertyPosition", "PropertyDirection"):
        if isinstance(value, (list, tuple)):
            return _App.Vector(float(value[0]), float(value[1]), float(value[2]))
        return value
    if short == "PropertyPlacement":
        if isinstance(value, dict):
            pos = value.get("position", [0, 0, 0])
            rot = value.get("rotation")
            if rot is None:
                r = _App.Rotation()
            elif isinstance(rot, dict):
                ax = rot.get("axis", [0, 0, 1])
                ang = rot.get("angle_deg", 0.0)
                r = _App.Rotation(_App.Vector(float(ax[0]), float(ax[1]), float(ax[2])), float(ang))
            elif isinstance(rot, (list, tuple)) and len(rot) == 4:
                r = _App.Rotation(_App.Vector(float(rot[0]), float(rot[1]), float(rot[2])), float(rot[3]))
            else:
                r = _App.Rotation()
            return _App.Placement(_App.Vector(float(pos[0]), float(pos[1]), float(pos[2])), r)
        return value
    if short == "PropertyRotation":
        if isinstance(value, dict):
            ax = value.get("axis", [0, 0, 1]); ang = value.get("angle_deg", 0.0)
            return _App.Rotation(_App.Vector(float(ax[0]), float(ax[1]), float(ax[2])), float(ang))
        return value
    if short == "PropertyColor":
        if isinstance(value, str) and value.startswith("#") and len(value) >= 7:
            value = [int(value[1:3], 16) / 255.0, int(value[3:5], 16) / 255.0, int(value[5:7], 16) / 255.0]
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            v = list(value) + [0.0] * (4 - len(value))
            return (float(v[0]), float(v[1]), float(v[2]), float(v[3]))
        return value
    if short == "PropertyLink":
        if isinstance(value, str):
            d = _App.ActiveDocument
            return d.getObject(value) if d else value
        return value
    if short == "PropertyLinkSub":
        return value
    if short == "PropertyInteger":
        return int(value)
    if short in ("PropertyFloat", "PropertyLength", "PropertyDistance", "PropertyAngle",
                 "PropertySpeed", "PropertyAcceleration", "PropertyArea", "PropertyVolume",
                 "PropertyQuantity", "PropertyForce", "PropertyPressure"):
        return float(value)
    if short == "PropertyBool":
        return bool(value)
    if short == "PropertyString":
        return str(value)
    return value

def _setprop(obj, name, value):
    target = obj
    tid = None
    try:
        tid = obj.getTypeIdOfProperty(name)
    except Exception:
        tid = None
    if not tid:
        _vo = getattr(obj, "ViewObject", None)
        if _vo is not None:
            try:
                tid = _vo.getTypeIdOfProperty(name)
            except Exception:
                tid = None
            if tid:
                target = _vo
    if not tid:
        raise ValueError("object %s has no data or view property %r" % (obj.Name, name))
    if value is None:
        return
    obj = target
    short = tid.split("::")[-1]
    doc = getattr(obj, "Document", None)
    if short in ("PropertyLink", "PropertyLinkChild") and isinstance(value, str):
        value = (doc.getObject(value) if doc else None) or value
    elif short in ("PropertyLinkList", "PropertyLinkChild") and isinstance(value, list):
        value = [((doc.getObject(v) if doc else None) or v) if isinstance(v, str) else v for v in value]
    elif short == "PropertyLinkSub":
        if isinstance(value, str):
            value = (doc.getObject(value) if doc else None)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            a, b = value
            if isinstance(a, str):
                a = doc.getObject(a) if doc else None
            if isinstance(b, str):
                b = [b] if b else []
            value = (a, b)
    elif short == "PropertyLinkSubList" and isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                if "." in item:
                    onm, sub = item.split(".", 1)
                    tgt = doc.getObject(onm) if doc else None
                    # A Body/App::Part exposes its datum planes through its Origin child,
                    # so accept 'Body.XY_Plane' as well as 'Body.Origin.XY_Plane'.
                    try:
                        names = [f.Name for f in tgt.Origin.OriginFeatures]
                    except Exception:
                        names = []
                    if sub in names:
                        tgt = tgt.Origin
                    out.append((tgt, [sub]))
                else:
                    out.append(((doc.getObject(item) if doc else None), []))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                subs = item[1]
                if not isinstance(subs, (list, tuple)):
                    subs = [subs]
                out.append(((doc.getObject(item[0]) if doc else None), list(subs)))
            else:
                out.append(item)
        value = out
    else:
        value = _conv(tid, value)
    setattr(obj, name, value)


def _getprop(obj, name):
    """Read a property from the object or, failing that, its ViewObject."""
    try:
        return obj.getPropertyByName(name)
    except Exception:
        pass
    _vo = getattr(obj, "ViewObject", None)
    if _vo is not None:
        return _vo.getPropertyByName(name)
    raise ValueError("no property %r" % name)


class _Tx(object):
    """Context manager: commit on success, abort on error (undo-able change)."""
    def __init__(self, d, label):
        self.d = d
        self.label = label
    def __enter__(self):
        try:
            self.d.openTransaction(self.label)
        except Exception:
            pass
        return self.d
    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self.d.commitTransaction()
            else:
                self.d.abortTransaction()
        except Exception:
            pass
        return False
'''


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------
class BridgeError(Exception):
    pass


JS_TEMPLATE = r'''
var _py = %s;
function _exp(n) {
  try { var p = Module.findGlobalExportByName(n); if (p) return p; } catch (e) {}
  try { var p = Module.findExportByName(null, n); if (p) return p; } catch (e) {}
  return null;
}
var _ensure = new NativeFunction(_exp("PyGILState_Ensure"), 'pointer', []);
var _release = new NativeFunction(_exp("PyGILState_Release"), 'void', ['pointer']);
var _run = new NativeFunction(_exp("PyRun_SimpleString"), 'int', ['pointer']);
rpc.exports.runpy = function () {
  var cp = Memory.allocUtf8String(_py);
  var st = _ensure();
  var rc;
  try { rc = _run(cp); } finally { _release(st); }
  return rc;
};
'''


class AppBridge(object):
    CANDIDATE_BINS = ("freecad-python3", "freecad", "FreeCAD", "freecadcmd-python3")

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp(prefix="freecad-mcp-")
        self.lock = threading.Lock()
        self._pid = None
        self._method = None
        self._preferred = os.environ.get("FREECAD_MCP_METHOD")  # "frida" | "gdb"
        self.last_errors = []

    # -- process discovery -------------------------------------------------
    def find_pid(self):
        env = os.environ.get("FREECAD_PID")
        if env:
            try:
                return int(env)
            except Exception:
                pass
        cands = []
        for p in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(os.path.basename(p))
            except Exception:
                continue
            try:
                with open(os.path.join(p, "cmdline"), "rb") as f:
                    raw = f.read()
            except Exception:
                continue
            if not raw:
                continue
            args = raw.split(b"\x00")
            exe = os.path.basename(args[0].decode("utf-8", "replace"))
            if exe in self.CANDIDATE_BINS:
                if any(a.startswith(b"--type=") for a in args):
                    continue
                cands.append(pid)
        if not cands:
            return None
        for pid in cands:
            try:
                with open("/proc/%d/maps" % pid) as f:
                    m = f.read()
                if "libFreeCADGui" in m:
                    return pid
            except Exception:
                pass
        return cands[0]

    # -- mechanisms --------------------------------------------------------
    def _run_frida(self, pid, prog_path, timeout):
        try:
            frida = _import_frida()
        except Exception as e:
            raise BridgeError("frida not available: %r" % e)
        code = open(prog_path, encoding="utf-8").read()
        js = JS_TEMPLATE % json.dumps(code)
        session = frida.attach(pid)
        script = session.create_script(js)
        script.on("message", lambda m, d: log("frida:", json.dumps(m)[:400]))
        script.load()
        box = {}

        def work():
            try:
                box["rc"] = script.exports_sync.runpy()
            except Exception as e:
                box["err"] = e

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            def _detach():
                try:
                    session.detach()
                except Exception:
                    pass
            threading.Thread(target=_detach, daemon=True).start()
            raise BridgeError("operation timed out after %ss (FreeCAD may be showing a modal dialog)" % timeout)
        try:
            session.detach()
        except Exception:
            pass
        if "err" in box:
            raise BridgeError("frida rpc error: %r" % (box["err"],))

    def _run_gdb(self, pid, prog_path, timeout):
        gdb = shutil.which("gdb")
        if not gdb:
            raise BridgeError("gdb not found")
        call = 'call (int)PyRun_SimpleString("exec(open(\'%s\').read())")' % prog_path
        args = [gdb, "-p", str(pid), "-batch", "-nx",
                "-iex", "set pagination off", "-iex", "set confirm off",
                "-ex", "thread 1",
                "-ex", "set $st = (int)PyGILState_Ensure()",
                "-ex", call,
                "-ex", "call (int)PyGILState_Release($st)",
                "-ex", "detach", "-ex", "quit"]
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BridgeError("gdb timed out after %ss" % timeout)
        out = (r.stdout or "") + (r.stderr or "")
        if "Operation not permitted" in out or "not being run" in out:
            raise BridgeError("gdb could not attach (ptrace not permitted)")

    # -- public API --------------------------------------------------------
    def call(self, program_body, timeout=100):
        with self.lock:
            return self._call_once(program_body, timeout)

    @staticmethod
    def build_program(program_body, result_path):
        indented = "\n".join("    " + l for l in program_body.splitlines())
        tail = ("\ntry:\n    _r = _fc_run_on_main(_fc_task)\n"
                "except BaseException:\n    _r = {'error': _tb.format_exc()}\n"
                "try:\n    _s = _json.dumps(_r, default=str)\n"
                "except Exception as _e:\n    _s = _json.dumps({'error': 'serialisation failed: ' + repr(_e)})\n"
                "open(" + repr(result_path) + ", 'w').write(_s)\n")
        return PREAMBLE + "\n\ndef _fc_task():\n" + indented + "\n" + tail

    def _call_once(self, program_body, timeout):
        pid = self.find_pid()
        if pid is None:
            raise BridgeError("FreeCAD process not found (is the application running?)")
        self._pid = pid
        d = self.tmpdir
        prog_path = os.path.join(d, "prog.py")
        result_path = os.path.join(d, "result.json")
        try:
            os.remove(result_path)
        except OSError:
            pass
        prog = self.build_program(program_body, result_path)
        with open(prog_path, "w", encoding="utf-8") as f:
            f.write(prog)

        order = ["frida", "gdb"] if self._preferred != "gdb" else ["gdb", "frida"]
        errors = []
        self.last_errors = errors
        for m in order:
            try:
                if m == "frida":
                    self._run_frida(pid, prog_path, timeout)
                else:
                    self._run_gdb(pid, prog_path, timeout)
            except Exception as e:
                errors.append("%s: %s" % (m, e))
                continue
            if os.path.exists(result_path):
                self._method = m
                self._preferred = m
                break
            else:
                errors.append("%s: no result produced" % m)
        if not os.path.exists(result_path):
            raise BridgeError("no result produced. " + " | ".join(errors))
        try:
            with open(result_path, encoding="utf-8") as f:
                return json.loads(f.read())
        except Exception as e:
            raise BridgeError("could not parse result: %r" % e)


BRIDGE = AppBridge()


# --------------------------------------------------------------------------
# Tool code generators
# --------------------------------------------------------------------------
def _j(v):
    """Embed a JSON value as a Python literal (None/True/False, dicts, lists)."""
    return repr(v)


def code_list_documents(args):
    return """
docs = []
for name, d in _App.listDocuments().items():
    try:
        docs.append({"name": d.Name, "label": d.Label, "fileName": d.FileName,
                     "objectCount": len(d.Objects), "saved": bool(d.isSaved()),
                     "active": (_App.ActiveDocument is d)})
    except Exception as e:
        docs.append({"name": name, "error": repr(e)})
return {"documents": docs,
        "active": _App.ActiveDocument.Name if _App.ActiveDocument else None,
        "gui": bool(_Gui)}
"""


def code_get_document(args):
    doc = args.get("document")
    inc = args.get("include_objects", True)
    return """
d = _doc(%s)
info = {"name": d.Name, "label": d.Label, "fileName": d.FileName,
        "saved": bool(d.isSaved()), "active": (_App.ActiveDocument is d),
        "properties": {}}
for p in ("Comment", "Company", "CreatedBy", "LastModifiedBy", "CreationDate",
          "LastModifiedDate", "License", "LicenseURL", "Uid", "Label"):
    try:
        info["properties"][p] = _val(getattr(d, p))
    except Exception:
        pass
if %s:
    objs = []
    for o in d.Objects:
        e = {"name": o.Name, "label": o.Label, "typeId": o.TypeId}
        try:
            e["visible"] = bool(o.ViewObject.Visibility) if getattr(o, "ViewObject", None) else None
        except Exception:
            e["visible"] = None
        try:
            e["inList"] = [x.Name for x in o.InList]
            e["outList"] = [x.Name for x in o.OutList]
        except Exception:
            pass
        objs.append(e)
    info["objects"] = objs
return info
""" % (_j(doc), "True" if inc else "False")


def code_new_document(args):
    name = args.get("name")
    label = args.get("label")
    return """
d = _App.newDocument(%s)
%s
return {"name": d.Name, "label": d.Label, "fileName": d.FileName, "objectCount": len(d.Objects)}
""" % (_j(name) if name else "", ("d.Label = %s" % _j(label)) if label else "")


def code_open_document(args):
    path = args.get("path")
    return """
import os as _o
p = %s
if not _o.path.exists(p):
    raise ValueError("file does not exist: %%s" %% p)
d = _App.openDocument(p)
return {"name": d.Name, "label": d.Label, "fileName": d.FileName,
        "objectCount": len(d.Objects), "objects": [o.Name for o in d.Objects]}
""" % _j(path)


def code_save_document(args):
    doc = args.get("document")
    path = args.get("path")
    return """
d = _doc(%s)
p = %s
if p:
    d.saveAs(p)
else:
    if not d.FileName:
        raise ValueError("document has never been saved; supply 'path'")
    d.save()
return {"name": d.Name, "fileName": d.FileName, "saved": bool(d.isSaved())}
""" % (_j(doc), _j(path))


def code_close_document(args):
    doc = args.get("document")
    save = bool(args.get("save", False))
    return """
d = _doc(%s)
name = d.Name
if %s:
    d.save()
_App.closeDocument(name)
return {"closed": name, "remaining": list(_App.listDocuments().keys())}
""" % (_j(doc), "True" if save else "False")


def code_list_object_types(args):
    flt = args.get("filter")
    wb = args.get("workbench")
    return """
if %s and _Gui:
    try:
        _Gui.activateWorkbench(%s)
    except Exception:
        pass
d = _App.ActiveDocument
_tmp = None
if d is None:
    _tmp = _App.newDocument("__MCP_type_probe__")
    d = _tmp
types = list(d.supportedTypes())
if _tmp is not None:
    _App.closeDocument(_tmp.Name)
flt = %s
if flt:
    types = [t for t in types if flt.lower() in t.lower()]
return {"count": len(types), "types": sorted(types)}
""" % ("True" if wb else "False", _j(wb), _j(flt))


def code_create_object(args):
    doc = args.get("document")
    typ = args.get("type")
    name = args.get("name")
    label = args.get("label")
    props = args.get("properties") or {}
    return """
d = _doc(%s)
_group = %s
with _Tx(d, "MCP create_object"):
    o = d.addObject(%s%s)
    if %s:
        o.Label = %s
    for k, v in %s.items():
        _setprop(o, k, v)
    if _group:
        _obj(d, _group).addObject(o)
    d.recompute()
return {"name": o.Name, "label": o.Label, "typeId": o.TypeId,
        "properties": {p: _val(o.getPropertyByName(p)) for p in o.PropertiesList}}
""" % (_j(doc), _j(args.get("group")), _j(typ), (", " + _j(name)) if name else "",
       "True" if label else "False", _j(label), _j(props))


def code_delete_object(args):
    doc = args.get("document")
    obj = args.get("object")
    return """
d = _doc(%s)
o = _obj(d, %s)
nm = o.Name
with _Tx(d, "MCP delete_object"):
    d.removeObject(nm)
    d.recompute()
return {"removed": nm, "objects": [x.Name for x in d.Objects]}
""" % (_j(doc), _j(obj))


def code_get_object(args):
    doc = args.get("document")
    obj = args.get("object")
    inc_shape = args.get("include_shape", True)
    only = args.get("properties")
    return """
d = _doc(%s)
o = _obj(d, %s)
_only = %s
names = o.PropertiesList if not _only else [p for p in o.PropertiesList if p in _only]
props = {}
for p in names:
    try:
        props[p] = _val(o.getPropertyByName(p))
    except Exception as e:
        props[p] = "ERR " + repr(e)
types = {}
for p in names:
    try:
        types[p] = o.getTypeIdOfProperty(p)
    except Exception:
        pass
out = {"name": o.Name, "label": o.Label, "typeId": o.TypeId, "document": d.Name,
       "properties": props, "propertyTypes": types,
       "expressions": dict(o.ExpressionEngine) if hasattr(o, "ExpressionEngine") else {},
       "inList": [x.Name for x in o.InList], "outList": [x.Name for x in o.OutList]}
if _only:
    out["unknownProperties"] = [p for p in _only if p not in o.PropertiesList]
_vo = getattr(o, "ViewObject", None)
if _vo is not None:
    vprops = {}
    for p in _vo.PropertiesList:
        try:
            vprops[p] = _val(_vo.getPropertyByName(p))
        except Exception:
            pass
    out["viewProperties"] = vprops
if %s:
    try:
        out["shape"] = _shape_summary(o.Shape)
    except Exception:
        pass
return out
""" % (_j(doc), _j(obj), _j(only), "True" if inc_shape else "False")


def code_set_properties(args):
    doc = args.get("document")
    obj = args.get("object")
    props = args.get("properties") or {}
    rec = args.get("recompute", True)
    return """
d = _doc(%s)
o = _obj(d, %s)
changed = {}
with _Tx(d, "MCP set_properties"):
    _items = list(%s.items())
    _items.sort(key=lambda kv: 0 if kv[0] in ("Support", "AttachmentSupport") else (1 if kv[0] in ("MapMode", "MapReversed", "MapPathParameter", "AttachmentOffset") else 2))
    for k, v in _items:
        _setprop(o, k, v)
        changed[k] = _val(_getprop(o, k))
    if %s:
        d.recompute()
return {"object": o.Name, "changed": changed}
""" % (_j(doc), _j(obj), _j(props), "True" if rec else "False")


def code_set_expression(args):
    doc = args.get("document")
    obj = args.get("object")
    prop = args.get("property")
    expr = args.get("expression")
    return """
d = _doc(%s)
o = _obj(d, %s)
_expr = %s
if _expr is None:
    o.setExpression(%s, None)
else:
    o.setExpression(%s, _expr)
d.recompute()
return {"object": o.Name, "expressions": dict(o.ExpressionEngine),
        "value": _val(o.getPropertyByName(%s))}
""" % (_j(doc), _j(obj), _j(expr), _j(prop), _j(prop), _j(prop))


def code_recompute(args):
    doc = args.get("document")
    return """
d = _doc(%s)
d.recompute()
return {"document": d.Name, "objects": [x.Name for x in d.Objects]}
""" % _j(doc)


def code_get_selection(args):
    return """
sel = []
if _Gui:
    for s in _Gui.Selection.getSelectionEx():
        sel.append({"document": s.DocumentName, "object": s.ObjectName,
                    "subObjects": list(s.SubElementNames)})
return {"selection": sel}
"""


def code_set_selection(args):
    doc = args.get("document")
    objs = args.get("objects") or []
    clear = args.get("clear", True)
    return """
d = _doc(%s)
if _Gui is None:
    raise RuntimeError("no GUI available")
if %s:
    _Gui.Selection.clearSelection()
for nm in %s:
    if "." in nm:
        onm, sub = nm.split(".", 1)
        o = _obj(d, onm)
        _Gui.Selection.addSelection(d.Name, o.Name, sub, 0, 0, 0, False)
    else:
        o = _obj(d, nm)
        _Gui.Selection.addSelection(d.Name, o.Name, "", 0, 0, 0, False)
return {"selection": [{"object": s.ObjectName, "subObjects": list(s.SubElementNames)}
                      for s in _Gui.Selection.getSelectionEx()]}
""" % (_j(doc), "True" if clear else "False", _j(objs))


def code_run_command(args):
    cmd = args.get("command")
    idx = int(args.get("index", 0))
    return """
if _Gui is None:
    raise RuntimeError("no GUI available")
_Gui.runCommand(%s, %d)
return {"command": %s, "ok": True}
""" % (_j(cmd), idx, _j(cmd))


def code_list_commands(args):
    flt = args.get("filter")
    return """
cmds = _Gui.listCommands() if _Gui else []
flt = %s
if flt:
    cmds = [c for c in cmds if flt.lower() in c.lower()]
return {"count": len(cmds), "commands": sorted(cmds)}
""" % _j(flt)


def code_list_workbenches(args):
    return """
wbs = list(_Gui.listWorkbenches().keys()) if _Gui else []
cur = None
try:
    cur = _Gui.activeWorkbench().name()
except Exception:
    pass
return {"workbenches": wbs, "active": cur}
"""


def code_activate_workbench(args):
    name = args.get("name")
    return """
if _Gui is None:
    raise RuntimeError("no GUI available")
_Gui.activateWorkbench(%s)
return {"active": _Gui.activeWorkbench().name()}
""" % _j(name)


def code_get_parameters(args):
    path = args.get("path")
    rec = args.get("recursive", False)
    return """
def _dump(p, prefix, out):
    try:
        cont = p.GetContents()
    except Exception:
        cont = None
    if cont:
        for item in cont:
            try:
                typ, nm, val = item
                out[prefix + nm] = {"type": typ, "value": _val(val)}
            except Exception:
                out[prefix + str(item)] = str(item)
    for g in list(p.GetGroups()):
        _dump(p.GetGroup(g), prefix + g + "/", out)
p = _App.ParamGet(%s)
entries = {}
_dump(p, "", entries)
groups = list(p.GetGroups())
if not %s:
    entries = {k: v for k, v in entries.items() if "/" not in k}
return {"path": %s, "groups": groups, "entries": entries}
""" % (_j(path), "True" if rec else "False", _j(path))


def code_delete_parameter(args):
    path = args.get("path")
    name = args.get("name")
    typ = args.get("type")
    return """
p = _App.ParamGet(%s)
nm = %s
t = %s
if t is None:
    t = "String"
if t == "Bool": p.RemBool(nm)
elif t == "Int": p.RemInt(nm)
elif t == "Float": p.RemFloat(nm)
elif t == "Unsigned": p.RemUnsigned(nm)
elif t == "String": p.RemString(nm)
else: raise ValueError("unknown type %%r" %% t)
return {"path": %s, "removed": nm, "type": t}
""" % (_j(path), _j(name), _j(typ), _j(path))


def code_set_active_document(args):
    doc = args.get("document")
    return """
d = _doc(%s)
_App.setActiveDocument(d.Name)
if _Gui is not None:
    try:
        _Gui.setActiveDocument(d.Name)
    except Exception:
        pass
return {"active": _App.ActiveDocument.Name}
""" % _j(doc)


def code_console_log(args):
    lines = int(args.get("lines", 100) or 100)
    return """
if _Gui is None:
    raise RuntimeError("no GUI available")
from PySide2 import QtWidgets as _QW
mw = _Gui.getMainWindow()
text = ""
dock = None
for _d in mw.findChildren(_QW.QDockWidget):
    if _d.objectName() == "Report view":
        dock = _d
        break
if dock is not None:
    tes = dock.findChildren(_QW.QTextEdit)
    if tes:
        text = tes[0].toPlainText()
    else:
        pes = dock.findChildren(_QW.QPlainTextEdit)
        if pes:
            text = pes[0].toPlainText()
all_lines = text.splitlines()
n = %d
return {"lines": all_lines[-n:], "text": chr(10).join(all_lines[-n:])}
""" % lines


def code_convert_units(args):
    value = args.get("value")
    unit = args.get("unit")
    return """
q = _App.Units.Quantity(%s)
out = {"text": str(q), "value": q.Value, "unit": str(q.Unit),
       "userPreferred": list(q.getUserPreferred())}
u = %s
if u:
    _cv = q.getValueAs(u)
    out["convertedValue"] = _cv
    out["convertedText"] = ("%%g %%s" %% (_cv, u))
return out
""" % (_j(value), _j(unit))


def code_set_parameter(args):
    path = args.get("path")
    name = args.get("name")
    value = args.get("value")
    typ = args.get("type")
    return """
p = _App.ParamGet(%s)
nm = %s
v = %s
t = %s
if t is None:
    t = {bool: "Bool", int: "Int", float: "Float", str: "String"}.get(type(v))
    if t is None:
        raise ValueError("cannot infer parameter type; pass 'type' in Bool/Int/Float/String")
if t == "Bool": p.SetBool(nm, bool(v))
elif t == "Int": p.SetInt(nm, int(v))
elif t == "Float": p.SetFloat(nm, float(v))
elif t == "Unsigned": p.SetUnsigned(nm, int(v))
elif t == "String": p.SetString(nm, str(v))
else: raise ValueError("unknown type %%r" %% t)
return {"path": %s, "name": nm, "type": t, "value": _val(v)}
""" % (_j(path), _j(name), _j(value), _j(typ), _j(path))


def code_spreadsheet_get(args):
    doc = args.get("document")
    obj = args.get("object")
    rng = args.get("range")
    return """
import re as _re
d = _doc(%s)
o = _obj(d, %s)
if not hasattr(o, "getContents"):
    raise ValueError("%%s is not a spreadsheet" %% o.Name)
addrs = []
rng = %s
if rng:
    m = _re.match(r"^([A-Za-z]+)([0-9]+):([A-Za-z]+)([0-9]+)$", rng.strip())
    if not m:
        raise ValueError("range must look like 'A1:C10'")
    c1, r1, c2, r2 = m.group(1).upper(), int(m.group(2)), m.group(3).upper(), int(m.group(4))
    def _cn(s):
        n = 0
        for ch in s:
            n = n * 26 + (ord(ch) - 64)
        return n
    for cc in range(_cn(c1), _cn(c2) + 1):
        nm = ""
        x = cc
        while x > 0:
            x, rem = divmod(x - 1, 26)
            nm = chr(65 + rem) + nm
        for rr in range(r1, r2 + 1):
            addrs.append(nm + str(rr))
else:
    content = o.cells.Content if hasattr(o, "cells") else ""
    addrs = _re.findall(r'address="([^"]+)"', content)
out = {}
for a in addrs:
    try:
        content = o.getContents(a)
    except Exception:
        content = ""
    try:
        value = _val(o.get(a)) if content != "" else None
    except Exception:
        value = None
    if content != "" or rng:
        out[a] = {"value": value, "content": content}
return {"object": o.Name, "cells": out}
""" % (_j(doc), _j(obj), _j(rng))


def code_spreadsheet_set(args):
    doc = args.get("document")
    obj = args.get("object")
    cells = args.get("cells") or {}
    return """
d = _doc(%s)
o = _obj(d, %s)
if not hasattr(o, "set"):
    raise ValueError("%%s is not a spreadsheet" %% o.Name)
for addr, v in %s.items():
    if v is None:
        o.clear(addr)
    else:
        o.set(addr, str(v))
d.recompute()
out = {}
for addr in %s.keys():
    try:
        content = o.getContents(addr)
    except Exception:
        content = ""
    try:
        value = _val(o.get(addr)) if content != "" else None
    except Exception:
        value = None
    out[addr] = {"value": value, "content": content}
return {"object": o.Name, "cells": out}
""" % (_j(doc), _j(obj), _j(cells), _j(cells))


def code_export_objects(args):
    doc = args.get("document")
    objs = args.get("objects")
    path = args.get("path")
    return """
d = _doc(%s)
objs = %s
path = %s
sel = [ _obj(d, n) for n in objs ] if objs else list(d.Objects)
import os as _o
ext = _o.path.splitext(path)[1].lower()
if ext in ('.fcstd', '.fcstd1'):
    d.saveAs(path)
elif ext in ('.stl', '.obj', '.ply', '.amf', '.3mf', '.ast', '.off'):
    import Mesh as _Mesh
    _Mesh.export(sel, path)
elif ext in ('.step', '.stp', '.iges', '.igs', '.brep', '.brp'):
    import Part as _Part
    _Part.export(sel, path)
else:
    try:
        if _Gui is not None:
            _Gui.export(sel, path)
        else:
            raise RuntimeError("no GUI")
    except Exception:
        pass
    if not _o.path.exists(path):
        import Import as _Imp
        _Imp.export(sel, path)
return {"path": path, "exists": _o.path.exists(path),
        "size": (_o.path.getsize(path) if _o.path.exists(path) else None),
        "objects": [o.Name for o in sel]}
""" % (_j(doc), _j(objs), _j(path))


def code_view(args):
    action = args.get("action")
    return """
if _Gui is None:
    raise RuntimeError("no GUI available")
av = _Gui.activeView()
act = %s
if act == "fit_all":
    av.fitAll()
    return {"action": "fit_all"}
elif act == "get_camera":
    pos = None
    try:
        node = av.getCameraNode()
        v = node.position.getValue()
        pos = [v[0], v[1], v[2]]
    except Exception:
        pass
    return {"position": pos, "orientation": _val(av.getCameraOrientation()),
            "viewDirection": list(av.getViewDirection()), "type": av.getCameraType(),
            "cameraString": av.getCamera() if isinstance(av.getCamera(), str) else None}
elif act == "set_camera":
    from FreeCAD import Base as _B
    pos = %s
    orient = %s
    if not hasattr(av, "getCameraNode"):
        raise RuntimeError("the active view has no 3D camera; activate a 3D view first")
    cam = av.getCameraNode()
    if pos:
        cam.position.setValue(float(pos[0]), float(pos[1]), float(pos[2]))
    if orient:
        r = _B.Rotation(_B.Vector(*[float(x) for x in orient["axis"]]), float(orient.get("angle_deg", 0)))
        cam.orientation.setValue(r.Q[0], r.Q[1], r.Q[2], r.Q[3])
    av.redraw()
    return {"action": "set_camera"}
elif act == "redraw":
    av.redraw()
    return {"action": "redraw"}
else:
    raise ValueError("unknown action %%r" %% act)
""" % (_j(action), _j(args.get("position")), _j(args.get("orientation")))


def code_undo(args):
    doc = args.get("document")
    return """
d = _doc(%s)
if not d.UndoCount:
    return {"document": d.Name, "undoCount": 0, "note": "nothing to undo"}
d.undo()
return {"document": d.Name, "undoCount": d.UndoCount, "objects": [x.Name for x in d.Objects]}
""" % _j(doc)


def code_redo(args):
    doc = args.get("document")
    return """
d = _doc(%s)
if not d.RedoCount:
    return {"document": d.Name, "redoCount": 0, "note": "nothing to redo"}
d.redo()
return {"document": d.Name, "redoCount": d.RedoCount, "objects": [x.Name for x in d.Objects]}
""" % _j(doc)


def code_import_file(args):
    path = args.get("path")
    doc = args.get("document")
    return """
import os as _o
p = %s
if not _o.path.exists(p):
    raise ValueError("file does not exist: %%s" %% p)
d = _doc(%s)
before = set(x.Name for x in d.Objects)
ext = _o.path.splitext(p)[1].lower()
try:
    if ext in (".fcstd", ".fcstd1"):
        d.mergeProject(p)
    elif ext in (".stl", ".obj", ".ply", ".ast", ".off", ".amf", ".3mf"):
        import Mesh as _Mesh
        _Mesh.insert(p, d.Name)
    else:
        import Import as _Imp
        _Imp.insert(p, d.Name)
except Exception:
    if _Gui is not None:
        _Gui.insert(p, d.Name)
    else:
        raise
d.recompute()
after = [x.Name for x in d.Objects if x.Name not in before]
return {"document": d.Name, "imported": after, "objects": [x.Name for x in d.Objects]}
""" % (_j(path), _j(doc))


def code_units(args):
    value = args.get("value")
    unit = args.get("unit")
    return """
q = _App.Units.Quantity(%s)
out = {"text": str(q), "value": q.Value, "unit": str(q.Unit)}
if %s:
    out["converted"] = _App.Units.Quantity(%s).getUserPreferred()
return out
""" % (_j(value), _j(unit), _j(value))


def code_sketch_geometry(args):
    tpl = """
import Part as _Part
d = _doc(@@DOC@@)
o = _obj(d, @@OBJ@@)
if not hasattr(o, "addGeometry"):
    raise ValueError("%s is not a sketch" % o.Name)
with _Tx(d, "MCP sketch_geometry"):
  for g in @@GEO@@:
        t = str(g.get("type", "line")).lower()
        def _v(p):
            return _App.Vector(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
        if t in ("line", "linesegment"):
            geo = _Part.LineSegment(_v(g["start"]), _v(g["end"]))
        elif t == "circle":
            geo = _Part.Circle(_v(g["center"]), _App.Vector(0, 0, 1), float(g["radius"]))
        elif t == "arc":
            a0 = _math.radians(float(g.get("start_angle", 0.0)))
            a1 = _math.radians(float(g.get("end_angle", 90.0)))
            geo = _Part.ArcOfCircle(_Part.Circle(_v(g["center"]), _App.Vector(0, 0, 1), float(g["radius"])), a0, a1)
        elif t == "point":
            geo = _Part.Point(_v(g["at"]))
        else:
            raise ValueError("unknown geometry type %r (use line/circle/arc/point)" % t)
        o.addGeometry(geo, False)
  d.recompute()
return {"object": o.Name, "geometryCount": o.GeometryCount,
        "geometry": [_val(g) for g in o.Geometry]}
"""
    return (tpl.replace("@@DOC@@", _j(args.get("document")))
               .replace("@@OBJ@@", _j(args.get("object")))
               .replace("@@GEO@@", _j(args.get("geometry") or [])))


def code_sketch_constraint(args):
    tpl = """
import Sketcher as _Sketcher
d = _doc(@@DOC@@)
o = _obj(d, @@OBJ@@)
if not hasattr(o, "addConstraint"):
    raise ValueError("%s is not a sketch" % o.Name)
with _Tx(d, "MCP sketch_constraint"):
  for c in @@CONS@@:
        typ = c["type"]
        vals = list(c.get("args", []))
        if "value" in c and not vals:
            vals = [c.get("first", 0), float(c["value"])]
        o.addConstraint(_Sketcher.Constraint(typ, *vals))
  d.recompute()
return {"object": o.Name, "constraintCount": o.ConstraintCount,
        "constraints": [_val(c) for c in o.Constraints],
        "fullyConstrained": bool(o.FullyConstrained)}
"""
    return (tpl.replace("@@DOC@@", _j(args.get("document")))
               .replace("@@OBJ@@", _j(args.get("object")))
               .replace("@@CONS@@", _j(args.get("constraints") or [])))


def code_get_sketch(args):
    tpl = """
d = _doc(@@DOC@@)
o = _obj(d, @@OBJ@@)
if not hasattr(o, "GeometryCount"):
    raise ValueError("%s is not a sketch" % o.Name)
return {"object": o.Name, "geometryCount": o.GeometryCount,
        "geometry": [_val(g) for g in o.Geometry],
        "constraintCount": o.ConstraintCount,
        "constraints": [_val(c) for c in o.Constraints],
        "fullyConstrained": bool(o.FullyConstrained)}
"""
    return (tpl.replace("@@DOC@@", _j(args.get("document")))
               .replace("@@OBJ@@", _j(args.get("object"))))


TOOLS = [
    {"name": "app_status",
     "description": "Check the connection to the running FreeCAD: process id, whether it is reachable, the execution mechanism used, and the open documents. Use this first.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "list_documents",
     "description": "List every document currently open in the running FreeCAD. Returns for each its internal Name (used by other tools), user Label, saved file path ('' if never saved), object count, saved flag and whether it is active.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "get_document",
     "description": "Get one document's metadata and, by default, its objects (name, label, typeId, visibility, dependency lists). Omit 'document' to use the active document.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string", "description": "Document Name or Label. Omit for the active document."},
         "include_objects": {"type": "boolean", "default": True}}, "additionalProperties": False}},
    {"name": "new_document",
     "description": "Create and activate a new empty document. Returns its Name/Label.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "label": {"type": "string"}}, "additionalProperties": False}},
    {"name": "open_document",
     "description": "Open a file into FreeCAD (a .FCStd document, or any format an installed importer handles, e.g. .step/.iges/.stl/.dxf). 'path' is an absolute filesystem path readable by the application. Returns the new document's Name and object list.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                     "required": ["path"], "additionalProperties": False}},
    {"name": "save_document",
     "description": "Save a document. With 'path' this is Save-As (writes .FCStd); without 'path' it saves to the current file (error if never saved). Returns the resulting file name.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "path": {"type": "string"}}, "additionalProperties": False}},
    {"name": "close_document",
     "description": "Close a document, optionally saving it first. Returns the documents still open.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "save": {"type": "boolean", "default": False}},
         "additionalProperties": False}},
    {"name": "list_object_types",
     "description": "List object type identifiers creatable with create_object (e.g. 'Part::Box', 'Part::Cylinder', 'App::Part', 'Spreadsheet::Sheet', 'PartDesign::Body'). Types come from loaded workbenches; pass 'workbench' to activate one first (loads its types). 'filter' is a case-insensitive substring.",
     "inputSchema": {"type": "object", "properties": {
         "filter": {"type": "string"},
         "workbench": {"type": "string", "description": "e.g. 'PartWorkbench', 'PartDesignWorkbench', 'SpreadsheetWorkbench'."}},
         "additionalProperties": False}},
    {"name": "create_object",
     "description": "Add a new object of the given type to a document, optionally set initial properties and a label, then recompute. Property values are application data: numbers, booleans, strings, lists (vectors as [x,y,z]), or objects for placements {position:[x,y,z], rotation:{axis:[x,y,z], angle_deg:a}}. Lengths are millimetres, angles degrees. Returns the new object's name and full property list.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"},
         "type": {"type": "string", "description": "e.g. 'Part::Box'."},
         "name": {"type": "string"}, "label": {"type": "string"},
         "properties": {"type": "object", "description": "Map of property name -> value."},
         "group": {"type": "string", "description": "Optional container (App::Part, PartDesign::Body, group) to add the new object into."}},
         "required": ["type"], "additionalProperties": False}},
    {"name": "delete_object",
     "description": "Remove an object from a document and recompute. Returns the remaining object names.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"},
         "object": {"type": "string", "description": "Object Name or Label."}},
         "required": ["object"], "additionalProperties": False}},
    {"name": "get_object",
     "description": "Read one object in full: data properties (values + declared types), the object's ViewObject appearance properties (ShapeColor, LineColor, Transparency, DisplayMode, Visibility, ...), expression bindings, dependency lists, and (for geometric objects) a shape summary with volume (mm^3), area (mm^2), length (mm), centre of mass, bounding box and element counts. Optional 'properties' restricts the data properties returned and reports any unknown names.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"},
         "include_shape": {"type": "boolean", "default": True},
         "properties": {"type": "array", "items": {"type": "string"},
                        "description": "Optional list of property names to return; omit for all."}},
         "required": ["object"], "additionalProperties": False}},
    {"name": "set_properties",
     "description": "Set one or more properties on an object and recompute. Values use the same formats as create_object. A name may be a data property (Length, Placement, Label, Base/Tool/Profile links as object names) or a ViewObject appearance property (ShapeColor/LineColor as [r,g,b,a] 0-1, Transparency 0-100, DisplayMode, Visibility); the data property is tried first. Colours may also be given as a hex string '#rrggbb'. For a sketch inside a PartDesign Body, set Support to e.g. ['Body.XY_Plane'] (also XZ_Plane/YZ_Plane) to attach it; attachment support is applied before map mode. Returns the values actually stored.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"},
         "properties": {"type": "object"}, "recompute": {"type": "boolean", "default": True}},
         "required": ["object", "properties"], "additionalProperties": False}},
    {"name": "set_expression",
     "description": "Bind a property to an expression (FreeCAD's formula language), e.g. property 'Length' of 'Box' = 'Spreadsheet.A1 * 2' or '2 mm'. Pass expression=null to clear the binding. Recomputes and returns all expressions and the resulting value.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"},
         "property": {"type": "string"}, "expression": {"type": ["string", "null"]}},
         "required": ["object", "property"], "additionalProperties": False}},
    {"name": "recompute",
     "description": "Recompute a document (re-evaluate all parametric objects and expressions). Returns the object list.",
     "inputSchema": {"type": "object", "properties": {"document": {"type": "string"}}, "additionalProperties": False}},
    {"name": "get_selection",
     "description": "Return the current GUI selection: document, object name and selected sub-elements (faces/edges) for each selected object.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "set_selection",
     "description": "Replace the GUI selection with the given objects in a document. Each entry is an object Name/Label, or 'Object.Sub' to select a sub-element such as 'Box.Face1' or 'Pad.Edge3'. Returns the resulting selection.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "objects": {"type": "array", "items": {"type": "string"}},
         "clear": {"type": "boolean", "default": True}},
         "required": ["objects"], "additionalProperties": False}},
    {"name": "run_command",
     "description": "Run a FreeCAD GUI command by name (as listed by list_commands, e.g. 'Std_New', 'Std_ViewFitAll', 'Part_Box', 'Std_DlgPreferences'). Same registry the menus/toolbars use. WARNING: commands that open a modal dialog block until dismissed; prefer the object/property tools for automated work.",
     "inputSchema": {"type": "object", "properties": {
         "command": {"type": "string"}, "index": {"type": "integer", "default": 0}},
         "required": ["command"], "additionalProperties": False}},
    {"name": "list_commands",
     "description": "List names of all registered GUI commands (menu/toolbar actions). Optional case-insensitive 'filter' substring.",
     "inputSchema": {"type": "object", "properties": {"filter": {"type": "string"}}, "additionalProperties": False}},
    {"name": "list_workbenches",
     "description": "List installed workbenches and the currently active one.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "activate_workbench",
     "description": "Switch to a workbench by name (e.g. 'PartWorkbench'). Also loads its object types and commands.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                     "required": ["name"], "additionalProperties": False}},
    {"name": "get_parameters",
     "description": "Read FreeCAD settings (the parameter/Preferences tree) for a group path such as 'User parameter:BaseApp/Preferences/General'. Returns sub-group names and the typed entries (name -> {type,value}); with recursive=true, sub-groups are included with '/' separated keys.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "recursive": {"type": "boolean", "default": False}},
         "required": ["path"], "additionalProperties": False}},
    {"name": "set_parameter",
     "description": "Write a FreeCAD setting in a parameter group. 'value' is a bool/int/float/string; 'type' (Bool/Int/Float/Unsigned/String) may be given, else inferred. Settings persist across restarts.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "name": {"type": "string"}, "value": {},
         "type": {"type": "string", "enum": ["Bool", "Int", "Float", "Unsigned", "String"]}},
         "required": ["path", "name", "value"], "additionalProperties": False}},
    {"name": "get_spreadsheet",
     "description": "Read cells of a spreadsheet object. By default returns every populated cell; pass 'range' (e.g. 'A1:C10') to read an explicit block. For each address returns the evaluated 'value' and the raw 'content' (formulas start with '=').",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"},
         "range": {"type": "string", "description": "Optional block such as 'A1:C10'."}},
         "required": ["object"], "additionalProperties": False}},
    {"name": "set_spreadsheet",
     "description": "Write cells of a spreadsheet object. 'cells' maps an address (e.g. 'A1') to content: a number, text, or a formula starting with '=' (e.g. '=A1*2'). Pass null to clear a cell. Recomputes and returns the resulting values/contents.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"}, "cells": {"type": "object"}},
         "required": ["object", "cells"], "additionalProperties": False}},
    {"name": "export_objects",
     "description": "Export objects to a file using FreeCAD's exporters (format inferred from extension, e.g. .stl, .step, .iges, .obj, .brep). 'objects' empty/omitted exports the whole document. Returns the written file's existence and size.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "objects": {"type": "array", "items": {"type": "string"}},
         "path": {"type": "string"}},
         "required": ["path"], "additionalProperties": False}},
    {"name": "view",
     "description": "Inspect or change the 3D view. action='fit_all' zooms to fit; 'get_camera' returns camera position/orientation/view direction; 'set_camera' sets position [x,y,z] and orientation {axis:[x,y,z],angle_deg:a}; 'redraw' repaints.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["fit_all", "get_camera", "set_camera", "redraw"]},
         "position": {"type": "array", "items": {"type": "number"}}, "orientation": {"type": "object"}},
         "required": ["action"], "additionalProperties": False}},
    {"name": "undo",
     "description": "Undo the last change in a document (FreeCAD's undo stack). Returns the remaining undo count.",
     "inputSchema": {"type": "object", "properties": {"document": {"type": "string"}}, "additionalProperties": False}},
    {"name": "redo",
     "description": "Redo the last undone change in a document. Returns the remaining redo count.",
     "inputSchema": {"type": "object", "properties": {"document": {"type": "string"}}, "additionalProperties": False}},
    {"name": "import_file",
     "description": "Import a file into an EXISTING document (as opposed to open_document which makes a new document). Use for merging a .FCStd or importing geometry (.step/.iges/.stl/...) into the current model. Returns the names of the newly created objects.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "document": {"type": "string"}},
         "required": ["path"], "additionalProperties": False}},
    {"name": "delete_parameter",
     "description": "Delete a setting from a parameter group. Give 'type' (Bool/Int/Float/Unsigned/String) to match how it was stored; defaults to String.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "name": {"type": "string"},
         "type": {"type": "string", "enum": ["Bool", "Int", "Float", "Unsigned", "String"]}},
         "required": ["path", "name"], "additionalProperties": False}},
    {"name": "set_active_document",
     "description": "Make a document the active one (affects which document tools default to). Returns the active document name.",
     "inputSchema": {"type": "object", "properties": {"document": {"type": "string"}},
                     "required": ["document"], "additionalProperties": False}},
    {"name": "get_console_log",
     "description": "Return recent lines from FreeCAD's Report view (the application's own log/error output). Useful to see why an operation failed.",
     "inputSchema": {"type": "object", "properties": {
         "lines": {"type": "integer", "default": 100}},
         "additionalProperties": False}},
    {"name": "convert_units",
     "description": "Parse/convert a quantity using FreeCAD's units system. 'value' is a value with a unit as text, e.g. '10 mm', '1 in', '25 deg', '5 kg'. Returns the numeric value in its unit and the user-preferred text; if 'unit' is given (e.g. 'in'), also returns the value and text in that unit.",
     "inputSchema": {"type": "object", "properties": {
         "value": {"type": "string"}, "unit": {"type": "string"}},
         "required": ["value"], "additionalProperties": False}},
    {"name": "sketch_geometry",
     "description": "Append 2D geometry to a Sketcher sketch (create one first with create_object type 'Sketcher::SketchObject'). Each item in 'geometry' is {type, ...}: {type:'line', start:[x,y], end:[x,y]}; {type:'circle', center:[x,y], radius:r}; {type:'arc', center:[x,y], radius:r, start_angle:deg, end_angle:deg}; {type:'point', at:[x,y]}. Coordinates are sketch-local mm. Recomputes and returns the sketch geometry.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"},
         "geometry": {"type": "array", "items": {"type": "object"}}},
         "required": ["object", "geometry"], "additionalProperties": False}},
    {"name": "sketch_constraint",
     "description": "Add constraints to a Sketcher sketch. Each item is {type, args:[...]} passed to FreeCAD's constraint constructor, e.g. {type:'Coincident', args:[0,1,1,0]} (edge0 vertex1 to edge1 vertex0), {type:'DistanceX', args:[0, 10.0]}, {type:'Horizontal', args:[0]}, {type:'Equal', args:[0,1]}, {type:'Radius', args:[0, 5.0]}. Recomputes and returns constraints and whether the sketch is fully constrained.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"},
         "constraints": {"type": "array", "items": {"type": "object"}}},
         "required": ["object", "constraints"], "additionalProperties": False}},
    {"name": "get_sketch",
     "description": "Read a Sketcher sketch: its geometry (with coordinates), constraints, and whether it is fully constrained.",
     "inputSchema": {"type": "object", "properties": {
         "document": {"type": "string"}, "object": {"type": "string"}},
         "required": ["object"], "additionalProperties": False}},
]

HANDLERS = {
    "list_documents": code_list_documents,
    "get_document": code_get_document,
    "new_document": code_new_document,
    "open_document": code_open_document,
    "save_document": code_save_document,
    "close_document": code_close_document,
    "list_object_types": code_list_object_types,
    "create_object": code_create_object,
    "delete_object": code_delete_object,
    "get_object": code_get_object,
    "set_properties": code_set_properties,
    "set_expression": code_set_expression,
    "recompute": code_recompute,
    "get_selection": code_get_selection,
    "set_selection": code_set_selection,
    "run_command": code_run_command,
    "list_commands": code_list_commands,
    "list_workbenches": code_list_workbenches,
    "activate_workbench": code_activate_workbench,
    "get_parameters": code_get_parameters,
    "set_parameter": code_set_parameter,
    "get_spreadsheet": code_spreadsheet_get,
    "set_spreadsheet": code_spreadsheet_set,
    "export_objects": code_export_objects,
    "view": code_view,
    "undo": code_undo,
    "redo": code_redo,
    "import_file": code_import_file,
    "delete_parameter": code_delete_parameter,
    "set_active_document": code_set_active_document,
    "get_console_log": code_console_log,
    "convert_units": code_convert_units,
    "sketch_geometry": code_sketch_geometry,
    "sketch_constraint": code_sketch_constraint,
    "get_sketch": code_get_sketch,
}


def _import_frida():
    """Import frida, falling back to the copy bundled in deps/."""
    try:
        import frida
        return frida
    except Exception:
        pass
    deps = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deps")
    if os.path.isdir(deps) and deps not in sys.path:
        sys.path.append(deps)
    import frida
    return frida


def _have_frida():
    try:
        return _import_frida().__version__
    except Exception as e:
        return "no (%r)" % e


def call_tool(name, args):
    if name == "app_status":
        pid = BRIDGE.find_pid()
        info = {"pid": pid, "reachable": pid is not None,
                "gdb": shutil.which("gdb"), "frida": _have_frida()}
        if pid:
            try:
                r = BRIDGE.call(code_list_documents({}))
                info["documents"] = r.get("documents")
                info["method"] = BRIDGE._method
            except Exception as e:
                info["error"] = str(e)
            info["last_errors"] = list(BRIDGE.last_errors)
            try:
                r = subprocess.run(["gdb", "-p", str(pid), "-batch", "-nx",
                                    "-ex", "info threads", "-ex", "detach", "-ex", "quit"],
                                   capture_output=True, text=True, timeout=30)
                blob = (r.stdout or "") + (r.stderr or "")
                info["gdb_attach_ok"] = ("Operation not permitted" not in blob
                                         and "not being run" not in blob)
                if not info["gdb_attach_ok"]:
                    info["gdb_attach_note"] = blob[-200:]
            except Exception as e:
                info["gdb_attach_ok"] = False
                info["gdb_attach_note"] = repr(e)
        return info
    fn = HANDLERS.get(name)
    if fn is None:
        raise ValueError("unknown tool: %s" % name)
    return BRIDGE.call(fn(args or {}))


# --------------------------------------------------------------------------
# JSON-RPC main loop
# --------------------------------------------------------------------------
def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Controls the running FreeCAD application through its embedded Python "
                "scripting interface, executed on FreeCAD's main (GUI) thread. Tools inspect "
                "and modify open documents, their objects and properties, spreadsheets, the "
                "GUI selection, FreeCAD commands and settings, and open/save/export files. "
                "Lengths are millimetres, angles degrees. An object's 'Name' is its stable "
                "internal identifier, 'Label' the user-visible name; most tools accept either. "
                "Omit 'document' to act on the active document. Changes affect the live "
                "application and persist only after save_document. Requires FreeCAD running. "
                "Expressions and spreadsheet formulas are accepted as data; arbitrary "
                "scripts/macros are deliberately not exposed."
            ),
        }}
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method in ("notifications/cancelled", "notifications/progress"):
        return None
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params", {})
        nm = p.get("name")
        try:
            res = call_tool(nm, p.get("arguments") or {})
            if isinstance(res, dict) and set(res.keys()) == {"error"}:
                raise RuntimeError(res["error"])
            text = json.dumps(res, indent=2, default=str)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": False}}
        except Exception:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "ERROR: " + traceback.format_exc()}],
                "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "method not found: %s" % method}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            log("bad json line")
            continue
        try:
            resp = handle(msg)
        except Exception:
            log("handler error", traceback.format_exc())
            resp = None
        if resp is not None:
            send(resp)


if __name__ == "__main__":
    main()

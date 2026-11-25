"""FreeCAD in-process operations dispatcher (injected into the running FreeCAD).

This module is exec'd inside the running FreeCAD interpreter.  It is *not*
imported by the MCP server; it runs inside the application process, on the
application's Qt main thread, and it drives the application through its own
Python API (the same layer the built-in Python console and macros use).

Entry point:  __fc_mcp_call(req_path, out_path)
"""
import json
import os
import threading
import traceback

VERSION = "1.0"



# --------------------------------------------------------------------------
# console capture
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# value conversion
# --------------------------------------------------------------------------
def _sanitize(obj, depth=0):
    """Replace non-finite floats so the result is always valid JSON."""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return str(obj)
        return obj
    if isinstance(obj, dict):
        return dict((str(k), _sanitize(v, depth + 1)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in obj]
    return obj


def _is_bad_float(v):
    try:
        return v != v or v in (float("inf"), float("-inf"))
    except Exception:
        return False


def to_json(v, depth=0):
    """Convert a FreeCAD Python value into JSON-safe data."""
    import FreeCAD
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return str(v) if _is_bad_float(v) else v
    if depth > 12:
        return str(v)
    if isinstance(v, (list, tuple, set)):
        return [to_json(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return dict((str(k), to_json(x, depth + 1)) for k, x in v.items())
    try:
        if isinstance(v, FreeCAD.Vector):
            return {"x": v.x, "y": v.y, "z": v.z}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Rotation):
            ax = v.Axis
            return {"axis": [ax.x, ax.y, ax.z], "angle": v.Angle,
                    "quaternion": list(v.Q), "yaw_pitch_roll": [v.toEuler()[0], v.toEuler()[1], v.toEuler()[2]]}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Placement):
            return {"position": {"x": v.Base.x, "y": v.Base.y, "z": v.Base.z},
                    "rotation": to_json(v.Rotation, depth + 1)}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Matrix):
            return {"matrix": [[v.A11, v.A12, v.A13, v.A14], [v.A21, v.A22, v.A23, v.A24],
                               [v.A31, v.A32, v.A33, v.A34], [v.A41, v.A42, v.A43, v.A44]]}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Units.Quantity):
            return v.Value
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.DocumentObject):
            return {"document": v.Document.Name if v.Document else None, "object": v.Name,
                    "label": v.Label, "type": v.TypeId}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Base.BoundBox):
            return {"xmin": v.XMin, "ymin": v.YMin, "zmin": v.ZMin,
                    "xmax": v.XMax, "ymax": v.YMax, "zmax": v.ZMax,
                    "xlength": v.XLength, "ylength": v.YLength, "zlength": v.ZLength,
                    "center": {"x": v.Center.x, "y": v.Center.y, "z": v.Center.z}}
    except Exception:
        pass
    try:
        if type(v).__name__ == "Constraint" and hasattr(v, "First"):
            d = {"type": getattr(v, "Type", None)}
            for key, attr in (("first", "First"), ("first_pos", "FirstPos"), ("second", "Second"),
                              ("second_pos", "SecondPos"), ("value", "Value"), ("name", "Name"),
                              ("driving", "Driving"), ("active", "Active"), ("virtual_space", "VirtualSpace"),
                              ("is_dimensional", "IsDimensional")):
                try:
                    d[key] = to_json(getattr(v, attr), depth + 1)
                except Exception:
                    pass
            return d
    except Exception:
        pass
    try:
        if hasattr(v, "ShapeType") and hasattr(v, "Faces"):
            d = {"topo_shape": True, "shape_type": v.ShapeType}
            try:
                d["is_null"] = bool(v.isNull())
            except Exception:
                pass
            for key, attr in (("vertices", "Vertexes"), ("edges", "Edges"), ("faces", "Faces"), ("solids", "Solids")):
                try:
                    d[key] = len(getattr(v, attr))
                except Exception:
                    pass
            return d
    except Exception:
        pass
    # App::Property objects expose TypeId and Value
    try:
        if hasattr(v, "TypeId") and hasattr(v, "Value"):
            return to_json(v.Value, depth + 1)
    except Exception:
        pass
    try:
        return str(v)
    except Exception:
        return repr(v)


def geom_to_json(g):
    """Structured form of a Part 2D/3D geometry object."""
    import FreeCAD
    try:
        cls = type(g).__name__
        if cls in ("LineSegment", "Line"):
            return {"type": "line", "start": to_json(g.StartPoint), "end": to_json(g.EndPoint)}
        if cls in ("Arc", "ArcOfCircle"):
            d = {"type": "arc", "start": to_json(g.StartPoint), "end": to_json(g.EndPoint)}
            try:
                d["mid"] = to_json(g.MidPoint)
            except Exception:
                pass
            try:
                d["center"] = to_json(g.Center)
                d["radius"] = g.Radius
            except Exception:
                pass
            return d
        if cls in ("Circle",):
            return {"type": "circle", "center": to_json(g.Center), "axis": to_json(g.Axis),
                    "radius": g.Radius}
        if cls in ("Ellipse",):
            return {"type": "ellipse", "center": to_json(g.Center), "axis": to_json(g.Axis),
                    "major_radius": g.MajorRadius, "minor_radius": g.MinorRadius}
        if cls in ("Point",):
            return {"type": "point", "point": to_json(g.Point)}
        if "BSpline" in cls or "Bezier" in cls:
            return {"type": cls, "degree": getattr(g, "Degree", None),
                    "poles": to_json(list(getattr(g, "Poles", []) or []))}
    except Exception:
        pass
    try:
        return str(g)
    except Exception:
        return repr(g)


def _tuplify(value):
    """FreeCAD's C++ bindings often insist on tuples rather than lists."""
    if isinstance(value, list):
        return tuple(_tuplify(x) for x in value)
    return value


def _quantity(value):
    """Parse a number or a unit string such as '10 mm', '1 in', '30 deg'."""
    import FreeCAD
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return FreeCAD.Units.Quantity(value).Value
    except Exception as e:
        raise ValueError(
            "cannot read %r as a number with units (%s). Give a plain number in FreeCAD's "
            "internal unit (mm, degrees) or a string such as '10 mm', '1 in', '30 deg', "
            "'2.5 kg'" % (value, e))


def _vec(value, what="vector"):
    import FreeCAD
    if isinstance(value, FreeCAD.Vector):
        return value
    if isinstance(value, dict):
        return FreeCAD.Vector(float(value.get("x", 0)), float(value.get("y", 0)), float(value.get("z", 0)))
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            return FreeCAD.Vector(float(value[0]), float(value[1]), 0.0)
        if len(value) == 3:
            return FreeCAD.Vector(float(value[0]), float(value[1]), float(value[2]))
    if isinstance(value, str):
        parts = [p for p in value.replace(";", ",").split(",") if p.strip()]
        if len(parts) == 3:
            return FreeCAD.Vector(*[float(p) for p in parts])
    raise ValueError("cannot read %s from %r (use [x, y, z] or {\"x\":..,\"y\":..,\"z\":..})" % (what, value))


def _rotation(value):
    import FreeCAD
    if isinstance(value, FreeCAD.Rotation):
        return value
    if isinstance(value, dict):
        if "quaternion" in value:
            q = value["quaternion"]
            return FreeCAD.Rotation(float(q[0]), float(q[1]), float(q[2]), float(q[3]))
        if "yaw_pitch_roll" in value:
            y, p, r = value["yaw_pitch_roll"]
            return FreeCAD.Rotation(float(y), float(p), float(r))
        if "euler" in value:
            y, p, r = value["euler"]
            return FreeCAD.Rotation(float(y), float(p), float(r))
        axis = _vec(value.get("axis", [0, 0, 1]), "rotation axis")
        ang = float(value.get("angle", 0.0))
        return FreeCAD.Rotation(axis, ang)
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return FreeCAD.Rotation(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    raise ValueError("cannot read rotation from %r" % (value,))


def _placement(value):
    import FreeCAD
    if isinstance(value, FreeCAD.Placement):
        return value
    if isinstance(value, dict):
        pos = _vec(value.get("position", value.get("base", [0, 0, 0])), "placement position")
        rot = _rotation(value["rotation"]) if "rotation" in value else FreeCAD.Rotation()
        return FreeCAD.Placement(pos, rot)
    if isinstance(value, (list, tuple)):
        if len(value) == 3:
            return FreeCAD.Placement(_vec(value), FreeCAD.Rotation())
        if len(value) == 7:
            return FreeCAD.Placement(FreeCAD.Vector(*[float(x) for x in value[:3]]),
                                     FreeCAD.Rotation(*[float(x) for x in value[3:]]))
        if len(value) == 4 and all(isinstance(r, (list, tuple)) for r in value):
            m = FreeCAD.Matrix(*[float(x) for row in value for x in row])
            return FreeCAD.Placement(m)
    raise ValueError("cannot read placement from %r" % (value,))


def _resolve_object(ref, doc=None):
    """Resolve an object reference: 'Name', 'Label', 'DocName#Name', 'DocName.Name'."""
    import FreeCAD
    if isinstance(ref, FreeCAD.DocumentObject):
        return ref
    if not isinstance(ref, str):
        raise ValueError("object reference must be a string or object, got %r" % (ref,))
    txt = ref.strip()
    if "#" in txt:
        dn, on = txt.split("#", 1)
        d = FreeCAD.getDocument(dn)
        if d is None:
            raise ValueError("no document named %r" % dn)
        o = d.getObject(on) or (d.getObjectsByLabel(on) or [None])[0]
        if o is None:
            raise ValueError("no object %r in document %r" % (on, dn))
        return o
    if doc is None:
        doc = FreeCAD.ActiveDocument
    if doc is not None:
        o = doc.getObject(txt)
        if o is not None:
            return o
        by_label = doc.getObjectsByLabel(txt)
        if by_label:
            return by_label[0]
    # search every open document
    for d in FreeCAD.listDocuments().values():
        o = d.getObject(txt)
        if o is not None:
            return o
        by_label = d.getObjectsByLabel(txt)
        if by_label:
            return by_label[0]
    if "." in txt:
        dn, on = txt.split(".", 1)
        d = FreeCAD.listDocuments().get(dn)
        if d is not None:
            o = d.getObject(on)
            if o is not None:
                return o
    low = txt.lower()
    for d in FreeCAD.listDocuments().values():
        for o in d.Objects:
            if o.Name.lower() == low or o.Label.lower() == low:
                return o
    raise ValueError("object %r not found in %s" % (ref, "the active document" if doc else "any open document"))


def from_json(value, type_id, doc=None):
    """Coerce JSON data into the Python value the given FreeCAD property type wants."""
    import FreeCAD
    t = type_id or ""
    if "PropertyVector" in t or "PropertyPosition" in t or "PropertyDirection" in t:
        return _vec(value)
    if "PropertyPlacement" in t:
        return _placement(value)
    if "PropertyRotation" in t:
        return _rotation(value)
    if "PropertyMatrix" in t:
        if isinstance(value, dict) and "matrix" in value:
            value = value["matrix"]
        if isinstance(value, (list, tuple)):
            flat = [float(x) for row in value for x in row] if value and isinstance(value[0], (list, tuple)) else [float(x) for x in value]
            return FreeCAD.Matrix(*flat)
        raise ValueError("matrix expects 16 numbers or 4 rows of 4")
    if "PropertyBool" in t:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if "PropertyInteger" in t or "PropertyPercent" in t or "PropertyIndex" in t:
        if isinstance(value, str):
            return int(_quantity(value))
        return int(value)
    if ("PropertyLength" in t or "PropertyDistance" in t or "PropertyAngle" in t or
            "PropertyQuantity" in t or "PropertyArea" in t or "PropertyVolume" in t or
            "PropertySpeed" in t or "PropertyAcceleration" in t or "PropertyForce" in t or
            "PropertyPressure" in t or "PropertyStiffness" in t or "PropertyVacuumPermittivity" in t or
            "PropertyFloat" in t or "PropertyFloatList" in t or "PropertyPrecision" in t):
        if isinstance(value, (list, tuple)):
            return [float(x) for x in value]
        return _quantity(value)
    if "PropertyLink" in t:
        is_list = ("LinkList" in t or "LinkSubList" in t or "XLinkList" in t)
        is_sub = ("LinkSub" in t)
        if value is None or value == "" or value == []:
            return [] if is_list else None
        if not isinstance(value, (list, tuple)):
            value = [value]
        out = []
        for item in value:
            if isinstance(item, dict):
                ref = item.get("object") or item.get("name")
                subs = item.get("sub", item.get("subelements", item.get("subelement")))
                if subs is None:
                    subs = []
                elif isinstance(subs, str):
                    subs = [subs]
                o = _resolve_object(ref, doc)
                out.append((o, list(subs)) if is_sub else o)
            else:
                o = _resolve_object(item, doc)
                out.append((o, []) if is_sub else o)
        if is_list:
            return out
        return out[0]
    if "FilletEdges" in t or "ChamferEdges" in t:
        out = []
        for item in (value or []):
            item = list(item)
            if len(item) == 2:
                item = [item[0], item[1], item[1]]
            out.append(tuple([int(item[0])] + [float(x) for x in item[1:]]))
        return out
    if "PropertyColor" in t:
        if isinstance(value, str):
            s = value.lstrip("#")
            if len(s) == 6:
                return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0, 1.0)
        if isinstance(value, (list, tuple)):
            return tuple(float(x) for x in value)
    if "PropertyStringList" in t:
        if isinstance(value, str):
            return [value]
        return [str(x) for x in value]
    if "PropertyFile" in t or "PropertyPath" in t or "PropertyString" in t or "PropertyEnumeration" in t or "PropertyFont" in t:
        return str(value)
    if "PropertyMap" in t:
        return dict(value)
    if "PropertyPythonObject" in t or "PropertyExpressionEngine" in t:
        return value
    # unknown: hand it over unchanged
    return value


# --------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------
class _OpError(Exception):
    """Carries the message and the in-application traceback of a failed operation."""

    def __init__(self, message, tb):
        Exception.__init__(self, message)
        self.message = message
        self.tb = tb


class _Bridge(object):
    def __init__(self):
        self._messages = []
        self._lock = threading.RLock()

    # ---- console capture -------------------------------------------------
    def install_observer(self):
        """FreeCAD 0.20 has no Console.AddObserver; the Report view widget is read instead."""
        return

    def take_messages(self):
        msgs = list(self._messages)
        del self._messages[:]
        return msgs

    def _report_text(self):
        """Text currently shown in FreeCAD's Report view (messages/warnings/errors)."""
        try:
            import FreeCADGui
            from PySide2 import QtWidgets
            mw = FreeCADGui.getMainWindow()
            if mw is None:
                return ""
            dock = mw.findChild(QtWidgets.QDockWidget, "Report view")
            w = dock.widget() if dock is not None else None
            if w is not None and hasattr(w, "toPlainText"):
                return w.toPlainText()
        except Exception:
            pass
        return ""

    def _report_delta(self, before):
        """Lines added to the Report view since 'before' was taken."""
        after = self._report_text()
        if not after:
            return []
        if before and after.startswith(before):
            new = after[len(before):]
        elif before:
            # the report view drops old lines once it is full: find the overlap
            b = before[-4000:]
            a = after[:4000]
            new = after
            for k in range(min(len(b), len(a)), 0, -1):
                if b[-k:] == a[:k]:
                    new = after[k:]
                    break
        else:
            new = after
        lines = [l for l in new.splitlines() if l.strip()]
        return lines[-200:]

    # ---- main thread trampoline -----------------------------------------
    def main_thread(self, fn, timeout=180):
        from PySide2.QtCore import QObject, QCoreApplication, Qt, QMetaObject, Slot
        app = QCoreApplication.instance()
        if app is None:
            return fn()
        box = {}
        done = threading.Event()

        class _Runner(QObject):
            @Slot()
            def run(self):
                try:
                    box["v"] = fn()
                    box["ok"] = True
                except BaseException as exc:
                    box["err"] = (str(exc), traceback.format_exc())
                    box["ok"] = False
                finally:
                    done.set()

        r = _Runner()
        r.moveToThread(app.thread())
        QMetaObject.invokeMethod(r, "run", Qt.QueuedConnection)
        if not done.wait(timeout):
            raise RuntimeError("timed out after %ss waiting for FreeCAD's main thread "
                               "(a modal dialog is probably open)" % timeout)
        if not box.get("ok"):
            err = box.get("err") or ("unknown error", "")
            raise _OpError(err[0], err[1])
        return box.get("v")

    # ---- dispatch --------------------------------------------------------
    def _run_op(self, op, fn, args):
        """Run one operation, wrapped in a document transaction when it changes data."""
        doc = None
        if op in _MUTATING:
            try:
                doc = _doc(args)
            except Exception:
                doc = None
            if doc is not None:
                try:
                    if getattr(doc, "HasPendingTransaction", False):
                        doc = None
                    else:
                        doc.openTransaction("MCP: " + op)
                except Exception:
                    doc = None
        try:
            result = fn(args)
        except BaseException:
            if doc is not None:
                try:
                    doc.abortTransaction()
                except Exception:
                    pass
            raise
        if doc is not None:
            try:
                doc.recompute()
            except Exception:
                pass
            try:
                doc.commitTransaction()
            except Exception:
                pass
        return result

    def handle(self, req):
        op = req.get("op")
        args = req.get("args") or {}
        with self._lock:
            self.install_observer()
            del self._messages[:]
            report_before = self._report_text()
            fn = OPS.get(op)
            if fn is None:
                raise ValueError("unknown operation %r; known operations: %s" %
                                 (op, ", ".join(sorted(OPS))))
            result = self.main_thread(lambda: self._run_op(op, fn, args))
            msgs = self.take_messages()
            msgs += self._report_delta(report_before)
        return {"result": result, "console": msgs}


_BRIDGE = None


def _bridge():
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = _Bridge()
    return _BRIDGE


# --------------------------------------------------------------------------
# helpers used by the operations
# --------------------------------------------------------------------------
def _doc(args, key="document"):
    import FreeCAD
    name = args.get(key)
    if name in (None, "", "Active", "active"):
        d = FreeCAD.ActiveDocument
        if d is None:
            open_docs = sorted(FreeCAD.listDocuments().keys())
            raise ValueError("no active document; open documents: %s" % (open_docs or "none"))
        return d
    try:
        d = FreeCAD.getDocument(name)
    except Exception:
        d = None
    if d is None:
        for d2 in FreeCAD.listDocuments().values():
            if d2.Label == name:
                return d2
        low = str(name).lower()
        for d2 in FreeCAD.listDocuments().values():
            if d2.Name.lower() == low or d2.Label.lower() == low:
                return d2
        raise ValueError("no open document named %r; open documents: %s" %
                         (name, sorted(FreeCAD.listDocuments().keys())))
    return d


def _obj(args, doc):
    return _resolve_object(args["object"], doc)


def _obj_summary(o):
    return {"name": o.Name, "label": o.Label, "type": o.TypeId}


def _prop_value(o, name):
    try:
        return to_json(getattr(o, name))
    except Exception as e:
        return "<unreadable: %s>" % e


def _expressions(o):
    out = {}
    try:
        for pair in o.ExpressionEngine:
            out[pair[0]] = pair[1]
    except Exception:
        pass
    return out


def _state(o):
    st = []
    try:
        st = list(o.State)
    except Exception:
        pass
    return st


def _visibility(o):
    try:
        vo = o.ViewObject
        return bool(vo.Visibility) if vo is not None else None
    except Exception:
        return None


def _obj_full(o, include_properties=True):
    d = _obj_summary(o)
    d["state"] = _state(o)
    try:
        d["valid"] = bool(o.isValid())
    except Exception:
        pass
    d["visibility"] = _visibility(o)
    try:
        d["parents"] = [p.Name for p in o.InList]
    except Exception:
        pass
    try:
        d["dependencies"] = [p.Name for p in o.OutList]
    except Exception:
        pass
    if include_properties:
        props = {}
        is_sketch = "Sketch" in o.TypeId
        for p in list(o.PropertiesList):
            if is_sketch and p == "Geometry":
                try:
                    props[p] = [geom_to_json(g) for g in o.Geometry]
                    continue
                except Exception:
                    pass
            props[p] = _prop_value(o, p)
        d["properties"] = props
        d["expressions"] = _expressions(o)
        disp = {}
        for p in list(o.PropertiesList):
            try:
                v = getattr(o, p)
                if hasattr(v, "UserString"):
                    disp[p] = v.UserString
            except Exception:
                pass
        if disp:
            d["display"] = disp
    return d


def _set_one(o, prop, value):
    """Set one property on one object; returns the resulting JSON value."""
    import FreeCAD
    if not hasattr(o, prop) and prop not in list(getattr(o, "PropertiesList", [])):
        raise ValueError("%s (%s) has no property %r; its properties are: %s" %
                         (o.Name, o.TypeId, prop, ", ".join(list(o.PropertiesList)[:60])))
    try:
        tid = o.getTypeIdOfProperty(prop)
    except Exception:
        tid = None
    if tid is None:
        try:
            tid = o.getTypeOfProperty(prop)
        except Exception:
            tid = None
    if tid == "App::PropertyEnumeration":
        try:
            allowed = list(o.getEnumerationsOfProperty(prop) or [])
        except Exception:
            allowed = []
        if allowed and value not in allowed:
            raise ValueError("%s.%s accepts only: %s (got %r)" %
                             (o.Name, prop, ", ".join(allowed), value))
    coerced = from_json(value, tid, o.Document)
    try:
        setattr(o, prop, coerced)
    except TypeError:
        setattr(o, prop, _tuplify(coerced))
    except Exception as e:
        hint = ""
        try:
            allowed = list(o.getEnumerationsOfProperty(prop) or [])
            if allowed:
                hint = " (accepted values: %s)" % ", ".join(allowed)
        except Exception:
            pass
        raise ValueError("could not set %s.%s (%s) to %r: %s%s" % (o.Name, prop, tid, value, e, hint))
    return _prop_value(o, prop)


def _gui_modified(name):
    try:
        import FreeCADGui
        gd = FreeCADGui.getDocument(name)
        return bool(gd.Modified) if gd is not None else None
    except Exception:
        return None


def _recompute(doc):
    import FreeCAD
    n = None
    try:
        n = doc.recompute()
    except Exception as e:
        return {"recomputed": None, "error": str(e)}
    return {"recomputed": n}


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------
def op_ping(args):
    return {"pong": True, "version": VERSION}


def op_app_info(args):
    import FreeCAD
    out = {}
    try:
        import FreeCADGui
        out["gui"] = True
        out["active_workbench"] = FreeCADGui.activeWorkbench().name() if FreeCADGui.activeWorkbench() else None
        try:
            out["workbenches"] = sorted(FreeCADGui.listWorkbenches().keys())
        except Exception:
            pass
        mw = FreeCADGui.getMainWindow()
        out["main_window_title"] = mw.windowTitle() if mw is not None else None
        try:
            from PySide2 import QtWidgets
            app = QtWidgets.QApplication.instance()
            modal = app.activeModalWidget() if app is not None else None
            out["modal_dialog"] = None
            if modal is not None:
                out["modal_dialog"] = {"title": modal.windowTitle(),
                                       "class": modal.metaObject().className()}
            out["windows"] = [w.windowTitle() for w in app.topLevelWidgets()
                              if w.isVisible() and w.windowTitle()] if app is not None else []
        except Exception:
            pass
    except Exception as e:
        out["gui"] = False
        out["gui_error"] = str(e)
    out["version"] = list(FreeCAD.Version())
    out["pid"] = os.getpid()
    for k, f in (("user_config_dir", FreeCAD.getUserConfigDir), ("user_app_data_dir", FreeCAD.getUserAppDataDir),
                 ("user_macro_dir", FreeCAD.getUserMacroDir), ("temp_dir", FreeCAD.getTempPath),
                 ("resource_dir", FreeCAD.getResourceDir)):
        try:
            out[k] = f()
        except Exception:
            pass
    docs = []
    for name, d in FreeCAD.listDocuments().items():
        docs.append({"name": name, "label": d.Label, "file": d.FileName or None,
                     "objects": len(d.Objects), "modified": not d.isSaved(),
                     "gui_modified": _gui_modified(name)})
    out["documents"] = docs
    try:
        cache = os.path.join(FreeCAD.getUserCachePath(), "Cache")
        rec = []
        for entry in os.listdir(cache):
            p = os.path.join(cache, entry)
            if os.path.isdir(p) and any(f.startswith("fc_recovery_file") for f in os.listdir(p)):
                rec.append(p)
        out["document_recovery_files"] = rec
    except Exception:
        pass
    ad = FreeCAD.ActiveDocument
    out["active_document"] = ad.Name if ad else None
    try:
        import FreeCADGui
        sel = FreeCADGui.Selection.getSelection()
        out["selection"] = [{"document": s.Document.Name, "object": s.Name, "label": s.Label,
                             "type": s.TypeId} for s in sel]
    except Exception:
        out["selection"] = []
    return out


def op_list_workbenches(args):
    import FreeCADGui
    out = []
    for name, wb in sorted(FreeCADGui.listWorkbenches().items()):
        out.append({"name": name, "menu": getattr(wb, "MenuText", None)})
    return out


def op_activate_workbench(args):
    import FreeCADGui
    name = args["workbench"]
    available = FreeCADGui.listWorkbenches()
    if name not in available:
        matches = [k for k in available if name.lower() in k.lower()]
        raise ValueError("no workbench %r; did you mean one of %s?" % (name, matches or sorted(available)))
    FreeCADGui.activateWorkbench(name)
    FreeCADGui.updateGui()
    wb = FreeCADGui.activeWorkbench()
    return {"workbench": name, "active": wb.name() if wb else None}


def op_list_commands(args):
    import FreeCADGui
    flt = (args.get("filter") or "").lower()
    limit = int(args.get("limit") or 0)
    names = sorted(FreeCADGui.listCommands())
    out = []
    for n in names:
        if flt and flt not in n.lower():
            continue
        entry = {"command": n}
        try:
            act = FreeCADGui.Command.get(n) if hasattr(FreeCADGui, "Command") else None
        except Exception:
            act = None
        if act is not None:
            try:
                entry["text"] = act.text() or None
                entry["tooltip"] = (act.toolTip() or None)
                entry["shortcut"] = act.shortcut().toString() or None
                entry["enabled"] = bool(act.isEnabled())
            except Exception:
                pass
        out.append(entry)
        if limit and len(out) >= limit:
            break
    return {"count": len(out), "commands": out}


def _active_modal():
    try:
        from PySide2 import QtWidgets
        app = QtWidgets.QApplication.instance()
        w = app.activeModalWidget() if app is not None else None
        if w is None:
            return None
        return {"title": w.windowTitle(), "class": w.metaObject().className(), "_widget": w}
    except Exception:
        return None


def op_run_command(args):
    import FreeCADGui
    name = args["command"]
    extra = args.get("args")
    known = name in FreeCADGui.listCommands()
    before = _active_modal()
    try:
        if extra:
            FreeCADGui.runCommand(name, str(extra))
        else:
            FreeCADGui.runCommand(name)
    except Exception as e:
        raise ValueError("command %r failed: %s (is it in list_commands?)" % (name, e))
    try:
        FreeCADGui.updateGui()
    except Exception:
        pass
    out = {"command": name, "known_command": known,
           "active_document": FreeCADGui.ActiveDocument.Document.Name if FreeCADGui.ActiveDocument else None}
    modal = _active_modal()
    if modal is not None and (before is None or before["title"] != modal["title"]):
        widget = modal.pop("_widget")
        if args.get("dismiss_dialog"):
            try:
                if hasattr(widget, "reject"):
                    widget.reject()
                else:
                    widget.close()
                modal["dismissed"] = True
            except Exception as e:
                modal["dismiss_error"] = str(e)
        out["modal_dialog"] = modal
        out["note"] = ("this command opened a dialog which is now shown in the FreeCAD window"
                       + ("; it was dismissed" if modal.get("dismissed") else
                          "; pass dismiss_dialog=true to close it, or close it in the window"))
    return out


def op_list_documents(args):
    import FreeCAD
    out = []
    for name, d in FreeCAD.listDocuments().items():
        out.append({"name": name, "label": d.Label, "file": d.FileName or None,
                    "objects": [o.Name for o in d.Objects], "modified": not d.isSaved(),
                    "gui_modified": _gui_modified(name)})
    return out


def op_get_document(args):
    import FreeCAD
    d = _doc(args)
    only = args.get("objects")
    include_props = args.get("properties", True)
    objs = []
    for o in d.Objects:
        if only and o.Name not in only and o.Label not in only:
            continue
        objs.append(_obj_full(o, include_props))
    out = {"name": d.Name, "label": d.Label, "file": d.FileName or None,
           "modified": not d.isSaved(), "gui_modified": _gui_modified(d.Name),
           "object_count": len(d.Objects),
           "objects": objs,
           "undo_count": d.UndoCount, "redo_count": d.RedoCount}
    try:
        out["root_objects"] = [o.Name for o in d.RootObjects]
    except Exception:
        pass
    try:
        out["topological_order"] = [o.Name for o in d.TopologicalSortedObjects]
    except Exception:
        pass
    try:
        out["active_object"] = d.ActiveObject.Name if d.ActiveObject else None
    except Exception:
        pass
    return out


def op_get_object(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    out = _obj_full(o, args.get("properties", True))
    try:
        out["document"] = d.Name
        out["id"] = o.ID
        out["supported_properties"] = list(o.supportedProperties())
    except Exception:
        pass
    return out


def _probe_document():
    """Return (document, temporary?) using an open document when possible."""
    import FreeCAD
    active = FreeCAD.ActiveDocument
    if active is not None:
        return active, False
    return FreeCAD.newDocument("__mcp_probe__"), True


def _probe_release(tmp_doc):
    import FreeCAD
    if tmp_doc is None:
        return
    name = tmp_doc.Name
    try:
        FreeCAD.closeDocument(name)
    except Exception:
        pass


_MODULES_LOADED = [False]


def _load_all_modules():
    """Import the workbench modules so document.supportedTypes() is complete."""
    if _MODULES_LOADED[0]:
        return
    _MODULES_LOADED[0] = True
    import FreeCAD
    import importlib
    names = ["Arch", "Draft", "Drawing", "Fem", "Idf", "Image", "Import", "Inspection",
             "Material", "Measure", "Mesh", "MeshPart", "OpenSCAD", "Part", "PartDesign",
             "Path", "Plot", "Points", "Raytracing", "ReverseEngineering", "Robot", "Show",
             "Sketcher", "Spreadsheet", "Start", "Surface", "TechDraw", "Test", "Tux", "Web"]
    for base in (os.path.join(FreeCAD.getResourceDir(), "Mod"),
                 os.path.join(os.path.dirname(FreeCAD.getUserAppDataDir()), "Mod"),
                 "/usr/lib/freecad/Mod"):
        try:
            for entry in sorted(os.listdir(base)):
                if entry not in names and os.path.isdir(os.path.join(base, entry)):
                    names.append(entry)
        except Exception:
            pass
    for n in names:
        try:
            importlib.import_module(n)
        except Exception:
            pass


def op_list_object_types(args):
    import FreeCAD
    _load_all_modules()
    flt = (args.get("filter") or "").lower()
    try:
        d = _doc(args)
        types = list(d.supportedTypes())
    except Exception:
        types = []
        for name, d in FreeCAD.listDocuments().items():
            types = list(d.supportedTypes())
            break
    if not types:
        d, tmp = _probe_document()
        try:
            types = list(d.supportedTypes())
        finally:
            _probe_release(d if tmp else None)
    if flt:
        types = [t for t in types if flt in t.lower()]
    return {"count": len(types), "types": types}


def op_object_schema(args):
    import FreeCAD
    doc = None
    if args.get("object"):
        doc = _doc(args)
        o = _obj(args, doc)
        type_id = o.TypeId
        name = o.Name
        props = list(o.PropertiesList)
    else:
        type_id = args["type"]
        import FreeCAD
        active = FreeCAD.ActiveDocument.Name if FreeCAD.ActiveDocument else None
        doc = FreeCAD.newDocument("__mcp_probe__")
        try:
            try:
                o = doc.addObject(type_id, "McpProbe")
            except Exception as e:
                raise ValueError("unknown object type %r (%s)" % (type_id, e))
            out = {"type": type_id, "object": None, "properties": []}
            for p in list(o.PropertiesList):
                out["properties"].append(_prop_info(o, p))
            return out
        finally:
            _probe_release(doc)
            if active:
                try:
                    FreeCAD.setActiveDocument(active)
                except Exception:
                    pass
    out = {"type": type_id, "object": name, "properties": []}
    for p in props:
        out["properties"].append(_prop_info(o, p))
    return out


def _prop_info(o, p):
    import FreeCAD
    info = {"name": p}
    try:
        info["type"] = o.getTypeIdOfProperty(p)
    except Exception:
        pass
    try:
        info["group"] = o.getGroupOfProperty(p)
    except Exception:
        pass
    try:
        en = o.getEnumerationsOfProperty(p)
        if en:
            info["enum"] = list(en)
    except Exception:
        pass
    try:
        v = getattr(o, p)
        info["value"] = to_json(v)
        if hasattr(v, "UserString"):
            info["user_string"] = v.UserString
    except Exception as e:
        info["read_error"] = str(e)
    try:
        st = o.getPropertyStatus(p)
        if st:
            names = [x for x in st if isinstance(x, str)]
            if names:
                info["status"] = names
    except Exception:
        pass
    try:
        info["documentation"] = o.getDocumentationOfProperty(p) or None
    except Exception:
        pass
    return info


def op_get_property(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    prop = args["property"]
    if prop not in list(o.PropertiesList) and not hasattr(o, prop):
        raise ValueError("%s (%s) has no property %r; its properties are: %s" %
                         (o.Name, o.TypeId, prop, ", ".join(list(o.PropertiesList))))
    info = _prop_info(o, prop)
    unit = args.get("unit")
    if unit:
        try:
            info["value_in_" + str(unit)] = o.getPropertyByName(prop).getValueAs(str(unit))
            info["requested_unit"] = unit
        except Exception as e:
            raise ValueError("cannot express %s.%s in %r: %s. This works for properties with a unit "
                             "(length, angle, mass, ...); examples: mm, cm, m, um, in, ft, deg, rad, "
                             "kg, g, s." % (o.Name, prop, unit, e))
    return info


def op_create_object(args):
    import FreeCAD
    d = _doc(args)
    type_id = args["type"]
    name = args.get("name") or None
    o = d.addObject(type_id, name) if name else d.addObject(type_id)
    if o is None:
        raise ValueError("could not create object of type %r" % type_id)
    parent = args.get("parent")
    if parent:
        pobj = _resolve_object(parent, d)
        try:
            if hasattr(pobj, "addObject"):
                pobj.addObject(o)
            else:
                pobj.Group = list(pobj.Group) + [o]
        except Exception as e:
            raise ValueError("could not put %s inside %s: %s" % (o.Name, pobj.Name, e))
    if args.get("label"):
        o.Label = args["label"]
    results = {}
    for k, v in (args.get("properties") or {}).items():
        results[k] = _set_one(o, k, v)
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    out = _obj_summary(o)
    out["document"] = d.Name
    out["set_properties"] = results
    if parent:
        try:
            out["parent"] = pobj.Name
        except Exception:
            pass
    return out


def op_set_property(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    prop = args["property"]
    val = _set_one(o, prop, args.get("value"))
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    return {"document": d.Name, "object": o.Name, "property": prop, "value": val,
            "expression": _expressions(o).get(prop)}


def op_set_properties(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    results = {}
    errors = {}
    for k, v in (args.get("properties") or {}).items():
        try:
            results[k] = _set_one(o, k, v)
        except Exception as e:
            errors[k] = str(e)
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    return {"document": d.Name, "object": o.Name, "values": results, "errors": errors}


def op_set_expression(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    prop = args["property"]
    expr = args.get("expression")
    if expr in (None, ""):
        o.setExpression(prop, None)
    else:
        try:
            o.setExpression(prop, str(expr))
        except Exception as e:
            raise ValueError(
                "FreeCAD's expression parser rejected %r for %s.%s: %s\n"
                "Expressions reference other objects by name (or label) and read their properties, "
                "e.g. 'Box.Length * 2', 'Spreadsheet.A1 + 1', 'Params.hole_dia / 2', "
                "'Box.Shape.BoundBox.XLength'. Functions this FreeCAD version understands: abs, acos, "
                "asin, atan, atan2, ceil, cos, cosh, exp, floor, hypot, log, log10, mod, pow, round, "
                "sin, sinh, sqrt, tan, tanh, trunc, average, count, max, min, stddev, sum, create, "
                "list, tuple. Numbers take units ('10 mm', '30 deg'); arguments are separated with "
                "';'. Vector and Placement properties cannot be driven by expressions in 0.20 - set "
                "them with freecad_set_property instead (read the spreadsheet cells first if you "
                "want to compute the values from them). Object names shorter than two characters are "
                "not accepted by the parser." % (expr, o.Name, prop, e))
    try:
        d.recompute()
    except Exception:
        pass
    return {"document": d.Name, "object": o.Name, "property": prop,
            "expression": _expressions(o).get(prop), "value": _prop_value(o, prop)}


def op_delete_object(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    name = o.Name
    d.removeObject(name)
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    return {"document": d.Name, "removed": name, "remaining": [x.Name for x in d.Objects]}


def op_recompute(args):
    import FreeCAD
    d = _doc(args)
    r = _recompute(d)
    states = []
    for o in d.Objects:
        st = _state(o)
        try:
            valid = bool(o.isValid())
        except Exception:
            valid = None
        if st or valid is False:
            states.append({"object": o.Name, "state": st, "valid": valid})
    r["document"] = d.Name
    r["objects"] = states
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    return r


def op_new_document(args):
    import FreeCAD
    name = args.get("name") or "Unnamed"
    d = FreeCAD.newDocument(name)
    if args.get("label"):
        d.Label = args["label"]
    return {"name": d.Name, "label": d.Label}


def op_open_document(args):
    import FreeCAD
    path = os.path.expanduser(args["path"])
    if not os.path.exists(path):
        raise ValueError("no such file: %s" % path)
    d = FreeCAD.open(path)
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    return {"name": d.Name, "label": d.Label, "file": d.FileName, "objects": [o.Name for o in d.Objects]}


def op_save_document(args):
    import FreeCAD
    d = _doc(args)
    path = args.get("path")
    if path:
        path = os.path.expanduser(path)
        d.saveAs(path)
    else:
        if not d.FileName:
            raise ValueError("document %r has never been saved; pass 'path'" % d.Name)
        d.save()
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
        # FreeCAD's own save command is what clears the "unsaved changes" flag the window
        # shows; a scripted save alone does not.  Do it for the active document only, and
        # only when it already has a file name, so no dialog can appear.
        if d.FileName and FreeCADGui.ActiveDocument is not None and \
                FreeCADGui.ActiveDocument.Document is d:
            FreeCADGui.runCommand("Std_Save")
    except Exception:
        pass
    return {"name": d.Name, "file": d.FileName, "saved": bool(d.isSaved()),
            "gui_modified": _gui_modified(d.Name),
            "exists": os.path.exists(d.FileName or "")}


def op_close_document(args):
    import FreeCAD
    d = _doc(args)
    name = d.Name
    if args.get("save") and not d.isSaved():
        if d.FileName:
            d.save()
        else:
            raise ValueError("document %r has no file name; save it with save_document(path=...)" % name)
    import FreeCADGui
    FreeCADGui.closeDocument(name) if args.get("gui") else FreeCAD.closeDocument(name)
    return {"closed": name, "open_documents": sorted(FreeCAD.listDocuments().keys())}


def op_export(args):
    import FreeCAD
    d = _doc(args)
    path = os.path.expanduser(args["path"])
    names = args.get("objects")
    objs = [_resolve_object(n, d) for n in names] if names else list(d.Objects)
    ext = os.path.splitext(path)[1].lower()
    d.recompute()
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    errs = []
    try:
        if ext in (".fcstd",):
            d.saveAs(path)
        elif ext in (".dxf", ".dwg"):
            import importDXF
            importDXF.export(objs, path)
        elif ext in (".svg", ".svgz"):
            import importSVG
            importSVG.export(objs, path)
        elif ext in (".stl", ".obj", ".ply", ".amf", ".3mf", ".ast", ".bms", ".wrl", ".vrml",
                     ".off", ".smf", ".x3d", ".gltf", ".glb"):
            import Mesh
            Mesh.export(objs, path)
        elif ext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
            import Part
            Part.export(objs, path)
        else:
            import Import
            Import.export(objs, path)
    except Exception as e:
        errs.append(str(e))
    if not os.path.exists(path):
        try:
            import Import
            Import.export(objs, path)
        except Exception as e:
            errs.append(str(e))
    if not os.path.exists(path):
        known = []
        try:
            known = sorted(set(FreeCAD.getExportType()))
        except Exception:
            pass
        raise ValueError("FreeCAD wrote no file for %r. This build exports: %s%s" %
                         (path, ", ".join("." + k for k in known) or "?",
                          (" (errors: %s)" % "; ".join(errs)) if errs else ""))
    return {"path": path, "bytes": os.path.getsize(path), "format": ext,
            "objects": [o.Name for o in objs]}


def op_import_file(args):
    import FreeCAD
    path = os.path.expanduser(args["path"])
    if not os.path.exists(path):
        raise ValueError("no such file: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    try:
        d = _doc(args) if (args.get("document") or FreeCAD.ActiveDocument) else FreeCAD.newDocument("Import")
    except Exception:
        d = FreeCAD.newDocument("Import")
    if ext in (".fcstd",):
        nd = FreeCAD.open(path)
        return {"opened": nd.Name, "objects": [o.Name for o in nd.Objects]}
    try:
        if ext in (".dxf", ".dwg"):
            import importDXF
            importDXF.insert(path, d.Name)
        elif ext in (".svg", ".svgz"):
            import importSVG
            importSVG.insert(path, d.Name)
        else:
            import Import
            Import.insert(path, d.Name)
    except Exception as e:
        raise ValueError("import of %s failed: %s" % (path, e))
    d.recompute()
    try:
        import FreeCADGui
        FreeCADGui.updateGui()
    except Exception:
        pass
    return {"document": d.Name, "objects": [o.Name for o in d.Objects], "imported": path}


def op_measure(args):
    import FreeCAD
    d = _doc(args)
    o = _obj(args, d)
    out = {"document": d.Name, "object": o.Name, "type": o.TypeId, "label": o.Label}
    shp = None
    try:
        shp = o.Shape
    except Exception:
        shp = None
    if shp is not None:
        s = {}
        for key, attr in (("volume", "Volume"), ("area", "Area"), ("length", "Length"),
                          ("vertexes", "Vertexes"), ("edges", "Edges"), ("wires", "Wires"),
                          ("faces", "Faces"), ("shells", "Shells"), ("solids", "Solids"),
                          ("compounds", "Compounds")):
            try:
                v = getattr(shp, attr)
                s[key] = len(v) if isinstance(v, (list, tuple)) else v
            except Exception:
                pass
        for key, attr in (("center_of_mass", "CenterOfMass"), ("bound_box", "BoundBox"),
                          ("matrix_of_inertia", "MatrixOfInertia"), ("static_moments", "StaticMoments")):
            try:
                s[key] = to_json(getattr(shp, attr))
            except Exception:
                pass
        try:
            s["is_valid"] = bool(shp.isValid())
            s["is_closed"] = bool(shp.isClosed())
            s["is_null"] = bool(shp.isNull())
        except Exception:
            pass
        if args.get("check"):
            # Shape.check() returns None; it *reports* problems to FreeCAD's report view,
            # which this server captures and returns in the "console" field of the reply.
            try:
                shp.check(True)
                s["topology_check_run"] = True
                s["is_valid"] = bool(shp.isValid())
            except Exception as e:
                s["topology_check_run"] = "error: %s" % e
        try:
            s["principal_properties"] = to_json(shp.PrincipalProperties)
        except Exception:
            pass
        out["shape"] = s
    if o.TypeId.startswith("Mesh::"):
        m = {}
        for key, attr in (("points", "CountPoints"), ("facets", "CountFacets"), ("volume", "Volume"),
                          ("area", "Area"), ("bound_box", "BoundBox"), ("center_of_mass", "CenterOfMass")):
            try:
                m[key] = to_json(getattr(o.Mesh, attr))
            except Exception:
                pass
        out["mesh"] = m
    if "Sketch" in o.TypeId:
        sk = {}
        for key, attr in (("geometry_count", "GeometryCount"), ("constraint_count", "ConstraintCount"),
                          ("fully_constrained", "FullyConstrained"), ("open_vertices", "OpenVertices"),
                          ("axis_count", "AxisCount")):
            try:
                sk[key] = to_json(getattr(o, attr))
            except Exception:
                pass
        try:
            sk["solved"] = (o.solve() == 0)
        except Exception:
            pass
        try:
            sk["missing_point_on_point"] = to_json(o.MissingPointOnPointConstraints)
            sk["missing_vertical_horizontal"] = to_json(o.MissingVerticalHorizontalConstraints)
            sk["missing_equality"] = to_json(o.MissingLineEqualityConstraints)
        except Exception:
            pass
        out["sketch"] = sk
    try:
        out["placement"] = to_json(o.Placement)
    except Exception:
        pass
    point = args.get("point")
    if point is not None and shp is not None:
        try:
            import Part
            pv = Part.Vertex(_vec(point, "point"))
            dmin, pts, _s = shp.distToShape(pv)
            out["distance_to_point"] = {"point": to_json(_vec(point)), "distance": dmin,
                                        "closest_point": to_json(pts[0][0])}
        except Exception as e:
            out["distance_to_point_error"] = str(e)
    other = args.get("other")
    if other:
        try:
            o2 = _resolve_object(other, d)
            dist, points, _sub = shp.distToShape(o2.Shape)
            out["distance"] = {
                "to": o2.Name,
                "minimum_distance": dist,
                "point_on_this": to_json(points[0][0]),
                "point_on_other": to_json(points[0][1]),
            }
        except Exception as e:
            out["distance_error"] = str(e)
    return out


def op_selection(args):
    import FreeCADGui
    action = args.get("action", "get")
    sel = FreeCADGui.Selection
    if action == "get":
        out = []
        for o in sel.getSelection():
            out.append({"document": o.Document.Name, "object": o.Name, "label": o.Label, "type": o.TypeId})
        sub = []
        for o in sel.getSelectionEx():
            for sn in o.SubElementNames:
                sub.append({"document": o.Document.Name, "object": o.Object.Name, "subelement": sn})
        return {"objects": out, "subelements": sub}
    if action == "clear":
        sel.clearSelection()
        return {"objects": []}
    if action in ("set", "add"):
        if action == "set":
            sel.clearSelection()
        added = []
        for item in args.get("objects") or []:
            if isinstance(item, dict):
                d = _doc({"document": item.get("document")}, )
                o = _resolve_object(item.get("object"), d)
                subs = item.get("subelements") or item.get("sub")
                if subs:
                    for sn in subs:
                        sel.addSelection(o, sn)
                        added.append("%s.%s" % (o.Name, sn))
                else:
                    sel.addSelection(o)
                    added.append(o.Name)
            else:
                o = _resolve_object(item, None)
                sel.addSelection(o)
                added.append(o.Name)
        try:
            FreeCADGui.updateGui()
        except Exception:
            pass
        return {"added": added}
    if action == "remove":
        removed = []
        for item in args.get("objects") or []:
            o = _resolve_object(item.get("object") if isinstance(item, dict) else item, None)
            sel.removeSelection(o)
            removed.append(o.Name)
        return {"removed": removed}
    raise ValueError("unknown selection action %r (use get, set, add, clear, remove)" % action)


def op_view(args):
    import FreeCAD, FreeCADGui
    action = args.get("action", "get")
    vd = FreeCADGui.ActiveDocument
    if vd is None:
        raise ValueError("no active document/view; open a document first")
    view = vd.ActiveView
    simple = {"fit_all": "fitAll", "isometric": "viewIsometric", "axonometric": "viewAxonometric",
              "front": "viewFront", "top": "viewTop", "right": "viewRight", "rear": "viewRear",
              "bottom": "viewBottom", "left": "viewLeft", "dimetric": "viewDimetric",
              "trimetric": "viewTrimetric", "default_orientation": "viewDefaultOrientation",
              "zoom_in": "zoomIn", "zoom_out": "zoomOut", "redraw": "redraw",
              "rotate_left": "viewRotateLeft", "rotate_right": "viewRotateRight"}
    if action in simple:
        getattr(view, simple[action])()
        FreeCADGui.updateGui()
        return {"action": action, "camera_type": view.getCameraType()}
    if action == "get_camera":
        return {"camera_type": view.getCameraType(), "view_direction": to_json(view.getViewDirection()),
                "orientation": to_json(view.getCameraOrientation()), "navigation": view.getNavigationType()}
    if action == "set_view_direction":
        view.setViewDirection(_vec(args["direction"]))
        return {"action": action, "view_direction": to_json(view.getViewDirection())}
    if action == "set_camera_type":
        view.setCameraType(args["camera_type"])
        return {"camera_type": view.getCameraType()}
    if action == "set_axis_cross":
        view.setAxisCross(bool(args.get("visible", True)))
        return {"axis_cross": view.hasAxisCross()}
    if action == "set_navigation":
        view.setNavigationType(args["navigation"])
        return {"navigation": view.getNavigationType()}
    if action == "list_camera_types":
        return {"camera_types": list(view.listCameraTypes())}
    if action == "set_visibility":
        d = _doc(args)
        changed = {}
        for name, vis in (args.get("objects") or {}).items():
            o = _resolve_object(name, d)
            if o.ViewObject is not None:
                o.ViewObject.Visibility = bool(vis)
            changed[name] = bool(vis)
        FreeCADGui.updateGui()
        return {"visibility": changed}
    raise ValueError("unknown view action %r (use %s, get_camera, set_view_direction, set_camera_type, "
                     "set_axis_cross, set_navigation, list_camera_types, set_visibility)" % (action, ", ".join(sorted(simple))))


def op_spreadsheet(args):
    import FreeCAD
    d = _doc(args)
    sheet = _resolve_object(args["sheet"], d)
    action = args.get("action", "get_cell")
    if action == "get_cell":
        cell = args["cell"].upper()
        return {"cell": cell, "contents": sheet.getContents(cell), "value": sheet.get(cell),
                "alias": sheet.getAlias(cell) or None,
                "display_unit": (sheet.getDisplayUnit(cell) or None)}
    if action == "set_cell":
        cell = args["cell"].upper()
        value = args.get("value")
        sheet.set(cell, "" if value is None else str(value))
        d.recompute()
        return {"cell": cell, "contents": sheet.getContents(cell), "value": sheet.get(cell)}
    if action == "set_alias":
        cell = args["cell"].upper()
        alias = args.get("alias") or ""
        try:
            sheet.setAlias(cell, alias)
        except Exception as e:
            raise ValueError("FreeCAD rejected the alias %r for %s.%s (%s). Aliases must start with a "
                             "letter, be at least two characters long and must not look like a cell "
                             "address (e.g. 'hole_dia', 'thickness')." % (alias, sheet.Name, cell, e))
        return {"cell": cell, "alias": sheet.getAlias(cell) or None}
    if action == "get_range":
        rng = args["range"]
        cells = {}
        for cell in _expand_range(rng):
            c = sheet.getContents(cell)
            if c != "":
                cells[cell] = {"contents": c, "value": sheet.get(cell)}
        return {"range": rng, "cells": cells}
    if action == "set_range":
        for cell, val in (args.get("cells") or {}).items():
            sheet.set(cell.upper(), str(val))
        d.recompute()
        return {"set": len(args.get("cells") or {})}
    if action == "list_cells":
        out = {}
        for cell in _expand_range(args.get("range") or "A1:Z200"):
            c = sheet.getContents(cell)
            if c != "":
                out[cell] = {"contents": c, "value": sheet.get(cell)}
        return {"cells": out, "count": len(out)}
    if action == "clear":
        sheet.clear(args["cell"].upper())
        d.recompute()
        return {"cleared": args["cell"]}
    if action == "clear_all":
        sheet.clearAll()
        d.recompute()
        return {"cleared": "all"}
    if action == "recompute":
        sheet.recomputeCells()
        d.recompute()
        return {"recomputed": True}
    raise ValueError("unknown spreadsheet action %r" % action)


def _col_to_num(col):
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n


def _num_to_col(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _expand_range(rng):
    rng = rng.upper().replace("$", "")
    if ":" not in rng:
        return [rng]
    a, b = rng.split(":", 1)
    import re
    ma = re.match(r"([A-Z]+)(\d+)", a)
    mb = re.match(r"([A-Z]+)(\d+)", b)
    if not ma or not mb:
        return [rng]
    c1, r1 = _col_to_num(ma.group(1)), int(ma.group(2))
    c2, r2 = _col_to_num(mb.group(1)), int(mb.group(2))
    out = []
    for c in range(min(c1, c2), max(c1, c2) + 1):
        for r in range(min(r1, r2), max(r1, r2) + 1):
            out.append("%s%d" % (_num_to_col(c), r))
    return out


def _param_items(g):
    """Normalise ParamGet.GetContents() ([[type, name, value], ...]) into tuples."""
    out = []
    try:
        contents = g.GetContents()
    except Exception:
        return out
    if isinstance(contents, str) or contents is None:
        return out
    for item in contents:
        try:
            kind, name, value = item[0], item[1], item[2]
        except Exception:
            continue
        out.append((str(kind).lower(), str(name), to_json(value)))
    return out


def op_preferences(args):
    import FreeCAD
    action = args.get("action", "get")
    path = args["path"]
    if not path.startswith("User parameter:") and not path.startswith("System parameter:"):
        path = "User parameter:" + path
    g = FreeCAD.ParamGet(path)
    if action == "get":
        name = args["name"]
        items = _param_items(g)
        for kind, pname, pval in items:
            if pname == name:
                return {"path": path, "name": name, "type": kind, "value": pval}

        # fall back to an explicit type if the caller knows it
        t = args.get("type")
        if t in ("bool", "int", "float", "unsigned", "string"):
            fn = {"bool": g.GetBool, "int": g.GetInt, "float": g.GetFloat,
                  "unsigned": g.GetUnsigned, "string": g.GetString}[t]
            default = {"bool": False, "int": 0, "float": 0.0, "unsigned": 0, "string": ""}[t]
            return {"path": path, "name": name, "type": t, "value": fn(name, default)}
        return {"path": path, "name": name, "value": None,
                "note": "this parameter is not set in %s" % path}
    if action == "set":
        name = args["name"]
        value = args.get("value")
        t = args.get("type")
        if t is None:
            if isinstance(value, bool):
                t = "bool"
            elif isinstance(value, int):
                t = "int"
            elif isinstance(value, float):
                t = "float"
            else:
                t = "string"
        if t == "bool":
            g.SetBool(name, bool(value))
        elif t == "int":
            g.SetInt(name, int(value))
        elif t == "float":
            g.SetFloat(name, float(value))
        elif t == "unsigned":
            g.SetUnsigned(name, int(value))
        else:
            g.SetString(name, str(value))
        FreeCAD.saveParameter()
        out = {"path": path, "name": name, "type": t, "value": value}
        if name == "UserSchema" and path.rstrip("/").endswith("Preferences/Units"):
            # the unit scheme is cached by the application; apply it at once
            try:
                FreeCAD.Units.setSchema(int(value))
                out["applied_immediately"] = True
                out["schema"] = FreeCAD.Units.getSchema()
                out["schemas"] = list(FreeCAD.Units.listSchemas())
            except Exception as e:
                out["applied_immediately"] = False
                out["apply_error"] = str(e)
        return out
    if action == "list" and path.rstrip("/").endswith("Preferences/Units"):
        return {"path": path, "params": _param_items(g),
                "schema": FreeCAD.Units.getSchema(), "schemas": list(FreeCAD.Units.listSchemas()),
                "note": "UserSchema selects the unit scheme; freecad_get_property(unit=...) converts "
                        "any length/angle/mass property into a unit of your choice."}
    if action == "list":
        out = {}
        for sub in g.GetGroups():
            out[sub] = _param_items(g.GetGroup(sub))
        out["<this group>"] = _param_items(g)
        return {"path": path, "contents": out}
    if action == "groups":
        return {"path": path, "groups": list(g.GetGroups()), "has": g.HasGroup(args.get("name", ""))}
    if action == "remove":
        name = args["name"]
        kind = args.get("type")
        if kind is None:
            for k, pname, _v in _param_items(g):
                if pname == name:
                    kind = k
                    break
        if kind is None:
            return {"path": path, "removed": None,
                    "note": "no parameter named %r in %s" % (name, path)}
        remover = {"boolean": g.RemBool, "bool": g.RemBool, "integer": g.RemInt, "int": g.RemInt,
                   "float": g.RemFloat, "unsigned": g.RemUnsigned, "string": g.RemString}.get(kind)
        if remover is None:
            raise ValueError("cannot remove a parameter of type %r" % kind)
        remover(name)
        FreeCAD.saveParameter()
        still = [p for _k, p, _v in _param_items(g) if p == name]
        return {"path": path, "removed": name, "type": kind, "still_present": bool(still)}
    raise ValueError("unknown preferences action %r (use get, set, list, groups, remove)" % action)


def _geoms_from_spec(spec):
    """Return a list of Part geometries for one geometry description."""
    import Part, FreeCAD
    t = (spec.get("type") or "").lower()
    if t in ("rectangle", "rect"):
        c1 = _vec(spec["corner1"])
        c2 = _vec(spec["corner2"])
        pts = [FreeCAD.Vector(c1.x, c1.y, 0), FreeCAD.Vector(c2.x, c1.y, 0),
               FreeCAD.Vector(c2.x, c2.y, 0), FreeCAD.Vector(c1.x, c2.y, 0)]
        return [Part.LineSegment(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    if t in ("polyline", "polygon"):
        pts = [_vec(p) for p in spec["points"]]
        closed = bool(spec.get("close", t == "polygon"))
        segs = [Part.LineSegment(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        if closed and len(pts) > 2:
            segs.append(Part.LineSegment(pts[-1], pts[0]))
        return segs
    return [_geom_from_spec(spec)]


def _geom_from_spec(spec):
    import Part, FreeCAD
    t = (spec.get("type") or "").lower()
    if t in ("line", "linesegment"):
        return Part.LineSegment(_vec(spec["start"]), _vec(spec["end"]))
    if t == "circle":
        return Part.Circle(_vec(spec.get("center", [0, 0, 0])), _vec(spec.get("normal", [0, 0, 1])), float(spec["radius"]))
    if t == "arc":
        if "mid" in spec:
            return Part.Arc(_vec(spec["start"]), _vec(spec["mid"]), _vec(spec["end"]))
        return Part.ArcOfCircle(Part.Circle(_vec(spec.get("center", [0, 0, 0])), _vec(spec.get("normal", [0, 0, 1])), float(spec["radius"])),
                                float(spec.get("start_angle", 0)), float(spec.get("end_angle", 3.14159265358979)))
    if t == "point":
        return Part.Point(_vec(spec["point"]))
    if t in ("ellipse",):
        return Part.Ellipse(_vec(spec.get("center", [0, 0, 0])), float(spec["major_radius"]), float(spec["minor_radius"]))
    raise ValueError("unknown geometry type %r (use line, circle, arc, point, ellipse)" % spec.get("type"))


def op_sketch(args):
    import FreeCAD, Part, Sketcher
    d = _doc(args)
    sk = _resolve_object(args["sketch"], d)
    if "Sketch" not in sk.TypeId:
        raise ValueError("%s is a %s, not a sketch. Create a 'Sketcher::SketchObject' "
                         "(freecad_create_object type=Sketcher::SketchObject) or point 'sketch' at an "
                         "existing one; sketches available here: %s" %
                         (sk.Name, sk.TypeId,
                          ", ".join(o.Name for o in d.Objects if "Sketch" in o.TypeId) or "none"))
    action = args.get("action")
    if action == "add_geometry":
        geoms = args.get("geometry") or []
        if isinstance(geoms, dict):
            geoms = [geoms]
        ids = []
        auto = []
        for spec in geoms:
            group = []
            for g in _geoms_from_spec(spec):
                idx = sk.addGeometry(g, bool(spec.get("construction", False)))
                ids.append(idx)
                group.append(idx)
            if spec.get("coincident") and len(group) > 1:
                for a, b in zip(group, group[1:] + ([group[0]] if spec.get("close") or
                                                    (spec.get("type", "").lower() in ("rectangle", "rect", "polygon")) else [])):
                    try:
                        sk.addConstraint(Sketcher.Constraint("Coincident", a, 2, b, 1))
                        auto.append((a, b))
                    except Exception:
                        pass
        d.recompute()
        return {"added": ids, "geometry_count": sk.GeometryCount,
                "coincident_constraints_added": auto}
    if action == "add_constraint" and isinstance(args.get("constraints"), list):
        out = []
        for c in args["constraints"]:
            sub = dict(args)
            sub.pop("constraints", None)
            sub["constraint"] = c
            out.append(op_sketch(sub))
        return {"results": out, "constraint_count": sk.ConstraintCount}
    if action == "add_constraint":
        c = args.get("constraint") or {}
        t = c["type"]
        args_list = [t]
        if "first" in c:
            args_list.append(int(c["first"]))
        if "first_pos" in c:
            args_list.append(int(c["first_pos"]))
        if "second" in c:
            args_list.append(int(c["second"]))
        if "second_pos" in c:
            args_list.append(int(c["second_pos"]))
        if "value" in c:
            args_list.append(float(c["value"]))
        if "x" in c:
            args_list.append(float(c["x"]))
        if "y" in c:
            args_list.append(float(c["y"]))
        idx = sk.addConstraint(Sketcher.Constraint(*args_list))
        if isinstance(idx, (list, tuple)):
            idx = list(idx)
        d.recompute()
        res = {"constraint_index": idx, "constraint_count": sk.ConstraintCount,
               "fully_constrained": bool(sk.FullyConstrained)}
        if not idx and idx != 0:
            res["note"] = ("FreeCAD rejected this constraint (it was redundant, conflicting, or "
                           "referred to geometry that does not exist). Check the geometry indices "
                           "with sketch action=get.")
        return res
    if action == "set_datum":
        sk.setDatum(int(args["index"]), FreeCAD.Units.Quantity(args["value"]) if isinstance(args["value"], str) else float(args["value"]))
        d.recompute()
        return {"index": args["index"], "constraint": to_json(sk.Constraints[int(args["index"])])}
    if action == "delete_geometry":
        sk.delGeometry(int(args["index"]))
        d.recompute()
        return {"geometry_count": sk.GeometryCount}
    if action == "delete_constraint":
        sk.delConstraint(int(args["index"]))
        d.recompute()
        return {"constraint_count": sk.ConstraintCount}
    if action == "solve":
        ret = sk.solve()
        return {"solved": (ret == 0), "solve_return": ret,
                "fully_constrained": bool(sk.FullyConstrained),
                "missing_point_on_point": to_json(getattr(sk, "MissingPointOnPointConstraints", None)),
                "missing_vertical_horizontal": to_json(getattr(sk, "MissingVerticalHorizontalConstraints", None)),
                "missing_equality": to_json(getattr(sk, "MissingLineEqualityConstraints", None))}
    if action == "get":
        return {"geometry": [geom_to_json(g) for g in sk.Geometry],
                "constraints": [to_json(c) for c in sk.Constraints],
                "geometry_count": sk.GeometryCount, "constraint_count": sk.ConstraintCount,
                "fully_constrained": bool(sk.FullyConstrained)}
    if action == "clear":
        sk.deleteAllGeometry()
        sk.deleteAllConstraints()
        d.recompute()
        return {"geometry_count": sk.GeometryCount}
    if action == "toggle_construction":
        sk.toggleConstruction(int(args["index"]))
        d.recompute()
        return {"index": args["index"]}
    raise ValueError("unknown sketch action %r" % action)


def op_console(args):
    """Return what FreeCAD last printed to its report view."""
    txt = _bridge()._report_text()
    lines = [l for l in txt.splitlines() if l.strip()]
    n = int(args.get("lines") or 60)
    return {"report_view_tail": lines[-n:], "line_count": len(lines)}


def op_set_view_object(args):
    import FreeCAD, FreeCADGui
    d = _doc(args)
    o = _resolve_object(args["object"], d)
    vo = o.ViewObject
    if vo is None:
        raise ValueError("%s has no view object" % o.Name)
    results = {}
    errors = {}
    for k, v in (args.get("properties") or {}).items():
        try:
            try:
                tid = vo.getTypeIdOfProperty(k)
            except Exception:
                tid = None
            coerced = from_json(v, tid, d)
            try:
                setattr(vo, k, coerced)
            except TypeError:
                if isinstance(coerced, list):
                    setattr(vo, k, tuple(coerced))
                else:
                    raise
            results[k] = to_json(getattr(vo, k))
        except Exception as e:
            errors[k] = str(e)
    FreeCADGui.updateGui()
    return {"object": o.Name, "values": results, "errors": errors}


def op_list_display_modes(args):
    import FreeCAD
    d = _doc(args)
    o = _resolve_object(args["object"], d)
    vo = o.ViewObject
    return {"modes": list(vo.listDisplayModes()), "current": vo.DisplayMode,
            "shape_color": to_json(vo.ShapeColor), "line_color": to_json(vo.LineColor),
            "transparency": vo.Transparency, "visibility": bool(vo.Visibility),
            "draw_style": vo.DrawStyle, "point_size": vo.PointSize, "line_width": vo.LineWidth}


_DOC_PROPS = ["Label", "Comment", "Company", "License", "LicenseURL", "CreatedBy",
              "LastModifiedBy", "CreationDate", "LastModifiedDate", "Uid", "ShowHidden",
              "FileName", "TipName", "TransientDir"]


_DRAFT_ACTIONS = ["line", "wire", "circle", "rectangle", "polygon", "bspline", "bezcurve",
                  "point", "text", "shapestring", "dimension", "array", "clone", "copy",
                  "move", "rotate", "scale", "offset", "upgrade", "downgrade", "sketch",
                  "layer", "facebinder"]


def _names(objs):
    if objs is None:
        return []
    if not isinstance(objs, (list, tuple)):
        objs = [objs]
    out = []
    for o in objs:
        try:
            out.append(o.Name)
        except Exception:
            pass
    return out


def op_draft(args):
    """Draft workbench operations (the same functions its toolbar buttons call)."""
    import FreeCAD
    d = _doc(args)
    import Draft
    action = (args.get("action") or "").lower()
    prev_active = FreeCAD.ActiveDocument.Name if FreeCAD.ActiveDocument else None
    try:
        FreeCAD.setActiveDocument(d.Name)
    except Exception:
        pass
    try:
        res = None
        if action == "line":
            res = Draft.make_line(_vec(args["start"]), _vec(args["end"]))
        elif action == "wire":
            res = Draft.make_wire([_vec(p) for p in args["points"]],
                                  closed=bool(args.get("closed", False)),
                                  face=bool(args.get("face", False)))
        elif action == "circle":
            pl = _placement(args["placement"]) if args.get("placement") else None
            res = Draft.make_circle(float(args["radius"]), placement=pl,
                                    face=bool(args.get("face", False)))
        elif action == "rectangle":
            pl = _placement(args["placement"]) if args.get("placement") else None
            res = Draft.make_rectangle(float(args["length"]), float(args["width"]), placement=pl,
                                       face=bool(args.get("face", False)))
        elif action == "polygon":
            pl = _placement(args["placement"]) if args.get("placement") else None
            res = Draft.make_polygon(int(args["sides"]), float(args.get("radius", 1)),
                                     inscribed=bool(args.get("inscribed", True)), placement=pl,
                                     face=bool(args.get("face", False)))
        elif action == "bspline":
            res = Draft.make_bspline([_vec(p) for p in args["points"]],
                                     closed=bool(args.get("closed", False)))
        elif action == "bezcurve":
            res = Draft.make_bezcurve([_vec(p) for p in args["points"]],
                                      closed=bool(args.get("closed", False)))
        elif action == "point":
            res = Draft.make_point(_vec(args.get("point", [0, 0, 0])))
        elif action == "text":
            pl = _placement(args["placement"]) if args.get("placement") else None
            res = Draft.make_text(str(args["text"]), placement=pl)
        elif action == "shapestring":
            res = Draft.makeShapeString(str(args["text"]), str(args["font"]),
                                        float(args.get("size", 10)),
                                        float(args.get("tracking", 0)))
        elif action == "dimension":
            first = _vec(args["start"])
            last = _vec(args["end"])
            dimline = _vec(args["dimline"]) if args.get("dimline") else None
            res = Draft.make_dimension(first, last, dimline)
        elif action == "array":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            kind = (args.get("kind") or "ortho").lower()
            res = []
            for o in objs:
                # the array builders take one object each
                if kind in ("ortho", "orthogonal", "linear"):
                    r = Draft.make_ortho_array(o, _vec(args.get("spacing_x", [10, 0, 0])),
                                               _vec(args.get("spacing_y", [0, 10, 0])),
                                               _vec(args.get("spacing_z", [0, 0, 10])),
                                               int(args.get("count_x", 2)), int(args.get("count_y", 1)),
                                               int(args.get("count_z", 1)),
                                               use_link=bool(args.get("use_link", False)))
                elif kind in ("polar", "circular"):
                    center = _vec(args.get("center", [0, 0, 0]))
                    if args.get("radius") is not None:
                        r = Draft.make_circular_array(
                            o, float(args["radius"]),
                            float(args.get("tangent_distance", args["radius"])),
                            int(args.get("count", 4)),
                            int(args.get("symmetry", 1)),
                            _vec(args.get("axis", [0, 0, 1])), center,
                            use_link=bool(args.get("use_link", False)))
                    else:
                        r = Draft.make_polar_array(o, int(args.get("count", 4)),
                                                   float(args.get("angle", 360.0)), center,
                                                   use_link=bool(args.get("use_link", False)))
                else:
                    raise ValueError("array kind must be 'ortho' or 'polar'")
                if isinstance(r, (list, tuple)):
                    res.extend(r)
                elif r is None or r is False:
                    raise ValueError("Draft could not build the array (it returned %r); check the "
                                     "spacing/count/centre arguments" % (r,))
                else:
                    res.append(r)
        elif action in ("clone", "copy"):
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = Draft.make_clone(objs) if action == "clone" else Draft.make_copy(objs)
        elif action == "move":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = Draft.move(objs, _vec(args["vector"]), copy=bool(args.get("copy", False)))
        elif action == "rotate":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = Draft.rotate(objs, float(args["angle"]), _vec(args.get("center", [0, 0, 0])),
                               axis=_vec(args.get("axis", [0, 0, 1])),
                               copy=bool(args.get("copy", False)))
        elif action == "scale":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = Draft.scale(objs, _vec(args.get("scale", [1, 1, 1])),
                              center=_vec(args.get("center", [0, 0, 0])),
                              copy=bool(args.get("copy", False)))
        elif action == "offset":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = []
            delta = args.get("vector")
            if delta is None:
                delta = [float(args.get("distance", 1)), 0, 0]
            for o in objs:
                r = Draft.offset(o, _vec(delta), copy=bool(args.get("copy", False)),
                                 bind=bool(args.get("bind", False)))
                res.extend(r if isinstance(r, (list, tuple)) else [r])
        elif action in ("upgrade", "downgrade"):
            objs = [_resolve_object(o, d) for o in args["objects"]]
            fn = Draft.upgrade if action == "upgrade" else Draft.downgrade
            r = fn(objs, delete=bool(args.get("delete", False)))
            # these return (new objects, objects to delete)
            res = r[0] if isinstance(r, (list, tuple)) and r else r
        elif action == "sketch":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = Draft.make_sketch(objs, autoconstraints=bool(args.get("autoconstraints", False)),
                                    name=args.get("name", "Sketch"))
        elif action == "layer":
            res = Draft.make_layer(name=args.get("name", "Layer"),
                                   line_color=tuple(args.get("line_color", [0, 0, 0, 1])),
                                   shape_color=tuple(args.get("shape_color", [0, 0, 0, 1])),
                                   line_width=float(args.get("line_width", 2)))
        elif action == "facebinder":
            objs = [_resolve_object(o, d) for o in args["objects"]]
            res = Draft.make_facebinder(objs, name=args.get("name", "Facebinder"))
        else:
            raise ValueError("unknown Draft action %r; use one of: %s" %
                             (args.get("action"), ", ".join(_DRAFT_ACTIONS)))
        d.recompute()
        created = _names(res)
        if not created:
            raise ValueError("Draft's %s created no object; check the arguments and the objects' "
                             "shape (the report view above may say why)" % action)
        return {"action": action, "document": d.Name, "created": created}
    finally:
        try:
            if prev_active and prev_active in FreeCAD.listDocuments():
                FreeCAD.setActiveDocument(prev_active)
        except Exception:
            pass


def op_document_properties(args):
    """Read or write the document-level information shown in File > Project information."""
    import FreeCAD
    d = _doc(args)
    action = args.get("action", "get")
    if action == "get":
        out = {"document": d.Name}
        for p in _DOC_PROPS:
            try:
                out[p] = to_json(getattr(d, p))
            except Exception as e:
                out[p] = "<unreadable: %s>" % e
        try:
            out["Tip"] = d.Tip.Name if d.Tip else None
        except Exception:
            pass
        try:
            out["Meta"] = to_json(d.Meta)
        except Exception:
            pass
        return out
    if action == "set":
        prop = args["property"]
        if prop not in _DOC_PROPS and prop not in ("Tip", "Material"):
            raise ValueError("document property %r is not settable here; settable properties: %s" %
                             (prop, ", ".join(_DOC_PROPS + ["Tip"])))
        value = args.get("value")
        if prop == "Tip":
            o = _resolve_object(value, d)
            d.Tip = o
        else:
            setattr(d, prop, value)
        return {"document": d.Name, "property": prop, "value": to_json(getattr(d, prop))}
    raise ValueError("unknown action %r (use get or set)" % action)


def op_add_property(args):
    """Add a new dynamic property to an object (App::PropertyContainer::addProperty)."""
    import FreeCAD
    d = _doc(args)
    o = _resolve_object(args["object"], d)
    ptype = args["property_type"]
    name = args["name"]
    if name in list(o.PropertiesList):
        raise ValueError("%s already has a property %r" % (o.Name, name))
    try:
        o.addProperty(ptype, name, args.get("group") or "", args.get("doc") or "", 0)
    except Exception as e:
        raise ValueError("could not add %s %r to %s: %s" % (ptype, name, o.Name, e))
    if "value" in args and args["value"] is not None:
        _set_one(o, name, args["value"])
    return {"object": o.Name, "property": name, "type": ptype,
            "value": _prop_value(o, name), "properties": list(o.PropertiesList)}


def op_remove_property(args):
    import FreeCAD
    d = _doc(args)
    o = _resolve_object(args["object"], d)
    name = args["name"]
    if name not in list(o.PropertiesList):
        raise ValueError("%s has no property %r" % (o.Name, name))
    o.removeProperty(name)
    return {"object": o.Name, "removed": name, "properties": list(o.PropertiesList)}


def op_undo_redo(args):
    import FreeCAD
    d = _doc(args)
    action = args.get("action", "undo")
    if action == "undo":
        d.undo()
    elif action == "redo":
        d.redo()
    elif action == "status":
        return {"undo_count": d.UndoCount, "redo_count": d.RedoCount,
                "undo_names": list(d.UndoNames), "redo_names": list(d.RedoNames)}
    elif action == "clear":
        d.clearUndos()
    else:
        raise ValueError("unknown action %r (undo, redo, status, clear)" % action)
    return {"action": action, "undo_count": d.UndoCount, "redo_count": d.RedoCount,
            "objects": [o.Name for o in d.Objects]}


_MUTATING = set([
    "create_object", "set_property", "set_properties", "set_expression", "delete_object",
    "sketch", "spreadsheet", "set_view_object", "add_property", "remove_property",
    "document_properties", "draft",
])


OPS = {
    "ping": op_ping,
    "app_info": op_app_info,
    "list_workbenches": op_list_workbenches,
    "activate_workbench": op_activate_workbench,
    "list_commands": op_list_commands,
    "run_command": op_run_command,
    "list_documents": op_list_documents,
    "get_document": op_get_document,
    "get_object": op_get_object,
    "get_property": op_get_property,
    "list_object_types": op_list_object_types,
    "object_schema": op_object_schema,
    "create_object": op_create_object,
    "set_property": op_set_property,
    "set_properties": op_set_properties,
    "set_expression": op_set_expression,
    "delete_object": op_delete_object,
    "recompute": op_recompute,
    "new_document": op_new_document,
    "open_document": op_open_document,
    "save_document": op_save_document,
    "close_document": op_close_document,
    "export": op_export,
    "import_file": op_import_file,
    "measure": op_measure,
    "selection": op_selection,
    "view": op_view,
    "spreadsheet": op_spreadsheet,
    "preferences": op_preferences,
    "sketch": op_sketch,
    "console": op_console,
    "set_view_object": op_set_view_object,
    "list_display_modes": op_list_display_modes,
    "undo_redo": op_undo_redo,
    "document_properties": op_document_properties,
    "draft": op_draft,
    "add_property": op_add_property,
    "remove_property": op_remove_property,
}


def __fc_mcp_call(req_path, out_path):
    """Entry point called from the injected bootstrap; runs on the Qt main thread."""
    out = {}
    try:
        with open(req_path) as fh:
            req = json.load(fh)
        out = {"ok": True, "data": _bridge().handle(req)}
    except _OpError as e:
        out = {"ok": False, "error": e.message, "traceback": e.tb}
    except Exception as e:
        out = {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
    try:
        with open(out_path, "w") as fh:
            json.dump(_sanitize(out), fh, default=str)
    except Exception:
        pass

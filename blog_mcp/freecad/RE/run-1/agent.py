"""In-process agent injected into a running FreeCAD process.

This module is executed inside the FreeCAD process by the MCP launcher (see
server.py).  It starts a Unix-domain socket server.  Requests are JSON objects
``{"op": "...", "args": {...}}`` and are executed on the Qt main thread so that
FreeCAD's App and Gui APIs are used exactly as a user would use them.

Nothing in here is exposed directly to the solver; server.py translates MCP
tools to these operations.
"""

import sys
import os
import json
import math
import queue
import socket
import threading
import traceback

SOCKET_PATH = "/tmp/freecad_mcp_agent.sock"
AGENT_VERSION = 19

_AGENT_STARTED = False
_dispatcher = None
_queue = None


# ---------------------------------------------------------------------------
# JSON conversion helpers
# ---------------------------------------------------------------------------

def _safe_len(x):
    try:
        return len(x)
    except Exception:
        return None


_MISSING = object()


def _safe_getattr(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _boundbox_to_dict(b):
    return {
        "xmin": b.XMin, "ymin": b.YMin, "zmin": b.ZMin,
        "xmax": b.XMax, "ymax": b.YMax, "zmax": b.ZMax,
        "xlength": b.XLength, "ylength": b.YLength, "zlength": b.ZLength,
        "center": _jsonable(b.Center),
    }


def _jsonable(v, depth=0):
    """Convert a FreeCAD/Python value to JSON-friendly data."""
    if depth > 8:
        return "<max-depth>"
    import FreeCAD
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return v
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except Exception:
            return repr(v)
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in v.items()}

    try:
        if isinstance(v, FreeCAD.BoundBox):
            return {"type": "BoundBox", **_boundbox_to_dict(v)}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Vector):
            return {"type": "Vector", "x": v.x, "y": v.y, "z": v.z,
                    "length": v.Length}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Rotation):
            return {"type": "Rotation",
                    "axis": _jsonable(v.Axis, depth + 1),
                    "angle": v.Angle,
                    "quaternion": [v.Q[0], v.Q[1], v.Q[2], v.Q[3]]}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Placement):
            return {"type": "Placement",
                    "base": _jsonable(v.Base, depth + 1),
                    "rotation": _jsonable(v.Rotation, depth + 1)}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Matrix):
            return {"type": "Matrix",
                    "a11": v.A11, "a12": v.A12, "a13": v.A13, "a14": v.A14,
                    "a21": v.A21, "a22": v.A22, "a23": v.A23, "a24": v.A24,
                    "a31": v.A31, "a32": v.A32, "a33": v.A33, "a34": v.A34,
                    "a41": v.A41, "a42": v.A42, "a43": v.A43, "a44": v.A44}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Units.Quantity):
            return {"type": "Quantity", "value": v.Value,
                    "unit": str(v.Unit), "str": str(v)}
    except Exception:
        pass
    try:
        if isinstance(v, FreeCAD.Color):
            vals = list(v)
            return {"type": "Color", "r": vals[0], "g": vals[1],
                    "b": vals[2], "a": vals[3] if len(vals) > 3 else 1.0}
    except Exception:
        pass
    # Mesh (must come before Shape: mesh objects also expose Volume/Area)
    try:
        cp = _safe_getattr(v, "CountPoints", _MISSING)
        cf = _safe_getattr(v, "CountFacets", _MISSING)
        if cp is not _MISSING and cf is not _MISSING:
            return {"type": "Mesh", "points": cp, "facets": cf,
                    "volume": _safe_getattr(v, "Volume"),
                    "area": _safe_getattr(v, "Area"),
                    "boundBox": _jsonable(_safe_getattr(v, "BoundBox"), depth + 1)}
    except Exception:
        pass
    # Part.Shape / Part.Solid / Part.Face ...
    try:
        vol = _safe_getattr(v, "Volume", _MISSING)
        area = _safe_getattr(v, "Area", _MISSING)
        bbox = _safe_getattr(v, "BoundBox", _MISSING)
        if vol is not _MISSING and area is not _MISSING and bbox is not _MISSING:
            return {
                "type": "Shape",
                "shapeType": _safe_getattr(v, "ShapeType"),
                "volume": vol,
                "area": area,
                "length": _safe_getattr(v, "Length"),
                "boundBox": _jsonable(bbox, depth + 1),
                "centerOfMass": _jsonable(
                    _safe_getattr(v, "CenterOfMass") is not None
                    and _safe_getattr(v, "CenterOfMass")
                    or _safe_getattr(v, "CenterOfGravity"), depth + 1),
                "centerOfGravity": _jsonable(_safe_getattr(v, "CenterOfGravity"), depth + 1),
                "solids": _safe_len(_safe_getattr(v, "Solids", [])),
                "shells": _safe_len(_safe_getattr(v, "Shells", [])),
                "faces": _safe_len(_safe_getattr(v, "Faces", [])),
                "wires": _safe_len(_safe_getattr(v, "Wires", [])),
                "edges": _safe_len(_safe_getattr(v, "Edges", [])),
                "vertices": _safe_len(_safe_getattr(v, "Vertexes", [])),
            }
    except Exception:
        pass
    # Invalid / empty Part shapes still expose ShapeType, Edges, Vertexes.
    try:
        st = _safe_getattr(v, "ShapeType", _MISSING)
        edges = _safe_getattr(v, "Edges", _MISSING)
        verts = _safe_getattr(v, "Vertexes", _MISSING)
        if st is not _MISSING or edges is not _MISSING or verts is not _MISSING:
            is_null = None
            try:
                is_null = bool(v.isNull())
            except Exception:
                pass
            return {
                "type": "Shape",
                "shapeType": st if st is not _MISSING else None,
                "isNull": is_null,
                "boundBox": _jsonable(_safe_getattr(v, "BoundBox"), depth + 1),
                "faces": _safe_len(_safe_getattr(v, "Faces", [])),
                "edges": _safe_len(edges if edges is not _MISSING else []),
                "vertices": _safe_len(verts if verts is not _MISSING else []),
            }
    except Exception:
        pass
    try:
        if hasattr(v, "Document") and hasattr(v, "Name") and hasattr(v, "TypeId"):
            return {"type": "Link",
                    "document": v.Document.Name if v.Document else None,
                    "name": v.Name,
                    "label": _safe_getattr(v, "Label"),
                    "typeId": v.TypeId}
    except Exception:
        pass
    # Generic Part.Shape / Mesh fallbacks (invalid or base objects).
    try:
        import Part
        if isinstance(v, Part.Shape):
            is_null = None
            try:
                is_null = bool(v.isNull())
            except Exception:
                pass
            return {"type": "Shape",
                    "shapeType": _safe_getattr(v, "ShapeType"),
                    "isNull": is_null,
                    "boundBox": _jsonable(_safe_getattr(v, "BoundBox"), depth + 1),
                    "faces": _safe_len(_safe_getattr(v, "Faces", [])),
                    "edges": _safe_len(_safe_getattr(v, "Edges", [])),
                    "vertices": _safe_len(_safe_getattr(v, "Vertexes", []))}
    except Exception:
        pass
    try:
        import Mesh
        if isinstance(v, Mesh.Mesh):
            return {"type": "Mesh",
                    "points": _safe_getattr(v, "CountPoints"),
                    "facets": _safe_getattr(v, "CountFacets"),
                    "volume": _safe_getattr(v, "Volume"),
                    "area": _safe_getattr(v, "Area"),
                    "boundBox": _jsonable(_safe_getattr(v, "BoundBox"), depth + 1)}
    except Exception:
        pass
    return repr(v)


def _prop_value(type_id, value, owner=None):
    """Convert a property value using its declared FreeCAD type."""
    t = (type_id or "").lower()
    try:
        if "propertycolorlist" in t:
            return [_color_to_dict(c) for c in value]
        if "propertycolor" in t:
            return _color_to_dict(value)
        if "propertymaterial" in t:
            return _material_to_dict(value)
        if "propertylinksub" in t and "list" not in t:
            return _linksub_to_json(value)
        if "propertylinksublist" in t:
            return [_linksub_to_json(v) for v in value]
        if "propertylinklist" in t:
            return [_link_to_json(v) for v in value]
        if "propertylink" in t:
            return _link_to_json(value)
    except Exception:
        pass
    return _jsonable(value)


def _color_to_dict(c):
    try:
        vals = list(c)
    except Exception:
        vals = [c]
    while len(vals) < 4:
        vals.append(1.0)
    return {"type": "Color", "r": vals[0], "g": vals[1],
            "b": vals[2], "a": vals[3]}


def _material_to_dict(m):
    if m is None:
        return None
    out = {"type": "Material"}
    try:
        out["name"] = m.Name if hasattr(m, "Name") else None
    except Exception:
        pass
    try:
        d = m.Material
        if isinstance(d, dict):
            out["properties"] = {str(k): _jsonable(v) for k, v in d.items()}
    except Exception:
        pass
    return out


def _link_to_json(v):
    if v is None:
        return None
    try:
        return {"type": "Link",
                "document": v.Document.Name if v.Document else None,
                "name": v.Name,
                "label": getattr(v, "Label", None),
                "typeId": v.TypeId}
    except Exception:
        return _jsonable(v)


def _linksub_to_json(v):
    try:
        if isinstance(v, tuple) and len(v) >= 2:
            return {"object": _link_to_json(v[0]), "subElements": list(v[1])}
    except Exception:
        pass
    return _jsonable(v)


# ---------------------------------------------------------------------------
# Main-thread dispatcher
# ---------------------------------------------------------------------------

def _install_dispatcher():
    global _dispatcher, _queue
    from PySide2 import QtCore, QtWidgets
    app = QtWidgets.QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication instance not found")

    class _Dispatcher(QtCore.QObject):
        @QtCore.Slot()
        def process(self):
            while True:
                try:
                    job = _queue.get_nowait()
                except queue.Empty:
                    return
                fn, args, kwargs, holder = job
                try:
                    holder["result"] = fn(*args, **kwargs)
                except BaseException:
                    holder["error"] = traceback.format_exc()
                finally:
                    holder["event"].set()

    _queue = queue.Queue()
    _dispatcher = _Dispatcher()
    _dispatcher.moveToThread(app.thread())
    return _dispatcher


def run_on_main(fn, *args, **kwargs):
    """Run ``fn`` on the Qt main thread and return its result."""
    holder = {"event": threading.Event()}
    _queue.put((fn, args, kwargs, holder))
    from PySide2 import QtCore
    QtCore.QMetaObject.invokeMethod(_dispatcher, "process",
                                    QtCore.Qt.QueuedConnection)
    if not holder["event"].wait(180):
        raise TimeoutError("main thread did not answer within 180s")
    if "error" in holder:
        raise RuntimeError(holder["error"])
    return holder.get("result")


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------

def _handle_connection(conn):
    try:
        f = conn.makefile("rwb")
        while True:
            line = f.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
            except Exception:
                resp = {"ok": False, "error": "invalid JSON request"}
            else:
                resp = _dispatch_request(req)
            f.write((json.dumps(resp, default=str) + "\n").encode("utf-8"))
            f.flush()
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _dispatch_request(req):
    op = req.get("op")
    args = req.get("args") or {}
    fn = OPS.get(op)
    if fn is None:
        return {"ok": False, "error": "unknown op: %r" % (op,)}
    try:
        result = run_on_main(fn, **args)
        return {"ok": True, "result": result}
    except Exception:
        return {"ok": False, "error": traceback.format_exc()}


def _serve():
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except Exception:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    srv.listen(16)
    while True:
        try:
            conn, _ = srv.accept()
        except Exception:
            break
        t = threading.Thread(target=_handle_connection, args=(conn,), daemon=True)
        t.start()


def _probe_existing():
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(SOCKET_PATH)
        s.close()
        return True
    except Exception:
        return False


def start_agent():
    global _AGENT_STARTED
    if _AGENT_STARTED:
        return {"started": False, "reason": "already running"}
    if _probe_existing():
        _AGENT_STARTED = True
        return {"started": False, "reason": "agent socket already present"}
    _AGENT_STARTED = True
    _install_dispatcher()
    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return {"started": True, "pid": os.getpid()}


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

def _get_doc(document=None):
    import FreeCAD
    if document:
        d = FreeCAD.getDocument(document)
        if d is None:
            raise ValueError("no such document: %r" % (document,))
        return d
    d = FreeCAD.ActiveDocument
    if d is None:
        raise ValueError("no active document")
    return d


def _get_obj(name, document=None):
    d = _get_doc(document)
    o = d.getObject(name)
    if o is None:
        # fall back to label
        for cand in d.Objects:
            if cand.Label == name:
                return cand
    if o is None:
        raise ValueError("no such object %r in document %r" % (name, d.Name))
    return o


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def op_status():
    import FreeCAD
    import FreeCADGui
    docs = list(FreeCAD.listDocuments().values())
    active = FreeCAD.ActiveDocument
    try:
        wb = FreeCADGui.activeWorkbench()
        wb_name = getattr(wb, "name", lambda: None)()
        if wb_name is None:
            wb_name = type(wb).__name__
    except Exception:
        wb_name = None
    return {
        "version": FreeCAD.Version(),
        "pid": os.getpid(),
        "documents": [_doc_summary(d) for d in docs],
        "activeDocument": active.Name if active else None,
        "activeWorkbench": wb_name,
        "agentVersion": AGENT_VERSION,
    }


def _doc_summary(d):
    import FreeCAD
    try:
        modified = not d.isSaved()
    except Exception:
        try:
            modified = bool(d.isTouched())
        except Exception:
            modified = None
    return {
        "name": d.Name,
        "label": d.Label,
        "fileName": d.FileName,
        "objectCount": len(d.Objects),
        "modified": modified,
        "active": d is FreeCAD.ActiveDocument,
    }


def op_list_documents():
    import FreeCAD
    return [_doc_summary(d) for d in FreeCAD.listDocuments().values()]


def op_new_document(name=None, label=None):
    import FreeCAD
    doc = FreeCAD.newDocument(name) if name else FreeCAD.newDocument()
    if label:
        doc.Label = label
    return _doc_summary(doc)


MESH_EXTENSIONS = (".stl", ".obj", ".ply", ".amf", ".ast", ".off")


def _normalize_doc_result(doc):
    """Import.open returns a list of documents; return a single Document."""
    import FreeCAD
    if isinstance(doc, (list, tuple)):
        if FreeCAD.ActiveDocument is not None:
            return FreeCAD.ActiveDocument
        for d in reversed(doc):
            if hasattr(d, "Name"):
                return d
        raise ValueError("import produced no document")
    if doc is None:
        if FreeCAD.ActiveDocument is None:
            raise ValueError("import produced no document")
        return FreeCAD.ActiveDocument
    return doc


def op_open_document(path):
    import FreeCAD
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise ValueError("file does not exist: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".fcstd", ".fcstd1", ".fcstd2"):
        doc = FreeCAD.openDocument(path)
    elif ext in MESH_EXTENSIONS:
        import Mesh
        doc = Mesh.open(path)
    elif ext in (".dxf", ".dwg"):
        import importDXF
        doc = importDXF.open(path)
    else:
        import Import
        doc = Import.open(path)
    doc = _normalize_doc_result(doc)
    return _doc_summary(doc)


def op_save_document(document=None, path=None):
    doc = _get_doc(document)
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        doc.saveAs(path)
    else:
        if not doc.FileName:
            raise ValueError("document %r has no file path; pass 'path' to save as" % doc.Name)
        doc.save()
    return _doc_summary(doc)


def op_close_document(document=None, save=False):
    import FreeCAD
    doc = _get_doc(document)
    name = doc.Name
    if save:
        if not doc.FileName:
            raise ValueError("document %r has no file path; save it before closing" % name)
        doc.save()
    FreeCAD.closeDocument(name)
    return {"closed": name}


def op_list_objects(document=None, type=None, name_filter=None, limit=1000):
    doc = _get_doc(document)
    out = []
    limit = int(limit) if limit else 0
    for o in doc.Objects:
        if type and type.lower() not in o.TypeId.lower():
            continue
        if name_filter:
            needle = name_filter.lower()
            if needle not in o.Name.lower() and needle not in (o.Label or "").lower():
                continue
        out.append(_obj_summary(o))
        if limit and len(out) >= limit:
            break
    return out


def _obj_summary(o):
    vis = None
    try:
        vo = o.ViewObject
        if vo is not None:
            vis = getattr(vo, "Visibility", None)
    except Exception:
        pass
    return {
        "name": o.Name,
        "label": o.Label,
        "type": o.TypeId,
        "document": o.Document.Name,
        "visibility": vis,
        "state": list(getattr(o, "State", []) or []),
    }


def op_get_object(document=None, name=None, properties=None, include_view=True):
    o = _get_obj(name, document)
    props = {}
    names = properties if properties else list(o.PropertiesList)
    for p in names:
        if p not in o.PropertiesList:
            props[p] = {"error": "no such property"}
            continue
        try:
            type_id = o.getTypeIdOfProperty(p)
        except Exception:
            type_id = None
        try:
            value = _prop_value(type_id, o.getPropertyByName(p), o)
        except Exception as e:
            value = {"error": str(e)}
        try:
            group = o.getGroupOfProperty(p)
        except Exception:
            group = None
        try:
            doc = o.getDocumentationOfProperty(p)
        except Exception:
            doc = None
        props[p] = {"type": type_id, "value": value, "group": group,
                    "documentation": doc}
    expr = {}
    try:
        for item in o.ExpressionEngine:
            try:
                expr[item[0]] = item[1]
            except Exception:
                expr[str(item)] = None
    except Exception:
        pass
    result = {
        "name": o.Name,
        "label": o.Label,
        "type": o.TypeId,
        "document": o.Document.Name,
        "state": list(getattr(o, "State", []) or []),
        "properties": props,
        "expressions": expr,
    }
    if include_view:
        result["view"] = _view_props(o)
    return result


def _view_props(o):
    try:
        vo = o.ViewObject
    except Exception:
        vo = None
    if vo is None:
        return None
    out = {}
    for p in list(vo.PropertiesList):
        try:
            out[p] = {
                "type": vo.getTypeIdOfProperty(p),
                "value": _prop_value(vo.getTypeIdOfProperty(p),
                                     vo.getPropertyByName(p), o),
            }
        except Exception as e:
            out[p] = {"error": str(e)}
    return out


def op_get_property(name, property, document=None):
    o = _get_obj(name, document)
    if property not in o.PropertiesList:
        raise ValueError("no such property: %s" % property)
    type_id = o.getTypeIdOfProperty(property)
    return {
        "name": o.Name,
        "property": property,
        "type": type_id,
        "value": _prop_value(type_id, o.getPropertyByName(property), o),
    }


def op_set_property(name, property, value, document=None, recompute=True):
    o = _get_obj(name, document)
    if property not in o.PropertiesList:
        raise ValueError("no such property: %s" % property)
    type_id = o.getTypeIdOfProperty(property)
    converted = _from_json(type_id, value, o)
    setattr(o, property, converted)
    if recompute:
        try:
            o.Document.recompute()
        except Exception:
            pass
    return {
        "name": o.Name,
        "property": property,
        "type": type_id,
        "value": _prop_value(type_id, o.getPropertyByName(property), o),
    }


def op_set_properties(name, values, document=None, recompute=True):
    o = _get_obj(name, document)
    results = {}
    for prop, value in values.items():
        if prop not in o.PropertiesList:
            results[prop] = {"error": "no such property"}
            continue
        type_id = o.getTypeIdOfProperty(prop)
        try:
            setattr(o, prop, _from_json(type_id, value, o))
            results[prop] = {"type": type_id,
                             "value": _prop_value(type_id,
                                                  o.getPropertyByName(prop), o)}
        except Exception as e:
            results[prop] = {"error": str(e)}
    if recompute:
        try:
            o.Document.recompute()
        except Exception:
            pass
    return {"name": o.Name, "properties": results}


def op_set_expression(name, property, expression, document=None):
    o = _get_obj(name, document)
    o.setExpression(property, expression)
    try:
        o.Document.recompute()
    except Exception:
        pass
    state = list(getattr(o, "State", []) or [])
    if "Invalid" in state:
        raise ValueError(
            "expression was accepted but the object is Invalid; check the "
            "expression and referenced names. Object state: %s" % state)
    return {"name": o.Name, "property": property, "expression": expression,
            "state": state}


def op_get_expression(name, property, document=None):
    o = _get_obj(name, document)
    expr = None
    try:
        for item in o.ExpressionEngine:
            if item[0] == property:
                expr = item[1]
                break
    except Exception:
        pass
    if expr is None:
        try:
            expr = o.getExpression(property)
        except Exception:
            pass
    return {"name": o.Name, "property": property, "expression": expr}


def op_clear_expression(name, property, document=None):
    o = _get_obj(name, document)
    try:
        o.clearExpression(property)
    except Exception:
        o.setExpression(property, None)
    try:
        o.Document.recompute()
    except Exception:
        pass
    return {"name": o.Name, "property": property, "cleared": True}


def op_recompute(document=None):
    doc = _get_doc(document)
    doc.recompute()
    return {"document": doc.Name, "objects": [_obj_summary(o) for o in doc.Objects]}


def op_create_object(type, name=None, document=None, properties=None, label=None):
    doc = _get_doc(document)
    if name:
        o = doc.addObject(type, name)
    else:
        o = doc.addObject(type)
    if label:
        o.Label = label
    errors = {}
    if properties:
        for k, v in properties.items():
            try:
                setattr(o, k, _from_json(o.getTypeIdOfProperty(k), v, o))
            except Exception as e:
                errors[k] = str(e)
    try:
        doc.recompute()
    except Exception:
        pass
    res = _obj_summary(o)
    res["errors"] = errors
    return res


def op_delete_object(name, document=None):
    doc = _get_doc(document)
    o = _get_obj(name, document)
    doc.removeObject(name)
    doc.recompute()
    return {"deleted": name, "document": doc.Name}


def op_list_commands(filter=None):
    import FreeCADGui
    names = list(FreeCADGui.Command.listAll())
    if filter:
        needle = filter.lower()
        names = [n for n in names if needle in n.lower()]
    return sorted(names)


def op_command_info(command):
    import FreeCADGui
    c = FreeCADGui.Command.get(command)
    if c is None:
        return {"name": command, "found": False}
    info = c.getInfo()
    info["found"] = True
    try:
        info["active"] = c.isActive()
    except Exception:
        info["active"] = None
    return info


DIALOG_COMMANDS = {
    "Std_DlgMacroExecute", "Std_DlgMacroExecuteDirect", "Std_DlgMacroRecord",
    "Std_DlgCustomize", "Std_DlgParameter", "Std_DlgPreferences",
    "Std_CommandLine", "Std_About", "Std_AboutQt", "Std_AddonMgr",
    "Std_Open", "Std_Import", "Std_Export", "Std_MergeProjects",
    "Std_ProjectInfo", "Std_Print", "Std_PrintPreview", "Std_PrintPdf",
}


def op_run_command(command, index=0, allow_dialog=False):
    import FreeCADGui
    import FreeCAD
    c = FreeCADGui.Command.get(command)
    if c is None:
        raise ValueError("no such command (is its workbench loaded?): %s" % command)
    if command in DIALOG_COMMANDS and not allow_dialog:
        raise ValueError(
            "command %s normally opens a modal dialog and may block; "
            "pass allow_dialog=true if you really want to run it" % command)
    doc = FreeCAD.ActiveDocument
    before = set(o.Name for o in doc.Objects) if doc else set()
    FreeCADGui.runCommand(command, index)
    after = set(o.Name for o in doc.Objects) if doc else set()
    return {"command": command, "index": index, "ran": True,
            "document": doc.Name if doc else None,
            "newObjects": sorted(after - before),
            "removedObjects": sorted(before - after)}


def op_list_workbenches():
    import FreeCADGui
    return sorted(FreeCADGui.listWorkbenches().keys())


def op_activate_workbench(name):
    import FreeCADGui
    FreeCADGui.activateWorkbench(name)
    wb = FreeCADGui.activeWorkbench()
    return {"active": getattr(wb, "name", lambda: type(wb).__name__)()}


def op_get_selection():
    import FreeCADGui
    sel = FreeCADGui.Selection.getSelection()
    out = []
    for o in sel:
        out.append(_obj_summary(o))
    sub = []
    try:
        for s in FreeCADGui.Selection.getSelectionEx():
            item = {"object": s.ObjectName, "document": s.DocumentName,
                    "subElementNames": list(s.SubElementNames)}
            sub.append(item)
    except Exception:
        pass
    return {"objects": out, "subElements": sub}


def op_set_selection(names, document=None, clear=True):
    import FreeCADGui
    if isinstance(names, str):
        names = [names]
    if clear:
        FreeCADGui.Selection.clearSelection()
    chosen = []
    for n in names:
        o = _get_obj(n, document)
        chosen.append(o)
        FreeCADGui.Selection.addSelection(o)
    try:
        FreeCADGui.updateGui()
    except Exception:
        pass
    return {"objects": [_obj_summary(o) for o in chosen],
            "subElements": []}


def op_clear_selection():
    import FreeCADGui
    FreeCADGui.Selection.clearSelection()
    return {"cleared": True}


def op_get_view_properties(name=None, document=None):
    o = _get_obj(name, document)
    return {"name": o.Name, "view": _view_props(o)}


def op_set_view_property(name, property, value, document=None):
    o = _get_obj(name, document)
    vo = o.ViewObject
    if vo is None:
        raise ValueError("object has no view provider")
    if property not in vo.PropertiesList:
        raise ValueError("no such view property: %s" % property)
    setattr(vo, property, _from_json(vo.getTypeIdOfProperty(property), value, o))
    try:
        o.Document.recompute()
    except Exception:
        pass
    return {"name": o.Name, "property": property,
            "value": _prop_value(vo.getTypeIdOfProperty(property),
                                 vo.getPropertyByName(property), o)}


def op_view_action(action):
    view = _get_3d_view()
    action = action.lower()
    mapping = {
        "fit_all": view.fitAll,
        "fit_selection": lambda: view.fitSelection(),
        "top": view.viewTop,
        "bottom": view.viewBottom,
        "front": view.viewFront,
        "rear": view.viewRear,
        "left": view.viewLeft,
        "right": view.viewRight,
        "isometric": view.viewIsometric,
        "axonometric": view.viewAxonometric,
        "trimetric": view.viewTrimetric,
        "dimetric": view.viewDimetric,
        "perspective": lambda: view.setCameraType("Perspective"),
        "orthographic": lambda: view.setCameraType("Orthographic"),
        "zoom_in": lambda: view.zoomIn(),
        "zoom_out": lambda: view.zoomOut(),
    }
    fn = mapping.get(action)
    if fn is None:
        raise ValueError("unknown view action: %s" % action)
    fn()
    return {"action": action}


def _get_3d_view():
    """Return a 3D view, even when a TechDraw page tab is active."""
    import FreeCADGui
    v = None
    try:
        v = FreeCADGui.activeView()
    except Exception:
        v = None
    if v is not None and hasattr(v, "getCamera"):
        return v
    import FreeCAD
    for d in FreeCAD.listDocuments().values():
        try:
            gd = FreeCADGui.getDocument(d.Name)
        except Exception:
            continue
        for attr in ("ActiveView", "View"):
            try:
                vv = getattr(gd, attr)
            except Exception:
                continue
            if vv is not None and hasattr(vv, "getCamera"):
                return vv
    raise ValueError("no 3D view is available (a drawing/page view may be active)")


def op_activate_view(document=None):
    import FreeCADGui
    import FreeCAD
    if document:
        d = FreeCAD.getDocument(document)
        if d is None:
            raise ValueError("no such document: %r" % (document,))
        FreeCADGui.setActiveDocument(d.Name)
    view = _get_3d_view()
    try:
        FreeCADGui.activateView(view)
    except Exception:
        pass
    return {"activeDocument": FreeCAD.ActiveDocument.Name if FreeCAD.ActiveDocument else None,
            "view": str(view)}


def op_get_camera():
    view = _get_3d_view()
    node = None
    try:
        node = view.getCameraNode()
    except Exception:
        pass
    out = {
        "type": view.getCameraType(),
        "position": _jsonable(view.viewPosition().Base),
        "orientation": _jsonable(view.viewPosition().Rotation),
        "viewDirection": _jsonable(view.getViewDirection()),
        "raw": view.getCamera(),
    }
    if node is not None:
        for attr in ("focalDistance", "height", "nearDistance", "farDistance",
                     "aspectRatio"):
            try:
                out[attr] = getattr(node, attr).getValue()
            except Exception:
                pass
    return out


def op_set_camera(position=None, orientation=None, camera_type=None,
                  focal_distance=None):
    import FreeCAD
    view = _get_3d_view()
    if position is not None or orientation is not None:
        pl = view.viewPosition()
        base = _vec_from(position) if position is not None else pl.Base
        rot = _rot_from(orientation) if orientation is not None else pl.Rotation
        view.viewPosition(FreeCAD.Placement(base, rot))
    if camera_type is not None:
        view.setCameraType(str(camera_type))
    if focal_distance is not None:
        try:
            view.getCameraNode().focalDistance.setValue(float(focal_distance))
        except Exception:
            pass
    return op_get_camera()


def op_list_spreadsheets(document=None):
    doc = _get_doc(document)
    out = []
    for o in doc.Objects:
        if o.TypeId == "Spreadsheet::Sheet":
            out.append(_obj_summary(o))
    return out


def op_get_cell(sheet, cell, document=None):
    o = _get_obj(sheet, document)
    result = {"sheet": o.Name, "cell": cell}
    try:
        result["value"] = _jsonable(o.get(cell))
    except Exception as e:
        result["valueError"] = str(e)
    try:
        result["contents"] = o.getContents(cell)
    except Exception as e:
        result["contentsError"] = str(e)
    try:
        result["alias"] = o.getAlias(cell)
    except Exception as e:
        result["aliasError"] = str(e)
    return result


def op_set_cell(sheet, cell, value, document=None):
    o = _get_obj(sheet, document)
    o.set(cell, value)
    try:
        o.Document.recompute()
    except Exception:
        pass
    return op_get_cell(sheet, cell, document)


def op_set_alias(sheet, cell, alias, document=None):
    o = _get_obj(sheet, document)
    o.setAlias(cell, alias)
    return {"sheet": o.Name, "cell": cell, "alias": alias}


def op_export_objects(path, objects=None, document=None):
    import Import
    doc = _get_doc(document)
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    objs = []
    if objects:
        if isinstance(objects, str):
            objects = [objects]
        for n in objects:
            objs.append(_get_obj(n, doc.Name))
    else:
        objs = list(doc.Objects)
    if ext == ".fcstd":
        doc.saveAs(path)
        return {"path": path, "format": "fcstd", "objects": len(objs)}
    if ext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
        Import.export(objs, path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise ValueError("export produced no file at %s (objects may have null shapes)" % path)
        return {"path": path, "format": ext.lstrip("."), "objects": len(objs)}
    if ext in (".stl", ".obj", ".ply", ".amf", ".ast", ".off"):
        import Mesh
        Mesh.export(objs, path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise ValueError("export produced no file at %s" % path)
        return {"path": path, "format": ext.lstrip("."), "objects": len(objs)}
    if ext in (".dxf",):
        import importDXF
        importDXF.export(objs, path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise ValueError("export produced no file at %s" % path)
        return {"path": path, "format": "dxf", "objects": len(objs)}
    if ext == ".csv":
        sheets = [o for o in objs if o.TypeId == "Spreadsheet::Sheet"]
        if not sheets:
            raise ValueError("CSV export needs a Spreadsheet::Sheet object")
        sheets[0].exportFile(path)
        return {"path": path, "format": "csv", "objects": 1,
                "sheet": sheets[0].Name}
    if ext == ".svg":
        import Draft
        if len(objs) != 1:
            raise ValueError("SVG export needs exactly one object")
        svg = Draft.get_svg(objs[0])
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        return {"path": path, "format": "svg", "objects": 1}
    Import.export(objs, path)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise ValueError("export produced no file at %s (objects may have null shapes)" % path)
    return {"path": path, "format": ext.lstrip("."), "objects": len(objs)}


def op_import_file(path, document=None, merge=False):
    import FreeCAD
    import Import
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise ValueError("file does not exist: %s" % path)
    if merge:
        doc = _get_doc(document)
        ext = os.path.splitext(path)[1].lower()
        if ext in MESH_EXTENSIONS:
            import Mesh
            Mesh.insert(path, doc.Name)
        elif ext in (".dxf", ".dwg"):
            import importDXF
            importDXF.insert(path, doc.Name)
        else:
            Import.insert(path, doc.Name)
        doc.recompute()
        return {"document": doc.Name, "imported": path, "merge": True}
    ext = os.path.splitext(path)[1].lower()
    if ext in (".fcstd", ".fcstd1", ".fcstd2"):
        doc = FreeCAD.openDocument(path)
    elif ext in MESH_EXTENSIONS:
        import Mesh
        doc = Mesh.open(path)
    elif ext in (".dxf", ".dwg"):
        import importDXF
        doc = importDXF.open(path)
    else:
        doc = Import.open(path)
    doc = _normalize_doc_result(doc)
    return {"document": doc.Name, "imported": path, "merge": False}


def op_get_computed(name, document=None):
    o = _get_obj(name, document)
    result = {"name": o.Name, "type": o.TypeId}
    shape = None
    try:
        shape = getattr(o, "Shape", None)
    except Exception:
        shape = None
    if shape is not None:
        result["shape"] = _jsonable(shape)
        for attr in ("MatrixOfInertia", "StaticMoments", "PrincipalProperties",
                     "Mass", "Density", "CenterOfMass", "CenterOfGravity"):
            try:
                result[attr] = _jsonable(getattr(shape, attr))
            except Exception:
                pass
    mesh = None
    try:
        mesh = getattr(o, "Mesh", None)
    except Exception:
        mesh = None
    if mesh is not None:
        result["mesh"] = _jsonable(mesh)
    for p in ("Length", "Area", "Volume", "Radius", "Height", "Width",
              "Depth", "Angle", "Mass", "Density"):
        if p in getattr(o, "PropertiesList", []):
            try:
                result[p] = _prop_value(o.getTypeIdOfProperty(p),
                                        o.getPropertyByName(p), o)
            except Exception:
                pass
    return result


def op_get_preferences(path=None):
    import FreeCAD
    if path is None:
        path = "User parameter:BaseApp/Preferences"
    grp = FreeCAD.ParamGet(path)
    out = {"path": path, "groups": [], "entries": {}}
    try:
        out["groups"] = list(grp.GetGroups())
    except Exception:
        pass
    contents = None
    try:
        contents = grp.GetContents()
    except Exception:
        contents = None
    if contents is None:
        contents = []
    for item in contents:
        try:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                typ = item[0]
                nm = item[1]
                val = item[2] if len(item) > 2 else None
            elif isinstance(item, str):
                # Some FreeCAD versions return "Type name = value" lines.
                if "=" in item:
                    left, val = item.split("=", 1)
                    parts = left.split()
                    typ = parts[0] if parts else "String"
                    nm = parts[1] if len(parts) > 1 else left.strip()
                else:
                    continue
            else:
                continue
            out["entries"][str(nm)] = {"type": str(typ),
                                       "value": _jsonable(val)}
        except Exception:
            continue
    # Also expose the names of known-typed entries even if GetContents hid them.
    for kind, meth, getter in (("bool", "GetBools", "GetBool"),
                               ("int", "GetInts", "GetInt"),
                               ("float", "GetFloats", "GetFloat"),
                               ("string", "GetStrings", "GetString")):
        try:
            names = getattr(grp, meth)()
        except Exception:
            continue
        for nm in names:
            if str(nm) not in out["entries"]:
                try:
                    val = getattr(grp, getter)(nm)
                except Exception:
                    val = None
                out["entries"][str(nm)] = {"type": kind, "value": _jsonable(val)}
    return out


def op_get_preference(path, name, type=None):
    import FreeCAD
    grp = FreeCAD.ParamGet(path)
    if type is None:
        for t, fn in (("bool", grp.GetBool), ("int", grp.GetInt),
                      ("float", grp.GetFloat), ("string", grp.GetString)):
            try:
                return {"path": path, "name": name, "type": t,
                        "value": fn(name)}
            except Exception:
                continue
        raise ValueError("could not read preference %s/%s" % (path, name))
    fn = {"bool": grp.GetBool, "int": grp.GetInt, "float": grp.GetFloat,
          "string": grp.GetString}[type]
    return {"path": path, "name": name, "type": type, "value": fn(name)}


def op_set_preference(path, name, type, value):
    import FreeCAD
    grp = FreeCAD.ParamGet(path)
    if type == "bool":
        grp.SetBool(name, bool(value))
    elif type == "int":
        grp.SetInt(name, int(value))
    elif type == "float":
        grp.SetFloat(name, float(value))
    elif type == "string":
        grp.SetString(name, str(value))
    else:
        raise ValueError("unknown preference type: %s" % type)
    try:
        FreeCAD.saveParameter()
    except Exception:
        pass
    return op_get_preference(path, name, type)


def op_list_object_types(document=None, activate_workbenches=None):
    import FreeCAD
    if activate_workbenches:
        import FreeCADGui
        for wb in activate_workbenches:
            try:
                FreeCADGui.activateWorkbench(wb)
            except Exception:
                pass
    doc = None
    created = False
    try:
        doc = _get_doc(document)
    except Exception:
        doc = FreeCAD.newDocument("__TypeList__")
        created = True
    try:
        types = sorted(doc.supportedTypes())
    except Exception as e:
        types = []
        err = str(e)
    else:
        err = None
    if created:
        try:
            FreeCAD.closeDocument(doc.Name)
        except Exception:
            pass
    return {"types": types, "error": err,
            "note": "Type names accepted by create_object. More types appear "
                    "after activate_workbench loads their module."}



# ---------------------------------------------------------------------------
# Sketch, document and view helpers
# ---------------------------------------------------------------------------

def _vec_xy(value):
    import FreeCAD
    if isinstance(value, dict):
        return FreeCAD.Vector(float(value.get("x", 0)),
                              float(value.get("y", 0)),
                              float(value.get("z", 0)))
    if isinstance(value, (list, tuple)):
        if len(value) >= 3:
            return FreeCAD.Vector(float(value[0]), float(value[1]),
                                  float(value[2]))
        if len(value) == 2:
            return FreeCAD.Vector(float(value[0]), float(value[1]), 0.0)
    raise ValueError("cannot convert to 2D point: %r" % (value,))


def _geom_from_json(g):
    import Part
    if not isinstance(g, dict):
        raise ValueError("geometry entry must be an object, got %r" % (g,))
    t = str(g.get("type", "")).lower().replace("_", "").replace(" ", "")
    if t in ("line", "linesegment", "segment"):
        start = g.get("start") or [g.get("x1", 0), g.get("y1", 0)]
        end = g.get("end") or [g.get("x2", 0), g.get("y2", 0)]
        return Part.LineSegment(_vec_xy(start), _vec_xy(end))
    if t in ("circle",):
        center = g.get("center") or [g.get("cx", 0), g.get("cy", 0)]
        axis = g.get("axis") or [0, 0, 1]
        return Part.Circle(_vec_xy(center), _vec_xy(axis), float(g["radius"]))
    if t in ("arc", "arcofcircle", "circlearc"):
        center = g.get("center") or [g.get("cx", 0), g.get("cy", 0)]
        axis = g.get("axis") or [0, 0, 1]
        circle = Part.Circle(_vec_xy(center), _vec_xy(axis), float(g["radius"]))
        return Part.ArcOfCircle(circle, float(g.get("start_angle", 0.0)),
                                float(g.get("end_angle", 0.0)))
    if t in ("ellipse",):
        center = g.get("center") or [g.get("cx", 0), g.get("cy", 0)]
        axis = g.get("axis") or [0, 0, 1]
        return Part.Ellipse(_vec_xy(center), float(g["major_radius"]),
                            float(g["minor_radius"]),
                            float(g.get("angle_xu", 0.0)))
    if t in ("arcofellipse", "ellipsearc"):
        center = g.get("center") or [g.get("cx", 0), g.get("cy", 0)]
        axis = g.get("axis") or [0, 0, 1]
        ellipse = Part.Ellipse(_vec_xy(center), float(g["major_radius"]),
                               float(g["minor_radius"]),
                               float(g.get("angle_xu", 0.0)))
        return Part.ArcOfEllipse(ellipse, float(g.get("start_angle", 0.0)),
                                 float(g.get("end_angle", 0.0)))
    if t in ("point",):
        pt = g.get("point") or g.get("position") or [g.get("x", 0), g.get("y", 0)]
        return Part.Point(_vec_xy(pt))
    if t in ("bspline", "bsplinecurve"):
        bs = Part.BSplineCurve()
        poles = [_vec_xy(p) for p in g.get("poles", [])]
        periodic = bool(g.get("periodic", False))
        bs.buildFromPoles(poles, periodic)
        if g.get("degree") is not None:
            try:
                bs.increaseDegree(int(g["degree"]))
            except Exception:
                pass
        return bs
    raise ValueError("unsupported sketch geometry type: %r" % (g.get("type"),))


def _geom_to_json(g):
    import Part
    try:
        if isinstance(g, Part.LineSegment):
            return {"type": "line", "start": _jsonable(g.StartPoint),
                    "end": _jsonable(g.EndPoint)}
        if isinstance(g, Part.Circle):
            return {"type": "circle", "center": _jsonable(g.Center),
                    "axis": _jsonable(g.Axis), "radius": g.Radius,
                    "angle_xu": g.AngleXU}
        if isinstance(g, Part.ArcOfCircle):
            return {"type": "arc", "center": _jsonable(g.Center),
                    "axis": _jsonable(g.Axis), "radius": g.Radius,
                    "start_angle": g.FirstParameter,
                    "end_angle": g.LastParameter,
                    "start": _jsonable(g.StartPoint),
                    "end": _jsonable(g.EndPoint)}
        if isinstance(g, Part.Ellipse):
            return {"type": "ellipse", "center": _jsonable(g.Center),
                    "axis": _jsonable(g.Axis), "major_radius": g.MajorRadius,
                    "minor_radius": g.MinorRadius, "angle_xu": g.AngleXU}
        if isinstance(g, Part.ArcOfEllipse):
            return {"type": "arc_of_ellipse", "center": _jsonable(g.Center),
                    "axis": _jsonable(g.Axis), "major_radius": g.MajorRadius,
                    "minor_radius": g.MinorRadius, "angle_xu": g.AngleXU,
                    "start_angle": g.FirstParameter,
                    "end_angle": g.LastParameter,
                    "start": _jsonable(g.StartPoint),
                    "end": _jsonable(g.EndPoint)}
        if isinstance(g, Part.Point):
            return {"type": "point", "x": g.X, "y": g.Y, "z": g.Z}
        if isinstance(g, Part.BSplineCurve):
            return {"type": "bspline", "degree": g.Degree,
                    "poles": [_jsonable(p) for p in g.getPoles()],
                    "weights": list(g.getWeights()),
                    "knots": list(g.getKnots()),
                    "periodic": g.isPeriodic(),
                    "closed": g.isClosed()}
    except Exception:
        pass
    return {"type": type(g).__name__, "repr": repr(g)}


def _constraint_to_json(c):
    out = {}
    for attr, key in (("Type", "type"), ("First", "first"),
                      ("FirstPos", "firstPos"), ("Second", "second"),
                      ("SecondPos", "secondPos"), ("Third", "third"),
                      ("ThirdPos", "thirdPos"), ("Value", "value"),
                      ("Name", "name"), ("Driving", "driving"),
                      ("IsActive", "active"), ("InVirtualSpace", "virtual")):
        try:
            out[key] = getattr(c, attr)
        except Exception:
            pass
    return out


def op_sketch_add_geometry(sketch, geometry, construction=False, document=None):
    o = _get_obj(sketch, document)
    items = geometry if isinstance(geometry, list) else [geometry]
    indices = []
    for g in items:
        geo = _geom_from_json(g)
        idx = o.addGeometry(geo, bool(construction))
        indices.append(idx)
    try:
        o.solve()
    except Exception:
        pass
    return {"sketch": o.Name, "indices": indices,
            "geometry": [_geom_to_json(g) for g in o.Geometry]}


def op_sketch_get_geometry(sketch, document=None):
    o = _get_obj(sketch, document)
    try:
        o.solve()
    except Exception:
        pass
    return {"sketch": o.Name,
            "geometry": [_geom_to_json(g) for g in o.Geometry],
            "construction": [o.getConstruction(i) for i in range(len(o.Geometry))]}


def op_sketch_add_constraint(sketch, type, args, document=None):
    import Sketcher
    o = _get_obj(sketch, document)
    c = Sketcher.Constraint(type, *list(args))
    idx = o.addConstraint(c)
    try:
        o.solve()
    except Exception:
        pass
    return {"sketch": o.Name, "index": idx,
            "constraint": _constraint_to_json(c),
            "constraints": [_constraint_to_json(x) for x in o.Constraints]}


def op_sketch_get_constraints(sketch, document=None):
    o = _get_obj(sketch, document)
    try:
        o.solve()
    except Exception:
        pass
    return {"sketch": o.Name,
            "constraints": [_constraint_to_json(c) for c in o.Constraints],
            "degreesOfFreedom": getattr(o, "DegreesOfFreedom", None),
            "fullyConstrained": getattr(o, "FullyConstrained", None)}


def op_sketch_solve(sketch, document=None):
    o = _get_obj(sketch, document)
    rc = o.solve()
    return {"sketch": o.Name, "result": rc,
            "degreesOfFreedom": getattr(o, "DegreesOfFreedom", None),
            "fullyConstrained": getattr(o, "FullyConstrained", None)}


def op_sketch_delete_geometry(sketch, index, document=None):
    o = _get_obj(sketch, document)
    o.delGeometry(int(index))
    o.solve()
    return op_sketch_get_geometry(sketch, document)


def op_sketch_delete_constraint(sketch, index, document=None):
    o = _get_obj(sketch, document)
    o.delConstraint(int(index))
    o.solve()
    return op_sketch_get_constraints(sketch, document)


def op_sketch_set_datum(sketch, index, value, document=None):
    o = _get_obj(sketch, document)
    o.setDatum(int(index), _quantity_from(value))
    o.solve()
    return op_sketch_get_constraints(sketch, document)


def op_sketch_toggle_construction(sketch, index, document=None):
    o = _get_obj(sketch, document)
    o.toggleConstruction(int(index))
    return op_sketch_get_geometry(sketch, document)


def op_sketch_add_external(sketch, object, subelement, document=None):
    o = _get_obj(sketch, document)
    target = _get_obj(object, document)
    idx = o.addExternal(target.Name, subelement)
    if idx is None:
        try:
            idx = len(o.ExternalGeometry) - 1
        except Exception:
            idx = None
    return {"sketch": o.Name, "externalIndex": idx,
            "external": _jsonable(list(o.ExternalGeometry))}


def op_get_document(document=None):
    d = _get_doc(document)
    props = {}
    for p in list(d.PropertiesList):
        try:
            type_id = d.getTypeIdOfProperty(p)
        except Exception:
            type_id = None
        try:
            value = _prop_value(type_id, d.getPropertyByName(p), None)
        except Exception as e:
            value = {"error": str(e)}
        try:
            group = d.getGroupOfProperty(p)
        except Exception:
            group = None
        props[p] = {"type": type_id, "value": value, "group": group}
    try:
        modified = not d.isSaved()
    except Exception:
        modified = None
    return {
        "name": d.Name,
        "label": d.Label,
        "fileName": d.FileName,
        "modified": modified,
        "comment": getattr(d, "Comment", None),
        "createdBy": getattr(d, "CreatedBy", None),
        "lastModifiedBy": getattr(d, "LastModifiedBy", None),
        "company": getattr(d, "Company", None),
        "license": getattr(d, "License", None),
        "properties": props,
        "objects": [_obj_summary(o) for o in d.Objects],
    }


def op_set_document_property(document=None, property=None, value=None):
    d = _get_doc(document)
    if property not in d.PropertiesList:
        raise ValueError("no such document property: %s" % property)
    type_id = d.getTypeIdOfProperty(property)
    setattr(d, property, _from_json(type_id, value, d))
    return {"document": d.Name, "property": property, "type": type_id,
            "value": _prop_value(type_id, d.getPropertyByName(property), None)}


def op_undo(document=None):
    d = _get_doc(document)
    d.undo()
    return {"document": d.Name, "undoCount": d.UndoCount,
            "redoCount": d.RedoCount, "undoNames": list(d.UndoNames)}


def op_redo(document=None):
    d = _get_doc(document)
    d.redo()
    return {"document": d.Name, "undoCount": d.UndoCount,
            "redoCount": d.RedoCount, "redoNames": list(d.RedoNames)}


def op_undo_state(document=None):
    d = _get_doc(document)
    return {"document": d.Name, "undoCount": d.UndoCount,
            "redoCount": d.RedoCount, "undoNames": list(d.UndoNames),
            "redoNames": list(d.RedoNames)}


def op_get_dependencies(name, document=None):
    o = _get_obj(name, document)
    out = {"name": o.Name, "type": o.TypeId}
    try:
        out["dependsOn"] = [_obj_summary(x) for x in o.OutList]
    except Exception:
        out["dependsOn"] = []
    try:
        out["usedBy"] = [_obj_summary(x) for x in o.InList]
    except Exception:
        out["usedBy"] = []
    try:
        out["parents"] = [_obj_summary(x) for x in o.Parents]
    except Exception:
        pass
    return out


def op_save_view_image(path, width=1280, height=800, background="Current"):
    view = _get_3d_view()
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    view.saveImage(path, int(width), int(height), background)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise ValueError("saveImage did not produce a file at %s" % path)
    return {"path": path, "width": int(width), "height": int(height),
            "exists": True, "size": os.path.getsize(path)}




def op_list_properties(name, document=None, include_view=False):
    o = _get_obj(name, document)
    props = []
    for p in list(o.PropertiesList):
        try:
            type_id = o.getTypeIdOfProperty(p)
        except Exception:
            type_id = None
        try:
            group = o.getGroupOfProperty(p)
        except Exception:
            group = None
        try:
            doc = o.getDocumentationOfProperty(p)
        except Exception:
            doc = None
        props.append({"name": p, "type": type_id, "group": group,
                      "documentation": doc})
    view = None
    if include_view:
        view = []
        try:
            vo = o.ViewObject
            if vo is not None:
                for p in list(vo.PropertiesList):
                    view.append({"name": p,
                                 "type": vo.getTypeIdOfProperty(p)})
        except Exception:
            pass
    return {"name": o.Name, "type": o.TypeId, "properties": props,
            "view": view}


def op_container_add_object(container, type, name=None, properties=None,
                            label=None, document=None):
    c = _get_obj(container, document)
    if hasattr(c, "newObject"):
        o = c.newObject(type, name) if name else c.newObject(type)
    elif hasattr(c, "addObject"):
        o = c.addObject(type, name) if name else c.addObject(type)
    else:
        raise ValueError("object %r is not a container" % (container,))
    if label:
        o.Label = label
    errors = {}
    if properties:
        for k, v in properties.items():
            try:
                setattr(o, k, _from_json(o.getTypeIdOfProperty(k), v, o))
            except Exception as e:
                errors[k] = str(e)
    try:
        c.Document.recompute()
    except Exception:
        pass
    res = _obj_summary(o)
    res["errors"] = errors
    return res


def op_part_boolean(operation, base, tool, name=None, others=None,
                    document=None):
    import FreeCAD
    doc = _get_doc(document)
    op = operation.lower()
    type_map = {
        "cut": "Part::Cut",
        "fuse": "Part::Fuse",
        "common": "Part::Common",
        "compound": "Part::Compound",
        "multi_fuse": "Part::MultiFuse",
        "multifuse": "Part::MultiFuse",
        "multi_common": "Part::MultiCommon",
        "multicommon": "Part::MultiCommon",
    }
    type_id = type_map.get(op)
    if type_id is None:
        raise ValueError("unknown boolean operation: %s" % operation)
    o = doc.addObject(type_id, name) if name else doc.addObject(type_id)
    base_obj = _get_obj(base, doc.Name)
    tool_obj = _get_obj(tool, doc.Name)
    if type_id in ("Part::MultiFuse", "Part::MultiCommon"):
        shapes = [base_obj, tool_obj]
        for n in (others or []):
            shapes.append(_get_obj(n, doc.Name))
        o.Shapes = shapes
    else:
        o.Base = base_obj
        o.Tool = tool_obj
    doc.recompute()
    res = _obj_summary(o)
    res["computed"] = op_get_computed(o.Name, doc.Name)
    return res



def op_spreadsheet_export(sheet, path, document=None):
    o = _get_obj(sheet, document)
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    o.exportFile(path)
    return {"sheet": o.Name, "path": path,
            "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else None}


def op_spreadsheet_import(sheet, path, document=None):
    o = _get_obj(sheet, document)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise ValueError("file does not exist: %s" % path)
    o.importFile(path)
    try:
        o.Document.recompute()
    except Exception:
        pass
    return {"sheet": o.Name, "imported": path}



def op_list_unit_schemas():
    import FreeCAD
    return {"schemas": list(FreeCAD.Units.listSchemas()),
            "currentIndex": FreeCAD.Units.getSchema()}


def op_set_unit_schema(schema):
    import FreeCAD
    schemas = list(FreeCAD.Units.listSchemas())
    if isinstance(schema, str):
        idx = None
        for i, name in enumerate(schemas):
            if schema.lower() in str(name).lower():
                idx = i
                break
        if idx is None:
            raise ValueError("no unit schema matches %r" % (schema,))
        schema = idx
    FreeCAD.Units.setSchema(int(schema))
    return {"currentIndex": FreeCAD.Units.getSchema(),
            "currentName": schemas[FreeCAD.Units.getSchema()],
            "schemas": schemas}



def op_techdraw_create_page(name=None, template=None, document=None):
    doc = _get_doc(document)
    page = doc.addObject("TechDraw::DrawPage", name) if name else doc.addObject("TechDraw::DrawPage")
    tpl_path = template or "/usr/share/freecad/Mod/TechDraw/Templates/A4_LandscapeTD.svg"
    tpl = doc.addObject("TechDraw::DrawSVGTemplate", page.Name + "_Template")
    tpl.Template = tpl_path
    page.Template = tpl
    doc.recompute()
    return {"page": _obj_summary(page), "template": _obj_summary(tpl),
            "templatePath": tpl_path}


def op_techdraw_add_view(page, objects, name=None, properties=None,
                         document=None):
    doc = _get_doc(document)
    page_obj = _get_obj(page, doc.Name)
    if isinstance(objects, str):
        objects = [objects]
    view = doc.addObject("TechDraw::DrawViewPart", name) if name else doc.addObject("TechDraw::DrawViewPart")
    view.Source = [_get_obj(n, doc.Name) for n in objects]
    errors = {}
    if properties:
        for k, v in properties.items():
            try:
                setattr(view, k, _from_json(view.getTypeIdOfProperty(k), v, view))
            except Exception as e:
                errors[k] = str(e)
    try:
        page_obj.Views = list(page_obj.Views) + [view]
    except Exception:
        pass
    doc.recompute()
    res = _obj_summary(view)
    res["errors"] = errors
    return res


def op_techdraw_export(page, path, document=None):
    import TechDrawGui
    page_obj = _get_obj(page, document)
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        TechDrawGui.exportPageAsPdf(page_obj, path)
    elif ext == ".svg":
        TechDrawGui.exportPageAsSvg(page_obj, path)
    else:
        raise ValueError("TechDraw export supports .pdf and .svg, got %s" % ext)
    return {"page": page_obj.Name, "path": path, "format": ext.lstrip("."),
            "exists": os.path.exists(path),
            "size": os.path.getsize(path) if os.path.exists(path) else None}


def op_techdraw_list_pages(document=None):
    doc = _get_doc(document)
    return [_obj_summary(o) for o in doc.Objects
            if o.TypeId == "TechDraw::DrawPage"]



def _draft_param(v):
    import FreeCAD
    if isinstance(v, dict) and "x" in v and "y" in v:
        return FreeCAD.Vector(float(v.get("x", 0)), float(v.get("y", 0)),
                              float(v.get("z", 0)))
    if isinstance(v, (list, tuple)) and len(v) in (2, 3) and all(
            isinstance(x, (int, float)) for x in v):
        if len(v) == 2:
            return FreeCAD.Vector(float(v[0]), float(v[1]), 0.0)
        return FreeCAD.Vector(float(v[0]), float(v[1]), float(v[2]))
    return v


def _draft_result(result):
    if result is None:
        return None
    if isinstance(result, (list, tuple)):
        return [_obj_summary(o) if hasattr(o, "TypeId") else _jsonable(o)
                for o in result]
    if hasattr(result, "TypeId"):
        return _obj_summary(result)
    return _jsonable(result)


def op_draft_help(kind=None):
    import Draft
    if kind:
        fn = getattr(Draft, "make_" + kind, None) or getattr(Draft, kind, None)
        if fn is None:
            raise ValueError("unknown Draft helper: %s" % kind)
        return {"name": kind, "doc": (fn.__doc__ or "")}
    names = sorted(n for n in dir(Draft) if n.startswith("make_"))
    actions = sorted(n for n in ("move", "rotate", "scale", "offset", "mirror",
                                 "clone", "array", "upgrade", "downgrade",
                                 "split", "extrude", "draftify", "heal")
                     if hasattr(Draft, n))
    return {"helpers": names, "actions": actions}


def op_draft_make(kind, args=None, params=None, document=None):
    import Draft
    import FreeCAD
    doc = _get_doc(document)
    FreeCAD.setActiveDocument(doc.Name)
    fn = getattr(Draft, "make_" + kind, None) or getattr(Draft, kind, None)
    if fn is None:
        raise ValueError("unknown Draft make kind: %s" % kind)
    positional = [_draft_param(v) for v in (args or [])]
    kwargs = {k: _draft_param(v) for k, v in (params or {}).items()}
    result = fn(*positional, **kwargs)
    doc.recompute()
    return _draft_result(result)


def op_draft_modify(action, objects, args=None, params=None, document=None):
    import Draft
    import FreeCAD
    doc = _get_doc(document)
    FreeCAD.setActiveDocument(doc.Name)
    if isinstance(objects, str):
        objects = [objects]
    objs = [_get_obj(n, doc.Name) for n in objects]
    fn = getattr(Draft, action, None)
    if fn is None:
        raise ValueError("unknown Draft action: %s" % action)
    positional = [_draft_param(v) for v in (args or [])]
    kwargs = {k: _draft_param(v) for k, v in (params or {}).items()}
    try:
        result = fn(objs, *positional, **kwargs)
    except TypeError:
        result = fn(objs[0], *positional, **kwargs) if objs else fn(*positional, **kwargs)
    doc.recompute()
    return _draft_result(result)


def op_mesh_boolean(operation, base, tool, name=None, document=None):
    doc = _get_doc(document)
    b = _get_obj(base, doc.Name)
    t = _get_obj(tool, doc.Name)
    bm = getattr(b, "Mesh", None)
    tm = getattr(t, "Mesh", None)
    if bm is None or tm is None:
        raise ValueError("both objects must have a Mesh property")
    try:
        bm = bm.copy()
    except Exception:
        pass
    op = operation.lower()
    if op in ("union", "unite", "fuse"):
        res = bm.unite(tm)
    elif op in ("cut", "difference", "subtract"):
        res = bm.difference(tm)
    elif op in ("intersection", "intersect", "common"):
        res = bm.intersect(tm)
    else:
        raise ValueError("unknown mesh operation: %s" % operation)
    o = doc.addObject("Mesh::Feature", name) if name else doc.addObject("Mesh::Feature")
    o.Mesh = res
    doc.recompute()
    return _obj_summary(o)



def _find_text_widget(object_name, plain=False):
    import FreeCADGui
    from PySide2 import QtWidgets
    mw = FreeCADGui.getMainWindow()
    cls = QtWidgets.QPlainTextEdit if plain else QtWidgets.QTextEdit
    for w in mw.findChildren(cls):
        if w.objectName() == object_name:
            return w
    return None


def op_get_report_view(lines=200):
    w = _find_text_widget("Report view", plain=False)
    if w is None:
        raise ValueError("Report view widget not found")
    text = w.toPlainText()
    all_lines = text.splitlines()
    tail = all_lines[-int(lines):] if lines else all_lines
    return {"lines": len(all_lines), "text": "\n".join(tail)}


def op_get_python_console(lines=200):
    w = _find_text_widget("Python console", plain=True)
    if w is None:
        raise ValueError("Python console widget not found")
    text = w.toPlainText()
    all_lines = text.splitlines()
    tail = all_lines[-int(lines):] if lines else all_lines
    return {"lines": len(all_lines), "text": "\n".join(tail)}


def op_list_sub_elements(name, document=None, limit=100,
                         include_faces=True, include_edges=True,
                         include_vertices=True):
    o = _get_obj(name, document)
    result = {"name": o.Name, "type": o.TypeId, "limit": int(limit)}
    shape = getattr(o, "Shape", None)
    if shape is not None:
        try:
            all_faces = list(shape.Faces)
            all_edges = list(shape.Edges)
            all_vertices = list(getattr(shape, "Vertexes", []))
            result["faceCount"] = len(all_faces)
            result["edgeCount"] = len(all_edges)
            result["vertexCount"] = len(all_vertices)
            result["truncated"] = False
            if include_faces:
                faces = []
                for i, f in enumerate(all_faces[:int(limit)]):
                    item = {"name": "Face%d" % (i + 1),
                            "area": getattr(f, "Area", None),
                            "centerOfMass": _jsonable(getattr(f, "CenterOfMass", None)),
                            "boundBox": _jsonable(getattr(f, "BoundBox", None))}
                    try:
                        item["surfaceType"] = f.Surface.__class__.__name__
                    except Exception:
                        pass
                    faces.append(item)
                result["faces"] = faces
                if len(all_faces) > int(limit):
                    result["truncated"] = True
            if include_edges:
                edges = []
                for i, e in enumerate(all_edges[:int(limit)]):
                    item = {"name": "Edge%d" % (i + 1),
                            "length": getattr(e, "Length", None),
                            "centerOfMass": _jsonable(getattr(e, "CenterOfMass", None)),
                            "vertexes": [_jsonable(p.Point) for p in getattr(e, "Vertexes", [])]}
                    try:
                        item["curveType"] = e.Curve.__class__.__name__
                    except Exception:
                        pass
                    edges.append(item)
                result["edges"] = edges
                if len(all_edges) > int(limit):
                    result["truncated"] = True
            if include_vertices:
                vertices = []
                for i, v in enumerate(all_vertices[:int(limit)]):
                    vertices.append({"name": "Vertex%d" % (i + 1),
                                     "point": _jsonable(v.Point)})
                result["vertices"] = vertices
                if len(all_vertices) > int(limit):
                    result["truncated"] = True
        except Exception as e:
            result["shapeError"] = str(e)
    mesh = getattr(o, "Mesh", None)
    if mesh is not None:
        try:
            result["mesh"] = {"points": mesh.CountPoints,
                              "facets": mesh.CountFacets,
                              "boundBox": _jsonable(mesh.BoundBox)}
        except Exception as e:
            result["meshError"] = str(e)
    return result


def op_begin_transaction(label=None, document=None):
    d = _get_doc(document)
    d.openTransaction(label or "MCP edit")
    return {"document": d.Name, "transacting": getattr(d, "Transacting", None)}


def op_commit_transaction(document=None):
    d = _get_doc(document)
    d.commitTransaction()
    return {"document": d.Name, "transacting": getattr(d, "Transacting", None),
            "undoCount": getattr(d, "UndoCount", None)}


def op_abort_transaction(document=None):
    d = _get_doc(document)
    d.abortTransaction()
    return {"document": d.Name, "transacting": getattr(d, "Transacting", None)}



def op_measure_distance(name1, name2, document=None):
    o1 = _get_obj(name1, document)
    o2 = _get_obj(name2, document)
    s1 = getattr(o1, "Shape", None)
    s2 = getattr(o2, "Shape", None)
    if s1 is None or s2 is None:
        raise ValueError("both objects must have a Shape")
    res = s1.distToShape(s2)
    return {"object1": o1.Name, "object2": o2.Name,
            "distance": res[0],
            "points": _jsonable(res[1]),
            "supportFaces": _jsonable(res[2]) if len(res) > 2 else None}



def op_sketch_rename_constraint(sketch, index, name, document=None):
    o = _get_obj(sketch, document)
    o.renameConstraint(int(index), name)
    return op_sketch_get_constraints(sketch, document)



def op_arch_help(kind=None):
    import Arch
    if kind:
        name = kind
        if not name.startswith("make"):
            name = "make" + name[0].upper() + name[1:]
        fn = getattr(Arch, name, None) or getattr(Arch, kind, None)
        if fn is None:
            raise ValueError("unknown Arch helper: %s" % kind)
        return {"name": name, "doc": (fn.__doc__ or "")}
    return {"helpers": sorted(n for n in dir(Arch) if n.startswith("make"))}


def op_arch_make(kind, args=None, params=None, document=None):
    import Arch
    import FreeCAD
    doc = _get_doc(document)
    FreeCAD.setActiveDocument(doc.Name)
    name = kind
    if not name.startswith("make"):
        name = "make" + name[0].upper() + name[1:]
    fn = getattr(Arch, name, None)
    if fn is None:
        raise ValueError("unknown Arch make helper: %s" % kind)
    positional = [_draft_param(v) for v in (args or [])]
    kwargs = {k: _draft_param(v) for k, v in (params or {}).items()}
    result = fn(*positional, **kwargs)
    doc.recompute()
    return _draft_result(result)


def op_arch_modify(action, objects, args=None, params=None, document=None):
    import Arch
    import FreeCAD
    doc = _get_doc(document)
    FreeCAD.setActiveDocument(doc.Name)
    if isinstance(objects, str):
        objects = [objects]
    objs = [_get_obj(n, doc.Name) for n in objects]
    fn = getattr(Arch, action, None)
    if fn is None:
        raise ValueError("unknown Arch action: %s" % action)
    positional = [_draft_param(v) for v in (args or [])]
    kwargs = {k: _draft_param(v) for k, v in (params or {}).items()}
    try:
        result = fn(objs, *positional, **kwargs)
    except TypeError:
        result = fn(objs[0], *positional, **kwargs) if objs else fn(*positional, **kwargs)
    doc.recompute()
    return _draft_result(result)


OPS = {
    "status": op_status,
    "list_documents": op_list_documents,
    "new_document": op_new_document,
    "open_document": op_open_document,
    "save_document": op_save_document,
    "close_document": op_close_document,
    "list_objects": op_list_objects,
    "get_object": op_get_object,
    "get_property": op_get_property,
    "set_property": op_set_property,
    "set_properties": op_set_properties,
    "set_expression": op_set_expression,
    "get_expression": op_get_expression,
    "clear_expression": op_clear_expression,
    "recompute": op_recompute,
    "create_object": op_create_object,
    "delete_object": op_delete_object,
    "list_commands": op_list_commands,
    "command_info": op_command_info,
    "run_command": op_run_command,
    "list_workbenches": op_list_workbenches,
    "activate_workbench": op_activate_workbench,
    "get_selection": op_get_selection,
    "set_selection": op_set_selection,
    "clear_selection": op_clear_selection,
    "get_view_properties": op_get_view_properties,
    "set_view_property": op_set_view_property,
    "view_action": op_view_action,
    "get_camera": op_get_camera,
    "set_camera": op_set_camera,
    "list_spreadsheets": op_list_spreadsheets,
    "get_cell": op_get_cell,
    "set_cell": op_set_cell,
    "set_alias": op_set_alias,
    "export_objects": op_export_objects,
    "import_file": op_import_file,
    "get_computed": op_get_computed,
    "get_preferences": op_get_preferences,
    "get_preference": op_get_preference,
    "set_preference": op_set_preference,
    "list_object_types": op_list_object_types,
    "sketch_add_geometry": op_sketch_add_geometry,
    "sketch_get_geometry": op_sketch_get_geometry,
    "sketch_add_constraint": op_sketch_add_constraint,
    "sketch_get_constraints": op_sketch_get_constraints,
    "sketch_solve": op_sketch_solve,
    "sketch_delete_geometry": op_sketch_delete_geometry,
    "sketch_delete_constraint": op_sketch_delete_constraint,
    "sketch_set_datum": op_sketch_set_datum,
    "sketch_toggle_construction": op_sketch_toggle_construction,
    "sketch_add_external": op_sketch_add_external,
    "sketch_rename_constraint": op_sketch_rename_constraint,
    "get_document": op_get_document,
    "set_document_property": op_set_document_property,
    "undo": op_undo,
    "redo": op_redo,
    "undo_state": op_undo_state,
    "get_dependencies": op_get_dependencies,
    "save_view_image": op_save_view_image,
    "list_properties": op_list_properties,
    "container_add_object": op_container_add_object,
    "part_boolean": op_part_boolean,
    "spreadsheet_export": op_spreadsheet_export,
    "spreadsheet_import": op_spreadsheet_import,
    "list_unit_schemas": op_list_unit_schemas,
    "set_unit_schema": op_set_unit_schema,
    "techdraw_create_page": op_techdraw_create_page,
    "techdraw_add_view": op_techdraw_add_view,
    "techdraw_export": op_techdraw_export,
    "techdraw_list_pages": op_techdraw_list_pages,
    "draft_help": op_draft_help,
    "draft_make": op_draft_make,
    "draft_modify": op_draft_modify,
    "mesh_boolean": op_mesh_boolean,
    "get_report_view": op_get_report_view,
    "get_python_console": op_get_python_console,
    "list_sub_elements": op_list_sub_elements,
    "begin_transaction": op_begin_transaction,
    "commit_transaction": op_commit_transaction,
    "abort_transaction": op_abort_transaction,
    "activate_view": op_activate_view,
    "measure_distance": op_measure_distance,
    "arch_help": op_arch_help,
    "arch_make": op_arch_make,
    "arch_modify": op_arch_modify,
}


# ---------------------------------------------------------------------------
# Value conversion (JSON -> FreeCAD)
# ---------------------------------------------------------------------------

def _vec_from(value):
    import FreeCAD
    if isinstance(value, dict):
        if value.get("type") == "Vector" or "x" in value:
            return FreeCAD.Vector(float(value.get("x", 0)),
                                  float(value.get("y", 0)),
                                  float(value.get("z", 0)))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return FreeCAD.Vector(float(value[0]), float(value[1]), float(value[2]))
    raise ValueError("cannot convert to Vector: %r" % (value,))


def _rot_from(value):
    import FreeCAD
    if isinstance(value, dict):
        if "quaternion" in value:
            q = value["quaternion"]
            return FreeCAD.Rotation(float(q[0]), float(q[1]), float(q[2]),
                                    float(q[3]))
        if "axis" in value or "angle" in value:
            ax = value.get("axis", {"x": 0, "y": 0, "z": 1})
            return FreeCAD.Rotation(_vec_from(ax), float(value.get("angle", 0)))
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return FreeCAD.Rotation(*[float(x) for x in value])
    raise ValueError("cannot convert to Rotation: %r" % (value,))


def _placement_from(value):
    import FreeCAD
    if isinstance(value, dict):
        base = _vec_from(value.get("base", value.get("Base", {"x": 0, "y": 0, "z": 0})))
        rot = _rot_from(value.get("rotation", value.get("Rotation", {"axis": {"x": 0, "y": 0, "z": 1}, "angle": 0})))
        return FreeCAD.Placement(base, rot)
    if isinstance(value, (list, tuple)) and len(value) == 7:
        return FreeCAD.Placement(FreeCAD.Vector(*value[:3]),
                                 FreeCAD.Rotation(*value[3:]))
    raise ValueError("cannot convert to Placement: %r" % (value,))


def _quantity_from(value):
    import FreeCAD
    if isinstance(value, dict):
        if "str" in value:
            return FreeCAD.Units.Quantity(value["str"])
        return FreeCAD.Units.Quantity(float(value.get("value", 0)),
                                      getattr(FreeCAD.Units, value.get("unit", "Length"), None))
    if isinstance(value, str):
        return FreeCAD.Units.Quantity(value)
    return FreeCAD.Units.Quantity(float(value))


def _color_from(value):
    if isinstance(value, dict):
        r = float(value.get("r", 0)); g = float(value.get("g", 0))
        b = float(value.get("b", 0)); a = float(value.get("a", 1))
        return (r, g, b, a)
    if isinstance(value, (list, tuple)):
        return tuple(float(x) for x in value)
    raise ValueError("cannot convert to Color: %r" % (value,))


def _link_from(value, owner):
    doc = owner.Document
    if value is None:
        return None
    if isinstance(value, dict):
        name = value.get("name") or value.get("Name")
    else:
        name = value
    if name is None:
        return None
    o = doc.getObject(name)
    if o is None:
        for cand in doc.Objects:
            if cand.Label == name:
                return cand
    if o is None:
        raise ValueError("no such object: %s" % name)
    return o


def _xlink_from(value, owner):
    import FreeCAD
    if value is None:
        return None
    docname = None
    name = None
    if isinstance(value, dict):
        name = value.get("name") or value.get("Name")
        docname = value.get("document") or value.get("Document")
    else:
        text = str(value)
        if "#" in text:
            docname, name = text.split("#", 1)
        else:
            name = text
    if not name:
        return None
    if docname:
        doc = FreeCAD.getDocument(docname)
    else:
        doc = owner.Document
    if doc is None:
        raise ValueError("no such document: %s" % docname)
    o = doc.getObject(name)
    if o is None:
        for cand in doc.Objects:
            if cand.Label == name:
                o = cand
                break
    if o is None:
        raise ValueError("no such object: %s" % name)
    return o


def _from_json(type_id, value, owner):
    import FreeCAD
    t = (type_id or "").lower()
    if value is None:
        return None
    if "propertylength" in t or "propertydistance" in t or "propertyangle" in t \
            or "propertyquantity" in t or "propertyposition" in t:
        return _quantity_from(value)
    if "propertyfloat" in t:
        return float(value)
    if "propertyinteger" in t:
        return int(value)
    if "propertybool" in t:
        return bool(value)
    if "propertymap" in t:
        return dict(value) if isinstance(value, dict) else value
    if "propertystring" in t or "propertyfile" in t or "propertyenumeration" in t:
        return str(value)
    if "propertyvector" in t and "list" not in t:
        return _vec_from(value)
    if "propertyvectorlist" in t:
        return [_vec_from(v) for v in value]
    if "propertyplacement" in t:
        return _placement_from(value)
    if "propertyrotation" in t:
        return _rot_from(value)
    if "propertymatrix" in t:
        if isinstance(value, dict):
            return FreeCAD.Matrix(*[value[k] for k in (
                "a11", "a12", "a13", "a14", "a21", "a22", "a23", "a24",
                "a31", "a32", "a33", "a34", "a41", "a42", "a43", "a44")])
        return FreeCAD.Matrix(*value)
    if "propertycolorlist" in t:
        return [_color_from(v) for v in value]
    if "propertycolor" in t:
        return _color_from(value)
    if "propertylinksub" in t and "list" not in t:
        if isinstance(value, dict):
            name = value.get("name") or value.get("Name") or value.get("object")
            subs = value.get("subElements") or value.get("SubElements") or []
        else:
            name = value
            subs = []
        return (_link_from(name, owner), list(subs))
    if "propertylinksublist" in t:
        if isinstance(value, dict):
            value = [value]
        out = []
        for v in value:
            if isinstance(v, dict):
                name = v.get("name") or v.get("Name") or v.get("object")
                subs = v.get("subElements") or v.get("SubElements") or []
            else:
                name = v
                subs = []
            out.append((_link_from(name, owner), list(subs)))
        return out
    if "propertyxlink" in t and "list" not in t and "sub" not in t:
        return _xlink_from(value, owner)
    if "propertyxlinklist" in t:
        if isinstance(value, dict):
            value = [value]
        return [_xlink_from(v, owner) for v in value]
    if "propertyxlinksub" in t and "list" not in t:
        if isinstance(value, dict):
            name = value.get("name") or value.get("Name") or value.get("object")
            subs = value.get("subElements") or value.get("SubElements") or []
        else:
            name = value
            subs = []
        return (_xlink_from(name, owner), list(subs))
    if "propertyxlinksublist" in t:
        if isinstance(value, dict):
            value = [value]
        out = []
        for v in value:
            if isinstance(v, dict):
                name = v.get("name") or v.get("Name") or v.get("object")
                subs = v.get("subElements") or v.get("SubElements") or []
            else:
                name = v
                subs = []
            out.append((_xlink_from(name, owner), list(subs)))
        return out
    if "propertylinklist" in t:
        return [_link_from(v, owner) for v in value]
    if "propertylink" in t:
        return _link_from(value, owner)
    return value


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _main():
    return start_agent()


if __name__ == "__main__":
    _main()

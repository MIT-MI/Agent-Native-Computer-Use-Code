#!/usr/bin/env python3
"""MCP server for the running FreeCAD application.

The server speaks MCP JSON-RPC on stdin/stdout.  It talks to an in-process
agent that it injects into FreeCAD through Frida (see agent.py).  All FreeCAD
operations are executed on FreeCAD's Qt main thread.

The tool arguments describe application data and operations (documents,
objects, properties, commands, preferences, files); no Python source or command
line is accepted from the solver.
"""

import glob
import json
import os
import socket
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DEPS = os.path.join(HERE, "deps")
for p in (DEPS, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

SOCKET_PATH = "/tmp/freecad_mcp_agent.sock"
AGENT_PATH = os.path.join(HERE, "agent.py")
SERVER_VERSION = "1.0.0"


def _read_agent_version():
    try:
        with open(AGENT_PATH, "r") as f:
            for line in f:
                if line.startswith("AGENT_VERSION"):
                    return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return None


EXPECTED_AGENT_VERSION = _read_agent_version()

_agent_ready = False


def log(msg):
    try:
        sys.stderr.write("[freecad-mcp] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FreeCAD process discovery and agent injection
# ---------------------------------------------------------------------------

def _proc_start_time(pid):
    try:
        with open("/proc/%d/stat" % pid, "r") as f:
            data = f.read()
        rp = data.rfind(")")
        fields = data[rp + 2:].split()
        return int(fields[19])
    except Exception:
        return 0


def find_freecad_pids():
    pids = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(d))
        except Exception:
            continue
        if pid == os.getpid():
            continue
        exe = ""
        cmd = ""
        try:
            exe = os.readlink(os.path.join(d, "exe"))
        except Exception:
            pass
        try:
            with open(os.path.join(d, "cmdline"), "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            pass
        low_exe = exe.lower()
        low_cmd = cmd.lower()
        base = os.path.basename(low_exe)
        # Only the actual FreeCAD binary, not wrappers such as cua-app or
        # python processes whose command line merely mentions FreeCAD.
        if base in ("freecad-python3", "freecad", "freecadcmd"):
            if "frida" in low_cmd or "gdb" in low_cmd:
                continue
            pids.append(pid)
    # Newest process first: after a restart the old process may still be
    # shutting down, and we must attach to the current one.
    pids.sort(key=_proc_start_time, reverse=True)
    return pids


def _readlink(pid):
    try:
        return os.readlink("/proc/%d/exe" % pid)
    except Exception:
        return ""


def _socket_status():
    """Return the agent status dict, or None if no agent answers."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(SOCKET_PATH)
        s.sendall(b'{"op":"status","args":{}}\n')
        f = s.makefile("rb")
        line = f.readline()
        s.close()
        if not line:
            return None
        resp = json.loads(line.decode("utf-8"))
        if resp.get("ok"):
            return resp.get("result")
        return None
    except Exception:
        return None


def _probe_socket():
    return _socket_status() is not None


def _start_freecad():
    log("FreeCAD is not running; trying cua-app start")
    try:
        subprocess.run(["cua-app", "start"], capture_output=True, timeout=60)
    except Exception as e:
        log("cua-app start failed: %s" % e)
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        pids = find_freecad_pids()
        if pids:
            last = pids[0]
            # Wait a moment and make sure the process is stable.
            time.sleep(1.5)
            if last in find_freecad_pids():
                return True
        time.sleep(0.5)
    return False


FRIDA_JS = r"""
function resolveExport(name) {
  if (typeof Module.getGlobalExportByName === 'function') {
    return Module.getGlobalExportByName(name);
  }
  if (typeof Module.getExportByName === 'function') {
    return Module.getExportByName(null, name);
  }
  throw new Error('cannot resolve export ' + name);
}
const ensure = new NativeFunction(resolveExport('PyGILState_Ensure'), 'int', []);
const release = new NativeFunction(resolveExport('PyGILState_Release'), 'void', ['int']);
const run = new NativeFunction(resolveExport('PyRun_SimpleString'), 'int', ['pointer']);
rpc.exports = {
  run: function(code) {
    const g = ensure();
    try {
      const p = Memory.allocUtf8String(code);
      return run(p);
    } finally {
      release(g);
    }
  }
};
"""


def inject_agent(pid):
    import frida
    code = "exec(compile(open(%r).read(), %r, 'exec'))" % (AGENT_PATH, AGENT_PATH)
    session = frida.attach(pid)
    try:
        script = session.create_script(FRIDA_JS)
        script.load()
        rc = script.exports_sync.run(code)
    finally:
        try:
            session.detach()
        except Exception:
            pass
    if rc != 0:
        raise RuntimeError("agent injection returned %r" % (rc,))


def ensure_agent(force=False):
    global _agent_ready
    started = False
    deadline = time.time() + 70
    last_exc = None
    while time.time() < deadline:
        pids = find_freecad_pids()
        if not pids:
            if not started:
                started = True
                _start_freecad()
            else:
                time.sleep(1.0)
            continue
        target = pids[0]
        if not force:
            st = _socket_status()
            if st is not None and st.get("pid") == target:
                if (EXPECTED_AGENT_VERSION is None
                        or st.get("agentVersion") == EXPECTED_AGENT_VERSION):
                    _agent_ready = True
                    return
                log("agent version %s != expected %s; re-injecting"
                    % (st.get("agentVersion"), EXPECTED_AGENT_VERSION))
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass
        log("injecting agent into pid %s" % target)
        try:
            inject_agent(target)
        except Exception as e:
            last_exc = e
            log("injection into pid %s failed: %s" % (target, e))
            time.sleep(1.0)
            continue
        for _ in range(60):
            st = _socket_status()
            if st is not None and st.get("pid") == target:
                if (EXPECTED_AGENT_VERSION is None
                        or st.get("agentVersion") == EXPECTED_AGENT_VERSION):
                    _agent_ready = True
                    return
            time.sleep(0.25)
        last_exc = RuntimeError("agent did not answer after injection")
        time.sleep(0.5)
    raise RuntimeError("could not attach to a running FreeCAD process: %s"
                       % (last_exc,))


def call_op(op, args=None, timeout=300):
    global _agent_ready
    last_exc = None
    for attempt in range(2):
        try:
            ensure_agent(force=(attempt > 0))
            req = json.dumps({"op": op, "args": args or {}}).encode("utf-8") + b"\n"
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(SOCKET_PATH)
            s.sendall(req)
            f = s.makefile("rb")
            line = f.readline()
            s.close()
            if not line:
                raise RuntimeError("empty response from FreeCAD agent")
            resp = json.loads(line.decode("utf-8"))
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "unknown FreeCAD agent error"))
            return resp.get("result")
        except Exception as e:
            last_exc = e
            _agent_ready = False
            try:
                os.unlink(SOCKET_PATH)
            except Exception:
                pass
            if attempt == 0:
                time.sleep(0.5)
                continue
            raise
    raise last_exc


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _obj_schema(extra=None, required=None):
    props = {
        "document": {"type": "string",
                     "description": "Document name. Omit to use the active document."},
    }
    if extra:
        props.update(extra)
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


TOOLS = [
    {
        "name": "freecad_status",
        "description": "Return FreeCAD version and process id, the list of open documents, the active document, the active workbench, and the injected agent version. Use this first to see what is open.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_list_documents",
        "description": "List open documents with name, label, file path, object count, modified flag and which one is active.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_new_document",
        "description": "Create a new empty document and make it active. Returns the document summary.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Internal document name (optional; FreeCAD generates one if omitted)."},
            "label": {"type": "string", "description": "User-visible label (optional)."},
        }},
    },
    {
        "name": "freecad_open_document",
        "description": "Open a file (FreeCAD .FCStd or any format FreeCAD can import) as a new document. Returns the document summary.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path to the file."},
        }, "required": ["path"]},
    },
    {
        "name": "freecad_save_document",
        "description": "Save a document. With no path, saves to the current file (an unsaved document needs a path). With a path, performs Save As. Returns the document summary including the file path.",
        "inputSchema": {"type": "object", "properties": {
            "document": {"type": "string", "description": "Document name (default: active document)."},
            "path": {"type": "string", "description": "Target path (optional). .FCStd is the native format."},
        }},
    },
    {
        "name": "freecad_close_document",
        "description": "Close a document, optionally saving it first.",
        "inputSchema": {"type": "object", "properties": {
            "document": {"type": "string", "description": "Document name (default: active document)."},
            "save": {"type": "boolean", "description": "Save before closing (default false)."},
        }},
    },
    {
        "name": "freecad_list_objects",
        "description": "List objects in a document. Each item has name, label, type id and visibility. Filter by type substring and/or name/label substring.",
        "inputSchema": _obj_schema({
            "type": {"type": "string", "description": "Only include objects whose type id contains this text, e.g. 'Part::', 'Sketch', 'Spreadsheet'."},
            "name_filter": {"type": "string", "description": "Only include objects whose name or label contains this text (case-insensitive)."},
            "limit": {"type": "integer", "description": "Maximum number of objects to return (default 1000; 0 means no limit)."},
        }),
    },
    {
        "name": "freecad_get_object",
        "description": "Return detailed information about one object: type, label, state, all App properties (name, type id, value, group, documentation), active expressions, and view properties. Use 'properties' to request only some properties. Length/angle properties are returned as Quantity objects; vectors, placements, colors, links and shapes are structured.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "properties": {"type": "array", "items": {"type": "string"}, "description": "Optional list of property names to return."},
            "include_view": {"type": "boolean", "description": "Include view-provider properties (default true)."},
        }, ["name"]),
    },
    {
        "name": "freecad_get_property",
        "description": "Return one property of one object: its type id and structured value.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "property": {"type": "string", "description": "Property name, e.g. Length, Placement, Shape."},
        }, ["name", "property"]),
    },
    {
        "name": "freecad_set_property",
        "description": "Set one property of one object and recompute. Values: numbers for floats/ints; booleans; strings for text/enums; lengths/angles as a number (mm or radians) or a string such as '10 mm' or '45 deg'; vectors as {'x':..,'y':..,'z':..} or [x,y,z]; placements as {'base':{...},'rotation':{'axis':{...},'angle':radians}}; colors as {'r':..,'g':..,'b':..,'a':..}; links as an object name or {'name':'...','subElements':['Face1']}. Returns the property type and value after the change.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "property": {"type": "string", "description": "Property name."},
            "value": {"description": "New value (JSON scalar, array or object; see description)."},
            "recompute": {"type": "boolean", "description": "Recompute the document after setting (default true)."},
        }, ["name", "property", "value"]),
    },
    {
        "name": "freecad_set_properties",
        "description": "Set several properties on one object at once. 'values' maps property names to values; the same value formats as freecad_set_property apply. Returns per-property results and errors.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "values": {"type": "object", "description": "Map of property name to new value."},
            "recompute": {"type": "boolean", "description": "Recompute after setting (default true)."},
        }, ["name", "values"]),
    },
    {
        "name": "freecad_create_object",
        "description": "Create a document object of a FreeCAD type id (for example 'Part::Box', 'Part::Cylinder', 'PartDesign::Body', 'Sketcher::SketchObject', 'Spreadsheet::Sheet'). Optionally set properties and a label. Returns the new object's summary and any property errors. Use freecad_list_object_types to discover type ids.",
        "inputSchema": _obj_schema({
            "type": {"type": "string", "description": "FreeCAD type id, e.g. Part::Box."},
            "name": {"type": "string", "description": "Internal object name (optional)."},
            "label": {"type": "string", "description": "User-visible label (optional)."},
            "properties": {"type": "object", "description": "Optional map of property name to value."},
        }, ["type"]),
    },
    {
        "name": "freecad_delete_object",
        "description": "Remove an object from its document and recompute.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
        }, ["name"]),
    },
    {
        "name": "freecad_set_expression",
        "description": "Bind a property to a FreeCAD expression (the same syntax as the property editor, e.g. 'Spreadsheet.A1 * 2' or '10 mm'). This is application data, not code. Recomputes afterwards.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "property": {"type": "string", "description": "Property name."},
            "expression": {"type": "string", "description": "FreeCAD expression string."},
        }, ["name", "property", "expression"]),
    },
    {
        "name": "freecad_get_expression",
        "description": "Return the expression bound to a property, or null.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "property": {"type": "string", "description": "Property name."},
        }, ["name", "property"]),
    },
    {
        "name": "freecad_clear_expression",
        "description": "Remove the expression bound to a property and recompute.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "property": {"type": "string", "description": "Property name."},
        }, ["name", "property"]),
    },
    {
        "name": "freecad_recompute",
        "description": "Recompute a document and return its objects, each with a 'state' list (e.g. Up-to-date, Touched, Invalid).",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_get_computed",
        "description": "Return computed geometry for an object: for shapes, volume, surface area, total edge length, bounding box, centre of mass, and counts of solids/faces/edges/vertices; for meshes, point/facet counts and volume/area. Also includes common scalar properties such as Length, Radius, Height. Units are mm, mm^2 and mm^3; volumes/areas are floats.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
        }, ["name"]),
    },
    {
        "name": "freecad_list_commands",
        "description": "List FreeCAD GUI command names available in currently loaded workbenches. Optionally filter by substring. Command names are the identifiers used by freecad_run_command.",
        "inputSchema": {"type": "object", "properties": {
            "filter": {"type": "string", "description": "Case-insensitive substring filter (optional)."},
        }},
    },
    {
        "name": "freecad_command_info",
        "description": "Return metadata for one command: menu text, tooltip, status tip, shortcut and whether it is active.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Command name, e.g. Part_Box."},
        }, "required": ["command"]},
    },
    {
        "name": "freecad_run_command",
        "description": "Run a FreeCAD GUI command by name (for example 'Part_Box', 'Std_New', 'Std_ViewFitAll'). Commands that open a modal dialog may block until the dialog is closed; prefer object/property tools for unattended changes. Returns the command, the active document, and the object names created/removed by the command.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string", "description": "Command name."},
            "index": {"type": "integer", "description": "Child command index (default 0)."},
            "allow_dialog": {"type": "boolean", "description": "Allow commands known to open modal dialogs (default false; such commands may block)."},
        }, "required": ["command"]},
    },
    {
        "name": "freecad_list_workbenches",
        "description": "List installed FreeCAD workbench names. Activating a workbench loads its commands and object types.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_activate_workbench",
        "description": "Activate a workbench by name (for example 'PartWorkbench', 'SketcherWorkbench', 'SpreadsheetWorkbench'). After activation, its commands appear in freecad_list_commands and its object types in freecad_list_object_types.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Workbench name."},
        }, "required": ["name"]},
    },
    {
        "name": "freecad_get_selection",
        "description": "Return the current 3D-view selection: selected objects and selected sub-elements (faces, edges, vertices).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_set_selection",
        "description": "Select objects in the active view by name/label, optionally clearing the previous selection first. Returns the objects that were selected (the live selection is updated as well).",
        "inputSchema": _obj_schema({
            "names": {"type": "array", "items": {"type": "string"}, "description": "Object names or labels to select."},
            "clear": {"type": "boolean", "description": "Clear current selection first (default true)."},
        }, ["names"]),
    },
    {
        "name": "freecad_clear_selection",
        "description": "Clear the current 3D-view selection.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_get_view_properties",
        "description": "Return all view-provider properties of an object (Visibility, ShapeColor, LineColor, Transparency, DisplayMode, DrawStyle, etc.) with type ids and values.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
        }, ["name"]),
    },
    {
        "name": "freecad_set_view_property",
        "description": "Set one view-provider property of an object, e.g. Visibility, ShapeColor, LineColor, Transparency, DisplayMode, DrawStyle. Colors use {'r':..,'g':..,'b':..,'a':..} with 0..1 values; enums use their string name.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "property": {"type": "string", "description": "View property name."},
            "value": {"description": "New value."},
        }, ["name", "property", "value"]),
    },
    {
        "name": "freecad_view_action",
        "description": "Perform a standard 3D-view action: fit_all, fit_selection, top, bottom, front, rear, left, right, isometric, axonometric, trimetric, dimetric, perspective, orthographic, zoom_in, zoom_out.",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "description": "One of the listed action names."},
        }, "required": ["action"]},
    },
    {
        "name": "freecad_get_camera",
        "description": "Return the active 3D view's camera as structured data: type, position, orientation, view direction, focal/height/near/far distances, plus the raw Inventor camera string. If a TechDraw page tab is active, the tool still finds a 3D view.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_set_camera",
        "description": "Change the active 3D view's camera. Position is a vector; orientation is an axis/angle or quaternion; camera_type is 'Perspective' or 'Orthographic'; focal_distance is a number. Returns the structured camera after the change.",
        "inputSchema": {"type": "object", "properties": {
            "position": {"description": "Camera position vector."},
            "orientation": {"description": "Camera orientation (axis/angle or quaternion)."},
            "camera_type": {"type": "string", "description": "Perspective or Orthographic."},
            "focal_distance": {"type": "number", "description": "Focal distance."},
        }},
    },
    {
        "name": "freecad_list_spreadsheets",
        "description": "List Spreadsheet::Sheet objects in a document.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_get_cell",
        "description": "Return a spreadsheet cell's computed value, its contents (formula or literal) and its alias. Cell addresses look like 'A1'.",
        "inputSchema": _obj_schema({
            "sheet": {"type": "string", "description": "Spreadsheet object name or label."},
            "cell": {"type": "string", "description": "Cell address, e.g. A1."},
        }, ["sheet", "cell"]),
    },
    {
        "name": "freecad_set_cell",
        "description": "Set a spreadsheet cell. A leading '=' makes the value a formula, e.g. '=A1*3' or '=SUM(A1:A10)'; otherwise it is a literal. Recomputes the document. Returns the cell after the change.",
        "inputSchema": _obj_schema({
            "sheet": {"type": "string", "description": "Spreadsheet object name or label."},
            "cell": {"type": "string", "description": "Cell address, e.g. A1."},
            "value": {"type": "string", "description": "Cell content (literal or '=formula')."},
        }, ["sheet", "cell", "value"]),
    },
    {
        "name": "freecad_set_alias",
        "description": "Set or clear a spreadsheet cell alias (a name usable in expressions).",
        "inputSchema": _obj_schema({
            "sheet": {"type": "string", "description": "Spreadsheet object name or label."},
            "cell": {"type": "string", "description": "Cell address, e.g. A1."},
            "alias": {"type": "string", "description": "Alias name, or empty string to clear."},
        }, ["sheet", "cell", "alias"]),
    },
    {
        "name": "freecad_export_objects",
        "description": "Export objects to a file. The format is chosen from the extension: .FCStd (native), .STEP/.STP/.IGES/.IGS/.BREP (CAD exchange), .STL/.OBJ/.PLY/.AMF/.OFF (mesh), .DXF (2D), .CSV (first spreadsheet), .SVG (single object). objects may be a single name or a list; with no objects, exports the whole document. Returns the path and object count, and raises an error if no file was produced (for example for null shapes).",
        "inputSchema": _obj_schema({
            "path": {"type": "string", "description": "Target file path."},
            "objects": {"type": "array", "items": {"type": "string"}, "description": "Object names/labels to export (default: all objects)."},
        }, ["path"]),
    },
    {
        "name": "freecad_import_file",
        "description": "Import a file. With merge=false (default), opens it as a new document. With merge=true, imports its objects into the given/active document. Supports .FCStd, STEP/IGES/BREP, STL/OBJ/PLY/AMF/OFF and DXF/DWG.",
        "inputSchema": _obj_schema({
            "path": {"type": "string", "description": "File path to import."},
            "merge": {"type": "boolean", "description": "Import into an existing document instead of opening a new one (default false)."},
        }, ["path"]),
    },
    {
        "name": "freecad_get_preferences",
        "description": "List a FreeCAD parameter group: its subgroups and entries. Path syntax is FreeCAD's, e.g. 'User parameter:BaseApp/Preferences/Mod/Part'. With no path, lists the root preferences group. Values are shown with their stored type.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Parameter path (optional)."},
        }},
    },
    {
        "name": "freecad_get_preference",
        "description": "Read one FreeCAD preference value from a parameter group. Type is bool, int, float or string; if omitted, types are tried in that order.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Parameter group path."},
            "name": {"type": "string", "description": "Preference key."},
            "type": {"type": "string", "enum": ["bool", "int", "float", "string"], "description": "Stored type (optional)."},
        }, "required": ["path", "name"]},
    },
    {
        "name": "freecad_set_preference",
        "description": "Write one FreeCAD preference value and return it. The value is written to FreeCAD's parameter store and FreeCAD.saveParameter() is called; it becomes durable when FreeCAD writes its configuration.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Parameter group path."},
            "name": {"type": "string", "description": "Preference key."},
            "type": {"type": "string", "enum": ["bool", "int", "float", "string"], "description": "Stored type."},
            "value": {"description": "New value."},
        }, "required": ["path", "name", "type", "value"]},
    },
    {
        "name": "freecad_list_object_types",
        "description": "List FreeCAD object type ids that can be passed to freecad_create_object. Only types from currently loaded modules are listed; call freecad_activate_workbench first (or pass activate_workbenches) to load more.",
        "inputSchema": _obj_schema({
            "activate_workbenches": {"type": "array", "items": {"type": "string"}, "description": "Optional workbench names to activate before listing."},
        }),
    },
    {
        "name": "freecad_get_document",
        "description": "Return a document's properties (Label, Comment, CreatedBy, LastModifiedBy, Company, License, FileName, Id, etc.) and the summaries of its objects. Use freecad_set_document_property to change writable document properties.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_set_document_property",
        "description": "Set a writable document property (for example Label or Comment) and return its type and new value.",
        "inputSchema": _obj_schema({
            "property": {"type": "string", "description": "Document property name."},
            "value": {"description": "New value."},
        }, ["property", "value"]),
    },
    {
        "name": "freecad_undo",
        "description": "Undo the last operation in a document and return the undo/redo counts and undo names.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_redo",
        "description": "Redo the last undone operation in a document and return the undo/redo counts and redo names.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_undo_state",
        "description": "Return a document's undo and redo counts and the names of the undo/redo steps.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_get_dependencies",
        "description": "Return the objects an object depends on (dependsOn) and the objects that depend on it (usedBy). Useful before deleting or changing an object.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
        }, ["name"]),
    },
    {
        "name": "freecad_save_view_image",
        "description": "Save a rendered image of the active 3D view to a PNG/JPG file. Width/height are pixels; background is 'Current', 'White' or 'Black'. Returns the file path and size.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Target image path (.png or .jpg)."},
            "width": {"type": "integer", "description": "Image width in pixels (default 1280)."},
            "height": {"type": "integer", "description": "Image height in pixels (default 800)."},
            "background": {"type": "string", "description": "Current, White or Black (default Current)."},
        }, "required": ["path"]},
    },
    {
        "name": "freecad_sketch_add_geometry",
        "description": "Add 2D geometry to a Sketcher::SketchObject. 'geometry' is one object or a list. Supported types: {'type':'line','start':[x,y],'end':[x,y]}, {'type':'circle','center':[x,y],'radius':r}, {'type':'arc','center':[x,y],'radius':r,'start_angle':a,'end_angle':b} (radians, counter-clockwise), {'type':'ellipse','center':[x,y],'major_radius':..,'minor_radius':..,'angle_xu':..}, {'type':'arc_of_ellipse',...}, {'type':'point','point':[x,y]}, {'type':'bspline','poles':[[x,y],...],'periodic':false,'degree':3}. Coordinates are in the sketch plane (mm). Returns the new geometry indices and the full geometry list.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "geometry": {"description": "One geometry object or a list of geometry objects."},
            "construction": {"type": "boolean", "description": "Create as construction geometry (default false)."},
        }, ["sketch", "geometry"]),
    },
    {
        "name": "freecad_sketch_get_geometry",
        "description": "Return a sketch's geometry list with type and parameters, plus a construction flag for each index.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
        }, ["sketch"]),
    },
    {
        "name": "freecad_sketch_add_constraint",
        "description": "Add a Sketcher constraint. 'type' is a constraint name (Coincident, DistanceX, DistanceY, Distance, Radius, Diameter, Angle, Horizontal, Vertical, Parallel, Perpendicular, Tangent, Equal, Symmetric, Block, ...). 'args' is the list of positional arguments after the type, exactly as FreeCAD's Sketcher Python API takes them: geometry indices are 0-based; position codes are 1=start, 2=end, 3=centre. Examples: Coincident -> [0,1,1,1]; DistanceX on a point -> [0,1,10.0]; DistanceX on a line -> [0,10.0]; Radius -> [0,5.0]; Distance between two points -> [0,1,1,2,10.0]; Angle between two lines -> [0,1,45.0]. Returns the new constraint index and the full constraint list.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "type": {"type": "string", "description": "Constraint type name."},
            "args": {"type": "array", "description": "Positional arguments after the type."},
        }, ["sketch", "type", "args"]),
    },
    {
        "name": "freecad_sketch_get_constraints",
        "description": "Return a sketch's constraints (type, geometry indices, positions, value, driving/active state) and its degrees of freedom / fully-constrained flag.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
        }, ["sketch"]),
    },
    {
        "name": "freecad_sketch_solve",
        "description": "Run the sketch solver and return its result code plus degrees of freedom / fully-constrained flag.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
        }, ["sketch"]),
    },
    {
        "name": "freecad_sketch_delete_geometry",
        "description": "Delete a sketch geometry element by its 0-based index and solve.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "index": {"type": "integer", "description": "Geometry index."},
        }, ["sketch", "index"]),
    },
    {
        "name": "freecad_sketch_delete_constraint",
        "description": "Delete a sketch constraint by its 0-based index and solve.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "index": {"type": "integer", "description": "Constraint index."},
        }, ["sketch", "index"]),
    },
    {
        "name": "freecad_sketch_set_datum",
        "description": "Set the value of a datum constraint (distance, radius, angle, ...) by its 0-based constraint index and solve. The value may be a number or a string with units, e.g. '10 mm' or '45 deg'.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "index": {"type": "integer", "description": "Constraint index."},
            "value": {"description": "New datum value."},
        }, ["sketch", "index", "value"]),
    },
    {
        "name": "freecad_sketch_toggle_construction",
        "description": "Toggle whether a sketch geometry element is construction geometry.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "index": {"type": "integer", "description": "Geometry index."},
        }, ["sketch", "index"]),
    },
    {
        "name": "freecad_sketch_add_external",
        "description": "Add an external geometry reference to a sketch from another object, e.g. object 'Box' sub-element 'Edge1' or 'Face1'. Returns the external geometry index.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "object": {"type": "string", "description": "Object to reference."},
            "subelement": {"type": "string", "description": "Sub-element name, e.g. Edge1 or Face1."},
        }, ["sketch", "object", "subelement"]),
    },

    {
        "name": "freecad_list_properties",
        "description": "List an object's property names with their type ids, groups and documentation, without returning values. Set include_view=true to also list view-provider property names. Use freecad_get_property or freecad_get_object to read values.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "include_view": {"type": "boolean", "description": "Also list view-provider property names (default false)."},
        }, ["name"]),
    },
    {
        "name": "freecad_container_add_object",
        "description": "Add a child object to a container object (App::Part, App::DocumentObjectGroup, PartDesign::Body, ...). This is the normal way to create PartDesign features inside a Body. Optionally set properties and a label. Returns the new object summary.",
        "inputSchema": _obj_schema({
            "container": {"type": "string", "description": "Container object name or label."},
            "type": {"type": "string", "description": "Type id of the child, e.g. PartDesign::Pad."},
            "name": {"type": "string", "description": "Internal child name (optional)."},
            "label": {"type": "string", "description": "Child label (optional)."},
            "properties": {"type": "object", "description": "Optional property map."},
        }, ["container", "type"]),
    },
    {
        "name": "freecad_part_boolean",
        "description": "Create a Part boolean result: operation is cut, fuse, common, compound, multi_fuse or multi_common. base and tool are object names/labels; others is an optional extra list for multi operations. Returns the new object summary and its computed geometry.",
        "inputSchema": _obj_schema({
            "operation": {"type": "string", "enum": ["cut", "fuse", "common", "compound", "multi_fuse", "multi_common"], "description": "Boolean operation."},
            "base": {"type": "string", "description": "Base object name/label."},
            "tool": {"type": "string", "description": "Tool object name/label."},
            "name": {"type": "string", "description": "Internal name for the result (optional)."},
            "others": {"type": "array", "items": {"type": "string"}, "description": "Extra objects for multi operations."},
        }, ["operation", "base", "tool"]),
    },

    {
        "name": "freecad_spreadsheet_export",
        "description": "Export a Spreadsheet::Sheet to a CSV file using FreeCAD's spreadsheet exporter.",
        "inputSchema": _obj_schema({
            "sheet": {"type": "string", "description": "Spreadsheet object name or label."},
            "path": {"type": "string", "description": "Target CSV path."},
        }, ["sheet", "path"]),
    },
    {
        "name": "freecad_spreadsheet_import",
        "description": "Import a CSV file into a Spreadsheet::Sheet using FreeCAD's spreadsheet importer and recompute.",
        "inputSchema": _obj_schema({
            "sheet": {"type": "string", "description": "Spreadsheet object name or label."},
            "path": {"type": "string", "description": "Source CSV path."},
        }, ["sheet", "path"]),
    },

    {
        "name": "freecad_list_unit_schemas",
        "description": "List FreeCAD's unit schemas (display unit systems) and the current schema index.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "freecad_set_unit_schema",
        "description": "Set FreeCAD's display unit schema by index or by a case-insensitive name substring (e.g. 'MKS', 'US customary', 'FEM'). This changes the GUI display unit system. MCP quantity values are always FreeCAD internal units (mm, radians); the 'str' field may not reflect the new schema immediately.",
        "inputSchema": {"type": "object", "properties": {
            "schema": {"description": "Schema index (integer) or name substring (string)."},
        }, "required": ["schema"]},
    },

    {
        "name": "freecad_techdraw_create_page",
        "description": "Create a TechDraw drawing page with an SVG template. 'template' is an optional path to a template SVG (default /usr/share/freecad/Mod/TechDraw/Templates/A4_LandscapeTD.svg). Returns the page and template objects.",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Page object name (optional)."},
            "template": {"type": "string", "description": "Template SVG path (optional)."},
        }),
    },
    {
        "name": "freecad_techdraw_add_view",
        "description": "Add a projected part view to a TechDraw page. 'objects' is one object name or a list. Optional properties include Direction, Scale, Rotation, X, Y, Perspective. Returns the new view object summary.",
        "inputSchema": _obj_schema({
            "page": {"type": "string", "description": "Page object name or label."},
            "objects": {"description": "Object name/label or list of them to project."},
            "name": {"type": "string", "description": "View object name (optional)."},
            "properties": {"type": "object", "description": "Optional view property map."},
        }, ["page", "objects"]),
    },
    {
        "name": "freecad_techdraw_export",
        "description": "Export a TechDraw page to .pdf or .svg using FreeCAD's TechDraw exporter. Returns the path and file size.",
        "inputSchema": _obj_schema({
            "page": {"type": "string", "description": "Page object name or label."},
            "path": {"type": "string", "description": "Target .pdf or .svg path."},
        }, ["page", "path"]),
    },
    {
        "name": "freecad_techdraw_list_pages",
        "description": "List TechDraw::DrawPage objects in a document.",
        "inputSchema": _obj_schema(),
    },

    {
        "name": "freecad_draft_help",
        "description": "Return the docstring of a Draft helper/action, or list available helper and action names. Use this to learn the exact positional/keyword arguments of Draft.make_line, make_circle, make_rectangle, move, rotate, array, etc.",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "description": "Helper suffix, e.g. line or rectangle (optional; omit to list helpers)."},
        }},
    },
    {
        "name": "freecad_draft_make",
        "description": "Create a Draft object using Draft's make_* helpers. 'kind' is the helper suffix, e.g. line, circle, rectangle, polygon, wire, bspline, bezcurve, point, text, shapestring, dimension, label, facebinder, shape2dview, clone, array. 'args' are positional arguments and 'params' are keyword arguments for that helper; 2D/3D point lists are accepted as [x,y] / [x,y,z] and converted to vectors. Use freecad_draft_help to see the exact signature. Returns the created object(s).",
        "inputSchema": _obj_schema({
            "kind": {"type": "string", "description": "Draft make helper suffix, e.g. line, circle, rectangle."},
            "args": {"type": "array", "description": "Positional arguments for the Draft helper."},
            "params": {"type": "object", "description": "Keyword arguments for the Draft helper."},
        }, ["kind"]),
    },
    {
        "name": "freecad_draft_modify",
        "description": "Apply a Draft modification to objects. 'action' is a Draft function name such as move, rotate, scale, offset, mirror, clone, array, upgrade, downgrade, split, extrude, draftify, heal. 'objects' is an object name or list; 'args' are extra positional arguments and 'params' keyword arguments (vectors may be [x,y,z]). The object/list is passed as the first argument. Returns the resulting object(s).",
        "inputSchema": _obj_schema({
            "action": {"type": "string", "description": "Draft action/function name."},
            "objects": {"description": "Object name or list of object names/labels."},
            "args": {"type": "array", "description": "Extra positional arguments after the object/list."},
            "params": {"type": "object", "description": "Keyword arguments for the Draft action."},
        }, ["action", "objects"]),
    },
    {
        "name": "freecad_mesh_boolean",
        "description": "Create a mesh boolean result from two Mesh objects. operation is union, cut or intersection. Returns the new Mesh::Feature summary.",
        "inputSchema": _obj_schema({
            "operation": {"type": "string", "enum": ["union", "cut", "intersection"], "description": "Mesh boolean operation."},
            "base": {"type": "string", "description": "Base mesh object name/label."},
            "tool": {"type": "string", "description": "Tool mesh object name/label."},
            "name": {"type": "string", "description": "Result object name (optional)."},
        }, ["operation", "base", "tool"]),
    },

    {
        "name": "freecad_activate_view",
        "description": "Make a 3D view active (and optionally activate a document). Use this after a TechDraw page or other view has become the active tab, before calling camera/view/image tools.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_get_report_view",
        "description": "Return the last lines of FreeCAD's Report view, where recompute errors and warnings appear. Useful for diagnosing why a recompute or expression failed.",
        "inputSchema": {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "Number of trailing lines to return (default 200)."},
        }},
    },
    {
        "name": "freecad_get_python_console",
        "description": "Return the last lines of FreeCAD's Python console widget (read-only; this does not execute anything).",
        "inputSchema": {"type": "object", "properties": {
            "lines": {"type": "integer", "description": "Number of trailing lines to return (default 200)."},
        }},
    },
    {
        "name": "freecad_list_sub_elements",
        "description": "List a shape object's sub-elements: faces (name Face1.., area, centre of mass, surface type), edges (name Edge1.., length, curve type, vertex points) and vertices (name Vertex1.., point). Returns faceCount/edgeCount/vertexCount and truncated flags; use limit to bound each list. For meshes, returns point/facet counts and bounding box. Use the names with tools that take sub-elements (links, TechDraw, fillets).",
        "inputSchema": _obj_schema({
            "name": {"type": "string", "description": "Object name or label."},
            "limit": {"type": "integer", "description": "Maximum items per list (default 100)."},
            "include_faces": {"type": "boolean", "description": "Include face details (default true)."},
            "include_edges": {"type": "boolean", "description": "Include edge details (default true)."},
            "include_vertices": {"type": "boolean", "description": "Include vertex details (default true)."},
        }, ["name"]),
    },
    {
        "name": "freecad_begin_transaction",
        "description": "Open a document transaction so that subsequent MCP edits are grouped into one undo step. Use freecad_commit_transaction or freecad_abort_transaction to finish.",
        "inputSchema": _obj_schema({
            "label": {"type": "string", "description": "Transaction label (optional)."},
        }),
    },
    {
        "name": "freecad_commit_transaction",
        "description": "Commit the current document transaction (creating an undo step) and return the document's undo count.",
        "inputSchema": _obj_schema(),
    },
    {
        "name": "freecad_abort_transaction",
        "description": "Abort the current document transaction, discarding its changes.",
        "inputSchema": _obj_schema(),
    },

    {
        "name": "freecad_measure_distance",
        "description": "Measure the minimum distance between the shapes of two objects using FreeCAD's shape distance algorithm. Returns the distance in mm and the closest point pair(s).",
        "inputSchema": _obj_schema({
            "name1": {"type": "string", "description": "First object name or label."},
            "name2": {"type": "string", "description": "Second object name or label."},
        }, ["name1", "name2"]),
    },

    {
        "name": "freecad_sketch_rename_constraint",
        "description": "Give a sketch constraint a name (useful for expressions and readability) and return the constraint list.",
        "inputSchema": _obj_schema({
            "sketch": {"type": "string", "description": "Sketch object name or label."},
            "index": {"type": "integer", "description": "Constraint index."},
            "name": {"type": "string", "description": "New constraint name."},
        }, ["sketch", "index", "name"]),
    },

    {
        "name": "freecad_arch_help",
        "description": "Return the docstring of an Arch/BIM make* helper, or list the available helpers. Use this to learn the exact arguments of Arch.makeWall, makeStructure, makeFloor, makeWindow, makeRoof, makeStairs, makePanel, etc.",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "description": "Helper suffix, e.g. wall or structure (optional; omit to list helpers)."},
        }},
    },
    {
        "name": "freecad_arch_make",
        "description": "Create an Arch/BIM object using Arch's make* helpers. 'kind' is the helper suffix (wall, structure, floor, building, window, roof, stairs, panel, equipment, space, site, axis, grid, reference, frame, component, ...). 'args' are positional arguments and 'params' are keyword arguments; point lists are converted to vectors. Returns the created object(s).",
        "inputSchema": _obj_schema({
            "kind": {"type": "string", "description": "Arch make helper suffix, e.g. wall."},
            "args": {"type": "array", "description": "Positional arguments for the helper."},
            "params": {"type": "object", "description": "Keyword arguments for the helper."},
        }, ["kind"]),
    },
    {
        "name": "freecad_arch_modify",
        "description": "Apply an Arch action to objects, such as removeComponents or makeComponent. 'objects' is an object name or list; 'args' and 'params' are passed after the object/list. Returns the resulting object(s).",
        "inputSchema": _obj_schema({
            "action": {"type": "string", "description": "Arch action/function name."},
            "objects": {"description": "Object name or list of object names/labels."},
            "args": {"type": "array", "description": "Extra positional arguments after the object/list."},
            "params": {"type": "object", "description": "Keyword arguments for the Arch action."},
        }, ["action", "objects"]),
    },

]


# Map tool names to agent operations.
TOOL_OPS = {
    "freecad_status": "status",
    "freecad_list_documents": "list_documents",
    "freecad_new_document": "new_document",
    "freecad_open_document": "open_document",
    "freecad_save_document": "save_document",
    "freecad_close_document": "close_document",
    "freecad_list_objects": "list_objects",
    "freecad_get_object": "get_object",
    "freecad_get_property": "get_property",
    "freecad_set_property": "set_property",
    "freecad_set_properties": "set_properties",
    "freecad_create_object": "create_object",
    "freecad_delete_object": "delete_object",
    "freecad_set_expression": "set_expression",
    "freecad_get_expression": "get_expression",
    "freecad_clear_expression": "clear_expression",
    "freecad_recompute": "recompute",
    "freecad_get_computed": "get_computed",
    "freecad_list_commands": "list_commands",
    "freecad_command_info": "command_info",
    "freecad_run_command": "run_command",
    "freecad_list_workbenches": "list_workbenches",
    "freecad_activate_workbench": "activate_workbench",
    "freecad_get_selection": "get_selection",
    "freecad_set_selection": "set_selection",
    "freecad_clear_selection": "clear_selection",
    "freecad_get_view_properties": "get_view_properties",
    "freecad_set_view_property": "set_view_property",
    "freecad_view_action": "view_action",
    "freecad_get_camera": "get_camera",
    "freecad_set_camera": "set_camera",
    "freecad_list_spreadsheets": "list_spreadsheets",
    "freecad_get_cell": "get_cell",
    "freecad_set_cell": "set_cell",
    "freecad_set_alias": "set_alias",
    "freecad_export_objects": "export_objects",
    "freecad_import_file": "import_file",
    "freecad_get_preferences": "get_preferences",
    "freecad_get_preference": "get_preference",
    "freecad_set_preference": "set_preference",
    "freecad_list_object_types": "list_object_types",
    "freecad_get_document": "get_document",
    "freecad_set_document_property": "set_document_property",
    "freecad_undo": "undo",
    "freecad_redo": "redo",
    "freecad_undo_state": "undo_state",
    "freecad_get_dependencies": "get_dependencies",
    "freecad_save_view_image": "save_view_image",
    "freecad_sketch_add_geometry": "sketch_add_geometry",
    "freecad_sketch_get_geometry": "sketch_get_geometry",
    "freecad_sketch_add_constraint": "sketch_add_constraint",
    "freecad_sketch_get_constraints": "sketch_get_constraints",
    "freecad_sketch_solve": "sketch_solve",
    "freecad_sketch_delete_geometry": "sketch_delete_geometry",
    "freecad_sketch_delete_constraint": "sketch_delete_constraint",
    "freecad_sketch_set_datum": "sketch_set_datum",
    "freecad_sketch_toggle_construction": "sketch_toggle_construction",
    "freecad_sketch_add_external": "sketch_add_external",
    "freecad_sketch_rename_constraint": "sketch_rename_constraint",
    "freecad_list_properties": "list_properties",
    "freecad_container_add_object": "container_add_object",
    "freecad_part_boolean": "part_boolean",
    "freecad_spreadsheet_export": "spreadsheet_export",
    "freecad_spreadsheet_import": "spreadsheet_import",
    "freecad_list_unit_schemas": "list_unit_schemas",
    "freecad_set_unit_schema": "set_unit_schema",
    "freecad_techdraw_create_page": "techdraw_create_page",
    "freecad_techdraw_add_view": "techdraw_add_view",
    "freecad_techdraw_export": "techdraw_export",
    "freecad_techdraw_list_pages": "techdraw_list_pages",
    "freecad_draft_help": "draft_help",
    "freecad_draft_make": "draft_make",
    "freecad_draft_modify": "draft_modify",
    "freecad_mesh_boolean": "mesh_boolean",
    "freecad_activate_view": "activate_view",
    "freecad_measure_distance": "measure_distance",
    "freecad_arch_help": "arch_help",
    "freecad_arch_make": "arch_make",
    "freecad_arch_modify": "arch_modify",
    "freecad_get_report_view": "get_report_view",
    "freecad_get_python_console": "get_python_console",
    "freecad_list_sub_elements": "list_sub_elements",
    "freecad_begin_transaction": "begin_transaction",
    "freecad_commit_transaction": "commit_transaction",
    "freecad_abort_transaction": "abort_transaction",
}


INSTRUCTIONS = """\
This server controls the running FreeCAD 0.20.2 application through its own
Python API; operations execute on FreeCAD's Qt main thread. It exposes the
application's documents, objects, properties, commands, preferences, views,
spreadsheets and file import/export.

Addressing: documents are addressed by internal name; objects by internal name
or by user-visible label. If 'document' is omitted, the active document is
used.

Units and values: FreeCAD stores lengths in mm and angles in radians. Length
properties may be set as a number (mm) or a string such as "10 mm" or "1 in";
angle properties as a number (radians) or a string such as "45 deg". On read,
such properties come back as {"type":"Quantity","value":<number>,"unit":"...",
"str":"..."}. Vectors are {"x":..,"y":..,"z":..} or [x,y,z]. Placements are
{"base":<vector>,"rotation":{"axis":<vector>,"angle":<radians>}} or a
7-number [x,y,z,qx,qy,qz,qw] list. Colors are {"r":..,"g":..,"b":..,"a":..}
with components in 0..1. Links are an object name or {"name":"...",
"subElements":["Face1"]}. Property changes recompute the document by default.

Expressions: freecad_set_expression accepts FreeCAD expression syntax (the same
data a user types in the property editor or a spreadsheet formula, e.g.
"Spreadsheet.A1 * 2" or "10 mm"), not Python code.

Files: .FCStd is FreeCAD's native format (open/save). freecad_export_objects
writes STEP/IGES/BREP/STL/OBJ/PLY/AMF/OFF/DXF based on the file extension.

Commands: freecad_list_commands returns GUI command names available in loaded
workbenches; freecad_run_command runs one. Commands that open modal dialogs may
block until the dialog is closed, so prefer object/property tools when you need
an unattended change. Activate a workbench to load its commands and object
types.

Errors: a tool result with isError=true contains the error text (often a Python
traceback). Restarting FreeCAD is handled automatically: the next tool call
re-injects the in-process agent.

Tool groups (all names are prefixed freecad_):
- Documents/files: status, list_documents, new_document, open_document,
  save_document, close_document, get_document, set_document_property,
  import_file, export_objects.
- Objects/properties: list_objects, get_object, list_properties, get_property,
  set_property, set_properties, create_object, delete_object,
  container_add_object, get_dependencies, recompute, get_computed.
- Expressions/spreadsheets: set_expression, get_expression, clear_expression,
  list_spreadsheets, get_cell, set_cell, set_alias, spreadsheet_export,
  spreadsheet_import.
- Sketches: sketch_add_geometry, sketch_get_geometry, sketch_add_constraint,
  sketch_get_constraints, sketch_solve, sketch_delete_geometry,
  sketch_delete_constraint, sketch_set_datum, sketch_toggle_construction,
  sketch_add_external.
- Part/PartDesign/Mesh: part_boolean, container_add_object, mesh_boolean.
- Draft: draft_help, draft_make, draft_modify.
- Arch/BIM: arch_help, arch_make, arch_modify.
- TechDraw: techdraw_create_page, techdraw_add_view, techdraw_export,
  techdraw_list_pages.
- Commands/workbenches: list_commands, command_info, run_command,
  list_workbenches, activate_workbench.
- Views/selection: get_selection, set_selection, clear_selection,
  get_view_properties, set_view_property, view_action, get_camera, set_camera,
  save_view_image, activate_view.
- Inspection: list_properties, list_sub_elements, get_computed,
  measure_distance, get_report_view, get_python_console.
- Settings: get_preferences, get_preference, set_preference,
  list_unit_schemas, set_unit_schema.
- History: undo, redo, undo_state, begin_transaction, commit_transaction,
  abort_transaction.
"""


# ---------------------------------------------------------------------------
# MCP JSON-RPC loop
# ---------------------------------------------------------------------------

def _send(obj):
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def handle_message(msg):
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method is None:
        return None
    if method == "initialize":
        version = params.get("protocolVersion") or "2024-11-05"
        return _result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "freecad-mcp", "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        op = TOOL_OPS.get(name)
        if op is None:
            return _result(req_id, {
                "content": [{"type": "text", "text": "Unknown tool: %s" % name}],
                "isError": True,
            })
        try:
            result = call_op(op, args)
            text = json.dumps(result, indent=2, default=str)
            return _result(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except Exception as e:
            tb = traceback.format_exc()
            log("tool %s failed: %s" % (name, e))
            return _result(req_id, {
                "content": [{"type": "text", "text": "Error: %s\n\n%s" % (e, tb)}],
                "isError": True,
            })
    if method == "resources/list":
        return _result(req_id, {"resources": []})
    if method == "prompts/list":
        return _result(req_id, {"prompts": []})
    if method in ("shutdown", "exit"):
        return _result(req_id, {})
    return _error(req_id, -32601, "Method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            log("invalid JSON input")
            continue
        try:
            resp = handle_message(msg)
        except Exception:
            resp = _error(msg.get("id") if isinstance(msg, dict) else None,
                          -32603, "Internal error", traceback.format_exc())
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()

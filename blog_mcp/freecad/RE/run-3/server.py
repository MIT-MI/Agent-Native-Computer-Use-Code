#!/usr/bin/env python3
"""FreeCAD MCP server.

Speaks MCP JSON-RPC on stdin/stdout and drives the *running* FreeCAD
application through its own embedded Python interpreter (the same layer that
FreeCAD's Python console and macros use).  Nothing is driven by clicking or
typing into the window.
"""
import json
import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "deps"))

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "freecad-mcp"
SERVER_VERSION = "1.0"

INSTRUCTIONS = """\
This server operates the FreeCAD 0.20 application that is already running on this
desktop. It talks to FreeCAD through its own embedded Python interpreter - the
same API layer FreeCAD's Python console and its macros use - and every operation
runs on FreeCAD's main (GUI) thread, so changes appear immediately in the open
documents and in the 3D view. Nothing is done by clicking or typing into the
window.

What you can do: inspect documents, objects and every property; create, modify,
delete and re-parent objects; set expressions; recompute; read computed results
(volume, area, bounding box, centre of mass, distances, sketch degrees of
freedom, spreadsheet cell values); read and write FreeCAD parameters
(preferences); run FreeCAD commands; change the view and object display; open,
save, import and export files; and restart the application.

Tool groups
  * Inspect: freecad_app_info, freecad_list_documents, freecad_get_document,
    freecad_get_object, freecad_get_property, freecad_document_properties,
    freecad_list_object_types, freecad_object_schema, freecad_console.
  * Change: freecad_create_object, freecad_set_property, freecad_set_properties,
    freecad_set_expression, freecad_add_property, freecad_remove_property,
    freecad_delete_object, freecad_recompute, freecad_undo_redo.
  * Documents and files: freecad_new_document, freecad_open_document, freecad_save_document,
    freecad_close_document, freecad_import_file, freecad_export.
  * Computed outputs: freecad_measure (volume, area, bounding box, centre of mass, inertia,
    distance to another object or to a point, sketch degrees of freedom),
    freecad_spreadsheet, freecad_get_property.
  * Sketches and 2D: freecad_sketch, freecad_draft.
  * Application: freecad_list_commands, freecad_run_command, freecad_list_workbenches,
    freecad_activate_workbench, freecad_preferences, freecad_view, freecad_selection,
    freecad_set_view_object, freecad_list_display_modes, freecad_app_lifecycle.

Suggested first steps
  1. freecad_app_info            - version, open documents, selection, active workbench.
  2. freecad_list_documents      - names/labels/files of the open documents.
  3. freecad_get_document        - every object with all its properties.
  4. freecad_object_schema / freecad_list_object_types - what you can create and set.
  5. freecad_create_object / freecad_set_property / freecad_set_expression - change state.
  6. freecad_recompute           - recompute; failed features report their reason in the
                                   "console" field (FreeCAD's own report view text).
  7. freecad_measure / freecad_spreadsheet / freecad_get_property - computed outputs.
  8. freecad_save_document / freecad_export - write results to disk.

Values, units and formats
  * FreeCAD works internally in millimetres, degrees, mm^2 and mm^3; numeric
    property values are returned in those units. Properties that carry a unit also
    report a human-readable string in the object's "display" map (e.g. "10.00 mm").
  * A length/angle property accepts either a number (internal units) or a string
    with units: "10 mm", "1 in", "30 deg", "0.5 rad". To read a value in another unit,
    use freecad_get_property(unit="in") - it asks FreeCAD to convert. The unit scheme
    preference is BaseApp/Preferences/Units/UserSchema (0..8, see freecad_preferences
    action=list path=BaseApp/Preferences/Units for the names); it only changes how
    values are displayed, never the stored millimetres.
  * Vectors: [x, y, z] or {"x":..,"y":..,"z":..}.
  * Placements: {"position":[x,y,z], "rotation":{"axis":[x,y,z], "angle":degrees}}
    or {"position":[...], "rotation":{"yaw_pitch_roll":[y,p,r]}} or
    {"position":[...], "rotation":{"quaternion":[x,y,z,w]}} or [x,y,z,qx,qy,qz,qw].
  * Object references ("object" arguments) may be the object Name, its Label, or
    "DocumentName#ObjectName". Documents may be named by Name or Label.
  * Links: give the object name/label, or {"object":"Sketch","sub":["Edge1"]} for a
    link with sub-elements. Link lists take an array of those.
  * Colours: [r,g,b] (0..1) or "#rrggbb".
  * Expressions are ordinary user data - the same formulas the property editor
    accepts, e.g. "Spreadsheet.A1 * 2", "Box.Length + 5 mm", "max(Box.Length; 2)".
    In FreeCAD 0.20 they can only drive scalar (number/unit/string) properties; Vector
    and Placement properties must be set with freecad_set_property, using values you
    read from the spreadsheet yourself.
  * Object type ids look like "Part::Box", "Part::Cut", "Sketcher::SketchObject",
    "PartDesign::Body", "PartDesign::Pad", "Spreadsheet::Sheet", "App::Part",
    "Mesh::Feature". PartDesign features must live inside a PartDesign::Body: pass
    parent="Body" to freecad_create_object.

Saving, restarting, undo
  * A document is only on disk after freecad_save_document (use a .FCStd path for a
    new document). freecad_save_document reports the file path it wrote.
  * Each document reports both 'gui_modified' (the unsaved-changes flag FreeCAD's
    window shows) and 'modified' (FreeCAD's internal isSaved flag, which stays false
    for scripted changes). gui_modified is the reliable one, and freecad_save_document
    clears it by also running FreeCAD's own save for the active document.
  * Every state-changing tool call runs inside a FreeCAD transaction, so it appears
    in the document's undo stack (freecad_undo_redo action=status lists them, and a
    user can undo it from the GUI too). freecad_app_lifecycle(action=restart)
    restarts the whole application - unsaved documents are lost, saved files survive
    and can be reopened with freecad_open_document.
  * FreeCAD's report view (messages, warnings, errors) is captured around every call
    and appended to the result as "FreeCAD report view during this call:".

Honest limitations
  * A command that opens a modal dialog (some freecad_run_command commands, e.g.
    file dialogs) blocks FreeCAD's main thread; the call then fails with a timeout.
    Prefer the data-level tools over dialog-opening commands.
  * Results are JSON text; very large documents are truncated in the middle. Read
    individual objects instead of dumping a huge document.
  * freecad_preferences changes are written to the user configuration file; some of
    them only take effect after a restart.
"""


def _tool(name, description, properties=None, required=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    }


OBJ = {"type": "string", "description": "Object reference: Name, Label, or 'Document#Object'."}
DOC = {"type": "string", "description": "Document name or Label. Omit for the active document."}

TOOLS = [
    _tool("freecad_app_info",
          "Report the running FreeCAD: version, process id, active workbench, available workbenches, "
          "open documents (name/label/file/object count/unsaved flag), the current selection, any modal "
          "dialog that is open (which is why a call may time out) and the user config, macro and temp "
          "directories. Call this first.",
          {}),
    _tool("freecad_list_documents",
          "List every open document with its name, label, file path and object names. "
          "'gui_modified' is the flag FreeCAD's window shows as \"unsaved changes\"; 'modified' is "
          "FreeCAD's internal isSaved flag (it stays false for changes made outside a document "
          "transaction, so prefer gui_modified when deciding whether to save).", {}),
    _tool("freecad_get_document",
          "Dump one document: for every object its name, label, type id, state (Valid/Touched/Invalid), "
          "visibility, parents, all property values (FreeCAD internal units: mm, degrees, mm^2, mm^3), "
          "a 'display' map with unit-annotated strings, and the expressions in use. Also reports "
          "root objects, the topological (dependency) order and the gui_modified flag. "
          "Use objects=[...] to restrict to some objects and properties=false for a compact listing.",
          {"document": DOC, "objects": {"type": "array", "items": {"type": "string"},
                                        "description": "Only these objects (Name or Label)."},
           "properties": {"type": "boolean", "description": "Include property values (default true)."}}),
    _tool("freecad_get_object",
          "Read one object: type, label, id, state, validity, visibility, parent objects, every property "
          "value, unit-annotated display strings, active expressions and the list of properties this type "
          "supports.",
          {"document": DOC, "object": OBJ, "properties": {"type": "boolean"}}, ["object"]),
    _tool("freecad_get_property",
          "Read one property in detail: property type id, property group, current value (FreeCAD "
          "internal units), unit-annotated string, allowed enumeration values, status flags and "
          "documentation. Pass unit='in'/'cm'/'deg'/... to also get the value converted into that "
          "unit (works for any property that carries a unit).",
          {"document": DOC, "object": OBJ, "property": {"type": "string"},
           "unit": {"type": "string"}}, ["object", "property"]),
    _tool("freecad_list_object_types",
          "List the object type ids that can be created in a document, e.g. 'Part::Box', "
          "'Part::Cylinder', 'Part::Cut', 'Sketcher::SketchObject', 'Spreadsheet::Sheet', "
          "'PartDesign::Body', 'App::Part', 'Mesh::Feature'. Optionally filter by substring. "
          "(It loads the installed workbench modules first, which takes a second or two.)",
          {"document": DOC, "filter": {"type": "string"}}),
    _tool("freecad_object_schema",
          "Describe an object type (or a live object): every property with its type id, group, current "
          "or default value, enumeration choices and documentation. Use this to discover the property "
          "names to read or set.",
          {"type": {"type": "string", "description": "Object type id, e.g. 'Part::Cylinder'."},
           "document": DOC, "object": OBJ}),
    _tool("freecad_create_object",
          "Create a new object of a given type in a document (like the toolbar/menu commands do). "
          "Returns the new object's name, label and type. Optional 'properties' are applied immediately.",
          {"type": {"type": "string"}, "document": DOC, "name": {"type": "string"},
           "label": {"type": "string"}, "properties": {"type": "object"},
           "parent": {"type": "string", "description": "Put the new object inside this container "
                      "(App::Part, App::DocumentObjectGroup or PartDesign::Body)."}}, ["type"]),
    _tool("freecad_set_property",
          "Set one property of one object and return the stored value. Values: numbers in FreeCAD "
          "internal units, or strings with units ('12 mm', '30 deg'); vectors [x,y,z]; placements as "
          "described in the server instructions; links by object name/label (or {'object':..,'sub':[..]}); "
          "colours [r,g,b] or '#rrggbb'; booleans; strings. Fails with the list of valid property names "
          "if the property does not exist.",
          {"document": DOC, "object": OBJ, "property": {"type": "string"},
           "value": {"description": "New value (any JSON type)."}}, ["object", "property"]),
    _tool("freecad_set_properties",
          "Set several properties of one object at once; returns the values that were stored and a map "
          "of per-property errors (setting one bad property does not stop the others).",
          {"document": DOC, "object": OBJ, "properties": {"type": "object"}}, ["object", "properties"]),
    _tool("freecad_set_expression",
          "Attach or clear a FreeCAD expression on a property (the same formulas the property editor "
          "accepts, e.g. 'Spreadsheet.A1 * 2', 'Box.Length + 5 mm', 'max(Box.Length; Box.Width)', "
          "'Box.Shape.BoundBox.XLength'). Pass expression=null or \"\" to remove it. Returns the "
          "expression and the recomputed value. Note: this FreeCAD version can only drive scalar "
          "(number/unit/string) properties this way - Vector and Placement properties must be set "
          "with freecad_set_property.",
          {"document": DOC, "object": OBJ, "property": {"type": "string"},
           "expression": {"type": ["string", "null"]}}, ["object", "property"]),
    _tool("freecad_delete_object",
          "Delete an object from its document; returns the remaining object names.",
          {"document": DOC, "object": OBJ}, ["object"]),
    _tool("freecad_recompute",
          "Recompute the document (equivalent to the refresh toolbar button). Returns how many objects "
          "were recomputed, the state of every object that is not plain Valid, and everything FreeCAD "
          "printed to its report view during the recompute - failed features report their error there.",
          {"document": DOC}),
    _tool("freecad_new_document",
          "Create a new empty document and make it active.",
          {"name": {"type": "string"}, "label": {"type": "string"}}),
    _tool("freecad_open_document",
          "Open an existing file from disk as a new document (.FCStd, .step, .iges, .stl, .obj, ...). "
          "Returns the document name and its objects.",
          {"path": {"type": "string", "description": "Absolute path."}}, ["path"]),
    _tool("freecad_save_document",
          "Save a document. With path it saves as that file (use the .FCStd extension); without path it "
          "saves to the document's existing file. Returns the resulting path and whether the document is "
          "now unmodified. Saving never opens a dialog.",
          {"document": DOC, "path": {"type": "string"}}),
    _tool("freecad_close_document",
          "Close a document, optionally saving it first (save=true only works if it has a file name).",
          {"document": DOC, "save": {"type": "boolean"}}),
    _tool("freecad_export",
          "Export objects to a file; the format follows the file extension: .stl/.obj/.ply/.amf/.3mf/"
          ".wrl/.off/.gltf/.x3d (mesh), .step/.stp/.iges/.igs/.brep (B-rep), .dxf/.dwg and .svg "
          "(2D/Draft), .fcstd (native document). Other extensions are passed to FreeCAD's own importer "
          "exporter registry. Without 'objects' the whole document is exported. Returns the path, "
          "format and byte size; if FreeCAD writes no file the error lists the formats this build "
          "supports.",
          {"path": {"type": "string"}, "document": DOC,
           "objects": {"type": "array", "items": {"type": "string"}}}, ["path"]),
    _tool("freecad_import_file",
          "Import a file's geometry into a document (STEP/IGES/BREP/STL/OBJ/DXF...), or open it as a "
          "document when it is a .FCStd. Returns the document and its objects.",
          {"path": {"type": "string"}, "document": DOC}, ["path"]),
    _tool("freecad_measure",
          "Computed geometry of one object: volume (mm^3), surface area (mm^2), total edge length (mm), "
          "vertex/edge/face/shell/solid counts, bounding box, centre of mass, inertia matrix, placement, "
          "and for sketches the geometry/constraint counts, degrees of freedom and solved state; for "
          "meshes the point/facet counts, area and volume. Pass 'other' to also get the minimum "
          "distance between this object's shape and another object's shape (mm), with the closest "
          "points on each; pass point=[x,y,z] for the distance to a point; pass check=true to run "
          "FreeCAD's topology check as well - its findings are reported in the reply's "
          "\"FreeCAD report view during this call\" section, and is_valid repeats the summary.",
          {"document": DOC, "object": OBJ, "other": OBJ,
           "point": {"type": "array", "items": {"type": "number"}},
           "check": {"type": "boolean"}}, ["object"]),
    _tool("freecad_selection",
          "Read or change the current selection. action=get returns selected objects and sub-elements; "
          "set/add take objects=[\"Box\", {\"object\":\"Sketch\",\"subelements\":[\"Edge1\"]}]; clear and "
          "remove also work.",
          {"action": {"type": "string", "enum": ["get", "set", "add", "clear", "remove"]},
           "objects": {"type": "array"}}, []),
    _tool("freecad_view",
          "Control the 3D view of the active document. action: fit_all, isometric, axonometric, front, "
          "top, right, rear, bottom, left, dimetric, trimetric, default_orientation, zoom_in, zoom_out, "
          "redraw, rotate_left, rotate_right, get_camera, set_view_direction (direction=[x,y,z]), "
          "set_camera_type (camera_type=Orthographic|Perspective), set_axis_cross (visible), "
          "set_navigation, list_camera_types, set_visibility (objects={'Box':false}).",
          {"action": {"type": "string"}, "direction": {"type": "array", "items": {"type": "number"}},
           "camera_type": {"type": "string"}, "visible": {"type": "boolean"},
           "navigation": {"type": "string"}, "objects": {"type": "object"}, "document": DOC},
          ["action"]),
    _tool("freecad_spreadsheet",
          "Read and write spreadsheet cells. action=get_cell/set_cell (cell='A1', value may be a number "
          "or a formula string starting with '='), set_alias (cell, alias), get_range/set_range "
          "(range='A1:C4', cells={'A1':'=1+2'}), list_cells (range optional), clear, clear_all, recompute. "
          "get_cell returns both the stored contents (the formula) and the computed value.",
          {"action": {"type": "string"}, "document": DOC, "sheet": {"type": "string"},
           "cell": {"type": "string"}, "range": {"type": "string"},
           "value": {}, "alias": {"type": "string"}, "cells": {"type": "object"}}, ["sheet"]),
    _tool("freecad_preferences",
          "Read or write FreeCAD parameters (Edit > Preferences). path is a parameter group such as "
          "'BaseApp/Preferences/Mod/Part' or 'BaseApp/Preferences/General' (the 'User parameter:' prefix "
          "is added for you). action=get needs name (type optional: bool/int/float/string); action=set "
          "writes name=value; action=list dumps the group and its subgroups; action=groups lists "
          "subgroup names; action=remove deletes a value. Changes are written to the user config file.",
          {"action": {"type": "string", "enum": ["get", "set", "list", "groups", "remove"]},
           "path": {"type": "string"}, "name": {"type": "string"}, "value": {},
           "type": {"type": "string"},
           "default": {"description": "Value to return if the parameter is not set (used with action=get)."}},
          ["action", "path"]),
    _tool("freecad_sketch",
          "Work with a sketch. action=add_geometry (geometry=[{\"type\":\"line\",\"start\":[x,y,z],"
          "\"end\":[x,y,z]}, {\"type\":\"circle\",\"center\":[x,y,0],\"radius\":5}, {\"type\":\"arc\","
          "\"start\":..,\"mid\":..,\"end\":..}, {\"type\":\"point\",\"point\":[x,y,0]}, "
          "{\"type\":\"rectangle\",\"corner1\":[x,y],\"corner2\":[x,y],\"coincident\":true}, "
          "{\"type\":\"polygon\",\"points\":[[x,y],..]}]; optional construction=true; "
          "\"coincident\":true also adds the corner coincidences), add_constraint "
          "(constraint={\"type\":\"DistanceX\",\"first\":0,\"value\":10} or "
          "{\"type\":\"Coincident\",\"first\":0,\"first_pos\":1,\"second\":1,\"second_pos\":1}, or "
          "constraints=[...] for several at once), set_datum (index, value), delete_geometry, "
          "delete_constraint, toggle_construction, solve, get (geometry + constraints), clear.",
          {"action": {"type": "string"}, "document": DOC, "sketch": {"type": "string"},
           "geometry": {}, "constraint": {"type": "object"}, "constraints": {"type": "array"},
           "index": {"type": "integer"}, "value": {}}, ["sketch", "action"]),
    _tool("freecad_list_commands",
          "List FreeCAD commands (the application's action registry, e.g. 'Std_ViewFitAll', "
          "'Part_Box', 'Std_Save'). Optional substring filter and limit.",
          {"filter": {"type": "string"}, "limit": {"type": "integer"}}),
    _tool("freecad_run_command",
          "Run a FreeCAD command by name, exactly as if its menu entry or toolbar button was triggered "
          "(e.g. 'Std_New', 'Part_Box', 'Std_ViewFitAll', 'Std_Save', 'Std_Undo'). The optional 'args' "
          "string is the parameter FreeCAD passes to the command. If the command opens a dialog the "
          "reply reports it under 'modal_dialog' (and the dialog stays open in the window unless you "
          "pass dismiss_dialog=true). Prefer the data-level tools: they never open dialogs.",
          {"command": {"type": "string"}, "args": {"type": "string"},
           "dismiss_dialog": {"type": "boolean"}}, ["command"]),
    _tool("freecad_list_workbenches",
          "List the installed workbenches and their menu names.", {}),
    _tool("freecad_activate_workbench",
          "Switch FreeCAD to a workbench (e.g. 'PartDesignWorkbench'), like choosing it in the "
          "workbench selector. Some tools only appear/work in their own workbench.",
          {"workbench": {"type": "string"}}, ["workbench"]),
    _tool("freecad_console",
          "Return what FreeCAD last printed to its Report view (messages, warnings and errors, "
          "each line prefixed with the time). Every other tool call also appends the report-view "
          "lines produced during that call under a 'FreeCAD report view during this call:' heading. "
          "Optional 'lines' limits how many lines are returned (default 60).",
          {"lines": {"type": "integer"}}),
    _tool("freecad_set_view_object",
          "Change the display properties of an object's view provider: properties={'Visibility':true, "
          "'ShapeColor':[0.8,0.2,0.2], 'Transparency':50, 'DisplayMode':'Flat Lines', 'LineWidth':2, "
          "'DrawStyle':'Solid', 'PointSize':4}. Returns the stored values and per-property errors.",
          {"document": DOC, "object": OBJ, "properties": {"type": "object"}}, ["object", "properties"]),
    _tool("freecad_list_display_modes",
          "List the display modes a view provider supports plus its current colours, transparency, "
          "point size, line width and visibility.",
          {"document": DOC, "object": OBJ}, ["object"]),
    _tool("freecad_draft",
          "Draft workbench operations - the same functions its toolbar buttons call. action: "
          "line (start,end), wire (points,closed,face), circle (radius,placement), rectangle "
          "(length,width,placement), polygon (sides,radius,inscribed,placement), bspline (points,closed), "
          "bezcurve (points,closed), point (point), text (text,placement), shapestring "
          "(text,font,size,tracking), dimension (start,end,dimline), array (objects, kind='ortho' with "
          "spacing_x/spacing_y/spacing_z and count_x/count_y/count_z, or kind='polar' with center, "
          "count, angle, or radius/tangent_distance/symmetry/axis, use_link), clone/copy (objects), move (objects, "
          "vector, copy), rotate (objects, angle, center, axis, copy), scale (objects, scale, center, "
          "copy), offset (objects, vector=[x,y,z] or distance along x, copy, bind), upgrade/downgrade "
          "(objects, delete), sketch "
          "(objects, autoconstraints), layer (name, line_color, shape_color, line_width), facebinder "
          "(objects). Points are [x,y,z]; placements as in the server instructions. Returns the names "
          "of the objects created.",
          {"action": {"type": "string"}, "document": DOC,
           "start": {"type": "array"}, "end": {"type": "array"}, "point": {"type": "array"},
           "points": {"type": "array"}, "closed": {"type": "boolean"}, "face": {"type": "boolean"},
           "radius": {"type": "number"}, "placement": {}, "length": {"type": "number"},
           "width": {"type": "number"}, "sides": {"type": "integer"}, "inscribed": {"type": "boolean"},
           "text": {"type": "string"}, "font": {"type": "string"}, "size": {"type": "number"},
           "tracking": {"type": "number"}, "dimline": {"type": "array"},
           "objects": {"type": "array"}, "kind": {"type": "string"},
           "spacing_x": {"type": "array"}, "spacing_y": {"type": "array"}, "spacing_z": {"type": "array"},
           "count_x": {"type": "integer"}, "count_y": {"type": "integer"}, "count_z": {"type": "integer"},
           "count": {"type": "integer"}, "angle": {"type": "number"}, "angle_step": {"type": "number"},
           "tangent_distance": {"type": "number"}, "symmetry": {"type": "integer"},
           "center": {"type": "array"}, "axis": {"type": "array"}, "vector": {"type": "array"},
           "scale": {"type": "array"}, "distance": {"type": "number"}, "copy": {"type": "boolean"},
           "use_link": {"type": "boolean"}, "delete": {"type": "boolean"},
           "bind": {"type": "boolean"},
           "autoconstraints": {"type": "boolean"}, "name": {"type": "string"},
           "line_color": {"type": "array"}, "shape_color": {"type": "array"},
           "line_width": {"type": "number"}},
          ["action"]),
    _tool("freecad_document_properties",
          "Read or write the document-level information that FreeCAD shows under File > Project "
          "information: Label, Comment, Company, License, LicenseURL, CreatedBy, LastModifiedBy, "
          "CreationDate, LastModifiedDate, Uid, ShowHidden, FileName, Tip (the feature that is "
          "computed last) and Meta. action=get returns them all; action=set needs property and "
          "value (Tip takes an object reference).",
          {"action": {"type": "string", "enum": ["get", "set"]}, "document": DOC,
           "property": {"type": "string"}, "value": {}}, ["action"]),
    _tool("freecad_add_property",
          "Add a new dynamic property to an object, the way Python features do "
          "(property_type is an App property type such as 'App::PropertyLength', "
          "'App::PropertyString', 'App::PropertyFloat', 'App::PropertyBool', "
          "'App::PropertyVector', 'App::PropertyLink'; group and doc are display metadata). "
          "Optional value is stored immediately. Returns the new property and its value.",
          {"document": DOC, "object": OBJ, "property_type": {"type": "string"},
           "name": {"type": "string"}, "group": {"type": "string"}, "doc": {"type": "string"},
           "value": {}}, ["object", "property_type", "name"]),
    _tool("freecad_remove_property",
          "Remove a dynamic property from an object; returns the remaining property names.",
          {"document": DOC, "object": OBJ, "name": {"type": "string"}}, ["object", "name"]),
    _tool("freecad_undo_redo",
          "Undo, redo, inspect or clear a document's undo stack (action=undo/redo/status/clear). "
          "Returns the undo/redo counts and the object names afterwards.",
          {"action": {"type": "string", "enum": ["undo", "redo", "status", "clear"]}, "document": DOC}),
    _tool("freecad_app_lifecycle",
          "Application lifecycle via the runtime: action=status (process ids), start, stop or restart. "
          "Restarting loses unsaved documents and takes a few seconds; the MCP re-attaches to the new "
          "process automatically on the next call.",
          {"action": {"type": "string", "enum": ["status", "start", "stop", "restart"]}}, ["action"]),
]

TOOL_TO_OP = {
    "freecad_app_info": "app_info",
    "freecad_list_documents": "list_documents",
    "freecad_get_document": "get_document",
    "freecad_get_object": "get_object",
    "freecad_get_property": "get_property",
    "freecad_list_object_types": "list_object_types",
    "freecad_object_schema": "object_schema",
    "freecad_create_object": "create_object",
    "freecad_set_property": "set_property",
    "freecad_set_properties": "set_properties",
    "freecad_set_expression": "set_expression",
    "freecad_delete_object": "delete_object",
    "freecad_recompute": "recompute",
    "freecad_new_document": "new_document",
    "freecad_open_document": "open_document",
    "freecad_save_document": "save_document",
    "freecad_close_document": "close_document",
    "freecad_export": "export",
    "freecad_import_file": "import_file",
    "freecad_measure": "measure",
    "freecad_selection": "selection",
    "freecad_view": "view",
    "freecad_spreadsheet": "spreadsheet",
    "freecad_preferences": "preferences",
    "freecad_sketch": "sketch",
    "freecad_list_commands": "list_commands",
    "freecad_run_command": "run_command",
    "freecad_list_workbenches": "list_workbenches",
    "freecad_activate_workbench": "activate_workbench",
    "freecad_console": "console",
    "freecad_set_view_object": "set_view_object",
    "freecad_list_display_modes": "list_display_modes",
    "freecad_undo_redo": "undo_redo",
    "freecad_draft": "draft",
    "freecad_document_properties": "document_properties",
    "freecad_add_property": "add_property",
    "freecad_remove_property": "remove_property",
}

MAX_CHARS = 30000


def _bridge():
    global _BRIDGE
    try:
        _BRIDGE
    except NameError:
        import fc_bridge
        _BRIDGE = fc_bridge.FreeCADBridge(os.path.join(HERE, "fc_app.py"))
    return _BRIDGE


def _dump(obj, limit=MAX_CHARS):
    txt = json.dumps(obj, indent=1, default=str, ensure_ascii=False, allow_nan=False)
    if len(txt) > limit:
        head = txt[: int(limit * 0.6)]
        tail = txt[-int(limit * 0.35):]
        txt = (head + "\n... [truncated: %d of %d characters shown; "
               "ask for fewer objects or use freecad_get_object per object] ...\n" % (limit, len(txt)) + tail)
    return txt


_REQUIRED = {t["name"]: t["inputSchema"].get("required", []) for t in TOOLS}
_KNOWN_ARGS = {t["name"]: set(t["inputSchema"].get("properties", {})) for t in TOOLS}


def _validate(name, args):
    missing = [k for k in _REQUIRED.get(name, []) if k not in args or args[k] is None]
    if missing:
        raise ValueError("missing required argument(s): %s. Accepted arguments: %s" %
                         (", ".join(missing), ", ".join(sorted(_KNOWN_ARGS.get(name, [])))))
    unknown = [k for k in args if k not in _KNOWN_ARGS.get(name, set())]
    if unknown:
        raise ValueError("unknown argument(s) %s; accepted arguments: %s" %
                         (", ".join(sorted(unknown)), ", ".join(sorted(_KNOWN_ARGS.get(name, [])))))


def _call_tool(name, args):
    if name == "freecad_app_lifecycle":
        import fc_bridge
        action = (args or {}).get("action", "status")
        if action == "status":
            pids = [p for p, _ in fc_bridge.find_freecad_pids()]
            return {"action": "status", "freecad_pids": pids, "running": bool(pids)}
        rc, out, err = fc_bridge.cua_app(action)
        b = _bridge()
        b.detach()
        return {"action": action, "returncode": rc, "stdout": out, "stderr": err}
    op = TOOL_TO_OP.get(name)
    if op is None:
        raise ValueError("unknown tool %r; available tools: %s" %
                         (name, ", ".join(sorted(t["name"] for t in TOOLS))))
    args = args or {}
    _validate(name, args)
    return _bridge().call(op, args)


def _send(msg):
    sys.stdout.write(json.dumps(msg, default=str) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": req_id, "error": err})


def handle(msg):
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        _result(req_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })
        return
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return
    if method == "ping":
        _result(req_id, {})
        return
    if method == "tools/list":
        _result(req_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            data = _call_tool(name, args)
        except Exception as e:
            tb = traceback.format_exc()
            sys.stderr.write(tb + "\n")
            txt = "ERROR from %s: %s" % (name, e)
            _result(req_id, {"content": [{"type": "text", "text": txt}], "isError": True})
            return
        console = None
        if isinstance(data, dict) and "console" in data:
            console = data.pop("console")
        text = _dump(data)
        if console:
            text += "\n\nFreeCAD report view during this call:\n" + _dump(console, 6000)
        _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        return
    if method in ("resources/list",):
        _result(req_id, {"resources": []})
        return
    if method in ("prompts/list",):
        _result(req_id, {"prompts": []})
        return
    if req_id is None:
        return
    _error(req_id, -32601, "method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            sys.stderr.write("bad json: %s\n" % e)
            continue
        try:
            handle(msg)
        except Exception as e:
            sys.stderr.write("handler error: %s\n%s\n" % (e, traceback.format_exc()))
            if msg.get("id") is not None:
                _error(msg["id"], -32603, "internal error: %s" % e)


if __name__ == "__main__":
    main()

"""Code typed into the GUI is blocked, as in the earlier FreeCAD GUI runs (same detector)."""
import json
import pathlib
import re

REJECTED = 'Rejected: scripts, code and console input are not allowed. Nothing was typed. Use the application GUI controls.'
FILES = r'(?!FCStd\b|FCBak\b|STEP\b|STP\b|STL\b|IGES\b|IGS\b|BREP\b|BRP\b|OBJ\b|PNG\b)'
CODE = re.compile('|'.join([
    r'\n',
    r'\b(?:import|from|for|while|if|elif|else|def|class|with|try|except|lambda|return|exec|eval|print|del|assert)\b\s*[\s(:\'"]',
    r'\b(?:App|FreeCAD|FreeCADGui|Gui|Part|PartDesign|Sketcher|Mesh|MeshPart|Draft|Base|Import|os|sys|subprocess)\.[A-Za-z_]',
    r'\b(?:ActiveDocument|getObject|addObject|getDocument|newDocument|removeObject|recompute|saveAs|setExpression|exportStep|PropertiesList|__\w+__)\b',
    r'\b[A-Za-z_][\w.]*\s*\(',
    r'(?:^|[;\s])[A-Za-z_][\w.\[\]\'"]*\s*=(?!=)\s*\S',
    r'\b[a-z_]\w*\.' + FILES + r'[A-Z]\w*',
    r'>>>',
]))


def looks_like_code(text):
    return bool(CODE.search(text or ''))

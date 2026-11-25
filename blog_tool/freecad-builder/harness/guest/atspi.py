#!/usr/bin/python3
"""Print the accessible desktop tree; errors remain observations."""
import json
import pyatspi

def visit(node):
    row = {'name': node.name, 'role': node.getRoleName(), 'children': []}
    for index in range(node.childCount):
        try: row['children'].append(visit(node.getChildAtIndex(index)))
        except Exception as exc: row['children'].append({'error': str(exc)})
    return row
print(json.dumps(visit(pyatspi.Registry.getDesktop(0))))

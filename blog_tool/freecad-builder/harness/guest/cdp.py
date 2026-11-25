#!/usr/bin/python3
"""One JSON DevTools protocol request to a declared local service."""
import argparse
import json
import websocket
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('websocket_url'); parser.add_argument('method'); parser.add_argument('params', nargs='?', default='{}')
args = parser.parse_args()
with websocket.create_connection(args.websocket_url) as connection:
    connection.send(json.dumps({'id': 1, 'method': args.method, 'params': json.loads(args.params)}))
    while True:
        reply = json.loads(connection.recv())
        print(json.dumps(reply), flush=True)
        if reply.get('id') == 1: break

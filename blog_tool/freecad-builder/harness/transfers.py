"""Chunked stdin writes; large payloads never sit in one buffer."""
import io

MAX_BUFFER_SIZE = 1 << 20


def write_stream(stdin, source, *, eof=False):
    if isinstance(source, str): source = source.encode()
    if isinstance(source, bytes): source = io.BytesIO(source)
    while chunk := source.read(MAX_BUFFER_SIZE):
        stdin.write(chunk)
        stdin.drain()
    if eof:
        stdin.write_eof()
        stdin.drain()

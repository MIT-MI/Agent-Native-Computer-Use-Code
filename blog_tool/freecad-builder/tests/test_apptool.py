"""apptool: generic inspection of a running application, tested without one."""
import json
import os
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('agent', 'harness', 'harness/guest', 'harness/re_tools')]
import pytest
import apptool


MAPS = '''55d0c0000000-55d0c0001000 r--p 00000000 08:01 1 /opt/app/bin/app
55d0c0001000-55d0c0003000 r-xp 00001000 08:01 1 /opt/app/bin/app
55d0c1000000-55d0c1021000 rw-p 00000000 00:00 0 [heap]
7f0000001000-7f0000002000 r-xp 00001000 08:01 2 /opt/app/lib/libcore.so (deleted)
7f0000002000-7f0000003000 rw-p 00002000 08:01 2 /opt/app/lib/libcore.so (deleted)
7ffc00000000-7ffc00021000 rw-p 00000000 00:00 0 [stack]
'''


class FakeMemory(apptool.Memory):
    """Bytes at fixed addresses, read like /proc/PID/mem."""
    def __init__(self, blocks): self.blocks = blocks
    def read(self, address, size):
        for start, data in self.blocks.items():
            if start <= address < start + len(data): return data[address - start:address - start + size]
        return b''


def test_apptool_reads_maps_and_names():
    maps = apptool.parse_maps(MAPS)
    found = {m['path']: m for m in apptool.modules(maps)}
    assert found['/opt/app/lib/libcore.so']['base'] == 0x7f0000000000 and found['/opt/app/bin/app']['end'] == 0x55d0c0003000
    assert [row['path'] for row in apptool.Memory.regions(None, maps)] == ['[heap]', '/opt/app/lib/libcore.so (deleted)', '[stack]']
    assert apptool.simple_name('N3App8DocumentE') == 'App::Document' and apptool.simple_name('7QObject') == 'QObject'
    assert apptool.simple_name('St9exception') == 'std::exception' and apptool.simple_name('N3App4ListIiEE') is None
    with patch('apptool.shutil.which', return_value=None):
        assert apptool.demangle(['_ZTVN3App8DocumentE', '_ZN3App4ListIiE3addEv']) == {
            '_ZTVN3App8DocumentE': 'vtable for App::Document', '_ZN3App4ListIiE3addEv': '_ZN3App4ListIiE3addEv'}
    assert apptool.number('0x10') == 16 and apptool.number('2.5') == 2.5 and apptool.number('-3') == -3


def test_apptool_finds_vtables_in_a_real_library_and_objects_in_memory():
    import ctypes.util
    name = ctypes.util.find_library('stdc++')
    paths = [p for p in ('/usr/lib/x86_64-linux-gnu/' + str(name), '/lib/x86_64-linux-gnu/' + str(name)) if os.path.exists(p)]
    if not paths: pytest.skip('no libstdc++ on this host')
    base = 0x7f1200000000
    symbols = apptool.Symbols([{'start': base, 'end': base + (64 << 20), 'perms': 'r-xp', 'offset': 0, 'path': paths[0]}])
    with patch('apptool.shutil.which', return_value=None):
        rows = symbols.vtables(__import__('re').compile(r'^std::exception$'))
    assert len(rows) == 1 and rows[0][3] == rows[0][2] + 16 and rows[0][2] > base
    module, name, offset = symbols.at(rows[0][3])
    assert name == '_ZTVSt9exception' and offset == 16
    # A live object starts with its class's address point.
    heap = {'start': 0x1000, 'end': 0x3000, 'perms': 'rw-p', 'offset': 0, 'path': '[heap]'}
    memory = FakeMemory({0x1000: bytes(0x800) + struct.pack('<Q', rows[0][3]) + bytes(0x17f8)})
    assert [hit[:2] for hit in apptool.search_words(memory, [heap], [rows[0][3]])] == [(0x1800, 0)]
    assert [hit[0] for hit in apptool.search(memory, [heap], [struct.pack('<Q', rows[0][3])], 8)] == [0x1800]


def test_apptool_search_does_not_report_a_match_twice_across_chunks():
    region = {'start': 0, 'end': 96, 'perms': 'rw-p', 'offset': 0, 'path': ''}
    data = bytearray(96); data[30:34] = b'ABCD'; data[62:66] = b'ABCD'
    with patch.object(apptool, 'CHUNK', 32):
        assert [hit[0] for hit in apptool.search(FakeMemory({0: bytes(data)}), [region], [b'ABCD'], 1)] == [30, 62]


def qt_image():
    """A Qt 5 meta-object: class Demo with signal changed(int), slot setValue(QString), property QString title."""
    names = [b'Demo', b'changed', b'', b'value', b'setValue', b'title']
    table, storage = b'', b''
    for index, name in enumerate(names):
        offset = 24 * (len(names) - index) + len(storage)
        table += struct.pack('<iiI4xq', -1, len(name), 0, offset); storage += name + b'\0'
    data = [7, 0, 0, 0, 2, 14, 1, 24, 0, 0, 0, 0, 0, 1,
            1, 1, 27, 2, 0x06, 4, 1, 30, 2, 0x0a,
            5, 10, 0x03,
            43, 2, 3, 43, 10, 3]
    return {0x1000: struct.pack('<6Q', 0, 0x2000, 0x3000, 0, 0, 0), 0x2000: table + storage, 0x3000: struct.pack('<%dI' % len(data), *data)}


def test_apptool_decodes_qt_signals_slots_and_properties():
    row = apptool.qt_class(FakeMemory(qt_image()), 0x1000, {})
    assert row['name'] == 'Demo' and row['parent'] is None
    assert row['methods'] == [
        {'kind': 'signal', 'access': 'public', 'signature': 'changed(int)', 'returns': 'void'},
        {'kind': 'slot', 'access': 'public', 'signature': 'setValue(QString)', 'returns': 'void'}]
    assert row['properties'] == [{'name': 'title', 'type': 'QString', 'access': 'rw'}]
    other = qt_image(); other[0x3000] = struct.pack('<14I', 12, *[0] * 13)
    assert apptool.qt_class(FakeMemory(other), 0x1000, {}) == {'name': None, 'revision': 12}


def test_apptool_watch_and_trace_summaries():
    before, after = struct.pack('<IfQ', 1, 2.0, 7), struct.pack('<IfQ', 1, 3.5, 7)
    assert apptool.changed_fields([before, before, after]) == [(4, [struct.pack('<f', 2.0), struct.pack('<f', 3.5)])]
    assert apptool.changed_fields([before, before]) == []
    summary = apptool.summarize_trace('''
101 openat(AT_FDCWD</home/u>, "/home/u/part.doc", O_RDONLY|O_CLOEXEC) = 12</home/u/part.doc>
101 openat(AT_FDCWD</home/u>, "/home/u/part.doc.tmp", O_WRONLY|O_CREAT|O_TRUNC, 0644) = 13</home/u/part.doc.tmp>
102 write(13</home/u/part.doc.tmp>, "PK"..., 2 <unfinished ...>
102 <... write resumed>) = 2
101 rename("/home/u/part.doc.tmp", "/home/u/part.doc") = 0
101 sendmsg(5<UNIX-STREAM:[77->78]>, {msg_name=NULL}, MSG_NOSIGNAL) = 24
103 execve("/usr/bin/helper", ["helper"], 0x7ffd /* 3 vars */) = 0
'''.splitlines())
    assert summary['calls'] == {'openat': 2, 'write': 1, 'rename': 1, 'sendmsg': 1, 'execve': 1}
    assert summary['read'] == {'/home/u/part.doc': 1}
    assert summary['written'] == {'/home/u/part.doc.tmp': 3, '/home/u/part.doc': 1}
    assert summary['sockets'] == {'UNIX-STREAM:[77->78]': 1} and summary['executed'] == ['/usr/bin/helper']

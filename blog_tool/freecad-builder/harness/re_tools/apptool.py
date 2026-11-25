#!/usr/bin/python3
"""apptool: inspect the running application, for analysis. It only reads and observes.

Every command finds the application's current process itself (process ids change across restarts)
unless --pid is given. Addresses and values accept hex (0x...) or decimal. Nothing is filtered or
cut: a command without a pattern prints a summary per module, and a pattern lists every match.

  apptool pid                      the application's processes
  apptool modules [REGEX]          mapped files with their load address and size
  apptool rtti [REGEX]             C++ classes with their vtables, from each module's symbols.
                                   A live object of a polymorphic class starts with the "object"
                                   address shown (the vtable's address point).
  apptool qt [REGEX]               Qt classes with their signals, slots, invokable methods and properties
  apptool find VALUE [--type ptr|u64|i64|u32|i32|f64|f32|str] [--tol T]
  apptool find --class REGEX       live objects of the matching C++ classes
                                   addresses in writable memory (--all: any readable memory)
  apptool dump ADDR [LEN]          every 8-byte word as hex, integers and floats, with the module,
                                   symbol or text each one points to
  apptool watch (ADDR ... | --class REGEX) [--len N] [--seconds S] [--hz H]
                                   sample memory while you act in the window; report each field that changed
  apptool trace [--seconds S] [--calls PATTERN ...]
                                   while you act in the window: system calls, files read and written, sockets,
                                   and with --calls the exported library functions called ("module!function"
                                   globs, e.g. "*!*Command*"), in order of first call
"""
import argparse
import json
import mmap
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

CONFIG = os.environ.get('APPTOOL_CONFIG', '/opt/cua-world-runtime/applications.json')
CHUNK = 32 << 20
FORMATS = {'ptr': '<Q', 'u64': '<Q', 'i64': '<q', 'u32': '<I', 'i32': '<i', 'f64': '<d', 'f32': '<f'}
# Qt 5 QMetaType ids that appear in method and property signatures.
QT_TYPES = dict(enumerate('? bool int uint qlonglong qulonglong double QChar QVariantMap QVariantList QString QStringList '
    'QByteArray QBitArray QDate QTime QDateTime QUrl QLocale QRect QRectF QSize QSizeF QLine QLineF QPoint QPointF QRegExp '
    'QVariantHash QEasingCurve QUuid void* long short char ulong ushort uchar float QObject* schar QVariant QModelIndex void'.split()))


def number(text):
    text = str(text)
    return int(text, 16) if text.lower().startswith(('0x', '-0x')) else (float(text) if re.search(r'[.eE]', text) else int(text))


def app_pids(config=None):
    """Processes running one of the declared application binaries, lowest first."""
    binaries = {os.path.realpath(app['binary']) for app in (config or json.load(open(CONFIG)))['applications']}
    found = []
    for name in os.listdir('/proc'):
        if not name.isdigit(): continue
        try:
            if os.path.realpath('/proc/' + name + '/exe') in binaries: found.append(int(name))
        except OSError: pass
    return sorted(found)


def the_pid(args):
    if args.pid: return args.pid
    pids = app_pids()
    if not pids: sys.exit('apptool: the application is not running (cua-app start)')
    return pids[0]


def parse_maps(text):
    rows = []
    for line in text.splitlines():
        fields = line.split(None, 5)
        start, end = (int(x, 16) for x in fields[0].split('-'))
        rows.append({'start': start, 'end': end, 'perms': fields[1], 'offset': int(fields[2], 16),
                     'path': fields[5] if len(fields) > 5 else ''})
    return rows


def read_maps(pid):
    with open('/proc/%d/maps' % pid) as stream: return parse_maps(stream.read())


def modules(maps):
    """File-backed mappings grouped by path: the address the file's offset 0 is loaded at, and its extent."""
    found = {}
    for row in maps:
        if not row['path'].startswith('/'): continue
        path = row['path'].removesuffix(' (deleted)')
        module = found.setdefault(path, {'path': path, 'base': row['start'] - row['offset'], 'end': row['end']})
        module['base'] = min(module['base'], row['start'] - row['offset']); module['end'] = max(module['end'], row['end'])
    return sorted(found.values(), key=lambda m: m['base'])


class Memory:
    def __init__(self, pid): self.fd = os.open('/proc/%d/mem' % pid, os.O_RDONLY)

    def read(self, address, size):
        try: return os.pread(self.fd, size, address)
        except (OSError, OverflowError): return b''

    def word(self, address):
        data = self.read(address, 8)
        return struct.unpack('<Q', data)[0] if len(data) == 8 else None

    def regions(self, maps, everything=False):
        for row in maps:
            if row['path'] in ('[vvar]', '[vdso]', '[vsyscall]') or not row['perms'].startswith('rw' if not everything else 'r'): continue
            yield row

    def chunks(self, row, overlap=0):
        """(address, bytes, count): a region's readable bytes, of which the first count belong to this chunk
        and the rest overlap the next one. Unreadable stretches are skipped page by page."""
        address = row['start']
        while address < row['end']:
            size = min(CHUNK, row['end'] - address)
            data = self.read(address, min(size + overlap, row['end'] - address))
            if not data:
                address += 4096; continue
            yield address, data, min(size, len(data))
            address += min(size, len(data))


def elf_symbols(path):
    """Defined symbols of a 64-bit little-endian ELF file: (name, value, size). Also its lowest load address."""
    with open(path, 'rb') as stream:
        try: data = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError: return [], 0
    if data[:6] != b'\x7fELF\x02\x01': return [], 0
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data, 0)
    phoff, shoff, phentsize, phnum, shentsize, shnum = header[5], header[6], header[9], header[10], header[11], header[12]
    loads = [struct.unpack_from('<IIQQ', data, phoff + i * phentsize) for i in range(phnum)]
    lowest = min((p[3] for p in loads if p[0] == 1), default=0) & ~0xfff
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shentsize) for i in range(shnum)] if shoff else []
    symbols = {}
    for section in sections:
        if section[1] not in (2, 11) or not section[9]: continue  # SHT_SYMTAB, SHT_DYNSYM
        strings = sections[section[6]]
        for name, info, other, index, value, size in struct.iter_unpack('<IBBHQQ', data[section[4]:section[4] + section[5] // 24 * 24]):
            if not index or not name or not value or info & 15 == 6: continue  # undefined, or thread-local
            end = data.find(b'\0', strings[4] + name)
            symbols[data[strings[4] + name:end].decode(errors='replace')] = (value, size)
    return [(name, value, size) for name, (value, size) in symbols.items()], lowest


def simple_name(mangled):
    """Itanium names without templates or substitutions: N3App8DocumentE -> App::Document."""
    text, parts = mangled, []
    nested = text.startswith('N')
    if nested: text = text[1:]
    if text.startswith('St'): parts.append('std'); text = text[2:]
    while text and text[0].isdigit():
        digits = re.match(r'\d+', text)[0]
        length = int(digits)
        parts.append(text[len(digits):len(digits) + length]); text = text[len(digits) + length:]
    if text != ('E' if nested else '') or not parts: return None
    return '::'.join(parts)


def demangle(names):
    """Readable names for mangled symbols: c++filt when present, otherwise plain nested names only."""
    names = list(names)
    tool = shutil.which('c++filt')
    if tool and names:
        out = subprocess.run([tool], input='\n'.join(names), capture_output=True, text=True).stdout.splitlines()
        if len(out) == len(names): return dict(zip(names, out))
    result = {}
    for name in names:
        kind = {'_ZTV': 'vtable for ', '_ZTI': 'typeinfo for ', '_ZTS': 'typeinfo name for '}.get(name[:4])
        plain = simple_name(name[4:]) if kind else None
        result[name] = kind + plain if plain else name
    return result


class Symbols:
    """Symbols of the mapped modules, loaded per module on first use."""
    def __init__(self, maps):
        self.modules = modules(maps)
        self.cache = {}

    def of(self, module):
        if module['path'] not in self.cache:
            try: symbols, lowest = elf_symbols(module['path'])
            except OSError: symbols, lowest = [], 0
            bias = module['base'] - lowest
            self.cache[module['path']] = sorted((value + bias, size, name) for name, value, size in symbols)
        return self.cache[module['path']]

    def module_at(self, address):
        return next((m for m in self.modules if m['base'] <= address < m['end']), None)

    def at(self, address):
        """(module, symbol name, offset into it) for an address inside a mapped file."""
        module = self.module_at(address)
        if not module: return None, None, None
        table = self.of(module)
        low, high = 0, len(table)
        while low < high:
            middle = (low + high) // 2
            if table[middle][0] <= address: low = middle + 1
            else: high = middle
        if low and address < table[low - 1][0] + max(table[low - 1][1], 1):
            return module, table[low - 1][2], address - table[low - 1][0]
        return module, None, None

    def vtables(self, pattern=None, module_pattern=None):
        """(class, module path, vtable address, the word a live object starts with) for every vtable symbol."""
        rows = []
        for module in self.modules:
            if module_pattern and not module_pattern.search(module['path']): continue
            rows += [(name, module['path'], address) for address, size, name in self.of(module) if name.startswith('_ZTV')]
        names = demangle(sorted({name for name, _, _ in rows}))
        result = []
        for name, path, address in rows:
            pretty = names[name].removeprefix('vtable for ')
            if pattern and not pattern.search(pretty) and not pattern.search(name): continue
            result.append((pretty, path, address, address + 16))
        return sorted(result)


def cmd_pid(args):
    print(json.dumps({'pids': app_pids()}))


def cmd_modules(args):
    pattern = re.compile(args.regex) if args.regex else None
    rows = [m for m in modules(read_maps(the_pid(args))) if not pattern or pattern.search(m['path'])]
    for m in rows: print('0x%012x %9d KiB  %s' % (m['base'], (m['end'] - m['base']) >> 10, m['path']))
    print('# %d modules' % len(rows))


def per_module(rows, what, hint):
    counts = {}
    for row in rows: counts[row[1]] = counts.get(row[1], 0) + 1
    for path, count in sorted(counts.items(), key=lambda item: -item[1]): print('%7d  %s' % (count, path))
    print('# %d %s in %d modules. %s' % (len(rows), what, len(counts), hint))


def cmd_rtti(args):
    symbols = Symbols(read_maps(the_pid(args)))
    rows = symbols.vtables(args.regex and re.compile(args.regex), args.module and re.compile(args.module))
    if not args.regex and not args.module:
        return per_module(rows, 'classes with vtables', 'List them with apptool rtti REGEX or --module REGEX.')
    for name, path, vtable, point in rows:
        print('%-56s vtable=0x%x object=0x%x  %s' % (name, vtable, point, os.path.basename(path)))
    print('# %d classes; a live object starts with its "object" address: apptool find --class REGEX' % len(rows))


def qt_class(memory, address, cache):
    """Decode a Qt 5 QMetaObject (revisions 7 and 8) from the live process."""
    if address in cache: return cache[address]
    cache[address] = None
    pointers = memory.read(address, 48)
    if len(pointers) < 48: return None
    parent, strings, data = struct.unpack_from('<QQQ', pointers)
    header = memory.read(data, 56)
    if len(header) < 56: return None
    revision, name, _, _, method_count, method_data, property_count, property_data, _, _, _, _, _, signal_count = struct.unpack('<14I', header)
    if revision not in (7, 8) or method_count > 4096 or property_count > 4096:
        cache[address] = {'name': None, 'revision': revision}
        return cache[address]
    def text(index):
        entry = memory.read(strings + 24 * index, 24)
        if len(entry) < 24: return '?'
        _, size, _, offset = struct.unpack('<iiI4xq', entry)
        return memory.read(strings + 24 * index + offset, min(max(size, 0), 512)).decode(errors='replace')
    def type_name(value):
        return text(value & 0x7fffffff) if value & 0x80000000 else QT_TYPES.get(value, 'type#%d' % value)
    words = lambda offset, count: struct.unpack('<%dI' % count, memory.read(data + 4 * offset, 4 * count).ljust(4 * count, b'\0'))
    methods = []
    for index in range(method_count):
        method, argc, parameters, _, flags = words(method_data + 5 * index, 5)
        types = words(parameters, 1 + min(argc, 32))
        kind = {0x04: 'signal', 0x08: 'slot', 0x0c: 'constructor'}.get(flags & 0x0c, 'method')
        methods.append({'kind': kind, 'access': ('private', 'protected', 'public')[flags & 3] if flags & 3 < 3 else '?',
                        'signature': '%s(%s)' % (text(method), ','.join(map(type_name, types[1:]))), 'returns': type_name(types[0])})
    properties = []
    for index in range(property_count):
        prop, kind, flags = words(property_data + 3 * index, 3)
        properties.append({'name': text(prop), 'type': type_name(kind), 'access': ('r' if flags & 1 else '') + ('w' if flags & 2 else '')})
    result = {'name': text(name), 'revision': revision, 'methods': methods, 'properties': properties, 'parent': None}
    cache[address] = result
    base = qt_class(memory, parent, cache) if parent else None
    result['parent'] = base['name'] if base else None
    return result


def cmd_qt(args):
    pid = the_pid(args)
    symbols, memory = Symbols(read_maps(pid)), Memory(pid)
    pattern, module_pattern = args.regex and re.compile(args.regex), args.module and re.compile(args.module)
    rows, cache, undecoded = [], {}, 0
    for module in symbols.modules:
        if module_pattern and not module_pattern.search(module['path']): continue
        for address, size, name in symbols.of(module):
            if not name.endswith('16staticMetaObjectE'): continue
            row = qt_class(memory, address, cache)
            if not row or not row['name']: undecoded += 1
            elif not pattern or pattern.search(row['name']): rows.append((row['name'], module['path'], address, row))
    note = ' %d meta-objects were not decoded (only Qt 5 layouts are).' % undecoded if undecoded else ''
    if not args.regex and not args.module:
        return per_module(rows, 'Qt classes', 'List them with apptool qt REGEX or --module REGEX.' + note)
    for name, path, address, row in sorted(rows, key=lambda r: r[0]):
        print('class %s : %s   metaobject=0x%x  %s' % (name, row['parent'] or '-', address, os.path.basename(path)))
        for method in row['methods']:
            print('  %-11s %-9s %s -> %s' % (method['kind'], method['access'], method['signature'], method['returns']))
        for prop in row['properties']: print('  property    %-9s %s %s' % (prop['access'], prop['type'], prop['name']))
    print('# %d Qt classes.%s' % (len(rows), note))


def patterns_for(value, kind):
    if kind == 'str': return [value.encode(), value.encode('utf-16-le')], 1
    return [struct.pack(FORMATS[kind], number(value) if kind.startswith('f') else int(number(value)))], struct.calcsize(FORMATS[kind])


def search(memory, maps, patterns, align, everything=False):
    """Yield (address, pattern index, mapping) for each aligned occurrence of any pattern."""
    longest = max(map(len, patterns))
    for row in memory.regions(maps, everything):
        for address, data, count in memory.chunks(row, longest - 1):
            for index, pattern in enumerate(patterns):
                at = data.find(pattern)
                while 0 <= at < count:
                    if (address + at) % align == 0: yield address + at, index, row
                    at = data.find(pattern, at + 1)


def search_words(memory, maps, words, everything=False):
    """The same for many 8-byte values at once, which numpy finds in one pass."""
    try: import numpy
    except ImportError: return search(memory, maps, [struct.pack('<Q', word) for word in words], 8, everything)
    wanted = numpy.array(words, dtype='<u8')
    index = {word: position for position, word in enumerate(words)}
    def found():
        for row in memory.regions(maps, everything):
            for address, data, count in memory.chunks(row):
                values = numpy.frombuffer(data[:count // 8 * 8], dtype='<u8')
                for position in numpy.nonzero(numpy.isin(values, wanted))[0]:
                    yield address + int(position) * 8, index[int(values[position])], row
    return found()


def near(memory, maps, value, kind, tolerance, everything):
    import numpy
    dtype = {'f64': '<f8', 'f32': '<f4'}[kind]
    for row in memory.regions(maps, everything):
        for address, data, count in memory.chunks(row):
            values = numpy.frombuffer(data[:count // 8 * 8], dtype=dtype)
            with numpy.errstate(invalid='ignore'):
                for index in numpy.nonzero(numpy.abs(values - value) <= tolerance)[0]:
                    yield address + int(index) * values.itemsize, 0, row


def class_points(symbols, regex):
    rows = symbols.vtables(re.compile(regex))
    if not rows: sys.exit('apptool: no class with a vtable matches; see apptool rtti')
    return {point: name for name, _, _, point in rows}


def cmd_find(args):
    pid = the_pid(args)
    maps, memory = read_maps(pid), Memory(pid)
    if args.cls:
        points = class_points(Symbols(maps), args.cls)
        addresses = sorted(points)
        hits = search_words(memory, maps, addresses, args.all)
        label = lambda index: points[addresses[index]]
    elif args.value is None: sys.exit('apptool find: give a VALUE or --class REGEX')
    elif args.tol and args.type in ('f64', 'f32'):
        hits, label = near(memory, maps, float(number(args.value)), args.type, args.tol, args.all), lambda index: ''
    else:
        patterns, align = patterns_for(args.value, args.type)
        hits = search(memory, maps, patterns, align, args.all)
        label = lambda index: ('utf-8', 'utf-16')[index] if args.type == 'str' else ''
    total, counts = 0, {}
    for address, index, row in hits:
        total += 1; counts[label(index)] = counts.get(label(index), 0) + 1
        if total <= args.max: print('0x%x  %s  %s' % (address, label(index), row['path'] or 'anonymous'))
    if args.cls:
        for name, count in sorted(counts.items()): print('# %6d  %s' % (count, name))
    print('# %d matches%s' % (total, '' if total <= args.max else ', first %d shown (--max N for more)' % args.max))


def describe(memory, symbols, maps, value, pending):
    """What a word points to: a symbol in a module, text, or a kind of memory."""
    module, name, offset = symbols.at(value)
    if name:
        pending.add(name)
        return ('-> ', name, '+0x%x' % offset if offset else '', '  ' + os.path.basename(module['path']))
    if module: return ('-> %s+0x%x' % (os.path.basename(module['path']), value - module['base']), None, '', '')
    row = next((r for r in maps if r['start'] <= value < r['end']), None)
    if not row: return ('', None, '', '')
    text = memory.read(value, 24).split(b'\0')[0]
    shown = ' "%s"' % text.decode() if len(text) >= 4 and text.isascii() and text.decode().isprintable() else ''
    return ('-> %s%s' % (row['path'] or 'anonymous ' + row['perms'], shown), None, '', '')


def cmd_dump(args):
    pid = the_pid(args)
    maps, memory = read_maps(pid), Memory(pid)
    symbols = Symbols(maps)
    address = int(number(args.address))
    data = memory.read(address, args.length)
    if not data: sys.exit('apptool: cannot read 0x%x' % address)
    rows, pending = [], set()
    for offset in range(0, len(data) // 8 * 8, 8):
        raw = data[offset:offset + 8]
        u64, = struct.unpack('<Q', raw); i64, = struct.unpack('<q', raw); f64, = struct.unpack('<d', raw)
        rows.append((offset, u64, i64, f64, struct.unpack('<ii', raw), struct.unpack('<ff', raw), describe(memory, symbols, maps, u64, pending)))
    names = demangle(sorted(pending))
    print('offset   hex                 i64                   f64            i32,i32                  f32,f32                    points to')
    for offset, u64, i64, f64, ints, floats, (lead, name, plus, tail) in rows:
        print('+0x%04x  0x%016x  %20d  %-13.6g  %-23s  %-25s  %s' % (offset, u64, i64, f64, '%d,%d' % ints, '%.6g,%.6g' % floats,
              lead + (names[name] + plus + tail if name else '')))


def changed_fields(samples):
    """Offsets (4-byte cells) whose bytes differ between samples of one object: (offset, values in order of appearance)."""
    fields = []
    for offset in range(0, min(map(len, samples)) // 4 * 4, 4):
        values = []
        for sample in samples:
            cell = sample[offset:offset + 4]
            if not values or values[-1] != cell: values.append(cell)
        if len(values) > 1: fields.append((offset, values))
    return fields


def cmd_watch(args):
    pid = the_pid(args)
    maps, memory = read_maps(pid), Memory(pid)
    if args.cls:
        points = class_points(Symbols(maps), args.cls)
        addresses = sorted(points)
        objects = [(address, points[addresses[index]]) for address, index, _ in search_words(memory, maps, addresses)]
    else: objects = [(int(number(a)), '') for a in args.address]
    if not objects: sys.exit('apptool: no objects to watch')
    shown = objects[:args.max_objects]
    samples, began = [], time.monotonic()
    while time.monotonic() - began < args.seconds:
        samples.append([memory.read(address, args.length) for address, _ in shown])
        time.sleep(1.0 / args.hz)
    print('# %d of %d objects, %d samples over %.1f s' % (len(shown), len(objects), len(samples), time.monotonic() - began))
    changed = 0
    for index, (address, name) in enumerate(shown):
        fields = changed_fields([sample[index] for sample in samples if sample[index]])
        if not fields: continue
        changed += 1
        print('object 0x%x %s' % (address, name))
        for offset, values in fields:
            first, last = values[0], values[-1]
            view = lambda cell: '0x%08x i32=%d f32=%.6g' % (struct.unpack('<I', cell)[0], struct.unpack('<i', cell)[0], struct.unpack('<f', cell)[0])
            print('  +0x%04x  %d values  %s  ->  %s' % (offset, len(values), view(first), view(last)))
    if not changed: print('# nothing changed: act in the window while watch is sampling (raise --seconds), or watch other objects')
    else: print('# %d objects changed; fields are 4-byte cells: an 8-byte field shows as two neighbours' % changed)


CALL = re.compile(r'^(?:\[pid +)?(\d+)\]? +(?:[\d:.]+ +)?(\w+)\((.*)$')
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
DESCRIPTOR = re.compile(r'^(\d+)<(.*?)>(?=[,)])')  # the label may itself hold '>': 5<UNIX-STREAM:[77->78]>


def summarize_trace(lines):
    """Counts and paths from strace -f -y output: what the application asked the system for."""
    calls, read, written, sockets, executed = {}, {}, {}, {}, []
    for line in lines:
        match = CALL.match(line)
        if not match: continue
        name, rest = match[2], match[3]
        calls[name] = calls.get(name, 0) + 1
        paths = QUOTED.findall(rest)
        descriptor = DESCRIPTOR.match(rest)
        if name in ('open', 'openat', 'creat') and paths:
            target = written if re.search(r'O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND', rest) or name == 'creat' else read
            target[paths[0]] = target.get(paths[0], 0) + 1
        elif name in ('rename', 'renameat', 'renameat2', 'unlink', 'unlinkat', 'mkdir', 'mkdirat', 'truncate', 'symlink', 'link'):
            for path in paths: written[path] = written.get(path, 0) + 1
        elif name in ('execve', 'execveat') and paths: executed.append(paths[0])
        elif descriptor and name in ('write', 'pwrite64', 'writev', 'sendto', 'sendmsg', 'recvfrom', 'recvmsg', 'connect', 'read', 'pread64'):
            label = descriptor[2]
            if label.startswith('/') and name in ('write', 'pwrite64', 'writev'): written[label] = written.get(label, 0) + 1
            elif not label.startswith('/'): sockets[label] = sockets.get(label, 0) + 1
    return {'calls': calls, 'read': read, 'written': written, 'sockets': sockets, 'executed': executed}


FRIDA_SCRIPT = r'''
const seen = {}; const order = []; let hooked = 0;
const resolver = new ApiResolver('module');
for (const pattern of PATTERNS) {
  for (const match of resolver.enumerateMatches('exports:' + pattern)) {
    if (hooked >= LIMIT) break;
    try {
      Interceptor.attach(match.address, { onEnter() {
        if (!(match.name in seen)) { seen[match.name] = 0; order.push(match.name); }
        seen[match.name] += 1; } });
      hooked += 1;
    } catch (error) {}
  }
}
rpc.exports = { report() { return { hooked: hooked, calls: order.map(name => [name, seen[name]]) }; } };
'''


def library_calls(pid, patterns, seconds, limit):
    import frida
    session = frida.attach(pid)
    try:
        patterns = [p if '!' in p else '*!' + p for p in patterns]
        script = session.create_script(FRIDA_SCRIPT.replace('PATTERNS', json.dumps(patterns)).replace('LIMIT', str(limit)))
        script.load()
        time.sleep(seconds)
        return script.exports_sync.report()
    finally: session.detach()


def cmd_trace(args):
    pid = the_pid(args)
    if args.calls:
        report = library_calls(pid, args.calls, args.seconds, args.limit)
        names = demangle(sorted({name.split('!')[-1] for name, _ in report['calls']}))
        for name, count in report['calls']:
            module, _, function = name.rpartition('!')
            print('%7d  %s  %s' % (count, names.get(function, function), module))
        print('# hooked %d functions (at most --limit %d); %d were called, listed in order of first call'
              % (report['hooked'], args.limit, len(report['calls'])))
        return
    log = args.output or tempfile.mkstemp(prefix='apptool-trace-', suffix='.log')[1]
    subprocess.run(['timeout', '--signal=INT', str(args.seconds), 'strace', '-f', '-qq', '-y', '-s', '256', '-o', log, '-p', str(pid)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with open(log, errors='replace') as stream: summary = summarize_trace(stream)
    if not summary['calls']: sys.exit('apptool: strace recorded nothing; is another tracer attached?')
    print('# system calls: ' + ', '.join('%s %d' % item for item in sorted(summary['calls'].items(), key=lambda item: -item[1])))
    for title, rows in (('opened to read', summary['read']), ('written, created, renamed or removed', summary['written']),
                        ('sockets and pipes used', summary['sockets'])):
        print('# %s: %d' % (title, len(rows)))
        for path, count in sorted(rows.items()): print('%7d  %s' % (count, path))
    for path in summary['executed']: print('# executed: ' + path)
    print('# every call with its arguments: ' + log)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--pid', type=int)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('pid').set_defaults(run=cmd_pid)
    for name, run in (('modules', cmd_modules), ('rtti', cmd_rtti), ('qt', cmd_qt)):
        p = sub.add_parser(name); p.add_argument('regex', nargs='?'); p.set_defaults(run=run)
        if name != 'modules': p.add_argument('--module', help='only modules whose path matches')
    p = sub.add_parser('find'); p.set_defaults(run=cmd_find)
    p.add_argument('value', nargs='?'); p.add_argument('--class', dest='cls')
    p.add_argument('--type', choices=[*FORMATS, 'str'], default='ptr'); p.add_argument('--tol', type=float, default=0.0)
    p.add_argument('--max', type=int, default=200); p.add_argument('--all', action='store_true')
    p = sub.add_parser('dump'); p.set_defaults(run=cmd_dump)
    p.add_argument('address'); p.add_argument('length', nargs='?', type=lambda s: int(number(s)), default=256)
    p = sub.add_parser('watch'); p.set_defaults(run=cmd_watch)
    p.add_argument('address', nargs='*'); p.add_argument('--class', dest='cls')
    p.add_argument('--len', dest='length', type=lambda s: int(number(s)), default=256)
    p.add_argument('--seconds', type=float, default=5.0); p.add_argument('--hz', type=float, default=10.0)
    p.add_argument('--max-objects', type=int, default=50)
    p = sub.add_parser('trace'); p.set_defaults(run=cmd_trace)
    p.add_argument('--seconds', type=float, default=10.0); p.add_argument('--calls', nargs='+')
    p.add_argument('--limit', type=int, default=2000); p.add_argument('--output')
    args = parser.parse_args(argv)
    args.run(args)


if __name__ == '__main__': main()

"""Read-only memory access to the running Fruit Ninja process via /run/fn-mem.sock."""
import socket, struct, json, threading

SOCK = '/run/fn-mem.sock'

class FnMem:
    def __init__(self):
        self.lock = threading.Lock()
        self._connect()

    def _connect(self):
        try:
            self.s.close()
        except Exception:
            pass
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(SOCK)
        self.f = self.s.makefile('rw')

    def _line(self, req):
        with self.lock:
            self.f.write(req + '\n')
            self.f.flush()
            return self.f.readline()

    def pid(self):
        return json.loads(self._line('pid'))['pid']

    def maps(self):
        return json.loads(self._line('maps'))

    def read(self, addr, n):
        """Read n bytes at addr. Returns bytes."""
        for attempt in range(3):
            try:
                return self._read_once(addr, n)
            except Exception:
                self._connect()
        return self._read_once(addr, n)

    def _read_once(self, addr, n):
        with self.lock:
            self.f.write('read 0x%x %d\n' % (addr, n))
            self.f.flush()
            hdr = b''
            while len(hdr) < 4:
                c = self.s.recv(4 - len(hdr))
                if not c:
                    raise IOError('sock closed')
                hdr += c
            cnt = struct.unpack('<I', hdr)[0]
            out = b''
            while len(out) < cnt:
                c = self.s.recv(cnt - len(out))
                if not c:
                    raise IOError('sock closed')
                out += c
            return out

    def u32(self, addr):
        return struct.unpack('<I', self.read(addr, 4))[0]

    def i32(self, addr):
        return struct.unpack('<i', self.read(addr, 4))[0]

    def f32(self, addr):
        return struct.unpack('<f', self.read(addr, 4))[0]

    def read_batch(self, reqs):
        """reqs: list of (addr, n). Returns list of bytes. One round trip."""
        for attempt in range(3):
            try:
                return self._batch_once(reqs)
            except Exception:
                self._connect()
        return self._batch_once(reqs)

    def _batch_once(self, reqs):
        with self.lock:
            for addr, n in reqs:
                self.f.write('read 0x%x %d\n' % (addr, n))
            self.f.flush()
            out = []
            for addr, n in reqs:
                hdr = b''
                while len(hdr) < 4:
                    c = self.s.recv(4 - len(hdr))
                    if not c:
                        raise IOError('sock closed')
                    hdr += c
                cnt = struct.unpack('<I', hdr)[0]
                d = b''
                while len(d) < cnt:
                    c = self.s.recv(cnt - len(d))
                    if not c:
                        raise IOError('sock closed')
                    d += c
                out.append(d)
            return out

_mem = None
def get():
    global _mem
    if _mem is None:
        _mem = FnMem()
    return _mem

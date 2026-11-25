"""Attach to the running FreeCAD process and call operations inside it.

Transport: Frida.  We attach to the FreeCAD process (same user; the application
explicitly allows this) and call FreeCAD's own embedded Python interpreter
through the CPython C API.  The injected code always runs on FreeCAD's Qt main
thread, so operations behave exactly like commands typed into FreeCAD's Python
console or run from a macro: they update documents, the GUI, the 3D view and the
application's settings.
"""
import atexit
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading

JS = r"""
var pyMod = null;
Process.enumerateModules().forEach(function (m) {
    if (!pyMod && /libpython3\.\d+\.so/.test(m.name)) pyMod = m;
});
var fEnsure = null, fRelease = null, fRun = null;
if (pyMod !== null) {
    fEnsure = new NativeFunction(pyMod.getExportByName("PyGILState_Ensure"), 'int', []);
    fRelease = new NativeFunction(pyMod.getExportByName("PyGILState_Release"), 'void', ['int']);
    fRun = new NativeFunction(pyMod.getExportByName("PyRun_SimpleString"), 'int', ['pointer']);
}
var installedHash = null;
var HELPER_SRC = __HELPER_SRC__;
var HELPER_HASH = __HELPER_HASH__;

function pyRun(code) {
    var st = fEnsure();
    var p = Memory.allocUtf8String(code);
    var rc = fRun(p);
    fRelease(st);
    return rc;
}

rpc.exports = {
    call: function (reqPath, outPath) {
        if (pyMod === null) {
            throw new Error("no libpython3 shared library found in the FreeCAD process");
        }
        var code = "";
        if (HELPER_HASH !== installedHash) {
            code += "if globals().get('__fc_mcp_src_hash__') != " + JSON.stringify(HELPER_HASH) + ":\n";
            code += "    exec(compile(" + JSON.stringify(HELPER_SRC) + ", 'fc_app.py', 'exec'), globals())\n";
            code += "    globals()['__fc_mcp_src_hash__'] = " + JSON.stringify(HELPER_HASH) + "\n";
            installedHash = HELPER_HASH;
        }
        code += "__fc_mcp_call(" + JSON.stringify(reqPath) + ", " + JSON.stringify(outPath) + ")\n";
        return pyRun(code);
    },
    ping: function () {
        return pyMod === null ? "no-python" : "ok";
    }
};
"""


def _hash(src):
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def find_freecad_pids():
    """Return pids of running FreeCAD GUI processes (this user only)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            exe = os.readlink("/proc/%d/exe" % pid)
        except OSError:
            continue
        base = os.path.basename(exe)
        if not base.startswith("freecad") or base.startswith("freecadcmd"):
            continue
        found.append((pid, exe))

    def has_gui(item):
        pid, _ = item
        try:
            with open("/proc/%d/maps" % pid) as fh:
                return "libFreeCADGui.so" in fh.read()
        except OSError:
            return False

    gui = [i for i in found if has_gui(i)]
    return gui or found


def cua_app(*args, **kw):
    try:
        p = subprocess.run(["cua-app"] + list(args), capture_output=True, text=True,
                           timeout=kw.get("timeout", 240))
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


class BridgeError(RuntimeError):
    pass


class FreeCADBridge(object):
    def __init__(self, helper_path, verbose=False):
        self.helper_path = helper_path
        self.verbose = verbose
        self._lock = threading.RLock()
        self._session = None
        self._script = None
        self._pid = None
        self._seq = itertools.count(1)
        self._tmpdir = self._make_tmpdir()
        self._last_error = None
        self._helper_src = open(helper_path).read()

    @staticmethod
    def _make_tmpdir():
        """One directory per server process; stale ones from dead processes are removed."""
        base = "/tmp/fc-mcp-%d" % os.getpid()
        for entry in os.listdir("/tmp"):
            if not entry.startswith("fc-mcp-"):
                continue
            path = os.path.join("/tmp", entry)
            if path == base:
                continue
            try:
                other = int(entry.split("-")[-1])
                os.kill(other, 0)
            except (ValueError, OSError):
                shutil.rmtree(path, ignore_errors=True)
        os.makedirs(base, exist_ok=True)
        atexit.register(shutil.rmtree, base, True)
        return base

    # ------------------------------------------------------------------
    def _log(self, *a):
        if self.verbose:
            print("[fc-bridge]", *a, file=sys.stderr)

    def status(self):
        pids = [p for p, _ in find_freecad_pids()]
        return {"attached": self._script is not None, "pid": self._pid,
                "freecad_pids": pids, "tmpdir": self._tmpdir}

    def detach(self):
        try:
            if self._session is not None:
                self._session.detach()
        except Exception:
            pass
        self._script = None
        self._session = None

    # ------------------------------------------------------------------
    def _ensure(self):
        if self._script is not None:
            return
        try:
            import frida
        except Exception as e:
            raise BridgeError("the bundled Frida library could not be imported: %s" % e)
        pids = find_freecad_pids()
        if not pids:
            raise BridgeError("FreeCAD is not running. Start it with freecad_app_lifecycle "
                              "(action=start), then call again.")
        last = None
        for pid, exe in pids:
            try:
                session = frida.attach(pid)
                js = JS.replace("__HELPER_SRC__", json.dumps(self._helper_src)) \
                       .replace("__HELPER_HASH__", json.dumps(_hash(self._helper_src)))
                script = session.create_script(js, name="freecad-mcp")
                script.on("message", self._on_message)
                script.on("destroyed", self._on_detached)
                session.on("detached", self._on_detached)
                script.load()
                self._session = session
                self._script = script
                self._pid = pid
                self._log("attached to pid %d (%s)" % (pid, exe))
                return
            except Exception as e:
                last = e
                self._log("attach to pid %d failed: %s" % (pid, e))
        raise BridgeError("could not attach to FreeCAD (pids %s): %s" % (pids, last))

    def _on_message(self, message, data):
        if message.get("type") == "error":
            self._last_error = message.get("description")
            self._log("script error: %s" % message.get("description"))

    def _on_detached(self, *args):
        self._log("session detached")
        self._script = None
        self._session = None

    # ------------------------------------------------------------------
    def call(self, op, args=None, timeout=300):
        """Run one operation inside FreeCAD and return its decoded result."""
        with self._lock:
            self._ensure()
            cid = next(self._seq)
            req_path = os.path.join(self._tmpdir, "req-%d.json" % cid)
            out_path = os.path.join(self._tmpdir, "out-%d.json" % cid)
            with open(req_path, "w") as fh:
                json.dump({"op": op, "args": args or {}}, fh)
            if os.path.exists(out_path):
                os.unlink(out_path)
            box = {}

            def worker():
                try:
                    box["rc"] = self._script.exports_sync.call(req_path, out_path)
                except Exception as e:
                    box["error"] = e

            t = threading.Thread(target=worker, name="fc-rpc")
            t.daemon = True
            t.start()
            t.join(timeout)
            if t.is_alive():
                self.detach()
                raise BridgeError(
                    "FreeCAD did not answer within %ss and the call was abandoned. "
                    "The application's main thread is busy - most likely a modal dialog is open "
                    "(close it in the FreeCAD window) - or the operation is very long." % timeout)
            if "error" in box:
                self.detach()
                raise BridgeError("lost the connection to FreeCAD (%s). It may have been restarted; "
                                  "the next call will re-attach automatically." % box["error"])
            rc = box.get("rc")
            if not os.path.exists(out_path):
                raise BridgeError("FreeCAD returned no result (interpreter status %r); the injected "
                                  "code did not complete." % rc)
            try:
                with open(out_path) as fh:
                    res = json.load(fh)
            except Exception as e:
                raise BridgeError("unreadable result from FreeCAD: %s" % e)
            finally:
                for p in (out_path, req_path):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            if not res.get("ok"):
                tb = res.get("traceback") or ""
                if self.verbose and tb:
                    sys.stderr.write("[fc-bridge] application traceback:\n%s\n" % tb)
                raise BridgeError(res.get("error") or "unknown error")
            return res.get("data") or {}




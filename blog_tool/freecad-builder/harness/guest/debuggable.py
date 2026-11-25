"""Start a program that its own user may debug, then run it unchanged.

Where the host's Yama policy lets a process attach only to its descendants, this process declares
that any process may attach to it. The kernel still requires the same user, so this opens exactly
this program to its own user's debuggers; every other process on the desktop stays as it was. The
declaration belongs to the process and survives exec.
"""
import ctypes
import errno
import os
import sys

PR_SET_PTRACER = 0x59616d61
PR_SET_PTRACER_ANY = ctypes.c_ulong(-1)

if __name__ == '__main__':
    libc = ctypes.CDLL(None, use_errno=True)
    # EINVAL: the kernel has no Yama policy, so there is nothing to declare.
    if libc.prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0) and ctypes.get_errno() != errno.EINVAL:
        raise OSError(ctypes.get_errno(), 'PR_SET_PTRACER')
    os.execv(sys.argv[1], sys.argv[1:])

"""Test helpers that differ between macOS or Linux and Windows."""
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

WINDOWS = os.name == 'nt'


def alive(pid):
    """Whether a process still runs. Never signals it: on Windows os.kill(pid, 0) would send Ctrl+C."""
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)      # query and wait rights
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill(pid):
    if WINDOWS:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def shell_tool(path, text):
    """A fake extraction tool written as a POSIX shell script. Windows skips the test; the Python fake tools cover it there."""
    if WINDOWS:
        raise unittest.SkipTest('POSIX shell fake tool')
    Path(path).write_text('#!/bin/sh\n' + text)
    os.chmod(path, 0o755)
    return str(path)


def python_tool(path, source):
    """A fake extraction tool that runs Python source on any system. Returns the command; Windows gets a .cmd wrapper."""
    path = Path(path)
    body = textwrap.dedent(source)
    if WINDOWS:
        script = path.with_suffix('.py')
        script.write_text(body, encoding='utf-8')
        wrapper = path.with_suffix('.cmd')
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding='utf-8')
        return str(wrapper)
    path.write_text(f'#!{sys.executable}\n' + body, encoding='utf-8')
    os.chmod(path, 0o755)
    return str(path)


# Source for a Python fake tool: the process that started the tool. On Windows the .cmd wrapper sits in between.
TOOL_PARENT = r'''
import os

def tool_parent():
    if os.name != 'nt':
        return os.getppid()
    import ctypes

    class Basic(ctypes.Structure):
        _fields_ = [(name, ctypes.c_void_p) for name in ('ExitStatus', 'PebBaseAddress', 'AffinityMask', 'BasePriority',
                                                         'UniqueProcessId', 'InheritedFromUniqueProcessId')]
    kernel32, ntdll = ctypes.WinDLL('kernel32'), ctypes.WinDLL('ntdll')
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, os.getppid())
    info = Basic()
    ntdll.NtQueryInformationProcess(ctypes.c_void_p(handle), 0, ctypes.byref(info), ctypes.sizeof(info), None)
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return info.InheritedFromUniqueProcessId
'''


def hanging_seven(path, pids, record_parent=False, fill_bytes=0):
    """A fake 7zz that lists one member, then starts a child that sleeps, writes the pids to the file pids and waits.

    record_parent also writes the pid of the process that started the tool. fill_bytes writes that many bytes into the
    output folder first, for the watchdog."""
    return python_tool(path, TOOL_PARENT + f'''
import subprocess, sys
if sys.argv[1] == 'l':
    sys.stdout.write('Path = a.txt\\nSize = 10\\nFolder = -\\nAttributes = A\\nEncrypted = -\\n\\n')
    sys.exit(0)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
started = [child.pid, os.getpid()] + ([tool_parent()] if {record_parent!r} else [])
with open({str(pids)!r}, 'a') as handle:
    handle.write(''.join(f'{{pid}}\\n' for pid in started))
out = next((a[2:] for a in sys.argv if a.startswith('-o')), None)
if {fill_bytes!r} and out:
    with open(os.path.join(out, 'a.txt'), 'wb') as handle:
        handle.write(b'\\0' * {fill_bytes!r})
child.wait()
''')


def symlinks_available(base):
    """Windows creates symbolic links only for administrators or in developer mode."""
    probe = Path(base) / 'symlink-probe'
    try:
        probe.symlink_to(base, target_is_directory=True)
    except OSError:
        return False
    probe.unlink()
    return True


def make_junction(link, target):
    """Windows: a directory junction, which every user may create."""
    import _winapi
    _winapi.CreateJunction(str(target), str(link))

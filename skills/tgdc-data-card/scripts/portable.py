"""What differs between macOS or Linux and Windows. Every other module asks here.

macOS and Linux: files are opened relative to directory descriptors with O_NOFOLLOW, so no link is ever followed.

Windows has no directory descriptors. A path is opened with FILE_FLAG_OPEN_REPARSE_POINT, so its last part is never
followed: a symbolic link or junction there is refused. The final path of the opened handle must then equal the requested
path, so no folder on the way was a link either. Selected folders are turned into that final form (\\\\?\\C:\\...) once,
which also lifts the 260-character path limit. An open folder handle pins its path: Windows refuses to rename or delete a
folder while a handle to it or below it is open. External tools run in a job object that kills them, and everything
they started, when the job is closed or its owner dies.
"""
import errno
import os
import shutil
import stat
import sys
import time
from pathlib import Path

WINDOWS = os.name == 'nt'

if WINDOWS:
    import _winapi
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _HANDLE = wintypes.HANDLE
    _INVALID = _HANDLE(-1).value
    GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
    FILE_LIST_DIRECTORY, FILE_TRAVERSE, FILE_READ_ATTRIBUTES = 0x1, 0x20, 0x80
    SHARE_READ, SHARE_WRITE, SHARE_DELETE = 0x1, 0x2, 0x4
    CREATE_NEW, OPEN_EXISTING, OPEN_ALWAYS = 1, 3, 4
    FLAG_BACKUP_SEMANTICS, FLAG_OPEN_REPARSE_POINT = 0x02000000, 0x00200000
    ATTRIBUTE_HIDDEN, ATTRIBUTE_SYSTEM, ATTRIBUTE_DIRECTORY, ATTRIBUTE_REPARSE_POINT = 0x2, 0x4, 0x10, 0x400
    NAME_SURROGATE = 0x20000000         # symbolic links, junctions and mount points carry this bit; cloud and dedup files do not
    SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
    KILL_ON_JOB_CLOSE = 0x2000

    class _AttributeTag(ctypes.Structure):
        _fields_ = [('FileAttributes', wintypes.DWORD), ('ReparseTag', wintypes.DWORD)]

    class _FileInformation(ctypes.Structure):
        _fields_ = [('dwFileAttributes', wintypes.DWORD), ('ftCreationTime', wintypes.FILETIME), ('ftLastAccessTime', wintypes.FILETIME),
                    ('ftLastWriteTime', wintypes.FILETIME), ('dwVolumeSerialNumber', wintypes.DWORD), ('nFileSizeHigh', wintypes.DWORD),
                    ('nFileSizeLow', wintypes.DWORD), ('nNumberOfLinks', wintypes.DWORD), ('nFileIndexHigh', wintypes.DWORD),
                    ('nFileIndexLow', wintypes.DWORD)]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD)] + [
            (name, ctypes.c_uint64) for name in ('ullTotalPhys', 'ullAvailPhys', 'ullTotalPageFile', 'ullAvailPageFile',
                                                 'ullTotalVirtual', 'ullAvailVirtual', 'ullAvailExtendedVirtual')]

    def _function(name, result, *arguments):
        function = getattr(_kernel32, name)
        function.restype, function.argtypes = result, list(arguments)
        return function

    _CreateFileW = _function('CreateFileW', _HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                             wintypes.DWORD, _HANDLE)
    _CloseHandle = _function('CloseHandle', wintypes.BOOL, _HANDLE)
    _GetFileInformationByHandleEx = _function('GetFileInformationByHandleEx', wintypes.BOOL, _HANDLE, ctypes.c_int, wintypes.LPVOID,
                                              wintypes.DWORD)
    _GetFileInformationByHandle = _function('GetFileInformationByHandle', wintypes.BOOL, _HANDLE, ctypes.POINTER(_FileInformation))
    _GetFinalPathNameByHandleW = _function('GetFinalPathNameByHandleW', wintypes.DWORD, _HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                           wintypes.DWORD)
    _CreateJobObjectW = _function('CreateJobObjectW', _HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
    _SetInformationJobObject = _function('SetInformationJobObject', wintypes.BOOL, _HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _AssignProcessToJobObject = _function('AssignProcessToJobObject', wintypes.BOOL, _HANDLE, _HANDLE)
    _TerminateJobObject = _function('TerminateJobObject', wintypes.BOOL, _HANDLE, wintypes.UINT)
    _OpenProcess = _function('OpenProcess', _HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _WaitForSingleObject = _function('WaitForSingleObject', wintypes.DWORD, _HANDLE, wintypes.DWORD)
    _GlobalMemoryStatusEx = _function('GlobalMemoryStatusEx', wintypes.BOOL, ctypes.POINTER(_MemoryStatus))
else:
    import fcntl
    import multiprocessing.reduction as reduction


# --------------------------------------------------------------------------- what a directory entry is
def identity(info):
    """What must stay the same between listing a file and opening it."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def entry_stat(entry):
    """The details of a directory entry, without following a link. A Windows folder listing holds no file id and may hold an
    outdated size or date (NTFS updates it lazily, for example for hard links), so Windows asks the file itself."""
    return os.lstat(entry.path) if WINDOWS else entry.stat(follow_symlinks=False)


def is_link_stat(info):
    """True for a symbolic link, and on Windows also for a junction or mount point, from an lstat result."""
    if stat.S_ISLNK(info.st_mode):
        return True
    return WINDOWS and bool(getattr(info, 'st_file_attributes', 0) & ATTRIBUTE_REPARSE_POINT) and bool(
        getattr(info, 'st_reparse_tag', 0) & NAME_SURROGATE)


def is_link_entry(entry):
    """True for a directory entry that is a link of any kind. Windows junctions look like folders to is_dir()."""
    if entry.is_symlink():
        return True
    return WINDOWS and is_link_stat(entry.stat(follow_symlinks=False))


def is_link_path(path):
    try:
        return is_link_stat(os.lstat(path))
    except OSError:
        return False


def hidden_entry(entry):
    """True for an entry Windows marks hidden, or a file it marks system, such as desktop.ini, Thumbs.db or an Office lock file.
    A folder marked system only (Windows marks folders with a custom icon so) is an ordinary folder and is entered."""
    if not WINDOWS:
        return False
    attributes = entry.stat(follow_symlinks=False).st_file_attributes
    return bool(attributes & ATTRIBUTE_HIDDEN) or bool(attributes & ATTRIBUTE_SYSTEM and not attributes & ATTRIBUTE_DIRECTORY)


def scandir_target(descriptor, path):
    """What os.scandir takes for an opened folder: its descriptor, or on Windows its path, pinned by the open handle."""
    return str(path) if WINDOWS else descriptor


# --------------------------------------------------------------------------- Windows paths and handles
def _long(path):
    """The \\\\?\\ form of an absolute Windows path, which has no length limit and is taken literally."""
    if path.startswith('\\\\?\\'):
        return path
    if path.startswith('\\\\'):
        return '\\\\?\\UNC\\' + path[2:]
    return '\\\\?\\' + path


def _short(path):
    path = str(path)
    if path.startswith('\\\\?\\UNC\\'):
        return '\\\\' + path[8:]
    if path.startswith('\\\\?\\'):
        return path[4:]
    return path


def plain(path):
    """A path for another program (an extraction tool, SQLite): without the \\\\?\\ prefix when the short form means the
    same file, otherwise unchanged. The same as str(path) outside Windows."""
    path = str(path)
    if not WINDOWS:
        return path
    short = _short(path)
    parts = short.replace('/', '\\').split('\\')
    if len(short) < 240 and not any(part != part.rstrip(' .') for part in parts[1:] if part):
        return short
    return path


def comparable(path):
    """A path for equality checks with paths from elsewhere: on Windows without the \\\\?\\ prefix that only selected paths carry."""
    return Path(_short(str(path))) if WINDOWS else Path(path)


def _same_path(first, second):
    return os.path.normcase(_short(first)).rstrip('\\') == os.path.normcase(_short(second)).rstrip('\\')


def _handle(path, access, share, disposition, flags):
    handle = _CreateFileW(str(path), access, share, None, disposition, flags, None)
    if handle == _INVALID or handle is None:
        error = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(error), str(path), error)
    return handle


def _attribute_tag(handle):
    info = _AttributeTag()
    if not _GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):      # FileAttributeTagInfo
        error = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(error), None, error)
    return info.FileAttributes, info.ReparseTag


def _file_id(handle):
    info = _FileInformation()
    if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
        return None
    return info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow


def _final_path(handle):
    """The path Windows resolved for an open handle, in \\\\?\\ form, or None where the file system cannot say."""
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = _GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if length == 0:
            return None
        if length < size:
            return buffer.value
        size = length + 1


def canonical(path):
    """Windows: the final \\\\?\\ path of a location whose existing parts are no links, plus the parts that do not exist yet.
    A symbolic link or junction among the existing parts raises ValueError."""
    absolute = Path(_long(os.path.abspath(os.path.expanduser(str(path)))))
    existing, remainder = Path(absolute.anchor), []
    parts = absolute.parts
    for index, part in enumerate(parts[1:], 1):
        candidate = existing / part
        try:
            info = os.lstat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            remainder = list(parts[index:])
            break
        except OSError:
            info = None
        if info is not None and is_link_stat(info):
            raise ValueError('Symlink roots, parents and outputs are not permitted.')
        existing = candidate
    try:
        handle = _handle(existing, FILE_READ_ATTRIBUTES, SHARE_READ | SHARE_WRITE | SHARE_DELETE, OPEN_EXISTING,
                         FLAG_BACKUP_SEMANTICS | FLAG_OPEN_REPARSE_POINT)
    except OSError:
        final = None
    else:
        try:
            final = _final_path(handle)
        finally:
            _CloseHandle(handle)
    return Path(final or str(existing)).joinpath(*remainder)


def _expected(path):
    """The final path an opened file must have: the path itself when it is already in final form."""
    path = str(path)
    if path.startswith('\\\\?\\'):
        return path
    location = Path(os.path.abspath(path))
    try:
        return str(canonical(location.parent) / location.name)
    except ValueError as error:
        raise OSError(errno.ELOOP, 'A folder on the way is a link', path) from error


def open_nofollow_windows(path, directory=False):
    """A CRT descriptor for a file or folder, opened without following a link at any part of the path."""
    path = str(path)
    share = SHARE_READ | SHARE_WRITE | (0 if directory else SHARE_DELETE)      # an open folder cannot be renamed under us
    flags = FLAG_OPEN_REPARSE_POINT | (FLAG_BACKUP_SEMANTICS if directory else 0)
    target = _long(os.path.abspath(path)) if not path.startswith('\\\\?\\') else path
    if directory:
        # A folder is listed by its path. Its handle asks for traverse (or list) rights rather than attributes alone, because
        # only a handle with such rights takes part in Windows sharing checks, which is what keeps the folder from being renamed.
        try:
            handle = _handle(target, FILE_READ_ATTRIBUTES | FILE_TRAVERSE, share, OPEN_EXISTING, flags)
        except PermissionError:
            handle = _handle(target, FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY, share, OPEN_EXISTING, flags)
    else:
        handle = _handle(target, GENERIC_READ, share, OPEN_EXISTING, flags)
    try:
        attributes, tag = _attribute_tag(handle)
        if attributes & ATTRIBUTE_REPARSE_POINT and tag & NAME_SURROGATE:
            raise OSError(errno.ELOOP, 'A link is not followed', path)
        if bool(attributes & ATTRIBUTE_DIRECTORY) != directory:
            raise (NotADirectoryError if directory else IsADirectoryError)(errno.ENOTDIR if directory else errno.EISDIR, 'Wrong kind', path)
        if attributes & ATTRIBUTE_REPARSE_POINT and not directory:
            # A cloud or deduplicated file: its data is read through a normal open, which must reach the same file.
            other = _handle(target, GENERIC_READ, share, OPEN_EXISTING, 0)
            if _file_id(other) != _file_id(handle) or _file_id(other) is None:
                _CloseHandle(other)
                raise OSError(errno.EIO, 'The file changed while it was opened', path)
            _CloseHandle(handle)
            handle = other
        final = _final_path(handle)
        if final is not None and not _same_path(final, _expected(path)):
            raise OSError(errno.ELOOP, 'A folder on the way is a link', path)
        return msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except BaseException:
        _CloseHandle(handle)
        raise


_OUTPUTS = {}       # descriptor -> path of the output folders opened here, for file systems that cannot tell the final path


def directory_path(descriptor):
    """The path of an opened folder (Windows)."""
    return _final_path(msvcrt.get_osfhandle(descriptor)) or _OUTPUTS[descriptor]


def output_directory_windows(path):
    """Create the folders of path that are missing and return a descriptor of the last one, which pins it (Windows)."""
    path = canonical(path)
    current = Path(path.anchor)
    held = open_nofollow_windows(current, True)
    try:
        for part in path.parts[1:]:
            current = current / part
            try:
                os.mkdir(current)                   # inside the folder held open, which cannot be renamed meanwhile
            except FileExistsError:
                pass
            following = open_nofollow_windows(current, True)
            os.close(held)
            held = following
        _OUTPUTS[held] = str(path)
        return held
    except BaseException:
        os.close(held)
        raise


# --------------------------------------------------------------------------- files inside an opened output folder
def open_at(directory, name, flags, mode=0o600):
    """Open name inside an opened folder without following a link. flags are os.O_* flags without O_NOFOLLOW."""
    if not WINDOWS:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=directory)
    path = os.path.join(directory_path(directory), name)
    access = {os.O_RDONLY: GENERIC_READ, os.O_WRONLY: GENERIC_WRITE}.get(flags & 3, GENERIC_READ | GENERIC_WRITE)
    if flags & os.O_CREAT:
        disposition = CREATE_NEW if flags & os.O_EXCL else OPEN_ALWAYS
    else:
        disposition = OPEN_EXISTING
    # A lock or report file that another program (a virus scanner) still holds, or that is still being deleted, is retried briefly.
    handle = _retry(lambda: _handle(path, access, SHARE_READ | SHARE_WRITE | SHARE_DELETE, disposition, FLAG_OPEN_REPARSE_POINT))
    try:
        attributes, tag = _attribute_tag(handle)
        if attributes & ATTRIBUTE_REPARSE_POINT and tag & NAME_SURROGATE:
            raise OSError(errno.ELOOP, 'A link is not followed', path)
        if attributes & ATTRIBUTE_DIRECTORY:
            raise IsADirectoryError(errno.EISDIR, 'Is a folder', path)
        return msvcrt.open_osfhandle(handle, 0)
    except BaseException:
        _CloseHandle(handle)
        raise


def _transient(error):
    """A Windows error that ends by itself: a file another program holds for a moment, or a folder whose deleted files are
    still being removed."""
    return WINDOWS and (isinstance(error, PermissionError) or error.errno == errno.ENOTEMPTY)


def _retry(action, attempts=20):
    """Run action; on Windows retry briefly while another program (a virus scanner, an indexer) still holds the file."""
    for attempt in range(attempts):
        try:
            return action()
        except OSError as error:
            if not _transient(error) or attempt == attempts - 1:
                raise
            time.sleep(min(0.5, 0.02 * 2 ** attempt))


def replace_at(directory, source, target):
    if not WINDOWS:
        os.replace(source, target, src_dir_fd=directory, dst_dir_fd=directory)
        return
    base = directory_path(directory)
    _retry(lambda: os.replace(os.path.join(base, source), os.path.join(base, target)))


def unlink_at(directory, name):
    if not WINDOWS:
        os.unlink(name, dir_fd=directory)
        return
    os.unlink(os.path.join(directory_path(directory), name))


def fchmod(descriptor, mode):
    """Set permission bits on POSIX. Windows has no such bits: the report folder's access list applies."""
    if not WINDOWS:
        os.fchmod(descriptor, mode)


def lock(descriptor):
    """Exclusive lock without waiting; OSError when another process holds it. The lock ends when the descriptor is closed."""
    if WINDOWS:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


# --------------------------------------------------------------------------- removing temporary data
def _writable_and_again(function, path, *_):
    try:
        os.chmod(path, stat.S_IWRITE)            # Windows refuses to delete a read-only file, which tools extract as such
    except OSError:
        pass
    try:
        _retry(lambda: function(path), attempts=8)
    except OSError:
        pass


def remove_tree(path):
    """Remove a folder and everything in it, never raising. On Windows read-only files and files that a virus scanner
    still holds for a moment are removed too. Returns True when the folder is gone."""
    if not WINDOWS:
        shutil.rmtree(path, ignore_errors=True)
        return not os.path.lexists(path)
    for attempt in range(6):
        if not os.path.lexists(path):
            return True
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_writable_and_again)
        else:
            shutil.rmtree(path, onerror=_writable_and_again)
        if os.path.lexists(path):
            time.sleep(0.1 * (attempt + 1))     # deleted files may still be pending while a scanner holds them
    return not os.path.lexists(path)


def remove_file(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        if WINDOWS:
            _writable_and_again(os.unlink, path)


# --------------------------------------------------------------------------- handing an open file to a worker process
def send_descriptor(connection, descriptor, pid):
    """Hand an open file to the worker process pid. On Windows the handle is copied into that process with the same access:
    multiprocessing.reduction asks for write access there, which a file opened for reading refuses."""
    if not WINDOWS:
        reduction.send_handle(connection, descriptor, pid)
        return
    process = _winapi.OpenProcess(_winapi.PROCESS_DUP_HANDLE, False, pid)
    try:
        handle = _winapi.DuplicateHandle(_winapi.GetCurrentProcess(), msvcrt.get_osfhandle(descriptor), process, 0, False,
                                         _winapi.DUPLICATE_SAME_ACCESS)
    finally:
        _winapi.CloseHandle(process)
    connection.send(handle)


def receive_descriptor(connection):
    if not WINDOWS:
        return reduction.recv_handle(connection)
    return msvcrt.open_osfhandle(connection.recv(), os.O_RDONLY)


# --------------------------------------------------------------------------- processes
def kill_on_close_job():
    """Windows: a job object that kills every process in it, and every process they start, when it is closed."""
    job = _CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
    if not _SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):     # JobObjectExtendedLimitInformation
        _CloseHandle(job)
        return None
    return job


def add_to_job(job, process_handle):
    return bool(job) and bool(_AssignProcessToJobObject(job, int(process_handle)))


def terminate_job(job):
    if job:
        _TerminateJobObject(job, 1)


def close_job(job):
    if job:
        _CloseHandle(job)


_WATCHED = {}


def process_alive(pid):
    """Windows: whether the process with this id still runs. The first look keeps a handle, so a reused id cannot fool later looks."""
    handle = _WATCHED.get(pid)
    if handle is None:
        handle = _OpenProcess(SYNCHRONIZE, False, pid) if pid else None
        if not handle:
            return False
        _WATCHED[pid] = handle
    return _WaitForSingleObject(handle, 0) == WAIT_TIMEOUT


def is_network_path(path):
    """Windows: True for a path on a network share (mapped drives resolve to one)."""
    return WINDOWS and _short(str(path)).startswith('\\\\')


def physical_memory():
    """Bytes of installed memory, or None when the system does not say."""
    if WINDOWS:
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        return status.ullTotalPhys if _GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        return os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError, AttributeError):
        return None


def tool_environment(folder):
    """The environment of an extraction tool: no inherited secrets, its own folder as home and temporary folder."""
    environment = {'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'C.UTF-8', 'LANG': 'C.UTF-8', 'HOME': folder}
    if WINDOWS:
        for name in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT'):
            if os.environ.get(name):
                environment[name] = os.environ[name]
        environment['TEMP'] = environment['TMP'] = folder
    return environment


def windows_seven_zip():
    """Windows: 7-Zip from its usual install folder, which its installer does not add to PATH."""
    if not WINDOWS:
        return None
    for variable in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)'):
        base = os.environ.get(variable)
        if base:
            candidate = os.path.join(base, '7-Zip', '7z.exe')
            if os.path.isfile(candidate):
                return candidate
    return None

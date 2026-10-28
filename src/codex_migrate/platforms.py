"""Conservative offline checks and filesystem primitives; never terminate an app.

Windows code uses only system APIs. Desktop validation remains an alpha release
requirement: synthetic tests cannot prove an installed application's behaviour.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

from .errors import MigrationError

_HELD_LOCKS = set()


def make_private_directory(path: Path) -> None:
    """Create a NEW recovery directory restricted to the current OS account."""
    if os.name != 'nt':
        Path(path).mkdir(mode=0o700)
        return
    # Python <3.13 ignores mkdir(mode=0o700) on Windows. Supply the protected
    # DACL at creation, avoiding a public-create then ACL-tightening window.
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    class Attributes(ctypes.Structure):
        _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p), ('bInheritHandle', wintypes.BOOL)]
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Attributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    sid_string = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is PSID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = 'D:P(A;OICI;FA;;;' + sid_string.value + ')(A;OICI;FA;;;SY)'
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid_string:
            kernel.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))
        kernel.CloseHandle(token)


def _darwin_filesystem(root: Path) -> tuple[str, int]:
    # Layout is Darwin's __DARWIN_STRUCT_STATFS64 from sys/mount.h. Explicit
    # statfs64 avoids legacy inode-size-dependent symbol aliases on Intel Macs.
    class StatFS(ctypes.Structure):
        _fields_ = [('bsize', ctypes.c_uint32), ('iosize', ctypes.c_int32),
                    ('blocks', ctypes.c_uint64), ('bfree', ctypes.c_uint64),
                    ('bavail', ctypes.c_uint64), ('files', ctypes.c_uint64),
                    ('ffree', ctypes.c_uint64), ('fsid', ctypes.c_int32 * 2),
                    ('owner', ctypes.c_uint32), ('type', ctypes.c_uint32),
                    ('flags', ctypes.c_uint32), ('subtype', ctypes.c_uint32),
                    ('fstypename', ctypes.c_char * 16), ('mntonname', ctypes.c_char * 1024),
                    ('mntfromname', ctypes.c_char * 1024), ('flags_ext', ctypes.c_uint32),
                    ('reserved', ctypes.c_uint32 * 7)]
    libc = ctypes.CDLL(None, use_errno=True)
    probe = libc.statfs64
    probe.argtypes = [ctypes.c_char_p, ctypes.POINTER(StatFS)]
    probe.restype = ctypes.c_int
    result = StatFS()
    if probe(os.fsencode(root), ctypes.byref(result)) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot inspect filesystem')
    return result.fstypename.decode('ascii'), result.flags


def flush_directory(path: Path) -> None:
    # Windows metadata operations below use MOVEFILE_WRITE_THROUGH. Windows does
    # not expose portable fsync(directory); FlushFileBuffers on a directory is
    # not a substitute and commonly returns ERROR_INVALID_HANDLE.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def move_file(source: Path, target: Path, *, replace: bool) -> None:
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        if not move(str(source), str(target), 8 | (1 if replace else 0)):
            raise ctypes.WinError(ctypes.get_last_error())
    elif replace:
        os.replace(source, target)
    else:
        # link() is an atomic no-clobber creation, unlike an exists()+replace().
        os.link(source, target, follow_symlinks=False)
        source.unlink()
    flush_directory(target.parent)


def remove_file(path: Path) -> None:
    """Durably remove the original name, including SQLite sidecar names."""
    if os.name == 'nt':
        # Windows has no directory fsync. A write-through rename first makes
        # the original database-sidecar name absent before journal completion.
        # A crash can leave this unrelated tombstone, which must not be treated
        # as an SQLite WAL/SHM or removed without checking tool ownership.
        tombstone = path.parent / ('.codex-migrate-delete-' + uuid.uuid4().hex)
        move_file(path, tombstone, replace=False)
        tombstone.unlink()
    else:
        path.unlink()
        flush_directory(path.parent)


@contextmanager
def journal_lock(path: Path):
    path = Path(path)
    try:
        for component in [*reversed(path.parents), path]:
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise MigrationError('Lock path cannot contain links or reparse points', code='unsafe_path')
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
        stream = os.fdopen(descriptor, 'r+b')
    except OSError as exc:
        raise MigrationError('Cannot open migration lock: ' + str(exc), code='busy') from exc
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise MigrationError('Migration lock must be a regular file', code='unsafe_path')
        if os.name == 'nt':
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MigrationError('Another operation owns this migration journal', code='busy') from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MigrationError('Another operation owns this migration journal', code='busy') from exc
        _HELD_LOCKS.add(path)
        acquired = True
        yield
    finally:
        # Closing the descriptor releases either platform's advisory lock.
        if acquired:
            _HELD_LOCKS.discard(path)
        stream.close()


@contextmanager
def migration_lock(home: Path):
    """Serialize this tool's operations for a home, independently of journals.

    Hold around preparation+apply or recovery. It does not stop Codex starting.
    Never unlink the sentinel: an unlinked lock permits parallel lock owners.
    """
    home = Path(home)
    if not home.is_absolute() or not home.is_dir():
        raise MigrationError('Migration lock requires an existing absolute home', code='unsafe_path')
    with journal_lock(home / '.codex-migrate.lock'):
        yield


def _processes():
    if os.name != 'nt':
        result = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True, text=True, check=True)
        rows = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                raise OSError('Unrecognized process listing')
            rows.append((int(parts[0]), parts[1]))
        return rows
    from ctypes import wintypes
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    rows = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(handle, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            rows.append((entry.th32ProcessID, entry.szExeFile))
            if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(ctypes.get_last_error())
                break
        return rows
    finally:
        kernel.CloseHandle(handle)


def _check_storage(root: Path) -> None:
    """Native imports are limited to known local filesystem implementations."""
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetVolumePathNameW.restype = wintypes.BOOL
        kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
        kernel.GetDriveTypeW.restype = wintypes.UINT
        kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                               ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
        kernel.GetVolumeInformationW.restype = wintypes.BOOL
        volume = ctypes.create_unicode_buffer(32768)
        if not kernel.GetVolumePathNameW(str(root), volume, len(volume)):
            raise ctypes.WinError(ctypes.get_last_error())
        filesystem = ctypes.create_unicode_buffer(256)
        if kernel.GetDriveTypeW(volume.value) != 3:  # DRIVE_FIXED; excludes remote/removable
            raise MigrationError('Use a fixed local NTFS disk for native migration', code='unsupported_storage')
        if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
            raise ctypes.WinError(ctypes.get_last_error())
        if filesystem.value.upper() != 'NTFS':
            raise MigrationError('This alpha only permits native writes on NTFS', code='unsupported_storage')
    elif sys.platform == 'darwin':
        filesystem, flags = _darwin_filesystem(root)
        if filesystem.lower() not in ('apfs', 'hfs') or not flags & 0x1000 or flags & 1:  # MNT_LOCAL and !MNT_RDONLY
            raise MigrationError('Use a local APFS/HFS disk for native migration', code='unsupported_storage')
    elif hasattr(os, 'statvfs'):
        # Linux is used for synthetic CI only, not a supported desktop target.
        if os.statvfs(root).f_flag & getattr(os, 'ST_RDONLY', 1):
            raise MigrationError('Filesystem is read-only', code='unsupported_storage')


def ensure_local_storage(path: Path) -> None:
    """Validate target/work storage, using the nearest existing parent if new.

    The path must be canonical and absolute. Windows currently permits fixed
    NTFS and macOS permits local writable APFS/HFS. Linux is synthetic CI only.
    """
    try:
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts:
            raise MigrationError('Storage paths must be canonical and absolute', code='unsafe_path')
        for component in [*reversed(path.parents), path]:
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise MigrationError('Storage path contains a link or reparse point', code='unsafe_path')
        directory = path if path.is_dir() else path.parent
        while not directory.exists():
            directory = directory.parent
        if not directory.is_dir():
            raise OSError('Storage ancestor is not a directory')
        _check_storage(directory)
    except MigrationError:
        raise
    except (OSError, ValueError) as exc:
        raise MigrationError('Cannot establish safe local storage: ' + str(exc), code='unsupported_storage') from exc


def _windows_file_probe(path: Path) -> None:
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    attributes = kernel.GetFileAttributesW
    attributes.argtypes = [wintypes.LPCWSTR]
    attributes.restype = wintypes.DWORD
    flags = attributes(str(path))
    if flags == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    if flags & (0x400 | 0x1000 | 0x40000 | 0x400000):  # reparse/offline/recall placeholders
        raise OSError('Linked or cloud-placeholder files cannot be migrated')
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CloseHandle(handle)


def _check_handles(roots):
    if os.name == 'nt':
        for root in roots:
            for directory, dirs, files in os.walk(root, onerror=lambda exc: (_ for _ in ()).throw(exc), followlinks=False):
                for name in dirs + files:
                    path = Path(directory) / name
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise OSError('Linked directories/files cannot be checked safely')
                for name in files:
                    path = Path(directory) / name
                    if path not in _HELD_LOCKS:
                        _windows_file_probe(path)
        return
    if sys.platform == 'darwin':
        # lsof is an OS utility on supported macOS, not a bundled third-party tool.
        for root in roots:
            result = subprocess.run(['/usr/sbin/lsof', '-n', '-P', '-t', '+D', str(root)], capture_output=True, text=True)
            if result.returncode not in (0, 1) or result.stderr.strip():
                raise OSError('Open-file inspection failed: ' + result.stderr.strip())
            pids = result.stdout.split()
            if any(not value.isdigit() for value in pids):
                raise OSError('Unrecognized open-file listing')
            if any(int(pid) != os.getpid() for pid in pids):
                raise MigrationError('Files in the Codex home are open; close all writers', code='busy')
        return
    # Linux is useful for CI; native desktop support is macOS/Windows only.
    proc = Path('/proc')
    if not proc.is_dir():
        raise OSError('No supported handle inspection API')
    for process in proc.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            handles = list((process / 'fd').iterdir())
            for handle in handles:
                try:
                    value = Path(os.readlink(handle))
                except FileNotFoundError:
                    continue
                if value.is_absolute() and any(value == root or root in value.parents for root in roots):
                    raise MigrationError('Files in the Codex home are open', code='busy')
        except FileNotFoundError:
            continue


def ensure_idle(home: Path, sqlite_home: Path | None = None) -> None:
    """Fail closed unless process enumeration AND file occupancy checks succeed."""
    try:
        roots = []
        for value in (home, sqlite_home):
            if value is None:
                continue
            path = Path(value).absolute()
            if path.is_symlink() or not path.is_dir():
                raise OSError('Home must be an existing real directory')
            if path not in roots:
                ensure_local_storage(path)
                roots.append(path)
        for pid, executable in _processes():
            name = re.split(r'[/\\]', executable)[-1].lower()
            if pid != os.getpid() and (name in ('codex', 'codex.exe', 'codex app', 'codex app.exe') or name.startswith('codex-')):
                raise MigrationError('Codex or a related writer is running; close it before continuing', code='busy')
        _check_handles(roots)
    except MigrationError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise MigrationError('Cannot prove that Codex is idle: ' + str(exc), code='idle_unknown') from exc

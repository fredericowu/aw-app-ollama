"""Root bootstrap: reconcile model-store ownership, then drop to the bind owner.

Runs as root only long enough to make a v0.1.0 (root-owned) model store writable
by the numeric owner of the bind root, then irrevocably becomes that user and
execs the gateway. Fails closed with an actionable message on any problem.
"""
import ctypes
import os
import re
import stat
import sys
import tempfile

BIND = os.environ.get("AW_OLLAMA_BIND", "/var/lib/ollama/.ollama")
GATEWAY = ["/app/venv/bin/python", "/app/gateway.py"]
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4


class BootstrapError(Exception):
    pass


def reconcile(dir_fd, path, uid, gid, dev):
    """Chown mismatched entries below dir_fd without following symlinks."""
    with os.scandir(dir_fd) as it:
        entries = list(it)
    for entry in entries:
        child = os.path.join(path, entry.name)
        st = os.stat(entry.name, dir_fd=dir_fd, follow_symlinks=False)
        if st.st_dev != dev:
            raise BootstrapError(f"nested mount or device boundary at {child}")
        mode = st.st_mode
        if stat.S_ISLNK(mode):
            # Ollama itself writes relative manifest links inside its store.
            # Re-own the link without following it; reject anything that could
            # point outside the bind.
            target = os.readlink(entry.name, dir_fd=dir_fd)
            resolved = os.path.normpath(os.path.join(path, target))
            if os.path.isabs(target) or os.path.commonpath([resolved, BIND]) != os.path.normpath(BIND):
                raise BootstrapError(f"unexpected symlink at {child} -> {target}")
            if (st.st_uid, st.st_gid) != (uid, gid):
                os.chown(entry.name, uid, gid, dir_fd=dir_fd, follow_symlinks=False)
            continue
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise BootstrapError(f"unexpected special file at {child}")
        if stat.S_ISREG(mode) and st.st_nlink > 1:
            raise BootstrapError(f"multiply linked file at {child}")
        if stat.S_ISDIR(mode):
            fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        else:
            fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        try:
            fst = os.fstat(fd)
            if (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
                raise BootstrapError(f"entry changed during repair: {child}")
            if (fst.st_uid, fst.st_gid) != (uid, gid):
                os.fchown(fd, uid, gid)
            if stat.S_ISDIR(mode):
                reconcile(fd, child, uid, gid, dev)
        finally:
            os.close(fd)


def overflow_gid():
    try:
        with open("/proc/sys/kernel/overflowgid") as f:
            return int(f.read())
    except (OSError, ValueError):
        return 65534


def in_user_namespace():
    """True unless uid_map is the identity map (rootful engine)."""
    try:
        with open("/proc/self/uid_map") as f:
            return f.read().split() != ["0", "0", "4294967295"]
    except OSError:
        return True


def next_groups(groups, gid, overflow, userns):
    """Supplementary groups to apply on drop, or None to leave them untouched.

    Runtime-granted groups (--group-add, or Podman keep-groups for GPU render
    nodes) must survive. With keep-groups the host groups appear as the
    overflow GID, which cannot be re-applied from inside the user namespace
    (crun skips setgroups for the same reason), so leave them as they are.
    GID 0 is root's group only on a rootful engine; in a user namespace it is
    the engine user's own primary group and may be what grants device access.
    """
    if overflow in groups:
        return None
    shed = {gid} if userns else {0, gid}
    return [g for g in groups if g not in shed]


def mount_points():
    """All mount points from mountinfo (st_dev/ismount miss same-device binds)."""
    with open("/proc/self/mountinfo", "rb") as f:
        return [os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m.group(1), 8)]), line.split()[4]))
                for line in f]


def mounts_below(path):
    base = os.path.normpath(path)
    return [p for p in mount_points() if p != base and p.startswith(base + "/")]


def lock_down():
    """Clear every capability set and forbid regaining privilege via exec."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise BootstrapError("could not set no_new_privs")

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

    data = (Data * 2)()
    if libc.capset(ctypes.byref(Header(0x20080522, 0)), data) != 0:
        raise BootstrapError("could not clear inheritable capabilities")


def drop(uid, gid):
    groups = next_groups(os.getgroups(), gid, overflow_gid(), in_user_namespace())
    if groups is not None:
        os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)
    lock_down()
    if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
        raise BootstrapError("privilege drop did not take effect")
    try:
        os.setuid(0)
    except PermissionError:
        return
    raise BootstrapError("root could be regained after privilege drop")


def main():
    try:
        if os.geteuid() == 0:
            if not os.path.isdir(BIND) or os.path.islink(BIND) or os.path.normpath(BIND) not in mount_points():
                raise BootstrapError(f"{BIND} must be a mounted directory (the workspace data bind)")
            root = os.stat(BIND)
            uid, gid = root.st_uid, root.st_gid
            if (uid == 0 or gid == 0) and not in_user_namespace():
                raise BootstrapError(f"{BIND} is owned by {uid}:{gid}; expected a nonzero workspace owner")
            nested = mounts_below(BIND)
            if nested:
                raise BootstrapError(f"nested mount inside the model store: {nested[0]}")
            if uid == 0 or gid == 0:
                # Rootless engine: the data owner is the engine user, which is
                # container root here. There is no higher identity to drop from.
                print("ollama bootstrap: data owner maps to 0 in a user namespace; running unprivileged on the host", flush=True)
                lock_down()
            else:
                fd = os.open(BIND, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    reconcile(fd, BIND, uid, gid, root.st_dev)
                finally:
                    os.close(fd)
                drop(uid, gid)
                os.environ["HOME"] = os.path.dirname(BIND.rstrip("/"))
        try:
            # Anonymous/exclusive temp file: never touches an existing entry.
            with tempfile.TemporaryFile(dir=BIND):
                pass
        except OSError as exc:
            raise BootstrapError(f"{BIND} is not writable by uid {os.geteuid()}: {exc}")
    except (BootstrapError, OSError) as exc:
        print(f"ollama bootstrap failed: {exc}", file=sys.stderr, flush=True)
        sys.exit(78)
    os.execv(GATEWAY[0], GATEWAY)


if __name__ == "__main__":
    main()

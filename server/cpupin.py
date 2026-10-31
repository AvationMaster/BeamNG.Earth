"""Separate cores for the world server and the game (Linux; a recorded session 2026-10-15, Las Vegas: whenever the
server's workers were busy the game fell to 12-24 fps with its physics at 0.5-0.9 of real time, even at idle
scheduling priority: the workers ran on the other half of the game's physical cores, hyperthreads sharing the cores'
execution units and caches. With the server on two cores and the game on the other six: 51-56 fps and the physics at
real time while the server worked; the server alone pinned did not help, the game's threads still landed there).

Windows too (KyzoFox 1.5.56): the same split through kernel32 (process affinity masks, which the worker processes
inherit; the cores from GetLogicalProcessorInformation). Before, a Windows server only lowered its priority class,
and on a 6-core laptop (i5-11400H) the CPU sat at 100 % with the game at 17-20 fps; the user fell through a bridge
whose tiles came 19-47 s late. BNGE_SERVER_CORES=<n> sets how many physical cores the server gets.

A hybrid CPU (Alder Lake and later: P-cores and E-cores) is split by kind instead (kinds below): the last quarter
of the physical cores there are E-cores, so the whole world build ran on the slowest cores while the game had every
faster one it did not need (a 13700K, 8 P + 8 E: 4 E-cores for the server's three tile threads, three far-tile and
four road-tile processes; 8 P-cores and 4 E-cores for the game). There the server gets all the E-cores and the last
HYBRID_P_CORES P-cores, the game the rest of the P-cores (BNGE_SERVER_P_CORES=<n> sets how many at start; the mod's
settings page sets CORE_LEVELS live, the server's 'cores' message).

    server_cpus, game_cpus = split()              # the CPU sets, or (None, None) when it cannot be done
    pin_self(server_cpus)                          # the server (its threads and worker processes inherit it)
    pin_game(game_cpus)                            # every thread of the running game
"""
import os
import sys

EXE = "BeamNG.drive.x64"
WIN = sys.platform == "win32"
WIN_EXE = EXE.lower() + ".exe"
SERVER_SHARE = 0.25         # of the physical cores, at least one, never more than half
# Hybrid CPUs: the E-cores for the server (a tile build is throughput work) and the fastest P-cores for the game (its
# physics, render and streaming threads must never land on an E-core: Windows' scheduler puts them there under load)
HYBRID_P_CORES = 2          # P-cores the server takes as well (all the E-cores go with them)
HYBRID_P_MIN = 6            # ... from this many P-cores per CPU: fewer, the last-quarter rule leaves the game enough
HYBRID_P_SHARE = 0.5        # ... at most this share of the P-cores
GAME_P_KEEP = 4             # P-cores the game always keeps (its physics and render threads; 4 measured as the floor)
# The settings page's "World build cores" (core.serverCoresSet, the mod's 'cores' message): (share of the physical
# cores, P-cores on a hybrid CPU) for split below. The player trades the world's build speed for the game's frame rate
# (2026-10-31: "it builds the world actually quicker than i need it to ... free up some cores for the game").
CORE_LEVELS = {
    "max": (SERVER_SHARE, HYBRID_P_CORES),      # the split the server starts with
    "balanced": (SERVER_SHARE / 2, 1),
    "min": (0.0, 0),                            # the E-cores alone (a CPU of one kind: one core)
}


def _siblings(usable=None):
    """[(core key, [cpu ids])] of the usable CPUs (default: this process's) grouped by physical core, in core order."""
    if WIN:
        return _win_cores(usable)
    if usable is None:
        usable = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    groups = {}
    for cpu in usable:
        base = f"/sys/devices/system/cpu/cpu{cpu}/topology/"
        try:
            core = int(open(base + "core_id").read())
            pkg = int(open(base + "physical_package_id").read())
        except (OSError, ValueError):
            core, pkg = cpu, 0
        groups.setdefault((pkg, core), []).append(cpu)
    return sorted(groups.items())


def machine_cores():
    """[(core key, [cpu ids])] of this process's usable CPUs, the way split() sees them. Call it before pin_self: once
    the server is pinned it sees only its own CPUs, and a split again from those (the mod's 'cores' message) would be
    a split of the server's share alone."""
    return _siblings()


def split(share=SERVER_SHARE, p_cores=None, cores=None):
    """(server CPU set, game CPU set): the last cores (with their hyperthread siblings) for the server; on a hybrid
    CPU the E-cores and the last `p_cores` P-cores (HYBRID_P_CORES by default, see _hybrid). `cores`: the CPU's
    physical cores (machine_cores, kept from the start) instead of this process's usable ones."""
    cores = _siblings() if cores is None else cores
    if len(cores) < 4:
        return None, None              # too few cores to split usefully
    kind = kinds(cores)
    p = [i for i, k in enumerate(kind or ()) if k == "p"]
    if p and len(p) >= HYBRID_P_MIN:
        return _hybrid(cores, kind, p_cores)
    k = max(1, min(len(cores) // 2, int(round(len(cores) * share))))
    try:                                # (the player's choice: BNGE_SERVER_CORES=<n>, still at most half)
        want = int(os.environ.get("BNGE_SERVER_CORES", "") or 0)
        if want > 0:
            k = max(1, min(len(cores) // 2, want))
    except ValueError:
        pass
    server = {c for _, cpus in cores[-k:] for c in cpus}
    game = {c for _, cpus in cores[:-k] for c in cpus}
    return server, game


def _hybrid(cores, kind, p_cores=None):
    """(server, game) on a hybrid CPU: all the E-cores and the last `want` P-cores for the server, the other P-cores
    for the game. `p_cores` (the settings page's "World build cores") or BNGE_SERVER_P_CORES=<n> sets the P-cores (0:
    the E-cores alone); the game keeps GAME_P_KEEP of them whatever the setting, so a fast core runs its physics."""
    p = [i for i, k in enumerate(kind) if k == "p"]
    e = [i for i, k in enumerate(kind) if k == "e"]
    want = HYBRID_P_CORES
    try:
        if p_cores is None and os.environ.get("BNGE_SERVER_P_CORES", ""):    # (the env var, for a manual start)
            want = int(os.environ["BNGE_SERVER_P_CORES"])
        elif p_cores is not None:
            want = int(p_cores)
    except (TypeError, ValueError):
        pass
    take = max(0, min(want, int(len(p) * HYBRID_P_SHARE), len(p) - GAME_P_KEEP))
    server_i = set(e) | set(p[len(p) - take:])
    server = {c for i in server_i for c in cores[i][1]}
    game = {c for i in range(len(cores)) if i not in server_i for c in cores[i][1]}
    return server, game


def pin_self(cpus):
    if cpus and WIN:
        _win_set(None, cpus)
    elif cpus and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cpus)


def game_pids():
    """The running game's main processes (not its CEF helpers) of this user."""
    if WIN:                 # (its helpers too: on the game's CPUs, where they belong)
        return [pid for pid, _pp, exe in _win_processes() if exe.lower() == WIN_EXE]
    out = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if uid is not None and os.stat(f"/proc/{d}").st_uid != uid:
                continue
            exe = os.path.basename(os.readlink(f"/proc/{d}/exe"))
            if exe != EXE:
                continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                if b"--type=" in f.read():
                    continue
            out.append(int(d))
        except OSError:
            continue
    return out


def pin_game(cpus):
    """Every thread of the game on cpus; the number of threads set (0: no game, or not allowed). Windows: the number
    of processes (a process's mask holds all its threads)."""
    if cpus and WIN:
        return sum(_win_set(pid, cpus) for pid in game_pids())
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return 0
    n = 0
    for pid in game_pids():
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cpus)
                n += 1
            except OSError:
                pass
    return n


# --- the loading boost ------------------------------------------------------------------------------------------------
# Right after a teleport the player stands and waits for the world (2026-10-16: ~30 s in Fells Point, the server's
# four CPUs busy throughout while the game's twelve mostly idled). Until the far field is done or the car moves, the
# server's processes may also run on half of the game's cores (the last ones); they keep their idle scheduling, so a
# game thread that wants a CPU still gets it. Then they go back to their own cores.
LEND_SHARE = 0.5            # of the game's physical cores


def boosted(server_cpus, game_cpus, share=LEND_SHARE):
    """The server's CPU set while loading: its own plus the last `share` of the game's physical cores (with their
    hyperthread siblings); None when there is nothing to lend."""
    if not server_cpus or not game_cpus:
        return None
    # (every CPU of the split: the server process itself is pinned to its own by now)
    cores = [cpus for _, cpus in _siblings(sorted(set(server_cpus) | set(game_cpus))) if set(cpus) <= set(game_cpus)]
    k = int(len(cores) * share)
    if k <= 0:
        return None
    return set(server_cpus) | {c for cpus in cores[-k:] for c in cpus}


def _children():
    kids = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    return kids


def pin_tree(cpus, root=None):
    """Every thread of this process (or root) and of its descendants on cpus; the number of threads set (Windows: of
    processes)."""
    if cpus and WIN:
        procs = _win_processes()
        kids = {}
        for pid, pp, _exe in procs:
            kids.setdefault(pp, []).append(pid)
        todo, n, seen = [root or os.getpid()], 0, set()
        while todo:
            pid = todo.pop()
            if pid in seen:
                continue
            seen.add(pid)
            todo += kids.get(pid, [])
            n += _win_set(pid, cpus)
        return n
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return 0
    kids = _children()
    todo, n = [root or os.getpid()], 0
    while todo:
        pid = todo.pop()
        todo += kids.get(pid, [])
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cpus)
                n += 1
            except OSError:
                pass
    return n


# --- Windows (KyzoFox 1.5.56) -----------------------------------------------------------------------------------------

def _k32():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetLogicalProcessorInformationEx.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.GetLogicalProcessorInformationEx.restype = wintypes.BOOL
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    k.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                         ctypes.POINTER(ctypes.c_size_t)]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.GetLogicalProcessorInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.SetProcessAffinityMask.restype = wintypes.BOOL
    return k


def _mask(cpus):
    m = 0
    for c in cpus:
        if 0 <= c < 64:             # (processor group 0)
            m |= 1 << c
    return m


def _bits(m):
    return [c for c in range(64) if m >> c & 1]


def cores_from_masks(masks, usable):
    """[((0, i), [cpu ids])] per physical core from the cores' processor masks, those CPUs in usable only. Pure."""
    use = set(usable) if usable is not None else None
    out = []
    for m in masks:
        cpus = [c for c in _bits(m) if use is None or c in use]
        if cpus:
            out.append(cpus)
    out.sort(key=lambda c: c[0])
    return [((0, i), c) for i, c in enumerate(out)]


def _win_core_masks():
    """The physical cores' processor masks (GetLogicalProcessorInformation, RelationProcessorCore)."""
    import ctypes

    class SLPI(ctypes.Structure):
        _fields_ = [("ProcessorMask", ctypes.c_size_t), ("Relationship", ctypes.c_int),
                    ("Reserved", ctypes.c_ulonglong * 2)]
    k = _k32()
    n = ctypes.c_ulong(0)
    k.GetLogicalProcessorInformation(None, ctypes.byref(n))
    if not n.value:
        return []
    buf = (SLPI * (n.value // ctypes.sizeof(SLPI) + 1))()
    if not k.GetLogicalProcessorInformation(buf, ctypes.byref(n)):
        return []
    return [buf[i].ProcessorMask for i in range(n.value // ctypes.sizeof(SLPI)) if buf[i].Relationship == 0]


def _win_classes():
    """{cpu id: efficiency class} from GetLogicalProcessorInformationEx, a hybrid Intel's P-cores 1 and E-cores 0 (a
    CPU of one kind: every core the same); {} when it cannot be read."""
    import ctypes
    from struct import unpack_from
    try:
        k = _k32()
        n = ctypes.c_ulong(0)
        core = 0                                    # RelationProcessorCore
        k.GetLogicalProcessorInformationEx(core, None, ctypes.byref(n))
        if not n.value:
            return {}
        buf = ctypes.create_string_buffer(n.value)
        if not k.GetLogicalProcessorInformationEx(core, buf, ctypes.byref(n)):
            return {}
        out, off = {}, 0
        while off + 8 <= n.value:
            rel, size = unpack_from("<II", buf.raw, off)
            if not size:
                break
            # SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX: Relationship, Size, then PROCESSOR_RELATIONSHIP's Flags,
            # EfficiencyClass, Reserved[20], GroupCount, GroupMask[0] (KAFFINITY Mask, WORD Group)
            if rel == core:
                eff = buf.raw[off + 9]
                mask, group = unpack_from("<QH", buf.raw, off + 32)
                if group == 0:                      # (processor group 0: every mask in this module)
                    for c in _bits(mask):
                        out[c] = eff
            off += size
        return out
    except Exception:
        return {}


def _win_cores(usable=None):
    try:
        if usable is None:
            import ctypes
            k = _k32()
            pm, sm = ctypes.c_size_t(0), ctypes.c_size_t(0)
            if k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(pm), ctypes.byref(sm)):
                usable = _bits(pm.value)
        return cores_from_masks(_win_core_masks(), usable)
    except Exception:
        return []


def _cpu_list(s):
    """The CPU ids of a kernel cpu list, "0-3,8,12-15": [0, 1, 2, 3, 8, 12, 13, 14, 15]."""
    out = []
    for part in s.strip().split(","):
        part = part.strip()
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                out += list(range(int(a), int(b) + 1))
            elif part:
                out.append(int(part))
        except ValueError:
            continue
    return out


def _sysfs_classes():
    """{cpu id: efficiency class} of a hybrid CPU from the kernel: Intel's hybrid groups (/sys/devices/cpu_core,
    cpu_atom, intel_pstate), else each CPU's cpu_capacity (ARM big.LITTLE); {} when the machine does not say."""
    out = {}
    for path, cls in (("/sys/devices/cpu_core/cpus", 1), ("/sys/devices/cpu_atom/cpus", 0)):
        try:
            with open(path) as f:
                out.update({c: cls for c in _cpu_list(f.read())})
        except OSError:
            pass
    if out:
        return out
    try:
        names = os.listdir("/sys/devices/system/cpu")
    except OSError:
        return out
    for d in names:
        if d.startswith("cpu") and d[3:].isdigit():
            try:
                with open(f"/sys/devices/system/cpu/{d}/cpu_capacity") as f:
                    out[int(d[3:])] = int(f.read())
            except (OSError, ValueError):
                continue
    return out


def kinds_from(cores, classes):
    """'p' / 'e' per physical core (the order of cores), the highest class the P-cores; None when a core's class is
    unknown or every core has the same one (not a hybrid: nothing to choose, the last-quarter rule fits)."""
    if not classes:
        return None
    vals = []
    for _key, cpus in cores:
        got = {classes[c] for c in cpus if c in classes}
        if not got:
            return None
        vals.append(max(got))
    if len(set(vals)) < 2:
        return None
    top = max(vals)
    return ["p" if v == top else "e" for v in vals]


def kinds(cores):
    """'p' / 'e' per physical core of a hybrid CPU, else None."""
    return kinds_from(cores, _win_classes() if WIN else _sysfs_classes())


def _win_set(pid, cpus):
    """Process pid's (None: this one's) CPUs; 1 if set, else 0."""
    try:
        k = _k32()
        if pid is None or pid == os.getpid():
            return 1 if k.SetProcessAffinityMask(k.GetCurrentProcess(), _mask(cpus)) else 0
        h = k.OpenProcess(0x0200 | 0x1000, False, int(pid))     # PROCESS_SET_INFORMATION | QUERY_LIMITED_INFORMATION
        if not h:
            return 0
        try:
            return 1 if k.SetProcessAffinityMask(h, _mask(cpus)) else 0
        finally:
            k.CloseHandle(h)
    except Exception:
        return 0


def _win_processes():
    """[(pid, parent pid, exe name)] of the running processes (a Toolhelp snapshot)."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    out = []
    try:
        k = _k32()
        snap = k.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return out
        try:
            e = PE()
            e.dwSize = ctypes.sizeof(PE)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
    except Exception:
        pass
    return out

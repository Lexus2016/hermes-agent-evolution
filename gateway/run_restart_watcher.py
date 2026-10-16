"""Detached restart watcher source and profile-scoped environment selection.

The environment mixin is inherited by ``GatewayShutdownMixin`` so its existing
patch seam remains the owner of watcher construction.
"""

from __future__ import annotations

# Windows has no bash/setsid chain: a tiny detached Python watcher waits for the gateway PID to
# exit (bounded), then spawns ``hermes gateway restart``.
_WINDOWS_RESTART_WATCHER = """
import os, subprocess, sys, time
from hermes_cli._subprocess_compat import windows_detach_flags_without_breakaway
pid = int(sys.argv[1])
restart_after_s = float(sys.argv[2])
cmd = sys.argv[3:]
deadline = time.monotonic() + restart_after_s

def _alive(p):
    # On Windows, os.kill(pid, 0) is NOT a no-op — it maps to
    # GenerateConsoleCtrlEvent(0, pid) (bpo-14484). Use the
    # Win32 handle-based existence check instead.
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.WaitForSingleObject.restype = ctypes.c_uint
        k32.GetLastError.restype = ctypes.c_uint
        h = k32.OpenProcess(0x1000 | 0x100000, False, int(p))
        if not h:
            return k32.GetLastError() != 87
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(p), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

while time.monotonic() < deadline:
    if not _alive(pid):
        break
    time.sleep(0.2)
subprocess.Popen(
    cmd,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    creationflags=windows_detach_flags_without_breakaway(),
)
""".strip()


class GatewayRestartEnvironmentMixin:
    """Select the owning profile environment for a detached restart watcher."""

    @staticmethod
    def _restart_watcher_env() -> dict:
        """Watcher env minus ``_HERMES_GATEWAY`` (else the CLI's self-restart guard refuses; gateway stays down).

        The host multiplexer is respawned with ``host_gateway_child_env`` (default-root
        secrets via ``served_profile_child_env``, not ``os.environ.copy()``). A standalone
        named-profile gateway keeps that profile's home — only a multiplexer, or a process
        already on the default root, is the host.
        """
        from gateway.config_loader import drop_bridged_env
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from tools.environments.local import host_gateway_child_env, served_profile_child_env

        home = get_hermes_home()
        try:
            on_default = home.resolve() == get_default_hermes_root().resolve()
        except Exception:
            on_default = False
        # ``resolve_multiplex_mode`` settles the default-on/unset decision before
        # restart. Carry that runtime identity instead of re-reading raw config:
        # ``None`` is the normal pre-resolution value for a named launcher.
        from agent.secret_scope import is_multiplex_active
        settled_multiplex = is_multiplex_active()
        multiplex = False
        if not on_default and not settled_multiplex:
            # Second settled source: the live host gateway's OWN published record
            # (its settled served set). Only when NO settled identity exists may the
            # raw config re-read stand — it reads the UNSET flag as False, which is
            # wrong exactly when this process IS the default-on host (#120305).
            try:
                from gateway import host_rendezvous as hr
                record = hr.read_record(hr.ROLE_GATEWAY)
                if record is not None and hr.liveness_is_proven(record) and len(record.profiles) > 1:
                    multiplex = True
            except Exception:
                multiplex = False
        if not on_default and not settled_multiplex and not multiplex:
            try:
                from gateway.config import load_gateway_config
                multiplex = bool(load_gateway_config().multiplex_profiles)
            except Exception:
                multiplex = False
        if on_default or settled_multiplex or multiplex:
            watcher_env = host_gateway_child_env()
        else:
            watcher_env = served_profile_child_env(
                target_home=home, inherit_credentials=True,
            )
        watcher_env = drop_bridged_env(watcher_env)
        watcher_env.pop("_HERMES_GATEWAY", None)
        return watcher_env

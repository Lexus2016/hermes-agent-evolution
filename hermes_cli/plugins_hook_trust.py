"""Hook registration trust checks shared by plugin contexts."""


class PluginHookTrustMixin:
    def _within_trusted_plugin_root(self) -> bool:
        """True when this plugin lives under the profile's trusted plugins dir.

        ``source`` alone is not enough: a symlink planted inside
        ``<HERMES_HOME>/plugins/`` is scanned as ``source="user"`` while its
        code lives anywhere on disk. Resolve both sides and require genuine
        containment, so the symlinked-directory vector from #1389 cannot
        inherit profile trust.
        """
        from hermes_cli.plugins import Path, get_hermes_home
        raw_path = getattr(self.manifest, "path", None)
        if not raw_path:
            return False
        try:
            plugin_path = Path(raw_path).resolve(strict=True)
            trusted_root = (get_hermes_home() / "plugins").resolve(strict=True)
        except OSError:
            return False
        return plugin_path == trusted_root or trusted_root in plugin_path.parents

    def _hook_trust_allowed(self) -> bool:
        """Return True if this plugin is trusted to register hooks (#1389).

        Trusted by default:

        * ``bundled`` — ships inside the Hermes repo itself.
        * ``user`` — lives under ``<HERMES_HOME>/plugins/``, the profile
          directory #1389 designates as trusted, and is opted in by name via
          ``plugins.enabled``. Containment is verified against the resolved
          path, not just the source label.
        """
        source = getattr(self.manifest, "source", "") or ""
        if source == "bundled":
            return True
        if source == "user" and self._within_trusted_plugin_root():
            return True
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            return False
        plugin_id = self.manifest.key or self.manifest.name
        entries = (cfg.get("plugins") or {}).get("entries") or {}
        entry = entries.get(f"{source}:{plugin_id}") or {}
        return bool(entry.get("allow_hooks", False))

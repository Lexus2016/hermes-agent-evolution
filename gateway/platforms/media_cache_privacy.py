"""Compatibility imports for the shared descriptor-verified media cache policy."""

from hermes_platform.media_cache_privacy import (
    os,
    stat,
    Path,
    _posix_permissions,
    _shared_policy,
    _verify_owner,
    _verify_private_directory,
    _open_directory,
    secure_media_cache_dir,
    write_cache_file,
)

__all__ = ['Path', '_open_directory', '_posix_permissions', '_shared_policy', '_verify_owner', '_verify_private_directory', 'os', 'secure_media_cache_dir', 'stat', 'write_cache_file']

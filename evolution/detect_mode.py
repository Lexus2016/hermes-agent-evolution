#!/usr/bin/env python3
"""
Evolution Mode Detection for Hermes Evolution Agent

Detects whether the agent is running in PUBLIC or PRIVATE mode.
"""

import os
import sys
from typing import Literal

Mode = Literal["PUBLIC", "PRIVATE"]


def detect_mode() -> Mode:
    """
    Detect the current mode based on available tokens.
    
    Returns:
        "PRIVATE" if GITHUB_PRIVATE_TOKEN is set (owner mode)
        "PUBLIC" if only GITHUB_TOKEN is set (public mode)
        "PUBLIC" if no tokens set (local read-only)
    """
    if os.getenv("GITHUB_PRIVATE_TOKEN"):
        return "PRIVATE"
    
    if os.getenv("GITHUB_TOKEN"):
        return "PUBLIC"
    
    # Default to public mode (read-only)
    return "PUBLIC"


def require_private_mode() -> None:
    """Raise an exception if not in PRIVATE mode."""
    if detect_mode() != "PRIVATE":
        raise PermissionError(
            f"This operation requires PRIVATE mode. Current mode: {detect_mode()}"
        )


def get_github_token() -> str:
    """Get the appropriate GitHub token for the current mode."""
    mode = detect_mode()
    
    if mode == "PRIVATE":
        token = os.getenv("GITHUB_PRIVATE_TOKEN")
        if not token:
            raise ValueError("GITHUB_PRIVATE_TOKEN not set in PRIVATE mode")
        return token
    else:
        token = os.getenv("GITHUB_TOKEN", "")
        return token


def get_github_config() -> dict:
    """Get GitHub configuration based on mode."""
    mode = detect_mode()
    
    return {
        "mode": mode,
        "owner": "Lexus2016",
        "repo": "hermes-agent-evolution",
        "token_env": "GITHUB_PRIVATE_TOKEN" if mode == "PRIVATE" else "GITHUB_TOKEN",
        "permissions": {
            "read": True,
            "create_issues": True,
            "create_prs": True,
            "merge": mode == "PRIVATE",
            "modify_code": mode == "PRIVATE",
        }
    }


def main() -> int:
    """CLI to check current mode."""
    mode = detect_mode()
    config = get_github_config()
    
    print(f"Current mode: {mode}")
    print(f"GitHub: {config['owner']}/{config['repo']}")
    print(f"Permissions:")
    for key, value in config['permissions'].items():
        status = "✓" if value else "✗"
        print(f"  {status} {key}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

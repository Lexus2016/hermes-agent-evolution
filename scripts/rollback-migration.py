#!/usr/bin/env python3
"""
Rollback Script for Hermes Evolution Migration

Rolls back to previous Hermes Agent installation if migration fails.
"""

import os
import sys
import shutil
from pathlib import Path


def rollback(backup_path: str, target_path: str = None) -> bool:
    """
    Rollback to backup.
    
    Args:
        backup_path: Path to backup directory
        target_path: Path where to restore (default: ~/.hermes)
    
    Returns:
        True if rollback successful, False otherwise
    """
    backup = Path(backup_path)
    target = Path(target_path or os.path.expanduser("~/.hermes"))
    
    print("=" * 60)
    print("Hermes Evolution Rollback")
    print("=" * 60)
    print()
    
    # Verify backup exists
    if not backup.exists():
        print(f"❌ Backup not found: {backup}")
        return False
    
    print(f"📂 Backup: {backup}")
    print(f"📂 Target: {target}")
    print()
    
    # Warn about data loss
    if target.exists():
        print("⚠️  WARNING: This will replace the current installation!")
        response = input("Continue? (yes/no): ")
        if response.lower() != "yes":
            print("Rollback cancelled.")
            return False
    
    # Remove current installation
    if target.exists():
        print("🗑️  Removing current installation...")
        shutil.rmtree(target)
    
    # Restore backup
    print("📦 Restoring backup...")
    shutil.copytree(backup, target)
    
    print("✅ Rollback complete!")
    print()
    print("Your previous installation has been restored.")
    print(f"Location: {target}")
    
    return True


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Rollback Hermes Evolution migration"
    )
    parser.add_argument(
        "backup_path",
        nargs="?",
        help="Path to backup directory"
    )
    parser.add_argument(
        "--target",
        default=os.path.expanduser("~/.hermes"),
        help="Target path (default: ~/.hermes)"
    )
    
    args = parser.parse_args()
    
    # Get backup path
    backup_path = args.backup_path
    if not backup_path:
        # Try to read from last backup file
        last_backup_file = Path(os.path.expanduser("~/.hermes.last_backup"))
        if last_backup_file.exists():
            backup_path = last_backup_file.read_text(encoding="utf-8").strip()
        else:
            print("❌ No backup path provided")
            print("Usage: python rollback_migration.py <backup_path>")
            sys.exit(1)
    
    # Run rollback
    success = rollback(backup_path, args.target)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

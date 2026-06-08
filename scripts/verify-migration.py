#!/usr/bin/env python3
"""
Migration Verification Script for Hermes Evolution

Verifies that migration from Hermes Agent to Hermes Evolution
preserved all data, configurations, and customizations.
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple


class MigrationVerifier:
    """Verify migration integrity."""
    
    def __init__(self, backup_path: str, current_path: str = None):
        """
        Initialize verifier.
        
        Args:
            backup_path: Path to backup directory
            current_path: Path to current installation (default: ~/.hermes)
        """
        self.backup_path = Path(backup_path)
        self.current_path = Path(current_path or os.path.expanduser("~/.hermes"))
        self.issues = []
        self.warnings = []
        self.successes = []
    
    def verify(self) -> bool:
        """
        Run all verification checks.
        
        Returns:
            True if verification passed, False otherwise
        """
        print("=" * 60)
        print("Hermes Evolution Migration Verification")
        print("=" * 60)
        print()
        
        # Check if backup exists
        if not self.backup_path.exists():
            self.issues.append(f"Backup directory not found: {self.backup_path}")
            print(f"❌ Backup directory not found: {self.backup_path}")
            return False
        
        print(f"📂 Backup: {self.backup_path}")
        print(f"📂 Current: {self.current_path}")
        print()
        
        # Run verification checks
        self.verify_profiles()
        self.verify_skills()
        self.verify_cron_jobs()
        self.verify_memories()
        self.verify_configurations()
        self.verify_session_data()
        
        # Print summary
        self.print_summary()
        
        return len(self.issues) == 0
    
    def verify_profiles(self):
        """Verify all profiles are preserved."""
        print("🔍 Verifying profiles...")
        
        backup_profiles = self.backup_path / "profiles"
        current_profiles = self.current_path / "profiles"
        
        if not backup_profiles.exists():
            self.warnings.append("No profiles in backup (might be fresh install)")
            print("  ⚠️  No profiles in backup")
            return
        
        if not current_profiles.exists():
            self.issues.append("Profiles directory missing in current installation")
            print("  ❌ Profiles directory missing")
            return
        
        backup_profile_list = list(backup_profiles.iterdir())
        current_profile_list = list(current_profiles.iterdir())
        
        backup_names = {p.name for p in backup_profile_list if p.is_dir()}
        current_names = {p.name for p in current_profile_list if p.is_dir()}
        
        # Check if all profiles are present
        missing = backup_names - current_names
        if missing:
            self.issues.append(f"Missing profiles: {missing}")
            print(f"  ❌ Missing profiles: {missing}")
        else:
            self.successes.append(f"All {len(backup_names)} profiles preserved")
            print(f"  ✅ All {len(backup_names)} profiles preserved")
        
        # Check for new profiles (evolution might add some)
        new = current_names - backup_names
        if new:
            self.successes.append(f"New profiles added: {new}")
            print(f"  ✅ New profiles added: {new}")
    
    def verify_skills(self):
        """Verify custom skills are preserved."""
        print("🔍 Verifying skills...")
        
        backup_skills = self.backup_path / "skills"
        current_skills = self.current_path / "skills"
        
        if not backup_skills.exists():
            print("  ⚠️  No skills in backup")
            return
        
        if not current_skills.exists():
            self.issues.append("Skills directory missing in current installation")
            print("  ❌ Skills directory missing")
            return
        
        backup_skill_list = [d for d in backup_skills.iterdir() if d.is_dir()]
        current_skill_list = [d for d in current_skills.iterdir() if d.is_dir()]
        
        backup_names = {s.name for s in backup_skill_list}
        current_names = {s.name for s in current_skill_list}
        
        # Check if custom skills are present
        # (exclude evolution skills as they're new)
        evolution_skills = {"evolution"}
        custom_backup = backup_names - evolution_skills
        custom_current = current_names - evolution_skills
        
        missing = custom_backup - custom_current
        if missing:
            self.issues.append(f"Missing custom skills: {missing}")
            print(f"  ❌ Missing custom skills: {missing}")
        else:
            preserved = len(custom_backup)
            self.successes.append(f"All {preserved} custom skills preserved")
            print(f"  ✅ All {preserved} custom skills preserved")
        
        # Check for evolution skills
        evolution_present = "evolution" in current_names
        if evolution_present:
            self.successes.append("Evolution skills added")
            print("  ✅ Evolution skills added")
    
    def verify_cron_jobs(self):
        """Verify cron jobs are preserved."""
        print("🔍 Verifying cron jobs...")
        
        backup_cron = self.backup_path / "cron"
        current_cron = self.current_path / "cron"
        
        if not backup_cron.exists():
            print("  ⚠️  No cron jobs in backup")
            return
        
        if not current_cron.exists():
            self.issues.append("Cron directory missing in current installation")
            print("  ❌ Cron directory missing")
            return
        
        # Count job files
        backup_jobs = len(list(backup_cron.glob("*.yaml")))
        current_jobs = len(list(current_cron.glob("*.yaml")))
        
        if current_jobs >= backup_jobs:
            self.successes.append(f"Cron jobs preserved ({current_jobs} total)")
            print(f"  ✅ Cron jobs preserved ({current_jobs} total)")
        else:
            self.issues.append(f"Some cron jobs missing ({current_jobs}/{backup_jobs})")
            print(f"  ❌ Some cron jobs missing ({current_jobs}/{backup_jobs})")
    
    def verify_memories(self):
        """Verify memories are preserved."""
        print("🔍 Verifying memories...")
        
        backup_memories = self.backup_path / "memories"
        current_memories = self.current_path / "memories"
        
        if not backup_memories.exists():
            print("  ⚠️  No memories in backup")
            return
        
        if not current_memories.exists():
            self.warnings.append("Memories directory missing (might be fresh)")
            print("  ⚠️  Memories directory missing")
            return
        
        backup_count = len(list(backup_memories.glob("*.json")))
        current_count = len(list(current_memories.glob("*.json")))
        
        if current_count >= backup_count:
            self.successes.append(f"Memories preserved ({current_count} files)")
            print(f"  ✅ Memories preserved ({current_count} files)")
        else:
            self.warnings.append(f"Some memories missing ({current_count}/{backup_count})")
            print(f"  ⚠️  Some memories missing ({current_count}/{backup_count})")
    
    def verify_configurations(self):
        """Verify configurations are preserved."""
        print("🔍 Verifying configurations...")
        
        configs_to_check = [
            "config.yaml",
            ".env",
        ]
        
        for config in configs_to_check:
            backup_config = self.backup_path / config
            current_config = self.current_path / config
            
            if backup_config.exists():
                if current_config.exists():
                    self.successes.append(f"{config} preserved")
                    print(f"  ✅ {config} preserved")
                else:
                    self.warnings.append(f"{config} missing (might use defaults)")
                    print(f"  ⚠️  {config} missing")
    
    def verify_session_data(self):
        """Verify session data is preserved."""
        print("🔍 Verifying session data...")
        
        backup_sessions = self.backup_path / "sessions"
        current_sessions = self.current_path / "sessions"
        
        if not backup_sessions.exists():
            print("  ⚠️  No sessions in backup")
            return
        
        if not current_sessions.exists():
            self.warnings.append("Sessions directory missing")
            print("  ⚠️  Sessions directory missing")
            return
        
        backup_count = len(list(backup_sessions.glob("*")))
        current_count = len(list(current_sessions.glob("*")))
        
        if current_count >= backup_count:
            self.successes.append(f"Sessions preserved ({current_count} files)")
            print(f"  ✅ Sessions preserved ({current_count} files)")
        else:
            self.warnings.append(f"Some sessions missing ({current_count}/{backup_count})")
            print(f"  ⚠️  Some sessions missing ({current_count}/{backup_count})")
    
    def print_summary(self):
        """Print verification summary."""
        print()
        print("=" * 60)
        print("Verification Summary")
        print("=" * 60)
        print()
        
        # Successes
        if self.successes:
            print(f"✅ Successes ({len(self.successes)}):")
            for success in self.successes:
                print(f"  • {success}")
            print()
        
        # Warnings
        if self.warnings:
            print(f"⚠️  Warnings ({len(self.warnings)}):")
            for warning in self.warnings:
                print(f"  • {warning}")
            print()
        
        # Issues
        if self.issues:
            print(f"❌ Issues ({len(self.issues)}):")
            for issue in self.issues:
                print(f"  • {issue}")
            print()
        
        # Final verdict
        if not self.issues:
            print("🎉 Migration verification PASSED!")
            print()
            print("Your data has been preserved successfully.")
            print("You can now use Hermes Evolution with all your customizations.")
        else:
            print("❌ Migration verification FAILED!")
            print()
            print("Some data may be missing. You can restore from backup:")
            print(f"  rm -rf {self.current_path}")
            print(f"  cp -r {self.backup_path} {self.current_path}")
        
        print()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Verify Hermes Evolution migration"
    )
    parser.add_argument(
        "backup_path",
        nargs="?",
        help="Path to backup directory (default: read from ~/.hermes.last_backup)"
    )
    parser.add_argument(
        "--current",
        default=os.path.expanduser("~/.hermes"),
        help="Path to current installation (default: ~/.hermes)"
    )
    
    args = parser.parse_args()
    
    # Get backup path
    backup_path = args.backup_path
    if not backup_path:
        # Try to read from last backup file
        last_backup_file = Path(os.path.expanduser("~/.hermes.last_backup"))
        if last_backup_file.exists():
            backup_path = last_backup_file.read_text().strip()
        else:
            print("❌ No backup path provided and ~/.hermes.last_backup not found")
            print("Usage: python verify_migration.py <backup_path>")
            sys.exit(1)
    
    # Run verification
    verifier = MigrationVerifier(backup_path, args.current)
    success = verifier.verify()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

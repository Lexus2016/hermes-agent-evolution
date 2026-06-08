#!/bin/bash
# REAL Automatic Upgrade Script: Hermes Agent → Hermes Evolution
# This script DOES EVERYTHING automatically - works on any system with Hermes installed

set -e

echo "🧬 Automatic Upgrade to Hermes Evolution"
echo "=========================================="
echo ""

# Configuration
EVOLUTION_REPO="https://github.com/Lexus2016/hermes-agent-evolution.git"
EVOLUTION_DIR="$HOME/hermes-agent-evolution"
BACKUP_DATE=$(date +%Y%m%d_%H%M%S)

# Detect where Hermes is installed
echo "🔍 Detecting Hermes installation..."
HERMES_PROJECT=$(hermes --version 2>/dev/null | grep "Project:" | cut -d' ' -f2 || echo "")
if [ -z "$HERMES_PROJECT" ]; then
    echo "❌ Cannot detect Hermes installation"
    exit 1
fi
echo "✅ Hermes installed at: $HERMES_PROJECT"

HERMES_SKILLS_DIR="$HERMES_PROJECT/skills"
HERMES_CRON_DIR="$HERMES_PROJECT/cron"

echo "📂 Skills directory: $HERMES_SKILLS_DIR"
echo "📂 Cron directory: $HERMES_CRON_DIR"
echo ""

# Step 1: Create backup
echo "📦 Step 1/6: Creating backup..."
if [ -d "$HOME/.hermes" ]; then
    cp -r "$HOME/.hermes" "$HOME/.hermes.backup.$BACKUP_DATE"
    echo "✅ Backup created: ~/.hermes.backup.$BACKUP_DATE"
else
    echo "ℹ️  No existing .hermes found (fresh installation)"
fi

# Step 2: Clean up and clone
echo ""
echo "📥 Step 2/6: Cloning Hermes Evolution..."
rm -rf "$EVOLUTION_DIR" /tmp/hermes-evolution
git clone "$EVOLUTION_REPO" "$EVOLUTION_DIR"
echo "✅ Cloned to: $EVOLUTION_DIR"

# Step 3: Run setup (THIS IS CRITICAL - actually updates Hermes)
echo ""
echo "🔧 Step 3/6: Running setup-hermes.sh (this updates Hermes)..."
cd "$EVOLUTION_DIR"
if [ -f "setup-hermes.sh" ]; then
    bash setup-hermes.sh
    echo "✅ Setup completed"
else
    echo "❌ setup-hermes.sh not found!"
    exit 1
fi

# Step 4: Copy evolution skills TO THE RIGHT PLACE
echo ""
echo "📚 Step 4/6: Installing evolution skills to: $HERMES_SKILLS_DIR"
EVOLUTION_SKILLS="$EVOLUTION_DIR/skills/evolution"

if [ -d "$EVOLUTION_SKILLS" ]; then
    mkdir -p "$HERMES_SKILLS_DIR"
    cp -r "$EVOLUTION_SKILLS" "$HERMES_SKILLS_DIR/"
    echo "✅ Evolution skills installed to: $HERMES_SKILLS_DIR/evolution"
    
    # List installed skills
    echo "📋 Installed evolution skills:"
    ls -1 "$EVOLUTION_SKILLS"/*.md 2>/dev/null | while read file; do
        echo "   - $(basename $file .md)"
    done
else
    echo "❌ Evolution skills not found in repository"
    exit 1
fi

# Step 5: Copy evolution cron jobs TO THE RIGHT PLACE
echo ""
echo "⏰ Step 5/6: Installing evolution cron jobs to: $HERMES_CRON_DIR"
EVOLUTION_CRON="$EVOLUTION_DIR/cron/evolution"

if [ -d "$EVOLUTION_CRON" ]; then
    mkdir -p "$HERMES_CRON_DIR"
    cp -r "$EVOLUTION_CRON" "$HERMES_CRON_DIR/"
    echo "✅ Evolution cron jobs installed to: $HERMES_CRON_DIR/evolution"
    
    # List installed cron jobs
    echo "📋 Installed evolution cron jobs:"
    ls -1 "$EVOLUTION_CRON"/*.yaml 2>/dev/null | while read file; do
        echo "   - $(basename $file .yaml)"
    done
else
    echo "❌ Evolution cron jobs not found in repository"
    exit 1
fi

# Step 6: Verify installation
echo ""
echo "✅ Step 6/6: Verifying installation..."

# Check if hermes command exists
if command -v hermes &> /dev/null; then
    echo "✅ Hermes command available"
    
    # Check evolution skills
    if hermes skills list 2>/dev/null | grep -q "evolution"; then
        echo "✅ Evolution skills installed and available"
    else
        echo "⚠️  Evolution skills installed but not yet visible"
        echo "📋 Try running: hermes skills list"
    fi
else
    echo "❌ Hermes command not found - something went wrong"
    exit 1
fi

echo ""
echo "=========================================="
echo "🎉 Upgrade to Hermes Evolution complete!"
echo ""
echo "📖 What's new:"
echo "  • Evolution skills (research, issues, analysis, implementation)"
echo "  • Evolution cron jobs (daily research, analysis, implementation)"
echo "  • Self-update capabilities"
echo ""
echo "🔗 Next steps:"
echo "  1. Test: hermes --help"
echo "  2. Check skills: hermes skills list"
echo "  3. Read docs: cat $EVOLUTION_DIR/EVOLUTION_README.md"
echo ""
echo "📂 Backup location: ~/.hermes.backup.$BACKUP_DATE"
echo "🔄 Rollback if needed: cp -r ~/.hermes.backup.$BACKUP_DATE ~/.hermes"
echo ""
echo "✨ You're now running Hermes Evolution!"
echo "=========================================="

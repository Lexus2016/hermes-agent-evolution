#!/bin/bash
# Migration Script: Hermes Agent → Hermes Evolution
# This script migrates existing Hermes Agent installation to Hermes Evolution
# without losing any data, skills, configurations, or cron jobs.

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
HERMES_BACKUP_ROOT="${HERMES_BACKUP_ROOT:-$HOME}"
HERMES_EVOLUTION_ROOT="${HERMES_EVOLUTION_ROOT:-$HOME/hermes-agent-evolution}"
FORCE_MIGRATION="${FORCE_MIGRATION:-false}"

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

# Check if Hermes is installed
check_hermes_installation() {
    log_info "Checking for existing Hermes installation..."
    
    if [ -d "$HOME/.hermes" ]; then
        log_success "Found Hermes installation at $HOME/.hermes"
        return 0
    else
        log_warning "No existing Hermes installation found."
        log_info "This appears to be a fresh installation."
        return 1
    fi
}

# Create backup
create_backup() {
    local backup_dir="$HERMES_BACKUP_ROOT/.hermes.backup.$(date +%Y%m%d_%H%M%S)"
    
    print_header "Creating Backup"
    
    if [ -d "$HOME/.hermes" ]; then
        log_info "Creating backup at: $backup_dir"
        
        # Create backup
        cp -r "$HOME/.hermes" "$backup_dir"
        
        # Verify backup
        if [ -d "$backup_dir" ] && [ "$(ls -A $backup_dir)" ]; then
            log_success "Backup created successfully"
            echo "$backup_dir" > "$HOME/.hermes.last_backup"
            return 0
        else
            log_error "Backup creation failed"
            return 1
        fi
    else
        log_info "Nothing to backup (no existing installation)"
        echo "" > "$HOME/.hermes.last_backup"
        return 0
    fi
}

# Verify backup integrity
verify_backup() {
    local backup_dir="$1"
    
    print_header "Verifying Backup"
    
    if [ -z "$backup_dir" ] || [ ! -d "$backup_dir" ]; then
        log_warning "No backup to verify"
        return 0
    fi
    
    log_info "Checking backup integrity..."
    
    # Check critical directories
    local critical_dirs="profiles skills cron"
    for dir in $critical_dirs; do
        if [ -d "$backup_dir/$dir" ]; then
            log_success "✓ $dir/ directory exists"
        else
            log_warning "✗ $dir/ directory missing (might not exist in original)"
        fi
    done
    
    # Check profiles
    if [ -d "$backup_dir/profiles" ]; then
        local profile_count=$(ls -1 "$backup_dir/profiles" 2>/dev/null | wc -l)
        log_success "✓ Found $profile_count profiles"
    fi
    
    log_success "Backup verification complete"
    return 0
}

# Install Hermes Evolution
install_evolution() {
    print_header "Installing Hermes Evolution"
    
    if [ ! -d "$HERMES_EVOLUTION_ROOT" ]; then
        log_error "Hermus Evolution directory not found: $HERMES_EVOLUTION_ROOT"
        log_info "Please clone Hermes Evolution first:"
        log_info "  git clone https://github.com/Lexus2016/hermes-agent-evolution.git"
        return 1
    fi
    
    cd "$HERMES_EVOLUTION_ROOT"
    
    log_info "Running setup script..."
    
    if [ -f "setup-hermes.sh" ]; then
        bash setup-hermes.sh
        log_success "Hermes Evolution installed"
        return 0
    else
        log_error "setup-hermes.sh not found"
        return 1
    fi
}

# Verify migration
verify_migration() {
    print_header "Verifying Migration"
    
    local backup_dir=$(cat "$HOME/.hermes.last_backup" 2>/dev/null)
    
    log_info "Checking that Hermes Evolution is working..."
    
    # Check if hermes command exists
    if command -v hermes &> /dev/null; then
        log_success "✓ hermes command available"
    else
        log_warning "✗ hermes command not found (may need to reload shell)"
    fi
    
    # Check profiles
    if [ -d "$HOME/.hermes/profiles" ]; then
        local profile_count=$(ls -1 "$HOME/.hermes/profiles" 2>/dev/null | wc -l)
        log_success "✓ Found $profile_count profiles"
    else
        log_error "✗ profiles directory missing"
        return 1
    fi
    
    # Compare with backup
    if [ -n "$backup_dir" ] && [ -d "$backup_dir/profiles" ]; then
        local backup_profiles=$(ls -1 "$backup_dir/profiles" 2>/dev/null | wc -l)
        if [ "$profile_count" -ge "$backup_profiles" ]; then
            log_success "✓ All profiles preserved"
        else
            log_warning "✗ Some profiles may be missing"
        fi
    fi
    
    log_success "Migration verification complete"
    return 0
}

# Print summary
print_summary() {
    print_header "Migration Summary"
    
    local backup_dir=$(cat "$HOME/.hermes.last_backup" 2>/dev/null)
    
    echo -e "${GREEN}Migration to Hermes Evolution complete!${NC}"
    echo ""
    echo "Backup location: $backup_dir"
    echo "Hermes Evolution: $HERMES_EVOLUTION_ROOT"
    echo ""
    echo "Next steps:"
    echo "1. Test your installation: hermes --help"
    echo "2. Check profiles: hermes profile list"
    echo "3. Read evolution docs: cat $HERMES_EVOLUTION_ROOT/EVOLUTION_README.md"
    echo ""
    echo "If you encounter any issues, you can restore from backup:"
    echo "  rm -rf ~/.hermes"
    echo "  cp -r $backup_dir ~/.hermes"
    echo ""
}

# Main migration flow
main() {
    print_header "Hermes Agent → Hermes Evolution Migration"
    
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            --force)
                FORCE_MIGRATION=true
                shift
                ;;
            --backup-dir)
                HERMES_BACKUP_ROOT="$2"
                shift 2
                ;;
            --evolution-dir)
                HERMES_EVOLUTION_ROOT="$2"
                shift 2
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Usage: $0 [--force] [--backup-dir DIR] [--evolution-dir DIR]"
                exit 1
                ;;
        esac
    done
    
    # Check if Hermes is installed
    check_hermes_installation
    local has_hermes=$?
    
    # Create backup
    create_backup
    local backup_success=$?
    
    if [ $backup_success -ne 0 ]; then
        log_error "Backup failed. Aborting migration."
        exit 1
    fi
    
    local backup_dir=$(cat "$HOME/.hermes.last_backup" 2>/dev/null)
    
    # Verify backup
    verify_backup "$backup_dir"
    
    # Install Hermes Evolution
    install_evolution
    local install_success=$?
    
    if [ $install_success -ne 0 ]; then
        log_error "Installation failed. You can restore from backup:"
        log_error "  rm -rf ~/.hermes"
        log_error "  cp -r $backup_dir ~/.hermes"
        exit 1
    fi
    
    # Verify migration
    verify_migration
    
    # Print summary
    print_summary
    
    log_success "Migration complete!"
    return 0
}

# Run main
main "$@"

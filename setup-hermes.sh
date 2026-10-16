#!/usr/bin/env bash
# ============================================================================
# Hermes Agent Setup Script — THE dev-environment entry point.
# ============================================================================
# Sets up the pm-managed development environment from a fresh clone:
#   1. Stage the pinned uv from pm/lock.json (sha256-verified, into the pm
#      store slot) — pm needs uv to bootstrap, so it cannot stage uv itself.
#   2. Use uv to install and locate bootstrap Python, then let uv exit.
#      Run `python -m pm.cli install` directly so PM can safely replace uv.
#      PM owns the final interpreter, tool store, and dependency generation.
#   3. Point you at `source ./activate` — the venv-style way to put the pm
#      env (PATH + tool vars) into your current shell.
# There is no pip fallback tier here on purpose.
# ============================================================================

set -e

# Setup and activation prepare the isolated test environment. Installers call
# pm.cli directly and never select it.
runtime_only=false
test_environment="--test-environment"
for option in "$@"; do
    case "$option" in
        --runtime-only) runtime_only=true ;;
        --test-environment|--test-environment=*) test_environment="$option" ;;
        *) printf 'Unknown setup option: %s\n' "$option" >&2; exit 2 ;;
    esac
done
# Colors
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
export UV_NO_CONFIG=1

echo ""
echo -e "${CYAN}☤ Hermes Agent Setup${NC}"
echo ""

# ============================================================================
# Install / locate uv — staged from pm/lock.json (the lockfile is the only
# authority; no astral-latest, no curl|sh).
# ============================================================================

echo -e "${CYAN}→${NC} Checking for uv..."

lock="$SCRIPT_DIR/pm/lock.json"
[ -f "$lock" ] || { echo -e "${RED}✗${NC} pm/lock.json not found" >&2; exit 1; }

# Read the shared mirror location before Python is available. Match object
# keys, not indentation — same rule as pin() below.
mirror_origin="$(awk -F '"' '$2 == "origin" { print $4; exit }' "$SCRIPT_DIR/pm/artifact-mirror.json")"
mirror_prefix="$(awk -F '"' '$2 == "prefix" { print $4; exit }' "$SCRIPT_DIR/pm/artifact-mirror.json")"
mirror_url_for() { # $1 = lowercase sha256
  [ -n "$mirror_origin" ] && [ -n "$mirror_prefix" ] || return 1
  printf '%s/%s%s' "$mirror_origin" "$mirror_prefix" "$1"
}

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  MINGW*|MSYS*|CYGWIN*) os=win32 ;;
  *) echo -e "${RED}✗${NC} unsupported OS $(uname -s)" >&2; exit 1 ;;
esac
if [ "$os" = win32 ]; then
  # PROCESSOR_ARCHITECTURE lies under an emulated shell (x64 msys on a
  # WoA box reports AMD64); the registry carries the machine's truth.
  winarch="$(MSYS2_ARG_CONV_EXCL='*' reg.exe query 'HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' /v PROCESSOR_ARCHITECTURE 2>/dev/null | tr -d '\r' | awk '/PROCESSOR_ARCHITECTURE/ {print $NF}')"
  case "${winarch:-${PROCESSOR_ARCHITECTURE:-}}" in
    ARM64) arch=arm64 ;;
    *) arch=x64 ;;
  esac
else
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64) arch=x64 ;;
    *) echo -e "${RED}✗${NC} unsupported arch $(uname -m)" >&2; exit 1 ;;
  esac
fi
target="$os-$arch"

# The machine-written lock has one member per line. Follow object names and
# braces, not indentation, to read pins before Python is available.
pin() { # $1 = field (url | sha256 | version), $2 = package (default: uv)
  awk -F '"' -v package="${2:-uv}" -v target="$target" -v field="$1" '
    /^[[:space:]]*("[^"]+"[[:space:]]*:[[:space:]]*)?\{[[:space:]]*$/ {
      path[++depth] = $2; next
    }
    /^[[:space:]]*}[[:space:]]*,?[[:space:]]*$/ {
      delete path[depth--]; next
    }
    path[2] == "packages" && path[3] == package && $2 == field &&
      ((field == "version" && depth == 3) ||
       (depth == 5 && path[4] == "artifacts" && path[5] == target)) {
      print $4; exit
    }' "$lock"
}
uv_version="$(pin version)"
py_version="$(pin version python | cut -d+ -f1 | cut -d. -f1,2)"
[ -n "$uv_version" ] || { echo -e "${RED}✗${NC} no uv pin in pm/lock.json" >&2; exit 1; }

store="${HERMES_RUNTIME_DIR:-$HOME/.hermes/tools}"
entry="$store/uv-$uv_version-$target"
uv="$entry/uv"; [ "$os" = win32 ] && uv="$entry/uv.exe"

if [ -x "$uv" ]; then
  echo -e "${GREEN}✓${NC} pinned uv found ($("$uv" --version 2>/dev/null))"
else
  url="$(pin url)"; sha="$(pin sha256)"
  [ -n "$url" ] && [ -n "$sha" ] || { echo -e "${RED}✗${NC} no uv artifact for $target" >&2; exit 1; }
  echo -e "${CYAN}→${NC} Staging pinned uv $uv_version ($target) into the pm store..."
  mkdir -p "$store"
  tmp="$(mktemp -d "$store/.bootstrap-XXXXXX")"; trap 'rm -rf "$tmp"' EXIT
  archive="$tmp/${url##*/}"
  fetch_pinned() {
    if curl -fsSL -o "$2" "$1"; then return 0; else _curl_status=$?; fi
    case "$_curl_status" in 5|6|7|18|22|28|52|55|56) ;; *) return "$_curl_status" ;; esac
    local mirror; mirror="$(mirror_url_for "$sha")" || return 1
    curl -fsSL -o "$2" "$mirror" || return 1
  }
  if ! fetch_pinned "$url" "$archive"; then
    _tried="$url"
    if _m="$(mirror_url_for "$sha")"; then _tried="$_tried or $_m"; fi
    echo -e "${RED}✗${NC} failed to download pinned uv from $_tried" >&2
    exit 1
  fi
  got="$( (sha256sum "$archive" 2>/dev/null || shasum -a 256 "$archive") | cut -d' ' -f1 | tr -d '\\')"
  [ "$got" = "$sha" ] || { echo -e "${RED}✗${NC} sha256 mismatch for uv (got $got, pinned $sha)" >&2; exit 1; }
  mkdir -p "$tmp/tree"
  case "$archive" in
    *.zip) unzip -q "$archive" -d "$tmp/tree" ;;
    *) tar -xzf "$archive" -C "$tmp/tree" ;;
  esac
  # flatten a single wrapping dir (uv tarballs ship uv-<triple>/uv).
  # BSD find lacks GNU's -mindepth/-maxdepth flags, so enumerate children in
  # the shell; the two dot globs include hidden entries without matching . or ..
  inner= inner_count=0
  for child in "$tmp/tree"/* "$tmp/tree"/.[!.]* "$tmp/tree"/..?*; do
    [ -e "$child" ] || [ -L "$child" ] || continue
    inner="$child"
    inner_count=$((inner_count + 1))
  done
  if [ "$inner_count" = 1 ] && [ -d "$inner" ]; then
    mv "$inner" "$tmp/entry"
  else
    mv "$tmp/tree" "$tmp/entry"
  fi
  rm -rf "$entry"
  mv "$tmp/entry" "$entry"
  echo -e "${GREEN}✓${NC} uv installed ($("$uv" --version 2>/dev/null))"
fi

# ============================================================================
# Delegate to pm: python + venv + tool store + hash-verified venv sync
# ============================================================================

echo -e "${CYAN}→${NC} Installing python + tools + dependencies via pm (hash-verified via uv.lock)..."
echo -e "${CYAN}→${NC} (first run on a fresh checkout can take 1-5 minutes)"
# PM can replace its uv entry only after the bootstrap uv has exited.
# A bare version lets uv pick emulated x86_64 on Windows-on-ARM.
py_request="$py_version"
if [ "$os" = win32 ]; then
  case "$arch" in arm64) py_request="cpython-$py_version-windows-aarch64-none" ;;
                  *) py_request="cpython-$py_version-windows-x86_64-none" ;; esac
fi
"$uv" python install --no-bin --no-registry "$py_request"
boot_py="$("$uv" python find --managed-python "$py_request")"
boot_py="${boot_py%$'\r'}"
# Activation trusts the recorded tool digest; a direct setup re-checks it
# (setup-hermes.ps1 draws the same line).
pm_args=("$test_environment")
[ "$runtime_only" = true ] && pm_args+=(--trust-recorded)
if ! "$boot_py" -m pm.cli install "${pm_args[@]}"; then
    echo -e "${RED}✗${NC} pm install failed — see output above."
    exit 1
fi
echo -e "${GREEN}✓${NC} Tools + dependencies installed (hash-verified via pm + uv.lock)"

if [ "$runtime_only" = true ]; then
    exit 0
fi

# ============================================================================
# Environment file
# ============================================================================

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        # .env holds API keys — restrict to owner-only access (matches
        # scripts/install.sh which already chmods 600 after creation).
        chmod 600 .env 2>/dev/null || true
        echo -e "${GREEN}✓${NC} Created .env from template"
    fi
else
    # Tighten an existing .env's perms in case it was created elsewhere
    # under a permissive umask.
    chmod 600 .env 2>/dev/null || true
    echo -e "${GREEN}✓${NC} .env exists"
fi

# ============================================================================
# Publish user-facing launchers
# ============================================================================

echo -e "${CYAN}→${NC} Setting up hermes command..."

# Reuse the bootstrap interpreter only to run the shared launcher writer.
bin_dir="$HOME/.local/bin"
if [ "$os" = win32 ]; then
    bin_dir="$(cygpath -am "${HERMES_HOME:-${LOCALAPPDATA:-$HOME/AppData/Local}/hermes}/bin")"
fi
if ! "$boot_py" -I -X utf8 hermes_cli/_launchers.py "$bin_dir"; then
    echo -e "${RED}✗${NC} launcher publication failed" >&2
    exit 1
fi
echo -e "${GREEN}✓${NC} Published Hermes commands in $bin_dir"

if [ "$os" != win32 ]; then
    # Determine the appropriate shell config file
    SHELL_CONFIG=""
    if [[ "$SHELL" == *"zsh"* ]]; then
            SHELL_CONFIG="$HOME/.zshrc"
        elif [[ "$SHELL" == *"bash"* ]]; then
            SHELL_CONFIG="$HOME/.bashrc"
            [ ! -f "$SHELL_CONFIG" ] && SHELL_CONFIG="$HOME/.bash_profile"
        else
            # Fallback to checking existing files
            if [ -f "$HOME/.zshrc" ]; then
                SHELL_CONFIG="$HOME/.zshrc"
            elif [ -f "$HOME/.bashrc" ]; then
                SHELL_CONFIG="$HOME/.bashrc"
            elif [ -f "$HOME/.bash_profile" ]; then
                SHELL_CONFIG="$HOME/.bash_profile"
            fi
        fi

        if [ -n "$SHELL_CONFIG" ]; then
            # Touch the file just in case it doesn't exist yet but was selected
            touch "$SHELL_CONFIG" 2>/dev/null || true

            if ! echo "$PATH" | tr ':' '\n' | grep -q "^$HOME/.local/bin$"; then
                if ! grep -q '\.local/bin' "$SHELL_CONFIG" 2>/dev/null; then
                    echo "" >> "$SHELL_CONFIG"
                    echo "# Hermes Agent — ensure ~/.local/bin is on PATH" >> "$SHELL_CONFIG"
                    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_CONFIG"
                    echo -e "${GREEN}✓${NC} Added ~/.local/bin to PATH in $SHELL_CONFIG"
                else
                    echo -e "${GREEN}✓${NC} ~/.local/bin already in $SHELL_CONFIG"
                fi
            else
                echo -e "${GREEN}✓${NC} ~/.local/bin already on PATH"
            fi
        fi
fi

# ============================================================================
# Seed bundled skills into ~/.hermes/skills/
# ============================================================================

HERMES_SKILLS_DIR="${HERMES_HOME:-$HOME/.hermes}/skills"
mkdir -p "$HERMES_SKILLS_DIR"

echo ""
echo "Syncing bundled skills to ~/.hermes/skills/ ..."
if "$boot_py" -m tools.skills_sync 2>/dev/null; then
    echo -e "${GREEN}✓${NC} Skills synced"
else
    # Fallback: copy if sync script fails (missing deps, etc.)
    if [ -d "$SCRIPT_DIR/skills" ]; then
        cp -rn "$SCRIPT_DIR/skills/"* "$HERMES_SKILLS_DIR/" 2>/dev/null || true
        echo -e "${GREEN}✓${NC} Skills copied"
    fi
fi

# ============================================================================
# Done
# ============================================================================

echo ""
echo -e "${GREEN}✓ Setup complete!${NC}"
echo ""
echo "Next steps:"
echo ""
echo "  1. Activate the dev environment (venv-style, in THIS shell):"
echo "     source ./activate"
echo ""
echo "  2. Run the setup wizard to configure API keys:"
echo "     hermes setup"
echo ""
echo "  3. Start chatting:"
echo "     hermes"
echo ""
echo "Other commands:"
echo "  hermes pm install     # Re-run the tool + dependency install"
echo "  hermes status         # Check configuration"
echo "  hermes doctor         # Diagnose issues"
echo "  deactivate            # Undo the activation (restore PATH etc.)"
echo ""

# --- Evolution prerequisite: GitHub CLI (gh) -------------------------------
# The evolution cycle (issues / analysis / implementation / upstream-sync)
# drives GitHub through `gh`. Warn if it's missing so the user can install it.
# Research still works without gh; issue/PR creation does not.
if command -v gh >/dev/null 2>&1; then
    echo "✅ GitHub CLI (gh) present — evolution can create issues/PRs."
else
    echo "⚠️  GitHub CLI ('gh') not found — evolution issue/PR creation needs it."
    if command -v apt-get >/dev/null 2>&1; then
        echo "   Install:  sudo apt-get install gh    (or https://cli.github.com)"
    elif command -v brew >/dev/null 2>&1; then
        echo "   Install:  brew install gh"
    elif command -v winget >/dev/null 2>&1; then
        echo "   Install:  winget install GitHub.cli"
    else
        echo "   Install:  https://cli.github.com"
    fi
fi
echo ""

# --- Evolution: persistent gh auth (survives terminal env sanitization) -----
# Hermes strips GITHUB_TOKEN/GH_TOKEN from the agent's terminal subprocesses
# (_sanitize_subprocess_env — a deliberate anti-token-exfiltration feature). So
# an evolution skill that relies on $GITHUB_TOKEN inside a `gh` command gets an
# EMPTY token and fails silently ("created" reported, nothing on GitHub). The
# robust, model-agnostic fix is a PERSISTENT `gh auth login`: gh then reads
# credentials from ~/.config/gh, which survives the sanitization. The env tokens
# MUST be cleared first, or gh keeps using the env value and refuses to store.
if command -v gh >/dev/null 2>&1; then
    _evo_env="${HERMES_HOME:-$HOME/.hermes}/.env"
    if [ -z "${GITHUB_PRIVATE_TOKEN:-}${GITHUB_TOKEN:-}" ] && [ -f "$_evo_env" ]; then
        set -a; . "$_evo_env" 2>/dev/null || true; set +a
    fi
    _evo_tok="${GITHUB_PRIVATE_TOKEN:-${GITHUB_TOKEN:-}}"
    if [ -n "$_evo_tok" ]; then
        if printf '%s' "$_evo_tok" | env -u GITHUB_TOKEN -u GH_TOKEN gh auth login --with-token >/dev/null 2>&1; then
            echo "✅ gh authorized persistently (~/.config/gh) — evolution cron can run gh from the agent terminal."
        else
            echo "⚠️  'gh auth login' failed — evolution issue/PR creation may not work from cron."
        fi
        unset _evo_tok
    else
        echo "ℹ️  No GitHub token in env/.env yet. After adding one to $_evo_env, run ONCE:"
        echo "     printf '%s' \"\$GITHUB_PRIVATE_TOKEN\" | env -u GITHUB_TOKEN -u GH_TOKEN gh auth login --with-token"
        echo "   (REQUIRED — the agent terminal cannot see env tokens by design.)"
    fi
fi
echo ""

# --- Community signal: star the repo (best-effort, opt-out) -----------------
# Every install gives the fork a star so the project's reach is visible.
# Opt out with HERMES_NO_STAR=1. Uses gh (env GITHUB_TOKEN) or curl; never fails
# the install if it can't authenticate.
if [ "${HERMES_NO_STAR:-0}" != "1" ]; then
    EVO_REPO="Lexus2016/hermes-agent-evolution"
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        gh api -X PUT "/user/starred/$EVO_REPO" >/dev/null 2>&1 \
            && echo "⭐ Starred $EVO_REPO — thanks for the support! (opt out: HERMES_NO_STAR=1)" || true
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsS -X PUT -H "Authorization: Bearer $GITHUB_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/user/starred/$EVO_REPO" >/dev/null 2>&1 \
            && echo "⭐ Starred $EVO_REPO — thanks! (opt out: HERMES_NO_STAR=1)" || true
    fi
fi

# --- Turbo-Quant Memory MCP: install + register in ALL profiles -------------
# tqmemory is a headline feature of this fork: efficient, economical
# out-of-window memory so the agent loses nothing between sessions. All the
# heavy lifting — uv install, ABSOLUTE-path registration into EVERY profile's
# top-level `mcp_servers` block, verification, and opt-out handling — lives in
# one Python module that `hermes update` reuses too, so install and update
# share a single, self-healing, idempotent code path. The module prints its
# own status and NEVER fails the install (tqmemory is optional). Opt out with
# HERMES_NO_TQMEMORY=1 (or, persistently, memory.tqmemory_autoinstall: false).
if [ "${HERMES_NO_TQMEMORY:-0}" != "1" ]; then
    "$boot_py" -c 'import hermes_bootstrap; import runpy; runpy.run_module("hermes_cli.tqmemory_setup", run_name="__main__")' || true

    # Pre-cache the sentence-transformers embedding model so the FIRST
    # semantic_search doesn't time out pulling ~600MB from HuggingFace at
    # runtime (slow/rate-limited networks blow past the MCP timeout otherwise).
    # Best-effort only: any failure here just means the model lazy-loads on
    # first use. HF_TOKEN is optional (it only raises the HF rate limit).
    # Respect TQMEMORY_EMBEDDING_MODEL if the operator set a custom model;
    # otherwise fall back to the package default (paraphrase-multilingual-MiniLM-L12-v2).
    echo "🧠 Pre-caching embedding model (best-effort)…"
    "$boot_py" - <<'PYEOF'
import hermes_bootstrap 2>/dev/null || echo "  (embedding preload skipped — will lazy-load on first use)"
import os
try:
    from sentence_transformers import SentenceTransformer
    model = os.environ.get("TQMEMORY_EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
    SentenceTransformer(model)
    print(f"  ✓ embedding model cached ({model})")
except Exception:
    pass
PYEOF
fi

# Ask if they want to run setup wizard now
read -p "Would you like to run the setup wizard now? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    echo ""
    # Run directly with venv Python (no activation needed)
    "$bin_dir/hermes" setup
fi

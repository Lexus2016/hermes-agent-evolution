#!/usr/bin/env bash
# upgrade.sh — switch an EXISTING Hermes Agent install onto Hermes Evolution and
# keep it auto-updating. One command, idempotent, safe to re-run.
#
# It uses the OFFICIAL `hermes update` (no clone hacks, no second copy). It also
# heals the legacy issues seen on real upgrades: stale flat evolution *.md,
# old "local" evolution skills not refreshed by skills_sync, the manifest
# "deleted-respected" quirk on re-seed, and a missing ~/.local/bin symlink.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/upgrade.sh | bash
#   # or:  bash upgrade.sh [--no-auto-update]
#
# Your data in $HERMES_HOME (sessions, memories, config) is never modified; a
# timestamped backup is taken anyway.

set -euo pipefail

FORK_URL="https://github.com/Lexus2016/hermes-agent-evolution.git"
UPSTREAM_URL="https://github.com/nousresearch/hermes-agent.git"
WITH_AUTO_UPDATE=1
WITH_STAR=1
IN_CONTAINER=0
for arg in "$@"; do
    case "$arg" in
        --no-auto-update) WITH_AUTO_UPDATE=0 ;;
        --no-star)        WITH_STAR=0 ;;
        --in-container)   IN_CONTAINER=1 ;;
        *) echo "Unknown option: $arg (use --no-auto-update / --no-star / --in-container)" >&2; exit 2 ;;
    esac
done
DOCKER_INPLACE=0
# Command prefix used to run data-dir writes as the unprivileged `hermes` user
# inside the container (see docker_inplace_upgrade).  Empty everywhere else.
RUN_AS=""

echo "🧬 Hermes Evolution — upgrade existing Hermes onto the fork"
echo "=========================================================="

# 1. Locate the existing install — or do a FRESH install of the fork ---------
# No Hermes yet? Don't make the user install the original first — install OUR
# fork directly (install.sh defaults to this repo), then finish evolution setup.
if ! command -v hermes >/dev/null 2>&1; then
    echo "ℹ️  No Hermes found — installing Hermes Evolution fresh (from this fork)..."
    curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/scripts/install.sh | bash
    hash -r 2>/dev/null || true
    if ! command -v hermes >/dev/null 2>&1; then
        echo "❌ Fresh install finished but 'hermes' isn't on PATH yet."
        echo "   Open a NEW terminal (so PATH refreshes) and re-run this command."
        exit 1
    fi
    echo "✅ Fresh install complete — continuing with evolution setup."
fi
HERMES_BIN="$(readlink -f "$(command -v hermes)" 2>/dev/null || command -v hermes)"
# Newer installs ship a shim script (not a symlink — #21454), so readlink alone
# lands on the shim itself. Resolve the real venv binary it points at.
# Two shim styles exist in the wild:
#   * `exec "<venv>/bin/hermes" "$@"`            — the plain install shim
#   * `REAL=/opt/hermes/.venv/bin/hermes` + a later `exec "$S6_SUID" hermes
#     "$REAL"` — the Docker privilege-drop shim (docker/hermes-exec-shim.sh)
# Matching only `^exec "` picked `$S6_SUID` out of the Docker shim (an
# unexpanded literal), silently left HERMES_BIN as the shim and resolved
# INSTALL_DIR to /opt. Instead: scan every absolute .../bin/hermes path the
# file mentions (comments included) and take the first that is executable and
# is not the shim itself.
case "$HERMES_BIN" in
    */venv/bin/hermes|*/.venv/bin/hermes) : ;;
    *)
        for CAND in $(grep -oE '/[A-Za-z0-9._/-]+/bin/hermes' "$HERMES_BIN" 2>/dev/null); do
            [ "$CAND" = "$HERMES_BIN" ] && continue
            if [ -x "$CAND" ]; then
                HERMES_BIN="$CAND"
                break
            fi
        done
        ;;
esac

# Layout detection, by evidence rather than by path arithmetic ---------------
# `dirname` three times assumes <root>/venv/bin/hermes and silently produces
# nonsense on any other layout (it resolved INSTALL_DIR=/opt on a real user's
# container). Instead:
#   1. find the virtualenv by its canonical marker, pyvenv.cfg;
#   2. ask that interpreter where hermes_cli actually lives — for an editable
#      install that IS the source tree, whatever it is named.
# Path arithmetic stays only as the last-resort fallback.
VENV_DIR=""
BIN_PARENT="$(dirname "$(dirname "$HERMES_BIN")")"
for CAND in "$(dirname "$(dirname "$HERMES_BIN")")" "$BIN_PARENT/.venv" "$BIN_PARENT/venv"; do
    if [ -f "$CAND/pyvenv.cfg" ]; then VENV_DIR="$CAND"; break; fi
done
if [ -z "$VENV_DIR" ]; then
    # One level down from the install root: <root>/<anything>/pyvenv.cfg
    for CAND in "$BIN_PARENT"/*/pyvenv.cfg; do
        [ -f "$CAND" ] && { VENV_DIR="$(dirname "$CAND")"; break; }
    done
fi
PY=""
for CAND in "$VENV_DIR/bin/python" "$VENV_DIR/bin/python3"; do
    [ -n "$VENV_DIR" ] && [ -x "$CAND" ] && { PY="$CAND"; break; }
done
# Last resort: any python that can import hermes_cli.
if [ -z "$PY" ]; then
    for CAND in python3 python; do
        if command -v "$CAND" >/dev/null 2>&1 && "$CAND" -c 'import hermes_cli' >/dev/null 2>&1; then
            PY="$(command -v "$CAND")"
            break
        fi
    done
fi

INSTALL_DIR=""
IS_EDITABLE=0
if [ -n "$PY" ]; then
    INSTALL_DIR="$("$PY" -c 'import hermes_cli, os; print(os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__))))' 2>/dev/null || echo)"
    case "$INSTALL_DIR" in
        # Inside site-packages => a regular (non-editable) install: there is no
        # source tree to swap, so the in-place container path does not apply.
        *"/site-packages"|*"/dist-packages") IS_EDITABLE=0 ;;
        "")                                   : ;;
        *)                                    IS_EDITABLE=1 ;;
    esac
fi
if [ -z "$INSTALL_DIR" ]; then
    INSTALL_DIR="$(dirname "$(dirname "$(dirname "$HERMES_BIN")")")"
fi

hermes_layout_report() {
    echo ""
    echo "🔎 Detected layout (paste this if you need to report the problem):"
    echo "   command -v hermes : $(command -v hermes 2>/dev/null || echo '(not found)')"
    echo "   resolved binary   : $HERMES_BIN"
    echo "   virtualenv        : ${VENV_DIR:-(not found — no pyvenv.cfg near the binary)}"
    echo "   interpreter       : ${PY:-(none could import hermes_cli)}"
    echo "   source tree       : ${INSTALL_DIR:-(unknown)}"
    echo "   editable install  : $IS_EDITABLE"
}

# 1b. Docker installs ------------------------------------------------------
# The published image excludes `.git` from the build context (see
# .dockerignore), so there is no working tree to switch remotes on — exactly
# what `hermes update` itself refuses to do for docker installs.  Detect it
# BEFORE the git check below so the user gets a real remediation instead of a
# misleading "not a git checkout".
INSTALL_METHOD=""
if [ -f "$INSTALL_DIR/.install_method" ]; then
    INSTALL_METHOD="$(tr -d '[:space:]' < "$INSTALL_DIR/.install_method" | tr '[:upper:]' '[:lower:]')"
fi

# In-place upgrade of a RUNNING container, for operators who only have a shell
# inside it (NAS/panel deployments, no host docker, no build daemon).
#
# Why this is safe to do at all: the image installs Hermes EDITABLE
# (`uv pip install --no-deps -e .` in the Dockerfile), so `<tree>/.venv`
# imports straight out of /opt/hermes.  Replacing the source tree IS the
# upgrade — no wheel rebuild, no venv surgery.  Everything the repo does not
# track (.venv/, node_modules/, bin/hermes shim, hermes_cli/web_dist/) is
# untracked and survives `git checkout -f` untouched.
docker_inplace_upgrade() {
    if [ -z "$VENV_DIR" ] || [ -z "$PY" ]; then
        echo "❌ Could not locate the virtualenv this Hermes runs from."
        hermes_layout_report
        echo ""
        echo "   Nothing was changed. Please report the block above."
        exit 1
    fi
    if [ "$IS_EDITABLE" != "1" ]; then
        # Regular install: the code is in site-packages, so there is no source
        # tree to swap. Hand off to the reinstall path, which turns this into an
        # editable checkout of the fork inside the same virtualenv.
        container_reinstall_from_fork
        return 0
    fi

    echo "🐳 In-container upgrade — swapping the source tree in place"
    echo "   Install tree: $INSTALL_DIR"
    echo "   Virtualenv:   $VENV_DIR"
    echo ""
    echo "⚠️  This writes into the container's WRITABLE LAYER."
    echo "    'docker restart' keeps it. Recreating the container from the image"
    echo "    (compose up --force-recreate, image pull) reverts to the original —"
    echo "    that is also your rollback. Re-run this command to reapply."
    echo ""

    if [ "$(id -u)" != "0" ]; then
        echo "❌ Needs root inside the container (the code tree is root-owned)."
        echo "   Re-run with: docker exec -u 0 -it <container> bash"
        exit 1
    fi
    if ! command -v git >/dev/null 2>&1; then
        echo "❌ git is not available in this image — cannot fetch the fork."
        echo "   Install it first (apt-get update && apt-get install -y git) and re-run."
        exit 1
    fi
    if [ ! -w "$INSTALL_DIR" ]; then
        echo "❌ $INSTALL_DIR is not writable (read-only rootfs?) — cannot upgrade in place."
        exit 1
    fi
    if [ ! -d "$VENV_DIR" ]; then
        echo "❌ No virtualenv at $VENV_DIR — unexpected image layout, aborting."
        exit 1
    fi

    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "ℹ️  Already a git checkout (re-run) — fetching the latest fork commit."
    else
        echo "📦 Converting $INSTALL_DIR into a git checkout of the fork..."
        git -C "$INSTALL_DIR" init -q
        # The image bakes permissions with --chmod, so mode-only diffs would
        # otherwise show up as spurious modifications on every later fetch.
        git -C "$INSTALL_DIR" config core.fileMode false
    fi
    git -C "$INSTALL_DIR" remote remove origin >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" remote add origin "$FORK_URL"
    git -C "$INSTALL_DIR" remote remove upstream >/dev/null 2>&1 || true
    git -C "$INSTALL_DIR" remote add upstream "$UPSTREAM_URL"

    # New files must stay readable for the UID-10000 gateway. Inheriting a
    # restrictive umask (027/077 is common on NAS/panel shells) would create
    # root-only files and the gateway would hit EACCES on its own code.
    umask 022

    # The container reads two things out of this tree AT BOOT, not just at
    # runtime: ENTRYPOINT is `/init /opt/hermes/docker/main-wrapper.sh`, and
    # the cont-init hook execs docker/stage2-hook.sh. If a fork commit changes
    # them in a way the older s6 layer in /etc/s6-overlay doesn't expect, the
    # RUNNING container stays fine but the NEXT boot can fail. Snapshot them
    # and diff after the swap so that risk is visible instead of latent.
    BOOT_BAK="$INSTALL_DIR/.preupgrade-boot"
    rm -rf "$BOOT_BAK" 2>/dev/null || true
    mkdir -p "$BOOT_BAK"
    cp -r "$INSTALL_DIR/docker" "$BOOT_BAK/docker" 2>/dev/null || true
    LOCK_BEFORE="$(cksum "$INSTALL_DIR/uv.lock" 2>/dev/null | awk '{print $1}' || echo none)"

    echo "⬇️  Fetching the fork (shallow)..."
    # Network first, working-tree mutation second: a failed fetch leaves the
    # container completely untouched.
    git -C "$INSTALL_DIR" fetch --depth=1 origin main
    echo "🔀 Checking the fork out over the image's sources..."
    git -C "$INSTALL_DIR" checkout -f -B main FETCH_HEAD
    git -C "$INSTALL_DIR" branch --set-upstream-to=origin/main main >/dev/null 2>&1 || true
    # Note: files the old image had and the fork does not are left in place as
    # untracked. `git clean` is deliberately NOT run — it would also delete
    # things the repo never tracked but the container needs (the bin/hermes
    # shim, .playwright browsers, the build stamp).

    # Boot-contract drift (see BOOT_BAK above).
    if command -v diff >/dev/null 2>&1 && [ -d "$BOOT_BAK/docker" ]; then
        if ! diff -rq "$BOOT_BAK/docker" "$INSTALL_DIR/docker" >/dev/null 2>&1; then
            echo ""
            echo "⚠️  The fork changes files under docker/ that this image executes at BOOT"
            echo "    (main-wrapper.sh / stage2-hook.sh / s6-rc.d). The running container is"
            echo "    unaffected, but the next container start uses the NEW ones against the"
            echo "    OLD /etc/s6-overlay layer. Review before you restart the container:"
            echo "      diff -r $BOOT_BAK/docker $INSTALL_DIR/docker"
            echo "    Originals are kept in $BOOT_BAK for a manual restore."
            echo ""
        fi
    fi

    # Dependency drift: --no-deps below deliberately never touches the image's
    # sealed dependency set, so a fork that added or bumped a package would
    # otherwise fail later at import time.
    LOCK_AFTER="$(cksum "$INSTALL_DIR/uv.lock" 2>/dev/null | awk '{print $1}' || echo none)"
    if [ "$LOCK_BEFORE" != "$LOCK_AFTER" ]; then
        echo "⚠️  uv.lock differs from the image's — the fork may need packages this"
        echo "    virtualenv doesn't have. The smoke test below catches the obvious"
        echo "    breakage; an optional adapter (Matrix, Telegram, …) can still fail"
        echo "    later with ModuleNotFoundError. Fix with:"
        echo "      cd $INSTALL_DIR && VIRTUAL_ENV=$VENV_DIR uv sync --frozen --extra all"
    fi

    # Entry points and package metadata can move between versions — refresh the
    # editable link.  --no-deps keeps it to a metadata rewrite: no resolution,
    # no network, no chance of disturbing the image's sealed dependency set.
    echo "🔗 Refreshing the editable install..."
    if command -v uv >/dev/null 2>&1; then
        (cd "$INSTALL_DIR" && VIRTUAL_ENV="$VENV_DIR" uv pip install --no-cache-dir --no-deps -e . >/dev/null 2>&1) \
            || echo "⚠️  Editable refresh failed — the source swap itself still applies."
    else
        "$VENV_DIR/bin/python" -m pip install --no-deps -e "$INSTALL_DIR" >/dev/null 2>&1 \
            || echo "⚠️  Editable refresh skipped (no uv/pip in the image)."
    fi

    # A fork commit may pull in a dependency the sealed image venv doesn't
    # carry.  Surface that here, not at the next gateway restart.
    if "$VENV_DIR/bin/hermes" --version >/dev/null 2>&1; then
        echo "✅ Now on the fork: $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
    else
        echo ""
        echo "⚠️  '$VENV_DIR/bin/hermes --version' fails after the swap — most likely a"
        echo "    dependency added by the fork is missing from the image's virtualenv:"
        echo "      cd $INSTALL_DIR && VIRTUAL_ENV=$VENV_DIR uv sync --frozen --extra all"
        echo "    Or roll back by recreating the container from its image."
    fi

    # Writes below land in \$HERMES_HOME, which the supervised gateway reads as
    # UID 10000. Doing them as root would leave root-owned files the gateway
    # can't read — the exact failure docker/hermes-exec-shim.sh exists to
    # prevent. Drop privileges the same way the shim does.
    if [ -x /command/s6-setuidgid ] && id hermes >/dev/null 2>&1; then
        RUN_AS="/command/s6-setuidgid hermes"
    fi
    DOCKER_INPLACE=1
}

# Some containers ship Hermes as a REGULAR (non-editable) install: the code
# sits in <venv>/lib/pythonX.Y/site-packages and there is no source tree to
# swap. Convert that into a git checkout of the fork installed EDITABLE into
# the same virtualenv — the package is replaced, the dependency set is left
# alone (--no-deps), and every later upgrade becomes a plain `git pull`.
container_reinstall_from_fork() {
    SRC_DIR="${HERMES_HOME:-/opt}/hermes-evolution"
    echo "🐳 In-container upgrade — installing the fork into $VENV_DIR"
    echo "   Virtualenv:  $VENV_DIR"
    echo "   Fork source: $SRC_DIR"
    echo ""
    echo "⚠️  This writes into the container's writable layer (and \$HERMES_HOME if"
    echo "    that is where the source lands). Recreating the container from the"
    echo "    image reverts the install — that is also your rollback."
    echo ""

    if [ "$(id -u)" != "0" ] && [ ! -w "$VENV_DIR" ]; then
        echo "❌ $VENV_DIR is not writable. Re-run as root: docker exec -u 0 -it <container> bash"
        exit 1
    fi
    if ! command -v git >/dev/null 2>&1; then
        echo "❌ git is not available in this image — cannot fetch the fork."
        echo "   Install it first: apt-get update && apt-get install -y git"
        exit 1
    fi

    # Pick an installer. uv first (no bootstrap needed), then pip, then the two
    # standard ways of getting pip into a venv that was built without it.
    INSTALLER=""
    if command -v uv >/dev/null 2>&1; then
        INSTALLER="uv"
    elif "$PY" -m pip --version >/dev/null 2>&1; then
        INSTALLER="pip"
    else
        echo "🔧 No pip in this virtualenv — bootstrapping one..."
        if "$PY" -m ensurepip --upgrade >/dev/null 2>&1 && "$PY" -m pip --version >/dev/null 2>&1; then
            INSTALLER="pip"
        elif curl -fsSL https://bootstrap.pypa.io/get-pip.py 2>/dev/null | "$PY" - >/dev/null 2>&1 \
             && "$PY" -m pip --version >/dev/null 2>&1; then
            INSTALLER="pip"
        fi
    fi
    if [ -z "$INSTALLER" ]; then
        echo "❌ No way to install packages into $VENV_DIR (no uv, no pip, and"
        echo "   bootstrapping pip failed)."
        hermes_layout_report
        echo ""
        echo "   Install uv (curl -LsSf https://astral.sh/uv/install.sh | sh) and re-run."
        echo "   Nothing was changed."
        exit 1
    fi
    echo "🔧 Installer: $INSTALLER"

    # Fetch the fork. Network first — a failure here leaves the install intact.
    if [ -d "$SRC_DIR/.git" ]; then
        echo "⬇️  Updating the existing fork checkout at $SRC_DIR..."
        git -C "$SRC_DIR" remote set-url origin "$FORK_URL"
        git -C "$SRC_DIR" fetch --depth=1 origin main
        git -C "$SRC_DIR" checkout -f -B main FETCH_HEAD
    else
        echo "⬇️  Cloning the fork into $SRC_DIR..."
        rm -rf "$SRC_DIR"
        git clone --depth=1 "$FORK_URL" "$SRC_DIR"
    fi
    git -C "$SRC_DIR" remote get-url upstream >/dev/null 2>&1 \
        || git -C "$SRC_DIR" remote add upstream "$UPSTREAM_URL"

    # An editable install UNINSTALLS the existing hermes-agent first, so a
    # failure partway through (build backend unreachable, disk full) would
    # leave the container with no Hermes at all. Record the current version so
    # that case can be put back.
    OLD_VER="$("$PY" -c 'import importlib.metadata as m; print(m.version("hermes-agent"))' 2>/dev/null || echo)"

    # --no-deps on purpose: the container's dependency set already satisfies
    # upstream Hermes, and resolving the full tree here could pull compilers
    # and native builds the image has no toolchain for. Drift is reported below.
    echo "📦 Installing the fork editable into the virtualenv (--no-deps)..."
    INSTALL_OK=0
    if [ "$INSTALLER" = "uv" ]; then
        uv pip install --python "$PY" --no-deps -e "$SRC_DIR" && INSTALL_OK=1
    else
        "$PY" -m pip install --no-deps -e "$SRC_DIR" && INSTALL_OK=1
    fi
    if [ "$INSTALL_OK" != "1" ]; then
        echo "❌ Installing the fork failed."
        if [ -n "$OLD_VER" ] && ! "$PY" -c 'import hermes_cli' >/dev/null 2>&1; then
            echo "↩️  hermes_cli no longer imports — restoring the original hermes-agent $OLD_VER..."
            if [ "$INSTALLER" = "uv" ]; then
                uv pip install --python "$PY" "hermes-agent==$OLD_VER" >/dev/null 2>&1 && echo "✅ Original restored."
            else
                "$PY" -m pip install "hermes-agent==$OLD_VER" >/dev/null 2>&1 && echo "✅ Original restored."
            fi
            "$PY" -c 'import hermes_cli' >/dev/null 2>&1 \
                || echo "⚠️  Restore failed too — recreate the container from its image to recover."
        else
            echo "   The previous install still imports; nothing further was changed."
        fi
        exit 1
    fi

    # Prove the swap took, and that the CLI still starts.
    NEW_SRC="$("$PY" -c 'import hermes_cli, os; print(os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__))))' 2>/dev/null || echo)"
    if [ "$NEW_SRC" != "$SRC_DIR" ]; then
        echo "⚠️  hermes_cli still resolves to '$NEW_SRC', not '$SRC_DIR'."
        echo "    An older copy in site-packages may be shadowing the new one:"
        echo "      $PY -m pip uninstall -y hermes-agent   # then re-run this command"
    fi
    if "$VENV_DIR/bin/hermes" --version >/dev/null 2>&1; then
        echo "✅ Now on the fork: $(git -C "$SRC_DIR" rev-parse --short HEAD)"
    else
        echo ""
        echo "⚠️  '$VENV_DIR/bin/hermes --version' fails — the fork likely needs a"
        echo "    package this virtualenv doesn't have. Install the missing deps:"
        if [ "$INSTALLER" = "uv" ]; then
            echo "      uv pip install --python $PY -e $SRC_DIR"
        else
            echo "      $PY -m pip install -e $SRC_DIR"
        fi
        echo "    (same command without --no-deps). Roll back by recreating the container."
    fi

    # From here the rest of the script operates on the fork checkout.
    INSTALL_DIR="$SRC_DIR"
    if [ -x /command/s6-setuidgid ] && id hermes >/dev/null 2>&1; then
        RUN_AS="/command/s6-setuidgid hermes"
    fi
    DOCKER_INPLACE=1
}

if [ "$IN_CONTAINER" = "1" ] && { [ "$INSTALL_METHOD" = "docker" ] || [ -f /.dockerenv ]; }; then
    # Explicitly asked for the container path: take it whether or not the tree
    # already has a .git. A second run must not fall through to `hermes update`,
    # which refuses on anything stamped docker and would abort under `set -e`.
    docker_inplace_upgrade
elif [ "$INSTALL_METHOD" = "docker" ] || { [ -f /.dockerenv ] && [ ! -d "$INSTALL_DIR/.git" ]; }; then
    cat <<EOF
❌ This is a Docker install — it has no git working tree to switch remotes on
   (.git is excluded from the image build context).

Two ways forward:

  A. Rebuild the image on the HOST — durable, survives container recreate:

       git clone $FORK_URL
       cd hermes-agent-evolution
       docker build -t hermes-agent-evolution:latest .
       # point your compose file / run command at that image, then:
       docker compose up -d --force-recreate

  B. Upgrade THIS RUNNING container in place — no host access, no image build:

       curl -fsSL https://raw.githubusercontent.com/Lexus2016/hermes-agent-evolution/main/upgrade.sh | bash -s -- --in-container

     The image installs Hermes as an editable checkout, so swapping the source
     tree takes effect immediately. Run it as root inside the container
     (docker exec -u 0). The change lives in the container's writable layer:
     'docker restart' keeps it, recreating the container from the image reverts
     it — re-run the same command to reapply.

Your data is safe either way: config, sessions and memories live on the
\$HERMES_HOME volume (/opt/data in the container) and no path here touches it.
EOF
    exit 1
fi

if [ ! -d "$INSTALL_DIR/.git" ]; then
    # Last resort: the default location install.sh uses.
    FALLBACK="${HERMES_HOME:-$HOME/.hermes}/hermes-agent"
    if [ -d "$FALLBACK/.git" ] && { [ -x "$FALLBACK/venv/bin/hermes" ] || [ -x "$FALLBACK/.venv/bin/hermes" ]; }; then
        INSTALL_DIR="$FALLBACK"
        if [ -x "$FALLBACK/venv/bin/hermes" ]; then
            HERMES_BIN="$FALLBACK/venv/bin/hermes"
        else
            HERMES_BIN="$FALLBACK/.venv/bin/hermes"
        fi
    else
        echo "❌ $INSTALL_DIR is not a git checkout — cannot switch remotes."
        hermes_layout_report
        exit 1
    fi
fi
# VENV_DIR/PY were resolved by evidence above (pyvenv.cfg + import location).
# Only fill them in by name if that detection came up empty — e.g. a checkout
# whose venv has not been created yet.
if [ -z "$VENV_DIR" ]; then
    VENV_DIR="$INSTALL_DIR/venv"
    [ -d "$VENV_DIR" ] || VENV_DIR="$INSTALL_DIR/.venv"
fi
[ -n "$PY" ] || PY="$VENV_DIR/bin/python"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
echo "📂 Install dir: $INSTALL_DIR"
echo "📂 Data dir:    $HOME_DIR"

# 2. Backup the data dir (best-effort, keep only the 3 newest to avoid disk
#    bloat when the script is re-run).
if [ -d "$HOME_DIR" ]; then
    BACKUP="$HOME_DIR.backup.$(date +%Y%m%d_%H%M%S)"
    # Non-fatal: a full disk (or a big session history on a small container
    # layer) must not abort the upgrade under `set -e`.
    # The fork checkout may live inside $HERMES_HOME so it survives container
    # recreates — copying it (and older backups) on every run would bloat the
    # volume, so exclude both. tar is present in every image that has a shell;
    # cp is the fallback.
    if command -v tar >/dev/null 2>&1 &&
       (cd "$HOME_DIR" && tar --exclude=./hermes-evolution --exclude='./*.backup.*' -cf - .) 2>/dev/null |
       (mkdir -p "$BACKUP" && cd "$BACKUP" && tar -xf -) 2>/dev/null; then
        echo "✅ Backup: $BACKUP"
    elif cp -r "$HOME_DIR" "$BACKUP" 2>/dev/null; then
        echo "✅ Backup: $BACKUP"
    else
        rm -rf "$BACKUP" 2>/dev/null || true
        echo "⚠️  Could not back up $HOME_DIR (out of space?) — continuing; the"
        echo "    upgrade below does not write into it destructively."
    fi
    ls -dt "$HOME_DIR".backup.* 2>/dev/null | tail -n +4 | while read -r old; do
        rm -rf "$old" && echo "   pruned old backup: $old"
    done
fi

# 3. Point origin at the fork, keep upstream at the original ---------------
if [ "$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || echo)" = "$FORK_URL" ]; then
    echo "ℹ️  Already on the Hermes Evolution fork (re-run) — proceeding safely."
fi
git -C "$INSTALL_DIR" remote set-url origin "$FORK_URL"
if ! git -C "$INSTALL_DIR" remote get-url upstream >/dev/null 2>&1; then
    git -C "$INSTALL_DIR" remote add upstream "$UPSTREAM_URL"
fi
echo "✅ origin → fork, upstream → original"

# 4. Remove legacy flat evolution *.md so `hermes update` won't autostash ---
rm -f "$INSTALL_DIR"/skills/evolution/analysis.md \
      "$INSTALL_DIR"/skills/evolution/implementation.md \
      "$INSTALL_DIR"/skills/evolution/issues.md \
      "$INSTALL_DIR"/skills/evolution/research.md \
      "$INSTALL_DIR"/skills/evolution/upstream-sync.md 2>/dev/null || true

# 5. Update onto the fork (official, fork-aware, with rollback) -------------
CODE_CHANGED=0
if [ "$DOCKER_INPLACE" = "1" ]; then
    # The in-place path already put the fork's sources on disk. `hermes update`
    # deliberately refuses to run on a docker install (it would exit 1 and abort
    # this script under `set -e`), so skip it here.
    CODE_CHANGED=1
    echo ""
    echo "ℹ️  Skipping 'hermes update' — the in-place checkout above already did it."
else
    echo ""
    echo "🔄 Running hermes update (pulls the fork; preserves evolution)..."
    PRE_HEAD="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo none)"
    hermes update --yes
    POST_HEAD="$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null || echo none)"
    if [ "$PRE_HEAD" != "$POST_HEAD" ]; then
        CODE_CHANGED=1
    else
        echo "ℹ️  No code change — already current (re-run is harmless)."
    fi
fi

# 5b. Ensure Turbo-Quant Memory (tqmemory) on the documented install path ----
# `hermes update` reconciles tqmemory internally (install if missing + register
# in every profile), but on a FRESH install the checkout install.sh just cloned
# is already current, so the update above is a no-op and may skip that internal
# reconcile. Run it explicitly here when nothing changed, so brand-new installs
# still get the memory installed + registered. Idempotent + non-fatal; honours
# HERMES_NO_TQMEMORY=1 (and the persistent memory.tqmemory_autoinstall flag).
if [ "$CODE_CHANGED" = "0" ] && [ "${HERMES_NO_TQMEMORY:-0}" != "1" ]; then
    echo ""
    echo "🧠 Ensuring Turbo-Quant Memory (tqmemory)..."
    HERMES_HOME="$HOME_DIR" $RUN_AS "$PY" -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); from hermes_cli.tqmemory_setup import reconcile_tqmemory; reconcile_tqmemory()" 2>&1 | tail -2 \
        || echo "ℹ️  tqmemory setup skipped (optional)."
fi

# 6. Force-fresh the evolution skills (heals legacy 'local' copies) ---------
# Drop evolution entries from the bundled manifest, remove the dir, re-seed —
# otherwise skills_sync may keep stale 'local' copies or skip re-adding ones
# it thinks the user deleted.
echo ""
echo "🧩 Refreshing evolution skills from the fork..."
MAN="$HOME_DIR/skills/.bundled_manifest"
if [ -f "$MAN" ]; then
    grep -v "^evolution-" "$MAN" > "$MAN.tmp" 2>/dev/null && mv "$MAN.tmp" "$MAN" || true
fi
rm -rf "$HOME_DIR/skills/evolution"
HERMES_HOME="$HOME_DIR" $RUN_AS "$PY" -c "import sys; sys.path.insert(0,'$INSTALL_DIR'); from tools.skills_sync import sync_skills; sync_skills()" >/dev/null 2>&1 \
    || echo "⚠️  skills re-seed reported issues (check: hermes skills list)"

# 7. Register evolution cron jobs into the native scheduler -----------------
echo "⏰ Registering evolution cron jobs..."
HERMES_HOME="$HOME_DIR" $RUN_AS "$PY" "$INSTALL_DIR/scripts/register_evolution_cron.py" 2>&1 | tail -1 \
    || echo "⚠️  cron registration reported issues"

# 8. Schedule the daily self-update ---------------------------------------
if [ "$WITH_AUTO_UPDATE" = "1" ] && [ "$DOCKER_INPLACE" = "1" ]; then
    # install_auto_update.sh schedules `hermes update` via crontab, and the
    # published image ships neither cron nor a writable-by-hermes code tree —
    # `hermes update` refuses on a docker install anyway. Re-running this
    # command is the update path for an in-place container.
    echo "ℹ️  Daily auto-update not scheduled (no cron in the container image)."
    echo "    To update later, re-run the same one-liner with --in-container."
elif [ "$WITH_AUTO_UPDATE" = "1" ]; then
    echo "🔁 Scheduling daily self-update..."
    bash "$INSTALL_DIR/scripts/install_auto_update.sh" >/dev/null 2>&1 \
        && echo "✅ Daily 'hermes update' scheduled" \
        || echo "⚠️  Could not schedule auto-update (run scripts/install_auto_update.sh manually)"
fi

# 9. Heal the command symlink; restart gateway ONLY if code actually changed
#    (avoids needlessly interrupting a running gateway on a no-op re-run).
hermes doctor --fix >/dev/null 2>&1 || true
if [ "$DOCKER_INPLACE" = "1" ]; then
    # s6 supervises the gateway; bounce the service rather than spawning a
    # second one. `s6-svc -r` only QUEUES the restart, so confirm the service
    # actually came back up — a fork commit that breaks startup must not look
    # like a successful upgrade.
    if [ -x /command/s6-svc ] && [ -d /run/service/main-hermes ]; then
        if /command/s6-svc -r /run/service/main-hermes 2>/dev/null; then
            sleep 5
            if [ -x /command/s6-svstat ] && /command/s6-svstat /run/service/main-hermes 2>/dev/null | grep -q '^up'; then
                echo "✅ Gateway service restarted and running"
            else
                echo "⚠️  The gateway did NOT come back up after the restart."
                echo "    Inspect: s6-svstat /run/service/main-hermes"
                echo "    Roll back by recreating the container from its image."
            fi
        else
            echo "⚠️  Could not bounce the s6 service — run: docker restart <container>"
        fi
    else
        echo "ℹ️  Restart the container to load the new code: docker restart <container>"
    fi
elif [ "$CODE_CHANGED" = "1" ] && hermes gateway status >/dev/null 2>&1; then
    # When no systemd/launchd service is installed (macOS default), `hermes
    # gateway restart` falls back to running the gateway in the FOREGROUND —
    # it never returns and would block this script forever. Detach it: with a
    # service manager the command exits quickly anyway; without one the
    # detached process simply becomes the new gateway.
    (nohup hermes gateway restart >/dev/null 2>&1 &)
    echo "✅ Gateway restart initiated (picked up new code)"
elif [ "$CODE_CHANGED" = "0" ]; then
    echo "ℹ️  No code change — gateway left running (not restarted)."
fi

# 9b. Star the repo (community signal) — transparent + opt-out (--no-star).
#     Best-effort: needs a token/login with user scope; silently skips otherwise.
if [ "$WITH_STAR" = "1" ]; then
    if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
        gh api -X PUT /user/starred/Lexus2016/hermes-agent-evolution >/dev/null 2>&1 \
            && echo "⭐ Starred Lexus2016/hermes-agent-evolution — thanks for the support! (skip with --no-star)" || true
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
        curl -fsS -X PUT -H "Authorization: token $GITHUB_TOKEN" -H "Content-Length: 0" \
            https://api.github.com/user/starred/Lexus2016/hermes-agent-evolution >/dev/null 2>&1 \
            && echo "⭐ Starred the repo — thanks! (skip with --no-star)" || true
    fi
fi

# 10. Verify ---------------------------------------------------------------
echo ""
echo "=========================================================="
echo "✅ Done. Verifying:"
EVO="$(hermes skills list 2>/dev/null | grep -i evolution || true)"
if [ -n "$EVO" ]; then
    echo "$EVO" | sed 's/^/   /'
    echo "✅ Evolution skills active."
else
    echo "⚠️  Evolution skills not visible — run: hermes skills list | grep evolution"
fi
echo ""
echo "Next (optional): set GITHUB_TOKEN in $HOME_DIR/.env for research/issues jobs."
if [ "$DOCKER_INPLACE" = "1" ]; then
    echo "Your container now runs Hermes Evolution. 🧬🚀"
    echo ""
    echo "Two things to remember about an in-place container upgrade:"
    echo "  • It lives in the container's writable layer. 'docker restart' keeps it;"
    echo "    recreating the container from the image reverts to stock Hermes."
    echo "  • There is no daily self-update here — re-run the same command with"
    echo "    --in-container whenever you want the latest evolution commit."
else
    echo "Your Hermes now runs Hermes Evolution and self-updates daily. 🧬🚀"
fi

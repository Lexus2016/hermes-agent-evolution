# nix/devShell.nix — Dev shell that delegates setup to each package
#
# Each npm workspace package exposes passthru.packageJsonPath (e.g.
# "ui-tui/package.json").  This file collects them all and passes the
# list to mkNpmDevShellHook, which stamps all package.jsons at once,
# then runs a single `npm i --package-lock-only` if any changed and
# `npm ci` if the lockfile changed.
{ ... }:
{
  perSystem =
    { pkgs, self', ... }:
    let
      packages = builtins.attrValues self'.packages;
      hermesNpmLib = self'.packages.default.passthru.hermesNpmLib;

      # Collect all packageJsonPath values from npm workspace packages.
      npmPackageJsonPaths = builtins.filter (p: p != null) (
        map (p: p.passthru.packageJsonPath or null) packages
      );

      hermesAgentDevShellHook = self'.packages.default.passthru.devShellHook;
    in
    {
      devShells.default = pkgs.mkShell {
        packages = with pkgs; [
          (pkgs.runCommand "hermes" { } ''
            mkdir -p $out/bin
            install -Dm755 ${../hermes} $out/bin/hermes
          '')
          uv
        ]
        # The Wayland E2E capture stack AND the bwrap sandbox are Linux-only.
        # `cage`/`grim` and `bubblewrap`/`slirp4netns` all carry
        # `meta.platforms = [ ... -linux ]`, so merely EVALUATING them on Darwin
        # aborts with "Refusing to evaluate package" (observed on this fork's
        # own `nix flake check` macos job, 2026-08-19). Upstream never notices:
        # `nix flake check` on macOS is this fork's own workflow (and upstream
        # has no such job). nix/hermes-agent.nix already guards its own `cage`
        # the same way.
        ++ lib.optionals stdenv.isLinux [
          # Headless Wayland compositor for E2E tests (test:e2e:visual).
          # cage renders a single client with no window management, so
          # the Electron window opens at a fixed size without tiling.
          # libglvnd provides libEGL.so.1 that cage needs on NixOS.
          cage
          libglvnd
          # Graphical terminal + Wayland screenshot client for CLI/TUI UI
          # evidence. `cage -- ghostty ...` keeps captures off the user's
          # live compositor; grim runs inside that isolated client session.
          ghostty
          grim
          # The `sandbox` script wraps `bwrap` (bubblewrap), so it only makes
          # sense on Linux — and forcing its evaluation on Darwin pulls
          # bubblewrap's derivation into the devShell closure and aborts
          # `nix flake check`.
          self'.packages.sandbox
        ]
        ++ self'.packages.default.passthru.devDeps;
        shellHook = ''
          ${hermesAgentDevShellHook}
          ${hermesNpmLib.mkNpmDevShellHook npmPackageJsonPaths}

          # Force Node to use Nix's playwright-test binary instead of node_modules/.bin
          export PATH="${pkgs.playwright-test}/bin:$PATH"

          # for the devshell to pick up the src
          export HERMES_PYTHON_SRC_ROOT=$(git rev-parse --show-toplevel)

          # Let `uv run --active --no-sync` reuse Nix's provisioned Python
          # environment instead of creating an empty project .venv.
          export VIRTUAL_ENV="$(dirname "$(dirname "$(readlink -f "$(command -v python)")")")"

          echo "Hermes Agent dev shell in $HERMES_PYTHON_SRC_ROOT"
          echo "Ready. Run 'hermes' or 'sandbox hermes' to start."
        '';
      };
    };
}

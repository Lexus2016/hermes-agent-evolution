# nix/packages.nix — Hermes Agent package built with uv2nix
{ inputs, ... }:
{
  perSystem =
    {
      pkgs,
      lib,
      inputs',
      ...
    }:
    let

      sandbox = pkgs.callPackage ./sandbox.nix { };

      minimal = pkgs.callPackage ./hermes-agent.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
        npm-lockfile-fix = inputs'.npm-lockfile-fix.packages.default;
        # Only embed clean revs — dirtyRev doesn't represent any upstream
        # commit, so comparing it would always claim "update available".
        rev = inputs.self.rev or null;
      };

      # All platform-portable optional integrations pre-built.
      full = minimal.override {
        extraDependencyGroups = [
          "anthropic"
          "azure-identity"
          "bedrock"
          "daytona"
          "dingtalk"
          "edge-tts"
          "exa"
          "fal"
          "feishu"
          "firecrawl"
          "honcho"
          "messaging"
          "modal"
          "parallel-web"
          "tts-premium"
          "vercel"
          "voice"
        ]
        # matrix is Linux-only (oqs/liboqs lacks aarch64-darwin wheels).
        ++ lib.optionals pkgs.stdenv.isLinux [ "matrix" ];
      };
    in
    {
      packages = {
        node-gyp =
          (pkgs.callPackage ./lib.nix {
            inherit (pkgs) npm-lockfile-fix;
          }).node-gyp;
        default = full;

        inherit minimal;

        # Ships discord.py + python-telegram-bot + slack-sdk so a plain
        # `nix profile install .#messaging` connects to Discord/Telegram/Slack
        # on first run — lazy-install can't write to the read-only /nix/store.
        messaging = minimal.override {
          extraDependencyGroups = [ "messaging" ];
        };

        tui = full.hermesTui;
        web = full.hermesWeb;
        desktop = full.hermesDesktop;

        update-npm-lockfile = full.hermesNpmLib.updateNpmLockfile;

        # CI diagnostic/fix: nix.yml and nix-lockfile-fix.yml call
        # `nix run .#fix-lockfiles -- --check|--apply`.  Without this
        # attribute the workflow's hash_check step crashes before emitting
        # stale=true|false and the "crashed without reporting" gate fires on
        # every PR (repo-wide red).  The script always reports a status.
        fix-lockfiles = full.hermesNpmLib.fixLockfiles;
      }
      # The dev sandbox is Linux-only — sandbox.nix pulls bubblewrap, which
      # carries `meta.platforms = [ linux ]` and REFUSES to evaluate on other
      # hostPlatforms, taking `nix flake check` down with it on macOS. Same
      # guard as the `matrix` group above (and `cage` in hermes-agent.nix).
      // lib.optionalAttrs pkgs.stdenv.isLinux { inherit sandbox; };
    };
}

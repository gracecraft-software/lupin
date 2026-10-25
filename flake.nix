{
  description = "lupin -- model routing and slot leases for multi-machine delegation loops";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe";
    oh-my-pi = {
      url = "github:can1357/oh-my-pi/cde91bb38674d365e8c3916d05d2292273bb8554";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { self, nixpkgs, oh-my-pi }:
    let
      systems = [ "aarch64-linux" "x86_64-linux" "aarch64-darwin" ];
      forEachSystem = f: nixpkgs.lib.genAttrs systems f;
      mkLupin =
        pkgs:
        withOmp:
        pkgs.python315Packages.buildPythonApplication {
          pname = "lupin";
          # `pyproject.toml` is the only place the version is set.
          version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
          pyproject = true;
          src = self;
          build-system = [ pkgs.python315Packages.setuptools ];
          # `python315Packages.redis` is the `redis` backend's only runtime
          # dependency (issue #210).
          dependencies = [ pkgs.python315Packages.redis ];
          doCheck = false;
          # `lupin serve` shells out to these fixed read-only probes and
          # runs them from PATH, so they belong on the wrapper's PATH.
          # `systemd` and `chromium` are Linux-only and this flake builds for darwin too.
          # `chromium` takes the periodic debrief screenshots.
          # `herdr` and `omp` are runtime tools used by Lupin; include them on
          # the wrapper PATH. `LUPIN_HERDR_BIN` can select another Herdr binary.
          makeWrapperArgs = [
            "--prefix"
            "PATH"
            ":"
            (nixpkgs.lib.makeBinPath (
              [
                pkgs.gh
                pkgs.git
                pkgs.tmux
                pkgs.herdr
              ]
              ++ nixpkgs.lib.optional withOmp
                oh-my-pi.packages.${pkgs.stdenv.hostPlatform.system}.default
              ++ nixpkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.systemd pkgs.chromium ]
            ))
          ];
          meta = {
            description = "Model routing and slot leases for multi-machine delegation loops";
            mainProgram = "lupin";
            license = nixpkgs.lib.licenses.fsl11Mit;
          };
        };
    in
    {
      packages = forEachSystem (
        system:
        let
          # nixpkgs counts FSL-1.1-MIT as unfree. Allow `lupin` and nothing else.
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "lupin";
          };
        in
        {
          default = mkLupin pkgs true;
          without-omp = mkLupin pkgs false;
        }
      );

      devShells = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python315.withPackages (pythonPackages: [
                pythonPackages.pytest
                pythonPackages.redis
              ]))
              # tests/conftest.py starts `redis-server` from PATH.
              pkgs.redis
            ];
            # uv puts .venv in the checkout by default. /code is shared with the Mac.
            shellHook = ''
              export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/lupin"
            '';
          };
        }
      );

      checks = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.runCommand "lupin-tests" {
            src = self;
            package = self.packages.${system}.default;
            nativeBuildInputs = [
              (pkgs.python315.withPackages (pythonPackages: [
                pythonPackages.pytest
                pythonPackages.redis
              ]))
              pkgs.redis
              # `roadmap._git_remote_url` shells out to `git`, and the
              # wrapper's PATH does not reach this builder -- the sandbox
              # only sees nativeBuildInputs.
              pkgs.git
            ]
            # The group-kill test calls `ps` (see `_running`). The Linux
            # sandbox has no `ps` on PATH.
            ++ nixpkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.procps ];
          } ''
            test -x "$package/bin/lupin"
            "$package/bin/lupin" --help >/dev/null
            mkdir source
            cp -R "$src"/. source/
            chmod -R u+w source
            cd source
            pytest -q
            touch "$out"
          '';
        }
        # The VM test needs a Linux host. A darwin check cannot build it.
        // nixpkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          nixos-module = import ./nixos/tests/module.nix {
            inherit pkgs self;
          };
        }
      );

      # The fleet services (agent, dashboard, quota timer, fleet-run timer)
      # for a worker, a dispatch machine, or both.
      nixosModules.default = import ./nixos/module.nix { inherit self; };
    };
}

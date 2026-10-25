{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = {nixpkgs, ...}: let
    eachSystem = f:
      nixpkgs.lib.genAttrs nixpkgs.lib.systems.flakeExposed (system: f nixpkgs.legacyPackages.${system});
  in {
    packages = eachSystem (pkgs: let
      libfdi = pkgs.runCommand "libfdi" {} ''
        install -Dm644 ${./runtimes/libfdi.h} $out/include/libfdi.h
      '';
    in {
      python3 = pkgs.python312.withPackages (ps: with ps; [numpy pyspark bitarray]);
      # libfdi.h is on gcc's default include path, so a program can
      # `#include "libfdi.h"` with nothing beside it in the box. `-isystem`
      # keeps `-Wall` quiet about the header's own code.
      gcc = pkgs.gcc.override {
        extraBuildCommands = ''
          echo "-isystem ${libfdi}/include" >> $out/nix-support/cc-cflags
        '';
      };
      jdk = pkgs.jdk21;
    });

    devShells = eachSystem (pkgs: let
      pgStart = pkgs.writeShellScriptBin "pg-start" ''
        set -euo pipefail
        : "''${PGDATA:?PGDATA not set — enter the dev shell first}"
        : "''${PGHOST:?PGHOST not set — enter the dev shell first}"
        mkdir -p "$PGHOST"
        if [ ! -s "$PGDATA/PG_VERSION" ]; then
          ${pkgs.postgresql}/bin/initdb -U postgres -E UTF8 --auth=trust --no-locale "$PGDATA"
        fi
        exec ${pkgs.postgresql}/bin/postgres -D "$PGDATA" -k "$PGHOST" -h ""
      '';
      pgStop = pkgs.writeShellScriptBin "pg-stop" ''
        set -euo pipefail
        : "''${PGDATA:?PGDATA not set — enter the dev shell first}"
        ${pkgs.postgresql}/bin/pg_ctl -D "$PGDATA" stop -m fast
      '';
      pgCreateDb = pkgs.writeShellScriptBin "create-db" ''
        set -euo pipefail
        if [ $# -ne 1 ]; then
          echo "usage: create-db <name>" >&2
          exit 2
        fi
        name="$1"
        : "''${PGHOST:?PGHOST not set — enter the dev shell first}"
        if ${pkgs.postgresql}/bin/psql -h "$PGHOST" -U postgres -d postgres -tAc \
            "SELECT 1 FROM pg_database WHERE datname='$name'" | grep -q 1; then
          echo "database '$name' already exists"
        else
          ${pkgs.postgresql}/bin/createdb -h "$PGHOST" -U postgres "$name"
          echo "created database '$name'"
        fi
      '';
    in {
      default = pkgs.mkShell {
        buildInputs = [
          pkgs.python3
          pkgs.uv
          pkgs.postgresql
          pkgs.nodejs_22
          pkgs.just
        ];
        shellHook = ''
          export LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib.outPath}/lib:$LD_LIBRARY_PATH";
          export PGHOST="localhost"
          export PGUSER=postgres
          export PGDATABASE=cocco-dev
          test -d .venv || uv venv --python=${pkgs.python3.interpreter} .venv
          source .venv/bin/activate
        '';
      };
    });
  };
}

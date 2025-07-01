{pkgs}:
pkgs.writeShellScriptBin "deploy" ''
  set -xe

   ${pkgs.nixos-rebuild}/bin/nixos-rebuild --target-host "$1" --build-host "$1" --flake . --fast switch
''


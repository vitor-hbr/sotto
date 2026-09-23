{ system ? builtins.currentSystem }:
let
  nixpkgs = builtins.fetchTarball {
    url = "https://github.com/NixOS/nixpkgs/archive/21a67dc470149f337cecafbe965d8d252a390518.tar.gz";
    sha256 = "sha256-ugpsyk3NM2s87vXfUiIIiibbJ4Pp0JPS5p/3mfs+q+c=";
  };
  pkgs = import nixpkgs {
    inherit system;
    # CUDA's redistributable compiler/runtime use NVIDIA's license.
    config.allowUnfree = true;
  };
in {
  client = import ./client.nix { inherit pkgs; };
  serverShell = import ./server-shell.nix { inherit pkgs; };
}

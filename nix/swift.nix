{ pkgs }:
assert pkgs.stdenv.hostPlatform.system == "x86_64-linux";
let
  # nixpkgs currently supplies Swift 5.10; Sotto needs Swift 6.2 or newer.
  # This pinned packager patches the official swift.org binary distribution for NixOS.
  swiftix = builtins.fetchTarball {
    url = "https://github.com/stillwind-ai/swiftix/archive/7445ad1e15fb4f62af985549110f2241ad4194a3.tar.gz";
    sha256 = "0k4ckd8v4zqzv19k07x9yqg8yzav4kw5x5hw69mvsa9szkhbfm9r";
  };
in import "${swiftix}/lib/mkToolchain.nix" {
  inherit pkgs;
  system = pkgs.stdenv.hostPlatform.system;
  version = "6.2.4";
  tag = "swift-6.2.4-RELEASE";
  sha256 = "sha256-FWCMT6A2TvkGAUND2BY5z1gWnopA3istNQPD81qLtm0=";
}

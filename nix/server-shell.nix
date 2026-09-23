{ pkgs }:
let
  swift = import ./swift.nix { inherit pkgs; };
  cuda = pkgs.cudaPackages;
  cudaToolkit = pkgs.symlinkJoin {
    name = "sotto-cuda-toolkit-${cuda.cudaMajorMinorVersion}";
    paths = pkgs.lib.concatMap
      (package: map (output: package.${output}) (builtins.filter (output: output != "static") package.outputs))
      [ cuda.cuda_nvcc cuda.cuda_cccl cuda.cuda_cudart cuda.libcublas ];
  };
in (pkgs.mkShell.override { stdenv = cuda.backendStdenv; }) {
  packages = with pkgs; [ swift cmake pkg-config git curl perl python3 llvmPackages.clang ];
  buildInputs = [ cudaToolkit pkgs.curl.dev ];
  shellHook = ''
    export SOTTO_SWIFT_ROOT=${swift}
    export SOTTO_SWIFT_CC=${pkgs.llvmPackages.clang}/bin/clang
    export SOTTO_SWIFT_CXX=${pkgs.llvmPackages.clang}/bin/clang++
    export SOTTO_NATIVE_CC=${cuda.backendStdenv.cc}/bin/gcc
    export SOTTO_NATIVE_CXX=${cuda.backendStdenv.cc}/bin/g++
    export CC="$SOTTO_NATIVE_CC" CXX="$SOTTO_NATIVE_CXX"
    export CUDAHOSTCXX="$SOTTO_NATIVE_CXX"
    export CUDAToolkit_ROOT=${cudaToolkit}
    export LD_LIBRARY_PATH=/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
  '';
}

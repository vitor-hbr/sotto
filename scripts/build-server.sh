#!/bin/bash
set -euo pipefail

project_dir=$(cd "$(dirname "$0")/.." && pwd)
cd "$project_dir"
build_jobs="${SOTTO_BUILD_JOBS:-8}"
build_text_engine="${SOTTO_BUILD_TEXT_ENGINE:-ON}"
case "$build_text_engine" in ON|OFF) ;; *) printf 'SOTTO_BUILD_TEXT_ENGINE must be ON or OFF.\n' >&2; exit 1 ;; esac
server_platform=$(uname -s)
if [[ "$server_platform" == Darwin && "$build_text_engine" == OFF ]]; then
    printf 'Speech-only packaging currently supports Linux only.\n' >&2
    exit 1
fi
if [[ "$server_platform" != Darwin && "$server_platform" != Linux ]]; then
    printf 'The Sotto server supports macOS and Linux.\n' >&2
    exit 1
fi
for dependency in cmake swift; do
    if ! command -v "$dependency" >/dev/null; then
        printf 'Missing build dependency: %s\n' "$dependency" >&2
        exit 1
    fi
done
if [[ ! -f vendor/whisper.cpp/include/whisper.h || ! -f vendor/llama.cpp/include/llama.h ]]; then
    git submodule update --init --recursive
fi

native_flags=(-DCMAKE_BUILD_TYPE=Release "-DSOTTO_CUDA=${SOTTO_CUDA:-OFF}")
if [[ "$server_platform" == Darwin ]]; then
    if [[ "$(uname -m)" != arm64 ]]; then
        printf 'The macOS server uses MLX and requires Apple Silicon.\n' >&2
        exit 1
    fi
    native_flags+=(-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0 -DCMAKE_OSX_ARCHITECTURES=arm64)
fi
if [[ -n "${SOTTO_CUDA_ARCHITECTURES:-}" ]]; then
    native_flags+=("-DCMAKE_CUDA_ARCHITECTURES=$SOTTO_CUDA_ARCHITECTURES")
fi
if [[ -n "${SOTTO_NATIVE:-}" ]]; then
    native_flags+=("-DGGML_NATIVE=$SOTTO_NATIVE")
fi
cmake -S . -B .build/server-native "${native_flags[@]}"
cmake --build .build/server-native --target sotto-engine --parallel "$build_jobs"
if [[ "$server_platform" == Darwin ]]; then
    bash ./scripts/build-text-engine.sh
    text_helper_dir="$project_dir/.build/text-native"
elif [[ "$build_text_engine" == ON ]]; then
    cmake -S TextEngine -B .build/server-llama "${native_flags[@]}"
    cmake --build .build/server-llama --target sotto-text-engine --parallel "$build_jobs"
    text_helper_dir="$project_dir/.build/server-llama"
fi
bash ./scripts/download-vad.sh

# CUDA may need a different host compiler from SwiftPM's Clang C/C++ frontend.
swift_environment=()
if [[ -n "${SOTTO_SWIFT_CC:-}" ]]; then swift_environment+=("CC=$SOTTO_SWIFT_CC"); fi
if [[ -n "${SOTTO_SWIFT_CXX:-}" ]]; then swift_environment+=("CXX=$SOTTO_SWIFT_CXX"); fi
swift_flags=(--scratch-path .build/server-swift -c release --jobs "$build_jobs" --product sotto-server --force-resolved-versions)
env "${swift_environment[@]}" swift build "${swift_flags[@]}"
swift_bin=$(env "${swift_environment[@]}" swift build "${swift_flags[@]}" --show-bin-path)

mkdir -p build
staging_dir=$(mktemp -d "$project_dir/build/.server.XXXXXX")
trap 'rm -rf "$staging_dir"' EXIT
mkdir -p "$staging_dir/helpers" "$staging_dir/resources"
cp "$swift_bin/sotto-server" "$staging_dir/sotto-server"
cp .build/server-native/Engine/sotto-engine "$staging_dir/helpers/sotto-engine"
if [[ "$build_text_engine" == ON ]]; then
    cp "$text_helper_dir/sotto-text-engine" "$staging_dir/helpers/sotto-text-engine"
fi
if [[ "$server_platform" == Darwin ]]; then
    cp "$text_helper_dir/mlx.metallib" "$staging_dir/helpers/mlx.metallib"
    for bundle in "$text_helper_dir/resources/"*.bundle; do
        [[ -d "$bundle" ]] || continue
        ditto "$bundle" "$staging_dir/helpers/$(basename "$bundle")"
    done
    codesign --force --sign - "$staging_dir/helpers/sotto-engine"
    codesign --force --sign - "$staging_dir/helpers/sotto-text-engine"
    codesign --force --sign - "$staging_dir/sotto-server"
fi
cp .build/models/silero-vad.bin "$staging_dir/resources/silero-vad.bin"
cp vendor/whisper.cpp/LICENSE "$staging_dir/resources/whisper-LICENSE.txt"
cp vendor/llama.cpp/LICENSE "$staging_dir/resources/llama-LICENSE.txt"
cp Resources/*-LICENSE.txt THIRD_PARTY_NOTICES.md "$staging_dir/resources/"
cp Server/README.md "$staging_dir/README.md"
prior_package="$project_dir/build/.server-previous-$$"
if [[ -d build/server ]]; then mv build/server "$prior_package"; fi
if ! mv "$staging_dir" "$project_dir/build/server"; then
    if [[ -d "$prior_package" ]]; then mv "$prior_package" "$project_dir/build/server"; fi
    exit 1
fi
rm -rf "$prior_package"
printf '\nBuilt %s/build/server\n' "$project_dir"

# Sotto server

The server is an independent HTTP process that owns models, shared preferences, recordings, and history. For same-Mac development, start with the [quick start](../README.md). This guide covers model installation and running the server separately.

| Server | Speech | Proofreading |
| --- | --- | --- |
| Apple Silicon macOS | Whisper large-v3-turbo / whisper.cpp / Metal | Qwen3-4B-Instruct-2507 / Swift MLX / 4-bit |
| Linux x86_64 or ARM64 | Whisper large-v3-turbo / whisper.cpp / CPU or CUDA | Qwen3-4B-Instruct-2507 / llama.cpp / Q4_K_M |

## Models

Run these commands from the repository root. Weights use about 4 GB of disk; runtime memory also includes model state and inference buffers. The server verifies pinned files before loading and keeps models warm. It does not download large weights automatically.

### Whisper, on either platform

```sh
SOTTO_MODEL_DIR="$PWD/.local/models" ./scripts/download-model.sh
```

This installs and verifies `ggml-large-v3-turbo.bin`. The URL, revision, and checksum are pinned in `scripts/download-model.sh` and `Sources/SottoCore/SpeechModel.swift`. The server build separately downloads the pinned Silero VAD model.

### Qwen on macOS

The MLX directory must contain exactly the six files listed below. Download the pinned revision:

```sh
(
  set -e
  sotto_qwen_dir="$PWD/.local/models/Qwen3-4B-Instruct-2507-MLX-4bit"
  sotto_qwen_url="https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit/resolve/50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
  mkdir -p "$sotto_qwen_dir"
  for file in model.safetensors config.json tokenizer.json tokenizer_config.json generation_config.json chat_template.jinja; do
    curl --fail --location --retry 3 --output "$sotto_qwen_dir/$file" "$sotto_qwen_url/$file"
  done
)
```

`Sources/SottoCore/TextModel.swift` defines the six-file size/hash manifest; the MLX helper verifies it before becoming ready. Use regular files, with no extra files or symlinks in the model directory.

### Qwen on Linux

```sh
mkdir -p .local/models
curl --fail --location --retry 3 \
  --output .local/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
```

Expected size: 2,497,281,120 bytes. SHA-256: `3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597`. The server verifies both before loading.

## Build

Initialize submodules with `git submodule update --init --recursive`. macOS requires Apple Silicon and full Xcode with its Metal compiler; the complete client/server build uses Xcode 26+ and Swift 6.2+. If Metal is missing, run `xcodebuild -downloadComponent MetalToolchain`.

Linux requires Swift 6.2+, a C/C++ toolchain, CMake, Git, curl, pkg-config, and libcurl development headers. CUDA builds also need a compatible NVIDIA driver and CUDA toolkit. The [Dockerfile](Dockerfile) provides a pinned build environment.

```sh
./scripts/build-server.sh                  # macOS Metal/MLX; Linux CPU
SOTTO_CUDA=ON ./scripts/build-server.sh     # Linux with CUDA
# Linux dictation without the optional Qwen helper:
SOTTO_CUDA=ON SOTTO_BUILD_TEXT_ENGINE=OFF ./scripts/build-server.sh
```

For a speech-only build, disable proofreading through the preferences API or
client settings. Keep the proof-helper and proof-model arguments configured,
but their files are not required while proofreading is disabled.

Output is `build/server`: executable, native helpers, VAD, notices, and resources. Keep the package together; the Mac proofreader requires the adjacent Metal library and bundles. Large model weights and user data live outside it.

`SOTTO_BUILD_JOBS` controls build concurrency. For another CPU/GPU host, use `SOTTO_NATIVE=OFF` and set `SOTTO_CUDA_ARCHITECTURES` for the destination GPU. CPU support is useful for compatibility tests; validate CUDA support, memory, and dictation latency on the selected host.

## Run

From the repository root, with the models installed above:

```sh
./build/server/sotto-server \
  --host 127.0.0.1 --port 8391 \
  --data-dir "$PWD/.local/server" \
  --speech-helper "$PWD/build/server/helpers/sotto-engine" \
  --speech-model "$PWD/.local/models/ggml-large-v3-turbo.bin" \
  --vad-model "$PWD/build/server/resources/silero-vad.bin" \
  --proof-helper "$PWD/build/server/helpers/sotto-text-engine" \
  --proof-model "$PWD/.local/models/Qwen3-4B-Instruct-2507-MLX-4bit"
```

On Linux, replace the last path with the GGUF file. Add `--dev` for a development label in health responses. If using the packaged distribution elsewhere, point helper/resource paths at that package and choose durable model/data paths.

Check `curl http://localhost:8391/v1/health`; HTTP reachability alone does not mean the models are ready. The `ready` field means the server can accept a recording. Quitting a client does not stop this process. Use launchd, systemd, or container supervision for boot/restart behavior; the scripts do not install a service.

| Argument | Environment variable |
| --- | --- |
| `--host`, `--port` | `SOTTO_SERVER_HOST`, `SOTTO_SERVER_PORT` |
| `--data-dir`, `--token-file` | `SOTTO_SERVER_DATA_DIR`, `SOTTO_SERVER_TOKEN_FILE` |
| `--speech-helper`, `--speech-model` | `SOTTO_ENGINE_PATH`, `SOTTO_SPEECH_MODEL` |
| `--vad-model` | `SOTTO_VAD_PATH` |
| `--proof-helper`, `--proof-model` | `SOTTO_TEXT_ENGINE_PATH`, `SOTTO_TEXT_MODEL` |
| `--dev` | `SOTTO_DEV=1` |

The dev runner fixes its host to loopback and defaults to port 8391, `.local/server` for data, and `.local/server.log` for logs. Set `SOTTO_SPEECH_MODEL` and `SOTTO_TEXT_MODEL` when using the paths above. Without those overrides, macOS searches the existing locations `~/Library/Application Support/Murmur/Models/ggml-large-v3-turbo.bin` and `~/.murmur/models/Qwen3-4B-Instruct-2507-MLX-4bit`.

## Remote access

Bind to a reachable address and pass `--token-file /absolute/path/to/token`. Nonloopback listeners require a token of at least 32 characters with no internal whitespace. In the Mac app, enter the endpoint and token under **This Mac**; tokens are stored in Keychain.

- Use an HTTPS reverse proxy for hosted servers and hostnames, including Tailscale MagicDNS names. The runner itself serves HTTP.
- HTTP is accepted for localhost and literal Tailscale IPs in `100.64.0.0/10` or `fd7a:115c:a1e0::/48` on your connected tailnet. Sotto checks the address range, not routing; use HTTPS if that private route cannot be assured.
- Ordinary LAN IPs require HTTPS. Endpoints cannot contain credentials, queries, or fragments. Credential-bearing redirects are not followed.

Keep the data directory on persistent storage and back it up. Only one runner can own it. See [storage](../docs/architecture.md#storage) and the [HTTP API](../docs/client-server-contract.md).

## Containers

Build from the repository root with initialized submodules:

```sh
docker build -f Server/Dockerfile --target cpu -t sotto-server:cpu .
docker build -f Server/Dockerfile --target cuda -t sotto-server:cuda .
```

`CUDA_ARCHITECTURES`, `CUDA_IMAGE`, `SWIFT_IMAGE`, and `BUILD_JOBS` are build arguments. Choose CUDA architectures/toolkit/driver versions for your GPU. GPU containers require NVIDIA Container Toolkit and `--gpus all`; Linux containers on a Mac do not have Metal access.

Mount a directory containing the Whisper `.bin` and Qwen `.gguf` files, plus a token file:

```sh
docker run --rm --name sotto-server \
  -p 127.0.0.1:8391:8391 \
  --mount type=volume,source=sotto-data,target=/data \
  --mount type=bind,source=/absolute/path/to/models,target=/models,readonly \
  --mount type=bind,source=/absolute/path/to/token,target=/run/secrets/sotto-token,readonly \
  sotto-server:cpu
```

For a GPU server, use `sotto-server:cuda` and add `--gpus all`. The example exposes only host loopback; use the remote-access setup above for clients on other machines. The container runs as UID 10001, which must be able to read model/token files and write `/data`. The named volume preserves history across container replacement.

## Verify

```sh
swift test
./scripts/smoke-test.sh
SOTTO_TEXT_MODEL=/absolute/path/to/qwen ./scripts/test-corrections.sh
```

The HTTP smoke test needs an idle Dev server with proofreading enabled. It uses public sample audio, checks progress/artifacts, temporarily changes and restores retention settings, and removes its test generations. The helper test accepts the MLX directory on Mac or GGUF file on Linux and uses synthetic text. Neither opens a microphone. These checks do not establish live cursor-insertion behavior or GPU performance on a different machine.

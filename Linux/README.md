# Sotto for Linux

A native GTK 4 desktop client targeting **Ubuntu 24.04+ / GNOME / Wayland**.
It connects to Sotto's existing Linux or macOS server. The client uses Python's
standard library, PyGObject, GTK, and GStreamer; no browser runtime is required.

## Install the client

```sh
sudo apt-get update
sudo apt-get install -y python3-gi gir1.2-gtk-4.0 \
  gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-good gstreamer1.0-pulseaudio
git clone --branch feat/linux-native https://github.com/vitor-hbr/sotto.git
cd sotto
sh Linux/install.sh
```

Launch **Sotto** from Applications, or run `~/.local/bin/sotto-linux`.
The installer copies the client into `${XDG_DATA_HOME:-~/.local/share}/sotto`,
adds an application launcher and creates `~/.local/bin/sotto-linux`. Rerun it
after updating this checkout. It does not install or start the model server.

For development without installing:

```sh
sh scripts/run-linux.sh
```

## Start a local Linux server

Follow the [server guide](../Server/README.md) for dependencies (including Swift
6.2+), pinned model downloads, CPU/CUDA builds, and remote authentication.
After building and downloading both Linux models, run from the repository root:

```sh
./build/server/sotto-server \
  --host 127.0.0.1 --port 8391 \
  --data-dir "$PWD/.local/server" \
  --speech-helper "$PWD/build/server/helpers/sotto-engine" \
  --speech-model "$PWD/.local/models/ggml-large-v3-turbo.bin" \
  --vad-model "$PWD/build/server/resources/silero-vad.bin" \
  --proof-helper "$PWD/build/server/helpers/sotto-text-engine" \
  --proof-model "$PWD/.local/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
```

Wait for `curl http://localhost:8391/v1/health` to report `"ready":true`.
Enter `http://localhost:8391` in the client. A remote server needs an HTTPS origin
and its bearer token. The Linux client allows plain HTTP only on loopback,
rejects URL credentials/paths, and does not follow redirects.

## Dictate

1. Select your input device in **GNOME Settings → Sound → Input**.
2. Open Sotto and set its server address and optional token.
3. Click **Start recording**, speak, then **Stop recording**. Wait until the UI
   says **Recording** before speaking; the server must admit the take first.
4. When the transcript appears, click **Copy transcript**, switch to your
   destination, and paste with Ctrl+V (usually Ctrl+Shift+V in terminals).

To record from another application, open **GNOME Settings → Keyboard → View and
Customize Shortcuts → Custom Shortcuts**. Add a shortcut named Sotto with command
`/home/YOUR_USER/.local/bin/sotto-linux --toggle` (replace with your absolute path).
Choose an unused combination, for example Ctrl+Alt+Space. Press once to start,
again to stop. The command reaches the running instance over the user session
bus and does not focus the window during recording. The result window opens
when processing finishes; click Copy explicitly for Wayland clipboard ownership.

Other commands: `--stop`, `--cancel`, `--quit`. Closing the window hides it and
keeps Sotto running so clipboard content and shortcuts remain available. Use
**Quit Sotto** or `--quit` to exit. There is no automatic startup entry.

## Behavior and storage

- Audio streams during capture. A take lasts 0.25–180 seconds. Cancellation,
  capture errors, and upload failures release the microphone; incomplete uploads
  are cancelled where possible, otherwise the server expires them.
- Capture is negotiated as mono 16 kHz float32 through the PulseAudio service
  provided by PipeWire on Ubuntu. Both retained streams contain this normalized
  audio, **not the microphone's original hardware rate/channel layout**.
- The admission-time `keepOriginalAudio` preference controls whether the original
  stream is uploaded. Inference audio is always retained by the server. No audio
  is written to local client files. Server history is preserved independently.
- Device identity and server address are saved in
  `${XDG_CONFIG_HOME:-~/.config}/sotto/client.json`. Tokens entered in the UI stay
  in memory. Optionally set `SOTTO_CLIENT_TOKEN_FILE` to an existing private file
  when launching the primary process. The token is never stored in client JSON.
- The server owns language, dictionary, cleanup, and history. Configure those
  through its [API](../docs/client-server-contract.md) or the existing Mac client.
- Linux delivery is recorded as `copied`, never `inserted`. Copying replaces the
  clipboard. There is no automatic paste, caret tracking, or continuation context.
- No offline queue, automatic upload retries, hold-to-talk, global key listener,
  portal shortcut registration, tray icon, history UI, or shared settings UI yet.
  GNOME custom shortcuts provide the initial desktop integration.

## Verification

```sh
PYTHONPATH=Linux /usr/bin/python3 -m unittest discover -s Linux/tests -p test_client.py -v
sudo apt-get install -y xvfb dbus-x11 desktop-file-utils
dbus-run-session -- xvfb-run -a env GDK_BACKEND=x11 PYTHONPATH=Linux \
  /usr/bin/python3 -m unittest discover -s Linux/tests -p test_desktop.py -v
desktop-file-validate "$HOME/.local/share/applications/io.github.vitor_hbr.Sotto.desktop"
```

CI runs protocol/lifecycle tests and real GTK/GStreamer checks on Ubuntu 24.04.
The desktop tests use Xvfb and synthetic audio. They do not establish end-to-end
GNOME/Wayland shortcut behavior, physical microphone capture, real Whisper/Qwen
inference, automatic focus behavior, or clipboard delivery to another Wayland
application. Validate those on the target desktop before treating this as a daily
driver. Server build and inference tests are documented in the server guide.

## Remove the client

Remove `~/.local/bin/sotto-linux`, the `sotto` directory under your XDG data
directory, and its `applications/io.github.vitor_hbr.Sotto.desktop` launcher.
Remove the GNOME custom shortcut separately. Client settings and server models,
recordings, and history are separate and remain until you explicitly remove them.

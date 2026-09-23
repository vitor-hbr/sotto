# Local dictation on NixOS

This setup uses Whisper large-v3-turbo with NVIDIA CUDA for speech recognition.
The server owns the models, settings, audio, and transcript history.
The GTK client captures the microphone. On GNOME, Vicinae sets the clipboard
and ydotool sends the paste shortcut, without a Remote Desktop session.
Neither process needs a cloud service after installation.

The server environment targets x86_64 Linux. It pins nixpkgs, Swift 6.2.4, and
the Swift packaging code. CUDA requires the NVIDIA license accepted by
`nix/packages.nix`. The client package is separate from the server environment.

## Build and install

Run these commands from the repository root as your desktop user.
Do not use `sudo`.

1. Download the pinned speech model:

   ```sh
   SOTTO_MODEL_DIR="$HOME/.local/share/sotto/models" bash scripts/download-model.sh
   ```

2. Build the server. Compute capability `89` applies to the RTX 2000 Ada laptop
   GPU. Select the correct capability before building for another GPU.

   ```sh
   nix-shell nix/packages.nix -A serverShell --run \
     'SOTTO_CUDA=ON SOTTO_CUDA_ARCHITECTURES=89 SOTTO_BUILD_TEXT_ENGINE=OFF SOTTO_BUILD_JOBS=4 bash scripts/build-server.sh'
   ```

   The first build downloads the compiler and CUDA libraries. Later builds
   reuse them. This speech-only build skips the optional text helper.
   Dictation does not require that helper or Qwen weights when proofreading is
   disabled. Omit `SOTTO_BUILD_TEXT_ENGINE=OFF` to build the helper later.

3. Install the client and server:

   ```sh
   bash scripts/install-linux-nix.sh
   ```

   The installer creates persistent Nix roots for the client and toolchain.
   It copies the built server into a user directory and enables
   `sotto-server.service`. It does not rebuild NixOS or change desktop shortcuts.
   Existing models and history remain in place.

4. Set English dictation without AI rewriting through the server API:

   ```sh
   curl -fsS http://127.0.0.1:8391/v1/preferences \
     | jq '.preferences.language = "en" |
           .preferences.textCorrectionEnabled = false |
           .preferences.keepOriginalAudio = false' \
     | curl -fsS -X PUT -H 'Content-Type: application/json' \
         --data-binary @- http://127.0.0.1:8391/v1/preferences
   ```

   The API checks the settings revision. If another client changes settings,
   repeat the read and update instead of editing `preferences.json` yourself.
   Original microphone audio is optional. Inference audio remains in history.

## Desktop setup

1. Open **Sotto** from Applications.
2. Set the server address to `http://localhost:8391`.
3. Select **Desktop → GNOME — Vicinae + ydotool, no Remote Desktop** as the paste backend. Quit and reopen Sotto after changing it. This requires the Vicinae GNOME extension, a running `ydotoold`, and permission to use its socket. All three are already configured on Vitor's P16v.
4. Select **Direct paste — automatic app shortcut (GNOME)** as the insertion mode. This uses Ctrl+Shift+V in Ghostty and Ctrl+V in browser, Slack, and T3 Code text inputs. The fixed terminal and standard modes remain available.
5. Enable **Start Sotto when I log in**. Clear **Show dictation notifications** for silent operation.
6. Bind an available GNOME custom shortcut to `/home/YOUR_USER/.local/bin/sotto-linux --toggle`.

On Vitor's P16v, **Caps+D** supplies Ctrl+Alt+Shift+D through keyd.
The binding also exists in `nixos-config/home/vicinae.nix`.
Press it once to start recording and again to finish.
Pause briefly after starting before speaking. Recording status remains visible
in Sotto when notifications are disabled.
The GNOME backend has no permission handshake, so the first press starts a
recording. The server keeps the speech model in memory between recordings.
When the helper process stops, the server loads the model again on its own.
A press after a long idle period starts at once. Only the first press after a
server restart waits for the model (up to two minutes) before it opens the
microphone. Pressing Caps+D again during that wait cancels the start. No later
recording starts on its own.

If the microphone or the server connection fails while you speak, the client
keeps the audio that the server already received. It transcribes that part and
shows a status line that starts with "Recording stopped early". The client
discards speech shorter than a quarter of a second.
If its clipboard or keyboard dependency fails, the transcript stays
in History. It never falls back to Remote Desktop or retries a paste.
The Nix client defaults `YDOTOOL_SOCKET` to `/run/ydotoold/socket`; an existing
environment value overrides it. This backend uses standard Linux Ctrl/V keycodes.
Do not enable a second portal shortcut on Alt+Space, which Vicinae uses.

Keep the intended application focused until transcription finishes.
Direct paste does not check the original window, caret, or password-field state.
It replaces both the clipboard and primary selection and does not send Enter.
If the text does not appear, open **History**, refresh, and copy the transcript.
Check the destination before copying to avoid inserting the text twice.

Closing the Sotto window leaves dictation active. **Quit Sotto** stops the client,
but the model server remains available. Both start again at the next login when
autostart is enabled. The GNOME backend does not ask for Remote Desktop permission.
The separate **Desktop portal** backend remains available for other setups and
still needs session permission; it is not used by this configuration.

## Check the installation

```sh
systemctl --user status sotto-server.service
curl -fsS http://127.0.0.1:8391/v1/health
nvidia-smi
journalctl --user -u sotto-server.service -n 50
```

The health response must contain `ready: true` and `speech.ready: true`.
A disabled proofreader does not prevent dictation.
Check that `sotto-engine` appears in NVIDIA's process list during model use.

The HTTP smoke test uses public sample audio and never opens the microphone.
It requires a separate, idle server started with `--dev`.
With proofreading disabled, run:

```sh
nix-shell nix/packages.nix -A serverShell --run \
  'python3 scripts/test-server.py --speech-only --endpoint http://127.0.0.1:8392'
```

Use a separate data directory and port for that Dev server. Supply the same
helper/model arguments as the service and add `--dev`. The test checks real
transcription, audio artifacts, history, and deletion. It restores preferences
and removes its own recordings. The normal test without `--speech-only` still
requires Qwen and checks proofreading too.

A sample-audio test does not prove that the selected microphone or destination
application works. Finish with a live shortcut recording and check History.

## Files and recovery

| Path under your home directory | Purpose |
| --- | --- |
| `.local/share/sotto/models` | Downloaded model weights |
| `.local/share/sotto/server` | Server settings, transcripts, and inference audio |
| `.local/share/sotto/runtime` | Installed server and helpers |
| `.local/share/sotto/client` | Nix root for the GTK client |
| `.local/share/sotto/toolchain` | Nix root for native runtime dependencies |
| `.config/sotto/client.json` | Device settings, not credentials |
| `.config/systemd/user/sotto-server.service` | Local model service |

History has no automatic expiry. Delete recordings in the History tab when you
no longer need them. Back up the server directory to preserve transcripts.
Do not remove the Nix roots while the installed application still needs them.
A failed upgrade retains recovery roots in `.local/share/sotto/.previous-nix-roots.*`.
Remove those directories only after confirming that the replacement installation
works and you no longer need the earlier runtime.

To stop the server and prevent its next login start:

```sh
systemctl --user disable --now sotto-server.service
```

Disable client autostart in Sotto separately. These actions do not delete history.

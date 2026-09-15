# Sotto for Linux

Native GTK 4 dictation for Ubuntu/GNOME on Wayland. The desktop client uses
GStreamer for microphone audio and the existing Sotto server for local Whisper
recognition and llama.cpp proofreading. No browser runtime is involved.

## Features

- Hold-to-talk through the XDG GlobalShortcuts portal, including key release,
  repeat suppression, cancellation, permission denial and session revocation.
- Automatic insertion into the original accessible text field, with focus,
  caret, selection and surrounding-text checks. Unicode insertion is verified;
  an ambiguous write is never retried. Password fields are excluded.
- Optional RemoteDesktop + Clipboard portal paste for applications such as
  terminals. It requests keyboard/clipboard access only, with Ctrl+Shift+V for
  terminal widgets and Ctrl+V elsewhere. No screen or pointer access is requested.
- Microphone selection and testing, live input level, recording notifications,
  bounded streaming uploads, cancellation and a three-minute recording limit.
- Original audio retains the negotiated source rate/channels; inference audio
  is independently resampled to mono 16 kHz. Both cover the same interval.
- Shared language, cleanup instructions, proofreading, retention, vocabulary,
  named dictionary lists, aliases, and priority hints. Saves use server revisions
  and preserve edits on conflicts.
- Paginated history, raw/final transcripts, audio playback, text export and
  confirmed deletion. Browsing history never triggers insertion.
- GNOME Keyring token storage, persistent device settings, optional login startup,
  single-instance command-line control, and a Debian package.
- Continuation context is reused only after verified insertion when the original
  field and caret still match. Editor text and accessibility handles stay local.

## Install

The client runs on Ubuntu 24.04+ with GTK 4 and Python 3.10+. **Hold-to-talk needs
a desktop implementing the GlobalShortcuts portal. Portal paste also needs the
RemoteDesktop and Clipboard portals.** Older GNOME installations can run the
client and accessible insertion but may lack these portal interfaces; update the
desktop/portal packages or use the command shortcut fallback below. Installing
the client cannot add missing compositor capabilities.

```sh
sudo apt-get update
sudo apt-get install -y python3-gi gir1.2-gtk-4.0 \
  gir1.2-gst-plugins-base-1.0 gir1.2-atspi-2.0 gir1.2-secret-1 \
  gstreamer1.0-plugins-good gstreamer1.0-pulseaudio \
  xdg-desktop-portal xdg-desktop-portal-gnome gnome-keyring
git clone --branch feat/linux-native https://github.com/vitor-hbr/sotto.git
cd sotto
sh Linux/install.sh
```

Launch **Sotto** from Applications or `~/.local/bin/sotto-linux`. Rerun the
installer after pulling updates. For development, use `sh scripts/run-linux.sh`.

Alternatively, build and install the system package:

```sh
sh scripts/build-linux-client.sh
sudo apt install ./build/sotto-linux_0.2.0_all.deb
```

The package installs `/usr/bin/sotto-linux` and the desktop launcher. The local
installer uses `~/.local/bin/sotto-linux` and the XDG data directory. Choose one
installation method; a pre-existing local launcher takes priority in many shells.

## Start the model server

The server is independent of the desktop client. Follow the
[server guide](../Server/README.md) for Swift 6.2+, native build dependencies,
pinned model downloads, CPU/CUDA support and remote authentication. After
building and downloading the Linux models, run from the repository root:

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

Use `http://localhost:8391` in Sotto, then **Check server connection**. The
models must report ready before recording. Remote servers require an HTTPS
origin and their bearer token. The client rejects URL credentials, paths,
queries, fragments and redirects. Plain HTTP is limited to loopback.

## Set up the desktop

1. In **Dictation**, enter the server address and token. Use **Desktop → Save
   current server token in GNOME Keyring** if it should persist across logins.
2. In **Desktop**, choose the microphone or keep System default. **Test microphone**
   records a take without inserting it; finish with Stop on the Dictation tab.
3. Click **Enable / configure hold-to-talk**. GNOME presents its shortcut dialog;
   choose an available trigger. The application suggests Ctrl+Alt+Space. The
   status line shows the granted shortcut. A declined or unavailable permission
   is shown explicitly and leaves no active session.
4. Enable automatic insertion. Focus a text field, hold the shortcut, wait for
   the Recording notification, speak, then release. Keep the same caret and
   field until completion. Successful insertion does not focus Sotto's window.
5. For a terminal or an application requiring simulated paste, enable
   **portal paste** and approve keyboard/clipboard access in GNOME's dialog.
   Session access can be revoked in Sotto or through the desktop indicator.
6. Optionally enable **Start Sotto when I log in**. Closing the window keeps
   shortcuts available; use Quit Sotto to end the process and portal sessions.

Automatic delivery depends on the destination application's accessibility
support. A changed caret, text selection, inaccessible destination, revoked
permission or ambiguous write leaves the transcript available in Sotto. Check
the destination before copying after an unconfirmed delivery. Portal paste
replaces the clipboard and reports `unconfirmed`, because sending a shortcut
does not prove the destination accepted it. It never triggers a second attempt.

### Command shortcut fallback

If the GlobalShortcuts portal is absent, bind the absolute path to
`sotto-linux --toggle` in **GNOME Settings → Keyboard → Custom Shortcuts**.
Press once to start, again to stop. Desktop environments or external shortcut
managers with separate press/release bindings can call `--start` on press and
`--stop` on release. `--start` is idempotent while a take is active.

Other commands: `--cancel`, `--background`, `--quit`. All commands reach the
existing instance over the session bus. A shortcut does not bring Sotto to the
foreground during capture. A manual GUI start has no external caret anchor,
so its result is displayed for copying.

## Shared settings and history

Use **Server → Load / reload from server** before editing. Language, cleanup,
dictionary and retention changes apply to future takes on every connected
device. Active takes retain their admission-time settings. Concurrent saves
are rejected by the server; the client retains unsaved edits and asks you to
reload. Dictionary list/word identities and empty lists survive round trips.

**History → Refresh history** loads the newest 50 recordings. Load older pages,
select a recording to inspect both transcripts, play its inference audio,
export text or delete it. Deletion asks for confirmation and removes the
recording and artifacts from the server. Active recordings cannot be deleted.

## Storage and privacy

- Device identity, endpoint, microphone and desktop preferences live in
  `${XDG_CONFIG_HOME:-~/.config}/sotto/client.json`, written atomically with
  owner-only permissions. Tokens never enter this JSON file.
- Tokens remain in memory unless explicitly saved in GNOME Keyring. They are
  scoped to the server origin. Changing the address clears the session token;
  use Desktop's Load saved token action for another saved server.
- `SOTTO_CLIENT_TOKEN_FILE` can supply a token from an existing private file
  when the primary process starts. It takes precedence over keyring lookup.
- Audio is bounded in memory and uploaded while speaking. Original retention
  follows the server's frozen preference. Inference audio and history are
  retained by the server. There is no offline queue or silent retry.
- History playback uses a private temporary WAV, removed on stop, selection
  change or normal quit. Text exports remain at the location you choose.
- Accessibility tracking inspects only the focused text field and a small local
  context around its caret. The context is never sent to the server or logged.

## Verification

```sh
sudo apt-get install -y xvfb dbus-x11 desktop-file-utils weston
dbus-run-session -- xvfb-run -a env GDK_BACKEND=x11 PYTHONPATH=Linux \
  /usr/bin/python3 -m unittest discover -s Linux/tests -p 'test_*.py' -v
sh scripts/test-linux-wayland.sh
sh scripts/build-linux-client.sh
desktop-file-validate "$HOME/.local/share/applications/io.github.vitor_hbr.Sotto.desktop"
```

CI exercises HTTP capture/upload, retention and frame alignment, permission
flows over a real D-Bus test service, clipboard file-descriptor transfer,
modifiers, settings conflicts, history, GTK, and insertion into a separate
native GTK editor. The Wayland checks run GTK against a nested Weston compositor
with a virtual display; they do not substitute for a real GNOME portal session.

Physical microphone hardware, GNOME's permission dialogs, third-party editors,
and real Whisper/Qwen inference need testing on the destination desktop.
The model server is unchanged; its build/inference checks are in the server
guide. Portal behavior follows the official [GlobalShortcuts](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html),
[RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
and [Clipboard](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Clipboard.html) interfaces.

## Uninstall

First disable login startup and quit Sotto. For a system package, use
`sudo apt remove sotto-linux`. For a local installation, remove
`~/.local/bin/sotto-linux`, the `sotto` directory under your XDG data directory,
and `applications/io.github.vitor_hbr.Sotto.desktop` there. Remove any manually
configured keyboard shortcut separately. Device settings, keyring credentials
and server models/history are separate; remove those only if wanted.

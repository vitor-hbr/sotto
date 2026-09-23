#!/usr/bin/env bash
# Install only. Build the server with scripts/build-server.sh in the matching
# nix/packages.nix serverShell first; rooting a different toolchain is not enough.
set -euo pipefail
umask 077

fail() { printf 'Sotto install: %s\n' "$*" >&2; exit 1; }
exists() { [[ -e "$1" || -L "$1" ]]; }
backup_once() {
    if exists "$1" && ! exists "$1.pre-nix-install"; then
        cp -a --no-dereference -- "$1" "$1.pre-nix-install"
    fi
}

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'Requires x86_64 Linux.'
(( EUID != 0 )) || fail 'Run as your desktop user, not root.'
[[ ${HOME:-} == /* && $HOME != / ]] || fail 'HOME must be an absolute user home path.'
for command in nix-build nix-store systemctl flock mktemp cp mv ln install grep readlink; do
    command -v "$command" >/dev/null || fail "Missing command: $command"
done

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$project_dir/build/server"
data_dir="$HOME/.local/share/sotto"
bin_dir="$HOME/.local/bin"
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
launcher="$bin_dir/sotto-linux"
desktop="$applications_dir/io.github.vitor_hbr.Sotto.desktop"
service=sotto-server.service
unit="$unit_dir/$service"
runtime="$data_dir/runtime"

[[ -f "$project_dir/nix/packages.nix" ]] || fail 'Missing nix/packages.nix.'
[[ -f "$project_dir/Linux/systemd/$service" ]] || fail 'Missing user-service template.'
for executable in sotto-server helpers/sotto-engine; do
    [[ -f "$source_dir/$executable" && -x "$source_dir/$executable" ]] ||
        fail "Missing executable: $source_dir/$executable. Run scripts/build-server.sh first."
done
[[ -s "$source_dir/resources/silero-vad.bin" ]] || fail 'Missing built resources/silero-vad.bin.'
# Check the user manager before building anything or stopping the existing server.
systemctl --user show-environment >/dev/null || fail 'No reachable systemd user manager; run from your user session.'

mkdir -p -- "$data_dir"
exec 9>"$data_dir/.install.lock"
flock -n 9 || fail 'Another Sotto installer is running.'

# Only replace launchers from known Sotto installs, never an unrelated command.
if exists "$launcher"; then
    if [[ -L "$launcher" ]]; then
        target=$(readlink -- "$launcher")
        case "$target" in
            "$data_dir/client/bin/sotto-linux"|/nix/store/*-sotto-linux-*/bin/sotto-linux) ;;
            *) fail "Refusing to replace unrelated launcher: $launcher" ;;
        esac
    elif [[ -f "$launcher" ]]; then
        # Match literal shell source, not this installer's expanded HOME or arguments.
        # shellcheck disable=SC2016
        grep -Fxq 'exec /usr/bin/python3 -m sotto_linux "$@"' "$launcher" ||
            grep -Fxq 'exec "$HOME/.local/share/sotto/client/bin/sotto-linux" "$@"' "$launcher" ||
            fail "Refusing to replace unrecognized launcher: $launcher"
    else
        fail "Refusing to replace non-file launcher: $launcher"
    fi
fi
if exists "$desktop"; then
    if [[ ! -f "$desktop" ]] ||
        ! grep -Eq '^Name=Sotto( Desktop Test)?$' "$desktop" ||
        ! grep -Eq '^Exec=.*(sotto-linux|sotto_linux|desktop-probe\.py)' "$desktop"; then
        fail "Refusing to replace unrelated desktop entry: $desktop"
    fi
fi
if exists "$unit"; then
    if [[ ! -f "$unit" ]] || ! grep -Eq '^ExecStart=.*sotto-server' "$unit"; then
        fail "Refusing to replace unrelated user unit: $unit"
    fi
fi
for root in "$data_dir/client" "$data_dir/toolchain"; do
    if exists "$root"; then
        [[ -L "$root" && $(readlink -- "$root") == /nix/store/* ]] ||
            fail "Refusing to replace a non-Nix output link: $root"
    fi
done
if exists "$runtime"; then
    [[ -d "$runtime" && ! -L "$runtime" && -f "$runtime/sotto-server" ]] ||
        fail "Refusing to replace an unrecognized runtime: $runtime"
fi

# Preserve registered roots across failed upgrades, not only backup symlinks.
# Each attempt keeps its own roots: a failed attempt may already have replaced
# the live links while the installed runtime still needs an older closure.
previous_roots=''
for root in "$data_dir/client" "$data_dir/toolchain"; do
    if exists "$root"; then
        if [[ -z "$previous_roots" ]]; then
            previous_roots=$(mktemp -d "$data_dir/.previous-nix-roots.XXXXXX")
            printf 'Previous Nix outputs protected at %s (retained if installation fails).\n' "$previous_roots"
        fi
        nix-store --realise "$(readlink -- "$root")" \
            --add-root "$previous_roots/$(basename -- "$root")" --indirect >/dev/null
    fi
done

# Keep both closures reachable by the Nix garbage collector after logout.
backup_once "$data_dir/client"
backup_once "$data_dir/toolchain"
nix-build "$project_dir/nix/packages.nix" -A client --out-link "$data_dir/client"
nix-build "$project_dir/nix/packages.nix" -A serverShell --out-link "$data_dir/toolchain"
[[ -x "$data_dir/client/bin/sotto-linux" ]] || fail 'Client output has no sotto-linux executable.'

stage=$(mktemp -d "$data_dir/.install.XXXXXX")
previous=''
cleanup() {
    result=$?
    trap - EXIT
    # Restore the original path if the second rename of the runtime swap failed.
    if [[ -n "$previous" && -d "$previous" ]]; then
        if ! exists "$runtime"; then
            mv -T -- "$previous" "$runtime" || true
        else
            printf 'Previous runtime retained at %s\n' "$previous" >&2
        fi
    fi
    rm -rf -- "$stage"
    if (( result != 0 )); then
        printf 'Install incomplete. Check systemctl --user status sotto-server.service before retrying.\n' >&2
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$stage/runtime"
# Dereference links so the installed distribution never depends on build/.
cp -aL -- "$source_dir/." "$stage/runtime/"
for executable in sotto-server helpers/sotto-engine; do
    [[ -f "$stage/runtime/$executable" && -x "$stage/runtime/$executable" ]] ||
        fail "Incomplete staged runtime: $executable"
done
[[ -s "$stage/runtime/resources/silero-vad.bin" ]] || fail 'Incomplete staged VAD model.'
ln -s -- "$data_dir/client/bin/sotto-linux" "$stage/launcher"
# Match Linux/install.sh: resolve HOME at launch, without embedding unescaped paths.
cat > "$stage/desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Sotto
Comment=Native Linux dictation
Exec=sh -c "exec \"\$HOME/.local/bin/sotto-linux\""
Icon=audio-input-microphone
Terminal=false
Categories=AudioVideo;Audio;
StartupNotify=true
DESKTOP
install -m 644 "$project_dir/Linux/systemd/$service" "$stage/unit"
chmod 644 "$stage/desktop"
mkdir -p -- "$bin_dir" "$applications_dir" "$unit_dir" "$data_dir/models" "$data_dir/server"
for path in "$launcher" "$desktop" "$unit"; do backup_once "$path"; done

# Stop only this user's Sotto server, and only after staging is complete.
load_state=$(systemctl --user show "$service" --property=LoadState --value) ||
    fail 'Could not query the existing user service.'
[[ -n "$load_state" ]] || fail 'Could not determine the existing user service state.'
if [[ "$load_state" != not-found ]]; then
    systemctl --user stop "$service"
fi
if exists "$runtime"; then
    previous=$(mktemp -d "$data_dir/.runtime-previous.XXXXXX")
    mv -T -- "$runtime" "$previous"
fi
# Same-filesystem rename publishes the complete distribution, not partial files.
if ! mv -T -- "$stage/runtime" "$runtime"; then
    fail 'Could not publish the staged runtime.'
fi
# -T replaces symlinks themselves; do not write through an earlier install's link.
mv -Tf -- "$stage/launcher" "$launcher"
mv -Tf -- "$stage/desktop" "$desktop"
mv -Tf -- "$stage/unit" "$unit"
systemctl --user daemon-reload
systemctl --user enable --now "$service"
if [[ -n "$previous" ]]; then
    rm -rf -- "$previous"
    previous=''
fi
if [[ -n "$previous_roots" ]]; then
    rm -rf -- "$previous_roots"
fi

printf 'Installed Sotto client and enabled the user server at http://127.0.0.1:8391.\n'
printf 'Models: %s/models\nHistory: %s/server\n' "$data_dir" "$data_dir"
printf 'Existing launcher, desktop entry, and unit backups use .pre-nix-install.\n'
if [[ ! -s "$data_dir/models/ggml-large-v3-turbo.bin" ]]; then
    printf 'Speech model missing: add ggml-large-v3-turbo.bin to the models directory before dictation.\n' >&2
fi
if [[ ! -x "$runtime/helpers/sotto-text-engine" || ! -s "$data_dir/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf" ]]; then
    printf 'Optional text helper or Qwen model missing: disable proofreading in Server settings.\n' >&2
fi
printf 'No GUI was launched or desktop permissions changed. User services normally run only while logged in.\n'

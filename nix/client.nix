{ pkgs }:

let
  python = pkgs.python3.withPackages (ps: [ ps.pygobject3 ]);
in
pkgs.stdenvNoCC.mkDerivation {
  pname = "sotto-linux";
  version = "0.2.0";

  src = pkgs.lib.cleanSourceWith {
    src = ../Linux;
    name = "sotto-linux-source";
    filter = path: type:
      let
        name = baseNameOf path;
      in
      pkgs.lib.cleanSourceFilter path type
      && name != "__pycache__"
      && !(pkgs.lib.hasSuffix ".pyc" name)
      && !(pkgs.lib.hasSuffix ".pyo" name);
  };

  nativeBuildInputs = [
    pkgs.wrapGAppsHook4
    pkgs.gobject-introspection
  ];
  buildInputs = with pkgs; [
    gtk4
    at-spi2-core
    libsecret
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    gst_all_1.gst-plugins-good
  ];

  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/lib/sotto" "$out/bin" "$out/share/applications"
    cp -r sotto_linux "$out/lib/sotto/"
    makeWrapper ${python}/bin/python3 "$out/bin/sotto-linux" \
      --prefix PYTHONPATH : "$out/lib/sotto" --add-flags '-m sotto_linux' \
      --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.ydotool ]} \
      --set-default YDOTOOL_SOCKET /run/ydotoold/socket
    makeWrapper ${python}/bin/python3 "$out/bin/sotto-python" \
      --prefix PYTHONPATH : "$out/lib/sotto" \
      --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.ydotool ]} \
      --set-default YDOTOOL_SOCKET /run/ydotoold/socket

    cat > "$out/share/applications/io.github.vitor_hbr.Sotto.desktop" <<EOF
    [Desktop Entry]
    Type=Application
    Name=Sotto
    Comment=Native Linux dictation
    Exec=$out/bin/sotto-linux
    Icon=audio-input-microphone
    Terminal=false
    Categories=AudioVideo;Audio;
    StartupNotify=true
    EOF

    runHook postInstall
  '';

  meta = {
    description = "Native GTK 4 Linux client for Sotto dictation";
    homepage = "https://github.com/vitor-hbr/sotto";
    mainProgram = "sotto-linux";
    platforms = pkgs.lib.platforms.linux;
  };
}

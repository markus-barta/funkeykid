{
  description = "funkeykid — Educational keyboard toy for children";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pythonDeps = ps: [
          ps.evdev
          ps.paho-mqtt
          ps.requests
        ];

        funkeykid = pkgs.stdenv.mkDerivation {
          pname = "funkeykid";
          version = "0.1.0";
          src = self;

          nativeBuildInputs = [ pkgs.makeWrapper ];

          installPhase = ''
            mkdir -p $out/bin $out/share/funkeykid/lang
            cp funkeykid.py $out/share/funkeykid/
            cp -r lang/* $out/share/funkeykid/lang/

            makeWrapper ${pkgs.python3.withPackages pythonDeps}/bin/python3 $out/bin/funkeykid \
              --add-flags "$out/share/funkeykid/funkeykid.py" \
              --set FUNKEYKID_LANG_DIR "$out/share/funkeykid/lang"
          '';
        };
      in
      {
        packages = {
          default = funkeykid;
          funkeykid = funkeykid;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages pythonDeps)
          ];
        };
      }
    );
}

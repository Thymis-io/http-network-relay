{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/release-24.05";
    poetry2nix = {
      url = "github:nix-community/poetry2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, poetry2nix, ... }:
  let
    eachSystem = nixpkgs.lib.genAttrs ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
  in
  {
    packages = eachSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        inherit (poetry2nix.lib.mkPoetry2Nix { inherit pkgs; }) mkPoetryApplication;
      in
      {
        default = mkPoetryApplication {
          name = "http-network-relay";
          projectDir = ./.;
          preferWheels = true;
          groups = [ ];
          python = pkgs.python312;
        };
      }
    );
  };
}
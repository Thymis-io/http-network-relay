{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-24.11";
    poetry2nix = {
      url = "github:Thymis-io/poetry2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, poetry2nix, ... }:
    let
      eachSystem = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
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
            python = pkgs.python313;
            checkPhase = ''
              runHook preCheck
              pytest --session-timeout=10
              runHook postCheck
            '';
          };
        }
      );
    };
}

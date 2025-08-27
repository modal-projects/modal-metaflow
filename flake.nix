{
  description = "Development environment for modal-metaflow";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Kubernetes tools
            minikube
            argo-workflows
            kubectl
          ];

          shellHook = ''
            # Export Argo Workflows version
            export ARGO_WORKFLOWS_VERSION="v${pkgs.argo-workflows.version}"
            
            # Activate uv virtual environment if it exists
            if [ -d ".venv" ]; then
              echo "Activating uv virtual environment..."
              source .venv/bin/activate
            else
              echo "No .venv found. Run 'uv sync' to create one."
            fi
          '';
        };
      });
}

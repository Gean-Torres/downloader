#!/bin/bash
# deploy.sh - Rootless deployment script for DLPOD
set -e

APP_NAME="dlpod"
CONFIG_DIR="$HOME/.config/$APP_NAME"
SERVICE_DIR="$HOME/.config/systemd/user"
DOWNLOAD_DIR="/srv/Downloads/media"
PODMAN_BIN=$(command -v podman)

INSTALL_SYSTEMD=false
for arg in "$@"; do
    if [ "$arg" == "--systemd" ]; then
        INSTALL_SYSTEMD=true
    fi
done

if [ -z "$PODMAN_BIN" ]; then
    echo "Error: podman not found. Please install podman."
    exit 1
fi

echo "--- Building Podman image ---"
$PODMAN_BIN build -t dlpod:latest -f app/Containerfile app/

echo "--- Preparing directories ---"
mkdir -p "$CONFIG_DIR"
mkdir -p "$DOWNLOAD_DIR"

echo "--- Patching and installing configuration ---"
# 1. Patch dlpod-pod.yaml: Set host download path to user's Downloads folder
YAML_PATH="$CONFIG_DIR/dlpod-pod.yaml"
sed "s|path: /srv/Downloads/media|path: $DOWNLOAD_DIR|g" dlpod-pod.yaml > "$YAML_PATH"

echo "--- Updating running pod ---"
if $PODMAN_BIN pod exists "$APP_NAME"; then
    echo "Existing pod found. Replacing..."
    $PODMAN_BIN pod rm -f "$APP_NAME"
fi
echo "Starting pod..."
$PODMAN_BIN kube play "$YAML_PATH"

if [ "$INSTALL_SYSTEMD" = true ]; then
    echo "--- Installing systemd service ---"
    mkdir -p "$SERVICE_DIR"
    # 2. Patch dlpod.service: 
    #    - Update paths from /opt/dlpod to the user's config dir
    #    - Update podman path to the one found on this system
    #    - Change WantedBy from multi-user.target to default.target (required for --user services)
    sed -e "s|/opt/dlpod/dlpod-pod.yaml|$CONFIG_DIR/dlpod-pod.yaml|g" \
        -e "s|/usr/bin/podman|$PODMAN_BIN|g" \
        -e "s|multi-user.target|default.target|g" \
        dlpod.service > "$SERVICE_DIR/dlpod.service"

    systemctl --user daemon-reload
    echo "Systemd service installed at $SERVICE_DIR/dlpod.service"
    echo "To start it:  systemctl --user start dlpod"
    echo "To enable:    systemctl --user enable dlpod"
else
    echo "--- Systemd service installation skipped ---"
    echo "You can now manage the pod using Cockpit Podman or manually:"
    echo "  $PODMAN_BIN kube play $CONFIG_DIR/dlpod-pod.yaml"
fi

echo ""
echo "===================================================="
echo " Deployment Preparation Complete!"
echo "===================================================="
echo " Image: dlpod:latest"
echo " YAML:  $CONFIG_DIR/dlpod-pod.yaml"
echo " Downloads: $DOWNLOAD_DIR"
echo " Web UI (once started): http://localhost:8765"
echo "===================================================="

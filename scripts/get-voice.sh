#!/usr/bin/env bash
# Download the neural voice. ~340 MB, Apache-2.0, runs locally on CPU.
#
# Without it the studio falls back to the browser's built-in speech synthesis, which works
# everywhere and sounds like a train station announcement. The gateway reports which engine is
# actually loaded, and the UI says so, so the fallback is never silent.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/services/gateway/models"
BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

mkdir -p "$DIR"
echo "downloading Kokoro-82M into $DIR ..."
curl -L --retry 3 -o "$DIR/kokoro-v1.0.onnx" "$BASE/kokoro-v1.0.onnx"
curl -L --retry 3 -o "$DIR/voices-v1.0.bin"  "$BASE/voices-v1.0.bin"
echo "done. restart the gateway and the studio will report 'neural voice'."

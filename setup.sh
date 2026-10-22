#!/usr/bin/env bash
# Instala o runtime da skill youtube-clips: venv Python, modelo de rosto e pastas de configuração.
# Idempotente: pode rodar de novo para conferir ou reparar.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${YOUTUBE_CLIPS_CONFIG:-$HOME/.config/youtube-clips}"
# config.env pode mudar o diretório do runtime (YOUTUBE_CLIPS_HOME)
for f in "$CONFIG_DIR/config.env" "$SKILL_DIR/config.env"; do
  [ -f "$f" ] && eval "$(grep -E '^YOUTUBE_CLIPS_HOME=' "$f" | sed 's/^/export /')" || true
done
HOME_DIR="${YOUTUBE_CLIPS_HOME:-$HOME/.local/share/youtube-clips}"
YUNET_URL="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"

ok() { printf '  \033[32mok\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33m!!\033[0m  %s\n' "$1"; }

echo "== Dependências do sistema"
command -v ffmpeg >/dev/null && ffmpeg -hide_banner -filters 2>/dev/null | grep " subtitles " >/dev/null \
  && ok "ffmpeg com libass" || warn "instale ffmpeg com libass (ex.: sudo apt install ffmpeg)"
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
  && ok "python3 >= 3.10" || { warn "instale python3 >= 3.10"; exit 1; }
if command -v yt-dlp >/dev/null || [ -x "$HOME/.local/bin/yt-dlp" ]; then ok "yt-dlp"; else
  warn "instale yt-dlp: 'uv tool install yt-dlp' ou 'pipx install yt-dlp'"; fi
command -v deno >/dev/null || [ -x "$HOME/.local/bin/deno" ] && ok "deno (desafios JS do YouTube)" \
  || warn "instale o deno (https://deno.land): o yt-dlp precisa dele para o YouTube"

echo "== Runtime em $HOME_DIR"
mkdir -p "$HOME_DIR/models"
if [ ! -x "$HOME_DIR/.venv/bin/python" ]; then
  if command -v uv >/dev/null; then uv venv -q "$HOME_DIR/.venv" --python 3.12 || uv venv -q "$HOME_DIR/.venv"
  else python3 -m venv "$HOME_DIR/.venv"; fi
fi
if command -v uv >/dev/null; then
  uv pip install -q --python "$HOME_DIR/.venv/bin/python" faster-whisper opencv-python-headless numpy
else
  "$HOME_DIR/.venv/bin/pip" install -q --upgrade pip faster-whisper opencv-python-headless numpy
fi
ok "venv com faster-whisper e opencv"
[ -s "$HOME_DIR/models/face_detection_yunet_2023mar.onnx" ] \
  || curl -fsSL -o "$HOME_DIR/models/face_detection_yunet_2023mar.onnx" "$YUNET_URL"
ok "modelo de rosto YuNet"

echo "== Configuração em $CONFIG_DIR"
mkdir -p "$CONFIG_DIR" && chmod 700 "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/.env" ]; then
  cp "$SKILL_DIR/env.example" "$CONFIG_DIR/.env" && chmod 600 "$CONFIG_DIR/.env"
  warn "criei $CONFIG_DIR/.env: preencha GROQ_API_KEY e o proxy (veja o README)"
else
  ok "$CONFIG_DIR/.env já existe (não alterei)"
fi
echo
echo "Pronto. Teste: python3 $SKILL_DIR/scripts/clips.py prepare <arquivo-ou-url> --out /tmp/teste-clips"

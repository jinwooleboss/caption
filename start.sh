#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$HOME/captionrenamer"
if [ ! -f .env ]; then
  echo "Fichier .env introuvable."
  exit 1
fi
set -a
. ./.env
set +a
if [ -z "${BOT_TOKEN:-}" ]; then
  echo "Token absent. Modifie ~/captionrenamer/.env puis relance ./start.sh"
  exit 1
fi
exec python bot.py
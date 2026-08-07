#!/bin/bash
# Vigía de Red de Acopio: si un bot se cae (o vuelve), avisa al operador por Telegram.
# Corre cada 5 min por cron (root). Solo avisa en CAMBIOS de estado (no spamea).
STATE=/opt/angeles/.health_state
ENVF=/opt/angeles/.env
TOK=$(grep '^APOYO_BOT_TOKEN=' $ENVF | cut -d= -f2- | tr -d '"')
VER=$(grep '^WHATSAPP_VERIFY_TOKEN=' $ENVF | cut -d= -f2- | tr -d '"')
ADMIN=$(grep '^ALERTAS_TG_IDS=' $ENVF | cut -d= -f2- | cut -d, -f1 | tr -d '"')

FAIL=""
systemctl is-active --quiet angeles-wa.service    || FAIL="$FAIL wa-bot:caído"
systemctl is-active --quiet angeles-apoyo.service || FAIL="$FAIL tg-bot:caído"
R=$(curl -s -m 10 "http://127.0.0.1:8091/wa/webhook?hub.mode=subscribe&hub.verify_token=$VER&hub.challenge=ping" 2>/dev/null)
[ "$R" = "ping" ] || FAIL="$FAIL webhook-wa:no-responde"

NOW=${FAIL:-ok}
PREV=$(cat $STATE 2>/dev/null)
if [ "$NOW" != "$PREV" ]; then
  echo "$NOW" > $STATE
  if [ "$NOW" = "ok" ]; then
    MSG="🟢 Red de Acopio: todos los servicios recuperados"
  else
    MSG="🔴 Red de Acopio — ALERTA:$FAIL"
  fi
  curl -s -m 10 "https://api.telegram.org/bot$TOK/sendMessage" \
    --data-urlencode "chat_id=$ADMIN" --data-urlencode "text=$MSG" >/dev/null
fi

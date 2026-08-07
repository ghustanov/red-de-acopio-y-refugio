"""
Bot de moderación (servicio que corre siempre).
Escucha los toques de los botones que reciben los moderadores en su grupo de
Telegram y, con UN toque, actualiza el estado de un desaparecido y regenera la web.

Seguridad: solo acepta toques que vengan del grupo de moderadores configurado.
"""
import html
import json
import time
import urllib.parse
import urllib.request

import db
import build_site
from config import TELEGRAM_BOT_TOKEN, GRUPO_MODERADORES

API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
ETIQUETAS = {"aparecio": "✅ Apareció", "fallecida": "🕯️ Fallecida", "buscando": "🟡 Buscando"}


def _tg(metodo, **params):
    payload = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
               for k, v in params.items() if v is not None}
    data = urllib.parse.urlencode(payload).encode()
    with urllib.request.urlopen(f"{API}/{metodo}", data=data, timeout=70) as r:
        return json.load(r)


def manejar(cq):
    data = cq.get("data", "")
    msg = cq.get("message", {}) or {}
    chat_id = msg.get("chat", {}).get("id")
    # Solo aceptamos toques desde el grupo de moderadores
    if str(chat_id) != str(GRUPO_MODERADORES):
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="No autorizado")
        return

    quien = cq.get("from", {}).get("first_name", "moderador")
    try:
        accion, tg_id, estado = data.split("|")
    except ValueError:
        return

    base = msg.get("text", "")
    if accion == "ign":
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="Reporte ignorado")
        _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
            text=base + f"\n\n❌ Ignorado por {quien}")
        return

    if accion == "set":
        db.marcar_persona(int(tg_id), estado)
        build_site.construir()
        _tg("answerCallbackQuery", callback_query_id=cq["id"], text="Actualizado ✅")
        _tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
            text=base + f"\n\n{ETIQUETAS.get(estado, estado)} — confirmado por {quien}")


def main():
    print("Bot de moderación escuchando…")
    offset = None
    while True:
        try:
            resp = _tg("getUpdates", offset=offset, timeout=60,
                       allowed_updates=["callback_query"])
        except Exception as e:
            print("error getUpdates:", e)
            time.sleep(5)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if cq:
                try:
                    manejar(cq)
                except Exception as e:
                    print("error manejando callback:", e)


if __name__ == "__main__":
    main()

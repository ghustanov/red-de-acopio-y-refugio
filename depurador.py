# -*- coding: utf-8 -*-
"""
depurador.py — Cruce periódico de desaparecidos reportados en Red de Acopio
contra los registros comunitarios externos (fuentes.py).

Para cada pedido ACTIVO de categoría 'desaparecido' (con el nombre de la persona
buscada en desaparecidos_reportes), consulta las fuentes; si la persona aparece
como ingresada/rescatada/localizada/de alta/fallecida, avisa al grupo de
moderadores con botones:

    ✅ Apareció: marcar resuelto   /   👁 Mantener activo

La decisión SIEMPRE es humana (callback dcz| en bot_apoyo.py); este script jamás
cambia estados por sí solo. Cada combinación de hallazgos se avisa UNA sola vez
(hash en desaparecidos_reportes.ultimo_hash): si en una corrida futura aparecen
referencias nuevas, vuelve a avisar.

Uso (cron, como usuario del bot):
    */240 min → venv/bin/python3 depurador.py
"""

import hashlib
import json
import time
import urllib.request

import db
import fuentes
from config import APOYO_BOT_TOKEN, GRUPO_MODERADORES

PAUSA_ENTRE_AVISOS = 2   # seg — no inundar al grupo de moderadores


def _hash_refs(refs):
    base = "|".join(sorted("{}~{}~{}".format(r["nombre"], r["estado"], r["lugar"])
                           for r in refs))
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _avisar(lid, persona, sector, refs):
    import html
    lineas = ([f"🔁 <b>Depurador</b> · pedido <b>#{lid}</b>",
               f"Se busca a <b>{html.escape(persona)}</b>"
               + (f" · 📍 {html.escape(sector)}" if sector else ""),
               "Apareció en los registros comunitarios:"]
              + fuentes.resumen_referencias(refs, html_out=True)
              + ["<i>Información comunitaria, no oficial — confirmar (llamar al centro "
                 "o al reportante) antes de marcar resuelto.</i>"])
    kb = {"inline_keyboard": [[
        {"text": "✅ Apareció: marcar resuelto", "callback_data": f"dcz|{lid}|ok"},
        {"text": "👁 Mantener activo", "callback_data": f"dcz|{lid}|no"}]]}
    payload = json.dumps({
        "chat_id": GRUPO_MODERADORES, "parse_mode": "HTML",
        "text": "\n".join(lineas), "reply_markup": kb,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20)


def correr():
    if not (APOYO_BOT_TOKEN and GRUPO_MODERADORES):
        print("depurador: falta APOYO_BOT_TOKEN/GRUPO_MODERADORES, nada que hacer")
        return
    pendientes = db.desaparecidos_activos()
    print(f"depurador: {len(pendientes)} pedidos de desaparecido activos")
    avisados = 0
    for p in pendientes:
        refs = fuentes.referencias(p["persona"])
        if not refs:
            continue
        h = _hash_refs(refs)
        if h == (p.get("ultimo_hash") or ""):
            continue  # mismos hallazgos ya avisados antes
        try:
            _avisar(p["llamado_id"], p["persona"], p.get("sector") or "", refs)
            db.marcar_aviso_cruce(p["llamado_id"], h)
            avisados += 1
            time.sleep(PAUSA_ENTRE_AVISOS)
        except Exception as e:
            print(f"depurador: error avisando #{p['llamado_id']}:", e)
    print(f"depurador: {avisados} avisos enviados")


if __name__ == "__main__":
    correr()

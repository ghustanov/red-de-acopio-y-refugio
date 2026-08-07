"""Recordatorio diario de carga de inventario — Red de Acopio y Refugio.

Corre 1 vez al día (cron). A cada centro que eligió "⏰ Seguir luego" al
registrarse y AÚN no ha cargado inventario, le recuerda por su canal (Telegram
o WhatsApp). Se auto-apaga cuando el centro ya tiene stock publicado.

Nota WhatsApp: fuera de la ventana de 24h el envío libre falla (limitación de
Meta hasta tener plantillas aprobadas — Fase 4). Se registra en el log y se
reintenta al día siguiente; si el usuario vuelve a escribir, la ventana se abre.
"""
import json
import urllib.request

import db
import stock
import wa_api
from config import APOYO_BOT_TOKEN

MSG = ("⏰ Recordatorio de Red de Acopio y Refugio: aún no has cargado el inventario "
       "de tu punto. Mándame una foto de tu lista, una nota de voz o un PDF/Excel y "
       "yo lo organizo. Así donantes y refugios sabrán qué tienes. 📦")


def _tg_send(chat_id, texto):
    data = json.dumps({"chat_id": int(chat_id), "text": texto}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{APOYO_BOT_TOKEN}/sendMessage",
                                 data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("ok", False)


def main():
    con = db.conectar()
    rows = con.execute(
        "SELECT id, rep_id, canal, destino FROM recordatorios_inv WHERE activo = 1").fetchall()
    con.close()
    for row_id, rep_id, canal, destino in rows:
        rep = db.representante(rep_id)
        punto_id = (rep or {}).get("punto_id") or rep_id
        # Ya cargó, o el registro murió → apagar y no molestar más.
        if not rep or rep.get("estado") in ("rechazado", "retirado") or stock.de_punto(punto_id):
            con = db.conectar()
            con.execute("UPDATE recordatorios_inv SET activo = 0 WHERE id = ?", (row_id,))
            con.commit()
            con.close()
            continue
        try:
            if canal == "tg":
                ok = _tg_send(destino, MSG)
            else:
                ok = bool(wa_api.enviar_texto(destino, MSG))  # {} = falló (p. ej. fuera de 24h)
            print(f"recordatorio rep={rep_id} canal={canal} -> {'enviado' if ok else 'NO entregado'}")
        except Exception as e:  # noqa: BLE001
            print(f"recordatorio rep={rep_id} canal={canal} ERROR: {e}")
        con = db.conectar()
        con.execute("UPDATE recordatorios_inv SET ultimo_envio = datetime('now') WHERE id = ?", (row_id,))
        con.commit()
        con.close()


if __name__ == "__main__":
    main()

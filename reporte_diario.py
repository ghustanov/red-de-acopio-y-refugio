#!/usr/bin/env python3
"""Reporte diario de métricas de Ángeles de la Autopista → DM por Telegram.
Lo dispara angeles-reporte.timer cada mañana (8:00 America/Caracas)."""
import datetime
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

import db
from config import TELEGRAM_BOT_TOKEN

DESTINO = os.getenv("ALERTAS_TG_IDS", "").split(",")[0]  # telegram_id del operador (DM directo)
VET = ZoneInfo("America/Caracas")


def vet_date(offset=0):
    d = datetime.datetime.now(VET).date() - datetime.timedelta(days=offset)
    return d.isoformat()


def construir_mensaje():
    con = db.conectar()
    filas = con.execute(
        """SELECT v.dia, v.vistas,
                  (SELECT COUNT(*) FROM visita_fp f WHERE f.dia = v.dia) AS unicos
           FROM visitas v ORDER BY v.dia DESC LIMIT 7"""
    ).fetchall()
    total = con.execute("SELECT COALESCE(SUM(vistas), 0) FROM visitas").fetchone()[0]
    con.close()

    por_dia = {r["dia"]: r for r in filas}
    hoy, ayer = vet_date(0), vet_date(1)

    def linea(dia, etiqueta):
        r = por_dia.get(dia)
        if not r:
            return f"{etiqueta}: sin datos aún"
        return f"{etiqueta}: {r['vistas']} vistas · {r['unicos']} visitantes únicos"

    # Tendencia: únicos de ayer vs antier
    antier = vet_date(2)
    flecha = ""
    if ayer in por_dia and antier in por_dia:
        a, b = por_dia[ayer]["unicos"], por_dia[antier]["unicos"]
        if b:
            dif = round((a - b) / b * 100)
            flecha = f"  ({'📈 +' if dif >= 0 else '📉 '}{dif}% vs antier)"

    # Contexto de contenido
    try:
        centros = db.resumen_centros()
        recib = sum(1 for c in centros if c.get("estado") == "disponible")
        centros_txt = f"\n📦 Centros de acopio: {recib}/{len(centros)} recibiendo"
    except Exception:
        centros_txt = ""

    tabla = "\n".join(
        f"  {r['dia']}: {r['vistas']} vistas · {r['unicos']} únicos" for r in filas
    )

    return (
        "📊 *Ángeles de la Autopista — reporte diario*\n\n"
        f"🗓️ {linea(ayer, 'Ayer')}{flecha}\n"
        f"🌅 {linea(hoy, 'Hoy (parcial)')}\n"
        f"👁️ Total acumulado: {total} vistas"
        f"{centros_txt}\n\n"
        f"_Últimos 7 días:_\n{tabla}"
    )


def enviar(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = json.dumps(
        {"chat_id": DESTINO, "text": texto, "parse_mode": "Markdown"}
    ).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read().decode())
    if not resp.get("ok"):
        print("ERROR Telegram:", resp, file=sys.stderr)
        sys.exit(1)
    print("OK enviado")


if __name__ == "__main__":
    enviar(construir_mensaje())

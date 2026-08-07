"""Auditoría de consumo de tokens. Mide con llamadas reales de muestra."""
import os
import db
import classifier
from config import MODELO, WEB_DIR

client = classifier.client

# USD por millón de tokens (entrada, salida)
PRECIOS = {"claude-opus-4-8": (5, 25), "claude-haiku-4-5": (1, 5)}


def medir(content):
    r = client.messages.parse(
        model=MODELO, max_tokens=1500, system=classifier.SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=classifier.Clasificacion,
    )
    return r.usage.input_tokens, r.usage.output_tokens


con = db.conectar()
total = con.execute("SELECT COUNT(*) FROM mensajes").fetchone()[0]
con_img = con.execute("SELECT COUNT(*) FROM mensajes WHERE foto IS NOT NULL").fetchone()[0]
sin_img = total - con_img

textos = [r["texto_original"] for r in con.execute(
    "SELECT texto_original FROM mensajes WHERE foto IS NULL AND length(texto_original)>30 ORDER BY RANDOM() LIMIT 3")]
fotos = [r["foto"] for r in con.execute(
    "SELECT foto, texto_original FROM mensajes WHERE foto IS NOT NULL ORDER BY RANDOM() LIMIT 2")]
fotos_full = con.execute(
    "SELECT foto, texto_original FROM mensajes WHERE foto IS NOT NULL ORDER BY RANDOM() LIMIT 2").fetchall()

print(f"Modelo actual: {MODELO}")
print(f"Mensajes en base: {total}  (con foto: {con_img}, solo texto: {sin_img})\n")

# Texto solo
it, ot = [], []
for t in textos:
    i, o = medir(t)
    it.append(i); ot.append(o)
in_text = sum(it) // len(it); out_text = sum(ot) // len(ot)
print(f"📝 Mensaje SOLO TEXTO  → entrada ~{in_text} tok | salida ~{out_text} tok")

# Con imagen (ya reducida por _bloque_imagen)
ii, oo, tam_orig, tam_red = [], [], [], []
for f in fotos_full:
    ruta = WEB_DIR / f["foto"]
    if not ruta.exists():
        continue
    tam_orig.append(os.path.getsize(ruta))
    blk = classifier._bloque_imagen(str(ruta))
    tam_red.append(len(blk["source"]["data"]) * 3 // 4)  # bytes aprox del base64
    content = [blk, {"type": "text", "text": f["texto_original"] or "(sin texto)"}]
    i, o = medir(content)
    ii.append(i); oo.append(o)
in_img = sum(ii) // len(ii); out_img = sum(oo) // len(oo)
print(f"🖼️  Mensaje CON FOTO   → entrada ~{in_img} tok | salida ~{out_img} tok")
print(f"    Imagen original ~{sum(tam_orig)//len(tam_orig)//1024} KB → reducida ~{sum(tam_red)//len(tam_red)//1024} KB\n")

# Proyección de una corrida completa
in_run = sin_img * in_text + con_img * in_img
out_run = sin_img * out_text + con_img * out_img


def costo(modelo, ent, sal):
    pi, ps = PRECIOS[modelo]
    return ent / 1e6 * pi + sal / 1e6 * ps


print(f"=== COSTO de procesar {total} mensajes (una vez) ===")
for mod in ("claude-opus-4-8", "claude-haiku-4-5"):
    print(f"  {mod:20} ${costo(mod, in_run, out_run):.3f}")

print(f"\n=== COSTO por cada 100 mensajes nuevos ===")
in100 = (in_text + in_img) / 2 * 100
out100 = (out_text + out_img) / 2 * 100
for mod in ("claude-opus-4-8", "claude-haiku-4-5"):
    print(f"  {mod:20} ${costo(mod, in100, out100):.3f}")

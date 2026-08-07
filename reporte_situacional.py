# -*- coding: utf-8 -*-
"""
Renderer del REPORTE SITUACIONAL de un refugio — Red de Acopio y Refugio.
Diseño propio con la identidad de redacopioyrefugio.com (paleta navy/teal del
sitio + Poppins). Genera un PNG 1080x1300 con Pillow a partir de un dict.
Pensado para portarse al VPS (angeles) leyendo de la DB; corre con datos
de muestra al final del archivo.
"""
import os
import re
import sqlite3
from PIL import Image, ImageDraw, ImageFont

# --- rutas SIEMPRE relativas a esta carpeta: así cada instancia (producción,
# sandbox, Mac) usa su propia DB, fuentes y logo, sin cruzarse ---
_AQUI = os.path.dirname(os.path.abspath(__file__))


def _primero(*rutas):
    return next((r for r in rutas if os.path.exists(r)), rutas[-1])


_FONTS_DEF = _primero(os.path.join(_AQUI, "fonts"),
                      "/Users/ghus/Documents/angeles-dashboard/fonts")
_LOGO_DEF = _primero(os.path.join(_AQUI, "web", "icon-512.png"),
                     os.path.join(_AQUI, "brand", "icon-512.png"))
_DB_DEF = os.path.join(_AQUI, "angeles.db")
FONT_DIR = os.environ.get("REPORTE_FONT_DIR", _FONTS_DEF)
F_REG = os.path.join(FONT_DIR, "Poppins-Regular.ttf")
F_SEMI = os.path.join(FONT_DIR, "Poppins-SemiBold.ttf")
LOGO = os.environ.get("REPORTE_LOGO", _LOGO_DEF)
DB_PATH = os.environ.get("REPORTE_DB", _DB_DEF)

# ---------- paleta oficial del sitio (styles.css) ----------
NAVY   = "#003C67"   # --navy
TEAL   = "#1E9C94"   # --teal
TINTA  = "#0f1f33"   # --tinta
SUAVE  = "#5b6b80"   # --suave
LINEA  = "#e2e8f0"   # --linea
BG     = "#eef1f5"   # --bg
VERDE  = "#15803d"   # --disponible
VERDE_BG = "#f0fdf4"
ROJO   = "#dc2626"   # --lleno
ROJO_BG  = "#fef2f2"
NARANJA = "#ea580c"  # --rescate
TEAL_CLARO = "#9fd8d4"

W, H = 1080, 1360
M = 44


def font(size, weight="Regular"):
    path = F_REG if weight == "Regular" else F_SEMI
    return ImageFont.truetype(path, size)


def label_seccion(d, y, num, texto):
    """Etiqueta editorial: 01 — CAPACIDAD ...  con línea al costado."""
    f = font(21, "SemiBold")
    d.text((M, y), num, font=f, fill=TEAL)
    x = M + d.textlength(num + "  ", font=f)
    d.text((x, y), texto.upper(), font=f, fill=NAVY)
    x2 = x + d.textlength(texto.upper(), font=f) + 18
    d.line([x2, y + 12, W - M, y + 12], fill=LINEA, width=2)
    return y + 44


def check(d, cx, cy, r, ok):
    color = VERDE if ok else ROJO
    if ok:
        d.line([cx - r * .6, cy + r * .05, cx - r * .12, cy + r * .55], fill=color, width=4)
        d.line([cx - r * .12, cy + r * .55, cx + r * .65, cy - r * .45], fill=color, width=4)
    else:
        d.line([cx - r * .5, cy - r * .5, cx + r * .5, cy + r * .5], fill=color, width=4)
        d.line([cx - r * .5, cy + r * .5, cx + r * .5, cy - r * .5], fill=color, width=4)


def pegar_logo(img, cx, cy, size):
    """Pega el logo oficial (trazo blanco) centrado en (cx, cy).
    Usa la luminancia del icono como máscara para soltar el fondo navy."""
    logo = Image.open(LOGO).convert("L").resize((size, size), Image.LANCZOS)
    # el fondo navy del icono ronda L≈50; se anula y se conserva el antialias del trazo
    logo = logo.point(lambda v: 0 if v < 70 else min(255, int((v - 70) * 255 / 185)))
    blanco = Image.new("RGB", (size, size), "white")
    img.paste(blanco, (int(cx - size / 2), int(cy - size / 2)), mask=logo)


def render(data, out_path):
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    # ================= banda de marca =================
    d.rectangle([0, 0, W, 148], fill=NAVY)
    d.rectangle([0, 148, W, 154], fill=TEAL)
    pegar_logo(img, M + 40, 74, 88)
    d.text((M + 100, 44), "RED DE ACOPIO Y REFUGIO", font=font(19, "SemiBold"),
           fill=TEAL_CLARO, anchor="lm")
    d.text((M + 100, 84), "Reporte situacional", font=font(40, "SemiBold"),
           fill="white", anchor="lm")
    # fecha/hora + estado a la derecha
    fecha_txt = f"{data['fecha']}  ·  {data['hora']}"
    ff = font(19, "Regular")
    d.text((W - M, 46), fecha_txt, font=ff, fill=TEAL_CLARO, anchor="rm")
    libre = data["disponibilidad"]
    if libre is None:
        estado, ecol = "SIN DATO DE CUPO", "#94a3b8"   # --desconocido del sitio
    elif libre > 0:
        estado, ecol = "CUPOS DISPONIBLES", VERDE
    else:
        estado, ecol = "CUPO LLENO", ROJO
    ef = font(18, "SemiBold")
    ew = d.textlength(estado, font=ef) + 44
    d.rounded_rectangle([W - M - ew, 74, W - M, 112], radius=19, fill=ecol)
    d.text((W - M - ew / 2, 93), estado, font=ef, fill="white", anchor="mm")

    # ================= refugio =================
    y = 190
    d.text((M, y), data["refugio"], font=font(33, "SemiBold"), fill=TINTA, anchor="lm")
    ub = "  ·  ".join(p for p in (data["ubicacion_1"], data["ubicacion_2"]) if p)
    fu = font(20, "Regular")
    while d.textlength(ub, font=fu) > W - 2 * M and len(ub) > 8:
        ub = ub[:-2].rstrip() + "…"
    d.text((M, y + 40), ub, font=fu, fill=SUAVE, anchor="lm")
    d.line([M, y + 68, M + 76, y + 68], fill=TEAL, width=5)

    # ================= 01 capacidad =================
    y = label_seccion(d, y + 96, "01", "Capacidad y ocupación")
    kh = 148
    gap = 18
    kw = (W - 2 * M - 3 * gap) / 4
    kpis = [
        ("Capacidad máxima", data["cap_max"], NAVY),
        ("Capacidad instalada", data["cap_inst"], TEAL),
        ("Personas hoy", data["total_personas"], VERDE),
        ("Cupos libres", data["disponibilidad"], NARANJA),
    ]
    for i, (lab, val, color) in enumerate(kpis):
        x0 = M + i * (kw + gap)
        d.rounded_rectangle([x0, y, x0 + kw, y + kh], radius=16, fill="white",
                            outline=LINEA, width=2)
        d.rounded_rectangle([x0, y, x0 + kw, y + 10], radius=5, fill=color)
        d.text((x0 + 20, y + 62), str(val if val is not None else "—"),
               font=font(52, "SemiBold"), fill=color, anchor="lm")
        d.text((x0 + 20, y + 112), lab, font=font(17, "Regular"), fill=SUAVE, anchor="lm")
    y += kh + 22

    # barra de ocupación
    if data.get("cap_inst"):
        pct = min(1.0, data["total_personas"] / data["cap_inst"])
        d.text((M, y + 10), "Ocupación", font=font(18, "SemiBold"), fill=TINTA, anchor="lm")
        ptxt = f"{round(pct * 100)} %  ({data['total_personas']} de {data['cap_inst']})"
        d.text((W - M, y + 10), ptxt, font=font(18, "SemiBold"), fill=TEAL, anchor="rm")
        by = y + 30
        d.rounded_rectangle([M, by, W - M, by + 16], radius=8, fill=BG)
        d.rounded_rectangle([M, by, M + (W - 2 * M) * pct, by + 16], radius=8, fill=TEAL)
        y = by + 44
    else:
        y += 10

    # ================= 02 población =================
    y = label_seccion(d, y, "02", "Población por edad y sexo")
    # matriz (izquierda) + panel (derecha)
    mw = 660
    mx1 = M + mw
    filas = data["matriz"]          # [(etiqueta, m, f), ...]
    tot_m = sum(r[1] for r in filas)
    tot_f = sum(r[2] for r in filas)
    rh = 46
    mh = rh * (len(filas) + 2) + 16
    d.rounded_rectangle([M, y, mx1, y + mh], radius=16, fill="white", outline=LINEA, width=2)
    cols = [M + 24, mx1 - 260, mx1 - 165, mx1 - 60]   # etiqueta, M, F, total
    hy = y + 8 + rh / 2
    for cx, t in zip(cols[1:], ("Masc.", "Fem.", "Total")):
        d.text((cx, hy), t, font=font(17, "SemiBold"), fill=SUAVE, anchor="mm")
    for i, (lab, m, f_) in enumerate(filas):
        ry = y + 8 + rh * (i + 1)
        if i % 2 == 0:
            d.rounded_rectangle([M + 12, ry, mx1 - 12, ry + rh], radius=10, fill=BG)
        cy = ry + rh / 2
        d.text((cols[0], cy), lab, font=font(19, "Regular"), fill=TINTA, anchor="lm")
        d.text((cols[1], cy), f"{m:02d}", font=font(20, "SemiBold"), fill=NAVY, anchor="mm")
        d.text((cols[2], cy), f"{f_:02d}", font=font(20, "SemiBold"), fill=NAVY, anchor="mm")
        d.text((cols[3], cy), f"{m + f_:02d}", font=font(20, "SemiBold"), fill=TEAL, anchor="mm")
    ry = y + 8 + rh * (len(filas) + 1)
    d.line([M + 20, ry + 2, mx1 - 20, ry + 2], fill=LINEA, width=2)
    cy = ry + rh / 2 + 4
    d.text((cols[0], cy), "Total", font=font(20, "SemiBold"), fill=TINTA, anchor="lm")
    d.text((cols[1], cy), f"{tot_m}", font=font(21, "SemiBold"), fill=NAVY, anchor="mm")
    d.text((cols[2], cy), f"{tot_f}", font=font(21, "SemiBold"), fill=NAVY, anchor="mm")
    d.text((cols[3], cy), f"{tot_m + tot_f}", font=font(23, "SemiBold"), fill=TEAL, anchor="mm")

    # panel derecho: familias + condiciones
    px0 = mx1 + 18
    d.rounded_rectangle([px0, y, W - M, y + mh], radius=16, fill=NAVY)
    pcx = (px0 + W - M) / 2
    d.text((pcx, y + 40), "NÚCLEOS FAMILIARES", font=font(16, "SemiBold"),
           fill=TEAL_CLARO, anchor="mm")
    d.text((pcx, y + 96), str(data["familias"]), font=font(64, "SemiBold"),
           fill="white", anchor="mm")
    prom = data["total_personas"] / data["familias"] if data["familias"] else 0
    d.text((pcx, y + 148), f"≈ {prom:.1f} personas por familia",
           font=font(15, "Regular"), fill=TEAL_CLARO, anchor="mm")
    d.line([px0 + 28, y + 178, W - M - 28, y + 178], fill="#1a5580", width=2)
    extras = [("Con discapacidad", data["discapacidad"]),
              ("Embarazadas", data["embarazadas"])]
    for i, (lab, val) in enumerate(extras):
        ey = y + 212 + i * 44
        d.text((px0 + 28, ey), lab, font=font(17, "Regular"), fill="white", anchor="lm")
        d.text((W - M - 28, ey), f"{val:02d}", font=font(21, "SemiBold"),
               fill=TEAL_CLARO, anchor="rm")
    y += mh + 30

    # ================= 03 servicios =================
    y = label_seccion(d, y, "03", "Servicios del refugio")
    chips = data["servicios"]       # [(etiqueta, True/False, detalle opcional)]
    per_row = 3
    cw = (W - 2 * M - (per_row - 1) * 14) / per_row
    ch = 52
    for i, (lab, ok, det) in enumerate(chips):
        r, c = divmod(i, per_row)
        x0 = M + c * (cw + 14)
        y0 = y + r * (ch + 12)
        d.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=26,
                            fill=(VERDE_BG if ok else ROJO_BG),
                            outline=(VERDE if ok else ROJO), width=2)
        check(d, x0 + 30, y0 + ch / 2, 11, ok)
        texto = lab if not det else f"{lab} · {det}"
        d.text((x0 + 52, y0 + ch / 2), texto, font=font(17, "SemiBold"),
               fill=(VERDE if ok else ROJO), anchor="lm")
    rows = (len(chips) + per_row - 1) // per_row
    y += rows * (ch + 12) + 20

    # ================= pie =================
    d.line([M, y, W - M, y], fill=LINEA, width=2)
    d.text((M, y + 26), "redacopioyrefugio.com", font=font(17, "SemiBold"),
           fill=TEAL, anchor="lm")
    d.text((W - M, y + 26), data.get("pie", "Reporte generado automáticamente"),
           font=font(15, "Regular"), fill=SUAVE, anchor="rm")

    img.save(out_path)
    return out_path


# ================= CAPA DE DATOS (angeles.db) =================
# Módulo autocontenido a propósito: no importa db.py ni nada del pipeline del
# canal, para que el subsistema de refugios pueda migrarse/entregarse aparte.

RANGOS = [("am", "Adultos mayores (60+)"), ("ad", "Adultos (18–59)"),
          ("adol", "Adolescentes (12–17)"), ("nino", "Niños y niñas (2–11)"),
          ("lact", "Lactantes (menos de 2)")]

FICHA_SERVICIOS = [  # (campo en representantes, etiqueta del chip, lleva número)
    ("camas", "Camas / literas", True), ("banos", "Baños", True),
    ("duchas", "Duchas", True), ("cocina", "Cocina y equipos", False),
    ("agua_potable", "Agua potable", False), ("electricidad", "Electricidad", False),
    ("planta", "Planta eléctrica", False), ("refrigeracion", "Refrigeración", False),
    ("almacenamiento", "Almacenamiento", False), ("acceso_vehiculos", "Acceso vehículos", False),
    ("accesibilidad", "Accesibilidad", False),
]


def _num(txt):
    """Primer entero razonable en un texto libre de la ficha, o None."""
    if txt is None:
        return None
    if isinstance(txt, (int, float)):
        return int(txt)
    m = re.search(r"\d{1,4}", str(txt))
    return int(m.group()) if m else None


def _si(txt):
    """True/False/None desde texto libre ('Sí', '2', 'No posee', 'no aplica'…)."""
    if txt is None:
        return None
    t = str(txt).strip().lower()
    if not t or t.startswith(("no aplica", "n/a")):
        return None
    if t.startswith(("no", "0")):
        return False
    if t.startswith(("sí", "si", "yes")) or _num(t):
        return True
    return None


def datos_de_db(rep_id, con=None, cuando=None):
    """Arma el dict del reporte para un refugio (representantes.id)."""
    import datetime
    cerrar = con is None
    con = con or sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rep = con.execute("SELECT * FROM representantes WHERE id=?", (rep_id,)).fetchone()
    if not rep:
        raise ValueError(f"No existe representante id={rep_id}")
    clave = rep["punto_id"] or rep["id"]   # agrupador del punto

    # censo activo
    filas = []
    disc = emb = 0
    for r, etiqueta in RANGOS:
        row = con.execute("""
            SELECT COALESCE(SUM(CASE WHEN m.sexo='M' THEN 1 ELSE 0 END),0) m,
                   COALESCE(SUM(CASE WHEN m.sexo='F' THEN 1 ELSE 0 END),0) f
            FROM miembros m JOIN familias fa ON fa.id=m.familia_id
            WHERE fa.punto_id=? AND fa.fecha_egreso IS NULL
              AND m.activo=1 AND m.rango_edad=?""", (clave, r)).fetchone()
        filas.append((etiqueta, row["m"], row["f"]))
    ex = con.execute("""
        SELECT COALESCE(SUM(m.discapacidad),0) d, COALESCE(SUM(m.embarazada),0) e,
               COUNT(DISTINCT fa.id) fam
        FROM familias fa LEFT JOIN miembros m
             ON m.familia_id=fa.id AND m.activo=1
        WHERE fa.punto_id=? AND fa.fecha_egreso IS NULL""", (clave,)).fetchone()
    total = sum(m + f for _, m, f in filas)

    cap_max = _num(rep["aforo"])
    cap_inst = _num(rep["camas"]) or cap_max
    servicios = []
    for campo, etiqueta, con_num in FICHA_SERVICIOS:
        ok = _si(rep[campo])
        if ok is None:
            continue
        det = str(_num(rep[campo])) if (con_num and ok and _num(rep[campo])) else None
        servicios.append((etiqueta, ok, det))

    ahora = cuando or datetime.datetime.now()
    ub = (rep["ubicacion"] or "").split("http")[0].strip(" ,·")
    partes = [p.strip() for p in ub.split(",") if p.strip()]
    data = {
        "refugio": rep["punto"] or "Refugio",
        "ubicacion_1": ", ".join(partes[-2:]) if len(partes) > 1 else ub,
        "ubicacion_2": ", ".join(partes[:-2])[:60] if len(partes) > 2 else "",
        "hora": ahora.strftime("%I:%M %p").lstrip("0").lower().replace("am", "a. m.").replace("pm", "p. m."),
        "fecha": ahora.strftime("%d/%m/%Y"),
        "cap_max": cap_max, "cap_inst": cap_inst,
        "total_personas": total,
        "disponibilidad": (cap_inst - total) if cap_inst else None,
        "matriz": filas, "familias": ex["fam"],
        "discapacidad": ex["d"], "embarazadas": ex["e"],
        "servicios": servicios,
        "pie": "Reporte generado automáticamente",
    }
    if cerrar:
        con.close()
    return data


def guardar_snapshot(rep_id, data, png_path, con=None):
    """Congela el reporte del día en reportes_situacionales (1 por punto y fecha)."""
    import json
    import datetime
    cerrar = con is None
    con = con or sqlite3.connect(DB_PATH)
    m = {r: (mm, ff) for (lab, mm, ff), (r, _) in zip(data["matriz"], RANGOS)}
    con.execute("""
        INSERT OR REPLACE INTO reportes_situacionales
        (punto_id, fecha, hora, total_personas, familias,
         am_m, am_f, ad_m, ad_f, adol_m, adol_f, nino_m, nino_f, lact_m, lact_f,
         discapacidad, embarazadas, cap_max, cap_inst, servicios_json, png_path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rep_id, datetime.date.today().isoformat(), data["hora"],
         data["total_personas"], data["familias"],
         *m["am"], *m["ad"], *m["adol"], *m["nino"], *m["lact"],
         data["discapacidad"], data["embarazadas"],
         data["cap_max"], data["cap_inst"],
         json.dumps(data["servicios"], ensure_ascii=False), png_path))
    con.commit()
    if cerrar:
        con.close()


# ================= DATOS DE MUESTRA =================
SAMPLE = {
    "refugio": "Espacio Terapéutico Nuestra América",
    "ubicacion_1": "Catia, Distrito Capital",
    "ubicacion_2": "U.E. Nuestra América, calle El Cristo",
    "hora": "09:30 p. m.",
    "fecha": "13/07/2026",
    "cap_max": 150, "cap_inst": 150, "total_personas": 128, "disponibilidad": 22,
    # matriz edad×sexo: (etiqueta, masculinos, femeninos) — misma lógica de la planilla
    "matriz": [
        ("Adultos mayores (60+)", 5, 5),
        ("Adultos (18–59)", 40, 41),
        ("Adolescentes (12–17)", 3, 9),
        ("Niños y niñas (2–11)", 11, 9),
        ("Lactantes (menos de 2)", 2, 3),
    ],
    "familias": 45,
    "discapacidad": 3,
    "embarazadas": 2,
    "servicios": [
        ("Literas y colchones", True, None),
        ("Baños", True, "4"),
        ("Duchas", True, "5"),
        ("Cocina y equipos", True, None),
        ("Agua (tanques)", True, None),
        ("Agua potable", True, None),
        ("Alimentación", True, None),
        ("Electricidad", True, None),
        ("Planta eléctrica", False, None),
        ("Internet", True, None),
        ("Ambulancia", False, None),
        ("Circuito de seguridad", False, None),
    ],
    "pie": "Reporte generado automáticamente · DATOS DE MUESTRA",
}

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        # uso: reporte_situacional.py <rep_id> [salida.png]
        rid = int(sys.argv[1])
        out = sys.argv[2] if len(sys.argv) > 2 else f"/tmp/reporte_refugio_{rid}.png"
        print(render(datos_de_db(rid), out))
    else:
        out = os.path.join(_AQUI, "reporte_muestra.png")
        print(render(SAMPLE, out))

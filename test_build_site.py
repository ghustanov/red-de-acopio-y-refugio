"""Red de seguridad del refactor de build_site.py (Fase 1b).

Corre construir() y valida que la generación del sitio NO se rompa ni pierda
contenido. No compara byte-a-byte (el refactor cambia bytes a propósito): valida
invariantes estructurales. Correr con el venv:

    venv/bin/python3 test_build_site.py
"""
import json
import re
import pathlib
import build_site
from config import WEB_DIR, SITE_TITLE

ARCHIVOS = ["index.html", "sw.js", "ofrecer.html", "pedir.html",
            "aviso-legal.html", "manifest.webmanifest", "sitemap.xml",
            "styles.css", "app.js", "datos.json"]

# Claves de datos que el front necesita — ahora en datos.json (Fase 1b; antes inline).
DATOS_KEYS = ["CATS", "DATOS", "NECESIDADES", "SEGUIMIENTO", "OFERTA", "DIRECTORIO",
              "ALIADOS", "LLAMADOS", "PEDIDOS", "SISMOS", "RESUMEN", "HMETA"]


def main():
    fallos = []
    ck = lambda cond, msg: None if cond else fallos.append(msg)

    ruta, n = build_site.construir()
    ck(pathlib.Path(ruta).exists(), "no se generó index.html")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    app = (WEB_DIR / "app.js").read_text(encoding="utf-8")

    # 1) todos los archivos clave existen y no están vacíos
    for f in ARCHIVOS:
        p = WEB_DIR / f
        ck(p.exists() and p.stat().st_size > 100, f"archivo faltante/vacío: {f}")

    # 2) el index quedó como shell chico (datos y código fuera)
    ck(len(html) < 80_000, f"index.html no se aligeró: {len(html)} bytes")

    # 3) CLAVE para refactors: sin placeholders sin reemplazar (en index Y en app.js)
    ck(not re.findall(r"__[A-Z_]{3,}__", html), "placeholders sin reemplazar en index")
    ck(not re.findall(r"__[A-Z_]{3,}__", app), "placeholders sin reemplazar en app.js")

    # 4) datos externalizados a datos.json, válidos y completos
    ck("const DATOS=" not in html, "los datos siguen inline en el index")
    try:
        d = json.loads((WEB_DIR / "datos.json").read_text(encoding="utf-8"))
    except Exception as e:
        d = {}
        ck(False, f"datos.json no es JSON válido: {e}")
    for k in DATOS_KEYS:
        ck(k in d, f"datos.json: falta la clave {k}")
    ck(isinstance(d.get("DATOS"), list) and len(d["DATOS"]) > 0, "datos.json: DATOS vacío")

    # 5) el index carga assets externos + bootstrap de datos
    ck("<style>" not in html, "el CSS sigue inline")
    ck("styles.css?v=" in html, "falta el <link> a styles.css")
    ck("app.js?v=" in html, "falta la carga de app.js")
    ck('fetch("datos.json' in html, "falta el bootstrap que busca datos.json")

    # 6) app.js estático (sin valores dinámicos: el contador se lee del dato HMETA)
    ck("textContent=HMETA" in app, "app.js no lee el contador desde los datos")
    ck("let miUbic=null" in app and "function itemsTab" in app, "app.js sin el código esperado")

    # 7) identidad
    ck(SITE_TITLE in html, "falta el título del sitio")
    ck("Venezuela" in html, "falta la referencia geográfica base")

    if fallos:
        print(f"❌ {len(fallos)} FALLO(S):")
        for f in fallos:
            print("  -", f)
        raise SystemExit(1)
    kb = lambda f: (WEB_DIR / f).stat().st_size // 1024
    print(f"✅ regresión OK · index {len(html)//1024} KB · datos.json {kb('datos.json')} KB "
          f"· app.js {kb('app.js')} KB estático · {n} mensajes · sin placeholders")


if __name__ == "__main__":
    main()

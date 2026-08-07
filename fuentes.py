# -*- coding: utf-8 -*-
"""
fuentes.py — Adaptadores del buscador federado de desaparecidos/hospitalizados.

Consulta en vivo 3 fuentes comunitarias del terremoto 2026 y normaliza los
resultados a un formato único. NO copia bases ajenas: consulta al momento
(el uniprocesador se cachea 15 min porque su API solo entrega la base completa).

Fuentes (evaluadas 2026-07-03, ver memoria fuentes-buscador-desaparecidos):
  1. uniprocesador (Railway)  — base unificada de 13 portales + cruces inteligentes
  2. hospitalesenvenezuela.com — ingresados en hospitales (Supabase RPC público)
  3. redayudavenezuela.com     — desaparecidos + hospitales + salvos (POST /api/data)

Toda la información es comunitaria y NO oficial: se muestra como referencia
con fuente y fecha. Las cédulas y teléfonos se enmascaran SIEMPRE (política
de privacidad de Red de Acopio y Refugio).
"""

import json
import os
import re
import time
import unicodedata
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

TIMEOUT = 12          # seg por fuente
CACHE_TTL = 15 * 60   # 15 min para la base del uniprocesador
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_buscador")
UA = "RedAcopioYRefugio-Buscador/0.1 (+https://redacopioyrefugio.com; contacto@redacopioyrefugio.com)"

UNIPROC_BASE = "https://uniprocesador-data-listas-production.up.railway.app"
HOSP_RPC = "https://ozuxfepfkvnxkywdsqxy.supabase.co/rest/v1/rpc/buscar_paciente"
# Llave pública (rol anon) publicada en el JS de hospitalesenvenezuela.com
HOSP_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im96dXhmZXBma3ZueGt5d2RzcXh5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI0MjI5NTEsImV4cCI6MjA5Nzk5ODk1MX0."
    "YhW0GalGkQZdO2NJTg_01C5XhdMmJ6RbNSNXXC0xG4o"
)
REDAYUDA_API = "https://redayudavenezuela.com/api/data"


# ---------------------------------------------------------------- utilidades

def _http(url, data=None, headers=None, timeout=TIMEOUT):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _norm(s):
    """minúsculas y sin acentos, para comparar nombres."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


_RE_NUM = re.compile(r"\d[\d.\-\s]{4,}\d")


def _mask_ids(text):
    """Enmascara cédulas/teléfonos (secuencias de 6+ dígitos) dejando los últimos 4."""
    if not text:
        return text

    def repl(m):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 6:
            return m.group(0)
        return "···" + digits[-4:]

    return _RE_NUM.sub(repl, str(text))


def _mask_cedula(ced):
    if not ced:
        return ""
    ced = str(ced)
    if "•" in ced or "···" in ced or "*" in ced:
        return ced.replace("•", "·").replace("*", "·")
    digits = re.sub(r"\D", "", ced)
    if len(digits) >= 5:
        return "···" + digits[-4:]
    return ced


def norm_estado(raw):
    """Mapea los estados crudos de cada fuente a un vocabulario único."""
    s = _norm(raw)
    if not s:
        return "desconocido"
    if "falleci" in s:
        return "fallecido"
    if "rescatad" in s:
        return "rescatado"
    if "alta" in s:
        return "alta"
    if "localizad" in s or "salvo" in s or "encontrad" in s or "located" in s:
        return "localizado"
    if "desapar" in s or "buscad" in s or s == "active" or "se busca" in s:
        return "desaparecido"
    if "hospital" in s or "ingres" in s or s in ("valido", "válido") or "info recibida" in s:
        return "ingresado"
    if "incompleto" in s:
        return "desconocido"
    return "desconocido"


def _matches(query_tokens, *fields):
    hay = _norm(" ".join(f for f in fields if f))
    return all(t in hay for t in query_tokens)


def _res(nombre, cedula, estado_raw, lugar, zona, detalle, fecha, foto, fuente, url, origen=None):
    return {
        "nombre": (nombre or "").strip(),
        "cedula": _mask_cedula(cedula),
        "estado": norm_estado(estado_raw),
        "estado_raw": (estado_raw or "").strip(),
        "lugar": (lugar or "").strip(),
        "zona": _mask_ids((zona or "").strip()),
        "detalle": _mask_ids((detalle or "").strip()),
        "fecha": (fecha or "").strip(),
        "foto": foto or None,
        "fuente": fuente,
        "origen": origen,
        "url": url,
    }


# ------------------------------------------------------------------ fuentes

def _cache_get(name, url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL:
        with open(path) as f:
            return json.load(f)
    data = _http(url, timeout=60)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


def buscar_uniprocesador(query_tokens, limit=40):
    """Base unificada (19k+ registros de 13 portales). Cache local 15 min."""
    data = _cache_get("uniproc_global.json", UNIPROC_BASE + "/api/global")
    out = []
    for p in data.get("pacientes", []):
        nombre = "{} {}".format(p.get("nombre") or "", p.get("apellido") or "").strip()
        if not _matches(query_tokens, nombre, p.get("cedula")):
            continue
        externo = p.get("isExternal")
        estatus = p.get("estatus") or ""
        # Registros locales del uniprocesador = listas de pacientes por centro
        if not externo and norm_estado(estatus) == "desconocido":
            estatus = "ingresado"
        out.append(_res(
            nombre=nombre,
            cedula=p.get("cedula"),
            estado_raw=estatus,
            lugar=p.get("centro"),
            zona=p.get("edad_sector"),
            detalle="",
            fecha=p.get("fecha_ingreso") or "",
            foto=None,
            fuente="Buscador Multiorigen",
            url=UNIPROC_BASE,
            origen=p.get("origen") or ("Listas de centros" if not externo else None),
        ))
        if len(out) >= limit:
            break
    return out


def buscar_cruces(query_tokens, min_score=40, limit=20):
    """Cruces inteligentes del uniprocesador: paciente local ↔ portal de desaparecidos."""
    data = _cache_get(
        "uniproc_cruces.json",
        UNIPROC_BASE + "/api/crossmatch?mode=results&minScore={}".format(min_score),
    )
    out = []
    for m in data.get("matches", []):
        nom_l = "{} {}".format(m.get("nombre_local") or "", m.get("apellido_local") or "").strip()
        nom_e = "{} {}".format(m.get("nombre_externo") or "", m.get("apellido_externo") or "").strip()
        if query_tokens and not _matches(
            query_tokens, nom_l, nom_e, m.get("cedula_local"), m.get("cedula_externo")
        ):
            continue
        fuentes = m.get("sources") or []
        out.append({
            "nombre_local": nom_l,
            "nombre_externo": nom_e,
            "cedula": _mask_cedula(m.get("cedula_local") or m.get("cedula_externo")),
            "centro": m.get("centro_externo") or "",
            "estado": norm_estado(m.get("estado_externo")),
            "estado_raw": m.get("estado_externo") or "",
            "edad": m.get("edad_externo") or "",
            "score": m.get("match_score"),
            "fuente": (fuentes[0].get("name") if fuentes else "Buscador Multiorigen"),
            "fecha": m.get("created_at") or "",
        })
        if len(out) >= limit:
            break
    return out


def buscar_hospitales(term, limit=40):
    """hospitalesenvenezuela.com — RPC público buscar_paciente(p_term)."""
    rows = _http(
        HOSP_RPC,
        data={"p_term": term},
        headers={"apikey": HOSP_ANON_KEY, "Authorization": "Bearer " + HOSP_ANON_KEY},
    )
    out = []
    for r in rows[:limit]:
        detalle = r.get("detalle") or ""
        corr = r.get("correcciones") or []
        if corr:
            notas = "; ".join(str(c.get("nota") or c) for c in corr[:2])
            detalle = (detalle + " · Correcciones: " + notas).strip(" ·")
        estado_raw = r.get("estado") or "ingresado"
        out.append(_res(
            nombre=r.get("nombre"),
            cedula=r.get("cedula"),
            estado_raw=estado_raw,
            lugar=r.get("centro"),
            zona=r.get("ciudad"),
            detalle=detalle,
            fecha=r.get("registrado") or "",
            foto=None,
            fuente="Hospitales en Venezuela",
            url="https://hospitalesenvenezuela.com",
        ))
    return out


def buscar_redayuda(term, limit=40):
    """redayudavenezuela.com — search_people (hospital/salvo/localizado) + missing_search."""
    out = []
    resp = _http(REDAYUDA_API, data={"op": "search_people", "term": term})
    for r in (resp.get("data") or [])[:limit]:
        out.append(_res(
            nombre=r.get("name"),
            cedula=r.get("contact") or "",
            estado_raw=r.get("category") or r.get("label"),
            lugar=r.get("loc"),
            zona="",
            detalle=r.get("detail"),
            fecha="",
            foto=r.get("photo_url"),
            fuente="Red Ayuda Venezuela",
            url="https://redayudavenezuela.com",
        ))
    resp2 = _http(REDAYUDA_API, data={"op": "missing_search", "term": term, "status": "active", "page": 0})
    for r in (resp2.get("data") or [])[:limit]:
        estado_raw = "localizado" if r.get("located_at") else (r.get("status") or "desaparecido")
        out.append(_res(
            nombre=r.get("name"),
            cedula="",
            estado_raw=estado_raw,
            lugar="",
            zona=r.get("last_seen"),
            detalle=r.get("description"),
            fecha=(r.get("synced_at") or "")[:10],
            foto=r.get("photo_url"),
            fuente="Red Ayuda Venezuela",
            url="https://redayudavenezuela.com",
            origen=r.get("source"),
        ))
    return out


# ------------------------------------------------------------------ fachada

def _dedup(resultados):
    """Colapsa duplicados exactos (mismo nombre normalizado + estado + lugar)."""
    vistos = {}
    for r in resultados:
        clave = (_norm(r["nombre"]), r["estado"], _norm(r["lugar"]))
        if clave in vistos:
            prev = vistos[clave]
            if r["fuente"] not in prev["fuentes"]:
                prev["fuentes"].append(r["fuente"])
            for campo in ("detalle", "zona", "fecha", "foto", "cedula"):
                if not prev.get(campo) and r.get(campo):
                    prev[campo] = r[campo]
        else:
            r["fuentes"] = [r["fuente"]]
            vistos[clave] = r
    return list(vistos.values())


ORDEN_ESTADO = {"fallecido": 0, "rescatado": 1, "localizado": 2, "alta": 3,
                "ingresado": 4, "desaparecido": 5, "desconocido": 6}


def buscar_todo(term):
    """Busca en todas las fuentes en paralelo. Devuelve resultados + cruces + salud por fuente."""
    term = (term or "").strip()
    tokens = [t for t in _norm(term).split() if len(t) >= 2]
    if not tokens:
        return {"error": "Escribe al menos un nombre (2+ letras)."}

    tareas = {
        "Buscador Multiorigen": lambda: buscar_uniprocesador(tokens),
        "Hospitales en Venezuela": lambda: buscar_hospitales(term),
        "Red Ayuda Venezuela": lambda: buscar_redayuda(term),
        "_cruces": lambda: buscar_cruces(tokens),
    }
    resultados, cruces, salud = [], [], {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fn): nombre for nombre, fn in tareas.items()}
        for fut in as_completed(futs):
            nombre = futs[fut]
            try:
                datos = fut.result()
                if nombre == "_cruces":
                    cruces = datos
                else:
                    resultados.extend(datos)
                    salud[nombre] = {"ok": True, "n": len(datos)}
            except Exception as e:
                if nombre != "_cruces":
                    salud[nombre] = {"ok": False, "error": str(e)[:120]}

    resultados = _dedup(resultados)
    resultados.sort(key=lambda r: (ORDEN_ESTADO.get(r["estado"], 9), _norm(r["nombre"])))
    return {
        "consulta": term,
        "resultados": resultados,
        "cruces": cruces,
        "fuentes": salud,
        "ms": int((time.time() - t0) * 1000),
    }


# Estados que significan "esta persona apareció en algún registro":
ESTADOS_REFERENCIA = {"ingresado", "rescatado", "localizado", "alta", "fallecido"}


def referencias(persona, max_n=3):
    """Coincidencias FUERTES para una persona buscada (sugerencias y depurador).

    Exige nombre y apellido (≥2 palabras) y que TODAS aparezcan en el nombre
    del registro, con estado de tipo "apareció" (ingresado/rescatado/localizado/
    alta/fallecido). Devuelve hasta max_n dicts compactos. Nunca lanza: ante
    fuentes caídas devuelve lo que haya (o lista vacía)."""
    tokens = [t for t in _norm(persona).split() if len(t) >= 2]
    if len(tokens) < 2:
        return []
    try:
        r = buscar_todo(persona)
    except Exception:
        return []
    refs = []
    for p in r.get("resultados", []):
        if p["estado"] not in ESTADOS_REFERENCIA:
            continue
        nombre_norm = _norm(p["nombre"])
        if not all(t in nombre_norm for t in tokens):
            continue
        refs.append({
            "nombre": p["nombre"], "estado": p["estado"], "estado_raw": p["estado_raw"],
            "lugar": p["lugar"], "cedula": p["cedula"], "fecha": p["fecha"],
            "fuente": " + ".join(p.get("fuentes") or [p["fuente"]]),
            "origen": p.get("origen"),
        })
        if len(refs) >= max_n:
            break
    return refs


ETIQUETA_ESTADO = {"ingresado": "🏥 Ingresado/a", "rescatado": "✅ Rescatado/a",
                   "localizado": "✅ Localizado/a", "alta": "🏥 Dado/a de alta",
                   "fallecido": "🕊️ Reportado/a como fallecido/a"}


def resumen_referencias(refs, html_out=False):
    """Líneas de texto listas para WhatsApp (plano/markdown suave) o Telegram (HTML)."""
    lineas = []
    for r in refs:
        etiqueta = ETIQUETA_ESTADO.get(r["estado"], r["estado"])
        donde = " en {}".format(r["lugar"]) if r["lugar"] else ""
        if html_out:
            import html as _h
            lineas.append("• <b>{}</b> — {}{} <i>({})</i>".format(
                _h.escape(r["nombre"]), etiqueta, _h.escape(donde), _h.escape(r["fuente"])))
        else:
            lineas.append("• *{}* — {}{} ({})".format(r["nombre"], etiqueta, donde, r["fuente"]))
    return lineas


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "maria gonzalez"
    r = buscar_todo(q)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:4000])
    print("\nTOTAL: {} resultados, {} cruces, {} ms".format(
        len(r.get("resultados", [])), len(r.get("cruces", [])), r.get("ms")))

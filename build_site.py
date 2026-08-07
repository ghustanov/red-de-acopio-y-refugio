"""Genera la página web (web/index.html) + manifest y service worker (PWA)."""
import json
import time
import hashlib
import re
import datetime
import urllib.parse
import urllib.request
from datetime import timezone
from zoneinfo import ZoneInfo

from config import WEB_DIR, SITE_TITLE, SITE_TAGLINE, AVISO_WHATSAPP, BASE, CATS_PEDIR
import db

SISMOS_CACHE = BASE / "sismos_cache.json"   # caché 5 min (fuera de web/, no público)

FUENTE = "Emergencias Viales · Ángeles de las Autopistas (MPPT)"

# Números de emergencia, agrupados (base: redayudavenezuela.com — editable)
EMERGENCIAS = [
    {"grupo": "📞 Líneas de emergencia", "items": [
        {"nombre": "Cantv (desde fijo)", "numeros": ["171"]},
        {"nombre": "Movilnet", "numeros": ["*1"]},
        {"nombre": "Digitel", "numeros": ["112"]},
        {"nombre": "Movistar", "numeros": ["911"]},
    ]},
    {"grupo": "🚑 Ambulancias", "items": [
        {"nombre": "Rescarven", "numeros": ["(0212) 993.69.11", "(0212) 993.69.91", "(0212) 993.13.10", "(0212) 993.33.67"]},
        {"nombre": "Servicio Metropolitano", "numeros": ["(0212) 545.45.45", "(0212) 545.46.55", "(0212) 577.92.09"]},
    ]},
    {"grupo": "🚁 Aeroambulancias", "items": [
        {"nombre": "Aeroambulancias", "numeros": ["(0212) 993.25.41", "(0212) 992.89.80", "(0212) 992.89.90", "(0212) 991.79.40"]},
    ]},
    {"grupo": "🚒 Bomberos (por zona)", "items": [
        {"nombre": "Antímano", "numeros": ["(0212) 472.20.54"]},
        {"nombre": "Catia la Mar", "numeros": ["(0212) 351.99.66"]},
        {"nombre": "Chacao", "numeros": ["(0212) 265.32.61"]},
        {"nombre": "del Este (Cafetal)", "numeros": ["(0212) 987.43.34", "(0212) 985.50.60"]},
        {"nombre": "El Cafetal", "numeros": ["(0212) 985.36.40", "(0212) 985.29.77"]},
        {"nombre": "Sucre", "numeros": ["(0212) 985.36.40"]},
        {"nombre": "El Paraíso", "numeros": ["(0212) 481.09.61"]},
        {"nombre": "El Valle", "numeros": ["(0212) 672.01.75", "(0212) 672.06.36"]},
        {"nombre": "La Guaira", "numeros": ["(0212) 332.76.20", "(0212) 331.04.45"]},
        {"nombre": "La Trinidad", "numeros": ["(0212) 943.43.61"]},
        {"nombre": "La Urbina", "numeros": ["(0212) 241.66.41"]},
        {"nombre": "Metropolitanos", "numeros": ["(0212) 545.45.45"]},
        {"nombre": "Miranda", "numeros": ["(0212) 235.69.67"]},
        {"nombre": "Plaza Venezuela", "numeros": ["(0212) 793.00.39", "(0212) 793.64.57"]},
        {"nombre": "San Bernardino", "numeros": ["(0212) 577.92.09"]},
    ]},
]

# Plataformas ciudadanas que centralizan la búsqueda de personas ingresadas en
# hospitales / desaparecidas tras el terremoto. ENLAZAMOS a ellas (no copiamos sus
# datos: ya enmascaran cédulas/teléfonos y se actualizan solas). Editable. Verificadas
# en vivo (HTTP 200) el 27/06/2026.
PLATAFORMAS = [
    {"nombre": "Localiza Pacientes (hospitales)",
     "url": "https://localizapacientes.com",
     "desc": "Busca por nombre y te dice en qué hospital está una persona atendida tras el terremoto. Específico de hospitales; no muestra cédula ni teléfono."},
    {"nombre": "Red de Ayuda Venezuela",
     "url": "https://redayudavenezuela.com",
     "desc": "Escribe el nombre o sube una foto y te dice si la persona está en hospitales, desaparecida o a salvo."},
    {"nombre": "Venezuela Te Busca",
     "url": "https://venezuelatebusca.com",
     "desc": "Reporta y busca personas; incluye directorio actualizado de hospitales y centros de atención."},
    {"nombre": "Afectados por el Terremoto",
     "url": "https://www.afectadosporelterremotovenezuela.com",
     "desc": "Registro humanitario de hospitalizados, rescatados y desaparecidos. Datos sensibles enmascarados."},
]

# --- Ventana de frescura: tras estas HORAS, el item se ARCHIVA (se oculta de la
#     página, pero queda en la base con su link original). None = nunca expira. ---
VENTANA_HORAS = {
    "info": 24,          # alertas: caducan rápido
    "heridos": 48,
    "rescate": 48,
    "donaciones": 72,    # 3 días
    "contacto": 336,     # 14 días
    "mascotas": 336,     # 14 días (las mascotas perdidas siguen buscándose un tiempo)
    "refugios": 168,     # 7 días
    "ofrecimientos": 96, # 4 días (un ofrecimiento viejo puede ya no estar disponible)
    "desaparecidos": None,   # nunca por tiempo: lo resuelven los moderadores
}
CENTROS_VENTANA_HORAS = 96          # centros sin mención en 4 días → se ocultan
SERVICIOS_VENTANA_HORAS = 48        # estado de servicios sin actualizar en 2 días → se oculta
DESAP_RESUELTO_HORAS = 168          # apareció/fallecida: se muestra 7 días y se archiva

CATEGORIAS = {
    "heridos":       {"icono": "🏥", "nombre": "Heridos / Hospitales"},
    "rescate":       {"icono": "🆘", "nombre": "Pedidos de rescate"},
    "desaparecidos": {"icono": "🔎", "nombre": "Desaparecidos"},
    "mascotas":      {"icono": "🐾", "nombre": "Mascotas"},
    "refugios":      {"icono": "🏠", "nombre": "Refugios"},
    "ofrecimientos": {"icono": "🤝", "nombre": "Ofrecimientos"},
    "donaciones":    {"icono": "📦", "nombre": "Donaciones"},
    "info":          {"icono": "ℹ️", "nombre": "Info / Alertas"},
    "contacto":      {"icono": "📞", "nombre": "Contactos"},
}

# --- SEO (título y descripción que ve Google; separados del H1/marca) ---
SEO_TITULO = "Red de Acopio y Refugio — Directorio de centros de acopio, refugios y hospitales en Venezuela"
SEO_DESC = ("Registro unificado de centros de acopio, refugios y hospitales en Venezuela: "
            "ubicación, capacidad (aforo), insumos disponibles y requerimientos en tiempo real. "
            "Coordina la ayuda y los insumos donde de verdad hacen falta.")
SITE_URL_BASE = "https://redacopioyrefugio.com/"
OG_IMAGEN = "https://redacopioyrefugio.com/og.png?v=hero1"

PLANTILLA = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#003C67">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Acopio y Refugio">
<link rel="manifest" href="manifest.webmanifest?v=ofc">
<link rel="icon" type="image/svg+xml" href="logo.svg?v=ofc">
<link rel="apple-touch-icon" href="apple-touch-icon.png?v=ofc">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap">
<title>__SEO_TITULO__</title>
<meta name="description" content="__SEO_DESC__">
<link rel="canonical" href="https://redacopioyrefugio.com/">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Red de Acopio y Refugio">
<meta property="og:title" content="__SEO_TITULO__">
<meta property="og:description" content="__SEO_DESC__">
<meta property="og:url" content="https://redacopioyrefugio.com/">
<meta property="og:image" content="https://redacopioyrefugio.com/og.png?v=hero1">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="es_VE">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="__SEO_TITULO__">
<meta name="twitter:description" content="__SEO_DESC__">
<meta name="twitter:image" content="https://redacopioyrefugio.com/og.png?v=hero1">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
{"@type":"WebSite","@id":"https://redacopioyrefugio.com/#website","url":"https://redacopioyrefugio.com/","name":"Red de Acopio y Refugio","inLanguage":"es","description":"__SEO_DESC__"},
{"@type":"Organization","@id":"https://redacopioyrefugio.com/#org","name":"Red de Acopio y Refugio","url":"https://redacopioyrefugio.com/","logo":"https://redacopioyrefugio.com/icon-512.png?v=ofc"}
]}
</script>
<style>
  :root{
    --bg:#eef1f5; --card:#fff; --tinta:#0f1f33; --suave:#5b6b80; --linea:#e2e8f0;
    --navy:#003C67; --navy2:#005B67; --teal:#1E9C94; --rojo:#e11d48; --wa:#25d366;
    --heridos:#dc2626; --rescate:#ea580c; --desaparecidos:#7c3aed;
    --donaciones:#15803d; --info:#2563eb; --contacto:#0e7490; --mascotas:#b45309;
    --refugios:#0d9488; --ofrecimientos:#0891b2;
    --lleno:#dc2626; --disponible:#15803d; --desconocido:#94a3b8;
    --lleno-bg:#fef2f2; --disponible-bg:#f0fdf4; --desconocido-bg:#f1f5f9;
    --safe-b:env(safe-area-inset-bottom);
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;font-family:"Poppins","Segoe UI",system-ui,-apple-system,Roboto,Helvetica,sans-serif;
       font-weight:300;
       background:var(--bg);color:var(--tinta);line-height:1.5;-webkit-font-smoothing:antialiased;
       overscroll-behavior-y:contain}
  .tope{height:4px;background:linear-gradient(90deg,var(--navy),var(--teal))}
  /* ===== hero ===== */
  header{position:relative;overflow:hidden;color:#fff;
         background:linear-gradient(118deg,#022B4D,var(--navy) 34%,var(--navy2) 66%,#15857E);
         padding:calc(13px + env(safe-area-inset-top)) 16px 16px}
  header::before{content:"";position:absolute;inset:0;pointer-events:none;
         background:radial-gradient(640px 300px at 88% -12%,rgba(255,255,255,.13),transparent 60%),
                    radial-gradient(520px 280px at -10% 112%,rgba(1,16,30,.30),transparent 56%)}
  .h-marca-agua{position:absolute;right:-72px;bottom:-92px;width:330px;height:330px;opacity:.07;
         pointer-events:none;filter:brightness(0) invert(1);transform:rotate(-6deg)}
  .hwrap,.tabs,.wrap{max-width:820px;margin:0 auto}
  .hwrap{position:relative}
  .marca{display:flex;align-items:center;gap:11px}
  .marca .logo{width:42px;height:42px;flex:0 0 auto;display:block;
       background:#fff;border-radius:12px;padding:5px;box-shadow:0 2px 10px rgba(2,20,38,.28)}
  .marca .nom{font-size:15.5px;font-weight:700;line-height:1.25}
  .marca .dom{font-size:11.5px;font-weight:400;opacity:.72;letter-spacing:.3px}
  .hero-t{margin:22px 0 0;font-size:clamp(27px,5.4vw,38px);line-height:1.14;font-weight:700;
          letter-spacing:-.02em;text-wrap:balance}
  .hero-s{margin:12px 0 0;font-size:15px;line-height:1.6;max-width:58ch;
          color:rgba(255,255,255,.9);font-weight:300}
  .hero-s b{font-weight:600;color:#fff}
  .hero-cta{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}
  .hbtn{display:inline-flex;align-items:center;justify-content:center;gap:9px;border-radius:12px;
        padding:14px 20px;font-size:15px;font-weight:700;text-decoration:none;
        transition:transform .12s,filter .15s,background .15s;-webkit-tap-highlight-color:transparent}
  .hbtn svg{flex:0 0 auto}
  .hbtn:active{transform:scale(.97)}
  .hbtn-sos{background:#D2262C;color:#fff;box-shadow:0 8px 22px rgba(120,10,20,.35)}
  .hbtn-sos:hover{filter:brightness(1.07)}
  .hbtn-of{background:#fff;color:var(--navy);box-shadow:0 8px 22px rgba(2,20,38,.24)}
  .hbtn-of:hover{filter:brightness(.97)}
  .hbtn-bp{background:rgba(255,255,255,.10);color:#fff;border:1px solid rgba(255,255,255,.45)}
  .hbtn-bp:hover{background:rgba(255,255,255,.18)}
  @media(max-width:559px){
    .hbtn{padding:14px 12px;font-size:14.5px}
    .hero-cta .hbtn-sos,.hero-cta .hbtn-of{flex:1 1 calc(50% - 5px)}
    .hero-cta .hbtn-bp{flex:1 1 100%}
  }
  .hmeta{font-size:12px;margin-top:13px;color:rgba(255,255,255,.82)}
  .cintillo-top{display:flex;align-items:center;gap:11px}
  .cintillo-top .logo{width:40px;height:40px;flex:0 0 auto;display:block;background:#fff;border-radius:11px;padding:5px;box-shadow:0 2px 10px rgba(2,20,38,.28)}
  .cintillo-top .marca-txt{min-width:0}
  .cintillo-top .nom{font-size:15px;font-weight:700;line-height:1.2}
  .cintillo-top .dom{font-size:11px;font-weight:400;opacity:.72;letter-spacing:.3px}
  .cintillo-top .vivo{margin-left:auto;font-size:12px;white-space:nowrap}
  /* pestañas */
  .tabbar{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid var(--linea);
          box-shadow:0 1px 4px rgba(0,0,0,.05)}
  .tabs{display:flex;flex-wrap:wrap;gap:7px;padding:10px 12px}
  .tab{flex:0 0 auto;display:flex;align-items:center;gap:7px;border:1px solid var(--linea);background:#fff;
       border-radius:999px;min-height:44px;box-sizing:border-box;padding:9px 15px;font-size:14px;font-weight:600;color:var(--suave);cursor:pointer;
       white-space:nowrap;-webkit-tap-highlight-color:transparent}
  .tab.on{background:var(--navy);border-color:var(--navy);color:#fff}
  .tab .pill{background:rgba(0,0,0,.08);border-radius:999px;padding:1px 8px;font-size:12px}
  .tab.on .pill{background:rgba(255,255,255,.25)}
  :focus-visible{outline:3px solid #34d399;outline-offset:2px}
  .hbtn:focus-visible,.su-btn:focus-visible,.tab:focus-visible{outline-offset:3px}
  .wrap{padding:14px 16px calc(36px + var(--safe-b))}
  .aviso{background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;padding:11px 13px;
         font-size:13px;color:#9a3412;margin-bottom:14px}
  .search{position:relative;margin-bottom:16px}
  .search input{width:100%;padding:14px 14px 14px 42px;border:1px solid var(--linea);
                border-radius:12px;font-size:16px;background:#fff;color:var(--tinta)}
  .search .ico{position:absolute;left:14px;top:13px;opacity:.5}
  .zona-sel{width:100%;padding:12px;border:1px solid var(--linea);border-radius:12px;
            font-size:15px;background:#fff;color:var(--tinta);margin-bottom:14px}
  /* centros */
  .mapa-red{height:360px;border-radius:14px;overflow:hidden;margin:4px 0 6px;border:1px solid var(--linea);background:#dfe6ee;z-index:0}
  .pop b{font-size:14px} .pop-sub{color:#5b6b80;font-size:12px} .leaflet-popup-content a{color:#1E9C94;text-decoration:none;font-weight:600}
  .aviso-mini{background:#f0f9ff;border-color:#bae6fd;padding:9px 12px;font-size:13px}
  .aviso-ms{background:#ecfdf5;border-color:#a7f3d0;color:#065f46}
  .leyenda{display:flex;gap:16px;justify-content:center;margin:0 0 12px;font-size:12px;color:var(--suave)}
  .leg-dot{display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:middle;margin-right:5px}
  .ms-chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}
  .ms-chip{background:#f1f5f9;color:#334155;border-radius:20px;padding:6px 13px;font-size:13px;cursor:pointer;user-select:none}
  .ms-chip.on{background:#003C67;color:#fff;font-weight:600}
  .centro.ms-card{display:block;margin-bottom:9px}
  .centro.lla-card{display:block}
  .ms-top{display:flex;align-items:center;gap:9px} .ms-ico{font-size:22px;flex:0 0 auto}
  .ms-nom{font-weight:600;color:#065f46;font-size:15px;line-height:1.25} .ms-sub{font-size:12px;color:var(--suave)}
  .ms-desc{font-size:14px;color:var(--tinta);margin:8px 0 2px}
  .ms-card .acciones .btn{flex:1;justify-content:center;min-width:120px}
  .btn.b-wa{background:#1E9C94;color:#fff;border-color:#1E9C94}
  .centro{background:#fff;border:1px solid var(--linea);border-radius:13px;padding:13px 14px;
          margin-bottom:10px;display:flex;gap:12px;align-items:flex-start}
  .centro .info{flex:1;min-width:0}
  .centro .cn{font-weight:700;font-size:15px}
  .badge{flex:0 0 auto;font-size:12px;font-weight:700;padding:5px 11px;border-radius:999px;white-space:nowrap}
  .b-disponible{color:var(--disponible);background:var(--disponible-bg)}
  .b-lleno{color:var(--lleno);background:var(--lleno-bg)}
  .b-desconocido{color:var(--desconocido);background:var(--desconocido-bg)}
  .meta{font-size:13px;color:var(--suave);margin:3px 0;word-wrap:break-word}
  .meta b{color:var(--tinta);font-weight:600}
  /* tarjetas */
  .card{background:#fff;border:1px solid var(--linea);
        border-radius:14px;padding:14px;margin-bottom:12px;box-shadow:0 1px 2px rgba(15,31,51,.04)}
  .card .cab{display:flex;align-items:center;gap:9px;margin-bottom:4px}
  .card .ico{font-size:21px;line-height:1}
  .card .cat{font-size:11px;color:var(--suave);font-weight:700;text-transform:uppercase;letter-spacing:.4px}
  .card h3{margin:1px 0 0;font-size:16px;font-weight:700}
  .det{font-size:14px;margin:7px 0}
  .completo{margin:6px 0 2px}
  .completo summary{cursor:pointer;color:var(--navy);font-weight:700;font-size:13px;list-style:none;display:inline-block}
  .completo summary::-webkit-details-marker{display:none}
  .completo summary:before{content:"▾ "}
  .completo[open] summary:before{content:"▴ "}
  .completo-txt{white-space:pre-wrap;font-size:13px;line-height:1.45;margin-top:8px;max-height:360px;
                overflow:auto;background:#f8fafc;border:1px solid var(--linea);border-radius:10px;padding:10px 12px}
  .serv-chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:8px}
  .serv-chip{font-size:13px;font-weight:700;padding:5px 11px;border-radius:999px;border:1px solid var(--linea)}
  .s-ok{color:#15803d;background:#f0fdf4;border-color:#bbf7d0}
  .s-falla{color:#dc2626;background:#fef2f2;border-color:#fecaca}
  .s-parcial{color:#b45309;background:#fff7ed;border-color:#fed7aa}
  .s-desc{color:#475569;background:#f1f5f9}
  .stats{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
  .stat{background:#fff;border:1px solid var(--linea);border-top:4px solid var(--suave);border-radius:13px;
        padding:16px 12px;text-align:center;box-shadow:0 1px 2px rgba(15,31,51,.04)}
  .stat-n{font-size:30px;font-weight:800;line-height:1;color:var(--navy)}
  .stat-l{font-size:12.5px;color:var(--suave);margin-top:6px;font-weight:600;line-height:1.25}
  .cambios{background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;padding:11px 13px;
           margin-top:14px;font-size:14px;text-align:center}
  @media(min-width:560px){.stats{grid-template-columns:repeat(3,1fr)}}
  .nec-acc{padding:0;overflow:hidden;display:block}
  .nec-acc summary{list-style:none;cursor:pointer;position:relative;padding:13px 38px 13px 14px;display:flex;flex-direction:column;gap:2px}
  .nec-acc summary::-webkit-details-marker{display:none}
  .nec-acc summary:after{content:"▾";position:absolute;right:15px;top:14px;color:var(--suave);font-size:13px}
  .nec-acc[open] summary:after{content:"▴"}
  .destacado{box-shadow:0 0 0 3px var(--wa);transition:box-shadow .5s ease}
  .seg-badge{display:inline-block;font-size:12px;font-weight:700;padding:1px 7px;border-radius:10px;margin-left:4px}
  .seg-busc{background:#fef3c7;color:#92400e}
  .seg-disp{background:#dcfce7;color:#166534}
  .seg-coord{background:#dbeafe;color:#1e40af}
  .nec-it{padding:9px 0;border-bottom:1px solid #eef1f5}
  .nec-it:last-child{border-bottom:none}
  .nec-it-tit{font-size:14px;margin-bottom:7px}
  .nec3{margin:2px 0}
  .nec3-bar{display:flex;height:11px;width:100%;border-radius:7px;overflow:hidden;background:#eef1f5;border:1px solid #e1e6ee}
  .nec3-bar span{display:block;height:100%}
  .seg-rec{background:#1E9C94}
  .seg-enc{background:#EF9F27}
  .nec3-leg{display:flex;flex-wrap:wrap;gap:13px;margin-top:7px;font-size:12px;color:var(--suave)}
  .nec3-leg .d{width:9px;height:9px;border-radius:2px;display:inline-block;margin-right:5px;vertical-align:middle}
  .d-rec{background:#1E9C94}.d-enc{background:#EF9F27}.d-fal{background:#dfe5ee}
  .nec3-foot{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-top:11px}
  .nec3-num{font-size:17px;font-weight:800;color:#0f2238}
  .nec3-meta{font-size:12.5px;font-weight:600;color:var(--suave)}
  .nec3-sinmeta{font-size:13px;color:var(--suave)}
  .urg{display:inline-block;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;margin-bottom:8px}
  .urg-crit{background:#fef2f2;color:#dc2626}.urg-alta{background:#fff7ed;color:#b45309}.urg-ok{background:#f0fdf4;color:#15803d}
  .btn.b-llevar{background:#1E7CA8;color:#fff;border-color:#1E7CA8;flex:1;min-width:170px;justify-content:center}
  .btn.b-accweb{background:var(--navy);border-color:var(--navy);cursor:pointer}
  .cmp-ovl{position:fixed;inset:0;background:rgba(2,20,38,.55);z-index:200;display:flex;align-items:center;justify-content:center;padding:16px}
  .cmp-box{background:#fff;border-radius:16px;max-width:420px;width:100%;padding:18px;max-height:88vh;overflow:auto;box-shadow:0 18px 50px rgba(2,20,38,.35)}
  .cmp-box h3{margin:0 0 4px;color:var(--navy);font-size:17px;padding-right:26px}
  .cmp-box label{display:block;font-weight:700;font-size:13.5px;margin:10px 0 4px}
  .cmp-box input{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:10px;font-size:15px;box-sizing:border-box;font-family:inherit}
  .cmp-box .btn{width:100%;margin-top:14px;justify-content:center;cursor:pointer}
  .cmp-err{color:#9b2226;font-size:13px;margin-top:8px;display:none}
  .cmp-x{float:right;border:0;background:none;font-size:20px;cursor:pointer;color:#8a99ad;line-height:1}
  /* Necesitan ayuda AHORA: fila de necesidades críticas arriba de todo */
  .ahora{margin:0 0 16px}
  .ahora-head{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}
  .ahora-head b{color:var(--navy);font-size:17px}
  .ahora-head span{color:var(--suave);font-size:12.5px}
  .ahora-head a{margin-left:auto;color:#1E7CA8;font-weight:700;font-size:13px;text-decoration:none;white-space:nowrap}
  .ahora-grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
  .ahora-card{background:#fff;border:1px solid var(--linea);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:6px}
  .ahora-card .a-item{font-weight:800;color:var(--tinta);line-height:1.25}
  .ahora-card .a-punto{color:var(--suave);font-size:12.5px}
  .ahora-card .a-falta{color:#c0392b;font-weight:800;font-size:13px}
  .ahora-card .btn{margin-top:auto;justify-content:center;cursor:pointer}
  .btn.b-accwa{background:#128C7E;border-color:#128C7E}
  .btn.b-cubierto{background:#eef1f5;color:#8a97a8;flex:1;min-width:140px;justify-content:center;cursor:default}
  /* llamados: el par "Puedo ayudar" apilado a ancho completo; compartir/copiar en fila secundaria */
  .acc-lla .btn{flex:1 1 auto;justify-content:center}
  .acc-lla .b-llevar{flex:1 1 100%}
  .lla-resp{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;background:#ecfdf5;color:#065f46}
  .lla-sin{display:inline-block;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;background:#fef2f2;color:#dc2626}
  .nec-tit{font-weight:700;font-size:15px}
  .nec-sub{font-size:13px;color:var(--suave)}
  .nec-body{padding:2px 14px 14px}
  .inv-q{width:100%;box-sizing:border-box;padding:8px 10px;margin:6px 0 8px;border:1px solid #d6dbe4;border-radius:8px;font-size:14px}
  .inv-cat{margin:3px 0;border-bottom:1px solid #eef1f5}
  .inv-cat>summary{cursor:pointer;list-style:none;padding:7px 0;user-select:none}
  .inv-cat>summary::-webkit-details-marker{display:none}
  .inv-cat>summary::before{content:"▸";color:var(--suave);margin-right:7px;font-size:12px;display:inline-block}
  .inv-cat[open]>summary::before{content:"▾"}
  .inv-cat-body{padding:0 0 8px 16px}
  .inv-grupo{font-weight:800;color:var(--navy);font-size:13px;margin:0}
  .inv-n{font-weight:600;color:var(--suave)}
  .inv-lista{margin-top:4px}
  .inv-fila{font-size:14px;line-height:1.6;color:#374151;padding:1px 0}
  .inv-desc{color:var(--suave)}
  .dir-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:6px 0 4px}
  .dir-f{background:#f7f9fc;border:1px solid var(--linea);border-radius:9px;padding:7px 10px}
  .dir-f.wide{grid-column:1 / -1}
  .dir-k{font-size:10.5px;text-transform:uppercase;letter-spacing:.3px;color:var(--suave)}
  .dir-v{font-size:14px;font-weight:700;margin-top:2px;color:var(--navy)}
  .dir-v.yes{color:var(--disponible)} .dir-v.non{color:var(--lleno)} .dir-v.naa{color:var(--desconocido);font-weight:600}
  .dir-resp{background:#f7f9fc;border:1px solid var(--linea);border-radius:9px;padding:9px 10px;margin-top:8px}
  .foto{width:100%;max-height:260px;object-fit:cover;border-radius:10px;margin:8px 0;background:#e7ebf0}
  /* botones de acción */
  .btn-tel{display:inline-flex;align-items:center;gap:6px;background:var(--disponible);color:#fff;
           border-radius:10px;padding:10px 14px;font-size:14px;font-weight:700;text-decoration:none;margin-top:8px}
  .acciones{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--linea);background:#fff;
       border-radius:10px;padding:9px 13px;font-size:13px;font-weight:700;color:var(--navy);
       text-decoration:none;cursor:pointer;-webkit-tap-highlight-color:transparent}
  .b-share{background:var(--wa);border-color:var(--wa);color:#fff}
  .b-report{background:var(--info);border-color:var(--info);color:#fff}
  .compartir-pagina{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .compartir-pagina .btn{flex:0 1 auto;justify-content:center;padding:9px 14px;font-size:12.5px}
  .compartir-pagina .b-share{background:#fff;border-color:#bfe0d2;color:#0B6E5F}
  .pie{display:flex;justify-content:space-between;align-items:center;margin-top:10px;font-size:12px;color:var(--suave)}
  .pie a{color:var(--navy);text-decoration:none;font-weight:600}
  .vieja{background:#fef3c7;color:#92400e;border-radius:6px;padding:1px 7px;font-size:11px;margin-left:6px}
  /* galería desaparecidos */
  .galeria{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
  .dperson{background:#fff;border:1px solid var(--linea);border-radius:14px;overflow:hidden;
           display:flex;flex-direction:column;box-shadow:0 1px 2px rgba(15,31,51,.04)}
  .dperson .fimg{width:100%;aspect-ratio:1/1;object-fit:cover;background:#e7ebf0;display:block}
  .dperson .dbody{padding:11px 12px;display:flex;flex-direction:column;gap:5px;flex:1}
  .dperson h3{margin:0;font-size:15px;font-weight:700}
  .dperson.resuelto{opacity:.72}
  .ebadge{display:inline-block;align-self:flex-start;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px}
  .e-buscando{color:#92400e;background:#fef3c7}
  .e-aparecio{color:#15803d;background:#f0fdf4}
  .e-fallecida{color:#475569;background:#f1f5f9}
  .dperson .acciones{margin-top:auto}
  .dperson .btn-tel{justify-content:center;align-self:stretch}
  .dperson .acciones .btn{flex:1;justify-content:center;padding:9px 8px}
  .vacio{text-align:center;color:var(--suave);padding:50px 16px}
  .vacio .em{font-size:36px;display:block;margin-bottom:10px}
  /* visor de fotos a pantalla completa */
  .fimg,.foto{cursor:zoom-in}
  .lightbox{position:fixed;inset:0;background:rgba(0,0,0,.93);display:none;align-items:center;
            justify-content:center;z-index:100;padding:16px;flex-direction:column;gap:14px}
  .lightbox.on{display:flex}
  .lightbox img{max-width:100%;max-height:82%;border-radius:10px;object-fit:contain}
  .lightbox .cerrar{color:#fff;font-size:14px;font-weight:600;opacity:.85}
  .modal-bg{position:fixed;inset:0;background:rgba(15,31,51,.55);display:none;align-items:center;justify-content:center;padding:16px;z-index:120}
  .modal-bg.on{display:flex}
  .modal-card{background:#fff;border-radius:16px;padding:18px;max-width:440px;width:100%;max-height:88vh;overflow:auto}
  .modal-card h3{margin:0 0 4px;font-size:18px;color:var(--navy)}
  .modal-card label{display:block;font-weight:600;font-size:14px;margin:12px 0 6px}
  .modal-card input,.modal-card textarea{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:10px;font-size:15px;font-family:inherit;box-sizing:border-box}
  .modal-card textarea{min-height:70px;resize:vertical}
  .modal-card .mbtn{display:block;width:100%;margin-top:14px;padding:13px;border:0;border-radius:11px;background:#1E9C94;color:#fff;font-weight:800;font-size:15px;cursor:pointer}
  .modal-card .mx{background:none;border:0;color:#8a99ad;font-size:13px;cursor:pointer;margin-top:10px;width:100%}
  /* ===== footer institucional ===== */
  footer{margin-top:26px;background:#062033;color:rgba(255,255,255,.72);font-size:13px;
         padding:30px 20px calc(40px + var(--safe-b))}
  .f-inner{max-width:820px;margin:0 auto;display:grid;gap:24px}
  @media(min-width:640px){.f-inner{grid-template-columns:1.4fr 1fr}}
  .f-marca{display:flex;align-items:center;gap:10px;color:#fff;font-weight:700;font-size:15px}
  .f-marca img{width:34px;height:34px;background:#fff;border-radius:9px;padding:4px}
  .f-mision{margin:10px 0 0;line-height:1.6;font-weight:300}
  .f-tit{color:#fff;font-weight:700;margin-bottom:8px;font-size:13px}
  .f-item{margin:5px 0;line-height:1.5}
  footer a{color:#7FD4CD;text-decoration:none;font-weight:600}
  .f-legal{max-width:820px;margin:24px auto 0;padding-top:16px;border-top:1px solid rgba(255,255,255,.12);
           font-size:12px;color:rgba(255,255,255,.58);line-height:1.65}
  @media(min-width:560px){.galeria{grid-template-columns:repeat(3,1fr)}}

  /* ===== refinamiento visual (look profesional) ===== */
  :root{ --sombra:0 1px 2px rgba(15,31,51,.05),0 6px 16px rgba(15,31,51,.06); --radio:14px; }
  body{ background:#eef2f7; }
  header{ box-shadow:0 2px 14px rgba(11,42,74,.20); }
  .vivo{ display:inline-flex; align-items:center; gap:6px; font-weight:700; letter-spacing:.3px }
  .vivo .punto{ width:8px; height:8px; border-radius:50%; background:#34d399; animation:pulso 2s infinite }
  @keyframes pulso{ 0%{box-shadow:0 0 0 0 rgba(52,211,153,.55)} 70%{box-shadow:0 0 0 7px rgba(52,211,153,0)} 100%{box-shadow:0 0 0 0 rgba(52,211,153,0)} }
  .tabbar{ box-shadow:0 2px 8px rgba(15,31,51,.06) }
  .tab{ padding:9px 16px; transition:all .15s }
  .tab:not(.on):hover{ border-color:#cbd5e1; color:var(--tinta) }
  .card,.centro{ border-radius:var(--radio); box-shadow:var(--sombra); border-color:#e8edf3; transition:box-shadow .18s,transform .18s }
  .card:hover,.centro:hover{ box-shadow:0 6px 24px rgba(11,42,74,.11) }
  .centro{ align-items:center }
  .card h3,.centro .cn{ letter-spacing:-.2px }
  .badge{ padding:6px 12px; font-size:12px }
  .btn,.btn-tel{ border-radius:11px; transition:transform .1s,filter .15s }
  .btn:active,.btn-tel:active{ transform:scale(.97) }
  .btn-tel:hover{ filter:brightness(1.05) }
  .grupo-titulo{ font-size:13px; font-weight:700; color:var(--suave); text-transform:uppercase; letter-spacing:.5px; margin:20px 2px 8px }
  .search input:focus,.zona-sel:focus{ outline:none; border-color:var(--navy); box-shadow:0 0 0 3px rgba(11,42,74,.08) }
  .foto,.fimg{ border-radius:12px }

  /* ===== menú de secciones colapsable ===== */
  .menu-toggle{width:100%;max-width:820px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;
               gap:8px;background:#fff;border:none;padding:14px 16px;font-size:16px;font-weight:700;color:var(--navy);cursor:pointer}
  .mt-actual{display:flex;align-items:center;gap:8px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mt-mas{flex-shrink:0;display:inline-flex;align-items:center;gap:6px;background:#eef2f8;color:var(--navy);
          border:1px solid var(--linea);border-radius:999px;padding:7px 13px;font-size:13px;font-weight:700;white-space:nowrap}
  .mt-chevron{font-size:18px;line-height:1;transition:transform .2s}
  .tabbar:has(.tabs.abierto) .mt-chevron{transform:rotate(180deg)}
  .tabs{display:none;flex-direction:column;flex-wrap:nowrap;gap:4px;max-width:820px;margin:0 auto;
        padding:4px 10px 12px;max-height:64vh;overflow-y:auto;border-top:1px solid var(--linea)}
  .tabs.abierto{display:flex}
  .tabs .tab{width:100%;justify-content:flex-start;border:1px solid transparent;border-radius:10px;
             padding:13px 14px;font-size:15px;background:#f5f8fc}
  .tabs .tab.on{background:var(--navy);border-color:var(--navy);color:#fff}
  /* grupo desplegable "Ángeles de la Autopista" dentro del menú */
  .tabs .tab-grupo{font-weight:800}
  .tab-grupo .g-chevron{margin-left:auto;transition:transform .15s;color:var(--suave)}
  .tab-grupo.abierto .g-chevron{transform:rotate(180deg)}
  .subtabs{display:none;flex-direction:column;gap:4px;margin-left:16px;padding-left:10px;
           border-left:2px solid var(--linea)}
  .subtabs.abierto{display:flex}
  .ac-angeles{background:#fdf1f2;border-color:#f2ccd3}
  .ac-angeles.on{background:var(--rojo);border-color:var(--rojo);color:#fff}
  /* ===== secciones (tiles uniformes, jerarquía silenciosa) ===== */
  .sec-titulo{max-width:820px;margin:20px auto 0;padding:0 16px;font-size:15px;font-weight:700;
              color:var(--tinta);letter-spacing:-.1px}
  /* minmax(0,1fr): sin el 0, un rótulo largo con letra agrandada (accesibilidad) impide
     encoger la columna y desborda la página en pantallas angostas (banda con hueco blanco).
     4 columnas recién desde 700px: "Requerimientos" (109px) necesita ~159px de columna. */
  .accesos{max-width:820px;margin:10px auto 2px;padding:0 16px;display:grid;
           grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
  @media(min-width:700px){.accesos{grid-template-columns:repeat(4,minmax(0,1fr))}}
  .secmenu{max-width:820px;margin:12px auto 4px;padding:0 16px;display:flex;flex-wrap:wrap;gap:8px}
  .secchip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--linea);background:#fff;color:var(--suave);border-radius:999px;min-height:44px;box-sizing:border-box;padding:9px 15px;font:inherit;font-size:14px;font-weight:600;cursor:pointer;line-height:1.1;transition:border-color .15s,color .15s;-webkit-tap-highlight-color:transparent}
  .secchip:hover{border-color:#c3cfdd;color:var(--tinta)}
  .secchip.on{background:var(--navy);border-color:var(--navy);color:#fff}
  .secchip .pill{background:rgba(0,0,0,.08);border-radius:999px;padding:1px 8px;font-size:12px;font-weight:700}
  .secchip.on .pill{background:rgba(255,255,255,.25)}
  .subchips{flex-basis:100%;display:flex;flex-wrap:wrap;gap:7px;margin-top:2px;padding-top:8px;border-top:1px dashed var(--linea)}
  .secchip.sub{font-size:13px;padding:7px 13px}
  .acceso{display:flex;align-items:center;gap:9px;padding:13px 12px;
          border:1px solid var(--linea);border-radius:12px;background:#fff;cursor:pointer;font:inherit;
          color:var(--tinta);font-weight:600;font-size:13.5px;line-height:1.25;text-align:left;
          transition:border-color .15s,box-shadow .15s,transform .08s;-webkit-tap-highlight-color:transparent}
  .acceso:hover{border-color:#c3cfdd;box-shadow:0 3px 12px rgba(15,31,51,.07);transform:translateY(-1px)}
  .acceso.on{background:var(--navy);border-color:var(--navy);color:#fff}
  .acceso .ac-ico{font-size:17px;flex:0 0 auto}
  /* el rótulo debe poder encogerse dentro del botón; si no, con letra agrandada
     una palabra larga ("Requerimientos") desborda la página entera. Si de verdad no
     cabe, se corta con guion (es) en vez de partirse a lo bruto. */
  .acceso>span:not(.ac-ico){min-width:0;overflow-wrap:break-word;-webkit-hyphens:auto;hyphens:auto}
  .ac-angeles{background:#fdf4f5;border-color:#f0d9dd}
  .ac-angeles.on{background:var(--rojo);border-color:var(--rojo);color:#fff}
  .ac-hub{white-space:normal;font-size:13px}
  .ac-apoya{background:#fff;border-color:var(--linea)}
  .ac-apoya.on{background:var(--navy);border-color:var(--navy)}
  /* "Apoya la plataforma" vive al final de la página, antes del footer */
  .apoya-pie{max-width:820px;margin:14px auto 4px;padding:0 16px;display:flex;justify-content:center}
  .apoya-pie .acceso{width:auto;padding:12px 24px}
  /* ===== franja "súmate a la red" ===== */
  .sumate{max-width:820px;margin:16px 16px 2px;padding:16px;display:flex;align-items:center;
          justify-content:space-between;gap:14px;flex-wrap:wrap;background:#fff;
          border:1px solid var(--linea);border-radius:14px;box-shadow:var(--sombra)}
  @media(min-width:852px){.sumate{margin-inline:auto}}
  .su-txt{flex:1 1 260px;min-width:0}
  .su-txt b{display:block;font-size:14.5px;color:var(--tinta);letter-spacing:-.1px}
  .su-txt span{display:block;font-size:13px;color:var(--suave);margin-top:3px}
  .su-btns{display:flex;gap:8px;flex-wrap:wrap}
  .su-btn{display:inline-flex;align-items:center;gap:7px;border-radius:11px;padding:11px 16px;
          font-size:13.5px;font-weight:700;text-decoration:none;color:#fff;
          transition:filter .15s,transform .1s;-webkit-tap-highlight-color:transparent}
  .su-btn:active{transform:scale(.97)} .su-btn:hover{filter:brightness(1.06)}
  .su-wa{background:#128C7E} .su-tg{background:#1E7CA8} .su-web{background:#1E9C94}
  a.tab-reg{display:flex;align-items:center;gap:7px;text-decoration:none;font-weight:700}
  a.tab-reg svg{flex:none}
  /* ===== transición al cambiar de sección: confirma visualmente que el botón hizo algo ===== */
  @keyframes contenidoIn{ from{opacity:0; transform:translateY(10px)} to{opacity:1; transform:translateY(0)} }
  #contenido.fx-in{ animation:contenidoIn .3s ease-out; }
  /* spinner del botón "cargar más" */
  .btn .spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(11,42,74,.25);
             border-top-color:var(--navy);border-radius:50%;animation:giro .7s linear infinite;vertical-align:-2px}
  @keyframes giro{to{transform:rotate(360deg)}}
  .btn:disabled{opacity:.75;cursor:default}
  @media (prefers-reduced-motion: reduce){
    .vivo .punto{animation:none}
    .hbtn,.acceso,.su-btn,.btn,.btn-tel,.tab{transition:none}
    #contenido.fx-in{animation:none}
    .btn .spin{animation:none}
  }
</style>
</head>
<body>
<div class="tope"></div>
<header>
  <img class="h-marca-agua" src="logo.svg?v=ofc" alt="" aria-hidden="true">
  <div class="hwrap">
    <div class="cintillo-top"><img class="logo" src="logo.svg?v=ofc" width="40" height="40" alt="Red de Acopio y Refugio">
      <div class="marca-txt"><div class="nom">__TITULO__</div><div class="dom">redacopioyrefugio.com</div></div>
      <span class="vivo"><span class="punto"></span>EN VIVO</span></div>
    <h1 class="hero-s">Directorio <b>en tiempo real</b> de centros de acopio, refugios y hospitales en Venezuela: dónde están, qué tienen y qué necesitan.</h1>
    <div class="hero-cta">
      <a class="hbtn hbtn-sos" href="/pedir.html"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.6"/><path d="M5.9 5.9l3.4 3.4M18.1 5.9l-3.4 3.4M18.1 18.1l-3.4-3.4M5.9 18.1l3.4-3.4"/></svg>Pedir ayuda</a>
      <a class="hbtn hbtn-of" href="/ofrecer.html"><svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 21s-7.4-4.6-9.9-9.1C.7 8.9 2.6 5 6.3 5c2.1 0 3.6 1.2 4.7 2.8h2C14.1 6.2 15.6 5 17.7 5c3.7 0 5.6 3.9 4.2 6.9C19.4 16.4 12 21 12 21z" transform="scale(.92) translate(1 .5)"/></svg>Ofrecer ayuda</a>
      <a class="hbtn hbtn-bp" href="/buscar.html"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4.2-4.2"/></svg>Buscar persona · hospitales y rescatados</a>
    </div>
    <div class="hmeta"><span id="hmeta"></span> · <span style="white-space:nowrap">actualizado __ACTUALIZADO__</span></div>
  </div>
</header>
<div class="sumate">
  <div class="su-txt"><b>¿Manejas un centro, refugio u hospital — o quieres colaborar?</b><span>Súmate a la red gratis; toma unos 2 minutos.</span></div>
  <div class="su-btns">
    <a class="su-btn su-web" href="/registrar.html">🌐 Registrarme aquí</a>
    <a class="su-btn su-wa" href="https://wa.me/message/6TEVCZCHJ2TPN1" target="_blank" rel="noopener"><svg width="17" height="17" viewBox="0 0 24 24" fill="#fff" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>WhatsApp</a>
    <a class="su-btn su-tg" href="https://t.me/angelesapoyove_bot" target="_blank" rel="noopener"><svg width="17" height="17" viewBox="0 0 24 24" fill="#fff" aria-hidden="true"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>Telegram</a>
  </div>
</div>
<div class="secmenu" id="secmenu"></div>
<div class="wrap" role="main">
  <div class="search"><span class="ico">🔎</span>
    <input id="q" placeholder="Buscar por lugar, nombre, necesidad…"></div>
  <select class="zona-sel" id="zona"></select>
  <div id="ahora"></div>
  <div id="contenido">__PRERENDER__</div>
  <div class="compartir-pagina">
    <button class="btn b-share" id="share-page">📤 Compartir esta página</button>
    <button class="btn" id="copy-page">📋 Copiar texto</button>
  </div>
  <div class="aviso">⚠️ <b>Información colaborativa, no oficial</b> — parte se recopila del canal <b>__FUENTE__</b> y puede tener errores; <b>verifica antes de actuar</b>. Iniciativa independiente, sin afiliación con dicho canal ni con organismos oficiales.</div>
</div>
<footer>
  <div class="f-inner">
    <div>
      <div class="f-marca"><img src="logo.svg?v=ofc" alt="">__TITULO__</div>
      <p class="f-mision">Plataforma ciudadana e independiente que conecta la necesidad con la ayuda en Venezuela: centros de acopio, refugios y hospitales, con su ubicación, capacidad y necesidades en tiempo real.</p>
    </div>
    <div>
      <div class="f-tit">Enlaces</div>
      <div class="f-item"><a href="/aviso-legal.html">Aviso legal y condiciones de uso</a></div>
      <div class="f-item"><a href="mailto:contacto@redacopioyrefugio.com">contacto@redacopioyrefugio.com</a></div>
      <div class="f-tit" style="margin-top:16px">Síguenos</div>
      <div class="f-item"><a href="https://instagram.com/redacopioyrefugio" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:7px"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style="flex:none"><path d="M12 2.16c3.2 0 3.58.01 4.85.07 1.17.05 1.8.25 2.23.41.56.22.96.48 1.38.9.42.42.68.82.9 1.38.16.42.36 1.06.41 2.23.06 1.27.07 1.65.07 4.85s-.01 3.58-.07 4.85c-.05 1.17-.25 1.8-.41 2.23-.22.56-.48.96-.9 1.38-.42.42-.82.68-1.38.9-.42.16-1.06.36-2.23.41-1.27.06-1.65.07-4.85.07s-3.58-.01-4.85-.07c-1.17-.05-1.8-.25-2.23-.41-.56-.22-.96-.48-1.38-.9-.42-.42-.68-.82-.9-1.38-.16-.42-.36-1.06-.41-2.23-.06-1.27-.07-1.65-.07-4.85s.01-3.58.07-4.85c.05-1.17.25-1.8.41-2.23.22-.56.48-.96.9-1.38.42-.42.82-.68 1.38-.9.42-.16 1.06-.36 2.23-.41 1.27-.06 1.65-.07 4.85-.07M12 0C8.74 0 8.33.01 7.05.07 5.78.13 4.9.33 4.14.63c-.79.31-1.46.72-2.13 1.38C1.34 2.68.94 3.35.63 4.14.33 4.9.13 5.78.07 7.05.01 8.33 0 8.74 0 12s.01 3.67.07 4.95c.06 1.27.26 2.15.56 2.91.31.79.72 1.46 1.38 2.13.67.66 1.34 1.07 2.13 1.38.76.3 1.64.5 2.91.56C8.33 23.99 8.74 24 12 24s3.67-.01 4.95-.07c1.27-.06 2.15-.26 2.91-.56.79-.31 1.46-.72 2.13-1.38.66-.67 1.07-1.34 1.38-2.13.3-.76.5-1.64.56-2.91.06-1.28.07-1.69.07-4.95s-.01-3.67-.07-4.95c-.06-1.27-.26-2.15-.56-2.91-.31-.79-.72-1.46-1.38-2.13-.67-.66-1.34-1.07-2.13-1.38-.76-.3-1.64-.5-2.91-.56C15.67.01 15.26 0 12 0z"/><path d="M12 5.84A6.16 6.16 0 1 0 18.16 12 6.16 6.16 0 0 0 12 5.84zM12 16a4 4 0 1 1 4-4 4 4 0 0 1-4 4z"/><circle cx="18.41" cy="5.59" r="1.44"/></svg>@redacopioyrefugio</a></div>
      <div class="f-tit" style="margin-top:16px">Úsala como app</div>
      <div class="f-item">📲 En tu teléfono: menú del navegador → <b style="color:#fff">"Agregar a pantalla de inicio"</b>.</div>
    </div>
  </div>
  <div class="f-legal">Fuente: __FUENTE__ · Recopilado con IA · No es información oficial, siempre verifica.<br>Iniciativa independiente: no afiliada a ningún grupo de rescate ni a organismos oficiales.
  <div id="contador" style="margin-top:10px"></div></div>
</footer>
<div class="lightbox" id="lightbox"><img id="lbimg" alt=""><div class="cerrar">✕ Toca para cerrar</div></div>
<div class="modal-bg" id="atender-bg"><div class="modal-card" id="atender-card"></div></div>
<script>
const CATS=__CATS__, DATOS=__DATOS__, AVISO=__AVISO__;
const SITE_URL="https://redacopioyrefugio.com";
const SHARE_MSG="📍 Red de Acopio y Refugio — directorio en tiempo real de centros de acopio, refugios y hospitales en Venezuela: dónde están, su capacidad, qué necesitan y qué insumos tienen disponibles para coordinar la ayuda.";
const EMERGENCIAS=__EMERGENCIAS__;
const PLATAFORMAS=__PLATAFORMAS__;
const SERVICIOS=__SERVICIOS__;
const NECESIDADES=__NECESIDADES__;
const SEGUIMIENTO=__SEGUIMIENTO__;
const CANAL=__CANAL__;
const OFERTA=__OFERTA__;
const DIRECTORIO=__DIRECTORIO__;
const ALIADOS=__ALIADOS__;
const LLAMADOS=__LLAMADOS__;
const PEDIDOS=__PEDIDOS__;
const RESUMEN=__RESUMEN__;
const SISMOS=__SISMOS__;
let miUbic=null;
let msCat="todas";
const ORDEN=["heridos","rescate","desaparecidos","mascotas","ofrecimientos","donaciones","info","contacto"];
// Aterrizaje action-first (pedido de Gustavo): el visitante nuevo cae en "Lo que
// hace falta" (necesidades con barra y botón «Lo llevo»), no en el texto de Registro.
let activo="necesidades", q="", zonaSel="todas";
const DESAP_LOTE=20;
let desapMostrar=DESAP_LOTE;

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function tiempoDesde(iso){
  const d=new Date(iso),min=Math.round((new Date()-d)/60000);
  if(min<1)return"ahora"; if(min<60)return"hace "+min+" min";
  const h=Math.round(min/60); if(h<24)return"hace "+h+" h";
  return"hace "+Math.round(h/24)+" d";
}
function telDe(t){const m=String(t||"").match(/\\+?\\d[\\d\\s().\\-]{6,}\\d/);return m?m[0].replace(/[^\\d+]/g,""):null;}
function coincide(txt){return !q || txt.toLowerCase().includes(q);}
function vacio(msg){return `<div class="vacio"><span class="em">🕊️</span>${esc(msg)}</div>`;}
function matchZona(x){return zonaSel==="todas" || (x.zona||"Otras zonas")===zonaSel;}
function itemsTab(){
  if(activo==="info"||activo==="apoya"||activo==="buscar"||activo==="emergencias"||activo==="sismos"||activo==="servicios"||activo==="situacion"||activo==="necesidades"||activo==="oferta"||activo==="directorio"||activo==="manos"||activo==="llamados"||activo==="pedir"||activo==="angeles") return [];
  if(activo==="desaparecidos") return DATOS.filter(m=>m.categoria==="desaparecidos");
  // La categoría "info" del canal navega con la clave "alertas": la clave "info"
  // ya la usa la sección Registro y chocaban.
  const k=activo==="alertas"?"info":activo;
  return DATOS.filter(m=>m.categoria===k);
}
function poblarZonas(items){
  const sel=document.getElementById("zona");
  const zonas=[...new Set(items.map(x=>x.zona||"Otras zonas"))]
    .sort((a,b)=> a==="Otras zonas"?1 : b==="Otras zonas"?-1 : a.localeCompare(b,"es"));
  sel.innerHTML=`<option value="todas">📍 Todas las zonas</option>`+
    zonas.map(z=>`<option value="${esc(z)}">${esc(z)}</option>`).join("");
  if(zonaSel!=="todas" && zonas.includes(zonaSel)) sel.value=zonaSel;
  else { zonaSel="todas"; sel.value="todas"; }
  sel.style.display = zonas.length>1 ? "" : "none";
}
function mapa(ubic){return ubic?`<a class="btn" target="_blank" rel="noopener" href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(ubic+" Venezuela")}">🗺️ Cómo llegar</a>`:"";}
function compartirBtn(texto,url){return `<button class="btn b-share" data-share="${esc(texto)}" data-url="${esc(url||"")}">💬 Compartir</button>`;}
function copiarBtn(url){return url?`<button class="btn" data-copiar="${esc(url)}">🔗 Copiar enlace</button>`:"";}
function denunBtn(tipo,id){return `<button class="btn b-report" data-denunciar="${tipo}:${id}">🚩 Denunciar</button>`;}

function listaTabs(){
  const t=[];
  if(LLAMADOS.length) t.push({key:"llamados",ico:"📣",nom:"Llamados de apoyo",n:LLAMADOS.length});
  if(PEDIDOS.length) t.push({key:"pedir",ico:"🆘",nom:"Pedidos de ayuda",n:PEDIDOS.length});
  if(DIRECTORIO.length) t.push({key:"directorio",ico:"🗺️",nom:"Red de Acopios y Centros de Ayuda",n:DIRECTORIO.length});
  if(ALIADOS.length) t.push({key:"manos",ico:"🤝",nom:"Manos Solidarias",n:ALIADOS.length});
  t.push({key:"info",ico:"📋",nom:"Registro"});
  if(OFERTA.length) t.push({key:"oferta",ico:"🎁",nom:"Disponible para entregar",n:OFERTA.length});
  if(NECESIDADES.length||SEGUIMIENTO.length) t.push({key:"necesidades",ico:"📥",nom:"Lo que hace falta",n:NECESIDADES.reduce((s,p)=>s+p.items.length,0)+SEGUIMIENTO.reduce((s,p)=>s+p.items.length,0)});
  // Todo lo que se alimenta del canal de emergencias viales va agrupado en un
  // solo desplegable "Ángeles de la Autopista" (categorías + Servicios + Situación).
  const sub=[];
  if(RESUMEN) sub.push({key:"situacion",ico:"📊",nom:"Situación"});
  ORDEN.forEach(k=>{const n=DATOS.filter(m=>m.categoria===k).length; if(n) sub.push({key:k==="info"?"alertas":k,ico:CATS[k].icono,nom:CATS[k].nombre,n});});
  if(SERVICIOS.length) sub.push({key:"servicios",ico:"🔌",nom:"Servicios",n:SERVICIOS.length});
  if(sub.length) t.push({key:"angeles",ico:"🚨",nom:"Ángeles de la Autopista",n:sub.reduce((s,x)=>s+(x.n||0),0),sub});
  t.push({key:"buscar",ico:"🔦",nom:"Buscar persona"});
  t.push({key:"emergencias",ico:"📞",nom:"Emergencias"});
  t.push({key:"sismos",ico:"🌎",nom:"Sismos"});
  t.push({key:"apoya",ico:"🤍",nom:"Apoya la plataforma"});
  return t;
}
function pintarTabs(){
  const cont=document.getElementById("secmenu");
  if(!cont) return;
  const T=listaTabs();
  if(!T.length){cont.innerHTML="";return;}
  const CORE=["llamados","pedir","info","directorio","manos","necesidades","oferta","angeles"];
  const planos=T.flatMap(t=>t.sub?t.sub:[t]);
  if(!activo || (!planos.some(t=>t.key===activo) && !CORE.includes(activo))) activo=T[0].key;
  const grupo=T.find(t=>t.sub && t.sub.some(s=>s.key===activo));
  const chip=(t,on,sub)=>`<button class="secchip${sub?" sub":""}${on?" on":""}" data-k="${t.key}">${t.ico} ${esc(t.nom)}${t.n!=null?` <span class="pill">${t.n}</span>`:""}</button>`;
  cont.innerHTML=T.map(t=>chip(t,(t.key===activo)||(!!grupo&&grupo.key===t.key),false)).join("")
    +(grupo?`<div class="subchips">`+grupo.sub.map(s=>chip(s,s.key===activo,true)).join("")+`</div>`:"");
  cont.querySelectorAll(".secchip[data-k]").forEach(el=>el.onclick=()=>{
    const k=el.dataset.k, g=T.find(t=>t.key===k && t.sub);
    irTab(g?g.sub[0].key:k);
  });
}

function tarjeta(m){
  const C=CATS[m.categoria]||{icono:"•",nombre:m.categoria};
  const tel=telDe(m.contacto);
  const fecha=`${tiempoDesde(m.tg_fecha)}${m.vigencia==="posible_desactualizado"?'<span class="vieja">posible viejo</span>':""}`;
  const sh=`${m.titulo}`+(m.ubicacion?` — 📍 ${m.ubicacion}`:"")+` — ${m.detalle}`+(m.contacto?` — 📞 ${m.contacto}`:"");
  // Si el mensaje original trae bastante más que el resumen (ej. una lista de
  // nombres), lo mostramos en un desplegable para no perder esa info.
  const _orig=(m.texto_original||"").trim();
  const completo=(_orig && _orig.length>(m.detalle||"").length+60)
    ? `<details class="completo"><summary>📋 Ver lista / mensaje completo</summary><div class="completo-txt">${esc(_orig)}</div></details>`
    : "";
  return `<div class="card">
    <div class="cab"><span class="ico">${C.icono}</span>
      <div><div class="cat">${esc(C.nombre)}</div><h3>${esc(m.titulo)}</h3></div></div>
    ${m.foto?`<img class="foto" src="${esc(m.foto)}" loading="lazy" alt="">`:""}
    ${m.ubicacion?`<div class="meta">📍 <b>${esc(m.ubicacion)}</b></div>`:""}
    <div class="det">${esc(m.detalle)}</div>${completo}
    ${m.necesidades?`<div class="meta">🧰 ${esc(m.necesidades)}</div>`:""}
    ${m.contacto&&tel?`<a class="btn-tel" href="tel:${tel}">📞 Llamar ${esc(m.contacto)}</a>`:(m.contacto?`<div class="meta">📞 ${esc(m.contacto)}</div>`:"")}
    <div class="acciones">${mapa(m.ubicacion)}${compartirBtn(sh,m.link)}</div>
    <div class="pie"><span>${fecha}</span>
      <a href="${esc(m.link)}" target="_blank" rel="noopener">ver original ↗</a></div>
  </div>`;
}
const DESAP_EST={buscando:["🟡 Buscando","e-buscando"],aparecio:["✅ Apareció","e-aparecio"],fallecida:["🕯️ Fallecida","e-fallecida"]};
function itemsDesap(){
  const ord={buscando:0,aparecio:1,fallecida:2};
  return DATOS.filter(m=>m.categoria==="desaparecidos" && matchZona(m) && coincide(m.titulo+" "+(m.ubicacion||"")+" "+m.detalle))
    .sort((a,b)=>(ord[a.estado_persona||"buscando"]||0)-(ord[b.estado_persona||"buscando"]||0));
}
function dpersonCard(m){
  const est=m.estado_persona||"buscando", e=DESAP_EST[est]||DESAP_EST.buscando;
  const tel=telDe(m.contacto);
  const sh=`🔎 SE BUSCA: ${m.titulo}`+(m.ubicacion?` — visto en ${m.ubicacion}`:"")+(m.detalle?` — ${m.detalle}`:"")+(m.contacto?` — 📞 ${m.contacto}`:"");
  const reportar=(est==="buscando")?`<button class="btn b-report" data-reportar="${m.tg_id}">✋ Reportar novedad</button>`:"";
  return `<div class="dperson${est!=="buscando"?" resuelto":""}">
    ${m.foto?`<img class="fimg" src="${esc(m.foto)}" loading="lazy" alt="">`:`<div class="fimg"></div>`}
    <div class="dbody"><span class="ebadge ${e[1]}">${e[0]}</span><h3>${esc(m.titulo)}</h3>
      ${m.ubicacion?`<div class="meta">📍 ${esc(m.ubicacion)}</div>`:""}
      ${m.detalle?`<div class="meta">${esc(m.detalle)}</div>`:""}
      ${est==="buscando"&&tel?`<a class="btn-tel" href="tel:${tel}">📞 Llamar</a>`:(m.contacto?`<div class="meta">📞 ${esc(m.contacto)}</div>`:"")}
      <div class="acciones">${compartirBtn(sh,m.link)}${reportar}</div>
      <div class="pie"><span>${tiempoDesde(m.tg_fecha)}</span>
        <a href="${esc(m.link)}" target="_blank" rel="noopener">ver ↗</a></div>
    </div></div>`;
}
function masDesapHTML(restantes){
  return restantes>0
    ? `<div id="mas-desap-wrap" style="text-align:center;margin:14px 0"><button class="btn" id="mas-desap" onclick="cargarMasDesap()">⬇️ Cargar ${Math.min(DESAP_LOTE,restantes)} más (quedan ${restantes})</button></div>`
    : "";
}
function renderDesap(){
  const items=itemsDesap();
  if(!items.length) return vacio("No hay reportes de desaparecidos.");
  return `<div class="galeria">`+items.slice(0,desapMostrar).map(dpersonCard).join("")+`</div>`+masDesapHTML(items.length-desapMostrar);
}
// Carga el siguiente lote AÑADIENDO tarjetas al final (no repinta las ya mostradas:
// con cientos en pantalla, un re-render completo se siente lento en teléfonos sencillos).
let _cargandoMas=false;
function cargarMasDesap(){
  if(_cargandoMas) return;
  const btn=document.getElementById("mas-desap"), gal=document.querySelector("#contenido .galeria");
  if(!btn||!gal){ desapMostrar+=DESAP_LOTE; render(); return; }
  _cargandoMas=true; btn.disabled=true;
  btn.innerHTML=`<span class="spin"></span> Cargando…`;
  setTimeout(()=>{ // deja que el spinner alcance a pintarse antes del trabajo de DOM
    _cargandoMas=false;
    if(activo!=="desaparecidos") return;
    const items=itemsDesap();
    const nuevos=items.slice(desapMostrar,desapMostrar+DESAP_LOTE);
    desapMostrar+=nuevos.length;
    gal.insertAdjacentHTML("beforeend",nuevos.map(dpersonCard).join(""));
    const restantes=items.length-desapMostrar;
    if(restantes>0){ btn.disabled=false; btn.innerHTML=`⬇️ Cargar ${Math.min(DESAP_LOTE,restantes)} más (quedan ${restantes})`; }
    else{ const w=document.getElementById("mas-desap-wrap"); if(w) w.remove(); }
  },60);
}
// Nota: NADA de scroll infinito a propósito (decisión 2026-07-04): en el celular hace
// que la página "no termine nunca". Cargar más tarjetas es SIEMPRE un toque explícito.
function renderCategoria(k){
  const items=DATOS.filter(m=>m.categoria===k && matchZona(m) && coincide(m.titulo+" "+(m.ubicacion||"")+" "+m.detalle+" "+(m.necesidades||"")));
  if(!items.length) return vacio("No hay nada que coincida.");
  return items.map(tarjeta).join("");
}
function renderEmergencias(){
  let html=`<div class="aviso">Números de referencia (base: redayudavenezuela.com). Verifica antes de llamar.</div>`;
  let hay=false;
  EMERGENCIAS.forEach(g=>{
    const items=g.items.filter(it=>coincide(it.nombre+" "+it.numeros.join(" ")+" "+g.grupo));
    if(!items.length) return;
    hay=true;
    html+=`<h3 class="grupo-titulo">${esc(g.grupo)}</h3>`;
    html+=items.map(it=>{
      const botones=it.numeros.map(n=>{
        const tel=String(n).replace(/[^0-9+*#]/g,"");
        return `<a class="btn-tel" href="tel:${tel}">📞 ${esc(n)}</a>`;
      }).join("");
      return `<div class="centro"><div class="info">
        <div class="cn">${esc(it.nombre)}</div>
        <div class="acciones">${botones}</div>
      </div></div>`;
    }).join("");
  });
  return hay ? html : vacio("Sin resultados.");
}
function statCard(num,label,color){
  return `<div class="stat" style="border-top-color:${color}"><div class="stat-n">${esc(String(num))}</div><div class="stat-l">${esc(label)}</div></div>`;
}
function renderSituacion(){
  const r=RESUMEN||{};
  let html=`<div class="aviso">📊 Panorama de la emergencia según lo recopilado del canal. Cifras <b>referenciales, no oficiales</b> — verifica.</div>`;
  html+=`<div class="stats">`+
    statCard(r.desaparecidos_buscando,"Desaparecidos en búsqueda","var(--desaparecidos)")+
    statCard(r.desaparecidos_resueltos,"Personas localizadas","var(--disponible)")+
    statCard(r.heridos,"Reportes de heridos","var(--heridos)")+
    statCard(r.rescate,"Pedidos de rescate","var(--rescate)")+
    (r.ofrecimientos?statCard(r.ofrecimientos,"Ofrecimientos de ayuda","var(--ofrecimientos)"):"")+
    (r.servicios_zonas?statCard(r.servicios_zonas,"Zonas con reporte de servicios","var(--info)"):"")+
  `</div>`;
  if(r.nuevos_24h) html+=`<div class="cambios">🆕 <b>${esc(String(r.nuevos_24h))}</b> actualizaciones nuevas en las últimas 24 h.</div>`;
  html+=`<div class="pie" style="justify-content:center;margin-top:14px">Actualizado: ${esc(r.actualizado||"")}</div>`;
  return html;
}
const PUNTO_ICO={acopio:"📦",refugio:"🏠",hospital:"🏥",cdi:"🩺",maternidad:"🤰",clinica:"➕",fundacion:"🏛",otro:"📍"};
const PUNTO_NOM={acopio:"Centro de acopio",refugio:"Refugio",hospital:"Hospital",cdi:"CDI",maternidad:"Maternidad",clinica:"Clínica",fundacion:"Fundación",otro:"Punto"};
// Orden y etiqueta de las secciones (encabezados) dentro de Requerimientos y Directorio.
const PUNTO_GRUPOS=[["acopio","📦 Centros de acopio"],["refugio","🏠 Refugios"],["hospital","🏥 Hospitales"],["cdi","🩺 CDI"],["maternidad","🤰 Maternidades"],["clinica","➕ Clínicas"],["fundacion","🏛 Fundaciones aliadas"],["otro","📍 Otros puntos"]];
// Agrupa los puntos por tipo, respetando PUNTO_GRUPOS; solo devuelve las secciones con datos.
function agruparPorTipo(items){
  const map={};
  items.forEach(p=>{const k=PUNTO_ICO[p.tipo_punto]?p.tipo_punto:"otro";(map[k]=map[k]||[]).push(p);});
  return PUNTO_GRUPOS.filter(([k])=>map[k]&&map[k].length).map(([k,lbl])=>[lbl,map[k]]);
}
const DIR_CAMPOS=[
  ["tipo_inmueble","🏚️ Inmueble"],["aforo","👥 Aforo"],["banos","🚻 Baños"],["duchas","🚿 Duchas"],
  ["cocina","🍳 Cocina"],["agua_potable","🚰 Agua potable"],["electricidad","⚡ Electricidad"],
  ["planta","🔋 Planta/generador"],["camas","🛏️ Camas disp."],["acceso_vehiculos","🚛 Acceso vehículos"],
  ["accesibilidad","♿ Accesibilidad"],["almacenamiento","📦 Almacenamiento"],
  ["refrigeracion","❄️ Refrigeración"],["cobertura","🗺️ Cobertura"],["areas","🎯 Atiende"],["horario","🕐 Horario"]];
function dirClase(v){
  const s=(v||"").toString().trim().toLowerCase();
  if(s==="sí"||s==="si") return "yes";
  if(s==="no") return "non";
  if(s.startsWith("no aplica")||s.startsWith("no posee")||s==="") return "naa";
  return "";
}
function dirIcono(v){const c=dirClase(v);return c==="yes"?"✅ ":c==="non"?"🚫 ":c==="naa"?"➖ ":"";}
function dirItem(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const sub=[PUNTO_NOM[p.tipo_punto]||"Punto",p.ubicacion].filter(Boolean).map(esc).join(" · ");
  const abrir=q?" open":"";
  let grid="";
  DIR_CAMPOS.forEach(([k,lbl])=>{
    const v=(p[k]||"").toString().trim(); if(!v) return;
    const wide=(k==="almacenamiento")?" wide":"";
    grid+=`<div class="dir-f${wide}"><div class="dir-k">${lbl}</div><div class="dir-v ${dirClase(v)}">${dirIcono(v)}${esc(v)}</div></div>`;
  });
  if(p.ubicacion) grid+=`<div class="dir-f wide"><div class="dir-k">📍 Ubicación</div><div class="dir-v">${esc(p.ubicacion)}</div></div>`;
  const tel=telDe(p.contacto);
  const sh=`${ico} ${PUNTO_NOM[p.tipo_punto]||"Punto"}: ${p.punto}`+(p.ubicacion?` — 📍 ${p.ubicacion}`:"")+(p.contacto?` — 📞 ${p.contacto}`:"");
  return `<details class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">${ico} ${esc(p.punto)}</span><span class="nec-sub">${sub}</span></summary>
    <div class="nec-body">
      <div class="dir-grid">${grid}</div>
      ${p.responsable?`<div class="dir-resp"><div class="dir-k">🧑‍💼 Responsable</div><div class="dir-v">${esc(p.responsable)}</div></div>`:""}
      ${tel?`<a class="btn-tel" href="tel:${tel}" style="margin-top:8px">📞 Llamar ${esc(p.contacto)}</a>`:(p.contacto?`<div class="meta" style="margin-top:8px">📞 ${esc(p.contacto)}</div>`:"")}
      <div class="acciones">${mapaBtn(p)}${compartirBtn(sh, paginaPunto(p))}${copiarBtn(paginaPunto(p))}${denunBtn("centro",p.id)}</div>
    </div>
  </details>`;
}
function tieneGps(p){return p.lat!=null&&p.lon!=null&&p.lat!==""&&p.lon!==""&&!isNaN(+p.lat)&&!isNaN(+p.lon);}
function mapsUrl(p){return tieneGps(p)?`https://www.google.com/maps/dir/?api=1&destination=${p.lat},${p.lon}`:(p.ubicacion?`https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(p.ubicacion+" Venezuela")}`:SITE_URL);}
function mapaBtn(p){return (tieneGps(p)||p.ubicacion)?`<a class="btn" target="_blank" rel="noopener" href="${mapsUrl(p)}">🗺️ Cómo llegar</a>`:"";}
function renderDirectorio(){
  const items=DIRECTORIO.filter(p=>coincide(p.punto+" "+(p.tipo_punto||"")+" "+(p.tipo_inmueble||"")+" "+(p.ubicacion||"")+" "+(p.responsable||"")));
  if(!items.length) return vacio("Aún no hay puntos con ficha completa en el directorio.");
  const sinGps=items.filter(p=>!tieneGps(p)).length;
  let html=`<div class="aviso">🗺️ <b>Red de Acopios y Centros de Ayuda</b>: cada punto de la red de colaboradores en el mapa, con su ficha (qué tiene: agua, camas, electricidad…) completada por sus responsables vía el bot. Toca un pin o un punto para ver los detalles. <b>No es información oficial</b>, verifica.</div>`;
  html+=`<div id="mapa-red" class="mapa-red"></div>`+MAPA_LEYENDA;
  if(sinGps) html+=`<div class="aviso aviso-mini">📍 ${sinGps} punto${sinGps>1?"s":""} todavía sin ubicación en el mapa. Su responsable la comparte enviando <b>/ubicacion</b> al bot.</div>`;
  agruparPorTipo(items).forEach(([lbl,gr])=>{
    html+=`<h3 class="grupo-titulo">${lbl} · ${gr.length}</h3>`;
    html+=gr.map(dirItem).join("");
  });
  // Autoregistro: puntos DETECTADOS por el canal Emergencias Viales que aún no
  // tienen responsable en la red — cada uno con su botón de reclamo (el registro
  // llega pre-llenado con los datos que el canal ya dio).
  // APARCADO por Gustavo (2026-07-05): riesgo de suplantación en el reclamo abierto.
  // Para activarlo: RECLAMO_CANAL=true + backend con aprobación manual de reclamos.
  const RECLAMO_CANAL=false;
  const canal=RECLAMO_CANAL?CANAL.filter(c=>coincide(c.nombre+" "+(c.ubicacion||""))):[];
  if(canal.length){
    html+=`<h3 class="grupo-titulo">📡 Detectados en el canal · ${canal.length}</h3>`;
    html+=`<div class="aviso aviso-mini">Estos puntos aparecieron en el canal <b>Emergencias Viales</b> pero aún <b>no tienen responsable</b> en la red. ¿Manejas alguno? <b>Reclámalo</b>: confirmas sus datos en 2 minutos y la red te hace llegar justo lo que te falta.</div>`;
    html+=canal.map(canalItem).join("");
  }
  return html;
}
function canalItem(c){
  const est=c.estado==="disponible"?`<span class="urg" style="background:#e7f6f4;color:#0b5f59">🟢 Recibiendo</span>`
           :(c.estado==="lleno"?`<span class="urg" style="background:#fdeaea;color:#9b2226">🔴 Lleno</span>`:"");
  const url="/registrar.html?canal=1&tipo="+(c.salud?"hospital":"acopio")
    +"&punto="+encodeURIComponent(c.nombre)+"&ubicacion="+encodeURIComponent(c.ubicacion||"");
  return `<div class="centro">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><b>${c.salud?"🏥":"📦"} ${esc(c.nombre)}</b>${est}<span class="meta">· ${c.reportes} reporte${c.reportes==1?"":"s"} del canal</span></div>
    ${c.ubicacion?`<div class="meta" style="margin-top:4px">📍 ${esc(c.ubicacion)}</div>`:""}
    <div class="meta" style="margin-top:2px">🕐 ${tiempoDesde(c.tg_fecha)}</div>
    <div class="acciones"><a class="btn b-llevar b-accweb" href="${url}">🏠 Es mi punto — reclamarlo</a></div>
  </div>`;
}
// Lazy-load de Leaflet (mapa) solo al abrir la pestaña; cachea la promesa.
let _leafletP=null;
function cargarLeaflet(){
  if(window.L) return Promise.resolve();
  if(_leafletP) return _leafletP;
  _leafletP=new Promise((res,rej)=>{
    const css=document.createElement("link");
    css.rel="stylesheet"; css.href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    css.crossOrigin=""; document.head.appendChild(css);
    const s=document.createElement("script");
    s.src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    s.crossOrigin=""; s.onload=res; s.onerror=rej; document.head.appendChild(s);
  });
  return _leafletP;
}
// --- Mapa compartido: capa de centros (navy) + capa de Manos Solidarias (teal) ---
const MS_ICO={comida:"🍲",herramientas:"🔦",transporte:"🚐",agua:"💧",carga:"🔌",salud:"🩹",hospedaje:"🛏️",remoto:"💻",estructural:"🏗️",oficios:"🛠️",otro:"📦"};
const MS_LABEL={comida:"Comida",herramientas:"Herramientas/linternas",transporte:"Transporte",agua:"Agua",carga:"Carga de teléfonos",salud:"Salud",hospedaje:"Hospedaje temporal",remoto:"Remoto / En Línea",estructural:"Estructural / Inspección",oficios:"Oficios",otro:"Otra ayuda"};
const MAPA_LEYENDA=`<div class="leyenda"><span><span class="leg-dot" style="background:#003C67"></span>Centros de la red</span><span><span class="leg-dot" style="background:#1E9C94"></span>Manos Solidarias</span></div>`;
function waLink(c){const d=telDe(c);if(!d)return"";let n=d.replace(/[^0-9]/g,"");if(d.startsWith("+"))return"https://wa.me/"+n;if(n.startsWith("0"))n="58"+n.slice(1);else if(!n.startsWith("58"))n="58"+n;return"https://wa.me/"+n;}
let _mapa=null;
function popupRed(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const tel=telDe(p.contacto);
  const llegar=mapsUrl(p);
  return `<div class="pop"><b>${ico} ${esc(p.punto)}</b><br>`+
    `<span class="pop-sub">${esc(PUNTO_NOM[p.tipo_punto]||"Punto")}${p.ubicacion?" · "+esc(p.ubicacion):""}</span><br>`+
    (tel?`<a href="tel:${tel}">📞 ${esc(p.contacto)}</a> · `:"")+
    `<a href="${llegar}" target="_blank" rel="noopener">🗺️ Cómo llegar</a></div>`;
}
function popupMS(a){
  const ico=MS_ICO[a.categoria]||"🤝";
  const llegar=mapsUrl(a);
  return `<div class="pop"><b>${ico} ${esc(a.nombre)}</b><br>`+
    `<span class="pop-sub">${esc(MS_LABEL[a.categoria]||"Ayuda")}${a.zona?" · "+esc(a.zona):""}</span><br>`+
    `<span style="font-size:12px">${esc(a.descripcion||"")}</span><br>`+
    `<a href="#" onclick="abrirContactoMS(${a.id});return false">🤝 Solicitar contacto</a> · `+
    `<a href="${llegar}" target="_blank" rel="noopener">🗺️ Cómo llegar</a></div>`;
}
function pinIcon(emoji,color){return L.divIcon({className:"",html:`<div style="width:28px;height:28px;border-radius:50% 50% 50% 0;background:${color};transform:rotate(-45deg);border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center"><span style="transform:rotate(45deg);font-size:13px">${emoji}</span></div>`,iconSize:[28,28],iconAnchor:[14,26],popupAnchor:[0,-24]});}
function initMapa(id){
  const el=document.getElementById(id); if(!el) return;
  cargarLeaflet().then(()=>{
    const cont=document.getElementById(id); if(!cont||!window.L) return;
    if(_mapa){try{_mapa.remove();}catch(e){}_mapa=null;}
    const map=L.map(cont,{scrollWheelZoom:false}).setView([7.9,-66.0],6); // Venezuela
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:'&copy; OpenStreetMap'}).addTo(map);
    const coords=[];
    DIRECTORIO.filter(tieneGps).forEach(p=>{
      L.marker([+p.lat,+p.lon],{icon:pinIcon(PUNTO_ICO[p.tipo_punto]||"📦","#003C67")}).addTo(map).bindPopup(popupRed(p));
      coords.push([+p.lat,+p.lon]);
    });
    ALIADOS.filter(tieneGps).forEach(a=>{
      L.marker([+a.lat,+a.lon],{icon:pinIcon(MS_ICO[a.categoria]||"🤝","#1E9C94")}).addTo(map).bindPopup(popupMS(a));
      coords.push([+a.lat,+a.lon]);
    });
    if(coords.length===1) map.setView(coords[0],14);
    else if(coords.length>1) map.fitBounds(coords,{padding:[40,40],maxZoom:13});
    _mapa=map;
    setTimeout(()=>map.invalidateSize(),120);
  }).catch(()=>{el.innerHTML='<div class="vacio"><span class="em">🗺️</span>No se pudo cargar el mapa. Revisa tu conexión.</div>';});
}
function renderManos(){
  let items=ALIADOS.filter(a=>coincide(a.nombre+" "+(a.categoria||"")+" "+(MS_LABEL[a.categoria]||"")+" "+(a.descripcion||"")+" "+(a.zona||"")));
  if(msCat!=="todas") items=items.filter(a=>a.categoria===msCat);
  let html=`<div class="aviso aviso-ms">🤝 <b>Manos Solidarias</b>: personas de la comunidad que ofrecen ayuda <b>gratis</b> (comida, transporte, salud, inspección estructural, ayuda en línea…). Toca un pin o una tarjeta para contactar. <b>No es información oficial</b>, verifica.</div>`;
  html+=`<div style="text-align:center;margin:8px 0 12px"><a class="btn" href="/ofrecer.html" style="background:#1E9C94;color:#fff;font-weight:800;border-color:#1E9C94">➕ Ofrecer mi ayuda</a></div>`;
  const cats=[...new Set(ALIADOS.map(a=>a.categoria))];
  html+=`<div class="ms-chips"><span class="ms-chip${msCat==="todas"?" on":""}" onclick="filtrarMS('todas')">Todos</span>`;
  cats.forEach(c=>{html+=`<span class="ms-chip${msCat===c?" on":""}" onclick="filtrarMS('${esc(c)}')">${MS_ICO[c]||"📦"} ${esc(MS_LABEL[c]||c)}</span>`;});
  html+=`</div>`;
  html+=`<div id="mapa-ms" class="mapa-red"></div>`+MAPA_LEYENDA;
  if(!items.length){html+=vacio("No hay aliados en esta categoría.");return html;}
  html+=items.map(msItem).join("");
  return html;
}
function msItem(a){
  const ico=MS_ICO[a.categoria]||"🤝";
  const sub=[MS_LABEL[a.categoria]||"Ayuda",a.zona].filter(Boolean).map(esc).join(" · ");
  const llegar=tieneGps(a)?`<a class="btn" target="_blank" rel="noopener" href="${mapsUrl(a)}">🗺️ Cómo llegar</a>`:"";
  const hor=a.horario?`<div class="meta" style="margin-top:6px">🕐 ${esc(a.horario)}</div>`:"";
  const dsc=(a.descripcion||"");
  const sh=`🤝 ${a.nombre} — ${MS_LABEL[a.categoria]||"ayuda"}: ${dsc.length>120?dsc.slice(0,119).trim()+"…":dsc}`+(a.zona?` (${a.zona})`:"")+" — pide su contacto en la web";
  const urlMS=paginaAliado(a);
  return `<div class="centro ms-card" id="ms-${a.id}">
    <div class="ms-top"><span class="ms-ico">${ico}</span><div><div class="ms-nom">${esc(a.nombre)}</div><div class="ms-sub">${sub}</div></div></div>
    <div class="ms-desc">${esc(a.descripcion||"")}</div>${hor}
    <div class="acciones"><button class="btn b-wa" data-contactar="${a.id}">🤝 Solicitar contacto</button>${llegar}${compartirBtn(sh, urlMS)}${copiarBtn(urlMS)}${denunBtn("aliado",a.id)}</div>
  </div>`;
}
function filtrarMS(c){msCat=c;render();}
function renderLlamados(){
  let items=LLAMADOS.filter(l=>coincide((l.sector||"")+" "+(l.mensaje||"")));
  let html=`<div class="aviso">📣 <b>Llamados de apoyo</b>: solicitudes que levantan nuestros <b>voluntarios</b> en terreno, de sectores que siguen en emergencia y con poca cobertura. Si puedes ayudar, <b>compártelos</b> para que la ayuda llegue. <b>No es información oficial</b>, verifica.</div>`;
  if(!items.length){html+=vacio("No hay llamados de apoyo activos ahora.");return html;}
  html+=items.map(llamadoItem).join("");
  return html;
}
function llamadoItem(l){
  const msj=(l.mensaje||"");
  const sh=`📣 Llamado de apoyo${l.sector?` — ${l.sector}`:""}: ${msj.length>140?msj.slice(0,139).trim()+"…":msj}`;
  const urlLla=paginaLlamado(l);
  const nresp=l.respuestas||0;
  let estado="";
  if(nresp>0){
    estado=`<span class="lla-resp">🤝 ${nresp} ${nresp==1?"persona respondiendo":"personas respondiendo"}</span>`;
  }else{
    const h=(Date.now()-new Date(l.creado_en).getTime())/3.6e6;
    if(h>6) estado=`<span class="lla-sin">⚠️ Aún sin respuesta — ¿puedes tú?</span>`;
  }
  const ayudarWeb=ACCION_LLA?`<button class="btn b-llevar b-accweb" onclick="llaAbrir(${l.id})">🙋 Puedo ayudar — aquí</button>`:"";
  const ayudar=ayudarWeb+(ACCION_LLA?accionBtn("🙋 Puedo ayudar","lla_"+l.id,
    "Hola 🙋 quiero ayudar con el llamado LL-"+l.id+(l.sector?" — "+l.sector:"")):"");
  return `<div class="centro lla-card" id="ll-${l.id}">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="font-size:22px">📣</span>
      <span style="font-weight:700;color:#005B67">${esc(l.sector||"Sector en emergencia")}</span>
    </div>
    ${l.foto?`<img class="foto" src="${esc(l.foto)}" loading="lazy" alt="">`:""}
    <div style="white-space:pre-wrap;line-height:1.45">${esc(l.mensaje||"")}</div>
    ${estado?`<div style="margin-top:8px">${estado}</div>`:""}
    <div class="meta" style="margin-top:6px">🕐 ${tiempoDesde(l.creado_en)}</div>
    <div class="acciones acc-lla">${ayudar}${compartirBtn(sh,urlLla)}${copiarBtn(urlLla)}</div>
  </div>`;
}
// Agrupación de insumos por categoría (para inventarios largos) + buscador interno.
const INV_CATS=[["medicament","💊 Medicamentos"],["farmac","💊 Medicamentos"],["insumo","🩹 Insumos médicos"],["curaci","🩹 Insumos médicos"],["medico","🩹 Insumos médicos"],["agua","💧 Agua y bebidas"],["bebida","💧 Agua y bebidas"],["aliment","🍚 Alimentos"],["comida","🍚 Alimentos"],["bebe","🍼 Bebés"],["panal","🍼 Bebés"],["higiene","🧼 Higiene y aseo"],["aseo","🧼 Higiene y aseo"],["limpieza","🧴 Limpieza"],["ropa","👕 Ropa"],["calzado","👟 Calzado"],["colchon","🛏️ Descanso"],["cobija","🛏️ Descanso"],["herramient","🛠️ Herramientas"]];
function _na(s){return (s||"").toLowerCase().normalize("NFD").replace(/[\\u0300-\\u036f]/g,"");}
// La ropa NO se agrupa genérica: se subclasifica por público (niño/hombre/mujer/bebé),
// detectándolo en categoría + ítem + descripción (el dato puede venir en cualquiera).
const ROPA_LABELS=["👨 Ropa de hombre","👩 Ropa de mujer","🧒 Ropa de niños","🍼 Ropa de bebé","👕 Ropa unisex","👕 Ropa (varios)","👕 Ropa (sin especificar)"];
function ropaBucket(i){
  const txt=_na((i.categoria||"")+" "+(i.item||"")+" "+(i.detalle||""));
  if(txt.includes("bebe")||txt.includes("recien nac")) return "🍼 Ropa de bebé";
  const nino=txt.includes("nino")||txt.includes("nina")||txt.includes("infant")||txt.includes("nene");
  const h=txt.includes("hombre")||txt.includes("caballero")||txt.includes("masculin");
  const m=txt.includes("mujer")||txt.includes("dama")||txt.includes("femenin");
  if([nino,h,m].filter(Boolean).length>1) return "👕 Ropa (varios)";
  if(nino) return "🧒 Ropa de niños";
  if(h) return "👨 Ropa de hombre";
  if(m) return "👩 Ropa de mujer";
  if(txt.includes("unisex")) return "👕 Ropa unisex";
  return "👕 Ropa (sin especificar)";
}
function catLabel(i){
  const cat=_na(i.categoria||"");
  if(cat.includes("ropa")||cat.includes("vestiment")||cat.includes("prenda")) return ropaBucket(i);
  for(const kv of INV_CATS) if(cat.includes(kv[0])) return kv[1];
  return i.categoria ? "📦 "+i.categoria.charAt(0).toUpperCase()+i.categoria.slice(1) : "📦 Otros";
}
function agruparInsumos(items){
  const map={};
  items.forEach(i=>{const l=catLabel(i);(map[l]=map[l]||[]).push(i);});
  const orden=[...new Set(INV_CATS.map(x=>x[1])), ...ROPA_LABELS];
  return Object.keys(map).sort((a,b)=>{
    const ia=orden.indexOf(a), ib=orden.indexOf(b);
    if(ia!==-1||ib!==-1) return (ia===-1?99:ia)-(ib===-1?99:ib);
    return a.localeCompare(b,"es");
  }).map(k=>[k,map[k]]);
}
function invFila(i){
  const cant=(i.cantidad&&i.cantidad.trim())?esc(i.cantidad):"cant.: —";
  const desc=(i.detalle&&i.detalle.trim())?` · <span class="inv-desc">${esc(i.detalle)}</span>`:"";
  const txt=esc(_na((i.item||"")+" "+(i.cantidad||"")+" "+(i.detalle||"")));
  return `<div class="inv-fila" data-txt="${txt}">• <b>${esc(i.item)}</b> — ${cant}${desc}</div>`;
}
function cuerpoInsumos(items){
  if(!items.length) return "";
  const buscador=items.length>12?`<input class="inv-q" type="search" placeholder="🔎 Buscar en esta lista…" oninput="filtrarInv(this)">`:"";
  const grupos=agruparInsumos(items);
  // Cada categoría es un submenú COLAPSABLE (default cerrado; si hay una sola, abierta).
  let h="";
  grupos.forEach(([lbl,gr])=>{
    const abierto=grupos.length===1?" open":"";
    h+=`<details class="inv-cat"${abierto}><summary class="inv-grupo">${esc(lbl)} <span class="inv-n">(${gr.length})</span></summary>`;
    h+=`<div class="inv-cat-body">${gr.map(invFila).join("")}</div></details>`;
  });
  return buscador+`<div class="inv-lista">${h}</div>`;
}
function filtrarInv(inp){
  const t=_na(inp.value.trim()), body=inp.closest(".nec-body");
  body.querySelectorAll(".inv-cat").forEach(cat=>{
    let vis=0;
    cat.querySelectorAll(".inv-fila").forEach(f=>{
      const m=(!t||f.dataset.txt.includes(t)); f.style.display=m?"":"none"; if(m)vis++;
    });
    cat.style.display=vis?"":"none";
    cat.open=t?vis>0:false;   // al buscar abre las categorías con coincidencias; al limpiar, colapsa
  });
}
// Deep link: ancla estable por punto (usa punto_id; cae a slug del nombre si falta).
function pidDe(p){ return p.id!=null ? ("pt-"+p.id) : ("ptn-"+_na(p.punto||"").replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"")); }
function deepLink(tab,p){ return SITE_URL+"#"+tab+":"+pidDe(p); }
// Página compartible del punto (/p/pt-<id>.html, con vista previa propia en WhatsApp/redes).
// En seguimiento/directorio la identidad es punto_id (id ahí es de la necesidad/representante).
function pidPunto(p){return (p.punto_id!=null)?("pt-"+p.punto_id):pidDe(p);}
function paginaPunto(p){return SITE_URL+"/p/"+pidPunto(p)+".html";}
function paginaPedido(p){return SITE_URL+"/s/pd-"+p.id+".html";}
function paginaLlamado(l){return SITE_URL+"/s/ll-"+l.id+".html";}
function paginaAliado(a){return SITE_URL+"/m/ms-"+a.id+".html";}
// --- Acción desde el web → bot (deep link). Genérico: sirve para necesidades y, luego, llamados.
const BOT_USER="angelesapoyove_bot";
const ACCION_NEC=true;  // se activa cuando el bot maneje /start nec_<id> (Fase 5)
const ACCION_LLA=true;  // se activa cuando el bot maneje /start lla_<id> (Fase 5 llamados)
function botLink(payload){ return "https://t.me/"+BOT_USER+"?start="+encodeURIComponent(payload); }
// WhatsApp no tiene deep link con payload: se manda el texto prellenado con un código
// (LL-<id> / ND-<id>) que bot_wa.py reconoce al recibirlo. Mismo flujo que Telegram.
const WA_BOT_NUM="58412XXXXXXX";
function waBotLink(texto){ return "https://wa.me/"+WA_BOT_NUM+"?text="+encodeURIComponent(texto); }
function accionBtn(label,payload,waTexto){
  return `<a class="btn b-llevar b-accwa" target="_blank" rel="noopener" href="${waBotLink(waTexto)}">${label} · WhatsApp</a>`
       + `<a class="btn b-llevar" target="_blank" rel="noopener" href="${botLink(payload)}">${label} · Telegram</a>`;
}
// Compromiso INLINE desde la web (POST /api/comprometer) — tercera vía, sin salir
// de la página. La reserva vence sola (12 h) y el punto recibe el aviso por SU canal.
function _necDe(id){ for(const p of (SEGUIMIENTO||[])){ for(const it of (p.items||[])) if(it.id===id) return {it:it,p:p}; } return null; }
function cmpAbrir(id){
  const n=_necDe(id); if(!n) return;
  const meta=+n.it.meta||0;
  const falta=meta?Math.max(0,meta-(+n.it.recibido||0)-(+n.it.en_camino||0)):"";
  const old=document.getElementById("cmp-ovl"); if(old) old.remove();
  const d=document.createElement("div"); d.className="cmp-ovl"; d.id="cmp-ovl";
  d.innerHTML=`<div class="cmp-box"><button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
    <h3>🚚 Voy a llevar: ${esc(n.it.item||"")}</h3>
    <div style="color:var(--suave);font-size:13px">${esc(n.p.punto||"")}${falta!==""?` · faltan ${falta}`:""}</div>
    <label>¿Cuánto llevas?</label><input id="cmp-cant" type="number" min="1" inputmode="numeric" placeholder="${falta||"cantidad"}">
    <label>Tu nombre</label><input id="cmp-nom" maxlength="80" placeholder="Nombre u organización">
    <label>Tu WhatsApp</label><input id="cmp-wa" maxlength="60" placeholder="Ej.: 0412 1234567">
    <div style="font-size:12px;color:#8a99ad;margin-top:6px">El punto recibe tu WhatsApp para coordinar. Si al final no puedes, la reserva se libera sola en 12 h.</div>
    <div class="cmp-err" id="cmp-err"></div>
    <button class="btn b-llevar" id="cmp-go" onclick="cmpEnviar(${id})">Confirmar compromiso</button></div>`;
  d.addEventListener("click",e=>{ if(e.target===d) d.remove(); });
  document.body.appendChild(d);
  setTimeout(()=>{const c=document.getElementById("cmp-cant"); if(c) c.focus();},50);
}
function cmpEnviar(id){
  const g=x=>document.getElementById(x), err=g("cmp-err"), btn=g("cmp-go");
  const body={necesidad_id:id,cantidad:(g("cmp-cant").value||"").trim(),nombre:(g("cmp-nom").value||"").trim(),contacto:(g("cmp-wa").value||"").trim()};
  if(!body.cantidad||!body.nombre||!body.contacto){ err.textContent="Completa cantidad, nombre y WhatsApp."; err.style.display="block"; return; }
  btn.disabled=true; btn.textContent="Enviando…";
  fetch("/api/comprometer",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(r=>r.json().catch(()=>({ok:false})))
    .then(d=>{
      if(!(d&&d.ok)) throw new Error((d&&d.error)||"No se pudo enviar");
      const c=d.centro||{};
      document.querySelector("#cmp-ovl .cmp-box").innerHTML=`<button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
        <h3>✅ ¡Anotado! El punto ya fue avisado</h3>
        <div style="line-height:1.7;font-size:14.5px;margin-top:8px"><b>${esc(c.punto||"")}</b><br>${c.ubicacion?`📍 ${esc(c.ubicacion)}<br>`:""}${c.horario?`🕐 ${esc(c.horario)}<br>`:""}${c.contacto?`📞 ${esc(c.contacto)}`:""}</div>
        <div style="font-size:12.5px;color:#8a99ad;margin-top:10px">Lleva lo comprometido a esa dirección. Tu aporte ya cuenta como «en camino» (la web se actualiza en minutos).</div>`;
    })
    .catch(e=>{ err.textContent=(e.message||"No se pudo enviar")+" — puedes intentar de nuevo."; err.style.display="block"; btn.disabled=false; btn.textContent="Confirmar compromiso"; });
}
// Respuesta INLINE a un llamado de apoyo (POST /api/responder_llamado) — ayuda
// puntual sin registro: nombre + WhatsApp; quien levantó el llamado coordina.
function _llaDe(id){ for(const l of (LLAMADOS||[])) if(l.id===id) return l; return null; }
function llaAbrir(id){
  const l=_llaDe(id); if(!l) return;
  const old=document.getElementById("cmp-ovl"); if(old) old.remove();
  const d=document.createElement("div"); d.className="cmp-ovl"; d.id="cmp-ovl";
  d.innerHTML=`<div class="cmp-box"><button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
    <h3>🙋 Responder al llamado</h3>
    <div style="color:var(--suave);font-size:13px">${esc(l.sector||"Sector en emergencia")}</div>
    <label>¿Con qué puedes ayudar?</label><input id="lla-msg" maxlength="300" placeholder="Ej.: llevo agua, pico y pala">
    <label>Tu nombre</label><input id="lla-nom" maxlength="80" placeholder="Nombre u organización">
    <label>Tu WhatsApp</label><input id="lla-wa" maxlength="60" placeholder="Ej.: 0412 1234567">
    <div style="font-size:12px;color:#8a99ad;margin-top:6px">Quien levantó el llamado recibe tu WhatsApp y coordina contigo. Tu número no se publica en la web.</div>
    <div class="cmp-err" id="lla-err"></div>
    <button class="btn b-llevar" id="lla-go" onclick="llaEnviar(${id})">Enviar mi respuesta</button></div>`;
  d.addEventListener("click",e=>{ if(e.target===d) d.remove(); });
  document.body.appendChild(d);
}
function llaEnviar(id){
  const g=x=>document.getElementById(x), err=g("lla-err"), btn=g("lla-go");
  const body={llamado_id:id,mensaje:(g("lla-msg").value||"").trim(),nombre:(g("lla-nom").value||"").trim(),contacto:(g("lla-wa").value||"").trim()};
  if(!body.nombre||!body.contacto){ err.textContent="Completa tu nombre y tu WhatsApp."; err.style.display="block"; return; }
  btn.disabled=true; btn.textContent="Enviando…";
  fetch("/api/responder_llamado",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(r=>r.json().catch(()=>({ok:false})))
    .then(d=>{
      if(!(d&&d.ok)) throw new Error((d&&d.error)||"No se pudo enviar");
      document.querySelector("#cmp-ovl .cmp-box").innerHTML=`<button class="cmp-x" onclick="document.getElementById('cmp-ovl').remove()">✕</button>
        <h3>✅ ¡Gracias! Tu respuesta ya llegó</h3>
        <div style="line-height:1.6;font-size:14.5px;margin-top:8px">Quien levantó el llamado recibió tu WhatsApp y te contactará para coordinar. 🤝</div>`;
    })
    .catch(e=>{ err.textContent=(e.message||"No se pudo enviar")+" — puedes intentar de nuevo."; err.style.display="block"; btn.disabled=false; btn.textContent="Enviar mi respuesta"; });
}
function urgChip(u){ u=(u||"media").toString().toLowerCase();
  if(u.indexOf("crit")===0) return `<span class="urg urg-crit">⚠ Crítico</span>`;
  if(u==="alta") return `<span class="urg urg-alta">🔥 Alta</span>`;
  return ""; }
function barra3(i){
  const meta=+i.meta||0, rec=+i.recibido||0, enc=+i.en_camino||0;
  const btnWeb=ACCION_NEC?`<button class="btn b-llevar b-accweb" onclick="cmpAbrir(${i.id})">🚚 Lo llevo — anotar aquí</button>`:"";
  const btn=btnWeb+(ACCION_NEC?accionBtn("🤝 Puedo llevar esto","nec_"+i.id,
    "Hola 🤝 puedo llevar "+(i.item||"insumos")+" — necesidad ND-"+i.id):"");
  if(!meta){
    return `<div class="nec3">${urgChip(i.urgencia)}<div class="nec3-foot"><span class="nec3-sinmeta">Cantidad por confirmar</span>${btn}</div></div>`;
  }
  const faltan=Math.max(0,meta-rec-enc), cub=faltan<=0;
  const pRec=Math.min(100,Math.round(rec/meta*100));
  const pEnc=Math.max(0,Math.min(100-pRec,Math.round(enc/meta*100)));
  const u=i.unidad?(" "+esc(i.unidad)):"";
  const foot=cub?`<span class="urg urg-ok">✓ Cubierto</span>`:btn;
  return `<div class="nec3">${cub?"":urgChip(i.urgencia)}
    <div class="nec3-bar"><span class="seg-rec" style="width:${pRec}%"></span><span class="seg-enc" style="width:${pEnc}%"></span></div>
    <div class="nec3-leg"><span><i class="d d-rec"></i>${rec} recibido${rec==1?"":"s"}</span><span><i class="d d-enc"></i>${enc} en camino</span><span><i class="d d-fal"></i>${faltan} faltan</span></div>
    <div class="nec3-foot"><span class="nec3-num">${cub?"Completo":("Faltan "+faltan)}<span class="nec3-meta"> / meta ${meta}${u}</span></span>${foot}</div>
  </div>`;
}
function necItem(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const ni=p.items.length;
  const abrir=q?" open":"";
  const sub=(p.personas?`👥 ${p.personas} personas · `:"")+(ni?`${ni} ${ni==1?"requerimiento":"requerimientos"}`:"sin requerimientos aún");
  const _muestra=p.items.map(i=>i.item).slice(0,8).join(", ")+(ni>8?` y ${ni-8} más`:"");
  const sh=`${ico} ${p.punto}`+(p.personas?` — ${p.personas} personas`:"")+(ni?` necesita: ${_muestra}.`:"");
  const urlPunto=paginaPunto(p);
  return `<details id="${pidDe(p)}" class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">${ico} ${esc(p.punto)}</span><span class="nec-sub">${esc(sub)}</span></summary>
    <div class="nec-body">
      ${p.personas?`<div class="meta" style="font-size:15px;font-weight:800;color:var(--navy)">👥 ${esc(String(p.personas))} personas en este momento</div>`:""}
      ${cuerpoInsumos(p.items)}
      <div class="acciones">${compartirBtn(sh,urlPunto)}${copiarBtn(urlPunto)}</div>
      <div class="pie"><span>${tiempoDesde(p.actualizado)}</span></div>
    </div>
  </details>`;
}
function segBadge(e,m){
  if(e==="disponible") return `<span class="seg-badge seg-disp">✅ Disponible${m?` en ${esc(m)}`:" cerca"}</span>`;
  if(e==="coordinando") return `<span class="seg-badge seg-coord">🤝 Coordinando</span>`;
  return `<span class="seg-badge seg-busc">🔎 Buscando</span>`;
}
function segPunto(p){
  const ico=PUNTO_ICO[p.tipo_punto]||"📍";
  const abrir=q?" open":"";
  const lista=p.items.map(i=>`<div class="nec-it"><div class="nec-it-tit">• <b>${esc(i.item)}</b> ${segBadge(i.estado,i.match_punto)}</div>${barra3(i)}</div>`).join("");
  const sub=`${p.items.length} en seguimiento`;
  const ni=p.items.length;
  const _muestra=p.items.map(i=>i.item).slice(0,8).join(", ")+(ni>8?` y ${ni-8} más`:"");
  const sh=`${ico} ${p.punto} necesita: ${_muestra}.`;
  const urlPunto=paginaPunto(p);
  return `<details id="${pidPunto(p)}" class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">${ico} ${esc(p.punto)}</span><span class="nec-sub">${esc(sub)}</span></summary>
    <div class="nec-body">${lista}
      <div class="acciones">${compartirBtn(sh,urlPunto)}${copiarBtn(urlPunto)}</div>
      <div class="pie"><span>${tiempoDesde(p.actualizado)}</span></div></div>
  </details>`;
}
function renderNecesidades(){
  const f=p=>coincide(p.punto+" "+(p.tipo_punto||"")+" "+p.items.map(i=>i.item).join(" "));
  const seg=SEGUIMIENTO.filter(f);
  // Dedup: lo que ya está en seguimiento (con barra + botón) no se repite en la lista de texto libre.
  const segKeys=new Set();
  seg.forEach(p=>p.items.forEach(i=>segKeys.add(_na(p.punto)+"|"+_na(i.item))));
  const nec=NECESIDADES.filter(f)
    .map(p=>({...p, items:p.items.filter(i=>!segKeys.has(_na(p.punto)+"|"+_na(i.item)))}))
    .filter(p=>p.items.length||p.personas)
    .sort((a,b)=>(b.actualizado||"").localeCompare(a.actualizado||""));
  if(!seg.length && !nec.length) return vacio("Aún no hay necesidades reportadas por la red de colaboradores.");
  let html=`<div class="aviso">📥 <b>Lo que hace falta</b> ahora, por punto. Los badges <b>🔎 Buscando / ✅ Disponible / 🤝 Coordinando</b> indican necesidades <b>en seguimiento</b>: el sistema avisa solo a quien lo pidió en cuanto aparece en un acopio cercano. Se actualiza solo. <b>No es información oficial</b>, verifica.</div>`;
  // Agrupado por tipo de punto, mismas categorías que el mapa (acopio/refugio/
  // hospital/CDI/maternidad/clínica + otros). Dentro de cada grupo: primero las
  // necesidades en seguimiento, luego el resto.
  PUNTO_GRUPOS.forEach(([k,titulo])=>{
    const cond=p=>(PUNTO_ICO[p.tipo_punto]?p.tipo_punto:"otro")===k;
    const s=seg.filter(cond), n=nec.filter(cond);
    if(!s.length && !n.length) return;
    const cnt=s.reduce((a,p)=>a+p.items.length,0)+n.reduce((a,p)=>a+p.items.length,0);
    html+=`<h3 class="grupo-titulo">${titulo} · ${cnt}</h3>`;
    html+=s.map(segPunto).join("");
    html+=n.map(necItem).join("");
  });
  return html;
}
function ofItem(p){
  const ni=p.items.length;
  const abrir=q?" open":"";
  const sub=ni?`${ni} ${ni==1?"insumo disponible":"insumos disponibles"}`:"sin inventario aún";
  const _muestra=p.items.map(i=>i.item).slice(0,8).join(", ")+(ni>8?` y ${ni-8} más`:"");
  const sh=`📦 ${p.punto} (centro de acopio)`+(ni?` tiene disponible ${ni} ${ni==1?"insumo":"insumos"}: ${_muestra}.`:".");
  const urlPunto=paginaPunto(p);
  return `<details id="${pidDe(p)}" class="centro nec-acc"${abrir}>
    <summary><span class="nec-tit">📦 ${esc(p.punto)}</span><span class="nec-sub">${esc(sub)}</span></summary>
    <div class="nec-body">
      ${cuerpoInsumos(p.items)}
      <div class="acciones">${compartirBtn(sh,urlPunto)}${copiarBtn(urlPunto)}</div>
      <div class="pie"><span>${tiempoDesde(p.actualizado)}</span></div>
    </div>
  </details>`;
}
function renderOferta(){
  const items=OFERTA.filter(p=>coincide(p.punto+" "+p.items.map(i=>i.item).join(" "))).sort((a,b)=>(b.actualizado||"").localeCompare(a.actualizado||""));
  if(!items.length) return vacio("Aún no hay inventario publicado por los centros de acopio de la red.");
  let html=`<div class="aviso">🎁 Lo que cada <b>centro de acopio</b> de la red tiene <b>disponible para dar</b>, reportado por sus responsables. Toca un punto para ver el detalle, o usa el buscador. Se actualiza solo. <b>No es información oficial</b>, verifica.</div>`;
  agruparPorTipo(items).forEach(([lbl,gr])=>{
    html+=`<h3 class="grupo-titulo">${lbl} · ${gr.length}</h3>`;
    html+=gr.map(ofItem).join("");
  });
  return html;
}
const SERV_ICO={luz:"⚡",agua:"💧",gasolina:"⛽",internet:"🌐",telefonia:"📶",gas:"🔥",via:"🛣️",otro:"🔧"};
const SERV_NOM={luz:"Luz",agua:"Agua",gasolina:"Gasolina",internet:"Internet",telefonia:"Señal",gas:"Gas",via:"Vías",otro:"Otro"};
function servEstado(e){
  const t={ok:["✅ Funciona","s-ok"],falla:["⛔ Sin servicio","s-falla"],parcial:["⚠️ Intermitente","s-parcial"],desconocido:["❔ Sin dato","s-desc"]};
  return t[e]||t.desconocido;
}
function renderServicios(){
  const items=SERVICIOS.filter(s=>coincide((SERV_NOM[s.tipo]||s.tipo)+" "+(s.zona||"")+" "+(s.detalle||"")));
  if(!items.length) return vacio("No hay reportes de servicios todavía.");
  const zonas={};
  items.forEach(s=>{(zonas[s.zona]=zonas[s.zona]||[]).push(s);});
  let html=`<div class="aviso">🔌 Estado de servicios reportado en el canal (luz, agua, gasolina, señal, vías). Es referencial y puede cambiar — verifica.</div>`;
  const _ultZ=z=>zonas[z].reduce((m,s)=>(s.tg_fecha||"")>m?(s.tg_fecha||""):m,"");
  Object.keys(zonas).sort((a,b)=>_ultZ(b).localeCompare(_ultZ(a))).forEach(z=>{
    const lista=zonas[z];
    html+=`<div class="centro"><div class="info" style="width:100%">
      <div class="cn">📍 ${esc(z)}</div>
      <div class="serv-chips">`+lista.map(s=>{
        const e=servEstado(s.estado);
        return `<span class="serv-chip ${e[1]}">${SERV_ICO[s.tipo]||"🔧"} ${esc(SERV_NOM[s.tipo]||s.tipo)}: ${e[0]}</span>`;
      }).join("")+`</div>
      <div class="pie"><span>${tiempoDesde(lista[0].tg_fecha)}</span></div>
    </div></div>`;
  });
  return html;
}
function renderBuscar(){
  let html=`<div class="aviso">🔦 ¿Buscas a alguien que pudo ingresar a un hospital? Estas plataformas ciudadanas centralizan los listados de <b>hospitalizados, rescatados y desaparecidos</b> del terremoto y <b>se actualizan constantemente</b>. Abre cualquiera y busca por nombre o foto. <b>No es información oficial</b> — verifica.</div>`;
  const items=PLATAFORMAS.filter(p=>coincide(p.nombre+" "+p.desc));
  if(!items.length) return html+vacio("Ninguna plataforma coincide con tu búsqueda.");
  html+=items.map(p=>{
    const sh=`🔦 Busca personas en hospitales tras el terremoto en ${p.nombre}: ${p.url}`;
    return `<div class="centro"><div class="info">
      <div class="cn">${esc(p.nombre)}</div>
      <div class="meta">${esc(p.desc)}</div>
      <div class="acciones">
        <a class="btn b-share" target="_blank" rel="noopener" href="${esc(p.url)}">🔎 Abrir y buscar ↗</a>
        ${compartirBtn(sh,p.url)}
      </div>
    </div></div>`;
  }).join("");
  html+=`<div class="aviso" style="margin-top:16px">📂 ¿Buscas otra cosa (refugios, mascotas, transporte, daños estructurales)? Hay un directorio con más recursos del terremoto: <a href="https://enlacesterremotovenezuela.netlify.app/" target="_blank" rel="noopener" style="color:var(--navy);font-weight:800;text-decoration:underline">ver más recursos ↗</a>. Es un directorio externo — verifica cada enlace.</div>`;
  return html;
}
function sismoMag(s){ return s.mag!=null ? Number(s.mag).toFixed(1) : "?"; }
function distKm(a,b){
  const R=6371, dLat=(b.lat-a.lat)*Math.PI/180, dLon=(b.lon-a.lon)*Math.PI/180;
  const x=Math.sin(dLat/2)**2+Math.cos(a.lat*Math.PI/180)*Math.cos(b.lat*Math.PI/180)*Math.sin(dLon/2)**2;
  return Math.round(2*R*Math.asin(Math.sqrt(x)));
}
function intensidad(mag,d){ const s=(mag||0)-d/120; return s>=4.8?"fuerte":s>=3.8?"moderado":s>=2.8?"leve":"casi imperceptible"; }
function pedirUbicacion(){
  if(!navigator.geolocation) return;
  navigator.geolocation.getCurrentPosition(
    p=>{ miUbic={lat:p.coords.latitude,lon:p.coords.longitude}; if(activo==="sismos") render(); },
    ()=>{}, {timeout:8000, maximumAge:300000});
}
function sismoExtra(s){
  if(!miUbic || s.lat==null) return "";
  const d=distKm(miUbic,{lat:s.lat,lon:s.lon});
  return `<div class="meta">📍 a <b>${d} km</b> de ti · estimado: <b>${intensidad(s.mag,d)}</b></div>`;
}
function sismosHTML(fs){
  if(!fs.length) return vacio("Sin sismos registrados cerca de Venezuela en los últimos 3 días.");
  let html=`<div class="aviso">🌎 Sismos cerca de Venezuela (últimos 3 días · fuentes USGS + EMSC). <b>Es informativo, no es alerta temprana</b> — las réplicas muy pequeñas pueden no aparecer.</div>`;
  if(!miUbic) html+=`<button class="btn b-report" id="geo-btn" style="width:100%;justify-content:center;margin-bottom:12px">📍 Calcular distancia a mi ubicación</button>`;
  const s0=fs[0], m0=s0.mag, col0=m0>=5?"var(--lleno)":(m0>=4?"var(--rescate)":"var(--disponible)");
  html+=`<div class="card" style="border-left:5px solid ${col0}">
    <div class="cab"><span class="ico">🌎</span><div><div class="cat">Sismo más reciente</div><h3>Magnitud ${sismoMag(s0)}</h3></div></div>
    <div class="meta">📍 ${esc(s0.lugar||"(sin lugar)")}</div>
    ${sismoExtra(s0)}
    <div class="pie"><span>${tiempoDesde(s0.fecha)}</span><span>${esc(s0.fuente||"")}</span></div>
  </div>`;
  if(fs.length>1){
    html+=`<h3 class="grupo-titulo">Anteriores (${fs.length-1})</h3>`;
    html+=fs.slice(1).map(s=>{
      const m=s.mag, col=m>=5?"var(--lleno)":(m>=4?"var(--rescate)":"var(--disponible)");
      return `<div class="centro"><div class="info">
        <div class="cn">📍 ${esc(s.lugar||"(sin lugar)")}</div>
        ${sismoExtra(s)}
        <div class="meta">${tiempoDesde(s.fecha)} · ${esc(s.fuente||"")}</div>
      </div><span class="badge" style="color:${col};background:#f1f5f9">M ${sismoMag(s)}</span></div>`;
    }).join("");
  }
  return html;
}
function renderSismos(){ return sismosHTML(SISMOS); }
function renderAngeles(){
  // Hub del canal de emergencias viales: mismos submenús que el grupo del acordeón.
  const g=listaTabs().find(t=>t.sub);
  if(!g) return vacio("No hay reportes del canal todavía.");
  return `<div class="aviso">🚨 <b>Ángeles de la Autopista</b> — reportes en vivo recopilados automáticamente del canal de emergencias viales. Puede haber errores o datos viejos: <b>verifica antes de actuar</b>.</div>
    <div class="accesos" style="margin:12px 0 0;padding:0">`
    +g.sub.map(s=>`<button class="acceso ac-hub" onclick="irTab('${s.key}')"><span class="ac-ico">${s.ico}</span><span>${esc(s.nom)}${s.n!=null?` <span class="pill" style="background:rgba(0,0,0,.08);border-radius:999px;padding:1px 8px;font-size:12px">${s.n}</span>`:""}</span></button>`).join("")
    +`</div>`;
}
function irTab(k){
  activo=k;q="";zonaSel="todas";msCat="todas";desapMostrar=DESAP_LOTE;
  var qi=document.getElementById("q");if(qi)qi.value="";
  pintarTabs();render();
  const c=document.getElementById("contenido");
  c.classList.remove("fx-in"); void c.offsetWidth; c.classList.add("fx-in");
  const destino=document.getElementById("secmenu")||c;
  destino.scrollIntoView({behavior:"smooth",block:"start"});
}
function renderInfo(){
  const BOT="https://t.me/angelesapoyove_bot";
  const WA="https://wa.me/message/6TEVCZCHJ2TPN1";
  const paso=(n,t,d)=>`<div style="display:flex;gap:12px;margin:12px 0"><div style="flex:0 0 30px;height:30px;border-radius:50%;background:var(--navy);color:#fff;font-weight:800;display:flex;align-items:center;justify-content:center">${n}</div><div><b>${t}</b><div style="color:var(--suave);font-size:14px;margin-top:2px">${d}</div></div></div>`;
  const h2=(t)=>`<h2 style="color:var(--navy);font-size:20px;margin:28px 0 8px">${t}</h2>`;
  return `<div class="info-pagina" style="line-height:1.55;color:var(--tinta)">
    <h2 style="color:var(--navy);font-size:20px;margin:6px 0 8px">Súmate a la red</h2>
    <p>Todo se hace <b>gratis</b> y por la vía que prefieras: <b>WhatsApp, Telegram o directamente aquí en la web</b> — es la misma red por las 3 vías. Eliges tu camino según lo que puedas aportar:</p>
    <div style="display:grid;gap:12px;margin:12px 0">
      <div style="border:1px solid var(--linea);border-radius:14px;padding:14px 16px;background:var(--card)">
        <div style="font-weight:800;color:var(--navy)">🏠 Centros y colaboradores</div>
        <div style="color:var(--suave);font-size:14px;margin-top:4px">Si manejas o ayudas en un <b>centro de acopio, refugio u hospital</b>. Registra un punto nuevo o súmate a uno que ya existe como colaborador, sin repetir la ficha. Reportas lo que el punto tiene y lo que necesita.</div>
      </div>
      <div style="border:1px solid var(--linea);border-radius:14px;padding:14px 16px;background:var(--card)">
        <div style="font-weight:800;color:var(--navy)">🤝 Manos Solidarias</div>
        <div style="color:var(--suave);font-size:14px;margin-top:4px">Si ofreces ayuda <b>gratis</b> sin ser un centro fijo: comida, agua, transporte, carga de teléfonos, atención de salud, hospedaje. Apareces en el mapa para que te encuentren.</div>
      </div>
      <div style="border:1px solid var(--linea);border-radius:14px;padding:14px 16px;background:var(--card)">
        <div style="font-weight:800;color:var(--navy)">📣 Voluntariado</div>
        <div style="color:var(--suave);font-size:14px;margin-top:4px">Si estás <b>en terreno</b>. Levanta <b>llamados de apoyo</b> (un sector, un mensaje, una foto) y el sistema avisa en privado a los centros y Manos Solidarias que puedan cubrirlos. Tu contacto nunca se publica.</div>
      </div>
    </div>
    <div style="text-align:center;margin:16px 0 6px">
      <a class="btn" href="/registrar.html" style="background:var(--teal);color:#fff;font-weight:800;border-color:var(--teal)">🌐 Registrar mi centro aquí (2 min)</a>
      <a class="btn" href="${WA}" target="_blank" rel="noopener" style="background:#25D366;color:#fff;font-weight:800;border-color:#25D366">💬 Sumarme por WhatsApp</a>
      <a class="btn" href="${BOT}" target="_blank" rel="noopener" style="background:var(--navy);color:#fff;font-weight:800;border-color:var(--navy)">✈️ Sumarme por Telegram</a>
    </div>
    <p style="color:var(--suave);font-size:13.5px;text-align:center">🔒 Cédulas y datos personales <b>nunca se publican</b> · moderamos la red y retiramos contenido indebido.</p>
    <details style="border:1px solid var(--linea);border-radius:14px;background:var(--card);padding:4px 16px;margin:18px 0">
      <summary style="cursor:pointer;font-weight:800;color:var(--navy);padding:10px 0;font-size:15.5px">ℹ️ Quiénes somos, qué es esto y cómo funciona</summary>
      ${h2("Qué es")}
      <p>Cuando ocurre una emergencia, la solidaridad aparece rápido — pero desordenada. Unos centros se llenan de donaciones que ya no necesitan; a otros, a pocas cuadras, les falta lo básico. Nadie tiene el panorama completo.</p>
      <p><b>Red de Acopio y Refugio</b> es un registro vivo y abierto de los puntos que atienden la emergencia en Venezuela. Cada centro de acopio, refugio y hospital aparece con su ubicación, su capacidad, lo que tiene disponible y lo que necesita. Así, quien quiere ayudar sabe exactamente a dónde ir.</p>
      ${h2("Cómo funciona")}
      ${paso(1,"El centro se registra","Aquí en la web, por WhatsApp o por Telegram: una ficha rápida con dónde está, qué tipo de punto es y a quién contactar. Toma unos 2 minutos.")}
      ${paso(2,"Reporta lo que tiene y lo que necesita","Por texto, foto, nota de voz o un archivo. El sistema lo organiza solo y lo mantiene al día.")}
      ${paso(3,"Todo aparece en el mapa público","Ordenado, buscable y sin datos personales sensibles. Solo lo que sirve para coordinar.")}
      ${paso(4,"La ayuda llega a su destino","Donantes, voluntarios y otros centros encuentran al instante dónde hace falta lo que pueden dar — y van directo.")}
      ${h2("Para quién es")}
      <ul style="margin:8px 0 0;padding-left:18px">
        <li style="margin:6px 0">🤝 <b>Si quieres ayudar:</b> mira en tiempo real qué necesita cada centro cerca de ti antes de salir a donar.</li>
        <li style="margin:6px 0">📦 <b>Si manejas un centro de acopio:</b> gana visibilidad y recibe justo lo que te falta, no lo que sobra.</li>
        <li style="margin:6px 0">🏠 <b>Si coordinas un refugio:</b> reporta tu capacidad y tus necesidades en un solo lugar.</li>
        <li style="margin:6px 0">🏥 <b>Si atiendes en un hospital:</b> publica tus requerimientos de insumos para que lleguen más rápido.</li>
      </ul>
      <div class="aviso" style="margin-top:18px">🔒 <b>Tu información está protegida.</b> Las cédulas y los nombres de las personas afiliadas <b>nunca se publican</b>. En la web solo se muestra lo necesario para coordinar la ayuda: el centro, su ubicación y lo que necesita.</div>
      <div class="aviso">✅ <b>Transparencia.</b> Registrar un centro <b>siempre es gratis</b>. No redistribuimos donaciones de terceros: la ayuda va directa del donante al centro. Somos una <b>iniciativa ciudadana e independiente</b>, sin afiliación a grupos de rescate ni a organismos oficiales.</div>
      ${h2("Apoya la plataforma")}
      <p style="padding-bottom:10px">Red de Acopio y Refugio es gratis para todos y se mantiene con <b>aportes voluntarios</b> que cubren dominio, servidor y costos de operación. El dinero solo mantiene la plataforma funcionando: <b>no financiamos ni redistribuimos a los centros</b>. ¿Quieres apoyarla? Escríbenos a <a href="mailto:contacto@redacopioyrefugio.com" style="color:var(--navy);font-weight:700">contacto@redacopioyrefugio.com</a>.</p>
    </details>
    <div style="background:var(--navy);color:#fff;border-radius:16px;padding:20px 18px;margin:22px 0 6px;text-align:center">
      <div style="font-size:18px;font-weight:800">¿Manejas un centro, refugio o atiendes en un hospital?</div>
      <p style="margin:8px 0 14px;opacity:.95">Súmate a la red en 2 minutos, por la vía que prefieras.</p>
      <a class="btn" href="/registrar.html" style="background:#fff;color:var(--navy);font-weight:800;border-color:#fff">🌐 Registrarme aquí en la web</a>
      <a class="btn" href="${WA}" target="_blank" rel="noopener" style="background:#25D366;color:#fff;font-weight:800;border-color:#25D366">💬 Sumarme por WhatsApp</a>
      <a class="btn" href="${BOT}" target="_blank" rel="noopener" style="background:rgba(255,255,255,.14);color:#fff;font-weight:800;border-color:rgba(255,255,255,.5)">✈️ Sumarme por Telegram</a>
    </div>
  </div>`;
}
function renderApoya(){
  const MAIL="contacto@redacopioyrefugio.com";
  return `<div class="info-pagina" style="line-height:1.55;color:var(--tinta)">
    <div style="background:linear-gradient(135deg,var(--navy),var(--navy2));color:#fff;border-radius:16px;padding:22px 18px;margin-bottom:6px">
      <div style="font-size:22px;font-weight:800;line-height:1.2">🤍 Apoya la plataforma</div>
      <p style="margin:10px 0 0;opacity:.95">Red de Acopio y Refugio es <b>gratis para todos</b> y se mantiene con <b>aportes voluntarios</b> que cubren dominio, servidor y costos de operación.</p>
    </div>
    <div class="aviso" style="margin-top:14px">✅ <b>Importante:</b> tu aporte <b>solo mantiene la plataforma funcionando</b>. No financiamos ni redistribuimos donaciones a los centros — la ayuda va <b>directa del donante al centro</b>, y registrar un centro <b>siempre es gratis</b>.</div>
    <p style="margin-top:14px">¿Quieres ayudar a sostenerla? Escríbenos y te pasamos las formas de aporte:</p>
    <div style="text-align:center;margin:16px 0 6px">
      <a class="btn" href="mailto:${MAIL}" style="background:var(--navy);color:#fff;font-weight:800;border-color:var(--navy)">✉️ Escribir a ${MAIL}</a>
    </div>
    <div class="aviso">🔒 Transparencia total: el aporte es 100% para sostener la web (dominio, servidor, IA). Sin fines de lucro.</div>
  </div>`;
}
function renderPedir(){
  let items=PEDIDOS.filter(p=>coincide((p.sector||"")+" "+(p.mensaje||"")));
  let html=`<div class="aviso">🆘 <b>Pedidos de ayuda</b>: solicitudes de personas que necesitan algo (comida, agua, medicinas, refugio, encontrar a una persona desaparecida…), enviadas <b>desde la web</b> y revisadas por nuestro equipo. Por seguridad <b>no mostramos el contacto de quien pide</b>; los centros y Manos Solidarias de la zona ya fueron avisados en privado. <b>No es información oficial</b>, verifica.</div>`;
  html+=`<div style="text-align:center;margin:8px 0 12px"><a class="btn" href="/pedir.html" style="background:#b3261e;color:#fff;font-weight:800;border-color:#b3261e">🆘 Pedir ayuda</a></div>`;
  if(!items.length){html+=vacio("No hay pedidos de ayuda publicados ahora.");return html;}
  html+=items.map(pedidoItem).join("");
  return html;
}
function pedidoItem(p){
  const msj=(p.mensaje||"");
  const sh=`🆘 Pedido de ayuda${p.sector?` — ${p.sector}`:""}: ${msj.length>140?msj.slice(0,139).trim()+"…":msj}`;
  const urlPed=paginaPedido(p);
  const enAt=p.estado==="atencion";
  const n=p.n_atienden||0;
  const badge=(enAt||n>0)?`<div style="margin-top:8px"><span class="lla-resp">🤝 ${n>0?(n+" "+(n==1?"persona atendiendo":"personas atendiendo")):"Alguien lo está atendiendo"}</span></div>`:"";
  const esSalud=/salud|medicin|psic|emocional|mental/i.test(p.mensaje||"");
  const salud=esSalud?`<div class="meta" style="margin-top:8px;background:#eef6f7;border-radius:8px;padding:8px 10px;color:#005B67">🧠 Apoyo voluntario entre personas: <b>no sustituye atención médica ni psicológica profesional</b>. En crisis llama al 911.</div>`:"";
  const btnAt=`<button class="btn" data-atender="${p.id}" style="background:#1E9C94;color:#fff;font-weight:700;border-color:#1E9C94">🤝 ${enAt?"También quiero ayudar":"Atender este pedido"}</button>`;
  return `<div class="centro lla-card" id="pd-${p.id}">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="font-size:22px">🆘</span>
      <span style="font-weight:700;color:#b3261e">${esc(p.sector||"Zona no indicada")}</span>
    </div>
    ${p.foto?`<img class="foto" src="${esc(p.foto)}" loading="lazy" alt="">`:""}
    <div style="white-space:pre-wrap;line-height:1.45">${esc(p.mensaje||"")}</div>
    ${badge}${salud}
    <div class="meta" style="margin-top:6px">🕐 ${tiempoDesde(p.creado_en)}</div>
    <div class="acciones">${btnAt}${compartirBtn(sh,urlPed)}${copiarBtn(urlPed)}</div>
  </div>`;
}
function render(){
  poblarZonas(itemsTab());
  const c=document.getElementById("contenido");
  if(activo==="info") c.innerHTML=renderInfo();
  else if(activo==="situacion") c.innerHTML=renderSituacion();
  else if(activo==="necesidades") c.innerHTML=renderNecesidades();
  else if(activo==="oferta") c.innerHTML=renderOferta();
  else if(activo==="directorio") c.innerHTML=renderDirectorio();
  else if(activo==="servicios") c.innerHTML=renderServicios();
  else if(activo==="buscar") c.innerHTML=renderBuscar();
  else if(activo==="emergencias") c.innerHTML=renderEmergencias();
  else if(activo==="sismos") c.innerHTML=renderSismos();
  else if(activo==="angeles") c.innerHTML=renderAngeles();
  else if(activo==="apoya") c.innerHTML=renderApoya();
  else if(activo==="manos") c.innerHTML=renderManos();
  else if(activo==="llamados") c.innerHTML=renderLlamados();
  else if(activo==="pedir") c.innerHTML=renderPedir();
  else if(activo==="desaparecidos") c.innerHTML=renderDesap();
  else c.innerHTML=renderCategoria(activo==="alertas"?"info":activo);
  if(activo==="directorio") initMapa("mapa-red");
  else if(activo==="manos") initMapa("mapa-ms");
}

// Compartir (WhatsApp / hoja nativa) por delegación de eventos
document.getElementById("contenido").addEventListener("click",ev=>{
  const b=ev.target.closest("[data-share]"); if(!b)return;
  const t=b.getAttribute("data-share"), u=b.getAttribute("data-url")||"";
  if(navigator.share){navigator.share({text:t,url:u||undefined}).catch(()=>{});}
  else{window.open("https://wa.me/?text="+encodeURIComponent(t+(u?(" "+u):"")),"_blank");}
});
// Copiar enlace del punto al portapapeles (con fallback a prompt si no hay clipboard)
document.getElementById("contenido").addEventListener("click",ev=>{
  const b=ev.target.closest("[data-copiar]"); if(!b)return;
  const u=b.getAttribute("data-copiar");
  const p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(u):Promise.reject();
  p.then(()=>{const t=b.textContent;b.textContent="✅ Enlace copiado";setTimeout(()=>{b.textContent=t;},1600);})
   .catch(()=>{prompt("Copia el enlace:",u);});
});
// Botón público: reportar novedad sobre un desaparecido → avisa a moderadores
document.getElementById("contenido").addEventListener("click",ev=>{
  const r=ev.target.closest("[data-reportar]"); if(!r) return;
  const id=Number(r.getAttribute("data-reportar"));
  r.disabled=true; r.textContent="Enviando…";
  fetch("/api/reportar",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tg_id:id,tipo:"novedad"})})
    .then(res=>res.json()).then(()=>{r.textContent="✅ Reporte enviado, ¡gracias!";})
    .catch(()=>{r.textContent="No se pudo enviar"; r.disabled=false;});
});
// Botón público: DENUNCIAR una publicación (Manos Solidarias o centro) → avisa a moderadores.
// El público NO puede quitar; solo alerta. Los moderadores revisan y deciden.
document.getElementById("contenido").addEventListener("click",ev=>{
  const d=ev.target.closest("[data-denunciar]"); if(!d) return;
  if(!confirm("¿Denunciar esta publicación como indebida o estafa? Nuestro equipo la revisará.")) return;
  const parts=(d.getAttribute("data-denunciar")||"").split(":");
  d.disabled=true; d.textContent="Enviando…";
  fetch("/api/denunciar",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({tipo:parts[0],id:Number(parts[1])})})
    .then(res=>res.json()).then(()=>{d.textContent="✅ Denuncia enviada, ¡gracias!";})
    .catch(()=>{d.textContent="No se pudo enviar"; d.disabled=false;});
});

// 🤝 Solicitar contacto de una Mano Solidaria: sin números en la web; el aliado decide con ✅/❌ desde el bot.
let _msqId=null;
function abrirContactoMS(id){
  _msqId=Number(id);
  const al=ALIADOS.find(x=>x.id===_msqId);
  _modalAt(`<h3>🤝 Solicitar contacto</h3>
    <p style="font-size:13px;color:#5b6b80;margin:4px 0 0;line-height:1.45">Por seguridad no publicamos números. ${al?esc(al.nombre):"La persona"} recibirá tu solicitud y, si acepta, cada quien recibe el contacto del otro.</p>
    <div id="mq-err" style="display:none;color:#9b2226;font-size:13.5px;margin-top:10px"></div>
    <input type="text" id="mq-hp" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off" aria-hidden="true">
    <label>Tu nombre</label><input id="mq-nom" maxlength="80" placeholder="Tu nombre">
    <label>Tu WhatsApp</label><input id="mq-con" maxlength="60" placeholder="Ej.: 0412 1234567">
    <label>¿Qué necesitas? <span style="color:#8a99ad;font-weight:500">(breve)</span></label>
    <textarea id="mq-msg" maxlength="300" placeholder="Ej.: Necesito pan para 3 personas en Caricuao."></textarea>
    <p style="font-size:12px;color:#8a99ad;margin:10px 0 0;line-height:1.4">Al continuar aceptas el <a href="/aviso-legal.html" target="_blank" rel="noopener" style="color:#1E9C94">Aviso legal</a>. Tu solicitud queda registrada para seguridad de ambos.</p>
    <button class="mbtn" id="mq-go">Enviar solicitud</button>
    <button class="mx" onclick="cerrarAt()">Cancelar</button>`);
  document.getElementById("mq-go").onclick=_enviarMsq;
}
function _enviarMsq(){
  const nom=(document.getElementById("mq-nom").value||"").trim();
  const con=(document.getElementById("mq-con").value||"").trim();
  const msj=(document.getElementById("mq-msg").value||"").trim();
  const hp=(document.getElementById("mq-hp").value||"");
  const err=document.getElementById("mq-err");
  if(!nom||!con){err.textContent="Pon tu nombre y tu WhatsApp para continuar.";err.style.display="block";return;}
  const b=document.getElementById("mq-go"); b.disabled=true; b.textContent="Enviando…";
  fetch("/api/contactar-ms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({aliado_id:_msqId,nombre:nom,contacto:con,mensaje:msj,website:hp})})
    .then(r=>r.json()).then(d=>{
      if(!d||!d.ok||!d.token) throw new Error((d&&d.error)||"No se pudo");
      _modalAt(`<h3>📨 ¡Solicitud enviada!</h3>
        <p style="font-size:14px;margin:8px 0;line-height:1.5">Le avisamos a la persona: tiene hasta <b>24 horas</b> para responder. Si acepta, te escribimos por WhatsApp y aquí también puedes ver la respuesta:</p>
        <a class="mbtn" style="text-decoration:none;text-align:center;box-sizing:border-box" href="/api/contacto-ms/${d.token}" target="_blank" rel="noopener">🔔 Ver estado de mi solicitud</a>
        <button class="mx" onclick="cerrarAt()">Cerrar</button>`);
    })
    .catch(e=>{err.textContent="No se pudo ("+(e.message||"error")+"). Intenta de nuevo.";err.style.display="block";b.disabled=false;b.textContent="Enviar solicitud";});
}
document.getElementById("contenido").addEventListener("click",ev=>{
  const c=ev.target.closest("[data-contactar]"); if(!c) return;
  abrirContactoMS(c.getAttribute("data-contactar"));
});

// 🆘 Atender un pedido: quien ayuda deja su contacto (queda registrado) → ve el WhatsApp del solicitante.
let _atId=null;
function _modalAt(html){document.getElementById("atender-card").innerHTML=html;document.getElementById("atender-bg").classList.add("on");}
function cerrarAt(){document.getElementById("atender-bg").classList.remove("on");_atId=null;if(activo==="pedir")render();}
document.getElementById("atender-bg").addEventListener("click",ev=>{if(ev.target.id==="atender-bg")cerrarAt();});
document.getElementById("contenido").addEventListener("click",ev=>{
  const a=ev.target.closest("[data-atender]"); if(!a) return;
  _atId=Number(a.getAttribute("data-atender"));
  _modalAt(`<h3>🤝 Atender este pedido</h3>
    <p style="font-size:13px;color:#5b6b80;margin:4px 0 0;line-height:1.45">Deja tus datos (para saber quién ayuda). Luego verás el WhatsApp de la persona para escribirle directo.</p>
    <div id="at-err" style="display:none;color:#9b2226;font-size:13.5px;margin-top:10px"></div>
    <input type="text" id="at-hp" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off" aria-hidden="true">
    <label>Tu nombre</label><input id="at-nom" maxlength="80" placeholder="Tu nombre o el de tu grupo">
    <label>Tu WhatsApp</label><input id="at-con" maxlength="60" placeholder="Ej.: 0412 1234567">
    <label>¿Cómo puedes ayudar? <span style="color:#8a99ad;font-weight:500">(opcional)</span></label>
    <textarea id="at-nota" maxlength="300" placeholder="Ej.: Tengo camioneta, puedo hacer el traslado hoy."></textarea>
    <p style="font-size:12px;color:#8a99ad;margin:10px 0 0;line-height:1.4">Al continuar aceptas el <a href="/aviso-legal.html" target="_blank" rel="noopener" style="color:#1E9C94">Aviso legal</a>: la Red solo conecta personas; no verifica ni supervisa la ayuda. Verifica identidad y coordina en lugares seguros.</p>
    <button class="mbtn" id="at-go">Ofrecer ayuda</button>
    <button class="mx" onclick="cerrarAt()">Cancelar</button>`);
  document.getElementById("at-go").onclick=_enviarAt;
});
function _enviarAt(){
  const nom=(document.getElementById("at-nom").value||"").trim();
  const con=(document.getElementById("at-con").value||"").trim();
  const nota=(document.getElementById("at-nota").value||"").trim();
  const hp=(document.getElementById("at-hp").value||"");
  const err=document.getElementById("at-err");
  if(!nom||!con){err.textContent="Pon tu nombre y tu WhatsApp para continuar.";err.style.display="block";return;}
  const b=document.getElementById("at-go"); b.disabled=true; b.textContent="Enviando…";
  fetch("/api/atender",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({llamado_id:_atId,nombre:nom,contacto:con,nota:nota,website:hp})})
    .then(r=>r.json()).then(d=>{
      if(!d||!d.ok) throw new Error((d&&d.error)||"No se pudo");
      const p=PEDIDOS.find(x=>x.id===_atId); if(p){p.estado="atencion";p.n_atienden=(p.n_atienden||0)+1;}
      _modalAt(`<h3>✅ Oferta enviada</h3>
        <p style="font-size:14px;margin:8px 0;line-height:1.5">Avisamos a la persona que pidió ayuda. Si acepta compartir su contacto, ambos recibirán los datos para coordinar.</p>
        <button class="mx" onclick="cerrarAt()">Cerrar ventana</button>`);
    })
    .catch(e=>{err.textContent="No se pudo ("+(e.message||"error")+"). Intenta de nuevo.";err.style.display="block";b.disabled=false;b.textContent="Ofrecer ayuda";});
}
function _resolverAt(tk){
  const id=_atId;
  fetch("/api/resolver",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({llamado_id:id,token:tk})})
    .then(r=>r.json()).then(d=>{
      const i=PEDIDOS.findIndex(x=>x.id===id); if(i>=0) PEDIDOS.splice(i,1);
      cerrarAt(); pintarTabs(); render();
    }).catch(()=>{cerrarAt();});
}
document.getElementById("q").oninput=e=>{q=e.target.value.toLowerCase();desapMostrar=DESAP_LOTE;render();};
document.getElementById("zona").onchange=e=>{zonaSel=e.target.value;desapMostrar=DESAP_LOTE;render();};
document.getElementById("contenido").addEventListener("click",e=>{ if(e.target.closest("#geo-btn")) pedirUbicacion(); });

// Compartir / copiar la página completa
(function(){
  const sp=document.getElementById("share-page"), cp=document.getElementById("copy-page");
  if(sp) sp.onclick=()=>{
    if(navigator.share) navigator.share({title:"Red de Acopio y Refugio",text:SHARE_MSG,url:SITE_URL}).catch(()=>{});
    else window.open("https://wa.me/?text="+encodeURIComponent(SHARE_MSG+" "+SITE_URL),"_blank");
  };
  if(cp) cp.onclick=()=>{
    navigator.clipboard.writeText(SHARE_MSG+String.fromCharCode(10)+SITE_URL).then(()=>{
      cp.textContent="✅ Copiado"; setTimeout(()=>cp.textContent="📋 Copiar texto",2000);
    }).catch(()=>{});
  };
})();

// Visor de fotos: tocar una imagen la abre grande; tocar el fondo la cierra.
document.body.addEventListener("click",ev=>{
  const im=ev.target.closest("img.fimg, img.foto");
  if(im && im.getAttribute("src")){
    document.getElementById("lbimg").src=im.getAttribute("src");
    document.getElementById("lightbox").classList.add("on");
    return;
  }
  if(ev.target.closest("#lightbox")) document.getElementById("lightbox").classList.remove("on");
});
document.getElementById("hmeta").textContent="__HMETA__";
// Deep link: abre la pestaña y enfoca/abre el punto compartido (#oferta:pt-5).
function enfocarPunto(target){
  if(!target) return;
  setTimeout(()=>{
    const el=document.getElementById(target);
    if(!el) return;
    el.open=true;
    el.scrollIntoView({behavior:"smooth",block:"start"});
    el.classList.add("destacado");
    setTimeout(()=>el.classList.remove("destacado"),2600);
  },70);
}
function rutearHash(){
  const m=(location.hash||"").replace(/^#/,"");
  let target="";
  if(m){
    const i=m.indexOf(":");
    const tab=decodeURIComponent(i<0?m:m.slice(0,i));
    target=i<0?"":decodeURIComponent(m.slice(i+1));
    if(tab) activo=tab;
  }
  pintarTabs(); render();
  enfocarPunto(target);
}
// «Necesitan ayuda AHORA»: las 3 necesidades activas más urgentes (por urgencia y
// faltante), visibles en TODAS las pestañas, cada una con su «Lo llevo» inline.
function renderAhora(){
  const el=document.getElementById("ahora");
  if(!el) return;
  const rank=u=>{u=(u||"media").toString().toLowerCase();
    return u.indexOf("crit")===0?0:(u==="alta"?1:(u==="media"?2:3));};
  const items=(SEGUIMIENTO||[]).flatMap(p=>(p.items||[]).map(i=>({...i,_punto:p.punto})))
    .map(i=>({...i,_falta:(+i.meta||0)-(+i.recibido||0)-(+i.en_camino||0)}))
    .filter(i=>(+i.meta||0)>0 && i._falta>0)
    .sort((a,b)=>rank(a.urgencia)-rank(b.urgencia)||b._falta-a._falta);
  if(!items.length){el.innerHTML="";return;}
  el.innerHTML=`<div class="ahora">
    <div class="ahora-head"><b>⚡ Necesitan ayuda AHORA</b><span>lo más urgente en este momento</span><a href="#necesidades" onclick="irTab('necesidades');return false">Ver todo →</a></div>
    <div class="ahora-grid">${items.slice(0,3).map(i=>{
      const u=i.unidad?(" "+esc(i.unidad)):"";
      return `<div class="ahora-card">${urgChip(i.urgencia)||""}
        <div class="a-item">${esc(i.item||"")}</div>
        <div class="a-punto">${esc(i._punto||"")}</div>
        <div class="a-falta">Faltan ${i._falta}${u}</div>
        <button class="btn b-llevar b-accweb" onclick="cmpAbrir(${i.id})">🚚 Lo llevo</button></div>`;
    }).join("")}</div></div>`;
}
renderAhora();
rutearHash();
window.addEventListener("hashchange",rutearHash);

// Contador de visitas (sin cookies; cuenta del lado del servidor, no a los bots)
fetch("/api/visita",{method:"POST"}).then(r=>r.json()).then(d=>{
  if(!d||!d.ok) return;
  const f=n=>Number(n).toLocaleString("es-VE");
  const el=document.getElementById("contador");
  if(el) el.innerHTML=`👁️ <b>${f(d.total)}</b> visitas`+(d.unicos_hoy?` · <b>${f(d.unicos_hoy)}</b> visitantes hoy`:"");
}).catch(()=>{});

// Service worker (funciona sin señal; se activa al servir por https)
if("serviceWorker" in navigator){navigator.serviceWorker.register("sw.js").catch(()=>{});}
</script>
</body>
</html>"""

MANIFEST = {
    "name": SITE_TITLE,
    "short_name": "Acopio",
    "description": SITE_TAGLINE,
    "lang": "es",
    "start_url": "./",
    "scope": "./",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#003C67",
    "theme_color": "#003C67",
    "icons": [
        {"src": "icon-192.png?v=ofc", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "icon-512.png?v=ofc", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "icon-512.png?v=ofc", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SERVICE_WORKER = """// Cache de Red de Acopio y Refugio — sirve la última versión vista aunque no haya señal.
const CACHE = "angeles-v1";
self.addEventListener("install", e => { self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const ruta = new URL(e.request.url).pathname;
  if (ruta.startsWith("/api/institucional/") || ruta === "/panel.html") return;
  e.respondWith(
    fetch(e.request).then(resp => {
      const copia = resp.clone();
      caches.open(CACHE).then(c => c.put(e.request, copia));
      return resp;
    }).catch(() => caches.match(e.request))
  );
});
"""


# Formulario web público para inscribirse en Manos Solidarias sin usar Telegram.
# Envía un POST JSON a /api/ofrecer (backend.py) → queda 'pendiente' → un moderador
# aprueba con un toque desde el bot (mismo flujo que las inscripciones por Telegram).
PAGINA_LEGAL = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aviso legal y condiciones de uso — Red de Acopio y Refugio</title>
<meta name="robots" content="index,follow">
<link rel="icon" href="/logo.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap">
<style>
  :root{--navy:#003C67;--navy2:#005B67;--teal:#1E9C94;--suave:#5b6b80}
  *{box-sizing:border-box}
  body{font-family:"Poppins",-apple-system,system-ui,sans-serif;margin:0;background:#eef2f7;color:#1c2b3a;line-height:1.6}
  .top{position:relative;overflow:hidden;color:#fff;background:linear-gradient(118deg,#022B4D,#003C67 34%,#005B67 66%,#15857E);padding:calc(14px + env(safe-area-inset-top)) 18px 24px}
  .top::before{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(520px 240px at 90% -12%,rgba(255,255,255,.13),transparent 60%)}
  .top-in{max-width:720px;margin:0 auto;position:relative}
  .top-marca{display:flex;align-items:center;gap:9px;text-decoration:none;color:#fff}
  .top-marca img{width:30px;height:30px;background:#fff;border-radius:8px;padding:3px;box-shadow:0 2px 8px rgba(2,20,38,.25)}
  .top-marca b{font-size:13px;font-weight:600;opacity:.94}
  .top-marca .volver-top{margin-left:auto;font-size:12.5px;color:rgba(255,255,255,.88);font-weight:600;white-space:nowrap}
  .top h1{margin:16px 0 0;color:#fff;font-size:clamp(22px,4.6vw,27px);font-weight:700;letter-spacing:-.02em;line-height:1.2;display:flex;align-items:center;gap:10px}
  .top h1 svg{flex:0 0 auto}
  .top .fecha{margin:8px 0 0;color:rgba(255,255,255,.75);font-size:13px}
  .wrap{max-width:720px;margin:0 auto;padding:14px 18px 60px}
  h2{color:var(--navy2);font-size:17px;margin:26px 0 6px}
  p,li{font-size:15px}
  .volver{display:inline-block;margin-top:6px;color:var(--navy2);font-weight:700;text-decoration:none}
  .caja{background:#eef6f7;border:1px solid #b9dfe0;border-radius:12px;padding:12px 14px;margin:16px 0}
</style>
</head>
<body>
<div class="top"><div class="top-in">
  <a class="top-marca" href="/"><img src="/logo.svg?v=ofc" alt=""><b>Red de Acopio y Refugio</b><span class="volver-top">← Volver al inicio</span></a>
  <h1><svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>Aviso legal y condiciones de uso</h1>
  <p class="fecha">Última actualización: 1 de julio de 2026</p>
</div></div>
<div class="wrap">

  <h2>1. Qué es esta plataforma</h2>
  <p><b>Red de Acopio y Refugio</b> es una iniciativa ciudadana, <b>gratuita, voluntaria e
  independiente</b>, que sirve únicamente como <b>canal de conexión</b> entre personas que
  necesitan ayuda y personas, voluntarios, Manos Solidarias o centros de acopio dispuestos a
  darla. No estamos afiliados a ningún organismo oficial ni grupo de rescate. Al usar la web
  o los bots de la Red aceptas estas condiciones.</p>

  <h2>2. No verificamos ni supervisamos</h2>
  <p>La Red <b>no verifica</b> la identidad, credenciales, títulos profesionales ni
  antecedentes de quienes piden u ofrecen ayuda, y <b>no participa, supervisa ni garantiza</b>
  la ayuda que las personas acuerden entre sí. Toda coordinación posterior al contacto ocurre
  directamente entre las partes y bajo su exclusiva responsabilidad.</p>

  <h2>3. Tu responsabilidad al usar la Red</h2>
  <ul>
    <li>Verifica la identidad de la persona con quien vas a coordinar.</li>
    <li>Coordina encuentros en <b>lugares públicos o seguros</b> y, si puedes, acompañado.</li>
    <li>No entregues dinero, claves ni datos sensibles (cédula, cuentas) a desconocidos.</li>
    <li>Si algo te parece sospechoso, no continúes y usa el botón 🚩 <b>Denunciar</b>.</li>
  </ul>

  <h2>4. Salud y apoyo emocional</h2>
  <div class="caja">🧠 Los contactos de salud, medicinas o apoyo emocional que se logren a
  través de la Red son <b>ayuda voluntaria entre personas</b> y <b>no sustituyen la atención
  médica ni psicológica profesional</b>. Si estás en crisis o es una emergencia médica, llama
  al <b>911</b> o acude al centro de salud más cercano.</div>

  <h2>5. No somos un servicio de emergencias</h2>
  <p>La Red no atiende emergencias en tiempo real ni despacha rescates. En una emergencia
  llama siempre al <b>911</b>.</p>

  <h2>6. Tus datos</h2>
  <p>El contacto de quien pide ayuda <b>nunca se publica</b> en la web: solo se comparte en
  privado con miembros de la red que puedan ayudar. Puedes pedir el retiro de tus datos o de
  una publicación escribiendo a
  <a href="mailto:contacto@redacopioyrefugio.com">contacto@redacopioyrefugio.com</a>.</p>

  <h2>7. Moderación</h2>
  <p>Revisamos los pedidos antes de publicarlos y retiramos el contenido denunciado. Esta
  moderación es de <b>mejor esfuerzo</b> y no constituye una garantía sobre las personas ni
  sobre el contenido publicado.</p>

  <h2>8. Limitación de responsabilidad</h2>
  <p>En la máxima medida permitida por la ley, la plataforma, sus operadores y colaboradores
  <b>no son responsables</b> por daños, pérdidas o perjuicios derivados de las interacciones,
  encuentros o ayuda acordada entre usuarios, ni por la exactitud de la información publicada
  por terceros.</p>

  <h2>9. Contacto</h2>
  <p><a href="mailto:contacto@redacopioyrefugio.com">contacto@redacopioyrefugio.com</a></p>

  <a class="volver" href="/">← Volver a la Red de Acopio y Refugio</a>
</div>
</body>
</html>
"""

FORM_OFRECER = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,follow">
<meta name="theme-color" content="#003C67">
<title>Ofrecer mi ayuda · Red de Acopio y Refugio</title>
<link rel="icon" href="/logo.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap">
<style>
:root{--navy:#003C67;--teal:#1E9C94;--linea:#dfe6ee;--txt:#0f1f33}
*{box-sizing:border-box}
body{margin:0;font-family:"Poppins",-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#eef2f7;color:var(--txt)}
.top{position:relative;overflow:hidden;color:#fff;background:linear-gradient(118deg,#022B4D,#003C67 34%,#005B67 66%,#15857E);padding:calc(14px + env(safe-area-inset-top)) 18px 24px}
.top::before{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(520px 240px at 90% -12%,rgba(255,255,255,.13),transparent 60%)}
.top-in{max-width:560px;margin:0 auto;position:relative}
.top-marca{display:flex;align-items:center;gap:9px;text-decoration:none;color:#fff}
.top-marca img{width:30px;height:30px;background:#fff;border-radius:8px;padding:3px;box-shadow:0 2px 8px rgba(2,20,38,.25)}
.top-marca b{font-size:13px;font-weight:600;opacity:.94}
.top-marca .volver{margin-left:auto;font-size:12.5px;color:rgba(255,255,255,.88);font-weight:600;white-space:nowrap}
.top h1{margin:16px 0 0;font-size:clamp(23px,5vw,28px);font-weight:700;letter-spacing:-.02em;line-height:1.18;display:flex;align-items:center;gap:10px}
.top h1 svg{flex:0 0 auto}
.top p{margin:9px 0 0;font-size:14px;color:rgba(255,255,255,.9);line-height:1.55}
.wrap{max-width:560px;margin:0 auto;padding:16px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--navy);box-shadow:0 0 0 3px rgba(11,42,74,.08)}
.pie-sub{text-align:center;font-size:12px;color:#8a99ad;margin:22px 0 26px;line-height:1.6}
.pie-sub a{color:#005B67;font-weight:600;text-decoration:none}
.card{background:#fff;border:1px solid var(--linea);border-radius:16px;padding:18px;margin:14px 0}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
label .op{color:#8a99ad;font-weight:500}
input,select,textarea{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:10px;font-size:15px;font-family:inherit;background:#fff}
textarea{min-height:96px;resize:vertical}
.hp{position:absolute;left:-9999px;top:-9999px}
.btn{display:block;width:100%;margin-top:18px;padding:14px;border:0;border-radius:12px;background:var(--teal);color:#fff;font-weight:800;font-size:16px;cursor:pointer}
.btn:disabled{opacity:.6}
.msg{padding:14px;border-radius:12px;margin:10px 0;font-size:14.5px;line-height:1.45;display:none}
.ok{background:#e7f6f4;border:1px solid var(--teal);color:#0b5f59}
.err{background:#fdeaea;border:1px solid #e57373;color:#9b2226}
.alt{text-align:center;font-size:13.5px;color:#5b6b80;margin-top:16px;line-height:1.5}
.alt a{color:var(--teal);font-weight:700;text-decoration:none}
.nota{font-size:12.5px;color:#8a99ad;margin-top:10px;line-height:1.45}
</style></head><body>
<div class="top"><div class="top-in">
  <a class="top-marca" href="/"><img src="/logo.svg?v=ofc" alt=""><b>Red de Acopio y Refugio</b><span class="volver">← Volver al inicio</span></a>
  <h1><svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 21s-7.4-4.6-9.9-9.1C.7 8.9 2.6 5 6.3 5c2.1 0 3.6 1.2 4.7 2.8h2C14.1 6.2 15.6 5 17.7 5c3.7 0 5.6 3.9 4.2 6.9C19.4 16.4 12 21 12 21z"/></svg>Ofrecer mi ayuda</h1>
  <p>Si puedes ayudar <b>gratis</b> — comida, transporte, salud, inspección de estructuras, ayuda en línea, un oficio — publícate aquí y la gente te encontrará. Toma menos de 1 minuto. Aparece de inmediato; nuestro equipo puede retirar cualquier publicación indebida.</p>
</div></div>
<div class="wrap">
  <div class="card">
    <div id="ok" class="msg ok"></div>
    <div id="err" class="msg err"></div>
    <form id="f" autocomplete="on">
      <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
      <label>Tu nombre</label>
      <input id="nombre" maxlength="80" placeholder="Nombre o del grupo/negocio" required>
      <label>¿Qué tipo de ayuda ofreces?</label>
      <select id="categoria" required>
        <option value="">Elige una categoría…</option>
        <option value="comida">🍲 Comida</option>
        <option value="agua">💧 Agua</option>
        <option value="salud">🩹 Salud / primeros auxilios</option>
        <option value="transporte">🚐 Transporte</option>
        <option value="carga">🔌 Carga de teléfonos</option>
        <option value="hospedaje">🛏️ Hospedaje temporal</option>
        <option value="herramientas">🔦 Herramientas / linternas</option>
        <option value="remoto">💻 Remoto / En Línea</option>
        <option value="estructural">🏗️ Estructural / Inspección</option>
        <option value="oficios">🛠️ Oficios</option>
        <option value="otro">📦 Otra ayuda</option>
      </select>
      <label>¿Qué ofreces exactamente?</label>
      <textarea id="descripcion" maxlength="600" placeholder="Ej.: Soy ingeniero civil, hago inspección de grietas y columnas sin costo. O: consulta médica online gratis." required></textarea>
      <label>Zona donde ayudas</label>
      <input id="zona" maxlength="120" placeholder="Ej.: Caracas, o toda Venezuela si es en línea" required>
      <label>WhatsApp de contacto</label>
      <input id="contacto" maxlength="60" placeholder="Ej.: 0412 1234567" required>
      <div style="background:#eef6f5;border-radius:8px;padding:9px 11px;font-size:12.5px;color:#0e5a55;line-height:1.45;margin-top:6px">🔒 Tu número <b>NO se publica</b> en la web. Cuando alguien pida contactarte te llegará una solicitud (WhatsApp o Telegram) y tú decides si compartes tu número.</div>
      <label>Disponibilidad <span class="op">(opcional)</span></label>
      <input id="horario" maxlength="80" placeholder="Ej.: 8am-6pm, o cualquier hora">
      <p class="nota">Al publicar aceptas el <a href="/aviso-legal.html" target="_blank" rel="noopener">Aviso legal</a>: la Red solo conecta personas voluntariamente; no verifica ni supervisa la ayuda que acuerdes dar.</p>
      <button class="btn" id="enviar" type="submit">Publicar mi ayuda</button>
      <p class="nota">🔒 Tu WhatsApp se muestra para que te contacten. No publicamos cédula ni datos sensibles. Es una iniciativa ciudadana e independiente.</p>
    </form>
  </div>
  <p class="alt">¿Prefieres hacerlo por WhatsApp? <a href="https://wa.me/message/6TEVCZCHJ2TPN1" target="_blank" rel="noopener">💬 Escríbenos →</a> · ¿O por Telegram? <a href="https://t.me/angelesapoyove_bot" target="_blank" rel="noopener">Abre nuestro bot →</a></p>
  <p class="pie-sub">Red de Acopio y Refugio · Iniciativa ciudadana e independiente · <a href="/aviso-legal.html">Aviso legal</a></p>
</div>
<script>
var f=document.getElementById('f'),ok=document.getElementById('ok'),err=document.getElementById('err'),btn=document.getElementById('enviar');
function val(id){return (document.getElementById(id).value||'').trim();}
f.addEventListener('submit',function(ev){
  ev.preventDefault();
  err.style.display='none';
  var body={
    website:f.website.value||'',
    nombre:val('nombre'),categoria:val('categoria'),descripcion:val('descripcion'),
    zona:val('zona'),contacto:val('contacto'),horario:val('horario')
  };
  if(!body.nombre||!body.categoria||!body.descripcion||!body.zona||!body.contacto){
    err.textContent='Por favor completa todos los campos obligatorios.';err.style.display='block';return;
  }
  btn.disabled=true;btn.textContent='Enviando… (puede tardar en conexiones lentas)';
  fetch('/api/ofrecer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json().catch(function(){return {ok:r.ok};});})
    .then(function(d){
      if(d&&d.ok){
        f.style.display='none';err.style.display='none';
        ok.innerHTML='✅ <b>¡Gracias!</b> Tu ayuda <b>ya está publicada</b> en la web para que la gente te encuentre. <br><br><a href="/" style="color:#0b5f59;font-weight:700">Volver al inicio</a>';
        ok.style.display='block';window.scrollTo(0,0);
      }else{
        throw new Error((d&&d.error)||'No se pudo enviar');
      }
    })
    .catch(function(e){
      err.textContent='No se pudo enviar ('+(e.message||'error')+'). Tranquilo: tus datos siguen aquí en el formulario — revisa tu conexión y toca «Reintentar publicación».';
      err.style.display='block';btn.disabled=false;btn.textContent='Reintentar publicación';
    });
});
</script>
</body></html>"""


# Formulario web público para PEDIR ayuda sin usar Telegram ni el chat de emergencias.
# POST JSON a /api/solicitar (backend.py) → crea un llamado 'pendiente' (no sale a la web) +
# guarda el contacto PRIVADO en solicitudes_web → un moderador lo Publica con un toque desde
# el bot (callback pweb|) → pasa a estado 'web' (sale anonimizado) y se avisa a la red.
FORM_PEDIR = """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,follow">
<meta name="theme-color" content="#003C67">
<title>Pedir ayuda · Red de Acopio y Refugio</title>
<link rel="icon" href="/logo.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700&display=swap">
<style>
:root{--navy:#003C67;--rojo:#b3261e;--linea:#dfe6ee;--txt:#0f1f33}
*{box-sizing:border-box}
body{margin:0;font-family:"Poppins",-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#eef2f7;color:var(--txt)}
.top{position:relative;overflow:hidden;color:#fff;background:linear-gradient(118deg,#022B4D,#003C67 34%,#005B67 66%,#15857E);padding:calc(14px + env(safe-area-inset-top)) 18px 24px}
.top::before{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(520px 240px at 90% -12%,rgba(255,255,255,.13),transparent 60%)}
.top-in{max-width:560px;margin:0 auto;position:relative}
.top-marca{display:flex;align-items:center;gap:9px;text-decoration:none;color:#fff}
.top-marca img{width:30px;height:30px;background:#fff;border-radius:8px;padding:3px;box-shadow:0 2px 8px rgba(2,20,38,.25)}
.top-marca b{font-size:13px;font-weight:600;opacity:.94}
.top-marca .volver{margin-left:auto;font-size:12.5px;color:rgba(255,255,255,.88);font-weight:600;white-space:nowrap}
.top h1{margin:16px 0 0;font-size:clamp(23px,5vw,28px);font-weight:700;letter-spacing:-.02em;line-height:1.18;display:flex;align-items:center;gap:10px}
.top h1 svg{flex:0 0 auto}
.top p{margin:9px 0 0;font-size:14px;color:rgba(255,255,255,.9);line-height:1.55}
.wrap{max-width:560px;margin:0 auto;padding:16px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--navy);box-shadow:0 0 0 3px rgba(11,42,74,.08)}
.pie-sub{text-align:center;font-size:12px;color:#8a99ad;margin:22px 0 26px;line-height:1.6}
.pie-sub a{color:#005B67;font-weight:600;text-decoration:none}
.card{background:#fff;border:1px solid var(--linea);border-radius:16px;padding:18px;margin:14px 0}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
label .op{color:#8a99ad;font-weight:500}
input,select,textarea{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:10px;font-size:15px;font-family:inherit;background:#fff}
textarea{min-height:96px;resize:vertical}
.hp{position:absolute;left:-9999px;top:-9999px}
.btn{display:block;width:100%;margin-top:18px;padding:14px;border:0;border-radius:12px;background:var(--rojo);color:#fff;font-weight:800;font-size:16px;cursor:pointer}
.btn:disabled{opacity:.6}
.msg{padding:14px;border-radius:12px;margin:10px 0;font-size:14.5px;line-height:1.45;display:none}
.ok{background:#e7f6f4;border:1px solid #1E9C94;color:#0b5f59}
.err{background:#fdeaea;border:1px solid #e57373;color:#9b2226}
.alt{text-align:center;font-size:13.5px;color:#5b6b80;margin-top:16px;line-height:1.5}
.alt a{color:#005B67;font-weight:700;text-decoration:none}
.nota{font-size:12.5px;color:#8a99ad;margin-top:10px;line-height:1.45}
.priv{background:#fff7f7;border:1px solid #f0d9d9;border-radius:12px;padding:12px;margin-top:14px;font-size:13px;color:#7a2e2a;line-height:1.45}
.urg{background:#fff3cd;border:1px solid #ffe08a;border-radius:12px;padding:12px;margin:0 0 6px;font-size:13px;color:#7a5c00;line-height:1.45}
.flyer{background:#f0f6fb;border:1.5px dashed #9db8cf;border-radius:12px;padding:14px;margin:2px 0 4px}
.flyer b{font-size:14.5px}
.flyer p{margin:6px 0 10px;font-size:13px;color:#5b6b80;line-height:1.45}
.flyer input[type=file]{display:none}
.btn-flyer{display:block;width:100%;padding:12px;border:0;border-radius:10px;background:var(--navy);color:#fff;font-weight:700;font-size:14.5px;cursor:pointer;font-family:inherit}
#fprev{display:none;margin-top:10px}
#fprev img{max-width:100%;max-height:280px;border-radius:10px;display:block;margin:0 auto}
.fquitar{display:block;margin:8px auto 0;background:none;border:0;color:#9b2226;font-weight:700;font-size:13px;cursor:pointer;font-family:inherit}
.fstat{display:none;margin-top:10px;font-size:13.5px;border-radius:10px;padding:10px 12px;line-height:1.45}
.fstat.lee{background:#fff8e6;border:1px solid #f2dfa0;color:#7a5c00}
.fstat.ok2{background:#e7f6f4;border:1px solid #1E9C94;color:#0b5f59}
.fstat.err2{background:#fdeaea;border:1px solid #e57373;color:#9b2226}
#refs{display:none;margin-top:10px}
.ref-card{background:#eef6ff;border:1px solid #b6d4f0;border-radius:10px;padding:10px 12px;margin-top:8px;font-size:13px;line-height:1.5;color:#1c3a5a}
.ref-card b{color:#003C67}
.ref-est{display:inline-block;font-size:10.5px;font-weight:700;padding:1px 8px;border-radius:99px;color:#fff;background:#1E9C94;margin-right:5px;text-transform:uppercase}
.ref-est.gris{background:#4a4a4a}
.ref-nota{font-size:12px;color:#5b6b80;margin-top:8px;line-height:1.45}
.ref-nota a{color:#005B67;font-weight:700}
</style></head><body>
<div class="top"><div class="top-in">
  <a class="top-marca" href="/"><img src="/logo.svg?v=ofc" alt=""><b>Red de Acopio y Refugio</b><span class="volver">← Volver al inicio</span></a>
  <h1><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.6"/><path d="M5.9 5.9l3.4 3.4M18.1 5.9l-3.4 3.4M18.1 18.1l-3.4-3.4M5.9 18.1l3.4-3.4"/></svg>Pedir ayuda</h1>
  <p>¿Necesitas algo — comida, agua, medicinas, un lugar donde dormir? ¿Buscas a una persona desaparecida y tienes su flyer? Cuéntanos o sube la imagen. Tu pedido se publica al instante y le avisamos a los centros y voluntarios de tu zona.</p>
</div></div>
<div class="wrap">
  <div class="urg">⚠️ Si es una <b>emergencia de vida o muerte</b> (persona atrapada, herida grave), llama primero al <b>911</b> o a los cuerpos de rescate. Este formulario no es un servicio de emergencia inmediata.</div>
  <div class="card">
    <div id="ok" class="msg ok"></div>
    <div id="err" class="msg err"></div>
    <form id="f" autocomplete="on">
      <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
      <div class="flyer">
        <b>📸 ¿Tienes un flyer o una foto con la información?</b>
        <p>Por ejemplo, el volante de una <b>persona desaparecida</b>. Súbelo y lo leemos por ti: el formulario se llena solo y la imagen se publica junto con tu pedido. No necesitas registrarte.</p>
        <input type="file" id="flyer" accept="image/*">
        <button type="button" class="btn-flyer" id="fbtn">📷 Subir flyer o foto</button>
        <div id="fprev"><img id="fimg" alt="Imagen del pedido"><button type="button" class="fquitar" id="fquitar">✖ Quitar la imagen</button></div>
        <div id="fstat" class="fstat"></div>
      </div>
      <label>Tu nombre <span class="op">(opcional)</span></label>
      <input id="nombre" maxlength="80" placeholder="Como quieres que te llamen">
      <label>¿Qué necesitas?</label>
      <select id="categoria" required>
        <option value="">Elige una categoría…</option>
__OPCIONES_CATS_PEDIR__
      </select>
      <div id="bloque-persona" style="display:none">
        <label>¿Cómo se llama la persona que buscas?</label>
        <input id="persona" maxlength="80" placeholder="Nombre y apellido de la persona desaparecida">
        <div id="refs"></div>
      </div>
      <label>Cuéntanos con detalle</label>
      <textarea id="descripcion" maxlength="600" placeholder="Ej.: Somos 4 personas, 2 niños, necesitamos agua y comida. O: mi mamá necesita insulina." required></textarea>
      <label>¿En qué zona estás?</label>
      <input id="zona" maxlength="120" placeholder="Ej.: Caracas, Petare, sector La Línea" required>
      <label>WhatsApp para que te contacten</label>
      <input id="contacto" maxlength="60" placeholder="Ej.: 0412 1234567" required>
      <div class="priv">🔒 Tu WhatsApp <b>NO se publica</b> en la web. Solo lo ven, en privado, los centros y voluntarios de la red que puedan ayudarte. En la página aparece únicamente <b>qué necesitas y tu zona</b>, sin tu nombre ni tu número.</div>
      <p class="nota">Al enviar aceptas el <a href="/aviso-legal.html" target="_blank" rel="noopener">Aviso legal</a>: la Red solo conecta personas voluntariamente; no verifica ni supervisa la ayuda. Verifica identidad y coordina en lugares seguros.</p>
      <button class="btn" id="enviar" type="submit">Enviar mi pedido</button>
      <p class="nota">Iniciativa ciudadana e independiente. Tu pedido se publica al instante; nuestro equipo modera todos los pedidos y remueve los abusos.</p>
    </form>
  </div>
  <p class="alt">¿Prefieres hacerlo por WhatsApp? <a href="https://wa.me/message/6TEVCZCHJ2TPN1" target="_blank" rel="noopener">💬 Escríbenos →</a> · ¿O por Telegram? <a href="https://t.me/angelesapoyove_bot" target="_blank" rel="noopener">Abre nuestro bot →</a></p>
  <p class="pie-sub">Red de Acopio y Refugio · Iniciativa ciudadana e independiente · <a href="/aviso-legal.html">Aviso legal</a></p>
</div>
<script>
var f=document.getElementById('f'),ok=document.getElementById('ok'),err=document.getElementById('err'),btn=document.getElementById('enviar');
function val(id){return (document.getElementById(id).value||'').trim();}

// --- Persona desaparecida: campo extra + búsqueda EN VIVO en los registros
// comunitarios (/api/buscar_persona: hospitales, rescatados, desaparecidos).
// Si la persona ya aparece como rescatada/ingresada, se le muestra a quien
// reporta ANTES de enviar — referencia, nunca confirmación.
var bloqueP=document.getElementById('bloque-persona'),inpP=document.getElementById('persona'),
    refs=document.getElementById('refs'),tRef=null,ultimaQ='';
var EST_REF={ingresado:'🏥 Ingresado/a',rescatado:'✅ Rescatado/a',localizado:'✅ Localizado/a',
             alta:'🏥 Dado/a de alta',fallecido:'🕊️ Reportado/a como fallecido/a'};
function normTxt(s){return (s||'').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');}
function escT(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
document.getElementById('categoria').addEventListener('change',function(){
  bloqueP.style.display=this.value==='desaparecido'?'block':'none';
});
function buscarRefs(){
  var q=(inpP.value||'').trim();
  var toks=normTxt(q).split(/\\s+/).filter(function(t){return t.length>=2;});
  if(toks.length<2||q===ultimaQ)return;
  ultimaQ=q;
  fetch('/api/buscar_persona?q='+encodeURIComponent(q))
    .then(function(r){return r.json().catch(function(){return null;});})
    .then(function(r){
      if(!r||!r.ok||(inpP.value||'').trim()!==q)return;
      var hits=(r.resultados||[]).filter(function(p){
        if(!EST_REF[p.estado])return false;
        var n=normTxt(p.nombre);
        return toks.every(function(t){return n.indexOf(t)>=0;});
      }).slice(0,3);
      if(!hits.length){refs.style.display='none';refs.innerHTML='';return;}
      var h='<div class="ref-card"><b>👀 Buenas noticias posibles:</b> encontramos registros con ese nombre:</div>';
      hits.forEach(function(p){
        h+='<div class="ref-card"><span class="ref-est'+(p.estado==='fallecido'?' gris':'')+'">'
          +EST_REF[p.estado]+'</span> <b>'+escT(p.nombre)+'</b>'
          +(p.lugar?' — 📍 '+escT(p.lugar):'')+(p.cedula?' · C.I. '+escT(p.cedula):'')
          +'<br><small>Fuente: '+escT((p.fuentes||[p.fuente]).join(' + '))+(p.fecha?' · '+escT(p.fecha):'')+'</small></div>';
      });
      h+='<div class="ref-nota">⚠️ Información comunitaria, no oficial: <b>confirma llamando al centro</b>. '
        +'<a href="/buscar.html?q='+encodeURIComponent(q)+'" target="_blank" rel="noopener">Ver todos los resultados →</a>'
        +'<br>Si no es tu familiar, envía tu pedido normalmente.</div>';
      refs.innerHTML=h;refs.style.display='block';
    })
    .catch(function(){});
}
inpP.addEventListener('input',function(){clearTimeout(tRef);tRef=setTimeout(buscarRefs,800);});
inpP.addEventListener('blur',buscarRefs);

// --- Flyer / foto del pedido: se reduce en el navegador (canvas → JPEG), la IA
// la lee (/api/leer_flyer) y prellena el formulario; la persona revisa y envía.
var IMG_B64=null;
var fin=document.getElementById('flyer'),fbtn=document.getElementById('fbtn'),
    fprev=document.getElementById('fprev'),fimg=document.getElementById('fimg'),
    fquitar=document.getElementById('fquitar'),fstat=document.getElementById('fstat');
function fmsg(cls,html){fstat.className='fstat '+cls;fstat.innerHTML=html;fstat.style.display='block';}
function reducir(file,cb){
  var fr=new FileReader();
  fr.onload=function(){
    var im=new Image();
    im.onload=function(){
      var m=1400,k=Math.min(1,m/Math.max(im.width,im.height));
      var cv=document.createElement('canvas');
      cv.width=Math.max(1,Math.round(im.width*k));cv.height=Math.max(1,Math.round(im.height*k));
      cv.getContext('2d').drawImage(im,0,0,cv.width,cv.height);
      var q=0.82,du=cv.toDataURL('image/jpeg',q);
      // Tope ~700k de base64: el POST completo debe quedar bajo el límite de 1 MB
      // que nginx pone al body (client_max_body_size por defecto).
      while(du.length>700000&&q>0.4){q-=0.15;du=cv.toDataURL('image/jpeg',q);}
      cb(du.length>950000?null:du);
    };
    im.onerror=function(){cb(null);};
    im.src=fr.result;
  };
  fr.onerror=function(){cb(null);};
  fr.readAsDataURL(file);
}
function pon(id,v){var el=document.getElementById(id);if(v&&!el.value.trim())el.value=v;}
fbtn.addEventListener('click',function(){fin.click();});
fquitar.addEventListener('click',function(){
  IMG_B64=null;fin.value='';fprev.style.display='none';fstat.style.display='none';
});
fin.addEventListener('change',function(){
  var file=fin.files&&fin.files[0];if(!file)return;
  fmsg('lee','⏳ Preparando la imagen…');
  reducir(file,function(du){
    if(!du){fmsg('err2','No se pudo procesar esa imagen. Prueba con otra (JPG o PNG).');fin.value='';return;}
    IMG_B64=du.split(',')[1];fimg.src=du;fprev.style.display='block';
    fmsg('lee','🤖 Leyendo la imagen… (puede tardar unos segundos)');
    fetch('/api/leer_flyer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({website:f.website.value||'',imagen:IMG_B64})})
      .then(function(r){return r.json().catch(function(){return {ok:false};});})
      .then(function(d){
        if(d&&d.ok&&d.legible){
          if(d.categoria&&!val('categoria'))document.getElementById('categoria').value=d.categoria;
          // En un flyer de desaparecido, el nombre leído es el de la PERSONA BUSCADA:
          // va al campo persona (y dispara la búsqueda en registros), no al del reportante.
          if(val('categoria')==='desaparecido'){
            bloqueP.style.display='block';
            pon('persona',d.nombre);buscarRefs();
          }else{
            pon('nombre',d.nombre);
          }
          pon('descripcion',d.descripcion);pon('zona',d.zona);pon('contacto',d.contacto);
          fmsg('ok2','✅ Leímos la imagen y llenamos el formulario por ti. <b>Revisa que los datos estén bien</b>, corrige lo que falte y envía.');
        }else{
          fmsg('err2','No pudimos leer la imagen automáticamente. Tranquilo: <b>la imagen igual se enviará con tu pedido</b>; completa los campos tú mismo.');
        }
      })
      .catch(function(){
        fmsg('err2','No pudimos leer la imagen automáticamente. Tranquilo: <b>la imagen igual se enviará con tu pedido</b>; completa los campos tú mismo.');
      });
  });
});

f.addEventListener('submit',function(ev){
  ev.preventDefault();
  err.style.display='none';
  var body={
    website:f.website.value||'',
    nombre:val('nombre'),categoria:val('categoria'),descripcion:val('descripcion'),
    zona:val('zona'),contacto:val('contacto'),imagen:IMG_B64||'',
    persona:val('categoria')==='desaparecido'?val('persona'):''
  };
  if(!body.categoria||!body.descripcion||!body.zona||!body.contacto){
    err.textContent='Por favor completa qué necesitas, la descripción, tu zona y tu WhatsApp.';err.style.display='block';return;
  }
  btn.disabled=true;btn.textContent='Enviando… (puede tardar en conexiones lentas)';
  fetch('/api/solicitar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json().catch(function(){return {ok:r.ok};});})
    .then(function(d){
      if(d&&d.ok){
        f.style.display='none';err.style.display='none';
        // `publicado` viene del backend: true = auto-publicado; false = en revisión
        // (persona desaparecida — datos de un tercero — o kill-switch apagado).
        ok.innerHTML=(d.publicado
          ?'✅ <b>¡Publicado!</b> Tu pedido ya está en la web y le estamos avisando a los centros y voluntarios de tu zona. Te contactarán por WhatsApp.'
          :'✅ <b>¡Recibido!</b> Nuestro equipo revisa tu pedido y avisa a los centros y voluntarios de tu zona. Te contactarán por WhatsApp.')
          +' <br><br><a href="/" style="color:#0b5f59;font-weight:700">Volver al inicio</a>';
        ok.style.display='block';window.scrollTo(0,0);
      }else{
        throw new Error((d&&d.error)||'No se pudo enviar');
      }
    })
    .catch(function(e){
      err.textContent='No se pudo enviar ('+(e.message||'error')+'). Tranquilo: tu pedido sigue aquí en el formulario — revisa tu conexión y toca «Reintentar envío».';
      err.style.display='block';btn.disabled=false;btn.textContent='Reintentar envío';
    });
});
</script>
</body></html>"""


def _js(obj):
    """JSON seguro para incrustar en <script> (escapa < > &)."""
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _horas(iso):
    try:
        d = datetime.datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.datetime.now(timezone.utc) - d).total_seconds() / 3600
    except Exception:
        return 0.0


def _norm(s):
    return " ".join((s or "").lower().split())


ZONAS = [
    (["san joaquin", "san joaquín"], "San Joaquín"),
    (["valencia"], "Valencia"),
    (["caraballeda"], "Caraballeda (Vargas)"),
    (["tanaguarena", "tanaguarenas"], "Tanaguarena (Vargas)"),
    (["los corales"], "Los Corales (Vargas)"),
    (["la guaira", "maiquetia", "maiquetía", "catia la mar", "macuto", "vargas", "el cojo", "naiguata", "naiguatá", "caribe", "coralmar", "los cocos"], "La Guaira (Vargas)"),
    (["catia"], "Catia"),
    (["sabana grande"], "Sabana Grande"),
    (["chacao"], "Chacao"),
    (["altamira"], "Altamira"),
    (["los palos grandes"], "Los Palos Grandes"),
    (["la candelaria", "candelaria"], "La Candelaria"),
    (["montalban", "montalbán"], "Montalbán"),
    (["algodonal"], "El Algodonal"),
    (["san agustin", "san agustín"], "San Agustín"),
    (["el cafetal"], "El Cafetal"),
    (["piedra azul"], "Piedra Azul"),
    (["bello campo"], "Bello Campo"),
    (["antimano", "antímano"], "Antímano"),
    (["caricuao"], "Caricuao"),
    (["el valle"], "El Valle"),
    (["petare"], "Petare"),
    (["baruta"], "Baruta"),
    (["el hatillo"], "El Hatillo"),
    (["chacaito", "chacaíto"], "Chacaíto"),
    (["bello monte"], "Bello Monte"),
    (["el paraiso", "el paraíso"], "El Paraíso"),
    (["la urbina"], "La Urbina"),
    (["quinta crespo"], "Quinta Crespo"),
    (["san bernardino"], "San Bernardino"),
    (["la pastora"], "La Pastora"),
    (["las acacias"], "Las Acacias"),
    (["la paz"], "La Paz (Montalbán)"),
    (["domingo luciani", "el llanito", "llanito"], "El Llanito (Sucre)"),
    (["guarenas"], "Guarenas"),
    (["el vigia", "el vigía"], "El Vigía"),
    (["sucre", "gato negro"], "Municipio Sucre"),
    (["libertador"], "Municipio Libertador"),
    (["caracas", "ucv", "ccs"], "Caracas (otros)"),
]


def _zona(texto):
    t = (texto or "").lower()
    for claves, etiqueta in ZONAS:
        if any(c in t for c in claves):
            return etiqueta
    return "Otras zonas"


def _util(m):
    """¿Tiene la información mínima para que alguien pueda ayudar?"""
    ubi = (m.get("ubicacion") or "").strip()
    det = (m.get("detalle") or "").strip()
    con = (m.get("contacto") or "").strip()
    cat = m["categoria"]
    vagas = ("sin especificar", "no especifica", "sin ubicaci", "desconocid", "(sin")
    ubi_vaga = (not ubi) or any(v in ubi.lower() for v in vagas)
    if cat in ("rescate", "heridos", "donaciones"):
        return (not ubi_vaga) and len(det) >= 8     # sin DÓNDE real no se puede ayudar
    if cat == "desaparecidos":
        return (bool(ubi) and not ubi_vaga) or bool(con)  # dónde se vio o a quién llamar
    if cat == "contacto":
        return bool(con)
    if cat == "info":
        if len(det) < 12:
            return False
        if bool(ubi) and not ubi_vaga:
            return True                              # info con lugar → se muestra
        # info SIN lugar: solo si es un recurso de coordinación (otro canal/bot/grupo)
        texto = ((m.get("titulo") or "") + " " + det + " " + (m.get("texto_original") or "")).lower()
        return any(k in texto for k in ("t.me", "telegram", "canal", "grupo", "bot"))
    return True


def _filtrar(datos):
    """Ventana de frescura + dedup (reposts idénticos y desaparecidos por persona)."""
    vivos = []
    for m in datos:
        if not _util(m):                     # sin info básica → no se muestra
            continue
        v = VENTANA_HORAS.get(m["categoria"], 72)
        if v is not None and _horas(m["tg_fecha"]) > v:
            continue
        vivos.append(m)

    desap, vistos_txt, otros = {}, set(), []
    for m in vivos:  # llega ordenado más reciente primero
        if m["categoria"] == "desaparecidos":
            resuelto = (m.get("estado_persona") or "buscando") != "buscando"
            if resuelto and _horas(m["tg_fecha"]) > DESAP_RESUELTO_HORAS:
                continue  # archiva resueltos viejos
            k = _norm(m["titulo"])
            prev = desap.get(k)
            if prev is None:
                desap[k] = m
            elif resuelto and (prev.get("estado_persona") or "buscando") == "buscando":
                desap[k] = m  # el resuelto gana sobre el "buscando"
            continue
        # otros: colapsar reposts de texto idéntico
        h = hashlib.md5(_norm(m["texto_original"]).encode()).hexdigest()
        if h in vistos_txt:
            continue
        vistos_txt.add(h)
        otros.append(m)

    combinado = otros + list(desap.values())
    combinado.sort(key=lambda m: m["tg_fecha"], reverse=True)
    return combinado


def _fetch_json(url, timeout=7):
    req = urllib.request.Request(url, headers={"User-Agent": "angeles-dashboard"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _ts(iso):
    try:
        return datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _sismos_red():
    """Consulta USGS + EMSC (resiliente: si una falla, usa la otra)."""
    ini = (datetime.datetime.now(timezone.utc) - datetime.timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    # USGS
    try:
        d = _fetch_json("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
                        f"&starttime={ini}&minlatitude=0.6&maxlatitude=15.9"
                        "&minlongitude=-73.4&maxlongitude=-59.8&orderby=time&limit=60")
        for f in d.get("features", []):
            p, c = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None])
            if c[0] is None:
                continue
            ms = p.get("time")
            iso = datetime.datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None
            out.append({"mag": p.get("mag"), "lugar": p.get("place") or "", "fecha": iso,
                        "lat": c[1], "lon": c[0], "fuente": "USGS"})
    except Exception:
        pass
    # EMSC
    try:
        d = _fetch_json("https://www.seismicportal.eu/fdsnws/event/1/query?format=json"
                        f"&start={ini}&minlat=0.6&maxlat=15.9&minlon=-73.4&maxlon=-59.8&limit=60")
        for f in d.get("features", []):
            p, c = f.get("properties", {}), f.get("geometry", {}).get("coordinates", [None, None])
            if c[0] is None:
                continue
            out.append({"mag": p.get("mag"), "lugar": p.get("flynn_region") or "", "fecha": p.get("time"),
                        "lat": c[1], "lon": c[0], "fuente": "EMSC"})
    except Exception:
        pass
    # dedup (mismo sismo en ambas redes) + orden por fecha desc
    vistos, dedup = set(), []
    for s in sorted(out, key=lambda x: _ts(x["fecha"]), reverse=True):
        k = (round(_ts(s["fecha"]) / 120), round(s["lat"] or 0, 1), round(s["lon"] or 0, 1))
        if k in vistos:
            continue
        vistos.add(k)
        dedup.append(s)
    return dedup[:30]


def obtener_sismos():
    """Devuelve los sismos combinados, con caché de 5 min para no consultar en cada build."""
    try:
        if SISMOS_CACHE.exists() and (time.time() - SISMOS_CACHE.stat().st_mtime) < 300:
            return json.loads(SISMOS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    datos = _sismos_red()
    try:
        if datos:
            SISMOS_CACHE.write_text(json.dumps(datos), encoding="utf-8")
    except Exception:
        pass
    return datos


def _prerender(resumen, directorio):
    """Bloque de TEXTO REAL dentro de #contenido para que Google indexe contenido sin
    ejecutar JS (el sitio es SPA). El JS lo reemplaza al cargar (pintarTabs/render, al final
    del <script>), así que el usuario ve siempre la app; esto es solo capa SEO/accesibilidad.
    Solo info pública NO personal (cifras agregadas + nombres de centros de la red de
    colaboradores), nunca nombres de personas heridas/desaparecidas."""
    import html as _h
    esc = lambda s: _h.escape(str(s))
    r = resumen
    p = ['<section class="prerender">',
         '<h2>Registro de centros de acopio, refugios y hospitales en Venezuela</h2>',
         '<p>Red de Acopio y Refugio es un registro unificado y en tiempo real de centros de acopio, '
         'refugios y hospitales en Venezuela (terremoto de La Guaira, junio 2026): su ubicación, '
         'capacidad (aforo), insumos disponibles y los requerimientos que necesitan, para coordinar la '
         'ayuda y los insumos entre centros donde de verdad hacen falta. Cada punto lo registra y '
         'mantiene su propio responsable a través del bot de Telegram. De forma complementaria organiza '
         'la información del canal de emergencias: pedidos de rescate, personas heridas, personas '
         f'desaparecidas y servicios por zona. Última actualización: {esc(r.get("actualizado",""))}.</p>',
         '<h3>Situación actual</h3><ul>']
    for lbl, val in [
        ("Centros registrados en la red", len(directorio)),
        ("Ofrecimientos de ayuda disponibles", r.get("ofrecimientos", 0)),
        ("Pedidos y reportes de rescate", r.get("rescate", 0)),
        ("Reportes de personas heridas", r.get("heridos", 0)),
        ("Personas desaparecidas en búsqueda", r.get("desaparecidos_buscando", 0)),
        ("Personas localizadas", r.get("desaparecidos_resueltos", 0)),
    ]:
        p.append(f'<li>{esc(lbl)}: {esc(val)}</li>')
    p.append('</ul>')
    if directorio:
        _tipo = {"acopio": "centro de acopio", "hospital": "hospital", "clinica": "clínica",
                 "refugio": "refugio", "otro": "punto"}
        p.append('<h3>Centros de la red</h3><ul>')
        for c in directorio[:80]:
            nom = esc(c.get("punto") or "")
            if not nom:
                continue
            ub = esc(c.get("ubicacion") or "")
            tp = _tipo.get(c.get("tipo_punto"), "punto")
            p.append(f'<li>{nom} ({esc(tp)}){(" — " + ub) if ub else ""}</li>')
        p.append('</ul>')
    p.append('<h3>Secciones</h3><p>Directorio de la red de centros de acopio, refugios y hospitales, '
             'requerimientos por centro, insumos disponibles para coordinar entre centros, panel de '
             'situación, personas heridas, pedidos de rescate, personas desaparecidas, '
             'donaciones, ofrecimientos de ayuda, servicios por zona (luz, agua, gasolina, '
             'internet), buscar persona, emergencias y sismos.</p>'
             '</section>')
    return "".join(p)


def _pedidos_web(horas=336):
    """Pedidos de ayuda llegados por la WEB y ya PUBLICADOS por un moderador (estado 'web').
    Solo zona + mensaje anonimizado + fecha. NUNCA el contacto de quien pide (ese vive en la
    tabla privada solicitudes_web, que la web jamás lee)."""
    desde = "-{} hours".format(int(horas))
    con = db.conectar()
    try:
        db._asegurar_columna(con, "origen", "TEXT", "llamados")
        rows = con.execute(
            "SELECT id, sector, mensaje, estado, creado_en FROM llamados "
            "WHERE estado IN ('web','atencion') AND creado_en >= datetime('now', ?) "
            "AND (origen = 'pedido' OR (origen IS NULL AND voluntario_id IS NULL)) "
            "ORDER BY creado_en DESC", (desde,)).fetchall()
    except Exception:
        rows = []
    pedidos = [dict(r) for r in rows]
    if pedidos:
        # Cuántas personas se registraron para atender cada pedido (tabla atenciones).
        try:
            cur = con.execute("SELECT llamado_id, COUNT(*) AS n FROM atenciones GROUP BY llamado_id")
            cuenta = {r["llamado_id"]: r["n"] for r in cur.fetchall()}
        except Exception:
            cuenta = {}
        for p in pedidos:
            p["n_atienden"] = cuenta.get(p["id"], 0)
    con.close()
    _fotos_pedidos(pedidos)
    return pedidos


def _fotos_pedidos(pedidos):
    """Imagen del pedido (flyer de desaparecido, etc.). El backend la guarda en la
    carpeta PRIVADA pedidos_img/ (no servida por nginx); aquí, SOLO para los pedidos
    ya publicados, se copia a web/img/pedido_<id>.jpg y se marca p['foto']. Las copias
    públicas de pedidos que ya no están publicados (resueltos, despublicados, viejos)
    se borran en cada corrida. A prueba de fallos: sin foto, el pedido sale igual."""
    import shutil
    try:
        priv = BASE / "pedidos_img"
        img_dir = WEB_DIR / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        vivos = set()
        for p in pedidos:
            src = priv / ("%s.jpg" % p["id"])
            if src.exists():
                nombre = "pedido_%s.jpg" % p["id"]
                dst = img_dir / nombre
                if not dst.exists():
                    shutil.copyfile(src, dst)
                p["foto"] = "img/" + nombre
                vivos.add(nombre)
        for f in img_dir.glob("pedido_*.jpg"):
            if f.name not in vivos:
                f.unlink()
    except Exception as e:
        print("fotos de pedidos error (la web sale igual):", e)


def _overlay_stock(oferta):
    """Inventario numérico VIVO (tabla `stock`, Fase A.2): si un acopio tiene stock
    — lo mantienen los bots con Sumar/Reemplazar y luego lo descontarán los pedidos —
    sus insumos en la web salen de ahí (cantidades reales), no del histórico de
    capturas. Los puntos sin stock siguen con el resumen clásico. Aditivo y a prueba
    de fallos: si algo revienta, la web sale igual con los datos clásicos."""
    try:
        import stock as _stock
        con = db.conectar()
        pids = [r[0] for r in con.execute(
            "SELECT DISTINCT punto_id FROM stock WHERE estado = 'disponible'").fetchall()]
        con_stock = {}
        for pid in pids:
            items, ultimo = [], ""
            for r in _stock.de_punto(pid):
                if r["cantidad"] is not None:
                    disp = r["cantidad"] - (r.get("reservado") or 0)  # lo reservado por envíos no se ofrece
                    if disp <= 0:
                        continue
                    cant = ("%g" % disp) + ((" " + r["unidad"]) if r.get("unidad") else "")
                else:
                    cant = None
                items.append({"categoria": r.get("categoria"), "item": r.get("item"),
                              "cantidad": cant, "detalle": r.get("detalle")})
                ultimo = max(ultimo, r.get("actualizado_en") or "")
            if items:
                con_stock[pid] = (items, ultimo.replace(" ", "T") + "Z" if ultimo else "")
        # Reemplaza los insumos de los puntos que ya salen en oferta…
        vistos = set()
        for g in oferta:
            if g.get("id") in con_stock:
                items, ultimo = con_stock[g["id"]]
                g["items"] = items
                if ultimo:
                    g["actualizado"] = ultimo
                vistos.add(g["id"])
        # …y suma los acopios/fundaciones con stock que quedaron fuera (capturas viejas > ventana).
        for pid in set(con_stock) - vistos:
            r = con.execute("SELECT punto, tipo_punto FROM representantes "
                            "WHERE id = ? AND estado = 'aprobado'", (pid,)).fetchone()
            if r and r[0] and r[1] in ("acopio", "fundacion"):
                items, ultimo = con_stock[pid]
                oferta.append({"id": pid, "punto": r[0], "tipo_punto": r[1], "items": items,
                               "personas": 0, "actualizado": ultimo})
        con.close()
    except Exception as e:
        print("overlay stock error (la web sale con datos clásicos):", e)
    return oferta


# ---------------------------------------------------------------------------
# Páginas por punto (web/p/pt-<id>.html): enlace compartible por WhatsApp/redes
# con vista previa propia (Open Graph del punto, no la genérica del sitio) +
# requerimientos, inventario y datos del centro. Las regenera construir() en cada
# corrida (timer 5 min). Si un punto sale de la red, su página pasa a ser una
# redirección a la home → los enlaces ya compartidos nunca dan 404 ni muestran
# información vieja. Mismos datos públicos que la web (nunca datos personales).
# ---------------------------------------------------------------------------
_BOT_USER = "angelesapoyove_bot"
PUNTO_ICO_PY = {"acopio": "📦", "refugio": "🏠", "hospital": "🏥", "cdi": "🩺",
                "maternidad": "🤰", "clinica": "➕", "fundacion": "🏛", "otro": "📍"}
PUNTO_NOM_PY = {"acopio": "Centro de acopio", "refugio": "Refugio", "hospital": "Hospital",
                "cdi": "CDI", "maternidad": "Maternidad", "clinica": "Clínica",
                "fundacion": "Fundación", "otro": "Punto"}
DIR_CAMPOS_PY = [
    ("tipo_inmueble", "🏚️ Inmueble"), ("aforo", "👥 Aforo"), ("banos", "🚻 Baños"),
    ("duchas", "🚿 Duchas"), ("cocina", "🍳 Cocina"), ("agua_potable", "🚰 Agua potable"),
    ("electricidad", "⚡ Electricidad"), ("planta", "🔋 Planta/generador"),
    ("camas", "🛏️ Camas disp."), ("acceso_vehiculos", "🚛 Acceso vehículos"),
    ("accesibilidad", "♿ Accesibilidad"), ("almacenamiento", "📦 Almacenamiento"),
    ("refrigeracion", "❄️ Refrigeración"), ("cobertura", "🗺️ Cobertura"),
    ("areas", "🎯 Atiende"), ("horario", "🕐 Horario")]

_PAGINA_REDIR = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="2;url=https://redacopioyrefugio.com/">
<title>Red de Acopio y Refugio</title></head>
<body style="font-family:Poppins,system-ui,sans-serif;text-align:center;padding:48px 16px;color:#0f1f33;background:#eef1f5">
<p>Este punto ya no tiene información activa en la red.</p>
<p><a href="https://redacopioyrefugio.com/" style="color:#1E9C94;font-weight:600">Ir a Red de Acopio y Refugio →</a></p>
</body></html>"""


def _pid_punto(punto_id, punto):
    """Mismo identificador que pidDe() en el JS: pt-<punto_id>, o slug del nombre si falta."""
    import re as _re
    import unicodedata as _ud
    if punto_id is not None:
        return "pt-%s" % punto_id
    s = _ud.normalize("NFD", (punto or "").lower())
    s = "".join(ch for ch in s if not _ud.combining(ch))
    return "ptn-" + _re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _norm_item(s):
    import unicodedata as _ud
    s = _ud.normalize("NFD", (s or "").strip().lower())
    return "".join(ch for ch in s if not _ud.combining(ch))


def _tel_de(t):
    import re as _re
    m = _re.search(r"\+?\d[\d\s().\-]{6,}\d", str(t or ""))
    return _re.sub(r"[^\d+]", "", m.group(0)) if m else None


def _maps_url(f):
    from urllib.parse import quote
    lat, lon = f.get("lat"), f.get("lon")
    try:
        if lat not in (None, "") and lon not in (None, ""):
            return "https://www.google.com/maps/dir/?api=1&destination=%s,%s" % (float(lat), float(lon))
    except (TypeError, ValueError):
        pass
    if f.get("ubicacion"):
        return "https://www.google.com/maps/search/?api=1&query=" + quote(f["ubicacion"] + " Venezuela")
    return None


def _fuente_og(tam, bold=True):
    """Poppins de /opt/angeles/fonts (marca); cae a DejaVu del sistema si falta."""
    from PIL import ImageFont
    rutas = [str(BASE / "fonts" / ("Poppins-SemiBold.ttf" if bold else "Poppins-Regular.ttf")),
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
             else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for r in rutas:
        try:
            return ImageFont.truetype(r, tam)
        except OSError:
            continue
    return ImageFont.load_default()


def _ajustar_og(dr, texto, fuente, ancho):
    """Parte `texto` en líneas que caben en `ancho` px."""
    lineas, actual = [], ""
    for w in (texto or "").split():
        prueba = (actual + " " + w).strip()
        if dr.textlength(prueba, font=fuente) <= ancho:
            actual = prueba
        else:
            if actual:
                lineas.append(actual)
            actual = w
    if actual:
        lineas.append(actual)
    return lineas


def _og_punto(d, pdir):
    """Imagen 1200x630 propia del punto para la vista previa (og:image) en WhatsApp/redes.
    El nombre lleva un hash del contenido → si cambian los datos cambia la URL de la
    imagen (rompe el caché de vista previa). Devuelve el nombre dentro de p/og/,
    o None si no se pudo generar (la página cae al og.png genérico)."""
    seg, nec, of = d["seg"], d["nec"], d["of"]
    ntot = len(seg) + len(nec)
    if ntot:
        sub = "%d requerimiento%s activo%s" % (ntot, "" if ntot == 1 else "s", "" if ntot == 1 else "s")
    elif of:
        sub = "%d insumo%s disponible%s" % (len(of), "" if len(of) == 1 else "s", "" if len(of) == 1 else "s")
    else:
        sub = PUNTO_NOM_PY.get(d["tipo_punto"]) or "Punto de la red"
    if d["personas"]:
        sub += " · %s personas" % d["personas"]
    tops = [i.get("item") for i in (seg + nec) if i.get("item")][:3]
    if not tops:
        tops = [i.get("item") for i in of if i.get("item")][:3]
    resto = max(0, (ntot or len(of)) - len(tops))

    firma = hashlib.md5(json.dumps([d["punto"], sub, tops, resto],
                                   ensure_ascii=False).encode()).hexdigest()[:8]
    nombre = "%s-%s.png" % (d["pid"], firma)
    ogdir = pdir / "og"
    ogdir.mkdir(exist_ok=True)
    ruta = ogdir / nombre
    if not ruta.exists():
        from PIL import Image, ImageDraw
        NAVY, TEAL = (0, 60, 103), (30, 156, 148)
        BLANCO, CLARO, TENUE = (255, 255, 255), (203, 216, 230), (150, 175, 198)
        im = Image.new("RGB", (1200, 630), NAVY)
        dr = ImageDraw.Draw(im)
        dr.rectangle([0, 0, 16, 630], fill=TEAL)
        x = 72
        try:  # el icono trae el mismo navy de fondo → se funde solo
            logo = Image.open(WEB_DIR / "icon-512.png").convert("RGB").resize((92, 92))
            im.paste(logo, (56, 44))
            x_marca = 152
        except Exception:
            x_marca = x
        dr.text((x_marca, 72), "Red de Acopio y Refugio", font=_fuente_og(32), fill=BLANCO)
        # Nombre del punto: reduce la letra hasta caber en 3 líneas
        ancho = 1200 - x - 72
        for tam in (60, 50, 42):
            f_tit = _fuente_og(tam)
            lineas = _ajustar_og(dr, d["punto"], f_tit, ancho)
            if len(lineas) <= 3:
                break
        if len(lineas) > 3:
            lineas = lineas[:3]
            lineas[-1] += "…"
        y = 190
        for ln in lineas:
            dr.text((x, y), ln, font=f_tit, fill=BLANCO)
            y += int(tam * 1.28)
        # Chip con el resumen (requerimientos / insumos / personas)
        y += 14
        f_sub = _fuente_og(30)
        w_sub = dr.textlength(sub, font=f_sub)
        dr.rounded_rectangle([x, y, x + w_sub + 44, y + 56], radius=28, fill=TEAL)
        dr.text((x + 22, y + 9), sub, font=f_sub, fill=BLANCO)
        y += 82
        # Top de necesidades (o insumos)
        f_it = _fuente_og(31, bold=False)
        for t in tops:
            ln = _ajustar_og(dr, "•  " + t, f_it, ancho)
            if not ln:
                continue
            txt = ln[0] + ("…" if len(ln) > 1 else "")
            if y > 520:
                break
            dr.text((x, y), txt, font=f_it, fill=CLARO)
            y += 48
        if resto > 0 and y <= 520:
            dr.text((x, y), "y %d más" % resto, font=_fuente_og(29), fill=CLARO)
        dr.text((x, 566), "redacopioyrefugio.com", font=_fuente_og(26, bold=False), fill=TENUE)
        im.save(ruta, "PNG", optimize=True)
    # limpia versiones viejas del mismo punto (el hash cambió)
    for f in ogdir.glob(d["pid"] + "-*.png"):
        if f.name != nombre:
            f.unlink()
    return nombre


def _pagina_punto(d, ahora):
    import html as _h
    esc = lambda s: _h.escape(str(s if s is not None else ""))
    ico = PUNTO_ICO_PY.get(d["tipo_punto"]) or "📍"
    nom = PUNTO_NOM_PY.get(d["tipo_punto"]) or "Punto"
    url = SITE_URL_BASE + "p/" + d["pid"] + ".html"
    og = (SITE_URL_BASE + "p/og/" + d["og"]) if d.get("og") else OG_IMAGEN
    f = d.get("ficha") or {}
    seg, nec, of = d["seg"], d["nec"], d["of"]
    ntot = len(seg) + len(nec)

    # Título + descripción de la vista previa (lo que se ve al pegar el link en WhatsApp)
    if ntot:
        titulo = "%s %s — %d requerimiento%s activo%s" % (
            ico, d["punto"], ntot, "" if ntot == 1 else "s", "" if ntot == 1 else "s")
    elif of:
        titulo = "%s %s — %d insumo%s disponible%s" % (
            ico, d["punto"], len(of), "" if len(of) == 1 else "s", "" if len(of) == 1 else "s")
    else:
        titulo = "%s %s — %s" % (ico, d["punto"], nom)
    tops = [i.get("item") for i in (seg + nec) if i.get("item")][:6]
    partes = []
    if d["personas"]:
        partes.append("👥 %s personas en este momento" % d["personas"])
    if tops:
        mas = " y %d más" % (ntot - len(tops)) if ntot > len(tops) else ""
        partes.append("Necesita: " + ", ".join(tops) + mas)
    elif of:
        oftops = [i.get("item") for i in of if i.get("item")][:6]
        omas = " y %d más" % (len(of) - len(oftops)) if len(of) > len(oftops) else ""
        partes.append("Tiene disponible: " + ", ".join(oftops) + omas)
    if f.get("ubicacion"):
        ub = f["ubicacion"].strip()
        partes.append("📍 " + (ub[:90].rstrip() + "…" if len(ub) > 90 else ub))
    desc = " · ".join(partes) or (nom + " registrado en la red")
    if len(desc) > 230:
        desc = desc[:229].rstrip() + "…"
    desc += " · Mira cómo ayudar."

    # Texto del botón compartir de la propia página (mismo estilo que la web)
    sh = "%s %s" % (ico, d["punto"])
    if d["personas"]:
        sh += " — %s personas" % d["personas"]
    if tops:
        sh += " necesita: " + ", ".join(tops) + ("…" if ntot > len(tops) else ".")

    # --- Secciones del cuerpo ---
    cuerpo = []
    if d["personas"]:
        cuerpo.append('<div class="personas">👥 %s personas en este momento</div>' % esc(d["personas"]))
    if seg or nec:
        cuerpo.append('<section><h2>📥 Qué necesita ahora</h2>')
        for i in seg:
            urg = (i.get("urgencia") or "media").lower()
            chip = ('<span class="urg crit">⚠ Crítico</span>' if urg.startswith("crit")
                    else '<span class="urg alta">🔥 Alta</span>' if urg == "alta" else "")
            badge = {"disponible": '<span class="badge disp">✅ Disponible cerca</span>',
                     "coordinando": '<span class="badge coord">🤝 Coordinando</span>'}.get(i.get("estado"), "")
            meta = int(i.get("meta") or 0)
            rec = int(i.get("recibido") or 0)
            enc = int(i.get("en_camino") or 0)
            cubierto = False
            if meta:
                faltan = max(0, meta - rec - enc)
                cubierto = faltan <= 0
                u = (" " + esc(i["unidad"])) if i.get("unidad") else ""
                prog = ('<div class="prog ok">✓ Cubierto</div>' if cubierto else
                        '<div class="prog">Faltan <b>%d</b> / meta %d%s · %d recibidos · %d en camino</div>'
                        % (faltan, meta, u, rec, enc))
            else:
                prog = '<div class="prog">Cantidad por confirmar</div>'
            btn = ('' if cubierto or i.get("id") is None else
                   '<a class="btn llevar" href="https://t.me/%s?start=nec_%s">🤝 Yo puedo llevar esto</a>'
                   % (_BOT_USER, i["id"]))
            cuerpo.append('<div class="item"><div class="item-t">• <b>%s</b> %s%s</div>%s%s</div>'
                          % (esc(i.get("item")), chip, badge, prog, btn))
        for i in nec:
            cant = (" — " + esc(i["cantidad"])) if i.get("cantidad") else ""
            det = ('<div class="det">%s</div>' % esc(i["detalle"])) if i.get("detalle") else ""
            cuerpo.append('<div class="item"><div class="item-t">• <b>%s</b>%s</div>%s</div>'
                          % (esc(i.get("item")), cant, det))
        cuerpo.append('</section>')
    if of:
        TOPE_OF = 15
        cuerpo.append('<section><h2>🎁 Tiene disponible para dar</h2>')
        for i in of[:TOPE_OF]:
            cant = (" — " + esc(i["cantidad"])) if i.get("cantidad") else ""
            cuerpo.append('<div class="item"><div class="item-t">• <b>%s</b>%s</div></div>'
                          % (esc(i.get("item")), cant))
        if len(of) > TOPE_OF:
            cuerpo.append('<p class="mas"><a href="%s#oferta:%s">Ver los %d insumos en la web →</a></p>'
                          % (SITE_URL_BASE, d["pid"], len(of)))
        cuerpo.append('</section>')
    ent = d.get("entregas")
    if ent and ent.get("n"):
        n = int(ent["n"])
        cuando = (ent.get("ultima") or "")[:10]
        cuerpo.append('<div class="personas">✅ %d entrega%s realizada%s a la red%s</div>'
                      % (n, "" if n == 1 else "s", "" if n == 1 else "s",
                         (" · última: " + esc(cuando)) if cuando else ""))
    filas = "".join(
        '<div class="dir-f"><div class="dir-k">%s</div><div class="dir-v">%s</div></div>'
        % (lbl, esc(f[k])) for k, lbl in DIR_CAMPOS_PY if (f.get(k) or "").strip())
    if f.get("ubicacion"):
        filas += ('<div class="dir-f wide"><div class="dir-k">📍 Ubicación</div><div class="dir-v">%s</div></div>'
                  % esc(f["ubicacion"]))
    if f.get("responsable"):
        filas += ('<div class="dir-f wide"><div class="dir-k">🧑‍💼 Responsable</div><div class="dir-v">%s</div></div>'
                  % esc(f["responsable"]))
    if filas:
        cuerpo.append('<section><h2>ℹ️ Datos del punto</h2><div class="dir-grid">%s</div></section>' % filas)

    # Acciones
    acc = ['<button class="btn share" id="share">📤 Compartir</button>',
           '<button class="btn" id="copiar">🔗 Copiar enlace</button>']
    mapa = _maps_url(f)
    if mapa:
        acc.append('<a class="btn" target="_blank" rel="noopener" href="%s">🗺️ Cómo llegar</a>' % esc(mapa))
    tel = _tel_de(f.get("contacto"))
    if tel:
        acc.append('<a class="btn" href="tel:%s">📞 Llamar</a>' % esc(tel))
    tab_red = "necesidades" if (seg or nec) else ("oferta" if of else "directorio")
    acc.append('<a class="btn red" href="%s#%s:%s">🌐 Ver toda la red</a>' % (SITE_URL_BASE, tab_red, d["pid"]))

    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#003C67">
<link rel="icon" type="image/svg+xml" href="/logo.svg?v=ofc">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap">
<title>%(titulo)s · Red de Acopio y Refugio</title>
<meta name="description" content="%(desc)s">
<link rel="canonical" href="%(url)s">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Red de Acopio y Refugio">
<meta property="og:title" content="%(titulo)s">
<meta property="og:description" content="%(desc)s">
<meta property="og:url" content="%(url)s">
<meta property="og:image" content="%(og)s">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="es_VE">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="%(titulo)s">
<meta name="twitter:description" content="%(desc)s">
<meta name="twitter:image" content="%(og)s">
<style>
  :root{--navy:#003C67;--teal:#1E9C94;--wa:#25d366;--tinta:#0f1f33;--suave:#5b6b80;--linea:#e2e8f0}
  *{box-sizing:border-box}
  body{margin:0;background:#eef1f5;color:var(--tinta);font-family:Poppins,system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.5}
  .top{position:relative;overflow:hidden;color:#fff;padding:12px 16px;display:flex;align-items:center;gap:10px;
       background:linear-gradient(118deg,#022B4D,#003C67,#005B67,#15857E)}
  .top::before{content:"";position:absolute;inset:0;pointer-events:none;
       background:radial-gradient(360px 150px at top right,rgba(255,255,255,.14),transparent)}
  .top img{width:28px;height:28px;background:#fff;border-radius:7px;padding:3px;position:relative;
       box-shadow:0 1px 6px rgba(2,20,38,.25)}
  .top a{color:#fff;text-decoration:none;font-weight:600;font-size:14px;position:relative}
  .top .ir{margin-left:auto;font-size:12.5px;font-weight:600;color:rgba(255,255,255,.88);white-space:nowrap}
  .wrap{max-width:640px;margin:0 auto;padding:16px}
  .card{background:#fff;border:1px solid var(--linea);border-radius:14px;padding:18px;box-shadow:0 1px 3px rgba(15,31,51,.06)}
  h1{font-size:21px;margin:0 0 2px;color:var(--navy);line-height:1.3}
  .tipo{color:var(--suave);font-size:13px;margin-bottom:10px}
  h2{font-size:15px;color:var(--navy);margin:18px 0 8px;border-top:1px solid var(--linea);padding-top:14px}
  .personas{font-weight:700;color:var(--navy);margin:6px 0 2px;font-size:15px}
  .item{padding:7px 0;border-bottom:1px dashed var(--linea)}
  .item:last-of-type{border-bottom:0}
  .item-t{font-size:14.5px}
  .det{color:var(--suave);font-size:13px;margin-top:2px}
  .prog{color:var(--suave);font-size:13px;margin-top:3px}
  .prog.ok{color:#15803d;font-weight:600}
  .urg{font-size:11.5px;font-weight:700;border-radius:99px;padding:2px 8px;margin-left:6px;vertical-align:1px}
  .urg.crit{background:#fee2e2;color:#b91c1c}.urg.alta{background:#ffedd5;color:#c2410c}
  .badge{font-size:11.5px;font-weight:700;border-radius:99px;padding:2px 8px;margin-left:6px;vertical-align:1px}
  .badge.disp{background:#dcfce7;color:#15803d}.badge.coord{background:#e0f2fe;color:#0369a1}
  .dir-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .dir-f{background:#f6f8fb;border-radius:9px;padding:7px 10px}
  .dir-f.wide{grid-column:1/-1}
  .dir-k{font-size:11.5px;color:var(--suave)}
  .dir-v{font-size:13.5px;font-weight:600}
  .mas{font-size:13.5px}.mas a{color:var(--teal);font-weight:600}
  .acciones{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
  .btn{flex:1;min-width:130px;text-align:center;border:1.5px solid var(--linea);background:#fff;border-radius:10px;
       padding:11px 12px;font:inherit;font-size:14px;font-weight:600;color:var(--tinta);text-decoration:none;cursor:pointer}
  .btn.share{background:var(--wa);border-color:var(--wa);color:#fff}
  .btn.llevar{display:inline-block;flex:none;min-width:0;margin-top:6px;padding:7px 12px;font-size:13px;
              background:var(--teal);border-color:var(--teal);color:#fff}
  .btn.red{background:var(--navy);border-color:var(--navy);color:#fff}
  .pie{color:var(--suave);font-size:12px;text-align:center;margin:16px 4px;line-height:1.6}
  .pie a{color:var(--teal)}
</style>
</head>
<body>
<div class="top"><img src="/logo.svg?v=ofc" alt=""><a href="/">Red de Acopio y Refugio</a><a class="ir" href="/">Ver la red →</a></div>
<div class="wrap">
  <div class="card">
    <h1>%(ico)s %(punto)s</h1>
    <div class="tipo">%(nom)s · registrado en la red</div>
    %(cuerpo)s
    <div class="acciones">%(acciones)s</div>
  </div>
  <div class="pie">Actualizado: %(ahora)s · Esta página se actualiza sola.<br>
  La Red solo conecta; la información la reporta cada punto y <b>no está verificada</b>. ·
  <a href="/aviso-legal.html">Aviso legal</a></div>
</div>
<script>
var t=%(sh_js)s,u=%(url_js)s;
document.getElementById("share").onclick=function(){
  if(navigator.share){navigator.share({text:t,url:u}).catch(function(){});}
  else{window.open("https://wa.me/?text="+encodeURIComponent(t+" "+u),"_blank");}
};
document.getElementById("copiar").onclick=function(){
  var b=this;
  var p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(u):Promise.reject();
  p.then(function(){var x=b.textContent;b.textContent="✅ Enlace copiado";setTimeout(function(){b.textContent=x;},1600);})
   .catch(function(){prompt("Copia el enlace:",u);});
};
</script>
</body>
</html>""" % {
        "titulo": esc(titulo), "desc": esc(desc), "url": esc(url), "og": esc(og),
        "ico": ico, "punto": esc(d["punto"]), "nom": esc(nom),
        "cuerpo": "".join(cuerpo), "acciones": "".join(acc), "ahora": esc(ahora),
        "sh_js": _js(sh), "url_js": _js(url),
    }


def _paginas_puntos(necesidades, seguimiento, oferta, directorio, ahora):
    """Escribe web/p/<pid>.html para cada punto con datos públicos y convierte en
    redirección las páginas de puntos que ya no están. Devuelve los nombres de
    archivo escritos (para el sitemap)."""
    puntos = {}

    def g(punto_id, punto, tipo):
        pid = _pid_punto(punto_id, punto)
        d = puntos.setdefault(pid, {"pid": pid, "punto": (punto or "").strip(), "tipo_punto": None,
                                    "personas": 0, "nec": [], "seg": [], "of": [], "ficha": None})
        if tipo and not d["tipo_punto"]:
            d["tipo_punto"] = tipo
        if not d["punto"] and punto:
            d["punto"] = punto.strip()
        if punto_id is not None and d.get("punto_id") is None:
            d["punto_id"] = punto_id
        return d

    for p in seguimiento:
        g(p.get("punto_id"), p.get("punto"), p.get("tipo_punto"))["seg"] = p.get("items") or []
    for p in necesidades:
        d = g(p.get("id"), p.get("punto"), p.get("tipo_punto"))
        segkeys = {_norm_item(i.get("item")) for i in d["seg"]}
        d["nec"] = [i for i in (p.get("items") or []) if _norm_item(i.get("item")) not in segkeys]
        d["personas"] = p.get("personas") or 0
    for p in oferta:
        g(p.get("id"), p.get("punto"), p.get("tipo_punto"))["of"] = p.get("items") or []
    for p in directorio:
        g(p.get("punto_id"), p.get("punto"), p.get("tipo_punto"))["ficha"] = p

    # Contador de entregas completadas (envíos con QR cerrados) por punto de origen.
    try:
        entregas = db.entregas_por_punto()
    except Exception as e:  # noqa: BLE001 — la página sale igual, sin el contador
        print("entregas web error:", e)
        entregas = {}
    for d in puntos.values():
        d["entregas"] = entregas.get(d.get("punto_id"))

    pdir = WEB_DIR / "p"
    pdir.mkdir(parents=True, exist_ok=True)
    escritos = set()
    for pid, d in puntos.items():
        if not d["punto"]:
            continue
        try:
            d["og"] = _og_punto(d, pdir)
        except Exception as e:  # sin imagen propia la página usa el og.png genérico
            print("og punto error:", pid, e)
            d["og"] = None
        (pdir / (pid + ".html")).write_text(_pagina_punto(d, ahora), encoding="utf-8")
        escritos.add(pid + ".html")
    for viejo in pdir.glob("*.html"):
        if viejo.name not in escritos:
            viejo.write_text(_PAGINA_REDIR, encoding="utf-8")
    pids_vivos = {n[:-5] for n in escritos}  # sin ".html"
    for img in (pdir / "og").glob("*.png") if (pdir / "og").is_dir() else []:
        if img.name.rsplit("-", 1)[0] not in pids_vivos:
            img.unlink()
    return sorted(escritos)


_PAGINA_REDIR_PEDIDO = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="2;url=https://redacopioyrefugio.com/#pedir">
<title>Red de Acopio y Refugio</title></head>
<body style="font-family:Poppins,system-ui,sans-serif;text-align:center;padding:48px 16px;color:#0f1f33;background:#eef1f5">
<p>Este pedido de ayuda ya fue atendido o dejó de estar publicado. 🙏</p>
<p><a href="https://redacopioyrefugio.com/#pedir" style="color:#1E9C94;font-weight:600">Ver los pedidos activos →</a></p>
</body></html>"""


def _og_solicitud(pid, chip_txt, color, sector, mensaje, sdir):
    """Imagen 1200x630 propia de una solicitud (pedido de ayuda o llamado de apoyo) para
    la vista previa (og:image) en WhatsApp/redes. Mismo esquema que _og_punto: hash del
    contenido en el nombre → si cambia el contenido cambia la URL (rompe caché de vista
    previa); devuelve el nombre dentro de s/og/."""
    import re as _re
    # Poppins no dibuja emojis (saldría un cuadrito) → se quitan SOLO en la imagen
    _sin_emoji = lambda s: _re.sub(
        "[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿️‍]", "", s or "")
    sector = " ".join(_sin_emoji(sector).split()) or "Sector no indicado"
    mensaje = " ".join(_sin_emoji(mensaje or "").split())
    firma = hashlib.md5(json.dumps([chip_txt, sector, mensaje],
                                   ensure_ascii=False).encode()).hexdigest()[:8]
    nombre = "%s-%s.png" % (pid, firma)
    ogdir = sdir / "og"
    ogdir.mkdir(exist_ok=True)
    ruta = ogdir / nombre
    if not ruta.exists():
        from PIL import Image, ImageDraw
        NAVY = (0, 60, 103)
        BLANCO, CLARO, TENUE = (255, 255, 255), (203, 216, 230), (150, 175, 198)
        im = Image.new("RGB", (1200, 630), NAVY)
        dr = ImageDraw.Draw(im)
        dr.rectangle([0, 0, 16, 630], fill=color)
        x = 72
        try:  # el icono trae el mismo navy de fondo → se funde solo
            logo = Image.open(WEB_DIR / "icon-512.png").convert("RGB").resize((92, 92))
            im.paste(logo, (56, 44))
            x_marca = 152
        except Exception:
            x_marca = x
        dr.text((x_marca, 72), "Red de Acopio y Refugio", font=_fuente_og(32), fill=BLANCO)
        ancho = 1200 - x - 72
        # Chip de color con el tipo de solicitud
        y = 172
        f_chip = _fuente_og(30)
        w_chip = dr.textlength(chip_txt, font=f_chip)
        dr.rounded_rectangle([x, y, x + w_chip + 44, y + 56], radius=28, fill=color)
        dr.text((x + 22, y + 9), chip_txt, font=f_chip, fill=BLANCO)
        y += 82
        # Sector como título: reduce la letra hasta caber en 2 líneas
        for tam in (56, 46, 40):
            f_tit = _fuente_og(tam)
            lineas = _ajustar_og(dr, sector, f_tit, ancho)
            if len(lineas) <= 2:
                break
        if len(lineas) > 2:
            lineas = lineas[:2]
            lineas[-1] += "…"
        for ln in lineas:
            dr.text((x, y), ln, font=f_tit, fill=BLANCO)
            y += int(tam * 1.26)
        y += 10
        # Mensaje del pedido (hasta 4 líneas)
        f_msj = _fuente_og(31, bold=False)
        ln_msj = _ajustar_og(dr, mensaje, f_msj, ancho)
        corte = False
        for i, ln in enumerate(ln_msj):
            if y > 500 or i >= 4:
                corte = True
                break
            dr.text((x, y), ln, font=f_msj, fill=CLARO)
            y += 46
        if corte and y <= 520:
            dr.text((x, y), "…", font=f_msj, fill=CLARO)
        dr.text((x, 566), "redacopioyrefugio.com", font=_fuente_og(26, bold=False), fill=TENUE)
        im.save(ruta, "PNG", optimize=True)
    # limpia versiones viejas de la misma solicitud (el hash cambió)
    for f in ogdir.glob(pid + "-*.png"):
        if f.name != nombre:
            f.unlink()
    return nombre


def _og_pedido(p, sdir):
    return _og_solicitud("pd-%s" % p["id"], "Pedido de ayuda", (179, 38, 30),
                         p.get("sector") or "Zona no indicada", p.get("mensaje"), sdir)


def _og_llamado(l, sdir):
    return _og_solicitud("ll-%s" % l["id"], "Llamado de apoyo", (30, 156, 148),
                         l.get("sector") or "Sector en emergencia", l.get("mensaje"), sdir)


def _pagina_pedido(p, ahora):
    import html as _h
    import re as _re
    esc = lambda s: _h.escape(str(s if s is not None else ""))
    pid = "pd-%s" % p["id"]
    url = SITE_URL_BASE + "s/" + pid + ".html"
    og = (SITE_URL_BASE + "s/og/" + p["og"]) if p.get("og") else OG_IMAGEN
    sector = (p.get("sector") or "Zona no indicada").strip()
    mensaje = (p.get("mensaje") or "").strip()
    plano = " ".join(mensaje.split())
    n = p.get("n_atienden") or 0

    titulo = "🆘 Pedido de ayuda — %s" % sector
    desc = plano[:229].rstrip() + "…" if len(plano) > 230 else plano
    desc = (desc + " · Mira cómo ayudar.") if desc else "Pedido de ayuda publicado en la red. Mira cómo ayudar."
    sh = "🆘 Pedido de ayuda — %s: %s" % (sector, (plano[:139].rstrip() + "…" if len(plano) > 140 else plano))

    foto = ""
    if p.get("foto"):
        src = p["foto"] if str(p["foto"]).startswith(("http", "/")) else "/" + str(p["foto"])
        foto = '<img class="foto" src="%s" alt="">' % esc(src)
    badge = ""
    if p.get("estado") == "atencion" or n > 0:
        badge = ('<div class="atienden">🤝 %s</div>'
                 % (("%d persona atendiendo" % n) if n == 1
                    else ("%d personas atendiendo" % n) if n
                    else "Alguien lo está atendiendo"))
    salud = ""
    if _re.search(r"salud|medicin|psic|emocional|mental", mensaje, _re.I):
        salud = ('<div class="nota">🧠 Apoyo voluntario entre personas: <b>no sustituye atención médica '
                 'ni psicológica profesional</b>. En crisis llama al 911.</div>')

    acc = ['<a class="btn atender" href="%s#pedir:%s">🤝 Atender este pedido</a>' % (SITE_URL_BASE, pid),
           '<button class="btn share" id="share">📤 Compartir</button>',
           '<button class="btn" id="copiar">🔗 Copiar enlace</button>',
           '<a class="btn red" href="%s#pedir">🌐 Ver más pedidos</a>' % SITE_URL_BASE]

    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#003C67">
<link rel="icon" type="image/svg+xml" href="/logo.svg?v=ofc">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap">
<title>%(titulo)s · Red de Acopio y Refugio</title>
<meta name="description" content="%(desc)s">
<link rel="canonical" href="%(url)s">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Red de Acopio y Refugio">
<meta property="og:title" content="%(titulo)s">
<meta property="og:description" content="%(desc)s">
<meta property="og:url" content="%(url)s">
<meta property="og:image" content="%(og)s">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="es_VE">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="%(titulo)s">
<meta name="twitter:description" content="%(desc)s">
<meta name="twitter:image" content="%(og)s">
<style>
  :root{--navy:#003C67;--teal:#1E9C94;--rojo:#b3261e;--wa:#25d366;--tinta:#0f1f33;--suave:#5b6b80;--linea:#e2e8f0}
  *{box-sizing:border-box}
  body{margin:0;background:#eef1f5;color:var(--tinta);font-family:Poppins,system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.5}
  .top{position:relative;overflow:hidden;color:#fff;padding:12px 16px;display:flex;align-items:center;gap:10px;
       background:linear-gradient(118deg,#022B4D,#003C67,#005B67,#15857E)}
  .top::before{content:"";position:absolute;inset:0;pointer-events:none;
       background:radial-gradient(360px 150px at top right,rgba(255,255,255,.14),transparent)}
  .top img{width:28px;height:28px;background:#fff;border-radius:7px;padding:3px;position:relative;
       box-shadow:0 1px 6px rgba(2,20,38,.25)}
  .top a{color:#fff;text-decoration:none;font-weight:600;font-size:14px;position:relative}
  .top .ir{margin-left:auto;font-size:12.5px;font-weight:600;color:rgba(255,255,255,.88);white-space:nowrap}
  .wrap{max-width:640px;margin:0 auto;padding:16px}
  .card{background:#fff;border:1px solid var(--linea);border-radius:14px;padding:18px;box-shadow:0 1px 3px rgba(15,31,51,.06)}
  h1{font-size:21px;margin:0 0 2px;color:var(--rojo);line-height:1.3}
  .tipo{color:var(--suave);font-size:13px;margin-bottom:12px}
  .foto{max-width:100%%;border-radius:10px;margin:4px 0 8px}
  .mensaje{white-space:pre-wrap;line-height:1.5;font-size:15px;margin:6px 0 2px}
  .atienden{display:inline-block;background:#e6f4f3;color:#0e5a55;font-weight:700;font-size:13px;border-radius:99px;padding:5px 12px;margin-top:12px}
  .nota{background:#eef6f7;border-radius:8px;padding:9px 11px;font-size:12.5px;color:#005B67;line-height:1.45;margin-top:12px}
  .aviso{background:#fdf3e7;border-radius:8px;padding:9px 11px;font-size:12.5px;color:#7a4a12;line-height:1.45;margin-top:12px}
  .acciones{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
  .btn{flex:1;min-width:130px;text-align:center;border:1.5px solid var(--linea);background:#fff;border-radius:10px;
       padding:11px 12px;font:inherit;font-size:14px;font-weight:600;color:var(--tinta);text-decoration:none;cursor:pointer}
  .btn.atender{background:var(--teal);border-color:var(--teal);color:#fff}
  .btn.share{background:var(--wa);border-color:var(--wa);color:#fff}
  .btn.red{background:var(--navy);border-color:var(--navy);color:#fff}
  .pie{color:var(--suave);font-size:12px;text-align:center;margin:16px 4px;line-height:1.6}
  .pie a{color:var(--teal)}
</style>
</head>
<body>
<div class="top"><img src="/logo.svg?v=ofc" alt=""><a href="/">Red de Acopio y Refugio</a><a class="ir" href="/">Ver la red →</a></div>
<div class="wrap">
  <div class="card">
    <h1>🆘 Pedido de ayuda</h1>
    <div class="tipo">📍 %(sector)s</div>
    %(foto)s
    <div class="mensaje">%(mensaje)s</div>
    %(badge)s%(salud)s
    <div class="aviso">🔒 Por seguridad <b>no mostramos el contacto de quien pide</b>. Toca
    «🤝 Atender este pedido» y, al dejar tus datos, verás su WhatsApp para coordinar directo.
    <b>No es información oficial</b>, verifica.</div>
    <div class="acciones">%(acciones)s</div>
  </div>
  <div class="pie">Actualizado: %(ahora)s · Esta página se actualiza sola.<br>
  La Red solo conecta; la información la reporta cada persona y <b>no está verificada</b>. ·
  <a href="/aviso-legal.html">Aviso legal</a></div>
</div>
<script>
var t=%(sh_js)s,u=%(url_js)s;
document.getElementById("share").onclick=function(){
  if(navigator.share){navigator.share({text:t,url:u}).catch(function(){});}
  else{window.open("https://wa.me/?text="+encodeURIComponent(t+" "+u),"_blank");}
};
document.getElementById("copiar").onclick=function(){
  var b=this;
  var p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(u):Promise.reject();
  p.then(function(){var x=b.textContent;b.textContent="✅ Enlace copiado";setTimeout(function(){b.textContent=x;},1600);})
   .catch(function(){prompt("Copia el enlace:",u);});
};
</script>
</body>
</html>""" % {
        "titulo": esc(titulo), "desc": esc(desc), "url": esc(url), "og": esc(og),
        "sector": esc(sector), "mensaje": esc(mensaje), "foto": foto, "badge": badge, "salud": salud,
        "acciones": "".join(acc), "ahora": esc(ahora),
        "sh_js": _js(sh), "url_js": _js(url),
    }


_PAGINA_REDIR_LLAMADO = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="2;url=https://redacopioyrefugio.com/#llamados">
<title>Red de Acopio y Refugio</title></head>
<body style="font-family:Poppins,system-ui,sans-serif;text-align:center;padding:48px 16px;color:#0f1f33;background:#eef1f5">
<p>Este llamado de apoyo ya fue resuelto o dejó de estar activo. 🙏</p>
<p><a href="https://redacopioyrefugio.com/#llamados" style="color:#1E9C94;font-weight:600">Ver los llamados activos →</a></p>
</body></html>"""


def _pagina_llamado(l, ahora):
    import html as _h
    esc = lambda s: _h.escape(str(s if s is not None else ""))
    pid = "ll-%s" % l["id"]
    url = SITE_URL_BASE + "s/" + pid + ".html"
    og = (SITE_URL_BASE + "s/og/" + l["og"]) if l.get("og") else OG_IMAGEN
    sector = (l.get("sector") or "Sector en emergencia").strip()
    mensaje = (l.get("mensaje") or "").strip()
    plano = " ".join(mensaje.split())
    n = l.get("respuestas") or 0

    titulo = "📣 Llamado de apoyo — %s" % sector
    desc = plano[:229].rstrip() + "…" if len(plano) > 230 else plano
    desc = (desc + " · Mira cómo ayudar.") if desc else "Llamado de apoyo levantado por voluntarios en terreno. Mira cómo ayudar."
    sh = "📣 Llamado de apoyo — %s: %s" % (sector, (plano[:139].rstrip() + "…" if len(plano) > 140 else plano))

    foto = ""
    if l.get("foto"):
        src = l["foto"] if str(l["foto"]).startswith(("http", "/")) else "/" + str(l["foto"])
        foto = '<img class="foto" src="%s" alt="">' % esc(src)
    badge = ""
    if n > 0:
        badge = ('<div class="atienden">🤝 %d persona%s respondiendo</div>'
                 % (n, "" if n == 1 else "s"))

    msg_wa = urllib.parse.quote("Hola 🙋 quiero ayudar con el llamado LL-%s — %s" % (l["id"], sector))
    acc = ['<a class="btn ayudarwa" target="_blank" rel="noopener" '
           'href="https://wa.me/58412XXXXXXX?text=%s">🙋 Puedo ayudar · WhatsApp</a>' % msg_wa,
           '<a class="btn ayudar" target="_blank" rel="noopener" '
           'href="https://t.me/%s?start=lla_%s">🙋 Puedo ayudar · Telegram</a>' % (_BOT_USER, l["id"]),
           '<button class="btn share" id="share">📤 Compartir</button>',
           '<button class="btn" id="copiar">🔗 Copiar enlace</button>',
           '<a class="btn red" href="%s#llamados">🌐 Ver más llamados</a>' % SITE_URL_BASE]

    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#003C67">
<link rel="icon" type="image/svg+xml" href="/logo.svg?v=ofc">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap">
<title>%(titulo)s · Red de Acopio y Refugio</title>
<meta name="description" content="%(desc)s">
<link rel="canonical" href="%(url)s">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Red de Acopio y Refugio">
<meta property="og:title" content="%(titulo)s">
<meta property="og:description" content="%(desc)s">
<meta property="og:url" content="%(url)s">
<meta property="og:image" content="%(og)s">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="es_VE">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="%(titulo)s">
<meta name="twitter:description" content="%(desc)s">
<meta name="twitter:image" content="%(og)s">
<style>
  :root{--navy:#003C67;--teal:#1E9C94;--teal2:#005B67;--wa:#25d366;--tinta:#0f1f33;--suave:#5b6b80;--linea:#e2e8f0}
  *{box-sizing:border-box}
  body{margin:0;background:#eef1f5;color:var(--tinta);font-family:Poppins,system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.5}
  .top{position:relative;overflow:hidden;color:#fff;padding:12px 16px;display:flex;align-items:center;gap:10px;
       background:linear-gradient(118deg,#022B4D,#003C67,#005B67,#15857E)}
  .top::before{content:"";position:absolute;inset:0;pointer-events:none;
       background:radial-gradient(360px 150px at top right,rgba(255,255,255,.14),transparent)}
  .top img{width:28px;height:28px;background:#fff;border-radius:7px;padding:3px;position:relative;
       box-shadow:0 1px 6px rgba(2,20,38,.25)}
  .top a{color:#fff;text-decoration:none;font-weight:600;font-size:14px;position:relative}
  .top .ir{margin-left:auto;font-size:12.5px;font-weight:600;color:rgba(255,255,255,.88);white-space:nowrap}
  .wrap{max-width:640px;margin:0 auto;padding:16px}
  .card{background:#fff;border:1px solid var(--linea);border-radius:14px;padding:18px;box-shadow:0 1px 3px rgba(15,31,51,.06)}
  h1{font-size:21px;margin:0 0 2px;color:var(--teal2);line-height:1.3}
  .tipo{color:var(--suave);font-size:13px;margin-bottom:12px}
  .foto{max-width:100%%;border-radius:10px;margin:4px 0 8px}
  .mensaje{white-space:pre-wrap;line-height:1.5;font-size:15px;margin:6px 0 2px}
  .atienden{display:inline-block;background:#e6f4f3;color:#0e5a55;font-weight:700;font-size:13px;border-radius:99px;padding:5px 12px;margin-top:12px}
  .aviso{background:#fdf3e7;border-radius:8px;padding:9px 11px;font-size:12.5px;color:#7a4a12;line-height:1.45;margin-top:12px}
  .acciones{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
  .btn{flex:1;min-width:130px;text-align:center;border:1.5px solid var(--linea);background:#fff;border-radius:10px;
       padding:11px 12px;font:inherit;font-size:14px;font-weight:600;color:var(--tinta);text-decoration:none;cursor:pointer}
  .btn.ayudar{background:var(--teal);border-color:var(--teal);color:#fff}
  .btn.ayudarwa{background:var(--wa);border-color:var(--wa);color:#fff}
  .btn.share{background:var(--wa);border-color:var(--wa);color:#fff}
  .btn.red{background:var(--navy);border-color:var(--navy);color:#fff}
  .pie{color:var(--suave);font-size:12px;text-align:center;margin:16px 4px;line-height:1.6}
  .pie a{color:var(--teal)}
</style>
</head>
<body>
<div class="top"><img src="/logo.svg?v=ofc" alt=""><a href="/">Red de Acopio y Refugio</a><a class="ir" href="/">Ver la red →</a></div>
<div class="wrap">
  <div class="card">
    <h1>📣 Llamado de apoyo</h1>
    <div class="tipo">📍 %(sector)s</div>
    %(foto)s
    <div class="mensaje">%(mensaje)s</div>
    %(badge)s
    <div class="aviso">📣 Llamado levantado por nuestros <b>voluntarios</b> en terreno, de un sector
    que sigue en emergencia y con poca cobertura. Si puedes ayudar, toca «🙋 Puedo ayudar»
    (por WhatsApp o Telegram) o <b>compártelo</b> para que la ayuda llegue. <b>No es información oficial</b>, verifica.</div>
    <div class="acciones">%(acciones)s</div>
  </div>
  <div class="pie">Actualizado: %(ahora)s · Esta página se actualiza sola.<br>
  La Red solo conecta; la información la reportan voluntarios y <b>no está verificada</b>. ·
  <a href="/aviso-legal.html">Aviso legal</a></div>
</div>
<script>
var t=%(sh_js)s,u=%(url_js)s;
document.getElementById("share").onclick=function(){
  if(navigator.share){navigator.share({text:t,url:u}).catch(function(){});}
  else{window.open("https://wa.me/?text="+encodeURIComponent(t+" "+u),"_blank");}
};
document.getElementById("copiar").onclick=function(){
  var b=this;
  var p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(u):Promise.reject();
  p.then(function(){var x=b.textContent;b.textContent="✅ Enlace copiado";setTimeout(function(){b.textContent=x;},1600);})
   .catch(function(){prompt("Copia el enlace:",u);});
};
</script>
</body>
</html>""" % {
        "titulo": esc(titulo), "desc": esc(desc), "url": esc(url), "og": esc(og),
        "sector": esc(sector), "mensaje": esc(mensaje), "foto": foto, "badge": badge,
        "acciones": "".join(acc), "ahora": esc(ahora),
        "sh_js": _js(sh), "url_js": _js(url),
    }


def _paginas_solicitudes(items, prefijo, og_fn, pagina_fn, redir, ahora):
    """Escribe web/s/<prefijo>-<id>.html para cada solicitud activa y convierte en
    redirección las que ya no están. Cada tipo (pd-/ll-) solo toca SUS archivos.
    Devuelve los nombres de archivo escritos (para el sitemap)."""
    sdir = WEB_DIR / "s"
    sdir.mkdir(parents=True, exist_ok=True)
    escritos = set()
    for it in items:
        if it.get("id") is None:
            continue
        try:
            it["og"] = og_fn(it, sdir)
        except Exception as e:  # sin imagen propia la página usa el og.png genérico
            print("og %s error:" % prefijo, it.get("id"), e)
            it["og"] = None
        nombre = "%s-%s.html" % (prefijo, it["id"])
        (sdir / nombre).write_text(pagina_fn(it, ahora), encoding="utf-8")
        escritos.add(nombre)
    for viejo in sdir.glob(prefijo + "-*.html"):
        if viejo.name not in escritos:
            viejo.write_text(redir, encoding="utf-8")
    vivos = {n[:-5] for n in escritos}  # sin ".html"
    for img in (sdir / "og").glob(prefijo + "-*.png") if (sdir / "og").is_dir() else []:
        if img.name.rsplit("-", 1)[0] not in vivos:
            img.unlink()
    return sorted(escritos)


def _paginas_pedidos(pedidos, ahora):
    return _paginas_solicitudes(pedidos, "pd", _og_pedido, _pagina_pedido,
                                _PAGINA_REDIR_PEDIDO, ahora)


def _paginas_llamados(llamados, ahora):
    return _paginas_solicitudes(llamados, "ll", _og_llamado, _pagina_llamado,
                                _PAGINA_REDIR_LLAMADO, ahora)


# Mismos rótulos e íconos que MS_ICO / MS_LABEL del JS (mantener en espejo).
MS_ICO_PY = {"comida": "🍲", "herramientas": "🔦", "transporte": "🚐", "agua": "💧",
             "carga": "🔌", "salud": "🩹", "hospedaje": "🛏️", "remoto": "💻",
             "estructural": "🏗️", "oficios": "🛠️", "otro": "📦"}
MS_LABEL_PY = {"comida": "Comida", "herramientas": "Herramientas/linternas",
               "transporte": "Transporte", "agua": "Agua", "carga": "Carga de teléfonos",
               "salud": "Salud", "hospedaje": "Hospedaje temporal", "remoto": "Remoto / En Línea",
               "estructural": "Estructural / Inspección", "oficios": "Oficios", "otro": "Otra ayuda"}

_PAGINA_REDIR_ALIADO = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="2;url=https://redacopioyrefugio.com/#manos">
<title>Red de Acopio y Refugio</title></head>
<body style="font-family:Poppins,system-ui,sans-serif;text-align:center;padding:48px 16px;color:#0f1f33;background:#eef1f5">
<p>Esta Mano Solidaria ya no está publicada en la red.</p>
<p><a href="https://redacopioyrefugio.com/#manos" style="color:#1E9C94;font-weight:600">Ver las Manos Solidarias activas →</a></p>
</body></html>"""


def _og_aliado(a, mdir):
    """Imagen 1200x630 propia de la Mano Solidaria para la vista previa (og:image).
    Mismo esquema que _og_punto/_og_pedido: hash del contenido en el nombre."""
    pid = "ms-%s" % a["id"]
    nombre = (a.get("nombre") or "").strip()
    label = MS_LABEL_PY.get(a.get("categoria")) or "Ayuda solidaria"
    zona = (a.get("zona") or "").strip()
    desc = " ".join((a.get("descripcion") or "").split())
    firma = hashlib.md5(json.dumps([nombre, label, zona, desc],
                                   ensure_ascii=False).encode()).hexdigest()[:8]
    nombre_png = "%s-%s.png" % (pid, firma)
    ogdir = mdir / "og"
    ogdir.mkdir(exist_ok=True)
    ruta = ogdir / nombre_png
    if not ruta.exists():
        from PIL import Image, ImageDraw
        NAVY, TEAL = (0, 60, 103), (30, 156, 148)
        BLANCO, CLARO, TENUE = (255, 255, 255), (203, 216, 230), (150, 175, 198)
        im = Image.new("RGB", (1200, 630), NAVY)
        dr = ImageDraw.Draw(im)
        dr.rectangle([0, 0, 16, 630], fill=TEAL)
        x = 72
        try:  # el icono trae el mismo navy de fondo → se funde solo
            logo = Image.open(WEB_DIR / "icon-512.png").convert("RGB").resize((92, 92))
            im.paste(logo, (56, 44))
            x_marca = 152
        except Exception:
            x_marca = x
        dr.text((x_marca, 72), "Red de Acopio y Refugio", font=_fuente_og(32), fill=BLANCO)
        ancho = 1200 - x - 72
        # Chip teal "Mano Solidaria · <categoría>"
        y = 172
        f_chip = _fuente_og(30)
        txt_chip = "Mano Solidaria · " + label
        w_chip = dr.textlength(txt_chip, font=f_chip)
        if w_chip > ancho - 44:
            txt_chip = "Mano Solidaria"
            w_chip = dr.textlength(txt_chip, font=f_chip)
        dr.rounded_rectangle([x, y, x + w_chip + 44, y + 56], radius=28, fill=TEAL)
        dr.text((x + 22, y + 9), txt_chip, font=f_chip, fill=BLANCO)
        y += 82
        # Nombre como título: reduce la letra hasta caber en 2 líneas
        for tam in (56, 46, 40):
            f_tit = _fuente_og(tam)
            lineas = _ajustar_og(dr, nombre, f_tit, ancho)
            if len(lineas) <= 2:
                break
        if len(lineas) > 2:
            lineas = lineas[:2]
            lineas[-1] += "…"
        for ln in lineas:
            dr.text((x, y), ln, font=f_tit, fill=BLANCO)
            y += int(tam * 1.26)
        if zona:
            dr.text((x, y + 4), "Zona: " + zona, font=_fuente_og(29), fill=CLARO)
            y += 50
        y += 10
        # Qué ofrece (hasta 3 líneas)
        f_msj = _fuente_og(31, bold=False)
        ln_msj = _ajustar_og(dr, desc, f_msj, ancho)
        corte = False
        for i, ln in enumerate(ln_msj):
            if y > 500 or i >= 3:
                corte = True
                break
            dr.text((x, y), ln, font=f_msj, fill=CLARO)
            y += 46
        if corte and y <= 520:
            dr.text((x, y), "…", font=f_msj, fill=CLARO)
        dr.text((x, 566), "redacopioyrefugio.com", font=_fuente_og(26, bold=False), fill=TENUE)
        im.save(ruta, "PNG", optimize=True)
    # limpia versiones viejas del mismo aliado (el hash cambió)
    for f in ogdir.glob(pid + "-*.png"):
        if f.name != nombre_png:
            f.unlink()
    return nombre_png


def _pagina_aliado(a, ahora):
    import html as _h
    esc = lambda s: _h.escape(str(s if s is not None else ""))
    pid = "ms-%s" % a["id"]
    url = SITE_URL_BASE + "m/" + pid + ".html"
    og = (SITE_URL_BASE + "m/og/" + a["og"]) if a.get("og") else OG_IMAGEN
    nombre = (a.get("nombre") or "").strip()
    ico = MS_ICO_PY.get(a.get("categoria")) or "🤝"
    label = MS_LABEL_PY.get(a.get("categoria")) or "Ayuda solidaria"
    zona = (a.get("zona") or "").strip()
    descripcion = (a.get("descripcion") or "").strip()
    plano = " ".join(descripcion.split())

    titulo = "%s %s — %s" % (ico, nombre, label)
    desc = plano[:200].rstrip() + "…" if len(plano) > 201 else plano
    desc = " · ".join(x for x in [desc, ("📍 " + zona) if zona else ""] if x)
    desc = (desc + " · Ayuda gratis de la comunidad. Pide su contacto en la web.") if desc \
        else "Ayuda gratis de la comunidad. Pide su contacto en la web."
    if len(desc) > 240:
        desc = desc[:239].rstrip() + "…"
    sh = "🤝 %s — %s: %s%s — pide su contacto en la web" % (
        nombre, label, (plano[:119].rstrip() + "…" if len(plano) > 120 else plano),
        (" (%s)" % zona) if zona else "")

    hor = ('<div class="tipo" style="margin:8px 0 0">🕐 %s</div>' % esc(a["horario"])) if a.get("horario") else ""

    acc = ['<a class="btn contactar" href="%s#manos:%s">🤝 Solicitar contacto</a>' % (SITE_URL_BASE, pid),
           '<button class="btn share" id="share">📤 Compartir</button>',
           '<button class="btn" id="copiar">🔗 Copiar enlace</button>']
    mapa = _maps_url({"lat": a.get("lat"), "lon": a.get("lon")})
    if mapa:
        acc.append('<a class="btn" target="_blank" rel="noopener" href="%s">🗺️ Cómo llegar</a>' % esc(mapa))
    acc.append('<a class="btn red" href="%s#manos">🌐 Ver todas las Manos Solidarias</a>' % SITE_URL_BASE)

    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#003C67">
<link rel="icon" type="image/svg+xml" href="/logo.svg?v=ofc">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap">
<title>%(titulo)s · Red de Acopio y Refugio</title>
<meta name="description" content="%(desc)s">
<link rel="canonical" href="%(url)s">
<meta name="robots" content="index,follow">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Red de Acopio y Refugio">
<meta property="og:title" content="%(titulo)s">
<meta property="og:description" content="%(desc)s">
<meta property="og:url" content="%(url)s">
<meta property="og:image" content="%(og)s">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:locale" content="es_VE">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="%(titulo)s">
<meta name="twitter:description" content="%(desc)s">
<meta name="twitter:image" content="%(og)s">
<style>
  :root{--navy:#003C67;--teal:#1E9C94;--wa:#25d366;--tinta:#0f1f33;--suave:#5b6b80;--linea:#e2e8f0}
  *{box-sizing:border-box}
  body{margin:0;background:#eef1f5;color:var(--tinta);font-family:Poppins,system-ui,-apple-system,sans-serif;font-size:15px;line-height:1.5}
  .top{position:relative;overflow:hidden;color:#fff;padding:12px 16px;display:flex;align-items:center;gap:10px;
       background:linear-gradient(118deg,#022B4D,#003C67,#005B67,#15857E)}
  .top::before{content:"";position:absolute;inset:0;pointer-events:none;
       background:radial-gradient(360px 150px at top right,rgba(255,255,255,.14),transparent)}
  .top img{width:28px;height:28px;background:#fff;border-radius:7px;padding:3px;position:relative;
       box-shadow:0 1px 6px rgba(2,20,38,.25)}
  .top a{color:#fff;text-decoration:none;font-weight:600;font-size:14px;position:relative}
  .top .ir{margin-left:auto;font-size:12.5px;font-weight:600;color:rgba(255,255,255,.88);white-space:nowrap}
  .wrap{max-width:640px;margin:0 auto;padding:16px}
  .card{background:#fff;border:1px solid var(--linea);border-radius:14px;padding:18px;box-shadow:0 1px 3px rgba(15,31,51,.06)}
  h1{font-size:21px;margin:0 0 2px;color:var(--navy);line-height:1.3}
  .tipo{color:var(--suave);font-size:13px;margin-bottom:10px}
  .mensaje{white-space:pre-wrap;line-height:1.5;font-size:15px;margin:6px 0 2px}
  .nota{background:#eef6f7;border-radius:8px;padding:9px 11px;font-size:12.5px;color:#005B67;line-height:1.45;margin-top:12px}
  .acciones{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
  .btn{flex:1;min-width:130px;text-align:center;border:1.5px solid var(--linea);background:#fff;border-radius:10px;
       padding:11px 12px;font:inherit;font-size:14px;font-weight:600;color:var(--tinta);text-decoration:none;cursor:pointer}
  .btn.contactar{background:var(--teal);border-color:var(--teal);color:#fff}
  .btn.share{background:var(--wa);border-color:var(--wa);color:#fff}
  .btn.red{background:var(--navy);border-color:var(--navy);color:#fff}
  .pie{color:var(--suave);font-size:12px;text-align:center;margin:16px 4px;line-height:1.6}
  .pie a{color:var(--teal)}
</style>
</head>
<body>
<div class="top"><img src="/logo.svg?v=ofc" alt=""><a href="/">Red de Acopio y Refugio</a><a class="ir" href="/">Ver la red →</a></div>
<div class="wrap">
  <div class="card">
    <h1>%(ico)s %(nombre)s</h1>
    <div class="tipo">%(label)s%(zona)s · Mano Solidaria de la red</div>
    <div class="mensaje">%(descripcion)s</div>
    %(hor)s
    <div class="nota">🔒 Su número <b>no se publica</b>. Toca «🤝 Solicitar contacto»: la persona recibe
    tu solicitud y, si acepta, cada quien recibe el contacto del otro. Ayuda <b>gratis</b> de la comunidad;
    <b>no es información oficial</b>, verifica.</div>
    <div class="acciones">%(acciones)s</div>
  </div>
  <div class="pie">Actualizado: %(ahora)s · Esta página se actualiza sola.<br>
  La Red solo conecta; la información la reporta cada persona y <b>no está verificada</b>. ·
  <a href="/aviso-legal.html">Aviso legal</a></div>
</div>
<script>
var t=%(sh_js)s,u=%(url_js)s;
document.getElementById("share").onclick=function(){
  if(navigator.share){navigator.share({text:t,url:u}).catch(function(){});}
  else{window.open("https://wa.me/?text="+encodeURIComponent(t+" "+u),"_blank");}
};
document.getElementById("copiar").onclick=function(){
  var b=this;
  var p=(navigator.clipboard&&navigator.clipboard.writeText)?navigator.clipboard.writeText(u):Promise.reject();
  p.then(function(){var x=b.textContent;b.textContent="✅ Enlace copiado";setTimeout(function(){b.textContent=x;},1600);})
   .catch(function(){prompt("Copia el enlace:",u);});
};
</script>
</body>
</html>""" % {
        "titulo": esc(titulo), "desc": esc(desc), "url": esc(url), "og": esc(og),
        "ico": ico, "nombre": esc(nombre), "label": esc(label),
        "zona": (" · 📍 " + esc(zona)) if zona else "",
        "descripcion": esc(descripcion), "hor": hor,
        "acciones": "".join(acc), "ahora": esc(ahora),
        "sh_js": _js(sh), "url_js": _js(url),
    }


def _paginas_aliados(aliados, ahora):
    """Escribe web/m/ms-<id>.html para cada Mano Solidaria aprobada y convierte en
    redirección las páginas de aliados que ya no están. Devuelve los nombres de
    archivo escritos (para el sitemap). OJO: recibe `aliados` YA sin contacto."""
    mdir = WEB_DIR / "m"
    mdir.mkdir(parents=True, exist_ok=True)
    escritos = set()
    for a in aliados:
        if a.get("id") is None or not (a.get("nombre") or "").strip():
            continue
        try:
            a["og"] = _og_aliado(a, mdir)
        except Exception as e:  # sin imagen propia la página usa el og.png genérico
            print("og aliado error:", a.get("id"), e)
            a["og"] = None
        nombre = "ms-%s.html" % a["id"]
        (mdir / nombre).write_text(_pagina_aliado(a, ahora), encoding="utf-8")
        escritos.add(nombre)
    for viejo in mdir.glob("*.html"):
        if viejo.name not in escritos:
            viejo.write_text(_PAGINA_REDIR_ALIADO, encoding="utf-8")
    vivos = {n[:-5] for n in escritos}  # sin ".html"
    for img in (mdir / "og").glob("*.png") if (mdir / "og").is_dir() else []:
        if img.name.rsplit("-", 1)[0] not in vivos:
            img.unlink()
    return sorted(escritos)


def _hmeta_contadores():
    """Franja EN VIVO del hero: contadores ACUMULATIVOS de actividad (crecen solos
    con la ingesta del canal). Antes decía «9 centros en la red» — un número chico
    en vitrina es prueba social negativa; estos presumen el diferencial real:
    la ingesta automática + la organización con IA."""
    try:
        con = db.conectar()
        n_rep = con.execute("SELECT COUNT(*) FROM mensajes").fetchone()[0]
        n_pts = con.execute("SELECT COUNT(*) FROM centros").fetchone()[0]
        n_ins = con.execute("SELECT COUNT(*) FROM inventario").fetchone()[0]
        con.close()
        _mil = lambda n: f"{n:,}".replace(",", ".")
        return (f"{_mil(n_rep)} reportes procesados · {_mil(n_pts)} puntos mapeados · "
                f"{_mil(n_ins)} insumos registrados")
    except Exception:
        return "red activa las 24 horas"


def _externalizar(html, web_data):
    """Fase 1b: CSS -> styles.css, codigo -> app.js (estatico, cacheable de verdad),
    datos -> datos.json. El index queda como shell chico; un bootstrap busca datos.json,
    asigna los globales y recien ahi carga app.js (que arranca con los datos ya listos)."""
    m_css = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    css = m_css.group(1)
    m_js = re.search(r"<script>\n(const CATS=.*?)</script>", html, re.DOTALL)
    full = m_js.group(1)
    corte = full.index("let miUbic=null;")
    preamble, codigo = full[:corte], full[corte:].strip()
    site_url = re.search(r'const SITE_URL="[^"]*";', preamble).group(0)
    share_msg = re.search(r'const SHARE_MSG="[^"]*";', preamble).group(0)
    # el contador (hmeta) deja de estar incrustado en el codigo: se lee del dato HMETA,
    # asi app.js queda 100% estatico y el navegador lo cachea de verdad.
    codigo = re.sub(r'document\.getElementById\("hmeta"\)\.textContent="[^"]*";',
                    'document.getElementById("hmeta").textContent=HMETA;', codigo)
    ver = hashlib.sha1((css + codigo).encode("utf-8")).hexdigest()[:8]
    datos_txt = json.dumps(web_data, ensure_ascii=False, separators=(",", ":"))
    dver = hashlib.sha1(datos_txt.encode("utf-8")).hexdigest()[:8]
    (WEB_DIR / "styles.css").write_text(css, encoding="utf-8")
    (WEB_DIR / "app.js").write_text(codigo, encoding="utf-8")
    (WEB_DIR / "datos.json").write_text(datos_txt, encoding="utf-8")
    claves = ",".join(web_data.keys())
    asigna = ";".join("%s=D.%s" % (k, k) for k in web_data)
    boot = (site_url + "\n" + share_msg + "\nvar " + claves + ";\n"
            + 'fetch("datos.json?v=%s").then(r=>r.json()).then(D=>{%s;'
              'var s=document.createElement("script");s.src="app.js?v=%s";'
              'document.body.appendChild(s);}).catch(()=>{});' % (dver, asigna, ver))
    html = html.replace(m_css.group(0), '<link rel="stylesheet" href="styles.css?v=%s">' % ver)
    html = html.replace(m_js.group(0), "<script>\n%s\n</script>" % boot)
    return html


def construir():
    datos = _filtrar(db.listar_relevantes())
    for m in datos:
        m["zona"] = _zona((m.get("ubicacion") or "") + " " + (m.get("detalle") or ""))
    # Los centros de acopio / refugios / centros de atención ya NO se alimentan del
    # canal de emergencias: viven en la red de colaboradores (directorio / oferta /
    # requerimientos). El canal queda solo como flujo en vivo (rescate, heridos,
    # desaparecidos, donaciones, ofrecimientos, servicios, mascotas, info, contactos).
    servicios = [s for s in db.resumen_servicios() if _horas(s["tg_fecha"]) <= SERVICIOS_VENTANA_HORAS]
    necesidades = db.resumen_necesidades()
    seguimiento = db.necesidades_web()
    # Autoregistro: puntos detectados por el canal aún sin responsable (reclamables).
    try:
        canal_sin_reclamar = db.centros_sin_reclamar()
    except Exception:
        canal_sin_reclamar = []
    oferta = _overlay_stock(db.resumen_oferta())
    directorio = db.resumen_directorio()
    aliados = db.resumen_aliados()
    for _a in aliados:  # el número JAMÁS viaja a la web: se solicita vía bot (contacto_ms)
        _a.pop("contacto", None)
    llamados = db.resumen_llamados()
    pedidos = _pedidos_web()
    sismos = obtener_sismos()
    ahora = datetime.datetime.now(ZoneInfo("America/Caracas")).strftime("%d/%m/%Y %H:%M") + " VET"
    from collections import Counter
    _catc = Counter(m["categoria"] for m in datos)
    _desap = [m for m in datos if m["categoria"] == "desaparecidos"]
    _buscando = sum(1 for m in _desap if (m.get("estado_persona") or "buscando") == "buscando")
    resumen = {
        "desaparecidos_buscando": _buscando,
        "desaparecidos_resueltos": len(_desap) - _buscando,
        "heridos": _catc.get("heridos", 0),
        "rescate": _catc.get("rescate", 0),
        "ofrecimientos": _catc.get("ofrecimientos", 0),
        "servicios_zonas": len({s["zona"] for s in servicios}),
        "nuevos_24h": sum(1 for m in datos if _horas(m["tg_fecha"]) <= 24),
        "actualizado": ahora,
    }
    html = (
        PLANTILLA.replace("__SEO_TITULO__", SEO_TITULO)
        .replace("__SEO_DESC__", SEO_DESC)
        .replace("__TITULO__", SITE_TITLE)
        .replace("__TAGLINE__", SITE_TAGLINE)
        .replace("__FUENTE__", FUENTE)
        .replace("__ACTUALIZADO__", ahora)
        .replace("__HMETA__", _hmeta_contadores())
        .replace("__CATS__", _js(CATEGORIAS))
        .replace("__DATOS__", _js(datos))
        .replace("__AVISO__", _js(AVISO_WHATSAPP))
        .replace("__EMERGENCIAS__", _js(EMERGENCIAS))
        .replace("__PLATAFORMAS__", _js(PLATAFORMAS))
        .replace("__SERVICIOS__", _js(servicios))
        .replace("__NECESIDADES__", _js(necesidades))
        .replace("__SEGUIMIENTO__", _js(seguimiento))
        .replace("__CANAL__", _js(canal_sin_reclamar))
        .replace("__OFERTA__", _js(oferta))
        .replace("__DIRECTORIO__", _js(directorio))
        .replace("__ALIADOS__", _js(aliados))
        .replace("__LLAMADOS__", _js(llamados))
        .replace("__PEDIDOS__", _js(pedidos))
        .replace("__RESUMEN__", _js(resumen))
        .replace("__SISMOS__", _js(sismos))
        .replace("__PRERENDER__", _prerender(resumen, directorio))
    )
    # R2 2026-07-10: el JSON publico solo lleva los campos que el frontend usa;
    # se quitan metadatos internos (id, tg_id, relevante, procesado_en, pedido_lid).
    _CAMPOS_PUB_DATOS = ("categoria", "tg_fecha", "vigencia", "contacto", "titulo",
        "ubicacion", "detalle", "texto_original", "necesidades", "foto", "link", "estado_persona")
    _web_data = {
        "CATS": CATEGORIAS, "DATOS": [{k: m.get(k) for k in _CAMPOS_PUB_DATOS} for m in datos], "AVISO": AVISO_WHATSAPP,
        "EMERGENCIAS": EMERGENCIAS, "PLATAFORMAS": PLATAFORMAS, "SERVICIOS": servicios,
        "NECESIDADES": necesidades, "SEGUIMIENTO": seguimiento, "CANAL": canal_sin_reclamar,
        "OFERTA": oferta, "DIRECTORIO": directorio, "ALIADOS": aliados,
        "LLAMADOS": llamados, "PEDIDOS": pedidos, "RESUMEN": resumen, "SISMOS": sismos,
        "HMETA": _hmeta_contadores(),
    }
    WEB_DIR.mkdir(exist_ok=True)
    html = _externalizar(html, _web_data)
    (WEB_DIR / "index.html").write_text(html, encoding="utf-8")
    (WEB_DIR / "manifest.webmanifest").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    (WEB_DIR / "sw.js").write_text(SERVICE_WORKER, encoding="utf-8")
    (WEB_DIR / "ofrecer.html").write_text(FORM_OFRECER, encoding="utf-8")
    # Las categorías del <select> salen de config.CATS_PEDIR (única definición,
    # la misma que validan backend.py y usan los bots) — nunca editarlas aquí.
    _opciones = "\n".join('        <option value="%s">%s</option>' % (k, v)
                          for k, v in CATS_PEDIR.items())
    (WEB_DIR / "pedir.html").write_text(
        FORM_PEDIR.replace("__OPCIONES_CATS_PEDIR__", _opciones), encoding="utf-8")
    (WEB_DIR / "aviso-legal.html").write_text(PAGINA_LEGAL, encoding="utf-8")
    try:
        paginas = _paginas_puntos(necesidades, seguimiento, oferta, directorio, ahora)
    except Exception as e:  # las páginas por punto nunca deben tumbar el build del sitio
        print("paginas por punto error (la web sale igual):", e)
        paginas = []
    try:
        paginas_ped = _paginas_pedidos(pedidos, ahora)
    except Exception as e:  # ídem: las páginas de pedidos nunca tumban el build
        print("paginas de pedidos error (la web sale igual):", e)
        paginas_ped = []
    try:
        paginas_ped += _paginas_llamados(llamados, ahora)
    except Exception as e:  # ídem: las páginas de llamados nunca tumban el build
        print("paginas de llamados error (la web sale igual):", e)
    try:
        paginas_ms = _paginas_aliados(aliados, ahora)
    except Exception as e:  # ídem: las páginas de Manos Solidarias nunca tumban el build
        print("paginas de aliados error (la web sale igual):", e)
        paginas_ms = []
    hoy = datetime.date.today().isoformat()
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '<url><loc>https://redacopioyrefugio.com/</loc>'
        f'<lastmod>{hoy}</lastmod>'
        '<changefreq>hourly</changefreq><priority>1.0</priority></url>\n'
        + "".join(
            f'<url><loc>https://redacopioyrefugio.com/p/{n}</loc>'
            f'<lastmod>{hoy}</lastmod>'
            '<changefreq>hourly</changefreq><priority>0.7</priority></url>\n'
            for n in paginas)
        + "".join(
            f'<url><loc>https://redacopioyrefugio.com/s/{n}</loc>'
            f'<lastmod>{hoy}</lastmod>'
            '<changefreq>hourly</changefreq><priority>0.6</priority></url>\n'
            for n in paginas_ped)
        + "".join(
            f'<url><loc>https://redacopioyrefugio.com/m/{n}</loc>'
            f'<lastmod>{hoy}</lastmod>'
            '<changefreq>hourly</changefreq><priority>0.6</priority></url>\n'
            for n in paginas_ms)
        + '</urlset>\n'
    )
    (WEB_DIR / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    return WEB_DIR / "index.html", len(datos)

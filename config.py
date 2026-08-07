"""Configuración central. Lee todo desde el archivo .env."""
import os
import pathlib
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
CANAL = os.getenv("TELEGRAM_CANAL", "")

# --- Anthropic (modo híbrido) ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Modelo fuerte (preciso) para mensajes CON foto (volantes de desaparecidos, etc.)
MODELO_FUERTE = os.getenv("MODELO_FUERTE", "claude-opus-4-8")
# Modelo rápido (barato) para mensajes de SOLO texto
MODELO_RAPIDO = os.getenv("MODELO_RAPIDO", "claude-haiku-4-5")
MODELO = os.getenv("MODELO_CLAUDE", MODELO_FUERTE)  # compatibilidad / utilidades

# --- Parámetros ---
LIMITE_MENSAJES = int(os.getenv("LIMITE_MENSAJES", "80") or "80")
# Número de WhatsApp (solo dígitos, ej. 58412...) para recibir avisos de "novedad".
# Déjalo vacío si no quieres mostrar el botón de avisar.
AVISO_WHATSAPP = os.getenv("AVISO_WHATSAPP", "")

# --- Bot de moderación (Telegram) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GRUPO_MODERADORES = os.getenv("GRUPO_MODERADORES", "")   # chat_id del grupo (número negativo)

# --- Bot de afiliación de representantes (Ángeles · Apoyo) ---
APOYO_BOT_TOKEN = os.getenv("APOYO_BOT_TOKEN", "")
# Auto-PUBLICAR sin aprobación previa. Cuando está activo, centros, voluntariado y
# Manos Solidarias aparecen al instante y el aviso al grupo trae solo "❌ Quitar de la
# web" (post-moderación + botón público de Denunciar). DECISIÓN de Gustavo 2026-07-01:
# producción lo usa en "1" (todo auto-publicado). El sandbox también.
AUTO_APROBAR = os.getenv("AUTO_APROBAR", "").strip().lower() in ("1", "true", "yes", "si", "sí")

# --- Panel master privado (vista de capturas, con login) ---
PANEL_USER = os.getenv("PANEL_USER", "")
PANEL_PASS = os.getenv("PANEL_PASS", "")

# --- API institucional (lectura, para instituciones gubernamentales) ---
# Header requerido: X-API-Key. Vacío = la API queda desactivada (404).
INSTITUCION_API_KEY = os.getenv("INSTITUCION_API_KEY", "")

# --- Observabilidad (opcional; Sentry apagado si SENTRY_DSN vacío) ---
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
# Alertas de error por Telegram (al operador; lista separada por comas).
ALERTAS_TG_IDS = os.getenv("ALERTAS_TG_IDS", "")
ALERTAS_BOT_TOKEN = os.getenv("ALERTAS_BOT_TOKEN", "") or TELEGRAM_BOT_TOKEN

# --- Rutas (no tocar) ---
BASE = pathlib.Path(__file__).parent
DB_PATH = BASE / "angeles.db"
SESSION_PATH = str(BASE / "angeles")          # archivo de sesión de Telethon
WEB_DIR = BASE / "web"
SITE_TITLE = "Red de Acopio y Refugio"
SITE_TAGLINE = "Registro en tiempo real de centros de acopio, refugios y hospitales en Venezuela"

# --- WhatsApp Cloud API (Meta) ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
WA_TPL_LLAMADO = os.getenv("WA_TPL_LLAMADO", "")  # plantilla WABA del aviso de llamado

# --- Pedir ayuda: categorías canónicas (ÚNICA definición; backend.py, bot_apoyo.py y
# bot_wa.py la importan de aquí para que el mensaje salga idéntico por los 3 canales) ---
CATS_PEDIR = {
    "comida": "🍲 Comida", "agua": "💧 Agua", "salud": "🩹 Salud / medicinas",
    "refugio": "🛏️ Refugio / dónde dormir", "rescate": "🆘 Rescate / persona atrapada",
    "transporte": "🚐 Transporte", "desaparecido": "🔍 Persona desaparecida",
    "otro": "📦 Otra necesidad",
}

# Auto-PUBLICAR pedidos de ayuda (mismo modelo que AUTO_APROBAR para registros,
# DECISIÓN de Gustavo 2026-07-03): el pedido sale a la web al instante y el grupo de
# moderadores recibe un REPORTE con botón "🗑 Remover de la web" (post-moderación).
# El aviso a la red (acopios/MS/voluntarios) lo abanica el barrido de bot_apoyo.py.
# Kill-switch: AUTO_PUBLICAR_PEDIDOS=0 en el entorno restaura la revisión previa.
AUTO_PUBLICAR_PEDIDOS = os.getenv("AUTO_PUBLICAR_PEDIDOS", "1").strip().lower() not in ("0", "false", "no")
# Categorías que SIEMPRE pasan por revisión previa aunque el auto-publicar esté activo:
# publican datos de un TERCERO (p. ej. el nombre de una persona desaparecida) y un
# reporte falso hace daño real.
CATS_PEDIR_MODERADAS = {"desaparecido"}

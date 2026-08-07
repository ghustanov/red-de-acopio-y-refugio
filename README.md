# Red de Acopio y Refugio

Plataforma comunitaria para coordinar ayuda humanitaria en emergencias:
centros de acopio, refugios, voluntarios, pedidos de ayuda y reportes de
personas desaparecidas. Operó en Venezuela durante 2026 en
redacopioyrefugio.com y se publica aquí, con licencia MIT, para que
cualquier comunidad que enfrente una emergencia pueda levantar algo
similar sin empezar de cero.

> **Estado: proyecto cerrado.** Este repositorio es un legado técnico.
> No hay soporte activo, pero el código está completo y funcionó en
> producción real.

## Qué hace

- **Ingesta con IA**: lee un canal de Telegram de emergencia (Telethon),
  clasifica los mensajes con Claude (heridos, donaciones, rescates,
  alertas, contactos) y publica la información organizada en una web
  estática.
- **Directorio vivo**: centros de acopio y refugios con registro por
  web o WhatsApp, auto-publicación con post-moderación y botón público
  de denuncia.
- **Pedidos de ayuda**: formulario web + bot de WhatsApp (Cloud API)
  para pedir u ofrecer ayuda; emparejador necesidad→acopio.
- **Moderación**: bot de Telegram para el grupo de moderadores
  (aprobar, quitar, cerrar puntos).
- **Censo y reportes**: censo por núcleo familiar en refugios y
  reportes situacionales en PNG.
- **Operación**: monitor del feed, reporte diario de métricas por DM,
  depurador automático y backups.

## Arquitectura

```
canal Telegram ──> captura.py / reader.py ──> classifier.py (Claude)
                                                   │
web (formularios) ──> backend.py (Flask) ──────> angeles.db (SQLite)
WhatsApp Cloud API ─> bot_wa.py (Flask)  ──────>     │
bots Telegram ─────> bot.py / bot_apoyo.py ─────>    │
                                                   ▼
                                     build_site.py ──> web/ (sitio estático)
```

Todo corre en un solo VPS pequeño: SQLite como única base de datos,
sitio estático servido por nginx, backends Flask detrás de gunicorn en
localhost, y systemd + cron para los procesos.

## Componentes principales

| Archivo | Rol |
|---|---|
| `captura.py`, `reader.py`, `run_once.py` | Ingesta del canal de Telegram |
| `classifier.py` | Clasificación de mensajes con Claude |
| `build_site.py` | Generador del sitio estático |
| `backend.py` | API Flask de formularios web (gunicorn :8090) |
| `bot_wa.py`, `wa_api.py`, `wa_util.py` | Bot de WhatsApp (Cloud API, gunicorn :8091) |
| `bot.py` | Bot de moderación (Telegram) |
| `bot_apoyo.py` | Bot de afiliación de representantes (Telegram) |
| `canal_pedidos.py`, `envios.py`, `seguimiento.py` | Pedidos, envíos y seguimiento |
| `reporte_situacional.py`, `reporte_diario.py` | Reportes PNG y métricas |
| `monitor_feed.py`, `depurador.py`, `health.sh` | Operación y salud |
| `db.py`, `config.py` | Acceso a datos y configuración por `.env` |
| `web/` | Shell estático del sitio (HTML/CSS/JS) |

`Red_de_Acopio_y_Refugio_Tecnico.pdf` documenta el diseño con más detalle.

## Cómo levantarlo

1. Python 3.11+, `python3 -m venv venv && venv/bin/pip install -r requirements.txt`.
2. `cp .env.example .env` y completa tus credenciales (Telegram API,
   Anthropic, tokens de bots; WhatsApp es opcional).
3. `python3 login.py` para iniciar sesión de Telethon y obtener el ID
   del canal.
4. `python3 run_once.py` genera el sitio en `web/` a partir del canal.
5. Para producción: nginx sirviendo `web/` + gunicorn para
   `backend:app` y `bot_wa:app` + systemd para los bots (los nombres de
   servicio que usamos: `angeles-bot`, `angeles-apoyo`, `angeles-wa`,
   `angeles-web`, más timers de monitor y reporte).

## Privacidad

La base de datos real (censo de personas, teléfonos, reportes de
desaparecidos) **no está ni estará** en este repositorio, igual que las
credenciales y las imágenes capturadas. Los teléfonos que aparecen en
tests y ejemplos son sintéticos. Si replicas el proyecto, trata los
datos de las personas con el mismo cuidado: son población vulnerable.

## Licencia

MIT — ver [LICENSE](LICENSE). Si este código le sirve a tu comunidad,
úsalo. Para eso está.

"""
Corre esto UNA sola vez para iniciar sesión en Telegram con tu cuenta.
Te va a pedir tu número de teléfono y el código que llega por Telegram.
Después de esto, los demás scripts ya funcionan solos.

Uso:
    python3 login.py
"""
from telethon.sync import TelegramClient
from config import API_ID, API_HASH, SESSION_PATH

if API_ID == 0 or not API_HASH:
    raise SystemExit(
        "Falta TELEGRAM_API_ID o TELEGRAM_API_HASH en el archivo .env.\n"
        "Sácalos en https://my.telegram.org → 'API development tools'."
    )

with TelegramClient(SESSION_PATH, API_ID, API_HASH) as client:
    yo = client.get_me()
    print(f"\n✅ Sesión iniciada como: {yo.first_name} (@{yo.username})\n")
    print("Estos son los canales/grupos donde estás (busca el de rescate):\n")
    for dialog in client.iter_dialogs():
        if dialog.is_channel or dialog.is_group:
            print(f"  ID: {dialog.id:>16}   {dialog.name}")
    print(
        "\nCopia el ID del canal de rescate y pégalo en .env como TELEGRAM_CANAL=\n"
    )

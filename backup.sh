#!/bin/bash
# Respaldo de Red de Acopio y Refugio: DB (WAL-safe) + código + .env → local y Google Drive.
# AVISO: el respaldo incluye el .env (credenciales en claro). Si lo subes a un
# almacenamiento externo, cífralo antes (p. ej. age o gpg) y restringe el acceso.
# Corre como ROOT (por rclone). La copia SQLite se hace como 'angeles' (WAL/permite).
# Diseñado para NO abortar: primero asegura el backup local, luego intenta Google Drive.
DATE=$(date +%Y%m%d-%H%M%S)
BASE=/opt/angeles
LOG=$BASE/backup.log

# 1) DB WAL-safe (como angeles, para no crear -wal root-owned)
runuser -u angeles -- sqlite3 $BASE/angeles.db ".backup '$BASE/_backups/db_$DATE.db'" \
  && echo "$(date '+%F %T'): DB local OK ($DATE)" >> $LOG \
  || echo "$(date '+%F %T'): ERROR backup DB local" >> $LOG
runuser -u angeles -- cp $BASE/.env $BASE/_backups/env_$DATE.bak 2>/dev/null

# 2) Rotación local: conservar los últimos 30 db_ y 30 env_
ls -t $BASE/_backups/db_*.db 2>/dev/null  | tail -n +31 | xargs -r rm -f
ls -t $BASE/_backups/env_*.bak 2>/dev/null | tail -n +31 | xargs -r rm -f

# 3) Off-site: tar código + DB CONSISTENTE + .env → Google Drive (excluye lo pesado/regenerable).
#    FIX 2026-07-06: el offsite ahora empaqueta el snapshot consistente (.backup) desde _backups/,
#    NO el angeles.db vivo. En modo journal=delete, copiar la DB viva durante una escritura podía
#    dejar el offsite inconsistente/corrupto — y el offsite era la única copia fuera del VPS.
ARCHIVE=/tmp/redacopio_backup_$DATE.tar.gz
SNAP=$BASE/_backups/db_$DATE.db
STAGE=$(mktemp -d)
if [ -f "$SNAP" ]; then
  cp "$SNAP" "$STAGE/angeles.db"
else
  cp "$BASE/angeles.db" "$STAGE/angeles.db"
  echo "$(date '+%F %T'): WARN offsite usando DB viva (sin snapshot consistente)" >> $LOG
fi
cp "$BASE/.env" "$STAGE/.env" 2>/dev/null
cd $BASE && tar czf $ARCHIVE \
  --exclude='venv' --exclude='_backups' --exclude='__pycache__' \
  --exclude='whisper_models' --exclude='.cache' --exclude='.gunicorn' \
  --exclude='web' --exclude='*.log' \
  *.py *.md *.txt *.sh -C "$STAGE" angeles.db .env 2>/dev/null
rm -rf "$STAGE"
if rclone copy $ARCHIVE gdrive:RedAcopioBackups/ 2>>$LOG; then
  echo "$(date '+%F %T'): Google Drive OK -> gdrive:RedAcopioBackups/$(basename $ARCHIVE)" >> $LOG
else
  echo "$(date '+%F %T'): ERROR subiendo a Google Drive" >> $LOG
fi
rm -f $ARCHIVE

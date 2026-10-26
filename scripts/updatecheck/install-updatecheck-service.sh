#!/bin/bash
# Installa soltanto il checker settimanale; gli otto sensori restano indipendenti.
set -euo pipefail
umask 077
if [[ "${1:-}" == --help ]]; then
  echo "Uso: /bin/bash install-updatecheck-service.sh"
  echo "Installa il calendario di updatecheck.json (default lunedi 19:00) e avvia un controllo."
  echo "Richiede sudo, CPython 3.14.9+ e updatecheck-push-url.txt. Nessun aggiornamento software."
  exit 0
fi
if (( $# != 0 )); then
  echo "ERRORE: nessun argomento previsto. Usa --help." >&2
  exit 2
fi
if [[ "$(/usr/bin/uname -s)" != Darwin ]]; then
  echo "ERRORE: eseguire sul Mac mini." >&2
  exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
if [[ "$SCRIPT_DIR" != "$PROJECT_ROOT/scripts/updatecheck" ]]; then
  echo "ERRORE: collocare i file in Fithub/scripts/updatecheck." >&2
  exit 2
fi
for filename in update-check.sh update_checker.py updatecheck.example.json ch.nmperformance.fithub.updatecheck.plist; do
  if [[ ! -f "$SCRIPT_DIR/$filename" || -L "$SCRIPT_DIR/$filename" ]]; then
    echo "ERRORE: file mancante o non valido: $filename" >&2
    exit 2
  fi
done
if (( EUID != 0 )); then
  exec /usr/bin/sudo /bin/bash "$SCRIPT_DIR/install-updatecheck-service.sh"
fi
SERVICE_USER="${SUDO_USER:-}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == root ]]; then
  echo "ERRORE: avvia dal tuo account amministratore, non da una shell root." >&2
  exit 2
fi
SERVICE_GROUP="$(/usr/bin/id -gn "$SERVICE_USER")"
LABEL=ch.nmperformance.fithub.updatecheck
PLIST="/Library/LaunchDaemons/$LABEL.plist"
CONFIG="$SCRIPT_DIR/updatecheck.json"
URL_FILE="$SCRIPT_DIR/updatecheck-push-url.txt"
LOG_DIR="$PROJECT_ROOT/logs/system/updatecheck"
for directory in "$LOG_DIR" "$SCRIPT_DIR/state"; do
  if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
    echo "ERRORE: directory stato/log non valida." >&2
    exit 2
  fi
done
if [[ -L "$CONFIG" || ( -e "$CONFIG" && ! -f "$CONFIG" ) ]]; then
  echo "ERRORE: updatecheck.json non valido." >&2
  exit 2
fi
if [[ ! -e "$CONFIG" ]]; then
  /usr/bin/install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 600 "$SCRIPT_DIR/updatecheck.example.json" "$CONFIG"
fi
if [[ ! -s "$URL_FILE" || ! -f "$URL_FILE" || -L "$URL_FILE" ]]; then
  echo "ERRORE: salva il nuovo URL Push Kuma in $URL_FILE prima di installare." >&2
  exit 2
fi
# Preflight prima di fermare un eventuale checker esistente; nessuna rete.
/bin/bash -n "$SCRIPT_DIR/update-check.sh"
KUMA_UPDATECHECK_PUSH_URL= /bin/bash "$SCRIPT_DIR/update-check.sh" --self-check
STAGING="$(/usr/bin/mktemp -d /private/tmp/fithub-updatecheck.XXXXXX)"
trap '/bin/rm -f "$STAGING/service.plist"; /bin/rmdir "$STAGING"' EXIT
/bin/cp "$SCRIPT_DIR/ch.nmperformance.fithub.updatecheck.plist" "$STAGING/service.plist"
# plutil sugli array richiede remove+insert: replace puo inserire una voce extra.
/usr/bin/plutil -remove ProgramArguments.1 "$STAGING/service.plist"
/usr/bin/plutil -insert ProgramArguments.1 -string "$SCRIPT_DIR/update-check.sh" "$STAGING/service.plist"
/usr/bin/plutil -replace UserName -string "$SERVICE_USER" "$STAGING/service.plist"
for key in weekday hour minute; do
  case "$key" in weekday) plist_key=Weekday ;; hour) plist_key=Hour ;; minute) plist_key=Minute ;; esac
  value="$(/usr/bin/plutil -extract "schedule.$key" raw -o - "$CONFIG")"
  /usr/bin/plutil -replace "StartCalendarInterval.$plist_key" -integer "$value" "$STAGING/service.plist"
done
/usr/bin/plutil -lint "$STAGING/service.plist"
if /bin/launchctl print "system/$LABEL" >/dev/null 2>&1; then
  # Non interrompere una scansione lunga in corso per reinstallare il calendario.
  service_info="$(/bin/launchctl print "system/$LABEL")"
  if [[ "$service_info" == *'state = running'* ]]; then
    echo "ERRORE: checker in esecuzione; attendere la fine prima di reinstallare." >&2
    exit 2
  fi
fi
/usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$SCRIPT_DIR"
/bin/chmod 700 "$SCRIPT_DIR"
for filename in update-check.sh update_checker.py; do
  /usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$SCRIPT_DIR/$filename"
  /bin/chmod 700 "$SCRIPT_DIR/$filename"
done
for filename in "$CONFIG" "$URL_FILE"; do
  /usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$filename"
  /bin/chmod 600 "$filename"
done
for directory in "$PROJECT_ROOT/logs" "$PROJECT_ROOT/logs/system"; do
  if [[ ! -d "$directory" ]]; then
    /usr/bin/install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 755 "$directory"
  fi
done
/usr/bin/install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 700 "$LOG_DIR" "$SCRIPT_DIR/state"
for filename in update-check.log update-check-error.log; do
  if [[ -L "$LOG_DIR/$filename" || ( -e "$LOG_DIR/$filename" && ! -f "$LOG_DIR/$filename" ) ]]; then
    echo "ERRORE: percorso log non valido." >&2
    exit 2
  fi
  /usr/bin/touch "$LOG_DIR/$filename"
  /usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$LOG_DIR/$filename"
  /bin/chmod 600 "$LOG_DIR/$filename"
done
if /bin/launchctl print "system/$LABEL" >/dev/null 2>&1; then
  /bin/launchctl bootout "system/$LABEL"
fi
/usr/bin/install -o root -g wheel -m 644 "$STAGING/service.plist" "$PLIST"
BASE_LOG_BYTES="$(/usr/bin/stat -f %z "$LOG_DIR/update-check.log")"
/bin/launchctl enable "system/$LABEL"
/bin/launchctl bootstrap system "$PLIST"
/bin/launchctl kickstart "system/$LABEL"
verified=0
for attempt in {1..25}; do
  service_info="$(/bin/launchctl print "system/$LABEL" 2>/dev/null)" || service_info=""
  if [[ "$service_info" == *'job state = spawn failed'* ]]; then
    echo "ERRORE: launchd non avvia il checker. Verificare Privacy di /bin/bash e accesso a Documents." >&2
    exit 2
  fi
  log_bytes="$(/usr/bin/stat -f %z "$LOG_DIR/update-check.log" 2>/dev/null)" || log_bytes=0
  if (( log_bytes > BASE_LOG_BYTES )); then
    if [[ "$service_info" == *'state = running'* ]]; then
      echo "Checker avviato: prima scansione in corso; puo richiedere alcuni minuti."
      verified=1
      break
    fi
    code="$(printf '%s\n' "$service_info" | /usr/bin/awk '/^[[:space:]]*last exit code =/ { sub(/:$/, "", $5); print $5; exit }')"
    case "$code" in
      0) echo "Prima scansione completata: nessuna novita."; verified=1; break ;;
      1) echo "Prima scansione completata: aggiornamenti/revisione richiesti."; verified=1; break ;;
      2) echo "ERRORE: scansione incompleta; leggere latest.txt e update-check-error.log in $LOG_DIR." >&2; exit 2 ;;
    esac
  fi
  /bin/sleep 1
done
if (( verified == 0 )); then
  echo "ERRORE: avvio non verificato entro 25 secondi. Leggere i log e launchctl print system/$LABEL." >&2
  exit 2
fi
echo "Calendario installato da updatecheck.json, nell'orario locale del Mac."
echo "Report: $LOG_DIR/latest.txt"
echo "Solo il plist e in /Library/LaunchDaemons; script, token, stati e log restano nel progetto."

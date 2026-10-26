#!/bin/bash
# Installa o aggiorna il monitor HOST direttamente nel progetto FitHub.
set -euo pipefail
umask 077

if [[ "${1:-}" == --help ]]; then
  echo "Uso: /bin/bash install-host-service.sh"
  echo "Avvia HOST al boot, ogni 30 secondi. Richiede sudo."
  echo "Script e URL restano in Fithub/scripts/monitormetrics; log in Fithub/logs/system/monitormetrics."
  exit 0
fi
if (( $# != 0 )); then
  echo "ERRORE: nessun argomento previsto. Usa --help per le istruzioni." >&2
  exit 2
fi
if [[ "$(/usr/bin/uname -s)" != Darwin ]]; then
  echo "ERRORE: eseguire questo installer sul Mac mini." >&2
  exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
if [[ "$SCRIPT_DIR" != "$PROJECT_ROOT/scripts/monitormetrics" ||
      ! -f "$SCRIPT_DIR/host.sh" || -L "$SCRIPT_DIR/host.sh" ]]; then
  echo "ERRORE: collocare installer e host.sh in Fithub/scripts/monitormetrics." >&2
  exit 2
fi
if (( EUID != 0 )); then
  exec /usr/bin/sudo /bin/bash "$SCRIPT_DIR/install-host-service.sh"
fi
SERVICE_USER="${SUDO_USER:-}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == root ]]; then
  echo "ERRORE: avvia l'installer dal tuo account amministratore, non da una shell root." >&2
  exit 2
fi
SERVICE_GROUP="$(/usr/bin/id -gn "$SERVICE_USER")"
LABEL=ch.nmperformance.fithub.host
LOG_DIR="$PROJECT_ROOT/logs/system/monitormetrics"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
# Vecchi percorsi: usati soltanto per recuperare dati durante la migrazione.
LEGACY_DIR="/Library/Application Support/FitHub/monitormetrics"
LEGACY_LOG_DIR="/Library/Logs/FitHub"
URL_FILE="$SCRIPT_DIR/host-push-url.txt"
URL_SOURCE="$URL_FILE"
if [[ ! -e "$URL_FILE" && ! -L "$URL_FILE" ]]; then
  URL_SOURCE="$LEGACY_DIR/host-push-url.txt"
fi
if [[ ! -f "$URL_SOURCE" || -L "$URL_SOURCE" || ! -s "$URL_SOURCE" ]]; then
  echo "ERRORE: serve host-push-url.txt non vuoto nella cartella degli script." >&2
  exit 2
fi
# Convalidare prima di fermare il servizio; non stampare mai il token.
url="$(/bin/cat "$URL_SOURCE")" || exit 2
url="${url%$'\r'}"
url="${url%%\?*}"
if [[ ! "$url" =~ ^https?://[^/?#[:space:]]+/api/push/[A-Za-z0-9_-]+$ ]] ||
   [[ "$url" == *'"'* || "$url" == *'\'* || "$url" == *'@'* ]]; then
  echo "ERRORE: URL Push HOST non valido." >&2
  exit 2
fi
unset url
/bin/bash -n "$SCRIPT_DIR/host.sh"
STAGING="$(/usr/bin/mktemp -d /private/tmp/fithub-host.XXXXXX)"
trap '/bin/rm -f "$STAGING/service.plist" "$STAGING/status-daily.json" "$STAGING/status-weekly.json" "$STAGING/status-monthly.json"; /bin/rmdir "$STAGING"' EXIT

# Il plist separato è un riferimento per admin; qui i percorsi sono dinamici.
/bin/cat > "$STAGING/service.plist" <<'PLIST_XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>ch.nmperformance.fithub.host</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/bash</string>
		<string>/Users/admin/Documents/Fithub/scripts/monitormetrics/host.sh</string>
		<string>--service</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StartInterval</key>
	<integer>30</integer>
	<key>Umask</key>
	<integer>63</integer>
</dict>
</plist>
PLIST_XML
/usr/bin/plutil -insert UserName -string "$SERVICE_USER" "$STAGING/service.plist"
/usr/bin/plutil -remove ProgramArguments.1 "$STAGING/service.plist"
/usr/bin/plutil -insert ProgramArguments.1 -string "$SCRIPT_DIR/host.sh" "$STAGING/service.plist"
/usr/bin/plutil -lint "$STAGING/service.plist"

# Stessa label: sostituisce il vecchio servizio senza creare un secondo job.
if /bin/launchctl print "system/$LABEL" >/dev/null 2>&1; then
  /bin/launchctl bootout "system/$LABEL"
fi
/usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$SCRIPT_DIR" "$SCRIPT_DIR/host.sh"
/bin/chmod 700 "$SCRIPT_DIR" "$SCRIPT_DIR/host.sh"
if [[ "$URL_SOURCE" != "$URL_FILE" ]]; then
  /usr/bin/install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 600 "$URL_SOURCE" "$URL_FILE"
fi
/usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$URL_FILE"
/bin/chmod 600 "$URL_FILE"
# Conservare proprietario e permessi delle directory condivise già presenti.
for directory in "$PROJECT_ROOT/logs" "$PROJECT_ROOT/logs/system"; do
  if [[ ! -d "$directory" ]]; then
    /usr/bin/install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 755 "$directory"
  fi
done
/usr/bin/install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 700 "$LOG_DIR"
for filename in host.log host-error.log; do
  if [[ -L "$LOG_DIR/$filename" || ( -e "$LOG_DIR/$filename" && ! -f "$LOG_DIR/$filename" ) ]]; then
    echo "ERRORE: percorso log non valido: $LOG_DIR/$filename" >&2
    exit 2
  fi
  if [[ ! -e "$LOG_DIR/$filename" && -f "$LEGACY_LOG_DIR/$filename" && ! -L "$LEGACY_LOG_DIR/$filename" ]]; then
    /usr/bin/install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 600 "$LEGACY_LOG_DIR/$filename" "$LOG_DIR/$filename"
  fi
  /usr/bin/touch "$LOG_DIR/$filename"
  /usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$LOG_DIR/$filename"
  /bin/chmod 600 "$LOG_DIR/$filename"
done

/usr/bin/install -o root -g wheel -m 644 "$STAGING/service.plist" "$PLIST"
BASE_LOG_BYTES="$(/usr/bin/stat -f %z "$LOG_DIR/host.log")"
/bin/launchctl enable "system/$LABEL"
/bin/launchctl bootstrap system "$PLIST"

# bootstrap riuscito significa soltanto registrato: verificare il primo processo reale.
echo "Verifica del primo campione host tramite launchd..."
verified=0
for attempt in {1..25}; do
  service_info="$(/bin/launchctl print "system/$LABEL" 2>/dev/null)" || service_info=""
  if [[ "$service_info" == *'job state = spawn failed'* ]]; then
    echo "ERRORE: $LABEL non si avvia (spawn failed). Verificare accesso a Documents e configurazione launchd." >&2
    exit 2
  fi
  code="$(printf '%s\n' "$service_info" | /usr/bin/awk '/^[[:space:]]*last exit code =/ { sub(/:$/, "", $5); print $5; exit }')"
  if [[ "$code" =~ ^-?[0-9]+$ && "$service_info" != *'state = running'* ]]; then
    log_bytes="$(/usr/bin/stat -f %z "$LOG_DIR/host.log" 2>/dev/null)" || log_bytes=0
    if [[ "$code" == 0 || "$code" == 1 ]] && (( log_bytes <= BASE_LOG_BYTES )); then
      /bin/sleep 1
      continue
    fi
    case "$code" in
      0) echo "Primo campione host: lettura e invio riusciti."; verified=1; break ;;
      1) echo "Primo campione host: sensore eseguito, condizione DOWN rilevata."; verified=1; break ;;
      78|126|127)
        echo "ERRORE: host non eseguibile da launchd (codice $code). Controllare i permessi Privacy di /bin/bash e l'accesso a Documents." >&2
        exit 2 ;;
      *) echo "ERRORE: primo campione host fallito (codice $code). Leggere $LOG_DIR/host-error.log e $LOG_DIR/host.log." >&2; exit 2 ;;
    esac
  fi
  /bin/sleep 1
done
if (( verified == 0 )); then
  echo "ERRORE: primo campione host non verificato entro 25 secondi; controllare launchctl print e i log." >&2
  exit 2
fi

echo "Servizio HOST installato: ogni 30 secondi, utente $SERVICE_USER."
echo "Script: $SCRIPT_DIR/host.sh"
echo "Log: $LOG_DIR/host.log"
echo "Errori: $LOG_DIR/host-error.log"

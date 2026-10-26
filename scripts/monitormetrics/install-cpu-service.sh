#!/bin/bash
# Installa o aggiorna il servizio CPU sul Mac che esegue questo comando.
set -euo pipefail

if [[ "${1:-}" == --help ]]; then
  echo "Uso: /bin/bash install-cpu-service.sh"
  echo "Installa e avvia il monitor CPU al boot, ogni 15 secondi. Richiede sudo."
  echo "cpu.sh e cpu-push-url.txt devono essere nella stessa cartella."
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

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ ! -f "$SCRIPT_DIR/cpu.sh" || ! -s "$SCRIPT_DIR/cpu-push-url.txt" ]]; then
  echo "ERRORE: servono cpu.sh e cpu-push-url.txt non vuoto accanto all'installer." >&2
  exit 2
fi
if (( EUID != 0 )); then
  exec /usr/bin/sudo /bin/bash "$SCRIPT_DIR/install-cpu-service.sh"
fi

SERVICE_USER="${SUDO_USER:-}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == root ]]; then
  echo "ERRORE: avvia l'installer dal tuo account amministratore, non da una shell root." >&2
  exit 2
fi
SERVICE_GROUP="$(/usr/bin/id -gn "$SERVICE_USER")"
LABEL=ch.nmperformance.fithub.cpu
SERVICE_DIR="/Library/Application Support/FitHub/monitormetrics"
LOG_DIR=/Library/Logs/FitHub
PLIST="/Library/LaunchDaemons/$LABEL.plist"
STAGING="$(/usr/bin/mktemp -d /private/tmp/fithub-cpu.XXXXXX)"
trap '/bin/rm -f "$STAGING/service.plist"; /bin/rmdir "$STAGING"' EXIT

# Il plist è incorporato: basta trasferire questo installer accanto a cpu.sh.
/bin/cat > "$STAGING/service.plist" <<'PLIST_XML'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ch.nmperformance.fithub.cpu</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Library/Application Support/FitHub/monitormetrics/cpu.sh</string>
        <string>--once</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>15</integer>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>/Library/Logs/FitHub/cpu.log</string>
    <key>StandardErrorPath</key>
    <string>/Library/Logs/FitHub/cpu-error.log</string>
</dict>
</plist>
PLIST_XML
/usr/bin/plutil -insert UserName -string "$SERVICE_USER" "$STAGING/service.plist"
/usr/bin/plutil -lint "$STAGING/service.plist"
/bin/bash -n "$SCRIPT_DIR/cpu.sh"

# Ricarica soltanto il servizio CPU, anche quando era già installato.
if /bin/launchctl print "system/$LABEL" >/dev/null 2>&1; then
  /bin/launchctl bootout "system/$LABEL"
fi
/usr/bin/install -d -o root -g wheel -m 755 "$SERVICE_DIR"
/usr/bin/install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 700 "$LOG_DIR"
/usr/bin/install -o root -g wheel -m 755 "$SCRIPT_DIR/cpu.sh" "$SERVICE_DIR/cpu.sh"
/usr/bin/install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 600 \
  "$SCRIPT_DIR/cpu-push-url.txt" "$SERVICE_DIR/cpu-push-url.txt"
/usr/bin/touch "$LOG_DIR/cpu.log" "$LOG_DIR/cpu-error.log"
/usr/sbin/chown "$SERVICE_USER:$SERVICE_GROUP" "$LOG_DIR/cpu.log" "$LOG_DIR/cpu-error.log"
/bin/chmod 600 "$LOG_DIR/cpu.log" "$LOG_DIR/cpu-error.log"
/usr/bin/install -o root -g wheel -m 644 "$STAGING/service.plist" "$PLIST"
/bin/launchctl enable "system/$LABEL"
/bin/launchctl bootstrap system "$PLIST"

echo "Servizio CPU installato e avviato: campioni ogni 15 secondi, utente $SERVICE_USER."
echo "Avvio automatico ai prossimi boot; puoi chiudere il Terminale."
echo "Log: $LOG_DIR/cpu.log"
echo "Errori: $LOG_DIR/cpu-error.log"

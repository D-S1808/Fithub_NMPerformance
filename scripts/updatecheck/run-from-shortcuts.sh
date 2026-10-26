#!/bin/bash
# Comandi Rapidi riceve un rapporto leggibile anche quando il checker rileva errori.
# I codici originali restano disponibili con --strict-exit e in update-check.sh.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
LOG_DIR="$PROJECT_ROOT/logs/system/updatecheck"
STRICT_EXIT=0
if [[ "${1:-}" == --strict-exit ]]; then
  STRICT_EXIT=1
  shift
fi
WRITE_LOG=1
for argument in "$@"; do
  case "$argument" in --help|-h|--self-check|--diagnose-sources) WRITE_LOG=0 ;; esac
done
STARTED_AT="$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')"
code=0
output="$(/bin/bash "$SCRIPT_DIR/update-check.sh" "$@" 2>&1)" || code=$?
case "$code" in
  0) status="OK" ;;
  1) status="AGGIORNAMENTI / REVISIONE RICHIESTI" ;;
  2) status="ERRORE: uno o piu controlli/invii non conclusi" ;;
  *) status="ERRORE DI ESECUZIONE" ;;
esac
details=""
# Leggere i dettagli solo se questa esecuzione ha dichiarato il report: un errore
# di runtime/configurazione non deve riproporre un vecchio report come attuale.
if (( code == 2 )) && [[ "$output" == *"Report: $LOG_DIR/latest.txt"* ]] &&
   [[ -f "$LOG_DIR/latest.txt" && ! -L "$LOG_DIR/latest.txt" && -r "$LOG_DIR/latest.txt" ]]; then
  details="$(/usr/bin/awk '/^\[ERROR\]/{p=1} p{print} p && /^$/{p=0}' "$LOG_DIR/latest.txt")" || details=""
fi
report="$(printf 'Esito checker: %s\nCodice checker: %s\nAvvio: %s\n\n%s\n' "$status" "$code" "$STARTED_AT" "$output")"
if [[ -n "$details" ]]; then
  report="$(printf '%s\n\nDettaglio dei controlli falliti:\n%s\n' "$report" "$details")"
fi
if (( code == 2 )); then
  report="$(printf '%s\n\nIl rapporto contiene errori: verificare i dettagli. Lo stato Kuma non viene convertito in UP.\n' "$report")"
fi
# L'azione successiva "Mostra risultato" puo leggere anche stderr e il codice reale.
printf '%s\n' "$report"
if (( WRITE_LOG )); then
  LOG_FILE="$LOG_DIR/shortcut-last-run.log"
  if [[ -L "$LOG_DIR" || -L "$LOG_FILE" || ( -e "$LOG_FILE" && ! -f "$LOG_FILE" ) ]]; then
    echo "AVVISO: log Shortcuts non salvato: percorso non valido."
  elif ! { /bin/mkdir -p "$LOG_DIR" && printf '%s\n' "$report" > "$LOG_FILE" && /bin/chmod 600 "$LOG_FILE"; } 2>/dev/null; then
    echo "AVVISO: log Shortcuts non salvato; verificare accesso alla cartella del progetto."
  fi
fi
if (( STRICT_EXIT )); then
  exit "$code"
fi
case "$code" in
  0|1|2) exit 0 ;; # rapporto consegnato; lo stato dei controlli resta nel rapporto/Kuma
  *) exit "$code" ;; # crash, permessi o eseguibile mancante restano errori di esecuzione
esac

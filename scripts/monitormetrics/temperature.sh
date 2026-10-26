#!/bin/bash
# TEMPERATURA dell'host macOS: un campione ogni 30 secondi, Push Kuma opzionale.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
INTERVAL=30
ONCE=0
LOCAL_ONLY=0
SERVICE_MODE=0

usage() {
  cat <<'EOF'
Uso: /bin/bash temperature.sh [--once | --dry-run]

  senza opzioni  Legge lo stato termico e invia a Kuma ogni 30 secondi; Ctrl+C termina.
  --once         Esegue un solo campione, con invio a Kuma se configurato.
  --dry-run      Esegue un solo campione locale, senza inviare a Kuma.
  --service      Campione singolo con log nel progetto, usato da launchd.
  --help         Mostra questo messaggio.

Stati: nominal = UP; fair = AVVISO (UP); serious/critical = DOWN.
Il valore è lo stato termico macOS, non una temperatura in gradi Celsius.
Codici: 0 = OK/AVVISO; 1 = stato serious/critical; 2 = errore di lettura/invio/configurazione.

URL Push: variabile KUMA_TEMPERATURE_PUSH_URL oppure file temperature-push-url.txt
nella stessa cartella dello script. Senza URL mostra solo lo stato termico nel Terminale.
EOF
}

case "${1:-}" in
  "") ;;
  --once) ONCE=1 ;;
  --service) ONCE=1; SERVICE_MODE=1 ;;
  --dry-run) ONCE=1; LOCAL_ONLY=1 ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi
if [[ "$(/usr/bin/uname -s)" != Darwin ]]; then
  echo "ERRORE: eseguire questo script direttamente su macOS." >&2
  exit 2
fi

# launchd avvia solo Bash: i log sotto Documents vengono aperti dal processo utente.
if (( SERVICE_MODE )); then
  umask 077
  LOG_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)/logs/system/monitormetrics"
  if ! { exec 3>>"$LOG_DIR/temperature-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.temperature "FitHub: impossibile aprire i log in Documents; verificare i permessi Privacy di /bin/bash."
    echo "ERRORE: log non accessibili; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  if ! { exec >>"$LOG_DIR/temperature.log"; }; then
    echo "ERRORE: log non accessibile; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
fi

push_url=""
if (( LOCAL_ONLY == 0 )); then
  push_url="${KUMA_TEMPERATURE_PUSH_URL:-}"
  if [[ -z "$push_url" && -f "$SCRIPT_DIR/temperature-push-url.txt" ]]; then
    if [[ ! -r "$SCRIPT_DIR/temperature-push-url.txt" ]]; then
      echo "ERRORE: temperature-push-url.txt non leggibile; verifica proprietario e permessi." >&2
      exit 2
    fi
    push_url="$(/bin/cat "$SCRIPT_DIR/temperature-push-url.txt")" || exit 2
    push_url="${push_url%$'\r'}"
    if [[ -z "$push_url" ]]; then
      echo "ERRORE: temperature-push-url.txt è vuoto." >&2
      exit 2
    fi
  fi
  # Accetta anche l'URL completo copiato da Kuma; i parametri sono ricreati.
  push_url="${push_url%%\?*}"
  if [[ -n "$push_url" ]]; then
    if [[ ! "$push_url" =~ ^https?://[^/?#[:space:]]+/api/push/[A-Za-z0-9_-]+$ ]] ||
       [[ "$push_url" == *'"'* || "$push_url" == *'\'* || "$push_url" == *'@'* ]]; then
      echo "ERRORE: URL Push non valido; usare http(s)://HOST/api/push/TOKEN." >&2
      exit 2
    fi
  fi
fi

if (( LOCAL_ONLY == 0 )) && [[ -z "$push_url" ]]; then
  echo "AVVISO: URL Kuma TEMPERATURA non configurato; solo lettura locale." >&2
fi

read_temperature() {
  local state
  # JXA accede solo all API Foundation: nessuna automazione di altre app.
  # Verificare disponibilità ed esito: un valore sconosciuto non equivale a nominal.
  state="$(/usr/bin/osascript -l JavaScript -e '
    ObjC.import("Foundation");
    var info = $.NSProcessInfo.processInfo;
    if (!info.respondsToSelector("thermalState")) {
      throw new Error("thermalState non disponibile");
    }
    Number(info.thermalState);
  ' 2>/dev/null)" || return 1
  [[ "$state" =~ ^[0-3]$ ]] || return 1
  printf '%s' "$state"
}

send_push() {
  local status="$1" message="$2" response accepted http_code
  # URL su stdin: il token non compare negli argomenti di curl o nei log.
  # Nessun redirect: una pagina di login/Cloudflare Access deve essere un errore.
  response="$(printf 'url = "%s"\n' "$push_url" | /usr/bin/curl -q \
    --config - --silent --fail --connect-timeout 5 --max-time 10 \
    --get --data-urlencode "status=$status" --data-urlencode "msg=$message" \
    --write-out '\n%{http_code}' \
    2>/dev/null)" || return 1
  http_code="${response##*$'\n'}"
  [[ "$http_code" =~ ^2[0-9][0-9]$ ]] || return 1
  response="${response%$'\n'*}"
  # HTTP 200 da solo non basta: Kuma deve confermare {"ok": true}.
  accepted="$(printf '%s' "$response" | /usr/bin/plutil -extract ok raw -o - - 2>/dev/null)" || return 1
  [[ "$accepted" == true ]]
}

sample_and_report() {
  local state status message result=0 timestamp
  if state="$(read_temperature)"; then
    status=up
    case "$state" in
      0) message="Stato termico nominal - OK" ;;
      1) message="Stato termico fair - AVVISO: pressione termica moderata" ;;
      2)
        status=down
        message="Stato termico serious - CRITICO: pressione termica elevata"
        result=1
        ;;
      3)
        status=down
        message="Stato termico critical - CRITICO: pressione termica critica"
        result=1
        ;;
    esac
  else
    status=down
    message="Temperatura: lettura dello stato termico non riuscita"
    result=2
  fi
  timestamp="$(/bin/date '+%Y-%m-%d %H:%M:%S %Z')"
  printf '%s | %s\n' "$timestamp" "$message"
  if [[ -n "$push_url" ]]; then
    if ! send_push "$status" "$message"; then
      echo "ERRORE: invio a Uptime Kuma fallito o risposta non valida." >&2
      result=2
    fi
  fi
  return "$result"
}

if (( ONCE )); then
  sample_and_report
  exit $?
fi

trap 'exit 0' INT TERM
if [[ -n "$push_url" ]]; then
  echo "Monitor TEMPERATURA ogni ${INTERVAL}s; invio a Uptime Kuma attivo. Ctrl+C per terminare."
else
  echo "Monitor TEMPERATURA ogni ${INTERVAL}s; visualizzazione locale. Ctrl+C per terminare."
fi
next_sample=$SECONDS
while :; do
  # Un errore non interrompe i campioni successivi.
  sample_and_report || :
  next_sample=$((next_sample + INTERVAL))
  if (( next_sample <= SECONDS )); then
    next_sample=$((SECONDS + INTERVAL))
  fi
  /bin/sleep "$((next_sample - SECONDS))"
done

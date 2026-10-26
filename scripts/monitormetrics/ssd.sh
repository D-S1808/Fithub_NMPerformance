#!/bin/bash
# SSD dell'host macOS: un campione ogni 60 secondi, Push Kuma opzionale.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
INTERVAL=60
ONCE=0
LOCAL_ONLY=0
SERVICE_MODE=0

usage() {
  cat <<'EOF'
Uso: /bin/bash ssd.sh [--once | --dry-run]

  senza opzioni  Legge lo spazio SSD e invia a Kuma ogni 60 secondi; Ctrl+C termina.
  --once         Esegue un solo campione, con invio a Kuma se configurato.
  --dry-run      Esegue un solo campione locale, senza inviare a Kuma.
  --service      Campione singolo con log nel progetto, usato da launchd.
  --help         Mostra questo messaggio.

Soglie: SSD >85% = AVVISO (UP); SSD >90% = CRITICO (DOWN).
Disco monitorato: contenitore APFS dell’SSD interno, volume /System/Volumes/Data.
Codici: 0 = OK/AVVISO; 1 = soglia critica superata; 2 = errore di lettura/invio/configurazione.

URL Push: variabile KUMA_SSD_PUSH_URL oppure file ssd-push-url.txt
nella stessa cartella dello script. Senza URL mostra solo lo spazio SSD nel Terminale.
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
  if ! { exec 3>>"$LOG_DIR/ssd-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.ssd "FitHub: impossibile aprire i log in Documents; verificare i permessi Privacy di /bin/bash."
    echo "ERRORE: log non accessibili; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  if ! { exec >>"$LOG_DIR/ssd.log"; }; then
    echo "ERRORE: log non accessibile; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
fi

push_url=""
if (( LOCAL_ONLY == 0 )); then
  push_url="${KUMA_SSD_PUSH_URL:-}"
  if [[ -z "$push_url" && -f "$SCRIPT_DIR/ssd-push-url.txt" ]]; then
    if [[ ! -r "$SCRIPT_DIR/ssd-push-url.txt" ]]; then
      echo "ERRORE: ssd-push-url.txt non leggibile; verifica proprietario e permessi." >&2
      exit 2
    fi
    push_url="$(/bin/cat "$SCRIPT_DIR/ssd-push-url.txt")" || exit 2
    push_url="${push_url%$'\r'}"
    if [[ -z "$push_url" ]]; then
      echo "ERRORE: ssd-push-url.txt è vuoto." >&2
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
  echo "AVVISO: URL Kuma SSD non configurato; solo lettura locale." >&2
fi

read_ssd() {
  local info total free filesystem internal solid_state mount_point
  info="$(/usr/sbin/diskutil info -plist /System/Volumes/Data 2>/dev/null)" || return 1
  filesystem="$(printf '%s' "$info" | /usr/bin/plutil -extract FilesystemType raw -o - - 2>/dev/null)" || return 1
  internal="$(printf '%s' "$info" | /usr/bin/plutil -extract Internal raw -o - - 2>/dev/null)" || return 1
  solid_state="$(printf '%s' "$info" | /usr/bin/plutil -extract SolidState raw -o - - 2>/dev/null)" || return 1
  mount_point="$(printf '%s' "$info" | /usr/bin/plutil -extract MountPoint raw -o - - 2>/dev/null)" || return 1
  [[ "$filesystem" == apfs && "$internal" == true && "$solid_state" == true &&
     "$mount_point" == /System/Volumes/Data ]] || return 1
  # APFS condivide lo spazio tra i volumi: leggere i contatori del contenitore.
  # FreeSpace/VolumeSize del singolo volume possono essere zero anche se il
  # contenitore ha spazio disponibile. Non usare questi campi come fallback.
  total="$(printf '%s' "$info" | /usr/bin/plutil -extract APFSContainerSize raw -o - - 2>/dev/null)" || return 1
  free="$(printf '%s' "$info" | /usr/bin/plutil -extract APFSContainerFree raw -o - - 2>/dev/null)" || return 1
  [[ "$total" =~ ^[1-9][0-9]*$ && "$free" =~ ^[0-9]+$ ]] || return 1
  /usr/bin/awk -v total="$total" -v free="$free" 'BEGIN {
    total += 0
    free += 0
    if (total <= 0 || free > total) exit 1
    used = total - free
    # Soglie strettamente maggiori, confrontate prima di arrotondare.
    level = (used * 100 > total * 90) ? "critical" : ((used * 100 > total * 85) ? "warning" : "ok")
    printf "%.2f|%.2f|%.2f|%.2f|%s", used * 100 / total, used / 1073741824, total / 1073741824, free / 1073741824, level
  }'
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
  local reading percent used total free level status message result=0 timestamp
  if reading="$(read_ssd)"; then
    IFS='|' read -r percent used total free level <<< "$reading"
    status=up
    message="SSD ${percent}% (${used}/${total} GiB) - liberi ${free} GiB"
    case "$level" in
      critical)
        status=down
        message="$message - CRITICO: soglia 90% superata"
        result=1
        ;;
      warning)
        message="$message - AVVISO: soglia 85% superata"
        ;;
    esac
  else
    status=down
    message="SSD: lettura non riuscita"
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
  echo "Monitor SSD ogni ${INTERVAL}s; invio a Uptime Kuma attivo. Ctrl+C per terminare."
else
  echo "Monitor SSD ogni ${INTERVAL}s; visualizzazione locale. Ctrl+C per terminare."
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

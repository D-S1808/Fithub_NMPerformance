#!/bin/bash
# RAM dell'host macOS: un campione ogni 15 secondi, Push Kuma opzionale.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
INTERVAL=15
ONCE=0
LOCAL_ONLY=0
SERVICE_MODE=0

usage() {
  cat <<'EOF'
Uso: /bin/bash ram.sh [--once | --dry-run]

  senza opzioni  Legge la RAM e invia a Kuma ogni 15 secondi; Ctrl+C termina.
  --once         Esegue un solo campione, con invio a Kuma se configurato.
  --dry-run      Esegue un solo campione locale, senza inviare a Kuma.
  --service      Campione singolo con log nel progetto, usato da launchd.
  --help         Mostra questo messaggio.

Soglia: RAM >90% invia DOWN; RAM <=90% invia UP.
Codici: 0 = OK; 1 = soglia superata; 2 = errore di lettura/invio/configurazione.

URL Push: variabile KUMA_RAM_PUSH_URL oppure file ram-push-url.txt
nella stessa cartella dello script. Senza URL mostra solo la RAM nel Terminale.
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
  if ! { exec 3>>"$LOG_DIR/ram-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.ram "FitHub: impossibile aprire i log in Documents; verificare i permessi Privacy di /bin/bash."
    echo "ERRORE: log non accessibili; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  if ! { exec >>"$LOG_DIR/ram.log"; }; then
    echo "ERRORE: log non accessibile; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
fi

push_url=""
if (( LOCAL_ONLY == 0 )); then
  push_url="${KUMA_RAM_PUSH_URL:-}"
  if [[ -z "$push_url" && -f "$SCRIPT_DIR/ram-push-url.txt" ]]; then
    if [[ ! -r "$SCRIPT_DIR/ram-push-url.txt" ]]; then
      echo "ERRORE: ram-push-url.txt non leggibile; verifica proprietario e permessi." >&2
      exit 2
    fi
    push_url="$(/bin/cat "$SCRIPT_DIR/ram-push-url.txt")" || exit 2
    push_url="${push_url%$'\r'}"
    if [[ -z "$push_url" ]]; then
      echo "ERRORE: ram-push-url.txt è vuoto." >&2
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
  echo "AVVISO: URL Kuma RAM non configurato; solo lettura locale." >&2
fi

read_ram() {
  local total samples
  total="$(/usr/sbin/sysctl -n hw.memsize 2>/dev/null)" || return 1
  [[ "$total" =~ ^[1-9][0-9]*$ ]] || return 1
  samples="$(/usr/bin/vm_stat 2>/dev/null)" || return 1
  # Stima della RAM impegnata: anonima - purgeable + wired + compressore fisico.
  # La cache file non entra nel conteggio. Le pagine memorizzate nel compressore
  # sono logiche: usare "occupied", non "stored", per evitare il doppio conteggio.
  printf '%s\n' "$samples" | /usr/bin/awk -v total="$total" '
    /^Mach Virtual Memory Statistics:/ {
      headers++
      page_size = $0
      sub(/^.*page size of /, "", page_size)
      sub(/ bytes.*$/, "", page_size)
      if (page_size !~ /^[0-9]+$/ || page_size + 0 <= 0) invalid = 1
    }
    /^(Anonymous pages|Pages wired down|Pages occupied by compressor|Pages purgeable):/ {
      key = $0
      sub(/:.*/, "", key)
      value = $0
      sub(/^[^:]*:[[:space:]]*/, "", value)
      sub(/[.]?[[:space:]]*$/, "", value)
      if (value !~ /^[0-9]+$/) invalid = 1
      pages[key] = value + 0
      seen[key]++
    }
    END {
      if (invalid || headers != 1 ||
          seen["Anonymous pages"] != 1 || seen["Pages wired down"] != 1 ||
          seen["Pages occupied by compressor"] != 1 || seen["Pages purgeable"] != 1 ||
          pages["Pages purgeable"] > pages["Anonymous pages"]) exit 1
      used_pages = pages["Anonymous pages"] - pages["Pages purgeable"]
      used_pages += pages["Pages wired down"] + pages["Pages occupied by compressor"]
      used = used_pages * page_size
      if (used < 0 || used > total + 0) exit 1
      # La soglia usa il valore non arrotondato: esattamente 90% resta UP.
      status = (used * 100 > total * 90) ? "down" : "up"
      printf "%.2f|%.2f|%.2f|%s", used * 100 / total, used / 1073741824, total / 1073741824, status
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
  local reading percent used total status message result=0 timestamp
  if reading="$(read_ram)"; then
    IFS='|' read -r percent used total status <<< "$reading"
    message="RAM ${percent}% (${used}/${total} GiB)"
    if [[ "$status" == down ]]; then
      message="$message - CRITICO: soglia 90% superata"
      result=1
    fi
  else
    status=down
    message="RAM: lettura non riuscita"
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
  echo "Monitor RAM ogni ${INTERVAL}s; invio a Uptime Kuma attivo. Ctrl+C per terminare."
else
  echo "Monitor RAM ogni ${INTERVAL}s; visualizzazione locale. Ctrl+C per terminare."
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

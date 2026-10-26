#!/bin/bash
# DOCKER dell'host macOS: un campione ogni 30 secondi, Push Kuma opzionale.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
INTERVAL=30
ONCE=0
LOCAL_ONLY=0
SERVICE_MODE=0

usage() {
  cat <<'EOF'
Uso: /bin/bash docker.sh [--once | --dry-run]

  senza opzioni  Controlla il daemon Docker e invia a Kuma ogni 30 secondi; Ctrl+C termina.
  --once         Esegue un solo campione, con invio a Kuma se configurato.
  --dry-run      Esegue un solo campione locale, senza inviare a Kuma.
  --service      Campione singolo con log nel progetto, usato da launchd.
  --help         Mostra questo messaggio.

Stati: API Docker locale raggiungibile = UP; daemon/socket non raggiungibile = DOWN.
Usa il socket Docker Desktop dell'account che esegue lo script, o /var/run/docker.sock.
Codici: 0 = daemon raggiungibile; 1 = daemon non raggiungibile; 2 = errore di lettura/invio/configurazione.

URL Push: variabile KUMA_DOCKER_PUSH_URL oppure file docker-push-url.txt
nella stessa cartella dello script. Senza URL mostra solo lo stato Docker nel Terminale.
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
  if ! { exec 3>>"$LOG_DIR/docker-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.docker "FitHub: impossibile aprire i log in Documents; verificare i permessi Privacy di /bin/bash."
    echo "ERRORE: log non accessibili; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  if ! { exec >>"$LOG_DIR/docker.log"; }; then
    echo "ERRORE: log non accessibile; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
fi

push_url=""
if (( LOCAL_ONLY == 0 )); then
  push_url="${KUMA_DOCKER_PUSH_URL:-}"
  if [[ -z "$push_url" && -f "$SCRIPT_DIR/docker-push-url.txt" ]]; then
    if [[ ! -r "$SCRIPT_DIR/docker-push-url.txt" ]]; then
      echo "ERRORE: docker-push-url.txt non leggibile; verifica proprietario e permessi." >&2
      exit 2
    fi
    push_url="$(/bin/cat "$SCRIPT_DIR/docker-push-url.txt")" || exit 2
    push_url="${push_url%$'\r'}"
    if [[ -z "$push_url" ]]; then
      echo "ERRORE: docker-push-url.txt è vuoto." >&2
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
  echo "AVVISO: URL Kuma DOCKER non configurato; solo lettura locale." >&2
fi

find_docker_socket() {
  local username directory_info account_home candidate
  username="$(/usr/bin/id -un)" || return 1
  # La home deriva dall'account effettivo, senza dipendere da HOME del Terminale.
  # Docker Desktop normalmente espone il socket nella home del proprio utente.
  if directory_info="$(/usr/bin/dscl . -read "/Users/$username" NFSHomeDirectory 2>/dev/null)"; then
    account_home="${directory_info#NFSHomeDirectory: }"
    if [[ "$account_home" == /* && "$account_home" != *$'\n'* ]]; then
      candidate="$account_home/.docker/run/docker.sock"
      if [[ -S "$candidate" ]]; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
  fi
  candidate=/var/run/docker.sock
  if [[ -S "$candidate" ]]; then
    printf '%s' "$candidate"
    return 0
  fi
  return 1
}

check_docker() {
  local socket response http_code body
  if ! socket="$(find_docker_socket)"; then
    printf '%s' "Docker: socket locale assente o non accessibile - DOWN"
    return 1
  fi
  # GET /_ping è una lettura dell'Engine API, senza modificare container o daemon.
  # Il socket Unix rende il controllo locale; il proxy è escluso esplicitamente.
  if ! response="$(/usr/bin/curl -q --silent --fail --noproxy '*' \
    --unix-socket "$socket" --connect-timeout 3 --max-time 10 \
    --write-out '\n%{http_code}' http://localhost/_ping 2>/dev/null)"; then
    printf '%s' "Docker: daemon non raggiungibile (arresto, timeout o permessi socket) - DOWN"
    return 1
  fi
  http_code="${response##*$'\n'}"
  body="${response%$'\n'*}"
  if [[ "$http_code" != 200 || "$body" != OK ]]; then
    printf '%s' "Docker: risposta API locale non valida - DOWN"
    return 1
  fi
  printf '%s' "Docker: daemon raggiungibile - API OK"
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
  local status message result=0 timestamp
  if message="$(check_docker)"; then
    status=up
  else
    status=down
    result=1
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
  echo "Monitor DOCKER ogni ${INTERVAL}s; invio a Uptime Kuma attivo. Ctrl+C per terminare."
else
  echo "Monitor DOCKER ogni ${INTERVAL}s; visualizzazione locale. Ctrl+C per terminare."
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

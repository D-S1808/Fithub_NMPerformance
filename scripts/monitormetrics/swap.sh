#!/bin/bash
# SWAP dell'host macOS: un campione ogni 30 secondi, Push Kuma opzionale.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
STATE_DIR="$SCRIPT_DIR/swap-state"
STATE_FILE="$STATE_DIR/history"
STATE_TEMP=""
INTERVAL=30
WARN_MIB=1024
CRITICAL_MIB=2048
CRITICAL_SECONDS=300
MAX_SAMPLE_GAP=75
umask 077
ONCE=0
LOCAL_ONLY=0
SERVICE_MODE=0

usage() {
  cat <<'EOF'
Uso: /bin/bash swap.sh [--once | --dry-run]

  senza opzioni  Legge lo Swap e invia a Kuma ogni 30 secondi; Ctrl+C termina.
  --once         Esegue un solo campione, con invio a Kuma se configurato.
  --dry-run      Esegue un solo campione locale, senza inviare a Kuma.
  --service      Campione singolo con log nel progetto, usato da launchd.
  --help         Mostra questo messaggio.

Soglie: Swap >1 GiB = AVVISO; >2 GiB per almeno 300s = CRITICO (DOWN).
Il conteggio persiste tra le esecuzioni e si azzera al riavvio o dopo un gap >75s.
Codici: 0 = OK/AVVISO; 1 = soglia critica superata; 2 = errore di lettura/invio/configurazione.

URL Push: variabile KUMA_SWAP_PUSH_URL oppure file swap-push-url.txt
nella stessa cartella dello script. Senza URL mostra solo lo Swap nel Terminale.
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
  if ! { exec 3>>"$LOG_DIR/swap-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.swap "FitHub: impossibile aprire i log in Documents; verificare i permessi Privacy di /bin/bash."
    echo "ERRORE: log non accessibili; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  if ! { exec >>"$LOG_DIR/swap.log"; }; then
    echo "ERRORE: log non accessibile; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
fi

push_url=""
if (( LOCAL_ONLY == 0 )); then
  if [[ -L "$STATE_DIR" ]]; then
    echo "ERRORE: la cartella di stato Swap non può essere un link simbolico." >&2
    exit 2
  fi
  push_url="${KUMA_SWAP_PUSH_URL:-}"
  if [[ -z "$push_url" && -f "$SCRIPT_DIR/swap-push-url.txt" ]]; then
    if [[ ! -r "$SCRIPT_DIR/swap-push-url.txt" ]]; then
      echo "ERRORE: swap-push-url.txt non leggibile; verifica proprietario e permessi." >&2
      exit 2
    fi
    push_url="$(/bin/cat "$SCRIPT_DIR/swap-push-url.txt")" || exit 2
    push_url="${push_url%$'\r'}"
    if [[ -z "$push_url" ]]; then
      echo "ERRORE: swap-push-url.txt è vuoto." >&2
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
  echo "AVVISO: URL Kuma SWAP non configurato; solo lettura locale." >&2
fi

# lockf mantiene il lock sul descrittore aperto dal processo shell; il sistema
# lo rilascia anche dopo un crash. Il file .lock resta sul disco e non va rimosso.
if (( LOCAL_ONLY == 0 )); then
  if ! /bin/mkdir -p -m 700 "$STATE_DIR"; then
    echo "ERRORE: impossibile creare la cartella di stato Swap." >&2
    exit 2
  fi
  if ! { exec 9>"$STATE_DIR/.lock"; } || ! /usr/bin/lockf -s -t 0 9; then
    echo "ERRORE: stato Swap non accessibile o un altro monitor è già in esecuzione." >&2
    exit 2
  fi
fi
cleanup() {
  if [[ -n "$STATE_TEMP" ]]; then /bin/rm -f "$STATE_TEMP"; fi
}
trap cleanup EXIT

read_swap() {
  local info
  info="$(/usr/sbin/sysctl -n vm.swapusage 2>/dev/null)" || return 1
  printf '%s\n' "$info" | /usr/bin/awk -v warn="$WARN_MIB" -v critical="$CRITICAL_MIB" '
    BEGIN {
      factor["B"] = 1; factor["K"] = 1024; factor["M"] = 1048576
      factor["G"] = 1073741824; factor["T"] = 1099511627776
    }
    {
      for (i = 1; i <= NF; i++) {
        if ($i == "total" || $i == "used" || $i == "free") {
          key = $i
          value = $(i + 2)
          if ($(i + 1) != "=" || value !~ /^[0-9]+([.][0-9]+)?[BKMGT]$/) { invalid = 1; continue }
          unit = substr(value, length(value), 1)
          bytes[key] = (substr(value, 1, length(value) - 1) + 0) * factor[unit]
          if (factor[unit] > quantum) quantum = factor[unit]
          seen[key]++
        }
      }
    }
    END {
      if (invalid || seen["total"] != 1 || seen["used"] != 1 || seen["free"] != 1) exit 1
      total = bytes["total"]; used = bytes["used"]; free = bytes["free"]
      difference = total - used - free
      if (difference < 0) difference = -difference
      # sysctl arrotonda ogni valore a due decimali: tollerare solo quel margine.
      if (used > total || free > total || difference > quantum * 0.02) exit 1
      percent = (total > 0) ? used * 100 / total : 0
      level = (used > critical * 1048576) ? "high" : ((used > warn * 1048576) ? "warning" : "normal")
      printf "%.2f|%.2f|%.2f|%.2f|%s", percent, used / 1073741824, total / 1073741824, free / 1073741824, level
    }'
}

read_boot() {
  local info
  info="$(/usr/sbin/sysctl -n kern.boottime 2>/dev/null)" || return 1
  [[ "$info" =~ ^\{[[:space:]]*sec[[:space:]]*=[[:space:]]*([0-9]+), ]] || return 1
  printf '%s' "${BASH_REMATCH[1]}"
}

load_history() {
  local version stored_boot first last extra
  HIGH_SINCE=0
  [[ -s "$STATE_FILE" ]] || return 0
  if ! read -r version stored_boot first last extra < "$STATE_FILE" ||
     [[ "$version" != 1 || -n "$extra" ||
        ! "$stored_boot" =~ ^[0-9]{1,12}$ || ! "$first" =~ ^[0-9]{1,12}$ ||
        ! "$last" =~ ^[0-9]{1,12}$ ]]; then
    echo "AVVISO: cronologia Swap non valida; conteggio azzerato." >&2
    return 0
  fi
  stored_boot=$((10#$stored_boot)); first=$((10#$first)); last=$((10#$last))
  if (( stored_boot == boot && first >= boot && first <= last &&
        last <= now && now - last <= MAX_SAMPLE_GAP )); then
    HIGH_SINCE=$first
  fi
}

save_history() {
  (( LOCAL_ONLY == 0 )) || return 0
  [[ ! -d "$STATE_FILE" && ! -L "$STATE_FILE" ]] || return 1
  STATE_TEMP="$(/usr/bin/mktemp "$STATE_DIR/.history.XXXXXX")" || return 1
  if ! printf '1 %s %s %s\n' "$1" "$2" "$3" > "$STATE_TEMP" ||
     ! /bin/mv -f "$STATE_TEMP" "$STATE_FILE"; then
    /bin/rm -f "$STATE_TEMP"
    STATE_TEMP=""
    return 1
  fi
  STATE_TEMP=""
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
  local boot now HIGH_SINCE=0 elapsed=0
  if reading="$(read_swap)" && boot="$(read_boot)" && now="$(/bin/date +%s)" &&
     [[ "$boot" =~ ^[1-9][0-9]{0,11}$ && "$now" =~ ^[1-9][0-9]{0,11}$ ]] &&
     (( now >= boot )); then
    IFS='|' read -r percent used total free level <<< "$reading"
    status=up
    message="SWAP ${percent}% (${used}/${total} GiB) - liberi ${free} GiB"
    load_history
    if [[ "$level" == high ]]; then
      if (( HIGH_SINCE == 0 )); then HIGH_SINCE=$now; fi
      elapsed=$((now - HIGH_SINCE))
      if (( elapsed >= CRITICAL_SECONDS )); then
        status=down
        message="$message - CRITICO: oltre $CRITICAL_MIB MiB da ${elapsed}s"
        result=1
      else
        message="$message - AVVISO: oltre $CRITICAL_MIB MiB da ${elapsed}/${CRITICAL_SECONDS}s"
      fi
    else
      HIGH_SINCE=0
      if [[ "$level" == warning ]]; then
        message="$message - AVVISO: oltre $WARN_MIB MiB"
      fi
    fi
    if ! save_history "$boot" "$HIGH_SINCE" "$now"; then
      status=down
      message="$message - ERRORE: cronologia Swap non salvabile"
      result=2
    fi
  else
    status=down
    message="SWAP: lettura non riuscita"
    result=2
    # Una lettura fallita interrompe la continuità della soglia alta.
    save_history 0 0 0 || :
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
  echo "Monitor SWAP ogni ${INTERVAL}s; invio a Uptime Kuma attivo. Ctrl+C per terminare."
else
  echo "Monitor SWAP ogni ${INTERVAL}s; visualizzazione locale. Ctrl+C per terminare."
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

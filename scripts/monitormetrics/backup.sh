#!/bin/bash
# BACKUP dell'host macOS: un campione ogni 60 secondi, Push Kuma opzionale.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
INTERVAL=60
STATUS_DIR="$SCRIPT_DIR"
WARN_SECONDS=108000
MAX_AGE_SECONDS=129600
ONCE=0
LOCAL_ONLY=0
SERVICE_MODE=0

usage() {
  cat <<'EOF'
Uso: /bin/bash backup.sh [--once | --dry-run]

  senza opzioni  Controlla lo stato dei backup e invia a Kuma ogni 60 secondi; Ctrl+C termina.
  --once         Esegue un solo campione, con invio a Kuma se configurato.
  --dry-run      Esegue un solo campione locale, senza inviare a Kuma.
  --service      Campione singolo con log nel progetto, usato da launchd.
  --help         Mostra questo messaggio.

Soglie: oltre 30 ore = AVVISO (UP); oltre 36 ore = DOWN.
Ultimo tentativo fallito, nessun backup o stato non leggibile/valido = DOWN.
Codici: 0 = OK/AVVISO; 1 = backup assente/scaduto/fallito; 2 = stato non valido o errore di invio/configurazione.

URL Push: variabile KUMA_BACKUP_PUSH_URL oppure file backup-push-url.txt
nella stessa cartella dello script. Senza URL mostra solo lo stato backup nel Terminale.
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
  if ! { exec 3>>"$LOG_DIR/backup-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.backup "FitHub: impossibile aprire i log in Documents; verificare i permessi Privacy di /bin/bash."
    echo "ERRORE: log non accessibili; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  if ! { exec >>"$LOG_DIR/backup.log"; }; then
    echo "ERRORE: log non accessibile; verificare permessi e Privacy di macOS." >&2
    exit 78
  fi
fi

push_url=""
if (( LOCAL_ONLY == 0 )); then
  push_url="${KUMA_BACKUP_PUSH_URL:-}"
  if [[ -z "$push_url" && -f "$SCRIPT_DIR/backup-push-url.txt" ]]; then
    if [[ ! -r "$SCRIPT_DIR/backup-push-url.txt" ]]; then
      echo "ERRORE: backup-push-url.txt non leggibile; verifica proprietario e permessi." >&2
      exit 2
    fi
    push_url="$(/bin/cat "$SCRIPT_DIR/backup-push-url.txt")" || exit 2
    push_url="${push_url%$'\r'}"
    if [[ -z "$push_url" ]]; then
      echo "ERRORE: backup-push-url.txt è vuoto." >&2
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
  echo "AVVISO: URL Kuma BACKUP non configurato; solo lettura locale." >&2
fi

read_backup() {
  /usr/bin/osascript -l JavaScript -e '
    ObjC.import("Foundation");
    function parseTime(value) {
      if (typeof value !== "string") throw new Error("timestamp");
      var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
      if (!m) throw new Error("timestamp");
      var y = Number(m[1]), mo = Number(m[2]), d = Number(m[3]);
      var h = Number(m[4]), mi = Number(m[5]), sec = Number(m[6]);
      var ms = Number(((m[7] || "") + "000").slice(0, 3));
      var local = Date.UTC(y, mo - 1, d, h, mi, sec, ms);
      var check = new Date(local);
      if (check.getUTCFullYear() !== y || check.getUTCMonth() !== mo - 1 ||
          check.getUTCDate() !== d || check.getUTCHours() !== h ||
          check.getUTCMinutes() !== mi || check.getUTCSeconds() !== sec) throw new Error("timestamp");
      var zone = m[8], offset = 0;
      if (zone !== "Z") {
        var zh = Number(zone.slice(1, 3)), zm = Number(zone.slice(4, 6));
        if (zh > 23 || zm > 59) throw new Error("timezone");
        offset = (zh * 60 + zm) * (zone[0] === "+" ? 1 : -1);
      }
      return (local - offset * 60000) / 1000;
    }
    function run(args) {
      var directory = args[0], warning = Number(args[1]), maximum = Number(args[2]);
      var now = Number(args[3]), fm = $.NSFileManager.defaultManager;
      var rows = [], kinds = ["daily", "weekly", "monthly"];
      try {
        for (var i = 0; i < kinds.length; i++) {
          var path = directory + "/status-" + kinds[i] + ".json";
          if (!fm.fileExistsAtPath(path)) continue;
          if (!fm.isReadableFileAtPath(path)) throw new Error("unreadable");
          var text = ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null));
          if (typeof text !== "string") throw new Error("unreadable");
          var record = JSON.parse(text);
          if (!record || typeof record.ok !== "boolean") throw new Error("schema");
          var stamp = parseTime(record.finished_at);
          if (!isFinite(stamp) || stamp > now + 300) throw new Error("future");
          rows.push({kind: kinds[i], ok: record.ok, stamp: stamp, label: record.finished_at});
        }
      } catch (error) {
        return "down|2|Backup: stato non leggibile, non valido o timestamp futuro";
      }
      if (!rows.length) return "down|1|Backup: nessun backup registrato";
      rows.sort(function(a, b) {
        if (a.stamp !== b.stamp) return b.stamp - a.stamp;
        return Number(a.ok) - Number(b.ok);
      });
      var latest = rows[0];
      if (!latest.ok) return "down|1|Backup: ultimo tentativo " + latest.kind + " FALLITO (" + latest.label + ")";
      var age = Math.max(0, now - latest.stamp);
      var message = "Backup " + latest.kind + ": ultimo successo " + latest.label + " - eta " + (age / 3600).toFixed(1) + "h";
      if (age > maximum) return "down|1|" + message + " - CRITICO: oltre " + (maximum / 3600) + " ore";
      if (age > warning) return "up|0|" + message + " - AVVISO: oltre " + (warning / 3600) + " ore";
      return "up|0|" + message + " - OK";
    }
  ' "$STATUS_DIR" "$WARN_SECONDS" "$MAX_AGE_SECONDS" "$(/bin/date +%s)" 2>/dev/null
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
  local reading status message result timestamp
  if reading="$(read_backup)"; then
    IFS='|' read -r status result message <<< "$reading"
    if [[ ! "$status" =~ ^(up|down)$ || ! "$result" =~ ^[012]$ || -z "$message" ]]; then
      status=down
      result=2
      message="Backup: risposta del lettore non valida"
    fi
  else
    status=down
    result=2
    message="Backup: lettura dello stato non riuscita"
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
  echo "Monitor BACKUP ogni ${INTERVAL}s; invio a Uptime Kuma attivo. Ctrl+C per terminare."
else
  echo "Monitor BACKUP ogni ${INTERVAL}s; visualizzazione locale. Ctrl+C per terminare."
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

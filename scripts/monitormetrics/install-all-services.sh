#!/bin/bash
# Installa/migra tutti gli otto monitor nell'architettura del progetto FitHub.
set -euo pipefail
umask 077

if [[ "${1:-}" == --help ]]; then
  echo "Uso: /bin/bash install-all-services.sh"
  echo "Controlla e installa CPU, RAM, SSD, Swap, Temperatura, Docker, Backup e Host."
  echo "Usare dal proprio account amministratore; richiede sudo una sola volta."
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
if [[ "$SCRIPT_DIR" != "$PROJECT_ROOT/scripts/monitormetrics" ]]; then
  echo "ERRORE: collocare gli installer in Fithub/scripts/monitormetrics." >&2
  exit 2
fi
if (( EUID != 0 )); then
  exec /usr/bin/sudo /bin/bash "$SCRIPT_DIR/install-all-services.sh"
fi
if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == root ]]; then
  echo "ERRORE: avvia l'installer dal tuo account amministratore, non da una shell root." >&2
  exit 2
fi
LEGACY_DIR="/Library/Application Support/FitHub/monitormetrics"
METRICS=(cpu ram ssd swap temperature docker backup host)
# Verificare tutti i file prima di modificare il primo servizio.
for metric in "${METRICS[@]}"; do
  for source_file in "$SCRIPT_DIR/$metric.sh" "$SCRIPT_DIR/install-$metric-service.sh"; do
    if [[ ! -f "$source_file" || -L "$source_file" ]]; then
      echo "ERRORE: file mancante o non regolare: $source_file" >&2
      exit 2
    fi
    /bin/bash -n "$source_file"
  done
  url_file="$SCRIPT_DIR/$metric-push-url.txt"
  if [[ ! -e "$url_file" && ! -L "$url_file" ]]; then
    url_file="$LEGACY_DIR/$metric-push-url.txt"
  fi
  if [[ ! -f "$url_file" || -L "$url_file" || ! -s "$url_file" ]]; then
    echo "ERRORE: configurare $metric-push-url.txt prima di installare tutti i monitor." >&2
    exit 2
  fi
  url="$(/bin/cat "$url_file")" || exit 2
  url="${url%$'\r'}"
  url="${url%%\?*}"
  if [[ ! "$url" =~ ^https?://[^/?#[:space:]]+/api/push/[A-Za-z0-9_-]+$ ]] ||
     [[ "$url" == *'"'* || "$url" == *'\'* || "$url" == *'@'* ]]; then
    echo "ERRORE: URL Push $metric non valido." >&2
    exit 2
  fi
done
unset url
failed=()
for metric in "${METRICS[@]}"; do
  if ! /bin/bash "$SCRIPT_DIR/install-$metric-service.sh"; then
    failed+=("$metric")
  fi
done
if (( ${#failed[@]} > 0 )); then
  echo "ERRORE: verifica non riuscita per: ${failed[*]}. Gli altri servizi sono stati comunque elaborati." >&2
  exit 2
fi
echo "Tutti gli otto servizi sono stati verificati tramite launchd."
echo "Log: $PROJECT_ROOT/logs/system/monitormetrics/"

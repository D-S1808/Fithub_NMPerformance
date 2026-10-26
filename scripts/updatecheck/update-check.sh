#!/bin/bash
# Controllo aggiornamenti: nessuna installazione o modifica ai container.
set -euo pipefail
export LC_ALL=C
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  cat <<'EOF'
Uso: /bin/bash update-check.sh [--no-push | --service | --self-check | --retry-kuma | --diagnose-sources]
App, Homebrew, macOS, Docker Desktop e servizi Docker: dati locali e fonti pubbliche, senza account.
  senza opzioni  Controlla, salva report e invia il riepilogo a Kuma se configurato.
  --no-push      Controlla e salva report senza contattare Kuma.
  --service      Come sopra, con stdout/stderr nei log del progetto; usato da launchd.
  --self-check   Verifica runtime e configurazione senza rete o modifiche.
  --retry-kuma   Riinvia il risultato reale di una scansione conclusa da non oltre 15 minuti.
  --diagnose-sources  Controlla servizi Docker e commit Git locale, senza invio Kuma o riscrittura dei report.
  --help        Mostra questo messaggio.
Codici: 0 = nessuna novità; 1 = aggiornamenti/revisione richiesti; 2 = controllo incompleto/errore.
Per Comandi Rapidi: /bin/bash run-from-shortcuts.sh (esito e codice nel rapporto leggibile).
EOF
  exit 0
fi
if [[ "$(/usr/bin/uname -s)" != Darwin ]]; then
  echo "ERRORE: eseguire l'update checker sul Mac mini." >&2
  exit 2
fi
if [[ "${1:-}" == --service ]]; then
  umask 077
  LOG_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)/logs/system/updatecheck"
  if ! { exec 3>>"$LOG_DIR/update-check-error.log"; } 2>/dev/null; then
    /usr/bin/logger -t ch.nmperformance.fithub.updatecheck "FitHub update checker: log non accessibili; controllare Privacy di /bin/bash."
    exit 78
  fi
  exec 2>&3
  exec 3>&-
  exec >>"$LOG_DIR/update-check.log"
fi
if ! command -v python3.14 >/dev/null 2>&1; then
  echo "ERRORE: serve CPython 3.14.9+ del ramo 3.14, come per i backup." >&2
  exit 2
fi
exec python3.14 -E -s "$SCRIPT_DIR/update_checker.py" "$@"

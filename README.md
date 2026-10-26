# FitHub NMPerformance — script operativi

Gli script di backup da trasferire sul Mac mini si trovano in
[`scripts/backup`](scripts/backup/README.md).

I monitor CPU e RAM (ogni 15 secondi), Swap, stato termico, Docker e heartbeat Host (ogni 30 secondi)
e SSD e stato backup (ogni 60 secondi) del Mac mini, con invio opzionale a Uptime Kuma, si trovano in
[`scripts/monitormetrics`](scripts/monitormetrics/README.md).

I servizi eseguono gli script direttamente da `Fithub/scripts/monitormetrics/`;
URL e stati restano nella stessa cartella, i log sono in
`Fithub/logs/system/monitormetrics/`. Solo i plist di avvio sono installati
in `/Library/LaunchDaemons/`. L'installer `install-all-services.sh` aggiorna
tutti gli otto servizi e recupera i dati della precedente installazione.

La struttura di riferimento del server è riportata in `fithubStructure.txt`.
Questo repository non contiene l'applicazione o i dati di produzione.

Il controllo aggiornamenti del Mac mini è in
[`scripts/updatecheck`](scripts/updatecheck/README.md): ogni lunedì alle 19:00
verifica le versioni software dei servizi Docker, Docker Desktop, macOS e Homebrew,
oltre alle applicazioni nelle cartelle standard del Mac. Si può avviare tramite
un'automazione di Comandi Rapidi; salva report nel progetto e invia un riepilogo
a un monitor Push Kuma dedicato. Le app senza una fonte verificabile sono elencate
con versione e motivazione.
Usa fonti pubbliche senza account Docker o Git. I sorgenti FitHub sono censiti
localmente, senza confronto remoto; le ricostruzioni delle immagini Docker non
vengono verificate. Segnala le novità senza installarle.

# Monitor Mac mini → Uptime Kuma

Gli otto sensori seguono l'architettura FitHub: una sola cartella per script,
URL e stati, con i log sotto `logs/system`. Il servizio esegue direttamente
lo script del progetto. Non ci sono copie operative degli script sotto `/Library/`.

| Monitor | Campione | Intervallo Push Kuma consigliato | Condizione principale |
| --- | --- | --- | --- |
| [CPU](README-CPU.md) | 15 s | 60 s | Percentuale; nessuna soglia di sovraccarico applicata |
| [RAM](README-RAM.md) | 15 s | 60 s | DOWN sopra il 90% |
| [SSD](README-SSD.md) | 60 s | 120 s | Avviso sopra l'85%, DOWN sopra il 90% |
| [Swap](README-SWAP.md) | 30 s | 60 s | Avviso oltre 1 GiB; DOWN oltre 2 GiB per 5 minuti |
| [Temperatura](README-TEMPERATURE.md) | 30 s | 60 s | Stato termico; DOWN per serious/critical |
| [Docker](README-DOCKER.md) | 30 s | 60 s | DOWN se il daemon locale non risponde |
| [Backup](README-BACKUP.md) | 60 s | 120 s | Ultimo tentativo fra daily/weekly/monthly; avviso dopo 30 h, DOWN dopo 36 h o fallimento |
| [Host](README-HOST.md) | 30 s | 60 s | Heartbeat con uptime; Kuma rileva gli invii mancanti |

Le soglie e le frequenze non cambiano con il riallineamento dei percorsi.
Il sensore Backup non controlla scadenze separate per weekly e monthly.

## Directory del Mac mini

Root: `/Users/admin/Documents/Fithub/`.

```text
Fithub/
├── scripts/
│   ├── backup/
│   │   ├── backup.py
│   │   └── backup.json
│   └── monitormetrics/
│       ├── cpu.sh, ram.sh, ssd.sh, swap.sh
│       ├── temperature.sh, docker.sh, backup.sh, host.sh
│       ├── install-*-service.sh
│       ├── install-all-services.sh
│       ├── *-push-url.txt
│       ├── status-daily.json, status-weekly.json, status-monthly.json
│       ├── swap-state/
│       └── ch.nmperformance.fithub.*.plist
├── logs/
│   └── system/
│       └── monitormetrics/
│           ├── cpu.log, cpu-error.log
│           └── ... log ed errori degli altri sette sensori
└── backups/
    ├── status-daily.json, status-weekly.json, status-monthly.json
    └── ... dati e log dei backup applicativi
```

Solo i plist installati per l'avvio automatico sono in
`/Library/LaunchDaemons/ch.nmperformance.fithub.<monitor>.plist`.
Quelli nel progetto sono riferimenti per l'account admin e il percorso standard;
gli installer incorporano il plist e aggiornano utente e percorso dello script in base alla posizione effettiva del progetto.
Non impostano `WorkingDirectory`, `StandardOutPath` o `StandardErrorPath`: i log
vengono aperti da Bash in modalità `--service`, dopo l'avvio del processo.

Tutti gli script leggono l'URL accanto a sé. Swap usa `swap-state/` nella stessa
cartella; Backup legge i tre `status-*.json` accanto a `backup.sh`.
Il motore `scripts/backup/backup.py` pubblica gli esiti sotto la root configurata
in `backup.json`, che deve corrispondere al progetto installato.
URL, stati e log sono esclusi da Git.

## Installazione e migrazione con un comando

Sul Mac mini copia/aggiorna l'intera cartella `scripts/monitormetrics` del
progetto, conservando i tuoi `*-push-url.txt`, `status-*.json` e `swap-state/`.
Per il sensore Backup aggiorna anche `scripts/backup/backup.py`, conservando
`backup.json`. Non sostituire la cartella Fithub che contiene i dati reali.

Crea un Push Monitor distinto per ogni sensore e salva ciascun URL nel suo
`<monitor>-push-url.txt`, su una sola riga senza virgolette. Puoi incollare anche
l'URL completo di query: lo script ricrea i parametri. Le guide dei sensori
spiegano i valori e i messaggi. Non condividere i token.

Ferma gli eventuali cicli manuali con Ctrl+C ed esegui dal tuo account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-all-services.sh
```

L'installer richiede sudo una sola volta, controlla script e URL di tutti gli
otto monitor prima di modificare il primo servizio, quindi richiama gli
installer dedicati. Ogni installer genera il plist, sostituisce il job con la
stessa label, abilita l'avvio al boot e lo avvia subito come utente admin.
Non esegue backup di produzione. Dopo il caricamento verifica il primo campione
reale di ogni servizio, per un massimo di 25 secondi. Richiede un nuovo messaggio
nel log e codice di uscita 0 o 1: il codice 1 indica un sensore funzionante
che ha rilevato una condizione DOWN. Errori di spawn, accesso, lettura o invio
vengono segnalati; un monitor fallito non impedisce il controllo degli altri sette.

La migrazione recupera:

- URL da `/Library/Application Support/FitHub/monitormetrics/`, solo se assenti nel progetto;
- log da `/Library/Logs/FitHub/`, solo se i nuovi file non esistono;
- cronologia Swap precedente, solo se manca `swap-state/history` nel progetto;
- esiti Backup da `Fithub/backups/`, con ripiego sulla vecchia cartella di stato,
  solo se non sono già pubblicati nel progetto.

Le copie vecchie restano disponibili per recupero, ma i nuovi plist non le
usano. I file già presenti nel progetto hanno precedenza. La reinstallazione
preserva la cronologia Swap: eventuali gap oltre 75 secondi azzerano comunque
il conteggio critico secondo le regole del sensore. Non eliminare `.lock`.

Se non hai configurato tutti gli otto monitor, usa il singolo installer:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-host-service.sh
```

Gli installer dedicati sono autonomi e non dipendono da un helper condiviso.
Il plist è l'unico file che installano sotto `/Library/`. Le directory di
monitoraggio hanno permessi 700 e appartengono all'utente del servizio;
script 700, URL/stati/log 600. I permessi delle directory condivise `logs` e
`logs/system` già presenti non vengono modificati.

Se usavi un vecchio LaunchAgent con la stessa label, scaricalo e sposta il
suo plist fuori da `~/Library/LaunchAgents` per evitare un secondo avvio al login.
Questo riguarda soltanto precedenti installazioni manuali con LaunchAgent.

## Aggiornamenti, verifiche e log

Modificando uno script o il suo URL nel progetto, la successiva esecuzione
`--service` del servizio legge il file aggiornato. Non occorre copiare lo script
in un'altra cartella. Ripeti l'installer dopo cambi a plist, frequenze di
launchd, percorso o account, oppure per riattivare un servizio disabilitato.
Non spostare il progetto con i servizi caricati senza reinstallarli.

Esempio Host, senza stampare il token:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/host.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/host.sh --once
sudo launchctl print system/ch.nmperformance.fithub.host
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/host.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/host-error.log
```

Tutti i sensori accettano `--dry-run` (un campione locale), `--once`
(un campione con invio se configurato) o nessuna opzione (ciclo nel Terminale).
`--service` esegue un solo campione con stdout e stderr nei log del progetto:
è la modalità usata da launchd. Restituisce 78 se non può aprire i log.
La variabile `KUMA_<MONITOR>_PUSH_URL` ha precedenza sul file URL nelle prove
manuali; i LaunchDaemon leggono i file del progetto. Per Temperatura il nome è
`KUMA_TEMPERATURE_PUSH_URL`.

In Kuma passa il mouse sull'ultima barretta heartbeat per leggere `msg`.
Il campo `ping` non viene usato per percentuali, uptime o esiti. Un invio
richiede HTTP 2xx e JSON `ok: true`; i token non compaiono nei log né negli
argomenti di curl. Abilita le notifiche nei monitor Kuma.

Tra i campioni il job può mostrare `not running`: è normale per `--service`.
Controlla l'ultimo codice di uscita e il messaggio nel log. I log vengono
aggiunti agli stessi file senza rotazione automatica.

I servizi partono al boot, senza Terminale aperto; con FileVault occorre prima
sbloccare il disco. Durante lo stop non vengono raccolti campioni. macOS
protegge Documents: se compaiono errori `Operation not permitted`, verifica
i permessi Privacy del processo che esegue gli script. I permessi 700/600 e
l'accesso riuscito dal Terminale non dimostrano l'accesso dal servizio launchd.
L'installer non modifica le autorizzazioni Privacy.

### Errore di avvio EX_CONFIG e accesso a Documents

`state = not running` fra campioni è normale. `job state = spawn failed` o
`last exit code = 78: EX_CONFIG` sono invece errori di avvio: il processo non
ha necessariamente letto la metrica. La configurazione attuale evita che
launchd debba entrare nella directory del progetto o aprire i log prima di
avviare Bash. Non usare i plist della prima migrazione che impostavano queste chiavi.

Per mantenere script e log sotto Documents, sul Mac mini verifica in
**Impostazioni di Sistema → Privacy e Sicurezza → Accesso completo al disco**
che `/bin/bash` sia autorizzato. Con il pulsante `+`, premi `Cmd+Shift+G`,
inserisci `/bin/bash`, aggiungilo e abilitalo. L'autorizzazione permette a Bash
l'accesso anche alle altre cartelle protette; concedila consapevolmente.
I permessi del Terminale non autorizzano automaticamente Bash lanciato da launchd.

Dopo l'autorizzazione ripeti `install-all-services.sh`: gli installer verificano
l'esecuzione tramite launchd, non soltanto la riuscita di `bootstrap`. Un errore
2 dopo un avvio riuscito riguarda lettura, configurazione o invio: guarda i log
del monitor indicato. Il sensore Host conserva un problema separato se il suo
campione fallisce mentre gli altri sono tornati operativi.


Se Kuma gira sullo stesso Mac/Docker monitorato, durante quel guasto non può
notificare: per rilevarlo in tempo reale serve un monitor indipendente.

Per fermare Host fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.host
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.host
```

Ripeti l'installer per riabilitarlo e avviarlo. Gli altri monitor hanno label
analoghe con il proprio nome.

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -v
```

I test simulano le letture hardware, Kuma, launchctl e i privilegi degli installer.
I test di installazione eseguono realmente la generazione dei plist e la
migrazione dei file in directory temporanee, anche con spazi e caratteri XML
nei percorsi. Non installano veri servizi, non inviano richieste di rete e non
eseguono backup di produzione.

Riferimenti: [struttura FitHub](../../fithubStructure.txt),
[LaunchDaemon Apple](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html),
[accesso a file e cartelle su macOS](https://support.apple.com/guide/mac-help/control-access-to-files-and-folders-on-mac-mchld5a35146/mac),
[endpoint Push Kuma](https://github.com/louislam/uptime-kuma/blob/master/server/routers/api-router.js).

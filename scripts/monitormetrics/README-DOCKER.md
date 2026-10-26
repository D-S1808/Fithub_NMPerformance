# Docker Mac mini → Uptime Kuma

`docker.sh` controlla ogni **30 secondi** che l'API del daemon Docker locale
risponda. Usa `curl` e gli strumenti inclusi in macOS, senza dipendere dalla
CLI Docker o dal PATH del Terminale. È predisposto per Docker Desktop locale.

| Condizione | Stato Kuma |
| --- | --- |
| GET `/_ping` restituisce HTTP 200 e corpo `OK` | UP |
| Socket assente/non accessibile, daemon fermo, timeout, errore API | DOWN |
| Invio a Kuma fallito | Errore locale; Kuma può rilevare il mancato heartbeat |

Quando Docker torna raggiungibile, lo script invia nuovamente UP. Abilita le
notifiche nel monitor Kuma. Non verifica lo stato dei singoli container o
l'applicazione FitHub: quelli rimangono oggetto dei monitor dei servizi.

**Se Kuma è nello stesso Docker monitorato, non può ricevere messaggi né
inviare notifiche mentre quel daemon è fermo.** Lo script continua fuori da
Docker e registra localmente il guasto, ma per un allarme in tempo reale
occorre anche un monitor esterno o Kuma su una macchina indipendente.

## Configurazione sul Mac mini

Copia `docker.sh` e `install-docker-service.sh` nella cartella:

```text
/Users/admin/Documents/Fithub/scripts/monitormetrics/
```

Crea in Kuma un nuovo monitor **Push**, nome `Mac mini - Docker`, intervallo
**60 secondi**. Salva il suo URL in `docker-push-url.txt`, accanto allo script,
su una sola riga senza virgolette. Usa un token dedicato.

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_DOCKER
```

Se necessario correggi proprietari e permessi dall'account admin:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" docker.sh docker-push-url.txt
chmod 700 docker.sh
chmod 600 docker-push-url.txt
```

Prova il controllo locale e poi un invio singolo:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/docker.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/docker.sh --once
```

Esempi:

```text
Docker: daemon raggiungibile - API OK
Docker: socket locale assente o non accessibile - DOWN
Docker: daemon non raggiungibile (arresto, timeout o permessi socket) - DOWN
```

In Kuma apri il monitor e passa il mouse sull'ultima barretta heartbeat. Il
risultato è in `msg`; il campo Ping resta vuoto. Il token non viene stampato
nei log né passato negli argomenti di curl. Per l'invio a Kuma servono HTTP
2xx e JSON `ok: true`; errori di invio non interrompono i campioni successivi.

Senza opzioni il ciclo continua ogni 30 secondi; Ctrl+C termina. `--once`
esegue un controllo con invio, `--dry-run` controlla soltanto il daemon locale.
Codici: 0 daemon raggiungibile, 1 daemon/socket/API non raggiungibile o risposta
non valida, 2 errore di configurazione o invio a Kuma.
`KUMA_DOCKER_PUSH_URL` ha precedenza sul file URL. Senza URL lo script avvisa
ed esegue controlli locali.

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-docker-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.docker` con
`docker.sh --service` ogni **30 secondi**, usando l'account che esegue
l'installazione. Il plist è incorporato; quello separato è un riferimento per
l'utente admin e il percorso standard del Mac mini.

Il servizio esegue direttamente lo script in
`/Users/admin/Documents/Fithub/scripts/monitormetrics/` e legge l'URL dalla
stessa cartella. I log sono in
`/Users/admin/Documents/Fithub/logs/system/monitormetrics/`.
L'installer ricava i percorsi dalla posizione del progetto. In `/Library/`
viene installato solo il plist in `LaunchDaemons`.

Le modifiche a script e URL nel progetto sono lette dalla successiva esecuzione.
Ripeti l'installer per cambiare il plist, riattivare il servizio o migrare la
vecchia installazione. Recupera l'URL precedente solo se manca quello nel
progetto; importa i vecchi log solo se i nuovi file non esistono. Le copie
precedenti restano disponibili ma non sono più usate dal servizio.

```bash
sudo launchctl print system/ch.nmperformance.fithub.docker
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/docker.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/docker-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.docker
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.docker
```

Ripetendo l'installer viene riabilitato e riavviato.

## Socket locale e permessi

Lo script ricava la home dell'account effettivo con `id` e `dscl`, senza
dipendere dalla variabile HOME del Terminale. Preferisce il socket
`<home>/.docker/run/docker.sock`; se assente, prova `/var/run/docker.sock`.
Se il socket utente esiste ma non risponde, segnala DOWN senza cambiare daemon.
Il processo deve poter attraversare le cartelle e connettersi al socket.

Il controllo usa `curl --unix-socket ... http://localhost/_ping`, con timeout
di connessione 3 secondi, tempo massimo 10 secondi e proxy esclusi. Il nome
localhost identifica la richiesta HTTP sul socket Unix, senza collegamenti
TCP a server remoti. Non usa `DOCKER_HOST`, `DOCKER_CONTEXT` o il contesto
attualmente selezionato nella CLI: il bersaglio è il Docker locale del Mac.

Configurazioni con Colima, Rancher Desktop, socket personalizzati o daemon
remoti richiedono di adattare la scelta del socket nello script del progetto.
Non viene cambiato alcun permesso del socket; non viene esposto su TCP e
non si avviano, fermano, riavviano o modificano container.

Fonti: [socket e account di Docker Desktop per Mac](https://docs.docker.com/desktop/setup/install/mac-permission-requirements/),
[endpoint Ping dell'Engine API](https://github.com/moby/moby/blob/master/api/swagger.yaml).

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -p test_docker_monitormetrics.py -v
```

I test simulano socket, risposta Docker e Kuma. Coprono UP/DOWN, recupero,
socket utente e standard, errori, timeout, contesti remoti ignorati, token e
cadenza 30 secondi. Non richiedono Docker né installano servizi. La verifica
con un daemon reale va eseguita sul Mac mini tramite `--dry-run`.

# Host Mac mini → Uptime Kuma

`host.sh` invia un **heartbeat ogni 30 secondi** con il tempo trascorso dal boot:

```text
Host online - uptime 2g 03h 14m 08s
```

Ogni lettura riuscita invia UP. Lo spegnimento, lo stop o un problema di rete
interrompono gli invii: è il **Push Monitor di Kuma** a rilevare il heartbeat
mancante. Non esiste un invio DOWN da un Mac già spento. Un errore di lettura
dell'uptime tenta invece di inviare DOWN con il messaggio di errore.

**Per ricevere notifiche mentre il Mac mini è spento, Kuma deve funzionare su
un altro dispositivo.** Se Kuma è nello stesso Docker/Mac mini, serve un
monitor esterno indipendente. L'assenza di heartbeat indica che il collegamento
non funziona; da sola non distingue uno spegnimento da stop o guasti di rete.

## Configurazione sul Mac mini

Copia `host.sh` e `install-host-service.sh` nella cartella:

```text
/Users/admin/Documents/Fithub/scripts/monitormetrics/
```

Crea in Kuma un **Push Monitor**, nome `Mac mini - Host`, intervallo
**60 secondi**. Abilita le notifiche. Usa zero tentativi aggiuntivi se vuoi
l'allarme al primo controllo con heartbeat scaduto; eventuali tentativi
aggiuntivi ritardano il DOWN. I 60 secondi lasciano margine fra gli invii
nominali ogni 30 secondi, senza garantire una rilevazione esattamente al secondo.

Salva l'URL generato da Kuma in `host-push-url.txt`, accanto allo script,
su una sola riga senza virgolette, con un token dedicato:

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_HOST
```

Proteggi il file. Se appartiene a root, correggi prima il proprietario:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" host.sh host-push-url.txt
chmod 700 host.sh
chmod 600 host-push-url.txt
/bin/bash host.sh --dry-run
/bin/bash host.sh --once
```

Nel dettaglio del monitor Kuma passa il mouse sull'ultima barretta heartbeat:
l'uptime si trova nel messaggio `msg`. Non viene usato `ping`, che Kuma
interpreta come tempo di risposta in millisecondi.

Senza opzioni lo script continua ogni 30 secondi fino a Ctrl+C. `--once` esegue
un campione con invio; `--dry-run` esegue un campione locale ignorando l'URL.
Codici: 0 lettura e invio riusciti (oppure sola lettura senza URL),
2 errore di configurazione, lettura o invio. Senza URL avvisa e mostra solo
l'uptime locale. La variabile `KUMA_HOST_PUSH_URL` ha precedenza sul file.
Il file URL è escluso da Git; il token non compare nei log o negli argomenti
di curl. L'invio richiede HTTP 2xx e risposta JSON `ok: true`; in modalità
continua gli errori non interrompono i campioni successivi.

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-host-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.host` con
`host.sh --service` ogni **30 secondi**, usando l'account che esegue
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
sudo launchctl print system/ch.nmperformance.fithub.host
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/host.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/host-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.host
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.host
```

Ripetendo l'installer viene riabilitato e riavviato.

## Lettura e verifica

Usa `sysctl -n kern.boottime` e `date +%s`, inclusi in macOS, senza Python,
Homebrew o privilegi amministratore per la lettura. La differenza fra ora
corrente e ora di boot include lo stop e si azzera al riavvio; non misura
il solo tempo di attività della CPU. Correzioni dell'orologio di sistema
possono modificare il valore. Timestamp assenti, non validi o boot nel futuro
producono un errore, senza dichiarare un uptime valido.

Test di sviluppo:

```bash
python3 -m unittest discover -s tests -p test_host_monitormetrics.py -v
```

I test simulano orologio, boot e Kuma. Non installano servizi né inviano
richieste di rete. Per verificare il timeout in Kuma, ferma il servizio e
attendi la scadenza del monitor, poi ripeti l'installer per ripristinarlo.

Riferimenti ufficiali: [endpoint Push](https://github.com/louislam/uptime-kuma/blob/master/server/routers/api-router.js)
e [controllo degli heartbeat mancanti](https://github.com/louislam/uptime-kuma/blob/master/server/model/monitor.js).

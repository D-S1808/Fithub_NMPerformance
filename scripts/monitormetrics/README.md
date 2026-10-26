# CPU Mac mini → Uptime Kuma

`cpu.sh` misura la percentuale totale di utilizzo della CPU del Mac, da 0 a
100%, ogni **15 secondi**. Eseguilo direttamente su macOS, nell'account del Mac
mini, fuori da Docker. Usa solo strumenti inclusi in macOS; non richiede Python,
Homebrew o `sudo`.

Copia questa cartella in `Fithub/scripts/monitormetrics/` sul Mac mini.
Da Terminale, nella cartella Fithub esterna:

```bash
/bin/bash scripts/monitormetrics/cpu.sh
```

Esempio di output:

```text
2026-10-26 12:00:01 CET | CPU 24.15%
2026-10-26 12:00:16 CET | CPU 36.70%
```

Il primo valore arriva dopo circa un secondo. Ogni valore misura l'utilizzo
nell'ultimo secondo di campionamento, **non la media di tutti i 15 secondi**.
Lo script usa il secondo campione di `top` e calcola `100 - idle`; il primo
campione cumulativo viene scartato. La cadenza di 15 secondi include il tempo
di lettura e invio, senza aggiungere ogni volta altri 15 secondi di attesa.
L'orario usa il fuso del Mac.

Per interrompere: **Ctrl+C**. Se il Terminale si chiude o il Mac va in stop, il
monitoraggio continuo si ferma. Per un avvio automatico vedi più sotto.

## Collegamento a Uptime Kuma

1. Crea un monitor di tipo **Push** chiamato `Mac mini - CPU`.
2. Imposta l'intervallo del monitor a **60 secondi**, così ci sono quattro invii
   nominali per ogni finestra. Questo concede margine a piccoli ritardi.
3. Salva e copia l'URL Push generato da Kuma.
4. Crea `cpu-push-url.txt` accanto allo script, con **una sola riga** contenente
   quell'URL, senza virgolette. Puoi incollare anche i parametri già presenti:
   lo script li sostituisce con il campione corrente.

Esempio del contenuto, da sostituire con l'URL vero:

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_CPU
```

Proteggi il file ed esegui una prova:

```bash
chmod 600 scripts/monitormetrics/cpu-push-url.txt
/bin/bash scripts/monitormetrics/cpu.sh --once
```

In alternativa, l'URL può essere fornito dalla variabile d'ambiente
`KUMA_CPU_PUSH_URL`, che ha precedenza sul file. Il file è escluso da Git;
non condividere il token. Non vengono contattati server se l'URL non è configurato.

In Kuma vedrai `CPU 24.15%` nel messaggio del monitor. Ogni lettura riuscita
invia `status=up`; una lettura fallita tenta di inviare `status=down`.
Questa versione misura l'utilizzo: non applica soglie di sovraccarico.
Se gli invii si interrompono, il Push Monitor rileva l'assenza del heartbeat.
Le notifiche vanno abilitate nel monitor Kuma.

Il valore è nel parametro `msg`. Non viene usato `ping`, che Kuma interpreta
come tempo di risposta e mostrerebbe con unità errata. Un invio è riuscito solo
se la risposta HTTP ha esito positivo e il JSON contiene `ok: true`.
Se Cloudflare Access protegge l'endpoint, occorre un percorso/API raggiungibile
dallo script senza login interattivo, oppure l'indirizzo locale di Kuma con
la porta effettivamente pubblicata sul Mac. Gli errori di invio sono segnalati
senza stampare il token; il ciclo continua ai campioni successivi.

## Singola lettura

```bash
# Un campione, incluso l'invio a Kuma se configurato:
/bin/bash scripts/monitormetrics/cpu.sh --once

# Un campione esclusivamente locale:
/bin/bash scripts/monitormetrics/cpu.sh --dry-run
```

`--once` e `--dry-run` restituiscono 0 in caso di successo, 2 in caso di errore.
Non avviare contemporaneamente il ciclo continuo e una pianificazione esterna.

## Servizio in background: avvio al boot, ogni 15 secondi

### Installazione con un solo comando

Copia `install-cpu-service.sh` accanto a `cpu.sh` e al file `cpu-push-url.txt`
sul Mac mini. Interrompi l'eventuale ciclo nel Terminale con Ctrl+C, poi esegui:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-cpu-service.sh
```

Inserisci la password amministratore quando richiesta. L'installer configura
launchd, imposta i permessi, copia script e URL nella cartella del servizio e
lo avvia subito. Rileva automaticamente l'account che esegue il comando;
non richiede di creare o modificare manualmente un plist. Non occorre `chmod +x`
per l'installer, perché viene eseguito tramite Bash.

Per aggiornare la copia di servizio dopo una modifica allo script o al token,
ripeti lo stesso comando: l'istanza già installata viene fermata e ricaricata.
Il servizio riparte ai boot successivi, senza Terminale aperto e senza login.
Con FileVault occorre prima sbloccare il disco; durante lo stop non raccoglie dati.
Non avviare contemporaneamente un vecchio LaunchAgent o il ciclo manuale.

### Installazione manuale alternativa

Il file `ch.nmperformance.fithub.cpu.plist` configura un **LaunchDaemon**:
parte all'avvio di macOS, anche senza login, e richiama `cpu.sh --once` ogni
15 secondi. Il processo usa l'account **admin** del Mac mini. Se il nome breve
dell'account è diverso, modifica `UserName` nel plist prima dell'installazione.

Il servizio legge una copia dello script e dell'URL da
`/Library/Application Support/FitHub/monitormetrics/`, fuori dalla cartella
Documents protetta da macOS. L'originale rimane nel progetto. Non occorre dare
Accesso completo al disco a Bash.

**Sul Mac mini**, termina prima il ciclo nel Terminale con Ctrl+C e copia il
nuovo plist dal progetto nella cartella accanto a `cpu.sh`. Poi esegui:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics

plutil -lint ch.nmperformance.fithub.cpu.plist
sudo install -d -o root -g wheel -m 755 "/Library/Application Support/FitHub/monitormetrics"
sudo install -d -o admin -g staff -m 700 /Library/Logs/FitHub
sudo install -o root -g wheel -m 755 cpu.sh "/Library/Application Support/FitHub/monitormetrics/cpu.sh"
sudo install -o admin -m 600 cpu-push-url.txt "/Library/Application Support/FitHub/monitormetrics/cpu-push-url.txt"
sudo install -o root -g wheel -m 644 ch.nmperformance.fithub.cpu.plist /Library/LaunchDaemons/ch.nmperformance.fithub.cpu.plist

sudo launchctl bootstrap system /Library/LaunchDaemons/ch.nmperformance.fithub.cpu.plist
```

Il servizio parte subito e si ricarica ai riavvii successivi. Non richiede
Terminale aperto. La password amministratore è richiesta solo per installare
e gestire il servizio; il monitor CPU viene eseguito come `admin`.
Con FileVault, dopo l'accensione occorre prima sbloccare il disco.
Durante lo stop o lo spegnimento non vengono raccolti campioni; l'intervallo
è una pianificazione, non una garanzia in tempo reale.

### Verifica e log

```bash
sudo launchctl print system/ch.nmperformance.fithub.cpu
sudo tail -n 10 /Library/Logs/FitHub/cpu.log
sudo tail -n 10 /Library/Logs/FitHub/cpu-error.log
```

Nel log CPU compare una nuova riga circa ogni 15 secondi. Tra un campione e
l'altro il job può risultare `not running`: è normale, perché viene avviato
in modalità `--once`. `last exit code = 0` indica un'esecuzione riuscita;
`2` indica errore di lettura CPU o di invio. Verifica anche l'arrivo dei messaggi
nel monitor Kuma. I log si aggiungono agli stessi file, senza rotazione automatica.

### Arresto, riattivazione e aggiornamenti

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.cpu
```

Per riattivarlo subito, ripeti `launchctl bootstrap` come sopra. Per disabilitarlo
anche ai riavvii:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.cpu
```

Per rimuovere la disabilitazione prima del caricamento:

```bash
sudo launchctl enable system/ch.nmperformance.fithub.cpu
```

Per aggiornare script, token o plist, ferma il job con `bootout`, ripeti le copie
con `install` e infine `bootstrap`. Le modifiche alla copia nel progetto non
aggiornano automaticamente quella di servizio. Un errore `service already loaded`
richiede di fermare l'istanza caricata prima di ripetere `bootstrap`.

Se avevi già configurato il precedente LaunchAgent, scaricalo prima:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/ch.nmperformance.fithub.cpu.plist"
```

Poi sposta il vecchio plist fuori da `~/Library/LaunchAgents`, per evitare invii
doppi al login. Questo passaggio serve soltanto se il vecchio job esiste.

Kuma sullo stesso Mac non può notificare mentre il Mac è spento: per rilevare
quel guasto in tempo reale serve anche un monitor esterno.

Riferimento: [LaunchDaemon e LaunchAgent nella documentazione Apple](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

Riferimento integrazione: [endpoint Push ufficiale di Uptime Kuma](https://github.com/louislam/uptime-kuma/blob/master/server/routers/api-router.js).

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -p test_monitormetrics.py -v
```

I test simulano `top` e `curl`: verificano il secondo campione, la cadenza,
l'invio dei messaggi e gli errori senza leggere la CPU reale o contattare Kuma.
Sul Mac mini esegui prima `--dry-run`, poi `--once` con l'URL configurato.

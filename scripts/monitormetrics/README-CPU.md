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

Per leggere il valore, apri il dettaglio del monitor CPU e passa il mouse
sull'ultima barretta verde della cronologia heartbeat: il tooltip mostra il
messaggio del campione. Il riquadro Ping e il relativo grafico non mostrano
la CPU, perché questo script invia la percentuale in `msg`, non in `ping`.
La tabella degli eventi registra i cambi di stato e può non aggiornarsi per
ogni nuovo campione UP; usa l'ultimo heartbeat per il valore recente.

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

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-cpu-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.cpu` con
`cpu.sh --service` ogni **15 secondi**, usando l'account che esegue
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
sudo launchctl print system/ch.nmperformance.fithub.cpu
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/cpu.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/cpu-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.cpu
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.cpu
```

Ripetendo l'installer viene riabilitato e riavviato.

## Lettura e test

Il campione usa il secondo valore di `top -l 2 -s 1 -n 0`, da cui calcola
`100 - idle`. Non applica ancora una soglia di sovraccarico prolungato.

```bash
python3 -m unittest discover -s tests -p test_monitormetrics.py -v
```

I test simulano top e Kuma senza leggere la CPU reale o inviare richieste di rete.

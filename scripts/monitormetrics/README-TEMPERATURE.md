# Temperatura Mac mini → Uptime Kuma

`temperature.sh` legge lo **stato termico macOS ogni 30 secondi**, tramite
`NSProcessInfo.thermalState` di Foundation. Usa solo strumenti inclusi in macOS
e non richiede Homebrew, Python, sensori aggiuntivi o `sudo` per la lettura.

| Stato macOS | Stato Kuma | Messaggio |
| --- | --- | --- |
| nominal | UP | OK |
| fair | UP | AVVISO: pressione termica moderata |
| serious | DOWN | CRITICO: pressione termica elevata |
| critical | DOWN | CRITICO: pressione termica critica |
| Lettura fallita/stato sconosciuto | DOWN | Errore di lettura |

Le condizioni vengono applicate a ogni campione. Tornando a nominal o fair,
il monitor invia nuovamente UP. L'associazione serious/critical → DOWN è la
policy di questo monitor. Il warning fair è nel messaggio: non cambia lo stato
Kuma e non provoca da solo una notifica. Abilita le notifiche per gli allarmi DOWN.

Il sensore riporta uno **stato complessivo, non gradi Celsius**: non inventa una
temperatura CPU o SSD. La lettura non misura direttamente temperature dei singoli
componenti, giri delle ventole o frequenze di throttling.

## Configurazione sul Mac mini

Copia `temperature.sh` e `install-temperature-service.sh` nella cartella:

```text
/Users/admin/Documents/Fithub/scripts/monitormetrics/
```

Crea in Kuma un nuovo **Push Monitor**, nome `Mac mini - Temperatura`, con
intervallo **60 secondi**. Salva il suo URL in `temperature-push-url.txt`, accanto
allo script, su una sola riga senza virgolette. Usa un token dedicato.

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_TEMPERATURA
```

Se i file appartengono a root, correggi il proprietario; proteggi il file URL:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" temperature.sh temperature-push-url.txt
chmod 700 temperature.sh
chmod 600 temperature-push-url.txt
```

Prova una lettura locale e poi un invio singolo:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/temperature.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/temperature.sh --once
```

Esempi:

```text
Stato termico nominal - OK
Stato termico fair - AVVISO: pressione termica moderata
Stato termico serious - CRITICO: pressione termica elevata
```

In Kuma apri il monitor e passa il mouse sull'ultima barretta heartbeat.
Il valore è inviato nel messaggio `msg`; il campo Ping resta vuoto.
Il token non viene stampato nei log né passato negli argomenti di curl.
Un invio richiede HTTP 2xx e JSON `ok: true`. Un errore di rete non interrompe
i campioni successivi; Kuma può rilevare il mancato heartbeat.

Senza opzioni lo script continua ogni 30 secondi fino a Ctrl+C. `--once` esegue
un campione con invio; `--dry-run` esegue un campione locale ignorando l'URL.
Codici: 0 nominal/fair, 1 serious/critical, 2 errore di configurazione/lettura/invio.
`KUMA_TEMPERATURE_PUSH_URL` ha precedenza sul file URL. Senza URL lo script
avvisa ed esegue soltanto letture locali.

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-temperature-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.temperature` con
`temperature.sh --service` ogni **30 secondi**, usando l'account che esegue
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
sudo launchctl print system/ch.nmperformance.fithub.temperature
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/temperature.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/temperature-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.temperature
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.temperature
```

Ripetendo l'installer viene riabilitato e riavviato.

## Lettura e verifica

`osascript -l JavaScript` usa JXA per accedere a Foundation, senza controllare
altre applicazioni. Lo script verifica che `thermalState` sia disponibile e
accetta esclusivamente i valori previsti dall'enumerazione Apple:
0 nominal, 1 fair, 2 serious, 3 critical. Valori mancanti, non numerici,
sconosciuti o errori del comando generano DOWN con errore; non vengono
interpretati come nominal. Non modifica le impostazioni energetiche del Mac.

Riferimenti: [thermalState di Apple](https://developer.apple.com/documentation/foundation/processinfo/thermalstate-swift.property),
[stati termici](https://developer.apple.com/documentation/foundation/processinfo/thermalstate-swift.enum).

Test di sviluppo:

```bash
python3 -m unittest discover -s tests -p '*monitormetrics.py' -v
```

I test simulano la lettura termica e Kuma: verificano tutti gli stati, recupero,
letture sconosciute/fallite, invio, token, cadenza 30 secondi e plist dedicato.
Non installano servizi, modificano temperature o inviano richieste di rete.

# RAM Mac mini → Uptime Kuma

`ram.sh` legge la RAM dell'host macOS ogni **15 secondi** e invia il risultato
al proprio Push Monitor. Non richiede Homebrew, Python o Docker. Il monitor
RAM ha URL, servizio e log separati da quello CPU.

| Condizione | Stato inviato a Kuma |
| --- | --- |
| RAM <=90% | UP |
| RAM >90% | DOWN, messaggio CRITICO |
| Lettura RAM fallita | DOWN, messaggio di errore |
| Invio fallito o heartbeat assente | Errore locale; Kuma rileva il mancato heartbeat |

La soglia si applica a ogni campione, senza attesa. Tornando al 90% o meno,
il monitor invia nuovamente UP. Abilita le notifiche nel monitor Kuma.

## Configurazione sul Mac mini

Copia accanto agli script CPU, in `/Users/admin/Documents/Fithub/scripts/monitormetrics/`:

- `ram.sh`
- `install-ram-service.sh`

L'installer contiene il plist e non richiede di copiarlo separatamente.
`ch.nmperformance.fithub.ram.plist` è disponibile come riferimento per
l'installazione manuale con account `admin`.

In Kuma crea un **nuovo** monitor di tipo **Push**, chiamato `Mac mini - RAM`,
con intervallo **60 secondi**: lo script invia quattro campioni nominali per
ogni finestra. Salva l'URL generato, su una sola riga senza virgolette, nel file
`ram-push-url.txt` accanto a `ram.sh`. Non riutilizzare il token della CPU.

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_RAM
```

Il file privato è escluso da Git. Se appartiene a root, correggi il proprietario
come già fatto per il monitor CPU, poi proteggilo:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" ram.sh ram-push-url.txt
chmod 700 ram.sh
chmod 600 ram-push-url.txt
```

Prova prima una lettura locale, poi l'invio singolo:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/ram.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/ram.sh --once
```

Esempi di messaggi:

```text
RAM 72.30% (11.57/16.00 GiB)
RAM 92.50% (14.80/16.00 GiB) - CRITICO: soglia 90% superata
```

In Kuma apri il monitor RAM e passa il mouse sull'ultima barretta della cronologia
heartbeat per vedere la percentuale, come per la CPU. Il valore è nel messaggio
`msg`; il campo Ping non viene usato. Il token non viene stampato nei log né
passato negli argomenti di curl. L'invio richiede HTTP 2xx e JSON `ok: true`.

Per il solo ciclo nel Terminale, avvia `ram.sh` senza opzioni; Ctrl+C lo ferma.
`KUMA_RAM_PUSH_URL` può sostituire il file URL e ha precedenza su di esso.
Senza URL lo script avvisa ed esegue solo letture locali; `--dry-run` ignora
intenzionalmente l'URL. `--once` e `--dry-run` restituiscono 0 per UP,
1 per soglia superata, 2 per errori di configurazione, lettura o invio.

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-ram-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.ram` con
`ram.sh --service` ogni **15 secondi**, usando l'account che esegue
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
sudo launchctl print system/ch.nmperformance.fithub.ram
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/ram.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/ram-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.ram
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.ram
```

Ripetendo l'installer viene riabilitato e riavviato.

## Calcolo della RAM

`sysctl -n hw.memsize` fornisce la RAM fisica totale. `vm_stat` fornisce
dimensione delle pagine e contatori. La stima utilizzata è:

```text
RAM impegnata = (Anonymous pages - Pages purgeable
                + Pages wired down + Pages occupied by compressor) × page size
Percentuale = RAM impegnata / RAM fisica totale × 100
```

Si includono memoria anonima delle applicazioni, memoria wired del sistema e
spazio fisico del compressore. Si escludono la cache file e le pagine purgeable
recuperabili. Non si usa `Pages stored in compressor`, che conta le pagine
logiche prima della compressione. La dimensione pagina viene letta dal sistema,
senza assumere 4 KiB o 16 KiB. I valori incoerenti o mancanti generano errore.

È una stima basata sui contatori VM, che può differire da Monitoraggio Attività;
non rappresenta l'indicatore macOS di pressione memoria. La soglia del 90%
riguarda questa percentuale. Si confronta il valore prima dell'arrotondamento;
nel messaggio percentuale e GiB sono arrotondati a due decimali.

Fonti: [contatori VM nel kernel Apple](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/mach/vm_statistics.h),
[definizioni memoria di Monitoraggio Attività](https://support.apple.com/guide/activity-monitor/view-memory-usage-actmntr1004/mac).

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -p '*monitormetrics.py' -v
```

I test RAM simulano i comandi macOS e Kuma, senza installare servizi o inviare
richieste di rete: verificano soglia, recupero, pagina 4/16 KiB, compressione,
letture errate, URL, messaggi e risposte fallite.

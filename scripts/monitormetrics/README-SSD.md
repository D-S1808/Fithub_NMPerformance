# SSD Mac mini → Uptime Kuma

`ssd.sh` controlla lo spazio dell'SSD interno ogni **60 secondi** e invia un
messaggio al proprio Push Monitor. Usa solo comandi inclusi in macOS.

| Occupazione | Stato Kuma | Messaggio |
| --- | --- | --- |
| <=85% | UP | Percentuale, GiB occupati/totali e liberi |
| >85% e <=90% | UP | AVVISO: soglia 85% superata |
| >90% | DOWN | CRITICO: soglia 90% superata |
| Lettura fallita | DOWN | SSD: lettura non riuscita |

Kuma non ha uno stato Warning per i Push Monitor: l'avviso è nel messaggio,
senza cambio di stato o notifica automatica. Il superamento del 90% provoca
DOWN e le notifiche abilitate nel monitor. Tornando sotto la soglia critica,
lo script invia UP. Esattamente 85% è normale; esattamente 90% è avviso.

## Configurazione sul Mac mini

Copia `ssd.sh` e `install-ssd-service.sh` nella cartella:

```text
/Users/admin/Documents/Fithub/scripts/monitormetrics/
```

Crea in Kuma un **nuovo Push Monitor**, chiamato `Mac mini - SSD`, con intervallo
**120 secondi**, per concedere margine agli invii nominali ogni 60 secondi.
Salva l'URL generato in `ssd-push-url.txt`, accanto a `ssd.sh`, su una sola
riga senza virgolette. Usa il token del monitor SSD, distinto da CPU e RAM.

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_SSD
```

Il file URL è escluso da Git. Se i file appartengono a root, correggi proprietario
e permessi dall'account admin:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" ssd.sh ssd-push-url.txt
chmod 700 ssd.sh
chmod 600 ssd-push-url.txt
```

Prova una lettura locale e poi un invio singolo:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/ssd.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/ssd.sh --once
```

Esempio di messaggio:

```text
SSD 86.00% (430.00/500.00 GiB) - liberi 70.00 GiB - AVVISO: soglia 85% superata
```

In Kuma apri il monitor SSD e passa il mouse sull'ultima barretta heartbeat.
La percentuale e l'avviso sono nel messaggio `msg`; il campo Ping resta vuoto.
Il token non viene stampato nei log né passato negli argomenti di curl.
Un invio richiede HTTP 2xx e JSON `ok: true`. Se la rete o Kuma non risponde,
il ciclo continua e Kuma può rilevare il mancato heartbeat.

Senza opzioni lo script continua nel Terminale, ogni 60 secondi; Ctrl+C termina.
`--once` fa un campione con invio, `--dry-run` fa un campione locale.
Codici di uscita: 0 normale/avviso, 1 soglia critica, 2 errore.
`KUMA_SSD_PUSH_URL` ha precedenza sul file URL; senza URL lo script avvisa
ed esegue solo letture locali.

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-ssd-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.ssd` con
`ssd.sh --service` ogni **60 secondi**, usando l'account che esegue
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
sudo launchctl print system/ch.nmperformance.fithub.ssd
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/ssd.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/ssd-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.ssd
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.ssd
```

Ripetendo l'installer viene riabilitato e riavviato.

## Cosa misura

Lo script legge `diskutil info -plist /System/Volumes/Data` e verifica che sia
un volume APFS su SSD interno. Usa `APFSContainerSize` e `APFSContainerFree`
per calcolare `(capacità - spazio libero) / capacità × 100` dell'intero
contenitore APFS di avvio, inclusi i volumi che condividono quello spazio.
Non misura eventuali altri contenitori/partizioni o dischi esterni.

Il singolo volume Data può riportare `FreeSpace` o `VolumeSize` uguali a zero:
questi campi non vengono usati come ripiego. Contatori mancanti, incoerenti o
relativi a un altro tipo di disco causano errore, senza inventare un valore.
Si legge un solo campione ogni minuto; non vengono eseguite scritture,
scansioni dei file o pulizie del disco.

I valori mostrati sono in **GiB** (1024³ byte). Lo spazio libero del contenitore
può differire dallo spazio disponibile mostrato dal Finder, che può includere
spazio recuperabile. Queste soglie riguardano l'occupazione del contenitore,
non l'usura o lo stato SMART dell'SSD. I confronti usano i byte prima
dell'arrotondamento dei messaggi.

Riferimento: [spazio condiviso dei volumi APFS nella documentazione Apple](https://support.apple.com/guide/disk-utility/add-delete-or-erase-apfs-volumes-dskua9e6a110/mac).

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -p '*monitormetrics.py' -v
```

I test SSD simulano diskutil e Kuma: verificano limiti 85/90%, recupero,
contenitore pieno/vuoto, dati APFS e volumi con campi a zero, errori di lettura
e invio, riservatezza del token e cadenza 60 secondi nel ciclo e nel plist.
Non installano servizi né modificano dischi.

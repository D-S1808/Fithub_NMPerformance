# Swap Mac mini → Uptime Kuma

`swap.sh` legge lo Swap di macOS ogni **30 secondi**, con messaggi e servizio
separati da CPU, RAM e SSD. Usa gli strumenti già presenti in macOS.

| Condizione | Stato Kuma |
| --- | --- |
| Swap usato <=1 GiB | UP |
| Swap usato >1 GiB | UP, messaggio AVVISO |
| Swap usato >2 GiB da meno di 5 minuti | UP, AVVISO con durata osservata |
| Swap usato >2 GiB per almeno 5 minuti | DOWN, CRITICO |
| Lettura o salvataggio dello stato falliti | DOWN, errore |

Le soglie iniziali sono una scelta operativa, non limiti prescritti da Apple.
Sono definite all'inizio dello script: `WARN_MIB=1024`, `CRITICAL_MIB=2048`,
`CRITICAL_SECONDS=300`. L'intervallo è `INTERVAL=30`.
Esattamente 1 GiB è normale; esattamente 2 GiB resta avviso e interrompe il
conteggio critico. Sotto la soglia critica il monitor torna subito UP.

Il warning è nel messaggio: Kuma non ha uno stato Push Warning separato e
non invia una notifica solo perché cambia quel testo. Abilita le notifiche
del monitor per ricevere l'allarme DOWN.

## Configurazione sul Mac mini

Copia `swap.sh` e `install-swap-service.sh` nella cartella:

```text
/Users/admin/Documents/Fithub/scripts/monitormetrics/
```

Crea in Kuma un nuovo monitor **Push**, nome `Mac mini - Swap`, intervallo
**60 secondi**. Salva il suo URL in `swap-push-url.txt` accanto allo script,
su una sola riga senza virgolette. Usa un token distinto dagli altri sensori.

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_SWAP
```

Se necessario correggi i proprietari, poi proteggi i file:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" swap.sh swap-push-url.txt
chmod 700 swap.sh
chmod 600 swap-push-url.txt
```

Prova una lettura locale e poi un invio singolo:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/swap.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/swap.sh --once
```

Esempio:

```text
SWAP 75.00% (3.00/4.00 GiB) - liberi 1.00 GiB - AVVISO: oltre 2048 MiB da 60/300s
```

Passa il mouse sull'ultima barretta heartbeat del monitor per vedere il valore.
Il dato è inviato in `msg`, non nel campo Ping. Il token non compare nei log
o negli argomenti di curl. HTTP 2xx e JSON `ok: true` confermano l'invio.

Senza opzioni il ciclo continua ogni 30 secondi fino a Ctrl+C. `--once` esegue
un campione con invio; `--dry-run` ignora l'URL e non crea/modifica la cronologia.
Codici: 0 normale/avviso, 1 soglia critica prolungata, 2 errore.
`KUMA_SWAP_PUSH_URL` ha precedenza sul file. Senza URL lo script avvisa e
monitora solo localmente.

## Servizio in background nel progetto

Ferma l'eventuale ciclo manuale con Ctrl+C ed esegui dall'account admin:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-swap-service.sh
```

L'installer prepara il LaunchDaemon `ch.nmperformance.fithub.swap` con
`swap.sh --service` ogni **30 secondi**, usando l'account che esegue
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
sudo launchctl print system/ch.nmperformance.fithub.swap
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/swap.log
tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/swap-error.log
```

Tra due campioni `not running` è normale, perché il job usa `--service` (un solo campione).
I log non hanno rotazione automatica. Il monitor parte ai boot successivi;
con FileVault occorre prima sbloccare il disco. Durante lo stop non invia dati.
Per i permessi di accesso a Documents e la migrazione di tutti gli otto servizi,
vedi la [guida comune](README.md).

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.swap
```

Per impedirne anche la partenza ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.swap
```

Ripetendo l'installer viene riabilitato e riavviato.

## Come funziona il conteggio

La cronologia registra il boot, l'inizio del periodo oltre 2 GiB e l'ultimo
campione. Il primo campione alto avvia il conteggio; dopo **300 secondi reali**
di campioni alti regolari viene inviato DOWN: normalmente sono 11 letture a
distanza di 30 secondi, dalla prima all'undicesima.

Un riavvio, una lettura fallita, il ritorno a <=2 GiB, l'orologio che torna
indietro o un intervallo **oltre 75 secondi** tra campioni azzera il conteggio.
Un errore di rete non azzera le letture hardware già raccolte. Una cronologia
mancante o non valida fa ripartire l'osservazione, senza inventare una durata.
Il salvataggio è atomico; `lockf` impedisce scritture da due istanze contemporanee.
Il file `.lock` resta presente anche senza processi attivi: non cancellarlo.

La cronologia è unica per prove manuali e servizio: `swap-state/` accanto
allo script. L'installer preserva quella già presente e recupera la vecchia
solo se manca. Restano valide le regole di reset per boot, soglia e gap oltre
75 secondi. `--dry-run` può leggerla ma non la aggiorna. Stato e URL sono
esclusi da Git; non avviare un ciclo manuale insieme al servizio.

## Cosa misura

`sysctl -n vm.swapusage` fornisce spazio Swap allocato, usato e libero.
La percentuale mostrata è `usato / allocato × 100`, oppure 0 se non c'è Swap
allocato. L'allocazione può crescere dinamicamente: **gli allarmi usano i GiB
assoluti**, non questa percentuale. Non si divide lo Swap per la RAM fisica.
Non si misura qui la velocità di swap-in/out o la pressione memoria macOS.

I dati sono in GiB/MiB binari e arrotondati nel messaggio; i confronti avvengono
prima dell'arrotondamento. Lo script legge soltanto le metriche: non svuota
lo Swap e non modifica la memoria virtuale.

Fonti: [struttura Swap nel kernel Apple](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/sysctl.h),
[definizione dello Swap in Monitoraggio Attività](https://support.apple.com/guide/activity-monitor/view-memory-usage-actmntr1004/mac).

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -p '*monitormetrics.py' -v
```

I test simulano sysctl, tempo e Kuma, senza modificare Swap o installare servizi.
Coprono soglie, durata persistente tra processi, recupero, riavvii, gap, errori,
esecuzioni concorrenti e configurazione `launchd` ogni 30 secondi.

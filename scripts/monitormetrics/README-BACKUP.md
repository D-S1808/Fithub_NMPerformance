# Stato backup FitHub → Uptime Kuma

`backup.sh` controlla ogni **60 secondi** l'ultimo tentativo di backup applicativo
registrato dai tuoi script. Non esegue backup, restore o pulizie.

| Condizione | Stato Kuma |
| --- | --- |
| Ultimo tentativo riuscito, età <=30 ore | UP |
| Ultimo tentativo riuscito, età >30 e <=36 ore | UP, AVVISO |
| Ultimo tentativo riuscito, età >36 ore | DOWN, CRITICO |
| Ultimo tentativo registrato fallito | DOWN, FALLITO |
| Nessun backup registrato o stato non leggibile/valido | DOWN |

Le soglie iniziali sono pensate per un backup giornaliero e possono essere
cambiate nelle costanti `WARN_SECONDS=108000` e `MAX_AGE_SECONDS=129600`.
Il warning è nel messaggio, senza cambio di stato o notifica automatica;
abilita le notifiche del monitor per gli allarmi DOWN.

Il sensore considera `daily`, `weekly` e `monthly` e sceglie il tentativo più
recente usando `finished_at`. Un successo successivo a un fallimento ripristina
UP se ancora recente. A parità di timestamp prevale il fallimento. Verifiche,
sincronizzazioni e inizializzazioni non rendono più recente il backup.
Non applica scadenze settimanali o mensili indipendenti.

## File da trasferire e configurazione

Sul Mac mini copia:

- `backup.sh` e `install-backup-service.sh` in `Fithub/scripts/monitormetrics/`;
- **il motore aggiornato `scripts/backup/backup.py`**, sostituendo la copia
  precedente in `Fithub/scripts/backup/backup.py`. Conserva il tuo `backup.json`.

L'aggiornamento del motore è necessario per pubblicare i nuovi esiti al monitor.
Restano validi gli stessi requisiti Python, Docker e restic dei backup esistenti.
Il monitor, invece, usa solo strumenti inclusi in macOS e non richiede Python.

Crea in Kuma un nuovo monitor **Push**, nome `Mac mini - Backup`, con intervallo
**120 secondi**. Salva il suo URL in `backup-push-url.txt`, accanto a `backup.sh`,
su una sola riga senza virgolette. Usa un token dedicato.

```text
https://monitoring.nmperformance.ch/api/push/TOKEN_DEL_MONITOR_BACKUP
```

Se necessario correggi proprietario e permessi:

```bash
cd /Users/admin/Documents/Fithub/scripts/monitormetrics
sudo chown "$(id -un)" backup.sh backup-push-url.txt
chmod 700 backup.sh
chmod 600 backup-push-url.txt
```

Installa il servizio dall'account admin, lo stesso con cui esegui i backup:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/install-backup-service.sh
```

L'installer crea il plist e avvia il LaunchDaemon
`ch.nmperformance.fithub.backup`, con `backup.sh --service` ogni **60 secondi**.
Il plist separato nel progetto è solo un riferimento per installazione manuale
con utente admin. Il sensore non modifica le pianificazioni degli altri job.

## Collegamento con gli script di backup

Gli stati completi restano in `Fithub/backups/status-*.json`, dentro Documents.
Il sensore legge la copia minima degli stati nella directory richiesta:

```text
/Users/admin/Documents/Fithub/scripts/monitormetrics/
```

Il motore aggiornato vi scrive, al termine di daily/weekly/monthly, soltanto:

```json
{"ok": true, "finished_at": "2026-10-26T18:00:00.123456+01:00"}
```

I file si chiamano `status-daily.json`, `status-weekly.json` e
`status-monthly.json` e sono esclusi da Git. L'installer imposta la cartella
con permessi 700 per l'utente del servizio. I file non contengono password,
documenti, dump, percorsi di log o dettagli degli errori. Esegui i backup come
admin, senza `sudo`, così il monitor può leggere i file pubblicati.
`backup.py` ricava questa directory dalla root configurata: `scripts/monitormetrics`
sotto Fithub. Il sensore usa la propria cartella (`SCRIPT_DIR`) e l'installer
ricava la root risalendo di due directory. Mantieni la struttura del progetto
e la stessa root in `backup.json`; non occorre modificare percorsi nel codice.

Gli esiti sono ora in Documents. Se la lettura dal Terminale funziona ma quella
tramite launchd fallisce, verifica i permessi Privacy di macOS per il processo
che legge i file: i permessi POSIX 700/600 non risolvono i blocchi Privacy.

L'installer importa una copia minima degli stati già presenti nella cartella
standard `Fithub/backups/`, se non sono ancora pubblicati. Recupera anche
la vecchia cartella di monitoring come ripiego se manca lo stato originale.
Non sovrascrive stati di monitoring già esistenti. La root configurata in
`backup.json` deve corrispondere al progetto che contiene questo sensore.

Se non esistono stati precedenti, il monitor resterà DOWN fino al primo backup
completato e pubblicato. Un backup in corso non viene contato come un nuovo
successo prima del completamento. Un errore di pubblicazione viene scritto nel
log del backup senza alterarne il vero esito; il monitor continuerà a vedere
l'ultimo stato pubblicato, che potrà scadere.

**Aggiornare solo il sensore senza aggiornare `backup.py` non basta:** gli stati
importati all'installazione non verrebbero aggiornati dalle vecchie esecuzioni.

## Prova, messaggi e log

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/backup.sh --dry-run
/bin/bash /Users/admin/Documents/Fithub/scripts/monitormetrics/backup.sh --once
```

Esempi di messaggi:

```text
Backup daily: ultimo successo 2026-10-26T18:00:00+01:00 - eta 2.0h - OK
Backup weekly: ultimo successo 2026-10-24T18:00:00+02:00 - eta 49.0h - CRITICO: oltre 36 ore
Backup: ultimo tentativo daily FALLITO (2026-10-26T18:00:00+01:00)
```

In Kuma apri il monitor e passa il mouse sull'ultima barretta heartbeat. Si usa
`msg`, non Ping. Non vengono inviati dettagli grezzi degli errori o dei log.
L'invio richiede HTTP 2xx e JSON `ok: true`; il token non compare nei log
o negli argomenti di curl. Un errore di invio non interrompe il ciclo.

Senza opzioni il ciclo continua ogni 60 secondi; Ctrl+C termina. `--once`
esegue un controllo e invio, `--dry-run` legge solo gli stati locali.
Codici: 0 normale/avviso, 1 backup assente/scaduto/fallito, 2 stato non valido
o errore di lettura/configurazione/invio. `KUMA_BACKUP_PUSH_URL` ha precedenza
sul file URL; senza URL lo script avvisa ed esegue controlli locali.

```bash
sudo launchctl print system/ch.nmperformance.fithub.backup
sudo tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/backup.log
sudo tail -n 10 /Users/admin/Documents/Fithub/logs/system/monitormetrics/backup-error.log
```

Tra due controlli `not running` è normale. I log non hanno rotazione automatica.
Il servizio legge direttamente `backup.sh` e l'URL nella cartella del progetto;
le modifiche sono lette alla successiva esecuzione. Ripeti l'installer per
aggiornare il plist o migrare il servizio. Gli stati già pubblicati restano presenti.
In `/Library/` viene installato soltanto il plist in `LaunchDaemons`.
La [guida comune](README.md) descrive la migrazione di tutti gli otto monitor.
Con FileVault occorre sbloccare il disco al boot; durante lo stop non esegue
controlli. Il sensore legge gli esiti del produttore e non verifica autonomamente
integrità, copie cloud/SSD o ripristinabilità dei dati: per quelli restano le
procedure di verifica degli script di backup.

Per fermarlo fino al prossimo boot:

```bash
sudo launchctl bootout system/ch.nmperformance.fithub.backup
```

Per impedirne anche il caricamento ai boot successivi, esegui poi:

```bash
sudo launchctl disable system/ch.nmperformance.fithub.backup
```

Ripetendo l'installer viene riabilitato e avviato.

## Verifica nello sviluppo

```bash
python3 -m unittest discover -s tests -p 'test_backup*.py' -v
```

I test coprono età, errori, priorità dei tentativi, fusi orari, date non valide,
stati iniziali e invii simulati. Eseguono realmente il lettore Foundation sui
JSON temporanei. I test del motore verificano anche export minimale, errori di
pubblicazione e regressioni dei backup, senza eseguire operazioni di produzione.

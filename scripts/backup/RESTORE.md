# Appunti di ripristino FitHub

## Cosa conservare

Per ripristinare FitHub occorrono i file applicativi e i dump PostgreSQL
**della stessa esecuzione di backup**. Il daily salva già entrambi nello
snapshot restic; il weekly salva un pacchetto con archivio applicativo e dump.

Una copia completa del repository restic su cloud o SSD contiene snapshot,
file e dump. Non richiede la copia locale del Mac mini. Servono inoltre la
password del repository e, per il cloud, le credenziali del servizio: conservale
in un password manager accessibile anche dopo la perdita del Mac.

Un solo file della sottocartella `snapshots` o il solo ID non bastano: i dati
deduplicati si trovano nel resto del repository. Per spostare il backup usa
`run.sh sync`, che esegue `restic copy` e trasferisce tutti i blocchi necessari.
Le destinazioni contengono i dump cifrati insieme agli altri file, non dump
separati in chiaro da abbinare manualmente.

## Prova di ripristino senza sovrascrivere la produzione

Sul Mac mini, con gli script e la configurazione installati:

```bash
# Repository locale
/bin/bash "/Users/admin/Documents/Fithub/scripts/backup/verify-backup.sh" --restore-test

# Repository cloud
/bin/bash "/Users/admin/Documents/Fithub/scripts/backup/verify-backup.sh" --cloud --restore-test

# Repository SSD montato sotto /Volumes
/bin/bash "/Users/admin/Documents/Fithub/scripts/backup/verify-backup.sh" --ssd --restore-test
```

Si può aggiungere `--snapshot ID` per selezionare una copia storica, usando
l'ID del repository scelto. Il test estrae file e dump, verifica i checksum e
importa i database in un container temporaneo isolato. Poi elimina i file
temporanei: questa è una prova, non un ripristino permanente dell'applicazione.
Il container usa l'immagine PostgreSQL registrata nel manifest, che deve essere
presente localmente. Non usa i volumi PostgreSQL di produzione.

## Recupero permanente dei file dopo un guasto

Non inizializzare di nuovo il repository e non generare una nuova password.
Installa restic sul computer di recupero e rendi disponibili repository e
password. Per il cloud servono anche le credenziali del provider. Questi comandi
di esempio leggono un SSD: sostituisci `NOME_SSD` e `ID_SNAPSHOT` con i valori reali.

Elenca gli snapshot disponibili:

```bash
restic --repo "/Volumes/NOME_SSD/FitHub-backup/restic" \
  --password-file "/Users/admin/.config/fithub-backup/restic-ssd-password" \
  snapshots --host fithub-mac-mini --tag fithub
```

Estrai lo snapshot completo in una directory **nuova e vuota**, separata
dalla produzione:

```bash
restic --repo "/Volumes/NOME_SSD/FitHub-backup/restic" \
  --password-file "/Users/admin/.config/fithub-backup/restic-ssd-password" \
  restore ID_SNAPSHOT --target "/Users/admin/FitHub-ripristino" --verify
```

Per il repository locale usa invece `/Users/admin/Documents/Fithub/backups/restic`
e la password `restic-password`. Per il cloud usa l'URL `cloud_repository` e la
password `restic-cloud-password`, con le credenziali del servizio configurate.
Puoi eseguire l'estrazione senza avviare PostgreSQL di produzione.

Restic conserva il percorso originale sotto la directory di estrazione. Per
un daily del server attuale troverai:

```text
/Users/admin/FitHub-ripristino/Users/admin/Documents/Fithub/
├── FitHub/
├── configs/
├── library/
├── scripts/
├── SQL/
├── srv/
└── backups/database/current/
    ├── database/
    │   ├── db-001.dump
    │   ├── db-002.dump
    │   └── globals.sql
    ├── manifest.json
    └── checksums.json
```

Per il weekly troverai invece `backups/weekly/DATA-ORA-ID/`, con i dump e
`application.tar.gz`. L'archivio contiene i file applicativi da estrarre nella
directory di recupero. Leggi `manifest.json` per conoscere il nome reale
associato a ogni dump: non basarti soltanto sul numero nel nome del file.

## Ripristino PostgreSQL e riapertura di FitHub

1. Recupera configurazioni e applicazione dalla stessa copia scelta per i dump.
2. Prepara un nuovo PostgreSQL con versione compatibile, estensioni e tablespace
   necessari. Il manifest registra l'immagine usata al momento del backup.
3. Usa un amministratore temporaneo distinto dai ruoli salvati per importare
   `database/globals.sql` con `psql` e `ON_ERROR_STOP=1`.
4. Importa ciascun `.dump` con `pg_restore --create --exit-on-error`, collegandoti
   inizialmente a `template1`. Nel nuovo cluster possono già esistere database
   come `postgres`: la loro sostituzione va gestita esclusivamente nella
   destinazione nuova, come fa il test isolato degli script.
5. Controlla database, permessi, documenti e configurazioni, quindi riavvia FitHub
   e verifica le funzioni applicative prima di riaprire l'accesso agli utenti.

Il ripristino dei file con restic non importa automaticamente i database:
prima restic recupera i dump, poi PostgreSQL li importa. Non copiare i dump
dell'ultimo `current` locale sopra quelli di uno snapshot storico.

Ogni dump di un singolo database è consistente. Database diversi e documenti
non sono acquisiti in una sola transazione: per una coerenza completa tra file
e record occorre sospendere le scritture applicative durante il backup.

Riferimenti: [restic copy e repository](https://restic.readthedocs.io/en/stable/045_working_with_repos.html),
[restore restic](https://restic.readthedocs.io/en/stable/050_restore.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html).

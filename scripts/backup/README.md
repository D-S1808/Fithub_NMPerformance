# Backup FitHub sul Mac mini

Questi file vanno copiati in `Fithub/scripts/backup/` sul Mac mini. Non occorre
trasferire qui i dati dei clienti o il codice dell'applicazione. La struttura è
basata su `fithubStructure.txt`; il contenuto dei Compose non era disponibile.

## Cosa eseguono

| Script | Operazione |
| --- | --- |
| `backup-daily.sh` | Dump completi PostgreSQL e snapshot restic cifrato e deduplicato dei file |
| `backup-weekly.sh` | Pacchetto completo, snapshot restic e test reale di ripristino database |
| `verify-backup.sh` | Ripristino temporaneo dei file dallo snapshot, checksum e controllo dei dump |
| `backup-monthly.sh` | Backup settimanale con test DB, poi Time Machine sull'SSD già configurato |
| `run.sh` | Inizializzazione repository, copie cloud/SSD e comandi avanzati |

Gli script usano Bash di macOS e un motore Python condiviso, senza pacchetti pip.
Ogni avvio esegue subito l'operazione richiesta: non ci sono orari, giorni
obbligatori o una pianificazione interna. I nomi daily, weekly e monthly
identificano il tipo di backup; la frequenza viene decisa da chi li esegue.
Il giornaliero include anche codice sorgente e SQL: un file modificato dopo il
settimanale è quindi recuperabile dal successivo giornaliero.

Il container PostgreSQL viene individuato con `configs/fithub-postgres17.yml`.
Se presente, viene passato `configs/.env`. L'utente viene letto da
`POSTGRES_USER`; con `databases: []` si salvano **tutti i database non template
che accettano connessioni**, incluso `postgres`. I dump utilizzano gli strumenti
già presenti nel container, con la stessa versione del server.

Le credenziali PostgreSQL restano nel container. È supportato anche
`POSTGRES_PASSWORD_FILE`. L'utente deve poter leggere tutti i database e i
globals; normalmente si usa l'utente amministratore del container. Non vengono
avviati, fermati o ricreati servizi di produzione.

## 1. Copia e configurazione iniziale

Sul Mac mini occorrono Docker con Compose v2, **CPython 3.14.9 o patch stabile
successiva del ramo 3.14** e restic >= 0.17. Baseline verificata il 22 ottobre
2026 sulla [release ufficiale](https://www.python.org/downloads/release/python-3149/).
La precedente indicazione `Python >= 3.9` è sostituita da questo requisito.
Il ramo 3.9 è fuori supporto; la compatibilità del codice non basta a rendere
appropriata una versione per l'installazione sul server.

Se utilizzi Homebrew:

```bash
brew update
brew install python@3.14 restic
python3.14 --version
```

Se il pacchetto è già installato, aggiorna con `brew upgrade python@3.14`.
Homebrew deve fornire almeno 3.14.9; il controllo nello script blocca patch
precedenti. In alternativa usa l'installer macOS dalla pagina ufficiale del
rilascio. `run.sh` cerca esplicitamente `python3.14`, senza ripiegare su un
generico Python di sistema. Il controllo viene eseguito prima di leggere le
configurazioni o creare file di backup e rifiuta prerelease e altri rami.
Questa è una scelta di deployment: non significa che tutti gli altri rami
attualmente supportati siano vulnerabili. Le patch successive di 3.14 sono
accettate; il passaggio a un nuovo ramo richiede aggiornamento della policy e test.

3.14.9 include l'aggiornamento di pip/certifi per l'incidente dei certificati
eMudhra/emSign. Gli script non richiedono pip, pacchetti PyPI o ambienti virtuali.
Il livello minimo è una baseline alla data indicata, non una garanzia per future
vulnerabilità: controlla i nuovi avvisi e mantieni aggiornate le patch Python,
restic e Docker. Non si installano aggiornamenti automaticamente durante un backup.

Apri il terminale nella cartella **Fithub esterna**, quella con `configs`, `srv`,
`library`, `FitHub`, `SQL` e `scripts`. Copia in essa la cartella `scripts/backup`
di questo progetto, senza cancellare eventuali altri script già esistenti.

```bash
cp -n scripts/backup/backup.example.json scripts/backup/backup.json
chmod 600 scripts/backup/backup.json
```

`root: "../.."` è relativo alla posizione di `backup.json`, quindi non dipende
dal nome dell'account del Mac. Puoi usare un percorso assoluto se necessario.

Se Compose non individua esattamente un container, imposta `postgres_container`
al suo nome reale. Questo serve anche se il progetto Compose è stato avviato
con un nome personalizzato (`-p`). `postgres_user` e `databases` consentono
override espliciti. Le cartelle elencate in `required_paths` devono esistere,
anche se vuote: in caso contrario lo script termina con errore.

Genera la password del repository locale **fuori dalla directory FitHub**:

```bash
umask 077
mkdir -p "$HOME/.config/fithub-backup"
(
  set -C
  openssl rand -base64 48 > "$HOME/.config/fithub-backup/restic-password"
)
chmod 600 "$HOME/.config/fithub-backup/restic-password"
bash scripts/backup/run.sh init-local
```

Esegui la generazione della password una sola volta; rigenerarla non cambia la
password di un repository già creato. Conservala anche in un password manager
accessibile dopo un guasto del Mac: senza password il repository non si ripristina.

## 2. Primo backup e prova

```bash
bash scripts/backup/backup-daily.sh
bash scripts/backup/verify-backup.sh --restore-test
bash scripts/backup/backup-weekly.sh
```

Il settimanale esegue già `--restore-test`. Per eseguire un settimanale senza
test DB durante una diagnosi: `bash scripts/backup/run.sh weekly`.

Ogni esecuzione restituisce codice 0 se completata e un codice diverso da zero
in caso di errore. Il percorso del log viene stampato al termine. I dettagli
e l'output di Docker/restic restano separati in `backups/logs/daily/`,
`backups/logs/weekly/` e `backups/logs/monthly/`, con un file per esecuzione.
Le operazioni di verifica, sincronizzazione e inizializzazione usano rispettivamente
le sottocartelle `verify/`, `sync/`, `init-local/`, `init-cloud/` e `init-ssd/`.
Il backup applicativo eseguito dal monthly viene registrato nello stesso log monthly.
I vecchi log già presenti direttamente in `backups/logs/` restano nella posizione originale.
Lo stato viene scritto in
`backups/status-daily.json`, `status-weekly.json`, `status-verify.json`, ecc.
Durante un'operazione lunga puoi leggere il log in un secondo terminale.

La verifica ripristina l'intero snapshot in una directory temporanea sotto
`backups/`, verifica i file con restic e i checksum SHA-256, e controlla la
presenza delle cartelle richieste. Per verificare anche tutti i blocchi del
repository: `bash scripts/backup/verify-backup.sh --read-data --restore-test`.
Con `--cloud` o `--ssd` la verifica legge solo la destinazione esterna scelta.
Servono spazio libero per lo snapshot estratto e spazio Docker per il DB di prova.

Il test crea un container senza rete né porte pubblicate e senza montare i
volumi di produzione. Ripristina ruoli, database, dati e permessi, esegue una
query sulle tabelle, poi elimina container e volumi temporanei anche se il
restore fallisce. Usa l'ID immutabile dell'immagine PostgreSQL registrato nel
manifest: quell'immagine deve essere ancora disponibile localmente. Su un
nuovo Mac occorre prima installare un'immagine compatibile e adattare il test.
Tablespace esterni o estensioni non presenti nell'immagine richiedono preparazione
specifica; il test segnala l'errore e non considera riuscito il backup.

## 3. Contenuto e conservazione

```text
Fithub/backups/
├── restic/                         # cronologia locale cifrata
├── database/current/               # ultimo dump completo, globals e metadata
├── weekly/AAAAmmgg-HHMMSS-id/
│   ├── application.tar.gz          # applicazione, configurazioni, clienti, library, SQL
│   ├── database/                   # un dump per database e globals.sql
│   ├── manifest.json               # nomi DB, immagine PostgreSQL, percorsi, data
│   └── checksums.json              # SHA-256 di tutti i file del pacchetto
├── logs/
│   ├── daily/
│   ├── weekly/
│   ├── monthly/
│   ├── verify/
│   ├── sync/
│   ├── init-local/
│   ├── init-cloud/
│   └── init-ssd/
└── status-*.json
```

Le vecchie cartelle `backups/clients`, `backups/library`, ecc. non vengono né
incluse ricorsivamente né cancellate. Si escludono i log applicativi, `.git`,
`node_modules` e `.DS_Store`. I JAR, `target`, i sorgenti, i file nascosti come
`configs/.env` e le chiavi dell'applicazione sono inclusi. Le dipendenze Node si
reinstallano dal lockfile. I symlink vengono salvati come link, senza seguire
dati esterni alla directory: i percorsi obbligatori non possono essere symlink.

Eventuali bind mount PostgreSQL che contengono `PGDATA` vengono esclusi
automaticamente. Altri percorsi live da non copiare (per esempio Redis con dati
su disco o directory esterne dedicate) vanno aggiunti a `exclude_paths` dopo
aver controllato i Compose. I volumi Docker nominati non si trovano nella
directory FitHub e non vengono copiati. Questi script salvano PostgreSQL;
Redis è considerato cache ricostruibile. Se Redis contiene dati indispensabili,
occorre aggiungere una sua procedura di backup consistente.

La retention del repository è separata per host e tipo, **non per percorso**:

- Daily: 30 giorni con snapshot disponibili.
- Weekly: 8 settimane e 12 mesi con snapshot disponibili.
- Pacchetti settimanali locali in chiaro: ultimi 2, configurabili con
  `keep_weekly_bundles`; la cronologia lunga resta in restic.

La retention si applica solo dopo backup, controlli e sincronizzazione cloud
e SSD configurati riusciti. La pulizia fisica restic (`prune`) avviene nel settimanale. Dopo un
errore i pacchetti incompleti restano disponibili per diagnosi e non vengono
rimossi automaticamente. I log non hanno rotazione automatica in questa versione.

I dump in staging e i pacchetti settimanali sono **in chiaro**, con permessi
privati; contengono anche configurazioni, chiavi e hash delle password DB.
La cifratura restic copre i repository locale, cloud e SSD. Usa FileVault per
proteggere anche i file temporanei sul disco del Mac mini.

Ogni dump DB è consistente, ma dump di database diversi e file applicativi
non rappresentano un'unica transazione. Per un punto di ripristino perfettamente
coerente tra documenti e record, sospendi temporaneamente le scritture
applicative durante il backup. Gli script non impongono questa sospensione.

## 4. Cloud opzionale

Inizialmente `cloud_repository` e `ssd_mount` sono `null`: il log indica **SOLO LOCALE**.
Quando sarà scelto il provider, imposta l'URL repository restic e, se servono
credenziali, `cloud_env_file` a un file JSON privato fuori da FitHub. Per S3 la
mappa contiene, ad esempio, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` e
`AWS_DEFAULT_REGION`, con i valori del proprio account. Il file non è uno script
shell e non viene eseguito. Provider via rclone richiedono anche rclone configurato.

```bash
umask 077
(
  set -C
  openssl rand -base64 48 > "$HOME/.config/fithub-backup/restic-cloud-password"
)
chmod 600 "$HOME/.config/fithub-backup/restic-cloud-password"
bash scripts/backup/run.sh init-cloud
bash scripts/backup/run.sh sync
bash scripts/backup/verify-backup.sh --cloud --restore-test
```

Anche la password cloud si genera una sola volta e si conserva fuori dal Mac.
Da quel momento ogni backup copia automaticamente nel cloud gli snapshot locali
non ancora trasferiti, inclusi quelli rimasti in attesa dopo un guasto di rete.
Un errore cloud rende fallita l'esecuzione e blocca la retention, conservando
lo snapshot locale. `sync` consente di riprovare senza creare un nuovo backup.
Usa repository dedicati a FitHub, con URL privi di password incorporate.

La copia esterna è autonoma: `restic copy` trasferisce lo snapshot con tutti
i blocchi necessari, inclusi i dump DB di quell'esecuzione, i globals, il manifest
e i checksum. Non viene copiato solo l'ID o il piccolo file descrittivo dello snapshot.
La cartella locale `database/current` non serve per ripristinare un repository
cloud/SSD già copiato. Ogni dump è abbinato ai file dello stesso snapshot, anche
quando il daily locale successivo ha già sostituito `current`.

Dopo ogni copia, lo script esegue `restic check` e verifica che tutti gli snapshot
locali selezionati siano presenti nella destinazione con lo stesso albero di dati
e gli stessi percorsi. L'ID esterno può essere diverso da quello locale: restic
conserva il riferimento all'originale. Per il test reale del ripristino usa
`verify-backup.sh --cloud --restore-test` oppure `--ssd --restore-test`.
Il controllo di struttura non sostituisce questo test né una lettura periodica
di tutti i blocchi con `--read-data`.

### Copia applicativa su SSD sotto /Volumes

Questa copia restic è distinta dal mensile Time Machine. Aggiungi a `backup.json`
questi campi, se usi una configurazione creata con la precedente versione:

```json
"ssd_mount": "/Volumes/NOME_SSD",
"ssd_repository": "FitHub-backup/restic",
"ssd_password_file": "~/.config/fithub-backup/restic-ssd-password"
```

Sostituisci `NOME_SSD` con il nome reale. Lo script richiede che sia esattamente
un volume montato sotto `/Volumes` e che il repository rimanga al suo interno.
Non crea il mount point se il disco è assente. La cartella del repository sarà
`/Volumes/NOME_SSD/FitHub-backup/restic`. Se utilizzi anche Time Machine, scegli
un volume separato per la copia restic.

Per la prima inizializzazione soltanto, genera e conserva la password SSD:

```bash
umask 077
mkdir -p "$HOME/.config/fithub-backup"
(
  set -C
  openssl rand -base64 48 > "$HOME/.config/fithub-backup/restic-ssd-password"
)
chmod 600 "$HOME/.config/fithub-backup/restic-ssd-password"
bash scripts/backup/run.sh init-ssd
bash scripts/backup/run.sh sync --ssd
bash scripts/backup/verify-backup.sh --ssd --restore-test
```

`set -C` impedisce di sovrascrivere una password già presente. Se il repository
SSD esiste già, usa la sua password originale e non ripetere `init-ssd`.

Dopo la configurazione, daily e weekly copiano automaticamente verso tutte le
destinazioni abilitate. `run.sh sync` riprova entrambe senza generare nuovi dump;
`sync --cloud` e `sync --ssd` selezionano una sola destinazione. Se una copia
fallisce, l'altra viene comunque tentata e l'esecuzione termina con errore,
senza avviare la retention. Un SSD configurato ma scollegato è un errore, non
un successo silenzioso; `ssd_mount: null` disabilita intenzionalmente la copia.

I dati esterni rimangono cifrati nel repository. Dump e file sono recuperabili
insieme con una sola estrazione dello snapshot, senza esportare password DB e
documenti in chiaro sul cloud o sull'SSD.

## 5. Avvio immediato da Comandi Rapidi

Gli script non aspettano un orario e non saltano determinati giorni: appena
richiamati iniziano i controlli e il backup. L'eventuale automazione della
frequenza è gestita esternamente in Comandi Rapidi sul Mac mini.

In Comandi Rapidi crea un comando con l'azione **Esegui script shell**. Scegli
`/bin/bash` come shell e inserisci uno dei comandi seguenti, sostituendo
`/percorso/reale/Fithub` con la directory principale sul Mac mini.

Daily:

```bash
/bin/bash "/percorso/reale/Fithub/scripts/backup/backup-daily.sh"
```

Weekly, incluso il test di ripristino:

```bash
/bin/bash "/percorso/reale/Fithub/scripts/backup/backup-weekly.sh"
```

Monthly, con l'SSD Time Machine collegato e configurato:

```bash
/bin/bash "/percorso/reale/Fithub/scripts/backup/backup-monthly.sh"
```

Verifica separata dei backup con test DB:

```bash
/bin/bash "/percorso/reale/Fithub/scripts/backup/verify-backup.sh" --restore-test
```

Usa una chiamata diretta, senza `&`, `nohup` o l'avvio in un altro Terminale:
Comandi Rapidi potrà attendere l'esito del processo e ricevere un codice diverso
da zero in caso di errore. Non serve cambiare directory: i percorsi vengono
calcolati rispetto a `backup.json` e alla posizione degli script.

Se macOS lo richiede, abilita **Consenti l'esecuzione di script** nelle impostazioni
avanzate di Comandi Rapidi. Verifica un primo avvio dalla stessa app per controllare
accesso a Documents e Docker: i permessi del Terminale non confermano quelli
di Comandi Rapidi. Docker deve essere avviato nell'account che esegue il comando.
I requisiti specifici di Time Machine sono descritti nella sezione seguente.

Una seconda esecuzione contemporanea viene rifiutata con errore. Lo stato JSON
e i log restano locali; non sono collegati a notifiche email o a monitoraggio.
Non viene installata né modificata alcuna automazione sul Mac.

Se sul Mac mini avevi già installato i due LaunchAgents della precedente versione,
disattivali prima di usare la tua automazione per evitare avvii duplicati:

```bash
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/ch.nmperformance.fithub.backup-daily.plist"
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/ch.nmperformance.fithub.backup-weekly.plist"
```

Questi due comandi servono soltanto per vecchi job già caricati; in seguito
sposta fuori da `Library/LaunchAgents` i due plist per impedirne il caricamento
al prossimo login. Il pacchetto attuale non contiene un generatore di pianificazioni.

Riferimento: [consentire gli script in Comandi Rapidi](https://support.apple.com/guide/shortcuts-mac/apdfeb05586f/mac).

## 6. Backup mensile su SSD

Configura prima l'SSD cifrato nelle impostazioni di Time Machine. Ricava l'ID con
`tmutil destinationinfo`, quindi imposta `time_machine_destination_id` e
`time_machine_mount` (per esempio `/Volumes/FitHub Time Machine`) nel JSON.

Con SSD collegato e montato:

```bash
bash scripts/backup/backup-monthly.sh
```

Lo script controlla ID e mount point, crea un backup applicativo fresco con
test DB, avvia Time Machine in attesa del completamento e verifica che esista
un nuovo backup completato sulla destinazione scelta. Può richiedere permessi
amministratore/Accesso completo al disco secondo la configurazione macOS.
Non formatta, configura o espelle il disco. Dopo il successo espellilo dal
Finder e scollegalo. La cadenza mensile richiede il collegamento fisico dell'SSD.

Time Machine salva i dati recuperabili tramite gli strumenti macOS, non è un
clone avviabile né un full indipendente nuovo ogni mese. La sua retention è
gestita da macOS e dallo spazio disponibile: i 12 mesi sopra riguardano restic.
Controlla nelle opzioni che FitHub, inclusa `backups`, non sia escluso.

## 7. Ripristino operativo

La procedura da conservare separatamente è in [RESTORE.md](RESTORE.md).
Per il ripristino completo occorrono i file applicativi e i dump DB della stessa
esecuzione: entrambi sono già contenuti nello snapshot scelto. Basta quindi
un repository esterno completo e la relativa password; non occorre anche
la cartella locale `database/current` del Mac mini.

Usa prima `verify-backup.sh --restore-test` (eventualmente `--cloud` e
`--snapshot ID`). Questo verifica senza sovrascrivere la produzione. Per un
ripristino reale estrai lo snapshot restic in **una directory nuova**:

```bash
restic --repo /percorso/Fithub/backups/restic \
  --password-file "$HOME/.config/fithub-backup/restic-password" snapshots
restic --repo /percorso/Fithub/backups/restic \
  --password-file "$HOME/.config/fithub-backup/restic-password" \
  restore ID_SNAPSHOT --target /percorso/nuovo/ripristino --verify
```

Restic ricrea i percorsi assoluti sotto la destinazione indicata. Nel weekly
troverai il pacchetto sotto il percorso originale di `backups/weekly/...`; nel
daily troverai i file applicativi e `backups/database/current` con i dump.

Ripristina configurazioni e applicazione dal pacchetto scelto, avvia PostgreSQL
pulito con immagine compatibile, importa prima `globals.sql` e poi ogni dump
con `pg_restore --create --exit-on-error` collegandoti a `template1`. Il manifest
associa i nomi reali dei database ai file numerati. Verifica i dati prima di
riaprire FitHub agli utenti. La procedura di test nel motore Python mostra la
sequenza, ma non esiste un comando automatico che sovrascrive la produzione.

## Verifica nello sviluppo

```bash
python3.14 -m unittest discover -s tests -v
```

I test usano filesystem temporanei e simulano Docker/restic. Coprono archive,
checksum, retention, lock, guasti cloud/dump/restore, cleanup e controllo runtime.
Sono incluse copie cloud/SSD autonome, controllo del mount e verifica del
contenuto copiato prima della retention.
Non sostituiscono il primo test con Docker, restic e dati reali sul Mac mini.

Riferimenti ufficiali:
[pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html),
[restic copy](https://restic.readthedocs.io/en/stable/045_working_with_repos.html),
[retention restic](https://restic.readthedocs.io/en/stable/060_forget.html),
[Time Machine](https://support.apple.com/en-us/104984).

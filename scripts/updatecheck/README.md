# Update checker del Mac mini

Controlla gli aggiornamenti di programmi e servizi con dati locali e cataloghi
pubblici. Non richiede account Docker o Git. Salva il report nel progetto e invia
il riepilogo al monitor Push Kuma già configurato. Le installazioni restano manuali.

## Uso con Comandi Rapidi

Nell'azione **Esegui script shell**, usare:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/updatecheck/run-from-shortcuts.sh
```

L'automazione può eseguirlo ogni **lunedì alle 19:00**, nell'orario locale del Mini.
Il percorso è lo stesso delle versioni precedenti. Per aggiornare un'installazione
esistente, sostituire i file del pacchetto nella cartella `scripts/updatecheck/`,
conservando `updatecheck.json`, `updatecheck-push-url.txt` e la cartella `state/`.
La nuova modalità senza account è attiva anche con una configurazione precedente:
`docker.image_overrides`, `git.remote` e `git.branch` non sono più utilizzati.
La precedente cache dei digest Docker non viene più letta o aggiornata.

Per il solo risultato locale, senza invio Kuma:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/updatecheck/run-from-shortcuts.sh --no-push
```

Il wrapper consegna a Comandi Rapidi un rapporto leggibile con l'esito e il codice
reale del checker. Errori di controllo o invio restano visibili nel rapporto e
nello stato Kuma. Per propagare anche il codice di uscita alla shell, usare
`run-from-shortcuts.sh --strict-exit`. Per automazioni senza intervento non serve
aggiungere un'azione che mostri finestre o chieda conferme.

## Controlli

| Componente | Metodo |
| --- | --- |
| Applicazioni macOS | Inventario dei bundle `.app`, versione e build; confronto con Homebrew Cask, Mac App Store o feed Sparkle quando riconoscibili. |
| macOS e componenti Apple | `softwareupdate --list`, senza download o installazioni. |
| Pacchetti Homebrew | Inventario locale e API pubbliche di formule e cask; non esegue `brew update` o `upgrade`. |
| Docker Desktop | Versione dell'app e feed stabile pubblico Docker; ripiego dichiarato sul catalogo Homebrew. |
| Servizi Docker | Tutti i container presenti, compresi quelli fermi; versioni software locali e cataloghi pubblici. |
| Sorgenti FitHub | Solo commit locale, indicato come aggiornamento remoto non verificato. |

Per i servizi Docker sono riconosciute le immagini ufficiali PostgreSQL, Redis,
MySQL e Node, oltre a `louislam/uptime-kuma`. Le versioni disponibili provengono da:

- Cataloghi dei sorgenti delle immagini ufficiali:
  [PostgreSQL](https://github.com/docker-library/postgres/blob/master/versions.json),
  [Redis](https://github.com/docker-library/redis/blob/master/versions.json),
  [MySQL](https://github.com/docker-library/mysql/blob/master/versions.json).
- [Indice pubblico delle release Node](https://nodejs.org/dist/index.json).
- [Release pubbliche di Kuma](https://github.com/louislam/uptime-kuma/releases),
  lette tramite l'[API GitHub senza autenticazione per risorse pubbliche](https://docs.github.com/en/rest/releases/releases#list-releases).

Ogni catalogo viene richiesto una sola volta per scansione. Sono escluse beta,
release candidate e prerelease riconoscibili. Il confronto resta nel ramo
installato: major per PostgreSQL, Node e Kuma; major.minor per Redis e MySQL.
Se il ramo non compare più nel catalogo, una versione stabile superiore richiede
`REVIEW`; un catalogo con sole versioni inferiori resta `SKIP`.

La versione installata viene letta dai metadati dell'immagine locale. Per Kuma,
quando manca l'etichetta versione, viene letto `/app/package.json` con
`docker exec … /bin/cat`, solo se il container è in esecuzione. La versione di Node
usata da Kuma non viene scambiata per quella dell'applicazione.

Non vengono interrogati Docker Hub o altri registry, non vengono confrontati
digest e non vengono verificate ricostruzioni della stessa versione di un'immagine.
Il report riguarda le versioni software pubblicate, senza garantire la disponibilità
di un'immagine identica per ogni tag o piattaforma. Le build locali FitHub e i
servizi senza versione o fonte riconoscibile sono censiti con `SKIP` e motivazione.
Gli aggiornamenti dei sorgenti privati FitHub non vengono verificati: nessun fetch,
login o lettura delle credenziali Git.

Le app sono cercate in `/Applications`, `~/Applications`, `/System/Applications`
e `/System/Library/CoreServices/Applications`. Le app di sistema sono delegate al
controllo macOS. Il Mac App Store usa il catalogo pubblico del paese configurato
(default `ch`), senza accesso all'account Apple. Il risultato può differire dalle
proposte dell'updater del produttore per licenze, requisiti o distribuzioni graduali.
Le app senza catalogo verificabile restano `SKIP`: non vengono dichiarate aggiornate.
Software fuori dalle cartelle configurate, plugin interni e pacchetti pip/npm
richiedono controlli dedicati.

## Percorsi e configurazione

| Contenuto | Percorso sul Mac mini |
| --- | --- |
| Script e configurazione | `/Users/admin/Documents/Fithub/scripts/updatecheck/` |
| URL Push privato | `/Users/admin/Documents/Fithub/scripts/updatecheck/updatecheck-push-url.txt` |
| Stato e lock | `/Users/admin/Documents/Fithub/scripts/updatecheck/state/` |
| Report e log | `/Users/admin/Documents/Fithub/logs/system/updatecheck/` |

Richiede macOS e CPython stabile **3.14.9+ del ramo 3.14**, come i backup.
Per i container serve la CLI Docker con accesso al daemon locale, già usata dagli
altri sensori. Non serve Buildx. Il checker gira come `admin`, senza sudo.
Conservare l'accesso a Documents già configurato per gli altri script.

Le impostazioni vengono lette da `updatecheck.json` oppure, se assente, da
`updatecheck.example.json`. Non sovrascrivere una configurazione personale durante
l'aggiornamento. Si possono configurare le cartelle delle app, il paese App Store,
le esclusioni Docker, le immagini locali e i gruppi da abilitare. Il percorso Git
è relativo alla radice Fithub e serve solo per l'inventario locale dei sorgenti.

## Kuma

Usare un monitor **Push** dedicato. Per il controllo settimanale l'intervallo
heartbeat consigliato è **691200 secondi (8 giorni)**, tentativi `0`.
Il file `updatecheck-push-url.txt` contiene solo l'URL completo copiato dal monitor,
in testo semplice UTF-8 su una riga. La query dell'URL può restare: il checker la
sostituisce con l'esito reale. Non inserire comandi shell o documenti RTF nel file.
La variabile `KUMA_UPDATECHECK_PUSH_URL`, se valorizzata, ha precedenza sul file.

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/updatecheck/run-from-shortcuts.sh --self-check
```

Questo comando verifica runtime, configurazione e formato dell'URL, senza rete.
Se il file URL manca, il report viene salvato soltanto localmente.

Con `down_on_updates: true`, Kuma riceve DOWN se ci sono aggiornamenti/revisioni
o errori; UP se i controlli conclusi non hanno rilevato novità. Gli elementi
`SKIP` sono esclusi e riportati separatamente: UP non significa che tutti i
programmi installati siano stati verificati. Con `down_on_updates: false`, gli
aggiornamenti non causano DOWN, mentre gli errori continuano a farlo.

Un errore di rete di un catalogo conosciuto o un invio Kuma fallito resta un errore.
Il report distingue DNS, TLS, HTTP e risposta Push non valida senza mostrare token
o stderr potenzialmente privato. Per ritentare solo l'invio di una scansione
conclusa da non oltre 15 minuti:

```bash
/bin/bash /Users/admin/Documents/Fithub/scripts/updatecheck/run-from-shortcuts.sh --retry-kuma
```

Viene riutilizzato l'esito reale, anche se DOWN, con la data originale. Non è un
heartbeat per tenere artificialmente attuale un vecchio risultato.

## Report e diagnosi

```bash
cat /Users/admin/Documents/Fithub/logs/system/updatecheck/latest.txt
/bin/bash /Users/admin/Documents/Fithub/scripts/updatecheck/run-from-shortcuts.sh --diagnose-sources
```

La diagnosi verifica solo servizi Docker e commit Git locale. Non invia a Kuma e
non riscrive i report. Il codice originale è `0` senza errori, `2` con errori.
La scansione normale usa `0` nessuna novità, `1` aggiornamenti/revisioni, `2` errore.
`latest.txt`, `latest.json`, i report datati e `shortcut-last-run.log` conservano
il risultato e gli eventuali errori. URL, configurazione, stato e log sono privati,
ignorati da Git e assenti dal pacchetto di distribuzione.

Per chi preferisce launchd rimangono disponibili l'installer e il plist settimanale.
Solo il plist viene installato in `/Library/LaunchDaemons/`; gli script e i log
restano nel progetto. L'installer ricava i percorsi dalla cartella di esecuzione.
Il servizio usa `--service`, senza `WorkingDirectory`, `StandardOutPath` o
`StandardErrorPath`; Bash apre i log dopo l'avvio.

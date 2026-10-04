# merge-dirs

Unisce due cartelle in una **terza cartella nuova**, senza modificare le
originali. Pensato per riunire copie e backup cresciuti separatamente (per
esempio due dischi esterni) senza perdere file e senza creare doppioni.

Comprende uno script da riga di comando (`merge_dirs.sh`) e un'interfaccia
web locale (`merge_dirs_web.py`).

## Regole del merge

Date una cartella **A** (prioritaria) e una cartella **B**:

| Situazione | Risultato |
|---|---|
| File presente solo in A o solo in B | viene copiato |
| Stesso percorso, contenuto diverso | vince la versione di A |
| Stesso percorso, file da una parte e cartella dall'altra | vince la versione di A |
| File di B identico (SHA-256) a un file di A che sta in un'altra posizione | non viene copiato: resta solo dove sta in A |
| File vuoti | non sono mai considerati duplicati (`.gitkeep`, `__init__.py`, ...) |

A e B non vengono mai modificate. Le cartelle di sistema nella radice dei
dischi (`.Trashes`, `.Spotlight-V100`, `.fseventsd`, `.DocumentRevisions-V100`,
`.TemporaryItems`, `System Volume Information`, `$RECYCLE.BIN`) vengono
ignorate.

## Requisiti

- macOS o Linux con `bash`, `rsync` e `sha256sum` (o `shasum`)
- per l'interfaccia web: Python 3 (su macOS si ottiene con
  `xcode-select --install`)

## Riga di comando

```sh
merge_dirs.sh [-n] [-C] DIR_A DIR_B DIR_MERGE
```

| Opzione | Effetto |
|---|---|
| `-n` | anteprima: mostra il report senza creare nulla |
| `-C` | ignora la cache dei checksum e rilegge tutti i file |
| `-h`, `--help` | mostra l'aiuto |

`DIR_MERGE` deve essere nuova o vuota e non puo' stare dentro A o B.

```sh
merge_dirs.sh -n ~/Foto /Volumes/Backup/Foto ~/Foto_unite   # anteprima
merge_dirs.sh    ~/Foto /Volumes/Backup/Foto ~/Foto_unite   # merge
```

Nel Terminale l'avanzamento di ogni fase viene mostrato su una riga che si
aggiorna.

## Interfaccia web

```sh
merge_dirs_web.py [--porta N] [--no-browser]
```

Avvia un server su `127.0.0.1` (porta 8765 o la prima libera dopo) e apre la
pagina nel browser. Ctrl+C nel Terminale per chiudere: interrompe anche
l'operazione in corso.

- scelta delle cartelle con la finestra di sistema di macOS o scrivendo il
  percorso
- **Anteprima** ed **Esegui merge**, anche senza anteprima (con conferma)
- avanzamento per fasi con stima del tempo rimasto, e pulsante **Interrompi**
- report con conteggi e sezioni pieghevoli, apertura del risultato nel Finder

L'operazione gira nel server, non nella pagina: se la connessione cade o la
pagina viene ricaricata, riaprendola si ritrova lo stato. Si puo' eseguire una
sola operazione alla volta.

Il server accetta solo connessioni da questo computer e ogni avvio genera un
link con un codice casuale: senza quel codice le API rifiutano le richieste,
quindi altri siti aperti nel browser non possono usarle.

## Come funziona

1. **Controllo dei permessi**: se un file non e' leggibile lo script si ferma
   prima di creare qualsiasi cosa.
2. **Checksum**: ogni file viene letto una sola volta. Il checksum serve sia a
   trovare i conflitti allo stesso percorso sia i duplicati in altre posizioni.
3. **Confronto** della struttura, fatto sui checksum senza rileggere i file.
4. **Copia di A** e **aggiunta da B** con `rsync` (solo nel merge).

Gli errori di lettura momentanei, frequenti con i dischi USB, vengono gestiti
con un secondo tentativo automatico, sia nei checksum sia nella copia.

### Cache dei checksum

I checksum vengono salvati in `~/.cache/merge_dirs/` (o in
`$XDG_CACHE_HOME/merge_dirs/`), un file per cartella di origine. Un file con la
stessa dimensione e la stessa data di modifica non viene riletto: il merge dopo
un'anteprima, o un nuovo tentativo dopo un errore, riusa i checksum gia'
calcolati. La cache serve solo a velocizzare: se manca o viene cancellata, i
checksum vengono ricalcolati. Con `-C` (o la casella "Ricalcola tutti i
checksum") viene ignorata.

### Report

Il merge salva il report completo in `DIR_MERGE.report.txt`, accanto alla
cartella creata. Contiene i conteggi e l'elenco di:

- elementi solo in A
- elementi solo in B da copiare
- conflitti di contenuto e di tipo (tenuta la versione di A)
- file di B gia' presenti in A in un'altra posizione (non copiati)

Se il merge non va a buon fine, il report lo dice in fondo con una riga
`!!! MERGE INCOMPLETO`.

## Limiti noti

- I nomi di file che contengono un a capo non sono supportati.
- I link simbolici vengono confrontati solo per dimensione, non per
  destinazione.
- La cache si basa su dimensione e data di modifica: un file modificato senza
  che cambi nessuna delle due verrebbe confrontato con il checksum vecchio. In
  caso di dubbio usare `-C`.

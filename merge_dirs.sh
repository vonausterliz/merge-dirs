#!/usr/bin/env bash
#
# merge_dirs.sh - Confronta due directory e ne crea una terza che e' l'unione.
#
# Regole:
#   - stesso percorso, contenuto diverso      -> vince la PRIMA directory (A)
#   - file di B identico (SHA-256) a un file di A che sta in un'altra posizione
#                                             -> non viene copiato: resta solo
#                                                nella posizione di A
#   - tutto il resto di B                     -> viene aggiunto
# Le due directory di origine non vengono mai modificate.
#
# Uso:
#   ./merge_dirs.sh [-n] [-C] DIR_A DIR_B DIR_MERGE
#
#   -n          solo confronto: mostra il report senza creare nulla
#   -C          ignora la cache dei checksum e rilegge tutti i file
#   -h, --help  mostra l'aiuto
#
# I checksum vengono salvati in ~/.cache/merge_dirs: i file non modificati
# (stessa dimensione e data) non vengono riletti alla volta successiva.
#
# Funziona su macOS e Linux (richiede rsync e sha256sum/shasum).

set -u
# Nomi di file trattati come byte: con una locale UTF-8, awk e sed si
# bloccano sui nomi non UTF-8 validi (dischi vecchi, file da Windows)
export LC_ALL=C

die()   { echo "ERRORE: $*" >&2; exit 1; }
usage() { echo "Uso: $0 [-n] [-C] DIR_A DIR_B DIR_MERGE   (-h per l'aiuto)" >&2; exit 2; }

help() {
  cat <<EOF
Uso: $(basename "$0") [-n] [-C] DIR_A DIR_B DIR_MERGE

Confronta DIR_A e DIR_B e crea una TERZA directory, DIR_MERGE, con
l'unione delle due. DIR_A e DIR_B non vengono mai modificate.

Regole:
  - stesso percorso, contenuto diverso   vince DIR_A
  - stesso percorso, file vs directory   vince DIR_A
  - file di DIR_B identico (SHA-256) a un file di DIR_A in un'altra
    posizione                            non viene copiato
  - tutto il resto di DIR_B              viene aggiunto
  I file vuoti non sono mai considerati duplicati.

Opzioni:
  -n          solo confronto: mostra il report senza creare nulla
  -C          ignora la cache dei checksum e rilegge tutti i file
  -h, --help  mostra questo aiuto

I checksum vengono salvati in ~/.cache/merge_dirs: i file non modificati
(stessa dimensione e data di modifica) non vengono riletti alla volta
successiva, per esempio nel merge dopo l'anteprima.

DIR_MERGE deve essere nuova o vuota e non puo' stare dentro DIR_A o DIR_B.
Al termine il report completo viene salvato in DIR_MERGE.report.txt.

Esempi:
  $(basename "$0") -n ~/Foto ~/Backup/Foto ~/Foto_unite   # anteprima
  $(basename "$0")    ~/Foto ~/Backup/Foto ~/Foto_unite   # esegue il merge
EOF
  exit 0
}

DRY=0
USE_CACHE=1
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) help ;;
    -n)        DRY=1; shift ;;
    -C)        USE_CACHE=0; shift ;;
    --)        shift; break ;;
    -*)        echo "ERRORE: opzione sconosciuta '$1'" >&2; usage ;;
    *)         break ;;
  esac
done
[ $# -eq 3 ] || usage

command -v rsync >/dev/null 2>&1 || die "rsync non trovato"
if command -v sha256sum >/dev/null 2>&1; then HASH="sha256sum"
elif command -v shasum >/dev/null 2>&1; then HASH="shasum -a 256"
else die "ne' sha256sum ne' shasum trovati"; fi

[ -d "$1" ] || die "'$1' non e' una directory"
[ -d "$2" ] || die "'$2' non e' una directory"
A=$(cd "$1" && pwd)
B=$(cd "$2" && pwd)
[ "$A" != "$B" ] || die "le due directory di origine coincidono"

# Percorso assoluto della destinazione (la cartella padre deve esistere)
PARENT=$(cd "$(dirname "$3")" 2>/dev/null && pwd) || die "la cartella padre di '$3' non esiste"
DEST="$PARENT/$(basename "$3")"

# La destinazione non deve stare dentro A o B, e deve essere nuova o vuota
case "$DEST/" in
  "$A"/*|"$B"/*) die "la destinazione non puo' stare dentro una delle due origini" ;;
esac
if [ -e "$DEST" ]; then
  [ -d "$DEST" ] || die "'$DEST' esiste e non e' una directory"
  [ -z "$(ls -A "$DEST")" ] || die "'$DEST' esiste e non e' vuota"
fi

TMP=$(mktemp -d) || die "impossibile creare la directory temporanea"
trap 'rm -rf "$TMP"' EXIT
trap 'exit 143' TERM   # interruzione: esce passando dalla pulizia di EXIT
trap 'exit 130' INT

# Cartelle di sistema che macOS (e Windows) creano nella radice dei dischi:
# spesso illeggibili e comunque estranee ai file dell'utente, si ignorano
SYS_NAMES=(.Trashes .Spotlight-V100 .fseventsd .DocumentRevisions-V100
           .TemporaryItems "System Volume Information" '$RECYCLE.BIN')
for n in "${SYS_NAMES[@]}"; do
  printf '/%s\n' "$n"
done > "$TMP/sys_exclude.txt"

# find che salta le cartelle di sistema nella radice indicata.
# L'espressione passata deve terminare con -print o -exec.
sfind() {
  local root=$1 expr=() n
  shift
  for n in "${SYS_NAMES[@]}"; do expr+=(-o -path "$root/$n"); done
  find "$root" \( "${expr[@]:1}" \) -prune -o "$@"
}

# ------------------------------------------------------------ avanzamento
# MERGE_DIRS_PROGRESS=1 (interfaccia web): righe "@@P FASE FASI N TOTALE ETICHETTA"
# (TOTALE 0 = durata non stimabile). Nel Terminale: percentuale sulla stessa riga.
if [ "${MERGE_DIRS_PROGRESS:-}" = 1 ]; then PMODE=web
elif [ -t 1 ]; then PMODE=tty
else PMODE=none; fi
if [ "$DRY" -eq 1 ]; then STEPS=3; else STEPS=5; fi
exec 3>&1   # i messaggi di avanzamento escono qui anche dai cicli rediretti

pmsg() {  # pmsg FASE N TOTALE ETICHETTA
  case "$PMODE" in
    web) printf '@@P %d %d %d %d %s\n' "$1" "$STEPS" "$2" "$3" "$4" >&3 ;;
    tty) [ "$3" -gt 0 ] && printf '\r  %s: %d/%d (%d%%)   ' "$4" "$2" "$3" $(( $2 * 100 / $3 )) >&3 ;;
  esac
  return 0
}
pend() { [ "$PMODE" = tty ] && echo >&3; return 0; }

# Conta le righe in ingresso e aggiorna l'avanzamento; con FILE le salva li'.
# Riconosce l'output di rsync -v (totale annunciato, righe di riepilogo).
progress() {  # progress FASE TOTALE ETICHETTA [FILE]
  awk -v step="$1" -v steps="$STEPS" -v tot="$2" -v label="$3" -v out="${4:-}" -v mode="$PMODE" '
    function show(k) {
      if (mode == "web") printf "@@P %d %d %d %d %s\n", step, steps, k, tot, label
      else if (mode == "tty" && tot > 0)
        printf "\r  %s: %d/%d (%d%%)   ", label, k, tot, (k > tot ? 100 : 100 * k / tot)
      fflush()
    }
    function every_n() { e = int(tot / 200); return e < 1 ? 1 : e }
    BEGIN { every = every_n(); show(0) }
    out == "" && /^Transfer starting: [0-9]+ files/ { tot = $3 + 0; every = every_n(); show(n); next }
    out == "" && /^(sent |total size|sending incremental|building file list|$)/ { next }
    { if (out != "") print > out; n++; if (n % every == 0) show(n) }
    END { if (n > tot) tot = n; show(tot); if (mode == "tty") printf "\n" }
  '
}

# Come progress, ma un suo eventuale errore non interrompe chi scrive nella
# pipe (rsync, sha256sum): il resto dell'output viene comunque consumato
progress_safe() { progress "$@"; cat > /dev/null; }

# ------------------------------------------------------ controllo permessi
# Un file o una cartella illeggibile renderebbe il confronto incompleto e
# farebbe fallire rsync a meta' copia: meglio fermarsi prima di creare nulla.
echo "Controllo dei permessi di lettura..."
pmsg 1 0 0 "Conteggio dei file"
N_A=$(sfind "$A" -print 2>/dev/null | wc -l | tr -d ' ')
N_B=$(sfind "$B" -print 2>/dev/null | wc -l | tr -d ' ')
i=0
for SRC in "$A" "$B"; do
  sfind "$SRC" -print 2>/dev/null |
  while IFS= read -r p; do
    i=$((i + 1))
    [ $((i % 500)) -eq 0 ] && pmsg 1 "$i" $((N_A + N_B)) "Controllo dei permessi"
    [ "$p" = "$SRC" ] || [ -L "$p" ] && continue
    if [ -d "$p" ]; then
      { [ -r "$p" ] && [ -x "$p" ]; } || printf '%s\n' "$p"
    else
      [ -r "$p" ] || printf '%s\n' "$p"
    fi
  done >> "$TMP/unreadable.txt"
  [ "$SRC" = "$A" ] && i=$N_A   # il ciclo gira in una subshell: si riparte dal conteggio di A
done
pmsg 1 $((N_A + N_B)) $((N_A + N_B)) "Controllo dei permessi"; pend
if [ -s "$TMP/unreadable.txt" ]; then
  echo "ERRORE: questi elementi non sono leggibili, nessuna operazione eseguita:" >&2
  sed 's/^/  /' "$TMP/unreadable.txt" >&2
  echo "Sistema i permessi (es. chmod -R u+rX <cartella>) e riprova." >&2
  exit 1
fi

# ------------------------------------------------------- elenco dei file
# Ogni file viene letto una sola volta: il suo checksum serve sia a trovare
# i conflitti allo stesso percorso sia i file di B gia' presenti in A altrove.
echo "Elenco dei file..."
pmsg 2 0 0 "Elenco dei file"
if stat -f '%z' / >/dev/null 2>&1; then
  STAT=(stat -f '%z%t%Fm%t%HT%t%N')        # macOS/BSD: dimensione, data, tipo, nome
else
  STAT=(stat -c $'%s\t%.9Y\t%F\t%n')       # Linux
fi
list_tree() {  # list_tree RADICE SIGLA
  ( cd "$1" && sfind . -mindepth 1 -type d -print ) > "$TMP/dirs_$2.txt"
  ( cd "$1" && sfind . -mindepth 1 ! -type d -exec "${STAT[@]}" {} + ) > "$TMP/stat_$2.tsv"
}
list_tree "$A" a
list_tree "$B" b

# ------------------------------------------------------------- checksum
# I checksum restano in cache (per dimensione e data di modifica): anteprima
# e merge successivi non rileggono i file invariati. -C la ignora.
# I file vuoti sono esclusi: sono tutti "uguali" tra loro e verrebbero
# scartati a torto come duplicati (.gitkeep, __init__.py, ...).
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/merge_dirs"
mkdir -p "$CACHE_DIR" 2>/dev/null || USE_CACHE=0
cache_file() { printf '%s/%s.tsv' "$CACHE_DIR" "$(printf '%s' "$1" | $HASH | cut -c1-64)"; }

# Divide i file regolari non vuoti in: gia' in cache (hits) e da calcolare (todo)
split_cached() {  # split_cached RADICE SIGLA
  local cache
  cache=$(cache_file "$1")
  [ "$USE_CACHE" -eq 1 ] && [ -f "$cache" ] || cache=/dev/null
  awk -F '\t' -v hits="$TMP/hits_$2.txt" -v todo="$TMP/todo_$2.txt" '
    function rest(n,   s, i) { s = $0; for (i = 0; i < n; i++) s = substr(s, index(s, "\t") + 1); return s }
    FILENAME == ARGV[1] { c[rest(3)] = $1 "\t" $2 "\t" $3; next }    # cache: dim data hash nome
    tolower($3) !~ /regular/ || $1 == 0 { next }                      # stat:  dim data tipo nome
    { p = rest(3)
      if ((p in c) && split(c[p], v, "\t") == 3 && v[1] == $1 && v[2] == $2) print v[3] "  " p > hits
      else print p > todo }
  ' "$cache" "$TMP/stat_$2.tsv"
  touch "$TMP/hits_$2.txt" "$TMP/todo_$2.txt"
}

# Calcola i checksum dei file elencati; output "HASH  ./nome".
# shasum segnala con "\" iniziale i nomi con caratteri speciali: si ripristinano.
hash_list() {  # hash_list RADICE ELENCO
  [ -s "$2" ] || return 0
  ( cd "$1" && tr '\n' '\0' < "$2" | xargs -0 $HASH 2>> "$TMP/hash_err.txt" ) |
  awk '
    function unesc(s,   r, i, ch) {
      r = ""
      for (i = 1; i <= length(s); i++) {
        ch = substr(s, i, 1)
        if (ch == "\\" && i < length(s)) { i++; ch = substr(s, i, 1); if (ch == "n") ch = "\n" }
        r = r ch
      }
      return r
    }
    substr($0, 1, 1) == "\\" { print substr($0, 2, 64) "  " unesc(substr($0, 68)); next }
    { print }
  '
}

split_cached "$A" a
split_cached "$B" b
F_AB=$(( $(wc -l < "$TMP/hits_a.txt") + $(wc -l < "$TMP/todo_a.txt") + \
         $(wc -l < "$TMP/hits_b.txt") + $(wc -l < "$TMP/todo_b.txt") ))
N_HIT=$(( $(wc -l < "$TMP/hits_a.txt") + $(wc -l < "$TMP/hits_b.txt") ))
echo "Calcolo dei checksum: $((F_AB - N_HIT)) file da leggere, $N_HIT dalla cache..."
{
  sed 's/^/A /' "$TMP/hits_a.txt"; hash_list "$A" "$TMP/todo_a.txt" | sed 's/^/A /'
  sed 's/^/B /' "$TMP/hits_b.txt"; hash_list "$B" "$TMP/todo_b.txt" | sed 's/^/B /'
} | tee "$TMP/hash_ab.txt" | progress_safe 2 "$F_AB" "Calcolo dei checksum"
sed -n 's/^A //p' "$TMP/hash_ab.txt" > "$TMP/hash_a.txt"
sed -n 's/^B //p' "$TMP/hash_ab.txt" > "$TMP/hash_b.txt"

# File non letti (es. errore momentaneo di un disco USB): secondo tentativo,
# poi ci si ferma. Nessuna cartella e' stata ancora creata.
missing() {  # missing SIGLA: file da calcolare per cui non c'e' un checksum
  awk 'FILENAME == ARGV[1] { got[substr($0, 67)] = 1; next } !($0 in got)' \
    "$TMP/hash_$1.txt" "$TMP/todo_$1.txt"
}
for x in a b; do
  if [ "$x" = a ]; then R=$A; else R=$B; fi
  missing "$x" > "$TMP/retry_$x.txt"
  if [ -s "$TMP/retry_$x.txt" ]; then
    echo "Rilettura di $(wc -l < "$TMP/retry_$x.txt" | tr -d ' ') file non letti al primo tentativo..."
    hash_list "$R" "$TMP/retry_$x.txt" >> "$TMP/hash_$x.txt"
    missing "$x" > "$TMP/unhashed_$x.txt"
    if [ -s "$TMP/unhashed_$x.txt" ]; then
      echo "ERRORE: impossibile leggere questi file, nessuna operazione eseguita:" >&2
      sed "s|^\.|  $R|" "$TMP/unhashed_$x.txt" >&2
      sed 's/^/  /' "$TMP/hash_err.txt" >&2
      exit 1
    fi
  fi
done

# Aggiorna la cache con i checksum di questo giro (anche con -C)
save_cache() {  # save_cache RADICE SIGLA
  local cache
  cache=$(cache_file "$1")
  awk -F '\t' '
    function rest(n,   s, i) { s = $0; for (i = 0; i < n; i++) s = substr(s, index(s, "\t") + 1); return s }
    FILENAME == ARGV[1] { h[substr($0, 67)] = substr($0, 1, 64); next }
    { p = rest(3); if (p in h) print $1 "\t" $2 "\t" h[p] "\t" p }
  ' "$TMP/hash_$2.txt" "$TMP/stat_$2.tsv" > "$cache.tmp.$$" && mv "$cache.tmp.$$" "$cache"
}
if [ -w "$CACHE_DIR" ]; then save_cache "$A" a; save_cache "$B" b; fi

# ------------------------------------------------------------- confronto
# Come "diff -rq": si scende solo nelle cartelle presenti da entrambe le
# parti e un elemento presente da una sola parte e' segnalato al livello piu'
# alto. Contenuto diverso = dimensione o checksum diversi (i link simbolici
# si confrontano solo per dimensione).
echo "Confronto della struttura..."
pmsg 3 0 0 "Confronto della struttura"
awk -F '\t' -v a="$A" -v b="$B" -v out="$TMP" '
  function rest(n,   s, i) { s = $0; for (i = 0; i < n; i++) s = substr(s, index(s, "\t") + 1); return s }
  function parent(p) { match(p, /\/[^\/]*$/); return substr(p, 1, RSTART - 1) }
  # prima "in", poi la lettura: in awk leggere ta[q] crea un elemento vuoto
  function common(q) { return q == "." || ((q in ta) && (q in tb) && ta[q] == "d" && tb[q] == "d") }
  function only(root, p, q) {
    return "Only in " root (q == "." ? "" : "/" substr(q, 3)) ": " substr(p, length(q) + 2)
  }
  FILENAME == ARGV[1] { ta[$0] = "d"; next }
  FILENAME == ARGV[2] { tb[$0] = "d"; next }
  FILENAME == ARGV[3] { p = rest(3); ta[p] = "f"; sa[p] = $1; next }
  FILENAME == ARGV[4] { p = rest(3); tb[p] = "f"; sb[p] = $1; next }
  FILENAME == ARGV[5] { ha[substr($0, 67)] = substr($0, 1, 64); next }
  FILENAME == ARGV[6] { hb[substr($0, 67)] = substr($0, 1, 64); next }
  END {
    for (p in ta) {
      q = parent(p)
      if (!common(q)) continue
      if (!(p in tb))       print only(a, p, q) > (out "/only_a.txt")
      else if (ta[p] != tb[p]) print substr(p, 2) > (out "/type_conflicts.txt")
      else if (ta[p] == "f" && (sa[p] != sb[p] || ha[p] != hb[p]))
        print "Files " a "/" substr(p, 3) " and " b "/" substr(p, 3) " differ" > (out "/conflicts.txt")
    }
    for (p in tb)
      if (!(p in ta) && common(parent(p))) print only(b, p, parent(p)) > (out "/only_b.txt")
  }
' "$TMP/dirs_a.txt" "$TMP/dirs_b.txt" "$TMP/stat_a.tsv" "$TMP/stat_b.tsv" "$TMP/hash_a.txt" "$TMP/hash_b.txt"
for f in only_a only_b conflicts type_conflicts; do
  touch "$TMP/$f.txt"; sort -o "$TMP/$f.txt" "$TMP/$f.txt"
done

# File di B che non esistono in A allo stesso percorso, ma il cui contenuto
# (SHA-256) coincide con quello di un file di A in un'altra posizione
TAB=$(printf '\t')
awk -v tab="$TAB" '
  { h = substr($0, 1, 64); p = substr($0, 67) }
  NR == FNR { if (!(h in first)) first[h] = p; next }
  (h in first) { print p tab first[h] }
' "$TMP/hash_a.txt" "$TMP/hash_b.txt" |
while IFS="$TAB" read -r pb pa; do
  # se in A esiste gia' qualcosa a quel percorso non e' uno "spostamento"
  if [ ! -e "$A/$pb" ] && [ ! -L "$A/$pb" ]; then
    printf '%s\t%s\n' "$pb" "$pa"
  fi
done | sort > "$TMP/moved.txt"

# Elementi solo in B che verranno davvero copiati: si tolgono i file gia'
# presenti in A in un'altra posizione e le cartelle fatte solo di tali file
cut -f1 "$TMP/moved.txt" | sed 's/^\.//' > "$TMP/moved_paths.txt"
while IFS= read -r line; do
  s=${line#"Only in $B"}
  rel="${s%%: *}/${s#*: }"
  if [ -d "$B$rel" ] && [ ! -L "$B$rel" ]; then
    total=$( (cd "$B$rel" && find . ! -type d) | wc -l | tr -d ' ')
    dup=$(awk -v p="$rel/" 'index($0, p) == 1' "$TMP/moved_paths.txt" | wc -l | tr -d ' ')
    [ "$total" -gt 0 ] && [ "$total" -eq "$dup" ] && continue
  else
    grep -qxF -- "$rel" "$TMP/moved_paths.txt" && continue
  fi
  printf '%s\n' "$line"
done < "$TMP/only_b.txt" > "$TMP/only_b_copied.txt"

# Elenco di esclusione per rsync (percorsi ancorati, caratteri jolly protetti)
{
  cat "$TMP/type_conflicts.txt"
  cut -f1 "$TMP/moved.txt" | sed 's/^\.//'
} | sed '/[][*?]/s/[][*?\\]/\\&/g' > "$TMP/exclude.txt"
cat "$TMP/sys_exclude.txt" >> "$TMP/exclude.txt"

# ------------------------------------------------------------------ report
count() { wc -l < "$1" | tr -d ' '; }

{
  echo "REPORT MERGE - $(date)"
  echo "A (prioritaria): $A"
  echo "B:               $B"
  echo "Destinazione:    $DEST"
  echo
  echo "Elementi solo in A:                         $(count "$TMP/only_a.txt")"
  echo "Elementi solo in B (da copiare):            $(count "$TMP/only_b_copied.txt")"
  echo "Conflitti di contenuto (vince A):           $(count "$TMP/conflicts.txt")"
  echo "Conflitti di tipo (vince A):                $(count "$TMP/type_conflicts.txt")"
  echo "File di B gia' in A in altra posizione:     $(count "$TMP/moved.txt")"
  echo
  echo "--- SOLO IN A ---"; cat "$TMP/only_a.txt"; echo
  echo "--- SOLO IN B, DA COPIARE (se e' una cartella, vale per il contenuto non duplicato) ---"; cat "$TMP/only_b_copied.txt"; echo
  echo "--- CONFLITTI DI CONTENUTO (tenuta la versione di A) ---"; cat "$TMP/conflicts.txt"; echo
  echo "--- CONFLITTI DI TIPO file/directory (tenuta la versione di A) ---"; cat "$TMP/type_conflicts.txt"; echo
  echo "--- FILE DI B IDENTICI A UN FILE DI A IN ALTRA POSIZIONE (non copiati) ---"
  awk -F "$TAB" '{ print "B: " $1 "   ==   A: " $2 }' "$TMP/moved.txt"; echo
} > "$TMP/report.txt"

sed -n '1,11p' "$TMP/report.txt"

if [ "$DRY" -eq 1 ]; then
  echo "Modalita' -n: nessuna copia eseguita. Report completo:"
  echo
  sed -n '12,$p' "$TMP/report.txt"
  exit 0
fi

# ------------------------------------------------------------------- merge
REPORT="$DEST.report.txt"
cp "$TMP/report.txt" "$REPORT" || die "impossibile scrivere il report '$REPORT'"

# In caso di errore il report resta, con l'avviso che il merge e' incompleto
fail() {
  printf '\n!!! MERGE INCOMPLETO: %s\n' "$*" >> "$REPORT"
  die "$* - il contenuto di '$DEST' e' INCOMPLETO (report: $REPORT)"
}
trap 'fail "operazione interrotta"' TERM INT

mkdir -p "$DEST" || fail "impossibile creare '$DEST'"

# rsync con un secondo tentativo: un errore momentaneo di lettura (frequente
# con i dischi USB) si risolve rifacendo il giro, che copia solo cio' che manca
copy() {  # copy FASE TOTALE ETICHETTA ARGOMENTI_RSYNC...
  local step=$1 tot=$2 label=$3
  shift 3
  rsync -a -v "$@" | progress_safe "$step" "$tot" "$label"
  [ "${PIPESTATUS[0]}" -eq 0 ] && return 0
  echo "rsync ha segnalato un errore: secondo tentativo..."
  rsync -a -v "$@" | progress_safe "$step" "$tot" "$label (secondo tentativo)"
  [ "${PIPESTATUS[0]}" -eq 0 ]
}

echo "Copia di A..."
copy 4 "$N_A" "Copia di A" --exclude-from="$TMP/sys_exclude.txt" "$A/" "$DEST/" ||
  fail "copia di A fallita"

echo "Aggiunta da B di cio' che manca..."
copy 5 "$N_B" "Aggiunta da B" --ignore-existing --exclude-from="$TMP/exclude.txt" "$B/" "$DEST/" ||
  fail "copia di B fallita"

# Rimuove le cartelle (provenienti solo da B) rimaste vuote perche' tutti i
# loro file erano gia' presenti in A in un'altra posizione
cut -f1 "$TMP/moved.txt" | while IFS= read -r p; do
  d=$(dirname "$p")
  while [ "$d" != "." ] && [ ! -e "$A/$d" ]; do
    rmdir "$DEST/$d" 2>/dev/null || break
    d=$(dirname "$d")
  done
done

echo
echo "Fatto. Merge in:  $DEST"
echo "Report completo:  $REPORT"

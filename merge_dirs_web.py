#!/usr/bin/env python3
#
# merge_dirs_web.py - Interfaccia web locale per merge_dirs.sh
#
# Avvia un piccolo server su 127.0.0.1 (raggiungibile solo da questo Mac)
# e apre la pagina nel browser. Ctrl+C nel Terminale per chiudere.
#
# Uso:
#   merge_dirs_web.py [--porta N] [--no-browser]
#
# Le chiamate all'API richiedono il token casuale contenuto nel link
# stampato all'avvio: altri siti aperti nel browser non possono usarla.

import argparse
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "merge_dirs.sh")
TOKEN = secrets.token_urlsafe(16)
JOB_LOCK = threading.Lock()  # protegge la creazione di JOB: un'operazione alla volta


# ------------------------------------------------------- lettura del report
SECTIONS = [  # inizio del titolo nel report -> chiave usata dalla pagina
    ("SOLO IN A", "only_a"),
    ("SOLO IN B", "only_b"),
    ("CONFLITTI DI CONTENUTO", "conflicts"),
    ("CONFLITTI DI TIPO", "type_conflicts"),
    ("FILE DI B IDENTICI", "moved"),
    ("ALTRE SEGNALAZIONI", "other"),
]
COUNT_KEYS = ["only_a", "only_b", "conflicts", "type_conflicts", "moved"]
COUNT_RE = re.compile(r"^(Elementi|Conflitti|File di B).*:\s+(\d+)$")
SECTION_RE = re.compile(r"^--- (.*) ---$")
PROGRESS_RE = re.compile(r"^@@P (\d+) (\d+) (\d+) (\d+) (.*)$")


def strip_root(path, root):
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root) + 1:]
    return path


def dot_strip(p):
    return p[2:] if p.startswith("./") else p


def only_item(line, root):
    # "Only in /root/sub: nome" -> sub/nome
    d, _, name = line[len("Only in "):].partition(": ")
    d = strip_root(d, root)
    rel = d + "/" + name if d else name
    return {"path": rel, "dir": os.path.isdir(os.path.join(root, rel))}


def parse_report(text):
    head = {"a": "", "b": "", "dest": "", "date": ""}
    counts = {}
    sections = {key: [] for _, key in SECTIONS}
    incomplete = None
    current = None
    n = 0

    for line in text.splitlines():
        if line.startswith("REPORT MERGE - "):
            head["date"] = line[len("REPORT MERGE - "):]
        elif line.startswith("A (prioritaria):"):
            head["a"] = line.split(":", 1)[1].strip()
        elif line.startswith("B:") and not head["b"]:
            head["b"] = line.split(":", 1)[1].strip()
        elif line.startswith("Destinazione:"):
            head["dest"] = line.split(":", 1)[1].strip()
        elif line.startswith("!!! MERGE INCOMPLETO:"):
            incomplete = line.split(":", 1)[1].strip()
        elif current is None and COUNT_RE.match(line) and n < len(COUNT_KEYS):
            counts[COUNT_KEYS[n]] = int(COUNT_RE.match(line).group(2))
            n += 1
        elif SECTION_RE.match(line):
            title = SECTION_RE.match(line).group(1)
            current = next((k for p, k in SECTIONS if title.startswith(p)), None)
        elif line == "":
            current = None
        elif current:
            sections[current].append(line)

    a, b = head["a"], head["b"]
    items = {
        "only_a": [only_item(l, a) for l in sections["only_a"]],
        "only_b": [only_item(l, b) for l in sections["only_b"]],
        "conflicts": [],
        "type_conflicts": [{"path": l.lstrip("/")} for l in sections["type_conflicts"]],
        "moved": [],
        "other": [{"path": l.replace(a + "/", "A/").replace(b + "/", "B/")}
                  for l in sections["other"]],
    }
    for l in sections["conflicts"]:
        # "Files /A/p and /B/p differ" -> p
        m = re.match(r"^Files (.*) and (.*) differ$", l)
        items["conflicts"].append({"path": strip_root(m.group(1), a) if m else l})
    for l in sections["moved"]:
        # "B: ./x   ==   A: ./y"
        pb, _, pa = l.partition("   ==   A: ")
        items["moved"].append({"path": dot_strip(pb[len("B: "):]),
                               "other": dot_strip(pa)})

    return {"head": head, "counts": counts, "items": items, "incomplete": incomplete}


# ------------------------------------------------------------- utilita'
def expand(p):
    return os.path.expanduser((p or "").strip())


def as_string(s):
    """Stringa letterale AppleScript."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------- operazione
class Job:
    """Un'esecuzione di merge_dirs.sh. Vive nel server, indipendente dalla
    pagina: se la connessione cade o la pagina viene ricaricata, lo stato resta."""

    def __init__(self, inputs, dry, no_cache=False):
        self.id = secrets.token_hex(6)
        self.inputs, self.dry, self.no_cache = inputs, dry, no_cache
        self.log, self.out = [], []
        self.progress = None
        self.result = None
        self.cancelled = False
        self.proc = None
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.result is None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                # tutto il gruppo: lo script e i suoi find, sha256sum, rsync
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def snapshot(self, since):
        with self.lock:
            return {"id": self.id, "running": self.running, "dry": self.dry,
                    "inputs": self.inputs, "cancelled": self.cancelled,
                    "log": self.log[since:], "log_len": len(self.log),
                    "progress": self.progress, "result": self.result}

    def _run(self):
        a, b, dest = (expand(self.inputs[k]) for k in ("a", "b", "dest"))
        args = [SCRIPT] + (["-n"] if self.dry else []) + (["-C"] if self.no_cache else []) + ["--", a, b, dest]
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1, errors="replace",
                env=dict(os.environ, MERGE_DIRS_PROGRESS="1"), start_new_session=True)
        except OSError as e:
            self._finish(127, "ERRORE: impossibile avviare lo script: %s" % e)
            return

        quiet, skip = False, 0
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            # "@@P FASE FASI N TOTALE ETICHETTA": avanzamento, non va nel log
            m = PROGRESS_RE.match(line)
            if m:
                step, steps, cur, tot = (int(x) for x in m.group(1, 2, 3, 4))
                with self.lock:
                    self.progress = {"step": step, "steps": steps, "cur": cur,
                                     "tot": tot, "label": m.group(5)}
                continue
            # riepilogo (11 righe) e, con -n, report completo vanno nel
            # riquadro del report, non nel log
            if line.startswith("REPORT MERGE - "):
                skip = 11
            if line.startswith("Modalita' -n"):
                quiet = True
            with self.lock:
                self.out.append(line)
                if skip:
                    skip -= 1
                elif not quiet:
                    self.log.append(line)
        self._finish(self.proc.wait())

    def _finish(self, code, error=None):
        out = self.out
        text = "\n".join(out)
        result = {"code": code, "dry": self.dry, "dest": None,
                  "report_path": None, "report": None, "error": error}
        m = re.search(r"^Fatto\. Merge in:\s+(.*)$", text, re.M)
        if m:
            result["dest"] = m.group(1)
        m = re.search(r"^Report completo:\s+(.*)$", text, re.M) or \
            re.search(r"\(report: (.*)\)$", text, re.M)
        if m:
            result["report_path"] = m.group(1)

        if self.dry and code == 0:
            result["report"] = parse_report(text)
        elif result["report_path"] and os.path.isfile(result["report_path"]):
            with open(result["report_path"], encoding="utf-8", errors="replace") as f:
                result["report"] = parse_report(f.read())
            if code != 0:
                result["dest"] = result["report"]["head"]["dest"]
        if code != 0 and not result["error"]:
            if self.cancelled:
                result["error"] = "Operazione interrotta." + (
                    "" if self.dry else " La cartella di merge e' INCOMPLETA.")
            else:
                i = next((n for n, l in enumerate(out) if l.startswith("ERRORE")), None)
                result["error"] = "\n".join(out[i:]) if i is not None else "\n".join(out[-5:])
        with self.lock:
            self.result = result


JOB = None


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "merge_dirs_web"

    def log_message(self, fmt, *args):
        pass

    def host_ok(self):
        # protezione dal "DNS rebinding": solo richieste dirette a questo server
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, dict):
            body = json.dumps(body)
        body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "host non permesso"})
        if urlparse(self.path).path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        self.send(404, {"error": "non trovato"})

    def do_POST(self):
        if not self.host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self.send(403, {"error": "Accesso negato: riapri la pagina dal link stampato nel Terminale."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, {"error": "richiesta non valida"})
        route = {
            "/api/choose": self.api_choose,
            "/api/run": self.api_run,
            "/api/status": self.api_status,
            "/api/cancel": self.api_cancel,
            "/api/open": self.api_open,
        }.get(urlparse(self.path).path)
        if route is None:
            return self.send(404, {"error": "non trovato"})
        route(data)

    # Finestra di sistema per scegliere una cartella (o il nome di quella nuova)
    def api_choose(self, data):
        prompts = {
            "a": "Scegli la cartella A (prioritaria)",
            "b": "Scegli la cartella B",
            "dest": "Posizione e nome della nuova cartella di merge",
        }
        kind = data.get("kind")
        if kind not in prompts:
            return self.send(400, {"error": "tipo non valido"})

        cur = expand(data.get("current")).rstrip("/")
        loc = cur if os.path.isdir(cur) and kind != "dest" else os.path.dirname(cur)
        if kind == "dest":
            cmd = "choose file name with prompt %s default name %s" % (
                as_string(prompts[kind]), as_string(os.path.basename(cur) or "merge"))
        else:
            cmd = "choose folder with prompt %s" % as_string(prompts[kind])
        if loc and os.path.isdir(loc):
            cmd += " default location (POSIX file %s)" % as_string(loc)

        # "activate" porta la finestra davanti al browser
        p = subprocess.run(["osascript", "-e", "activate", "-e", "POSIX path of (%s)" % cmd],
                           capture_output=True, text=True)
        if p.returncode != 0:
            if "-128" in p.stderr:
                return self.send(200, {"cancelled": True})
            return self.send(500, {"error": p.stderr.strip() or "finestra non disponibile"})
        path = p.stdout.strip()
        if kind != "dest":
            path = path.rstrip("/") or "/"
        self.send(200, {"path": path})

    # Avvia merge_dirs.sh in background; la pagina ne segue lo stato con /api/status
    def api_run(self, data):
        global JOB
        inputs = {k: (data.get(k) or "").strip() for k in ("a", "b", "dest")}
        if not all(inputs.values()):
            return self.send(400, {"error": "Indica tutte e tre le cartelle."})
        with JOB_LOCK:
            if JOB and JOB.running:
                return self.send(409, {"error": "C'e' gia' un'operazione in corso: attendi o interrompila."})
            JOB = Job(inputs, bool(data.get("dry")), bool(data.get("no_cache")))
        JOB.start()
        self.send(200, {"id": JOB.id})

    # Stato dell'operazione corrente (o dell'ultima): log dalla riga "since" in poi
    def api_status(self, data):
        job = JOB
        if job is None:
            return self.send(200, {"id": None})
        since = data.get("since") if data.get("id") == job.id else 0
        self.send(200, job.snapshot(since if isinstance(since, int) else 0))

    def api_cancel(self, data):
        job = JOB
        if job is None or not job.running:
            return self.send(409, {"error": "Nessuna operazione in corso."})
        job.cancel()
        self.send(200, {"ok": True})

    # Apre una cartella o mostra un file nel Finder
    def api_open(self, data):
        path = expand(data.get("path"))
        if not os.path.exists(path):
            return self.send(404, {"error": "'%s' non esiste" % path})
        cmd = ["open", "-R", path] if data.get("reveal") else ["open", path]
        subprocess.run(cmd)
        self.send(200, {"ok": True})


def main():
    ap = argparse.ArgumentParser(description="Interfaccia web locale per merge_dirs.sh")
    ap.add_argument("--porta", type=int, default=8765, help="porta iniziale (default 8765)")
    ap.add_argument("--no-browser", action="store_true", help="non aprire il browser")
    args = ap.parse_args()

    if not os.access(SCRIPT, os.X_OK):
        sys.exit("ERRORE: non trovo lo script eseguibile %s" % SCRIPT)

    srv = None
    for port in range(args.porta, args.porta + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("ERRORE: nessuna porta libera tra %d e %d" % (args.porta, args.porta + 19))

    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], TOKEN)
    print("Interfaccia di merge_dirs attiva:\n  %s\nCtrl+C per chiudere." % url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        # non lasciare uno script orfano che continua a lavorare
        if JOB and JOB.running:
            print("\nInterrompo l'operazione in corso...")
            JOB.cancel()
        print("\nChiuso.")


# ------------------------------------------------------------------ pagina
PAGE = r"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Merge cartelle</title>
<style>
:root {
  --bg: #f5f5f3; --card: #ffffff; --text: #1d1d1f; --muted: #6e6e73; --line: #e2e2e0;
  --accent: #2f6fed; --accent-text: #ffffff; --ok: #1f8a4c; --err: #c9372c; --warn: #b26a00;
  --soft-ok: #e7f5ec; --soft-err: #fbeceb; --soft-warn: #fdf3e2; --code: #f0f0ee;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161617; --card: #1f1f21; --text: #f2f2f2; --muted: #9a9aa0; --line: #333336;
    --accent: #5b8ff9; --accent-text: #0b0b0c; --ok: #4cc27f; --err: #f06a5f; --warn: #f0a640;
    --soft-ok: #18301f; --soft-err: #3a1d1b; --soft-warn: #362a14; --code: #19191a;
    color-scheme: dark;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: var(--muted); margin: 0 0 24px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 20px; margin-bottom: 16px; }
.card h2 { font-size: 15px; margin: 0 0 14px; display: flex; align-items: center; gap: 10px; }
.field { margin-bottom: 14px; }
.field label { display: block; font-weight: 600; margin-bottom: 4px; }
.field .hint { color: var(--muted); font-size: 13px; margin-top: 3px; }
.row { display: flex; gap: 8px; }
.row input { flex: 1; min-width: 0; }
input[type=text] { font: 13px ui-monospace, SFMono-Regular, Menlo, monospace; padding: 8px 10px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--bg); color: var(--text); }
input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button { font: inherit; font-size: 14px; padding: 8px 14px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: var(--card); color: var(--text); white-space: nowrap; }
button:hover:not(:disabled) { border-color: var(--muted); }
button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-text); font-weight: 600; }
button.danger { background: var(--err); border-color: var(--err); color: #fff; font-weight: 600; }
button:disabled { opacity: .45; cursor: default; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 6px; }
.note { color: var(--muted); font-size: 13px; }
.check { display: flex; gap: 8px; align-items: center; font-size: 13.5px; color: var(--muted); margin: 2px 0 12px; cursor: pointer; }
.confirm { margin-top: 12px; padding: 12px 14px; border-radius: 8px; background: var(--soft-warn);
  display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.confirm span { flex: 1 1 260px; }
.badge { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 99px; background: var(--code); color: var(--muted); }
.badge.ok { background: var(--soft-ok); color: var(--ok); }
.badge.err { background: var(--soft-err); color: var(--err); }
.badge.run { color: var(--accent); }
.progress { margin-bottom: 14px; }
.progress .top { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  font-size: 13.5px; margin-bottom: 6px; }
.progress .top b { font-weight: 600; }
.progress .top span { color: var(--muted); font-variant-numeric: tabular-nums; }
.bar { height: 8px; border-radius: 99px; background: var(--code); overflow: hidden; }
.bar > div { height: 100%; width: 0; background: var(--accent); border-radius: 99px; transition: width .25s; }
.bar.ok > div { background: var(--ok); }
.bar.err > div { background: var(--err); }
.bar.indet > div { width: 30% !important; animation: indet 1.2s ease-in-out infinite; }
@keyframes indet { 0% { margin-left: -30%; } 100% { margin-left: 100%; } }
.steps { display: flex; gap: 4px; margin-top: 8px; }
.steps i { flex: 1; height: 3px; border-radius: 99px; background: var(--code); }
.steps i.done { background: var(--ok); }
.steps i.cur { background: var(--accent); }
pre.log { margin: 0; max-height: 220px; overflow: auto; background: var(--code); border-radius: 8px;
  padding: 10px 12px; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; word-break: break-all; }
.alert { padding: 12px 14px; border-radius: 8px; margin-bottom: 14px; white-space: pre-wrap;
  font-size: 14px; }
.alert.err { background: var(--soft-err); color: var(--err); }
.alert.warn { background: var(--soft-warn); color: var(--warn); }
.alert.ok { background: var(--soft-ok); color: var(--ok); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 16px; }
.stat { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.stat b { display: block; font-size: 22px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 12.5px; }
details { border-top: 1px solid var(--line); }
details:last-of-type { border-bottom: 1px solid var(--line); }
summary { cursor: pointer; padding: 10px 2px; display: flex; gap: 10px; align-items: baseline; list-style: none; }
summary::-webkit-details-marker { display: none; }
summary::before { content: "\25B8"; color: var(--muted); width: 10px; transition: transform .15s; }
details[open] summary::before { transform: rotate(90deg); }
summary .t { font-weight: 600; flex: 1; }
summary .d { color: var(--muted); font-size: 13px; font-weight: 400; }
summary .n { font-variant-numeric: tabular-nums; color: var(--muted); }
details.empty summary { cursor: default; opacity: .5; }
details.empty summary::before { visibility: hidden; }
ul.items { list-style: none; margin: 0 0 12px; padding: 0 0 0 20px; max-height: 360px; overflow: auto;
  font: 12.5px/1.7 ui-monospace, SFMono-Regular, Menlo, monospace; }
ul.items li { word-break: break-all; }
ul.items .dir { color: var(--accent); }
ul.items .eq { color: var(--muted); }
.meta { color: var(--muted); font-size: 13px; margin: 0 0 14px; word-break: break-all; }
[hidden] { display: none !important; }
@media (max-width: 560px) { .row { flex-wrap: wrap; } .row button { flex: 1; } }
</style>
</head>
<body>
<main>
  <h1>Merge cartelle</h1>
  <p class="sub">Unisce A e B in una terza cartella nuova. A e B non vengono mai modificate; in caso di conflitto vince A.</p>

  <div class="alert err" id="tokenErr" hidden>Link non valido: riapri la pagina dal link stampato nel Terminale da merge_dirs_web.py.</div>

  <section class="card">
    <h2>Cartelle</h2>
    <div class="field">
      <label for="a">A &mdash; prioritaria</label>
      <div class="row"><input type="text" id="a" placeholder="~/Foto" spellcheck="false"><button data-choose="a">Sfoglia&hellip;</button></div>
      <div class="hint">In caso di conflitto viene tenuta la versione di questa cartella.</div>
    </div>
    <div class="field">
      <label for="b">B</label>
      <div class="row"><input type="text" id="b" placeholder="~/Backup/Foto" spellcheck="false"><button data-choose="b">Sfoglia&hellip;</button></div>
      <div class="hint">Da qui viene aggiunto solo cio' che manca in A.</div>
    </div>
    <div class="field">
      <label for="dest">Nuova cartella di merge</label>
      <div class="row"><input type="text" id="dest" placeholder="~/Foto_unite" spellcheck="false"><button data-choose="dest">Sfoglia&hellip;</button></div>
      <div class="hint">Deve essere nuova o vuota e non puo' stare dentro A o B.</div>
    </div>
    <label class="check"><input type="checkbox" id="noCache"> Ricalcola tutti i checksum (ignora la cache)</label>
    <div class="actions">
      <button class="primary" id="preview">Anteprima</button>
      <button id="run" disabled>Esegui merge&hellip;</button>
      <span class="note" id="runNote">Fai prima l'anteprima.</span>
    </div>
    <div class="confirm" id="confirm" hidden>
      <span id="confirmText"></span>
      <button class="danger" id="confirmYes">Si', esegui</button>
      <button id="confirmNo">Annulla</button>
    </div>
  </section>

  <section class="card" id="logCard" hidden>
    <h2>Avanzamento <span class="badge" id="status"></span><button id="cancel" hidden style="margin-left:auto">Interrompi</button></h2>
    <div class="progress" id="progress">
      <div class="top"><b id="pLabel">Avvio&hellip;</b><span id="pCount"></span></div>
      <div class="bar indet" id="pBar"><div></div></div>
      <div class="steps" id="pSteps"></div>
    </div>
    <pre class="log" id="log"></pre>
  </section>

  <section class="card" id="reportCard" hidden>
    <h2 id="reportTitle">Report</h2>
    <div id="reportAlert"></div>
    <p class="meta" id="reportMeta"></p>
    <div class="actions" id="finder" hidden style="margin: 0 0 16px">
      <button id="openDest">Apri la cartella di merge</button>
      <button id="openReport">Mostra il report nel Finder</button>
    </div>
    <div class="stats" id="stats"></div>
    <div id="sections"></div>
  </section>
</main>

<script>
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = id => document.getElementById(id);
const FIELDS = ["a", "b", "dest"];
let previewed = null;   // cartelle dell'ultima anteprima riuscita
let busy = false;
let last = null;        // ultimo risultato (per i pulsanti del Finder)

const SECTIONS = [
  ["only_a", "Solo in A", "copiati cosi' come sono"],
  ["only_b", "Solo in B", "aggiunti al merge"],
  ["conflicts", "Contenuto diverso", "tenuta la versione di A"],
  ["type_conflicts", "File contro cartella", "tenuta la versione di A"],
  ["moved", "Gia' presenti in A in un'altra posizione", "non copiati"],
  ["other", "Altre segnalazioni", ""],
];

if (!TOKEN) $("tokenErr").hidden = false;

// ---------------------------------------------------------------- utilita'
function api(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    body: JSON.stringify(body),
  });
}
function values() { return Object.fromEntries(FIELDS.map(k => [k, $(k).value.trim()])); }
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function save() { try { localStorage.setItem("merge_dirs", JSON.stringify(values())); } catch (e) {} }
try {
  const v = JSON.parse(localStorage.getItem("merge_dirs") || "{}");
  FIELDS.forEach(k => { if (v[k]) $(k).value = v[k]; });
} catch (e) {}

function refreshButtons() {
  const same = previewed && JSON.stringify(previewed) === JSON.stringify(values());
  const filled = FIELDS.every(k => $(k).value.trim());
  $("preview").disabled = busy;
  $("run").disabled = busy || !filled;
  document.querySelectorAll("[data-choose]").forEach(b => b.disabled = busy);
  $("runNote").textContent = busy ? "Operazione in corso…"
    : same ? "Anteprima pronta: il merge riusera' i checksum gia' calcolati."
    : previewed ? "Le cartelle sono cambiate: puoi rifare l'anteprima o eseguire direttamente."
    : "Puoi fare prima l'anteprima, oppure eseguire direttamente il merge.";
}
FIELDS.forEach(k => $(k).addEventListener("input", () => { save(); refreshButtons(); }));

// ------------------------------------------------------- scelta cartelle
document.querySelectorAll("[data-choose]").forEach(btn => btn.addEventListener("click", async () => {
  const kind = btn.dataset.choose;
  btn.disabled = true;
  try {
    const r = await api("/api/choose", { kind, current: $(kind).value });
    const j = await r.json();
    if (j.path) { $(kind).value = j.path; save(); }
    else if (j.error) alertBox("err", "Impossibile aprire la finestra: " + j.error);
  } catch (e) { alertBox("err", "Server non raggiungibile. E' ancora attivo nel Terminale?"); }
  btn.disabled = false;
  refreshButtons();
}));

// ---------------------------------------------------------- esecuzione
$("preview").addEventListener("click", () => run(true));
$("run").addEventListener("click", () => {
  const same = previewed && JSON.stringify(previewed) === JSON.stringify(values());
  const t = $("confirmText");
  t.replaceChildren("Verra' creata ", el("b", null, values().dest), " con l'unione di A e B. ");
  if (!same) t.append(el("b", null, "Senza anteprima: "),
    "conflitti e duplicati li vedrai nel report alla fine. ");
  t.append("Procedo?");
  $("confirm").hidden = false;
});
FIELDS.forEach(k => $(k).addEventListener("input", () => { $("confirm").hidden = true; }));
$("confirmNo").addEventListener("click", () => { $("confirm").hidden = true; });
$("confirmYes").addEventListener("click", () => { $("confirm").hidden = true; run(false); });

const fmt = n => n.toLocaleString("it-IT");
let pStart = 0, pKey = "";

function showProgress(p) {
  const bar = $("pBar");
  bar.className = "bar" + (p.tot ? "" : " indet");
  $("pLabel").textContent = "Fase " + p.step + " di " + p.steps + " · " + p.label;
  // stima del tempo restante, ricalcolata a ogni fase
  const key = p.step + p.label;
  if (key !== pKey) { pKey = key; pStart = Date.now(); }
  let txt = "";
  if (p.tot) {
    const pct = Math.min(100, Math.floor(p.cur * 100 / p.tot));
    bar.firstElementChild.style.width = pct + "%";
    txt = fmt(Math.min(p.cur, p.tot)) + " / " + fmt(p.tot) + " · " + pct + "%";
    const el_ = (Date.now() - pStart) / 1000;
    if (p.cur > 0 && p.cur < p.tot && el_ > 3) {
      const left = Math.round(el_ * (p.tot - p.cur) / p.cur);
      txt += " · circa " + (left >= 90 ? Math.round(left / 60) + " min" : left + " s");
    }
  } else txt = "in corso…";
  $("pCount").textContent = txt;
  const steps = $("pSteps");
  if (steps.children.length !== p.steps)
    steps.replaceChildren(...Array.from({ length: p.steps }, () => el("i")));
  [...steps.children].forEach((s, i) => s.className = i + 1 < p.step ? "done" : i + 1 === p.step ? "cur" : "");
}
function endProgress(ok) {
  const bar = $("pBar");
  bar.className = "bar " + (ok ? "ok" : "err");
  if (ok) {
    bar.firstElementChild.style.width = "100%";
    [...$("pSteps").children].forEach(s => s.className = "done");
    $("pLabel").textContent = "Completato";
  } else $("pLabel").textContent = "Interrotto: " + $("pLabel").textContent.replace(/^Interrotto: /, "");
}

function setStatus(cls, text) { const s = $("status"); s.className = "badge " + cls; s.textContent = text; }
function alertBox(cls, text) {
  $("reportCard").hidden = false;
  $("reportTitle").textContent = "Esito";
  const box = $("reportAlert");
  box.replaceChildren(el("div", "alert " + cls, text));
}

let jobId = null, logLen = 0, pollTimer = null, netErrors = 0, curDry = true;

function resetRun(dry) {
  curDry = dry;
  $("logCard").hidden = false; $("log").textContent = "";
  $("pBar").className = "bar indet"; $("pBar").firstElementChild.style.width = "0";
  $("pLabel").textContent = "Avvio…"; $("pCount").textContent = ""; $("pSteps").replaceChildren(); pKey = "";
  setStatus("run", dry ? "anteprima in corso…" : "merge in corso…");
  $("reportCard").hidden = true; $("reportAlert").replaceChildren();
  $("stats").replaceChildren(); $("sections").replaceChildren(); $("finder").hidden = true;
  resetCancel();
}

async function run(dry) {
  const v = values();
  resetRun(dry);
  busy = true; refreshButtons();
  try {
    const r = await api("/api/run", { ...v, dry, no_cache: $("noCache").checked });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.error || ("Errore " + r.status)), { status: r.status });
    jobId = j.id; logLen = 0; netErrors = 0;
    poll();
  } catch (e) {
    setStatus("err", "errore"); endProgress(false);
    alertBox("err", e.message === "Failed to fetch" || e.message === "Load failed"
      ? "Server non raggiungibile. E' ancora attivo nel Terminale?" : e.message);
    busy = false; refreshButtons();
    // se un'altra operazione e' gia' in corso, la si mostra
    if (e.status === 409) poll();
  }
}

// Lo stato vive nel server: la pagina lo chiede a intervalli regolari, quindi
// un'interruzione della connessione o un ricaricamento non perdono nulla
async function poll() {
  clearTimeout(pollTimer);
  let s;
  try {
    const r = await api("/api/status", { id: jobId, since: logLen });
    if (!r.ok) throw new Error("Errore " + r.status);
    s = await r.json();
    netErrors = 0;
  } catch (e) {
    if (++netErrors <= 20) { pollTimer = setTimeout(poll, 1500); return; }
    setStatus("err", "server non raggiungibile");
    alertBox("err", "Il server non risponde. Se e' ancora attivo nel Terminale ricarica la pagina: l'operazione continua sul server e la ritrovi.");
    busy = false; refreshButtons();
    return;
  }
  if (!s.id) return;                       // nessuna operazione finora
  if (s.id !== jobId) {                    // operazione avviata prima (es. pagina ricaricata)
    jobId = s.id; logLen = 0;
    resetRun(s.dry);
    FIELDS.forEach(k => { $(k).value = s.inputs[k]; });
  }
  if (s.log.length) {
    const log = $("log");
    log.textContent += s.log.join("\n") + "\n";
    log.scrollTop = log.scrollHeight;
  }
  logLen = s.log_len;
  if (s.progress) showProgress(s.progress);

  if (s.running) {
    busy = true; $("cancel").hidden = false;
    setStatus("run", s.cancelled ? "interruzione in corso…" : s.dry ? "anteprima in corso…" : "merge in corso…");
    refreshButtons();
    pollTimer = setTimeout(poll, 800);
    return;
  }
  finish(s.result, s.inputs, s.cancelled);
}

function finish(result, inputs, cancelled) {
  busy = false; last = result;
  $("cancel").hidden = true; resetCancel();
  endProgress(result.code === 0);
  if (result.code === 0) {
    setStatus("ok", result.dry ? "anteprima completata" : "merge completato");
    previewed = result.dry ? inputs : null;   // dopo il merge la destinazione non e' piu' vuota
  } else {
    setStatus("err", cancelled ? "interrotto" : "errore (codice " + result.code + ")");
    previewed = null;
  }
  showResult(result);
  refreshButtons();
}

// Interrompi: immediato per l'anteprima, con conferma per il merge
let cancelArmed = false;
function resetCancel() {
  cancelArmed = false;
  $("cancel").textContent = "Interrompi";
  $("cancel").className = "";
}
$("cancel").addEventListener("click", async () => {
  if (!curDry && !cancelArmed) {
    cancelArmed = true;
    $("cancel").textContent = "Conferma: il merge restera' incompleto";
    $("cancel").className = "danger";
    setTimeout(() => { if (cancelArmed) resetCancel(); }, 5000);
    return;
  }
  $("cancel").disabled = true;
  try { await api("/api/cancel", {}); } catch (e) {}
  $("cancel").disabled = false;
  poll();
});

// ---------------------------------------------------------------- report
function showResult(r) {
  $("reportCard").hidden = false;
  const box = $("reportAlert"); box.replaceChildren();
  $("reportTitle").textContent = r.dry ? "Anteprima del merge" : "Report del merge";

  if (r.error) box.append(el("div", "alert err", r.error));
  if (r.report && r.report.incomplete)
    box.append(el("div", "alert warn", "Il merge e' INCOMPLETO: " + r.report.incomplete +
      ".\nLa cartella di destinazione contiene solo una parte dei file."));
  if (r.code === 0 && !r.dry)
    box.append(el("div", "alert ok", "Merge completato in " + r.dest));
  if (r.code === 0 && r.dry)
    box.append(el("div", "alert ok", "Nessun file e' stato copiato. Controlla il riepilogo e poi premi “Esegui merge”."));

  $("finder").hidden = r.dry || !(r.dest || r.report_path);
  $("openDest").hidden = !r.dest;
  $("openReport").hidden = !r.report_path;

  const rep = r.report;
  $("reportMeta").textContent = "";
  if (!rep) return;
  $("reportMeta").textContent = "A: " + rep.head.a + "  ·  B: " + rep.head.b + "  ·  Destinazione: " + rep.head.dest;

  const stats = $("stats");
  SECTIONS.slice(0, 5).forEach(([key, title]) => {
    const s = el("div", "stat");
    s.append(el("b", null, rep.counts[key] ?? rep.items[key].length), el("span", null, title));
    stats.append(s);
  });

  const secs = $("sections");
  SECTIONS.forEach(([key, title, desc]) => {
    const items = rep.items[key] || [];
    if (key === "other" && !items.length) return;
    const d = el("details", items.length ? "" : "empty");
    const sum = el("summary");
    const t = el("span", "t", title + " ");
    if (desc) t.append(el("span", "d", "— " + desc));
    sum.append(t, el("span", "n", items.length));
    d.append(sum);
    if (!items.length) sum.addEventListener("click", e => e.preventDefault());
    else d.addEventListener("toggle", () => { if (d.open && !d.dataset.filled) fill(d, key, items); }, { once: false });
    secs.append(d);
  });
}

function fill(d, key, items) {
  d.dataset.filled = "1";
  const MAX = 2000;
  const ul = el("ul", "items");
  items.slice(0, MAX).forEach(it => {
    const li = el("li");
    if (key === "moved") {
      li.append(el("span", null, "B/" + it.path), el("span", "eq", "  =  "), el("span", null, "A/" + it.other));
    } else {
      li.append(el("span", it.dir ? "dir" : null, it.path + (it.dir ? "/" : "")));
      if (it.dir) li.append(el("span", "eq", "  (cartella, con tutto il contenuto)"));
    }
    ul.append(li);
  });
  if (items.length > MAX) ul.append(el("li", "eq", "… e altri " + (items.length - MAX) + " (vedi il file di report)"));
  d.append(ul);
}

$("openDest").addEventListener("click", () => last && api("/api/open", { path: last.dest }));
$("openReport").addEventListener("click", () => last && api("/api/open", { path: last.report_path, reveal: true }));

refreshButtons();
poll();   // riprende un'operazione gia' in corso o mostra l'ultima
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()

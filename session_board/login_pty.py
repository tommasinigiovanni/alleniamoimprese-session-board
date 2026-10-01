# login_pty.py
"""Login di un abbonamento Claude guidato dalla dashboard, senza terminale.

`claude auth login` pretende un terminale: senza, non stampa nulla e va in
timeout. Ma un terminale vero può essere uno pseudo-terminale creato dal
server. Qui il comando UFFICIALE viene lanciato dentro un pty (`pty.fork`),
la dashboard legge quello che stampa e gli riscrive quello che l'utente gli
darebbe a mano. Nessuna riga di questo modulo reimplementa il protocollo di
autorizzazione e nessuna tocca le intestazioni di rete: si legge e si scrive
su un descrittore, punto.

Il testo stampato è la fonte di DUE sole cose: il link di autorizzazione e il
messaggio di codice non valido. Il SUCCESSO non si deduce mai dal testo - si
interroga `claude_accounts.account_status`, l'unica verifica che non si rompe
se un domani il comando cambia le sue frasi.

Vincolo di servizio: la dashboard è un server Flask di sviluppo che serve
anche il resto della board, quindi NESSUNA chiamata HTTP può restare bloccata
in lettura ad aspettare la fine del flusso. Ogni lettura ha una finestra
breve (le costanti `*_READ_SECONDS`) e lo stato del login sopravvive fra una
richiesta e l'altra dentro il gestore: è il browser a tornare a chiedere, non
il server ad aspettare.

Il codice incollato dall'utente non viene MAI registrato nei log e non
compare mai in una risposta: viene scritto sul descrittore del processo (non
in una shell: nessun rischio di iniezione di comandi) e dimenticato.
"""
import logging
import os
import re
import select
import signal
import threading
import time
import urllib.parse

from . import claude_accounts

_LOGGER = logging.getLogger("login_pty")

CLAUDE_BIN = os.environ.get("CLAUDE_STATUS_BIN", "claude")

# Finestre di lettura. Sono la traduzione del vincolo "mai bloccare la
# dashboard": nessuna richiesta HTTP di questo modulo può superare la somma
# della propria finestra di lettura più (quando serve davvero) la lettura
# dello stato dell'abbonamento.
#   - avvio: il comando è un processo node che deve partire e stampare il
#     link. Sulla macchina ci mette 1-2 secondi; 5 dà margine senza far
#     sembrare la board bloccata. Se il link non c'è ancora, la richiesta
#     torna comunque (stato "starting") e il browser richiede: il processo
#     resta vivo, la lettura riprende da dove era.
#   - poll: 1.5 al massimo, ma si esce appena non arriva più niente
#     (tipicamente 0.2), perché il poll gira ogni pochi secondi.
#   - codice: 1.2, quanto basta a raccogliere la risposta immediata del
#     comando ("Invalid code..."); l'esito vero lo dà lo stato, non il testo.
START_READ_SECONDS = 5.0
POLL_READ_SECONDS = 1.5
CODE_READ_SECONDS = 1.2
# Lettura dello stato dell'abbonamento: `claude auth status` è un altro
# processo node. 4 secondi è il tetto oltre il quale si preferisce rispondere
# "verifica in corso" e riprovare al poll successivo, invece di tenere
# occupata la richiesta.
STATUS_TIMEOUT_SECONDS = 4.0
# Un login che non ha ancora prodotto il link dopo 45 secondi è un comando
# piantato, non un comando lento: si chiude.
URL_WAIT_SECONDS = 45.0
# Dopo l'invio del codice la verifica è una domanda allo stato
# dell'abbonamento, ripetuta a ogni poll del browser. 90 secondi è il tetto:
# oltre, il codice non è stato accettato e continuare significherebbe solo
# lanciare un processo `claude auth status` ogni due secondi per un quarto
# d'ora. Si dichiara il fallimento e si lascia ripartire il login.
CODE_WAIT_SECONDS = 90.0
# Tempo massimo di vita di un login in corso: l'utente deve aprire un'altra
# finestra del browser, autenticarsi (magari con un secondo fattore) e
# incollare il codice. 15 minuti sono generosi e comunque finiti: oltre, il
# processo viene chiuso e la traccia rimossa, perché un login abbandonato non
# deve lasciare processi vivi sulla macchina.
MAX_AGE_SECONDS = 900.0
# Ogni quanto lo spazzino di fondo ripassa a chiudere gli abbandonati. Il
# thread esiste solo finché c'è almeno un login in corso.
JANITOR_INTERVAL_SECONDS = 30.0

MAX_CODE_LENGTH = 512
# Larghezza dello pseudo-terminale. Il link di autorizzazione vero è di ~450
# caratteri (misurato dal vivo): con 400 colonne il margine era NEGATIVO e una
# riga andata a capo avrebbe spezzato l'indirizzo visibile. Mille colonne
# tolgono di mezzo il caso, anche se i parametri del link si allungano.
PTY_COLUMNS = 1000
PTY_ROWS = 50
# Tetto alla scrittura del codice sul descrittore (non bloccante): oltre, si
# dice "riprova" invece di troncare il codice o di dare per morta la sessione.
WRITE_SECONDS = 1.0
# Coda dell'output tenuta in memoria: serve solo a cercare il link e il
# messaggio di codice non valido, non è uno storico.
_MAX_BUFFER = 64 * 1024

# I due soli domini su cui il comando ufficiale manda ad autorizzare un
# abbonamento Claude (claude.ai è la forma storica, claude.com quella
# corrente). Il link arriva dal testo di un processo: si mostra solo se è
# davvero uno di questi.
ALLOWED_AUTH_HOSTS = frozenset({"claude.ai", "claude.com"})

# L'unico messaggio d'errore su cui ci si appoggia, e solo per dire "riprova":
# se un domani cambia, l'esito resta comunque corretto (lo decide lo stato
# dell'abbonamento), si perde solo la spiegazione immediata.
_INVALID_CODE_MARKER = "invalid code"

_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC ... BEL | ST (titolo, OSC 8)
    r"|\x1b[P^_][^\x1b]*\x1b\\"            # DCS/PM/APC
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI (colori, cursore, cancellazioni)
    r"|\x1b[@-Z\\-_]"                      # sequenze a due byte
)
_URL_RE = re.compile(r"https?://[^\s\"'<>`\\]+")
# OSC 8: ESC ]8;<parametri>;<bersaglio> chiuso da BEL o da ST. Il comando vero
# avvolge il link in questa sequenza, quindi l'indirizzo compare DUE volte -
# una come bersaglio del collegamento e una come testo visibile. Il BERSAGLIO
# è la fonte migliore: non subisce l'andata a capo del terminale, mentre il
# testo visibile sì.
_OSC8_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]*)(?:\x07|\x1b\\)")


class NoPendingLogin(ValueError):
    """Nessun login in corso per questo abbonamento: non è un errore di input."""

    http_status = 404


class WriteNotAccepted(OSError):
    """Il descrittore non ha accettato la scrittura ADESSO (buffer pieno).

    Sottoclasse di OSError come BlockingIOError, ma va distinta: significa
    "riprova", non "il processo è morto". Confonderle distruggeva una
    sessione sana.
    """


def clean_output(text):
    """Testo leggibile a partire da quello che esce da un pty."""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch >= " ")


def _accept_auth_url(candidate):
    """L'indirizzo se è davvero un link di autorizzazione atteso, altrimenti None.

    Non basta "una https": il valore finisce in un collegamento cliccabile
    nella pagina. Si accettano solo https, solo i domini dell'allowlist
    (`hostname` di urlparse, che scarta da sé la userinfo del tipo
    `https://claude.com@altro.example/`) e solo un percorso di autorizzazione.
    """
    candidate = candidate.strip().rstrip(".,;:)]}'\"")
    if not candidate or "\x1b" in candidate:
        return None
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    if (parsed.hostname or "").lower() not in ALLOWED_AUTH_HOSTS:
        return None
    if "oauth" not in parsed.path.lower():
        return None
    return candidate


def extract_auth_url(text):
    """Il link di autorizzazione cercato nel testo VISIBILE, già ripulito.

    Un indirizzo che tocca la FINE del testo letto finora viene ignorato: il
    pty consegna a pezzi, e quel pezzo può essere la prima metà del link. Un
    link troncato passerebbe ogni controllo (è https, è il dominio giusto, ha
    "oauth" nel percorso) e arriverebbe all'utente rotto. Si aspetta la
    lettura successiva: il comando stampa comunque un a-capo e il prompt del
    codice subito dopo il link.
    """
    for match in _URL_RE.finditer(text):
        if match.end() >= len(text):
            continue
        accepted = _accept_auth_url(match.group(0))
        if accepted:
            return accepted
    return None


def find_auth_url(raw):
    """Il link di autorizzazione a partire dall'uscita GREZZA del pty.

    Prima il bersaglio delle sequenze OSC 8, poi - se non ce n'è uno buono -
    il testo visibile ripulito. L'ordine non è un dettaglio: il comando vero
    avvolge il link in un OSC 8 e poi lo ristampa come testo, quindi
    l'indirizzo compare due volte. Cercarlo sul testo di due letture ripulite
    separatamente (con il taglio dentro la sequenza) restituiva i due
    indirizzi ATTACCATI - 898 caratteri invece di 450, un link che non
    funziona. Per questo si parte sempre dal grezzo INTERO accumulato, mai da
    un pezzo ripulito per conto suo.
    """
    for match in _OSC8_RE.finditer(raw):
        accepted = _accept_auth_url(match.group(1))
        if accepted:
            return accepted
    return extract_auth_url(clean_output(raw))


def validate_code(value):
    """Il codice ripulito, o ValueError. Il valore non compare mai nell'errore.

    Il codice non passa da una shell - va sul descrittore di un processo -
    quindi non c'è iniezione di comandi da temere. Si valida lo stesso: un
    a-capo dentro il valore sarebbe un invio in più al comando, e una stringa
    lunghissima solo spazzatura da scrivere in un terminale.
    """
    if not isinstance(value, str):
        raise ValueError("codice mancante")
    code = value.strip()
    if not code:
        raise ValueError("codice mancante")
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError("codice troppo lungo (massimo %d caratteri)" % MAX_CODE_LENGTH)
    if any(ch < " " or ch == "\x7f" for ch in code):
        raise ValueError("il codice contiene caratteri di controllo")
    return code


def _write_all(fd, data, writer=None, waiter=None, seconds=WRITE_SECONDS):
    """Scrive TUTTI i byte sul descrittore non bloccante, o solleva.

    `os.write` su un descrittore non bloccante può scriverne solo una parte:
    ignorare il valore di ritorno significa troncare il codice in silenzio, e
    un codice troncato è un codice sbagliato che l'utente non sa di aver
    mandato. Buffer pieno (EAGAIN) -> WriteNotAccepted, cioè "riprova": non è
    un processo morto, e non deve distruggere la sessione.
    """
    write = writer or os.write
    wait = waiter or select.select
    view = memoryview(data)
    deadline = time.monotonic() + max(seconds, 0.0)
    while view:
        try:
            written = write(fd, view)
        except BlockingIOError:
            written = 0
        if written:
            view = view[written:]
            continue
        if time.monotonic() >= deadline:
            raise WriteNotAccepted("il descrittore non accetta la scrittura")
        wait([], [fd], [], 0.05)
    return len(data)


def login_argv(binary=None, email=None):
    """Il comando ufficiale, invariato: non si reimplementa niente."""
    argv = [binary or CLAUDE_BIN, "auth", "login", "--claudeai"]
    if email:
        argv += ["--email", email]
    return argv


def login_env(directory, base=None):
    """Ambiente del login: la config dir dell'abbonamento e un terminale vero.

    DISPLAY viene tolta di proposito: il comando prova ad aprire un browser e
    sulla macchina c'è un Chromium persistente condiviso su :99 che non deve
    essere dirottato da un login della board. Senza DISPLAY il tentativo
    fallisce da solo e resta la riga "If the browser didn't open, visit: ...",
    che è esattamente quella che serve qui.
    """
    env = dict(os.environ if base is None else base)
    for name in claude_accounts._AUTH_ENV:
        env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = directory
    env["TERM"] = "xterm-256color"
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("BROWSER", None)
    return env


def _spawn_login_pty(slug, directory, email=None, binary=None, environment=None):
    """Lancia il comando dentro uno pseudo-terminale. Ritorna (pid, fd)."""
    import fcntl
    import pty
    import struct
    import termios

    argv = login_argv(binary=binary, email=email)
    env = login_env(directory, base=environment)
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - il figlio esegue solo exec
        try:
            os.execvpe(argv[0], argv, env)
        except BaseException:
            os._exit(127)
    # Finestra larga: il link di autorizzazione è lungo (~450 caratteri) e un
    # terminale stretto lo manderebbe a capo spezzando l'indirizzo visibile.
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0))
    except OSError:
        pass
    try:
        os.set_blocking(fd, False)
    except OSError:
        pass
    return pid, fd


def _signal_process(pid, number):
    """Segnale al GRUPPO (pty.fork fa setsid: il figlio è capogruppo di una
    sessione tutta sua), con ripiego sul solo processo."""
    try:
        os.killpg(pid, number)
        return True
    except OSError:
        pass
    try:
        os.kill(pid, number)
        return True
    except OSError:
        return False


def _reap(pid, seconds):
    """Raccoglie il processo per al massimo `seconds`. True se raccolto.

    SOLO attese non bloccanti: questa funzione gira con il lock del gestore
    preso, quindi non può permettersi un `waitpid` che aspetta senza tetto.
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except OSError:
            return True  # non è (più) un nostro figlio: niente da raccogliere
        if done:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _terminate_process(pid):
    """Chiude il processo del login e ne raccoglie il cadavere, con un tetto.

    Tetto complessivo ~1 secondo (0,6 dopo il TERM, 0,4 dopo il KILL). Se
    nemmeno dopo il KILL il processo viene raccolto, si preferisce lasciare
    uno zombie che tenere occupato il lock del gestore: il caso non si è mai
    visto, e un lock tenuto a oltranza fermerebbe ogni login.
    """
    _signal_process(pid, signal.SIGTERM)
    if _reap(pid, 0.6):
        return
    _signal_process(pid, signal.SIGKILL)
    if not _reap(pid, 0.4):
        _LOGGER.warning("processo di login %s non raccolto dopo SIGKILL", pid)


def bounded_status_reader(slug):
    """Stato dell'abbonamento con un tetto di tempo adatto a una richiesta web.

    Il default di `claude_accounts` è un timeout di 10 secondi: troppo per una
    chiamata HTTP che, per giunta, tiene il lock del gestore.
    """
    import subprocess

    def runner(argv, env):
        try:
            return subprocess.run(argv, env=env, capture_output=True, text=True,
                                  timeout=STATUS_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    return claude_accounts.account_status(slug, runner=runner)


def default_preparer(slug, email=None):
    """Directory pronta per il login, con la lettura di stato limitata.

    Senza passare il lettore limitato, `ensure_login_dir` usa il timeout di
    modulo da 10 secondi: sul percorso più frequente (la directory esiste già,
    cioè si RIPRENDE un login) l'avvio sarebbe potuto arrivare a ~15 secondi
    con il lock preso, invece dei ~5 documentati.
    """
    return claude_accounts.ensure_login_dir(slug, email=email,
                                            status_reader=bounded_status_reader)


def _default_rollback(slug, directory):
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


class _Pending:
    """Un login in corso: il processo, il suo descrittore e cosa ha detto."""

    __slots__ = ("slug", "pid", "fd", "started_at", "state", "auth_url",
                 "message", "raw", "code_sent_at", "closed")

    def __init__(self, slug, pid, fd, started_at):
        self.slug = slug
        self.pid = pid
        self.fd = fd
        self.started_at = started_at
        self.state = "starting"
        self.auth_url = None
        self.message = None
        # Si accumula il GREZZO, non il ripulito: una sequenza di controllo
        # può essere spezzata a metà fra due letture del pty, e ripulire ogni
        # pezzo per conto suo lascia in mezzo i residui (vedi find_auth_url).
        self.raw = ""
        self.code_sent_at = None
        self.closed = False

    @property
    def code_sent(self):
        return self.code_sent_at is not None

    def append(self, chunk):
        self.raw = (self.raw + chunk)[-_MAX_BUFFER:]

    def text(self):
        """L'uscita leggibile accumulata finora."""
        return clean_output(self.raw)

    def payload(self, state=None, message=None):
        """La risposta per il browser: mai percorsi, mai token, mai output grezzo."""
        return {
            "slug": self.slug,
            "state": state or self.state,
            "auth_url": self.auth_url,
            "message": message if message is not None else self.message,
        }


class LoginManager:
    """I login in corso, uno per abbonamento.

    Tutte le operazioni pubbliche prendono lo stesso lock: la struttura dei
    login in corso è condivisa fra i thread con cui il server serve le
    richieste, e le operazioni che la toccano sono brevi per costruzione (le
    finestre di lettura in testa al modulo). Il lock serializza fra loro solo
    le operazioni di login, non il resto della board.
    """

    def __init__(self, spawner=None, terminator=None, status_reader=None,
                 preparer=None, rollback=None, writer=None, clock=None,
                 start_read_seconds=START_READ_SECONDS,
                 poll_read_seconds=POLL_READ_SECONDS,
                 code_read_seconds=CODE_READ_SECONDS,
                 url_wait_seconds=URL_WAIT_SECONDS,
                 code_wait_seconds=CODE_WAIT_SECONDS,
                 max_age_seconds=MAX_AGE_SECONDS,
                 janitor_interval=JANITOR_INTERVAL_SECONDS,
                 start_janitor=True):
        self._spawner = spawner or _spawn_login_pty
        self._terminator = terminator or _terminate_process
        self._status_reader = status_reader or bounded_status_reader
        self._preparer = preparer or default_preparer
        self._rollback = rollback or _default_rollback
        self._writer = writer or _write_all
        self._clock = clock or time.monotonic
        self._start_read = start_read_seconds
        self._poll_read = poll_read_seconds
        self._code_read = code_read_seconds
        self._url_wait = url_wait_seconds
        self._code_wait = code_wait_seconds
        self._max_age = max_age_seconds
        self._janitor_interval = janitor_interval
        self._start_janitor = start_janitor
        self._janitor = None
        self._sessions = {}
        self._lock = threading.RLock()

    # --- operazioni pubbliche -------------------------------------------

    def start(self, slug, email=None):
        """Prepara la directory (se non c'è), lancia il comando, cerca il link.

        Un login già in corso sullo stesso abbonamento viene chiuso e
        sostituito: un tentativo abbandonato non deve impedire di ritentare.
        """
        with self._lock:
            self.sweep_expired()
            existing = self._sessions.pop(slug, None)
            if existing is not None:
                self._close(existing)
            directory, created_here = self._preparer(slug, email)
            try:
                pid, fd = self._spawner(slug, directory, email)
            except ValueError:
                if created_here:
                    self._rollback(slug, directory)
                raise
            except OSError as error:
                if created_here:
                    self._rollback(slug, directory)
                raise ValueError("impossibile avviare il login: %s" % error) from error
            entry = _Pending(slug, pid, fd, self._clock())
            self._sessions[slug] = entry
            self._pump(entry, self._start_read, stop_on_url=True)
            self._ensure_janitor()
            # Nei log solo slug e stato: mai il codice, mai il link (che porta
            # con sé il client_id ed è di fatto una credenziale di passaggio).
            _LOGGER.info("login avviato per l'abbonamento %s, stato %s",
                         slug, entry.state)
            return entry.payload()

    def poll(self, slug):
        """Stato corrente, leggendo quel poco che nel frattempo è arrivato."""
        with self._lock:
            entry = self._require(slug)
            expired = self._expire_if_due(entry)
            if expired is not None:
                return expired
            self._pump(entry, self._poll_read, stop_on_url=True, stop_when_idle=True)
            if entry.code_sent:
                return self._decide(entry)
            return entry.payload()

    def submit_code(self, slug, code):
        """Scrive il codice sul descrittore e riferisce l'esito.

        La validazione avviene PRIMA di scrivere: un valore rifiutato non
        arriva mai al processo.
        """
        cleaned = validate_code(code)
        with self._lock:
            entry = self._require(slug)
            expired = self._expire_if_due(entry)
            if expired is not None:
                return expired
            try:
                self._writer(entry.fd, cleaned.encode("utf-8", "ignore") + b"\r")
            except WriteNotAccepted as error:
                # Buffer pieno: la sessione è sana, si riprova. Distruggerla
                # qui vorrebbe dire buttare via un login valido per un
                # ingorgo di un decimo di secondo.
                raise ValueError("il login non ha accettato il codice adesso, "
                                 "riprova fra un istante") from error
            except OSError as error:
                self._sessions.pop(slug, None)
                self._close(entry)
                raise ValueError("il login non è più in ascolto: %s" % error) from error
            del cleaned
            entry.code_sent_at = self._clock()
            entry.state = "checking"
            entry.message = None
            self._pump(entry, self._code_read)
            return self._decide(entry)

    def cancel(self, slug):
        """Chiude il processo del login e ne rimuove la traccia."""
        with self._lock:
            entry = self._require(slug)
            self._sessions.pop(slug, None)
            self._close(entry)
            return entry.payload(state="cancelled",
                                 message="Login annullato.")

    def active_slugs(self):
        with self._lock:
            return sorted(self._sessions)

    def sweep_expired(self):
        """Chiude i login abbandonati. Ritorna gli slug chiusi."""
        with self._lock:
            closed = []
            for slug, entry in list(self._sessions.items()):
                if self._is_expired(entry):
                    self._sessions.pop(slug, None)
                    _LOGGER.info("login abbandonato per l'abbonamento %s: chiuso "
                                 "dallo spazzino", slug)
                    self._close(entry)
                    closed.append(slug)
            return sorted(closed)

    # --- interni ---------------------------------------------------------

    def _require(self, slug):
        entry = self._sessions.get(slug)
        if entry is None:
            raise NoPendingLogin("nessun login in corso per questo abbonamento")
        return entry

    def _is_expired(self, entry):
        age = self._clock() - entry.started_at
        if age > self._max_age:
            return True
        return entry.auth_url is None and age > self._url_wait

    def _expire_if_due(self, entry):
        if not self._is_expired(entry):
            return None
        self._sessions.pop(entry.slug, None)
        self._close(entry)
        return entry.payload(
            state="expired",
            message="Il login è scaduto: riavvialo per ottenere un link nuovo.")

    def _decide(self, entry):
        """Esito dopo l'invio del codice. Il successo lo dice lo STATO."""
        status = self._status_reader(entry.slug) or {}
        if status.get("logged_in"):
            self._sessions.pop(entry.slug, None)
            self._close(entry)
            return entry.payload(state="ok", message="Abbonamento collegato.")
        if _INVALID_CODE_MARKER in entry.text().lower():
            # Il marcatore resta a schermo anche dopo: si consuma, così un
            # tentativo nuovo non eredita l'errore di quello vecchio. Anche
            # l'orologio della verifica riparte: il prossimo codice ha diritto
            # alla sua finestra intera.
            entry.raw = ""
            entry.code_sent_at = None
            entry.state = "waiting_code"
            entry.message = ("Codice non valido: copialo per intero dalla pagina "
                             "di autorizzazione e riprova.")
            return entry.payload(state="invalid_code")
        if self._clock() - entry.code_sent_at > self._code_wait:
            self._sessions.pop(entry.slug, None)
            self._close(entry)
            return entry.payload(
                state="failed",
                message=("Il codice non è stato accettato: riavvia il login, "
                         "oppure completalo dal terminale."))
        entry.state = "checking"
        if status.get("error"):
            entry.message = ("Stato dell'abbonamento non leggibile (%s): "
                             "verifica in corso." % status["error"])
        else:
            entry.message = "Codice inviato, verifica in corso."
        return entry.payload()

    def _pump(self, entry, seconds, stop_on_url=False, stop_when_idle=False):
        """Legge quel che c'è sul descrittore per al massimo `seconds`.

        Non aspetta MAI la fine del flusso: è la garanzia che nessuna
        richiesta HTTP resti appesa. Lo stato letto resta nel `_Pending`, così
        la chiamata successiva riprende da dove questa si è fermata.
        """
        if entry.closed or entry.fd is None or entry.fd < 0:
            return
        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _w, _x = select.select([entry.fd], [], [], min(remaining, 0.2))
            except (OSError, ValueError):
                entry.closed = True
                break
            if not ready:
                # Niente da leggere adesso: chi legge "quel che c'è" (il poll)
                # torna subito invece di consumare tutta la finestra, così una
                # richiesta di solo controllo costa un quinto di secondo.
                if stop_when_idle:
                    break
                continue
            try:
                chunk = os.read(entry.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                # EIO: il pty si è chiuso perché il processo è finito.
                entry.closed = True
                break
            if not chunk:
                entry.closed = True
                break
            entry.append(chunk.decode("utf-8", "replace"))
            if entry.auth_url is None:
                url = find_auth_url(entry.raw)
                if url:
                    entry.auth_url = url
                    if not entry.code_sent:
                        entry.state = "waiting_code"
                    if stop_on_url:
                        break

    def _close(self, entry):
        """Chiude processo e descrittore. Idempotente, e per una buona ragione:
        pid e numero di descrittore vengono RICICLATI dal sistema, quindi una
        seconda chiusura manderebbe un segnale (o una close) a qualcosa che nel
        frattempo è di qualcun altro. Per questo si azzerano entrambi."""
        pid, fd = entry.pid, entry.fd
        entry.pid = None
        entry.fd = -1
        entry.closed = True
        if pid:
            try:
                self._terminator(pid)
            except OSError:
                pass
        if fd is not None and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        _LOGGER.info("login chiuso per l'abbonamento %s", entry.slug)

    def _ensure_janitor(self):
        """Un solo thread di fondo, vivo solo finché c'è un login in corso.

        Serve perché un utente che chiude la scheda non fa più nessuna
        chiamata: senza spazzino, il suo processo di login resterebbe in
        attesa di stdin per sempre.
        """
        if not self._start_janitor:
            return
        if self._janitor is not None and self._janitor.is_alive():
            return
        thread = threading.Thread(target=self._janitor_loop,
                                  name="login-pty-janitor", daemon=True)
        self._janitor = thread
        thread.start()

    def _janitor_loop(self):  # pragma: no cover - thread di fondo
        while True:
            time.sleep(self._janitor_interval)
            with self._lock:
                self.sweep_expired()
                if not self._sessions:
                    self._janitor = None
                    return

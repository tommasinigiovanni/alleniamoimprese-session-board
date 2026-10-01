"""Shared password + TOTP authentication for the full and portable boards.

Only opaque challenge IDs enter Flask's signed (readable) session cookie.
Pending enrollment seeds stay in bounded process memory. Deploy one worker;
restarting it cancels pending logins but never resets the persistent MFA limiter.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
import io
import math
import secrets
import threading
import time

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session
import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage

from .mfa_store import MFAStore, MFAStoreError, RateLimited


@dataclass
class Challenge:
    username: str
    epoch: str
    created: float
    generation: str | None
    enrollment_id: str | None = None
    secret: str | None = None
    recovery_pending: bool = False


def _equal(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeError:
        return False


class AuthFlow:
    challenge_seconds = 300
    max_challenges = 1024
    max_password_clients = 2048

    def __init__(self, app, *, username, password_ok, credential_epoch, origin_allowed, state_dir):
        if not isinstance(username, str) or not username or len(username) > 256:
            raise ValueError("A nonempty board username is required")
        self.app = app
        self.username = username
        self.password_ok = password_ok
        self.credential_epoch = credential_epoch
        self.origin_allowed = origin_allowed
        self.store = MFAStore(state_dir)
        self.clock = time.time
        self._challenges = OrderedDict()
        self._attempts = OrderedDict()
        self._lock = threading.RLock()
        self._password_lock = threading.Lock()

    def authenticated(self):
        if session.get("authenticated") is not True:
            return False
        if not _equal(session.get("auth_username"), self.username):
            return False
        if not _equal(session.get("auth_epoch"), self.credential_epoch()):
            return False
        try:
            generation = self.store.generation(self.username)
            return generation is not None and _equal(session.get("auth_mfa_generation"), generation)
        except MFAStoreError:
            return False

    def revoke_incomplete_cookie(self):
        # Run before any historical upstream guard can trust its boolean flag.
        if session.get("authenticated") and not self.authenticated():
            self._cancel()

    def _cancel(self):
        with self._lock:
            challenge_id = session.get("auth_challenge")
            if isinstance(challenge_id, str):
                self._challenges.pop(challenge_id, None)
        session.clear()
        session["csrf"] = secrets.token_urlsafe(32)

    def _challenge(self):
        now = self.clock()
        with self._lock:
            expired = [key for key, value in self._challenges.items()
                       if now - value.created >= self.challenge_seconds or now < value.created]
            for key in expired:
                self._challenges.pop(key, None)
            key = session.get("auth_challenge")
            pending = self._challenges.get(key) if isinstance(key, str) else None
            if pending is None:
                return None
            if not _equal(pending.epoch, self.credential_epoch()) or pending.username != self.username:
                self._cancel()
                return None
            generation = self.store.generation(self.username)
            if generation != pending.generation:
                self._cancel()
                return None
            return pending

    def _csrf_valid(self):
        if not self.origin_allowed():
            return False
        supplied = request.headers.get("X-CSRF-Token")
        if supplied is None:
            if request.is_json:
                payload = request.get_json(silent=True)
                supplied = payload.get("csrf") if isinstance(payload, dict) else None
            else:
                supplied = request.form.get("csrf")
        return bool(supplied) and _equal(supplied, session.get("csrf"))

    def endpoint(self, handler):
        @wraps(handler)
        def protected(*args, **kwargs):
            if request.method == "POST":
                if request.content_length is not None and request.content_length > 16384:
                    return self._error("Richiesta troppo grande.", 400)
                if not self._csrf_valid():
                    return self._error("Richiesta non valida. Ricarica la pagina e riprova.", 403)
            try:
                # Serialise challenge transitions as well as the atomic store
                # operations: racing confirmations must show recovery only once.
                with self._lock:
                    return handler(*args, **kwargs)
            except RateLimited as exc:
                return self._error("Troppi tentativi. Attendi prima di riprovare.", 429,
                                   retry_after=exc.retry_after)
            except MFAStoreError:
                self._cancel()
                return self._error("Accesso temporaneamente non disponibile. Contatta l'amministratore.", 503)
        return protected

    def _error(self, message, status, *, retry_after=None, template="error.html", **context):
        if request.is_json or request.path == "/api/login":
            response = jsonify(error=message)
        else:
            response = self.app.make_response(render_template("auth/" + template, error=message, **context))
        response.status_code = status
        if retry_after is not None:
            response.headers["Retry-After"] = str(max(1, int(retry_after)))
        return response

    def login_page(self):
        if request.method == "GET":
            if self.authenticated():
                return redirect("/")
            pending = self._challenge()
            if pending is not None:
                if pending.recovery_pending:
                    return redirect("/auth/recovery")
                return redirect("/auth/setup" if pending.generation is None else "/auth/verify")
            if session.get("auth_challenge"):
                self._cancel()
            else:
                # Background browser requests and another login tab must not
                # invalidate the CSRF token already present in the visible form.
                session.setdefault("csrf", secrets.token_urlsafe(32))
            return render_template("auth/login.html", error=None)
        return self._credentials(json_mode=False)

    def login_api(self):
        return self._credentials(json_mode=True)

    def _credentials(self, *, json_mode):
        payload = request.get_json(silent=True) if json_mode else request.form
        if payload is None or not hasattr(payload, "get"):
            return self._error("Credenziali non valide.", 400)
        username, password = payload.get("username"), payload.get("password")
        if (not isinstance(username, str) or not username or len(username) > 256
                or not isinstance(password, str) or not password or len(password) > 4096):
            return self._error("Credenziali non valide.", 400)
        try:
            username.encode("utf-8")
            password.encode("utf-8")
        except UnicodeError:
            return self._error("Credenziali non valide.", 400)
        epoch = self.credential_epoch()
        ip = request.remote_addr or "unknown"
        with self._password_lock:
            now = self.clock()
            for key, (start, _) in list(self._attempts.items()):
                if now - start >= 300:
                    del self._attempts[key]
            start, count = self._attempts.get(ip, (now, 0))
            if count >= 5:
                return self._error("Troppi tentativi. Attendi prima di riprovare.", 429,
                                   retry_after=math.ceil(300 - (now - start)))
            self._attempts[ip] = start, count + 1
            self._attempts.move_to_end(ip)
            if len(self._attempts) > self.max_password_clients:
                self._attempts.popitem(last=False)
            # Check the password even for an unknown username.
            valid_password = self.password_ok(password)
            if (not valid_password or not _equal(username, self.username)
                    or not _equal(epoch, self.credential_epoch())):
                return self._error("Nome utente o password non validi.", 401, template="login.html")
            self._attempts.pop(ip, None)
        generation = self.store.generation(self.username)
        self._challenge()  # Prune expired pending logins before checking capacity.
        self._cancel()
        if len(self._challenges) >= self.max_challenges:
            return self._error("Accesso occupato. Riprova tra qualche minuto.", 503)
        challenge_id = secrets.token_urlsafe(32)
        self._challenges[challenge_id] = Challenge(self.username, epoch, now, generation)
        session["auth_challenge"] = challenge_id
        target = "/auth/verify" if generation is not None else "/auth/setup"
        return (jsonify(next=target), 202) if json_mode else redirect(target)

    def verify(self):
        pending = self._challenge()
        if pending is None:
            return redirect("/login")
        if pending.recovery_pending:
            return redirect("/auth/recovery")
        if pending.generation is None:
            return redirect("/auth/setup")
        if request.method == "GET":
            return render_template("auth/verify.html", error=None)
        code = request.form.get("code", "").strip()
        if len(code) > 256 or not self.store.verify(self.username, code):
            return self._error("Codice non valido o già utilizzato. Riprova.", 401, template="verify.html")
        return self._grant(pending)

    def setup(self):
        pending = self._challenge()
        if pending is None:
            return redirect("/login")
        if pending.recovery_pending:
            return redirect("/auth/recovery")
        if pending.generation is not None:
            return redirect("/auth/verify")
        if request.method == "POST" and pending.enrollment_id is None:
            bootstrap = request.form.get("bootstrap", "").strip()
            enrollment = self.store.begin_enrollment(self.username, bootstrap)
            if enrollment is None:
                return self._error("Codice di attivazione non valido o scaduto.", 401,
                                   template="setup.html", secret=None)
            pending.enrollment_id, pending.secret = enrollment
        return render_template("auth/setup.html", secret=pending.secret, error=None)

    def qr(self):
        pending = self._challenge()
        if pending is None or pending.secret is None or pending.recovery_pending:
            return self._error("Configurazione scaduta. Ripeti l'accesso.", 404)
        uri = pyotp.TOTP(pending.secret).provisioning_uri(name=self.username, issuer_name="Session Board")
        picture = qrcode.make(uri, image_factory=SvgPathImage)
        output = io.BytesIO()
        picture.save(output)
        return Response(output.getvalue(), mimetype="image/svg+xml")

    def confirm(self):
        pending = self._challenge()
        if pending is None:
            return redirect("/login")
        if pending.recovery_pending:
            return redirect("/auth/recovery")
        if pending.enrollment_id is None or pending.secret is None:
            return redirect("/auth/setup")
        code = request.form.get("code", "").strip()
        codes = self.store.confirm_enrollment(self.username, pending.enrollment_id, code)
        if codes is None:
            return self._error("Codice non valido o già utilizzato. Riprova.", 401,
                               template="setup.html", secret=pending.secret)
        pending.generation = self.store.generation(self.username)
        pending.recovery_pending = True
        pending.secret = None
        pending.enrollment_id = None
        # Plain recovery values live only in this one response, never the
        # challenge or cookie. Refreshing does not retrieve them again.
        return render_template("auth/recovery.html", codes=codes, error=None)

    def recovery(self):
        pending = self._challenge()
        if pending is None:
            return redirect("/login")
        if not pending.recovery_pending:
            return redirect("/auth/setup" if pending.generation is None else "/auth/verify")
        return render_template("auth/recovery.html", codes=None, error=None)

    def acknowledge(self):
        pending = self._challenge()
        if pending is None:
            return redirect("/login")
        if not pending.recovery_pending:
            return self._error("Completa prima la configurazione del secondo fattore.", 400)
        if request.form.get("saved") != "yes":
            return self._error("Conferma di aver salvato i codici di recupero.", 400,
                               template="recovery.html", codes=None)
        return self._grant(pending)

    def _grant(self, pending):
        # Recheck rotations after a potentially slow factor verification.
        if self._challenge() is not pending or pending.generation is None:
            self._cancel()
            return redirect("/login")
        self._cancel()
        session.update(authenticated=True, auth_username=self.username,
                       auth_epoch=pending.epoch, auth_mfa_generation=pending.generation)
        session.permanent = True
        return redirect("/")

    def cancel(self):
        self._cancel()
        return redirect("/login")


def install_auth(app, *, username, password_ok, credential_epoch, origin_allowed,
                 state_dir, login_endpoint=None):
    """Install identical HTML/JSON credential routes and fail-closed 2FA guards."""
    if "board_auth" in app.extensions:
        raise ValueError("Board authentication is already installed")
    flow = AuthFlow(app, username=username, password_ok=password_ok,
                    credential_epoch=credential_epoch, origin_allowed=origin_allowed,
                    state_dir=state_dir)
    blueprint = Blueprint("board_auth", __name__, template_folder="templates",
                          static_folder="static", static_url_path="/auth/assets")
    routes = (
        ("/auth/verify", "verify", flow.verify, ["GET", "POST"]),
        ("/auth/setup", "setup", flow.setup, ["GET", "POST"]),
        ("/auth/setup/qr", "qr", flow.qr, ["GET"]),
        ("/auth/setup/confirm", "confirm", flow.confirm, ["POST"]),
        ("/auth/recovery", "recovery", flow.recovery, ["GET"]),
        ("/auth/recovery/ack", "acknowledge", flow.acknowledge, ["POST"]),
        ("/auth/cancel", "cancel", flow.cancel, ["POST"]),
    )
    for path, endpoint, handler, methods in routes:
        blueprint.add_url_rule(path, endpoint, flow.endpoint(handler), methods=methods)
    login_handler = flow.endpoint(flow.login_page)
    login_rules = [rule for rule in app.url_map.iter_rules() if rule.rule == "/login"]
    if login_rules:
        for rule in login_rules:
            app.view_functions[rule.endpoint] = login_handler
        endpoint = login_endpoint or login_rules[0].endpoint
        # The historical full login page was GET-only.
        if not any("POST" in rule.methods for rule in login_rules):
            app.add_url_rule("/login", endpoint, login_handler, methods=["POST"])
    else:
        blueprint.add_url_rule("/login", "login", login_handler, methods=["GET", "POST"])
        if login_endpoint and login_endpoint in app.view_functions:
            app.view_functions[login_endpoint] = login_handler
    api_handler = flow.endpoint(flow.login_api)
    if "api_login" in app.view_functions:
        app.view_functions["api_login"] = api_handler
    else:
        blueprint.add_url_rule("/api/login", "login_api", api_handler, methods=["POST"])

    @blueprint.after_app_request
    def private_auth_response(response):
        if request.path in {"/login", "/api/login"} or request.path.startswith("/auth/"):
            response.headers.update({
                "Cache-Control": "no-store", "Pragma": "no-cache",
                "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                           "img-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                                           "base-uri 'none'; form-action 'self'",
            })
        return response

    app.register_blueprint(blueprint)
    app.before_request_funcs.setdefault(None, []).insert(0, flow.revoke_incomplete_cookie)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_REFRESH_EACH_REQUEST=False,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    if not app.config.get("SESSION_COOKIE_SAMESITE"):
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.extensions["board_auth"] = flow
    return flow

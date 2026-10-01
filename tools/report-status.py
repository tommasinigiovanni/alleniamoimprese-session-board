#!/usr/bin/env python3
"""Report an explicit session state; never install hooks or send terminal input."""

import argparse
import ipaddress
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def event_url(value):
    try:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError
        # Accessing .port also validates malformed or out-of-range port values.
        parsed.port
        if parsed.scheme == "http":
            if parsed.hostname != "localhost" and not ipaddress.ip_address(parsed.hostname).is_loopback:
                raise ValueError
        return urlunsplit((parsed.scheme, parsed.netloc, "/api/events", "", ""))
    except ValueError:
        raise ValueError("BOARD_URL non valido: usare HTTPS o HTTP su loopback, senza credenziali o percorsi.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="nome della sessione tmux")
    parser.add_argument("--status", required=True, choices=("working", "waiting", "blocked", "idle", "done"))
    parser.add_argument("--detail", default="", help="dettaglio opzionale, massimo 1000 caratteri")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.session):
        parser.exit(1, "Nome session non valido.\n")
    if len(args.detail) > 1000:
        parser.exit(1, "Dettaglio troppo lungo.\n")
    token = os.environ.get("BOARD_INGEST_TOKEN", "")
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        parser.exit(1, "BOARD_INGEST_TOKEN mancante o non valido.\n")
    try:
        destination = event_url(os.environ.get("BOARD_URL", "http://127.0.0.1:8099"))
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    request = Request(
        destination,
        data=json.dumps({"session": args.session, "status": args.status, "detail": args.detail}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    # Ignore environment proxies so a local status token never reaches a proxy.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            if not 200 <= response.status < 300:
                parser.exit(1, f"Invio non riuscito (HTTP {response.status}).\n")
    except HTTPError as exc:
        parser.exit(1, f"Invio non riuscito (HTTP {exc.code}); redirect non consentiti.\n")
    except (URLError, OSError, ValueError):
        parser.exit(1, "Invio non riuscito: connessione, certificato o timeout.\n")
    print("Stato inviato.")


if __name__ == "__main__":
    main()

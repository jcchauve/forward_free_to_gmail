#!/usr/bin/env python3
"""
Forward emails from Free.fr to Gmail via IMAP IDLE (push) — compatible Windows.

1. Lance setup_credentials.py une première fois pour chiffrer tes mots de passe.
2. Lance ce script :
       python forward_free_to_gmail.py
   Ou double-clique sur start.bat

Le script maintient une connexion IMAP persistante et utilise IDLE pour être
notifié instantanément à chaque nouvel email — zéro polling, zéro délai.
La connexion est renouvelée toutes les 28 minutes (limite RFC 2177).
En cas d'erreur réseau, reconnexion automatique avec backoff exponentiel.
"""

import imaplib
import smtplib
import email
import logging
import socket
import signal
import sys
import json
import time
import threading
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("Module manquant : pip install cryptography")
    sys.exit(1)

# ─────────────────────────────────────────────
# CHARGEMENT DES CREDENTIALS CHIFFRÉS
# ─────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
KEY_FILE   = BASE_DIR / ".secret.key"
CREDS_FILE = BASE_DIR / "credentials.enc"


def load_credentials() -> dict:
    if not KEY_FILE.exists():
        print(f"Cle introuvable : {KEY_FILE}")
        print("   Lancez d'abord : python setup_credentials.py")
        sys.exit(1)
    if not CREDS_FILE.exists():
        print(f"Credentials introuvables : {CREDS_FILE}")
        print("   Lancez d'abord : python setup_credentials.py")
        sys.exit(1)
    try:
        fernet    = Fernet(KEY_FILE.read_bytes())
        decrypted = fernet.decrypt(CREDS_FILE.read_bytes())
        return json.loads(decrypted)
    except InvalidToken:
        print("Dechiffrement impossible : cle incorrecte ou fichier corrompu.")
        sys.exit(1)


_creds         = load_credentials()
FREE_EMAIL     = _creds["FREE_EMAIL"]
FREE_PASSWORD  = _creds["FREE_PASSWORD"]
GMAIL_EMAIL    = _creds["GMAIL_EMAIL"]
GMAIL_PASSWORD = _creds["GMAIL_PASSWORD"]

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
# Dossiers IMAP a surveiller. Ajoute ou retire des entrees selon tes besoins.
# Le nom exact doit correspondre au nom du dossier sur le serveur Free.fr.
IMAP_FOLDERS = [
    "INBOX",
    "Flux d'activite",   # verifier le nom exact via un client mail
]


# IMAP IDLE expire après 30 min côté serveur (RFC 2177).
# On renouvelle la connexion toutes les 28 minutes par sécurité.
IDLE_TIMEOUT = 28 * 60

# Timeout de lecture bloquante sur le socket (en secondes).
# Le thread IDLE se réveille toutes les 60 s pour vérifier l'arrêt demandé.
SOCKET_READ_TIMEOUT = 60

# Backoff exponentiel en cas d'erreur réseau : 5s, 10s, 20s … jusqu'à 5 min max.
RECONNECT_DELAY_INIT = 5
RECONNECT_DELAY_MAX  = 300

# ─────────────────────────────────────────────
# SERVEURS
# ─────────────────────────────────────────────
FREE_IMAP_HOST  = "imap.free.fr"
FREE_IMAP_PORT  = 993
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "forward.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ARRET PROPRE  (Ctrl+C sur Windows et Linux)
# SIGTERM n'existe pas sur Windows — on gère uniquement SIGINT (Ctrl+C)
# ─────────────────────────────────────────────
_running = True
_stop_event = threading.Event()


def _handle_signal(sig, frame):
    global _running
    log.info("Arret demande (signal %s)...", sig)
    _running = False
    _stop_event.set()


signal.signal(signal.SIGINT, _handle_signal)
if hasattr(signal, "SIGTERM"):          # absent sur Windows
    signal.signal(signal.SIGTERM, _handle_signal)


# ─────────────────────────────────────────────
# CONSTRUCTION DU MESSAGE FORWARDÉ
# ─────────────────────────────────────────────
def build_forward_message(original: email.message.Message) -> MIMEMultipart:
    fwd = MIMEMultipart("mixed")
    fwd["From"]    = GMAIL_EMAIL
    fwd["To"]      = GMAIL_EMAIL
    fwd["Subject"] = "Fwd: " + original.get("Subject", "(sans objet)")

    header_text = (
        f"\n---------- Message transfere ----------\n"
        f"De      : {original.get('From', '')}\n"
        f"A       : {original.get('To', '')}\n"
        f"Date    : {original.get('Date', '')}\n"
        f"Objet   : {original.get('Subject', '')}\n"
        f"---------------------------------------\n\n"
    )

    if original.is_multipart():
        for part in original.walk():
            content_type = part.get_content_type()
            disposition  = str(part.get("Content-Disposition", ""))

            if "attachment" in disposition:
                att = MIMEBase("application", "octet-stream")
                att.set_payload(part.get_payload(decode=True))
                encoders.encode_base64(att)
                att.add_header("Content-Disposition", "attachment",
                               filename=part.get_filename() or "fichier_joint")
                fwd.attach(att)
            elif content_type == "text/plain":
                body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace")
                fwd.attach(MIMEText(header_text + body, "plain", "utf-8"))
            elif content_type == "text/html":
                body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace")
                fwd.attach(MIMEText(header_text.replace("\n", "<br>") + body, "html", "utf-8"))
    else:
        body = original.get_payload(decode=True).decode(
            original.get_content_charset() or "utf-8", errors="replace")
        fwd.attach(MIMEText(header_text + body, "plain", "utf-8"))

    return fwd


# ─────────────────────────────────────────────
# ENVOI GMAIL
# ─────────────────────────────────────────────
def send_via_gmail(original: email.message.Message):
    smtp = smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT)
    try:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(GMAIL_EMAIL, GMAIL_PASSWORD)
        fwd = build_forward_message(original)
        smtp.sendmail(GMAIL_EMAIL, GMAIL_EMAIL, fwd.as_string())
    finally:
        smtp.quit()


# ─────────────────────────────────────────────
# FETCH & FORWARD
# ─────────────────────────────────────────────
def fetch_and_forward(imap: imaplib.IMAP4_SSL) -> int:
    _, uids_raw = imap.uid("search", None, "UNSEEN")
    uids = uids_raw[0].split()
    if not uids:
        return 0

    log.info("%d nouveau(x) message(s) a transferer.", len(uids))
    count = 0
    for uid in uids:
        _, msg_data = imap.uid("fetch", uid, "(RFC822)")
        original = email.message_from_bytes(msg_data[0][1])
        log.info("  -> %s", original.get("Subject", "(sans objet)"))
        try:
            send_via_gmail(original)
            imap.uid("store", uid, "+FLAGS", "\\Seen")
            count += 1
            log.info("     Transfere et marque comme lu.")
        except Exception as e:
            log.error("     Erreur : %s", e)

    return count


# ─────────────────────────────────────────────
# ENCODAGE UTF-7 MODIFIE (RFC 3501)
# Les noms de dossiers IMAP avec accents doivent etre encodes en UTF-7 modifie.
# Python ne fournit pas ce codec nativement — on l'implemente ici.
# ─────────────────────────────────────────────
def encode_imap_utf7(folder: str) -> str:
    # Les caracteres ASCII imprimables (sauf &) passent tels quels
    # Les autres sont encodes en base64 UTF-16BE entre & et -
    import base64
    res = []
    buf = []

    def flush():
        if buf:
            encoded = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii").rstrip("=")
            res.append(f"&{encoded}-")
            buf.clear()

    for ch in folder:
        if ch == "&":
            flush()
            res.append("&-")
        elif 0x20 <= ord(ch) <= 0x7e:
            flush()
            res.append(ch)
        else:
            buf.append(ch)

    flush()
    return "".join(res)


# ─────────────────────────────────────────────
# CONNEXION IMAP (une connexion par dossier)
# ─────────────────────────────────────────────
def connect_imap(folder: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(FREE_IMAP_HOST, FREE_IMAP_PORT)
    imap.login(FREE_EMAIL, FREE_PASSWORD)
    encoded_folder = encode_imap_utf7(folder)
    # Guillemets obligatoires si le nom contient des espaces ou caracteres speciaux
    status, detail = imap.select(f'"{encoded_folder}"')
    if status != "OK":
        raise RuntimeError(f"Dossier introuvable : {folder!r} (encode: {encoded_folder!r}) ({detail})")
    log.info("[%s] Connecte.", folder)
    return imap


# ─────────────────────────────────────────────
# BOUCLE IMAP IDLE pour un dossier  — compatible Windows
#
# Sur Windows, select.select() ne fonctionne pas sur les sockets SSL.
# On utilise socket.settimeout() sur le socket sous-jacent :
# readline() bloque jusqu'a reception de donnees ou jusqu'au timeout.
# ─────────────────────────────────────────────
def idle_loop(imap: imaplib.IMAP4_SSL, folder: str):
    # Traiter les eventuels messages deja non lus a la connexion
    fetch_and_forward(imap)

    raw_sock = imap.socket()
    raw_sock.settimeout(SOCKET_READ_TIMEOUT)

    deadline = time.monotonic() + IDLE_TIMEOUT

    while _running:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info("[%s] Renouvellement IDLE (timeout %d min).", folder, IDLE_TIMEOUT // 60)
            return  # le caller se reconnecte

        tag = imap._new_tag().decode()
        imap.send(f"{tag} IDLE\r\n".encode())
        imap.readline()  # "+ idling"

        notified = False
        idle_start = time.monotonic()

        while _running and (time.monotonic() - idle_start) < min(remaining, IDLE_TIMEOUT):
            try:
                line = imap.readline()
                if b"EXISTS" in line or b"RECENT" in line:
                    notified = True
                    break
                if tag.encode() in line:
                    break
            except (socket.timeout, TimeoutError):
                break

        try:
            imap.send(b"DONE\r\n")
            while True:
                line = imap.readline()
                if tag.encode() in line:
                    break
        except Exception:
            pass

        if notified:
            log.info("[%s] Notification -- verification des nouveaux messages...", folder)
            fetch_and_forward(imap)
        elif not _running:
            break
        else:
            try:
                imap.noop()
            except Exception:
                raise  # connexion perdue, le caller reconnecte

    log.info("[%s] Boucle IDLE terminee.", folder)


# ─────────────────────────────────────────────
# WORKER PAR DOSSIER  (tourne dans son propre thread)
# ─────────────────────────────────────────────
def folder_worker(folder: str):
    delay = RECONNECT_DELAY_INIT
    while _running:
        try:
            imap = connect_imap(folder)
            delay = RECONNECT_DELAY_INIT
            idle_loop(imap, folder)
            try:
                imap.logout()
            except Exception:
                pass
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("[%s] Erreur : %s", folder, e)
            if _running:
                log.info("[%s] Reconnexion dans %ds...", folder, delay)
                _stop_event.wait(timeout=delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    log.info("[%s] Worker arrete.", folder)


# ─────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────
def main():
    log.info("Demarrage du service Free -> Gmail (IMAP IDLE, %d dossier(s))", len(IMAP_FOLDERS))
    for f in IMAP_FOLDERS:
        log.info("  - %s", f)

    # Un thread daemon par dossier — ils s'arrêtent quand le thread principal quitte
    threads = []
    for folder in IMAP_FOLDERS:
        t = threading.Thread(target=folder_worker, args=(folder,), name=f"idle-{folder}", daemon=True)
        t.start()
        threads.append(t)

    # Attendre Ctrl+C ou SIGTERM
    try:
        while _running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    log.info("Arret demande — attente de la fin des workers...")
    _stop_event.set()
    for t in threads:
        t.join(timeout=10)
    log.info("Service arrete.")


if __name__ == "__main__":
    main()
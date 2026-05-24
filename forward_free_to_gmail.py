#!/usr/bin/env python3
"""
Forward emails from Free.fr to Gmail via IMAP IDLE (push).

1. Lance setup_credentials.py une première fois pour chiffrer tes mots de passe.
2. Lance ce script en continu :
       python3 forward_free_to_gmail.py

Le script maintient une connexion IMAP persistante et utilise IDLE pour être
notifié instantanément à chaque nouvel email — zéro polling, zéro délai.
La connexion est renouvelée toutes les 28 minutes (limite RFC 2177).
En cas d'erreur réseau, reconnexion automatique avec backoff exponentiel.
"""

import imaplib
import smtplib
import email
import logging
import select
import signal
import sys
import json
import time
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("❌ Module manquant : pip install cryptography")
    sys.exit(1)

# ─────────────────────────────────────────────
# CHARGEMENT DES CREDENTIALS CHIFFRÉS
# ─────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
KEY_FILE   = BASE_DIR / ".secret.key"
CREDS_FILE = BASE_DIR / "credentials.enc"


def load_credentials() -> dict:
    if not KEY_FILE.exists():
        print(f"❌ Clé introuvable : {KEY_FILE}")
        print("   Lancez d'abord : python3 setup_credentials.py")
        sys.exit(1)
    if not CREDS_FILE.exists():
        print(f"❌ Credentials introuvables : {CREDS_FILE}")
        print("   Lancez d'abord : python3 setup_credentials.py")
        sys.exit(1)
    try:
        fernet    = Fernet(KEY_FILE.read_bytes())
        decrypted = fernet.decrypt(CREDS_FILE.read_bytes())
        return json.loads(decrypted)
    except InvalidToken:
        print("❌ Déchiffrement impossible : clé incorrecte ou fichier corrompu.")
        sys.exit(1)


_creds         = load_credentials()
FREE_EMAIL     = _creds["FREE_EMAIL"]
FREE_PASSWORD  = _creds["FREE_PASSWORD"]
GMAIL_EMAIL    = _creds["GMAIL_EMAIL"]
GMAIL_PASSWORD = _creds["GMAIL_PASSWORD"]

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
FREE_INBOX   = "INBOX"

# IMAP IDLE expire après 30 min côté serveur (RFC 2177 recommande 29 min max).
# On renouvelle la connexion toutes les 28 minutes par sécurité.
IDLE_TIMEOUT = 28 * 60

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
        logging.FileHandler(BASE_DIR / "forward.log"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ARRÊT PROPRE (Ctrl+C / SIGTERM)
# ─────────────────────────────────────────────
_running = True

def _handle_signal(sig, frame):
    global _running
    log.info("Signal %s reçu — arrêt en cours…", sig)
    _running = False

signal.signal(signal.SIGINT,  _handle_signal)
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
        f"\n---------- Message transféré ----------\n"
        f"De      : {original.get('From', '')}\n"
        f"À       : {original.get('To', '')}\n"
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
# FETCH & FORWARD DES MESSAGES NON LUS
# ─────────────────────────────────────────────
def fetch_and_forward(imap: imaplib.IMAP4_SSL) -> int:
    """Récupère tous les UNSEEN, les forward et les marque comme lus. Retourne le nombre traités."""
    _, uids_raw = imap.uid("search", None, "UNSEEN")
    uids = uids_raw[0].split()
    if not uids:
        return 0

    log.info("%d nouveau(x) message(s) à transférer.", len(uids))
    count = 0
    for uid in uids:
        _, msg_data = imap.uid("fetch", uid, "(RFC822)")
        original = email.message_from_bytes(msg_data[0][1])
        subject  = original.get("Subject", "(sans objet)")
        log.info("  → %s", subject)
        try:
            send_via_gmail(original)
            imap.uid("store", uid, "+FLAGS", "\\Seen")
            count += 1
            log.info("    ✓ Transféré et marqué comme lu.")
        except Exception as e:
            log.error("    ✗ Erreur : %s", e)

    return count


# ─────────────────────────────────────────────
# CONNEXION IMAP
# ─────────────────────────────────────────────
def connect_imap() -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(FREE_IMAP_HOST, FREE_IMAP_PORT)
    imap.login(FREE_EMAIL, FREE_PASSWORD)
    imap.select(FREE_INBOX)
    log.info("Connecté à %s (%s)", FREE_IMAP_HOST, FREE_INBOX)
    return imap


# ─────────────────────────────────────────────
# BOUCLE IMAP IDLE
# ─────────────────────────────────────────────
def idle_loop(imap: imaplib.IMAP4_SSL):
    """
    Entre en mode IDLE et attend une notification serveur.
    Traite les nouveaux messages dès leur arrivée.
    Renouvelle la session après IDLE_TIMEOUT secondes.
    Retourne quand _running devient False ou quand une reconnexion est nécessaire.
    """
    # Traiter d'abord les éventuels messages déjà non lus à la connexion
    fetch_and_forward(imap)

    deadline = time.monotonic() + IDLE_TIMEOUT

    while _running:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info("Renouvellement IDLE (timeout %d min).", IDLE_TIMEOUT // 60)
            return  # le caller reconnecte

        # Envoyer la commande IDLE
        tag = imap._new_tag().decode()
        imap.send(f"{tag} IDLE\r\n".encode())
        imap.readline()  # "+ idling"

        # Attendre une réponse serveur ou le timeout
        sock = imap.socket()
        timeout = min(remaining, 60)  # vérifie _running toutes les 60 s max
        ready, _, _ = select.select([sock], [], [], timeout)

        # Sortir d'IDLE proprement
        imap.send(b"DONE\r\n")

        if ready:
            # Lire la réponse IDLE (peut contenir "EXISTS", "RECENT", etc.)
            response = b""
            while True:
                chunk = imap.readline()
                response += chunk
                # La réponse se termine par la ligne "tag OK IDLE terminated"
                if tag.encode() in chunk:
                    break

            if b"EXISTS" in response or b"RECENT" in response:
                log.info("📬 Notification serveur — vérification des nouveaux messages…")
                fetch_and_forward(imap)
        else:
            # Timeout : juste un NOOP pour maintenir la connexion vivante
            imap.noop()

    log.info("Boucle IDLE terminée.")


# ─────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────
def main():
    log.info("🚀 Démarrage du service de forwarding Free → Gmail (IMAP IDLE)")
    delay = RECONNECT_DELAY_INIT

    while _running:
        try:
            imap = connect_imap()
            delay = RECONNECT_DELAY_INIT  # reset backoff après connexion réussie
            idle_loop(imap)
            try:
                imap.logout()
            except Exception:
                pass
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("Erreur connexion/IDLE : %s", e)
            if _running:
                log.info("Reconnexion dans %ds…", delay)
                time.sleep(delay)
                delay = min(delay * 2, RECONNECT_DELAY_MAX)

    log.info("Service arrêté. Bye.")


if __name__ == "__main__":
    main()
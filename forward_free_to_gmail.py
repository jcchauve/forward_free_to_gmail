#!/usr/bin/env python3
"""
Forward emails from Free.fr to Gmail via IMAP + SMTP.

1. Lance setup_credentials.py une première fois pour chiffrer tes mots de passe.
2. Lance ce script en continu :
       python3 forward_free_to_gmail.py
   Il poll les emails toutes les 10 minutes.
"""

import imaplib
import smtplib
import email
import logging
import os
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
FREE_INBOX             = "INBOX"
POLL_INTERVAL_SECONDS  = 10 * 60   # 10 minutes

# ─────────────────────────────────────────────
# SERVEURS — ne pas modifier sauf besoin
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
# FORWARD
# ─────────────────────────────────────────────
def build_forward_message(original: email.message.Message) -> MIMEMultipart:
    """Reconstruit un email à forwarder vers Gmail en conservant les pièces jointes."""
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
                attachment = MIMEBase("application", "octet-stream")
                attachment.set_payload(part.get_payload(decode=True))
                encoders.encode_base64(attachment)
                attachment.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=part.get_filename() or "fichier_joint",
                )
                fwd.attach(attachment)
            elif content_type == "text/plain":
                body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
                fwd.attach(MIMEText(header_text + body, "plain", "utf-8"))
            elif content_type == "text/html":
                body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
                html_header = header_text.replace("\n", "<br>")
                fwd.attach(MIMEText(html_header + body, "html", "utf-8"))
    else:
        body = original.get_payload(decode=True).decode(
            original.get_content_charset() or "utf-8", errors="replace"
        )
        fwd.attach(MIMEText(header_text + body, "plain", "utf-8"))

    return fwd


def forward_emails():
    # ── Connexion IMAP Free.fr ──
    log.info("Connexion IMAP à %s…", FREE_IMAP_HOST)
    try:
        imap = imaplib.IMAP4_SSL(FREE_IMAP_HOST, FREE_IMAP_PORT)
        imap.login(FREE_EMAIL, FREE_PASSWORD)
    except Exception as e:
        log.error("Impossible de se connecter à Free IMAP : %s", e)
        return

    imap.select(FREE_INBOX)

    _, uids_raw = imap.uid("search", None, "UNSEEN")
    uids = uids_raw[0].split()

    if not uids:
        log.info("Aucun nouveau message.")
        imap.logout()
        return

    log.info("%d nouveau(x) message(s) trouvé(s).", len(uids))

    # ── Connexion SMTP Gmail ──
    try:
        smtp = smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT)
        smtp.ehlo()
        smtp.starttls()
        smtp.login(GMAIL_EMAIL, GMAIL_PASSWORD)
    except Exception as e:
        log.error("Impossible de se connecter à Gmail SMTP : %s", e)
        imap.logout()
        return

    forwarded_count = 0
    for uid in uids:
        _, msg_data = imap.uid("fetch", uid, "(RFC822)")
        raw      = msg_data[0][1]
        original = email.message_from_bytes(raw)

        subject = original.get("Subject", "(sans objet)")
        log.info("  → Transfert de : %s", subject)

        try:
            fwd = build_forward_message(original)
            smtp.sendmail(GMAIL_EMAIL, GMAIL_EMAIL, fwd.as_string())
            # Marquer comme lu sur Free.fr pour ne pas reforwarder au prochain cycle
            imap.uid("store", uid, "+FLAGS", "\\Seen")
            forwarded_count += 1
            log.info("    ✓ Transféré et marqué comme lu.")
        except Exception as e:
            log.error("    ✗ Erreur lors du transfert : %s", e)

    smtp.quit()
    imap.logout()
    log.info("Terminé. %d message(s) transféré(s).", forwarded_count)


# ─────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Démarrage du service de forwarding (poll toutes les %d min).", POLL_INTERVAL_SECONDS // 60)
    while True:
        try:
            forward_emails()
        except Exception as e:
            log.error("Erreur inattendue : %s — on reprend au prochain cycle.", e)

        log.info("Prochain check dans %d minutes…", POLL_INTERVAL_SECONDS // 60)
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("Arrêt demandé (Ctrl+C). Bye.")
            break
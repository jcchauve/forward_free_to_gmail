#!/usr/bin/env python3
"""
Chiffrement des mots de passe pour forward_free_to_gmail.py
-----------------------------------------------------------
À lancer UNE SEULE FOIS pour stocker les credentials de façon sécurisée.

    python3 setup_credentials.py

Génère deux fichiers :
  - credentials.enc  : mots de passe chiffrés (AES-128 via Fernet)
  - .secret.key      : clé de déchiffrement (à ne jamais partager/commiter)

Prérequis :
    pip install cryptography
"""

import os
import json
import getpass
from pathlib import Path
from cryptography.fernet import Fernet

BASE_DIR    = Path(__file__).parent
KEY_FILE    = BASE_DIR / ".secret.key"
CREDS_FILE  = BASE_DIR / "credentials.enc"


def generate_key() -> bytes:
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)  # lecture uniquement par le propriétaire
    print(f"✓ Clé générée : {KEY_FILE}")
    return key


def load_or_create_key() -> bytes:
    if KEY_FILE.exists():
        confirm = input(f"Une clé existe déjà ({KEY_FILE}). La remplacer ? [o/N] ").strip().lower()
        if confirm != "o":
            print("→ Clé existante conservée.")
            return KEY_FILE.read_bytes()
    return generate_key()


def encrypt_credentials(key: bytes):
    print("\nEntrez vos identifiants (ils ne s'afficheront pas) :\n")

    free_email    = input("Adresse Free.fr     : ").strip()
    free_password = getpass.getpass("Mot de passe Free   : ")
    gmail_email   = input("Adresse Gmail       : ").strip()
    gmail_password = getpass.getpass("Mot de passe Gmail  : ")

    data = {
        "FREE_EMAIL":     free_email,
        "FREE_PASSWORD":  free_password,
        "GMAIL_EMAIL":    gmail_email,
        "GMAIL_PASSWORD": gmail_password,
    }

    fernet     = Fernet(key)
    encrypted  = fernet.encrypt(json.dumps(data).encode())
    CREDS_FILE.write_bytes(encrypted)
    CREDS_FILE.chmod(0o600)

    print(f"\n✓ Credentials chiffrés : {CREDS_FILE}")
    print("\n⚠️  Ne commitez jamais ces fichiers :")
    print(f"   {KEY_FILE}")
    print(f"   {CREDS_FILE}")
    print("\nAjoutez-les à votre .gitignore :")
    print("   echo '.secret.key' >> .gitignore")
    print("   echo 'credentials.enc' >> .gitignore")


if __name__ == "__main__":
    key = load_or_create_key()
    encrypt_credentials(key)
    print("\n✅ Setup terminé. Vous pouvez lancer forward_free_to_gmail.py")

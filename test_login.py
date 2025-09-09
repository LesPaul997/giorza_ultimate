#!/usr/bin/env python3
"""
Script per testare il login dell'utente trasporti
"""

import sys
from pathlib import Path

# Aggiungi la directory corrente al path per importare app
sys.path.insert(0, str(Path(__file__).parent))

from app import app, db
from models import User
from werkzeug.security import check_password_hash

def test_transport_login():
    """Testa il login dell'utente trasporti"""
    
    with app.app_context():
        # Cerca l'utente trasporti
        user = User.query.filter_by(username="trasporti").first()
        
        if not user:
            print("❌ Utente 'trasporti' non trovato nel database!")
            return
        
        print(f"✅ Utente trovato: {user.username}")
        print(f"   Ruolo: {user.role}")
        print(f"   Reparto: {user.reparto}")
        
        # Testa la password
        password = "trasporti123"
        is_valid = check_password_hash(user.password_hash, password)
        
        if is_valid:
            print("✅ Password corretta!")
        else:
            print("❌ Password non corretta!")
        
        # Controlla se il ruolo è corretto
        if user.role == 'trasporti':
            print("✅ Ruolo corretto!")
        else:
            print(f"❌ Ruolo non corretto: {user.role}")

if __name__ == "__main__":
    test_transport_login()

#!/usr/bin/env python3
"""
Script per creare un utente con ruolo 'trasporti'
"""

import sys
from pathlib import Path

# Aggiungi la directory corrente al path per importare app
sys.path.insert(0, str(Path(__file__).parent))

from app import app, db
from models import User

def create_transport_user():
    """Crea un utente con ruolo trasporti"""
    
    with app.app_context():
        # Crea le tabelle se non esistono
        db.create_all()
        
        # Dati utente trasporti
        username = "trasporti"
        password = "trasporti123"  # Cambia questa password!
        role = "trasporti"
        
        # Controlla se l'utente esiste già
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            print(f"❌ L'utente '{username}' esiste già!")
            return
        
        # Crea il nuovo utente
        user = User(username=username, role=role)
        user.set_password(password)
        
        try:
            db.session.add(user)
            db.session.commit()
            print(f"✅ Utente '{username}' creato con successo!")
            print(f"   Username: {username}")
            print(f"   Password: {password}")
            print(f"   Ruolo: {role}")
            print("\n⚠️  IMPORTANTE: Cambia la password dopo il primo accesso!")
            
        except Exception as e:
            print(f"❌ Errore nella creazione dell'utente: {e}")
            db.session.rollback()

if __name__ == "__main__":
    create_transport_user()

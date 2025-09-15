#!/usr/bin/env python3
"""
Script per correggere gli utenti esistenti
"""
import os
import sys
from pathlib import Path

# Aggiungi il percorso dell'app
sys.path.insert(0, str(Path(__file__).parent))

from app import app, db
from models import User
from reparti import get_all_reparti

def fix_users():
    """Elimina e ricrea tutti gli utenti con password corrette"""
    print("🔄 Correzione utenti...")
    
    with app.app_context():
        # Elimina tutti gli utenti esistenti
        User.query.delete()
        print("✅ Utenti esistenti eliminati")
        
        # Crea utenti per ogni reparto
        reparti = get_all_reparti()
        utenti_creati = []
        
        for codice, nome in reparti:
            username = f"picker_{codice.lower()}"
            user = User(
                username=username,
                role='picker',
                reparto=codice
            )
            user.set_password('pass')
            db.session.add(user)
            utenti_creati.append(f"{username} ({nome})")
        
        # Crea anche un utente cassiere generico
        cassiere = User(
            username='cassier1',
            role='cassa'
        )
        cassiere.set_password('pass')
        db.session.add(cassiere)
        utenti_creati.append('cassier1 (cassa)')
        
        # Crea utente display
        display_user = User(
            username='display',
            role='display'
        )
        display_user.set_password('display')
        db.session.add(display_user)
        utenti_creati.append('display (display)')
        
        # Crea utente admin
        admin_user = User(
            username='admin',
            role='admin',
            reparto='admin'
        )
        admin_user.set_password('admin123')
        db.session.add(admin_user)
        utenti_creati.append('admin (admin)')
        
        # Crea utente trasporti
        trasporti_user = User(
            username='trasporti',
            role='trasporti',
            reparto='trasporti'
        )
        trasporti_user.set_password('trasporti123')
        db.session.add(trasporti_user)
        utenti_creati.append('trasporti (trasporti)')
        
        db.session.commit()
        print("✅ Utenti ricreati:")
        for utente in utenti_creati:
            print(f"   - {utente}")
        
        print("🎉 Utenti corretti con successo!")

if __name__ == "__main__":
    fix_users()

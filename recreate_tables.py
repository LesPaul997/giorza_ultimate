#!/usr/bin/env python3
"""
Script per ricreare le tabelle con la nuova struttura
"""
import os
import sys
from pathlib import Path

# Aggiungi il percorso dell'app
sys.path.insert(0, str(Path(__file__).parent))

from app import app, db
from models import User

def recreate_tables():
    """Ricrea le tabelle con la nuova struttura"""
    print("🔄 Ricreazione tabelle...")
    
    with app.app_context():
        # Elimina tutte le tabelle
        db.drop_all()
        print("✅ Tabelle eliminate")
        
        # Ricrea tutte le tabelle
        db.create_all()
        print("✅ Tabelle ricreate con nuova struttura")
        
        print("🎉 Database ricreato con successo!")

if __name__ == "__main__":
    recreate_tables()

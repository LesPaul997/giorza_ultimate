#!/usr/bin/env python3
"""
Script per inizializzare il database PostgreSQL su Render
"""
import os
import sys
from pathlib import Path

# Aggiungi il percorso dell'app
sys.path.insert(0, str(Path(__file__).parent))

from app import app, db
from models import User, OrderEdit, OrderStatus, OrderStatusByReparto, OrderRead, OrderNote, ChatMessage, ArticoloReparto, ModifiedOrderLine, UnavailableLine, OrderAttachment, DeliveryAddress, DeliveryRoute, FuelCost, PartialOrderResidue

def init_database():
    """Inizializza il database PostgreSQL"""
    print("🔄 Inizializzazione database PostgreSQL...")
    
    with app.app_context():
        # Crea tutte le tabelle
        db.create_all()
        print("✅ Tabelle create")
        
        # Crea utenti di default
        users_data = [
            ('admin', 'admin123', 'admin', 'admin'),
            ('picker_rep01', 'picker123', 'picker', 'rep01'),
            ('picker_rep02', 'picker123', 'picker', 'rep02'),
            ('picker_rep03', 'picker123', 'picker', 'rep03'),
            ('picker_rep04', 'picker123', 'picker', 'rep04'),
            ('picker_rep05', 'picker123', 'picker', 'rep05'),
            ('picker_rep06', 'picker123', 'picker', 'rep06'),
            ('picker_rep07', 'picker123', 'picker', 'rep07'),
            ('picker_rep08', 'picker123', 'picker', 'rep08'),
            ('picker_rep09', 'picker123', 'picker', 'rep09'),
            ('picker_rep10', 'picker123', 'picker', 'rep10'),
            ('trasporti', 'trasporti123', 'trasporti', 'trasporti'),
        ]
        
        for username, password, role, reparto in users_data:
            existing = User.query.filter_by(username=username).first()
            if not existing:
                user = User(username=username, role=role, reparto=reparto)
                user.set_password(password)
                db.session.add(user)
                print(f"✅ Utente creato: {username}")
            else:
                print(f"⚠️  Utente già esistente: {username}")
        
        db.session.commit()
        print("✅ Utenti salvati")
        
        # Carica reparti se il file esiste
        reparti_file = Path(__file__).parent / "reparti_articoli.csv"
        if reparti_file.exists():
            print("🔄 Caricamento reparti...")
            import carica_reparti
            carica_reparti.carica_reparti()
            print("✅ Reparti caricati")
        else:
            print("⚠️  File reparti_articoli.csv non trovato")
        
        print("🎉 Database inizializzato con successo!")

if __name__ == "__main__":
    init_database()



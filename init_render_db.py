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
        users = [
            User(username='admin', password='admin123', role='admin', reparto='admin'),
            User(username='picker_rep01', password='picker123', role='picker', reparto='rep01'),
            User(username='picker_rep02', password='picker123', role='picker', reparto='rep02'),
            User(username='picker_rep03', password='picker123', role='picker', reparto='rep03'),
            User(username='picker_rep04', password='picker123', role='picker', reparto='rep04'),
            User(username='picker_rep05', password='picker123', role='picker', reparto='rep05'),
            User(username='picker_rep06', password='picker123', role='picker', reparto='rep06'),
            User(username='picker_rep07', password='picker123', role='picker', reparto='rep07'),
            User(username='picker_rep08', password='picker123', role='picker', reparto='rep08'),
            User(username='picker_rep09', password='picker123', role='picker', reparto='rep09'),
            User(username='picker_rep10', password='picker123', role='picker', reparto='rep10'),
            User(username='trasporti', password='trasporti123', role='trasporti', reparto='trasporti'),
        ]
        
        for user in users:
            existing = User.query.filter_by(username=user.username).first()
            if not existing:
                db.session.add(user)
                print(f"✅ Utente creato: {user.username}")
            else:
                print(f"⚠️  Utente già esistente: {user.username}")
        
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



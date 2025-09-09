#!/usr/bin/env python3
"""
Script per aggiornare lo schema del database rimuovendo il vincolo UNIQUE
"""

from app import app, db
from models import DeliveryAddress

def update_database_schema():
    """Aggiorna lo schema del database"""
    with app.app_context():
        print("🔄 Aggiornamento schema database...")
        
        # Ricrea la tabella delivery_addresses senza il vincolo UNIQUE
        try:
            # Elimina la tabella esistente
            with db.engine.connect() as conn:
                conn.execute(db.text("DROP TABLE IF EXISTS delivery_addresses"))
                conn.commit()
            print("✅ Tabella delivery_addresses eliminata")
            
            # Ricrea la tabella con il nuovo schema
            db.create_all()
            print("✅ Tabella delivery_addresses ricreata senza vincolo UNIQUE")
            
            print("✅ Schema database aggiornato con successo!")
            
        except Exception as e:
            print(f"❌ Errore durante l'aggiornamento: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    update_database_schema()

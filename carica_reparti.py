# carica_reparti.py
"""Carica i dati dei reparti dal CSV nel database SQLite."""

import csv
from pathlib import Path
from app import app
from models import db, ArticoloReparto

def carica_reparti():
    """Carica i dati dei reparti dal CSV nel database."""
    
    with app.app_context():
        # Crea le tabelle se non esistono
        db.create_all()
        
        # Legge il CSV
        csv_path = Path(__file__).with_name("reparti_articoli.csv")
        
        if not csv_path.exists():
            print("❌ File reparti_articoli.csv non trovato!")
            print("Esegui prima: python estrai_reparti.py")
            return
        
        articoli_caricati = 0
        articoli_aggiornati = 0
        
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                codice_articolo = row['codice_articolo'].strip()
                tipo_collo_1 = row['tipo_collo_1'].strip() if row['tipo_collo_1'] else None
                tipo_collo_2 = row['tipo_collo_2'].strip() if row['tipo_collo_2'] else None
                unita_misura_2 = row['unita_misura_2'].strip() if row['unita_misura_2'] else None
                operatore_conversione = row['operatore_conversione'].strip() if row['operatore_conversione'] else None
                fattore_conversione = float(row['fattore_conversione']) if row['fattore_conversione'] and row['fattore_conversione'].strip() else None
                
                # Salta righe vuote o con codici non validi
                if not codice_articolo or codice_articolo in ['.', '..']:
                    continue
                
                # Cerca se l'articolo esiste già
                articolo = ArticoloReparto.query.filter_by(codice_articolo=codice_articolo).first()
                
                if articolo:
                    # Aggiorna
                    articolo.tipo_collo_1 = tipo_collo_1
                    articolo.tipo_collo_2 = tipo_collo_2
                    articolo.unita_misura_2 = unita_misura_2
                    articolo.operatore_conversione = operatore_conversione
                    articolo.fattore_conversione = fattore_conversione
                    articoli_aggiornati += 1
                else:
                    # Crea nuovo
                    articolo = ArticoloReparto(
                        codice_articolo=codice_articolo,
                        tipo_collo_1=tipo_collo_1,
                        tipo_collo_2=tipo_collo_2,
                        unita_misura_2=unita_misura_2,
                        operatore_conversione=operatore_conversione,
                        fattore_conversione=fattore_conversione
                    )
                    db.session.add(articolo)
                    articoli_caricati += 1
        
        # Commit delle modifiche
        db.session.commit()
        
        print(f"✅ Articoli caricati: {articoli_caricati}")
        print(f"✅ Articoli aggiornati: {articoli_aggiornati}")
        print(f"📊 Totale articoli nel database: {ArticoloReparto.query.count()}")

if __name__ == "__main__":
    carica_reparti() 
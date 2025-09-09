from app import app
from models import db, OrderNote
from datetime import datetime

def add_test_notes():
    with app.app_context():
        # Crea alcune note di test con "ritiro" per ordini esistenti
        test_notes = [
            {
                'seriale': '0000111668',
                'articolo': None,
                'operatore': 'cassier1',
                'nota': 'Ritiro previsto per oggi pomeriggio'
            },
            {
                'seriale': '0000111669',
                'articolo': None,
                'operatore': 'cassier1',
                'nota': 'Cliente chiama per ritirare ordine'
            },
            {
                'seriale': '0000111670',
                'articolo': None,
                'operatore': 'cassier1',
                'nota': 'Ritira entro le 18:00'
            },
            {
                'seriale': '0000111671',
                'articolo': None,
                'operatore': 'cassier1',
                'nota': 'Ordine da ritirare domani mattina'
            },
            {
                'seriale': '0000111672',
                'articolo': None,
                'operatore': 'cassier1',
                'nota': 'Ritiro urgente - cliente in attesa'
            }
        ]
        
        notes_added = 0
        for note_data in test_notes:
            # Controlla se la nota esiste già
            existing_note = OrderNote.query.filter_by(
                seriale=note_data['seriale'],
                nota=note_data['nota']
            ).first()
            
            if not existing_note:
                note = OrderNote(
                    seriale=note_data['seriale'],
                    articolo=note_data['articolo'],
                    operatore=note_data['operatore'],
                    nota=note_data['nota'],
                    timestamp=datetime.now()
                )
                db.session.add(note)
                notes_added += 1
                print(f"✅ Aggiunta nota per ordine {note_data['seriale']}: {note_data['nota']}")
            else:
                print(f"🔔 Nota già esistente per ordine {note_data['seriale']}")
        
        if notes_added > 0:
            db.session.commit()
            print(f"\n🎉 Aggiunte {notes_added} note di test")
        else:
            print("\n🔔 Tutte le note di test sono già presenti")

if __name__ == "__main__":
    add_test_notes() 
#!/usr/bin/env python3
"""
Script per aggiungere le nuove colonne alla tabella todo_items
"""

from app import app, db
from sqlalchemy import text

def migrate_todo_items():
    """Aggiunge le nuove colonne alla tabella todo_items"""
    with app.app_context():
        dialect = db.engine.dialect.name
        print(f"🔄 Aggiornamento tabella todo_items... (dialetto: {dialect})")

        try:
            with db.engine.connect() as conn:
                # Verifica se la tabella esiste
                if dialect == 'sqlite':
                    result = conn.execute(text("""
                        SELECT name FROM sqlite_master 
                        WHERE type='table' AND name='todo_items'
                    """))
                    table_exists = bool(result.fetchone())
                elif dialect == 'postgresql':
                    result = conn.execute(text("SELECT to_regclass('public.todo_items')"))
                    table_exists = bool(result.scalar())
                else:
                    result = conn.execute(text("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public' AND table_name = 'todo_items'
                    """))
                    table_exists = bool(result.fetchone())

                if not table_exists:
                    print("⚠️  Tabella todo_items non esiste. Creazione...")
                    db.create_all()
                    print("✅ Tabella todo_items creata")
                    return
                
                # Lista delle colonne da aggiungere
                datetime_type = 'DATETIME' if dialect == 'sqlite' else 'TIMESTAMP'
                bool_default = 'BOOLEAN DEFAULT 0' if dialect == 'sqlite' else 'BOOLEAN DEFAULT FALSE'

                colonne_da_aggiungere = [
                    ('confermato', bool_default),
                    ('categoria', 'VARCHAR(50)'),
                    ('operatore_assegnato', 'VARCHAR(80)'),
                    ('completato_da', 'VARCHAR(80)'),
                    ('confermato_da', 'VARCHAR(80)'),
                    ('data_completamento', datetime_type),
                    ('data_conferma', datetime_type),
                    ('note_completamento', 'TEXT')
                ]
                
                # Verifica quali colonne esistono già
                if dialect == 'sqlite':
                    result = conn.execute(text("PRAGMA table_info(todo_items)"))
                    colonne_esistenti = [row[1] for row in result.fetchall()]
                else:
                    result = conn.execute(text("""
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = 'todo_items'
                    """))
                    colonne_esistenti = [row[0] for row in result.fetchall()]
                
                print(f"📋 Colonne esistenti: {', '.join(colonne_esistenti)}")
                
                # Aggiungi le colonne mancanti
                colonne_aggiunte = []
                for nome_colonna, tipo_colonna in colonne_da_aggiungere:
                    if nome_colonna not in colonne_esistenti:
                        try:
                            conn.execute(text(f"ALTER TABLE todo_items ADD COLUMN {nome_colonna} {tipo_colonna}"))
                            colonne_aggiunte.append(nome_colonna)
                            print(f"✅ Colonna '{nome_colonna}' aggiunta")
                        except Exception as e:
                            print(f"⚠️  Errore aggiunta colonna '{nome_colonna}': {e}")
                    else:
                        print(f"ℹ️  Colonna '{nome_colonna}' già esistente")
                
                conn.commit()
                
                if colonne_aggiunte:
                    print(f"✅ Migrazione completata! Colonne aggiunte: {', '.join(colonne_aggiunte)}")
                else:
                    print("✅ Tutte le colonne sono già presenti")
                
                # Crea indici se non esistono
                indici_da_creare = [
                    ('idx_todo_items_confermato', 'confermato'),
                    ('idx_todo_items_scadenza', 'scadenza'),
                    ('idx_todo_items_operatore_assegnato', 'operatore_assegnato')
                ]
                
                for nome_indice, colonna in indici_da_creare:
                    try:
                        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {nome_indice} ON todo_items({colonna})"))
                        print(f"✅ Indice '{nome_indice}' creato/verificato")
                    except Exception as e:
                        print(f"⚠️  Errore creazione indice '{nome_indice}': {e}")
                
                conn.commit()
                
        except Exception as e:
            print(f"❌ Errore durante la migrazione: {e}")
            import traceback
            traceback.print_exc()
            db.session.rollback()

if __name__ == "__main__":
    migrate_todo_items()


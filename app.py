# app.py
"""Flask back‑office (ordini + magazzino) – pronto per locale e Render.

Principali correzioni / miglioramenti
------------------------------------
* Caricamento del `.env` **prima** della lettura delle variabili.
* Import di `python-dotenv` protetto da *try/except* (non obbligatorio
  in produzione).
* Uso di `pathlib.Path` per referenziare i due script di estrazione e i
  CSV, così l'app funziona qualunque sia la *working directory*.
* Scheduler avviato **una sola volta** (anche sotto Gunicorn con più
  worker) grazie a una flag su `app.config`.
* Nessuna modifica alla logica, alle query SQL o alle rotte esistenti.
"""

from __future__ import annotations

import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List
from datetime import datetime, timezone, date
import pyodbc

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
    send_file,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash

from apscheduler.schedulers.background import BackgroundScheduler

# Import per la gestione dei reparti
from reparti import get_all_reparti, get_reparto_by_code, is_valid_reparto, get_display_reparti, is_valid_display_reparto

# ------------------------------------------------------------------
# 1) .env & configurazione base
# ------------------------------------------------------------------
try:
    from dotenv import load_dotenv  # type: ignore

    # Carica `.env` che si trova accanto a questo file (se presente)
    load_dotenv(Path(__file__).with_name(".env"), override=False)
except ModuleNotFoundError:
    # In ambienti dove python-dotenv non è installato (p.es. container)
    # ci si affida alle ENVs già presenti.
    pass

# ------------------------------------------------------------------
# 2) Flask app & SQLAlchemy
# ------------------------------------------------------------------
app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "changeme‑please"),
    SQLALCHEMY_DATABASE_URI=os.getenv(
        "SQLALCHEMY_DATABASE_URI", "sqlite:///app.db"
    ),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    ORDERS_CACHE=[],  # popolati dallo scheduler
    STOCK_CACHE=[],
    MODIFIED_LINES={},  # righe modificate/cancellate per seriale
)

# Lazy import del modello: evita dipendenze circolari con db.init_app
from models import OrderEdit, OrderStatus, OrderStatusByReparto, OrderRead, OrderNote, ChatMessage, User, ModifiedOrderLine, UnavailableLine, OrderAttachment, DeliveryAddress, DeliveryRoute, FuelCost, PartialOrderResidue, ArticoloReparto, CalendarioAppuntamento, TodoItem, NoteAppunto, AnnuncioUrgente, OrderArchive, db  # noqa: E402  pylint: disable=wrong-import-position

db.init_app(app)

# ------------------------------------------------------------------
# 3) Login‑manager
# ------------------------------------------------------------------
login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id: str):  # type: ignore[override]
    return db.session.get(User, int(user_id))  # pyright: ignore[reportUnknownMemberType]


# ------------------------------------------------------------------
# Helper per datetime (compatibilità)
# ------------------------------------------------------------------
def get_utc_now():
    """Restituisce datetime UTC timezone-aware (compatibile con SQLAlchemy)"""
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------
# 4) Helper I/O CSV
# ------------------------------------------------------------------
CSV_NUMERIC_FIELDS: dict[str, Iterable[str]] = {
    "ordini_clienti.csv": ("quantita", "prezzo_unitario"),
    "magazzino.csv": ("Saldo_Disponibile",),
}


def _load_csv(path: Path, numeric_fields: Iterable[str] = ()) -> List[dict]:
    """Carica un CSV e converte i campi numerici."""
    data = []
    with open(path, "r", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        for row in reader:
            # Converti i campi numerici
            for field in numeric_fields:
                if field in row and row[field]:
                    try:
                        row[field] = float(row[field])
                    except ValueError:
                        pass
            data.append(row)
    return data


def _run_script(path: Path) -> None:
    """Esegue uno script Python."""
    subprocess.run([sys.executable, str(path)], check=True)


def refresh_orders() -> None:
    """Aggiorna la cache degli ordini."""
    _run_script(Path(__file__).with_name("estrai_ordini.py"))
    orders = _load_csv(
        Path(__file__).with_name("ordini_clienti.csv"),
        CSV_NUMERIC_FIELDS["ordini_clienti.csv"],
    )
    
    # Aggiungi data di arrivo e aggiorna il codice_reparto
    current_date = datetime.now().strftime('%Y-%m-%d')
    
    # Aggiorna il codice_reparto per ogni ordine usando i dati del database
    from models import ArticoloReparto
    
    # Assicurati di essere nel contesto dell'applicazione
    with app.app_context():
        for order in orders:
            # Aggiungi data di arrivo
            order["data_arrivo"] = current_date
            
            codice_articolo = order.get("codice_articolo", "").strip()
            if codice_articolo:
                try:
                    articolo = ArticoloReparto.query.filter_by(codice_articolo=codice_articolo).first()
                    if articolo:
                        if articolo.tipo_collo_1:
                            order["codice_reparto"] = articolo.tipo_collo_1
                        else:
                            order["codice_reparto"] = 'REP05'  # Default
                        
                        # Aggiungi la seconda unità di misura se disponibile
                        if articolo.unita_misura_2:
                            order["unita_misura_2"] = articolo.unita_misura_2
                            
                            # Calcola la quantità convertita se abbiamo operatore e fattore
                            if (articolo.operatore_conversione and 
                                articolo.fattore_conversione and 
                                order.get("quantita")):
                                try:
                                    if articolo.operatore_conversione == "/":
                                        quantita_convertita = order["quantita"] / articolo.fattore_conversione
                                    elif articolo.operatore_conversione == "*":
                                        quantita_convertita = order["quantita"] * articolo.fattore_conversione
                                    else:
                                        quantita_convertita = None
                                    
                                    if quantita_convertita is not None:
                                        # CORREZIONE: Aggiungi come seconda unità, NON sostituire la prima
                                        order["quantita_um2"] = round(quantita_convertita, 3)
                                        order["operatore_conversione"] = articolo.operatore_conversione
                                        order["fattore_conversione"] = articolo.fattore_conversione
                                    else:
                                        order["quantita_um2"] = None
                                        order["operatore_conversione"] = None
                                        order["fattore_conversione"] = None
                                except (TypeError, ZeroDivisionError):
                                    order["quantita_um2"] = None
                                    order["operatore_conversione"] = None
                                    order["fattore_conversione"] = None
                            else:
                                order["quantita_um2"] = None
                                order["operatore_conversione"] = None
                                order["fattore_conversione"] = None
                        else:
                            order["unita_misura_2"] = None
                            order["quantita_um2"] = None
                            order["operatore_conversione"] = None
                            order["fattore_conversione"] = None
                    else:
                        order["codice_reparto"] = 'REP05'  # Default
                        order["unita_misura_2"] = None
                        order["quantita_um2"] = None
                        order["operatore_conversione"] = None
                        order["fattore_conversione"] = None
                except Exception as e:
                    order["codice_reparto"] = 'REP05'  # Default
                    order["unita_misura_2"] = None
                    order["quantita_um2"] = None
                    order["operatore_conversione"] = None
                    order["fattore_conversione"] = None
    
    app.config["ORDERS_CACHE"] = orders


def refresh_orders_incremental() -> None:
    """Aggiorna la cache degli ordini con rilevamento di modifiche agli ordini esistenti."""
    # Esegui lo script di estrazione per ottenere il CSV aggiornato
    _run_script(Path(__file__).with_name("estrai_ordini.py"))
    
    # Carica il CSV aggiornato
    new_orders = _load_csv(
        Path(__file__).with_name("ordini_clienti.csv"),
        CSV_NUMERIC_FIELDS["ordini_clienti.csv"],
    )
    
    # Aggiungi data di arrivo e aggiorna il codice_reparto
    current_date = datetime.now().strftime('%Y-%m-%d')
    
    # Aggiorna il codice_reparto per ogni ordine usando i dati del database
    from models import ArticoloReparto
    
    # Assicurati di essere nel contesto dell'applicazione
    with app.app_context():
        for order in new_orders:
            # Aggiungi data di arrivo
            order["data_arrivo"] = current_date
            
            codice_articolo = order.get("codice_articolo", "").strip()
            if codice_articolo:
                try:
                    articolo = ArticoloReparto.query.filter_by(codice_articolo=codice_articolo).first()
                    if articolo:
                        if articolo.tipo_collo_1:
                            order["codice_reparto"] = articolo.tipo_collo_1
                        else:
                            order["codice_reparto"] = ""  # Nessun default, reparto non determinato
                        
                        # Aggiungi la seconda unità di misura se disponibile
                        if articolo.unita_misura_2:
                            order["unita_misura_2"] = articolo.unita_misura_2
                            
                            # Calcola la quantità convertita se abbiamo operatore e fattore
                            if (articolo.operatore_conversione and 
                                articolo.fattore_conversione and 
                                order.get("quantita")):
                                try:
                                    if articolo.operatore_conversione == "/":
                                        quantita_convertita = order["quantita"] / articolo.fattore_conversione
                                    elif articolo.operatore_conversione == "*":
                                        quantita_convertita = order["quantita"] * articolo.fattore_conversione
                                    else:
                                        quantita_convertita = None
                                    
                                    if quantita_convertita is not None:
                                        # CORREZIONE: Aggiungi come seconda unità, NON sostituire la prima
                                        order["quantita_um2"] = round(quantita_convertita, 3)
                                        order["operatore_conversione"] = articolo.operatore_conversione
                                        order["fattore_conversione"] = articolo.fattore_conversione
                                    else:
                                        order["quantita_um2"] = None
                                        order["operatore_conversione"] = None
                                        order["fattore_conversione"] = None
                                except (TypeError, ZeroDivisionError):
                                    order["quantita_um2"] = None
                                    order["operatore_conversione"] = None
                                    order["fattore_conversione"] = None
                            else:
                                order["quantita_um2"] = None
                                order["operatore_conversione"] = None
                                order["fattore_conversione"] = None
                        else:
                            order["unita_misura_2"] = None
                            order["quantita_um2"] = None
                            order["operatore_conversione"] = None
                            order["fattore_conversione"] = None
                    else:
                        order["codice_reparto"] = 'REP05'  # Default
                        order["unita_misura_2"] = None
                        order["quantita_um2"] = None
                        order["operatore_conversione"] = None
                        order["fattore_conversione"] = None
                except Exception as e:
                    order["codice_reparto"] = 'REP05'  # Default
                    order["unita_misura_2"] = None
                    order["quantita_um2"] = None
                    order["operatore_conversione"] = None
                    order["fattore_conversione"] = None
    
    # Ottieni gli ordini attualmente in cache
    current_orders = app.config.get("ORDERS_CACHE", [])
    
    # Se la cache è vuota, carica tutto
    if not current_orders:
        # print("🔄 Cache vuota - Caricamento completo ordini")  # Rimosso per ridurre log
        app.config["ORDERS_CACHE"] = new_orders
        return
    
    # Conta gli ordini attuali e quelli nuovi
    current_count = len(current_orders)
    new_count = len(new_orders)
    
    # print(f"📊 Ordini attuali: {current_count}, Ordini nel CSV: {new_count}")  # Rimosso per ridurre log
    
    # Crea un dizionario per accesso rapido agli ordini attuali per seriale
    current_orders_by_serial = {}
    for order in current_orders:
        seriale = order.get("seriale", "")
        if seriale not in current_orders_by_serial:
            current_orders_by_serial[seriale] = []
        current_orders_by_serial[seriale].append(order)
    
    # Crea un dizionario per accesso rapido ai nuovi ordini per seriale
    new_orders_by_serial = {}
    for order in new_orders:
        seriale = order.get("seriale", "")
        if seriale not in new_orders_by_serial:
            new_orders_by_serial[seriale] = []
        new_orders_by_serial[seriale].append(order)
    
    # Confronta ordini esistenti per trovare modifiche
    orders_modified = False
    modified_orders = set()  # Set per tracciare gli ordini modificati
    
    for seriale in new_orders_by_serial.keys():
        if seriale in current_orders_by_serial:
            current_righe = current_orders_by_serial[seriale]
            new_righe = new_orders_by_serial[seriale]
            
            # Confronta il numero di righe per questo seriale
            current_lines = len(current_righe)
            new_lines = len(new_righe)
            
            # Crea identificatori unici per le righe attuali
            current_identifiers = set()
            for riga in current_righe:
                identifier = f"{riga.get('codice_articolo', '')}_{riga.get('quantita', 0)}_{riga.get('unita_misura', '')}"
                current_identifiers.add(identifier)
            
            # Crea identificatori unici per le righe nuove
            new_identifiers = set()
            for riga in new_righe:
                identifier = f"{riga.get('codice_articolo', '')}_{riga.get('quantita', 0)}_{riga.get('unita_misura', '')}"
                new_identifiers.add(identifier)
            
            # Controlla se ci sono differenze nel contenuto
            if current_identifiers != new_identifiers:
                # print(f"🔄 Ordine {seriale} modificato: contenuto righe cambiato")  # Rimosso per ridurre log
                # print(f"   Righe attuali: {len(current_identifiers)}")  # Rimosso per ridurre log
                # print(f"   Righe nuove: {len(new_identifiers)}")  # Rimosso per ridurre log
                # print(f"   Righe rimosse: {len(current_identifiers - new_identifiers)}")  # Rimosso per ridurre log
                # print(f"   Righe aggiunte: {len(new_identifiers - current_identifiers)}")  # Rimosso per ridurre log
                orders_modified = True
                modified_orders.add(seriale)
            elif current_lines != new_lines:
                print(f"🔄 Ordine {seriale} modificato: {current_lines} -> {new_lines} righe")
                orders_modified = True
                modified_orders.add(seriale)
    
    # Se ci sono modifiche agli ordini esistenti o ordini completamente nuovi, aggiorna tutto
    if orders_modified or new_count != current_count:
        # print("🔄 Rilevate modifiche - Aggiornamento completo cache ordini")  # Rimosso per ridurre log
        
        # Salva le righe modificate/cancellate per la visualizzazione
        if orders_modified:
            # Assicurati di essere nel contesto dell'applicazione per il database
            with app.app_context():
                # Crea un dizionario delle righe modificate per seriale
                modified_lines = {}
                for seriale in modified_orders:
                    if seriale in current_orders_by_serial:
                        # Trova le righe che non sono più presenti nei nuovi ordini
                        current_righe = current_orders_by_serial[seriale]
                        new_righe = new_orders_by_serial.get(seriale, [])
                        
                        # Crea identificatori unici per le righe attuali e nuove
                        current_identifiers = set()
                        for riga in current_righe:
                            identifier = f"{riga.get('codice_articolo', '')}_{riga.get('quantita', 0)}_{riga.get('unita_misura', '')}"
                            current_identifiers.add(identifier)
                        
                        new_identifiers = set()
                        for riga in new_righe:
                            identifier = f"{riga.get('codice_articolo', '')}_{riga.get('quantita', 0)}_{riga.get('unita_misura', '')}"
                            new_identifiers.add(identifier)
                        
                        # Trova le righe che non sono più presenti (differenza tra set)
                        removed_identifiers = current_identifiers - new_identifiers
                        removed_lines = []
                        for riga in current_righe:
                            identifier = f"{riga.get('codice_articolo', '')}_{riga.get('quantita', 0)}_{riga.get('unita_misura', '')}"
                            if identifier in removed_identifiers:
                                # Marca la riga come rimossa
                                riga["removed"] = True
                                removed_lines.append(riga)
                                
                                # Salva la riga modificata nel database
                                try:
                                    # Verifica se la riga è già stata salvata
                                    existing = ModifiedOrderLine.query.filter_by(
                                        seriale=seriale,
                                        codice_articolo=riga.get('codice_articolo', ''),
                                        quantita=riga.get('quantita', 0),
                                        unita_misura=riga.get('unita_misura', '')
                                    ).first()
                                    
                                    if not existing:
                                        modified_line = ModifiedOrderLine(
                                            seriale=seriale,
                                            codice_articolo=riga.get('codice_articolo', ''),
                                            descrizione_articolo=riga.get('descrizione_articolo', ''),
                                            descrizione_supplementare=riga.get('descrizione_supplementare', ''),
                                            quantita=riga.get('quantita', 0),
                                            unita_misura=riga.get('unita_misura', ''),
                                            unita_misura_2=riga.get('unita_misura_2'),
                                            quantita_um2=riga.get('quantita_um2'),
                                            operatore_conversione=riga.get('operatore_conversione'),
                                            fattore_conversione=riga.get('fattore_conversione'),
                                            prezzo_unitario=riga.get('prezzo_unitario'),
                                            codice_reparto=riga.get('codice_reparto'),
                                            data_ordine=riga.get('data_ordine'),
                                            numero_ordine=riga.get('numero_ordine'),
                                            nome_cliente=riga.get('nome_cliente'),
                                            ritiro=riga.get('ritiro'),
                                            data_arrivo=riga.get('data_arrivo'),
                                            removed=True
                                        )
                                        db.session.add(modified_line)
                                        # print(f"💾 Salvata riga modificata: {seriale} - {riga.get('codice_articolo', '')}")  # Rimosso per ridurre log
                                except Exception as e:
                                    print(f"❌ Errore nel salvare riga modificata: {e}")
                        
                        if removed_lines:
                            modified_lines[seriale] = removed_lines
                
                # Commit delle modifiche al database
                try:
                    db.session.commit()
                    # print(f"✅ Salvate {len([line for lines in modified_lines.values() for line in lines])} righe modificate nel database")  # Rimosso per ridurre log
                except Exception as e:
                    print(f"❌ Errore nel commit delle righe modificate: {e}")
                    db.session.rollback()
            
            # Salva le righe modificate nella cache
            app.config["MODIFIED_LINES"] = modified_lines
            
            # Salva anche gli ordini modificati (anche se non hanno righe rimosse)
            if modified_orders:
                # Crea un dizionario per gli ordini modificati senza righe rimosse
                modified_orders_cache = {}
                for seriale in modified_orders:
                    if seriale not in modified_lines:
                        # Se l'ordine è stato modificato ma non ha righe rimosse, salvalo comunque
                        modified_orders_cache[seriale] = []
                
                # Unisci con le righe modificate esistenti
                app.config["MODIFIED_LINES"].update(modified_orders_cache)
        
        app.config["ORDERS_CACHE"] = new_orders
    else:
        # print("✅ Nessuna modifica rilevata")  # Rimosso per ridurre log
        pass

def refresh_stock() -> None:
    """Aggiorna la cache del magazzino."""
    _run_script(Path(__file__).with_name("estrai_magazzino.py"))
    app.config["STOCK_CACHE"] = _load_csv(
        Path(__file__).with_name("magazzino.csv"),
        CSV_NUMERIC_FIELDS["magazzino.csv"],
    )


def get_ordine_reparti(seriale: str) -> list:
    """Ottiene la lista dei reparti coinvolti in un ordine."""
    orders = app.config.get("ORDERS_CACHE", [])
    reparti = set()
    
    for order in orders:
        if order.get("seriale") == seriale:
            reparto = order.get("codice_reparto")
            if reparto:
                reparti.add(reparto)
    
    return list(reparti)


def get_ordine_status_by_reparto(seriale: str) -> dict:
    """Ottiene lo stato di un ordine per ogni reparto."""
    with app.app_context():
        status_by_reparto = {}
        reparti = get_ordine_reparti(seriale)
        
        for reparto in reparti:
            status_record = OrderStatusByReparto.query.filter_by(
                seriale=seriale, 
                reparto=reparto
            ).order_by(OrderStatusByReparto.timestamp.desc()).first()
            
            if status_record:
                status_by_reparto[reparto] = {
                    'status': status_record.status,
                    'operatore': status_record.operatore,
                    'timestamp': status_record.timestamp
                }
            else:
                status_by_reparto[reparto] = {
                    'status': 'nuovo',
                    'operatore': None,
                    'timestamp': None
                }
        
        return status_by_reparto


def get_articolo_reparto(codice_articolo: str) -> str:
    """Ottiene il reparto di un articolo dal database."""
    from models import ArticoloReparto
    
    try:
        # Assicurati di essere nel contesto dell'applicazione
        if not hasattr(app, '_get_current_object'):
            # Se non siamo nel contesto, ritorna stringa vuota
            return ""
        
        articolo = ArticoloReparto.query.filter_by(codice_articolo=codice_articolo).first()
        if articolo and articolo.tipo_collo_1:
            return articolo.tipo_collo_1
    except Exception as e:
        print(f"Errore in get_articolo_reparto per {codice_articolo}: {e}")
    
    return ""  # Nessun default, reparto non determinato


def get_reparto_by_code(codice_reparto: str) -> str:
    """Converte il codice reparto in nome leggibile."""
    reparti = {
        'REP01': 'PROFILATI - LAMIERE - TUBOLARI',
        'REP02': 'EDILE',
        'REP03': 'TRAVI',
        'REP04': 'COIBENTATI - RECINZIONI',
        'REP05': 'FERRAMENTA',
        'REP06': 'BOMBOLE',
    }
    return reparti.get(codice_reparto, codice_reparto)


def _serialize_for_snapshot(obj):
    """Converte datetime/date in stringhe per JSON snapshot."""
    if hasattr(obj, 'isoformat'):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize_for_snapshot(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_snapshot(x) for x in obj]
    return obj


def _build_order_snapshot(seriale: str):
    """Costruisce lo snapshot di un ordine (cache + DB). Ritorna (snapshot_dict, data_ordine_date) o (None, None)."""
    orders = app.config.get("ORDERS_CACHE", [])
    righe = [o for o in orders if o.get("seriale") == seriale]
    if not righe:
        return None, None

    with app.app_context():
        # Header dal primo record
        h = righe[0]
        data_ordine_raw = h.get("data_ordine")
        data_ordine_date = None
        if hasattr(data_ordine_raw, 'date'):
            data_ordine_date = data_ordine_raw.date() if hasattr(data_ordine_raw, 'date') else data_ordine_raw
        elif isinstance(data_ordine_raw, str):
            try:
                for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y'):
                    try:
                        data_ordine_date = datetime.strptime(data_ordine_raw.split()[0], fmt).date()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # Righe: raggruppa per articolo
        articoli_raggruppati = {}
        for r in righe:
            codice = r.get("codice_articolo", "")
            if codice not in articoli_raggruppati:
                articoli_raggruppati[codice] = dict(r)
            else:
                articoli_raggruppati[codice]["quantita"] = (articoli_raggruppati[codice].get("quantita") or 0) + (r.get("quantita") or 0)
        righe_list = list(articoli_raggruppati.values())

        applied_edits = (
            OrderEdit.query.filter_by(seriale=seriale, applied=True)
            .order_by(OrderEdit.timestamp.desc())
            .all()
        )
        edited_by_articolo = {e.articolo: e for e in applied_edits}
        for r in righe_list:
            r["quantita_originale"] = r.get("quantita")
            edit = edited_by_articolo.get(r.get("codice_articolo"))
            if edit:
                r["confermata"] = True
                r["quantita_confermata"] = edit.quantita_nuova
                r["unita_misura_confermata"] = edit.unita_misura
                r["edit_operatore"] = edit.operatore
                r["edit_timestamp"] = edit.timestamp
            else:
                r["confermata"] = False
                r["quantita_confermata"] = None
                r["unita_misura_confermata"] = None
                r["edit_operatore"] = None
                r["edit_timestamp"] = None
        existing_codes = {r.get("codice_articolo", "") for r in righe_list}
        for codice, edit in edited_by_articolo.items():
            if codice not in existing_codes:
                righe_list.append({
                    "codice_articolo": codice,
                    "descrizione_articolo": f"Aggiunta in app: {codice}",
                    "quantita": 0,
                    "quantita_originale": None,
                    "confermata": True,
                    "quantita_confermata": edit.quantita_nuova,
                    "unita_misura": edit.unita_misura,
                    "unita_misura_confermata": edit.unita_misura,
                    "edit_operatore": edit.operatore,
                    "edit_timestamp": edit.timestamp,
                    "codice_reparto": None,
                    "removed": False,
                    "is_added": True,
                })
                existing_codes.add(codice)

        # Unavailable
        for ul in UnavailableLine.query.filter_by(seriale=seriale).all():
            if not ul.unavailable:
                continue
            for r in righe_list:
                if r.get("codice_articolo") == ul.codice_articolo:
                    r["unavailable"] = True
                    r["substitution_text"] = ul.substitution_text or ""

        # Righe modificate/cancellate dal DB
        for db_line in ModifiedOrderLine.query.filter_by(seriale=seriale).all():
            if not db_line.removed:
                continue
            righe_list.append({
                "codice_articolo": db_line.codice_articolo,
                "descrizione_articolo": db_line.descrizione_articolo,
                "descrizione_supplementare": db_line.descrizione_supplementare,
                "quantita": db_line.quantita,
                "unita_misura": db_line.unita_misura,
                "unita_misura_2": db_line.unita_misura_2,
                "quantita_um2": db_line.quantita_um2,
                "operatore_conversione": db_line.operatore_conversione,
                "fattore_conversione": db_line.fattore_conversione,
                "prezzo_unitario": db_line.prezzo_unitario,
                "codice_reparto": db_line.codice_reparto,
                "numero_ordine": db_line.numero_ordine,
                "nome_cliente": db_line.nome_cliente,
                "ritiro": db_line.ritiro,
                "data_arrivo": db_line.data_arrivo,
                "removed": True,
                "quantita_originale": db_line.quantita,
                "confermata": False,
                "quantita_confermata": None,
                "unita_misura_confermata": None,
                "edit_operatore": None,
                "edit_timestamp": None,
            })

        # Stato generale
        status_rec = OrderStatus.query.filter_by(seriale=seriale).first()
        status_general = {
            'status': status_rec.status if status_rec else 'nuovo',
            'operatore': status_rec.operatore if status_rec else None,
            'timestamp': status_rec.timestamp if status_rec else None,
        }
        status_by_reparto = get_ordine_status_by_reparto(seriale)

        # Note
        notes = []
        for n in OrderNote.query.filter_by(seriale=seriale).order_by(OrderNote.timestamp).all():
            notes.append({
                'articolo': n.articolo,
                'operatore': n.operatore,
                'nota': n.nota,
                'timestamp': n.timestamp,
            })

        # Indirizzi consegna
        delivery_addresses = []
        for a in DeliveryAddress.query.filter_by(seriale=seriale).all():
            delivery_addresses.append({
                'indirizzo': a.indirizzo,
                'citta': a.citta,
                'provincia': a.provincia,
                'cap': a.cap,
                'note_indirizzo': a.note_indirizzo,
                'operatore': a.operatore,
                'timestamp': a.timestamp,
            })

        # Allegati (solo metadati)
        attachments = []
        for att in OrderAttachment.query.filter_by(seriale=seriale).all():
            attachments.append({
                'filename': att.original_filename,
                'file_path': att.file_path,
                'mime_type': att.mime_type,
                'operatore': att.operatore,
                'note': att.note,
                'timestamp': att.timestamp,
            })

        # Residui parziali
        partial_residues = []
        for pr in PartialOrderResidue.query.filter_by(seriale=seriale).all():
            partial_residues.append({
                'reparto': pr.reparto,
                'codice_articolo': pr.codice_articolo,
                'descrizione_articolo': pr.descrizione_articolo,
                'residuo_quantita': pr.residuo_quantita,
                'unita_misura': pr.unita_misura,
            })

        snapshot = {
            'header': {
                'seriale': seriale,
                'numero_ordine': h.get('numero_ordine'),
                'nome_cliente': h.get('nome_cliente') or h.get('cliente_codice'),
                'data_ordine': h.get('data_ordine'),
                'ritiro': h.get('ritiro'),
                'note_cliente': h.get('note_cliente'),
                'data_arrivo': h.get('data_arrivo'),
            },
            'righe': righe_list,
            'status': status_general,
            'status_by_reparto': status_by_reparto,
            'notes': notes,
            'delivery_addresses': delivery_addresses,
            'attachments': attachments,
            'partial_residues': partial_residues,
        }
        snapshot = _serialize_for_snapshot(snapshot)
        return snapshot, data_ordine_date


# ------------------------------------------------------------------
# 5) Scheduler (avviato una sola volta)
# ------------------------------------------------------------------
if not app.config.get("SCHEDULER_STARTED"):
    scheduler = BackgroundScheduler()
    # Carica tutti gli ordini all'avvio
    scheduler.add_job(refresh_orders, "cron", hour=0, minute=0)  # Caricamento completo a mezzanotte
    # Durante il giorno, carica solo i nuovi ordini
    scheduler.add_job(refresh_orders_incremental, "interval", seconds=30)
    # Magazzino ogni 12 ore (alle 6:00 e alle 18:00)
    scheduler.add_job(refresh_stock, "cron", hour="6,18", minute=0)
    scheduler.start()
    app.config["SCHEDULER_STARTED"] = True
    print("🔄 Scheduler avviato - Ordini ogni 30s, Magazzino ogni 12h")


# ------------------------------------------------------------------
# 6) Routes
# ------------------------------------------------------------------
@app.route("/")
@login_required
def root():
    return redirect(url_for("home"))


@app.route("/home")
@login_required
def home():
    # Calcola i contatori degli ordini
    orders = app.config.get("ORDERS_CACHE", [])
    
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in orders:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Filtra ordini in base al reparto dell'utente
    if current_user.reparto:
        # Per i picker, conta solo gli ordini del loro reparto
        ordini_con_articoli_reparto = set()
        for order in orders:
            if order.get("codice_reparto") == current_user.reparto:
                ordini_con_articoli_reparto.add(order["seriale"])
        
        filtered_orders = {}
        for seriale, order in unique_orders.items():
            if seriale in ordini_con_articoli_reparto:
                filtered_orders[seriale] = order
        
        unique_orders = filtered_orders
    
    # Conta gli ordini per stato
    total_orders = len(unique_orders)
    in_preparazione = 0
    pronti = 0
    nuovi = 0
    
    # OTTIMIZZAZIONE: Query batch per la home
    all_seriali = list(unique_orders.keys())
    
    # Carica tutti gli stati per reparto in una sola query
    reparto_statuses = {}
    if all_seriali:
        reparto_records = OrderStatusByReparto.query.filter(
            OrderStatusByReparto.seriale.in_(all_seriali)
        ).all()
        for record in reparto_records:
            if record.seriale not in reparto_statuses:
                reparto_statuses[record.seriale] = {}
            reparto_statuses[record.seriale][record.reparto] = {
                'status': record.status,
                'operatore': record.operatore,
                'timestamp': record.timestamp
            }
    
    # OTTIMIZZAZIONE: Pre-calcola tutti i reparti per tutti gli ordini in una sola passata
    reparti_by_seriale = {}
    for order in orders:
        seriale = order.get("seriale")
        reparto = order.get("codice_reparto")
        if seriale and reparto:
            if seriale not in reparti_by_seriale:
                reparti_by_seriale[seriale] = set()
            reparti_by_seriale[seriale].add(reparto)
    
    for seriale, order in unique_orders.items():
        if current_user.reparto:
            # Per i picker, controlla lo stato del loro reparto
            status_by_reparto = reparto_statuses.get(seriale, {})
            my_reparto_status = status_by_reparto.get(current_user.reparto, {})
            status = my_reparto_status.get('status', 'nuovo')
        else:
            # Per i cassiere, controlla lo stato generale dell'ordine
            # basato sullo stato di tutti i reparti coinvolti
            status_by_reparto = reparto_statuses.get(seriale, {})
            # OTTIMIZZAZIONE: Usa la cache pre-calcolata invece di get_ordine_reparti()
            reparti_ordine = list(reparti_by_seriale.get(seriale, set()))
            
            if not reparti_ordine:
                status = 'nuovo'
            else:
                # Controlla se tutti i reparti sono pronti
                all_pronti = all(
                    status_by_reparto.get(reparto, {}).get('status') == 'pronto'
                    for reparto in reparti_ordine
                )
                
                # Controlla se almeno un reparto è in preparazione
                has_in_preparazione = any(
                    status_by_reparto.get(reparto, {}).get('status') == 'in_preparazione'
                    for reparto in reparti_ordine
                )
                
                if all_pronti:
                    status = 'pronto'
                elif has_in_preparazione:
                    status = 'in_preparazione'
                else:
                    status = 'nuovo'
        
        if status == 'in_preparazione':
            in_preparazione += 1
        elif status == 'pronto':
            pronti += 1
        else:
            nuovi += 1
    
    # Conta ordini da completare (manuali)
    partial_query = PartialOrderResidue.query
    if current_user.role == 'picker' and current_user.reparto:
        partial_query = partial_query.filter_by(reparto=current_user.reparto)
    partially_count = partial_query.with_entities(PartialOrderResidue.seriale).distinct().count()
    
    counters = {
        'total': total_orders,
        'in_preparazione': in_preparazione,
        'pronti': pronti,
        'nuovi': nuovi,
        'parziali': partially_count
    }
    
    return render_template("home.html", role=current_user.role, counters=counters)


# ------------------------------------------------------------------
# Archivio ordini
# ------------------------------------------------------------------

@app.route("/archivio-ordini")
@login_required
def archivio_ordini_list():
    """Pagina elenco ordini archiviati."""
    return render_template("archivio_ordini_list.html")


@app.route("/api/archivio/orders")
@login_required
def api_archivio_orders():
    """API elenco ordini archiviati (paginated, ricerca)."""
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        per_page = min(max(per_page, 5), 100)
        q = request.args.get("q", "").strip()

        query = OrderArchive.query
        if q:
            q_like = f"%{q}%"
            query = query.filter(
                db.or_(
                    OrderArchive.seriale.ilike(q_like),
                    OrderArchive.numero_ordine.ilike(q_like),
                    OrderArchive.nome_cliente.ilike(q_like),
                )
            )
        query = query.order_by(OrderArchive.data_ordine.desc().nullslast(), OrderArchive.numero_ordine.desc())
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        items = []
        for arch in pagination.items:
            items.append({
                "seriale": arch.seriale,
                "numero_ordine": arch.numero_ordine,
                "data_ordine": arch.data_ordine.isoformat() if arch.data_ordine else None,
                "nome_cliente": arch.nome_cliente,
                "archived_at": arch.archived_at.isoformat() if arch.archived_at else None,
            })
        return jsonify({
            "success": True,
            "orders": items,
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
            "total_pages": pagination.pages,
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "orders": [],
            "total": 0,
            "page": 1,
            "per_page": 20,
            "total_pages": 0,
            "error": "Archivio non disponibile. Esegui l'inizializzazione del database (init_render_db.py).",
        }), 200


@app.route("/api/archivio/run-2025", methods=["POST"])
@login_required
def api_archivio_run_2025():
    """Esegue l'archivio ordini dalla cache corrente."""
    try:
        from datetime import date as date_type
        cutoff = date_type(2025, 12, 31)
        orders = app.config.get("ORDERS_CACHE", [])
        unique_seriali = {}
        for o in orders:
            seriale = o.get("seriale")
            if not seriale:
                continue
            data_raw = o.get("data_ordine")
            try:
                if hasattr(data_raw, 'date'):
                    d = data_raw.date()
                elif isinstance(data_raw, str):
                    d = None
                    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y'):
                        try:
                            d = datetime.strptime(data_raw.split()[0], fmt).date()
                            break
                        except (ValueError, AttributeError):
                            continue
                    if d is None:
                        continue
                else:
                    continue
                if d <= cutoff and seriale not in unique_seriali:
                    unique_seriali[seriale] = {
                        "numero_ordine": o.get("numero_ordine"),
                        "nome_cliente": o.get("nome_cliente") or o.get("cliente_codice"),
                        "data_ordine": d,
                    }
            except Exception:
                continue

        archived = 0
        updated = 0
        errors = []
        with app.app_context():
            for seriale, info in unique_seriali.items():
                snapshot, data_ordine_date = _build_order_snapshot(seriale)
                if snapshot is None:
                    errors.append(f"Ordine {seriale}: non in cache")
                    continue
                try:
                    rec = OrderArchive.query.filter_by(seriale=seriale).first()
                    if rec:
                        rec.numero_ordine = info.get("numero_ordine")
                        rec.data_ordine = info.get("data_ordine") or data_ordine_date
                        rec.nome_cliente = info.get("nome_cliente")
                        rec.snapshot = json.dumps(snapshot, ensure_ascii=False)
                        db.session.commit()
                        updated += 1
                    else:
                        rec = OrderArchive(
                            seriale=seriale,
                            numero_ordine=info.get("numero_ordine"),
                            data_ordine=info.get("data_ordine") or data_ordine_date,
                            nome_cliente=info.get("nome_cliente"),
                            snapshot=json.dumps(snapshot, ensure_ascii=False),
                        )
                        db.session.add(rec)
                        db.session.commit()
                        archived += 1
                except Exception as e:
                    db.session.rollback()
                    errors.append(f"Ordine {seriale}: {e}")
        return jsonify({
            "success": True,
            "archived": archived,
            "updated": updated,
            "total_candidates": len(unique_seriali),
            "errors": errors[:20],
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "archived": 0,
            "updated": 0,
            "total_candidates": 0,
            "errors": ["Archivio non disponibile. Esegui l'inizializzazione del database (init_render_db.py)."],
        }), 200


@app.route("/archivio-ordini/<seriale>")
@login_required
def archivio_ordini_detail(seriale):
    """Dettaglio read-only di un ordine archiviato."""
    arch = OrderArchive.query.filter_by(seriale=seriale).first()
    if not arch:
        abort(404)
    try:
        snapshot = json.loads(arch.snapshot)
    except Exception:
        abort(404)
    return render_template("archivio_ordini_detail.html", arch=arch, snapshot=snapshot)


# ------------------------------------------------------------------


@app.route("/api/orders/search")
@login_required
def api_orders_search():
    """Endpoint per ricerca ordini - cerca in TUTTI gli ordini, non solo quelli caricati"""
    search_term = request.args.get('q', '').strip()
    
    if not search_term:
        return jsonify({"orders": [], "total": 0})
    
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in app.config["ORDERS_CACHE"]:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Filtra ordini in base al reparto dell'utente
    if current_user.reparto:
        # Per i picker, filtra solo gli ordini del loro reparto
        ordini_con_articoli_reparto = set()
        for order in app.config["ORDERS_CACHE"]:
            if order.get("codice_reparto") == current_user.reparto:
                ordini_con_articoli_reparto.add(order["seriale"])
        
        filtered_orders = {}
        for seriale, order in unique_orders.items():
            if seriale in ordini_con_articoli_reparto:
                filtered_orders[seriale] = order
        
        unique_orders = filtered_orders
    
    # Applica il filtro di ricerca
    search_term_lower = search_term.lower()
    filtered_orders = []
    
    for order in unique_orders.values():
        # Cerca in tutti i campi rilevanti
        if (search_term_lower in str(order.get("numero_ordine", "")).lower() or
            search_term_lower in str(order.get("nome_cliente", "")).lower() or
            search_term_lower in str(order.get("seriale", "")).lower() or
            search_term_lower in str(order.get("codice_articolo", "")).lower() or
            search_term_lower in str(order.get("descrizione_articolo", "")).lower() or
            search_term_lower in str(order.get("descrizione_supplementare", "")).lower()):
            filtered_orders.append(order)
    
    # Ordina per data (più recente prima) e poi per numero ordine (maggiore prima)
    # Questo risolve il problema quando il progressivo riparte da 1 in un nuovo anno
    def sort_key(order):
        # Estrai data_ordine e convertila in formato comparabile
        data_ordine = order.get("data_ordine")
        if data_ordine:
            # Se è già un datetime, usa direttamente
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            # Se è una stringa, prova a parsarla
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    # Prova vari formati comuni
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)  # Fallback per date non parseabili
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)  # Fallback per ordini senza data
        
        # Estrai numero_ordine
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        
        # Ritorna tupla (data, numero) per ordinamento: più recente e numero maggiore prima
        return (data_sort, num_sort)
    
    sorted_orders = sorted(filtered_orders, key=sort_key, reverse=True)
    
    return jsonify({
        "orders": sorted_orders,
        "total": len(sorted_orders),
        "search_term": search_term
    })


@app.route("/api/orders")
@login_required
def api_orders():
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in app.config["ORDERS_CACHE"]:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Filtra ordini in base al reparto dell'utente
    if current_user.reparto:
        # Ottieni tutti gli ordini con articoli del reparto dell'utente
        ordini_con_articoli_reparto = set()
        for order in app.config["ORDERS_CACHE"]:
            if order.get("codice_reparto") == current_user.reparto:
                ordini_con_articoli_reparto.add(order["seriale"])
        
        # Filtra solo gli ordini che hanno articoli del reparto dell'utente
        filtered_orders = {}
        for seriale, order in unique_orders.items():
            if seriale in ordini_con_articoli_reparto:
                filtered_orders[seriale] = order
        
        unique_orders = filtered_orders
    
    # OTTIMIZZAZIONE: Query batch invece di query individuali
    all_seriali = list(unique_orders.keys())
    
    # 1. Carica tutti gli stati generali in una sola query
    general_statuses = {}
    if all_seriali:
        status_records = OrderStatus.query.filter(OrderStatus.seriale.in_(all_seriali)).all()
        for status_record in status_records:
            general_statuses[status_record.seriale] = {
                'status': status_record.status,
                'operatore': status_record.operatore,
                'timestamp': status_record.timestamp.isoformat() if status_record.timestamp else None
            }
    
    # 2. Carica tutti gli stati per reparto in una sola query
    reparto_statuses = {}
    if all_seriali:
        reparto_records = OrderStatusByReparto.query.filter(
            OrderStatusByReparto.seriale.in_(all_seriali)
        ).all()
        for record in reparto_records:
            if record.seriale not in reparto_statuses:
                reparto_statuses[record.seriale] = {}
            reparto_statuses[record.seriale][record.reparto] = {
                'status': record.status,
                'operatore': record.operatore,
                'timestamp': record.timestamp
            }
    
    # 3. OTTIMIZZAZIONE: Carica tutti i read del picker in una sola query (solo per picker)
    read_seriali_set = set()
    if current_user.reparto and all_seriali:
        read_records = OrderRead.query.filter(
            OrderRead.seriale.in_(all_seriali),
            OrderRead.operatore == current_user.username
        ).all()
        read_seriali_set = {r.seriale for r in read_records}
    
    # 4. OTTIMIZZAZIONE: Pre-calcola tutti i reparti per tutti gli ordini in una sola passata
    reparti_by_seriale = {}
    for order in app.config["ORDERS_CACHE"]:
        seriale = order.get("seriale")
        reparto = order.get("codice_reparto")
        if seriale and reparto:
            if seriale not in reparti_by_seriale:
                reparti_by_seriale[seriale] = set()
            reparti_by_seriale[seriale].add(reparto)
    
    # 5. Applica gli stati agli ordini
    for seriale, order in unique_orders.items():
        # Stato generale dell'ordine
        if seriale in general_statuses:
            order["status"] = general_statuses[seriale]['status']
            order["status_operatore"] = general_statuses[seriale]['operatore']
            order["status_timestamp"] = general_statuses[seriale]['timestamp']
        else:
            order["status"] = "nuovo"
            order["status_operatore"] = None
            order["status_timestamp"] = None
        
        # Stato per reparto (solo per cassiere o se l'utente ha un reparto)
        if current_user.role in ["cassiere", "cassa"] or current_user.reparto:
            # Usa i dati precaricati invece di chiamare get_ordine_status_by_reparto
            status_by_reparto = reparto_statuses.get(seriale, {})
            
            # OTTIMIZZAZIONE: Usa la cache pre-calcolata invece di get_ordine_reparti()
            reparti_ordine = list(reparti_by_seriale.get(seriale, set()))
            for reparto in reparti_ordine:
                if reparto not in status_by_reparto:
                    status_by_reparto[reparto] = {
                        'status': 'nuovo',
                        'operatore': None,
                        'timestamp': None
                    }
            
            order["status_by_reparto"] = status_by_reparto
            
            # Per i picker, aggiungi solo lo stato del loro reparto
            if current_user.reparto:
                my_reparto_status = status_by_reparto.get(current_user.reparto, {})
                order["my_reparto_status"] = my_reparto_status
                
                # OTTIMIZZAZIONE: Usa il Set pre-caricato invece di query individuale
                is_read = seriale in read_seriali_set
                
                # MODIFICA: Per i picker, mostra lo stato del loro reparto nell'elenco
                # Se c'è uno stato del reparto (in_preparazione, pronto, ecc.), mostra quello
                # Altrimenti, se è stato letto, mostra "letto", altrimenti "nuovo"
                reparto_status = my_reparto_status.get('status', 'nuovo')
                
                if reparto_status in ['in_preparazione', 'pronto']:
                    # C'è uno stato operativo del reparto - mostra quello
                    order["status"] = reparto_status
                    order["status_operatore"] = my_reparto_status.get('operatore')
                    order["status_timestamp"] = my_reparto_status.get('timestamp')
                elif is_read:
                    # Non c'è stato operativo ma è stato letto
                    order["status"] = "letto"
                    order["status_operatore"] = current_user.username
                    order["status_timestamp"] = None
                else:
                    # Non c'è stato operativo e non è stato letto
                    order["status"] = "nuovo"
                    order["status_operatore"] = None
                    order["status_timestamp"] = None
            else:
                # Per i cassiere, aggiungi un riassunto dello stato
                status_summary = []
                for reparto in reparti_ordine:
                    reparto_status = status_by_reparto.get(reparto, {})
                    status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
                order["status_summary"] = " | ".join(status_summary)
    
    # Ordina gli ordini per data (più recente prima) e poi per numero ordine (maggiore prima)
    # Questo risolve il problema quando il progressivo riparte da 1 in un nuovo anno
    def sort_key(order):
        # Estrai data_ordine e convertila in formato comparabile
        data_ordine = order.get("data_ordine")
        if data_ordine:
            # Se è già un datetime, usa direttamente
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            # Se è una stringa, prova a parsarla
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    # Prova vari formati comuni
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)  # Fallback per date non parseabili
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)  # Fallback per ordini senza data
        
        # Estrai numero_ordine
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        
        # Ritorna tupla (data, numero) per ordinamento: più recente e numero maggiore prima
        return (data_sort, num_sort)
    
    sorted_orders = sorted(unique_orders.values(), key=sort_key, reverse=True)
    
    # Parametri di paginazione
    page = request.args.get('page', 1, type=int)
    limit = 10
    
    # Applica paginazione
    total_orders = len(sorted_orders)
    offset = (page - 1) * limit
    paginated_orders = sorted_orders[offset:offset + limit]
    has_more = offset + limit < total_orders
    
    return jsonify({
        "orders": paginated_orders,
        "has_more": has_more,
        "current_page": page,
        "total_orders": total_orders,
        "total_pages": (total_orders + limit - 1) // limit
    })


@app.route("/api/refresh")
@login_required
def api_refresh():
    """Endpoint per il refresh automatico - restituisce se ci sono state modifiche"""
    # Controlla se ci sono modifiche recenti nel database
    with app.app_context():
        # Conta le righe modificate degli ultimi 5 minuti
        from datetime import timedelta
        five_minutes_ago = datetime.now() - timedelta(minutes=5)
        
        recent_modified_lines = ModifiedOrderLine.query.filter(
            ModifiedOrderLine.created_at >= five_minutes_ago
        ).all()
        
        # Raggruppa per seriale
        modified_orders = set()
        for line in recent_modified_lines:
            modified_orders.add(line.seriale)
        
        # Controlla anche la cache in memoria per modifiche recenti
        modified_lines_cache = app.config.get("MODIFIED_LINES", {})
        for seriale in modified_lines_cache.keys():
            modified_orders.add(seriale)
        
        has_changes = len(modified_orders) > 0
        
        # Log per debug
        if has_changes:
            print(f"🔄 API Refresh: Modifiche rilevate per ordini: {list(modified_orders)}")
    
    return jsonify({
        "has_changes": has_changes,
        "modified_orders": list(modified_orders),
        "timestamp": datetime.now().isoformat()
    })


@app.route("/orders")
@login_required
def index():
    """Pagina principale con lista ordini."""
    return render_template("index.html")


@app.route("/api/magazzino")
@login_required
def api_magazzino():
    return jsonify(app.config["STOCK_CACHE"])


@app.route("/magazzino")
@login_required
def magazzino():
    return render_template("magazzino.html")


@app.route("/anagrafica")
@login_required
def anagrafica():
    return render_template("anagrafica.html")


@app.route("/organizza-giornata")
@login_required
def organizza_giornata():
    # Solo per cassiere
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    return render_template("organizza_giornata.html")


# ============================================
# API ROUTES PER "ORGANIZZA GIORNATA"
# ============================================

# CALENDARIO APPUNTAMENTI
@app.route("/api/organizza/calendario", methods=["GET"])
@login_required
def api_calendario_get():
    """Ottieni gli appuntamenti (ottimizzato: solo 6 mesi avanti/indietro)"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    from datetime import date, timedelta
    
    # Ottimizzazione: carica solo 6 mesi indietro e 12 mesi avanti
    oggi = date.today()
    data_inizio = oggi - timedelta(days=180)  # 6 mesi indietro
    data_fine = oggi + timedelta(days=365)    # 12 mesi avanti
    
    appuntamenti = CalendarioAppuntamento.query.filter(
        CalendarioAppuntamento.data >= data_inizio,
        CalendarioAppuntamento.data <= data_fine
    ).order_by(CalendarioAppuntamento.data, CalendarioAppuntamento.ora).all()
    
    return jsonify([{
        'id': a.id,
        'titolo': a.titolo,
        'descrizione': a.descrizione,
        'data': a.data.isoformat() if a.data else None,
        'ora': a.ora.strftime('%H:%M') if a.ora else None,
        'colore': a.colore,
        'creato_da': a.creato_da
    } for a in appuntamenti])


@app.route("/api/organizza/calendario", methods=["POST"])
@login_required
def api_calendario_post():
    """Crea nuovo appuntamento"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    data = request.json
    from datetime import date, time
    
    appuntamento = CalendarioAppuntamento(
        titolo=data.get('titolo'),
        descrizione=data.get('descrizione'),
        data=datetime.strptime(data.get('data'), '%Y-%m-%d').date() if data.get('data') else date.today(),
        ora=datetime.strptime(data.get('ora'), '%H:%M').time() if data.get('ora') else None,
        colore=data.get('colore', 'blue'),
        creato_da=current_user.username
    )
    
    db.session.add(appuntamento)
    db.session.commit()
    
    return jsonify({
        'id': appuntamento.id,
        'titolo': appuntamento.titolo,
        'descrizione': appuntamento.descrizione,
        'data': appuntamento.data.isoformat(),
        'ora': appuntamento.ora.strftime('%H:%M') if appuntamento.ora else None,
        'colore': appuntamento.colore
    }), 201


@app.route("/api/organizza/calendario/<int:appuntamento_id>", methods=["PUT"])
@login_required
def api_calendario_put(appuntamento_id):
    """Modifica appuntamento"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    appuntamento = CalendarioAppuntamento.query.get_or_404(appuntamento_id)
    data = request.json
    from datetime import date, time
    
    if data.get('titolo'):
        appuntamento.titolo = data['titolo']
    if data.get('descrizione') is not None:
        appuntamento.descrizione = data['descrizione']
    if data.get('data'):
        appuntamento.data = datetime.strptime(data['data'], '%Y-%m-%d').date()
    if data.get('ora'):
        appuntamento.ora = datetime.strptime(data['ora'], '%H:%M').time()
    if data.get('colore'):
        appuntamento.colore = data['colore']
    
    appuntamento.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    
    return jsonify({
        'id': appuntamento.id,
        'titolo': appuntamento.titolo,
        'descrizione': appuntamento.descrizione,
        'data': appuntamento.data.isoformat(),
        'ora': appuntamento.ora.strftime('%H:%M') if appuntamento.ora else None,
        'colore': appuntamento.colore
    })


@app.route("/api/organizza/calendario/<int:appuntamento_id>", methods=["DELETE"])
@login_required
def api_calendario_delete(appuntamento_id):
    """Elimina appuntamento"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    appuntamento = CalendarioAppuntamento.query.get_or_404(appuntamento_id)
    db.session.delete(appuntamento)
    db.session.commit()
    
    return jsonify({'success': True})


# TO-DO LIST
@app.route("/api/organizza/todo", methods=["GET"])
@login_required
def api_todo_get():
    """Ottieni tutti i task (con filtri avanzati)"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    # Filtri opzionali
    filtro_stato = request.args.get('stato', 'tutti')  # tutti, attivi, completati, confermati
    filtro_operatore = request.args.get('operatore', None)  # Filtra per operatore assegnato
    filtro_priorita = request.args.get('priorita', None)  # Filtra per priorità
    filtro_categoria = request.args.get('categoria', None)  # Filtra per categoria
    
    # Query base: tutti i task (non solo quelli creati dall'utente, ma anche quelli assegnati)
    query = TodoItem.query.filter(
        (TodoItem.creato_da == current_user.username) | 
        (TodoItem.operatore_assegnato == current_user.username)
    )
    
    # Applica filtri
    if filtro_stato == 'attivi':
        query = query.filter(TodoItem.completato == False)
    elif filtro_stato == 'completati':
        query = query.filter(TodoItem.completato == True, TodoItem.confermato == False)
    elif filtro_stato == 'confermati':
        query = query.filter(TodoItem.confermato == True)
    
    if filtro_operatore:
        query = query.filter(TodoItem.operatore_assegnato == filtro_operatore)
    
    if filtro_priorita:
        query = query.filter(TodoItem.priorita == filtro_priorita)
    
    if filtro_categoria:
        query = query.filter(TodoItem.categoria == filtro_categoria)
    
    # Ottimizzazione: limita a 500 task per evitare problemi di performance
    todos = query.order_by(
        TodoItem.ordine, 
        TodoItem.completato, 
        TodoItem.priorita.desc(),  # Priorità alta prima
        TodoItem.scadenza.asc().nullslast(),  # Scadenze prima
        TodoItem.created_at.desc()
    ).limit(500).all()  # Limite per performance
    
    return jsonify([{
        'id': t.id,
        'titolo': t.titolo,
        'descrizione': t.descrizione,
        'completato': t.completato,
        'confermato': t.confermato,
        'priorita': t.priorita,
        'categoria': t.categoria,
        'scadenza': t.scadenza.isoformat() if t.scadenza else None,
        'operatore_assegnato': t.operatore_assegnato,
        'creato_da': t.creato_da,
        'completato_da': t.completato_da,
        'confermato_da': t.confermato_da,
        'data_completamento': t.data_completamento.isoformat() if t.data_completamento else None,
        'data_conferma': t.data_conferma.isoformat() if t.data_conferma else None,
        'note_completamento': t.note_completamento,
        'ordine': t.ordine,
        'created_at': t.created_at.isoformat() if t.created_at else None
    } for t in todos])


@app.route("/api/organizza/todo", methods=["POST"])
@login_required
def api_todo_post():
    """Crea nuovo task"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    data = request.json
    from datetime import date
    
    todo = TodoItem(
        titolo=data.get('titolo'),
        descrizione=data.get('descrizione'),
        priorita=data.get('priorita', 'media'),
        categoria=data.get('categoria'),
        scadenza=datetime.strptime(data['scadenza'], '%Y-%m-%d').date() if data.get('scadenza') else None,
        operatore_assegnato=data.get('operatore_assegnato'),
        creato_da=current_user.username,
        ordine=data.get('ordine', 0)
    )
    
    db.session.add(todo)
    db.session.commit()
    
    return jsonify({
        'id': todo.id,
        'titolo': todo.titolo,
        'descrizione': todo.descrizione,
        'completato': todo.completato,
        'confermato': todo.confermato,
        'priorita': todo.priorita,
        'categoria': todo.categoria,
        'scadenza': todo.scadenza.isoformat() if todo.scadenza else None,
        'operatore_assegnato': todo.operatore_assegnato,
        'ordine': todo.ordine
    }), 201


@app.route("/api/organizza/todo/<int:todo_id>", methods=["PUT"])
@login_required
def api_todo_put(todo_id):
    """Modifica task (creatore o operatore assegnato possono modificare)"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    todo = TodoItem.query.get_or_404(todo_id)
    # Permetti modifica se sei il creatore o l'operatore assegnato
    if todo.creato_da != current_user.username and todo.operatore_assegnato != current_user.username:
        abort(403)
    
    data = request.json
    from datetime import date
    
    if data.get('titolo'):
        todo.titolo = data['titolo']
    if data.get('descrizione') is not None:
        todo.descrizione = data['descrizione']
    if 'completato' in data:
        todo.completato = data['completato']
    if data.get('priorita'):
        todo.priorita = data['priorita']
    if data.get('categoria') is not None:
        todo.categoria = data['categoria']
    if data.get('scadenza'):
        todo.scadenza = datetime.strptime(data['scadenza'], '%Y-%m-%d').date() if data['scadenza'] else None
    if data.get('operatore_assegnato') is not None:
        todo.operatore_assegnato = data['operatore_assegnato']
    if 'ordine' in data:
        todo.ordine = data['ordine']
    
    todo.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    
    return jsonify({
        'id': todo.id,
        'titolo': todo.titolo,
        'descrizione': todo.descrizione,
        'completato': todo.completato,
        'confermato': todo.confermato,
        'priorita': todo.priorita,
        'categoria': todo.categoria,
        'scadenza': todo.scadenza.isoformat() if todo.scadenza else None,
        'operatore_assegnato': todo.operatore_assegnato,
        'ordine': todo.ordine
    })


@app.route("/api/organizza/todo/<int:todo_id>", methods=["DELETE"])
@login_required
def api_todo_delete(todo_id):
    """Elimina task (solo il creatore può eliminare)"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    todo = TodoItem.query.get_or_404(todo_id)
    if todo.creato_da != current_user.username:
        abort(403)
    
    db.session.delete(todo)
    db.session.commit()
    
    return jsonify({'success': True})


@app.route("/api/organizza/todo/<int:todo_id>/completa", methods=["POST"])
@login_required
def api_todo_completa(todo_id):
    """Completa un task (con note opzionali)"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    todo = TodoItem.query.get_or_404(todo_id)
    # Solo l'operatore assegnato o il creatore possono completare
    if todo.operatore_assegnato and todo.operatore_assegnato != current_user.username:
        if todo.creato_da != current_user.username:
            abort(403)
    
    data = request.json
    todo.completato = True
    todo.completato_da = current_user.username
    todo.data_completamento = datetime.now(timezone.utc)
    todo.note_completamento = data.get('note_completamento')
    todo.updated_at = datetime.now(timezone.utc)
    
    db.session.commit()
    
    return jsonify({
        'id': todo.id,
        'completato': todo.completato,
        'completato_da': todo.completato_da,
        'data_completamento': todo.data_completamento.isoformat(),
        'note_completamento': todo.note_completamento
    })


@app.route("/api/organizza/todo/<int:todo_id>/conferma", methods=["POST"])
@login_required
def api_todo_conferma(todo_id):
    """Conferma un task completato (solo il creatore può confermare)"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    todo = TodoItem.query.get_or_404(todo_id)
    if todo.creato_da != current_user.username:
        abort(403)
    
    if not todo.completato:
        return jsonify({'error': 'Il task deve essere completato prima di essere confermato'}), 400
    
    todo.confermato = True
    todo.confermato_da = current_user.username
    todo.data_conferma = datetime.now(timezone.utc)
    todo.updated_at = datetime.now(timezone.utc)
    
    db.session.commit()
    
    return jsonify({
        'id': todo.id,
        'confermato': todo.confermato,
        'confermato_da': todo.confermato_da,
        'data_conferma': todo.data_conferma.isoformat()
    })


# Endpoint rimosso: assegnazione operatore ora è manuale (campo testo)


# NOTE/APPUNTI
@app.route("/api/organizza/note", methods=["GET"])
@login_required
def api_note_get():
    """Ottieni le note dell'utente"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    note = NoteAppunto.query.filter_by(creato_da=current_user.username).first()
    
    if not note:
        # Crea foglio vuoto se non esiste
        note = NoteAppunto(creato_da=current_user.username, contenuto='')
        db.session.add(note)
        db.session.commit()
    
    return jsonify({
        'contenuto': note.contenuto or '',
        'updated_at': note.updated_at.isoformat() if note.updated_at else None
    })


@app.route("/api/organizza/note", methods=["POST"])
@login_required
def api_note_post():
    """Salva le note"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    data = request.json
    contenuto = data.get('contenuto', '')
    
    note = NoteAppunto.query.filter_by(creato_da=current_user.username).first()
    
    if note:
        note.contenuto = contenuto
        note.updated_at = datetime.now(timezone.utc)
    else:
        note = NoteAppunto(creato_da=current_user.username, contenuto=contenuto)
        db.session.add(note)
    
    db.session.commit()
    
    return jsonify({
        'contenuto': note.contenuto,
        'updated_at': note.updated_at.isoformat() if note.updated_at else None
    })


# ANNUNCI URGENTI
@app.route("/api/organizza/annunci", methods=["GET"])
@login_required
def api_annunci_get():
    """Ottieni tutti gli annunci urgenti attivi"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    include_all = request.args.get('all') == '1'
    
    query = AnnuncioUrgente.query
    if not include_all:
        query = query.filter(
            AnnuncioUrgente.attivo == True,
            (AnnuncioUrgente.scadenza == None) | (AnnuncioUrgente.scadenza > now)
        )
    
    annunci = query.order_by(AnnuncioUrgente.created_at.desc()).all()
    
    return jsonify([{
        'id': a.id,
        'titolo': a.titolo,
        'messaggio': a.messaggio,
        'attivo': a.attivo,
        'creato_da': a.creato_da,
        'created_at': a.created_at.isoformat() if a.created_at else None,
        'scadenza': a.scadenza.isoformat() if a.scadenza else None
    } for a in annunci])


@app.route("/api/organizza/annunci", methods=["POST"])
@login_required
def api_annunci_post():
    """Crea nuovo annuncio urgente"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    data = request.json or {}
    from datetime import datetime, timezone
    
    titolo = (data.get('titolo') or '').strip()
    messaggio = (data.get('messaggio') or '').strip()
    if not titolo or not messaggio:
        return jsonify({'error': 'Titolo e messaggio sono obbligatori'}), 400
    
    scadenza = None
    scadenza_raw = data.get('scadenza')
    if scadenza_raw:
        try:
            cleaned = scadenza_raw.strip().replace('Z', '+00:00')
            scadenza = datetime.fromisoformat(cleaned)
        except ValueError:
            return jsonify({'error': 'Formato scadenza non valido'}), 400
    
    annuncio = AnnuncioUrgente(
        titolo=titolo,
        messaggio=messaggio,
        attivo=True,
        creato_da=current_user.username,
        scadenza=scadenza
    )
    
    db.session.add(annuncio)
    db.session.commit()
    
    return jsonify({
        'id': annuncio.id,
        'titolo': annuncio.titolo,
        'messaggio': annuncio.messaggio,
        'creato_da': annuncio.creato_da,
        'created_at': annuncio.created_at.isoformat() if annuncio.created_at else None,
        'scadenza': annuncio.scadenza.isoformat() if annuncio.scadenza else None,
        'attivo': annuncio.attivo
    }), 201


@app.route("/api/organizza/annunci/<int:annuncio_id>", methods=["PUT"])
@login_required
def api_annunci_put(annuncio_id):
    """Modifica annuncio"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    annuncio = AnnuncioUrgente.query.get_or_404(annuncio_id)
    data = request.json or {}
    from datetime import datetime
    
    if 'titolo' in data:
        titolo = (data.get('titolo') or '').strip()
        if not titolo:
            return jsonify({'error': 'Il titolo non può essere vuoto'}), 400
        annuncio.titolo = titolo
    if 'messaggio' in data:
        messaggio = (data.get('messaggio') or '').strip()
        if not messaggio:
            return jsonify({'error': 'Il messaggio non può essere vuoto'}), 400
        annuncio.messaggio = messaggio
    if 'attivo' in data:
        annuncio.attivo = bool(data['attivo'])
    if 'scadenza' in data:
        scadenza_raw = data.get('scadenza')
        if scadenza_raw:
            try:
                cleaned = scadenza_raw.strip().replace('Z', '+00:00')
                annuncio.scadenza = datetime.fromisoformat(cleaned)
            except ValueError:
                return jsonify({'error': 'Formato scadenza non valido'}), 400
        else:
            annuncio.scadenza = None
    
    db.session.commit()
    
    return jsonify({
        'id': annuncio.id,
        'titolo': annuncio.titolo,
        'messaggio': annuncio.messaggio,
        'attivo': annuncio.attivo,
        'scadenza': annuncio.scadenza.isoformat() if annuncio.scadenza else None
    })


@app.route("/api/organizza/annunci/<int:annuncio_id>", methods=["DELETE"])
@login_required
def api_annunci_delete(annuncio_id):
    """Elimina annuncio"""
    if current_user.role not in ['cassiere', 'cassa']:
        abort(403)
    
    annuncio = AnnuncioUrgente.query.get_or_404(annuncio_id)
    db.session.delete(annuncio)
    db.session.commit()
    
    return jsonify({'success': True})


@app.route("/ordini/preparazione")
@login_required
def ordini_preparazione():
    # VERSIONE ULTRA-ALLEGGERITA: Rimuovo contatori e semplifico logica
    
    # Ottieni il filtro reparto dalla query string (solo per cassiere)
    reparto_filter = request.args.get('reparto', 'tutti')
    
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in app.config["ORDERS_CACHE"]:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Filtra ordini in base al reparto dell'utente
    if current_user.reparto:
        # Per i picker, filtra solo gli ordini del loro reparto
        ordini_con_articoli_reparto = set()
        for order in app.config["ORDERS_CACHE"]:
            if order.get("codice_reparto") == current_user.reparto:
                ordini_con_articoli_reparto.add(order["seriale"])
        
        filtered_orders = {}
        for seriale, order in unique_orders.items():
            if seriale in ordini_con_articoli_reparto:
                filtered_orders[seriale] = order
        
        unique_orders = filtered_orders
    
    # Pre-calcola gli ordini con articoli del reparto filtrato (solo se necessario)
    filtered_seriali = set()
    if not current_user.reparto and reparto_filter != 'tutti':
        for order_line in app.config.get("ORDERS_CACHE", []):
            if order_line.get("codice_reparto") == reparto_filter:
                filtered_seriali.add(order_line.get("seriale"))
    
    # OTTIMIZZAZIONE: Query batch per ordini in preparazione
    all_seriali = list(unique_orders.keys())
    
    # Carica tutti gli stati per reparto in una sola query
    reparto_statuses = {}
    if all_seriali:
        reparto_records = OrderStatusByReparto.query.filter(
            OrderStatusByReparto.seriale.in_(all_seriali)
        ).all()
        for record in reparto_records:
            if record.seriale not in reparto_statuses:
                reparto_statuses[record.seriale] = {}
            reparto_statuses[record.seriale][record.reparto] = {
                'status': record.status,
                'operatore': record.operatore,
                'timestamp': record.timestamp
            }
    
    # VERSIONE SEMPLIFICATA: Solo query database essenziali
    orders_with_status = []
    
    for order in unique_orders.values():
        # Filtro per reparto specifico (solo per cassiere)
        if not current_user.reparto and reparto_filter != 'tutti':
            if order["seriale"] not in filtered_seriali:
                continue
        
        if current_user.reparto:
            # Per i picker: controllo semplificato dello stato
            status_by_reparto = reparto_statuses.get(order["seriale"], {})
            my_reparto_status = status_by_reparto.get(current_user.reparto, {})
            if my_reparto_status.get('status') == 'in_preparazione':
                # RIMUOVO query OrderRead per alleggerire
                order["read_by"] = []  # Semplificato
                order["status"] = my_reparto_status
                orders_with_status.append(order)
        else:
            # Per i cassiere: controllo semplificato
            status_by_reparto = reparto_statuses.get(order["seriale"], {})
            reparti_ordine = get_ordine_reparti(order["seriale"])
            
            # Controlli semplificati
            has_in_preparazione = any(
                status_by_reparto.get(reparto, {}).get('status') == 'in_preparazione'
                for reparto in reparti_ordine
            )
            
            all_pronti = all(
                status_by_reparto.get(reparto, {}).get('status') == 'pronto'
                for reparto in reparti_ordine
            )
            
            if has_in_preparazione and not all_pronti:
                # RIMUOVO query OrderRead per alleggerire
                order["read_by"] = []  # Semplificato
                order["status_by_reparto"] = status_by_reparto
                order["reparti_ordine"] = reparti_ordine
                
                # Status summary semplificato
                status_summary = []
                for reparto in reparti_ordine:
                    reparto_status = status_by_reparto.get(reparto, {})
                    status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
                order["status_summary"] = " | ".join(status_summary)
                
                orders_with_status.append(order)
    
    # Ordina per data (più recente prima) e numero ordine (maggiore prima)
    # Usa la stessa funzione sort_key per gestire correttamente il cambio anno
    def sort_key(order):
        data_ordine = order.get("data_ordine")
        if data_ordine:
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        return (data_sort, num_sort)
    
    sorted_orders = sorted(orders_with_status, key=sort_key, reverse=True)
    
    # RIMUOVO COMPLETAMENTE I CONTATORI per alleggerire
    # Solo per cassiere, contatori semplificati
    reparti_counters = {}
    total_orders_in_preparation = len(sorted_orders)
    
    if not current_user.reparto:  # Solo per cassiere
        from reparti import REPARTI
        # Escludi REP06 (BOMBOLE) dai filtri
        reparti_filtri = {k: v for k, v in REPARTI.items() if k != 'REP06'}
        for reparto_code in reparti_filtri.keys():
            reparti_counters[reparto_code] = 0  # Semplificato: sempre 0
        
        # Se c'è un filtro specifico, usa il contatore semplificato
        if reparto_filter != 'tutti' and reparto_filter in reparti_counters:
            reparti_counters['selected_count'] = len(sorted_orders)
    
    return render_template("ordini_preparazione.html", 
                         orders=sorted_orders, 
                         reparto_filter=reparto_filter,
                         reparti_counters=reparti_counters,
                         total_orders_in_preparation=total_orders_in_preparation)


@app.route("/ordini/preparati")
@login_required
def ordini_preparati():
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in app.config["ORDERS_CACHE"]:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Filtra ordini in base al reparto dell'utente
    if current_user.reparto:
        # Per i picker, filtra solo gli ordini del loro reparto
        ordini_con_articoli_reparto = set()
        for order in app.config["ORDERS_CACHE"]:
            if order.get("codice_reparto") == current_user.reparto:
                ordini_con_articoli_reparto.add(order["seriale"])
        
        filtered_orders = {}
        for seriale, order in unique_orders.items():
            if seriale in ordini_con_articoli_reparto:
                filtered_orders[seriale] = order
        
        unique_orders = filtered_orders
    
    # OTTIMIZZAZIONE: Query batch per ordini preparati
    all_seriali = list(unique_orders.keys())
    
    # 1. Carica tutti gli stati per reparto in una sola query
    reparto_statuses = {}
    if all_seriali:
        reparto_records = OrderStatusByReparto.query.filter(
            OrderStatusByReparto.seriale.in_(all_seriali)
        ).all()
        for record in reparto_records:
            if record.seriale not in reparto_statuses:
                reparto_statuses[record.seriale] = {}
            reparto_statuses[record.seriale][record.reparto] = {
                'status': record.status,
                'operatore': record.operatore,
                'timestamp': record.timestamp
            }
    
    # 2. Carica tutte le informazioni di lettura in una sola query
    read_statuses = {}
    if all_seriali:
        read_records = OrderRead.query.filter(OrderRead.seriale.in_(all_seriali)).all()
        for read_record in read_records:
            if read_record.seriale not in read_statuses:
                read_statuses[read_record.seriale] = []
            read_statuses[read_record.seriale].append(read_record.operatore)
    
    # Filtra ordini in base al ruolo dell'utente
    orders_with_status = []
    for order in unique_orders.values():
        if current_user.reparto:
            # Per i picker: mostra solo ordini con status 'pronto' del loro reparto
            status_by_reparto = reparto_statuses.get(order["seriale"], {})
            my_reparto_status = status_by_reparto.get(current_user.reparto, {})
            if my_reparto_status.get('status') == 'pronto':
                # Aggiungi informazioni di lettura
                order["read_by"] = read_statuses.get(order["seriale"], [])
                order["status"] = my_reparto_status
                orders_with_status.append(order)
        else:
            # Per i cassiere: mostra solo ordini dove TUTTI i reparti sono pronti
            status_by_reparto = reparto_statuses.get(order["seriale"], {})
            reparti_ordine = get_ordine_reparti(order["seriale"])
            
            # Controlla se tutti i reparti sono pronti
            all_pronti = all(
                status_by_reparto.get(reparto, {}).get('status') == 'pronto'
                for reparto in reparti_ordine
            )
            
            # Mostra l'ordine solo se tutti i reparti sono pronti
            if all_pronti:
                # Aggiungi informazioni di lettura
                order["read_by"] = read_statuses.get(order["seriale"], [])
                order["status_by_reparto"] = status_by_reparto
                order["reparti_ordine"] = reparti_ordine
                
                # Crea un riassunto dello stato per reparto
                status_summary = []
                for reparto in reparti_ordine:
                    reparto_status = status_by_reparto.get(reparto, {})
                    status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
                order["status_summary"] = " | ".join(status_summary)
                
                orders_with_status.append(order)
    
    # Ordina per data (più recente prima) e numero ordine (maggiore prima)
    # Usa la stessa funzione sort_key per gestire correttamente il cambio anno
    def sort_key(order):
        data_ordine = order.get("data_ordine")
        if data_ordine:
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        return (data_sort, num_sort)
    
    sorted_orders = sorted(orders_with_status, key=sort_key, reverse=True)
    
    return render_template("ordini_preparati.html", orders=sorted_orders)


@app.route("/ordine/<seriale>")
@login_required
def order_detail(seriale: str):
    righe = [o for o in app.config["ORDERS_CACHE"] if o["seriale"] == seriale]
    if not righe:
        abort(404)

    # Filtra le righe in base al reparto dell'utente
    if current_user.reparto:
        righe_filtrate = [r for r in righe if r.get("codice_reparto") == current_user.reparto]
        # Se non ci sono righe per questo reparto, mostra tutte le righe ma con un avviso
        if not righe_filtrate:
            righe_filtrate = righe
    else:
        righe_filtrate = righe
    
    # Raggruppa le righe per articolo per evitare duplicati
    articoli_raggruppati = {}
    for riga in righe_filtrate:
        codice_articolo = riga.get("codice_articolo", "")
        if codice_articolo not in articoli_raggruppati:
            articoli_raggruppati[codice_articolo] = riga
        else:
            # Se l'articolo esiste già, somma le quantità
            articoli_raggruppati[codice_articolo]["quantita"] += riga.get("quantita", 0)
    
    # Converti il dizionario in lista
    righe_filtrate = list(articoli_raggruppati.values())

    # Marca righe non disponibili (per stampa in grigio)
    unavailable_by_code = {}
    with app.app_context():
        for ul in UnavailableLine.query.filter_by(seriale=seriale).all():
            if ul.unavailable:
                unavailable_by_code[ul.codice_articolo] = (ul.substitution_text or "")
    for r in righe_filtrate:
        code = r.get("codice_articolo")
        if code in unavailable_by_code:
            r["unavailable"] = True
            r["substitution_text"] = unavailable_by_code[code]

    # Aggiungi le righe modificate/cancellate dal database
    with app.app_context():
        modified_lines_db = ModifiedOrderLine.query.filter_by(seriale=seriale).all()
        unavailable_list = UnavailableLine.query.filter_by(seriale=seriale).all()

        # 1) Applica il flag "unavailable" direttamente alle righe ORIGINALI
        if unavailable_list:
            # Costruisci una mappa per codice_articolo -> testo sostituzione (se presente)
            unavail_by_code = {}
            for ul in unavailable_list:
                if ul.unavailable:
                    # per i cassieri mostriamo indipendentemente dal reparto
                    unavail_by_code.setdefault(ul.codice_articolo, ul.substitution_text or "")
            for line in righe_filtrate:
                code = line.get("codice_articolo")
                if code in unavail_by_code:
                    line["unavailable"] = True
                    line["substitution_text"] = unavail_by_code[code]

        # 2) Aggiungi SOLO righe rimosse dal DB (per storicizzare), evitando duplicati
        if modified_lines_db:
            modified_lines = []
            for db_line in modified_lines_db:
                if current_user.reparto and db_line.codice_reparto != current_user.reparto:
                    continue
                if not db_line.removed:
                    # Evita di duplicare righe non rimosse (sono già presenti dalle originali)
                    continue
                line_dict = {
                    "codice_articolo": db_line.codice_articolo,
                    "descrizione_articolo": db_line.descrizione_articolo,
                    "descrizione_supplementare": db_line.descrizione_supplementare,
                    "quantita": db_line.quantita,
                    "unita_misura": db_line.unita_misura,
                    "unita_misura_2": db_line.unita_misura_2,
                    "quantita_um2": db_line.quantita_um2,
                    "operatore_conversione": db_line.operatore_conversione,
                    "fattore_conversione": db_line.fattore_conversione,
                    "prezzo_unitario": db_line.prezzo_unitario,
                    "codice_reparto": db_line.codice_reparto,
                    "data_ordine": db_line.data_ordine,
                    "numero_ordine": db_line.numero_ordine,
                    "nome_cliente": db_line.nome_cliente,
                    "ritiro": db_line.ritiro,
                    "data_arrivo": db_line.data_arrivo,
                    "removed": True
                }
                modified_lines.append(line_dict)
            righe_filtrate.extend(modified_lines)

        # Crea righe "aggiunta" per ogni sostituzione proposta
        for ul in unavailable_list:
            if not ul.unavailable or not (ul.substitution_text or "").strip():
                continue
            # Trova UM di riferimento dalla riga originale
            base_line = next((r for r in righe if r.get("codice_articolo") == ul.codice_articolo), None)
            added_line = {
                "codice_articolo": ul.substitution_text.strip(),
                "descrizione_articolo": f"Aggiunta per sostituzione di {ul.codice_articolo}",
                "descrizione_supplementare": "Aggiunta",
                "quantita": 0,
                "unita_misura": (base_line.get("unita_misura") if base_line else "N."),
                "unita_misura_2": None,
                "quantita_um2": None,
                "codice_reparto": (base_line.get("codice_reparto") if base_line else current_user.reparto),
                "data_ordine": (base_line.get("data_ordine") if base_line else righe[0].get("data_ordine")),
                "numero_ordine": (base_line.get("numero_ordine") if base_line else righe[0].get("numero_ordine")),
                "nome_cliente": righe[0].get("nome_cliente"),
                "ritiro": righe[0].get("ritiro"),
                "data_arrivo": righe[0].get("data_arrivo"),
                "removed": False,
                "is_added": True
            }
            righe_filtrate.append(added_line)

    edits = OrderEdit.query.filter_by(seriale=seriale).all()
    edited_codes = {e.articolo for e in edits if e.applied}
    h = righe[0]
    
    # Ottieni lo stato per reparto
    status_by_reparto = get_ordine_status_by_reparto(seriale)
    reparti_ordine = get_ordine_reparti(seriale)
    
    # Per i cassiere, crea un riassunto dello stato
    status_summary = ""
    if current_user.role in ["cassiere", "cassa"]:
        status_parts = []
        for reparto in reparti_ordine:
            reparto_status = status_by_reparto.get(reparto, {})
            status_parts.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
        status_summary = " | ".join(status_parts)
    
    # Registra la lettura dell'ordine
    existing_read = OrderRead.query.filter_by(
        seriale=seriale, 
        operatore=current_user.username
    ).first()
    
    if not existing_read:
        read_record = OrderRead(
            seriale=seriale,
            operatore=current_user.username
        )
        db.session.add(read_record)
        db.session.commit()
        
        # Se è un picker e non c'è ancora uno stato, imposta "letto"
        if current_user.role == 'picker':
            existing_status = OrderStatus.query.filter_by(seriale=seriale).first()
            if not existing_status:
                new_status = OrderStatus(
                    seriale=seriale,
                    status="letto",
                    operatore=current_user.username
                )
                db.session.add(new_status)
                db.session.commit()
    
    # Vista parziali: calcola residui ed evasi se si arriva dall'elenco parzialmente evasi
    partial_view = request.args.get('back') == 'parziali'
    residual_lines = []
    delivered_lines = []
    if partial_view:
        # 1) Somma quantità richieste (originali) per codice
        original_all = [o for o in app.config.get("ORDERS_CACHE", []) if o.get("seriale") == seriale]
        if current_user.reparto:
            original_all = [o for o in original_all if o.get("codice_reparto") == current_user.reparto]
        requested_by_code = {}
        base_by_code = {}
        for o in original_all:
            code = o.get("codice_articolo")
            base_by_code[code] = base_by_code.get(code) or o
            requested_by_code[code] = requested_by_code.get(code, 0.0) + float(o.get("quantita", 0) or 0)

        # 2) Somma quantità confermate (edits applied) per codice, filtrando per reparto quando possibile
        delivered_by_code = {}
        applied_edits = OrderEdit.query.filter_by(seriale=seriale, applied=True).all()
        for e in applied_edits:
            # Tenta di ricavare reparto dalla riga base
            base = next((o for o in original_all if o.get("codice_articolo") == e.articolo), None)
            if current_user.reparto and base and base.get("codice_reparto") != current_user.reparto:
                continue
            delivered_by_code[e.articolo] = delivered_by_code.get(e.articolo, 0.0) + float(e.quantita_nuova)

        # 3) Costruisci liste
        for code, req in requested_by_code.items():
            deliv = delivered_by_code.get(code, 0.0)
            residuo = max(0.0, float(req) - float(deliv))
            b = base_by_code.get(code, {})
            if residuo > 0.0001:
                residual_lines.append({
                    "codice_articolo": code,
                    "descrizione_articolo": b.get("descrizione_articolo", ""),
                    "quantita": residuo,
                    "unita_misura": b.get("unita_misura", "")
                })
            if deliv > 0.0001:
                delivered_lines.append({
                    "codice_articolo": code,
                    "descrizione_articolo": b.get("descrizione_articolo", ""),
                    "quantita": deliv,
                    "unita_misura": b.get("unita_misura", "")
                })
        # Aggiungi eventuali articoli consegnati che non esistono tra i richiesti (aggiunte/sostituzioni)
        for code, deliv in delivered_by_code.items():
            if code not in requested_by_code and deliv > 0.0001:
                delivered_lines.append({
                    "codice_articolo": code,
                    "descrizione_articolo": code,
                    "quantita": deliv,
                    "unita_misura": ""
                })
    
    # Ottieni lo stato corrente dell'ordine
    order_status = OrderStatus.query.filter_by(seriale=seriale).first()

    return render_template(
        "order_detail.html",
        seriale=seriale,
        numero=h["numero_ordine"],
        date=h["data_ordine"],
        customer=h.get("nome_cliente", "Cliente non disponibile"),
        lines=righe_filtrate,
        edits=edits,
        edited_articoli=edited_codes,
        order_status=order_status.status if order_status else None,
        user_reparto=current_user.reparto,
        reparto_nome=get_reparto_by_code(current_user.reparto) if current_user.reparto else None,
        status_by_reparto=status_by_reparto,
        reparti_ordine=reparti_ordine,
        status_summary=status_summary,
        partial_view=partial_view,
        residual_lines=residual_lines,
        delivered_lines=delivered_lines,
        original_lines=righe_filtrate,
        in_da_completare=(PartialOrderResidue.query.filter_by(seriale=seriale, reparto=current_user.reparto).first() is not None) if current_user.reparto else (PartialOrderResidue.query.filter_by(seriale=seriale).first() is not None),
    )


@app.route("/ordine/<seriale>/print")
@login_required
def print_order(seriale: str):
    """Pagina di stampa dell'ordine - solo per cassiere"""
    if current_user.role not in ["cassiere", "cassa"]:
        abort(403)
    
    righe = [o for o in app.config["ORDERS_CACHE"] if o["seriale"] == seriale]
    if not righe:
        abort(404)
    
    # Raggruppa le righe per articolo per evitare duplicati
    articoli_raggruppati = {}
    for riga in righe:
        codice_articolo = riga.get("codice_articolo", "")
        if codice_articolo not in articoli_raggruppati:
            articoli_raggruppati[codice_articolo] = riga
        else:
            # Se l'articolo esiste già, somma le quantità
            articoli_raggruppati[codice_articolo]["quantita"] += riga.get("quantita", 0)
    
    # Converti il dizionario in lista
    righe_filtrate = list(articoli_raggruppati.values())
    
    # Applica le modifiche finali per ogni articolo e includi eventuali "aggiunte"
    # Usa SEMPRE l'ultima modifica/conferma per ogni articolo
    edits = (
        OrderEdit.query
        .filter_by(seriale=seriale, applied=True)
        .order_by(OrderEdit.timestamp.desc())
        .all()
    )
    edits = (
        OrderEdit.query
        .filter_by(seriale=seriale, applied=True)
        .order_by(OrderEdit.timestamp.desc())
        .all()
    )
    edited_articoli = {e.articolo: e for e in edits}
    
    # 1) Aggiorna righe esistenti
    for riga in righe_filtrate:
        codice_articolo = riga.get("codice_articolo", "")
        if codice_articolo in edited_articoli:
            edit = edited_articoli[codice_articolo]
            riga["quantita"] = edit.quantita_nuova
            riga["unita_misura"] = edit.unita_misura

    # 2) Aggiungi righe per articoli presenti nelle modifiche ma assenti nell'estratto base
    existing_codes = {r.get("codice_articolo", "") for r in righe_filtrate}
    added_codes = []
    for codice, edit in edited_articoli.items():
        if codice not in existing_codes:
            added_line = {
                "codice_articolo": codice,
                "descrizione_articolo": f"Aggiunta articolo {codice}",
                "descrizione_supplementare": "",
                "quantita": edit.quantita_nuova,
                "unita_misura": edit.unita_misura,
                "unita_misura_2": None,
                "quantita_um2": None,
                "operatore_conversione": None,
                "fattore_conversione": None,
                "codice_reparto": None,
                "data_ordine": None,
                "numero_ordine": None,
                "nome_cliente": None,
                "ritiro": None,
                "data_arrivo": None,
            }
            righe_filtrate.append(added_line)
            added_codes.append(codice)
    
    h = righe[0]
    
    return render_template(
        "print_order.html",
        seriale=seriale,
        numero=h["numero_ordine"],
        date=h["data_ordine"],
        customer=h.get("nome_cliente", "Cliente non disponibile"),
        lines=righe_filtrate,
        data_arrivo=h.get("data_arrivo", ""),
        ritiro=h.get("ritiro", ""),
        datetime=datetime,
        added_codes=added_codes
    )





# ---------- EDIT / CONFIRM --------------
@app.route("/ordine/<seriale>/confirm", methods=["POST"])
@login_required
def propose_confirm(seriale: str):
    if current_user.role != "picker":
        abort(403)
    
    # OTTIMIZZAZIONE: Crea edit e auto-start in un'unica transazione
    try:
        edit = OrderEdit(
            seriale=seriale,
            articolo=request.form["articolo"],
            quantita_nuova=float(request.form["quantita"]),
            unita_misura=request.form["unita"],
            operatore=current_user.username,
            applied=True,
        )
        db.session.add(edit)
        
        # Auto-start preparation quando un picker interagisce con l'ordine
        if current_user.role == "picker" and current_user.reparto:
            auto_start_preparation(seriale, current_user.username, current_user.reparto)
        
        # OTTIMIZZAZIONE: Un solo commit per tutto
        db.session.commit()
        
        # OTTIMIZZAZIONE: Solo flag cache, no refresh completo
        if hasattr(app, 'config') and 'ORDERS_CACHE' in app.config:
            app.config['CACHE_MODIFIED'] = True
            # print(f"✅ Quantità confermata: {edit.quantita_nuova} {edit.unita_misura} per articolo {edit.articolo}")  # Rimosso per ridurre log
            # print(f"🔄 Cache marcata come modificata per ordine {seriale}")  # Rimosso per ridurre log
        
        back = request.form.get("back")
        if back:
            return redirect(url_for("order_detail", seriale=seriale, back=back))
        return redirect(url_for("order_detail", seriale=seriale))
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Errore in propose_confirm: {e}")
        return redirect(url_for("order_detail", seriale=seriale))


@app.route("/ordine/<seriale>/edit", methods=["POST"])
@login_required
def propose_edit(seriale: str):
    if current_user.role != "picker":
        abort(403)
    
    # OTTIMIZZAZIONE: Crea edit e auto-start in un'unica transazione
    try:
        edit = OrderEdit(
            seriale=seriale,
            articolo=request.form["articolo"],
            quantita_nuova=float(request.form["quantita_nuova"]),
            unita_misura=request.form["unita_misura"],
            operatore=current_user.username,
            applied=True,
        )
        db.session.add(edit)
        
        # Gestisci la nota se presente
        nota = request.form.get("nota", "").strip()
        if nota:
            order_note = OrderNote(
                seriale=seriale,
                articolo=request.form["articolo"],
                operatore=current_user.username,
                nota=nota
            )
            db.session.add(order_note)
        
        # Auto-start preparation quando un picker interagisce con l'ordine
        if current_user.role == "picker" and current_user.reparto:
            auto_start_preparation(seriale, current_user.username, current_user.reparto)
        
        # OTTIMIZZAZIONE: Un solo commit per tutto
        db.session.commit()
        
        # OTTIMIZZAZIONE: Solo flag cache, no refresh completo
        if hasattr(app, 'config') and 'ORDERS_CACHE' in app.config:
            app.config['CACHE_MODIFIED'] = True
            # print(f"✅ Quantità modificata: {edit.quantita_nuova} {edit.unita_misura} per articolo {edit.articolo}")  # Rimosso per ridurre log
            # print(f"🔄 Cache marcata come modificata per ordine {seriale}")  # Rimosso per ridurre log
        
        back = request.form.get("back")
        if back:
            return redirect(url_for("order_detail", seriale=seriale, back=back))
        return redirect(url_for("order_detail", seriale=seriale))
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Errore in propose_edit: {e}")
        return redirect(url_for("order_detail", seriale=seriale))


# ---------- API FOR AUTO-PREPARATION --------------
@app.route("/api/ordine/<seriale>/auto-start-preparation", methods=["POST"])
@login_required
def api_auto_start_preparation(seriale: str):
    """API per mettere automaticamente un ordine in preparazione"""
    if current_user.role != "picker" or not current_user.reparto:
        abort(403)
    
    try:
        auto_start_preparation(seriale, current_user.username, current_user.reparto)
        return jsonify({"success": True, "message": "Ordine messo in preparazione"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ---------- HELPER FUNCTIONS --------------
def auto_start_preparation(seriale: str, operatore: str, reparto: str = None):
    """Mette automaticamente un ordine in preparazione quando un picker interagisce con esso"""
    try:
        if reparto:
            # Aggiorna o crea lo stato dell'ordine per il reparto specifico
            existing_status = OrderStatusByReparto.query.filter_by(
                seriale=seriale, 
                reparto=reparto
            ).first()
            
            if existing_status:
                # Aggiorna lo stato esistente solo se non è già in preparazione o pronto
                if existing_status.status not in ['in_preparazione', 'pronto']:
                    existing_status.status = 'in_preparazione'
                    existing_status.operatore = operatore
                    existing_status.timestamp = db.func.now()
            else:
                # Crea nuovo stato per il reparto
                new_status = OrderStatusByReparto(
                    seriale=seriale,
                    reparto=reparto,
                    status='in_preparazione',
                    operatore=operatore
                )
                db.session.add(new_status)
            
            # OTTIMIZZAZIONE: Aggiorna stato generale in modo semplificato
            general_status = OrderStatus.query.filter_by(seriale=seriale).first()
            if general_status:
                # Se non è già pronto, metti in preparazione
                if general_status.status != 'pronto':
                    general_status.status = 'in_preparazione'
                    general_status.operatore = operatore
                    general_status.timestamp = db.func.now()
            else:
                # Crea nuovo stato generale
                new_general_status = OrderStatus(
                    seriale=seriale,
                    status='in_preparazione',
                    operatore=operatore
                )
                db.session.add(new_general_status)
            
            db.session.commit()
            # print(f"✅ Auto-start preparation: Ordine {seriale} messo in preparazione per reparto {reparto}")  # Rimosso per ridurre log
            
    except Exception as e:
        print(f"❌ Errore in auto_start_preparation: {e}")
        db.session.rollback()

# ---------- ORDER STATUS --------------
@app.route("/ordine/<seriale>/status", methods=["POST"])
@login_required
def update_order_status(seriale: str):
    if current_user.role != "picker":
        abort(403)
    
    status = request.form.get("status")
    if status not in ["in_preparazione", "pronto"]:
        abort(400)
    
    # Se l'utente ha un reparto, aggiorna lo stato per quel reparto specifico
    if current_user.reparto:
        # Aggiorna o crea lo stato dell'ordine per il reparto specifico
        existing_status = OrderStatusByReparto.query.filter_by(
            seriale=seriale, 
            reparto=current_user.reparto
        ).first()
        
        if existing_status:
            existing_status.status = status
            existing_status.operatore = current_user.username
            existing_status.timestamp = db.func.now()
        else:
            new_status = OrderStatusByReparto(
                seriale=seriale,
                reparto=current_user.reparto,
                status=status,
                operatore=current_user.username
            )
            db.session.add(new_status)
        
        # OTTIMIZZAZIONE: Query batch per stati reparto
        reparti_ordine = get_ordine_reparti(seriale)
        
        # Carica tutti gli stati per reparto in una sola query
        status_by_reparto = {}
        if reparti_ordine:
            reparto_records = OrderStatusByReparto.query.filter(
                OrderStatusByReparto.seriale == seriale,
                OrderStatusByReparto.reparto.in_(reparti_ordine)
            ).all()
            for record in reparto_records:
                status_by_reparto[record.reparto] = {
                    'status': record.status,
                    'operatore': record.operatore,
                    'timestamp': record.timestamp
                }
        
        # Controlla se tutti i reparti sono pronti
        tutti_pronti = all(
            status_by_reparto.get(reparto, {}).get('status') == 'pronto' 
            for reparto in reparti_ordine
        )
        
        # Aggiorna lo stato generale dell'ordine
        general_status = OrderStatus.query.filter_by(seriale=seriale).first()
        if general_status:
            if tutti_pronti:
                general_status.status = 'pronto'
                # print(f"🎉 Ordine {seriale} COMPLETAMENTE PRONTO!")  # Rimosso per ridurre log
            elif status == 'in_preparazione':
                general_status.status = 'in_preparazione'
            # rimosso: lo stato "materiale_non_disponibile" non viene più tracciato a livello ordine
            general_status.operatore = current_user.username
            general_status.timestamp = db.func.now()
        else:
            # Crea nuovo stato generale se non esiste
            new_general_status = OrderStatus(
                seriale=seriale,
                status='pronto' if tutti_pronti else 'in_preparazione',
                operatore=current_user.username
            )
            db.session.add(new_general_status)
            if tutti_pronti:
                pass
                # print(f"🎉 Nuovo ordine {seriale} COMPLETAMENTE PRONTO!")  # Rimosso per ridurre log
        
        db.session.commit()
        
        # Forza refresh della cache per aggiornare immediatamente l'interfaccia
        # print(f"✅ Stato aggiornato: Ordine {seriale} -> {status} per reparto {current_user.reparto}")  # Rimosso per ridurre log
        if tutti_pronti:
            pass
            # print(f"🎉 Ordine {seriale} COMPLETAMENTE PRONTO!")  # Rimosso per ridurre log
        
        # OTTIMIZZAZIONE: Cache refresh mirato invece di refresh completo
        if hasattr(app, 'config') and 'ORDERS_CACHE' in app.config:
            # Marca la cache come modificata per forzare il refresh
            app.config['CACHE_MODIFIED'] = True
            # print(f"🔄 Cache marcata come modificata per ordine {seriale}")  # Rimosso per ridurre log

        # Se il reparto imposta PRONTO, calcola e salva i residui parziali per quel reparto
        if status == 'pronto':
            try:
                # Calcola quantità richieste originali per reparto
                original_lines = [o for o in app.config.get("ORDERS_CACHE", []) if o.get("seriale") == seriale and o.get("codice_reparto") == current_user.reparto]
                requested_by_code = {}
                header = None
                for r in original_lines:
                    header = header or r
                    code = r.get("codice_articolo")
                    requested_by_code[code] = requested_by_code.get(code, 0) + float(r.get("quantita", 0) or 0)

                # Somma quantità confermate (edits applied)
                confirmed_by_code = {}
                for e in OrderEdit.query.filter_by(seriale=seriale, applied=True).all():
                    mol = ModifiedOrderLine.query.filter_by(seriale=seriale, codice_articolo=e.articolo).first()
                    # Considera solo righe del reparto attuale se note
                    if current_user.reparto and mol and mol.codice_reparto and mol.codice_reparto != current_user.reparto:
                        continue
                    confirmed_by_code[e.articolo] = confirmed_by_code.get(e.articolo, 0) + float(e.quantita_nuova)

                # Pulisci snapshot precedente per questo reparto/seriale
                PartialOrderResidue.query.filter_by(seriale=seriale, reparto=current_user.reparto).delete()

                # Crea record di residuo > 0
                for code, requested in requested_by_code.items():
                    confirmed = confirmed_by_code.get(code, 0.0)
                    residuo = max(0.0, float(requested) - float(confirmed))
                    if residuo > 0.0001:
                        # Recupera descrizione
                        base = next((r for r in original_lines if r.get("codice_articolo") == code), None)
                        db.session.add(PartialOrderResidue(
                            seriale=seriale,
                            reparto=current_user.reparto,
                            numero_ordine=header.get("numero_ordine") if header else None,
                            nome_cliente=header.get("nome_cliente") if header else None,
                            codice_articolo=code,
                            descrizione_articolo=(base or {}).get("descrizione_articolo", ""),
                            residuo_quantita=residuo,
                            unita_misura=(base or {}).get("unita_misura", "")
                        ))
            except Exception as calc_err:
                print("[partial_residues] errore calcolo:", calc_err)
    else:
        # Per utenti senza reparto (cassiere), aggiorna solo lo stato generale
        existing_status = OrderStatus.query.filter_by(seriale=seriale).first()
        if existing_status:
            existing_status.status = status
            existing_status.operatore = current_user.username
            existing_status.timestamp = db.func.now()
        else:
            new_status = OrderStatus(
                seriale=seriale,
                status=status,
                operatore=current_user.username
            )
            db.session.add(new_status)
    
    db.session.commit()
    return redirect(url_for("order_detail", seriale=seriale))


# --- Materiale Non Disponibile per righe ---
@app.route("/ordine/<seriale>/unavailable", methods=["POST"])
@login_required
def mark_lines_unavailable(seriale: str):
    """Segna righe come non disponibili con testo sostituzione. Solo picker del reparto."""
    if current_user.role != "picker":
        abort(403)

    try:
        payload = request.get_json(force=True)
    except Exception:
        payload = None
    if not isinstance(payload, list):
        return jsonify({"success": False, "error": "Payload non valido"}), 400

    articoli_reparto = set()
    for riga in app.config.get("ORDERS_CACHE", []):
        if riga.get("seriale") == seriale and (not current_user.reparto or riga.get("codice_reparto") == current_user.reparto):
            articoli_reparto.add(riga.get("codice_articolo"))

    updated = 0
    for item in payload:
        articolo = item.get("articolo")
        if not articolo or articolo not in articoli_reparto:
            continue
        substitution_text = (item.get("substitution_text") or "").strip()
        unavailable = bool(item.get("unavailable"))

        mol = ModifiedOrderLine.query.filter_by(seriale=seriale, codice_articolo=articolo).first()
        if not mol:
            base = next((r for r in app.config.get("ORDERS_CACHE", []) if r.get("seriale") == seriale and r.get("codice_articolo") == articolo), None)
            if not base:
                continue
            mol = ModifiedOrderLine(
                seriale=seriale,
                codice_articolo=articolo,
                descrizione_articolo=base.get("descrizione_articolo", ""),
                descrizione_supplementare=base.get("descrizione_supplementare", ""),
                quantita=base.get("quantita", 0),
                unita_misura=base.get("unita_misura", ""),
                unita_misura_2=base.get("unita_misura_2"),
                quantita_um2=base.get("quantita_um2"),
                codice_reparto=base.get("codice_reparto"),
                data_ordine=base.get("data_ordine"),
                numero_ordine=base.get("numero_ordine"),
                nome_cliente=base.get("nome_cliente"),
                ritiro=base.get("ritiro"),
                data_arrivo=base.get("data_arrivo"),
                removed=False,
            )
            db.session.add(mol)

        # Salva record dedicato per disponibilità/sostituzione (persistente per cassiere)
        un = UnavailableLine.query.filter_by(seriale=seriale, codice_articolo=articolo, reparto=current_user.reparto).first()
        if not un:
            un = UnavailableLine(seriale=seriale, codice_articolo=articolo, reparto=current_user.reparto)
            db.session.add(un)
        un.unavailable = unavailable
        un.substitution_text = substitution_text if unavailable else None
        updated += 1

    if updated > 0:
        # Non aggiorniamo più lo stato a livello ordine per "materiale_non_disponibile".
        # La segnalazione è gestita a livello di riga tramite `UnavailableLine`.
        pass

    # Auto-start preparation quando un picker interagisce con l'ordine
    if current_user.role == "picker" and current_user.reparto:
        auto_start_preparation(seriale, current_user.username, current_user.reparto)

    db.session.commit()
    return jsonify({"success": True, "updated": updated})

# ------- EDITS DASHBOARD ---------------
@app.route("/edits")
@login_required
def edits_list():
    edits = OrderEdit.query.order_by(OrderEdit.timestamp.desc()).all()
    return render_template("edits.html", edits=edits)


# ------- ORDINI DA COMPLETARE -------
@app.route("/ordini-da-completare")
@login_required
def ordini_da_completare():
    """Lista ordini da completare (gestita manualmente dal picker).
    Auto-rimuove gli ordini completamente evasi (MVFLEVAS='S').
    """
    query = PartialOrderResidue.query
    if current_user.role == 'picker' and current_user.reparto:
        query = query.filter_by(reparto=current_user.reparto)
    residues = query.order_by(PartialOrderResidue.created_at.desc()).all()

    # Auto cleanup: se tutte le righe risultano evase nel gestionale per quel seriale/reparto, rimuovi il marker
    markers_to_remove = []
    for marker in list(residues):
        try:
            # Salva i valori PRIMA di accedere all'oggetto per evitare ObjectDeletedError
            try:
                seriale = marker.seriale
                reparto = marker.reparto
                marker_id = getattr(marker, 'id', 'unknown')
            except Exception as attr_error:
                print(f"⚠️ Errore accesso attributi marker: {attr_error}")
                markers_to_remove.append(marker)
                continue
            
            righe_seriale = [o for o in app.config.get("ORDERS_CACHE", []) if o.get("seriale") == seriale]
            if reparto:
                righe_seriale = [o for o in righe_seriale if o.get("codice_reparto") == reparto]
            if not righe_seriale:
                continue
            
            # RIMOSSO: Controllo flag evasione (non più necessario)
            tutte_evase = False  # Sempre False per evitare auto-rimozione
            if tutte_evase:
                try:
                    PartialOrderResidue.query.filter_by(seriale=seriale, reparto=reparto).delete()
                    db.session.commit()
                    markers_to_remove.append(marker)
                except Exception:
                    db.session.rollback()
        except Exception as e:
            print(f"⚠️ Errore nel cleanup marker {marker_id}: {e}")
            # Rimuovi il marker problematico dalla lista
            markers_to_remove.append(marker)
            continue
    
    # Rimuovi i marker processati
    for marker in markers_to_remove:
        if marker in residues:
            residues.remove(marker)

    grouped = {}
    for r in residues:
        if r.seriale not in grouped:
            grouped[r.seriale] = {
                'seriale': r.seriale,
                'reparto': r.reparto,
                'numero_ordine': r.numero_ordine,
                'nome_cliente': r.nome_cliente,
                'righe': []
            }
        grouped[r.seriale]['righe'].append({
            'codice_articolo': r.codice_articolo,
            'descrizione_articolo': r.descrizione_articolo,
            'residuo_quantita': r.residuo_quantita,
            'unita_misura': r.unita_misura,
        })

    return render_template('parzialmente_evasi.html', orders=list(grouped.values()))


@app.route("/ordini-da-completare/<seriale>/add", methods=["POST"])
@login_required
def add_to_ordini_da_completare(seriale: str):
    """Aggiunge manualmente un ordine alla lista 'da completare' per il reparto dell'operatore."""
    if current_user.role != 'picker':
        abort(403)

    # Evita duplicati per (seriale, reparto)
    exists = PartialOrderResidue.query.filter_by(seriale=seriale, reparto=current_user.reparto).first()
    if not exists:
        # Trova header ordine dalla cache
        header = next((o for o in app.config.get("ORDERS_CACHE", []) if o.get("seriale") == seriale), None)
        numero = header.get("numero_ordine") if header else None
        cliente = header.get("nome_cliente") if header else None
        marker = PartialOrderResidue(
            seriale=seriale,
            reparto=current_user.reparto,
            numero_ordine=numero,
            nome_cliente=cliente,
            codice_articolo="__MARKER__",
            descrizione_articolo="Ordine da completare",
            residuo_quantita=0.0,
            unita_misura=""
        )
        db.session.add(marker)
        db.session.commit()

    return redirect(url_for('ordini_da_completare'))


@app.route("/ordini-da-completare/<seriale>/remove", methods=["POST"])
@login_required
def remove_from_ordini_da_completare(seriale: str):
    """Rimuove un ordine dalla lista 'da completare'."""
    if current_user.role != 'picker' and current_user.role not in ['cassiere', 'cassa']:
        abort(403)

    query = PartialOrderResidue.query.filter_by(seriale=seriale)
    if current_user.role == 'picker' and current_user.reparto:
        query = query.filter_by(reparto=current_user.reparto)
    query.delete()
    db.session.commit()
    return redirect(url_for('ordini_da_completare'))
# ------- CHAT SYSTEM ---------------
@app.route("/chat")
@login_required
def chat():
    # Ottieni tutti gli utenti per la selezione del destinatario
    users = User.query.all()
    return render_template("chat.html", users=users)


@app.route("/api/chat/messages/<recipient>")
@login_required
def get_chat_messages(recipient):
    # Ottieni messaggi tra current_user e recipient
    messages = ChatMessage.query.filter(
        ((ChatMessage.sender == current_user.username) & (ChatMessage.recipient == recipient)) |
        ((ChatMessage.sender == recipient) & (ChatMessage.recipient == current_user.username))
    ).order_by(ChatMessage.timestamp.asc()).all()
    
    # Marca i messaggi ricevuti come letti
    unread_messages = ChatMessage.query.filter_by(
        recipient=current_user.username, 
        sender=recipient, 
        read=False
    ).all()
    
    for msg in unread_messages:
        msg.read = True
    db.session.commit()
    
    return jsonify([{
        'id': msg.id,
        'sender': msg.sender,
        'recipient': msg.recipient,
        'message': msg.message,
        'timestamp': msg.timestamp.isoformat(),
        'read': msg.read
    } for msg in messages])


@app.route("/api/chat/send", methods=["POST"])
@login_required
def send_message():
    recipient = request.form.get("recipient")
    message = request.form.get("message")
    
    if not recipient or not message:
        return jsonify({"error": "Destinatario e messaggio sono obbligatori"}), 400
    
    # Verifica che il destinatario esista
    recipient_user = User.query.filter_by(username=recipient).first()
    if not recipient_user:
        return jsonify({"error": "Destinatario non trovato"}), 404
    
    # Crea il messaggio
    chat_message = ChatMessage(
        sender=current_user.username,
        recipient=recipient,
        message=message
    )
    db.session.add(chat_message)
    db.session.commit()
    
    return jsonify({"success": True, "id": chat_message.id})


@app.route("/api/chat/unread")
@login_required
def get_unread_count():
    count = ChatMessage.query.filter_by(
        recipient=current_user.username, 
        read=False
    ).count()
    return jsonify({"count": count})


@app.route("/api/chat/received")
@login_required
def get_received_messages():
    # Ottieni tutti i messaggi ricevuti dall'utente corrente
    messages = ChatMessage.query.filter_by(
        recipient=current_user.username
    ).order_by(ChatMessage.timestamp.asc()).all()
    
    # Marca i messaggi come letti
    for msg in messages:
        if not msg.read:
            msg.read = True
    db.session.commit()
    
    return jsonify([{
        'id': msg.id,
        'sender': msg.sender,
        'recipient': msg.recipient,
        'message': msg.message,
        'timestamp': msg.timestamp.isoformat(),
        'read': msg.read
    } for msg in messages])


# ------- ORDER PREVIEW ---------------
@app.route("/api/order/<seriale>/preview")
@login_required
def get_order_preview(seriale):
    """Ottiene un'anteprima veloce delle righe ordine (solo per cassiere)"""
    # Solo per cassiere
    if current_user.reparto:
        return jsonify({"error": "Accesso negato"}), 403
    
    # Ottieni le righe ordine dalla cache
    righe = [o for o in app.config.get("ORDERS_CACHE", []) if o.get("seriale") == seriale]
    
    if not righe:
        return jsonify({"error": "Ordine non trovato"}), 404
    
    # NON raggruppare, mostra TUTTE le righe così come sono
    righe_preview = []
    for riga in righe:
        righe_preview.append({
            'codice_articolo': riga.get("codice_articolo", ""),
            'descrizione_articolo': riga.get("descrizione_articolo", ""),
            'descrizione_supplementare': riga.get("descrizione_supplementare", ""),
            'quantita': riga.get("quantita", 0),
            'unita_misura': riga.get("unita_misura", "")
        })
    
    return jsonify({
        'seriale': seriale,
        'numero_ordine': righe[0].get('numero_ordine') if righe else '',
        'nome_cliente': righe[0].get('nome_cliente') if righe else '',
        'righe': righe_preview
    })


# ------- ORDER NOTES ---------------
@app.route("/api/order/<seriale>/notes")
@login_required
def get_order_notes(seriale):
    # Per i picker, filtra solo le note del loro reparto
    if current_user.reparto:
        # Ottieni gli articoli del reparto dell'utente per questo ordine
        articoli_reparto = set()
        for order in app.config.get("ORDERS_CACHE", []):
            if order.get("seriale") == seriale and order.get("codice_reparto") == current_user.reparto:
                articoli_reparto.add(order.get("codice_articolo"))
        
        # Filtra le note: solo quelle per articoli del reparto o note generali dell'ordine
        notes = OrderNote.query.filter_by(seriale=seriale).order_by(OrderNote.timestamp.desc()).all()
        filtered_notes = []
        for note in notes:
            # Includi note generali (senza articolo) o note per articoli del reparto
            if not note.articolo or note.articolo in articoli_reparto:
                filtered_notes.append(note)
        notes = filtered_notes
    else:
        # Per i cassiere, mostra tutte le note
        notes = OrderNote.query.filter_by(seriale=seriale).order_by(OrderNote.timestamp.desc()).all()
    
    return jsonify([{
        'id': note.id,
        'seriale': note.seriale,
        'articolo': note.articolo,
        'operatore': note.operatore,
        'nota': note.nota,
        'timestamp': note.timestamp.isoformat()
    } for note in notes])


@app.route("/api/order/<seriale>/read", methods=["POST"])
@login_required
def mark_order_read(seriale):
    """Marca un ordine come letto dall'operatore corrente"""
    # Verifica che l'ordine esista
    order_exists = any(o["seriale"] == seriale for o in app.config["ORDERS_CACHE"])
    if not order_exists:
        return jsonify({"error": "Ordine non trovato"}), 404
    
    # Verifica che il picker abbia accesso a questo ordine
    if current_user.reparto:
        has_access = any(
            o["seriale"] == seriale and o.get("codice_reparto") == current_user.reparto 
            for o in app.config["ORDERS_CACHE"]
        )
        if not has_access:
            return jsonify({"error": "Accesso negato"}), 403
    
    # Controlla se già letto
    existing_read = OrderRead.query.filter_by(
        seriale=seriale, 
        operatore=current_user.username
    ).first()
    
    if not existing_read:
        # Crea nuovo record di lettura
        order_read = OrderRead(
            seriale=seriale,
            operatore=current_user.username
        )
        db.session.add(order_read)
        db.session.commit()
    
    return jsonify({"success": True})


@app.route("/api/order/<seriale>/notes", methods=["POST"])
@login_required
def add_order_note(seriale):
    articolo = request.form.get("articolo")  # può essere None per note dell'ordine
    nota = request.form.get("nota")
    
    if not nota:
        return jsonify({"error": "La nota è obbligatoria"}), 400
    
    # Verifica che l'ordine esista
    order_exists = any(o["seriale"] == seriale for o in app.config["ORDERS_CACHE"])
    if not order_exists:
        return jsonify({"error": "Ordine non trovato"}), 404
    
    # Crea la nota
    order_note = OrderNote(
        seriale=seriale,
        articolo=articolo,
        operatore=current_user.username,
        nota=nota
    )
    db.session.add(order_note)
    db.session.commit()
    
    return jsonify({
        "success": True, 
        "id": order_note.id,
        "timestamp": order_note.timestamp.isoformat()
    })


# ------- ORDER ATTACHMENTS ---------------
@app.route("/api/order/<seriale>/attachments")
@login_required
def get_order_attachments(seriale):
    """Ottiene tutti gli allegati di un ordine"""
    # Per i picker, filtra solo gli allegati del loro reparto
    if current_user.reparto:
        # Ottieni gli articoli del reparto dell'utente per questo ordine
        articoli_reparto = set()
        for order in app.config.get("ORDERS_CACHE", []):
            if order.get("seriale") == seriale and order.get("codice_reparto") == current_user.reparto:
                articoli_reparto.add(order.get("codice_articolo"))
        
        # Filtra gli allegati: solo quelli per articoli del reparto o allegati generali dell'ordine
        attachments = OrderAttachment.query.filter_by(seriale=seriale).order_by(OrderAttachment.timestamp.desc()).all()
        filtered_attachments = []
        for att in attachments:
            # Includi allegati generali (senza articolo) o allegati per articoli del reparto
            if not att.articolo or att.articolo in articoli_reparto:
                filtered_attachments.append(att)
        attachments = filtered_attachments
    else:
        # Per i cassiere, mostra tutti gli allegati
        attachments = OrderAttachment.query.filter_by(seriale=seriale).order_by(OrderAttachment.timestamp.desc()).all()
    
    return jsonify([{
        'id': att.id,
        'seriale': att.seriale,
        'articolo': att.articolo,
        'filename': att.filename,
        'original_filename': att.original_filename,
        'file_size': att.file_size,
        'mime_type': att.mime_type,
        'operatore': att.operatore,
        'note': att.note,
        'timestamp': att.timestamp.isoformat(),
        'download_url': url_for('download_attachment', attachment_id=att.id)
    } for att in attachments])


@app.route("/api/order/<seriale>/attachments", methods=["POST"])
@login_required
def upload_attachment(seriale):
    """Carica un allegato per un ordine"""
    # Verifica che l'ordine esista
    order_exists = any(o["seriale"] == seriale for o in app.config["ORDERS_CACHE"])
    if not order_exists:
        return jsonify({"error": "Ordine non trovato"}), 404
    
    # Verifica che sia stato inviato un file
    if 'file' not in request.files:
        return jsonify({"error": "Nessun file inviato"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Nessun file selezionato"}), 400
    
    # Verifica estensioni permesse
    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'txt'}
    if not ('.' in file.filename and 
            file.filename.rsplit('.', 1)[1].lower() in allowed_extensions):
        return jsonify({"error": "Tipo di file non permesso"}), 400
    
    try:
        # Crea la directory per gli allegati se non esiste
        import os
        from werkzeug.utils import secure_filename
        
        upload_folder = Path(__file__).parent / "uploads" / "attachments"
        upload_folder.mkdir(parents=True, exist_ok=True)
        
        # Genera un nome file sicuro
        original_filename = secure_filename(file.filename)
        filename = f"{seriale}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{original_filename}"
        file_path = upload_folder / filename
        
        # Salva il file
        file.save(str(file_path))
        
        # Ottieni informazioni sul file
        file_size = os.path.getsize(file_path)
        mime_type = file.content_type or 'application/octet-stream'
        
        # Ottieni parametri opzionali
        articolo = request.form.get("articolo")  # può essere None per allegati dell'ordine
        note = request.form.get("note")
        
        # Salva nel database
        attachment = OrderAttachment(
            seriale=seriale,
            articolo=articolo,
            filename=filename,
            original_filename=original_filename,
            file_path=str(file_path),
            file_size=file_size,
            mime_type=mime_type,
            operatore=current_user.username,
            note=note
        )
        db.session.add(attachment)
        db.session.commit()
        
        return jsonify({
            "success": True,
            "id": attachment.id,
            "filename": original_filename,
            "file_size": file_size,
            "timestamp": attachment.timestamp.isoformat()
        })
        
    except Exception as e:
        return jsonify({"error": f"Errore nel caricamento: {str(e)}"}), 500


@app.route("/api/attachments/<int:attachment_id>/download")
@login_required
def download_attachment(attachment_id):
    """Scarica un allegato"""
    attachment = OrderAttachment.query.get_or_404(attachment_id)
    
    # Verifica che l'utente abbia accesso all'ordine
    order_exists = any(o["seriale"] == attachment.seriale for o in app.config["ORDERS_CACHE"])
    if not order_exists:
        abort(404)
    
    file_path = Path(attachment.file_path)
    if not file_path.exists():
        abort(404)
    
    return send_file(
        file_path,
        as_attachment=True,
        download_name=attachment.original_filename,
        mimetype=attachment.mime_type
    )


@app.route("/api/attachments/<int:attachment_id>", methods=["DELETE"])
@login_required
def delete_attachment(attachment_id):
    """Elimina un allegato"""
    attachment = OrderAttachment.query.get_or_404(attachment_id)
    
    # Verifica che l'utente sia l'autore dell'allegato o un cassiere
    if (attachment.operatore != current_user.username and 
        current_user.role not in ["cassiere", "cassa"]):
        abort(403)
    
    try:
        # Elimina il file fisico
        file_path = Path(attachment.file_path)
        if file_path.exists():
            file_path.unlink()
        
        # Elimina dal database
        db.session.delete(attachment)
        db.session.commit()
        
        return jsonify({"success": True})
        
    except Exception as e:
        return jsonify({"error": f"Errore nell'eliminazione: {str(e)}"}), 500


# -------------- ADMIN ROUTES -------------------
@app.route("/admin/refresh-reparti", methods=["GET", "POST"])
@login_required
def refresh_reparti():
    """Interfaccia per aggiornare i reparti dal database SQL Server"""
    if current_user.role not in ["cassiere", "cassa"]:
        abort(403)
    
    if request.method == "GET":
        # Controlla se è stata inserita la password corretta
        admin_password = request.args.get('password')
        if admin_password != "Zarrella123":
            return render_template("admin_password.html")
        
        # Mostra il form
        return render_template("admin_refresh_reparti.html")
    
    # POST method - aggiorna i reparti
    
    try:
        # 1. Connessione al database SQL Server (via ngrok)
        conn_str = os.getenv("MSSQL_CONNSTRING_DEMO")
        if not conn_str:
            return jsonify({"success": False, "error": "Stringa di connessione non configurata"}), 500
        
        conn = pyodbc.connect(conn_str)
        
        # 2. Query per estrarre i reparti
        query = """
        SELECT 
            ARCODART                                AS codice_articolo,
            ARTIPCO1                                AS tipo_collo_1,
            ARTIPCO2                                AS tipo_collo_2,
            ARUNMIS2                                AS unita_misura_2,
            AROPERAT                                AS operatore_conversione,
            ARMOLTIP                                AS fattore_conversione
        FROM ZARREART_ICOL
        WHERE ARCODART IS NOT NULL 
          AND ARCODART <> ''
        ORDER BY ARCODART;
        """
        
        # 3. Esegui query e ottieni risultati
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            headers = [col[0] for col in cur.description]
        
        conn.close()
        
        # 4. Carica i dati nel database PostgreSQL
        articoli_caricati = 0
        articoli_aggiornati = 0
        
        # Pulisci la tabella esistente
        ArticoloReparto.query.delete()
        db.session.commit()
        
        # Carica i nuovi dati
        for row in rows:
            row_dict = dict(zip(headers, row))
            codice_articolo = row_dict['codice_articolo'].strip() if row_dict['codice_articolo'] else ''
            
            # Salta righe vuote o con codici non validi
            if not codice_articolo or codice_articolo in ['.', '..']:
                continue
            
            articolo = ArticoloReparto(
                codice_articolo=codice_articolo,
                tipo_collo_1=row_dict['tipo_collo_1'].strip() if row_dict['tipo_collo_1'] else None,
                tipo_collo_2=row_dict['tipo_collo_2'].strip() if row_dict['tipo_collo_2'] else None,
                unita_misura_2=row_dict['unita_misura_2'].strip() if row_dict['unita_misura_2'] else None,
                operatore_conversione=row_dict['operatore_conversione'].strip() if row_dict['operatore_conversione'] else None,
                fattore_conversione=float(row_dict['fattore_conversione']) if row_dict['fattore_conversione'] and str(row_dict['fattore_conversione']).strip() else None
            )
            db.session.add(articolo)
            articoli_caricati += 1
        
        # Commit delle modifiche
        db.session.commit()
        
        # 5. Forza il refresh della cache degli ordini
        refresh_orders()
        
        return jsonify({
            "success": True,
            "message": "Reparti aggiornati con successo",
            "articoli_caricati": articoli_caricati,
            "timestamp": datetime.now().isoformat()
        })
        
    except pyodbc.Error as e:
        db.session.rollback()
        return jsonify({
            "success": False,
            "error": f"Errore connessione database: {str(e)}"
        }), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({
            "success": False,
            "error": f"Errore generico: {str(e)}"
        }), 500


# -------------- TRASPORTI DASHBOARD -------------------
@app.route("/trasporti")
@login_required
def trasporti_dashboard():
    """Dashboard per la gestione dei trasporti/consegne"""
    if current_user.role != 'trasporti':
        abort(403)
    
    # Filtra ordini che hanno "consegna" nelle note
    ordini_consegna = []
    for order in app.config["ORDERS_CACHE"]:
        note = order.get("ritiro", "").lower()
        if "consegna" in note:
            ordini_consegna.append(order)
    
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in ordini_consegna:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Aggiungi lo stato per ogni ordine
    for seriale, order in unique_orders.items():
        # Stato generale dell'ordine
        status_record = OrderStatus.query.filter_by(seriale=seriale).first()
        if status_record:
            order["status"] = status_record.status
            order["status_operatore"] = status_record.operatore
            order["status_timestamp"] = status_record.timestamp.isoformat() if status_record.timestamp else None
        else:
            order["status"] = "nuovo"
            order["status_operatore"] = None
            order["status_timestamp"] = None
        
        # Stato per reparto
        status_by_reparto = get_ordine_status_by_reparto(seriale)
        order["status_by_reparto"] = status_by_reparto
        
        # Crea un riassunto dello stato per reparto
        reparti_ordine = get_ordine_reparti(seriale)
        status_summary = []
        for reparto in reparti_ordine:
            reparto_status = status_by_reparto.get(reparto, {})
            status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
        order["status_summary"] = " | ".join(status_summary)
        
        # Controlla se ha un indirizzo di consegna
        delivery_address = DeliveryAddress.query.filter_by(seriale=seriale).first()
        order["has_delivery_address"] = delivery_address is not None
        if delivery_address:
            order["delivery_address"] = f"{delivery_address.indirizzo}, {delivery_address.citta} ({delivery_address.provincia})"
    
    # Ordina gli ordini per data (più recente prima) e numero ordine (maggiore prima)
    # Usa la stessa funzione sort_key per gestire correttamente il cambio anno
    def sort_key(order):
        data_ordine = order.get("data_ordine")
        if data_ordine:
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        return (data_sort, num_sort)
    
    sorted_orders = sorted(unique_orders.values(), key=sort_key, reverse=True)
    
    return render_template("trasporti_dashboard.html", 
                         orders=sorted_orders)


def calculate_order_weight(seriale):
    """Calcola il peso totale in KG per un ordine"""
    total_weight_kg = 0.0
    
    # Trova tutte le righe dell'ordine
    order_lines = [o for o in app.config["ORDERS_CACHE"] if o["seriale"] == seriale]
    
    for line in order_lines:
        weight_kg = 0.0
        
        # Controlla la prima unità di misura
        um1 = (line.get("unita_misura") or "").strip().lower()
        quantita1 = line.get("quantita", 0)
        
        # Somma solo se l'unità di misura è kg o una sua variante
        if um1 in ["kg", "kg.", "kgr", "kgr."]:
            weight_kg += quantita1
        elif um1 in ["g", "gr", "gr."]:
            weight_kg += quantita1 / 1000.0
        elif um1 in ["t", "ton", "tonn", "tonnellate"]:
            weight_kg += quantita1 * 1000.0
        
        # Controlla la seconda unità di misura
        um2 = (line.get("unita_misura_2") or "").strip().lower()
        quantita2 = line.get("quantita_um2", 0)
        
        # Somma solo se l'unità di misura è kg o una sua variante
        if um2 in ["kg", "kg.", "kgr", "kgr."]:
            weight_kg += quantita2
        elif um2 in ["g", "gr", "gr."]:
            weight_kg += quantita2 / 1000.0
        elif um2 in ["t", "ton", "tonn", "tonnellate"]:
            weight_kg += quantita2 * 1000.0
        
        # Aggiungi al totale solo se abbiamo trovato un peso valido
        if weight_kg > 0:
            total_weight_kg += weight_kg
    
    return round(total_weight_kg, 2)


@app.route("/api/trasporti/orders")
@login_required
def api_trasporti_orders():
    """API per ottenere gli ordini di consegna"""
    if current_user.role != 'trasporti':
        abort(403)
    
    # Filtra ordini che hanno "consegna" nelle note
    ordini_consegna = []
    for order in app.config["ORDERS_CACHE"]:
        note = order.get("ritiro", "").lower()
        if "consegna" in note:
            ordini_consegna.append(order)
    
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in ordini_consegna:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # Aggiungi lo stato per ogni ordine
    for seriale, order in unique_orders.items():
        # Stato generale dell'ordine
        status_record = OrderStatus.query.filter_by(seriale=seriale).first()
        if status_record:
            order["status"] = status_record.status
            order["status_operatore"] = status_record.operatore
            order["status_timestamp"] = status_record.timestamp.isoformat() if status_record.timestamp else None
        else:
            order["status"] = "nuovo"
            order["status_operatore"] = None
            order["status_timestamp"] = None
        
        # Stato per reparto
        status_by_reparto = get_ordine_status_by_reparto(seriale)
        order["status_by_reparto"] = status_by_reparto
        
        # Crea un riassunto dello stato per reparto
        reparti_ordine = get_ordine_reparti(seriale)
        status_summary = []
        for reparto in reparti_ordine:
            reparto_status = status_by_reparto.get(reparto, {})
            status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
        order["status_summary"] = " | ".join(status_summary)
        
        # Calcola il peso totale dell'ordine
        order["peso_totale_kg"] = calculate_order_weight(seriale)
        
        # Controlla se ha indirizzi di consegna
        delivery_addresses = DeliveryAddress.query.filter_by(seriale=seriale).all()
        order["has_delivery_address"] = len(delivery_addresses) > 0
        order["delivery_addresses"] = []
        if delivery_addresses:
            for addr in delivery_addresses:
                order["delivery_addresses"].append({
                    "id": addr.id,
                    "indirizzo": f"{addr.indirizzo}, {addr.citta} ({addr.provincia})",
                    "indirizzo_completo": f"{addr.indirizzo}, {addr.citta}, {addr.provincia} {addr.cap}",
                    "coordinate_lat": addr.coordinate_lat,
                    "coordinate_lng": addr.coordinate_lng,
                    "note": addr.note_indirizzo,
                    "operatore": addr.operatore,
                    "timestamp": addr.timestamp.isoformat() if addr.timestamp else None
                })
            # Mantieni compatibilità con il codice esistente
            first_address = delivery_addresses[0]
            order["delivery_address"] = f"{first_address.indirizzo}, {first_address.citta} ({first_address.provincia})"
            order["coordinate_lat"] = first_address.coordinate_lat
            order["coordinate_lng"] = first_address.coordinate_lng
    
    # Ordina gli ordini per data (più recente prima) e numero ordine (maggiore prima)
    # Usa la stessa funzione sort_key per gestire correttamente il cambio anno
    def sort_key(order):
        data_ordine = order.get("data_ordine")
        if data_ordine:
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        return (data_sort, num_sort)
    
    sorted_orders = sorted(unique_orders.values(), key=sort_key, reverse=True)
    
    return jsonify({
        "success": True,
        "orders": sorted_orders
    })


@app.route("/api/trasporti/all-orders")
@login_required
def api_trasporti_all_orders():
    """API per ottenere tutti gli ordini per la dashboard trasporti"""
    if current_user.role != 'trasporti':
        abort(403)
    
    # Prendi tutti gli ordini dalla cache
    all_orders = app.config["ORDERS_CACHE"]
    

    
    # Raggruppa per seriale per evitare duplicati
    unique_orders = {}
    for order in all_orders:
        seriale = order["seriale"]
        if seriale not in unique_orders:
            unique_orders[seriale] = order
    
    # OTTIMIZZAZIONE: Query batch invece di N+1 queries
    # Prendi tutti i seriali
    all_seriali = list(unique_orders.keys())
    
    # Query batch per stati ordini (una sola query invece di N)
    status_records = {s.seriale: s for s in OrderStatus.query.filter(OrderStatus.seriale.in_(all_seriali)).all()}
    
    # Query batch per indirizzi (una sola query invece di N)
    all_addresses = DeliveryAddress.query.filter(DeliveryAddress.seriale.in_(all_seriali)).all()
    addresses_by_seriale = {}
    for addr in all_addresses:
        if addr.seriale not in addresses_by_seriale:
            addresses_by_seriale[addr.seriale] = []
        addresses_by_seriale[addr.seriale].append(addr)
    
    # Aggiungi informazioni per ogni ordine (ora usa i dati batch)
    for seriale, order in unique_orders.items():
        # Controlla se è un ordine di consegna
        note = order.get("ritiro", "").lower()
        order["is_consegna"] = "consegna" in note
        
        # Stato generale dell'ordine (da cache batch)
        status_record = status_records.get(seriale)
        if status_record:
            order["status"] = status_record.status
            order["status_operatore"] = status_record.operatore
            order["status_timestamp"] = status_record.timestamp.isoformat() if status_record.timestamp else None
        else:
            order["status"] = "nuovo"
            order["status_operatore"] = None
            order["status_timestamp"] = None
        
        # Stato per reparto
        status_by_reparto = get_ordine_status_by_reparto(seriale)
        order["status_by_reparto"] = status_by_reparto
        
        # Crea un riassunto dello stato per reparto
        reparti_ordine = get_ordine_reparti(seriale)
        status_summary = []
        for reparto in reparti_ordine:
            reparto_status = status_by_reparto.get(reparto, {})
            status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
        order["status_summary"] = " | ".join(status_summary)
        
        # Calcola il peso totale dell'ordine
        order["peso_totale_kg"] = calculate_order_weight(seriale)
        
        # Controlla se ha indirizzi di consegna (da cache batch)
        delivery_addresses = addresses_by_seriale.get(seriale, [])
        order["has_delivery_address"] = len(delivery_addresses) > 0
        order["delivery_addresses"] = []
        if delivery_addresses:
            for addr in delivery_addresses:
                order["delivery_addresses"].append({
                    "id": addr.id,
                    "indirizzo": f"{addr.indirizzo}, {addr.citta} ({addr.provincia})",
                    "indirizzo_completo": f"{addr.indirizzo}, {addr.citta}, {addr.provincia} {addr.cap}",
                    "coordinate_lat": addr.coordinate_lat,
                    "coordinate_lng": addr.coordinate_lng,
                    "note": addr.note_indirizzo,
                    "operatore": addr.operatore,
                    "timestamp": addr.timestamp.isoformat() if addr.timestamp else None
                })
            # Mantieni compatibilità con il codice esistente
            first_address = delivery_addresses[0]
            order["delivery_address"] = f"{first_address.indirizzo}, {first_address.citta} ({first_address.provincia})"
            order["coordinate_lat"] = first_address.coordinate_lat
            order["coordinate_lng"] = first_address.coordinate_lng
        
    # Ordina gli ordini per data (più recente prima) e numero ordine (maggiore prima)
    # Usa la stessa funzione sort_key per gestire correttamente il cambio anno
    def sort_key(order):
        data_ordine = order.get("data_ordine")
        if data_ordine:
            if hasattr(data_ordine, 'year'):
                data_sort = (data_ordine.year, data_ordine.month, data_ordine.day)
            elif isinstance(data_ordine, str):
                try:
                    from datetime import datetime
                    for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y', '%d-%m-%Y']:
                        try:
                            dt = datetime.strptime(data_ordine.split()[0], fmt)
                            data_sort = (dt.year, dt.month, dt.day)
                            break
                        except:
                            continue
                    else:
                        data_sort = (1900, 1, 1)
                except:
                    data_sort = (1900, 1, 1)
            else:
                data_sort = (1900, 1, 1)
        else:
            data_sort = (1900, 1, 1)
        numero_ordine = order.get("numero_ordine")
        num_sort = int(numero_ordine) if numero_ordine and str(numero_ordine).isdigit() else 0
        return (data_sort, num_sort)
    
    sorted_orders = sorted(unique_orders.values(), key=sort_key, reverse=True)
    
    return jsonify({
        "success": True,
        "orders": sorted_orders
    })


@app.route("/api/trasporti/delivery-address", methods=["POST"])
@login_required
def add_delivery_address():
    """Aggiunge un indirizzo di consegna per un ordine"""
    if current_user.role != 'trasporti':
        abort(403)
    
    seriale = request.form.get("seriale")
    indirizzo = request.form.get("indirizzo")
    citta = request.form.get("citta")
    provincia = request.form.get("provincia")
    cap = request.form.get("cap")
    note = request.form.get("note")
    
    if not all([seriale, indirizzo, citta, provincia, cap]):
        return jsonify({"error": "Tutti i campi sono obbligatori"}), 400
    
    # Verifica che l'ordine esista
    order_exists = any(o["seriale"] == seriale for o in app.config["ORDERS_CACHE"])
    if not order_exists:
        return jsonify({"error": "Ordine non trovato"}), 404
    
    # Geocoding dell'indirizzo per ottenere le coordinate
    coordinate_lat = None
    coordinate_lng = None
    
    try:
        import requests
        import time
        
        # Costruisci l'indirizzo completo
        indirizzo_completo = f"{indirizzo}, {cap} {citta}, {provincia}, Italia"
        # Geocoding silenzioso
        
        # Usa Nominatim (OpenStreetMap) per il geocoding gratuito
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': indirizzo_completo,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        
        # Geocoding silenzioso
        
        headers = {
            'User-Agent': 'EstazioneOrdini/1.0 (https://github.com/estazione-ordini; trasporti@example.com)'
        }
        
        # Aggiungi un delay per rispettare i limiti di rate
        time.sleep(1)
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            if data and len(data) > 0:
                coordinate_lat = float(data[0]['lat'])
                coordinate_lng = float(data[0]['lon'])
            else:
                # Prova con un indirizzo più semplice
                indirizzo_semplice = f"{citta}, {provincia}, Italia"
                
                time.sleep(1)  # Delay per il retry
                params['q'] = indirizzo_semplice
                response2 = requests.get(url, params=params, headers=headers, timeout=10)
                if response2.status_code == 200:
                    data2 = response2.json()
                    if data2 and len(data2) > 0:
                        coordinate_lat = float(data2[0]['lat'])
                        coordinate_lng = float(data2[0]['lon'])
        elif response.status_code == 403:
            # Nominatim ha rate limiting - non loggare come errore, è normale
            # Usa coordinate approssimative per l'Italia
            coordinate_lat = 41.9028  # Roma
            coordinate_lng = 12.4964
    except Exception as e:
        # Errore geocoding - continua senza coordinate (non loggare ogni errore)
        pass
    
    # Crea il nuovo indirizzo
    delivery_address = DeliveryAddress(
        seriale=seriale,
        indirizzo=indirizzo,
        citta=citta,
        provincia=provincia,
        cap=cap,
        coordinate_lat=coordinate_lat,
        coordinate_lng=coordinate_lng,
        note_indirizzo=note,
        operatore=current_user.username
    )
    
    db.session.add(delivery_address)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "id": delivery_address.id,
        "message": "Indirizzo aggiunto con successo",
        "coordinate_lat": coordinate_lat,
        "coordinate_lng": coordinate_lng
    })


@app.route("/api/trasporti/delivery-address/<int:address_id>", methods=["PUT"])
@login_required
def update_delivery_address(address_id):
    """Modifica un indirizzo di consegna esistente"""
    if current_user.role != 'trasporti':
        abort(403)
    
    delivery_address = DeliveryAddress.query.get_or_404(address_id)
    
    # Verifica che l'utente sia l'autore dell'indirizzo
    if delivery_address.operatore != current_user.username:
        abort(403)
    
    data = request.get_json()
    indirizzo = data.get("indirizzo")
    citta = data.get("citta")
    provincia = data.get("provincia")
    cap = data.get("cap")
    note = data.get("note")
    
    if not all([indirizzo, citta, provincia, cap]):
        return jsonify({"error": "Tutti i campi sono obbligatori"}), 400
    
    # Geocoding dell'indirizzo per ottenere le coordinate
    coordinate_lat = None
    coordinate_lng = None
    
    try:
        import requests
        import time
        
        # Costruisci l'indirizzo completo
        indirizzo_completo = f"{indirizzo}, {cap} {citta}, {provincia}, Italia"
        # Geocoding silenzioso
        
        # Usa Nominatim (OpenStreetMap) per il geocoding gratuito
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': indirizzo_completo,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        
        # Geocoding silenzioso
        
        headers = {
            'User-Agent': 'EstazioneOrdini/1.0 (https://github.com/estazione-ordini; trasporti@example.com)'
        }
        
        # Aggiungi un delay per rispettare i limiti di rate
        time.sleep(1)
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            if data and len(data) > 0:
                coordinate_lat = float(data[0]['lat'])
                coordinate_lng = float(data[0]['lon'])
            else:
                # Prova con un indirizzo più semplice
                indirizzo_semplice = f"{citta}, {provincia}, Italia"
                
                time.sleep(1)  # Delay per il retry
                params['q'] = indirizzo_semplice
                response2 = requests.get(url, params=params, headers=headers, timeout=10)
                if response2.status_code == 200:
                    data2 = response2.json()
                    if data2 and len(data2) > 0:
                        coordinate_lat = float(data2[0]['lat'])
                        coordinate_lng = float(data2[0]['lon'])
        elif response.status_code == 403:
            # Nominatim ha rate limiting - non loggare come errore, è normale
            # Usa coordinate approssimative per l'Italia
            coordinate_lat = 41.9028  # Roma
            coordinate_lng = 12.4964
    except Exception as e:
        # Errore geocoding - continua senza coordinate (non loggare ogni errore)
        pass
    
    # Aggiorna i campi
    delivery_address.indirizzo = indirizzo
    delivery_address.citta = citta
    delivery_address.provincia = provincia
    delivery_address.cap = cap
    delivery_address.coordinate_lat = coordinate_lat
    delivery_address.coordinate_lng = coordinate_lng
    delivery_address.note_indirizzo = note
    delivery_address.timestamp = db.func.now()
    
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": "Indirizzo aggiornato con successo",
        "coordinate_lat": coordinate_lat,
        "coordinate_lng": coordinate_lng
    })


@app.route("/api/trasporti/delivery-address/<int:address_id>", methods=["DELETE"])
@login_required
def delete_delivery_address(address_id):
    """Elimina un indirizzo di consegna"""
    if current_user.role != 'trasporti':
        abort(403)
    
    delivery_address = DeliveryAddress.query.get_or_404(address_id)
    
    # Verifica che l'utente sia l'autore dell'indirizzo
    if delivery_address.operatore != current_user.username:
        abort(403)
    
    db.session.delete(delivery_address)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": "Indirizzo eliminato con successo"
    })


@app.route("/api/trasporti/delivery-addresses/<seriale>")
@login_required
def get_delivery_addresses(seriale):
    """Ottiene tutti gli indirizzi di consegna per un ordine"""
    if current_user.role != 'trasporti':
        abort(403)
    
    addresses = DeliveryAddress.query.filter_by(seriale=seriale).order_by(DeliveryAddress.timestamp.desc()).all()
    
    return jsonify([{
        'id': addr.id,
        'seriale': addr.seriale,
        'indirizzo': addr.indirizzo,
        'citta': addr.citta,
        'provincia': addr.provincia,
        'cap': addr.cap,
        'note_indirizzo': addr.note_indirizzo,
        'operatore': addr.operatore,
        'timestamp': addr.timestamp.isoformat()
    } for addr in addresses])


@app.route("/api/trasporti/order-detail/<seriale>")
@login_required
def api_trasporti_order_detail(seriale):
    """API per ottenere i dettagli completi di un ordine (con righe non raggruppate)"""
    if current_user.role != 'trasporti':
        abort(403)
    
    # Trova tutte le righe dell'ordine (non raggruppate)
    order_lines = [o for o in app.config["ORDERS_CACHE"] if o["seriale"] == seriale]
    
    if not order_lines:
        return jsonify({"success": False, "error": "Ordine non trovato"}), 404
    
    # Prendi i dati base dal primo ordine
    first_line = order_lines[0]
    order_data = {
        "seriale": seriale,
        "numero_ordine": first_line.get("numero_ordine"),
        "cliente": first_line.get("cliente") or first_line.get("nome_cliente"),
        "nome_cliente": first_line.get("nome_cliente"),
        "data_ordine": first_line.get("data_ordine"),
        "peso_totale_kg": calculate_order_weight(seriale),
        "lines": []
    }
    
    # Aggiungi tutte le righe (non raggruppate)
    for line in order_lines:
        order_data["lines"].append({
            "codice_articolo": line.get("codice_articolo"),
            "descrizione_articolo": line.get("descrizione_articolo"),
            "quantita": line.get("quantita", 0),
            "unita_misura": line.get("unita_misura"),
            "codice_reparto": line.get("codice_reparto")
        })
    
    # Aggiungi stato
    status_record = OrderStatus.query.filter_by(seriale=seriale).first()
    if status_record:
        order_data["status"] = status_record.status
    else:
        order_data["status"] = "nuovo"
    
    # Aggiungi indirizzi di consegna
    delivery_addresses = DeliveryAddress.query.filter_by(seriale=seriale).all()
    order_data["delivery_addresses"] = []
    if delivery_addresses:
        for addr in delivery_addresses:
            order_data["delivery_addresses"].append({
                "id": addr.id,
                "indirizzo_completo": f"{addr.indirizzo}, {addr.citta}, {addr.provincia} {addr.cap}",
                "note": addr.note_indirizzo
            })
    
    return jsonify({
        "success": True,
        "order": order_data
    })


@app.route("/api/trasporti/weights", methods=["POST"])
@login_required
def api_trasporti_weights():
    """API leggera per ottenere solo i pesi aggiornati degli ordini"""
    if current_user.role != 'trasporti':
        abort(403)
    
    data = request.get_json()
    seriali = data.get("seriali", [])
    
    if not seriali:
        return jsonify({"success": False, "error": "Nessun seriale fornito"}), 400
    
    weights = {}
    for seriale in seriali:
        weights[seriale] = calculate_order_weight(seriale)
    
    return jsonify({
        "success": True,
        "weights": weights
    })


# Mappatura leggera province italiane (solo per conversione nome->codice)
# Dizionario compatto: solo le province principali, non tutte le varianti
PROVINCE_IT_MAP = {
    'agrigento': 'AG', 'alessandria': 'AL', 'ancona': 'AN', 'aosta': 'AO', 'arezzo': 'AR',
    'ascoli piceno': 'AP', 'asti': 'AT', 'avellino': 'AV', 'bari': 'BA', 'barletta-andria-trani': 'BT',
    'belluno': 'BL', 'benevento': 'BN', 'bergamo': 'BG', 'biella': 'BI', 'bologna': 'BO',
    'bolzano': 'BZ', 'brescia': 'BS', 'brindisi': 'BR', 'cagliari': 'CA', 'caltanissetta': 'CL',
    'campobasso': 'CB', 'caserta': 'CE', 'catania': 'CT', 'catanzaro': 'CZ', 'chieti': 'CH',
    'como': 'CO', 'cosenza': 'CS', 'cremona': 'CR', 'crotone': 'KR', 'cuneo': 'CN',
    'enna': 'EN', 'fermo': 'FM', 'ferrara': 'FE', 'firenze': 'FI', 'foggia': 'FG',
    'forlì-cesena': 'FC', 'forlì cesena': 'FC', 'frosinone': 'FR', 'genova': 'GE', 'gorizia': 'GO', 'grosseto': 'GR',
    'imperia': 'IM', 'isernia': 'IS', 'la spezia': 'SP', 'spezia': 'SP', 'l\'aquila': 'AQ', 'aquila': 'AQ', 'latina': 'LT',
    'lecce': 'LE', 'lecco': 'LC', 'livorno': 'LI', 'lodi': 'LO', 'lucca': 'LU',
    'macerata': 'MC', 'mantova': 'MN', 'massa-carrara': 'MS', 'massa carrara': 'MS', 'matera': 'MT', 'messina': 'ME',
    'milano': 'MI', 'modena': 'MO', 'monza e brianza': 'MB', 'monza': 'MB', 'napoli': 'NA', 'novara': 'NO',
    'nuoro': 'NU', 'oristano': 'OR', 'padova': 'PD', 'palermo': 'PA', 'parma': 'PR',
    'pavia': 'PV', 'perugia': 'PG', 'pesaro e urbino': 'PU', 'pesaro urbino': 'PU', 'pescara': 'PE', 'piacenza': 'PC',
    'pisa': 'PI', 'pistoia': 'PT', 'pordenone': 'PN', 'potenza': 'PZ', 'prato': 'PO',
    'ragusa': 'RG', 'ravenna': 'RA', 'reggio calabria': 'RC', 'reggio emilia': 'RE', 'rieti': 'RI',
    'rimini': 'RN', 'roma': 'RM', 'rovigo': 'RO', 'salerno': 'SA', 'sassari': 'SS',
    'savona': 'SV', 'siena': 'SI', 'siracusa': 'SR', 'sondrio': 'SO', 'sud sardegna': 'SU',
    'taranto': 'TA', 'teramo': 'TE', 'terni': 'TR', 'torino': 'TO', 'trapani': 'TP',
    'trento': 'TN', 'treviso': 'TV', 'trieste': 'TS', 'udine': 'UD', 'varese': 'VA',
    'venezia': 'VE', 'verbania': 'VB', 'vercelli': 'VC', 'verona': 'VR', 'vibo valentia': 'VV',
    'vicenza': 'VI', 'viterbo': 'VT'
}

def get_province_code(state_name):
    """Converte il nome provincia in codice (leggero, senza logiche complesse)"""
    if not state_name:
        return ''
    state_lower = state_name.lower().strip()
    # Se è già un codice a 2 lettere, restituiscilo
    if len(state_lower) == 2 and state_lower.isalpha():
        return state_lower.upper()
    # Cerca nella mappatura
    return PROVINCE_IT_MAP.get(state_lower, '')

# HERE API Credentials
HERE_APP_ID = "Gl6wmoOfjmk4m5bNwd4N"
HERE_API_KEY = "cLU54RrCd1PdVmhZBexdbO7ixqzPTjSn7HcqGTbRd7k"

@app.route("/api/trasporti/search-address", methods=["GET"])
@login_required
def api_trasporti_search_address():
    """API proxy per cercare indirizzi usando HERE Autocomplete API"""
    if current_user.role != 'trasporti':
        abort(403)
    
    query = request.args.get('q', '')
    if not query or len(query) < 3:
        return jsonify({"success": False, "error": "Query troppo corta"}), 400
    
    try:
        try:
            import requests
        except ImportError:
            return jsonify({"success": False, "error": "Libreria requests non disponibile"}), 500
        
        # HERE Autocomplete API
        url = "https://autocomplete.search.hereapi.com/v1/autocomplete"
        params = {
            'q': query,
            'limit': 6,
            'lang': 'it',
            'in': 'countryCode:ITA',  # Limita all'Italia
            'apiKey': HERE_API_KEY
        }
        
        headers = {
            'Accept': 'application/json'
        }
        
        # Timeout breve per risposta veloce
        response = requests.get(url, params=params, headers=headers, timeout=5)
        
        # Debug: log della risposta se errore
        if response.status_code != 200:
            print(f"⚠️ HERE API Error {response.status_code}: {response.text}")
            return jsonify({"success": True, "results": []})  # Ritorna lista vuota invece di errore
        
        response.raise_for_status()
        data = response.json()
        
        # Debug: log struttura risposta
        if not data.get('items'):
            print(f"⚠️ HERE API: nessun risultato per query '{query}'")
        
        # Formatta i risultati da HERE
        results = []
        if data.get('items'):
            for item in data['items']:
                # HERE restituisce i dati in item.address
                address = item.get('address', {})
                title = item.get('title', '')
                result_type_api = item.get('resultType', '')
                
                # Estrai informazioni dalla struttura HERE
                street = address.get('street', '') or ''
                city = address.get('city', '') or address.get('county', '')
                state = address.get('state', '') or ''  # Nome completo della regione/provincia
                county_code = address.get('countyCode', '') or ''  # Codice provincia (es. "NA")
                postcode = address.get('postalCode', '') or ''
                
                # Se non c'è street ma title contiene prefissi di strada, usa title
                if not street and title:
                    title_lower = title.lower()
                    street_prefixes = ['via ', 'viale ', 'piazza ', 'corso ', 'largo ', 'piazzale ', 
                                      'strada ', 'vicolo ', 'borgo ', 'contrada ', 'frazione ']
                    if any(title_lower.startswith(prefix) for prefix in street_prefixes):
                        street = title
                
                # Determina tipo e rilevanza basandosi su resultType di HERE
                if result_type_api == 'street' or street:
                    result_type = 'street'
                    relevance_score = 100
                elif result_type_api == 'city' or city:
                    result_type = 'city'
                    relevance_score = 50
                else:
                    result_type = 'other'
                    relevance_score = 10
                
                # Costruisci display_name usando il label di HERE se disponibile, altrimenti costruiscilo
                display_name = address.get('label', '')
                if not display_name:
                    display_parts = []
                    if street:
                        display_parts.append(street)
                    if city:
                        display_parts.append(city)
                    if postcode:
                        display_parts.append(f"({postcode})")
                    display_name = ', '.join(display_parts) if display_parts else title
                
                # Estrai codice provincia: HERE fornisce countyCode che è già il codice provincia
                province_code = county_code.upper() if county_code and len(county_code) == 2 else ''
                if not province_code and state:
                    # Se non c'è countyCode, prova a convertire il nome stato
                    province_code = get_province_code(state)
                
                results.append({
                    'name': title,
                    'street': street,
                    'city': city,
                    'state': state,
                    'state_code': province_code,
                    'postcode': postcode,
                    'display_name': display_name,
                    'type': result_type,
                    'relevance': relevance_score
                })
        
        # Ordina per rilevanza
        results.sort(key=lambda x: -x['relevance'])
        
        return jsonify({
            "success": True,
            "results": results[:6]  # Massimo 6 risultati
        })
        
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Timeout nella ricerca. Riprova."}), 500
    except requests.exceptions.HTTPError as e:
        # Se HERE restituisce errore, ritorna lista vuota invece di errore
        return jsonify({"success": True, "results": []})
    except requests.exceptions.RequestException as e:
        # Errore di connessione - ritorna lista vuota
        return jsonify({"success": True, "results": []})
    except Exception as e:
        # Errore generico - ritorna lista vuota
        return jsonify({"success": True, "results": []})


@app.route("/api/trasporti/assign-route", methods=["POST"])
@login_required
def api_trasporti_assign_route():
    """Assegna ordini ad autista e camion, calcola percorso con HERE"""
    if current_user.role != 'trasporti':
        abort(403)
    
    try:
        data = request.get_json()
        ordini_seriali = data.get('ordini_seriali', [])  # Lista di seriali
        autista = data.get('autista', '').strip()
        mezzo = data.get('mezzo', '').strip()
        indirizzo_partenza = data.get('indirizzo_partenza', '').strip()
        nome_tratta = data.get('nome_tratta', '').strip()
        
        if not all([ordini_seriali, autista, mezzo, indirizzo_partenza]):
            return jsonify({"success": False, "error": "Tutti i campi sono obbligatori"}), 400
        
        # Verifica che gli ordini esistano e abbiano indirizzi
        ordini_validi = []
        indirizzi_consegna = []
        
        for seriale in ordini_seriali:
            # Verifica ordine nella cache
            order_exists = any(o["seriale"] == seriale for o in app.config["ORDERS_CACHE"])
            if not order_exists:
                continue
            
            # Verifica indirizzo di consegna
            delivery_address = DeliveryAddress.query.filter_by(seriale=seriale).first()
            if not delivery_address or not delivery_address.coordinate_lat:
                continue
            
            ordini_validi.append(seriale)
            indirizzi_consegna.append({
                'seriale': seriale,
                'indirizzo': f"{delivery_address.indirizzo}, {delivery_address.citta}",
                'coordinate': f"{delivery_address.coordinate_lat},{delivery_address.coordinate_lng}"
            })
        
        if not ordini_validi:
            return jsonify({"success": False, "error": "Nessun ordine valido con indirizzo completo"}), 400
        
        # Geocoding indirizzo partenza con HERE
        partenza_lat = None
        partenza_lng = None
        try:
            import requests
            geocode_url = "https://geocode.search.hereapi.com/v1/geocode"
            geocode_params = {
                'q': indirizzo_partenza,
                'apiKey': HERE_API_KEY,
                'limit': 1
            }
            geocode_response = requests.get(geocode_url, params=geocode_params, timeout=5)
            if geocode_response.status_code == 200:
                geocode_data = geocode_response.json()
                if geocode_data.get('items'):
                    partenza_lat = float(geocode_data['items'][0]['position']['lat'])
                    partenza_lng = float(geocode_data['items'][0]['position']['lng'])
        except Exception as e:
            print(f"⚠️ Errore geocoding partenza: {e}")
        
        if not partenza_lat or not partenza_lng:
            return jsonify({"success": False, "error": "Impossibile geocodificare l'indirizzo di partenza"}), 400
        
        # Calcola percorso ottimizzato con HERE Routing API
        route_data = None
        distanza_totale_km = None
        tempo_stimato_minuti = None
        
        try:
            # Costruisci waypoints: partenza + tutte le destinazioni
            waypoints = [f"{partenza_lat},{partenza_lng}"]
            for addr in indirizzi_consegna:
                waypoints.append(addr['coordinate'])
            
            # HERE Routing API v8
            routing_url = "https://router.hereapi.com/v8/routes"
            routing_params = {
                'transportMode': 'car',
                'origin': waypoints[0],
                'destination': waypoints[-1],
                'via': ','.join(waypoints[1:-1]) if len(waypoints) > 2 else '',
                'return': 'summary',
                'apiKey': HERE_API_KEY
            }
            
            routing_response = requests.get(routing_url, params=routing_params, timeout=10)
            if routing_response.status_code == 200:
                route_data = routing_response.json()
                if route_data.get('routes'):
                    route = route_data['routes'][0]
                    # HERE v8 restituisce summary nelle sections
                    sections = route.get('sections', [])
                    if sections:
                        # Somma distanza e tempo di tutte le sezioni
                        total_length = 0
                        total_duration = 0
                        for section in sections:
                            summary = section.get('summary', {})
                            total_length += summary.get('length', 0)
                            total_duration += summary.get('duration', 0)
                        distanza_totale_km = total_length / 1000  # Converti da metri a km
                        tempo_stimato_minuti = total_duration // 60  # Converti da secondi a minuti
        except Exception as e:
            print(f"⚠️ Errore calcolo percorso: {e}")
        
        # Genera link navigazione HERE WeGo (navigatore completo)
        navigation_url = None
        if indirizzi_consegna:
            # Costruisci URL per HERE WeGo con navigazione turn-by-turn
            # Formato: https://wego.here.com/directions/mix/[lat1,lng1]/[lat2,lng2],.../[latN,lngN]
            # Per più destinazioni, usa il formato con waypoints
            waypoints_list = [f"{partenza_lat},{partenza_lng}"]
            for addr in indirizzi_consegna:
                waypoints_list.append(addr['coordinate'])
            
            # HERE WeGo: formato deep link corretto secondo documentazione ufficiale
            # Formato: here.route://mylocation/[coordinate] o here.route://[partenza]/[destinazione]
            # Questo formato apre direttamente l'app HERE WeGo se installata sul telefono
            
            if len(waypoints_list) == 2:
                # Singola destinazione: usa mylocation per partenza automatica
                # Formato: here.route://mylocation/[lat],[lng]
                navigation_url = f"here.route://mylocation/{waypoints_list[1]}"
            else:
                # Multiple destinazioni: usa partenza specifica
                # Formato: here.route://[lat1],[lng1]/[lat2],[lng2]/[lat3],[lng3]...
                navigation_url = "here.route://" + "/".join(waypoints_list)
            
            # Fallback web URL (per desktop o se l'app non è installata)
            # Questo apre il sito web HERE WeGo
            if len(waypoints_list) == 2:
                web_url = f"https://wego.here.com/directions/mix/{waypoints_list[0]}/{waypoints_list[1]}"
            else:
                web_url = "https://wego.here.com/directions/mix/" + "/".join(waypoints_list)
            
            # Nota: Il deep link "here.route://" apre direttamente l'app HERE WeGo sul telefono
            # Se l'app non è installata, il browser aprirà il link web come fallback
        
        # Salva la tratta nel database
        route = DeliveryRoute(
            nome_tratta=nome_tratta or f"Tratta {autista} - {datetime.now().strftime('%Y-%m-%d')}",
            ordini_seriali=','.join(ordini_validi),
            indirizzo_partenza=indirizzo_partenza,
            indirizzi_consegna='|'.join([f"{addr['indirizzo']}" for addr in indirizzi_consegna]),
            distanza_totale_km=distanza_totale_km,
            tempo_stimato_minuti=tempo_stimato_minuti,
            autista=autista,
            mezzo=mezzo,
            stato='pianificata',
            operatore=current_user.username
        )
        
        db.session.add(route)
        db.session.commit()
        
        return jsonify({
            "success": True,
            "route_id": route.id,
            "distanza_totale_km": round(distanza_totale_km, 2) if distanza_totale_km else None,
            "tempo_stimato_minuti": tempo_stimato_minuti,
            "navigation_url": navigation_url,  # Deep link here.route:// per app mobile
            "navigation_web_url": web_url if 'web_url' in locals() else navigation_url,  # Fallback web
            "waypoints": waypoints_list if 'waypoints_list' in locals() else [],
            "message": "Tratta assegnata con successo"
        })
        
    except Exception as e:
        print(f"❌ Errore assegnazione tratta: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"Errore: {str(e)}"}), 500


@app.route("/api/trasporti/routes", methods=["GET"])
@login_required
def api_trasporti_routes():
    """Ottiene tutte le tratte assegnate"""
    if current_user.role != 'trasporti':
        abort(403)
    
    routes = DeliveryRoute.query.order_by(DeliveryRoute.timestamp.desc()).limit(50).all()
    
    routes_data = []
    for route in routes:
        routes_data.append({
            'id': route.id,
            'nome_tratta': route.nome_tratta,
            'ordini_seriali': route.ordini_seriali.split(',') if route.ordini_seriali else [],
            'autista': route.autista,
            'mezzo': route.mezzo,
            'stato': route.stato,
            'distanza_totale_km': route.distanza_totale_km,
            'tempo_stimato_minuti': route.tempo_stimato_minuti,
            'indirizzo_partenza': route.indirizzo_partenza,
            'timestamp': route.timestamp.isoformat() if route.timestamp else None
        })
    
    return jsonify({
        "success": True,
        "routes": routes_data
    })


@app.route("/api/trasporti/route-detail/<int:route_id>", methods=["GET"])
@login_required
def api_trasporti_route_detail(route_id):
    """Ottiene i dettagli di una tratta per il navigatore con coordinate geocodificate e percorso calcolato"""
    if current_user.role != 'trasporti':
        abort(403)
    
    route = DeliveryRoute.query.get_or_404(route_id)
    
    # Geocodifica indirizzo partenza dal backend (evita CORS)
    partenza_coordinate = None
    if route.indirizzo_partenza:
        try:
            import requests
            geocode_url = "https://geocode.search.hereapi.com/v1/geocode"
            geocode_params = {
                'q': route.indirizzo_partenza,
                'apiKey': HERE_API_KEY,
                'limit': 1
            }
            geocode_response = requests.get(geocode_url, params=geocode_params, timeout=5)
            if geocode_response.status_code == 200:
                geocode_data = geocode_response.json()
                if geocode_data.get('items'):
                    partenza_coordinate = {
                        'lat': float(geocode_data['items'][0]['position']['lat']),
                        'lng': float(geocode_data['items'][0]['position']['lng'])
                    }
        except Exception as e:
            print(f"⚠️ Errore geocoding partenza: {e}")
    
    # Geocodifica indirizzi consegna dal backend
    indirizzi_consegna_coordinate = []
    seriali = route.ordini_seriali.split(',') if route.ordini_seriali else []
    for seriale in seriali:
        delivery_address = DeliveryAddress.query.filter_by(seriale=seriale).first()
        if delivery_address:
            if delivery_address.coordinate_lat and delivery_address.coordinate_lng:
                # Usa coordinate già salvate
                indirizzi_consegna_coordinate.append({
                    'seriale': seriale,
                    'indirizzo': f"{delivery_address.indirizzo}, {delivery_address.citta}",
                    'coordinate': {
                        'lat': delivery_address.coordinate_lat,
                        'lng': delivery_address.coordinate_lng
                    }
                })
            else:
                # Geocodifica se non ci sono coordinate
                try:
                    import requests
                    indirizzo_completo = f"{delivery_address.indirizzo}, {delivery_address.citta}, {delivery_address.provincia}"
                    geocode_url = "https://geocode.search.hereapi.com/v1/geocode"
                    geocode_params = {
                        'q': indirizzo_completo,
                        'apiKey': HERE_API_KEY,
                        'limit': 1
                    }
                    geocode_response = requests.get(geocode_url, params=geocode_params, timeout=5)
                    if geocode_response.status_code == 200:
                        geocode_data = geocode_response.json()
                        if geocode_data.get('items'):
                            indirizzi_consegna_coordinate.append({
                                'seriale': seriale,
                                'indirizzo': indirizzo_completo,
                                'coordinate': {
                                    'lat': float(geocode_data['items'][0]['position']['lat']),
                                    'lng': float(geocode_data['items'][0]['position']['lng'])
                                }
                            })
                except Exception as e:
                    print(f"⚠️ Errore geocoding indirizzo {seriale}: {e}")
    
    # Calcola percorso dal backend (evita CORS)
    route_data = None
    route_shape = None
    route_instructions = []
    route_distanza_km = None
    route_tempo_minuti = None
    
    if partenza_coordinate and indirizzi_consegna_coordinate:
        try:
            import requests
            # Costruisci waypoints
            waypoints = [f"{partenza_coordinate['lat']},{partenza_coordinate['lng']}"]
            for addr in indirizzi_consegna_coordinate:
                waypoints.append(f"{addr['coordinate']['lat']},{addr['coordinate']['lng']}")
            
            # HERE Routing API v8 (REST)
            routing_url = "https://router.hereapi.com/v8/routes"
            routing_params = {
                'transportMode': 'car',
                'origin': waypoints[0],
                'destination': waypoints[-1],
                'via': ','.join(waypoints[1:-1]) if len(waypoints) > 2 else '',
                'return': 'polyline,actions,instructions,summary',
                'apiKey': HERE_API_KEY
            }
            
            routing_response = requests.get(routing_url, params=routing_params, timeout=10)
            if routing_response.status_code == 200:
                route_data = routing_response.json()
                print(f"🔍 HERE Routing API Response Status: {routing_response.status_code}")
                
                if route_data.get('routes'):
                    route_obj = route_data['routes'][0]
                    
                    # Estrai summary per distanza e tempo
                    if route_obj.get('sections'):
                        total_length = 0
                        total_duration = 0
                        
                        for idx, section in enumerate(route_obj['sections']):
                            # Estrai summary (distanza e tempo)
                            summary = section.get('summary', {})
                            if summary:
                                total_length += summary.get('length', 0)  # in metri
                                total_duration += summary.get('duration', 0)  # in secondi
                            
                            # Estrai polyline (formato flexible polyline di HERE)
                            if section.get('polyline'):
                                route_shape = section['polyline']
                                print(f"✅ Polyline trovato nella sezione {idx}")
                            
                            # Estrai istruzioni turn-by-turn
                            if section.get('actions'):
                                for action in section['actions']:
                                    # HERE API v8 usa 'instruction' come oggetto con 'text'
                                    instruction_obj = action.get('instruction', {})
                                    if isinstance(instruction_obj, dict):
                                        instruction_text = instruction_obj.get('text', '')
                                    else:
                                        instruction_text = str(instruction_obj) if instruction_obj else ''
                                    
                                    if instruction_text:
                                        route_instructions.append({
                                            'instruction': instruction_text,
                                            'length': action.get('length', 0),
                                            'duration': action.get('duration', 0)
                                        })
                        
                        # Calcola distanza e tempo totali dalla risposta API
                        if total_length > 0:
                            route_distanza_km = total_length / 1000  # Converti da metri a km
                            route_tempo_minuti = total_duration // 60  # Converti da secondi a minuti
                            print(f"✅ Distanza calcolata: {route_distanza_km:.2f} km, Tempo: {route_tempo_minuti} minuti")
                        else:
                            route_distanza_km = None
                            route_tempo_minuti = None
                    else:
                        route_distanza_km = None
                        route_tempo_minuti = None
                        print(f"⚠️ Nessuna sezione trovata nella route")
                else:
                    route_distanza_km = None
                    route_tempo_minuti = None
                    print(f"⚠️ Nessuna route nella risposta")
            else:
                route_distanza_km = None
                route_tempo_minuti = None
                print(f"❌ HERE Routing API Error: {routing_response.status_code}")
                print(f"❌ Response: {routing_response.text[:200]}")
        except Exception as e:
            print(f"⚠️ Errore calcolo percorso backend: {e}")
            import traceback
            traceback.print_exc()
    
    return jsonify({
        "success": True,
        "route": {
            'id': route.id,
            'nome_tratta': route.nome_tratta,
            'ordini_seriali': seriali,
            'autista': route.autista,
            'mezzo': route.mezzo,
            'stato': route.stato,
            'distanza_totale_km': route_distanza_km if route_distanza_km is not None else route.distanza_totale_km,
            'tempo_stimato_minuti': route_tempo_minuti if route_tempo_minuti is not None else route.tempo_stimato_minuti,
            'indirizzo_partenza': route.indirizzo_partenza,
            'partenza_coordinate': partenza_coordinate,
            'indirizzi_consegna': indirizzi_consegna_coordinate,
            'route_shape': route_shape,  # Polyline del percorso già calcolato
            'route_instructions': route_instructions  # Indicazioni turn-by-turn già calcolate
        }
    })


# -------------- LOGIN -------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        reparto = request.form.get("reparto", "").strip()
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            # Controlla se è un operatore interno (ruoli che non richiedono selezione reparto)
            is_operatore_interno = user.role in ['cassiere', 'cassa', 'display', 'trasporti']
            
            if is_operatore_interno:
                # Per gli operatori interni, non serve il reparto
                user.reparto = None
                db.session.commit()
                login_user(user)
                
                # Reindirizza l'utente display alla pagina display
                if user.role == 'display':
                    return redirect(url_for("display"))
                elif user.role == 'trasporti':
                    return redirect(url_for("trasporti_dashboard"))
                else:
                    return redirect(url_for("home"))
            else:
                # Per i picker, verifica che il reparto sia valido
                if not reparto or not is_valid_reparto(reparto):
                    error = "Seleziona un reparto valido"
                    return render_template("login.html", error=error, reparti=get_all_reparti())
                
                # Aggiorna il reparto dell'utente
                user.reparto = reparto
                db.session.commit()
                login_user(user)
                return redirect(url_for("home"))
        error = "Credenziali invalide"
    return render_template("login.html", error=error, reparti=get_all_reparti())


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/display")
@login_required
def display():
    """Pagina display pubblico per TV"""
    if current_user.role != 'display':
        abort(403)
    
    # Ottieni solo i reparti da mostrare nel display (esclude BOMBOLE e FERRAMENTA)
    reparti = get_display_reparti()
    
    # Calcola i limiti temporali
    from datetime import timedelta
    now = datetime.now()
    limite_90_minuti = now - timedelta(minutes=90)  # Per ordini pronti nel display principale
    limite_4_ore = now - timedelta(hours=4)  # Per ordini nello storico
    
    # Filtra ordini che hanno "ritiro" o "ritira" nelle note
    ordini_ritiro = []
    for order in app.config["ORDERS_CACHE"]:
        note = order.get("ritiro", "").lower()
        if "ritiro" in note or "ritira" in note:
            ordini_ritiro.append(order)
    
    # Organizza ordini per reparto
    ordini_per_reparto = {}
    for codice_reparto, nome_reparto in reparti:
        ordini_per_reparto[codice_reparto] = {
            'nome': nome_reparto,
            'in_preparazione': [],
            'pronti': []
        }
    
    # Classifica ordini per reparto e stato con filtri temporali
    for order in ordini_ritiro:
        codice_reparto = order.get("codice_reparto", "")
        
        # Se il reparto non è valido per il display, salta questo ordine
        if not codice_reparto or codice_reparto not in ordini_per_reparto:
            continue
            
        # Determina lo stato dell'ordine per questo reparto
        status = get_ordine_status_by_reparto(order["seriale"])
        reparto_status_info = status.get(codice_reparto, {})
        reparto_status = reparto_status_info.get('status', 'nuovo') if isinstance(reparto_status_info, dict) else 'nuovo'
        
        if reparto_status == "in_preparazione":
            ordini_per_reparto[codice_reparto]['in_preparazione'].append(order)
        elif reparto_status == "pronto":
            # Controlla il timestamp dello stato per filtrare ordini pronti
            timestamp_status = reparto_status_info.get('timestamp')
            if timestamp_status:
                # Se l'ordine è stato marcato come pronto da meno di 90 minuti, mostralo nel display principale
                if timestamp_status > limite_90_minuti:
                    ordini_per_reparto[codice_reparto]['pronti'].append(order)
                # Se l'ordine è stato marcato come pronto da meno di 4 ore, includilo nello storico
                elif timestamp_status > limite_4_ore:
                    # Aggiungi un flag per indicare che è nello storico
                    order['storico'] = True
                    ordini_per_reparto[codice_reparto]['pronti'].append(order)
            else:
                # Se non c'è timestamp, mostra l'ordine (caso di fallback)
                ordini_per_reparto[codice_reparto]['pronti'].append(order)
    
    return render_template("display.html", 
                         reparti=reparti,
                         reparto_data=ordini_per_reparto,
                         timestamp=datetime.now().strftime('%d/%m/%Y %H:%M'))


@app.route("/api/display/status")
@login_required
def api_display_status():
    """Endpoint per controllare se ci sono cambiamenti negli ordini con ritiro"""
    if current_user.role != 'display':
        abort(403)
    
    # Ottieni solo i reparti da mostrare nel display (esclude BOMBOLE e FERRAMENTA)
    reparti = get_display_reparti()
    
    # Calcola i limiti temporali
    from datetime import timedelta
    now = datetime.now()
    limite_90_minuti = now - timedelta(minutes=90)  # Per ordini pronti nel display principale
    limite_4_ore = now - timedelta(hours=4)  # Per ordini nello storico
    
    # Filtra ordini che hanno "ritiro" o "ritira" nelle note
    ordini_ritiro = []
    for order in app.config["ORDERS_CACHE"]:
        note = order.get("ritiro", "").lower()
        if "ritiro" in note or "ritira" in note:
            ordini_ritiro.append(order)
    
    # Organizza ordini per reparto
    ordini_per_reparto = {}
    for codice_reparto, nome_reparto in reparti:
        ordini_per_reparto[codice_reparto] = {
            'nome': nome_reparto,
            'in_preparazione': [],
            'pronti': []
        }
    
    # Classifica ordini per reparto e stato con filtri temporali
    for order in ordini_ritiro:
        codice_reparto = order.get("codice_reparto", "")
        
        # Se il reparto non è valido per il display, salta questo ordine
        if not codice_reparto or codice_reparto not in ordini_per_reparto:
            continue
            
        # Determina lo stato dell'ordine per questo reparto
        status = get_ordine_status_by_reparto(order["seriale"])
        reparto_status_info = status.get(codice_reparto, {})
        reparto_status = reparto_status_info.get('status', 'nuovo') if isinstance(reparto_status_info, dict) else 'nuovo'
        
        if reparto_status == "in_preparazione":
            ordini_per_reparto[codice_reparto]['in_preparazione'].append(order)
        elif reparto_status == "pronto":
            # Controlla il timestamp dello stato per filtrare ordini pronti
            timestamp_status = reparto_status_info.get('timestamp')
            if timestamp_status:
                # Se l'ordine è stato marcato come pronto da meno di 90 minuti, mostralo nel display principale
                if timestamp_status > limite_90_minuti:
                    ordini_per_reparto[codice_reparto]['pronti'].append(order)
                # Se l'ordine è stato marcato come pronto da meno di 4 ore, includilo nello storico
                elif timestamp_status > limite_4_ore:
                    # Aggiungi un flag per indicare che è nello storico
                    order['storico'] = True
                    ordini_per_reparto[codice_reparto]['pronti'].append(order)
            else:
                # Se non c'è timestamp, mostra l'ordine (caso di fallback)
                ordini_per_reparto[codice_reparto]['pronti'].append(order)
    
    # Crea un hash dei dati attuali per confronto
    import hashlib
    data_string = ""
    for codice_reparto, reparto_data in ordini_per_reparto.items():
        data_string += f"{codice_reparto}:"
        for status in ["in_preparazione", "pronti"]:
            for order in reparto_data[status]:
                data_string += f"{order['numero_ordine']}_{order['seriale']}_{status}|"
    
    current_hash = hashlib.md5(data_string.encode()).hexdigest()
    
    # Controlla se c'è un hash precedente salvato
    previous_hash = app.config.get("DISPLAY_DATA_HASH")
    
    # Salva il nuovo hash
    app.config["DISPLAY_DATA_HASH"] = current_hash
    
    # Determina se ci sono cambiamenti
    has_changes = previous_hash is not None and previous_hash != current_hash
    
    return jsonify({
        "has_changes": has_changes,
        "timestamp": datetime.now().isoformat(),
        "current_hash": current_hash,
        "previous_hash": previous_hash
    })


# ------------------------------------------------------------------
# 8) Bootstrap locale
# ------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        # Carica sempre tutti gli ordini all'avvio
        print("🚀 Avvio applicazione - Caricamento ordini...")
        refresh_orders()
        refresh_stock()
        print(f"✅ Caricati {len(app.config['ORDERS_CACHE'])} ordini")

    app.run(host="0.0.0.0", port=5000, debug=True)

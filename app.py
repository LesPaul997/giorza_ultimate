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
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List
from datetime import datetime
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
from models import OrderEdit, OrderStatus, OrderStatusByReparto, OrderRead, OrderNote, ChatMessage, User, ModifiedOrderLine, UnavailableLine, OrderAttachment, DeliveryAddress, DeliveryRoute, FuelCost, PartialOrderResidue, ArticoloReparto, db  # noqa: E402  pylint: disable=wrong-import-position

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
            reparti_ordine = get_ordine_reparti(seriale)
            
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
    
    # Ordina per numero ordine (maggiore prima)
    sorted_orders = sorted(
        filtered_orders, 
        key=lambda x: int(x["numero_ordine"]) if x["numero_ordine"] and str(x["numero_ordine"]).isdigit() else 0, 
        reverse=True
    )
    
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
    
    # 3. Applica gli stati agli ordini
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
            
            # Aggiungi stati 'nuovo' per reparti mancanti
            reparti_ordine = get_ordine_reparti(seriale)
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
                order["my_reparto_status"] = status_by_reparto.get(current_user.reparto, {})
            else:
                # Per i cassiere, aggiungi un riassunto dello stato
                status_summary = []
                for reparto in reparti_ordine:
                    reparto_status = status_by_reparto.get(reparto, {})
                    status_summary.append(f"{reparto}: {reparto_status.get('status', 'nuovo')}")
                order["status_summary"] = " | ".join(status_summary)
    
    # Ordina gli ordini per numero ordine (maggiore prima)
    # Converte numero_ordine in int per ordinamento corretto
    sorted_orders = sorted(
        unique_orders.values(), 
        key=lambda x: int(x["numero_ordine"]) if x["numero_ordine"] and str(x["numero_ordine"]).isdigit() else 0, 
        reverse=True
    )
    
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
    sorted_orders = sorted(
        orders_with_status, 
        key=lambda x: (x["data_ordine"], x["numero_ordine"]), 
        reverse=True
    )
    
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
    sorted_orders = sorted(
        orders_with_status, 
        key=lambda x: (x["data_ordine"], x["numero_ordine"]), 
        reverse=True
    )
    
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


# ------- ORDER NOTES ---------------
@app.route("/api/order/<seriale>/notes")
@login_required
def get_order_notes(seriale):
    notes = OrderNote.query.filter_by(seriale=seriale).order_by(OrderNote.timestamp.desc()).all()
    return jsonify([{
        'id': note.id,
        'seriale': note.seriale,
        'articolo': note.articolo,
        'operatore': note.operatore,
        'nota': note.nota,
        'timestamp': note.timestamp.isoformat()
    } for note in notes])


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
    sorted_orders = sorted(
        unique_orders.values(), 
        key=lambda x: (x["data_ordine"], x["numero_ordine"]), 
        reverse=True
    )
    
    # Ottieni le tratte attive
    active_routes = DeliveryRoute.query.filter(
        DeliveryRoute.stato.in_(['pianificata', 'in_corso'])
    ).order_by(DeliveryRoute.timestamp.desc()).all()
    
    # Ottieni il prezzo del carburante più recente
    latest_fuel_cost = FuelCost.query.order_by(FuelCost.data_aggiornamento.desc()).first()
    fuel_price = latest_fuel_cost.prezzo_litro if latest_fuel_cost else 1.80  # Default
    
    return render_template("trasporti_dashboard.html", 
                         orders=sorted_orders,
                         active_routes=active_routes,
                         fuel_price=fuel_price)


@app.route('/pianifica-tratta')
@login_required
def pianifica_tratta():
    """Pagina per la pianificazione di nuove tratte"""
    if current_user.role != 'trasporti':
        abort(403)
    return render_template('pianifica_tratta.html')


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
    sorted_orders = sorted(
        unique_orders.values(), 
        key=lambda x: (x["data_ordine"], x["numero_ordine"]), 
        reverse=True
    )
    
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
    
    # Aggiungi informazioni per ogni ordine
    for seriale, order in unique_orders.items():
        # Controlla se è un ordine di consegna
        note = order.get("ritiro", "").lower()
        order["is_consegna"] = "consegna" in note
        
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
        
        # Controlla se l'ordine ha un trasporto associato
        delivery_route = DeliveryRoute.query.filter(
            DeliveryRoute.ordini_seriali.contains(seriale)
        ).first()
        order["has_transport"] = delivery_route is not None
        if delivery_route:
            order["transport_id"] = delivery_route.id
            order["transport_status"] = delivery_route.stato
            order["transport_date"] = delivery_route.data_consegna.isoformat() if delivery_route.data_consegna else None
    
    # Ordina gli ordini per data (più recente prima) e numero ordine (maggiore prima)
    sorted_orders = sorted(
        unique_orders.values(), 
        key=lambda x: (x["data_ordine"], x["numero_ordine"]), 
        reverse=True
    )
    
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
        print(f"🔍 Tentativo geocoding per: {indirizzo_completo}")
        
        # Usa Nominatim (OpenStreetMap) per il geocoding gratuito
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': indirizzo_completo,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        
        print(f"🌐 URL richiesta: {url}")
        print(f"📋 Parametri: {params}")
        
        headers = {
            'User-Agent': 'EstazioneOrdini/1.0 (https://github.com/estazione-ordini; trasporti@example.com)'
        }
        
        # Aggiungi un delay per rispettare i limiti di rate
        time.sleep(1)
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        print(f"📡 Status code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"📄 Risposta JSON: {data}")
            
            if data and len(data) > 0:
                coordinate_lat = float(data[0]['lat'])
                coordinate_lng = float(data[0]['lon'])
                print(f"✅ Geocoding riuscito per {indirizzo_completo}: {coordinate_lat}, {coordinate_lng}")
            else:
                print(f"❌ Geocoding fallito per {indirizzo_completo}: nessun risultato")
                # Prova con un indirizzo più semplice
                indirizzo_semplice = f"{citta}, {provincia}, Italia"
                print(f"🔄 Retry con indirizzo semplificato: {indirizzo_semplice}")
                
                time.sleep(1)  # Delay per il retry
                params['q'] = indirizzo_semplice
                response2 = requests.get(url, params=params, headers=headers, timeout=10)
                if response2.status_code == 200:
                    data2 = response2.json()
                    if data2 and len(data2) > 0:
                        coordinate_lat = float(data2[0]['lat'])
                        coordinate_lng = float(data2[0]['lon'])
                        print(f"✅ Geocoding riuscito con indirizzo semplificato: {coordinate_lat}, {coordinate_lng}")
                    else:
                        print(f"❌ Anche il retry è fallito")
                else:
                    print(f"❌ Errore nel retry: {response2.status_code}")
        elif response.status_code == 403:
            print(f"❌ Nominatim ha bloccato la richiesta (403). Usando coordinate di default.")
            # Usa coordinate approssimative per l'Italia
            coordinate_lat = 41.9028  # Roma
            coordinate_lng = 12.4964
        else:
            print(f"❌ Errore geocoding per {indirizzo_completo}: {response.status_code}")
            print(f"📄 Risposta: {response.text}")
    except Exception as e:
        print(f"❌ Errore durante il geocoding: {e}")
        import traceback
        traceback.print_exc()
        # Continua senza coordinate
    
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
        print(f"🔍 Tentativo geocoding per: {indirizzo_completo}")
        
        # Usa Nominatim (OpenStreetMap) per il geocoding gratuito
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': indirizzo_completo,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1
        }
        
        print(f"🌐 URL richiesta: {url}")
        print(f"📋 Parametri: {params}")
        
        headers = {
            'User-Agent': 'EstazioneOrdini/1.0 (https://github.com/estazione-ordini; trasporti@example.com)'
        }
        
        # Aggiungi un delay per rispettare i limiti di rate
        time.sleep(1)
        
        response = requests.get(url, params=params, headers=headers, timeout=10)
        print(f"📡 Status code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"📄 Risposta JSON: {data}")
            
            if data and len(data) > 0:
                coordinate_lat = float(data[0]['lat'])
                coordinate_lng = float(data[0]['lon'])
                print(f"✅ Geocoding riuscito per {indirizzo_completo}: {coordinate_lat}, {coordinate_lng}")
            else:
                print(f"❌ Geocoding fallito per {indirizzo_completo}: nessun risultato")
                # Prova con un indirizzo più semplice
                indirizzo_semplice = f"{citta}, {provincia}, Italia"
                print(f"🔄 Retry con indirizzo semplificato: {indirizzo_semplice}")
                
                time.sleep(1)  # Delay per il retry
                params['q'] = indirizzo_semplice
                response2 = requests.get(url, params=params, headers=headers, timeout=10)
                if response2.status_code == 200:
                    data2 = response2.json()
                    if data2 and len(data2) > 0:
                        coordinate_lat = float(data2[0]['lat'])
                        coordinate_lng = float(data2[0]['lon'])
                        print(f"✅ Geocoding riuscito con indirizzo semplificato: {coordinate_lat}, {coordinate_lng}")
                    else:
                        print(f"❌ Anche il retry è fallito")
                else:
                    print(f"❌ Errore nel retry: {response2.status_code}")
        elif response.status_code == 403:
            print(f"❌ Nominatim ha bloccato la richiesta (403). Usando coordinate di default.")
            # Usa coordinate approssimative per l'Italia
            coordinate_lat = 41.9028  # Roma
            coordinate_lng = 12.4964
        else:
            print(f"❌ Errore geocoding per {indirizzo_completo}: {response.status_code}")
            print(f"📄 Risposta: {response.text}")
    except Exception as e:
        print(f"❌ Errore durante il geocoding: {e}")
        import traceback
        traceback.print_exc()
        # Continua senza coordinate
    
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


@app.route("/api/trasporti/calculate-route", methods=["POST"])
@login_required
def calculate_route():
    """Calcola una tratta di consegna"""
    if current_user.role != 'trasporti':
        abort(403)
    
    data = request.get_json()
    ordini_seriali = data.get("ordini_seriali", [])
    indirizzi_selezionati = data.get("indirizzi_selezionati", [])  # Lista di ID indirizzi
    indirizzo_partenza = data.get("indirizzo_partenza")
    truck_id = data.get("truck_id")
    
    print(f"🔍 DEBUG CALCOLO TRATTA:")
    print(f"  Indirizzo partenza: {indirizzo_partenza}")
    print(f"  Ordini seriali: {ordini_seriali}")
    print(f"  Indirizzi selezionati: {indirizzi_selezionati}")
    print(f"  ID Camion: {truck_id}")
    
    if not ordini_seriali or not indirizzo_partenza:
        return jsonify({"error": "Dati mancanti"}), 400
    
    # Ottieni gli indirizzi di consegna selezionati
    delivery_addresses = []
    if indirizzi_selezionati:
        # Se sono stati selezionati indirizzi specifici, usa quelli
        for address_id in indirizzi_selezionati:
            address = DeliveryAddress.query.get(address_id)
            if address:
                delivery_addresses.append({
                    "id": address.id,
                    "seriale": address.seriale,
                    "indirizzo": f"{address.indirizzo}, {address.citta}",
                    "lat": address.coordinate_lat,
                    "lng": address.coordinate_lng
                })
    else:
        # Altrimenti usa il primo indirizzo di ogni ordine
        for seriale in ordini_seriali:
            address = DeliveryAddress.query.filter_by(seriale=seriale).first()
            if address:
                delivery_addresses.append({
                    "id": address.id,
                    "seriale": seriale,
                    "indirizzo": f"{address.indirizzo}, {address.citta}",
                    "lat": address.coordinate_lat,
                    "lng": address.coordinate_lng
                })
    
    if not delivery_addresses:
        return jsonify({"error": "Nessun indirizzo di consegna trovato"}), 400
    
    print(f"📦 Indirizzi di consegna trovati:")
    for i, addr in enumerate(delivery_addresses):
        print(f"  {i+1}. {addr['indirizzo']} - Coordinate: ({addr.get('lat')}, {addr.get('lng')})")
    
    # Calcolo della distanza usando le coordinate (formula di Haversine)
    def haversine_distance(lat1, lon1, lat2, lon2):
        """Calcola la distanza tra due punti usando la formula di Haversine"""
        # Raggio della Terra in km
        R = 6371
        
        # Converti gradi in radianti
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        
        # Differenze delle coordinate
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        
        # Formula di Haversine
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        return R * c
    
    # Calcola la distanza totale (andata + ritorno)
    distanza_totale_km = 0
    tempo_stimato_minuti = 0
    
    # Geocoding dell'indirizzo di partenza per ottenere le coordinate reali
    partenza_lat = None
    partenza_lon = None
    
    try:
        import requests
        import time
        
        # Usa Nominatim per geocoding dell'indirizzo di partenza
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': f"{indirizzo_partenza}, Italia",
            'format': 'json',
            'limit': 1
        }
        
        headers = {
            'User-Agent': 'EstazioneOrdini/1.0 (trasporti@example.com)'
        }
        
        time.sleep(1)  # Rispetta i limiti di rate
        response = requests.get(url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data and len(data) > 0:
                partenza_lat = float(data[0]['lat'])
                partenza_lon = float(data[0]['lon'])
                print(f"✅ Geocoding partenza riuscito: {indirizzo_partenza} -> ({partenza_lat}, {partenza_lon})")
            else:
                print(f"❌ Geocoding partenza fallito per: {indirizzo_partenza}")
        else:
            print(f"❌ Errore geocoding partenza: {response.status_code}")
    except Exception as e:
        print(f"❌ Errore durante geocoding partenza: {e}")
    
    # Se il geocoding fallisce, usa coordinate di default
    if partenza_lat is None or partenza_lon is None:
        partenza_lat = 40.7458  # Nocera Inferiore (default)
        partenza_lon = 14.6464
        print(f"⚠️ Usando coordinate di default per partenza: ({partenza_lat}, {partenza_lon})")
    
    # Calcola distanza per ogni consegna (solo andata)
    for i, delivery in enumerate(delivery_addresses):
        print(f"Consegna {i+1}: {delivery['indirizzo']}")
        print(f"  Coordinate: lat={delivery.get('lat')}, lng={delivery.get('lng')}")
        
        if delivery.get('lat') and delivery.get('lng'):
            # Calcola distanza dal punto di partenza (o dalla consegna precedente)
            if i == 0:
                # Prima consegna: dal punto di partenza
                dist = haversine_distance(partenza_lat, partenza_lon, delivery['lat'], delivery['lng'])
                print(f"  Distanza da partenza ({partenza_lat}, {partenza_lon}): {dist:.2f} km")
            else:
                # Conseguenti: dalla consegna precedente
                prev_delivery = delivery_addresses[i-1]
                dist = haversine_distance(prev_delivery['lat'], prev_delivery['lng'], delivery['lat'], delivery['lng'])
                print(f"  Distanza da consegna precedente: {dist:.2f} km")
            
            distanza_totale_km += dist
            # Stima tempo: 30 minuti per consegna + tempo di viaggio (60 km/h media)
            tempo_viaggio = (dist / 60) * 60  # minuti
            tempo_stimato_minuti += tempo_viaggio + 30
            print(f"  Tempo stimato per questa consegna: {tempo_viaggio + 30} minuti")
        else:
            # Se non ci sono coordinate, usa una stima fissa
            print(f"  Nessuna coordinata disponibile, usando stima fissa")
            distanza_totale_km += 25
            tempo_stimato_minuti += 30
    
    print(f"📊 CALCOLO SOLO ANDATA:")
    print(f"  Distanza totale (solo andata): {distanza_totale_km:.2f} km")
    print(f"  Tempo stimato (solo andata): {tempo_stimato_minuti:.0f} minuti")
    
    # Logica prezzi fissi per camion
    print(f"🚛 CALCOLO PREZZI FISSI:")
    print(f"  Camion ID: {truck_id}")
    print(f"  Distanza totale: {distanza_totale_km:.2f} km")
    
    # Prezzi fissi per camion (entro 10km)
    prezzi_fissi_camion = {
        "1": 50,  # FIAT 35F Daily
        "2": 50,  # IVECO DAILY 35-180
        "3": 75,  # Mercedes Sprinter (esempio)
        "4": 100, # Altri camion (esempio)
    }
    
    # Supplemento gru (€40)
    supplemento_gru = 40
    
    # Prezzo fisso del camion selezionato
    prezzo_fisso_camion = prezzi_fissi_camion.get(truck_id, 50)  # Default €50
    prezzo_fisso_totale = prezzo_fisso_camion + supplemento_gru
    
    print(f"  Prezzo fisso camion: €{prezzo_fisso_camion}")
    print(f"  Supplemento gru: €{supplemento_gru}")
    print(f"  Prezzo fisso totale: €{prezzo_fisso_totale}")
    
    # Calcolo costo carburante solo se oltre 10km
    if distanza_totale_km <= 10:
        # Entro 10km: solo prezzo fisso
        litri_totali = 0
        costo_carburante_euro = 0
        prezzo_litro = 0
        print(f"  ✅ Entro 10km: solo prezzo fisso €{prezzo_fisso_totale}")
    else:
        # Oltre 10km: prezzo fisso + consumo extra
        km_extra = distanza_totale_km - 10
        consumo_litro_per_km = 1 / 3  # 3 km/l = 1/3 l/km
        litri_totali = km_extra * consumo_litro_per_km
    
    # Ottieni il prezzo del carburante
    latest_fuel_cost = FuelCost.query.order_by(FuelCost.data_aggiornamento.desc()).first()
    prezzo_litro = latest_fuel_cost.prezzo_litro if latest_fuel_cost else 1.85
    
    # Aggiungi il costo dell'operatore (€3.15 per litro)
    prezzo_litro_con_operatore = prezzo_litro + 3.15
    
    costo_carburante_euro = litri_totali * prezzo_litro_con_operatore
    
    if distanza_totale_km > 10:
        print(f"  📈 Oltre 10km:")
        print(f"    Km extra: {km_extra:.2f}")
        print(f"    Litri extra: {litri_totali:.2f}")
        print(f"    Prezzo litro: €{prezzo_litro}")
        print(f"    Costo carburante extra: €{costo_carburante_euro:.2f}")
    
    # Prezzo totale finale
    prezzo_totale = prezzo_fisso_totale + costo_carburante_euro
    
    print(f"📊 RISULTATO CALCOLO:")
    print(f"  Distanza totale: {distanza_totale_km:.2f} km")
    print(f"  Tempo stimato: {tempo_stimato_minuti:.0f} minuti")
    print(f"  Litri totali: {litri_totali:.2f}")
    print(f"  Prezzo litro: €{prezzo_litro}")
    print(f"  Costo carburante: €{costo_carburante_euro:.2f}")
    print(f"  Prezzo totale: €{prezzo_totale:.2f}")
    
    return jsonify({
        "success": True,
        "distanza_totale_km": round(distanza_totale_km, 2),
        "tempo_stimato_minuti": math.ceil(tempo_stimato_minuti),
        "costo_carburante_euro": round(costo_carburante_euro, 2),
        "litri_totali": round(litri_totali, 2),
        "prezzo_litro": prezzo_litro,
        "prezzo_totale": round(prezzo_totale, 2),
        "delivery_addresses": delivery_addresses
    })


@app.route("/api/trasporti/save-route", methods=["POST"])
@login_required
def save_route():
    """Salva una tratta di consegna"""
    if current_user.role != 'trasporti':
        abort(403)
    
    nome_tratta = request.form.get("nome_tratta")
    ordini_seriali = request.form.get("ordini_seriali")
    indirizzo_partenza = request.form.get("indirizzo_partenza")
    indirizzi_consegna = request.form.get("indirizzi_consegna")
    distanza_totale_km = request.form.get("distanza_totale_km")
    tempo_stimato_minuti = request.form.get("tempo_stimato_minuti")
    costo_carburante_euro = request.form.get("costo_carburante_euro")
    autista = request.form.get("autista")
    mezzo = request.form.get("mezzo")
    data_consegna = request.form.get("data_consegna")
    note = request.form.get("note")
    
    if not all([nome_tratta, ordini_seriali, indirizzo_partenza]):
        return jsonify({"error": "Campi obbligatori mancanti"}), 400
    
    # Crea la nuova tratta
    delivery_route = DeliveryRoute(
        nome_tratta=nome_tratta,
        ordini_seriali=ordini_seriali,
        indirizzo_partenza=indirizzo_partenza,
        indirizzi_consegna=indirizzi_consegna or "",
        distanza_totale_km=float(distanza_totale_km) if distanza_totale_km else None,
        tempo_stimato_minuti=math.ceil(float(tempo_stimato_minuti)) if tempo_stimato_minuti else None,
        costo_carburante_euro=float(costo_carburante_euro) if costo_carburante_euro else None,
        autista=autista,
        mezzo=mezzo,
        data_consegna=datetime.strptime(data_consegna, '%Y-%m-%d').date() if data_consegna else None,
        note=note,
        operatore=current_user.username
    )
    
    db.session.add(delivery_route)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "id": delivery_route.id,
        "message": "Tratta salvata con successo"
    })


@app.route("/api/trasporti/fuel-cost", methods=["POST"])
@login_required
def update_fuel_cost():
    """Aggiorna il prezzo del carburante"""
    if current_user.role != 'trasporti':
        abort(403)
    
    tipo_carburante = request.form.get("tipo_carburante", "diesel")
    prezzo_litro = request.form.get("prezzo_litro")
    
    if not prezzo_litro:
        return jsonify({"error": "Prezzo obbligatorio"}), 400
    
    try:
        prezzo_litro = float(prezzo_litro)
    except ValueError:
        return jsonify({"error": "Prezzo non valido"}), 400
    
    # Crea il nuovo record
    fuel_cost = FuelCost(
        tipo_carburante=tipo_carburante,
        prezzo_litro=prezzo_litro,
        data_aggiornamento=datetime.now().date(),
        operatore=current_user.username
    )
    
    db.session.add(fuel_cost)
    db.session.commit()
    
    return jsonify({
        "success": True,
        "message": f"Prezzo {tipo_carburante} aggiornato a €{prezzo_litro}/l"
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

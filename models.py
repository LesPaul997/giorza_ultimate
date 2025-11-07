from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'picker', 'cassiere', 'cassa', 'display', 'trasporti'
    reparto = db.Column(db.String(20), nullable=True)  # Codice reparto (es. 'REP05')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class OrderEdit(db.Model):
    __tablename__ = "order_edits"
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    articolo = db.Column(db.String(50), nullable=False)
    quantita_nuova = db.Column(db.Float, nullable=False)
    unita_misura = db.Column(db.String(10), nullable=False)
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    applied = db.Column(db.Boolean, default=False)


class OrderStatus(db.Model):
    __tablename__ = 'order_status'
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False, unique=True)
    status = db.Column(db.String(50), nullable=False)  # 'nuovo', 'letto', 'materiale_non_disponibile', 'in_preparazione', 'pronto'
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())


class OrderStatusByReparto(db.Model):
    """Tabella per tracciare lo stato degli ordini per reparto specifico."""
    __tablename__ = 'order_status_by_reparto'
    
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    reparto = db.Column(db.String(20), nullable=False)  # Codice reparto (es. 'REP05')
    status = db.Column(db.String(50), nullable=False)  # 'nuovo', 'letto', 'materiale_non_disponibile', 'in_preparazione', 'pronto'
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    
    __table_args__ = (
        db.Index('idx_seriale_reparto', 'seriale', 'reparto'),
        db.UniqueConstraint('seriale', 'reparto', name='uq_seriale_reparto'),
    )
    
    def __repr__(self):
        return f'<OrderStatusByReparto {self.seriale} - {self.reparto} -> {self.status}>'


class OrderRead(db.Model):
    __tablename__ = 'order_reads'
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    __table_args__ = (
        db.Index('idx_seriale_operatore', 'seriale', 'operatore'),
    )


class OrderNote(db.Model):
    __tablename__ = 'order_notes'
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    articolo = db.Column(db.String(50), nullable=True)  # NULL per note dell'ordine intero
    operatore = db.Column(db.String(50), nullable=False)
    nota = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    __table_args__ = (
        db.Index('idx_seriale_articolo', 'seriale', 'articolo'),
    )


class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(50), nullable=False)
    recipient = db.Column(db.String(50), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    read = db.Column(db.Boolean, default=False)
    __table_args__ = (
        db.Index('idx_sender_recipient', 'sender', 'recipient'),
        db.Index('idx_recipient_read', 'recipient', 'read'),
    )


class ArticoloReparto(db.Model):
    """Tabella per la mappatura articolo -> reparto."""
    __tablename__ = 'articoli_reparti'
    
    id = db.Column(db.Integer, primary_key=True)
    codice_articolo = db.Column(db.String(50), nullable=False, unique=True)
    tipo_collo_1 = db.Column(db.String(20), nullable=True)  # Codice reparto principale
    tipo_collo_2 = db.Column(db.String(20), nullable=True)  # Codice reparto secondario
    unita_misura_2 = db.Column(db.String(10), nullable=True)  # Seconda unità di misura
    operatore_conversione = db.Column(db.String(1), nullable=True)  # Operatore (moltiplicazione o divisione)
    fattore_conversione = db.Column(db.Float, nullable=True)  # Fattore di conversione
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<ArticoloReparto {self.codice_articolo} -> {self.tipo_collo_1}>'


class ModifiedOrderLine(db.Model):
    """Tabella per salvare le righe modificate/cancellate degli ordini."""
    __tablename__ = 'modified_order_lines'
    
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    codice_articolo = db.Column(db.String(50), nullable=False)
    descrizione_articolo = db.Column(db.String(200), nullable=True)
    descrizione_supplementare = db.Column(db.Text, nullable=True)
    quantita = db.Column(db.Float, nullable=False)
    unita_misura = db.Column(db.String(10), nullable=False)
    unita_misura_2 = db.Column(db.String(10), nullable=True)
    quantita_um2 = db.Column(db.Float, nullable=True)
    operatore_conversione = db.Column(db.String(1), nullable=True)
    fattore_conversione = db.Column(db.Float, nullable=True)
    prezzo_unitario = db.Column(db.Float, nullable=True)
    codice_reparto = db.Column(db.String(20), nullable=True)
    data_ordine = db.Column(db.String(20), nullable=True)
    numero_ordine = db.Column(db.String(20), nullable=True)
    nome_cliente = db.Column(db.String(200), nullable=True)
    ritiro = db.Column(db.String(200), nullable=True)
    data_arrivo = db.Column(db.String(20), nullable=True)
    removed = db.Column(db.Boolean, default=True)  # True = riga rimossa
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        db.Index('idx_seriale_codice', 'seriale', 'codice_articolo'),
    )

    def __repr__(self):
        return f'<ModifiedOrderLine {self.seriale} - {self.codice_articolo} (removed: {self.removed})>'


class UnavailableLine(db.Model):
    """Righe segnate come non disponibili con eventuale sostituzione proposta."""
    __tablename__ = 'unavailable_lines'

    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    codice_articolo = db.Column(db.String(50), nullable=False)
    reparto = db.Column(db.String(20), nullable=True)
    unavailable = db.Column(db.Boolean, default=False)
    substitution_text = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_unavail_seriale_articolo_reparto', 'seriale', 'codice_articolo', 'reparto'),
    )

    def __repr__(self):
        return f'<UnavailableLine {self.seriale} - {self.codice_articolo} ({"ND" if self.unavailable else "OK"})>'

class OrderAttachment(db.Model):
    """Tabella per salvare gli allegati degli ordini (foto, documenti, etc.)"""
    __tablename__ = 'order_attachments'
    
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    articolo = db.Column(db.String(50), nullable=True)  # può essere None per allegati dell'ordine
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    operatore = db.Column(db.String(50), nullable=False)
    note = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<OrderAttachment {self.seriale} - {self.original_filename}>'


class DeliveryAddress(db.Model):
    """Tabella per salvare gli indirizzi di consegna degli ordini"""
    __tablename__ = 'delivery_addresses'
    
    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    indirizzo = db.Column(db.String(500), nullable=False)
    citta = db.Column(db.String(100), nullable=False)
    provincia = db.Column(db.String(10), nullable=False)
    cap = db.Column(db.String(10), nullable=False)
    coordinate_lat = db.Column(db.Float, nullable=True)  # Latitudine
    coordinate_lng = db.Column(db.Float, nullable=True)  # Longitudine
    note_indirizzo = db.Column(db.Text, nullable=True)
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<DeliveryAddress {self.seriale} - {self.indirizzo}, {self.citta}>'


class DeliveryRoute(db.Model):
    """Tabella per salvare le tratte di consegna"""
    __tablename__ = 'delivery_routes'
    
    id = db.Column(db.Integer, primary_key=True)
    nome_tratta = db.Column(db.String(100), nullable=False)
    ordini_seriali = db.Column(db.Text, nullable=False)  # Lista seriali separati da virgola
    indirizzo_partenza = db.Column(db.String(500), nullable=False)
    indirizzi_consegna = db.Column(db.Text, nullable=False)  # Lista indirizzi separati da |
    distanza_totale_km = db.Column(db.Float, nullable=True)
    tempo_stimato_minuti = db.Column(db.Integer, nullable=True)
    costo_carburante_euro = db.Column(db.Float, nullable=True)
    stato = db.Column(db.String(20), default='pianificata')  # 'pianificata', 'in_corso', 'completata'
    autista = db.Column(db.String(50), nullable=True)
    mezzo = db.Column(db.String(50), nullable=True)
    data_consegna = db.Column(db.Date, nullable=True)
    note = db.Column(db.Text, nullable=True)
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<DeliveryRoute {self.nome_tratta} - {self.stato}>'


class FuelCost(db.Model):
    """Tabella per i costi del carburante"""
    __tablename__ = 'fuel_costs'
    
    id = db.Column(db.Integer, primary_key=True)
    tipo_carburante = db.Column(db.String(20), nullable=False)  # 'diesel', 'benzina'
    prezzo_litro = db.Column(db.Float, nullable=False)
    data_aggiornamento = db.Column(db.Date, nullable=False)
    operatore = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<FuelCost {self.tipo_carburante} - €{self.prezzo_litro}/l>'


class PartialOrderResidue(db.Model):
    """Snapshot delle righe residue quando un reparto mette l'ordine a PRONTO.
    Ogni record rappresenta una riga con quantità ancora da evadere per un reparto specifico.
    """
    __tablename__ = 'partial_order_residues'

    id = db.Column(db.Integer, primary_key=True)
    seriale = db.Column(db.String(20), nullable=False)
    reparto = db.Column(db.String(20), nullable=False)
    numero_ordine = db.Column(db.String(20), nullable=True)
    nome_cliente = db.Column(db.String(200), nullable=True)
    codice_articolo = db.Column(db.String(50), nullable=False)
    descrizione_articolo = db.Column(db.String(200), nullable=True)
    residuo_quantita = db.Column(db.Float, nullable=False)
    unita_misura = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_partial_seriale_reparto', 'seriale', 'reparto'),
    )

    def __repr__(self) -> str:
        return f'<PartialResidue {self.seriale} {self.reparto} {self.codice_articolo} residuo={self.residuo_quantita}>'


# ============================================
# MODELLI PER "ORGANIZZA GIORNATA"
# ============================================

class CalendarioAppuntamento(db.Model):
    """Appuntamenti manuali nel calendario"""
    __tablename__ = 'calendario_appuntamenti'
    
    id = db.Column(db.Integer, primary_key=True)
    titolo = db.Column(db.String(200), nullable=False)
    descrizione = db.Column(db.Text, nullable=True)
    data = db.Column(db.Date, nullable=False, index=True)  # Indice per query veloci
    ora = db.Column(db.Time, nullable=True)
    colore = db.Column(db.String(20), default='blue')  # blue, red, green, yellow, purple
    creato_da = db.Column(db.String(80), nullable=False)  # username
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<CalendarioAppuntamento {self.titolo} {self.data}>'


class TodoItem(db.Model):
    """Task nella to-do list avanzata"""
    __tablename__ = 'todo_items'
    
    id = db.Column(db.Integer, primary_key=True)
    titolo = db.Column(db.String(200), nullable=False)
    descrizione = db.Column(db.Text, nullable=True)
    completato = db.Column(db.Boolean, default=False, index=True)  # Indice per filtri
    confermato = db.Column(db.Boolean, default=False)  # Flag di conferma (doppio check)
    priorita = db.Column(db.String(20), default='media')  # alta, media, bassa
    categoria = db.Column(db.String(50), nullable=True)  # Categoria/tag del task
    scadenza = db.Column(db.Date, nullable=True, index=True)  # Indice per filtri scadenza
    operatore_assegnato = db.Column(db.String(80), nullable=True, index=True)  # Operatore assegnato al task
    creato_da = db.Column(db.String(80), nullable=False, index=True)  # Chi ha creato il task
    completato_da = db.Column(db.String(80), nullable=True)  # Chi ha completato il task
    confermato_da = db.Column(db.String(80), nullable=True)  # Chi ha confermato il task
    data_completamento = db.Column(db.DateTime, nullable=True)  # Quando è stato completato
    data_conferma = db.Column(db.DateTime, nullable=True)  # Quando è stato confermato
    note_completamento = db.Column(db.Text, nullable=True)  # Note al completamento
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    ordine = db.Column(db.Integer, default=0)  # Per ordinare i task
    
    def __repr__(self):
        return f'<TodoItem {self.titolo} {self.completato}>'


class NoteAppunto(db.Model):
    """Note/appunti del cassiere (foglio unico)"""
    __tablename__ = 'note_appunti'
    
    id = db.Column(db.Integer, primary_key=True)
    contenuto = db.Column(db.Text, nullable=True)  # Testo completo delle note
    creato_da = db.Column(db.String(80), nullable=False, unique=True, index=True)  # Un solo foglio per utente, indice per query veloci
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<NoteAppunto {self.creato_da}>'


class AnnuncioUrgente(db.Model):
    """Annunci urgenti che scorrono nella navbar"""
    __tablename__ = 'annunci_urgenti'
    
    id = db.Column(db.Integer, primary_key=True)
    titolo = db.Column(db.String(200), nullable=False)
    messaggio = db.Column(db.Text, nullable=False)
    attivo = db.Column(db.Boolean, default=True, index=True)  # Indice per filtri attivi
    creato_da = db.Column(db.String(80), nullable=False)  # username
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)  # Indice per ordinamento
    scadenza = db.Column(db.DateTime, nullable=True, index=True)  # Indice per query scadenza
    
    def __repr__(self):
        return f'<AnnuncioUrgente {self.titolo} {self.attivo}>'

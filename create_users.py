from werkzeug.security import generate_password_hash
from app import app
from models import db, User
from reparti import get_all_reparti

with app.app_context():
    # Crea le tabelle se non esistono
    db.create_all()

    # Crea utenti per ogni reparto
    reparti = get_all_reparti()
    utenti_creati = []
    
    for codice, nome in reparti:
        username = f"picker_{codice.lower()}"
        if not User.query.filter_by(username=username).first():
            user = User(
                username=username,
                password_hash=generate_password_hash('pass'),
                role='picker',
                reparto=codice
            )
            db.session.add(user)
            utenti_creati.append(f"{username} ({nome})")
    
    # Crea anche un utente cassiere generico
    if not User.query.filter_by(username='cassier1').first():
        cassiere = User(
            username='cassier1',
            password_hash=generate_password_hash('pass'),
            role='cassa'
        )
        db.session.add(cassiere)
        utenti_creati.append('cassier1 (cassa)')
    
    # Crea utente display
    if not User.query.filter_by(username='display').first():
        display_user = User(
            username='display',
            password_hash=generate_password_hash('display'),
            role='display'
        )
        db.session.add(display_user)
        utenti_creati.append('display (display)')
    
    if utenti_creati:
        db.session.commit()
        print("✅ Utenti creati:")
        for utente in utenti_creati:
            print(f"   - {utente}")
    else:
        print("🔔 Tutti gli utenti sono già presenti")

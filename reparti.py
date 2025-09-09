# reparti.py
"""Gestione dei reparti disponibili nel sistema."""

# Dizionario dei reparti: codice -> nome
REPARTI = {
    'REP01': 'PROFILATI - LAMIERE - TUBOLARI',
    'REP02': 'EDILE', 
    'REP03': 'TRAVI',
    'REP04': 'COIBENTATI - RECINZIONI',
    'REP05': 'FERRAMENTA',
    'REP06': 'BOMBOLE'
}

# Dizionario inverso: nome -> codice
REPARTI_INVERSE = {nome: codice for codice, nome in REPARTI.items()}

def get_reparto_by_code(codice):
    """Restituisce il nome del reparto dato il codice."""
    return REPARTI.get(codice, 'Reparto Sconosciuto')

def get_code_by_reparto(nome):
    """Restituisce il codice del reparto dato il nome."""
    return REPARTI_INVERSE.get(nome)

def get_all_reparti():
    """Restituisce la lista di tutti i reparti."""
    return list(REPARTI.items())

def is_valid_reparto(codice):
    """Verifica se un codice reparto è valido."""
    return codice in REPARTI

def is_valid_display_reparto(codice):
    """Verifica se un codice reparto è valido per il display (esclude BOMBOLE e FERRAMENTA)."""
    display_reparti = {k: v for k, v in REPARTI.items() if k not in ['REP05', 'REP06']}
    return codice in display_reparti

def get_display_reparti():
    """Restituisce la lista dei reparti da mostrare nel display (esclude BOMBOLE e FERRAMENTA)."""
    display_reparti = {k: v for k, v in REPARTI.items() if k not in ['REP05', 'REP06']}
    return list(display_reparti.items()) 
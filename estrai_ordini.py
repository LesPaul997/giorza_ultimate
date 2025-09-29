# estrai_ordini.py
"""Estrae gli ordini clienti dal database Ad‑Hoc (demo) e li salva in CSV.

Correzioni principali rispetto alla versione originale
----------------------------------------------------
* carica il `.env` **prima** di leggere le variabili d'ambiente
* mantiene la logica incapsulata in `main()` (import‑safe)
* usa un fallback di connessione sovrascrivibile via ENV
* **query identica a quella già testata in produzione** (tabella DEMOCONTI)
"""

from __future__ import annotations

import os
import csv
import sys
from pathlib import Path

import pyodbc

# ------------------------------------------------------------------
# 1) .env & variabili
# ------------------------------------------------------------------
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(Path(__file__).with_name(".env"), override=False)
except ModuleNotFoundError:  # pragma: no cover
    pass

DEFAULT_CONNSTR = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=localhost\\SQLEXPRESS,1433;"
    "DATABASE=ahr_zarrella1;"
    "Trusted_Connection=yes;"
    "Encrypt=no;"
    "TrustServerCertificate=yes;"
)

CONN_STR = os.getenv("MSSQL_CONNSTRING_DEMO", DEFAULT_CONNSTR)
# Data di partenza per import ordini (YYYY-MM-DD). Default: 2025-09-01
ORDERS_FROM_DATE = os.getenv("ORDERS_FROM_DATE", "2025-09-01")
# print("Conn string:", CONN_STR)  # Rimosso per ridurre log

# ------------------------------------------------------------------
# 2) Query ordini (versione funzionante con DEMOCONTI)
# ------------------------------------------------------------------
QUERY = r"""
SELECT
    M.MVSERIAL                                AS seriale,
    M.MVNUMDOC                                AS numero_ordine,
    M.MVDATDOC                                AS data_ordine,
    COALESCE(
        NULLIF(LTRIM(RTRIM(M.MVCODORN)), ''),
        LTRIM(RTRIM(M.MVCODCON))
    )                                         AS cliente_codice,
    C.ANDESCRI                                AS nome_cliente,
    M.MV__NOTE                                AS note_cliente,
    M.MVDESDOC                                AS ritiro,
    D.MVCODART                                AS codice_articolo,
    D.MVCODART                                AS articolo,
	D.MVDESART								  AS descrizione_articolo,
	D.MVDESSUP								  AS descrizione_supplementare,
    D.MVQTAMOV                                AS quantita,
    D.MVUNIMIS                                AS unita_misura,
    D.MVPREZZO                                AS prezzo_unitario,
    FORMAT(D.MVDATEVA, 'dd/MM/yyyy')          AS data_evasione
    -- RIMOSSO: D.MVFLEVAS AS evasa_flag (non più necessario)
FROM ZARREDOC_MAST M
JOIN ZARREDOC_DETT D ON D.MVSERIAL = M.MVSERIAL
LEFT JOIN ZARRECONTI C ON C.ANCODICE = COALESCE(
        NULLIF(LTRIM(RTRIM(M.MVCODORN)), ''),
        LTRIM(RTRIM(M.MVCODCON))
    ) AND C.ANTIPCON = 'C'
WHERE M.MVTIPDOC = 'ORDCL'
  AND CONVERT(date, M.MVDATDOC) >= ?
ORDER BY M.MVSERIAL, D.CPROWNUM;
"""

# ------------------------------------------------------------------
# 3) estrazione & salvataggio CSV
# ------------------------------------------------------------------
CSV_PATH = Path(__file__).with_name("ordini_clienti.csv")


def estrai_ordini() -> None:
    """Esegue la query e salva il risultato in `ordini_clienti.csv`."""

    try:
        conn = pyodbc.connect(CONN_STR)
    except pyodbc.Error as exc:  # pragma: no cover
        sys.stderr.write(f"[estrai_ordini] Connessione fallita: {exc}\n")
        raise SystemExit(1) from exc

    with conn.cursor() as cur:
        # Passa la data come parametro per evitare SQL injection e problemi di formattazione
        cur.execute(QUERY, (ORDERS_FROM_DATE,))
        rows = cur.fetchall()
        headers = [col[0] for col in cur.description]

    # Elabora i dati per gestire i clienti generici
    processed_rows = []
    for row in rows:
        row_dict = dict(zip(headers, row))
        
        # Se il cliente è 1000, usa il campo note_cliente
        cliente_codice = str(row_dict.get('cliente_codice', '')).strip()
        note_cliente = str(row_dict.get('note_cliente', '')).strip()
        
        if cliente_codice == '000000000001000' or cliente_codice == '1000':
            # Usa il campo note_cliente come nome cliente se disponibile
            if note_cliente and note_cliente != '':
                row_dict['nome_cliente'] = note_cliente
                # print(f"🔄 Cliente generico sostituito: '{note_cliente}'")  # Debug log
        
        # Ricostruisce la riga nell'ordine originale
        processed_row = [row_dict.get(header, '') for header in headers]
        processed_rows.append(processed_row)

    # Scrive il CSV
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(processed_rows)

    # print(f"CSV generato: {CSV_PATH.relative_to(Path.cwd())}")  # Rimosso per ridurre log


def main() -> None:  # pragma: no cover
    estrai_ordini()


if __name__ == "__main__":
    main()

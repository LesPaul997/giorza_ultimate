# estrai_reparti.py
"""Estrae i dati dei reparti dalla tabella ZARREART_ICOL e li salva in CSV.

Questo script estrae solo i campi necessari per la mappatura articolo -> reparto:
- ARCODART: Codice articolo
- ARTIPCO1: Tipo collo 1 (codice reparto)
- ARTIPCO2: Tipo collo 2 (codice reparto secondario)
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
# print("→ Conn string:", CONN_STR)  # Rimosso per ridurre log

# ------------------------------------------------------------------
# 2) Query reparti (solo campi necessari)
# ------------------------------------------------------------------
QUERY = r"""
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

# ------------------------------------------------------------------
# 3) estrazione & salvataggio CSV
# ------------------------------------------------------------------
CSV_PATH = Path(__file__).with_name("reparti_articoli.csv")


def estrai_reparti() -> None:
    """Esegue la query e salva il risultato in `reparti_articoli.csv`."""

    try:
        conn = pyodbc.connect(CONN_STR)
    except pyodbc.Error as exc:  # pragma: no cover
        sys.stderr.write(f"[estrai_reparti] Connessione fallita: {exc}\n")
        raise SystemExit(1) from exc

    with conn.cursor() as cur:
        cur.execute(QUERY)
        rows = cur.fetchall()
        headers = [col[0] for col in cur.description]

    # Scrive il CSV
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    print(f"📊 CSV reparti generato: {CSV_PATH.relative_to(Path.cwd())} - {len(rows)} reparti estratti")


def main() -> None:  # pragma: no cover
    estrai_reparti()


if __name__ == "__main__":
    main() 
# estrai_magazzino.py
"""Estrae la giacenza di magazzino da Ad‐Hoc (db demo) e salva un CSV.

Correzioni rispetto alla versione precedente
-------------------------------------------
* il `.env` viene caricato **prima** di leggere le variabili d'ambiente
* la stringa di connessione ha un fallback unico, sovrascrivibile via ENV
* l'estrazione è incapsulata in `main()` così l'import non esegue side‑effect
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

    # Cerca prima un .env accanto allo script
    load_dotenv(Path(__file__).with_name(".env"), override=False)
except ModuleNotFoundError:
    # In produzione (Render) le variabili arrivano dall'environment
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
# 2) query saldo disponibile + descrizione (NON toccare)
# ------------------------------------------------------------------
QUERY = r"""
WITH Movimenti AS (
    /* === 1) MOVIMENTI MANUALI (DEF.) ====================== */
    SELECT
        MVM_DETT.MMCODMAG AS CODMAG,
        MVM_DETT.MMCODART AS CODART,
        /* carichi */
        MVM_DETT.MMQTAUM1 *
        ( CASE MVM_DETT.MMFLCASC WHEN '+' THEN 1 ELSE 0 END +
          CASE MVM_DETT.MMFLORDI WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END )  AS QTACAR,
        /* scarichi */
        MVM_DETT.MMQTAUM1 *
        ( CASE MVM_DETT.MMFLCASC WHEN '-' THEN 1 ELSE 0 END +
          CASE MVM_DETT.MMFLIMPE WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END +
          CASE MVM_DETT.MMFLRISE WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END )  AS QTASCA,
        'N' AS FLPROV
    FROM ZARREMVM_DETT MVM_DETT
    JOIN ZARREMVM_MAST MVM_MAST ON MVM_MAST.MMSERIAL = MVM_DETT.MMSERIAL
    WHERE COALESCE(MVM_DETT.MMCODMAG,'') <> ''

    UNION ALL
    /* === 2) DOCUMENTI (DEF./PROV.) ======================== */
    SELECT
        DOC_DETT.MVCODMAG AS CODMAG,
        DOC_DETT.MVCODART AS CODART,
        DOC_DETT.MVQTAUM1 *
        ( CASE DOC_DETT.MVFLCASC WHEN '+' THEN 1 ELSE 0 END +
          CASE DOC_DETT.MVFLORDI WHEN '+' THEN 1 WHEN '-' THEN -1 ELSE 0 END )  AS QTACAR,
        DOC_DETT.MVQTAUM1 *
        ( CASE DOC_DETT.MVFLCASC WHEN '-' THEN 1 ELSE 0 END +
          CASE DOC_DETT.MVFLIMPE WHEN '+' THEN 1 WHEN '-' THEN -1 ELSE 0 END +
          CASE DOC_DETT.MVFLRISE WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END )  AS QTASCA,
        DOC_MAST.MVFLPROV AS FLPROV
    FROM ZARREDOC_DETT DOC_DETT
    JOIN ZARREDOC_MAST DOC_MAST ON DOC_MAST.MVSERIAL = DOC_DETT.MVSERIAL
    WHERE DOC_DETT.MVTIPRIG = 'R'
      AND COALESCE(DOC_DETT.MVCODMAG,'') <> ''

    UNION ALL
    /* === 3) MOVIMENTI COLLEGATI (DEF.) ==================== */
    SELECT
        MVM_DETT.MMCODMAT AS CODMAG,
        MVM_DETT.MMCODART AS CODART,
        MVM_DETT.MMQTAUM1 *
        ( CASE MVM_DETT.MMF2CASC WHEN '+' THEN 1 ELSE 0 END +
          CASE MVM_DETT.MMF2ORDI WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END ) AS QTACAR,
        MVM_DETT.MMQTAUM1 *
        ( CASE MVM_DETT.MMF2CASC WHEN '-' THEN 1 ELSE 0 END +
          CASE MVM_DETT.MMF2IMPE WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END +
          CASE MVM_DETT.MMF2RISE WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END ) AS QTASCA,
        'N' AS FLPROV
    FROM ZARREMVM_DETT MVM_DETT
    JOIN ZARREMVM_MAST MVM_MAST ON MVM_MAST.MMSERIAL = MVM_DETT.MMSERIAL
    WHERE COALESCE(MVM_DETT.MMCODMAT,'') <> ''

    UNION ALL
    /* === 4) DOCUMENTI COLLEGATI (DEF./PROV.) ============== */
    SELECT
        DOC_DETT.MVCODMAT AS CODMAG,
        DOC_DETT.MVCODART AS CODART,
        DOC_DETT.MVQTAUM1 *
        ( CASE DOC_DETT.MVF2CASC WHEN '+' THEN 1 ELSE 0 END +
          CASE DOC_DETT.MVF2ORDI WHEN '+' THEN 1 WHEN '-' THEN -1 ELSE 0 END ) AS QTACAR,
        DOC_DETT.MVQTAUM1 *
        ( CASE DOC_DETT.MVF2CASC WHEN '-' THEN 1 ELSE 0 END +
          CASE DOC_DETT.MVF2IMPE WHEN '+' THEN 1 WHEN '-' THEN -1 ELSE 0 END +
          CASE DOC_DETT.MVF2RISE WHEN '-' THEN -1 WHEN '+' THEN 1 ELSE 0 END ) AS QTASCA,
        DOC_MAST.MVFLPROV AS FLPROV
    FROM ZARREDOC_DETT DOC_DETT
    JOIN ZARREDOC_MAST DOC_MAST ON DOC_MAST.MVSERIAL = DOC_DETT.MVSERIAL
    WHERE DOC_DETT.MVTIPRIG = 'R'
      AND COALESCE(DOC_DETT.MVCODMAT,'') <> ''
),

Saldi AS (
    SELECT
        CODMAG,
        CODART,
        SUM(QTACAR) - SUM(QTASCA) AS Saldo_Disponibile
    FROM Movimenti
    WHERE CODMAG = '1'      -- cambia / rimuovi filtro magazzino
      AND FLPROV = 'N'       -- esclude provvisori
    GROUP BY CODMAG, CODART
)

SELECT
    S.CODMAG,
    S.CODART,
    A.ARDESART        AS Descrizione_Articolo,
    S.Saldo_Disponibile
FROM Saldi S
LEFT JOIN ZARREART_ICOL A ON A.ARCODART = S.CODART
ORDER BY S.CODART;
"""

# ------------------------------------------------------------------
# 3) estrazione & salvataggio CSV
# ------------------------------------------------------------------
CSV_PATH = Path(__file__).with_name("magazzino.csv")


def estrai_magazzino() -> None:
    """Esegue la query e salva il risultato in `magazzino.csv`."""

    try:
        conn = pyodbc.connect(CONN_STR)
    except pyodbc.Error as exc:  # pragma: no cover
        sys.stderr.write(f"[estrai_magazzino] Connessione fallita: {exc}\n")
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

    # print(f"✅  CSV generato: {CSV_PATH.relative_to(Path.cwd())}")  # Rimosso per ridurre log


def main() -> None:  # pragma: no cover
    estrai_magazzino()


if __name__ == "__main__":
    main()

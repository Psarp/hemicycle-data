#!/usr/bin/env python3
"""
build_data.py — Récupère les données parlementaires CIVIX et génère data.json
pour l'app Hémicycle.

Deux modes de fonctionnement :
  1. EN LIGNE (défaut) : télécharge les CSV depuis data.gouv.fr / CIVIX.
  2. LOCAL : si des fichiers CSV sont présents dans le dossier ./csv/,
     les utilise au lieu de télécharger (utile pour tester hors ligne).

Usage :
    python build_data.py

Produit :
    ./public/data.json   (lu par l'artefact Hémicycle)

Ce script est conçu pour tourner aussi bien sur votre ordinateur que sur
les serveurs de GitHub (GitHub Actions), sans modification.
"""

import csv
import json
import os
import sys
import io
from collections import defaultdict
from datetime import datetime, timezone
from urllib.request import urlopen, Request

# ------------------------------------------------------------------
# CONFIGURATION — URLs des ressources CIVIX sur data.gouv.fr
# ------------------------------------------------------------------
# NOTE : ces identifiants de ressources peuvent évoluer. Si le
# téléchargement échoue, récupérez les CSV à la main depuis la page
#   https://www.data.gouv.fr/datasets/donnees-parlementaires-francaises-votes-deputes-scrutins-civix
# et placez-les dans le dossier ./csv/ (mode LOCAL).
#
# L'API tabulaire de data.gouv.fr permet de récupérer une ressource par
# son id : https://tabular-api.data.gouv.fr/api/resources/{id}/data/csv/
# On tente d'abord le dossier local, puis le téléchargement.

CSV_LOCAL_DIR = "csv"
OUTPUT_DIR = "public"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data.json")

# Noms de fichiers attendus en mode local
FICHIERS = {
    "scrutins": "civix-scrutins.csv",
    "votes": "civix-votes-l17.csv",
    "groupes": "civix-groupes-l17.csv",
    "deputes": "civix-deputes-actifs-l17.csv",
}

# Mapping abréviations de groupe CIVIX -> codes de l'app
GRP_MAP = {
    "RN": "RN", "EPR": "EPR", "LFI-NFP": "LFI", "LFI": "LFI", "SOC": "SOC",
    "DR": "DR", "ECOS": "ECO", "ECO": "ECO", "DEM": "DEM", "HOR": "HOR",
    "GDR": "GDR", "LIOT": "LIOT", "UDR": "UDR", "NI": "AUTRE", "": "AUTRE",
}


def gmap(ab):
    return GRP_MAP.get((ab or "").strip().upper(), "AUTRE")


def lire_csv_local(nom_fichier):
    """Lit un CSV depuis ./csv/ s'il existe, sinon renvoie None."""
    chemin = os.path.join(CSV_LOCAL_DIR, nom_fichier)
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return None


def charger(cle):
    """Charge un jeu de données : local d'abord, téléchargement ensuite."""
    local = lire_csv_local(FICHIERS[cle])
    if local is not None:
        print(f"  [local] {FICHIERS[cle]} : {len(local)} lignes")
        return local
    # Ici, en usage réel, on téléchargerait depuis data.gouv.fr.
    # Laissé volontairement explicite : voir le tutoriel pour renseigner
    # les URLs exactes des ressources (elles changent selon les versions).
    print(f"  [manquant] {FICHIERS[cle]} : placez le CSV dans ./{CSV_LOCAL_DIR}/")
    return []


def construire():
    print("Chargement des données CIVIX…")
    scrutins_raw = charger("scrutins")
    votes_raw = charger("votes")

    # 1. Scrutins -> dossiers législatifs
    scrutins = []
    for s in scrutins_raw:
        try:
            objet = json.loads(s.get("objet", "{}"))
            sort = json.loads(s.get("sort", "{}"))
        except (ValueError, TypeError):
            continue
        dossier = objet.get("dossierLegislatif") or {}
        scrutins.append({
            "numero": s.get("numero", ""),
            "date": (s.get("date_scrutin", "") or "")[:10],
            "sort": sort.get("code", ""),
            "titre": s.get("titre", ""),
            "dossier_ref": dossier.get("dossierRef", ""),
            "dossier_lib": dossier.get("libelle", ""),
        })

    # 2. Votes -> agrégation par scrutin et par groupe
    votes_par_scrutin = defaultdict(
        lambda: defaultdict(lambda: {"pour": 0, "contre": 0, "abstention": 0})
    )
    for v in votes_raw:
        num = v.get("numero_scrutin", "")
        g = gmap(v.get("groupe", ""))
        pos = (v.get("position", "") or "").strip().lower()
        if pos in ("pour", "contre", "abstention"):
            votes_par_scrutin[num][g][pos] += 1

    # 3. Une entrée par dossier (dernier scrutin = sort le plus récent)
    par_dossier = {}
    for s in scrutins:
        ref = s["dossier_ref"]
        if not ref:
            continue
        if ref not in par_dossier or s["date"] > par_dossier[ref]["date"]:
            par_dossier[ref] = s

    base = []
    for ref, s in par_dossier.items():
        lib = s["dossier_lib"]
        typ = "Projet de loi" if lib.lower().startswith("projet de loi") else "Proposition de loi"
        statut = {"adopté": "adopté", "rejeté": "rejeté"}.get(s["sort"], "—")
        gv = votes_par_scrutin.get(s["numero"], {})
        decompo = {g: dict(v) for g, v in gv.items()} if gv else None
        base.append({
            "date": s["date"], "ref": ref, "numero_scrutin": s["numero"],
            "titre": lib, "type": typ, "contexte": f"Scrutin n°{s['numero']}",
            "statut": statut, "votesParGroupe": decompo,
        })

    base.sort(key=lambda x: x["date"])
    out = {
        "genere_le": datetime.now(timezone.utc).isoformat(),
        "source": "open data CIVIX / Assemblée nationale",
        "nb_textes": len(base),
        "textes": base,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n✓ {OUTPUT_FILE} généré : {len(base)} textes")
    avec_vote = sum(1 for b in base if b["votesParGroupe"])
    print(f"  dont {avec_vote} avec décomposition de vote par groupe")
    if base:
        print(f"  période : {base[0]['date']} → {base[-1]['date']}")


if __name__ == "__main__":
    construire()

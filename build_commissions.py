#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_commissions.py — Brique DONNÉES pour les commissions (idées A + B + C).

Produit public/commissions.json :
  - la composition de chaque commission permanente (président, membres),
  - pour chaque commission, l'ENTONNOIR des textes qu'elle a EXAMINÉS
    (examiné en commission -> voté en séance -> issue), votés ou non.
Et enrichit public/data.json : chaque texte voté reçoit `commission` + `rapporteur`.

Détection par STRUCTURE (pas de codeActe en dur) :
  - une commission = un organe de type commission permanente (codeType COMPER),
  - la commission saisie d'un dossier = l'organe COMPER référencé dans ses actes,
  - le rapporteur = l'acteurRef (PA…) rattaché à cet acte.

Ce script est AUTO-VÉRIFIANT : il imprime un diagnostic complet de ce qu'il a
trouvé (types d'organes, commissions détectées, taux de résolution du rapporteur,
échantillon d'acte brut). On confirme la structure sur ce diagnostic AVANT de
bâtir l'interface. Réutilise le téléchargement de build_all.py (aucune nouvelle
dépendance).

Usage : python build_commissions.py           # run normal + diagnostic
        python build_commissions.py --local    # utilise les zips locaux de build_all
"""
import json, os, sys, re
from collections import defaultdict, Counter
from datetime import datetime, timezone

import build_all as BA                      # get_zip, URLS, LOCAL (import sans effet de bord)
from lib_commun import txt, ORGANE_TO_GROUPE

OUT_DIR   = "public"
DATA      = os.path.join(OUT_DIR, "data.json")
OUT       = os.path.join(OUT_DIR, "commissions.json")

# Codes de type d'organe considérés comme « commission permanente ».
# (Confirmé/élargi au 1er run via le diagnostic ci-dessous.)
CODES_COMMISSION = {"COMPER"}

PA_RE = re.compile(r"PA\d+")

# ---------------------------------------------------------------------------
# 1. Organes + acteurs (composition), depuis le jeu AMO
# ---------------------------------------------------------------------------
def charger_organes_et_membres(use_local):
    zf = BA.get_zip("acteurs", use_local)

    organes = {}                       # uid -> {libelle, abrege, codeType}
    diag_codetypes = Counter()
    for n in zf.namelist():
        if "/organe/PO" not in n or not n.endswith(".json"):
            continue
        try:
            o = json.loads(zf.read(n).decode("utf-8")).get("organe", {})
            uid = txt(o.get("uid"))
            ct  = txt(o.get("codeType"))
            organes[uid] = {"libelle": txt(o.get("libelle")),
                            "abrege":  txt(o.get("libelleAbrege")),
                            "codeType": ct}
            diag_codetypes[ct] += 1
        except Exception:
            continue

    commissions = {u: o for u, o in organes.items() if o["codeType"] in CODES_COMMISSION}

    # Composition : on parcourt les acteurs, on rattache aux commissions les
    # mandats EN COURS dont l'organe est une commission permanente.
    membres = defaultdict(list)         # organeUID -> [ {acteurRef, nom, groupe, fonction} ]
    diag_typeorg = Counter()
    for n in zf.namelist():
        if "/acteur/PA" not in n or not n.endswith(".json"):
            continue
        try:
            a = json.loads(zf.read(n).decode("utf-8")).get("acteur", {})
            uid = txt(a.get("uid"))
            ident = a.get("etatCivil", {}).get("ident", {})
            nom = (txt(ident.get("prenom")) + " " + txt(ident.get("nom"))).strip()
            mandats = a.get("mandats", {}).get("mandat", [])
            if isinstance(mandats, dict):
                mandats = [mandats]
            gp = ""
            for m in mandats:
                t = m.get("typeOrgane") or ""
                diag_typeorg[t] += 1
                ref = (m.get("organes") or {}).get("organeRef") or ""
                if isinstance(ref, dict):
                    ref = txt(ref.get("uid"))
                fin = m.get("dateFin")
                encours = (fin is None) or isinstance(fin, dict)
                if t == "GP" and encours and not gp:
                    gp = ORGANE_TO_GROUPE.get(ref, "AUTRE")
            # second passage : rattacher aux commissions (on a le groupe courant)
            for m in mandats:
                ref = (m.get("organes") or {}).get("organeRef") or ""
                if isinstance(ref, dict):
                    ref = txt(ref.get("uid"))
                fin = m.get("dateFin")
                encours = (fin is None) or isinstance(fin, dict)
                if encours and ref in commissions:
                    fonction = txt((m.get("infosQualite") or {}).get("libQualite")) or "Membre"
                    membres[ref].append({"acteurRef": uid, "nom": nom,
                                         "groupe": gp or "AUTRE", "fonction": fonction})
        except Exception:
            continue

    return organes, commissions, membres, diag_codetypes, diag_typeorg

# ---------------------------------------------------------------------------
# 2. Dossiers : commission au fond + rapporteur + examiné, par structure
# ---------------------------------------------------------------------------
def _valeurs_cle(node, cle, stop=("actesLegislatifs", "acteLegislatif")):
    """Collecte toutes les valeurs sous une clé donnée DANS l'acte courant,
    sans descendre dans les sous-actes (pour attribuer les refs au bon acte)."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in stop:
                continue
            if k == cle:
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, dict):
                    u = txt(v.get("uid"))
                    if u:
                        out.append(u)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            out.append(x)
                        elif isinstance(x, dict) and x.get("uid"):
                            out.append(txt(x.get("uid")))
            else:
                out += _valeurs_cle(v, cle, stop)
    elif isinstance(node, list):
        for x in node:
            out += _valeurs_cle(x, cle, stop)
    return out

def parcourir(node, commissions, trouve, diag):
    """Descend l'arbre d'actes. Pour chaque acte, si un organeRef pointe vers une
    commission, on l'enregistre (avec le rapporteur PA… et le texte associé)."""
    if isinstance(node, dict):
        diag["acte_keys"].update(node.keys())
        # organeRef repéré SUR l'acte courant (peu profond) -> identifie la commission
        organeRefs = [r for r in _valeurs_cle(node, "organeRef") if r in commissions]
        if organeRefs:
            ca = txt(node.get("codeActe"))
            diag["codeActe_commission"][ca] += 1
            if diag["sample_acte"] is None:
                diag["sample_acte"] = {k: v for k, v in node.items()
                                       if k not in ("actesLegislatifs", "acteLegislatif")}
            # rapporteur + rapport : cherchés dans TOUT le sous-arbre de cet acte
            # (le dépôt du rapport, qui porte l'acteurRef, est un sous-acte).
            acteurRefs = []
            for r in _valeurs_cle(node, "acteurRef", stop=()):
                acteurRefs += PA_RE.findall(r)
            textes_sous = _valeurs_cle(node, "texteAssocie", stop=())
            ta_rapport = next((x for x in textes_sous if (x or "").startswith("RAPP")), "")
            for cref in organeRefs:
                trouve[cref].append({
                    "codeActe": ca,
                    "date": txt(node.get("dateActe"))[:10],
                    "rapporteurRef": acteurRefs[0] if acteurRefs else "",
                    "texteAssocie": ta_rapport or txt(node.get("texteAssocie")),
                })
        sub = node.get("actesLegislatifs")
        if sub:
            parcourir(sub, commissions, trouve, diag)
        if "acteLegislatif" in node:
            parcourir(node["acteLegislatif"], commissions, trouve, diag)
    elif isinstance(node, list):
        for x in node:
            parcourir(x, commissions, trouve, diag)

def charger_dossiers(commissions, use_local):
    import zipfile
    zf = BA.get_zip("dossiers", use_local)
    resultats = {}       # dlr -> {titre, commission, rapporteurRef, examine, dateExamen}
    diag = {"acte_keys": set(), "codeActe_commission": Counter(),
            "sample_acte": None, "nb_dossiers": 0, "nb_avec_commission": 0}
    for n in zf.namelist():
        if "/dossierParlementaire/DLR" not in n or not n.endswith(".json"):
            continue
        try:
            d = json.loads(zf.read(n).decode("utf-8")).get("dossierParlementaire", {})
            uid = txt(d.get("uid"))
            titre = txt((d.get("titreDossier") or {}).get("titre"))
            diag["nb_dossiers"] += 1
            trouve = defaultdict(list)      # commissionUID -> [actes]
            parcourir(d.get("actesLegislatifs"), commissions, trouve, diag)
            if not trouve:
                continue
            diag["nb_avec_commission"] += 1
            # commission « au fond » = celle avec le plus d'actes (avis = moins fréquent)
            cref = max(trouve, key=lambda c: len(trouve[c]))
            actes = trouve[cref]
            rap = next((x["rapporteurRef"] for x in actes if x["rapporteurRef"]), "")
            a_rapport = any((x["texteAssocie"] or "").startswith("RAPP") for x in actes)
            dates = [x["date"] for x in actes if x["date"]]
            resultats[uid] = {
                "titre": titre,
                "commission": cref,
                "rapporteurRef": rap,
                "examine": bool(a_rapport or dates),   # examiné = rapport déposé ou acte daté
                "dateExamen": min(dates) if dates else "",
                "toutes_commissions": list(trouve.keys()),
            }
        except Exception:
            continue
    return resultats, diag

# ---------------------------------------------------------------------------
# 3. Assemblage + sorties + DIAGNOSTIC
# ---------------------------------------------------------------------------
def main():
    use_local = "--local" in sys.argv
    print("== Chargement organes + composition ==")
    organes, commissions, membres, diag_ct, diag_to = charger_organes_et_membres(use_local)
    print("== Parcours des dossiers (commission au fond + rapporteur) ==")
    dossiers, diag = charger_dossiers(commissions, use_local)

    # Données de vote déjà connues
    data = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {"textes": []}
    textes = {t["ref"]: t for t in data.get("textes", [])}

    def nom_acteur(ref):
        for c in membres.values():
            for m in c:
                if m["acteurRef"] == ref:
                    return m["nom"], m["groupe"]
        return "", ""

    # commissions.json : composition + entonnoir
    sortie = []
    for cuid, org in commissions.items():
        mbs = sorted(membres.get(cuid, []), key=lambda x: x["nom"])
        prez = next((m for m in mbs if "présid" in m["fonction"].lower()
                     and "vice" not in m["fonction"].lower()), None)
        textes_com = []
        for dlr, info in dossiers.items():
            if info["commission"] != cuid:
                continue
            t = textes.get(dlr)
            textes_com.append({
                "ref": dlr, "titre": info["titre"], "date": info["dateExamen"],
                "rapporteurRef": info["rapporteurRef"],
                "examine": info["examine"],
                "vote": bool(t),                      # présent dans data.json => voté
                "statut": (t or {}).get("statut", ""),
            })
        if not mbs and not textes_com:
            continue
        sortie.append({
            "uid": cuid, "nom": org["libelle"], "abrege": org["abrege"],
            "president": ({"acteurRef": prez["acteurRef"], "nom": prez["nom"]} if prez else None),
            "effectif": len(mbs),
            "membres": mbs,
            "nbTextesExamines": len(textes_com),
            "textes": sorted(textes_com, key=lambda x: x["date"], reverse=True),
        })
    sortie.sort(key=lambda c: c["nbTextesExamines"], reverse=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "commissions": sortie}, f, ensure_ascii=False, separators=(",", ":"))

    # Enrichir data.json (commission + rapporteur sur les textes votés)
    maj = 0
    for dlr, info in dossiers.items():
        t = textes.get(dlr)
        if not t:
            continue
        org = commissions.get(info["commission"], {})
        t["commission"] = {"uid": info["commission"], "nom": org.get("libelle", "")}
        rnom, rgp = nom_acteur(info["rapporteurRef"]) if info["rapporteurRef"] else ("", "")
        if info["rapporteurRef"]:
            t["rapporteur"] = {"acteurRef": info["rapporteurRef"], "nom": rnom, "groupe": rgp}
        maj += 1
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # ---------------- DIAGNOSTIC (à me recopier) ----------------
    print("\n" + "=" * 60)
    print("DIAGNOSTIC — à vérifier avant de bâtir l'interface")
    print("=" * 60)
    print("Types d'organes (codeType) les plus fréquents :")
    for ct, k in diag_ct.most_common(12):
        marque = "  <-- pris comme commission" if ct in CODES_COMMISSION else ""
        print(f"   {ct or '(vide)':12} : {k}{marque}")
    print(f"\nCommissions permanentes détectées : {len(commissions)}")
    for cuid, org in list(commissions.items())[:15]:
        print(f"   {org['abrege'] or org['libelle'][:40]:40} | {len(membres.get(cuid,[]))} membres")
    print("\nTypes de mandats (typeOrgane) rencontrés :")
    for t, k in diag_to.most_common(12):
        print(f"   {t or '(vide)':14} : {k}")
    print(f"\nDossiers parcourus                : {diag['nb_dossiers']}")
    print(f"Dossiers avec une commission      : {diag['nb_avec_commission']}")
    exam = sum(1 for i in dossiers.values() if i['examine'])
    rap  = sum(1 for i in dossiers.values() if i['rapporteurRef'])
    print(f"  dont examinés (rapport/acte daté): {exam}")
    print(f"  dont rapporteur résolu (PA…)     : {rap}")
    print(f"Textes votés enrichis (data.json) : {maj}")
    print("\ncodeActe des actes portant une commission (échantillon) :")
    for ca, k in diag['codeActe_commission'].most_common(10):
        print(f"   {ca or '(vide)':22} : {k}")
    print("\nClés présentes sur les actes :")
    print("   " + ", ".join(sorted(diag['acte_keys'])))
    print("\nÉCHANTILLON d'un acte portant une commission (pour confirmer les champs) :")
    print(json.dumps(diag['sample_acte'], ensure_ascii=False, indent=1)[:1200])
    print("=" * 60)
    print(f"Écrit : {OUT} ({len(sortie)} commissions) + data.json enrichi.")

if __name__ == "__main__":
    main()

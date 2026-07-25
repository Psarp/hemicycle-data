#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_commissions.py — Brique DONNÉES commissions (idées A + B + C).

Produit public/commissions.json (composition + entonnoir des LOIS examinées par
chaque commission, votées ou non) et enrichit public/data.json (commission +
rapporteur sur les textes votés). Détection par structure + codeActe confirmés.
Réutilise le téléchargement de build_all.py. Aucune dépendance externe.

Usage : python build_commissions.py          # run + diagnostic
        python build_commissions.py --local   # zips locaux
"""
import json, os, sys, re
from collections import defaultdict, Counter
from datetime import datetime, timezone

import build_all as BA
from lib_commun import txt, ORGANE_TO_GROUPE

OUT_DIR = "public"
DATA    = os.path.join(OUT_DIR, "data.json")
OUT     = os.path.join(OUT_DIR, "commissions.json")

CODES_COMMISSION = {"COMPER"}          # type d'organe = commission permanente
PA_RE = re.compile(r"PA\d+")

# ---------------------------------------------------------------------------
# 1. Organes + composition (inchangé — validé au diagnostic)
# ---------------------------------------------------------------------------
def charger_organes_et_membres(use_local):
    zf = BA.get_zip("acteurs", use_local)
    organes, diag_ct = {}, Counter()
    for n in zf.namelist():
        if "/organe/PO" not in n or not n.endswith(".json"):
            continue
        try:
            o = json.loads(zf.read(n).decode("utf-8")).get("organe", {})
            uid, ct = txt(o.get("uid")), txt(o.get("codeType"))
            organes[uid] = {"libelle": txt(o.get("libelle")),
                            "abrege": txt(o.get("libelleAbrege")), "codeType": ct}
            diag_ct[ct] += 1
        except Exception:
            continue
    commissions = {u: o for u, o in organes.items() if o["codeType"] in CODES_COMMISSION}

    membres, diag_to = defaultdict(list), Counter()
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
                diag_to[m.get("typeOrgane") or ""] += 1
                if m.get("typeOrgane") == "GP":
                    ref = (m.get("organes") or {}).get("organeRef") or ""
                    if isinstance(ref, dict):
                        ref = txt(ref.get("uid"))
                    fin = m.get("dateFin")
                    if ((fin is None) or isinstance(fin, dict)) and not gp:
                        gp = ORGANE_TO_GROUPE.get(ref, "AUTRE")
            for m in mandats:
                ref = (m.get("organes") or {}).get("organeRef") or ""
                if isinstance(ref, dict):
                    ref = txt(ref.get("uid"))
                fin = m.get("dateFin")
                if ((fin is None) or isinstance(fin, dict)) and ref in commissions:
                    fonction = txt((m.get("infosQualite") or {}).get("libQualite")) or "Membre"
                    membres[ref].append({"acteurRef": uid, "nom": nom,
                                         "groupe": gp or "AUTRE", "fonction": fonction})
        except Exception:
            continue
    return organes, commissions, membres, diag_ct, diag_to

# ---------------------------------------------------------------------------
# 2. Dossiers : commission AU FOND + rapporteur + entonnoir (par codeActe)
# ---------------------------------------------------------------------------
def _iter_actes(node):
    """Itère tous les actes (dicts avec codeActe), arbre aplati."""
    if isinstance(node, dict):
        if "codeActe" in node:
            yield node
        for k in ("actesLegislatifs", "acteLegislatif"):
            if k in node:
                yield from _iter_actes(node[k])
    elif isinstance(node, list):
        for x in node:
            yield from _iter_actes(x)

def _oref(acte):
    r = acte.get("organeRef")
    if isinstance(r, dict):
        return txt(r.get("uid"))
    return r or ""

def _refs_textes(acte):
    out = []
    for cle in ("texteAssocie", "texteAdopte"):
        v = acte.get(cle)
        if isinstance(v, str):
            out.append(v)
    ta = acte.get("textesAssocies")
    if ta:
        out += re.findall(r"(?:PION|PRJL|RAPP|RINF|TA)[A-Z0-9]+", json.dumps(ta, ensure_ascii=False))
    return out

def analyser_dossier(d, commissions, diag):
    """Renvoie un enregistrement {commission, rapporteurRef, examine, rapport,
    date, legislatif, titre} ou None si aucune commission au fond."""
    titre = txt((d.get("titreDossier") or {}).get("titre"))
    fond = Counter()
    rapporteurs, dates_fond = [], []
    examine = rapport = False
    tous_textes = []
    for acte in _iter_actes(d.get("actesLegislatifs")):
        ca = txt(acte.get("codeActe"))
        tous_textes += _refs_textes(acte)
        if "COM-FOND" not in ca:
            continue
        oref = _oref(acte)
        if oref not in commissions:
            continue
        fond[oref] += 1
        dd = txt(acte.get("dateActe"))[:10]
        if dd:
            dates_fond.append(dd)
        if "REUNION" in ca:
            examine = True
        if "RAPPORT" in ca:
            examine = True
            rapport = True
        # rapporteur : dans le champ rapporteurs de cet acte (nom de sous-champ variable)
        rp = acte.get("rapporteurs")
        if rp:
            pas = PA_RE.findall(json.dumps(rp, ensure_ascii=False))
            rapporteurs += pas
            if diag.get("sample_rapporteurs") is None:
                diag["sample_rapporteurs"] = rp
        diag["codeActe_fond"][ca] += 1
    if not fond:
        return None
    cref = fond.most_common(1)[0][0]
    legislatif = any(t.startswith(("PION", "PRJL")) for t in tous_textes)
    return {
        "commission": cref,
        "rapporteurRef": rapporteurs[0] if rapporteurs else "",
        "examine": examine,
        "rapport": rapport,
        "date": min(dates_fond) if dates_fond else "",
        "legislatif": legislatif,
        "titre": titre,
    }

def charger_dossiers(commissions, use_local):
    zf = BA.get_zip("dossiers", use_local)
    resultats = {}
    diag = {"nb": 0, "avec_fond": 0, "codeActe_fond": Counter(), "sample_rapporteurs": None}
    for n in zf.namelist():
        if "/dossierParlementaire/DLR" not in n or not n.endswith(".json"):
            continue
        try:
            d = json.loads(zf.read(n).decode("utf-8")).get("dossierParlementaire", {})
            diag["nb"] += 1
            rec = analyser_dossier(d, commissions, diag)
            if rec:
                diag["avec_fond"] += 1
                resultats[txt(d.get("uid"))] = rec
        except Exception:
            continue
    return resultats, diag

# ---------------------------------------------------------------------------
# 3. Assemblage + sorties + diagnostic
# ---------------------------------------------------------------------------
def main():
    use_local = "--local" in sys.argv
    print("== Composition des commissions ==")
    organes, commissions, membres, diag_ct, diag_to = charger_organes_et_membres(use_local)
    print("== Dossiers : commission au fond + rapporteur + entonnoir ==")
    dossiers, diag = charger_dossiers(commissions, use_local)

    data = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {"textes": []}
    textes = {t["ref"]: t for t in data.get("textes", [])}
    nom_par_ref = {m["acteurRef"]: (m["nom"], m["groupe"])
                   for c in membres.values() for m in c}

    sortie = []
    for cuid, org in commissions.items():
        mbs = sorted(membres.get(cuid, []), key=lambda x: x["nom"])
        prez = next((m for m in mbs if "présid" in m["fonction"].lower()
                     and "vice" not in m["fonction"].lower()), None)
        # entonnoir : LOIS examinées (réunion/rapport) par cette commission au fond
        lois = []
        for dlr, r in dossiers.items():
            if r["commission"] != cuid or not r["legislatif"] or not r["examine"]:
                continue
            t = textes.get(dlr)
            rref = r["rapporteurRef"]
            lois.append({
                "ref": dlr, "titre": r["titre"], "date": r["date"],
                "rapporteurRef": rref, "rapporteurNom": nom_par_ref.get(rref, ("", ""))[0],
                "rapport": r["rapport"], "vote": bool(t),
                "statut": (t or {}).get("statut", ""),
            })
        if not mbs and not lois:
            continue
        sortie.append({
            "uid": cuid, "nom": org["libelle"], "abrege": org["abrege"],
            "president": ({"acteurRef": prez["acteurRef"], "nom": prez["nom"]} if prez else None),
            "effectif": len(mbs), "membres": mbs,
            "nbLois": len(lois),
            "textes": sorted(lois, key=lambda x: x["date"], reverse=True),
        })
    sortie.sort(key=lambda c: c["nbLois"], reverse=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "commissions": sortie}, f, ensure_ascii=False, separators=(",", ":"))

    # Enrichir data.json (commission + rapporteur sur les textes votés)
    maj = maj_rap = 0
    for dlr, r in dossiers.items():
        t = textes.get(dlr)
        if not t:
            continue
        org = commissions.get(r["commission"], {})
        t["commission"] = {"uid": r["commission"], "nom": org.get("libelle", "")}
        maj += 1
        if r["rapporteurRef"]:
            nom, gp = nom_par_ref.get(r["rapporteurRef"], ("", ""))
            t["rapporteur"] = {"acteurRef": r["rapporteurRef"], "nom": nom, "groupe": gp}
            maj_rap += 1
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # ---------------- DIAGNOSTIC ----------------
    leg = [r for r in dossiers.values() if r["legislatif"]]
    exam = [r for r in leg if r["examine"]]
    rep  = [r for r in leg if r["rapport"]]
    rap  = [r for r in exam if r["rapporteurRef"]]
    print("\n" + "=" * 60)
    print("DIAGNOSTIC v2 — après corrections")
    print("=" * 60)
    print(f"Commissions permanentes : {len(commissions)}  (effectifs {[len(membres.get(c,[])) for c in commissions]})")
    print(f"Dossiers parcourus                 : {diag['nb']}")
    print(f"  avec commission au fond          : {diag['avec_fond']}")
    print(f"  dont LÉGISLATIFS (PION/PRJL)      : {len(leg)}")
    print(f"    dont EXAMINÉS (réunion/rapport) : {len(exam)}   <= entonnoir des fiches")
    print(f"    dont rapport déposé             : {len(rep)}")
    print(f"    dont rapporteur résolu          : {len(rap)}  ({100*len(rap)//max(1,len(exam))}% des examinés)")
    print(f"Textes votés enrichis (data.json)  : {maj}  (dont rapporteur : {maj_rap})")
    print("\nRépartition des lois examinées par commission :")
    parcom = Counter(r["commission"] for r in exam)
    for cuid, k in parcom.most_common():
        print(f"   {commissions.get(cuid,{}).get('abrege','?'):18} : {k}")
    print("\ncodeActe COM-FOND rencontrés :")
    for ca, k in diag["codeActe_fond"].most_common(10):
        print(f"   {ca:26} : {k}")
    print("\nÉCHANTILLON du champ 'rapporteurs' (pour confirmer l'extraction) :")
    print(json.dumps(diag["sample_rapporteurs"], ensure_ascii=False, indent=1)[:900])
    print("=" * 60)
    print(f"Écrit : {OUT} ({len(sortie)} commissions) + data.json enrichi.")

if __name__ == "__main__":
    main()

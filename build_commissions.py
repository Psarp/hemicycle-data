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
import json, os, sys, re, time
from collections import defaultdict, Counter
from datetime import datetime, timezone

import build_all as BA
import build_descriptions as BD          # réutilise la récupération d'exposé des motifs
from lib_commun import txt, ORGANE_TO_GROUPE

OUT_DIR = "public"
DATA    = os.path.join(OUT_DIR, "data.json")
OUT     = os.path.join(OUT_DIR, "commissions.json")
OUT_TXT = os.path.join(OUT_DIR, "textes_commission.json")   # fiches allégées (non votés)
CACHE_OBJ = os.path.join(OUT_DIR, "descriptions_commission.json")

CODES_COMMISSION = {"COMPER"}          # type d'organe = commission permanente
PA_RE = re.compile(r"PA\d+")
DOC_INIT = re.compile(r"^(PION|PRJL)(AN|SN)")   # texte initial (proposition/projet)

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
    acteurs_nom = {}                      # uid -> {"nom":…, "groupe":…} pour TOUS les acteurs
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
            acteurs_nom[uid] = {"nom": nom, "groupe": gp or "AUTRE"}
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
    return organes, commissions, membres, acteurs_nom, diag_ct, diag_to

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

def _pa_dans(node):
    """Tous les 'PA…' trouvés sous un nœud (recherche profonde)."""
    return PA_RE.findall(json.dumps(node, ensure_ascii=False)) if node else []

def analyser_dossier(d, commissions, diag):
    """Renvoie un enregistrement complet (commission, rapporteur, entonnoir + de
    quoi bâtir une fiche allégée : doc initial, auteur, cycle) ou None."""
    titre = txt((d.get("titreDossier") or {}).get("titre"))
    # auteur = initiateur du dossier (même emplacement que build_all.py)
    init = (((d.get("initiateur") or {}).get("acteurs") or {}).get("acteur") or {})
    if isinstance(init, list):
        init = init[0] if init else {}
    auteur_ref = txt(init.get("acteurRef"))
    fond = Counter()
    rapporteurs, dates_fond = [], []
    examine = rapport = False
    tous_textes = []
    date_depot = date_reunion = date_rapport = ""
    for acte in _iter_actes(d.get("actesLegislatifs")):
        ca = txt(acte.get("codeActe"))
        tous_textes += _refs_textes(acte)
        dd = txt(acte.get("dateActe"))[:10]
        if "DEPOT" in ca and dd and not date_depot:
            date_depot = dd
        if "COM-FOND" not in ca:
            continue
        oref = _oref(acte)
        if oref not in commissions:
            continue
        fond[oref] += 1
        if dd:
            dates_fond.append(dd)
        if "REUNION" in ca:
            examine = True
            if dd and not date_reunion:
                date_reunion = dd
        if "RAPPORT" in ca:
            examine = True; rapport = True
            if dd and not date_rapport:
                date_rapport = dd
        rp = acte.get("rapporteurs")
        if rp:
            rapporteurs += PA_RE.findall(json.dumps(rp, ensure_ascii=False))
            if diag.get("sample_rapporteurs") is None:
                diag["sample_rapporteurs"] = rp
        diag["codeActe_fond"][ca] += 1
    if not fond:
        return None
    # document initial (proposition/projet) : plus petit numéro, AN de préférence
    inits = sorted([t for t in set(tous_textes) if DOC_INIT.match(t)],
                   key=lambda r: ("SN" in r[:8], r))
    return {
        "commission": fond.most_common(1)[0][0],
        "rapporteurRef": rapporteurs[0] if rapporteurs else "",
        "examine": examine, "rapport": rapport,
        "date": min(dates_fond) if dates_fond else "",
        "legislatif": any(t.startswith(("PION", "PRJL")) for t in tous_textes),
        "titre": titre,
        "docInit": inits[0] if inits else "",
        "textesRefs": [t for t in set(tous_textes) if re.match(r"(PION|PRJL|RAPP)", t)],
        "auteurRef": auteur_ref,
        "dateDepot": date_depot, "dateReunion": date_reunion, "dateRapport": date_rapport,
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
# 3. Objet (exposé des motifs) des textes non votés — réutilise build_descriptions
# ---------------------------------------------------------------------------
def fetch_objet(dlr, rec, cache):
    if dlr in cache:
        c = cache[dlr]
        return c if c.get("statut") == "ok" else None
    t = {"textesRefs": rec["textesRefs"], "cycle": [],
         "date": rec.get("dateDepot") or rec.get("date") or "2025-01-01",
         "auteur": rec.get("auteurNom", ""), "titre": rec["titre"]}
    source, cible = BD.choisir_source(t)
    if source is None:
        cache[dlr] = {"statut": "aucun"}; return None
    urls = [BD.url_an(cible)] if source == "AN" else BD.urls_sn(cible, t)
    for url in urls:
        html = BD.telecharger(url); time.sleep(0.2)
        if html is None:
            continue
        texte = BD.html_vers_texte(html)
        if source == "SN" and not BD.page_coherente(texte, t):
            continue
        d = BD.extraire(texte)
        if d:
            cache[dlr] = {"statut": "ok", "paragraphes": d["paragraphes"],
                          "source": source, "url": url}
            return cache[dlr]
    cache[dlr] = {"statut": "vide"}; return None

# ---------------------------------------------------------------------------
# 4. Assemblage + sorties + diagnostic
# ---------------------------------------------------------------------------
def main():
    use_local = "--local" in sys.argv
    print("== Composition des commissions ==")
    organes, commissions, membres, acteurs_nom, diag_ct, diag_to = charger_organes_et_membres(use_local)
    print("== Dossiers : commission au fond + rapporteur + entonnoir ==")
    dossiers, diag = charger_dossiers(commissions, use_local)

    data = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else {"textes": []}
    textes = {t["ref"]: t for t in data.get("textes", [])}
    def nom_grp(ref):
        a = acteurs_nom.get(ref)
        return (a["nom"], a["groupe"]) if a else ("", "")

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
                "rapporteurRef": rref, "rapporteurNom": nom_grp(rref)[0],
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
            nom, gp = nom_grp(r["rapporteurRef"])
            t["rapporteur"] = {"acteurRef": r["rapporteurRef"], "nom": nom, "groupe": gp}
            maj_rap += 1
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    # --- Fiches allégées : lois EXAMINÉES mais NON votées (objet + auteur + cycle) ---
    cache_obj = {}
    if os.path.exists(CACHE_OBJ):
        try: cache_obj = json.load(open(CACHE_OBJ, encoding="utf-8"))
        except Exception: cache_obj = {}
    fiches = {}
    non_votes = [(dlr, r) for dlr, r in dossiers.items()
                 if r["legislatif"] and r["examine"] and dlr not in textes]
    obj_ok = 0
    for i, (dlr, r) in enumerate(non_votes, 1):
        au_nom, au_gp = nom_grp(r["auteurRef"])
        rap_nom, rap_gp = nom_grp(r["rapporteurRef"])
        obj = fetch_objet(dlr, {**r, "auteurNom": au_nom}, cache_obj)
        if obj: obj_ok += 1
        cycle = [{"label": l, "date": d} for l, d in
                 (("Dépôt", r["dateDepot"]), ("Examen en commission", r["dateReunion"]),
                  ("Rapport déposé", r["dateRapport"])) if d]
        org = commissions.get(r["commission"], {})
        fiches[dlr] = {
            "ref": dlr, "titre": r["titre"],
            "type": "Projet de loi" if (r["docInit"] or "").startswith("PRJL") else "Proposition de loi",
            "commission": {"uid": r["commission"], "nom": org.get("libelle", "")},
            "rapporteur": ({"acteurRef": r["rapporteurRef"], "nom": rap_nom, "groupe": rap_gp}
                           if r["rapporteurRef"] else None),
            "auteur": au_nom, "auteurGroupe": au_gp,
            "cycle": cycle,
            "objet": ({"paragraphes": obj["paragraphes"], "source": obj["source"], "url": obj["url"]}
                      if obj else None),
            "dossierUrl": "https://www.assemblee-nationale.fr/dyn/17/dossiers/" + dlr,
        }
        if i % 25 == 0 or i == len(non_votes):
            print(f"  objets non votés : {i}/{len(non_votes)}…")
    with open(CACHE_OBJ, "w", encoding="utf-8") as f:
        json.dump(cache_obj, f, ensure_ascii=False, indent=1)
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        json.dump({"genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "textes": fiches}, f, ensure_ascii=False, separators=(",", ":"))

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
    print(json.dumps(diag["sample_rapporteurs"], ensure_ascii=False, indent=1)[:600])
    print("\n--- FICHES ALLÉGÉES (lois examinées non votées) ---")
    aut = sum(1 for f in fiches.values() if f["auteur"])
    cyc = sum(1 for f in fiches.values() if f["cycle"])
    print(f"Fiches non votées produites        : {len(fiches)}")
    print(f"  avec auteur résolu               : {aut}  ({100*aut//max(1,len(fiches))}%)")
    print(f"  avec objet (exposé) récupéré      : {obj_ok}  ({100*obj_ok//max(1,len(fiches))}%)")
    print(f"  avec cycle daté                  : {cyc}")
    ex = next(iter(fiches.values()), None)
    if ex:
        apercu = {**ex, "objet": ("(%d paragraphes)" % len(ex["objet"]["paragraphes"])) if ex["objet"] else None}
        print("Exemple de fiche :")
        print(json.dumps(apercu, ensure_ascii=False, indent=1)[:900])
    print("=" * 60)
    print(f"Écrit : {OUT} ({len(sortie)} commissions) · {OUT_TXT} ({len(fiches)} fiches) · data.json enrichi.")

if __name__ == "__main__":
    main()

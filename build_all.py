#!/usr/bin/env python3
"""build_all.py — Orchestrateur Hémicycle (open data AN, sans IA).
Combine 4 jeux officiels : acteurs, dossiers législatifs, scrutins, amendements.
Produit :
  public/data.json                      (base enrichie : texte + auteur + contexte + cycle + résultat + votes/groupe + bilan amdts)
  public/amendements/<DLR...>.json      (détail complet des amendements par texte, pour le tableau filtrable)

En production (GitHub Actions), télécharge les 4 archives.
En test local, on peut injecter des ZIP locaux (voir --local)."""
import io, json, os, sys, zipfile, glob
from collections import defaultdict
from datetime import datetime, timezone
from urllib.request import urlopen, Request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_commun import ORGANE_TO_GROUPE, txt, clean_html

URLS = {
    "acteurs": "https://data.assemblee-nationale.fr/static/openData/repository/17/amo/deputes_senateurs_ministres_legislature/AMO20_dep_sen_min_tous_mandats_et_organes.json.zip",
    "dossiers": "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/dossiers_legislatifs/Dossiers_Legislatifs.json.zip",
    "scrutins": "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/scrutins/Scrutins.json.zip",
    "amendements": "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/amendements_div_legis/Amendements.json.zip",
}
OUT_DIR = "public"
OUT_DATA = os.path.join(OUT_DIR, "data.json")
OUT_AMDT_DIR = os.path.join(OUT_DIR, "amendements")

# --- Sources : locales (test) ou téléchargées (prod) ---
LOCAL = {
    "acteurs": "/mnt/user-data/uploads/AMO20_dep_sen_min_tous_mandats_et_organes_json.zip",
    "dossiers": "/mnt/user-data/uploads/Dossiers_Legislatifs_json.zip",
    "scrutins": "/mnt/user-data/uploads/Scrutins_json.zip",
    "amendements": None,  # archive complète non fournie ; on gère l'absence
}

def get_zip(cle, use_local):
    if use_local and LOCAL.get(cle) and os.path.exists(LOCAL[cle]):
        print(f"[local] {cle}: {LOCAL[cle]}")
        return zipfile.ZipFile(LOCAL[cle])
    url = URLS[cle]
    print(f"[web] {cle}: {url}")
    req = Request(url, headers={"User-Agent":"Hemicycle/1.0"})
    with urlopen(req, timeout=600) as r:
        data = r.read()
    print(f"     {len(data)//1024//1024} Mo")
    return zipfile.ZipFile(io.BytesIO(data))

# ---------- 1. ACTEURS : PA... -> nom + mandats (députés, sénateurs, ministres) ----------
def charger_acteurs(use_local):
    """Jeu AMO20 : députés, sénateurs ET ministres.
    Indexe aussi les mandats par uid pour retrouver la qualité EXACTE au moment
    du dépôt d'un texte (via le mandatRef fourni par le dossier)."""
    zf = get_zip("acteurs", use_local)

    # organes : uid -> (libelle, abrege) — utile pour ministères et groupes du Sénat
    organes = {}
    for n in zf.namelist():
        if "/organe/PO" not in n or not n.endswith(".json"): continue
        try:
            o = json.loads(zf.read(n).decode("utf-8")).get("organe", {})
            organes[txt(o.get("uid"))] = (txt(o.get("libelle")), txt(o.get("libelleAbrege")))
        except Exception: continue

    acteurs = {}
    for n in zf.namelist():
        if "/acteur/PA" not in n or not n.endswith(".json"): continue
        try:
            a = json.loads(zf.read(n).decode("utf-8")).get("acteur", {})
            uid = txt(a.get("uid"))
            ident = a.get("etatCivil",{}).get("ident",{})
            nom = (txt(ident.get("prenom"))+" "+txt(ident.get("nom"))).strip()
            mandats = a.get("mandats",{}).get("mandat",[])
            if isinstance(mandats, dict): mandats=[mandats]
            par_uid = {}
            gp_actif = ""
            for m in mandats:
                mu = txt(m.get("uid"))
                t = m.get("typeOrgane") or ""
                ref = (m.get("organes") or {}).get("organeRef") or ""
                if isinstance(ref, dict): ref = txt(ref.get("uid"))
                q = txt((m.get("infosQualite") or {}).get("libQualite"))
                fin = m.get("dateFin")
                encours = (fin is None) or isinstance(fin, dict)
                if mu:
                    par_uid[mu] = {"type": t, "ref": ref, "qualite": q}
                if t == "GP" and encours and not gp_actif:
                    gp_actif = ORGANE_TO_GROUPE.get(ref, "AUTRE")
            acteurs[uid] = {"nom": nom, "mandats": par_uid, "gp": gp_actif}
        except Exception: continue
    print(f"  {len(acteurs)} acteurs (députés, sénateurs, ministres), {len(organes)} organes")
    return acteurs, organes


def resoudre_auteur(acteurs, organes, acteur_ref, mandat_ref):
    """Renvoie {nom, groupe, qualite} depuis un acteurRef + mandatRef."""
    a = acteurs.get(acteur_ref)
    if not a:
        return {"nom": "", "groupe": "", "qualite": ""}
    nom = a["nom"]
    m = a["mandats"].get(mandat_ref)
    if m:
        t, ref, q = m["type"], m["ref"], m["qualite"]
        if t == "GP":
            return {"nom": nom, "groupe": ORGANE_TO_GROUPE.get(ref, "AUTRE"), "qualite": "Député"}
        if t in ("MINISTERE", "GOUVERNEMENT"):
            lib = organes.get(ref, ("", ""))[0]
            titre = q or lib or "Membre du Gouvernement"
            # évite « Ministre » tout court quand on connaît le ministère
            if lib and lib.lower() not in (titre or "").lower() and lib != "Gouvernement":
                titre = f"{titre} · {lib}"
            return {"nom": nom, "groupe": "", "qualite": titre}
        if t in ("SENAT", "GROUPESENAT", "COMSENAT", "COMSPSENAT"):
            abrev = organes.get(ref, ("", ""))[1] or organes.get(ref, ("", ""))[0]
            if abrev.strip().lower() in ("sénat", "senat", ""):
                abrev = ""  # évite « Sénateur · Sénat »
            return {"nom": nom, "groupe": "", "qualite": "Sénateur" + (f" · {abrev}" if abrev else "")}
        if t == "ASSEMBLEE":
            return {"nom": nom, "groupe": a["gp"], "qualite": "Député"}
    # Repli : si l'acteur a un groupe parlementaire actif, c'est un député
    if a["gp"]:
        return {"nom": nom, "groupe": a["gp"], "qualite": "Député"}
    return {"nom": nom, "groupe": "", "qualite": ""}

# ---------- 2. DOSSIERS : DLR... -> {titre, procédure, auteur, textes[], cycle[]} ----------
def _chambre(code):
    """Déduit la chambre depuis le code d'acte : AN1/AN2… = Assemblée, SN1/SN2… = Sénat."""
    c = (code or "").upper()
    if c.startswith("AN"):
        return "Assemblée"
    if c.startswith("SN"):
        return "Sénat"
    return ""

def _parcours_actes(node, etapes, textes):
    """Parcourt récursivement l'arbre d'actes pour extraire étapes + textes associés."""
    if isinstance(node, dict):
        code = txt(node.get("codeActe"))
        lib = (node.get("libelleActe") or {})
        nom = txt(lib.get("libelleCourt")) if isinstance(lib, dict) else ""
        date = txt(node.get("dateActe"))[:10]
        ta = txt(node.get("texteAssocie"))
        if ta: textes.add(ta)
        if code and nom and "-" not in code:  # étapes de haut niveau
            ch = _chambre(code)
            label = f"{nom} · {ch}" if ch else nom
            etapes.append({"code":code, "label":label, "date":date})
        sub = node.get("actesLegislatifs")
        if sub: _parcours_actes(sub, etapes, textes)
    elif isinstance(node, dict) is False and isinstance(node, list):
        for x in node: _parcours_actes(x, etapes, textes)
    # cas dict contenant 'acteLegislatif'
    if isinstance(node, dict) and "acteLegislatif" in node:
        _parcours_actes(node["acteLegislatif"], etapes, textes)

def charger_dossiers(use_local):
    zf = get_zip("dossiers", use_local)
    dossiers = {}
    for n in zf.namelist():
        if "/dossierParlementaire/DLR" not in n or not n.endswith(".json"): continue
        try:
            d = json.loads(zf.read(n).decode("utf-8")).get("dossierParlementaire", {})
            uid = txt(d.get("uid"))
            titre = txt((d.get("titreDossier") or {}).get("titre"))
            proc = txt((d.get("procedureParlementaire") or {}).get("libelle"))
            init = (((d.get("initiateur") or {}).get("acteurs") or {}).get("acteur") or {})
            if isinstance(init, list): init = init[0] if init else {}
            auteur_ref = txt(init.get("acteurRef"))
            mandat_ref = txt(init.get("mandatRef"))
            etapes, textes = [], set()
            _parcours_actes(d.get("actesLegislatifs"), etapes, textes)
            dossiers[uid] = {"titre":titre, "procedure":proc, "auteurRef":auteur_ref, "mandatRef":mandat_ref,
                             "textes":list(textes), "etapes":etapes}
        except Exception: continue
    print(f"  {len(dossiers)} dossiers")
    return dossiers

# ---------- 3. SCRUTINS : base par dossier ----------
def charger_scrutins(use_local):
    zf = get_zip("scrutins", use_local)
    scrutins = []
    for n in zf.namelist():
        if not n.endswith(".json"): continue
        try:
            s = json.loads(zf.read(n).decode("utf-8")).get("scrutin", {})
            numero = txt(s.get("numero")); date = txt(s.get("dateScrutin"))[:10]
            sort = (s.get("sort") or {}); code = txt(sort.get("code")).lower()
            statut = "adopté" if code.startswith("adopt") else "rejeté" if code.startswith("rejet") else "—"
            objet = s.get("objet") or {}; dl = (objet.get("dossierLegislatif") or {}) if isinstance(objet,dict) else {}
            ref = txt(dl.get("dossierRef")); lib = txt(dl.get("libelle"))
            vpg = {}
            vent = (s.get("ventilationVotes") or {}).get("organe") or {}
            groupes = (vent.get("groupes") or {}).get("groupe")
            if isinstance(groupes, dict): groupes=[groupes]
            if isinstance(groupes, list):
                for g in groupes:
                    cg = ORGANE_TO_GROUPE.get(txt(g.get("organeRef")),"AUTRE")
                    dv = (g.get("vote") or {}).get("decompteVoix") or {}
                    p=int(txt(dv.get("pour")) or 0); c=int(txt(dv.get("contre")) or 0); a=int(txt(dv.get("abstentions")) or 0)
                    if p or c or a:
                        cur=vpg.get(cg,{"pour":0,"contre":0,"abstention":0})
                        cur["pour"]+=p;cur["contre"]+=c;cur["abstention"]+=a;vpg[cg]=cur
            if date:
                scrutins.append({"date":date,"ref":ref,"numero":numero,"titre":lib,
                                 "statut":statut,"votesParGroupe":vpg or None})
        except Exception: continue
    # dédup par dossier (dernier scrutin) — UNIQUEMENT les scrutins rattachés à un dossier DLR
    par = {}
    for s in scrutins:
        k = s["ref"]
        if k and k.startswith("DLR") and (k not in par or s["date"]>par[k]["date"]): par[k]=s
    print(f"  {len(par)} dossiers avec scrutin")
    return par

def construire(use_local=False):
    print("=== 1/4 Acteurs ==="); acteurs, organes = charger_acteurs(use_local)
    print("=== 2/4 Dossiers ==="); dossiers = charger_dossiers(use_local)
    print("=== 3/4 Scrutins ==="); scrutins = charger_scrutins(use_local)
    print("=== 4/4 Assemblage ===")

    # index texte PION -> dossier DLR (pour rattacher amendements plus tard)
    texte_to_dossier = {}
    for dlr, d in dossiers.items():
        for t in d["textes"]: texte_to_dossier[t] = dlr

    textes = []
    for dlr, sc in scrutins.items():
        d = dossiers.get(dlr, {})
        auteur = resoudre_auteur(acteurs, organes, d.get("auteurRef",""), d.get("mandatRef",""))
        titre = sc["titre"] or d.get("titre","") or f"Dossier {dlr}"
        typ = "Projet de loi" if titre.lower().startswith("projet de loi") else "Proposition de loi"
        # cycle -> frise
        cycle = []
        for e in d.get("etapes", [])[:6]:
            cycle.append({"label":e["label"], "etat":"fait" if e["date"] else "avenir"})
        textes.append({
            "date": sc["date"], "ref": dlr, "titre": titre, "type": typ,
            "contexte": d.get("procedure","") or f"Scrutin n°{sc['numero']}",
            "auteur": auteur.get("nom",""), "auteurGroupe": auteur.get("groupe",""),
            "auteurQualite": auteur.get("qualite",""),
            "statut": sc["statut"], "votesParGroupe": sc["votesParGroupe"],
            "cycle": cycle,
            "textesRefs": d.get("textes", []),
        })
    textes.sort(key=lambda x:x["date"])

    # --- Préserver l'enrichissement amendements déjà présent ---
    # build_all.py tourne tous les jours et régénère data.json ; sans cette
    # étape, il effacerait le bilan d'amendements produit par le workflow
    # hebdomadaire (build_amendements_ci.py).
    anciens_amdts = {}
    if os.path.exists(OUT_DATA):
        try:
            ancien = json.load(open(OUT_DATA, encoding="utf-8"))
            for t in ancien.get("textes", []):
                if t.get("amendements"):
                    anciens_amdts[t["ref"]] = t["amendements"]
        except Exception:
            pass
    for t in textes:
        if t["ref"] in anciens_amdts:
            t["amendements"] = anciens_amdts[t["ref"]]
    if anciens_amdts:
        print(f"  bilan d'amendements conservé pour {len(anciens_amdts)} textes")

    out = {"genere_le":datetime.now(timezone.utc).isoformat(),
           "source":"open data Assemblée nationale (17e législature)",
           "nb_textes":len(textes),"textes":textes}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_DATA,"w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    avec_auteur = sum(1 for t in textes if t["auteur"])
    print(f"\n✓ {OUT_DATA} : {len(textes)} textes")
    print(f"  avec auteur nommé : {avec_auteur}")
    print(f"  avec contexte/procédure : {sum(1 for t in textes if t['contexte'])}")
    print(f"  avec cycle : {sum(1 for t in textes if t['cycle'])}")
    if textes: print(f"  période : {textes[0]['date']} → {textes[-1]['date']}")
    return texte_to_dossier

if __name__ == "__main__":
    use_local = "--local" in sys.argv
    construire(use_local)

#!/usr/bin/env python3
"""build_all.py — Orchestrateur Hémicycle (open data AN, sans IA).
Combine 4 jeux officiels : acteurs, dossiers législatifs, scrutins, amendements.
Produit :
  public/data.json                      (base enrichie : texte + auteur + contexte + cycle + résultat + votes/groupe + bilan amdts)
  public/amendements/<DLR...>.json      (détail complet des amendements par texte, pour le tableau filtrable)

En production (GitHub Actions), télécharge les 4 archives.
En test local, on peut injecter des ZIP locaux (voir --local)."""
import io, json, os, re, sys, zipfile, glob
from collections import defaultdict, Counter
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
OUT_VOTES_DIR = os.path.join(OUT_DIR, "votes")
DEBUT_LEGISLATURE = "2024-07-18"   # ouverture de la 17e législature
OUT_ACTEURS_DIR = os.path.join(OUT_DIR, "acteurs")
OUT_ACTEURS_INDEX = os.path.join(OUT_DIR, "acteurs.json")
OUT_GROUPES = os.path.join(OUT_DIR, "groupes.json")
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
            circo = None
            ministere = ""
            au_gouvernement = False
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
                # circonscription d'élection (mandat de député en cours)
                if t == "ASSEMBLEE" and encours and not circo:
                    lieu = ((m.get("election") or {}).get("lieu")) or {}
                    if lieu:
                        circo = {"dep": txt(lieu.get("departement")),
                                 "numDep": txt(lieu.get("numDepartement")),
                                 "region": txt(lieu.get("region")),
                                 "numCirco": txt(lieu.get("numCirco"))}
                # Appartenance ACTUELLE au Gouvernement : seul un mandat
                # GOUVERNEMENT en cours fait foi. Un mandat MINISTERE « en mission »
                # est une mission temporaire confiée à un parlementaire, pas un poste.
                if t == "GOUVERNEMENT" and encours:
                    au_gouvernement = True
                if t == "MINISTERE" and encours and not ministere:
                    if (q or "").strip().lower() != "en mission":
                        lib = organes.get(ref, ("", ""))[0]
                        if lib and lib.lower() not in (q or "").lower() and lib != "Gouvernement":
                            ministere = f"{q} · {lib}" if q else lib
                        else:
                            ministere = q or lib or ""
            # On ne retient la fonction que si la personne est bien au Gouvernement
            if not au_gouvernement:
                ministere = ""
            acteurs[uid] = {"nom": nom, "mandats": par_uid, "gp": gp_actif,
                            "circo": circo, "ministere": ministere}
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

def _parcours_actes(node, etapes, textes, votes=None, decisions=None, dates=None):
    """Parcourt récursivement l'arbre d'actes pour extraire étapes, textes associés
    et références de scrutins (voteRefs) — ce dernier lien couvre TOUTE la
    législature, alors que scrutin->dossierRef n'est renseigné que depuis 03/2026."""
    if votes is None: votes = set()
    if decisions is None: decisions = []
    if dates is None: dates = []
    if isinstance(node, dict):
        da = txt(node.get("dateActe"))[:10]
        if da: dates.append(da)
        # Décision de l'Assemblée (AN1-DEBATS-DEC, AN2-DEBATS-DEC…) : présente
        # même quand le vote a eu lieu à main levée, sans scrutin public.
        ca = txt(node.get("codeActe"))
        if re.match(r"^AN\d.*-DEBATS-DEC$", ca or ""):
            sc = node.get("statutConclusion") or {}
            lib = txt(sc.get("libelle")) if isinstance(sc, dict) else ""
            dt = txt(node.get("dateActe"))[:10]
            if dt:
                decisions.append({"date": dt, "statut": lib, "code": ca})
        vr = node.get("voteRefs") or {}
        if isinstance(vr, dict):
            v = vr.get("voteRef")
            if isinstance(v, str): votes.add(v)
            elif isinstance(v, list): votes.update(x for x in v if isinstance(x, str))
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
        if sub: _parcours_actes(sub, etapes, textes, votes, decisions, dates)
    elif isinstance(node, list):
        for x in node: _parcours_actes(x, etapes, textes, votes, decisions, dates)
    # cas dict contenant 'acteLegislatif'
    if isinstance(node, dict) and "acteLegislatif" in node:
        _parcours_actes(node["acteLegislatif"], etapes, textes, votes, decisions, dates)
    return votes

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
            etapes, textes, votes, decisions, dates = [], set(), set(), [], []
            _parcours_actes(d.get("actesLegislatifs"), etapes, textes, votes, decisions, dates)
            dossiers[uid] = {"titre":titre, "procedure":proc, "auteurRef":auteur_ref, "mandatRef":mandat_ref,
                             "textes":list(textes), "etapes":etapes, "votes":list(votes),
                             "decisions":decisions, "dateOuverture": min(dates) if dates else ""}
        except Exception: continue
    print(f"  {len(dossiers)} dossiers")
    return dossiers

# ---------- 3. SCRUTINS : base par dossier ----------
def charger_scrutins(use_local):
    zf = get_zip("scrutins", use_local)
    scrutins = []
    uid_to_nom = {}
    for n in zf.namelist():
        if not n.endswith(".json"): continue
        try:
            s = json.loads(zf.read(n).decode("utf-8")).get("scrutin", {})
            uid_to_nom[txt(s.get("uid"))] = n
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
                scrutins.append({"uid":txt(s.get("uid")),"date":date,"ref":ref,"numero":numero,
                                 "titre":lib,"statut":statut,"votesParGroupe":vpg or None})
        except Exception: continue
    # index par uid (pour le lien dossier -> voteRefs, valable sur TOUTE la législature)
    par_uid = {s["uid"]: s for s in scrutins if s["uid"]}
    # index par dossier (lien direct, seulement renseigné depuis mars 2026)
    par_dossier = {}
    for s in scrutins:
        k = s["ref"]
        if k and k.startswith("DLR") and (k not in par_dossier or s["date"]>par_dossier[k]["date"]):
            par_dossier[k]=s
    print(f"  {len(scrutins)} scrutins | {len(par_dossier)} rattachés directement à un dossier")
    return par_uid, par_dossier, zf, uid_to_nom

def _votants(bloc):
    """Le bloc pours/contres/... contient 'votant' : soit un dict, soit une liste."""
    if not isinstance(bloc, dict):
        return []
    v = bloc.get("votant")
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return [x for x in v if isinstance(x, dict)]
    return []


def extraire_votes_nominatifs(zf, uid_to_nom, retenu):
    """Écrit public/votes/<DLR>.json : le vote de chaque député, nommément.
    Ne traite que les textes retenus dans la base (pas les 8277 scrutins)."""
    os.makedirs(OUT_VOTES_DIR, exist_ok=True)
    positions = [("pours", "pour"), ("contres", "contre"),
                 ("abstentions", "abstention"), ("nonVotants", "non-votant")]
    ecrits = 0
    tous = {}
    for dlr, sc in retenu.items():
        uid = sc.get("uid")
        nom = uid_to_nom.get(uid) if uid else None
        if not nom:
            continue  # texte sans scrutin public (vote à main levée)
        try:
            s = json.loads(zf.read(nom).decode("utf-8")).get("scrutin", {})
        except Exception:
            continue
        groupes = ((s.get("ventilationVotes") or {}).get("organe") or {}).get("groupes") or {}
        gl = groupes.get("groupe")
        if isinstance(gl, dict): gl = [gl]
        if not isinstance(gl, list): continue
        votes = []
        for g in gl:
            code_g = ORGANE_TO_GROUPE.get(txt(g.get("organeRef")), "AUTRE")
            dn = (g.get("vote") or {}).get("decompteNominatif") or {}
            for cle, pos in positions:
                for v in _votants(dn.get(cle)):
                    votes.append({
                        "acteurRef": txt(v.get("acteurRef")),
                        "groupe": code_g,
                        "position": pos,
                        "delegation": txt(v.get("parDelegation")) == "true",
                    })
        if not votes:
            continue
        with open(os.path.join(OUT_VOTES_DIR, dlr + ".json"), "w", encoding="utf-8") as f:
            json.dump({"ref": dlr, "numero": sc.get("numero", ""), "date": sc.get("date", ""),
                       "nbVotants": len(votes), "votes": votes}, f, ensure_ascii=False)
        tous[dlr] = votes
        ecrits += 1
    print(f"  votes nominatifs écrits pour {ecrits} textes")
    return tous


def construire_fiches_acteurs(acteurs, organes, textes, votes_par_texte):
    """Écrit public/acteurs.json (index) et public/acteurs/<PA>.json (détail).
    Le détail contient les votes du député sur chaque texte, et les textes dont
    il est l'auteur."""
    os.makedirs(OUT_ACTEURS_DIR, exist_ok=True)
    par_ref = {t["ref"]: t for t in textes}

    # 1. Inverser : acteur -> ses votes
    votes_acteur = defaultdict(list)
    for dlr, votes in votes_par_texte.items():
        t = par_ref.get(dlr)
        if not t: continue
        for v in votes:
            votes_acteur[v["acteurRef"]].append({
                "ref": dlr, "date": t["date"], "titre": t["titre"],
                "position": v["position"], "delegation": v["delegation"],
            })

    # 2. Textes déposés par acteur
    deposes = defaultdict(list)
    for t in textes:
        ar = t.get("auteurActeurRef")
        if ar:
            deposes[ar].append({"ref": t["ref"], "date": t["date"], "titre": t["titre"],
                                "statut": t["statut"], "type": t["type"]})

    index = []
    # tous ceux qui ont voté ou déposé, PLUS tout député en exercice et tout
    # membre du Gouvernement (pour que la carte et l'encart soient complets)
    concernes = set(votes_acteur) | set(deposes)
    for ref, a in acteurs.items():
        if a.get("circo") or a.get("ministere"):
            concernes.add(ref)
    for ref in concernes:
        a = acteurs.get(ref)
        if not a: continue
        vs = sorted(votes_acteur.get(ref, []), key=lambda x: x["date"], reverse=True)
        dp = sorted(deposes.get(ref, []), key=lambda x: x["date"], reverse=True)
        # groupe : celui de son mandat GP actif
        grp = a.get("gp", "")
        compte = {"pour":0, "contre":0, "abstention":0, "non-votant":0}
        for v in vs: compte[v["position"]] = compte.get(v["position"], 0) + 1
        fiche = {
            "acteurRef": ref, "nom": a["nom"], "groupe": grp,
            "circo": a.get("circo"), "ministere": a.get("ministere",""),
            "nbVotes": len(vs), "compte": compte,
            "parDelegation": sum(1 for v in vs if v["delegation"]),
            "votes": vs, "textesDeposes": dp,
        }
        with open(os.path.join(OUT_ACTEURS_DIR, ref + ".json"), "w", encoding="utf-8") as f:
            json.dump(fiche, f, ensure_ascii=False)
        e = {"acteurRef": ref, "nom": a["nom"], "groupe": grp,
             "nbVotes": len(vs), "nbTextes": len(dp)}
        if a.get("circo"): e["circo"] = a["circo"]
        if a.get("ministere"): e["ministere"] = a["ministere"]
        index.append(e)

    index.sort(key=lambda x: x["nom"])
    with open(OUT_ACTEURS_INDEX, "w", encoding="utf-8") as f:
        json.dump({"nb": len(index), "acteurs": index}, f, ensure_ascii=False)
    print(f"  {len(index)} fiches d'acteurs (index + détail)")


def _position_dominante(v):
    """Position majoritaire d'un groupe sur un scrutin, depuis son décompte."""
    if not v: return None
    trio = [("pour", v.get("pour",0)), ("contre", v.get("contre",0)), ("abstention", v.get("abstention",0))]
    trio.sort(key=lambda x: -x[1])
    if trio[0][1] == 0: return None
    if len(trio) > 1 and trio[0][1] == trio[1][1]: return None   # égalité : pas de position nette
    return trio[0][0]


def construire_fiches_groupes(acteurs, textes, votes_par_texte):
    """Écrit public/groupes.json : composition, textes déposés, votes,
    cohésion interne et proximité de vote entre groupes."""
    par_ref = {t["ref"]: t for t in textes}

    # 1. Composition et présidence
    membres = defaultdict(list)
    presidents = {}
    for ref, a in acteurs.items():
        g = a.get("gp")
        if not g: continue
        membres[g].append({"acteurRef": ref, "nom": a["nom"]})
        for m in a["mandats"].values():
            if m["type"] == "GP" and "président" in (m["qualite"] or "").lower():
                if ORGANE_TO_GROUPE.get(m["ref"]) == g:
                    presidents[g] = {"acteurRef": ref, "nom": a["nom"]}
    for g in membres: membres[g].sort(key=lambda x: x["nom"])

    # 2. Textes déposés par un membre du groupe
    deposes = defaultdict(list)
    for t in textes:
        g = t.get("auteurGroupe")
        if g:
            deposes[g].append({"ref": t["ref"], "titre": t["titre"], "date": t["date"],
                               "statut": t["statut"], "type": t["type"]})

    # 3. Votes agrégés + positions par scrutin (pour la proximité)
    cumul = defaultdict(lambda: {"pour":0, "contre":0, "abstention":0, "scrutins":0})
    positions = {}   # ref texte -> {groupe: position dominante}
    for t in textes:
        vpg = t.get("votesParGroupe")
        if not vpg: continue
        pos = {}
        for g, v in vpg.items():
            cumul[g]["pour"] += v["pour"]; cumul[g]["contre"] += v["contre"]
            cumul[g]["abstention"] += v["abstention"]; cumul[g]["scrutins"] += 1
            p = _position_dominante(v)
            if p: pos[g] = p
        positions[t["ref"]] = pos

    # 4. Proximité entre groupes : sur quel % de scrutins votent-ils pareil ?
    codes = sorted(cumul.keys())
    proximite = {a: {} for a in codes}
    for i, a in enumerate(codes):
        for b in codes:
            if a == b: continue
            communs = accord = 0
            for pos in positions.values():
                if a in pos and b in pos:
                    communs += 1
                    if pos[a] == pos[b]: accord += 1
            if communs:
                proximite[a][b] = {"taux": round(accord/communs, 3), "sur": communs, "accords": accord}

    # 5. Cohésion interne : part des membres votant la position majoritaire du groupe
    coh = defaultdict(list)
    fractures = defaultdict(list)
    for dlr, votes in votes_par_texte.items():
        par_groupe = defaultdict(list)
        for v in votes:
            if v["position"] in ("pour","contre","abstention"):
                par_groupe[v["groupe"]].append(v["position"])
        for g, ps in par_groupe.items():
            if len(ps) < 5: continue          # trop peu de votants pour être significatif
            c = Counter(ps)
            taux = c.most_common(1)[0][1] / len(ps)
            coh[g].append(taux)
            if taux < 0.8:
                t = par_ref.get(dlr)
                fractures[g].append({"ref": dlr, "titre": (t or {}).get("titre",""),
                                     "date": (t or {}).get("date",""),
                                     "taux": round(taux,3), "detail": dict(c)})

    out = {}
    for g in set(list(membres) + list(cumul) + list(deposes)):
        dep = sorted(deposes.get(g, []), key=lambda x: x["date"], reverse=True)
        adoptes = sum(1 for x in dep if x["statut"] == "adopté")
        taux_coh = round(sum(coh[g])/len(coh[g]), 3) if coh.get(g) else None
        fr = sorted(fractures.get(g, []), key=lambda x: x["taux"])[:10]
        out[g] = {
            "code": g, "nom": None,
            "effectif": len(membres.get(g, [])),
            "president": presidents.get(g),
            "membres": membres.get(g, []),
            "textesDeposes": {"total": len(dep), "adoptes": adoptes, "liste": dep[:40]},
            "votes": dict(cumul.get(g, {})),
            "cohesion": {"moyenne": taux_coh, "surScrutins": len(coh.get(g, [])), "fractures": fr},
            "proximite": proximite.get(g, {}),
        }
    with open(OUT_GROUPES, "w", encoding="utf-8") as f:
        json.dump({"genere_le": datetime.now(timezone.utc).isoformat(), "groupes": out},
                  f, ensure_ascii=False)
    print(f"  {len(out)} fiches de groupe")


def statut_decision(libelle):
    """Normalise un statutConclusion en adopté / rejeté / —."""
    s = (libelle or "").lower()
    if s.startswith("rejet"):
        return "rejeté"
    if any(x in s for x in ("adopt", "modifi", "conforme", "accord")):
        return "adopté"
    return "—"


def deduire_type(procedure, titre):
    """Type du texte, déduit d'abord de la procédure officielle (fiable),
    puis du titre en repli."""
    p = (procedure or "").lower()
    t = (titre or "").lower()
    if "responsabilit" in p or t.startswith("motion de censure"):
        return "Motion de censure"
    if "résolution" in p or "resolution" in p:
        return "Résolution"
    if t.startswith("projet de loi"):
        return "Projet de loi"
    if t.startswith("proposition de loi"):
        return "Proposition de loi"
    if p.startswith("projet"):
        return "Projet de loi"
    if p.startswith("proposition"):
        return "Proposition de loi"
    return "Texte"


def construire(use_local=False):
    print("=== 1/4 Acteurs ==="); acteurs, organes = charger_acteurs(use_local)
    print("=== 2/4 Dossiers ==="); dossiers = charger_dossiers(use_local)
    print("=== 3/4 Scrutins ==="); scrutins_uid, scrutins_dossier, zf_scrutins, uid_to_nom = charger_scrutins(use_local)
    print("=== 4/4 Assemblage ===")

    # index texte PION -> dossier DLR (pour rattacher amendements plus tard)
    texte_to_dossier = {}
    for dlr, d in dossiers.items():
        for t in d["textes"]: texte_to_dossier[t] = dlr

    # --- Rattachement scrutin <-> dossier par DEUX voies complémentaires ---
    # (a) dossier.voteRefs -> scrutin : couvre toute la législature
    # (b) scrutin.objet.dossierLegislatif -> dossier : seulement depuis mars 2026
    # On retient, par dossier, le scrutin le plus récent trouvé par l'une ou l'autre.
    retenu = {}
    for dlr, d in dossiers.items():
        cands = [scrutins_uid[v] for v in d.get("votes", []) if v in scrutins_uid]
        if cands:
            retenu[dlr] = max(cands, key=lambda s: s["date"])
    voie_a = len(retenu)
    for dlr, sc in scrutins_dossier.items():
        if dlr not in retenu or sc["date"] > retenu[dlr]["date"]:
            retenu[dlr] = sc
    print(f"  {voie_a} dossiers via voteRefs (toute la législature) -> {len(retenu)} au total")

    # (c) Dossiers décidés à l'Assemblée SANS scrutin public (vote à main levée) :
    # la décision existe dans le dossier, mais sans détail par groupe (un vote à
    # main levée ne produit aucun décompte nominatif).
    sans_scrutin = 0
    for dlr, d in dossiers.items():
        if dlr in retenu:
            continue
        # ne garder que les décisions de la législature en cours (ouverte le 18/07/2024)
        decs = [x for x in (d.get("decisions") or []) if x["date"] >= DEBUT_LEGISLATURE]
        if not decs:
            continue
        derniere = max(decs, key=lambda x: x["date"])
        retenu[dlr] = {"uid": "", "date": derniere["date"], "ref": dlr, "numero": "",
                       "titre": "", "statut": statut_decision(derniere["statut"]),
                       "votesParGroupe": None, "sansScrutin": True,
                       "mentionDecision": derniere["statut"]}
        sans_scrutin += 1
    print(f"  + {sans_scrutin} dossiers décidés sans scrutin public -> {len(retenu)} textes")

    # Votes nominatifs (qui a voté quoi) pour les textes à scrutin public
    votes_par_texte = extraire_votes_nominatifs(zf_scrutins, uid_to_nom, retenu)

    textes = []
    for dlr, sc in retenu.items():
        d = dossiers.get(dlr, {})
        auteur = resoudre_auteur(acteurs, organes, d.get("auteurRef",""), d.get("mandatRef",""))
        titre = d.get("titre","") or sc["titre"] or f"Dossier {dlr}"
        typ = deduire_type(d.get("procedure",""), titre)
        # cycle -> frise
        cycle = []
        for e in d.get("etapes", [])[:6]:
            cycle.append({"label":e["label"], "etat":"fait" if e["date"] else "avenir"})
        textes.append({
            "date": sc["date"], "ref": dlr, "titre": titre, "type": typ,
            "contexte": d.get("procedure","") or f"Scrutin n°{sc['numero']}",
            "auteur": auteur.get("nom",""), "auteurGroupe": auteur.get("groupe",""),
            "auteurQualite": auteur.get("qualite",""),
            "auteurActeurRef": d.get("auteurRef",""),
            "statut": sc["statut"], "votesParGroupe": sc["votesParGroupe"],
            "sansScrutin": bool(sc.get("sansScrutin")),
            "mentionDecision": sc.get("mentionDecision", ""),
            "cycle": cycle,
            "textesRefs": d.get("textes", []),
        })
    textes.sort(key=lambda x:x["date"])

    construire_fiches_acteurs(acteurs, organes, textes, votes_par_texte)
    construire_fiches_groupes(acteurs, textes, votes_par_texte)

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

    # --- Entonnoir : tous les textes DÉPOSÉS pendant la législature, par groupe ---
    # Indispensable pour un taux de succès honnête : la base ne contient que les
    # textes VOTÉS, alors que l'immense majorité des propositions déposées ne sont
    # jamais examinées. Sans ce dénominateur, tout groupe afficherait ~100%.
    depots = defaultdict(lambda: {"deposes":0, "examines":0, "adoptes":0, "promulgues":0})
    refs_base = {t["ref"]: t for t in textes}
    for dlr, d in dossiers.items():
        ouv = d.get("dateOuverture") or ""
        if not ouv or ouv < DEBUT_LEGISLATURE:
            continue                       # dossier ouvert avant la législature en cours
        auteur = resoudre_auteur(acteurs, organes, d.get("auteurRef",""), d.get("mandatRef",""))
        g = auteur.get("groupe") or ("GOUV" if "ministre" in (auteur.get("qualite","") or "").lower() else "")
        if not g:
            continue
        depots[g]["deposes"] += 1
        t = refs_base.get(dlr)
        if t:
            depots[g]["examines"] += 1
            if t["statut"] == "adopté": depots[g]["adoptes"] += 1
            if t.get("promulguee"): depots[g]["promulgues"] += 1
    print(f"  entonnoir : dépôts comptés pour {len(depots)} groupes")

    out = {"genere_le":datetime.now(timezone.utc).isoformat(),
           "source":"open data Assemblée nationale (17e législature)",
           "nb_textes":len(textes),"depotsParGroupe":{k:dict(v) for k,v in depots.items()},
           "textes":textes}
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

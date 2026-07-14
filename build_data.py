#!/usr/bin/env python3
"""build_data.py — Source: OPEN DATA OFFICIEL DE L'ASSEMBLEE NATIONALE.
Telecharge l'archive scrutins 17e legislature, la parse, genere public/data.json.
Aucun fichier a fournir. Concu pour GitHub Actions."""
import io, json, os, zipfile
from datetime import datetime, timezone
from urllib.request import urlopen, Request

URL_SCRUTINS = "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/scrutins/Scrutins.json.zip"
OUTPUT_DIR = "public"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "data.json")

ORGANE_TO_GROUPE = {
    "PO845401":"RN","PO845407":"EPR","PO845413":"LFI","PO845419":"SOC",
    "PO845425":"DR","PO845439":"ECO","PO845454":"DEM","PO845470":"HOR",
    "PO845485":"LIOT","PO872880":"UDR","PO845514":"GDR","PO840056":"AUTRE",
    "PO847173":"AUTRE","PO0":"AUTRE",
}

def txt(c):
    if isinstance(c, dict):
        if c.get("@xsi:nil") == "true": return ""
        return c.get("#text","") or ""
    if isinstance(c,(int,float)): return str(c)
    return c or ""

def entier(c):
    try: return int(txt(c))
    except (ValueError, TypeError): return 0

def telecharger_zip(url):
    print("Telechargement de l'archive officielle...\n  "+url)
    req = Request(url, headers={"User-Agent":"Hemicycle/1.0"})
    with urlopen(req, timeout=180) as resp:
        data = resp.read()
    print("  %d Ko recus" % (len(data)//1024))
    return zipfile.ZipFile(io.BytesIO(data))

def parse_scrutin(s):
    numero = txt(s.get("numero"))
    date = txt(s.get("dateScrutin"))[:10]
    titre = txt(s.get("titre"))
    sort = s.get("sort") or {}
    code = txt(sort.get("code")).lower()
    statut = "adopté" if code.startswith("adopt") else "rejeté" if code.startswith("rejet") else "—"
    ref, lib_dossier = "", ""
    objet = s.get("objet") or {}
    if isinstance(objet, dict):
        dl = objet.get("dossierLegislatif") or {}
        if isinstance(dl, dict):
            ref = txt(dl.get("dossierRef")); lib_dossier = txt(dl.get("libelle"))
    total = None
    sv = s.get("syntheseVote") or {}
    dec = sv.get("decompte") if isinstance(sv, dict) else None
    if isinstance(dec, dict):
        total = {"pour":entier(dec.get("pour")),"contre":entier(dec.get("contre")),"abstention":entier(dec.get("abstentions"))}
    vpg = {}
    vent = s.get("ventilationVotes") or {}
    organe = vent.get("organe") if isinstance(vent, dict) else None
    groupes = None
    if isinstance(organe, dict):
        groupes = (organe.get("groupes") or {}).get("groupe")
    if isinstance(groupes, dict): groupes = [groupes]
    if isinstance(groupes, list):
        for g in groupes:
            code_g = ORGANE_TO_GROUPE.get(txt(g.get("organeRef")), "AUTRE")
            vote = (g.get("vote") or {}).get("decompteVoix") or {}
            p,c,a = entier(vote.get("pour")),entier(vote.get("contre")),entier(vote.get("abstentions"))
            if p or c or a:
                cur = vpg.get(code_g, {"pour":0,"contre":0,"abstention":0})
                cur["pour"]+=p; cur["contre"]+=c; cur["abstention"]+=a
                vpg[code_g]=cur
    titre_affiche = lib_dossier or titre or ("Scrutin n°"+numero)
    typ = "Projet de loi" if titre_affiche.lower().startswith("projet de loi") else "Proposition de loi"
    return {"date":date,"ref":ref,"numero_scrutin":numero,"titre":titre_affiche,
            "objet":titre,"type":typ,"contexte":"Scrutin n°"+numero,"statut":statut,
            "total":total,"votesParGroupe":vpg or None}

def construire():
    zf = telecharger_zip(URL_SCRUTINS)
    noms = [n for n in zf.namelist() if n.lower().endswith(".json")]
    print("  %d scrutins dans l'archive" % len(noms))
    scrutins = []
    for nom in noms:
        try:
            obj = json.loads(zf.read(nom).decode("utf-8"))
            s = obj.get("scrutin", obj)
            p = parse_scrutin(s)
            if p["date"]: scrutins.append(p)
        except Exception:
            continue
    par_dossier = {}
    for s in scrutins:
        if s["ref"]:
            k = s["ref"]
            if k not in par_dossier or s["date"] > par_dossier[k]["date"]:
                par_dossier[k] = s
    base = sorted(par_dossier.values(), key=lambda x: x["date"])
    out = {"genere_le":datetime.now(timezone.utc).isoformat(),
           "source":"open data Assemblée nationale — scrutins 17e législature",
           "source_url":URL_SCRUTINS,"nb_textes":len(base),
           "nb_scrutins_total":len(scrutins),"textes":base}
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE,"w",encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    avec = sum(1 for b in base if b["votesParGroupe"])
    print("\n✓ %s" % OUTPUT_FILE)
    print("  %d scrutins → %d dossiers distincts" % (len(scrutins), len(base)))
    print("  dont %d avec décompo par groupe" % avec)
    if base: print("  période : %s → %s" % (base[0]["date"], base[-1]["date"]))

if __name__ == "__main__":
    construire()

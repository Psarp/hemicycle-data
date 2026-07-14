#!/usr/bin/env python3
"""build_amendements_ci.py — Version GitHub Actions.
Télécharge l'archive globale des amendements et n'extrait QUE les dossiers
présents dans public/data.json (vos ~71 textes).

Optimisation clé : dans l'archive, les amendements sont rangés par dossier DLR
au premier niveau (json/<DLR>/...). On ne lit donc QUE les entrées dont le
chemin contient un DLR de la base — on ignore tout le reste sans le parser.
Cela évite de traiter les centaines de milliers d'amendements non pertinents.

Le rattachement se fait par le DLR du chemin (direct, fiable), pas par le
champ interne texteLegislatifRef."""
import io, json, os, sys, zipfile, re
from collections import defaultdict
from urllib.request import urlopen, Request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_commun import ORGANE_TO_GROUPE, txt, clean_html

URL = "https://data.assemblee-nationale.fr/static/openData/repository/17/loi/amendements_div_legis/Amendements.json.zip"
DATA = os.path.join("public", "data.json")
OUT_AMDT_DIR = os.path.join("public", "amendements")

def parse_amendement(a):
    ident = a.get("identification") or {}
    sign = a.get("signataires") or {}
    auteur = sign.get("auteur") or {}
    cv = a.get("cycleDeVie") or {}
    contenu = (a.get("corps") or {}).get("contenuAuteur") or {}
    return {
        "num": txt(ident.get("numeroLong")),
        "groupe": ORGANE_TO_GROUPE.get(txt(auteur.get("groupePolitiqueRef")), "AUTRE"),
        "sort": txt(cv.get("sort")) or "—",
        "dateDepot": txt(cv.get("dateDepot")),
        "organe": txt(ident.get("prefixeOrganeExamen")),
        "objet": clean_html(txt(contenu.get("exposeSommaire")))[:12000],
    }

def telecharger(url):
    """Télécharge l'archive par blocs vers un fichier disque (robuste face aux
    coupures), avec quelques tentatives en cas d'interruption réseau."""
    import time
    dest = "Amendements.json.zip"
    for tentative in range(1, 4):
        try:
            print("Téléchargement archive amendements (~283 Mo), tentative %d…" % tentative)
            req = Request(url, headers={"User-Agent": "Hemicycle/1.0"})
            recu = 0
            with urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                while True:
                    bloc = r.read(1024 * 256)  # 256 Ko par bloc
                    if not bloc:
                        break
                    f.write(bloc)
                    recu += len(bloc)
                    if recu % (20 * 1024 * 1024) < 256 * 1024:
                        print("  ... %d Mo reçus" % (recu // 1024 // 1024))
            taille = os.path.getsize(dest)
            print("  Terminé : %d Mo" % (taille // 1024 // 1024))
            if taille < 50 * 1024 * 1024:
                raise IOError("archive trop petite (%d o), téléchargement incomplet" % taille)
            return zipfile.ZipFile(dest)
        except Exception as e:
            print("  Échec tentative %d : %s" % (tentative, e))
            if tentative < 3:
                time.sleep(5)
            else:
                raise

def main():
    if not os.path.exists(DATA):
        print("ERREUR: public/data.json absent. Lancez build_all.py d'abord."); sys.exit(1)
    data = json.load(open(DATA, encoding="utf-8"))
    base_dlr = set(t["ref"] for t in data["textes"])
    print("%d textes dans la base" % len(base_dlr))

    zf = telecharger(URL)
    # regex pour repérer un DLR dans le chemin d'un fichier
    par_dlr = defaultdict(list)
    n = keep = 0
    for nom in zf.namelist():
        if not nom.endswith(".json"): continue
        n += 1
        m = re.search(r"(DLR5L17N\d+)", nom)
        if not m: continue
        dlr = m.group(1)
        if dlr not in base_dlr: continue    # on ignore tout ce qui n'est pas dans la base
        try:
            a = json.loads(zf.read(nom).decode("utf-8")).get("amendement", {})
            par_dlr[dlr].append(parse_amendement(a))
            keep += 1
        except Exception:
            continue
    print("Scan terminé: %d fichiers vus, %d amendements retenus" % (n, keep))

    os.makedirs(OUT_AMDT_DIR, exist_ok=True)
    for t in data["textes"]:
        amdts = par_dlr.get(t["ref"], [])
        dep = defaultdict(int); ado = defaultdict(int); adoptes = []
        for a in amdts:
            dep[a["groupe"]] += 1
            if a["sort"].lower().startswith("adopt"):
                ado[a["groupe"]] += 1
                adoptes.append({"num":a["num"],"groupe":a["groupe"],"objet":a["objet"],"sort":a["sort"]})
        t["amendements"] = {"total":len(amdts),"adoptes":sum(ado.values()),
                            "parGroupe":{g:{"deposes":dep[g],"adoptes":ado.get(g,0)} for g in dep},
                            "listeAdoptes":adoptes}
        if amdts:
            json.dump({"texteRef":t["ref"],"titre":t["titre"],"amendements":amdts},
                      open(os.path.join(OUT_AMDT_DIR, t["ref"]+".json"),"w",encoding="utf-8"), ensure_ascii=False)
    json.dump(data, open(DATA,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
    nf = sum(1 for t in data["textes"] if t.get("amendements",{}).get("total",0)>0)
    print("✓ %d textes enrichis d'amendements" % nf)

if __name__ == "__main__":
    main()

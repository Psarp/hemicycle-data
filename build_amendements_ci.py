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
    """Télécharge l'archive de façon robuste :
    - par blocs vers le disque (pas de gros bloc en mémoire) ;
    - REPREND là où ça s'est arrêté si le serveur coupe (requête Range) ;
    - vérifie la taille annoncée par le serveur (Content-Length) ;
    - plusieurs tentatives avec attente progressive.
    Le serveur de l'AN coupe parfois la connexion en cours de route."""
    import time
    dest = "Amendements.json.zip"
    attendu = None
    if os.path.exists(dest):
        os.remove(dest)

    for tentative in range(1, 7):
        deja = os.path.getsize(dest) if os.path.exists(dest) else 0
        try:
            entetes = {"User-Agent": "Hemicycle/1.0"}
            if deja > 0:
                entetes["Range"] = "bytes=%d-" % deja
                print("Reprise du téléchargement à %d Mo (tentative %d)…" % (deja // 1024 // 1024, tentative))
            else:
                print("Téléchargement archive amendements (tentative %d)…" % tentative)

            req = Request(url, headers=entetes)
            with urlopen(req, timeout=120) as r:
                # taille totale annoncée
                if attendu is None:
                    cl = r.headers.get("Content-Length")
                    cr = r.headers.get("Content-Range")  # ex: bytes 100-999/1000
                    if cr and "/" in cr:
                        attendu = int(cr.split("/")[-1])
                    elif cl:
                        attendu = int(cl) + deja
                    if attendu:
                        print("  taille annoncée : %d Mo" % (attendu // 1024 // 1024))

                reprise = r.status == 206  # le serveur accepte la reprise
                mode = "ab" if (deja > 0 and reprise) else "wb"
                if deja > 0 and not reprise:
                    print("  (serveur sans reprise : on repart de zéro)")
                    deja = 0
                recu = deja
                with open(dest, mode) as f:
                    while True:
                        bloc = r.read(1024 * 256)
                        if not bloc:
                            break
                        f.write(bloc)
                        recu += len(bloc)
                        if recu % (25 * 1024 * 1024) < 256 * 1024:
                            print("  ... %d Mo" % (recu // 1024 // 1024))

            taille = os.path.getsize(dest)
            complet = (attendu is not None and taille >= attendu) or (attendu is None and taille > 200 * 1024 * 1024)
            if complet:
                print("  ✓ Terminé : %d Mo" % (taille // 1024 // 1024))
                return zipfile.ZipFile(dest)
            print("  Incomplet : %d Mo reçus sur %s Mo attendus" % (
                taille // 1024 // 1024, (attendu // 1024 // 1024) if attendu else "?"))
        except Exception as e:
            print("  Interruption tentative %d : %s" % (tentative, e))

        if tentative < 6:
            attente = min(10 * tentative, 45)
            print("  nouvelle tentative dans %d s…" % attente)
            time.sleep(attente)

    raise IOError("Téléchargement impossible après 6 tentatives (serveur AN instable). "
                  "Relancez le workflow plus tard.")

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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_descriptions.py — Enrichit ET allège public/data.json.

1) DESCRIPTION : pour chaque texte, récupère l'EXPOSÉ DES MOTIFS complet du
   document initial (open data AN ou Sénat) et l'ajoute en prose. On montre
   l'objet ENTIER et contextualisé du texte, pas des fragments.
   -> INCRÉMENTAL : l'exposé est figé au dépôt, donc on maintient un cache
      (public/descriptions.json) et on ne télécharge que les textes manquants.

2) ALLÈGEMENT : retire de data.json le détail volumineux des amendements
   adoptés (listeAdoptes, ~16 Mo) qui est DÉJÀ servi à la demande par les
   fichiers public/amendements/{ref}.json. data.json passe de ~18 Mo à ~1 Mo,
   ce qui rend le chargement de la page quasi instantané.

Aucune dépendance externe (bibliothèque standard uniquement).

Usage :
    python build_descriptions.py            # complément incrémental (quotidien)
    python build_descriptions.py --retry    # réessaie aussi les "vide"/"echec"
    python build_descriptions.py --force     # tout refaire
"""
import json, re, sys, time, os, urllib.request, urllib.error
from datetime import datetime, timezone

RACINE   = os.path.dirname(os.path.abspath(__file__))
DATA     = os.path.join(RACINE, "public", "data.json")
CACHE    = os.path.join(RACINE, "public", "descriptions.json")
UA       = "Mozilla/5.0 (compatible; HemicycleBot/1.0; suivi citoyen open data)"
PAUSE    = 0.2            # politesse entre requêtes (s)
MAX_OCTETS = 600_000     # on ne lit que le début du document (l'exposé est en tête)
MAX_CARS   = 9000        # longueur max stockée par description
MAX_PARAS  = 30

REF_RE = re.compile(r"^(PION|PRJL)(AN|SN)R5.*?B(\d+)$")

MOTS_VIDES = {"de","la","le","les","des","du","aux","et","à","un","une","pour",
              "loi","proposition","projet","visant","relative","relatif","portant",
              "organique","sur","dans","en","au","par"}

# ---------------------------------------------------------------------------
# 1. Choix du document source (chambre d'origine)
# ---------------------------------------------------------------------------
def refs_texte(t):
    an, sn = {}, {}
    for r in (t.get("textesRefs") or []):
        m = REF_RE.match(r)
        if m:
            (an if m.group(2) == "AN" else sn)[int(m.group(3))] = r
    return an, sn

def chambre_origine(t):
    labels = " ".join(e.get("label", "") for e in (t.get("cycle") or []))
    ia, is_ = labels.find("Assemblée"), labels.find("Sénat")
    if ia == -1 and is_ == -1:
        return None
    return "AN" if (is_ == -1 or (ia != -1 and ia < is_)) else "SN"

def choisir_source(t):
    an, sn = refs_texte(t)
    if not an and not sn:
        return None, None
    ori = chambre_origine(t)
    if ori == "AN" and an:
        return "AN", an[min(an)]
    if ori == "SN" and sn:
        return "SN", min(sn)
    if an:
        return "AN", an[min(an)]
    if sn:
        return "SN", min(sn)
    return None, None

def url_an(ref):
    return "https://www.assemblee-nationale.fr/dyn/opendata/%s.html" % ref

def urls_sn(ref_num, t):
    nature = "ppl"
    for r in (t.get("textesRefs") or []):
        if r.endswith("B%04d" % ref_num) and "SN" in r:
            nature = "pjl" if r.startswith("PRJL") else "ppl"
            break
    an_vote = int((t.get("date") or "2025-01-01")[:4])
    annees = []
    for base in (an_vote - 1, an_vote, an_vote - 2, an_vote - 3):
        aa = base % 100
        if aa not in annees:
            annees.append(aa)
    return ["https://www.senat.fr/leg/exposes-des-motifs/%s%02d-%03d-expose.html"
            % (nature, aa, ref_num) for aa in annees]

# ---------------------------------------------------------------------------
# 2. Téléchargement (borné : on ne lit que le début du document)
# ---------------------------------------------------------------------------
def telecharger(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            brut = r.read(MAX_OCTETS)      # lecture partielle -> rapide même sur gros textes
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None
    for enc in ("utf-8", "latin-1"):
        try:
            return brut.decode(enc)
        except UnicodeDecodeError:
            continue
    return brut.decode("utf-8", "replace")

# ---------------------------------------------------------------------------
# 3. HTML -> texte en préservant les paragraphes
# ---------------------------------------------------------------------------
def html_vers_texte(html):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|article)\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&#x([0-9A-Fa-f]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    for a, b in (("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," "),
                 ("&#039;","'"),("&#39;","'"),("&laquo;","«"),("&raquo;","»"),
                 ("&rsquo;","’"),("&eacute;","é"),("&egrave;","è"),("&agrave;","à"),
                 ("&ccedil;","ç"),("&ocirc;","ô"),("&ldquo;","«"),("&rdquo;","»")):
        s = s.replace(a, b)
    lignes = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lignes)).strip()

# ---------------------------------------------------------------------------
# 4. Extraction de l'exposé des motifs COMPLET -> liste de paragraphes
# ---------------------------------------------------------------------------
def extraire(texte):
    t = (texte or "").replace("\u2011", "-").replace("\u00a0", " ")
    m = re.search(r"EXPOS[ÉE]\s+DES\s+MOTIFS", t, re.I)
    if not m:
        return None
    corps = t[m.end():]

    # Fin de l'exposé = début du dispositif. Marqueur fiable : « proposition/projet
    # de loi » suivi de « Article 1er ». Repli : première ligne « Article 1er » nue
    # (le dispositif ; dans l'exposé les renvois s'écrivent « L'article 1er… »).
    fin = re.search(r"(?is)(?:proposition|projet)\s+de\s+loi(?:\s+organique)?\s*\n+\s*"
                    r"Article\s+(?:1er|1\b|premier)", corps)
    if not fin:
        fin = re.search(r"(?im)^\s*Article\s+(?:1er|premier)\b", corps)
    if fin:
        corps = corps[:fin.start()]

    corps = re.sub(r"^\s*Mesdames,\s*(?:Messieurs|Mesdemoiselles)[^\n.]*[,.]?\s*",
                   "", corps.strip(), flags=re.I)

    # Paragraphes = blocs séparés par un saut de ligne, nettoyés.
    paras, total = [], 0
    for bloc in re.split(r"\n+", corps):
        p = re.sub(r"\s+", " ", bloc).strip()
        if len(p) < 40:                       # ignore titres, numéros, lignes vides
            continue
        if total + len(p) > MAX_CARS or len(paras) >= MAX_PARAS:
            break
        paras.append(p)
        total += len(p)
    return {"paragraphes": paras} if paras else None

def page_coherente(texte_page, t):
    """Garde-fou anti-collision (Sénat) : la page doit mentionner l'auteur ou des
    mots distinctifs du titre, sinon on a peut-être attrapé un homonyme d'une autre
    session portant le même numéro."""
    bas = (texte_page or "").lower()
    auteur = (t.get("auteur") or "").strip()
    if auteur:
        nom = auteur.split()[-1].lower()
        if len(nom) >= 4 and nom in bas:
            return True
    mots = [w for w in re.findall(r"[a-zà-ÿ]{5,}", (t.get("titre") or "").lower())
            if w not in MOTS_VIDES]
    if sum(1 for w in set(mots) if w in bas) >= 2:
        return True
    return not auteur and len(mots) < 2

# ---------------------------------------------------------------------------
# 5. Boucle principale
# ---------------------------------------------------------------------------
def maintenant():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def charger_json(path, defaut):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return defaut

def alleger_amendements(textes):
    """Retire le détail lourd (listeAdoptes) déjà servi par les fichiers séparés."""
    retire = 0
    for t in textes:
        a = t.get("amendements")
        if isinstance(a, dict) and "listeAdoptes" in a:
            a.pop("listeAdoptes", None)       # on garde total, adoptes, parGroupe
            retire += 1
    return retire

def main():
    force = "--force" in sys.argv
    retry = "--retry" in sys.argv or force

    data = charger_json(DATA, None)
    if not data or "textes" not in data:
        print("data.json introuvable ou vide — lancez build_all.py d'abord.")
        sys.exit(1)
    cache = charger_json(CACHE, {})
    textes = data["textes"]

    a_traiter = [t for t in textes
                 if force or cache.get(t["ref"]) is None
                 or (retry and cache.get(t["ref"], {}).get("statut") in ("vide", "echec"))]
    print("Textes : %d | en cache : %d | à traiter : %d"
          % (len(textes), len(cache), len(a_traiter)))

    stats = {"ok": 0, "aucun": 0, "vide": 0, "echec": 0}
    for i, t in enumerate(a_traiter, 1):
        ref = t["ref"]
        source, cible = choisir_source(t)
        if source is None:
            cache[ref] = {"statut": "aucun", "source": None, "maj": maintenant()}
            stats["aucun"] += 1
            continue

        urls = [url_an(cible)] if source == "AN" else urls_sn(cible, t)
        desc, url_ok, vu_reseau = None, None, False
        for url in urls:
            html = telecharger(url)
            time.sleep(PAUSE)
            if html is None:
                continue
            vu_reseau = True
            texte = html_vers_texte(html)
            if source == "SN" and not page_coherente(texte, t):
                continue
            d = extraire(texte)
            if d:
                desc, url_ok = d, url
                break

        if desc:
            cache[ref] = {"statut": "ok", "source": source, "url": url_ok,
                          "maj": maintenant(), **desc}
            stats["ok"] += 1
        else:
            statut = "vide" if vu_reseau else "echec"
            cache[ref] = {"statut": statut, "source": source, "maj": maintenant()}
            stats[statut] += 1

        if i % 20 == 0 or i == len(a_traiter):
            print("  %d/%d traités…" % (i, len(a_traiter)))

    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)

    # --- Injection des descriptions + allègement, puis écriture COMPACTE ---
    injectes = 0
    for t in textes:
        c = cache.get(t["ref"])
        if c and c.get("statut") == "ok" and c.get("paragraphes"):
            t["description"] = {"paragraphes": c["paragraphes"],
                                "source": c.get("source"), "url": c.get("url")}
            injectes += 1
        else:
            t.pop("description", None)
    retire = alleger_amendements(textes)

    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))  # compact

    taille = os.path.getsize(DATA) / 1024 / 1024
    print("Terminé. ok=%(ok)d  aucun=%(aucun)d  vide=%(vide)d  echec=%(echec)d" % stats)
    print("Descriptions dans data.json : %d/%d | amendements allégés : %d textes"
          % (injectes, len(textes), retire))
    print("Taille finale data.json : %.2f Mo" % taille)

if __name__ == "__main__":
    main()

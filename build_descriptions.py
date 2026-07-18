#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_descriptions.py — Ajoute une description à chaque texte de data.json,
extraite de l'EXPOSÉ DES MOTIFS du document initial (open data AN ou Sénat).

Principe : INCRÉMENTAL. L'exposé des motifs est figé au dépôt du texte et ne
change jamais. On maintient donc un cache (public/descriptions.json) : à chaque
exécution, on ne va chercher QUE les textes absents du cache (ou dont le dernier
essai a échoué pour raison réseau). Puis on injecte le champ `description` dans
public/data.json.

Aucune dépendance externe : uniquement la bibliothèque standard.

Usage :
    python build_descriptions.py            # complément incrémental (quotidien)
    python build_descriptions.py --retry    # réessaie aussi les "vide"/"echec"
    python build_descriptions.py --force     # tout refaire (rattrapage complet)
"""
import json, re, sys, time, os, urllib.request, urllib.error
from datetime import datetime, timezone

RACINE      = os.path.dirname(os.path.abspath(__file__))
DATA        = os.path.join(RACINE, "public", "data.json")
CACHE       = os.path.join(RACINE, "public", "descriptions.json")
UA          = "Mozilla/5.0 (compatible; HemicycleBot/1.0; suivi citoyen open data)"
PAUSE       = 0.5           # politesse entre deux requêtes (secondes)
MAX_ARTICLES_STOCKES = 40   # garde-fou : on ne stocke jamais plus que ça

# --- Phrases d'intention repérables pour le "résumé en une phrase" ----------
# Formules qui DÉCRIVENT ce que fait le texte (à conserver)…
INTENT = re.compile(
    r"(?:a pour (?:objet|but|objectif|finalité|ambition|effet)|vise[nt]? à|tend à|"
    r"entend\b|se propose de|propose de|ambitionne de|cherche à|consiste à|"
    r"permet(?:tra|tent)? de|a pour ambition de)", re.I)
# …mais on écarte les phrases « pointeur » qui ne décrivent rien.
INTENT_EXCLU = re.compile(
    r"(?:présente\s+(?:proposition|projet)|présent\s+projet|"
    r"c['’]est\s+(?:tout\s+)?l['’]objet|tel\s+est\s+l['’]objet)", re.I)

# Début d'un résumé d'article, ANCRÉ EN DÉBUT DE LIGNE :
#  « L'article 1er … », « Les articles 2 et 3 … ». Le (?![\d\-]) évite de confondre
#  avec un renvoi comme « l'article 227-17 du code pénal ».
ART_INTRO_LIGNE = re.compile(
    r"^(?:L['’]article\s+(?:premier|1er|\d{1,2})(?![\d\-])|Les\s+articles?\s+\d{1,2}\b)", re.I)
# Repli (si tout l'exposé est sur une seule ligne) : même idée mais en milieu de texte,
# en exigeant un verbe minuscule juste après (pour écarter les renvois de code).
ART_INTRO_INLINE = re.compile(
    r"(?=(?:^|[.\s])L['’]article\s+(?:premier|1er|\d{1,2})(?![\d\-])\s+[a-zà-ÿ])")

# ---------------------------------------------------------------------------
# 1. Analyse des références de documents (textesRefs)
# ---------------------------------------------------------------------------
# Format : {NATURE}{CHAMBRE}R5{...}B{NNNN}
#   NATURE : PION (proposition de loi), PRJL (projet de loi), RAPP (rapport)…
#   CHAMBRE: AN (Assemblée) / SN (Sénat)
REF_RE = re.compile(r"^(PION|PRJL)(AN|SN)R5.*?B(\d+)$")

def refs_texte(t):
    """Renvoie ({num:ref} AN, [(num,ref) SN]) des documents initiaux (PION/PRJL)."""
    an, sn = {}, {}
    for r in (t.get("textesRefs") or []):
        m = REF_RE.match(r)
        if not m:
            continue
        _nat, chambre, num = m.group(1), m.group(2), int(m.group(3))
        (an if chambre == "AN" else sn)[num] = r
    return an, sn

def chambre_origine(t):
    """'AN' ou 'SN' selon la chambre qui ouvre le cycle législatif."""
    labels = " ".join(e.get("label", "") for e in (t.get("cycle") or []))
    ia, is_ = labels.find("Assemblée"), labels.find("Sénat")
    if ia == -1 and is_ == -1:
        return None
    if is_ == -1 or (ia != -1 and ia < is_):
        return "AN"
    return "SN"

def choisir_source(t):
    """Retourne (source, ref_ou_num) pour aller chercher l'exposé, ou (None, None)."""
    an, sn = refs_texte(t)
    if not an and not sn:
        return None, None                       # motion, déclaration… : pas de texte
    ori = chambre_origine(t)
    # On privilégie la chambre d'origine (elle porte l'exposé initial),
    # sinon on se rabat sur ce qui existe.
    if ori == "AN" and an:
        return "AN", an[min(an)]                 # plus petit numéro = dépôt initial
    if ori == "SN" and sn:
        return "SN", min(sn)
    if an:
        return "AN", an[min(an)]
    if sn:
        return "SN", min(sn)
    return None, None

# ---------------------------------------------------------------------------
# 2. Construction des URL et récupération
# ---------------------------------------------------------------------------
def url_an(ref):
    return "https://www.assemblee-nationale.fr/dyn/opendata/%s.html" % ref

def urls_sn(ref_num, t):
    """Candidats d'URL Sénat. La référence Sénat ne donne pas l'année de session ;
    on la déduit de la date connue du texte, avec repli sur les sessions voisines."""
    # Nature Sénat : on relit la ref d'origine pour distinguer ppl / pjl
    nature = "ppl"
    for r in (t.get("textesRefs") or []):
        if r.endswith("B%04d" % ref_num) and "SN" in r:
            nature = "pjl" if r.startswith("PRJL") else "ppl"
            break
    an_vote = int((t.get("date") or "2025-01-01")[:4])
    # Session « AA » : une session parlementaire démarre en octobre et porte
    # l'année de début. Le dépôt au Sénat précède le vote à l'AN, souvent de
    # quelques mois : la session de dépôt la plus probable est (année du vote − 1),
    # puis l'année du vote, puis les précédentes.
    ordre, annees = [an_vote - 1, an_vote, an_vote - 2, an_vote - 3], []
    for base in ordre:
        aa = base % 100
        if aa not in annees:
            annees.append(aa)
    return ["https://www.senat.fr/leg/exposes-des-motifs/%s%02d-%03d-expose.html"
            % (nature, aa, ref_num) for aa in annees]

MOTS_VIDES = {"de","la","le","les","des","du","aux","et","à","un","une","pour",
              "loi","proposition","projet","visant","relative","relatif","portant",
              "organique","sur","dans","en","au","par"}

def page_coherente(texte_page, t):
    """Garde-fou anti-collision (Sénat) : la page récupérée doit mentionner l'auteur
    ou des mots distinctifs du titre — sinon on a peut-être attrapé un texte homonyme
    d'une autre session portant le même numéro."""
    bas = (texte_page or "").lower()
    # 1) nom de famille de l'auteur
    auteur = (t.get("auteur") or "").strip()
    if auteur:
        nom = auteur.split()[-1].lower()
        if len(nom) >= 4 and nom in bas:
            return True
    # 2) au moins deux mots distinctifs du titre
    mots = [m for m in re.findall(r"[a-zà-ÿ]{5,}", (t.get("titre") or "").lower())
            if m not in MOTS_VIDES]
    trouves = sum(1 for m in set(mots) if m in bas)
    if trouves >= 2:
        return True
    # 3) si on n'a ni auteur ni titre exploitable, on ne bloque pas
    return not auteur and len(mots) < 2

def telecharger(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            brut = r.read()
        for enc in ("utf-8", "latin-1"):
            try:
                return brut.decode(enc)
            except UnicodeDecodeError:
                continue
        return brut.decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None

# ---------------------------------------------------------------------------
# 3. HTML -> texte en préservant les sauts de paragraphe
# ---------------------------------------------------------------------------
def html_vers_texte(html):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|section|article)\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    # Entités
    s = re.sub(r"&#x([0-9A-Fa-f]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    for a, b in (("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," "),
                 ("&#039;","'"),("&#39;","'"),("&laquo;","«"),("&raquo;","»"),
                 ("&rsquo;","’"),("&eacute;","é"),("&egrave;","è"),("&agrave;","à")):
        s = s.replace(a, b)
    # Nettoyage : espaces multiples par ligne, lignes vides regroupées
    lignes = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lignes)).strip()

# ---------------------------------------------------------------------------
# 4. Extraction de l'exposé des motifs -> {resume, chapeau, articles}
# ---------------------------------------------------------------------------
def extraire(texte):
    t = (texte or "").replace("\u2011", "-").replace("\u00a0", " ")
    m = re.search(r"EXPOS[ÉE]\s+DES\s+MOTIFS", t, re.I)
    if not m:
        return None
    corps = t[m.end():]
    # Fin de l'exposé = début du dispositif (« proposition de loi » / « projet de loi »
    # sur une ligne, juste avant « Article 1er »).
    fin = re.search(r"(?im)^\s*(?:proposition|projet)\s+de\s+loi\s*$", corps)
    if fin:
        corps = corps[:fin.start()]
    corps = re.sub(r"^\s*Mesdames,\s*Messieurs,?\s*", "", corps.strip(), flags=re.I)

    # Découpe en « chapeau » (avant le 1er résumé d'article) + « articles ».
    # Méthode principale : ligne par ligne, un article = ligne commençant par
    # « L'article N … » jusqu'à la ligne d'intro suivante.
    lignes = [ln.strip() for ln in corps.split("\n")]
    idx = [i for i, ln in enumerate(lignes) if ART_INTRO_LIGNE.match(ln)]
    articles = []
    if idx:
        chapeau_src = " ".join(lignes[:idx[0]])
        bornes = idx + [len(lignes)]
        for k in range(len(idx)):
            frag = re.sub(r"\s+", " ", " ".join(l for l in lignes[bornes[k]:bornes[k+1]] if l)).strip()
            if 15 <= len(frag) <= 800:            # garde-fou anti-dispositif
                articles.append(frag)
    else:
        # Repli : exposé sur une seule ligne -> découpe par renvois inline stricts.
        plat = re.sub(r"\s+", " ", corps)
        coupes = [m.start() for m in ART_INTRO_INLINE.finditer(plat)]
        if coupes:
            chapeau_src = plat[:coupes[0]]
            bornes = coupes + [len(plat)]
            for k in range(len(coupes)):
                frag = plat[bornes[k]:bornes[k+1]].strip(" .")
                frag = re.sub(r"^[.\s]+", "", frag)
                if 15 <= len(frag) <= 800:
                    articles.append(frag)
        else:
            chapeau_src = plat
    articles = articles[:MAX_ARTICLES_STOCKES]

    chapeau = re.sub(r"\s+", " ", chapeau_src).strip()
    # Résumé « en une phrase » : première phrase DESCRIPTIVE du chapeau (formule
    # d'intention, hors phrases « pointeur »). Sinon vide — on n'invente rien.
    resume = ""
    for phrase in re.split(r"(?<=[.!?])\s+", chapeau):
        p = phrase.strip()
        if 30 <= len(p) <= 320 and INTENT.search(p) and not INTENT_EXCLU.search(p):
            resume = p
            break

    if not articles and not resume and len(chapeau) < 40:
        return None                               # rien d'exploitable
    return {"resume": resume, "chapeau": chapeau[:1500], "articles": articles}

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

def main():
    force = "--force" in sys.argv
    retry = "--retry" in sys.argv or force

    data = charger_json(DATA, None)
    if not data or "textes" not in data:
        print("data.json introuvable ou vide — lancez build_all.py d'abord.")
        sys.exit(1)
    cache = charger_json(CACHE, {})

    textes = data["textes"]
    a_traiter = []
    for t in textes:
        ref = t["ref"]
        c = cache.get(ref)
        if force or c is None or (retry and c.get("statut") in ("vide", "echec")):
            a_traiter.append(t)

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
        desc, url_ok = None, None
        for url in urls:
            html = telecharger(url)
            time.sleep(PAUSE)
            if not html:
                continue
            texte = html_vers_texte(html)
            # Sénat : on devine l'année de session, donc on vérifie qu'on n'a pas
            # attrapé un texte homonyme d'une autre session.
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
            # distingue "rien trouvé" (vide) d'un échec réseau total
            statut = "echec" if all(telecharger(u) is None for u in urls[:1]) else "vide"
            cache[ref] = {"statut": statut, "source": source, "maj": maintenant()}
            stats[statut] += 1

        if i % 20 == 0 or i == len(a_traiter):
            print("  %d/%d traités…" % (i, len(a_traiter)))

    # Sauvegarde du cache
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)

    # Injection dans data.json : on n'ajoute le champ que s'il y a du contenu.
    injectes = 0
    for t in textes:
        c = cache.get(t["ref"])
        if c and c.get("statut") == "ok":
            t["description"] = {
                "resume":   c.get("resume", ""),
                "articles": c.get("articles", []),
                "source":   c.get("source"),
                "url":      c.get("url"),
            }
            injectes += 1
        else:
            t.pop("description", None)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    print("Terminé. ok=%(ok)d  aucun=%(aucun)d  vide=%(vide)d  echec=%(echec)d" % stats)
    print("Descriptions présentes dans data.json : %d / %d" % (injectes, len(textes)))

if __name__ == "__main__":
    main()

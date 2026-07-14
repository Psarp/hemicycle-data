"""Fonctions et tables partagées par les scripts Hémicycle."""
import re

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

def clean_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&#x([0-9A-Fa-f]+);", lambda m: chr(int(m.group(1),16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = s.replace("&amp;","&").replace("&lt;","<").replace("&gt;",">").replace("&nbsp;"," ").replace("&#039;","'").replace("&#39;","'")
    return re.sub(r"\s+", " ", s).strip()

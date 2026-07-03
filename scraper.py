#!/usr/bin/env python3
# Recuperation des VL — architecture HYBRIDE (fiabilite long terme) :
#  1) Voie rapide : API JSON Amundi /product-services/fdr/share/v3/full/{ISIN} via curl_cffi
#     (empreinte TLS Chrome). NB: renvoie 405 a ce jour depuis GitHub Actions — la voie
#     rapide est conservee car elle gagnera si Amundi accepte un jour la requete
#     (ou quand la bonne methode/en-tete sera identifiee via un Copy-as-cURL navigateur).
#  2) Repli PROUVE : rendu de la page produit via Playwright + regex multi-lignes validee
#     ("Valeur liquidative / Au JJ/MM/AAAA / 39,51 EUR"). Le navigateur n'est lance que si
#     la voie 1 echoue (cout paye uniquement en cas de besoin).
#  3) Dernier filet : reprise de la derniere VL connue (status "ancien").
#  - Fonds PEG (QS...) : API JSON interne Amundi EE  /product-services/fdr/share/v3/full/{ISIN}
#                        -> champ lastNav.value (nombre) et lastNav.date (ISO AAAA-MM-JJ)
# Run attendu : quelques secondes (aucun Chromium a installer, aucun rendu a attendre).
import json, re, os, datetime, time
# curl_cffi remplace requests : meme API, mais reproduit l'empreinte TLS/JA3/HTTP2 de Chrome.
# Necessaire car l'API Amundi filtre par empreinte TLS (405 avec requests, 200 en navigateur).
from curl_cffi import requests

FONDS = [
    { "isin":"QS0009080720", "label":"Amundi Label Monetaire ESR - F",          "scope":"PEG+PER" },
    { "isin":"QS0009099829", "label":"Amundi Protect 90 ESR",                   "scope":"PEG+PER" },
    { "isin":"QS0009080746", "label":"Amundi Label Equilibre ESR - F",          "scope":"PEG+PER" },
    { "isin":"QS0009122746", "label":"CPR ES Action Climat - F",                "scope":"PEG+PER" },
    { "isin":"QS0009080175", "label":"Amundi Actions Internationales ESR - F",  "scope":"PEG+PER" },
    { "isin":"QS0009102334", "label":"Amundi Label Harmonie Solidaire ESR - F", "scope":"PEG+PER" },
    { "isin":"QS0009116219", "label":"Amundi Convictions ESR - F (C)",          "scope":"PER" },
]
OUT = "nav.json"; HIST = "history.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

def parse_num(s):
    s = s.strip().replace('\u00a0','').replace(' ','').replace('€','')
    if ',' in s and '.' in s:
        s = (s.replace('.','').replace(',','.')) if s.rfind(',')>s.rfind('.') else s.replace(',','')
    elif ',' in s:
        s = s.replace(',','.')
    return float(s)

def http_req(url, headers, tries=3, timeout=30, json_body=None):
    # json_body != None -> POST (l'API Amundi n'accepte que POST : GET -> 405)
    for t in range(1, tries+1):
        try:
            if json_body is not None:
                r = requests.post(url, headers=headers, json=json_body, timeout=timeout, impersonate="chrome")
            else:
                r = requests.get(url, headers=headers, timeout=timeout, impersonate="chrome")
            if r.status_code == 200:
                return r
            print(f"[http] {url} -> HTTP {r.status_code} (essai {t}/{tries})")
        except Exception as e:
            print(f"[http] {url} erreur: {e} (essai {t}/{tries})")
        time.sleep(4)
    return None

def http_get(url, headers, tries=3, timeout=30):
    return http_req(url, headers, tries=tries, timeout=timeout)

# --- PEG : API JSON Amundi EE ------------------------------------------------
def find_last_nav(obj):
    # Cherche un objet {value, date, ...} porte par une cle contenant "nav" (ex. lastNav).
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and "nav" in k.lower() and "value" in v and "date" in v:
                return v
        for v in obj.values():
            r = find_last_nav(v)
            if r: return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_last_nav(v)
            if r: return r
    return None

def scrape_amundi_api(isin):
    url = f"https://www.amundi-ee.com/product-services/fdr/share/v3/full/{isin}"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Origin": "https://www.amundi-ee.com",
        "Referer": f"https://www.amundi-ee.com/epargnant/product/view/{isin}",
    }
    # L'API exige un POST dont le corps liste les champs voulus (GET -> 405).
    body = {"fields": ["isin", "label", "lastNav.value", "lastNav.date", "currency.iso3Code"]}
    print(f"[amundi] POST {url}")
    r = http_req(url, headers, json_body=body)
    if not r:
        return None, None
    try:
        data = r.json()
    except Exception as e:
        print(f"[amundi]   JSON invalide: {e}")
        return None, None
    nav = find_last_nav(data)
    if nav and nav.get("value") is not None:
        try:
            value = float(nav["value"])
        except Exception:
            print(f"[amundi]   value non numerique: {nav.get('value')}")
            return None, None
        nav_date = nav.get("date")  # deja au format ISO AAAA-MM-JJ
        print(f"[amundi]   value={value} date={nav_date}")
        return value, nav_date
    print(f"[amundi]   lastNav/value introuvable dans le JSON")
    return None, None

# --- PEG repli : page produit via Playwright (regex multi-lignes validee) -----
_PW = {"browser": None, "page": None, "ctx": None, "pw": None, "failed": False}

def _playwright_page():
    # Demarre le navigateur une seule fois, a la premiere demande.
    if _PW["page"] is not None or _PW["failed"]:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
        _PW["pw"] = sync_playwright().start()
        _PW["browser"] = _PW["pw"].chromium.launch(headless=True,
            args=["--disable-blink-features=AutomationControlled","--no-sandbox"])
        _PW["ctx"] = _PW["browser"].new_context(user_agent=UA, locale="fr-FR",
            timezone_id="Europe/Paris", viewport={"width":1366,"height":900})
        _PW["ctx"].add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        _PW["page"] = _PW["ctx"].new_page()
    except Exception as e:
        print(f"[playwright] indisponible: {e}")
        _PW["failed"] = True
    return _PW["page"]

def _playwright_close():
    try:
        if _PW["browser"]: _PW["browser"].close()
        if _PW["pw"]: _PW["pw"].stop()
    except Exception:
        pass

def _accept_cookies(page):
    for sel in ["#onetrust-accept-btn-handler",
                "button:has-text('Tout accepter')",
                "button:has-text('Tout accepter et fermer')",
                "button:has-text('Accepter')",
                "button:has-text(\"J'accepte\")",
                "button:has-text('OK')"]:
        try:
            b = page.query_selector(sel)
            if b:
                b.click(timeout=3000); page.wait_for_timeout(1200)
                print(f"[playwright]   consentement clique ({sel})")
                return
        except Exception:
            pass

def scrape_amundi_playwright(isin):
    page = _playwright_page()
    if page is None:
        return None, None
    url = f"https://www.amundi-ee.com/epargnant/product/view/{isin}"
    text = ""
    for tentative in range(1, 4):
        print(f"[playwright] (PEG) {url}  (essai {tentative}/3)")
        try: page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e: print(f"[playwright]   goto: {e}")
        _accept_cookies(page)
        try:
            page.wait_for_function(
                "document.body && /valeur\\s+liquidative/i.test(document.body.innerText)",
                timeout=15000)
        except Exception:
            print("[playwright]   VL pas encore affichee...")
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        if "Service Unavailable" in text or "Access Denied" in text:
            print("[playwright]   Amundi indisponible/bloque, nouvel essai dans 8s...")
            page.wait_for_timeout(8000); continue
        # Format reel (multi-lignes) : "Valeur liquidative\nAu JJ/MM/AAAA\n39,51 EUR"
        m = re.search(
            r'Valeur\s+liquidative\s*(?:Au\s+(\d{2}/\d{2}/\d{4})\s*)?([\d\u00a0 ]+[.,]\d{2,4})\s*€',
            text, re.IGNORECASE)
        if m:
            value = parse_num(m.group(2))
            nav_date = None
            if m.group(1):
                jj,mm,aaaa = m.group(1).split("/"); nav_date = f"{aaaa}-{mm}-{jj}"
            print(f"[playwright]   value={value} date={nav_date}")
            return value, nav_date
        print("[playwright]   VL introuvable, nouvel essai dans 5s...")
        page.wait_for_timeout(5000)
    print("[playwright]   echec apres 3 essais, extrait:"); print((text or '')[:400])
    return None, None

# --- IO ----------------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return default

def merge_history(history, isin, date, value):
    arr = history.get(isin, [])
    for e in arr:
        if e["date"]==date: e["value"]=value; break
    else: arr.append({"date":date,"value":value})
    arr.sort(key=lambda e:e["date"]); history[isin]=arr[-400:]
    return history

def last_known(history, isin):
    arr = history.get(isin, [])
    if arr:
        last = arr[-1]
        return last["value"], last["date"]
    return None, None

def main():
    results={}; history=load_json(HIST,{})
    for f in FONDS:
        value,nav_date=scrape_amundi_api(f["isin"])
        if value is None:
            print(f"[robot]   API KO -> repli Playwright pour {f['isin']}")
            value,nav_date=scrape_amundi_playwright(f["isin"])

        if value is not None:
            date = nav_date or datetime.date.today().isoformat()
            results[f["isin"]]={"isin":f["isin"],"label":f["label"],"scope":f["scope"],
                "value":value,"currency":"EUR","date":date,"status":"ok"}
            merge_history(history,f["isin"],date,value)
        else:
            old_val, old_date = last_known(history, f["isin"])
            if old_val is not None:
                print(f"[robot]   -> reprise derniere VL connue : {old_val} ({old_date})")
                results[f["isin"]]={"isin":f["isin"],"label":f["label"],"scope":f["scope"],
                    "value":old_val,"currency":"EUR","date":old_date,"status":"ancien"}
            else:
                results[f["isin"]]={"isin":f["isin"],"label":f["label"],"scope":f["scope"],
                    "value":None,"currency":"EUR","date":datetime.date.today().isoformat(),"status":"not_found"}

        print(f"[robot] {f['isin']} -> {results[f['isin']]}")
    _playwright_close()
    with open(OUT,"w",encoding="utf-8") as fh: json.dump(results,fh,ensure_ascii=False,indent=2)
    with open(HIST,"w",encoding="utf-8") as fh: json.dump(history,fh,ensure_ascii=False,indent=2)
    print(f"[robot] Ecrit {OUT} et {HIST}")

if __name__=="__main__": main()

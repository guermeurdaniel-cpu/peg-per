#!/usr/bin/env python3
import json, re, os, datetime
from playwright.sync_api import sync_playwright

# Les 7 fonds. "scope" sert uniquement a l'affichage (PEG+PER ou PER).
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

def parse_num(s):
    s = s.strip().replace('\u00a0','').replace(' ','').replace('€','')
    if ',' in s and '.' in s:
        s = (s.replace('.','').replace(',','.')) if s.rfind(',')>s.rfind('.') else s.replace(',','')
    elif ',' in s:
        s = s.replace(',','.')
    return float(s)

def scrape_amundi(page, isin):
    url = f"https://www.amundi-ee.com/epargnant/product/view/{isin}"
    text = ""
    for tentative in range(1, 5):   # jusqu'a 4 essais
        print(f"[robot] {isin}  {url}  (essai {tentative}/4)")
        try: page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e: print(f"[robot]   goto: {e}")
        try:
            page.wait_for_function(
                "document.body && document.body.innerText.includes('Valeur Liquidative')",
                timeout=25000)
        except Exception:
            print("[robot]   VL pas encore affichee...")
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
        if "Service Unavailable" in text:
            print("[robot]   Amundi indisponible, nouvel essai dans 15s...")
            page.wait_for_timeout(15000); continue
        m = re.search(r'Valeur Liquidative\s*\(C\)\s*:\s*([\d \u00a0]+[.,]\d{2,4})', text)
        if m:
            value = parse_num(m.group(1))
            nav_date = None
            d = re.search(r'Date des donn[ée]es\s*:\s*(\d{2}/\d{2}/\d{4})', text)
            if d:
                jj,mm,aaaa = d.group(1).split("/"); nav_date=f"{aaaa}-{mm}-{jj}"
            return value, nav_date
        print("[robot]   VL introuvable, nouvel essai dans 12s...")
        page.wait_for_timeout(12000)
    print("[robot]   echec apres 4 essais, extrait:"); print((text or '')[:800])
    return None, None

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

def main():
    results={}; history=load_json(HIST,{})
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True); page=browser.new_page()
        for f in FONDS:
            value,nav_date=scrape_amundi(page,f["isin"])
            date=nav_date or datetime.date.today().isoformat()
            results[f["isin"]]={"isin":f["isin"],"label":f["label"],"scope":f["scope"],
                "value":value,"currency":"EUR","date":date,"status":"ok" if value is not None else "not_found"}
            print(f"[robot] {f['isin']} -> {results[f['isin']]}")
            if value is not None: merge_history(history,f["isin"],date,value)
        browser.close()
    with open(OUT,"w",encoding="utf-8") as fh: json.dump(results,fh,ensure_ascii=False,indent=2)
    with open(HIST,"w",encoding="utf-8") as fh: json.dump(history,fh,ensure_ascii=False,indent=2)
    print(f"[robot] Ecrit {OUT} et {HIST}")

if __name__=="__main__": main()

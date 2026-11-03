#!/usr/bin/env python3
"""Hämta Willys offentliga veckoblad, extrahera *konservativa* erbjudanden.

Avsiktligt ingen fabricering: om källdokumentet eller prisprodukten är
osäkra stoppas publicering. Kräver ej API-nyckel; kräver tillåten åtkomst.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import fitz
import requests

URL = "https://reklamblad.willys.se/willys/2176/"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "campaigns.json"
AGENT = "Smaksparare/1.0 (+weekly-public-leaflet; contact project owner)"
# Denna begränsade lista kopplar *exakt* specificerad produkt till receptets råvara.
# Kategorinamn som enbart 'OST' eller 'FÄRS' matchas aldrig mot fel varumärke.
CATALOG = {
    "falukorv": (r"\bFALUKORV\b", "g", 800),
    "flaskytterfile": (r"\bFLÄSKYTTERFIL[ÉE]\b", "g", 1000),
    "applen": (r"\bSVENSKA\s+ÄPPLEN\b", "g", 1000),
    "prastost": (r"\bPRÄST\b", "g", 1000),
    "salsiccia": (r"\bSALSICCIAFÄRS\b", "g", 400),
    "broccoli": (r"\bBROCCOLI\b", "g", 1000),
    "potatis": (r"\bPOTATIS\b", "g", 1000),
    "morot": (r"\bMORÖTTER\b", "g", 1000),
    "tomatkross": (r"\bKROSSADE\s+TOMATER\b", "g", 390),
    "kyckling": (r"\bKYCKLINGFIL[ÉE]\b", "g", 1000),
    "lax": (r"\bLAXFIL[ÉE]\b", "g", 500),
    "halloumi": (r"\bHALLOUMI\b", "g", 200),
}


def fetch_pdf(session):
    # iPaper dokumenterar suffixet, men ägaren kan stänga av PDF-åtkomst.
    candidate = URL + "viewpdf.ashx"
    response = session.get(candidate, timeout=35, headers={"Accept": "application/pdf,*/*"})
    response.raise_for_status()
    if not response.content.startswith(b"%PDF-"):
        raise RuntimeError("PDF-åtkomst är inte aktiverad eller svarade inte med en PDF. Hämtningen avbryts.")
    if len(response.content) > 35 * 1024 * 1024:
        raise RuntimeError("Reklambladets PDF är oväntat stor.")
    return response.content


def document_dates(text, now):
    # Tydliga, maskinläsbara veckodatum krävs. Acceptera inte bara filnamnet V45.
    m = re.search(r"(?:måndag\s+)?(\d{1,2})\s*/\s*(\d{1,2})\s*t\s*o\s*m\s*(?:söndag\s+)?(\d{1,2})\s*/\s*(\d{1,2})\s*(20\d{2})", text, re.I)
    if not m:
        m = re.search(r"(?:från\s+måndag\s+)?(\d{1,2})\s*/\s*(\d{1,2}).{0,24}?(?:söndag\s+)?(\d{1,2})\s*/\s*(\d{1,2})\s*(20\d{2})", text, re.I)
    if not m:
        raise RuntimeError("Hittade inte ett entydigt giltighetsintervall i bladet.")
    d1, mo1, d2, mo2, yr = map(int, m.groups())
    start = date(yr if mo1 <= mo2 else yr-1, mo1, d1)
    end = date(yr, mo2, d2)
    if not (start <= now <= end):
        raise RuntimeError(f"Bladet avser {start}–{end}, inte dagens aktiva vecka ({now}).")
    if not (0 <= (end-start).days <= 13):
        raise RuntimeError("Bladets datumintervall verkar orimligt.")
    return start, end


def pdf_lines(page):
    """Ett objekt per visuell textrad; ingen OCR eller extern AI används."""
    obj = page.get_text("dict", sort=True)
    result = []
    for block in obj["blocks"]:
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = line.get("spans") or []
            text = " ".join(x["text"].strip() for x in spans if x["text"].strip())
            if not text:
                continue
            rect = fitz.Rect(line["bbox"])
            result.append({"text": text, "box": rect})
    return result


def normalize(text):
    return re.sub(r"\s+", " ", text.upper())


def find_prices(lines):
    """Hitta bara uttryckliga huvudpriser – aldrig jämförpriser/lägsta 30 dagar.

    Layouten varierar. Resultaten granskas även geometriskt före publicering.
    """
    out = []
    for i, line in enumerate(lines):
        t = normalize(line["text"])
        if any(s in t for s in ("JÄMFÖRPRIS", "30-DGR", "LÄGSTA", "SPARA", "MAX ")):
            continue
        if "FÖR" in t and re.search(r"\d\s*FÖR", t):
            continue  # Flerköp kräver särskild kassaberäkning, tas ej in automatiskt.
        m = re.match(r"^\s*(\d{1,3})\s+([0-9]{2})(?:\s|$)", t)
        if m:
            price = int(m.group(1)) + int(m.group(2))/100
            if 2 <= price <= 500:
                out.append({"price":price, "box":line["box"], "line_index":i})
    return out


def extract_offers(pdf, now):
    doc = fitz.open(stream=pdf, filetype="pdf")
    if not (1 <= len(doc) <= 40):
        raise RuntimeError("Oväntat antal sidor i reklambladet")
    combined = "\n".join(p.get_text() for p in doc)
    start, end = document_dates(combined, now)
    accepted, queue = [], []
    for page_idx, page in enumerate(doc):
        lines = pdf_lines(page)
        prices = find_prices(lines)
        for key, (pattern, unit, pack) in CATALOG.items():
            # Sök exakt etikett, inte enbart delord. Produktens block måste vara tydligt.
            found = [x for x in lines if re.search(pattern, normalize(x["text"]))]
            for label in found:
                # Närliggande priser *ovanför* produkttext. Endast singelträffar med
                # rimlig geometri används. Om två kan passa blir det granskningskö.
                x0, y0, x1, y1 = label["box"]
                possible = []
                for p in prices:
                    px0, py0, px1, py1 = p["box"]
                    dx = abs((px0+px1)/2 - (x0+x1)/2)
                    dy = y0 - (py0+py1)/2
                    if dx < max(33, (x1-x0)*.65) and -14 < dy < 110:
                        possible.append((dx*.8 + abs(dy)*.65, p))
                possible.sort(key=lambda item: item[0])
                if not possible or (len(possible)>1 and possible[1][0]-possible[0][0]<24):
                    queue.append({"key":key, "label":label["text"], "page":page_idx+1, "reason":"Pris saknas eller flera närliggande priser"})
                    continue
                candidate = possible[0][1]
                # Automatisk säkerhet: enhet måste vara angiven i närheten och
                # får INTE mistas för pris per styck eller ett flerpack.
                nearby = " ".join(x["text"] for x in lines if abs(x["box"].y0-candidate["box"].y0)<23 and abs(x["box"].x0-candidate["box"].x0)<125).upper()
                kg = "PER KG" in nearby or "KR/KG" in nearby
                fp = "PER FÖRP" in nearby or "PER ST" in nearby
                if not (kg or fp):
                    queue.append({"key":key, "label":label["text"], "page":page_idx+1, "reason":"Prisenheten kunde inte kontrolleras"})
                    continue
                if kg:
                    pack_used = 1000
                    loose = key not in ('flaskytterfile','kyckling','lax')
                else:
                    pack_used = pack
                    loose = False
                item = {"id": f"auto-{start.isoformat()}-{page_idx+1}-{key}", "key": key,
                    "name":label["text"].strip(), "price":candidate["price"],
                    "pack":pack_used, "unit":unit, "loose":loose,
                    "price_type":"per_kg" if kg else "package",
                    "valid_from": start.isoformat(), "valid_until": end.isoformat(),
                    "member_only": any("WILLYSPLUS" in normalize(x["text"]) and abs(x["box"].x0-candidate["box"].x0)<145 and 0 <= candidate["box"].y0-x["box"].y0 < 85 for x in lines), "source_page":page_idx+1,
                    "confidence":"automatic_extraction", "source":"willys_official"}
                # 'member_only=None' betyder okänd medlemsstatus, inte falskt.
                accepted.append(item)
    # Dubbletter med olika priser är osäkra och publiceras inte.
    bykey = {}
    for item in accepted:
        bykey.setdefault(item["key"], []).append(item)
    verified = []
    for key, entries in bykey.items():
        if len(entries) == 1:
            verified.append(entries[0])
        else:
            queue.append({"key":key, "reason":"Flera kampanjer för samma råvara – granska varumärke/villkor", "page": entries[0]["source_page"]})
    return {"start":start,"end":end,"offers":verified,"queue":queue,"pages":len(doc)}


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", help="Lokal PDF för test utan nätverk")
    parser.add_argument("--output", default=str(DEFAULT_OUT))
    parser.add_argument("--min-offers", type=int, default=1)
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Europe/Stockholm")).date()
    output = Path(args.output)
    try:
        if args.pdf:
            pdf = Path(args.pdf).read_bytes()
        else:
            sess = requests.Session()
            sess.headers.update({"User-Agent": AGENT})
            pdf = fetch_pdf(sess)
        result = extract_offers(pdf, today)
        if len(result["offers"]) < args.min_offers:
            raise RuntimeError(f"Bara {len(result['offers'])} säkert tolkade erbjudanden – gammal fil behålls.")
        existing = None
        if output.exists():
            try: existing = json.loads(output.read_text(encoding="utf-8"))
            except (ValueError,OSError): pass
        seed_verified = [o for o in (existing or {}).get("offers",[]) if o.get("confidence")=="verified" and o.get("valid_until")>=today.isoformat() and (existing or {}).get("week")==f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"]
        keys = {o["key"] for o in seed_verified}
        offers = seed_verified + [o for o in result["offers"] if o["key"] not in keys]
        payload = {"schema":1,"store_id":"2176","store":"Willys Lund Norra Fäladen","source_url":URL,
            "source_type":"official_leaflet","week":f"{result['start'].isocalendar().year}-W{result['start'].isocalendar().week:02d}",
            "valid_from":result["start"].isoformat(),"valid_until":result["end"].isoformat(),
            "fetched_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status":"automatic_partial","source_sha256":hashlib.sha256(pdf).hexdigest(),
            "note":"Automatisk och försiktig PDF-extraktion. Kontrollera alltid varans villkor och kampanjens original.",
            "offers":offers,"review_queue":result["queue"][:100]}
        if existing and existing.get("source_sha256")==payload["source_sha256"] and existing.get("week")==payload["week"] and existing.get("offers")==payload["offers"]:
            logging.info("Reklambladet oförändrat; ingen fil skrivs om.")
            return 0
        output.parent.mkdir(parents=True,exist_ok=True)
        pending=output.with_suffix(".pending")
        pending.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        pending.replace(output)
        logging.info("Sparade %s erbjudanden, %s för granskning, %s sidor",len(offers),len(result["queue"]),result["pages"])
    except Exception as error:
        logging.error("Ingen ny kampanjfil publicerad: %s",error)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    sys.exit(run())

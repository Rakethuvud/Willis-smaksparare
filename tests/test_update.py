from datetime import date
from pathlib import Path
import sys
import fitz
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'scripts'))
from update_campaigns import document_dates, extract_offers


def make_pdf(valid=True):
    d=fitz.open()
    p=d.new_page()
    p.insert_text((60,50),'Priserna gäller från måndag 2/11 t o m söndag 8/11 2026')
    p.insert_text((75,112),'19 90 Per förp')
    p.insert_text((75,146),'FALUKORV')
    p.insert_text((75,168),'GARANT 800g')
    return d.tobytes()


def test_date_rejects_stale():
    try:
        document_dates('2/11 t o m 8/11 2026',date(2026,11,9))
    except RuntimeError:
        return
    raise AssertionError('expired date accepted')


def test_parses_valid_offers():
    parsed=extract_offers(make_pdf(),date(2026,11,2))
    matches=[o for o in parsed['offers'] if o['key']=='falukorv']
    assert matches and matches[0]['price']==19.90,parsed
    assert matches[0]['valid_until']=='2026-11-08'


def test_does_not_parse_without_price_unit():
    pdf=fitz.open(); p=pdf.new_page();p.insert_text((60,50),'2/11 t o m 8/11 2026');p.insert_text((75,112),'19 90');p.insert_text((75,146),'FALUKORV')
    out=extract_offers(pdf.tobytes(),date(2026,11,2))
    assert not out['offers']

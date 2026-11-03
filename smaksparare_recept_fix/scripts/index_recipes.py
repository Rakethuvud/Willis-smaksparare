#!/usr/bin/env python3
"""Löpande, robots-medveten indexering av receptsidor från ICA, Arla och Köket.se.

Bygger bara ett sökindex: titel, original-URL, ingrediensrader och schemametadata.
Kopierar inte tillagningsinstruktioner. Påstår aldrig att alla källrecept täcks.

Kräver uttrycklig granskning av respektive webbplats villkor innan publik drift.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit, urldefrag
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / 'data' / 'recipe-index.json'
STATE_PATH = ROOT / 'data' / 'recipe-index-state.json'
USER_AGENT = 'SmaksparareRecipeIndexer/1.0 (respectful-research; +https://github.com/)'
SOURCE_HOSTS = {'ICA': 'www.ica.se', 'Arla': 'www.arla.se', 'Köket.se': 'www.koket.se'}
DEFAULT_SITEMAPS = {name: [f'https://{host}/sitemap.xml', f'https://{host}/sitemap_index.xml'] for name, host in SOURCE_HOSTS.items()}
RECIPE_TYPES = {'recipe'}
# Dessa är bara prioriterade WEBBADRESSER: receptdata hämtas alltid live från källan.
# De behövs för att första körningen ska ge tydliga diagnostiska exempel.
PRIORITY_URLS = {
    'ICA': [
        'https://www.ica.se/recept/korvstroganoff-716092/',
        'https://www.ica.se/recept/kyckling-piccata-med-citron-och-kapris-751145/',
    ],
    'Arla': [
        'https://www.arla.se/recept/korv-stroganoff/',
        'https://www.arla.se/recept/kramig-korv-stroganoff/',
    ],
    'Köket.se': [
        'https://www.koket.se/kramig-salsiccia-och-saffranspasta',
        'https://www.koket.se/sara_begner/soppor_och_grytor/korv_och_chark/korv_stroganoff',
    ],
}
LIMIT_SITEMAPS = 400
MAX_SITEMAP_BYTES = 10 * 1024 * 1024
MAX_PAGES = 200
TIMEOUT = 18


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def canonical_url(url: str, source: str) -> str | None:
    try:
        url = urldefrag(str(url).strip())[0]
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        expected = SOURCE_HOSTS[source]
        if parsed.scheme != 'https' or host not in {expected, expected.removeprefix('www.')}:
            return None
        path = re.sub(r'/+', '/', parsed.path)
        if len(path) < 2 or len(url) > 650:
            return None
        return f'https://{expected}{path.rstrip("/")}/'
    except Exception:
        return None


def candidate_url(url: str, source: str) -> bool:
    p = urlsplit(url).path.lower()
    if source == 'ICA':
        return bool(re.fullmatch(r'/recept/[a-z0-9\-]+-\d+/', p))
    if source == 'Arla':
        return bool(re.fullmatch(r'/recept/[a-z0-9\-]+/', p)) and not any(s in p for s in ['/samling/', '/inspiration/', '/arla-mat/'])
    if source == 'Köket.se':
        # Köket använder även URL:er på toppnivån. Ett Recipe-objekt krävs innan publicering.
        # Köket har både korta receptlänkar och äldre adresser i flera nivåer.
        # Faktisk publicering kräver alltid ett verifierat Schema.org Recipe-objekt.
        segments = [part for part in p.split('/') if part]
        return 1 <= len(segments) <= 7 and not any(x in p for x in (
            '/artiklar/', '/program/', '/kockar/', '/inspiration/', '/om-', '/mat-', '/tips/', '/video/'))
    return False


def jsonld_recipe_nodes(html: str) -> list[dict]:
    soup = BeautifulSoup(html, 'html.parser')
    found = []
    def walk(v):
        if isinstance(v, list):
            for item in v: walk(item)
        elif isinstance(v, dict):
            types = v.get('@type') or []
            if isinstance(types, str): types = [types]
            if any(str(t).split('/')[-1].casefold() in RECIPE_TYPES for t in types): found.append(v)
            for key in ('@graph', 'mainEntity', 'itemListElement'):
                if key in v: walk(v[key])
    for node in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        try: walk(json.loads(node.string or node.get_text()))
        except (json.JSONDecodeError, ValueError, TypeError): continue
    return found


def duration_minutes(value) -> int | None:
    if not isinstance(value, str): return None
    value = value.upper().strip()
    match = re.fullmatch(r'P(?:\d+D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', value)
    if not match: return None
    hours, mins, seconds = match.groups()
    amount = int(hours or 0) * 60 + int(mins or 0) + (1 if int(seconds or 0) >= 30 else 0)
    return amount if 0 < amount < 24 * 60 else None


def get_str(v) -> str:
    if isinstance(v, str): return unescape(re.sub(r'\s+', ' ', v)).strip()
    if isinstance(v, list): return get_str(next((x for x in v if isinstance(x, str)), ''))
    if isinstance(v, dict): return get_str(v.get('name') or v.get('url') or '')
    return ''


def parse_servings(v) -> int | None:
    raw = get_str(v)
    match = re.search(r'\b(\d{1,2})\b', raw)
    if match and 0 < int(match[1]) <= 50: return int(match[1])
    if isinstance(v, (int, float)) and 0 < v <= 50: return int(v)
    return None


def recipe_from_html(html: str, url: str, source: str) -> dict | None:
    nodes = jsonld_recipe_nodes(html)
    for r in nodes:
        title = get_str(r.get('name'))
        ing = r.get('recipeIngredient') or []
        if not isinstance(ing, list): ing = [ing]
        ingredients = [get_str(x)[:170] for x in ing if get_str(x)]
        if not title or len(title) > 160 or not ingredients: continue
        mins = duration_minutes(r.get('totalTime'))
        if mins is None:
            prep, cook = duration_minutes(r.get('prepTime')), duration_minutes(r.get('cookTime'))
            if prep or cook: mins = (prep or 0) + (cook or 0)
        image = r.get('image')
        if isinstance(image, list): image = image[0] if image else None
        if isinstance(image, dict): image = image.get('url') or image.get('contentUrl')
        image = image if isinstance(image, str) and image.startswith('https://') else None
        rating = r.get('aggregateRating') if isinstance(r.get('aggregateRating'), dict) else {}
        try:
            rv = float(rating.get('ratingValue'))
            if not (1 <= rv <= 5): rv = None
        except (TypeError, ValueError): rv = None
        rec = {
            'id': hashlib.sha256(url.encode()).hexdigest()[:20], 'schema': 'Recipe',
            'source': source, 'url': url, 'title': title, 'description': get_str(r.get('description'))[:175],
            'minutes': mins, 'servings': parse_servings(r.get('recipeYield')), 'ingredients': ingredients,
            'image': image, 'rating': rv, 'categories': [get_str(r.get('recipeCategory'))] if r.get('recipeCategory') else [],
            'keywords': get_str(r.get('keywords'))[:175]
        }
        return rec
    return None


def parse_sitemap(payload: bytes):
    if len(payload) > MAX_SITEMAP_BYTES: raise ValueError('sitemap exceeds size limit')
    if payload.startswith(b'\x1f\x8b'): payload = gzip.decompress(payload)
    if len(payload) > MAX_SITEMAP_BYTES: raise ValueError('uncompressed sitemap exceeds size limit')
    root = ElementTree.fromstring(payload)
    category = root.tag.split('}')[-1]
    if category not in ('sitemapindex', 'urlset'): raise ValueError('not a recognized sitemap')
    urls = [e.text.strip() for e in root.iter() if e.tag.split('}')[-1] == 'loc' and e.text]
    return category, urls


class RobotsPolicy:
    def __init__(self, session: requests.Session, host: str):
        self.host = host
        r = session.get(f'https://{host}/robots.txt', timeout=TIMEOUT)
        r.raise_for_status()
        if not r.text.strip() or '<html' in r.text[:300].lower():
            raise ValueError('invalid robots.txt; indexer refuses to guess permissions')
        self.text = r.text
        self.rules = RobotFileParser(); self.rules.parse(r.text.splitlines())
        self.sitemaps = [line.split(':', 1)[1].strip() for line in r.text.splitlines()
                         if line.lower().startswith('sitemap:')]
    def permits(self, url: str):
        return self.rules.can_fetch(USER_AGENT, url)


def get_bytes(session, url, timeout=TIMEOUT):
    r = session.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    if len(r.content) > MAX_SITEMAP_BYTES: raise ValueError('oversized response')
    return r.content


def discover_sitemaps(session: requests.Session, source: str, policy: RobotsPolicy,
                      budget=LIMIT_SITEMAPS, nap=0.5) -> set[str]:
    host = SOURCE_HOSTS[source]
    queue = list(dict.fromkeys(policy.sitemaps or DEFAULT_SITEMAPS[source]))
    seen = set(); collected = set()
    while queue and len(seen) < budget:
        url = queue.pop(0)
        p = urlsplit(url)
        if url in seen or p.scheme != 'https' or not (p.hostname == host or p.hostname == host.removeprefix('www.')): continue
        seen.add(url)
        if not policy.permits(url): continue
        try:
            kind, entries = parse_sitemap(get_bytes(session, url))
        except Exception as ex:
            print(f'[{source}] sitemap skipped {url}: {ex}', file=sys.stderr)
            continue
        if kind == 'sitemapindex':
            queue.extend(x for x in entries if x not in seen)
        else:
            for x in entries:
                canonical = canonical_url(x, source)
                if canonical and candidate_url(canonical, source): collected.add(canonical)
        if nap: time.sleep(nap)
    return collected


def load_json(path: Path, default):
    if not path.exists(): return default
    try: return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError): return default


def atomic_json(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def run(limit: int, pause: float, rediscover: bool, dry_run=False):
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xml,text/xml;q=0.9,*/*;q=0.5'})
    loaded = load_json(INDEX_PATH, {'schema': 1, 'recipes': []})
    items = {rec['url']: rec for rec in loaded.get('recipes', []) if rec.get('source') in SOURCE_HOSTS and rec.get('url')}
    state = load_json(STATE_PATH, {'pending': {}, 'visited': [], 'discovered_at': None})
    pending = state.get('pending') or {}; visited = set(state.get('visited') or [])
    policies = {}; errors = {}
    for source, host in SOURCE_HOSTS.items():
        try:
            policies[source] = RobotsPolicy(session, host)
        except Exception as ex:
            errors[source] = f'robots unavailable: {ex}'
            print(f'[{source}] {errors[source]} – skipping source',file=sys.stderr)
    if not policies:
        raise RuntimeError('All sources are unreachable or have unreadable robots.txt; no changes published')
    # Receptadresserna är endast startpunkter, aldrig ett förinstallerat bibliotek.
    # All information måste läsas in och valideras från originalwebbsidan.
    for source, policy in policies.items():
        live_urls = [canonical_url(u, source) for u in PRIORITY_URLS[source]]
        first = [u for u in live_urls if u and candidate_url(u, source)
                 and u not in visited and u not in items and policy.permits(u)]
        pending[source] = list(dict.fromkeys(first + list(pending.get(source) or [])))
    if rediscover or not any(pending.values()):
        for source, policy in policies.items():
            found = discover_sitemaps(session, source, policy, nap=pause)
            old = set(pending.get(source) or [])
            unseen = sorted(found - visited - set(items))
            # Match current leaflet ingredient names first, but do not discard the rest.
            keywords = ('falukorv','flaskytter','fläskytter','ost','apple','äpple','kyckling','lax')
            unseen.sort(key=lambda u: (not any(k in u.lower() for k in keywords),u))
            pending[source] = list(dict.fromkeys(list(old) + unseen))
            print(f'[{source}] discovered {len(found)} candidate recipe URLs; queued {len(pending[source])}')
        state['discovered_at']=utc_now()
    processed = 0; added = 0
    failures = state.get('failures') or {}
    per_source=Counter()
    # Fair shares: cycle through all accessible sources, no local fixed recipe set.
    while processed < limit:
        progress=False
        for source, policy in policies.items():
            if processed >= limit: break
            q = pending.setdefault(source, [])
            if not q: continue
            url = q.pop(0); progress=True
            if url in visited or url in items: continue
            if not policy.permits(url):
                print(f'[{source}] robots forbids {url}',file=sys.stderr); visited.add(url); continue
            try:
                resp = session.get(url,timeout=TIMEOUT,allow_redirects=True)
                resp.raise_for_status()
                if resp.url.split('?')[0].rstrip('/') != url.rstrip('/'):
                    if not canonical_url(resp.url,source): raise ValueError('redirected outside source host')
                if 'text/html' not in resp.headers.get('Content-Type','text/html'):
                    raise ValueError('non-html response')
                rec=recipe_from_html(resp.text,url,source)
                if rec:
                    items[url]=rec;added+=1
            except Exception as ex:
                print(f'[{source}] cannot read {url}: {ex}',file=sys.stderr)
                failures[url] = int(failures.get(url, 0)) + 1
                if failures[url] < 3: q.append(url)
                else: visited.add(url)
            else:
                visited.add(url); failures.pop(url, None)
            processed+=1;per_source[source]+=1
            if pause: time.sleep(pause)
        if not progress: break
    output = {'schema':1,'generated_at':utc_now(),'status':'partial_index','indexed_count':len(items),
              'pending_count':sum(len(x) for x in pending.values()),
              'sources': list(SOURCE_HOSTS), 'source_counts':dict(Counter(r['source'] for r in items.values())),
              'source_errors':errors, 'recipes':sorted(items.values(),key=lambda r:(r['source'],r['title'].casefold()))}
    state.update({'pending':pending,'visited':sorted(visited),'failures':failures})
    print(f'Processed {processed}; new recipes {added}; indexed {len(items)}; pending {output["pending_count"]}')
    if not dry_run:
        atomic_json(INDEX_PATH, output)
        atomic_json(STATE_PATH, state)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-pages',type=int,default=MAX_PAGES)
    parser.add_argument('--delay',type=float,default=0.6)
    parser.add_argument('--rediscover',action='store_true')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    if not 1<=args.max_pages<=10000 or not 0<=args.delay<=10: parser.error('invalid limits')
    run(args.max_pages,args.delay,args.rediscover,args.dry_run)

if __name__=='__main__':main()

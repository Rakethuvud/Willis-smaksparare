import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from index_recipes import canonical_url,candidate_url,jsonld_recipe_nodes,recipe_from_html,parse_sitemap,duration_minutes,PRIORITY_URLS

def test_recipe_jsonld_graph():
    html='''<html><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebPage","name":"Test"},{"@type":["CreativeWork","Recipe"],"name":"Korvgryta med ris","recipeIngredient":["550 g falukorv","2 dl grädde"],"recipeYield":"4 portioner","totalTime":"PT35M","aggregateRating":{"ratingValue":"4.5"}}]}</script></html>'''
    result=recipe_from_html(html,'https://www.ica.se/recept/test-1234/','ICA')
    assert result['title']=='Korvgryta med ris'
    assert result['minutes']==35
    assert result['servings']==4
    assert result['ingredients']==['550 g falukorv','2 dl grädde']
    assert result['rating']==4.5


def test_not_recipe():
    assert recipe_from_html('<h1>Falsk sida</h1>', 'https://www.koket.se/x/', 'Köket.se') is None


def test_sitemap_and_host_filter():
    kind,urls=parse_sitemap(b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.ica.se/recept/test-1234/</loc></url></urlset>')
    assert kind=='urlset' and len(urls)==1
    assert canonical_url('https://www.ica.se/recept/test-1234/#ok','ICA')==urls[0]
    assert canonical_url('https://evil.com/recept/test-1234/','ICA') is None
    assert candidate_url(urls[0],'ICA')
    assert not candidate_url('https://www.ica.se/recept/middag/','ICA')


def test_duration():
    assert duration_minutes('PT1H25M')==85
    assert duration_minutes('PT15M')==15
    assert duration_minutes('nonsense') is None


def test_deep_koket_urls_and_verified_priority_hosts():
    assert candidate_url('https://www.koket.se/sara_begner/soppor_och_grytor/korv_och_chark/korv_stroganoff/', 'Köket.se')
    assert candidate_url('https://www.koket.se/kramig-salsiccia-och-saffranspasta/', 'Köket.se')
    for source, urls in PRIORITY_URLS.items():
        for url in urls:
            canon=canonical_url(url, source)
            assert canon and candidate_url(canon, source)

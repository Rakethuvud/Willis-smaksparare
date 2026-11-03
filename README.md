# Willys Smaksparare · Lund Norra Fäladen (2176)

## Version 5 – en enda dynamisk receptmeny

De **29 tidigare inbyggda recepten är helt borttagna**. Webbappen innehåller nu bara en gemensam receptmeny som hämtas från `data/recipe-index.json`. De poster som visas måste komma från identifierade `Recipe`-metadata på **ICA, Arla eller Köket.se** och innehåller alltid länkar till originalrecepten. Receptinformation används för sökning och planering, medan själva tillagningsstegen finns på originalwebbplatserna.

**Viktigt:** Det levererade `recipe-index.json` är avsiktligt tomt (`0 recept`), eftersom den automatiska genomsökningen inte har körts mot originalwebbplatserna från utvecklingsmiljön. Appen visar alltså **inte** i dagsläget hela källornas utbud. Indexet börjar växa när du publicerar projektet och kör `recipes.yml`. Att en URL listas i en sitemap betyder inte att vi har läst in och analyserat receptet. Kräver de externa webbplatserna inloggning, förbjuder de automatiserad åtkomst eller saknar de `Recipe`-data stannar den delen av indexeringen upp. Inga recept hittas på eller läggs till som ersättning.

### Så fungerar receptmenyn

- En enda samlad grid – inga lokala eller separata online-arkiv.
- Källfilter **Alla / ICA / Arla / Köket.se**, sökning på rätt och ingrediens, filtrering på kampanj, vegetariskt, snabbt och fullständig prissättning.
- Standardsortering **Bästa kampanjmatchning** som väger in antal aktiva kampanjmatchningar, värdet på ingredienser som ingår i aktuella erbjudanden och *faktiskt beräknad* besparing när jämförelsepris är känt.
- Om andra varors priser saknas anges **Pris saknas**, aldrig en uppdiktad totalprisuppgift. Kampanjmatchning är alltså **inte samma sak som lägsta totalkostnad**.
- Receptkort, portionsanpassning, receptmodal med ingredienser, skafferi, inköpslista och källlänk fungerar mot samma index.
- Ingrediensrader som inte kan få ett säkert mängd-/enhetsvärde behålls som **Kontrollera mängd** och ger inget falskt pris.

### Kom igång lokalt

```bash
python -m pip install -r requirements.txt
python -m pytest -q tests
python -m http.server 8000
```

Öppna `http://localhost:8000`. I början finns inga recept indexerade, vilket är förväntat. Den fristående HTML-kopian har samma gränssnitt men kan **inte** löpande fylla på index eller uppdatera reklamblad själv. Kör den på HTTP-server med projektmappen för att läsa `data/*.json`.

### Receptindexering

1. Granska **användningsvillkor, robots.txt, bilder och återpubliceringsrätt** för ICA, Arla och Köket.se innan ett större automatiserat index tas i drift. Att webbplatser har publika recept innebär inte att obegränsad masshämtning eller vidarespridning är tillåten.
2. Indexeraren hämtar sitemap-adresser via respektive `robots.txt` (med kontrollerade reservadresser), följer URL:erna och använder bara strukturerade JSON-LD-objekt av typen `Recipe` som har ingredienser och titel. Inga instruktionstexter samlas in.
3. Indexering sker inkrementellt, med begränsad hastighet och kontroller av åtkomstregler. Tillstånd och ej genomgångna adresser sparas i `data/recipe-index-state.json`; själva menydata finns i `data/recipe-index.json`.
4. Du kan manuellt starta indexeringen med `python scripts/index_recipes.py --max-pages 250 --delay 0.65`. Lägg till `--rediscover` vid ny kataloggenomsökning för att upptäcka nyligen publicerade recept.
5. Om en källa saknar läsbar `robots.txt` hämtas inte den källan. Om **alla** källor saknar tillåten åtkomst misslyckas körningen i stället för att hitta på data.
6. Produktionsindex kräver fortsatt övervakning och tester mot webbplatsernas verkliga HTML-format, som kan ändras över tid.

### Publicera och uppdatera automatiskt på GitHub

1. Packa upp projektet och lägg allt i ett GitHub-repository med `main` som standardgren.
2. Under **Settings → Pages → Build and deployment**, välj **GitHub Actions** som publiceringsmetod.
3. Under **Settings → Actions → General → Workflow permissions**, tillåt *Read and write permissions* för att uppdateringsjobben ska kunna committa nya JSON-filer.
4. Kör först **Indexera originalrecept från ICA, Arla och Köket.se** manuellt under Actions. Kontrollera körloggen och att JSON-filen innehåller riktiga recept.
5. Receptindexeringen är schemalagd varje dag **03:15 UTC** med begränsat antal sidförfrågningar. Den publicerar uppdaterat index om den lyckas.
6. Kampanjuppdateraren `update.yml` är från föregående version och kontrollerar Willys officiella reklamblad för butik 2176 varje dag. PDF-vägen är fortfarande **inte verifierad mot Willys server i produktionsdrift**. Om hämtningen inte fungerar behålls tidigare fil, och utgångna kampanjer aktiveras inte.
7. Webbappen laddar `data/recipe-index.json` när sidan öppnas eller man trycker **Uppdatera receptlistan**. Kampanjfilen läses vid öppning och med jämna mellanrum; erbjudandena filtreras på datum och medlemsvillkor.

### Varför är inte alla ICA-, Arla- och Köket.se-recept redan med?

Källorna omfattar tiotusentals recept och tillhandahåller inte här en verifierad offentlig, obegränsad mass-API-åtkomst. En server måste upptäcka URL:er, läsa de tillåtna sidorna och kontrollera ingrediensfält innan recepten kan användas för kampanjmatchning. Därför visar appen exakt hur många verkliga recept som **faktiskt har indexerats** samt hur många URL:er som väntar. Vi påstår inte att 100 procent täcks förrän det kan styrkas.

### Tekniska filer

- `index.html` – webbappen med en enda receptmeny
- `data/recipe-index.json` – dynamisk samlad meny (tom före första indexeringen)
- `scripts/index_recipes.py` – inkrementell indexerare som respekterar robots.txt
- `data/recipe-index-state.json` – skapas vid lyckad indexering och spårar kö/framsteg
- `.github/workflows/recipes.yml` – schemalagd receptindexering och publicering
- `data/campaigns.json` – annonserade Willys-erbjudanden med datum och villkor
- `scripts/update_campaigns.py` och `.github/workflows/update.yml` – befintlig Willys-reklambladhämtare (kräver liveverifiering)
- `tests/` – tester av indexeringslogik och kampanjregler

Appen är oberoende och inte officiellt ansluten till Willys, ICA, Arla eller Köket.se.

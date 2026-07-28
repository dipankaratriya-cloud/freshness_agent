# Refresh Date Extraction — Structural Blockers

> **Run date:** 2026-06-19  
> **Pipeline:** `provenance_refresh_extractor.py` (5-tier: HTTP HEAD → HTML parse → Gemini NLP → Playwright → Groq browser)  
> **Total datasets:** 686 | **Found:** 428 (62%) | **Blocked:** 258 (38%)

All 258 failures exhausted all 5 tiers (`tiers_attempted = 1,2,3,4,5`) before failing. These are not retry candidates — each represents a **structural reason** the current pipeline cannot extract a date regardless of how many times it runs.

---

## Summary Table

| # | Domain / Source | Datasets Blocked | Root Cause Category |
|---|---|---|---|
| 1 | `data.census.gov` | 37 | JavaScript SPA — no static HTML |
| 2 | `www.wikidata.org` | 36 | Wrong URL type — all point to homepage |
| 3 | `www.nccs.nasa.gov` | 28 | Auth-gated portal + all 28 share one URL |
| 4 | `datacommons.org` | 22 | Self-referential — no external source |
| 5 | `data.humdata.org` | 15 | Mix of homepage URLs + JS-rendered pages |
| 6 | `nces.ed.gov` | 9 | Interactive POST form — not a static page |
| 7 | `ec.europa.eu/eurostat` | 7 | Generic DB browser URL — not dataset-specific |
| 8 | `rbi.org.in` | 7 | Homepage URL — no machine-readable metadata |
| 9 | `www.eia.gov` | 7 | Deprecated API (dead links since 2023) |
| 10 | `www.rff.org` | 7 | Tool index page — no individual dataset dates |
| 11 | `data.cdc.gov` | 4 | Category browse pages — not specific datasets |
| 12 | `wonder.cdc.gov` | 4 | Interactive query tool — no static metadata |
| 13 | `ndap.niti.gov.in` | 4 | JavaScript-rendered portal (React SPA) |
| 14 | `kosis.kr` | 4 | Korean-language JS portal + non-English dates |
| 15 | `data-explorer.oecd.org` | 3 | React SPA with encoded state in URL |
| 16 | `gaftp.epa.gov` | 3 | FTP server — date in directory listing `<pre>`, not parsed |
| 17 | `www.fbi.gov` | 2 | HTTP URL redirects to HTTPS; date found by Tier 5 for other FBI URLs but these use old HTTP scheme |
| 18 | `github.com` | 2 | Tree pages — `Last-Modified` = page render time, not commit date |
| 19 | `api.climatetrace.org` | 2 | JSON API root — Tier 2 skips non-HTML responses |
| 20 | Remaining (long tail) | 60 | Various: broken URLs, homepages, auth walls, exotic portals |
| | **Total** | **258** | |

---

## Detailed Blocker Analysis

---

### 1. US Census Bureau — `data.census.gov` (37 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: JavaScript single-page application (SPA)**

The Census Bureau's data explorer is a fully client-side React application. Every table URL — whether a modern path like `data.census.gov/table/ACSST5Y2022.S0804` or a legacy cedsci path like `data.census.gov/cedsci/table?q=S1901&tid=ACSST5Y2023.S1901` — returns a blank HTML shell on initial load. All table metadata, column headers, and any "last updated" information is injected by JavaScript after the React bundle executes.

- **Tier 1 (HTTP HEAD):** Returns no `Last-Modified` header — CDN serves a generic shell.
- **Tier 2 (HTML parse):** Receives the blank shell; no JSON-LD, no OpenGraph, no Dublin Core, no visible date text.
- **Tier 3 (Gemini NLP on page text):** Page text is just navigation boilerplate — no data content.
- **Tier 4 (Playwright):** Playwright loads the shell and waits 2 seconds, but React initialization requires authenticated API calls to Census backend services that return data — these calls fail silently in a headless context.
- **Tier 5 (Groq browser):** Groq's compound-beta browser also fails to extract a meaningful date because the table content requires Census API authentication to populate.

**Additional problem — URL deduplication loss:** Many datasets share identical URLs. For example, `dc/base/CensusACS5YearSurvey_SubjectTables_S1251` and `dc/base/CensusACS5YearSurvey_SubjectTables_S1251_StatVarAgg` both point to the same URL. Even if scraping succeeded once, the result would be incorrectly shared.

**Note:** Two Census URLs (`data.census.gov/cedsci/table?q=S0504&tid=ACSST5Y2023.S0504` and `data.census.gov/table?q=S0801&tid=ACSST5Y2023.S0801`) did return a year via Tier 5 (`2023`). These are the exception — Groq managed to extract the vintage year from visible page text for those specific tables.

**Affected datasets (sample):**
```
dc/base/CensusSuperTMCFTable_ECNBASIC2012_EC1200A1     → data.census.gov/api/access/table/download
dc/base/CensusACS5YearSurvey_SubjectTables_S0804_StatVarAgg → data.census.gov/table/ACSST5Y2022.S0804
dc/base/CensusACS5YearSurvey_SubjectTables_S1901       → data.census.gov/cedsci/table?q=S1901&tid=ACSST5Y2023.S1901
dc/base/CensusACS5YearSurvey_SubjectTables_S2602PR     → data.census.gov/cedsci/table?q=S2602PR&tid=ACSST5Y2023.S2602PR
dc/base/CensusACSBtable5YearSurvey_Btable_B25031       → data.census.gov/table/ACSDT5Y2022.B25031
... (37 total)
```

**Fix path:** The vintage year is embedded directly in the `tid` URL parameter (e.g., `ACSST5Y2023` → year `2023`). A regex extraction on the URL itself — before making any HTTP request — can recover the data vintage year for most of these without scraping. For the `data.census.gov/api/access/table/download` URL (which has no `tid`), use the Census Discovery API: `https://api.census.gov/data.json`.

---

### 2. Wikidata — `www.wikidata.org` (36 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Wrong URL type — all 36 datasets point to the Wikidata homepage**

Every single one of the 36 Wikidata datasets uses `https://www.wikidata.org/` or `https://www.wikidata.org/wiki/Wikidata:Main_Page` as the provenance URL. This is the Wikidata portal homepage — a generic landing page that carries no information about any specific dataset or SPARQL-derived collection.

- **Tier 1:** `Last-Modified` header on the homepage reflects when the homepage HTML was last rendered by Wikidata's CDN, not any data update.
- **Tier 2–5:** All date signals found on the homepage are editorial content (news items, announcements) — not data refresh dates for the 36 geographic entity collections these datasets represent.

The datasets themselves (e.g., `WikidataGeo_Israel`, `WikidataGeo_Jordan`, `WikidataOtherIdGeos`) are derived from SPARQL queries against specific Wikidata entity classes. The relevant "refresh date" would be the date of the most recent edit to those entity classes on Wikidata — not anything on the homepage.

One entry, `dc/base/WikidataGeo_Macedonia`, points to a broken Wikipedia URL (`en.wikipedia.org/wiki/Governorates_of_Macedonian/`) — trailing slash causes a 404.

**Affected datasets (all 36):**
```
dc/base/WikidataGeo_Israel           → www.wikidata.org/
dc/base/WikidataGeo_Jordan           → www.wikidata.org/
dc/base/WikidataOtherIdGeos          → www.wikidata.org/
dc/base/WikidataGeo_Croatia          → www.wikidata.org/
dc/base/WikidataGeo_OCHA_Ireland     → www.wikidata.org/
dc/base/WikidataGeo_Macedonia        → en.wikipedia.org/wiki/Governorates_of_Macedonian/ (404)
... (36 total, 35 pointing to wikidata.org homepage)
```

**Fix path:** The Wikidata SPARQL endpoint (`https://query.wikidata.org/`) returns a `Last-Modified` HTTP header with the timestamp of the most recent update to the knowledge graph. A HEAD request to `query.wikidata.org` would give a meaningful refresh signal. For the Macedonia entry, the URL itself needs to be corrected (remove trailing slash or update to the correct Wikipedia article URL).

---

### 3. NASA NCCS — `www.nccs.nasa.gov` (28 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Authentication-gated portal + all 28 share two URLs**

All 28 NASA datasets point to one of two URLs:
- `https://www.nccs.nasa.gov/services/data-collections/land-based-products/nex-gddp` (25 datasets)
- `https://www.nccs.nasa.gov/services/data-collections/land-based-products/nex-dcp30` (3 datasets)

Both are landing pages for the NASA Center for Climate Simulation's NEX (NASA Earth Exchange) data products. Accessing the actual data requires navigating through NASA's Earthdata Login authentication system. The public landing pages show general product descriptions but contain no machine-readable `Last-Modified` metadata and no visible "last updated" date in the HTML.

- **Tier 1:** No `Last-Modified` header on the public landing pages.
- **Tier 2:** HTML contains project descriptions, not data update timestamps.
- **Tier 3 (Gemini):** Page text has no date signal — the Gemini model returns null.
- **Tier 4 (Playwright):** The page fully renders but still shows no update date — the content is static marketing text, not a data portal dashboard.
- **Tier 5 (Groq):** Groq's browser visits the page and also finds no date to extract.

**Affected datasets (sample):**
```
dc/base/IPCCPlaces                   → nccs.nasa.gov/.../nex-gddp
dc/base/IPCCPlaces_GeoCoordinates    → nccs.nasa.gov/.../nex-gddp
dc/base/NASA_NEXGDDP_Country         → nccs.nasa.gov/.../nex-gddp
dc/base/NASA_NEXGDDP_Subnational     → nccs.nasa.gov/.../nex-gddp
dc/base/NASA_NEXDCP30_AggrYearsStats → nccs.nasa.gov/.../nex-dcp30
... (28 total)
```

**Fix path:** NASA's Earthdata Common Metadata Repository (CMR) provides structured dataset metadata without authentication:
- `https://cmr.earthdata.nasa.gov/search/collections.json?short_name=NEX-GDDP`
- `https://cmr.earthdata.nasa.gov/search/collections.json?short_name=NEX-DCP30`

The CMR response includes `time_end`, `updated`, and `revision_date` fields giving the data collection's last update timestamp.

---

### 4. Data Commons Internal Datasets — `datacommons.org` (22 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Self-referential source — no external refresh date exists**

22 datasets use `https://datacommons.org` or `https://www.datacommons.org` as their provenance URL. These are **internal Data Commons datasets** — auto-generated statistical variables, base knowledge graph schemas, ontology definitions, and internal coverage statistics. They do not have an external source that publishes update dates.

The `datacommons.org` homepage is a product marketing page. No tier can extract a meaningful data refresh date from it because there is no such date to extract — the "source" for these datasets is the DC pipeline itself.

**Affected datasets (all 22):**
```
dc/base/AutoGeneratedStatVars        → datacommons.org
dc/base/BaseSchema                   → datacommons.org
dc/base/Climate_StatVarCalculation   → datacommons.org
dc/base/DataCommons_CoverageStats    → datacommons.org
dc/base/DotBayesNFPreds              → datacommons.org
dc/base/ExperimentalStatVars         → datacommons.org
dc/base/GenderIncomeInequality       → datacommons.org
dc/base/HumanReadableStatVars        → datacommons.org
dc/base/NearbyGeos                   → datacommons.org
... (22 total)
```

**Fix path:** These datasets should be flagged `programmatically_trackable = false` in the freshness model. Their staleness is determined by the DC import pipeline's own run history — not by scraping an external URL. Querying the DC import pipeline's internal run timestamps is the correct signal for these.

---

### 5. HUMDATA (HDX) — `data.humdata.org` (15 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Mix of homepage URLs (no dataset-specific path) and JavaScript-rendered pages**

The 15 blocked HUMDATA datasets split into two sub-problems:

**Sub-problem A — Homepage URLs (8 datasets):** `dc/base/OCHAGeoCoordinates`, `dc/base/OCHAGeoCoordinates_Simplified`, `dc/base/OCHAGeoCoordinatesExtended`, and others use `https://data.humdata.org` (bare homepage) as the provenance URL. The HDX homepage lists recently updated datasets but carries no date specific to any individual dataset.

**Sub-problem B — Specific dataset pages that are JS-rendered (7 datasets):** URLs like `data.humdata.org/dataset/whosonfirst-data-admin-hkg` and `data.humdata.org/dataset/whosonfirst-data-admin-svn` point to specific dataset pages. The HDX portal is built on CKAN but renders its dataset pages as a JavaScript application. The `Last Modified` date visible in a browser is injected by JS — the static HTML returned by Tier 2 does not contain it. Playwright (Tier 4) and Groq (Tier 5) also failed, likely due to bot-detection or rendering delays on these pages.

**Affected datasets (sample):**
```
dc/base/OCHAGeoCoordinates           → data.humdata.org  (homepage)
dc/base/OCHAGeoCoordinates_Simplified → data.humdata.org (homepage)
dc/base/OCHAGeoCoordinates_HongKong  → data.humdata.org/dataset/whosonfirst-data-admin-hkg
dc/base/OCHAGeoCoordinates_Slovenia  → data.humdata.org/dataset/whosonfirst-data-admin-svn
dc/base/OCHAGeoCoordinates_DRC       → data.humdata.org/dataset/whosonfirst-data-admin-cod
... (15 total)
```

**Fix path:** HDX provides a public CKAN-compatible REST API. For datasets with a specific slug in the URL (e.g., `whosonfirst-data-admin-hkg`):
```
GET https://data.humdata.org/api/3/action/package_show?id=whosonfirst-data-admin-hkg
```
Response includes `metadata_modified` and per-resource `last_modified` fields. For the homepage-URL datasets, the correct slug must first be mapped from the dataset ID — these will require manual URL correction in the provenance configuration.

---

### 6. NCES (National Center for Education Statistics) — `nces.ed.gov` (9 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Interactive POST-form tool, not a static data page**

All 9 datasets point to `https://nces.ed.gov/ccd/elsi/tableGenerator.aspx` — the ELSI (Elementary/Secondary Information System) Table Generator. This is a server-side form application: users select variables, grade levels, and year ranges, then submit a POST request to generate a custom table. The GET response to this URL is a generic tool-selection page with no dataset-specific metadata.

Two additional datasets point to `https://nces.ed.gov/programs/edge/Demographic/ACS` and `https://nces.ed.gov/ipeds/datacenter/DataFiles.aspx` — also forms / data-file browsers with no machine-readable update dates in their static HTML.

**Affected datasets:**
```
dc/base/NCES_SchoolDistrictStats     → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_PublicSchoolStats       → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_SchoolDistrict          → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_PublicSchool            → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_PrivateSchoolStats      → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_PrivateSchool           → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_UniversityStats         → nces.ed.gov/ccd/elsi/tableGenerator.aspx
dc/base/NCES_EdgeDemographics        → nces.ed.gov/programs/edge/Demographic/ACS
dc/base/NCES_IPEDSDataFile           → nces.ed.gov/ipeds/datacenter/DataFiles.aspx
```

**Fix path:** The Urban Institute's Education Data API wraps NCES CCD data with proper REST endpoints and update timestamps:
```
GET https://educationdata.urban.org/api/v1/schools/ccd/schools/?year=2023
```
Response includes `updated_at` in the metadata header. Alternatively, the NCES Data Lab API (`https://nces.ed.gov/datalab/api/`) exposes structured dataset metadata.

---

### 7. Eurostat Generic Database URL — `ec.europa.eu/eurostat` (7 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Generic database browser URL — not pointing to a specific dataset**

All 7 blocked Eurostat datasets use the same generic URL:
```
https://ec.europa.eu/eurostat/web/main/data/database
```
This is the Eurostat database browser — a navigation page that lists all available Eurostat datasets. Any date extracted from this page reflects when the browser UI was last updated, not when any specific dataset was refreshed.

Note: Other Eurostat datasets in the corpus **did** succeed because they used specific dataset URLs (e.g., `ec.europa.eu/eurostat/databrowser/view/trng_lfse_04/` which returned a date via Tier 5). The 7 failures all share the same non-specific root URL.

**Affected datasets:**
```
dc/base/EuroStatHealth_BodyMassIndex         → ec.europa.eu/eurostat/web/main/data/database
dc/base/EuroStatHealth_SocialEnvironment     → ec.europa.eu/eurostat/web/main/data/database
dc/base/EurostatHealth_Tobacco_Consumption   → ec.europa.eu/eurostat/web/main/data/database
dc/base/EuroStatHealth_FruitsAndVegetables   → ec.europa.eu/eurostat/web/main/data/database
dc/base/EuroStatHealth_AlcoholConsumption    → ec.europa.eu/eurostat/web/main/data/database
dc/base/EuroStatHealth_PhysicalActivity      → ec.europa.eu/eurostat/web/main/data/database
dc/base/EuroStatRegionalStatistics           → ec.europa.eu/eurostat/web/main/data/database (1 additional)
```

**Fix path:** Each import needs its specific Eurostat dataset code (e.g., `hlth_ehis_bm1e` for BMI data). With the code, the Eurostat SDMX REST API returns structured metadata including `lastUpdateDate`:
```
GET https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/dataflow/ESTAT/{dataset_code}/
```
The dataset codes must be identified per import and set as `source_data_url`.

---

### 8. Reserve Bank of India — `rbi.org.in` (7 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: All 7 point to the RBI homepage — no data-specific metadata**

Every RBI dataset uses `https://rbi.org.in/` — the homepage of the Reserve Bank of India's website. The RBI homepage contains current news, press releases, and announcements. The page renders server-side (not JS-dependent), but it carries no machine-readable date metadata relevant to statistical data refreshes. The visible dates on the page are press release publication dates, not data update timestamps.

**Affected datasets:**
```
dc/base/India_RBI_State_Statistics                              → rbi.org.in/
dc/base/India_RBIStateDomesticProduct                          → rbi.org.in/
dc/base/India_RBIStateDomesticProduct_StatVarAgg               → rbi.org.in/
dc/base/India_RBIStateDomesticProduct_StatVarAgg_AggCountry    → rbi.org.in/
dc/base/India_RBIStateDomesticProduct_AggCountry               → rbi.org.in/
dc/base/India_RBIKlems                                         → rbi.org.in/
dc/base/India_RBIManufacturing                                 → rbi.org.in/
```

**Fix path:** The RBI publishes state-level statistics through its DBIE (Database on Indian Economy) portal. The DBIE API returns structured data with timestamps:
```
GET https://dbie.rbi.org.in/DBIE/dbie.rbi?site=api
```
Each `source_data_url` for these 7 imports needs to be updated from the RBI homepage to the specific DBIE API endpoint for the relevant statistical series.

---

### 9. EIA OpenData — `www.eia.gov` (7 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Deprecated API endpoint — dead links since 2023**

All 7 EIA datasets use the old EIA Open Data API browser format:
```
https://www.eia.gov/opendata/qb.php?category={id}
```
The `opendata/qb.php` endpoint was **decommissioned by the EIA in 2023** when they launched their v2 API. GET requests to these URLs return either redirect loops, error pages, or generic EIA homepage content. No tier can extract a refresh date because the target page no longer exists as a data portal.

**Affected datasets:**
```
dc/base/EIA_SEDS           → eia.gov/opendata/qb.php?category=40203
dc/base/EIA_NuclearOutages → eia.gov/opendata/qb.php?category=2889994
dc/base/EIA_International  → eia.gov/opendata/qb.php?category=2134384
dc/base/EIA_NaturalGas     → eia.gov/opendata/qb.php?category=714804
dc/base/EIA_TotalEnergy    → eia.gov/opendata/qb.php?category=711224
dc/base/EIA_Coal           → eia.gov/opendata/qb.php?category=717234
dc/base/EIA_SEDS_StatVarAgg → eia.gov/opendata/qb.php?category=40203
```

**Fix path:** EIA's v2 API (`https://api.eia.gov/v2/`) is the replacement. Each EIA dataset category maps to a route in the v2 API. The v2 response metadata includes `lastHistoricalPeriod` — the most recent period for which data is available. An EIA API key is required (free, available at `eia.gov/opendata/register.php`). All 7 `source_data_url` fields need to be remapped from the dead `qb.php` URLs to the corresponding v2 API endpoints.

---

### 10. Resources for the Future (RFF) — `www.rff.org` (7 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Category index page — lists many tools but carries no per-dataset update date**

All 7 RFF datasets use `https://www.rff.org/publications/data-tools/` as the provenance URL. This is RFF's publications and data tools index page — a paginated list of all RFF publications. It is not a page for any specific dataset. The page carries no machine-readable metadata and no "last updated" date that would be meaningful for the individual grid geo or weather variability datasets derived from RFF's work.

**Affected datasets:**
```
dc/base/RFFGridGeos                                      → rff.org/publications/data-tools/
dc/base/RFF_USGridGeo_WeatherVariabilityForecast_AggCounty → rff.org/publications/data-tools/
dc/base/RFF_USGridGeo_WeatherVariabilityForecast         → rff.org/publications/data-tools/
dc/base/RFF_USGridGeo_WeatherVariabilityHistorical       → rff.org/publications/data-tools/
dc/base/RFF_USCounty_AdditionalWeatherVariabilityHistorical → rff.org/publications/data-tools/
dc/base/RFF_USCounty_WeatherVariabilityHistorical        → rff.org/publications/data-tools/
dc/base/RFF_USCounty_WeatherVariabilityForecast          → rff.org/publications/data-tools/
```

**Fix path:** Each RFF dataset corresponds to a specific publication page (e.g., `rff.org/publications/data-tools/us-climate-vulnerability-index/`). The individual publication pages include a "Published" or "Updated" date in their HTML. The `source_data_url` for each import needs to be updated to the specific publication URL rather than the general index page.

---

### 11. CDC Data Portal Browse Pages — `data.cdc.gov` (4 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Category browse pages — not specific dataset landing pages**

3 of the 4 blocked CDC datasets use category browse URLs:
```
https://data.cdc.gov/browse?category=Environmental+Health+%26+Toxicology
```
These are search result pages listing all datasets in a CDC category. No "last updated" date on a browse page would be meaningful for a specific dataset.

The fourth dataset (`dc/base/CDC_PM25County`) uses a more specific URL (`data.cdc.gov/Environmental-Health-Toxicology/Daily-County-Level-PM2-5-Concentratio...`) but this appears to be a truncated URL (cut off) that does not resolve to a valid page.

**Affected datasets:**
```
dc/base/CDC_OzoneCensusTract  → data.cdc.gov/browse?category=Environmental+Health+%26+Toxicology
dc/base/CDC_PM25CensusTract   → data.cdc.gov/browse?category=Environmental+Health+%26+Toxicology
dc/base/CDC_PM25County        → data.cdc.gov/Environmental-Health-Toxicology/Daily-County-Level-PM2-5-... (truncated)
dc/base/CDC_OzoneCounty       → data.cdc.gov/browse?category=Environmental+Health+%26+Toxicology&sortBy=last_modified
```

**Fix path:** `data.cdc.gov` is powered by Socrata. Each dataset has a unique 4x4 ID (e.g., `muzy-jte6`). With the dataset ID, the Socrata metadata API returns `updatedAt` without any scraping:
```
GET https://data.cdc.gov/api/views/{4x4-id}.json
```
The 4x4 IDs need to be identified per dataset and set as `source_data_url`.

---

### 12. CDC Wonder — `wonder.cdc.gov` (4 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Interactive query tool — no static page with refresh metadata**

CDC Wonder (`wonder.cdc.gov`) is an interactive epidemiological data query tool. Users select disease categories, geographic units, and time periods through a web form to generate custom tabulations. The URL endpoints are query-interface pages, not dataset landing pages. There is no static page associated with any CDC Wonder query that carries a "data as of" or "last updated" date.

**Affected datasets:**
```
dc/base/CDCWonder_NNDSS_InfectiousWeekly  → wonder.cdc.gov/nndss/nndss_weekly_tables_menu.asp
dc/base/CDC_Mortality_UnderlyingCause_SingleRace → wonder.cdc.gov/ucd-icd10-expanded.html
dc/base/CDC_Mortality_UnderlyingCause     → wonder.cdc.gov/ucd-icd10.html
dc/base/CDCWonder_NNDSS_InfectiousAnnual → wonder.cdc.gov/nndss/nndss_annual_tables_menu.asp
```

**Fix path:** CDC publishes many of the same datasets through `data.cdc.gov` with proper Socrata metadata. Mapping these 4 imports to their `data.cdc.gov` equivalents and using the Socrata API (see blocker #11) would resolve them. For NNDSS mortality data specifically, the CDC's public health data API (`data.cdc.gov/NNDSS/`) includes `updatedAt` metadata.

---

### 13. India NITI Aayog — `ndap.niti.gov.in` (4 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: React SPA — all data including "Last Updated" date is injected by JavaScript**

The National Data and Analytics Platform (NDAP) portal (`ndap.niti.gov.in/dataset/{id}`) loads as a blank React shell. Individual dataset pages in a browser show a clearly visible "Last Updated" field, but this field is populated by a JavaScript fetch call to a backend API after page load. The static HTML returned by an HTTP GET contains no content — just the React mount point.

- **Tier 4 (Playwright):** Playwright loads the page but the backend API calls that NDAP makes to populate the dataset card appear to be slow or require session state that Playwright's 2-second wait does not accommodate.
- **Tier 5 (Groq):** Groq's compound-beta browser failed on these too, possibly due to NDAP's anti-bot measures.

**Affected datasets:**
```
dc/base/IndiaNSS_HealthAilments         → ndap.niti.gov.in/dataset/7300
dc/base/IndiaNSS_HealthAilments_StatVarAgg → ndap.niti.gov.in/dataset/7300
dc/base/India_LifeExpectancy            → ndap.niti.gov.in/dataset/7375
dc/base/NITIIndiaPopulationProjection   → ndap.niti.gov.in/dataset/7208, /dataset/7209
dc/base/India_NFHS                      → ndap.niti.gov.in/dataset/6821, /6034, /6822
```

Note: `dc/base/India_NFHS` has a multi-URL provenance field (comma-separated), which the current pipeline does not split — it treats the entire comma-separated string as a single URL, causing the request to fail immediately.

**Fix path:** NDAP exposes an undocumented but publicly accessible API:
```
GET https://ndap.niti.gov.in/api/1/util/snippet/?id={dataset_id}
```
This returns JSON including a `last_updated` field. The dataset IDs are already in the provenance URLs (e.g., `7300`, `7375`). Additionally, the multi-URL parsing issue must be fixed so that comma-separated URLs are handled as separate sources.

---

### 14. South Korea Statistics (KOSIS) — `kosis.kr` (4 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: Korean-language JavaScript portal + non-English date formats**

The Korean Statistical Information Service (KOSIS) portal is a JavaScript-rendered application. Even if a date were found in the rendered HTML, the current date regex patterns in `_DATE_RE` only match English date expressions (e.g., "last updated", "as of") and ISO numeric formats. Korean date notation uses different vocabulary and characters (e.g., `2024년 12월 기준`) that do not match any current extraction pattern.

Additionally, the Groq compound-beta browser tier returned content but could not identify a date in the Korean-language rendered page.

**Affected datasets:**
```
dc/base/SouthKorea_Health       → kosis.kr/eng/statisticsList/statisticsListIndex.do
dc/base/SouthKorea_Employment   → kosis.kr/eng/statisticsList/statisticsListIndex.do
dc/base/SouthKorea_Demographics → kosis.kr/eng/statisticsList/statisticsListIndex.do
dc/base/SouthKorea_Education    → kosis.kr/eng/
```

**Fix path:** KOSIS provides an English-language API:
```
GET https://kosis.kr/openapi/statisticsData.do?method=getList&apiKey={key}&orgId={id}&tblId={table}
```
The API response includes `NDATE` (update date) fields in a parseable format. An API key is required (free registration at `kosis.kr/openapi/`).

---

### 15. OECD Data Explorer — `data-explorer.oecd.org` (3 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: React SPA with entire query state encoded in URL parameters**

The OECD's new data explorer uses extremely long, parameter-heavy URLs where the entire query configuration (dataset ID, dimensions, filters, time period, view type) is encoded as percent-encoded URL parameters. The pages themselves are React SPAs — a GET request returns a blank shell. Even Playwright and Groq browser automation fail because the application requires specific API calls to OECD's SDMX backend to populate content, and these calls appear to require session state or rate limiting that defeats automation.

**Affected datasets:**
```
dc/base/OECDRegionalDemography_LifeExpectancy → data-explorer.oecd.org/vis?df[ds]=DisseminateFinalDMZ&df[id]=DSD_REG_DEMO%40DF_LIFE_EXP...
dc/base/OECDRegionalDemography_Population     → data-explorer.oecd.org/vis?fs[0]=Topic%2C0%7CRegional...
dc/base/OECDRegionalDemography_Deaths         → data-explorer.oecd.org/vis?df[ds]=DisseminateFinalDMZ&df[id]=DSD_REG_DEMO%40DF_DEATHS...
```

**Fix path:** OECD has a public SDMX REST API that returns structured metadata without any browser interaction:
```
GET https://sdmx.oecd.org/public/rest/dataflow/OECD.CFE.EDS/DSD_REG_DEMO@DF_LIFE_EXP
```
The dataset identifiers (`DSD_REG_DEMO@DF_LIFE_EXP`, etc.) are embedded in the explorer URLs and can be extracted by regex. The SDMX response includes `validFrom` and `lastUpdateDate`.

---

### 16. EPA FTP Server — `gaftp.epa.gov` (3 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: FTP directory listing — dates are in `<pre>` text blocks, not parsed by any tier**

The EPA EJSCREEN datasets use:
```
https://gaftp.epa.gov/EJSCREEN/
```
This is an FTP server exposed over HTTPS. An HTTP GET returns an HTML directory listing containing file links and their modification timestamps embedded in `<pre>` tags (e.g., `2024-03-15 14:23   EJSCREEN_2024_BG.csv.zip`).

- **Tier 1 (HEAD):** The directory itself returns no `Last-Modified` header.
- **Tier 2 (HTML parse):** The HTML parser extracts JSON-LD, OpenGraph, and Dublin Core — none of which exist in an FTP directory listing. The body text regex (`_DATE_RE`) looks for phrases like "last updated" but the FTP listing has raw timestamps in `YYYY-MM-DD HH:MM` format without English labels.
- **Tier 3 (Gemini):** The page text is the raw directory listing text. Gemini does receive the timestamps but may not recognize them as "last refresh dates" without context.
- **Tier 4 (Playwright):** Playwright renders the raw directory text identically to what Tier 2 receives.
- **Tier 5 (Groq):** Groq saw the listing but did not extract a date.

**Affected datasets:**
```
dc/base/EPA_EJSCREEN                              → gaftp.epa.gov/EJSCREEN/
dc/base/EPA_EJSCREEN_AggCensusTract              → gaftp.epa.gov/EJSCREEN/
dc/base/EPA_EJSCREEN_AggCensusTract_AggCounty    → gaftp.epa.gov/EJSCREEN/
```

**Fix path:** Parse the FTP directory listing HTML directly — extract the most recent file modification timestamp from the `<pre>` or `<table>` block using a dedicated regex: `r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}'`. The most recent date among all listed files is the effective data refresh date. This requires a one-off domain-specific parser, not a generic tier.

---

### 17. FBI Crime Data — `www.fbi.gov` (2 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: HTTP URL that redirects inconsistently; HTTPS version of same URL succeeded for other FBI imports**

These 2 datasets use the old HTTP scheme:
```
http://www.fbi.gov/services/cjis/ucr
```
The current `https://www.fbi.gov/services/cjis/ucr/hate-crime` URL (HTTPS, more specific path) returned a date via Tier 5 for `dc/base/FBI_HateCrime`. The blocked URLs use HTTP and a less-specific path, which likely results in a redirect chain that exhausts the HTTP timeout or lands on a page with no date metadata.

**Affected datasets:**
```
dc/base/FBIGovCrime        → http://www.fbi.gov/services/cjis/ucr
dc/base/FBIGovCrime_AggCountry → http://www.fbi.gov/services/cjis/ucr
```

**Fix path:** Update provenance URL from `http://` to `https://` and use the specific UCR data landing page (`https://www.fbi.gov/services/cjis/ucr`) with trailing slash removed. The HTTPS version may be picked up by Tier 5 as the FBI hate crime URL was.

---

### 18. GitHub Tree Pages — `github.com` (2 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: GitHub tree/directory pages do not expose commit dates in static HTML**

Both datasets point to GitHub `/tree/` directory URLs:
```
https://github.com/wmgeolab/geoBoundaries/tree/main/releaseData/gbOpen/HND/ADM2
https://github.com/dsfsi/data-commons-data/tree/master/data/budget%20data/csv
```

GitHub tree pages display a file listing with relative time indicators ("last month", "3 weeks ago"), but these are:
1. Rendered by JavaScript, not present in static HTML
2. Relative phrases, not absolute dates
3. The `Last-Modified` HTTP header on GitHub pages reflects CDN cache age, not the last commit date

**Affected datasets:**
```
dc/base/GeoBoundaries_Honduras_ADM2 → github.com/wmgeolab/geoBoundaries/tree/main/releaseData/gbOpen/HND/ADM2
dc/base/SouthAfrica_Budget          → github.com/dsfsi/data-commons-data/tree/master/data/budget%20data/csv
```

**Fix path:** The GitHub Commits API returns the exact last commit date for a path without any scraping:
```
GET https://api.github.com/repos/{owner}/{repo}/commits?path={path}&per_page=1
Authorization: token {GITHUB_TOKEN}
```
The `GITHUB_TOKEN` environment variable is already set in this project's `.env` file. The owner, repo, and path can be extracted from the tree URL by regex.

---

### 19. ClimateTrace API Root — `api.climatetrace.org` (2 datasets)

**Tiers tried:** 1, 2, 3, 4, 5 — all fail

**Root cause: JSON API root — Tier 2 skips non-HTML responses**

Both ClimateTrace datasets point to `https://api.climatetrace.org/` — the root of the ClimateTrace REST API. A GET request to this URL returns a JSON response (the API's root index or version document), not HTML. Tier 2 explicitly checks `content_type` and returns `None` for non-HTML responses, which means no HTML is passed forward to Tiers 3–5.

The `climatetrace.org/inventory` URL for a third dataset (`dc/base/ClimateTrace_GHG_Emissions_OldAPI`) is JS-rendered but the HTTPS redirect fails — this URL has since been restructured.

**Affected datasets:**
```
dc/base/ClimateTrace_GreenhouseGas   → api.climatetrace.org/
dc/base/ClimateTrace_GHG_Emissions   → api.climatetrace.org/
```

**Fix path:** ClimateTrace's versioned API endpoint returns metadata including data vintage:
```
GET https://api.climatetrace.org/v6/
```
The response includes version and year fields. Tier 2 should be extended to parse JSON API roots for `version`, `year`, `updated`, or `lastUpdated` fields when `content_type` is `application/json` and the URL matches known API patterns.

---

### 20. Long-tail Blockers (60 datasets across 38 domains)

The remaining 60 blocked datasets span a diverse set of international and specialized portals. These fall into recognizable sub-patterns:

#### 20a. International Open Data Portals — OpenDataForAfrica (6 datasets)

**Domains:** `nigeria.opendataforafrica.org`, `cotedivoire.opendataforafrica.org`, `rwanda.opendataforafrica.org`, `southafrica.opendataforafrica.org`, `uganda.opendataforafrica.org`, `ethiopia.opendataforafrica.org`, `kenya.opendataforafrica.org`

These portals are all powered by the same platform. Tier 5 returned a year-only date for Nigeria (`2024`) and Ghana (`2024`) — but other African country portals timed out or returned no date. The portals are slow to respond and Groq's browser automation hit rate limits or timeouts on some of them.

**Fix:** Retry these individually with a longer timeout. The OpenDataForAfrica platform returns country-specific "last updated" dates on dataset pages.

#### 20b. India Government Portals (6 datasets)

**Domains:** `mospi.gov.in`, `hmis.nhp.gov.in`, `lgdirectory.gov.in`, `geosadak-pmgsy.nic.in`, `uidai.gov.in`, `pgi.seshagun.gov.in`

Indian government portals uniformly lack machine-readable date metadata. They use server-side rendering without JSON-LD or Dublin Core. Tier 5 succeeded for `ndap.niti.gov.in` in some cases but failed here. The `lgdirectory.gov.in` URL includes a CSRF token (`OWASP_CSRFTOKEN=G4BW-2HK0-...`) in the URL itself — this token expires, making the URL permanently invalid for future requests.

**Fix:** The CSRF-tokenized URL must be replaced with a stable URL. The other portals need Playwright with extended wait times (5+ seconds) to allow server-side content to render.

#### 20c. UN and International Agencies (3 datasets)

**Domains:** `unstats.un.org`, `www.who.int/data/gho` (missed — WHO homepage succeeded), `www.openfigi.com`

`unstats.un.org/unsd/energystats/data/` is a static table page that Tier 5 should handle but returned no date. `openfigi.com` is a financial data API home that returns no date metadata.

#### 20d. US Federal Agencies — Minor (6 datasets)

**Domains:** `www.bts.dot.gov`, `www.dol.gov`, `www.commerce.gov`, `www.fec.gov`, `ncsesdata.nsf.gov`, `ephtracking.cdc.gov`

These agencies have data portals with update dates visible in the browser, but Tier 5 (Groq) failed to extract them — likely due to bot detection or JS rendering issues. `www.fec.gov/data/browse-data/?tab=bulk-data` is a bulk data browser page where Groq returned no date.

#### 20e. Scientific Ontologies (3 datasets)

**Domains:** `disease-ontology.org`, `www.sequenceontology.org`, `mint.bio.uniroma2.it`

These are bioinformatics ontology databases. They use custom CMS or static sites that may carry version/release dates in unconventional formats (e.g., OBO ontology headers, GitHub release tags) that none of the 5 tiers recognize.

#### 20f. Other Portals with No Date (remainder)

| Dataset ID | URL | Failure Reason |
|---|---|---|
| `dc/base/NCBC_Gene` | `ncbi.nlm.nih.gov/gene` | Dynamic page, Groq hit no visible date |
| `dc/base/FireFAMWEB` | `fam.nwcg.gov/fam-web/` | Site loaded but no date found by Tier 5 |
| `dc/base/StormNOAA` | `www.ncdc.noaa.gov/` | Homepage only, NOAA redirected domain |
| `dc/base/Japan_Census` | `www.e-stat.go.jp/` | Japanese-language portal, no English date |
| `dc/base/Brazil_IBGE` | `www.ibge.gov.br/en/statistics/` | Portuguese site, no English date found |
| `dc/base/IndiaNHM` | `hmis.nhp.gov.in/` | Slow government portal, Tier 5 timed out |
| `dc/base/NYBG` | `nybg.org` | Tier 3 (Gemini) skipped (no html cache); no date |
| `dc/base/Wikipedia_Macedonia` | `en.wikipedia.org/wiki/Governorates_of_Macedonian/` | 404 — broken URL with trailing slash |
| `dc/base/NYT_COVID` | `nytimes.com/article/coronavirus-county-data-us.html` | Paywall / bot detection |

---

## Summary of Fix Paths by Priority

| Priority | Fix | Datasets Unblocked | Effort |
|---|---|---|---|
| **P0** | Extract Census vintage year from `tid=ACSST5Y{year}` URL parameter (no HTTP needed) | 37 | Very Low |
| **P0** | Flag `datacommons.org` datasets as `programmatically_trackable = false` | 22 | Very Low |
| **P0** | Fix multi-URL parsing in provenance field (comma-separated URLs) | ~5 | Very Low |
| **P1** | Add GitHub Commits API handler (token already in `.env`) | 2 | Low |
| **P1** | Add FTP directory listing parser for `gaftp.epa.gov` | 3 | Low |
| **P1** | Fix FBI URLs from HTTP to HTTPS | 2 | Very Low |
| **P1** | Add NASA CMR API handler for `nccs.nasa.gov` URLs | 28 | Low |
| **P1** | Add HUMDATA CKAN API handler for `/dataset/{slug}` URLs | 7 | Low |
| **P1** | Add NDAP API handler (`ndap.niti.gov.in/api/1/util/snippet/?id=`) | 4 | Low |
| **P1** | Add Wikidata SPARQL endpoint HEAD request | 36 | Low |
| **P2** | Add JSON API root date parsing to Tier 2 (for ClimateTrace, etc.) | 2 | Low |
| **P2** | Add OECD SDMX API handler | 3 | Medium |
| **P2** | Map CDC Wonder datasets to `data.cdc.gov` Socrata equivalents | 4 | Medium |
| **P2** | Map CDC browse-page datasets to specific Socrata view IDs | 4 | Medium |
| **P2** | Update EIA `source_data_url` from deprecated `qb.php` to v2 API | 7 | Medium |
| **P2** | Update Eurostat datasets from generic DB URL to specific dataset URLs | 7 | Medium |
| **P2** | Update RBI datasets from homepage to DBIE API endpoints | 7 | Medium |
| **P2** | Update RFF datasets from index page to specific publication URLs | 7 | Medium |
| **P3** | Add KOSIS API handler (requires registration) | 4 | Medium |
| **P3** | Add NCES Education Data API handler | 9 | Medium |
| **P3** | Extend Playwright wait time for slow Indian government portals | ~6 | Low |

**Estimated coverage if P0 + P1 fixes are applied: ~258 → ~140 remaining (~80% coverage)**  
**Estimated coverage if P0 + P1 + P2 fixes are applied: ~140 → ~50 remaining (~93% coverage)**

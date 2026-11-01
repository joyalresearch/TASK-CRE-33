# Project IRIS — TASK-CRE-33

## Overview

This implementation collects BAUHAUS store and OpenStreetMap evidence for Germany, reconciles store locations with building polygons, calculates projected areas and illustrative photovoltaic capacity, and loads building and parking candidates into PostgreSQL/PostGIS staging.

The submission includes source snapshots, screening decisions, geometry fixtures, automated tests, Docker configuration and database validation evidence.

All analysis polygons and staged geometries use **EPSG:25832**. Capacity figures are screening estimates, not verified installation capacity or engineering designs.

**National inventory completeness remains unverified.**

## Results

### Official store inventory

The collected locator responses contain **161 distinct official BAUHAUS store IDs**.

| Building-association outcome | Official stores |
| --- | ---: |
| Accepted screening association | 86 |
| Provisional association | 21 |
| Parking structure requiring review | 2 |
| No selected containing building | 52 |
| **Total** | **161** |

“Accepted” refers to the recorded screening association between a store and a building polygon. It does not establish ownership, exclusive roof rights or installation feasibility.

The 52 stores without selected building geometry remain in the reconciliation reports with unavailable capacity.

### Staging inventory

| Feature | Association status | Records |
| --- | --- | ---: |
| Building | `accepted_screening_association` | 86 |
| Building | `provisional_association` | 21 |
| Building | `parking_structure_review` | 2 |
| Building | `unmatched_osm_candidate` | 63 |
| Parking | `unverified_operator` | 329 |
| Parking | `unverified_proximity` | 808 |
| **Total** | | **1,309** |

The 172 building polygons comprise 167 consolidated OSM candidates and five additional cached geometry candidates. The 63 unmatched building candidates are not additional confirmed stores.

The 1,137 parking polygons form an acquisition and audit inventory. They are not all attributed to BAUHAUS. Only 34 polygons qualify for the separate parking-capacity scenarios.

## Key findings

The collected official locator evidence identifies 161 distinct BAUHAUS stores. Of these, 86 have accepted screening building associations and 21 have provisional associations. Another 54 remain unavailable for rooftop estimation: 52 lack selected containing geometry and two require parking-structure review.

The 86 accepted associations yield an illustrative rooftop estimate of 119.94 MWp under the stated assumptions. This represents the selected screening subset, not verified installable capacity or a nationwide total.

Strict parking identity screening reduces the 1,137 source candidates to 34 qualifying BAUHAUS-tagged polygons, covering 275,049.82 m² after geometric exclusions. Their conditional parking-canopy scenarios range from 8.251 to 16.503 MWp, with a central scenario of 13.752 MWp.

The main remaining uncertainties concern inventory completeness, unresolved building associations, property rights and physical installation feasibility. Rooftop and parking estimates remain separate.

## Runtime requirements

- Docker Engine or Docker Desktop.
- Docker Compose supporting the `!override` tag used by `compose.init-test.yaml`.
- PostgreSQL 16 / PostGIS 3.4, provided by Compose.
- Python 3.11+ for local execution; the tested Docker image uses Python 3.13.

Commands below use PowerShell and run from the repository root.

The packaged-data tests, dry run and database load require **no Apify account, token or live acquisition**. Building the image initially requires access to its base image and dependency packages.

## Offline validation

Build the application image:

```powershell
docker build -t iris-task-cre-33:local .
```

Validate packaged inputs without a database or network access:

```powershell
docker run --rm --network none iris-task-cre-33:local
```

Run the automated tests:

```powershell
docker run --rm --network none iris-task-cre-33:local python -m unittest discover -s tests -p "test_*.py" -v
```

Expected results:

- Pipeline dry run completes without database writes.
- 172 building polygons and 1,137 parking polygons.
- 1,309 expected staging records.
- 39 tests pass.

## PostgreSQL/PostGIS execution

### Environment configuration

Create the local configuration from the template if it does not already exist:

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
```

Set different values for `POSTGRES_PASSWORD` and `IRIS_LOADER_PASSWORD` in `.env`.

Compose supplies `DB_URI` and the loader password to the application container. The packaged-data workflow does not use `APIFY_TOKEN`.

Database initialization scripts apply when a database volume is first created. Updating `.env` does not change passwords in an existing PostgreSQL volume.

### Isolated database initialization

The following commands create a separate Compose project and database volume. The port override uses `127.0.0.1:5434`.

```powershell
$reviewProject = "iris-cre-33-review-" + (Get-Date -Format "yyyyMMddHHmmss")
$pvReport = "data/processed/reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json"

docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml --profile tools build app

docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml up -d --wait db
```

### Validation without writes

```powershell
docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml run --rm app python src/iris_cre_33/run_pipeline.py --dry-run --pv-report $pvReport
```

### Load and verification

```powershell
docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml run --rm app python src/iris_cre_33/run_pipeline.py --pv-report $pvReport
```

Expected first-load counts on an empty database:

| Feature | Inserted | Updated |
| --- | ---: | ---: |
| Building polygons | 172 | 0 |
| Parking polygons | 1,137 | 0 |
| **Total** | **1,309** | **0** |

The pipeline verifies identities, association statuses, valid nonempty geometry, SRID, area agreement and capacity/NULL values.

Expected verification output:

```text
Actual staging records: 1309
Missing feature IDs: 0
Unexpected feature IDs: 0
Association mismatches: 0
Invalid geometries/SRID: 0
Area mismatches (>2%): 0
PV/NULL mismatches: 0
Database verification: PASSED
```

### Repeat-load verification

Repeat the load against the same Compose project:

```powershell
docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml run --rm app python src/iris_cre_33/run_pipeline.py --pv-report $pvReport
```

Expected results:

- Zero inserts.
- 172 building updates.
- 1,137 parking updates.
- 1,309 records retained.
- Database verification passes.

Inspect the staged classifications:

```powershell
docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml exec -T db psql -X -U iris_user -d iris_local -c "SELECT feature_type, association_status, COUNT(*) AS records, COUNT(estimated_pv_kwp) AS nonnull_capacity_records FROM iris_staging.commercial_poi_staging GROUP BY feature_type, association_status ORDER BY feature_type, association_status;"
```

Stop the containers while retaining the database volume:

```powershell
docker compose -p $reviewProject -f compose.yaml -f compose.init-test.yaml stop
```

## Acquisition and source reconciliation

### OpenStreetMap

The initial Overpass query searches Germany for `shop=doityourself` and BAUHAUS brand/name matches.

Name matching broadens discovery but can return unrelated businesses. Search hits therefore remain candidates until reconciled with additional evidence.

Building polygons are assembled from preserved Overpass responses and OSM map extracts. Way processing requires complete closed boundaries. The locator-discovery assembler also supports multipolygon relations, joined outer boundaries and inner holes.

Source longitude/latitude coordinates are transformed with `always_xy=True`. Polygon analysis and persistence use EPSG:25832.

`geometry.py` applies `shapely.make_valid()` to invalid polygon input. Other parsing paths reject incomplete or unsupported geometry. Loaders reject empty, invalid or non-polygonal geometries.

### Official BAUHAUS locator

Direct scripted requests encountered HTTP 403 security responses. Official source responses were subsequently captured through the third-party `apify/web-fetch` service.

Observed interfaces:

- Postcode search: `https://www.bauhaus.info/api/stores?q=<postcode>`
- Coordinate search: `https://www.bauhaus.info/api/stores-search?lat=<lat>&lng=<lng>&user_lat=undefined&user_lng=undefined`
- Store sitemap: `https://www.bauhaus.info/sitemaps/stores-de-0.xml`

The postcode collection used 156 OSM-derived seeds and returned 704 observations covering 159 distinct official IDs. Three coordinate captures added Bremerhaven and Aschaffenburg, producing the combined 161-ID inventory.

The store sitemap contains 180 distinct IDs. All 161 collected official IDs appear in it. Of the remaining 19 sitemap URLs, eight returned source HTTP 404 and eleven ended in provider timeouts. Their facility status remains unresolved.

Kretzschau also remains unresolved. Its absence from collected locator results and an unavailable store page do not establish closure.

Captures retain source responses, request metadata, timestamps and hashes where recorded. Acquisition code distinguishes provider success from source status/content validation. Locator retries are bounded and restricted to classified failures; unknown outcomes remain available for inspection.

Live acquisition is separate from the default packaged-data workflow and requires its own service configuration and allowance.

### Building associations

Recorded decisions distinguish address-supported screening associations, provisional evidence, parking structures and unresolved geometry.

The final building loader checks the selected geometry identities, decision/report consistency, input hashes, geometry areas and calculated capacity values. Unaccepted capacities remain NULL in the main staging column.

### Parking candidates

The parking inventory combines:

- 339 eligible Cologne surface-parking ways.
- 808 nationwide proximity candidates.
- Ten overlapping OSM identities, counted once.

The original nationwide discovery searches within 150 m of the 167 consolidated building candidates and records 867 building–parking candidate pairs.

Acquisition classifications are preserved in staging. Eligibility for parking PV is determined separately by the stricter identity-screening model.

## Rooftop PV methodology

The rooftop screening model uses:

```text
assumed usable roof area (m²) = footprint area (m²) × 0.60
illustrative capacity (kWp) = assumed usable roof area (m²) ÷ 5.0 (m²/kWp)
```

The model assumes a **60% usable-area fraction** and an **area requirement of 5 m²/kWp**. These are project assumptions without external validation or site-specific calibration.

Building footprint area provides a proxy for roof plan area. The usable-area fraction is applied uniformly; it does not represent measured roof availability. The model does not separately quantify panel spacing, setbacks, shading, equipment or access routes. Neither factor has a documented allocation for these constraints.

Implementation: `src/iris_cre_33/estimates.py` (`estimate_pv_kwp`, defaults `usable_fraction=0.60` and `sqm_per_kwp=5.0`) and `src/iris_cre_33/estimate_reconciled_pv.py` (`--usable-fraction` and `--sqm-per-kwp`).

| Group | Footprint area m² | Assumed usable area m² | Illustrative capacity kWp |
| --- | ---: | ---: | ---: |
| Accepted screening associations, 86 stores | 999,459.37 | 599,675.62 | 119,935.12 |
| Provisional associations, 21 stores | 217,664.78 | 130,598.87 | 26,119.77 |

The accepted aggregate uses unrounded calculations. Summing the individually rounded staging estimates gives 119,935.13 kWp.

Only accepted screening estimates populate `estimated_pv_kwp`. Provisional estimates remain separately labelled in report records and building reconciliation metadata.

Parking structures and unresolved cases receive no rooftop estimate. Accepted and provisional aggregates are not combined into a national total.

## Parking PV methodology

Identity rule: `bauhaus-explicit-tags-v1`.

A parking polygon qualifies only when it has:

1. Explicit BAUHAUS text evidence in `brand`, `operator` or `name`.
2. No detected contradictory or ambiguous identity evidence.
3. Exactly one nearby selected accepted/provisional building association.
4. No covered or solar tag requiring review.
5. Positive area after geometric exclusions.

Other retailers, unidentified operators, ambiguous shared names, proximity-only candidates and parking linked to multiple selected stores are withheld.

The identity rules are conservative: unreviewed wording can withhold a genuine BAUHAUS candidate. Passing these rules does not verify ownership or installation rights.

Known building footprints and overlaps with conflicting/ambiguous parking polygons are subtracted. Eligible surfaces are unioned before estimation to avoid overlapping area being counted more than once.

| Measure | Result |
| --- | ---: |
| Source parking polygons retained for audit | 1,137 |
| Qualifying scenario polygons | 34 |
| Qualifying polygons near accepted building candidates | 25 |
| Qualifying polygons near provisional building candidates | 9 |
| Scenario surface union area | 275,049.82 m² |
| Verified parking ownership | 0 |

The all-source audit union is 3,569,643.25 m². It includes excluded candidates, is not BAUHAUS-attributed area and does not enter the capacity calculation.

### Assumptions and reference

The parking model uses:

```text
assumed module-covered area (m²) = eligible parking union area (m²)
                                  × parking-bay fraction
                                  × canopy fraction of bay area
conditional capacity (kWp) = assumed module-covered area (m²) ÷ 5.0 (m²/kWp)
```

The parking-bay fraction represents the assumed proportion of parking surface occupied by bays. The canopy fraction represents the assumed proportion of bay area covered by PV canopies. Their product determines the assumed module-covered area. The rooftop usable-area factor is not applied to parking.

Fraunhofer ISE's January 2024 BWIHK electricity study for Baden-Württemberg, section 3.5, pages 36–37, assumes parking bays occupy 50% of total parking area and that 50% of bays receive PV canopies. It also discusses bay-area shares of 30–60%.

This project adapts those fractions as sensitivity assumptions:

| Scenario | Bay share | Canopied share of bays | Effective module-area fraction | Conditional capacity MWp |
| --- | ---: | ---: | ---: | ---: |
| Lower | 30% | 50% | 15% | 8.251 |
| Central | 50% | 50% | 25% | 13.752 |
| Higher | 60% | 50% | 30% | 16.503 |

The bay and canopy fractions are adapted sensitivity assumptions, without site-specific calibration. The conversion factor of **5 m²/kWp** is a separate project assumption without external validation; it is not taken from the study. Panel spacing and other layout losses are not separately quantified, and the density factor has no documented spacing allowance.

Parking candidates must satisfy the identity and single-store relationship criteria within the estimator's default distance threshold of **150 m**. This threshold is a screening choice. Ownership and installation rights remain unverified. Missing covered or solar tags do not confirm the absence of existing cover or solar installations.

Implementation: `src/iris_cre_33/estimate_parking_pv.py`, bay fractions `0.30`, `0.50`, `0.60`, and defaults `--canopy-share=0.50`, `--sqm-per-kwp=5.0` and `--radius-m=150.0`.

The scenarios use selected assumptions from the reference study rather than its complete methodology. They exclude site-specific layout, clearance, tree/shading, structural, grid and permissions assessments.

Reference: [Fraunhofer ISE — Stromstudie für Baden-Württemberg, January 2024](https://www.ise.fraunhofer.de/content/dam/ise/de/documents/presseinformationen/2024/IHK-Stromstudie_v06.pdf).

All parking capacity results remain provisional. Parking `estimated_pv_kwp` values remain NULL in staging. Rooftop and parking capacities are not combined.

## Recomputing estimates

The Docker image contains the inputs required to recompute the reported estimates.

Rooftop estimates:

```powershell
docker run --rm --network none iris-task-cre-33:local python src/iris_cre_33/estimate_reconciled_pv.py --decisions data/processed/official_building_decisions_48087390d370408c872b99c386f7aabc.json --cached-gap-search data/processed/cached_geometry_gap_search_9ada88d77424467ca7066942a8f6321a.json
```

Parking scenarios:

```powershell
docker run --rm --network none iris-task-cre-33:local python src/iris_cre_33/estimate_parking_pv.py --pv-report data/processed/reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json
```

These commands print results and create reports inside disposable containers. Generated files disappear when those containers exit.

Equivalent local execution with `requirements.txt` installed retains reports under `data/processed`.

Generated reports have new timestamps, filenames and recorded paths. Numerical results are reproducible; byte-identical generated files are not expected.

## Implementation and schema

| Component | Responsibility |
| --- | --- |
| `reconciled_inputs.py` | Evidence-path resolution, hash checks and reconciled building records |
| `run_pipeline.py` | Input validation, loader orchestration and PostGIS verification |
| `load_bauhaus_germany.py` | Building validation and upsert |
| `load_parking.py` | Parking validation, deduplication and loading |
| `estimate_reconciled_pv.py` | Separate accepted/provisional rooftop estimates |
| `estimate_parking_pv.py` | Parking identity screening and capacity scenarios |
| `sql/001_init.sql` | Local staging schema, constraints and spatial index |
| `sql/002_create_loader.sh` | Restricted loader-role initialization |
| `tests/fixtures/locator_discovery/` | Representative preserved source fixture |

Target table: `iris_staging.commercial_poi_staging`.

| Column | Meaning |
| --- | --- |
| `source` | Geometry source: `openstreetmap` |
| `osm_type`, `osm_id` | Stable OSM identity |
| `feature_type` | Building or parking |
| `name`, `tags` | Source attributes and building reconciliation metadata |
| `geom` | MultiPolygon in EPSG:25832 |
| `area_sqm` | Projected polygon area |
| `association_status` | Evidence classification |
| `estimated_pv_kwp` | Accepted rooftop screening estimate; otherwise NULL |
| `created_at`, `updated_at` | Database timestamps |

The unique upsert key is:

```text
(source, osm_type, osm_id, feature_type)
```

A GiST index supports spatial queries on `geom`.

The included SQL defines a provisional local schema. Client target-DDL confirmation remains outstanding.

## Persistence and error handling

Application data writes target only `iris_staging.commercial_poi_staging`, through environment-variable `DB_URI` and the restricted `iris_loader` account.

Loaders reject unexpected database names or roles. SQL values are parameterized.

Each loader uses its own transaction. Buildings and parking do not share one combined transaction: a parking failure can occur after the building transaction commits.

Upserts prevent duplicate source identities. Repeat runs update `updated_at`; idempotency refers to stable identities and substantive values, not unchanged timestamps.

Obsolete rows are not automatically deleted. Post-load verification reports unexpected identities.

Geometry and input checks reject invalid or inconsistent records rather than silently replacing unavailable data.

## Tests and execution evidence

Observed results for the current implementation:

| Check | Result |
| --- | --- |
| Docker build | Passed |
| Offline packaged-data dry run | Passed |
| Local automated tests | 39 passed |
| Container automated tests | 39 passed |
| Fresh isolated database load | 172 building inserts; 1,137 parking inserts |
| Repeat database load | Zero inserts; 1,309 updates |
| PostGIS verification | Passed on both loads |

PostGIS verification found zero missing identities, unexpected identities, association mismatches, invalid geometries/SRIDs, area discrepancies above 2%, or capacity/NULL mismatches.

Tests cover projected area, invalid-input rejection, multipolygon assembly, courtyard holes, fixture integrity, changed source hashes, capacity eligibility, NULL preservation, parking identity conflicts and scenario calculation consistency.

Recorded evidence includes:

- `data/reports/final_validation/repeat_load.log`
- `data/reports/idempotency_ef2fc451e10c47c2949b89345ac0a366/report.json`
- Source captures under `data/raw/`
- Reconciliation and estimate reports under `data/processed/`

Current rooftop report:

```text
data/processed/reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json
```

Current parking report:

```text
data/processed/parking_pv_scenarios_2938d366aa7d4ef693e39f6df8964d71.json
```

Versioned development outputs may also be present. Earlier parking scenario reports are superseded by the current explicitly BAUHAUS-tagged model.

## Reproducibility boundaries

The packaged snapshot load, automated tests and estimate calculations run without live acquisition credentials.

Final association decisions are supplied as recorded screening inputs. Some consolidation and reconciliation steps were performed through development scripts or terminal snippets; the packaged workflow does not automate the complete raw-source-to-final-decision process.

Input hashes provide consistency checks between packaged evidence and calculations. They do not independently establish source accuracy, ownership or the correctness of every screening decision.

## Limitations

- OSM-derived postcode seeds are not an independent national census. Unresolved sitemap entries and unverified locator result limits prevent a completeness claim.
- The original parking acquisition surrounds 167 building candidates. Relevant parking can be absent where store/building geometry is unresolved.
- Surface-parking relations are not comprehensively assembled. Deferred relation information is retained where available.
- Direct BAUHAUS access encountered security blocks. Other captures returned HTTP 404 or provider timeouts.
- Apify provided third-party retrieval transport; source validation is limited to the recorded response checks.
- OSM tags, polygon boundaries and official point locations can be inaccurate or stale.
- Projected areas satisfy the required EPSG:25832 analysis convention but are not survey-grade measurements.
- Existing PV installations may be untagged. The estimates do not establish additional capacity beyond existing installations.
- No structural, shading, setback, electrical, grid-connection, canopy-layout or engineering assessment is performed.
- Ownership, leases, exclusive roof use, parking allocation, installation rights and permitting remain unverified.
- GRZ/GFZ development rights, WALT, ImmoWertV valuation, revenue forecasting and investment valuation are outside scope.

## Sources and attribution

Geographic source data: **© OpenStreetMap contributors**, available under the Open Database License.

[OpenStreetMap copyright and licensing](https://www.openstreetmap.org/copyright)

Official store information: [BAUHAUS](https://www.bauhaus.info/).

Parking scenario reference: [Fraunhofer ISE / BWIHK electricity study for Baden-Württemberg, January 2024](https://www.ise.fraunhofer.de/content/dam/ise/de/documents/presseinformationen/2024/IHK-Stromstudie_v06.pdf), section 3.5, pages 36–37.

Credentials are supplied through environment variables. `.env.example` contains configuration placeholders only.
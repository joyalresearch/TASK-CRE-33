FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check

COPY src/ ./src/
COPY tests/ ./tests/

COPY data/raw/parking_cologne_raw.json ./data/raw/parking_cologne_raw.json

COPY data/processed/bauhaus_buildings_consolidated.json ./data/processed/bauhaus_buildings_consolidated.json
COPY data/processed/parking_nationwide_candidates.json ./data/processed/parking_nationwide_candidates.json

COPY data/processed/official_building_decisions_48087390d370408c872b99c386f7aabc.json ./data/processed/official_building_decisions_48087390d370408c872b99c386f7aabc.json
COPY data/processed/cached_geometry_gap_search_9ada88d77424467ca7066942a8f6321a.json ./data/processed/cached_geometry_gap_search_9ada88d77424467ca7066942a8f6321a.json
COPY data/processed/reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json ./data/processed/reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json

COPY data/processed/parking_pv_scenarios_2938d366aa7d4ef693e39f6df8964d71.json ./data/processed/parking_pv_scenarios_2938d366aa7d4ef693e39f6df8964d71.json
COPY data/processed/parking_density_review/parking_pv_scenarios_05788ba5707c4d3d80e0085ea8cbcc92.json ./data/processed/parking_density_review/parking_pv_scenarios_05788ba5707c4d3d80e0085ea8cbcc92.json

CMD ["python", "src/iris_cre_33/run_pipeline.py", "--dry-run", "--pv-report", "data/processed/reconciled_pv_0357b8d5cc8f40ecaf9d77c7c255d33b.json"]
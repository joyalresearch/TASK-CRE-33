#!/usr/bin/env python3
"""Discover OSM surface parking near consolidated BAUHAUS building footprints.

Run from the repository root. This acquisition stage never writes to PostGIS.
It caches individual OSM map responses and produces reviewable candidate evidence.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import Transformer
from shapely import wkt
from shapely.geometry import Polygon
from shapely.ops import transform
from shapely.strtree import STRtree

SOURCE = Path('data/processed/bauhaus_buildings_consolidated.json')
CACHE = Path('data/raw/osm_parking_areas')
OUTPUT = Path('data/processed/parking_nationwide_candidates.json')
REPORT = Path('data/reports/parking_nationwide_discovery_report.json')
API = 'https://api.openstreetmap.org/api/0.6/map'
USER_AGENT = 'IRIS-CRE-33/0.1 (research; OSM data acquisition)'
TO_WGS84 = Transformer.from_crs(25832, 4326, always_xy=True)
TO_METRIC = Transformer.from_crs(4326, 25832, always_xy=True)


def read_buildings():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    buildings = []
    seen = set()
    for feature in data['features']:
        if feature['feature_type'] != 'building':
            continue
        identity = (feature['osm_type'], int(feature['osm_id']))
        if identity in seen:
            raise ValueError(f'Duplicate building: {identity}')
        seen.add(identity)
        geom = wkt.loads(feature['geometry_wkt_25832'])
        if geom.is_empty or not geom.is_valid:
            raise ValueError(f'Invalid building: {identity}')
        buildings.append((identity, geom))
    if not buildings:
        raise ValueError('No buildings found')
    return buildings


def area_bbox(geom, radius_m):
    minx, miny, maxx, maxy = geom.buffer(radius_m).bounds
    corners = [TO_WGS84.transform(x, y) for x in (minx, maxx) for y in (miny, maxy)]
    return (min(p[0] for p in corners), min(p[1] for p in corners),
            max(p[0] for p in corners), max(p[1] for p in corners))


def fetch(idx, bbox, retries, pause):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f'area_{idx}.osm'
    if path.exists():
        data = path.read_bytes()
        ET.fromstring(data)
        return data, True
    url = API + '?bbox=' + ','.join(f'{value:.7f}' for value in bbox)
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
            ET.fromstring(data)
            path.write_bytes(data)
            time.sleep(pause)
            return data, False
        except (urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
            if attempt == retries:
                raise RuntimeError(f'OSM request failed for area {idx}: {exc}') from exc
            time.sleep(pause * (2 ** attempt))
    raise RuntimeError('Unexpected request loop termination')


def parking_from_xml(data):
    root = ET.fromstring(data)
    nodes = {node.attrib['id']: (float(node.attrib['lon']), float(node.attrib['lat']))
             for node in root.findall('node')}
    results = []
    deferred = 0
    for relation in root.findall('relation'):
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in relation.findall('tag')}
        if tags.get('amenity') == 'parking' and tags.get('parking') == 'surface':
            deferred += 1
    for way in root.findall('way'):
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in way.findall('tag')}
        if tags.get('amenity') != 'parking' or tags.get('parking') != 'surface':
            continue
        refs = [nd.attrib['ref'] for nd in way.findall('nd')]
        if len(refs) < 4 or refs[0] != refs[-1] or any(ref not in nodes for ref in refs):
            continue
        geom = Polygon([nodes[ref] for ref in refs])
        if geom.is_empty or not geom.is_valid or geom.area == 0:
            continue
        metric = transform(TO_METRIC.transform, geom)
        if metric.is_empty or not metric.is_valid or metric.area <= 0:
            continue
        results.append((int(way.attrib['id']), tags, metric))
    return results, deferred


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--radius-m', type=float, default=150.0)
    parser.add_argument('--pause-s', type=float, default=2.0)
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--max-areas', type=int, default=0, help='Limit new processing for pilot; 0=all')
    args = parser.parse_args()
    if not (0 < args.radius_m <= 500) or not (0 <= args.pause_s <= 60) or not (0 <= args.retries <= 8) or args.max_areas < 0:
        parser.error('Invalid radius, pause, retries or max-areas')
    buildings = read_buildings()
    # Stable per-building cache keys allow safe resumption across runs.
    # Nearby areas may overlap; parking is deduplicated by OSM way ID.
    parking = {}
    processed = 0
    cached = 0
    failures = []
    deferred_relations = 0
    for identity, geom in buildings:
        if args.max_areas and processed >= args.max_areas:
            break
        key = f'{identity[0]}_{identity[1]}'
        bbox = area_bbox(geom, args.radius_m)
        # OSM API /map limit is 0.25 square degrees; reject unusually large bboxes.
        if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 0.25:
            failures.append({'building': key, 'error': 'bbox exceeds OSM map API area limit'})
            processed += 1
            continue
        try:
            xml, from_cache = fetch(key, bbox, args.retries, args.pause_s)
            cached += int(from_cache)
            candidates, deferred = parking_from_xml(xml)
            deferred_relations += deferred
            for osm_id, tags, candidate_geom in candidates:
                parking.setdefault(osm_id, (tags, candidate_geom))
            print(f'{processed + 1}/{len(buildings)} {key}: {len(candidates)} parking ways; cache={from_cache}', flush=True)
        except (RuntimeError, ValueError, ET.ParseError) as exc:
            failures.append({'building': key, 'error': str(exc)})
            print(f'{processed + 1}/{len(buildings)} {key}: FAILED {exc}', flush=True)
        processed += 1
    matches = []
    records = []
    if parking:
        ids = sorted(parking)
        geoms = [parking[i][1] for i in ids]
        parking_tree = STRtree(geoms)
        for identity, building_geom in buildings:
            for j in parking_tree.query(building_geom.buffer(args.radius_m), predicate='intersects'):
                j = int(j)
                distance = building_geom.distance(geoms[j])
                if distance <= args.radius_m:
                    matches.append({'building_osm_type': identity[0], 'building_osm_id': identity[1],
                                    'parking_osm_type': 'way', 'parking_osm_id': ids[j],
                                    'distance_m_epsg25832': round(distance, 2),
                                    'association_status': 'unverified_proximity', 'ownership_verified': False})
        matched_ids = {m['parking_osm_id'] for m in matches}
        for osm_id in ids:
            if osm_id not in matched_ids:
                continue
            tags, geom = parking[osm_id]
            records.append({'source': 'openstreetmap', 'osm_type': 'way', 'osm_id': osm_id,
                            'feature_type': 'parking', 'tags': tags, 'area_sqm': round(geom.area, 2),
                            'geometry_wkt_25832': geom.wkt, 'association_status': 'unverified_proximity',
                            'ownership_verified': False, 'estimated_pv_kwp': None})
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({'features': records, 'matches': matches}, indent=2, ensure_ascii=False), encoding='utf-8')
    report = {'buildings_total': len(buildings), 'areas_processed': processed,
              'areas_cached': cached, 'areas_failed': len(failures), 'failures': failures,
              'parking_ways_discovered': len(parking), 'parking_polygons_near_buildings': len(records),
              'candidate_pairs': len(matches), 'deferred_relation_occurrences': deferred_relations,
              'radius_m': args.radius_m, 'distance_crs': 'EPSG:25832',
              'ownership_verified': 0, 'partial_run': processed < len(buildings)}
    REPORT.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('REPORT:', json.dumps({k: v for k, v in report.items() if k != 'failures'}, indent=2))
    print('Saved:', OUTPUT, 'and', REPORT)


if __name__ == '__main__':
    main()

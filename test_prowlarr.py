#!/usr/bin/env python3
"""Quick Prowlarr query test for MotoGP Practice"""

import requests
import xml.etree.ElementTree as ET

PROWLARR_URL = "http://192.168.1.241:9696"
PROWLARR_API_KEY = "57e853d282924fbf962db854e8a2eb82"

def get_indexers():
    headers = {"X-Api-Key": PROWLARR_API_KEY}
    resp = requests.get(f"{PROWLARR_URL}/api/v1/indexer", headers=headers, timeout=10)
    return [i for i in resp.json() if i.get('enable')]

def search_indexer(query, indexer_id, indexer_name):
    """Search a specific indexer."""
    try:
        url = f"{PROWLARR_URL}/{indexer_id}/api"
        params = {"t": "search", "q": query, "apikey": PROWLARR_API_KEY, "limit": 100}
        resp = requests.get(url, params=params, timeout=60)
        
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            items = root.findall('.//item')
            titles = []
            for item in items:
                title_elem = item.find('title')
                if title_elem is not None and title_elem.text:
                    titles.append(title_elem.text)
            return titles
        return []
    except Exception as e:
        print(f"  Error: {e}")
        return []

# Add this temporarily to test
def test_practice_classification():
    test_cases = [
        "MotoGP.2026.USA.Practice.1080p.WEB.h264-VERUM",
        "MotoGP.2026.USA.Free.Practice.One.1080p.WEB.h264-VERUM",
        "motogp.2026.usa.practice.one.1080p.web.h264-verum",
        "Moto2.2026.USA.Practice.1080p.WEB.h264-BILLIE",
        "Moto2.2026.USA.FP1.1080p.WEB.h264-BILLIE",
    ]
    
    print("\n" + "="*70)
    print("PRACTICE CLASSIFICATION TEST")
    print("="*70)
    
    for title in test_cases:
        race_class = detect_race_class(title)
        session = classify_session(title, race_class)
        print(f"\n{title}")
        print(f"  Class: {race_class.display_name}")
        print(f"  Session: {session.full_display_name}")

# Call it
test_practice_classification()

def test_queries():
    indexers = get_indexers()
    print(f"Found {len(indexers)} enabled indexers:")
    for idx in indexers:
        print(f"  - {idx['name']} (ID: {idx['id']})")
    
    # Test queries - trying to find:
    # MotoGP.2026.USA.Practice.1080p.WEB.h264-VERUM
    # Moto2.2026.USA.Practice.1080p.WEB.h264-BILLIE
    
    test_queries = [
        # Broad queries
        "MotoGP 2026",
        "Moto2 2026",
        
        # With location
        "MotoGP 2026 USA",
        "Moto2 2026 USA",
        
        # With session
        "MotoGP 2026 Practice",
        "Moto2 2026 Practice",
        "MotoGP USA Practice",
        "Moto2 USA Practice",
        
        # Combined
        "MotoGP 2026 USA Practice",
        "Moto2 2026 USA Practice",
        
        # What your app currently uses
        "MotoGP Americas 2026",
        "MotoGP 2026 americas",
        "MotoGP 2026 usa",
        "Moto2 Americas 2026",
        "Moto2 2026 usa",
        
        # Round-based
        "MotoGP 2026 Round03",
        "MotoGP 2026 Round 3",
        "Moto2 2026 Round03",
    ]
    
    for query in test_queries:
        print(f"\n{'='*60}")
        print(f"QUERY: '{query}'")
        print(f"{'='*60}")
        
        for idx in indexers:
            results = search_indexer(query, idx['id'], idx['name'])
            
            # Filter for Practice (not FP1/FP2)
            practice_only = [t for t in results 
                           if 'practice' in t.lower() 
                           and 'fp1' not in t.lower() 
                           and 'fp2' not in t.lower()
                           and 'free practice' not in t.lower()]
            
            all_practice = [t for t in results if 'practice' in t.lower()]
            
            if practice_only:
                print(f"\n  ✅ {idx['name']}: {len(results)} total, {len(practice_only)} PRACTICE sessions")
                for t in practice_only[:5]:
                    print(f"     → {t}")
            elif all_practice:
                print(f"\n  ⚠️  {idx['name']}: {len(results)} total, {len(all_practice)} practice-related (FP1/FP2)")
                for t in all_practice[:3]:
                    print(f"     → {t}")
            elif results:
                print(f"\n  📋 {idx['name']}: {len(results)} results (no practice)")
            else:
                print(f"\n  ❌ {idx['name']}: No results")

if __name__ == "__main__":
    print("MotoGP Prowlarr Search Diagnostic")
    print("="*60)
    print(f"Prowlarr: {PROWLARR_URL}")
    print()
    test_queries()
#!/usr/bin/env python3
"""
Scrapes Veranstaltungen (events) for a set of Berlin Stadtbibliotheken from the
central berlin.de event calendar (berlin.de/land/kalender) and writes events.json.

Each supported library has a fixed "c" (channel) id in that calendar system.
Not all 12 Berlin districts have their own channel id yet -- see UNSUPPORTED_LIBRARIES.

Usage:
    python scrape.py                # current month
    python scrape.py 2026-09        # specific month (YYYY-MM)

This is a first working version. berlin.de's HTML structure was inferred from
rendered output, not verified byte-for-byte -- if a run produces zero events for
a library that normally has some, that library's parsing likely needs adjusting.
"""

import sys
import json
import re
import time
import calendar
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.berlin.de/land/kalender/index.php"

LIBRARIES = {
    "327": "Stadtbibliothek Berlin-Mitte",
    "146": "Stadtbibliothek Neukölln",
    "439": "Stadtbibliothek Marzahn-Hellersdorf",
    "409": "Stadtbibliothek Pankow",
    "230": "Stadtbibliothek Reinickendorf",
    "67": "Stadtbibliothek Steglitz-Zehlendorf",
    "145": "Stadtbibliothek Tempelhof-Schöneberg",
    "4": "Stadtbibliothek Charlottenburg-Wilmersdorf",
}

UNSUPPORTED_LIBRARIES = [
    "Stadtbibliothek Friedrichshain-Kreuzberg",
    "Stadtbibliothek Lichtenberg",
    "Stadtbibliothek Spandau",
    "Stadtbibliothek Treptow-Köpenick",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BibliothekenTermineBerlinBot/1.0; +https://github.com/)"
}


def month_bounds(month_str):
    year, month = (int(x) for x in month_str.split("-"))
    start = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day)
    return start, end


def fmt(d):
    return d.strftime("%d.%m.%Y")


def fetch_page(channel_id, date_start, date_stop, offset):
    params = {
        "c": channel_id,
        "date_start": fmt(date_start),
        "date_stop": fmt(date_stop),
        "ls": offset,
    }
    resp = requests.get(BASE, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


TERMIN_RE = re.compile(r"Termin:\s*([\d.]{10})(?:\s*-\s*[\d.]{10})?(?:\s+(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?)?", re.S)


def parse_events(html, channel_id, library_name):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    seen_links = set()

    detail_links = soup.select('a[href*="detail="]')
    for link_tag in detail_links:
        href = link_tag.get("href", "")
        if "detail=" not in href:
            continue
        full_link = urljoin("https://www.berlin.de", href)
        if full_link in seen_links:
            continue

        title = link_tag.get_text(strip=True)
        if not title:
            continue

        block = link_tag.find_parent(["article", "div", "li"])
        block_text = block.get_text(" ", strip=True) if block else ""

        date_label, time_label = None, None
        m = TERMIN_RE.search(block_text)
        if m:
            date_label = m.group(1)
            if m.group(2) and m.group(3):
                time_label = f"{m.group(2)}–{m.group(3)}"
            elif m.group(2):
                time_label = m.group(2)

        loc_match = re.search(r"Veranstaltungsort:\s*([^:]+?)(?:\s+in\s+\S+)?(?:Zur Veranstaltung|$)", block_text)
        location = loc_match.group(1).strip() if loc_match else ""

        recurring = "Regelmäßige Veranstaltung" in block_text

        if not date_label:
            continue

        try:
            iso_date = datetime.strptime(date_label, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue

        seen_links.add(full_link)
        events.append({
            "title": title,
            "date": iso_date,
            "dateLabel": date_label,
            "time": time_label or "",
            "library": library_name,
            "location": location,
            "desc": "",
            "link": full_link,
            "recurring": recurring,
        })

    return events


def scrape_library(channel_id, library_name, date_start, date_stop):
    all_events = []
    offset = 0
    seen_this_run = set()
    while True:
        html = fetch_page(channel_id, date_start, date_stop, offset)
        page_events = parse_events(html, channel_id, library_name)
        new_events = [e for e in page_events if e["link"] not in seen_this_run]
        if not new_events:
            break
        for e in new_events:
            seen_this_run.add(e["link"])
        all_events.extend(new_events)
        offset += 10
        time.sleep(0.5)
        if offset > 2000:
            break
    return all_events


def main():
    month_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m")
    date_start, date_stop = month_bounds(month_str)

    all_events = []
    for channel_id, library_name in LIBRARIES.items():
        try:
            events = scrape_library(channel_id, library_name, date_start, date_stop)
            print(f"{library_name}: {len(events)} Veranstaltungen")
            all_events.extend(events)
        except Exception as exc:
            print(f"Fehler bei {library_name}: {exc}", file=sys.stderr)

    month_labels_de = ["", "Januar", "Februar", "März", "April", "Mai", "Juni",
                        "Juli", "August", "September", "Oktober", "November", "Dezember"]
    year, month = (int(x) for x in month_str.split("-"))

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "month": month_str,
        "monthLabel": f"{month_labels_de[month]} {year}",
        "libraries": LIBRARIES,
        "unsupportedLibraries": UNSUPPORTED_LIBRARIES,
        "events": all_events,
    }

    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{len(all_events)} Veranstaltungen insgesamt geschrieben nach events.json")


if __name__ == "__main__":
    main()

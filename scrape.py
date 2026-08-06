#!/usr/bin/env python3
"""
Scrapes Veranstaltungen (events) for a set of Berlin Stadtbibliotheken from the
central berlin.de event calendar (berlin.de/land/kalender) and writes events.json.

By default this scrapes the CURRENT month plus the following two months (three
months total). Each supported library has a fixed "c" (channel) id in that
calendar system. Not all 12 Berlin districts have their own channel id yet --
see UNSUPPORTED_LIBRARIES.

Usage:
    python scrape.py                # current month + next 2 months
    python scrape.py 2026-09        # 2026-09, 2026-10, 2026-11
    python scrape.py 2026-09 1      # only 2026-09 (second arg = number of months)
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

MONTH_LABELS_DE = ["", "Januar", "Februar", "März", "April", "Mai", "Juni",
                    "Juli", "August", "September", "Oktober", "November", "Dezember"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BibliothekenTermineBerlinBot/1.0; +https://github.com/)"
}


def add_months(year, month, delta):
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


def month_bounds(year, month):
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


def parse_events(html, library_name):
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


def scrape_library_month(channel_id, library_name, date_start, date_stop):
    all_events = []
    offset = 0
    seen_this_run = set()
    while True:
        html = fetch_page(channel_id, date_start, date_stop, offset)
        page_events = parse_events(html, library_name)
        new_events = [e for e in page_events if e["link"] not in seen_this_run]
        if not new_events:
            break
        for e in new_events:
            seen_this_run.add(e["link"])
        all_events.extend(new_events)
        offset += 10
        time.sleep(0.4)
        if offset > 2000:
            break
    return all_events


def main():
    if len(sys.argv) > 1:
        year, month = (int(x) for x in sys.argv[1].split("-"))
    else:
        now = datetime.now()
        year, month = now.year, now.month

    num_months = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    months = []
    for i in range(num_months):
        y, m = add_months(year, month, i)
        months.append((y, m))

    all_events = []
    for y, m in months:
        date_start, date_stop = month_bounds(y, m)
        month_str = f"{y:04d}-{m:02d}"
        print(f"\n=== Monat {month_str} ===")
        for channel_id, library_name in LIBRARIES.items():
            try:
                events = scrape_library_month(channel_id, library_name, date_start, date_stop)
                print(f"{library_name}: {len(events)} Veranstaltungen")
                all_events.extend(events)
            except Exception as exc:
                print(f"Fehler bei {library_name} ({month_str}): {exc}", file=sys.stderr)

    month_strs = [f"{y:04d}-{m:02d}" for y, m in months]
    month_labels = {f"{y:04d}-{m:02d}": f"{MONTH_LABELS_DE[m]} {y}" for y, m in months}

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "monthsCovered": month_strs,
        "monthLabels": month_labels,
        "defaultMonth": month_strs[0],
        "libraries": LIBRARIES,
        "unsupportedLibraries": UNSUPPORTED_LIBRARIES,
        "events": all_events,
    }

    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{len(all_events)} Veranstaltungen insgesamt geschrieben nach events.json "
          f"({', '.join(month_strs)})")


if __name__ == "__main__":
    main()

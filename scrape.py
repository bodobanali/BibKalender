#!/usr/bin/env python3
"""
Scrapes Veranstaltungen (events) for a set of Berlin Stadtbibliotheken from the
central berlin.de event calendar (berlin.de/land/kalender) and writes events.json.

By default this scrapes the CURRENT month plus the following month (two
months total). Each supported library has a fixed "c" (channel) id in that
calendar system. Not all 12 Berlin districts have their own channel id yet --
see UNSUPPORTED_LIBRARIES.

Usage:
    python scrape.py                # current month + next month
    python scrape.py 2026-09        # 2026-09 + 2026-10
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

REQUEST_DELAY_SECONDS = 5

def make_session():
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=5,  # 5s, 10s -- give up quickly rather than stall the whole run
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=False,  # berlin.de's Retry-After is too short to be useful here
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = make_session()


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
    resp = SESSION.get(BASE, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


LOCATION_SUFFIX_RE = re.compile(r"\s+in\s+[\w\-]+$")


def parse_events(html, library_name):
    """Parses one results page of berlin.de/land/kalender.

    Structure (verified against live HTML, August 2026):
      article.teaser--event
        h3.title > a.js-ems-event-teaser-heading   (title + link)
        div.teaser__meta .categories a              (category tags, incl. "Regelmäßige Veranstaltung")
        dl.attributes
          dt "Termin:" / dd (span.date, span.time)
          dt "Veranstaltungsort:" / dd (location text, e.g. "X-Bibliothek in Neukölln")
    """
    soup = BeautifulSoup(html, "html.parser")
    events = []

    for article in soup.select("article.teaser--event"):
        heading = article.select_one("h3.title a")
        if not heading:
            continue
        title = heading.get_text(strip=True)
        href = heading.get("href", "")
        if not title or not href:
            continue
        full_link = urljoin("https://www.berlin.de/land/kalender/", href)

        categories = [a.get_text(strip=True) for a in article.select(".categories a")]
        recurring = any("regelmäßig" in c.lower() for c in categories)

        dl = article.select_one("dl.attributes")
        date_label, time_label, location = None, "", ""
        if dl:
            dts = dl.select("dt")
            dds = dl.select("dd")
            for dt, dd in zip(dts, dds):
                label = dt.get_text(strip=True).lower()
                if label.startswith("termin"):
                    date_span = dd.select_one("span.date")
                    time_span = dd.select_one("span.time")
                    date_label = date_span.get_text(strip=True) if date_span else None
                    if time_span:
                        time_label = time_span.get_text(strip=True).replace(" Uhr", "").replace(" - ", "–")
                elif label.startswith("veranstaltungsort"):
                    location = LOCATION_SUFFIX_RE.sub("", dd.get_text(strip=True))

        if not date_label:
            continue

        try:
            iso_date = datetime.strptime(date_label, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue

        events.append({
            "title": title,
            "date": iso_date,
            "dateLabel": date_label,
            "time": time_label,
            "library": library_name,
            "location": location,
            "desc": "",
            "link": full_link,
            "recurring": recurring,
            "categories": categories,
        })

    return events


def scrape_library_month(channel_id, library_name, date_start, date_stop):
    """Paginates through results. If a page fails (e.g. berlin.de blocks us
    mid-way), keeps whatever events were already collected instead of
    discarding the whole library's results.

    berlin.de sometimes silently drops the date_start/date_stop filter once
    the offset gets high, causing pagination to run away through the
    channel's entire history instead of just the requested month. As a
    safety net, we stop as soon as a page returns events outside the
    requested date range and discard that page's events."""
    all_events = []
    offset = 0
    seen_this_run = set()
    while True:
        try:
            html = fetch_page(channel_id, date_start, date_stop, offset)
        except Exception as exc:
            print(f"  -> Abbruch bei Seite ls={offset} ({exc}); "
                  f"behalte {len(all_events)} bereits gefundene Termine", file=sys.stderr)
            break
        page_events = parse_events(html, library_name)
        new_events = [e for e in page_events if e["link"] not in seen_this_run]
        if not new_events:
            break

        out_of_range = [
            e for e in new_events
            if not (date_start <= datetime.strptime(e["dateLabel"], "%d.%m.%Y") <= date_stop)
        ]
        if out_of_range:
            print(f"  -> Datumsfilter offenbar verloren bei ls={offset} "
                  f"(z.B. {out_of_range[0]['dateLabel']} außerhalb {fmt(date_start)}-{fmt(date_stop)}); "
                  f"breche Paginierung ab, behalte {len(all_events)} Termine", file=sys.stderr)
            break

        for e in new_events:
            seen_this_run.add(e["link"])
        all_events.extend(new_events)
        offset += 10
        time.sleep(REQUEST_DELAY_SECONDS)
        if offset > 500:
            print(f"  -> Sicherheits-Limit (500 Termine) erreicht, breche Paginierung ab", file=sys.stderr)
            break
    return all_events


def main():
    if len(sys.argv) > 1:
        year, month = (int(x) for x in sys.argv[1].split("-"))
    else:
        now = datetime.now()
        year, month = now.year, now.month

    num_months = int(sys.argv[2]) if len(sys.argv) > 2 else 2

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
            time.sleep(3)

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

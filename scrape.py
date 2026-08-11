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

# Some libraries aren't tied to any district's own "c" channel (e.g. the ZLB,
# Berlin's central state library, or branch libraries in districts without
# their own channel) and only show up in the city-wide calendar (c=22),
# filterable by "Veranstaltungsort" (v_ort).
#
# Found by diffing berlin.de's full v_ort venue dropdown (all venues whose
# name contains "Bibliothek") against the v_ort dropdowns scoped to each of
# the 8 district channels above -- any "Bibliothek" venue not already
# reachable through one of those 8 channels is listed here, so that every
# venue with "Bibliothek" in its name ends up scraped. Some venues have two
# near-duplicate entries in berlin.de's own database (e.g. old vs. current
# record for the same physical library) -- both ids are kept so no stray
# events are missed.
VENUES = {
    "53820": "Amerika-Gedenkbibliothek (ZLB)",
    "41945": "Amerika-Gedenkbibliothek (ZLB)",
    "47797": "Berliner Stadtbibliothek (ZLB)",
    "36866": "Anna-Seghers-Bibliothek (Lichtenberg)",
    "38116": "Anton-Saefkow-Bibliothek (Lichtenberg)",
    "38118": "Bodo-Uhse-Bibliothek (Lichtenberg)",
    "38117": "Egon-Erwin-Kisch-Bibliothek (Lichtenberg)",
    "41142": "Bezirkszentralbibliothek Pablo Neruda (Friedrichshain-Kreuzberg)",
    "41959": "Pablo-Neruda-Bibliothek (Friedrichshain-Kreuzberg)",
    "55918": "Familienbibliothek Else Ury (Friedrichshain-Kreuzberg)",
    "41820": "Mittelpunktbibliothek Adalbertstraße (Friedrichshain-Kreuzberg)",
    "49568": "Stadtteilbibliothek Friedrich von Raumer (Friedrichshain-Kreuzberg)",
    "23977": "Stadtbibliothek Spandau (Spandau)",
    "43859": "Stadtteilbibliothek Falkenhagener Feld (Spandau)",
    "44345": "Stadtteilbibliothek Heerstraße (Spandau)",
    "41106": "Mittelpunktbibliothek Köpenick (Treptow-Köpenick)",
    "44054": "Bibliothek Heinrich-von-Kleist (Marzahn-Hellersdorf)",
}
CITY_WIDE_CHANNEL = "22"

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
        allowed_methods=["GET", "POST"],
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


def submit_search(channel_id, date_start, date_stop, venue_id=None, offset=0):
    """Fetches one results page via POST, exactly like the real search form
    on berlin.de does. berlin.de silently ignores date_start/date_stop when
    they're sent as GET query params for dates outside a short near-term
    window -- the actual site form submits them as a POST body to
    index.php?suchmaske&c=<id>.

    Originally we POSTed once (page 0) and then paginated with plain GET
    requests, relying on berlin.de to remember the filter for the session
    (cookie-based). In practice that session-based filter is unreliable and
    silently resets after a couple dozen pages, causing unfiltered/out-of-
    range results to leak in and pagination to be aborted early -- missing
    real events. Re-POSTing the full filter (including date range) on
    *every* page, with the pagination offset passed as "ls" in the URL,
    keeps the filter reliably applied on every single request and avoids
    that drift entirely (verified live: page ls=0/100/200 all stayed
    correctly within the requested date range).

    If venue_id is given, also filters to that specific "Veranstaltungsort"
    (used for libraries that don't have their own district channel, e.g.
    the ZLB, and only show up in the city-wide channel c=22)."""
    url = f"{BASE}?suchmaske&c={channel_id}"
    if offset:
        url += f"&ls={offset}"
    data = {
        "date_start": fmt(date_start),
        "date_stop": fmt(date_stop),
        "stichwort": "",
    }
    if venue_id:
        data["v_ort"] = venue_id
    resp = SESSION.post(url, data=data, headers=HEADERS, timeout=30)
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


def scrape_library_month(channel_id, library_name, date_start, date_stop, venue_id=None):
    """Paginates through results, re-POSTing the full date filter on every
    single page (see submit_search) so the filter can't silently drift or
    reset mid-way through pagination like it did with GET-based pagination.

    Any event that still comes back outside the requested range on a given
    page is simply dropped (not treated as a reason to abort) -- since the
    filter is freshly re-applied every request, an occasional stray
    out-of-range event is far more likely a berlin.de data quirk on that
    one page than a systemic filter loss, so we keep paginating instead of
    giving up and losing real in-range events on later pages. Pagination
    stops once a page yields no new in-range events, or after a generous
    safety cap to avoid ever looping forever."""
    all_events = []
    offset = 0
    seen_this_run = set()

    while True:
        try:
            html = submit_search(channel_id, date_start, date_stop, venue_id=venue_id, offset=offset)
        except Exception as exc:
            print(f"  -> Abbruch bei Seite ls={offset} ({exc}); "
                  f"behalte {len(all_events)} bereits gefundene Termine", file=sys.stderr)
            break

        page_events = parse_events(html, library_name)
        new_events = [e for e in page_events if e["link"] not in seen_this_run]
        if not new_events:
            break

        in_range = [
            e for e in new_events
            if date_start <= datetime.strptime(e["dateLabel"], "%d.%m.%Y") <= date_stop
        ]
        out_of_range_count = len(new_events) - len(in_range)
        if out_of_range_count:
            print(f"  -> {out_of_range_count} Termin(e) außerhalb {fmt(date_start)}-{fmt(date_stop)} "
                  f"bei ls={offset} ignoriert", file=sys.stderr)

        for e in new_events:
            seen_this_run.add(e["link"])
        all_events.extend(in_range)

        if not in_range:
            # A page with only out-of-range events means we've reached the
            # end of the real results for this range.
            break

        offset += 10
        time.sleep(REQUEST_DELAY_SECONDS)
        if offset > 5000:
            print(f"  -> Sicherheits-Limit (5000 Termine) erreicht, breche Paginierung ab", file=sys.stderr)
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

        for venue_id, venue_name in VENUES.items():
            try:
                events = scrape_library_month(
                    CITY_WIDE_CHANNEL, venue_name, date_start, date_stop, venue_id=venue_id
                )
                print(f"{venue_name}: {len(events)} Veranstaltungen")
                all_events.extend(events)
            except Exception as exc:
                print(f"Fehler bei {venue_name} ({month_str}): {exc}", file=sys.stderr)
            time.sleep(3)

    month_strs = [f"{y:04d}-{m:02d}" for y, m in months]
    month_labels = {f"{y:04d}-{m:02d}": f"{MONTH_LABELS_DE[m]} {y}" for y, m in months}

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "monthsCovered": month_strs,
        "monthLabels": month_labels,
        "defaultMonth": month_strs[0],
        "libraries": {**LIBRARIES, **VENUES},
        "unsupportedLibraries": UNSUPPORTED_LIBRARIES,
        "events": all_events,
    }

    with open("events.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{len(all_events)} Veranstaltungen insgesamt geschrieben nach events.json "
          f"({', '.join(month_strs)})")


if __name__ == "__main__":
    main()

# Berliner Bibliotheken – Veranstaltungen

Zeigt Veranstaltungen von aktuell 8 Berliner Bibliothekssystemen an, gezogen aus dem
zentralen Veranstaltungskalender von berlin.de. Ansicht nach Datum oder nach Bibliothek,
mit Kennzeichnung wiederkehrender Angebote.

## Struktur

- `index.html` – die eigentliche Seite, lädt `events.json` und rendert die Ansicht
- `events.json` – die Daten (wird automatisch überschrieben)
- `scrape.py` – Python-Skript, das berlin.de abfragt und `events.json` neu schreibt
- `.github/workflows/update-events.yml` – GitHub-Actions-Workflow, der `scrape.py`
  automatisch laufen lässt (täglich 5:00 UTC) und die aktualisierte `events.json` commitet

## Erste Schritte (lokal hochladen)

```bash
git init
git add .
git commit -m "Erste Version"
git branch -M main
git remote add origin https://github.com/bodobanali/BibliothekenTermineBerlin.git
git push -u origin main
```

## GitHub Pages aktivieren

Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, Ordner `/ (root)`.
Danach ist die Seite unter `https://bodobanali.github.io/BibliothekenTermineBerlin/` erreichbar.

**Wichtig:** Damit auch Personen ohne GitHub-Zugriff die Seite öffnen können, muss das
Repository auf **Public** gestellt werden (Settings → General → Danger Zone → Change visibility).
Bei einem privaten Repo ist die Pages-Seite nur für Personen mit Repo-Zugriff sichtbar.

## Manuell aktualisieren

```bash
pip install requests beautifulsoup4
python scrape.py            # aktueller Monat
python scrape.py 2026-09    # bestimmter Monat
```

Der GitHub-Actions-Workflow lässt sich auch manuell anstoßen: Actions → "Update library
events" → "Run workflow", optional mit gewünschtem Monat (`YYYY-MM`).

## Bekannte Einschränkungen

- **4 Bezirke fehlen noch**: Friedrichshain-Kreuzberg, Lichtenberg, Spandau und
  Treptow-Köpenick haben (Stand jetzt) keine eigene, feste Kalender-Kategorie-ID im
  zentralen berlin.de-Kalender gefunden. Sie müssten über eine andere Methode
  (z. B. Venue-Filter oder eigene Programmseiten) ergänzt werden.
- **`scrape.py` ist ungetestet in Produktion**: Der HTML-Aufbau von berlin.de wurde aus
  bereits umgewandelten Textauszügen abgeleitet, nicht aus dem rohen HTML. Beim ersten
  echten Lauf (z. B. via `workflow_dispatch`) lohnt sich ein Blick ins Actions-Log bzw.
  in die entstandene `events.json`, um zu prüfen, ob Titel/Datum/Ort korrekt erkannt werden.
- **Beschreibungstexte** werden aktuell nicht mitgescraped (`desc` bleibt leer) – ließe
  sich ergänzen, indem `scrape.py` zusätzlich die Detailseite jedes Termins abruft.
- **Monatsauswahl im Frontend** ist vorbereitet, aber noch nicht interaktiv: aktuell
  zeigt die Seite immer den Monat, der zuletzt gescraped wurde.

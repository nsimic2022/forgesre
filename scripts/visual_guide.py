#!/usr/bin/env python3
"""Build the visual guide: screenshots of each page with numbered, coloured markers.

Opens every page of the UI in a headless browser, puts a numbered red circle on the
corner of each element described in PAGES, saves docs/guide/<page>.png and writes
docs/visual-guide.md (English) and docs/vizuelni-vodic-sr.md (Serbian) with one line per
number. Run it against a demo or lab install after a UI change, then commit the result:

    pip install playwright && playwright install chromium
    python3 scripts/visual_guide.py --base http://127.0.0.1:8080 --email admin@x --password ...
    python3 scripts/visual_guide.py --only noc tv --check     # list selectors that match nothing

A maintainer tool like scripts/screenshots.py, not part of the appliance. An element that is not
on the page (no data, feature off) is skipped with a warning, so the numbers stay in order.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT, MAX_HEIGHT = 1440, 900, 2400
COLORS = ["#ff1744"]  # one red for every mark, so only the number tells the order


def card(title: str) -> str:
    """Selector of a Card (section.card) whose heading contains ``title``."""
    return f"section.card:has(h2:has-text('{title}'))"


@dataclass
class Step:
    selector: str
    en: str
    sr: str


@dataclass
class Page:
    key: str
    path: str
    title_en: str
    title_sr: str
    intro_en: str
    intro_sr: str
    steps: list[Step]
    click: str = ""
    full: bool = True


PAGES: list[Page] = [
    Page("noc", "/app/", "NOC overview", "NOC pregled",
         "The start page: what is burning now, who is on call and whether the hosts answer. It refreshes by itself.",
         "Početna strana: šta gori sada, ko je dežuran i da li hostovi odgovaraju. Sama se osvežava.", [
             Step(".nav-links", "Main menu: move between pages; the dot shows where you are. Ctrl+K searches pages, incidents and hosts.",
                  "Glavni meni: prelaz između stranica; tačka pokazuje gde si. Ctrl+K pretražuje stranice, incidente i hostove."),
             Step(".strip", "Today's numbers: open incidents, not acknowledged, hosts down, time to acknowledge and to resolve.",
                  "Brojke za danas: otvoreni incidenti, nepotvrđeni, hostovi koji ne rade, vreme do potvrde i do rešenja."),
             Step("text=Wall screen (TV mode)", "Opens the wall screen for a TV in the NOC room.", "Otvara zidni ekran za TV u NOC sobi."),
             Step("[role=tablist] >> nth=0", "Tabs: not acknowledged, acknowledged, resolved today. The number is how many.",
                  "Tabovi: nepotvrđeni, potvrđeni, rešeni danas. Broj pokazuje koliko ih ima."),
             Step("button:has-text('All severities') >> xpath=..", "Filter by severity, or type a host, IP or incident number.",
                  "Filter po ozbiljnosti, ili ukucaj host, IP ili broj incidenta."),
             Step("tbody tr >> nth=0 >> td.sev-cell", "The incident name, coloured by severity (red critical, orange high, yellow warning). Click it to open the incident.",
                  "Ime incidenta, obojeno po ozbiljnosti (crveno kritično, narandžasto visoko, žuto upozorenje). Klik otvara incident."),
             Step("tbody tr >> nth=0 >> text=Not acknow", "Acknow.: Not acknow means nobody has taken it yet; acknowledge it so escalation stops. Who acknowledged it is on the incident page.",
                  "Acknow.: Not acknow znači da ga niko još nije preuzeo; potvrdi ga da eskalacija stane. Ko ga je potvrdio piše na stranici incidenta."),
             Step("tbody tr >> nth=0 >> input[type=checkbox]", "Tick rows to acknowledge, assign or resolve several at once.",
                  "Čekiraj redove da potvrdiš, dodeliš ili rešiš više odjednom."),
             Step(card("Shift handover"), "Shift handover: write what the next shift must know.", "Primopredaja smene: napiši šta sledeća smena treba da zna."),
             Step(card("Health by group"), "Hosts by group: green when all answer.", "Hostovi po grupama: zeleno kad svi odgovaraju."),
             Step(card("On call now"), "Who is on call right now; Schedule opens the roster.", "Ko je dežuran sada; Schedule otvara raspored."),
             Step(".nav-box", "ForgeRCA (rules, always on) and ForgeAI (the local model) status.", "Stanje ForgeRCA (pravila, uvek uključen) i ForgeAI (lokalni model)."),
             Step(".nav-user", "Who is signed in, the role, and Sign out.", "Ko je prijavljen, uloga i odjava (Sign out)."),
         ]),
    Page("tv", "/app/tv", "Wall screen (TV mode)", "Zidni ekran (TV mod)",
         "One screen for the TV on the wall. Sign it in with a Viewer account. Refreshes every 30 s.",
         "Jedan ekran za TV na zidu. Prijavi ga Viewer nalogom. Osvežava se na 30 s.", [
             Step(".tvw-top", "Time, open, not acknowledged, critical, hosts down, who is on call, and Live (red No updates when the screen stopped refreshing).",
                  "Vreme, otvoreni, nepotvrđeni, kritični, hostovi koji ne rade, ko je dežuran i Live (crveno No updates kad ekran prestane da se osvežava)."),
             Step(".tvw-burn", "Burning now: critical first, unacknowledged before acknowledged. More than eight are paged every 15 s.",
                  "Burning now: prvo kritični, nepotvrđeni pre potvrđenih. Više od osam se lista po stranama na 15 s."),
             Step(".tvw-row >> nth=0 >> .tvw-row-title", "Incident name in its severity colour.", "Ime incidenta u boji ozbiljnosti."),
             Step(".tvw-row >> nth=0 >> .tvw-row-ack", "Who acknowledged it, or red not acked.", "Ko ga je potvrdio, ili crveno not acked."),
             Step(".tvw-week", "The last 7 days like a weather forecast. Click a day to see its clock: ‹ AM left, PM › right.",
                  "Poslednjih 7 dana kao vremenska prognoza. Klik na dan prikazuje njegov sat: ‹ AM levo, PM › desno."),
             Step(".tvw-ring", "The last 12 hours on a clock: each hour stacked by severity; the hand is now.",
                  "Poslednjih 12 sati na satu: svaki sat složen po ozbiljnosti; kazaljka je sada."),
             Step(".tvw-hosts", "Host groups: green all up, red with up/total.", "Grupe hostova: zeleno svi rade, crveno sa radi/ukupno."),
             Step(".tvw-strip", "Last 24 hours: the line is open incidents, the bars are new ones by severity with the count under them.",
                  "Poslednja 24 sata: linija su otvoreni incidenti, stubići su novi po ozbiljnosti sa brojem ispod."),
             Step(".tvw-minis", "Small tiles: acknowledge and resolve times, backup, noisiest asset, ForgeAI, hosts, new devices; Shift at the far right.",
                  "Male pločice: vreme potvrde i rešenja, backup, najbučniji uređaj, ForgeAI, hostovi, novi uređaji; Shift skroz desno."),
             Step(".tvw-exit", "Back to the normal UI.", "Povratak u običan prikaz."),
         ], full=False),
    Page("incidents", "/app/incidents", "Incidents", "Incidenti",
         "Every incident, newest first, with filters, saved views and CSV export.",
         "Svi incidenti, najnoviji prvi, sa filterima, sačuvanim prikazima i CSV izvozom.", [
             Step("main >> text=On-call >> xpath=..", "Incidents and On-call (the roster).", "Incidenti i On-call (raspored dežurstava)."),
             Step("main >> text=/^All \\d+$/ >> xpath=..", "By state: not acknowledged, acknowledged, resolved, all.", "Po stanju: nepotvrđeni, potvrđeni, rešeni, svi."),
             Step("button:has-text('All severities') >> xpath=..", "Severity, search and team filters.", "Filteri: ozbiljnost, pretraga i tim."),
             Step("text=Save view", "Save the current filters as a view to open again later.", "Sačuvaj trenutne filtere kao prikaz za kasnije."),
             Step("text=Export CSV", "Pick dates and export the list as CSV.", "Izaberi datume i izvezi listu u CSV."),
             Step("thead", "Columns: severity, start, host, problem, source, Acknow. (acknowledged), tier, messages sent, ForgeRCA and ForgeAI.",
                  "Kolone: ozbiljnost, početak, host, problem, izvor, potvrda, nivo eskalacije, poslate poruke, ForgeRCA i ForgeAI."),
             Step("tbody tr >> nth=0 >> td.sev-cell", "The incident name in its severity colour; badges show SLA breached, flapping or major.",
                  "Ime incidenta u boji ozbiljnosti; oznake pokazuju probijen SLA, flapping ili major."),
             Step("tbody tr >> nth=0 >> td >> nth=7", "Tier: how far escalation has gone.", "Nivo: dokle je stigla eskalacija."),
         ]),
    Page("incident", "{incident}", "Incident page", "Stranica incidenta",
         "Everything about one incident: what happened, what ForgeRCA thinks, who did what.",
         "Sve o jednom incidentu: šta se desilo, šta misli ForgeRCA, ko je šta uradio.", [
             Step("main h1", "Severity and name; source and when it opened under it.", "Ozbiljnost i ime; ispod izvor i kada je otvoren."),
             Step("button:has-text('Acknowledge') >> xpath=..", "Actions: postmortem, send report, Acknowledge, Escalate, Resolve.",
                  "Akcije: postmortem, slanje izveštaja, Acknowledge (potvrdi), Escalate (eskaliraj), Resolve (reši)."),
             Step("text=Declare major incident", "Declare a major incident: a commander and regular updates.", "Proglasi veliki incident: komandant i redovna obaveštenja."),
             Step(card("ForgeRCA"), "ForgeRCA: facts collected, hypotheses with weights, what to do next.", "ForgeRCA: prikupljene činjenice, hipoteze sa težinama, šta dalje."),
             Step(card("ForgeAI"), "ForgeAI: the local model's opinion (when it is on).", "ForgeAI: mišljenje lokalnog modela (kad je uključen)."),
             Step(card("Who did what"), "Timeline: alarm, notifications, acknowledgements and notes, with the time.",
                  "Hronologija: alarm, obaveštenja, potvrde i beleške, sa vremenom."),
             Step(card("Evidence"), "Evidence captured when it opened: alert, metrics, history.", "Dokazi prikupljeni pri otvaranju: alarm, metrike, istorija."),
             Step(card("Who to call"), "Who to call for this asset.", "Koga zvati za ovaj uređaj."),
             Step(card("Confirm the cause"), "After the fix: write the real cause and rate ForgeRCA.", "Posle popravke: upiši pravi uzrok i oceni ForgeRCA."),
             Step(card("Follow-up actions"), "Follow-up actions with an owner and a date.", "Naknadne akcije sa vlasnikom i rokom."),
             Step(card("Notes"), "Notes for the team.", "Beleške za tim."),
         ]),
    Page("oncall", "/app/incidents/on-call", "On-call", "Dežurstva",
         "Who answers, every day of the week, in the appliance's time zone.", "Ko odgovara, svaki dan u nedelji, po vremenu servera.", [
             Step(card("On call now"), "Who is on call this minute.", "Ko je dežuran ovog minuta."),
             Step("text=Take the rest of today", "Take the rest of today yourself.", "Preuzmi ostatak dana na sebe."),
             Step("text=New window", "Add a window: who, which days, from and to.", "Dodaj termin: ko, koji dani, od i do."),
             Step(card("Week"), "The week: click a window to change it.", "Nedelja: klik na termin da ga izmeniš."),
         ], full=False),
    Page("inventory", "/app/inventory", "Inventory", "Inventar",
         "One record per asset, with state, location and owner.", "Jedan zapis po uređaju, sa stanjem, lokacijom i vlasnikom.", [
             Step("text=Add devices", "Add devices: scan a range or type them in.", "Dodaj uređaje: skeniraj opseg ili ih upiši."),
             Step("text=Dependency map >> xpath=..", "Dependency map, racks, sources, new asset, CSV import, list or tree, export.",
                  "Mapa zavisnosti, rekovi, izvori, novi uređaj, uvoz CSV, lista ili stablo, izvoz."),
             Step("main aside, main nav >> nth=0", "Groups: by location, type, environment, and ready views such as Down now.",
                  "Grupe: po lokaciji, tipu, okruženju, i gotovi prikazi kao Down now."),
             Step("input[placeholder^='Hostname']", "Search by hostname, IP, owner, rack or customer.", "Pretraga po imenu, IP-u, vlasniku, reku ili korisniku."),
             Step("tbody tr >> nth=0 >> td >> nth=1", "State: Alarm (open incident), Down, Up or Not probed.", "Stanje: Alarm (otvoren incident), Down, Up ili Not probed."),
             Step("tbody tr >> nth=0 >> td >> nth=2", "Hostname: click it to open the asset.", "Ime: klik otvara uređaj."),
             Step(".pager, nav[aria-label*='page' i] >> nth=0", "Pages and rows per page.", "Strane i broj redova po strani."),
         ], full=False),
    Page("automation", "/app/automation", "Automation", "Automatizacija",
         "What happens between an alarm and someone's phone, in four steps.", "Šta se dešava između alarma i nečijeg telefona, u četiri koraka.", [
             Step("text=1 · Collect >> xpath=../..", "The pipeline: collect, filter noise, route, escalate and notify, with today's numbers.",
                  "Tok: prikupljanje, filtriranje šuma, rutiranje, eskalacija i obaveštenja, sa današnjim brojkama."),
             Step("main [role=tablist] >> nth=0", "Tabs: noise filters, routing rules, escalation, playbooks, maintenance, known errors, ForgeAI skills.",
                  "Tabovi: filteri šuma, pravila rutiranja, eskalacija, playbook-ovi, održavanje, poznate greške, ForgeAI veštine."),
             Step("text=New filter", "New noise filter: who is not woken up, and when.", "Novi filter šuma: koga ne budimo i kada."),
             Step(card("Try an alarm"), "Try an alarm: see what would happen, without creating anything.", "Probaj alarm: vidi šta bi se desilo, bez pravljenja ičega."),
             Step("text=Export (JSON)", "Export all rules as JSON.", "Izvezi sva pravila kao JSON."),
         ], full=False),
    Page("investigations", "/app/investigations", "Investigations", "Istrage",
         "Resolved incidents with their confirmed cause, how well ForgeRCA and ForgeAI did, and Ask ForgeAI.",
         "Rešeni incidenti sa potvrđenim uzrokom, koliko su ForgeRCA i ForgeAI bili tačni, i Ask ForgeAI.", [
             Step("button:has-text('Last 90 days') >> xpath=..", "Search by host, alert or cause; period; only with ForgeAI or only with a confirmed cause.",
                  "Pretraga po hostu, alarmu ili uzroku; period; samo sa ForgeAI ili samo sa potvrđenim uzrokom."),
             Step("tbody tr >> nth=0 >> td.sev-cell", "The problem in its severity colour.", "Problem u boji ozbiljnosti."),
             Step("tbody tr >> nth=0 >> td >> nth=5", "The confirmed cause, or not reviewed.", "Potvrđeni uzrok, ili not reviewed."),
             Step(card("Accuracy"), "How often ForgeRCA and ForgeAI were right.", "Koliko često su ForgeRCA i ForgeAI bili u pravu."),
             Step(card("Recurring causes"), "Causes that keep coming back, and on which hosts.", "Uzroci koji se ponavljaju, i na kojim hostovima."),
             Step("h2:has-text('Ask ForgeAI') >> xpath=..", "Ask ForgeAI: chat about anything or one incident (local model).", "Ask ForgeAI: razgovor o bilo čemu ili o jednom incidentu (lokalni model)."),
         ]),
    Page("reports", "/app/reports", "Reports", "Izveštaji",
         "Numbers for a period against the period before, charts and scheduled reports.", "Brojke za period u odnosu na prethodni, grafikoni i zakazani izveštaji.", [
             Step("button:has-text('Last 30 days') >> xpath=..", "Period (7, 30, 90 days) and CSV export.", "Period (7, 30, 90 dana) i CSV izvoz."),
             Step(".strip", "Incidents, criticals, MTTA, MTTR and confirmed causes, with the change against the period before.",
                  "Incidenti, kritični, MTTA, MTTR i potvrđeni uzroci, sa promenom u odnosu na prethodni period."),
             Step(card("SLOs"), "SLOs and error budget.", "SLO-ovi i budžet grešaka."),
             Step("[aria-label='Chart range']", "Switch the chart: Today (per hour), 7 days, 4 weeks (per week), 30 days.",
                  "Promena grafikona: Today (po satu), 7 days, 4 weeks (po nedelji), 30 days."),
             Step("section.card:has([aria-label='Chart range']) [role=img]", "Bars stacked by severity: red critical, orange high, blue other.",
                  "Stubići složeni po ozbiljnosti: crveno kritično, narandžasto visoko, plavo ostalo."),
             Step(card("Scheduled reports"), "Scheduled reports by mail; Preview shows one before it goes out.", "Zakazani izveštaji mejlom; Preview prikazuje izveštaj pre slanja."),
             Step(card("By asset type"), "Breakdown by asset type.", "Podela po tipu uređaja."),
             Step(card("Noisiest assets"), "Assets with the most incidents.", "Uređaji sa najviše incidenata."),
         ]),
    Page("settings", "/app/settings", "Settings", "Podešavanja",
         "Admins change settings here; others pick a theme and see health.", "Admin ovde menja podešavanja; ostali biraju temu i vide stanje.", [
             Step("nav[aria-label='Settings sections']", "Sections: general, checks, alarms, integrations, notifications, AI, web push, appearance, health, users, backup…",
                  "Sekcije: opšte, provere, alarmi, integracije, obaveštenja, AI, web push, izgled, zdravlje, korisnici, backup…"),
             Step("main section.card >> nth=0", "The settings of the chosen section. Save writes config/forgesre.yml and the audit log.",
                  "Podešavanja izabrane sekcije. Save upisuje config/forgesre.yml i audit log."),
             Step("main section.card >> nth=0 >> button:has-text('Save') >> nth=0", "Save one value.", "Sačuvaj jednu vrednost."),
         ], full=False),
    Page("settings-appearance", "/app/settings?group=appearance", "Settings › Appearance", "Podešavanja › Izgled",
         "Per browser: theme, severity colours, alerts and push.", "Za ovaj browser: tema, boje ozbiljnosti, upozorenja i push.", [
             Step(card("Theme"), "Colour theme for this browser.", "Tema boja za ovaj browser."),
             Step(card("Severity colours"), "Colour the incident name by severity (on or off).", "Boji ime incidenta po ozbiljnosti (uključi ili isključi)."),
             Step(card("Alerts in this browser"), "Sound and desktop notification for a new critical while the page is open.",
                  "Zvuk i obaveštenje na desktopu za novi kritični dok je strana otvorena."),
             Step(card("Push to this device"), "Web push to this phone or browser, even when ForgeSRE is closed.", "Web push na ovaj telefon ili browser, i kad je ForgeSRE zatvoren."),
         ]),
    Page("settings-push", "/app/settings?group=push", "Settings › Web push", "Podešavanja › Web push",
         "Turn web push on for the server, then sign up each phone or browser.", "Uključi web push na serveru, pa prijavi svaki telefon ili browser.", [
             Step("nav[aria-label='Settings sections'] >> text=Web push", "The Web push section.", "Sekcija Web push."),
             Step(card("Push to this device"), "Off: an Admin turns it on with one button. On: tick Push to this device and Send a test.",
                  "Isključen: Admin ga uključuje jednim dugmetom. Uključen: čekiraj Push to this device i Send a test."),
         ], full=False),
]


OVERLAY_JS = """
([boxes]) => {
  document.querySelectorAll('.vg-mark').forEach((n) => n.remove());
  const placed = [];
  for (const b of boxes) {
    // move a circle that would cover an earlier one to the right
    // small targets (a checkbox, one word): the circle sits just left of them, not on top
    const small = b.w < 120 && b.h < 40;
    let bx = small ? Math.max(2, b.x - 38) : Math.max(2, b.x - 14), by = small ? b.y + b.h / 2 - 16 : Math.max(2, b.y - 14);
    while (placed.some(([px, py]) => Math.abs(px - bx) < 36 && Math.abs(py - by) < 36)) bx += 38;
    placed.push([bx, by]);
    const dot = document.createElement('div');
    dot.className = 'vg-mark';
    dot.textContent = String(b.n);
    Object.assign(dot.style, { position: 'absolute', left: bx + 'px', top: by + 'px', width: '32px',
      height: '32px', borderRadius: '50%', background: b.color, color: '#fff', font: '700 17px/32px Inter, sans-serif', textAlign: 'center',
      zIndex: 99999, pointerEvents: 'none', border: '2px solid #fff', boxShadow: '0 2px 6px rgba(0,0,0,.6)', textShadow: '0 1px 2px rgba(0,0,0,.6)' });
    document.body.appendChild(dot);
  }
}
"""


def _markdown(pages: list[tuple[Page, list[tuple[int, Step]]]], lang: str) -> str:
    """One section per page: the picture, then one line per number in the marker's colour."""
    sr = lang == "sr"
    head = ("# Vizuelni vodič\n\nSvaka stranica ForgeSRE-a sa crvenim brojevima; ispod slike piše šta je koji broj.\n"
            "Slike pravi `scripts/visual_guide.py`; engleska verzija: [visual-guide.md](visual-guide.md).\n") if sr else (
            "# Visual guide\n\nEach ForgeSRE page with numbered red circles; under the picture, what each number is.\n"
            "The pictures come from `scripts/visual_guide.py`; Serbian: [vizuelni-vodic-sr.md](vizuelni-vodic-sr.md).\n")
    out = [head]
    out.append("\n".join(f"- [{p.title_sr if sr else p.title_en}](#{p.key})" for p, _ in pages) + "\n")
    for page, steps in pages:
        out.append(f'<a id="{page.key}"></a>\n\n## {page.title_sr if sr else page.title_en}\n\n{page.intro_sr if sr else page.intro_en}\n\n'
                   f"![{page.title_en}](guide/{page.key}.png)\n")
        out.append("\n".join(f"{n}. {s.sr if sr else s.en}" for n, s in steps) + "\n")
    return "\n".join(out)


def main() -> int:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=os.environ.get("FORGESRE_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--email", default=os.environ.get("FORGESRE_EMAIL", ""))
    ap.add_argument("--password", default=os.environ.get("FORGESRE_PASSWORD", ""))
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--chrome", default=os.environ.get("CHROME", ""), help="path to a Chrome/Chromium binary (optional)")
    ap.add_argument("--only", nargs="*", help="page keys to take (default: all)")
    ap.add_argument("--check", action="store_true", help="only list selectors that match nothing")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "guide").mkdir(parents=True, exist_ok=True)
    done: list[tuple[Page, list[tuple[int, Step]]]] = []
    missing = 0
    with sync_playwright() as p:
        launch: dict = {"args": ["--no-sandbox"]}
        if args.chrome:
            launch["executable_path"] = args.chrome
        browser = p.chromium.launch(**launch)
        page = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}).new_page()
        page.goto(f"{args.base}/login")
        page.fill("input[name=email]", args.email)
        page.fill("input[name=password]", args.password)
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")
        noc = json.loads(page.evaluate("fetch('/api/v1/noc').then(r => r.text())"))
        active = (noc.get("incidents") or {}).get("active") or (noc.get("incidents") or {}).get("acknowledged") or []
        incident = f"/app/incidents/{active[0]['number']}" if active else "/app/incidents"
        for spec in PAGES:
            if args.only and spec.key not in args.only:
                continue
            page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
            page.goto(args.base + spec.path.format(incident=incident))
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            height = min(MAX_HEIGHT, page.evaluate("document.documentElement.scrollHeight")) if spec.full else HEIGHT
            page.set_viewport_size({"width": WIDTH, "height": max(HEIGHT, height)})
            page.wait_for_timeout(400)
            boxes, kept = [], []
            for step in spec.steps:
                loc = page.locator(step.selector).first
                box = loc.bounding_box() if loc.count() else None
                if not box or box["width"] < 2:
                    missing += 1
                    print(f"  {spec.key}: nothing matches {step.selector!r}")
                    continue
                n = len(kept) + 1
                kept.append((n, step))
                boxes.append({"x": box["x"], "y": box["y"], "w": box["width"], "h": box["height"], "n": n, "color": COLORS[(n - 1) % len(COLORS)]})
            if args.check:
                continue
            page.evaluate(OVERLAY_JS, [boxes])
            target = out / "guide" / f"{spec.key}.png"
            page.screenshot(path=str(target), clip={"x": 0, "y": 0, "width": WIDTH, "height": max(HEIGHT, height)})
            done.append((spec, kept))
            print(f"wrote {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target} ({len(kept)} marks)")
        browser.close()
    if done and not args.only:
        (out / "visual-guide.md").write_text(_markdown(done, "en"), encoding="utf-8")
        (out / "vizuelni-vodic-sr.md").write_text(_markdown(done, "sr"), encoding="utf-8")
        print("wrote docs/visual-guide.md and docs/vizuelni-vodic-sr.md")
    return 1 if args.check and missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

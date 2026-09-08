#!/usr/bin/env python3
"""
Monitor repertuaru Teatru Ateneum.

Wykrywa pojawienie się nowego miesiąca w repertuarze i wysyła powiadomienie
push (ntfy / Telegram) oraz zgłasza wynik do GitHub Actions.

Sygnał: strona repertuaru renderuje nawigację miesięcy wyłącznie dla miesięcy,
które mają już wgrane spektakle:

    <span class="repertuar-miesiac"><a href="?post_type=events&month=2026-10">październik</a></span>

Pobieramy tę nawigację ze strony "pustego" miesiąca w odległej przyszłości
(~83 kB zamiast ~240 kB strony głównej repertuaru), więc każde sprawdzenie
jest tanie.

Uruchomienie lokalnie:
    python check.py                 # normalne sprawdzenie
    python check.py --test          # wyślij powiadomienie testowe
    python check.py --dry-run       # sprawdź i wypisz, nic nie wysyłaj ani nie zapisuj
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://teatrateneum.pl/"
# Miesiąc gwarantowanie pusty -> lekka strona, ale z pełną nawigacją miesięcy.
NAV_PROBE_URL = BASE + "?post_type=events&month=2099-01"
MONTH_URL = BASE + "?post_type=events&month={month}"
RSS_URL = BASE + "?post_type=events&feed=rss2"

STATE_PATH = Path(__file__).parent / "state" / "state.json"
HEARTBEAT_DAYS = 7  # commit "na życzenie" GitHuba, by nie wyłączył crona

UA = "ateneum-repertuar-watch/1.0 (+https://github.com/)"

PL_MONTHS = {
    1: "styczeń", 2: "luty", 3: "marzec", 4: "kwiecień", 5: "maj", 6: "czerwiec",
    7: "lipiec", 8: "sierpień", 9: "wrzesień", 10: "październik",
    11: "listopad", 12: "grudzień",
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def fetch(url: str, tries: int = 3) -> str:
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "pl,en;q=0.8",
                    "Accept-Encoding": "identity",
                    "Cache-Control": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read()
            return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < tries:
                time.sleep(4 * attempt)
    raise RuntimeError(f"nie udało się pobrać {url}: {last}")


# --------------------------------------------------------------------------- #
# Parsowanie
# --------------------------------------------------------------------------- #

# Podstawowy, precyzyjny wzorzec (kotwiczony w klasie CSS motywu).
NAV_STRICT_RE = re.compile(
    r'class="repertuar-miesiac"[^>]*>\s*<a[^>]+month=(\d{4}-\d{2})',
    re.IGNORECASE,
)
# Zapasowy, gdy motyw się zmieni: dowolny link z parametrem month=.
NAV_LOOSE_RE = re.compile(
    r'post_type=events&(?:amp;|#038;)?month=(\d{4}-\d{2})',
    re.IGNORECASE,
)

SPEKTAKL_RE = re.compile(r'<div class="spektakl"', re.IGNORECASE)
TITLE_RE = re.compile(
    r'class="tytul-spektaklu[^"]*"[^>]*>\s*([^<]+?)\s*<', re.IGNORECASE
)
# Uwaga: w weekendy numer dnia ma dodatkowy atrybut style (kolor czerwony),
# dlatego [^>]* zamiast \s* — inaczej gubimy soboty i niedziele.
DAY_RE = re.compile(
    r'<div class="repertuar-data">.*?<p class="text-left"[^>]*>\s*(\d{1,2})\s*</p>',
    re.IGNORECASE | re.DOTALL,
)


def parse_published_months(html: str) -> tuple[list[str], str]:
    """Zwraca (posortowana lista 'YYYY-MM', tryb parsowania)."""
    months = NAV_STRICT_RE.findall(html)
    mode = "strict"
    if not months:
        months = NAV_LOOSE_RE.findall(html)
        mode = "loose"
    return sorted(set(months)), mode


def summarize_month(html: str) -> dict:
    """Podsumowanie strony jednego miesiąca."""
    titles = []
    for t in TITLE_RE.findall(html):
        t = re.sub(r"\s+", " ", t).strip()
        if t and t not in titles:
            titles.append(t)
    days = sorted({int(d) for d in DAY_RE.findall(html)})
    return {
        "performances": len(SPEKTAKL_RE.findall(html)),
        "titles": titles,
        "days": days,
    }


def parse_rss_latest(xml: str) -> str | None:
    m = re.search(r"<item>.*?<pubDate>([^<]+)</pubDate>", xml, re.DOTALL)
    return m.group(1).strip() if m else None


def month_label(ym: str) -> str:
    try:
        y, m = ym.split("-")
        return f"{PL_MONTHS[int(m)]} {y}"
    except Exception:
        return ym


# --------------------------------------------------------------------------- #
# Powiadomienia
# --------------------------------------------------------------------------- #

def post_json(url: str, payload: dict, headers: dict | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": UA, **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def notify_ntfy(title: str, message: str, url: str, priority: int, tags: list[str]) -> str:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return "ntfy: pominięte (brak NTFY_TOPIC)"
    # `or` a nie domyślna wartość get() — nieustawiona zmienna repozytorium
    # w GitHub Actions przychodzi jako pusty string, nie jako brak zmiennej.
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    token = os.environ.get("NTFY_TOKEN", "").strip()
    # Publikacja przez JSON — jedyny tryb ntfy bezpieczny dla polskich znaków.
    payload = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags,
        "click": url,
        "actions": [{"action": "view", "label": "Otwórz repertuar", "url": url}],
    }
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        post_json(server + "/", payload, headers)
        return f"ntfy: wysłane na {server}/{topic}"
    except Exception as exc:  # noqa: BLE001
        return f"ntfy: BŁĄD {exc}"


def notify_telegram(title: str, message: str, url: str) -> str:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return "telegram: pominięte (brak TELEGRAM_TOKEN/TELEGRAM_CHAT_ID)"
    text = f"*{title}*\n\n{message}\n\n{url}"
    try:
        post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {
                "chat_id": chat,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            },
        )
        return "telegram: wysłane"
    except Exception as exc:  # noqa: BLE001
        return f"telegram: BŁĄD {exc}"


def notify(title: str, message: str, url: str, priority: int = 5,
           tags: list[str] | None = None) -> list[str]:
    tags = tags or ["performing_arts"]
    return [
        notify_ntfy(title, message, url, priority, tags),
        notify_telegram(title, message, url),
    ]


# --------------------------------------------------------------------------- #
# Stan
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            # utf-8-sig: znosi ewentualny BOM, gdy plik był edytowany ręcznie.
            return json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def gh_output(**kwargs) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in kwargs.items():
            value = str(value)
            if "\n" in value:
                # Losowy separator: w wartościach są tytuły spektakli pobrane
                # ze strony, więc stały delimiter dałby się teoretycznie
                # podrobić i wstrzyknąć własne outputy do workflow.
                delim = "EOF_" + secrets.token_hex(12)
                fh.write(f"{key}<<{delim}\n{value}\n{delim}\n")
            else:
                fh.write(f"{key}={value}\n")


def now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Główna logika
# --------------------------------------------------------------------------- #

def run(dry_run: bool = False) -> int:
    state = load_state()
    known = set(state.get("known_months", []))
    log: list[str] = []

    nav_html = fetch(NAV_PROBE_URL)
    published, mode = parse_published_months(nav_html)

    # --- awaria parsera = cichy brak powiadomień. To też trzeba zgłosić. ---
    if not published:
        already = state.get("parser_broken_since")
        log.append("PARSER: nie znaleziono żadnego miesiąca w nawigacji")
        if not already:
            state["parser_broken_since"] = now().isoformat(timespec="seconds")
            if not dry_run:
                log += notify(
                    "⚠️ Monitor Ateneum: strona się zmieniła",
                    "Nie potrafię odczytać listy miesięcy z repertuaru. "
                    "Monitor NIE wykryje teraz publikacji nowego miesiąca — "
                    "trzeba poprawić parser w check.py.",
                    BASE + "?post_type=events",
                    priority=5,
                    tags=["warning"],
                )
            save_state(state)
            gh_output(state_changed="true", parser_broken="true", summary="\n".join(log))
        else:
            gh_output(state_changed="false", parser_broken="true", summary="\n".join(log))
        print("\n".join(log))
        return 1

    if mode == "loose":
        log.append("PARSER: użyto wzorca zapasowego (motyw mógł się zmienić)")

    # Skasowanie flagi awarii musi trafić do commita. Inaczej w repo zostałaby
    # flaga "zepsuty", a kolejna awaria zostałaby uznana za już zgłoszoną
    # i przeszłaby bez alarmu.
    recovered = state.pop("parser_broken_since", None) is not None
    if recovered:
        log.append("PARSER: odczyt znów działa — kasuję flagę awarii.")

    log.append(f"Opublikowane miesiące: {', '.join(published)}")

    # Pierwszy przebieg — tylko zapamiętaj, nie powiadamiaj o istniejącym stanie.
    first_run = "known_months" not in state
    new_months = [m for m in published if m not in known]
    if first_run:
        log.append("Pierwszy przebieg — zapisuję stan wyjściowy, bez powiadomień.")
        new_months = []

    changed = recovered
    notified_about = []

    if new_months:
        changed = True
        for ym in new_months:
            month_url = MONTH_URL.format(month=ym)
            try:
                info = summarize_month(fetch(month_url))
            except Exception as exc:  # noqa: BLE001
                info = {"performances": 0, "titles": [], "days": [], "error": str(exc)}

            label = month_label(ym)
            lines = [f"Teatr Ateneum opublikował repertuar na {label}."]
            if info["performances"]:
                lines.append(f"Spektakli: {info['performances']}")
            if info["days"]:
                lines.append(f"Dni z grą: {info['days'][0]}–{info['days'][-1]}")
            if info["titles"]:
                shown = info["titles"][:12]
                lines.append("Tytuły: " + ", ".join(shown)
                             + (" …" if len(info["titles"]) > len(shown) else ""))
            if info.get("error"):
                lines.append(f"(nie udało się pobrać szczegółów: {info['error']})")
            lines.append("Bilety na premiery i popularne tytuły schodzą najszybciej.")

            message = "\n".join(lines)
            title = f"🎭 Repertuar Ateneum: {label}"
            log.append(f"NOWY MIESIĄC: {ym} ({info['performances']} spektakli)")
            if dry_run:
                log.append(f"--- treść powiadomienia ---\n{title}\n{message}\n---")
            else:
                log += notify(title, message, month_url, priority=5,
                              tags=["performing_arts", "tada"])
            notified_about.append({"month": ym, "label": label, "url": month_url,
                                   "info": info, "message": message})

    # --- Sygnał pomocniczy: RSS (daty wgrania wpisów). Domyślnie wyłączony. ---
    rss_hints = os.environ.get("RSS_HINTS", "").strip().lower() in {"1", "true", "yes"}
    try:
        rss_latest = parse_rss_latest(fetch(RSS_URL))
    except Exception:  # noqa: BLE001
        rss_latest = None
    if rss_latest:
        prev_rss = state.get("rss_latest")
        if prev_rss != rss_latest:
            state["rss_latest"] = rss_latest
            changed = True
            log.append(f"RSS: nowy najświeższy wpis ({rss_latest}, było: {prev_rss})")
            if rss_hints and not first_run and not new_months and not dry_run:
                log += notify(
                    "Ateneum: ruch w repertuarze",
                    f"W kanale RSS pojawił się nowszy wpis ({rss_latest}). "
                    "Nowy miesiąc jeszcze się nie pokazał, ale teatr coś wgrywa.",
                    BASE + "?post_type=events",
                    priority=2,
                    tags=["eyes"],
                )

    state["known_months"] = published

    # Heartbeat: commit co HEARTBEAT_DAYS, żeby GitHub nie wyłączył crona
    # po 60 dniach bez aktywności w repozytorium.
    hb = state.get("last_heartbeat")
    due = True
    if hb:
        try:
            due = now() - datetime.fromisoformat(hb) > timedelta(days=HEARTBEAT_DAYS)
        except ValueError:
            due = True
    if changed or due:
        state["last_heartbeat"] = now().isoformat(timespec="seconds")
        changed = True
        if due and not new_months:
            log.append("Heartbeat — commit podtrzymujący harmonogram.")

    if not dry_run:
        save_state(state)

    gh_output(
        state_changed="true" if changed else "false",
        new_months=",".join(new_months),
        parser_broken="false",
        issue_title=(f"🎭 Repertuar Ateneum: {month_label(new_months[0])}"
                     if new_months else ""),
        issue_body=("\n\n".join(n["message"] + f"\n\n{n['url']}" for n in notified_about)
                    if notified_about else ""),
        summary="\n".join(log),
    )

    print("\n".join(log))
    return 0


def main() -> int:
    args = set(sys.argv[1:])

    if "--test" in args:
        results = notify(
            "🎭 Monitor Ateneum działa",
            "To powiadomienie testowe. Jeśli je widzisz, kanał jest poprawnie "
            "skonfigurowany i dostaniesz sygnał, gdy pojawi się nowy miesiąc "
            "repertuaru.",
            BASE + "?post_type=events",
            priority=4,
            tags=["white_check_mark"],
        )
        print("\n".join(results))
        return 0 if all("BŁĄD" not in r for r in results) else 1

    return run(dry_run="--dry-run" in args)


if __name__ == "__main__":
    sys.exit(main())

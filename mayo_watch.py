#!/usr/bin/env python3
"""Tour-slot watcher for Kewpie Mayo Terrace (Tokyo).

Read-only: it loads the public calendar and sends push notifications. It never
opens a slot's booking link; that link is only passed along in the alert.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
DEFAULT_DATES = ["2026-11-30", "2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"]
CALENDAR_URL = "https://reservation.kewpie.co.jp/reservations/calendar?label_id=57&visit=1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Only load the booking site itself: no trackers, images or fonts.
ALLOWED_HOST = re.compile(r"(^|\.)kewpie\.co\.jp$")
BLOCKED_RESOURCES = {"image", "media", "font"}
DEFAULT_INTERVAL = 900
REALERT_AFTER = timedelta(hours=2)
FAILURES_BEFORE_ALERT = 4
MAX_WEEK_CLICKS = 12
MAX_MESSAGE_LINES = 40  # ntfy turns bodies over 4096 bytes into attachments
GOTO_TIMEOUT_MS = 60_000
SELECTOR_TIMEOUT_MS = 30_000
WEEK_CHANGE_TIMEOUT_MS = 20_000
READ_RETRY_SECONDS = 15

PLACES_LEFT = re.compile(r"残り\s*(\d+)\s*人")
# Everything here means "not bookable". Other text is logged in case the site's
# wording changed, since a reworded "places left" would otherwise be missed.
KNOWN_CLOSED = {"", "not open", "受付前", "満席", "開催なし", "休館日", "休業日"}

READ_WEEK_JS = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  return {
    headers: [...document.querySelectorAll('#timeTable thead th span')].map(e => clean(e.innerText)),
    rows: [...document.querySelectorAll('#timeTable tbody tr')].map(tr => {
      const name = tr.querySelector('.table_reserve_name');
      return {
        tour: name ? clean(name.innerText) : '',
        cells: [...tr.querySelectorAll('.calender_list')].map(c => ({
          timestamp: c.getAttribute('data-usage-timestamp'),
          url: c.getAttribute('data-url'),
          text: clean(c.innerText),
        })),
      };
    }),
  };
}"""


class CheckError(Exception):
    """The calendar could not be read, or was not the expected shape."""


@dataclass(frozen=True)
class Slot:
    tour: str
    timestamp: str  # "2026/12/01 10:30"
    status: str
    left: int | None  # places left; None when the status isn't a count
    url: str

    @property
    def iso_date(self) -> str:
        return self.timestamp[:10].replace("/", "-")

    @property
    def key(self) -> str:
        return f"{self.timestamp}|{self.tour}"

    @property
    def is_english(self) -> bool:
        return "english" in self.tour.lower()


# ---------------------------------------------------------------- parsing

def parse_status(text: str) -> int | None:
    match = PLACES_LEFT.search(text or "")
    return int(match[1]) if match else None


def header_date(text: str, today: date) -> date | None:
    """'12/01(火)' -> a date. A month earlier than this month means next year."""
    match = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if not match:
        return None
    month, day = int(match[1]), int(match[2])
    return date(today.year + (month < today.month), month, day)


def slots_from_week(week: dict) -> list[Slot]:
    slots = []
    for row in week["rows"]:
        for cell in row["cells"]:
            timestamp = cell.get("timestamp") or ""
            if not re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", timestamp):
                continue
            url = urllib.parse.urljoin(CALENDAR_URL, cell["url"]) if cell.get("url") else CALENDAR_URL
            text = cell.get("text") or ""
            slots.append(Slot(row["tour"], timestamp, text, parse_status(text), url))
    return slots


def week_is_consistent(week: dict) -> bool:
    """True once the table body belongs to the week shown in the header."""
    shown = {m[0] for m in (re.search(r"\d{1,2}/\d{1,2}", h) for h in week["headers"]) if m}
    shown = {"/".join(f"{int(p):02d}" for p in s.split("/")) for s in shown}
    slots = slots_from_week(week)
    return bool(slots) and all(s.timestamp[5:10] in shown for s in slots)


def select_slots(slots: list[Slot], dates: set[str], any_tour: bool) -> list[Slot]:
    return [s for s in slots if s.iso_date in dates and (any_tour or s.is_english)]


def find_hits(slots: list[Slot], people: int) -> list[Slot]:
    return [s for s in slots if s.left is not None and s.left >= people]


def unknown_statuses(slots: list[Slot]) -> set[str]:
    return {s.status for s in slots
            if s.left is None and s.status.strip().lower() not in KNOWN_CLOSED}


# ---------------------------------------------------------------- site access

def read_week(page) -> dict:
    deadline = time.monotonic() + READ_RETRY_SECONDS
    while True:
        week = page.evaluate(READ_WEEK_JS)
        if week_is_consistent(week):
            return week
        if time.monotonic() >= deadline:
            raise CheckError(f"table not readable (headers {week['headers'][:2]}, "
                             f"{len(week['rows'])} rows, no matching slot cells)")
        page.wait_for_timeout(500)


def walk_calendar(page, dates: set[str], today: date) -> tuple[list[Slot], str | None]:
    """Page through the weeks. Returns what was read, plus any error, so seats
    found before a failure still produce an alert."""
    slots: list[Slot] = []
    last = date.fromisoformat(max(dates))
    try:
        page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        page.wait_for_selector("#timeTable thead th span", timeout=SELECTOR_TIMEOUT_MS)
        for clicks in range(MAX_WEEK_CLICKS + 1):
            week = read_week(page)
            slots.extend(slots_from_week(week))
            shown = [d for d in (header_date(h, today) for h in week["headers"]) if d]
            if not shown:
                raise CheckError(f"no dates in headers: {week['headers'][:3]}")
            if max(shown) >= last:
                return slots, None
            if clicks == MAX_WEEK_CLICKS:
                break
            time.sleep(random.uniform(1, 3))
            first = page.locator("#timeTable thead th span").first.inner_text()
            page.locator("#next").first.click(timeout=SELECTOR_TIMEOUT_MS)
            page.wait_for_function(
                "t => { const e = document.querySelector('#timeTable thead th span');"
                " return e && e.innerText !== t; }",
                arg=first, timeout=WEEK_CHANGE_TIMEOUT_MS,
            )
        return slots, f"did not reach {last} after {MAX_WEEK_CLICKS} clicks"
    except Exception as exc:
        text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return slots, text


def fetch_slots(dates: set[str], today: date, show: bool = False) -> tuple[list[Slot], str | None]:
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not show)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT, locale="ja-JP", timezone_id="Asia/Tokyo",
                )
                page = context.new_page()

                def filter_requests(route):
                    request = route.request
                    host = urllib.parse.urlsplit(request.url).hostname or ""
                    if ALLOWED_HOST.search(host) and request.resource_type not in BLOCKED_RESOURCES:
                        route.continue_()
                    else:
                        route.abort()

                page.route("**/*", filter_requests)
                return walk_calendar(page, dates, today)
            finally:
                browser.close()
    except Exception as exc:
        return [], f"browser error: {exc}"


# ---------------------------------------------------------------- alerts

def nice_time(slot: Slot) -> str:
    moment = datetime.strptime(slot.timestamp, "%Y/%m/%d %H:%M")
    return f"{moment:%a} {moment.day} {moment:%b} {moment:%H:%M}"


def describe(slot: Slot) -> str:
    text = f"{nice_time(slot)} ({slot.left} left)"
    return text if slot.is_english else f"{text} [{slot.tour}]"


def build_message(hits: list[Slot], people: int) -> str:
    kind = "English tour" if all(h.is_english for h in hits) else "tour"
    noun = "place" if people == 1 else "places"
    shown = hits[:MAX_MESSAGE_LINES]
    lines = [describe(hit) for hit in shown]
    lines[0] = f"Mayo Terrace {kind}: {people} {noun} — {lines[0]}"
    if len(hits) > len(shown):
        lines.append(f"+{len(hits) - len(shown)} more on the calendar")
    return "\n".join(lines)


def send_ntfy(topic: str, message: str, click: str | None = None,
              actions: str | None = None, tags: str = "calendar") -> None:
    headers = {"Title": "Mayo Terrace", "Priority": "high", "Tags": tags}
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = actions
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise OSError(f"ntfy returned HTTP {response.status}")


def alert_links(hits: list[Slot]) -> tuple[str, str]:
    """Tap opens the first hit's slot; buttons cover up to 3 slots in the alert."""
    actions = "; ".join(f'view, "{nice_time(h)}", "{h.url}"' for h in hits[:3])
    return hits[0].url, actions


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        state = {}
    except ValueError as exc:
        print(f"WARNING: ignoring unreadable state file: {exc}")
        state = {}
    return {
        "active": state.get("active") or {},  # slot key -> last alert time (UTC ISO)
        "consecutive_failures": state.get("consecutive_failures") or 0,
        "broken_alert_sent": bool(state.get("broken_alert_sent")),
    }


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def due_for_alert(hits: list[Slot], active: dict, now: datetime) -> list[Slot]:
    """Hits that are newly free, or whose last alert is REALERT_AFTER old."""
    return [
        hit for hit in hits
        if hit.key not in active
        or now - datetime.fromisoformat(active[hit.key]) >= REALERT_AFTER
    ]


def run_url() -> str | None:
    server, repo, run = (os.environ.get(k) for k in
                         ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else None


# ---------------------------------------------------------------- one check

def print_summary(slots: list[Slot]) -> None:
    by_tour: dict[str, list[Slot]] = {}
    for slot in sorted(slots, key=lambda s: s.timestamp):
        by_tour.setdefault(slot.tour, []).append(slot)
    for tour, tour_slots in by_tour.items():
        statuses = ", ".join(f"{s.timestamp[5:]} {s.status or '(empty)'}" for s in tour_slots)
        print(f"{tour}: {statuses}")


def run_once(dates: set[str], people: int, any_tour: bool, state_path: Path, topic: str | None,
             dry_run: bool, show: bool = False, fetch=None, send=send_ntfy,
             now: datetime | None = None) -> int:
    """Run one check. Returns a process exit code."""
    now = now or datetime.now(timezone.utc)
    state = load_state(state_path)
    today = now.astimezone(JST).date()
    fetched, error = fetch(dates, today) if fetch else fetch_slots(dates, today, show)

    # De-duplicate in case a week is ever read twice.
    wanted = sorted({s.key: s for s in select_slots(fetched, dates, any_tour)}.values(),
                    key=lambda s: (s.timestamp, s.tour))
    if not error and not wanted:
        error = f"zero slots parsed for the watched dates ({len(fetched)} slots in total)"

    print_summary(wanted)
    for status in sorted(unknown_statuses(wanted)):
        print(f"WARNING: unrecognised slot status treated as not bookable: {status!r}")
    if error:
        print(f"CHECK FAILED: {error}")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{error}")

    hits = find_hits(wanted, people)
    print(f"Parsed {len(wanted)} slots. Hits for {people}+ people: {len(hits)}")
    for hit in hits:
        print(f"  {describe(hit)}")

    exit_code = 0

    # Seat alert. Any due hit sends the whole current list in one message.
    due = due_for_alert(hits, state["active"], now)
    alerted = False
    if due:
        message = build_message(hits, people)
        click, actions = alert_links(hits)
        if dry_run:
            print(f"[dry run] would alert (tap opens {click}):\n{message}")
        else:
            try:
                send(topic, message, click=click, actions=actions)
                alerted = True
                print(f"Alert sent ({len(due)} new or re-alerted of {len(hits)} hits)")
            except Exception as exc:
                print(f"ERROR: could not send alert, will retry next run: {exc}")
                exit_code = 1
    elif hits:
        print("Nothing to alert: all hits were alerted under 2 hours ago")

    # After a failed check the missing slots say nothing about availability, so
    # keep their history; otherwise they would alert as "new" on the next good run.
    current = {hit.key for hit in hits}
    if not error:
        state["active"] = {k: v for k, v in state["active"].items() if k in current}
    if alerted:
        state["active"].update({key: now.isoformat() for key in current})

    # Failure tracking.
    if error:
        state["consecutive_failures"] += 1
        print(f"Consecutive failed checks: {state['consecutive_failures']}")
        if state["consecutive_failures"] >= FAILURES_BEFORE_ALERT and not state["broken_alert_sent"]:
            message = (f"Mayo watcher broken: {state['consecutive_failures']} failed checks in a "
                       f"row. It may be missing places. Last error: {error}")[:1000]
            if dry_run:
                print(f"[dry run] would alert:\n{message}")
            else:
                try:
                    send(topic, message, click=run_url() or CALENDAR_URL, tags="warning")
                    state["broken_alert_sent"] = True
                    print("Broken-watcher alert sent")
                except Exception as exc:
                    print(f"ERROR: could not send broken-watcher alert: {exc}")
                    exit_code = 1
    else:
        state["consecutive_failures"] = 0
        state["broken_alert_sent"] = False

    if dry_run:
        print("[dry run] state file not changed")
    else:
        save_state(state_path, state)
    return exit_code


# ---------------------------------------------------------------- CLI

def parse_dates(text: str) -> list[str]:
    try:
        dates = sorted({date.fromisoformat(d.strip()).isoformat()
                        for d in text.split(",") if d.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"dates must be YYYY-MM-DD,YYYY-MM-DD: {exc}")
    if not dates:
        raise argparse.ArgumentTypeError("at least one date is required")
    return dates


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Kewpie Mayo Terrace tour watcher (never books).")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--interval", type=positive_int, default=DEFAULT_INTERVAL, metavar="SECONDS",
                        help=f"seconds between checks in loop mode (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--dates", type=parse_dates, default=DEFAULT_DATES,
                        help="comma-separated YYYY-MM-DD (default: 30 Nov - 4 Dec 2026)")
    parser.add_argument("--people", type=positive_int, default=2, help="places needed (default: 2)")
    parser.add_argument("--any-tour", action="store_true", help="also include Japanese tours")
    parser.add_argument("--state-file", type=Path, default=Path("state.json"))
    parser.add_argument("--dry-run", action="store_true", help="print what would be alerted; send and save nothing")
    parser.add_argument("--show", action="store_true", help="use a visible browser, for debugging")
    args = parser.parse_args(argv)

    topic = os.environ.get("NTFY_TOPIC", "").strip() or None
    dry_run = args.dry_run
    if not topic and not dry_run:
        if os.environ.get("GITHUB_ACTIONS"):
            print("ERROR: the NTFY_TOPIC secret is not set, so no alert could be delivered.")
            return 2
        print("NTFY_TOPIC is not set: running as --dry-run, nothing will be sent.")
        dry_run = True

    while True:
        today = datetime.now(JST).date().isoformat()
        if today > max(args.dates):
            print(f"Last watched date {max(args.dates)} has passed (today is {today} JST). Stopping.")
            return 0
        dates = {d for d in args.dates if d >= today}
        code = run_once(dates, args.people, args.any_tour, args.state_file, topic, dry_run, args.show)
        if args.once:
            return code
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())

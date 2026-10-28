#!/usr/bin/env python3
"""Scrape the Bit app (ביט) over adb into the local PostgreSQL db `bitpay`.

Flow: open Bit -> home "See all" -> scroll the activity list, opening each new row's detail screen for
its reference number -> back to the top -> "Contact/Number" sheet, scroll it and save every contact ->
map counterparties to phones (exact name match, verified by filtering the activity per contact) -> close Bit.

Everything is parsed from `uiautomator dump` XML. Runs are incremental: the activity pass stops after
--stop-after consecutive already-known transactions, and the filter pass only checks contacts with
unconfirmed name matches (--full-sweep: every contact never checked). An interrupted run is continued.
"""
import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

PKG = 'com.bnhp.payments.paymentsapp'
TZ = ZoneInfo('Asia/Jerusalem')
BIT_GLYPH = ''  # private-use glyph Bit renders as its logo (" balance" = "bit balance")
DATE_RE = re.compile(r'^(Today|Yesterday|\d\d\.\d\d\.\d\d)$')
AMOUNT_RE = re.compile(r'^-?₪[\d,]+(\.\d+)?$')
PHONE_RE = re.compile(r'^\+?[\d\- ]{9,}$')
STATE = Path(__file__).with_name('.state.json')  # progress of an unfinished run (passes done, activity cursor)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)

DIRECTIONS = {'Money received': 'received', 'Money sent': 'sent'}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---------------------------------------------------------------- adb + UI tree

class Interrupted(Exception):
    """Bit left the foreground (someone used the phone) or the phone left adb, and is back: the screen is unknown."""


def device_ready():
    return subprocess.run(['adb', 'get-state'], capture_output=True, text=True).stdout.strip() == 'device'


def wait_for_device():
    """Block while the phone is off adb (unplugged, USB debugging re-authorised, ...)."""
    if device_ready():
        return
    log('phone not connected to adb; waiting for it')
    t0 = last = time.time()
    while not device_ready():
        if time.time() - last > 300:
            log(f'  still waiting for the phone ({int(time.time() - t0) // 60} min)')
            last = time.time()
        time.sleep(5)
    log(f'phone back after {int(time.time() - t0)}s')
    time.sleep(2)


def adb(*args, check=True):
    r = subprocess.run(['adb', *args], capture_output=True, text=True)
    if r.returncode != 0 and not device_ready():
        wait_for_device()
        raise Interrupted()  # the phone went away mid-step: restart from a known screen
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
    return r.stdout


def raw_dump():
    """Current UI tree as an ElementTree root (retries: dump fails while the UI animates)."""
    for attempt in range(10):
        out = adb('exec-out', 'uiautomator', 'dump', '/dev/tty', check=False)
        end = out.rfind('</hierarchy>')
        if end > 0:
            return ET.fromstring(out[out.find('<'):end + len('</hierarchy>')])
        time.sleep(1)
    raise RuntimeError(f'uiautomator dump failed: {out[-200:]!r}')


def window_pkg(root):
    return next((n.get('package') for n in root.iter('node') if n.get('package')), None)


IDLE_TAKEOVER = 30  # seconds the phone must sit on its launcher before we bring Bit back ourselves


def launcher_pkg():
    out = adb('shell', 'cmd', 'package', 'resolve-activity', '--brief',
              '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.HOME', check=False)
    return out.strip().splitlines()[-1].split('/')[0] if out.strip() else None


def phone_idle_on_launcher(fg, launcher):
    """The launcher is in front, the screen is on and unlocked: nobody is in an app."""
    if fg != launcher:
        return False
    power = adb('shell', 'dumpsys', 'power', check=False)
    window = adb('shell', 'dumpsys', 'window', check=False)
    return 'mWakefulness=Awake' in power and 'isKeyguardShowing=true' not in window


def wait_for_bit(reason):
    """Block while someone uses the phone: another app, the lock screen, the screen off. Poll until Bit is back.

    The launcher sitting in front for IDLE_TAKEOVER seconds counts as idle: then Bit is brought back."""
    log(f'paused: {reason}; waiting for Bit to be back in the foreground')
    launcher = launcher_pkg()
    t0 = last = time.time()
    idle_since = None
    while (fg := foreground_pkg()) != PKG:
        if phone_idle_on_launcher(fg, launcher):
            idle_since = idle_since or time.time()
            if time.time() - idle_since >= IDLE_TAKEOVER:
                log(f'  phone idle on the launcher for {IDLE_TAKEOVER}s: bringing Bit to the front')
                adb('shell', 'monkey', '-p', PKG, '-c', 'android.intent.category.LAUNCHER', '1')
                idle_since = None
                time.sleep(3)
                continue
        else:
            idle_since = None
        if time.time() - last > 300:
            log(f'  still paused ({int(time.time() - t0) // 60} min, {fg} in front)')
            last = time.time()
        time.sleep(5)
    log(f'resumed after {int(time.time() - t0)}s')
    time.sleep(2)


def dump():
    """Like raw_dump(), but pauses while Bit isn't in front, then raises Interrupted so the caller restarts
    from a known screen (whoever used the phone may have left Bit anywhere)."""
    root = raw_dump()
    pkg = window_pkg(root)
    if pkg != PKG:
        wait_for_bit(f'{pkg} is in the foreground')
        raise Interrupted()
    return root


def tap(x, y):
    adb('shell', 'input', 'tap', str(x), str(y))


def drag(x, y0, y1):
    """Scroll by dragging from y0 to y1 and holding still before lifting, so the list doesn't fling."""
    steps = [y0 + (y1 - y0) * i // 4 for i in range(1, 5)]
    cmd = [f'input motionevent DOWN {x} {y0}'] + [f'input motionevent MOVE {x} {y}' for y in steps]
    cmd += ['sleep 0.4', f'input motionevent MOVE {x} {y1}', f'input motionevent UP {x} {y1}']
    adb('shell', '; '.join(cmd))


def bounds(n):
    x0, y0, x1, y1 = map(int, re.findall(r'\d+', n.get('bounds')))
    return x0, y0, x1, y1


def center(n):
    x0, y0, x1, y1 = bounds(n)
    return (x0 + x1) // 2, (y0 + y1) // 2


def node_attrs(n):
    """The non-default attributes of a node, for the raw jsonb columns."""
    return {k: v for k, v in n.attrib.items() if v not in ('', 'false') and k not in ('package', 'index')}


def subtree(n):
    return [node_attrs(c) for c in n.iter('node') if c is not n]


def by_text(root, text):
    return next((n for n in root.iter('node') if n.get('text') == text), None)


def by_desc(root, desc):
    return next((n for n in root.iter('node') if n.get('content-desc') == desc), None)


def texts(root):
    return {n.get('text') for n in root.iter('node') if n.get('text')}


def clean(s):
    return s.replace(BIT_GLYPH, 'bit').strip() if s is not None else None


def wait_for(pred, what, timeout=15):
    t0 = time.time()
    while True:
        root = dump()
        if pred(root):
            return root
        if time.time() - t0 > timeout:
            raise RuntimeError(f'timed out waiting for {what}; screen texts: {sorted(texts(root))[:30]}')
        time.sleep(0.3)


# ---------------------------------------------------------------- screens

def is_home(r):
    """A main tab (Home/Pockets/Profile tab bar). Home keeps its scroll position, so "Recent activity" and
    "See all" may be off screen; open_activity() scrolls to them."""
    return all(by_text(r, t) is not None for t in ('Home', 'Pockets', 'Profile')) and by_desc(r, 'Back') is None


def is_activity(r):
    # the filter chips scroll away with the list; the Back button and Search field stay
    return by_desc(r, 'Back') is not None and by_text(r, 'Search') is not None and by_desc(r, 'Close screen') is None


def is_detail(r):
    return by_desc(r, 'Close screen') is not None and by_text(r, 'Date') is not None


def is_sheet(r):
    return by_text(r, 'Filter by contact or mobile number') is not None


def scroll_container(r):
    return max((n for n in r.iter('node') if n.get('scrollable') == 'true'),
               key=lambda n: (bounds(n)[3] - bounds(n)[1]), default=None)


def foreground_pkg():
    m = re.search(r'mCurrentFocus=Window\{\S+ \S+ ([^/}\s]+)', adb('shell', 'dumpsys', 'window'))
    return m.group(1) if m else None


def bit_running():
    """Bit has a live activity (open behind another app). Not pidof: background services revive the process."""
    return f'{PKG}/' in ''.join(l for l in adb('shell', 'dumpsys', 'activity', 'activities').splitlines()
                                if 'ActivityRecord' in l)


def open_home():
    if foreground_pkg() != PKG:
        if bit_running():
            # Bit is open but behind another app: someone is using the phone, don't take it over
            wait_for_bit(f'Bit is running in the background ({foreground_pkg()} in front)')
        else:
            log('launching Bit')
            adb('shell', 'monkey', '-p', PKG, '-c', 'android.intent.category.LAUNCHER', '1')
            time.sleep(3)
    for attempt in range(8):
        try:
            return wait_for(is_home, 'Bit home screen', timeout=20 if attempt == 0 else 4)
        except RuntimeError:
            r = dump()  # some other Bit screen: close it, or step back
            # (not `a or b`: an Element without children is falsy)
            close = next((n for n in map(lambda d: by_desc(r, d), ('Close screen', 'Close sheet', 'Back'))
                          if n is not None), None)
            if close is not None:
                tap(*center(close))
            elif not is_home(r):
                adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
            time.sleep(1)
    raise RuntimeError('could not reach the Bit home screen (locked? needs login?)')


def open_activity():
    r = open_home()
    tap(*center(by_text(r, 'Home')))  # the Home tab (we may be on Pockets/Profile)
    time.sleep(0.8)
    r = scroll_to_top(lambda r: any((n.get('content-desc') or '').startswith('Your balance') for n in r.iter('node')))
    for _ in range(6):
        if by_text(r, 'See all') is not None:
            break
        x0, y0, x1, y1 = bounds(scroll_container(r))
        drag(600, y1 - 50, y0 + 50)
        time.sleep(0.6)
        r = dump()
    tap(*center(by_text(r, 'See all')))
    return wait_for(is_activity, 'activity list')


# ---------------------------------------------------------------- parsing

def label_date(label, today):
    if label == 'Today':
        return today
    if label == 'Yesterday':
        return today - timedelta(days=1)
    return datetime.strptime(label, '%d.%m.%y').date()


def parse_amount(s):
    return float(s.replace('₪', '').replace(',', '')) if s else None


def parse_activity_row(n, today):
    """An activity list row -> dict, or None if it isn't a complete row (e.g. cut off at an edge)."""
    tx = [(c.get('text'), bounds(c)) for c in n.iter('node') if c is not n and c.get('text')]
    descs = [c.get('content-desc') for c in n.iter('node') if c is not n and c.get('content-desc')]
    amount = next((t for t, _ in tx if AMOUNT_RE.match(t.strip())), None)
    date_t = next(((t, b) for t, b in tx if DATE_RE.match(t.strip())), None)
    status = next((t for t, _ in tx if t.startswith('·')), None)
    direction = next((DIRECTIONS.get(d, d) for d in descs), None)
    rest = [(t, b) for t, b in tx if t not in (amount, status) and (date_t is None or t != date_t[0])]
    if not (amount and date_t and rest and direction):
        return None
    name = rest[0][0]
    note = ' '.join(t for t, b in rest[1:] if b[1] < date_t[1][1]) or None
    funding = ' '.join(t for t, b in rest[1:] if b[1] > date_t[1][1]) or None
    return {
        'name': clean(name), 'note': clean(note), 'funding': clean(funding),
        'amount': parse_amount(amount.strip()), 'direction': direction,
        'status': clean(status.lstrip('·')) if status else None,
        'date_label': date_t[0].strip(), 'date': label_date(date_t[0].strip(), today),
        'raw': subtree(n),
    }


def row_key(row):
    return (row['name'], row['note'], row['amount'], row['direction'], row['status'], row['date'])


def activity_rows(r):
    cont = scroll_container(r)
    # a list that fits on the screen (e.g. filtered by one contact) has no scrollable container
    _, ctop, _, cbot = bounds(cont) if cont is not None else (0, 0, 0, 10**6)
    today = datetime.now(TZ).date()
    rows = []
    for n in (cont if cont is not None else r).iter('node'):
        if n.get('clickable') != 'true':
            continue
        x0, y0, x1, y1 = bounds(n)
        if y0 < ctop or y1 > cbot:
            continue
        row = parse_activity_row(n, today)
        if row:
            row['node'] = n
            rows.append(row)
    return rows, cont


def parse_detail(r):
    fields = {}
    for n in r.iter('node'):
        kids = [c for c in n if c.get('class', '').endswith('TextView') and c.get('text')]
        if n.get('clickable') == 'true' and len(kids) == 2:
            fields[clean(kids[0].get('text'))] = clean(kids[1].get('text'))
    header = [clean(n.get('text')) for n in r.iter('node')
              if n.get('text') and n.get('clickable') != 'true' and bounds(n)[1] < 1300]
    ref = next((v for k, v in fields.items() if k.startswith('Reference number')), None)
    tx_at = None
    if 'Date' in fields:
        tx_at = datetime.strptime(f"{fields['Date']} {fields.get('Time', '00:00')}", '%d.%m.%y %H:%M').replace(tzinfo=TZ)
    return {'ref': ref, 'tx_at': tx_at, 'fields': fields, 'header': header, 'nodes': subtree(r)}


def normalize_phone(s):
    d = re.sub(r'\D', '', s or '')
    if d.startswith('972'):
        d = '0' + d[3:]
    return d or None


# ---------------------------------------------------------------- scrolling

def scroll_rows(get_rows, key, x=600, first=None, seek=None):
    """Yield every complete row of a scrollable list exactly once, top to bottom.

    After the rows on screen are used, the last of them is dragged to the top of the list, then found again
    there by key; only rows below it are new. The caller may leave and come back (the list keeps its
    position), but must be back on the list before asking for the next row.
    seek: a dict; when the consumer sets seek['date'], the list fast-forwards to the first screen with rows of
    that date (activity lists only, newest first) and continues from there.
    """
    last = None  # key of the last yielded row
    while True:
        if seek and seek.get('date'):
            first, seek['date'] = fast_forward(seek['date'], x), None
            last = None
        rows, cont = get_rows(first if first is not None else dump())
        first = None
        if not rows:
            return
        if last is not None:
            idx = next((i for i, rw in enumerate(rows) if key(rw) == last), None)
            if idx is None:
                log('warning: lost track of the list position; continuing after the top row')
                idx = 0
            rows = rows[idx + 1:]
            if not rows:
                return  # end of list: dragging didn't reveal anything new
        for rw in rows:
            yield rw
        last = key(rows[-1])
        if cont is None:
            return  # nothing to scroll
        top = bounds(rows[-1]['node'])[1]
        ctop = bounds(cont)[1]
        if top - ctop < 50:
            return
        drag(x, top + 5, ctop + 5)
        time.sleep(0.6)


def fast_forward(day, x=600):
    """Fling down the activity list past `day`, then step back up until the screen starts above it.

    Returns a dump whose top rows are newer than `day` (or the list top), so nothing on `day` or older
    is skipped; everything newer was handled by an earlier run.
    """
    log(f'  fast-forwarding to {day}')
    prev = None
    while True:  # fling until the screen shows rows older than `day`, or the list ends
        rows, cont = activity_rows(dump())
        keys = [row_key(r) for r in rows]
        if not rows or rows[-1]['date'] < day or keys == prev:
            break
        prev = keys
        adb('shell', 'input', 'swipe', str(x), '2300', str(x), '500', '80')
        time.sleep(0.8)
    while True:  # back up one screen at a time until the top row is newer than `day`
        r = dump()
        rows, cont = activity_rows(r)
        if not rows or cont is None or rows[0]['date'] > day:
            return r
        _, ctop, _, cbot = bounds(cont)
        before = [row_key(rw) for rw in rows]
        drag(x, ctop + 5, cbot - 5)
        time.sleep(0.6)
        r2 = dump()
        if [row_key(rw) for rw in activity_rows(r2)[0]] == before:
            return r2  # at the top of the list


def scroll_to_top(get_ready):
    for _ in range(100):
        r = dump()
        if get_ready(r):
            return r
        adb('shell', 'input', 'swipe', '600', '900', '600', '2400', '120')
        time.sleep(0.4)
    raise RuntimeError('could not scroll back to the top')


# ---------------------------------------------------------------- db

def db_connect():
    return psycopg.connect('dbname=bitpay', autocommit=True)


def known_by_key(cur, row, claimed):
    """A stored ref matching a list row without opening it: only when exactly one unclaimed ref matches."""
    cur.execute("""select ref from transactions
                   where counterparty_name = %s and note is not distinct from %s and amount = %s
                     and direction = %s and status is not distinct from %s
                     and (tx_at at time zone 'Asia/Jerusalem')::date = %s""",
                (row['name'], row['note'], row['amount'], row['direction'], row['status'], row['date']))
    refs = [r[0] for r in cur.fetchall() if r[0] not in claimed]
    return refs[0] if len(refs) == 1 else None


def open_detail(row):
    tap(*center(row['node']))
    r = wait_for(is_detail, 'transaction detail')
    det = parse_detail(r)
    back_on_list = lambda r: is_activity(r) and not is_detail(r)
    for attempt in range(4):  # a tap during the opening animation is sometimes ignored
        close = by_desc(r, 'Close screen')
        if close is None:
            break
        tap(*center(close))
        try:
            wait_for(back_on_list, 'activity list after detail', timeout=4)
            break
        except RuntimeError:
            r = dump()
    else:
        adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
        wait_for(back_on_list, 'activity list after detail (Back)', timeout=6)
    if not det['ref']:
        # no reference number on this screen: key it on what we have, stable across runs
        det['ref'] = 'noref:' + '|'.join(str(x) for x in (row['name'], row['note'], row['amount'],
                                                          row['direction'], det['tx_at'], row['status']))
    return det


def save_tx(cur, row, det, phone=None, phone_source=None):
    if phone is None and PHONE_RE.match(row['name']):
        phone, phone_source = normalize_phone(row['name']), 'row'
    cur.execute("""insert into transactions (ref, counterparty_name, counterparty_phone, phone_source, direction,
                       amount, note, status, funding, tx_at, list_row, detail)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   on conflict (ref) do update set
                       counterparty_name = excluded.counterparty_name, direction = excluded.direction,
                       amount = excluded.amount, note = excluded.note, status = excluded.status,
                       funding = excluded.funding, tx_at = excluded.tx_at, list_row = excluded.list_row,
                       detail = excluded.detail, last_seen_at = now(),
                       counterparty_phone = coalesce(excluded.counterparty_phone, transactions.counterparty_phone),
                       phone_source = case when excluded.counterparty_phone is not null then excluded.phone_source
                                           else transactions.phone_source end
                   returning (xmax = 0)""",
                (det['ref'], row['name'], phone, phone_source, row['direction'], row['amount'], row['note'],
                 row['status'], row['funding'], det['tx_at'], Jsonb(row['raw']),
                 Jsonb({'fields': det['fields'], 'header': det['header'], 'nodes': det['nodes']})))
    return cur.fetchone()[0]


def touch_tx(cur, ref, row, phone=None, phone_source=None):
    cur.execute("""update transactions set last_seen_at = now(), list_row = %s,
                       counterparty_phone = coalesce(%s, counterparty_phone),
                       phone_source = case when %s::text is not null then %s else phone_source end
                   where ref = %s""", (Jsonb(row['raw']), phone, phone, phone_source, ref))


def process_rows(cur, rows, stop_after=None, phone=None, limit=None, resume=None, on_row=None):
    """Store the rows of an activity list (full or filtered). Returns (new, seen, names, refs).

    resume: (seek dict, day) -- after the first run of known rows, have the list fast-forward to `day`, where an
    unfinished earlier pass stopped. on_row(row): called after each row is stored (progress cursor).
    """
    claimed, new, seen, known_streak, names = set(), 0, 0, 0, set()
    for row in rows:
        seen += 1
        names.add(row['name'])
        ref = known_by_key(cur, row, claimed)
        if ref:
            touch_tx(cur, ref, row, phone, 'filter' if phone else None)
            inserted = False
        else:
            det = open_detail(row)
            ref = det['ref']
            inserted = save_tx(cur, row, det, phone, 'filter' if phone else None)
        claimed.add(ref)
        new += inserted
        known_streak = 0 if inserted else known_streak + 1
        log(f"  {'NEW ' if inserted else 'seen'} {ref}  {row['date']} {row['direction']:8} {row['amount']:>9} "
            f"{row['name']} / {row['note'] or ''}")
        if on_row:
            on_row(row)
        if limit and seen >= limit:
            break
        if resume and known_streak >= 3 and row['date'] > resume[1]:
            resume[0]['date'] = resume[1]  # new rows on top are done; skip what the earlier pass already did
            resume = None
        if stop_after and known_streak >= stop_after:
            log(f'  {stop_after} known transactions in a row: rest is already stored')
            break
    return new, seen, names, claimed


# ---------------------------------------------------------------- passes

def activity_pass(cur, state, stop_after, limit=None):
    """state['cursor']: date of the last row this pass got to; set while it runs, so a crashed or interrupted
    pass resumes there (after picking up any newer rows on top) instead of starting over."""
    seek = {}
    cursor = state.get('cursor')
    resume = (seek, date.fromisoformat(cursor)) if cursor else None
    log(f'activity pass{f" (resuming at {cursor})" if cursor else ""}')

    def on_row(row):
        if not resume or seek.get('done_seek') or row['date'] <= resume[1]:
            seek['done_seek'] = True  # past the resume point: from here on, progress moves the cursor
            state['cursor'] = row['date'].isoformat()
            save_state(state)

    new, seen, _, _ = process_rows(cur, scroll_rows(activity_rows, row_key, seek=seek),
                                stop_after=None if resume else stop_after, limit=limit, resume=resume, on_row=on_row)
    log(f'activity pass: {new} new, {seen} rows looked at')


def sheet_rows(r):
    if not is_sheet(r):
        return [], None
    cont = scroll_container(r)
    # a search that leaves only a few hits shows them outside any scrollable container
    _, ctop, _, cbot = bounds(cont) if cont is not None else (0, 0, 0, 10**6)
    rows = []
    for n in (cont if cont is not None else r.iter('node')):
        if n.get('clickable') != 'true':
            continue
        x0, y0, x1, y1 = bounds(n)
        kids = [c for c in n.iter('node') if c.get('text')]
        if y0 < ctop or y1 > cbot or len(kids) < 2:
            continue
        phone_t = next((c.get('text') for c in kids if PHONE_RE.match(c.get('text'))), None)
        name_t = next((c.get('text') for c in kids if c.get('text') != phone_t), None)
        if phone_t:
            rows.append({'name': name_t, 'phone_display': phone_t, 'phone': normalize_phone(phone_t),
                         'raw': subtree(n), 'node': n})
    return rows, cont


def open_sheet():
    r = scroll_to_top(lambda r: is_activity(r) and by_text(r, 'Contact/Number') is not None)
    tap(*center(by_text(r, 'Contact/Number')))
    return wait_for(is_sheet, 'Contact/Number sheet')


def close_sheet():
    r = dump()
    if is_sheet(r):
        tap(*center(by_desc(r, 'Close sheet')))
        wait_for(lambda r: not is_sheet(r), 'sheet to close')


def contacts_pass(cur):
    log('contacts pass')
    open_sheet()
    n = 0
    for c in scroll_rows(sheet_rows, lambda c: (c['name'], c['phone']), x=610):
        cur.execute("""insert into contacts (phone, name, phone_display, raw) values (%s,%s,%s,%s)
                       on conflict (phone) do update set name = excluded.name, phone_display = excluded.phone_display,
                           raw = excluded.raw, last_seen_at = now()""",
                    (c['phone'], c['name'], c['phone_display'], Jsonb(c['raw'])))
        n += 1
    close_sheet()
    log(f'contacts pass: {n} contacts')


def propagate_bit_names(cur):
    """Unmapped transactions whose counterparty name maps to exactly one contact get that contact's phone."""
    cur.execute("""with m as (select b as bit_name, min(phone) as phone from contacts, unnest(bit_names) b
                              group by b having count(distinct phone) = 1)
                   update transactions t set counterparty_phone = m.phone, phone_source = 'bit_name'
                   from m where t.counterparty_phone is null and t.counterparty_name = m.bit_name""")
    return cur.rowcount


def norm_name(s):
    """Name as compared across Bit and the phone book: no leading "- ", no "/M" suffix, no punctuation."""
    s = re.sub(r'/M$', '', re.sub(r'^-\s*', '', s or ''))
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', s)).strip().lower()


def name_match(cur):
    """Rough layer: an unmapped transaction whose counterparty name equals exactly one contact's name gets that
    phone (phone_source 'name'). Bit shows a counterparty under your phone-book name when you have them as a
    contact, so this catches most contacts; the filter check then confirms or corrects each match."""
    cur.execute("select phone, name from contacts")
    by_name = {}
    for phone, name in cur.fetchall():
        by_name.setdefault(norm_name(name), set()).add(phone)
    cur.execute("""select distinct counterparty_name from transactions
                   where counterparty_phone is null and phone_source is null""")
    n = 0
    for (name,) in cur.fetchall():
        phones = by_name.get(norm_name(name), set())
        if len(phones) == 1:
            cur.execute("""update transactions set counterparty_phone = %s, phone_source = 'name'
                           where counterparty_name = %s and counterparty_phone is null and phone_source is null""",
                        (phones.pop(), name))
            n += cur.rowcount
    return n


class Filter:
    """Filters the activity list by one contact at a time, with as few UI dumps as possible (each ~2.3s).

    Positions learned on the first contact (the Contact/Number chip, the single search hit) are reused
    blind afterwards; the filtered screen's "Cancel filter - <name>" chip verifies each blind tap.
    """
    def __init__(self):
        self.chip = None  # Contact/Number chip, list at top
        self.hit = None   # the one search result, keyboard up

    def open_sheet(self):
        if self.chip is None:
            r = scroll_to_top(lambda r: is_activity(r) and by_text(r, 'Contact/Number') is not None)
            self.chip = center(by_text(r, 'Contact/Number'))
        tap(*self.chip)
        try:
            return wait_for(is_sheet, 'Contact/Number sheet', timeout=6)
        except RuntimeError:
            self.chip = None  # list wasn't at the top, or a filter is still on: find the chip properly
            self.clear()
            return open_sheet()

    def apply(self, phone, name):
        """Filtered activity root for this contact, or None if the sheet doesn't list it."""
        r = self.open_sheet()
        tap(*center(next(n for n in r.iter('node') if n.get('class', '').endswith('EditText'))))
        time.sleep(0.4)
        adb('shell', 'input', 'text', phone)
        time.sleep(1.0)
        if self.hit is not None:
            tap(*self.hit)
            time.sleep(1.2)
            r = dump()
            if active_filter(r) == name:
                return r
            if not is_sheet(r):  # blind tap picked someone else (two contacts on one number?)
                self.clear(r)
                return self.apply_slow(phone, name)
        else:
            r = dump()
        return self.select(r, phone)

    def apply_slow(self, phone, name):
        r = self.open_sheet()
        tap(*center(next(n for n in r.iter('node') if n.get('class', '').endswith('EditText'))))
        time.sleep(0.4)
        adb('shell', 'input', 'text', phone)
        time.sleep(1.0)
        return self.select(dump(), phone)

    def select(self, r, phone):
        rows, _ = sheet_rows(r)
        hit = next((c for c in rows if c['phone'] == phone), None)
        if hit is None:
            adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')  # hide keyboard
            close_sheet()
            return None
        if len(rows) == 1:
            self.hit = center(hit['node'])
        tap(*center(hit['node']))
        time.sleep(1.2)
        return wait_for(lambda r: active_filter(r) is not None, 'filtered activity')

    def clear(self, r=None):
        """Cancel the contact filter (if any) and check it's gone; r: a dump with the list at the top, if the
        caller has one. A tap right after scrolling is sometimes ignored, hence the check and retries."""
        chips_visible = lambda r: active_filter(r) is not None or by_text(r, 'Contact/Number') is not None
        for attempt in range(4):
            if r is None or not chips_visible(r):
                r = scroll_to_top(chips_visible)
            if active_filter(r) is None:
                return
            cancel = next(n for n in r.iter('node') if (n.get('content-desc') or '').startswith('Cancel filter'))
            tap(*center(cancel))
            time.sleep(0.8)
            r = dump()
        raise RuntimeError('could not clear the contact filter')


def active_filter(r):
    """Name in the "Cancel filter - <name>" chip, or None."""
    d = next((n.get('content-desc') for n in r.iter('node')
              if (n.get('content-desc') or '').startswith('Cancel filter ')), None)
    return d[len('Cancel filter '):] if d else None


def filter_pass(cur, max_checks, recheck_all, full_sweep, only=None):
    """Layered counterparty -> phone mapping:
    1. names already confirmed for a contact (contacts.bit_names) -> phone_source 'bit_name'
    2. exact name match against the phone book -> 'name' (rough)
    3. filter the activity by each contact with unconfirmed 'name' matches: rows it shows become 'filter',
       name matches it doesn't show are undone ('name_rejected', phone cleared)
    4. --full-sweep: also filter by every contact never checked (the whole phone book, ~9s each)
    """
    log(f'filter pass: {propagate_bit_names(cur)} transactions mapped by confirmed bit names, '
        f'{name_match(cur)} by exact name')
    if only:
        cur.execute("select phone, name from contacts where phone = any(%s)", (only,))
    else:
        cur.execute("""select c.phone, c.name from contacts c
                       where %s or exists (select from transactions t
                                           where t.counterparty_phone = c.phone and t.phone_source = 'name')
                       order by c.phone""", (recheck_all,))
    todo = cur.fetchall()
    if full_sweep and not only:
        cur.execute("""select phone, name from contacts where filter_checked_at is null and not (phone = any(%s))
                       order by created_at, phone""", ([p for p, _ in todo],))
        todo += cur.fetchall()
    log(f'filter pass: {len(todo)} contacts to check')
    flt = Filter()
    for checked, (phone, name) in enumerate(todo):
        if checked >= max_checks:
            log(f'filter pass: stopping at --max-filter {max_checks}; next run continues')
            return
        cur.execute("select ref from transactions where counterparty_phone = %s and phone_source = 'name'", (phone,))
        guessed = {r[0] for r in cur.fetchall()}
        r = flt.apply(phone, name)
        if r is None:
            log(f'  {phone} {name}: not in the sheet any more')
            seen, refs, names, new = 0, set(), set(), 0
        else:
            new, seen, names, refs = process_rows(cur, scroll_rows(activity_rows, row_key, first=r), phone=phone)
            flt.clear(r if seen <= 6 else None)  # few rows: the list never scrolled, the chip is where it was
        wrong = guessed - refs
        if wrong:
            cur.execute("""update transactions set counterparty_phone = null, phone_source = 'name_rejected'
                           where ref = any(%s)""", (sorted(wrong),))
        cur.execute("""update contacts set filter_checked_at = now(), filter_tx_count = %s,
                           bit_names = (select array(select distinct unnest(bit_names || %s::text[])))
                       where phone = %s""", (seen, sorted(names), phone))
        log(f'  {phone} {name}: {seen} transactions ({new} new), bit names {sorted(names)}'
            f'{f", {len(guessed) - len(wrong)}/{len(guessed)} name matches confirmed" if guessed else ""}')
    log(f'filter pass: {propagate_bit_names(cur)} more mapped by confirmed bit names')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stop-after', type=int, default=3,
                    help='stop the activity pass after this many known transactions in a row (0: scan everything)')
    ap.add_argument('--max-filter', type=int, default=10**6, help='max contacts to filter by in this run')
    ap.add_argument('--full-sweep', action='store_true',
                    help='also filter by every never-checked contact (whole phone book, ~9s each)')
    ap.add_argument('--max-activity', type=int, help='look at no more than this many activity rows (testing)')
    ap.add_argument('--recheck-all', action='store_true', help='filter by every contact again')
    ap.add_argument('--only', action='append', metavar='PHONE', help='filter by just these contacts (0541234567)')
    ap.add_argument('--skip', choices=['activity', 'contacts', 'filter'], action='append', default=[])
    ap.add_argument('--keep-open', action='store_true', help="don't close Bit at the end")
    a = ap.parse_args()

    # An unfinished earlier run (crash, Ctrl-C, phone gone) is continued: passes it finished are skipped,
    # the activity pass resumes at its cursor, the filter pass at the contacts not checked yet.
    state = load_state()
    if state.get('unfinished'):
        log(f"continuing the unfinished run from {state.get('started')}: done {state.get('done') or 'nothing'}"
            f"{', activity cursor ' + state['cursor'] if state.get('cursor') else ''}")
    else:
        state = {'unfinished': True, 'started': datetime.now(TZ).isoformat(timespec='seconds'), 'done': []}
    passes = [p for p in ('activity', 'contacts', 'filter') if p not in a.skip and p not in state['done']]
    test_run = a.max_activity or a.only or a.max_filter < 10**6
    if not test_run:
        save_state(state)
    failures = 0
    with db_connect() as conn, conn.cursor() as cur:
        try:
            while passes:
                try:
                    open_activity()
                    while passes:
                        if passes[0] == 'activity':
                            activity_pass(cur, state if not test_run else {}, a.stop_after or None, a.max_activity)
                        elif passes[0] == 'contacts':
                            contacts_pass(cur)
                        else:
                            filter_pass(cur, a.max_filter, a.recheck_all, a.full_sweep, a.only)
                        if not test_run:
                            state['done'].append(passes[0])
                            state.pop('cursor', None) if passes[0] == 'activity' else None
                            save_state(state)
                        passes.pop(0)
                except Interrupted:
                    log(f'restarting from the home screen ({passes[0]} pass)')
                except RuntimeError as e:  # an unexpected screen: start over from home, but not forever
                    failures += 1
                    if failures > 5:
                        raise
                    log(f'error ({e}); restarting from the home screen ({passes[0]} pass, failure {failures}/5)')
            log(f'mapped {propagate_bit_names(cur)} more transactions by bit name')
            if not test_run:
                save_state({'unfinished': False, 'last_finished': datetime.now(TZ).isoformat(timespec='seconds')})
        finally:
            if not a.keep_open:
                adb('shell', 'am', 'force-stop', PKG, check=False)
                log('closed Bit')
    cur_summary()


def cur_summary():
    with db_connect() as conn:
        n_tx, n_ph, n_c = conn.execute("""select (select count(*) from transactions),
                                                 (select count(counterparty_phone) from transactions),
                                                 (select count(*) from contacts)""").fetchone()
    log(f'done: {n_tx} transactions ({n_ph} with a phone), {n_c} contacts')


if __name__ == '__main__':
    main()

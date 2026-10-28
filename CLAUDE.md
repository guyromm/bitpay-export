# bitpay-exporter

Scrapes the Bit app (ביט, package `com.bnhp.payments.paymentsapp`) on an Android phone over adb by
parsing `uiautomator dump` XML, into the local PostgreSQL db `bitpay` (local user, unix socket).
Flow: open Bit if needed -> home "See all" on recent activity -> scroll + save activity
(incremental) -> scroll to top -> "Contact/Number" filter chip -> scroll + save contacts -> close Bit.
Output: two tables (transactions, contacts), each row with created_at + last_seen_at, joinable by a
stable counterparty key; any extra per-row fields from the UI tree are kept too (raw jsonb).
Schema is versioned with ./pg_schema_dump.py (per-project fork of fb-collect's copy,
see CLAUDE.local.md) into sql/schema/; run it after every schema change and commit.

## Running
`./bitpay_exporter.py` (needs the phone on adb, Bit logged in). Long runs go in a tmux window, output to run.log.
- Activity pass: each new row's detail screen gives the reference number (transactions.ref, the PK);
  known rows are matched by (name, note, amount, direction, status, date) without opening them. Stops after
  --stop-after (3) known rows in a row.
- No double work: .state.json records an unfinished run (passes done + activity cursor date). The next run
  skips finished passes; the activity pass takes new rows on top, then fast-forwards to the cursor.
- Contacts pass: whole Contact/Number sheet (~1270 contacts, ~10 min), upsert by normalized phone.
- Filter pass (layered; phone_source says which layer set counterparty_phone):
  1. names already confirmed for a contact (contacts.bit_names) -> 'bit_name'
  2. exact name match against the phone book -> 'name' (rough; Bit shows your phone-book name for contacts)
  3. filter activity by each contact with unconfirmed 'name' matches (~9s each) -> 'filter'; matches the
     filter doesn't show are undone ('name_rejected'). First run: 188/188 name matches confirmed.
  4. --full-sweep: filter by every never-checked contact (whole phone book, ~3h; finds little: the
     unmapped counterparties are mostly Bit names of people not in the phone book, and merchants).
  Counterparties shown as a number get their phone straight from the row ('row').
- If Bit leaves the foreground (another app, lock screen, screen off) the run pauses until Bit is back,
  then restarts from the home screen. The phone's launcher in front for 30s counts as idle: Bit is brought
  back. Phone off adb: waits for it. "Bit running" = has an ActivityRecord (pidof lies: services revive it).
- Stopping a run: kill the python pid (killing the tmux pane leaves it running).
- Join: transactions.counterparty_phone = contacts.phone. Merchants (PayMe, Domino's) have no phone.
- Useful flags: --skip activity|contacts|filter, --only PHONE, --max-filter N, --full-sweep, --keep-open.
- Start runs with `./run.sh <claude-pane>`: logs to run.log, then types "run ended (exit N)" into that pane.
- Incremental run measured: ~3s activity (0 new) + ~10 min contacts pass; data unchanged except last_seen_at.
- UI quirks: Compose, no resource-ids; `uiautomator dump` ~2.3s each (the bottleneck); scroll by
  motionevent drag held before lifting (plain swipes fling); short lists have no scrollable node;
  an ElementTree Element without children is falsy (never `node or other`).

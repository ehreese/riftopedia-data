#!/usr/bin/env python3
"""Build prices.json from TCGCSV's Riftbound price files.

The contract for the output is docs/format.md. Standard library only.

The script is split in two halves:

* Fetching (NetworkSource, OfflineSource): turns a path such as
  "tcgplayer/89/groups" into the raw bytes of the response. Only this half
  touches the network.
* Transformation (everything from parse_source_timestamp to render): pure
  functions from raw responses to rows to the text of the file. The unit
  tests cover this half without any network.

The job is scheduled every hour, but TCGCSV updates once a day. So before
anything else, run() asks whether an update is due at all: when the
published prices.json says TCGCSV last updated less than 23 hours ago
(UPDATE_INTERVAL), the run stops without a single request. Only a run 23
hours or more after the last update reads last-updated.txt, and only the
first run after TCGCSV's new update downloads the price files. Once the
update is overdue (OVERDUE_AFTER, 29 hours), a run goes ahead only in the
00, 06, 12 and 18 UTC hours (OVERDUE_CHECK_HOURS), so a day without an
update, or a failure that does not clear, costs a few runs a day rather than
24. --check-now (passed for runs started by hand) and --force skip both
rules.

run() ties the two together and applies the safety checks. On any problem
it raises BuildError, main() prints it and exits non-zero, and the existing
prices.json is never touched.

After a new prices.json is built, run() also adds its day to the monthly
price history in history/ (scripts/price_history.py, no network). The
history files are computed before anything is written, so a problem with
them also leaves every file untouched.
"""

import argparse
import gzip
import http.client
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import price_history as ph  # noqa: E402

BASE_URL = "https://tcgcsv.com"
CATEGORY_ID = 89  # Riftbound
USER_AGENT = "riftopedia-data/1.0 (+https://github.com/ehreese/riftopedia-data)"

MIN_INTERVAL = 0.2  # seconds between the start of two requests (TCGCSV asks for >= 0.15)
TIMEOUT = 30  # seconds per request
RETRY_PAUSE = 5  # seconds before the single polite retry

FORMAT_VERSION = 1
CURRENCY = "USD"
SOURCE = "TCGplayer market prices via TCGCSV"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "prices.json"
HISTORY_FOLDER = "history"  # next to the output file

LAST_UPDATED_PATH = "last-updated.txt"
GROUPS_PATH = f"tcgplayer/{CATEGORY_ID}/groups"

# TCGCSV updates once a day. A run less than this long after the update that
# the published file comes from contacts nothing.
UPDATE_INTERVAL = timedelta(hours=23)

# Once the published file is this old, TCGCSV's update is overdue (or runs
# keep failing): back off from every hour to every OVERDUE_CHECK_HOURS hours,
# going ahead only in the UTC hours divisible by it (00, 06, 12, 18).
OVERDUE_AFTER = timedelta(hours=29)
OVERDUE_CHECK_HOURS = 6

# A new file must keep at least this share of the published file's rows. One
# main set is 14-20% of all rows, so losing one stops the run.
MIN_KEPT_PERCENT = 90

FINISHES = {"Normal": 0, "Foil": 1}  # subTypeName -> index in (normal, foil)
CENT = Decimal("0.01")


class BuildError(Exception):
    """Anything that must stop the run and leave prices.json untouched."""


def prices_path(group_id):
    return f"tcgplayer/{CATEGORY_ID}/{group_id}/prices"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def decode_body(body, content_encoding):
    """Undo the transfer encoding of a response body.

    We only ever ask for gzip. Some servers compress anyway, so also accept a
    body that starts with the gzip magic number. Brotli cannot be decoded with
    the standard library, so it is an error.
    """
    encoding = (content_encoding or "").strip().lower()
    if encoding in ("", "identity"):
        if body[:2] == b"\x1f\x8b":
            return gzip.decompress(body)
        return body
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(body)
    raise BuildError(f"unsupported Content-Encoding {content_encoding!r}")


class NetworkSource:
    """Fetches from TCGCSV one request at a time, politely."""

    def __init__(self, base_url=BASE_URL, min_interval=MIN_INTERVAL, timeout=TIMEOUT,
                 retry_pause=RETRY_PAUSE, opener=urllib.request.urlopen,
                 sleep=time.sleep, clock=time.monotonic, save_dir=None, log=print):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.timeout = timeout
        self.retry_pause = retry_pause
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.save_dir = Path(save_dir) if save_dir else None
        self.log = log
        self.requests = 0
        self._last_start = None

    def _wait_turn(self):
        if self._last_start is not None:
            remaining = self.min_interval - (self.clock() - self._last_start)
            if remaining > 0:
                self.sleep(remaining)
        self._last_start = self.clock()

    def _request_once(self, url):
        self._wait_turn()
        self.requests += 1
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        })
        with self.opener(request, timeout=self.timeout) as response:
            body = response.read()
            return decode_body(body, response.headers.get("Content-Encoding"))

    def get(self, path):
        url = f"{self.base_url}/{path}"
        try:
            body = self._request_once(url)
        except TRANSIENT_ERRORS as first:
            if isinstance(first, urllib.error.HTTPError) and not _worth_retrying(first.code):
                raise BuildError(f"GET {url} failed: HTTP {first.code}") from first
            self.log(f"GET {url} failed ({_describe(first)}); retrying once in {self.retry_pause} s")
            self.sleep(self.retry_pause)
            try:
                body = self._request_once(url)
            except TRANSIENT_ERRORS as second:
                raise BuildError(f"GET {url} failed twice: {_describe(second)}") from second
        if self.save_dir is not None:
            target = self.save_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return body


# Network and decoding failures that earn one polite retry. URLError (and its
# HTTPError) and timeouts are OSErrors; a truncated gzip body raises EOFError,
# BadGzipFile or zlib.error; a dropped connection mid-body raises IncompleteRead.
TRANSIENT_ERRORS = (OSError, EOFError, zlib.error, http.client.HTTPException)


def _worth_retrying(status):
    return status == 429 or status >= 500


def _describe(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return f"{type(error).__name__}: {error}"


class OfflineSource:
    """Reads responses saved by --save-raw, laid out like the URL paths."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.requests = 0

    def get(self, path):
        target = self.directory / path
        try:
            return target.read_bytes()
        except OSError as error:
            raise BuildError(f"offline: cannot read {path}: {error.strerror}") from error


# ---------------------------------------------------------------------------
# Transformation (pure)
# ---------------------------------------------------------------------------


def format_timestamp(moment):
    """UTC, whole seconds, with a Z: 2026-11-16T20:05:58Z."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_source_timestamp(body):
    """Turn the body of last-updated.txt into the sourceUpdatedAt value."""
    try:
        text = body.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise BuildError("last-updated.txt is not UTF-8 text") from error
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise BuildError(f"last-updated.txt is not a timestamp: {text[:80]!r}") from error
    if moment.tzinfo is None:
        raise BuildError(f"last-updated.txt has no time zone: {text!r}")
    return format_timestamp(moment)


def parse_results(body, label):
    """Check a TCGCSV JSON envelope and return its results list."""
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise BuildError(f"{label}: response is not valid JSON") from error
    if not isinstance(document, dict):
        raise BuildError(f"{label}: response is not a JSON object")
    if document.get("success") is not True:
        raise BuildError(f"{label}: success is {document.get('success')!r}, "
                         f"errors: {document.get('errors')!r}")
    results = document.get("results")
    if not isinstance(results, list):
        raise BuildError(f"{label}: results is not a list")
    return results


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def group_ids(groups):
    """The groupId of every group, in the order TCGCSV lists them."""
    if not groups:
        raise BuildError("groups: no Riftbound groups listed")
    ids = []
    for group in groups:
        group_id = group.get("groupId") if isinstance(group, dict) else None
        if not _is_int(group_id) or group_id <= 0:
            raise BuildError(f"groups: bad groupId in {group!r}")
        if group_id not in ids:
            ids.append(group_id)
    return ids


def round_price(value, where):
    """A marketPrice as a float with at most two decimals, or None."""
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BuildError(f"{where}: marketPrice is not a number: {value!r}")
    if not math.isfinite(value) or value < 0:
        raise BuildError(f"{where}: marketPrice out of range: {value!r}")
    # repr() gives the shortest text that round-trips, so 0.125 rounds to 0.13
    # rather than suffering from its binary representation.
    return float(Decimal(repr(value)).quantize(CENT, rounding=ROUND_HALF_UP))


def build_rows(prices_by_group):
    """Merge the price results of every group into sorted output rows.

    prices_by_group maps groupId -> results list. Returns a list of
    [productId, normal, foil] rows, sorted by productId, one per product
    that has at least one market price.
    """
    merged = {}  # productId -> [normal, foil]
    seen_from = {}  # (productId, finish index) -> groupId that set it
    for group_id in sorted(prices_by_group):
        for entry in prices_by_group[group_id]:
            where = f"group {group_id}"
            if not isinstance(entry, dict):
                raise BuildError(f"{where}: price entry is not an object: {entry!r}")
            product_id = entry.get("productId")
            if not _is_int(product_id) or product_id <= 0:
                raise BuildError(f"{where}: bad productId in {entry!r}")
            where = f"group {group_id}, product {product_id}"
            finish = entry.get("subTypeName")
            if finish not in FINISHES:
                raise BuildError(f"{where}: unknown subTypeName {finish!r}")
            price = round_price(entry.get("marketPrice"), where)
            if price is None:
                continue
            index = FINISHES[finish]
            slot = merged.setdefault(product_id, [None, None])
            if slot[index] is None:
                slot[index] = price
                seen_from[(product_id, index)] = group_id
            elif slot[index] != price:
                raise BuildError(
                    f"product {product_id} has two {finish} prices: {slot[index]} "
                    f"(group {seen_from[(product_id, index)]}) and {price} (group {group_id})")
    return [[product_id, normal, foil]
            for product_id, (normal, foil) in sorted(merged.items())]


def render(rows, source_updated_at, generated_at):
    """The full text of prices.json, one price row per line."""
    header = {
        "version": FORMAT_VERSION,
        "currency": CURRENCY,
        "source": SOURCE,
        "sourceUpdatedAt": source_updated_at,
        "generatedAt": generated_at,
    }
    lines = ["{"]
    for key, value in header.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    if rows:
        lines.append('  "prices": [')
        body = [f"    {json.dumps(row, separators=(', ', ': '))}" for row in rows]
        lines.append(",\n".join(body))
        lines.append("  ]")
    else:
        lines.append('  "prices": []')
    lines.append("}")
    return "\n".join(lines) + "\n"


def check_shrink(new_count, old_count, allow_shrink):
    """Refuse a file with fewer than MIN_KEPT_PERCENT of the existing rows."""
    if old_count and new_count * 100 < old_count * MIN_KEPT_PERCENT and not allow_shrink:
        raise BuildError(
            f"new file would have {new_count} rows, fewer than {MIN_KEPT_PERCENT}% of the "
            f"existing {old_count}; rerun with --allow-shrink if this is expected")


def update_due(existing, now):
    """Whether TCGCSV may have updated since the published file, and why.

    Returns (due, reason). Not due when the file's sourceUpdatedAt is in
    the past and less than UPDATE_INTERVAL before now, or OVERDUE_AFTER or
    more before now while the UTC hour of now is not divisible by
    OVERDUE_CHECK_HOURS (the back-off). No file, a missing or unreadable
    sourceUpdatedAt, and a sourceUpdatedAt in the future all count as due,
    with no back-off, so the usual checks decide what happens.
    """
    if existing is None:
        return True, "no published prices.json"
    stamp = existing.get("sourceUpdatedAt")
    try:
        updated = parse_source_timestamp(stamp.encode("utf-8"))
    except (AttributeError, BuildError):
        return True, f"published sourceUpdatedAt {stamp!r} is not a timestamp"
    age = now - datetime.fromisoformat(updated.replace("Z", "+00:00"))
    if age < timedelta(0):
        return True, f"published sourceUpdatedAt {updated} is in the future"
    if age < UPDATE_INTERVAL:
        due_at = format_timestamp(now + (UPDATE_INTERVAL - age))
        return False, (f"prices.json is from {updated}, {_describe_age(age)} old; "
                       f"no update is due before {due_at}")
    if age >= OVERDUE_AFTER and now.astimezone(timezone.utc).hour % OVERDUE_CHECK_HOURS:
        return False, (f"update overdue: prices.json is from {updated}, "
                       f"{_describe_age(age)} old; overdue updates are checked only in the "
                       f"{_overdue_hours()} UTC hours; next check from "
                       f"{format_timestamp(_next_overdue_check(now))}")
    return True, f"prices.json is from {updated}, {_describe_age(age)} old"


def _overdue_hours():
    return ", ".join(f"{hour:02d}" for hour in range(0, 24, OVERDUE_CHECK_HOURS))


def _next_overdue_check(now):
    """The start of the next UTC hour divisible by OVERDUE_CHECK_HOURS."""
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hour += timedelta(hours=1)
    while hour.hour % OVERDUE_CHECK_HOURS:
        hour += timedelta(hours=1)
    return hour


def _describe_age(age):
    minutes = int(age.total_seconds()) // 60
    return f"{minutes // 60} h {minutes % 60:02d} min"


def read_existing(path):
    """The existing prices.json as a dict, or None if there is none."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise BuildError(f"cannot read existing {path}: {error.strerror}") from error
    try:
        document = json.loads(text)
    except ValueError as error:
        raise BuildError(f"existing {path} is not valid JSON; delete it to start over") from error
    if not isinstance(document, dict) or not isinstance(document.get("prices"), list):
        raise BuildError(f"existing {path} does not look like a prices file; "
                         f"delete it to start over")
    return document


def plan_history(history, source_updated_at, rows):
    """The history files to write for this build: {path: text}, changed only.

    Adds the day of source_updated_at (UTC) to the month file and the index
    in the history folder. Nothing is written here.
    """
    history = Path(history)
    day = ph.day_of_timestamp(source_updated_at)
    index_path = history / ph.INDEX_NAME
    month_path = history / ph.month_file_name(ph.month_of(day))
    index_text = _read_text(index_path)
    if index_text is None:
        raise BuildError(f"{index_path} is missing; run with --no-history to build "
                         f"prices.json alone")
    try:
        files = ph.append_day(index_text, _read_text(month_path), day, rows)
    except ph.HistoryError as error:
        raise BuildError(f"history: {error}") from error
    planned = {}
    for name, text in files.items():
        path = history / name
        if _read_text(path) != text:
            planned[path] = text
    return planned


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise BuildError(f"cannot read {path}: {error.strerror}") from error


def write_atomically(path, text):
    """Write to a temporary file next to path, then rename over it."""
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(prefix=".prices-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(source, output, force=False, allow_shrink=False, now=None, log=print, warn=None,
        history=None, check_now=False):
    """Build and write prices.json. Returns True if the file was written.

    With history (a folder), the new day is also added to the price history.
    now is the current time (UTC); it decides whether an update is due and
    becomes generatedAt. check_now (or force) checks TCGCSV even when no
    update is due or an overdue update is between checks.
    """
    warn = warn or log
    output = Path(output)
    now = now or datetime.now(timezone.utc)
    existing = read_existing(output)

    due, reason = update_due(existing, now)
    if not due and not (check_now or force):
        log(f"{reason}; nothing to do, TCGCSV not contacted")
        return False

    source_updated_at = parse_source_timestamp(source.get(LAST_UPDATED_PATH))
    if existing is not None and existing.get("sourceUpdatedAt") == source_updated_at and not force:
        log(f"TCGCSV data unchanged since {source_updated_at}; nothing to do")
        return False

    groups = parse_results(source.get(GROUPS_PATH), "groups")
    ids = group_ids(groups)
    names = {group["groupId"]: group.get("name") for group in groups}
    prices_by_group = {}
    for group_id in ids:
        results = parse_results(source.get(prices_path(group_id)), f"prices for group {group_id}")
        if not results:
            # A newly listed group can have no products yet. Skip it; the
            # shrink guard catches a group that loses its prices.
            warn(f"warning: group {group_id} ({names.get(group_id)}) has no prices; skipped")
            continue
        prices_by_group[group_id] = results

    rows = build_rows(prices_by_group)
    if not rows:
        raise BuildError("no product has a market price")
    old_count = len(existing["prices"]) if existing is not None else 0
    check_shrink(len(rows), old_count, allow_shrink)

    history_files = plan_history(history, source_updated_at, rows) if history else {}

    generated_at = format_timestamp(now)
    text = render(rows, source_updated_at, generated_at)
    # The history first and prices.json last: if a write fails part way, the
    # published file still names the previous day.
    for path in sorted(history_files):
        write_atomically(path, history_files[path])
        log(f"wrote {Path(path).parent.name}/{Path(path).name}")
    write_atomically(output, text)
    log(f"wrote {output.name}: {len(rows)} rows from {len(prices_by_group)} groups, "
        f"source updated {source_updated_at}")
    return True


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Build prices.json from TCGCSV.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help="where to write the file (default: prices.json at the repo root)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if TCGCSV has not updated since the existing file")
    parser.add_argument("--check-now", action="store_true",
                        help="check last-updated.txt even if the existing file is less than "
                             f"{UPDATE_INTERVAL.total_seconds() / 3600:g} hours old, or the "
                             "update is overdue and this is not a check hour (otherwise such "
                             "a run contacts nothing)")
    parser.add_argument("--allow-shrink", action="store_true",
                        help=f"accept a file with fewer than {MIN_KEPT_PERCENT}%% of the rows "
                             f"of the existing one")
    parser.add_argument("--offline", metavar="DIR",
                        help="read responses saved by --save-raw instead of the network")
    parser.add_argument("--save-raw", metavar="DIR",
                        help="also save every decoded response under DIR")
    parser.add_argument("--no-history", action="store_true",
                        help=f"do not add the day to the price history in {HISTORY_FOLDER}/ "
                             f"next to the output file")
    return parser.parse_args(argv)


def print_warning(message):
    """Print a warning; inside GitHub Actions also annotate the run with it."""
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message.removeprefix('warning: ')}", flush=True)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.offline:
        source = OfflineSource(args.offline)
    else:
        source = NetworkSource(save_dir=args.save_raw)
    try:
        history = None if args.no_history else Path(args.output).parent / HISTORY_FOLDER
        run(source, args.output, force=args.force, allow_shrink=args.allow_shrink,
            warn=print_warning, history=history, check_now=args.check_now)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        print("prices.json and the history were not changed", file=sys.stderr)
        return 1
    finally:
        if not args.offline:
            print(f"requests made: {source.requests}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

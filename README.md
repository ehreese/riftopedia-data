# riftopedia-data

Daily market prices for Riftbound trading cards, published as one small JSON file for the Riftopedia app, with a monthly price history for price graphs.

## What this is

A scheduled job publishes once a day, soon after TCGCSV's daily update: it reads the Riftbound price files from [TCGCSV](https://tcgcsv.com) (a daily export of TCGplayer's catalogue), and writes `prices.json` to this repository. The Riftopedia app downloads that one file at most once a day.

The job exists so that phones never contact TCGCSV directly: TCGCSV asks that requests not be made from user-facing applications, and that clients sync at most once every 24 hours.

## The file

`prices.json` is described in [docs/format.md](docs/format.md). In short: TCGplayer market prices in US dollars, keyed by TCGplayer product id, with a Normal and a Foil price where each exists.

Fetch it from:

```
https://raw.githubusercontent.com/ehreese/riftopedia-data/main/prices.json
```

## The price history

`history/` holds every day's prices since Riftbound's release on 31 October 2025, one file per calendar month (`history/2026-10.json`), plus `history/index.json`, which lists the months and the last day. The format is in the second half of [docs/format.md](docs/format.md). In short: each month file lists its days, and each product has one array of Normal prices and one of Foil prices, aligned with those days, with `null` where there was no price. A day with no data is left out, and the app draws a gap.

The history was built once from TCGCSV's daily archives (see [Rebuilding the history](#rebuilding-the-history)) and grows by one day with every new `prices.json`. A finished month never changes again.

## How the job works

The workflow [`.github/workflows/prices.yml`](.github/workflows/prices.yml) is scheduled every hour, at 17 minutes past. TCGCSV updates once a day at about 20:05 UTC, and GitHub often starts scheduled runs late (sometimes by hours, and now and then not at all), so an hourly schedule publishes each update soon after it appears. It can also be started by hand from the Actions tab ("Run workflow").

**Hourly does not mean contacting TCGCSV hourly.** A run first reads the published `prices.json`. If its `sourceUpdatedAt` (TCGCSV's last update) is less than 23 hours old, no update can be due yet: the run says so and ends green without making a single request. Only a run 23 hours or more after the last update asks TCGCSV anything, and it reads only `last-updated.txt` until the new update appears; the first run after the update does the one full download, and the 23-hour quiet period starts again. So on a normal day TCGCSV is contacted by one or two runs.

**When the update is late, the job backs off.** Once `sourceUpdatedAt` is 29 hours old or more (6 hours past the 23-hour mark), the update is overdue, because TCGCSV has not updated or runs keep failing. From then on a scheduled run asks TCGCSV only in the 00, 06, 12 and 18 UTC hours. Any other run says the update is overdue and when the next check will be, and ends green without a single request. The back-off needs no stored state: it follows from the age of `prices.json` and the clock, and it ends by itself with the next published update. With no published file, or a `sourceUpdatedAt` that is missing or in the future, there is no back-off. A run started by hand always reads `last-updated.txt`, whatever the age of the file and whatever the hour (the workflow passes `--check-now` to manual runs only).

Each run:

1. Runs the unit tests.
2. Runs [`scripts/build_prices.py`](scripts/build_prices.py), which:
   - stops, successfully and without any request, if the run was not started by hand and the published `prices.json` has a `sourceUpdatedAt` that is in the past and either less than 23 hours old, or 29 hours old or more while the UTC hour is not 00, 06, 12 or 18. With no published file, or a `sourceUpdatedAt` that is missing or in the future, it carries on;
   - reads `https://tcgcsv.com/last-updated.txt` and stops, successfully and without writing anything, if it matches the `sourceUpdatedAt` of the published `prices.json`;
   - otherwise fetches the list of Riftbound groups (TCGplayer category 89) and then each group's price file, one request at a time, at least 150 ms apart, with a 30 second timeout and a User-Agent naming this repository;
   - builds the rows described in [docs/format.md](docs/format.md);
   - adds them to the history as the day of `sourceUpdatedAt` (its UTC date): into the current month's file in `history/`, or a new file when the month is new, and moves `through` in `history/index.json`. This reads only the files already in the repository and makes no requests. If the history already ends on that day (the same TCGCSV update built twice, or a second update on the same date), the day is replaced, so building the same data twice changes nothing;
   - writes the history files and then `prices.json`, each atomically (a temporary file renamed into place). Everything is computed and checked before the first file is written.
3. If `prices.json` or anything in `history/` changed, commits them together to `main` as `github-actions[bot]` with the message `Prices as of <sourceUpdatedAt>`. If nothing changed, the run ends green with no commit.

A full run makes 2 requests plus one per group: 17 in all in November 2026. A run less than 23 hours after the last update makes none, and so does an overdue run outside the check hours. A run that goes ahead before TCGCSV's new update makes 1.

So a normal day costs TCGCSV one full run (17 requests), plus 1 request for each run that lands in the hour between 23 hours after the previous update and the new one (usually none or one). On a day TCGCSV does not update at all, the hourly runs from 23 to 29 hours after its last update each read `last-updated.txt` once (about 6 single requests), and after that only the runs in the four check hours do: 4 a day, until the update appears.

The workflow has `contents: write` permission and nothing else, uses no secrets, times out after 15 minutes, and never runs twice at once. A manual run started on a branch other than `main` builds and tests but does not commit.

## Safety checks

The script stops with a non-zero exit, leaves the published `prices.json` exactly as it was, and so turns the workflow run red, if:

- any request fails after one polite retry (the retry happens only for network errors, timeouts, HTTP 429 and 5xx, after a 5 second pause);
- a response is not valid JSON, has `success` other than `true`, or arrives brotli-encoded (only gzip is requested and supported);
- `last-updated.txt` is not a timestamp with a time zone;
- the group list is empty;
- a price row has an unexpected shape: a missing or non-integer `productId`, a `subTypeName` other than `Normal` or `Foil`, or a `marketPrice` that is neither null nor a non-negative number;
- the same product has two different prices for the same finish (after rounding to cents), for example in two groups;
- no product has a price at all;
- the new file would have fewer than 90% of the rows of the published one;
- the published `prices.json` is not readable as a prices file;
- the history is missing or broken: `history/index.json` absent or unreadable, the current month's file unreadable, missing although listed, present although not listed, or ending on a different day from the index;
- the new day is earlier than the last day of the history (past days never change).

A group whose price file is valid but empty does not stop the run. TCGplayer sometimes lists a new group before it has any products, so the script skips it and prints a warning naming the group (`warning: group <id> (<name>) has no prices; skipped`), which also appears as a warning annotation on the workflow run. If a group that used to have prices comes back empty, its rows disappear from the new file, and the 90% rule stops the run: one main set is 14 to 20% of all rows.

## Running it locally

Python 3.11 or later, standard library only. From the repository root:

```sh
# Unit tests. No network.
python3 -I -m unittest discover -s tests

# Build prices.json from TCGCSV. Contacts nothing if prices.json is less than
# 23 hours old, and does nothing more if TCGCSV has not updated.
python3 -I scripts/build_prices.py

# Same, but read last-updated.txt whatever the age of prices.json.
python3 -I scripts/build_prices.py --check-now

# Same, and keep every raw response to inspect or replay later.
python3 -I scripts/build_prices.py --save-raw .cache/raw

# Rebuild from saved responses without touching the network.
python3 -I scripts/build_prices.py --offline .cache/raw --force
```

Other options: `--force` rebuilds even when TCGCSV has not updated (and so also ignores the 23-hour rule), `--allow-shrink` accepts a file with fewer than 90% of the rows of the existing one, `--output PATH` writes somewhere other than `prices.json` (the history is then the `history/` folder next to `PATH`), and `--no-history` builds `prices.json` without touching the history. `.cache/` is ignored by git.

Please run against the network sparingly: TCGCSV is a one-person hobby project and the scheduled job already fetches everything once a day. Use `--save-raw` once and `--offline` after that.

## When a run fails

A failed run publishes nothing, so the app keeps the previous prices and shows them as stale once they are 36 hours old. There is no emergency.

Because nothing was published, the update is still due, so the next hourly run tries again, and so does every run after it until one succeeds. A failure that clears by itself (TCGCSV or the network having a bad moment) needs nothing. A failure that does not clear repeats, and each failing run makes its requests to TCGCSV again (up to a full download): every hour until the update is 29 hours old, so at most about 6 runs, and then 4 a day in the check hours (00, 06, 12 and 18 UTC). If it will not clear within a few hours, disable the workflow in the Actions tab ("Disable workflow") until the fix lands, then enable it again.

1. Open the failed run in the Actions tab and read the `Build prices.json` step. Its last lines start with `error:` and say what was wrong.
2. Decide from the message:
   - **A request failed, timed out or returned bad JSON.** Usually TCGCSV or the network had a bad moment. The next run tries again by itself (hourly, or in the next check hour once the update is overdue); there is no need to start one by hand.
   - **No Riftbound groups listed.** TCGCSV published an empty or broken group list. Its next update will normally fix it, and the scheduled runs pick that up.
   - **Two different prices for one product, or an unknown `subTypeName`.** TCGCSV's data changed shape. Look at the product named in the message. The script needs a change, and if the file the app reads has to change too, so do [docs/format.md](docs/format.md) and its `version`.
   - **Fewer than 90% of the rows.** Look for `warning: group … has no prices; skipped` lines just above the error: they name any group that came back empty, the usual cause. Then check on [tcgcsv.com](https://tcgcsv.com) whether TCGCSV really lost those products. A partial TCGCSV update fixes itself with the next update; wait for it. If the drop is genuine (for example TCGplayer removed products), run the script locally with `--allow-shrink`, check the result, and land the new `prices.json` through a pull request. The workflow never passes `--allow-shrink`.
   - **A `history:` error, or `history/index.json is missing`.** The history in the repository is inconsistent, or the new day is older than its last day. Nothing was published. Look at the named file in `history/`; fix it through a pull request, rebuilding from the archive cache if needed (see below). Never edit a finished month by hand.
   - **The unit tests failed.** Something merged to `main` broke them. Fix it through a pull request.
   - **The push to `main` failed.** Another commit landed on `main` during the run. The next run will publish normally.
3. To reproduce a failure locally, run `python3 -I scripts/build_prices.py --save-raw .cache/raw` once, then iterate with `--offline .cache/raw --force`.

## Rebuilding the history

[`scripts/backfill_history.py`](scripts/backfill_history.py) built `history/` once, from TCGCSV's daily archives (`https://tcgcsv.com/archive/tcgplayer/prices-YYYY-MM-DD.ppmd.7z`, one per day, every game together). It is not part of the daily job and is run by hand on a developer's machine. The daily job never touches the archives.

TCGCSV's rules for the archives: the files never change, downloading the whole archive once is fine, a file must not be downloaded again once you have it, and clients that download archives repeatedly may be banned. So the script:

1. **Downloads** each missing day into `.cache/history/archives/`, one request at a time, with at least a second's pause between requests (`--pause`, default 1.5 s) and the same User-Agent as the daily job. A file already in the cache is never requested again, so an interrupted run resumes where it stopped. A day TCGCSV answers 404 for is recorded in `.cache/history/not-found.txt` and not asked for again. Any other failure, including HTTP 429, stops the run at once, with no retry.
2. **Extracts** only the Riftbound members (`YYYY-MM-DD/89/<groupId>/prices`) into `.cache/history/extracted/`. The archives are 7z with PPMd compression, which Python's standard library cannot read, so this step runs `bsdtar` (libarchive; it comes with macOS). It is the only part of this repository that uses a tool outside the standard library.
3. **Builds** each day with the same rules as `build_prices.py` (the same validation, rounding and merging of groups) and writes `history/`. A day with no Riftbound prices is a missing day. It then compares its last day with the published `prices.json`.

The same cache always gives byte-identical files.

```sh
# Download what is missing (first run: 384 archives to 2026-11-18, 3.5 to 4 MB each), then build.
python3 -I scripts/backfill_history.py

# Rebuild from the cache. No network.
python3 -I scripts/backfill_history.py --offline

# Build from an extracted folder only (no network, no bsdtar).
python3 -I scripts/backfill_history.py --extracted .cache/history/extracted
```

By default it covers 31 October 2025 to the day of the published `prices.json`; `--first` and `--last` change that, and `--output` writes somewhere other than `history/`. Keep `.cache/history/` once you have it: the archives must never be downloaded twice.

## What this is not

- No card images or card art.
- No application code.
- Not a general-purpose API. The format can change; consumers should check its `version`.

## Credits and legal

- Prices come from TCGplayer by way of TCGCSV. This project is not affiliated with either.
- Riftopedia is an unofficial fan project. It is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riftbound and League of Legends are trademarks or registered trademarks of Riot Games, Inc.

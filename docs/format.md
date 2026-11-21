# `prices.json` format

Version 1. This document is the contract between the daily job and the Riftopedia app.

## Example

```json
{
  "version": 1,
  "currency": "USD",
  "source": "TCGplayer market prices via TCGCSV",
  "sourceUpdatedAt": "2026-11-16T20:05:58Z",
  "generatedAt": "2026-11-16T20:31:07Z",
  "prices": [
    [653002, null, 0.33],
    [653069, 0.25, 3.74]
  ]
}
```

## Fields

| Field | Meaning |
|---|---|
| `version` | Integer. Changes whenever the shape of this file changes. A consumer that does not know the version must ignore the file. |
| `currency` | Always `"USD"`. |
| `source` | Human-readable credit. |
| `sourceUpdatedAt` | When TCGCSV last updated its data, from its `last-updated.txt`, as a UTC timestamp. This is the "prices are from" date. |
| `generatedAt` | When the job wrote this file, UTC. |
| `prices` | One row per TCGplayer product that has at least one market price. |

## Price rows

Each row is `[productId, normal, foil]`:

- `productId` — TCGplayer product id (integer). The same id the Riftopedia catalogue uses for a printing.
- `normal` — market price of the Normal finish, or `null` if there is none.
- `foil` — market price of the Foil finish, or `null` if there is none.

Rules:

- Prices are decimal numbers in dollars with at most two decimal places.
- Rows are sorted by `productId`, ascending. A product appears at most once.
- A product with no market price for either finish has no row.
- Every product in TCGCSV's Riftbound price files is included, sealed products too. Consumers ignore ids they do not know.
- The file is written deterministically: unchanged source data produces a byte-identical `prices` array.

## For consumers

- Fetch at most once a day, with `If-None-Match` so an unchanged file costs almost nothing.
- Treat prices as stale when `sourceUpdatedAt` is more than 36 hours old.
- Never fall back to contacting TCGCSV directly.

# Price history format

Version 1. Past market prices, one file per month, for drawing price graphs. The files sit in `history/` next to `prices.json`:

```
https://raw.githubusercontent.com/ehreese/riftopedia-data/main/history/index.json
https://raw.githubusercontent.com/ehreese/riftopedia-data/main/history/2026-10.json
```

## `history/index.json`

```json
{"version":1,"months":["2025-10","2025-11","2026-10"],"through":"2026-10-04"}
```

| Field | Meaning |
|---|---|
| `version` | Integer. Changes whenever the shape of the history files changes. A consumer that does not know the version must ignore the history. |
| `months` | Every month that has a file, ascending, as `YYYY-MM`. A month with no day of data has no file and is not listed. |
| `through` | The last day in the history, `YYYY-MM-DD`. Always in the last listed month. |

Read the index first and fetch only the months it lists; never guess file names.

## `history/YYYY-MM.json`

Example (three days, with no data for 3 October):

```json
{"version":1,"month":"2026-10","currency":"USD","source":"TCGplayer market prices via TCGCSV",
"days":["2026-10-01","2026-10-02","2026-10-04"],
"prices":[
[10,[0.25,0.3,0.31],[3.74,3.5,null]],
[20,[1.0,1.1,null],[null,2.0,null]],
[30,[null,5.0,5.0],null]
]}
```

| Field | Meaning |
|---|---|
| `version` | As in the index. |
| `month` | The month this file covers, `YYYY-MM`. Equal to the file name. |
| `currency` | Always `"USD"`. |
| `source` | Human-readable credit. |
| `days` | The days of this month that have prices, ascending, as `YYYY-MM-DD`. Never empty. |
| `prices` | One row per TCGplayer product with at least one market price on at least one of `days`. |

### Days

- A day is a UTC calendar date: the date of the TCGCSV update the prices come from. For the daily job that is the date of `prices.json`'s `sourceUpdatedAt`; for the backfill, the date of TCGCSV's daily archive. The two agree: the archive for a date holds that date's update.
- Days are not consecutive. A day TCGCSV has no archive for, or whose data has no Riftbound prices at all, is not in `days`. Draw a gap; do not invent a value and do not treat it as zero.
- Each day holds exactly what `prices.json` held (or would have held) for that update, so the last day in the history and a `prices.json` with the same date have the same prices.

### Price rows

Each row is `[productId, normal, foil]`:

- `productId`: the TCGplayer product id, as in `prices.json`.
- `normal`: an array with one entry per day in `days`, in the same order: the market price of the Normal finish on that day, or `null` when it had none. When the product had no Normal price on any day of the month, `normal` is `null` instead of an array.
- `foil`: the same for the Foil finish.

Rules:

- Every array has exactly as many entries as `days`. Entry `i` belongs to `days[i]`.
- Prices are decimal numbers in dollars with at most two decimal places, rounded exactly as in `prices.json`.
- Rows are sorted by `productId`, ascending. A product appears at most once per file.
- A product with no price on any day of the month has no row. `normal` and `foil` are never both `null`.
- A `null` inside an array means TCGplayer had no market price for that finish that day (the product was unlisted, unsold, or not yet released). It is not zero.
- Products come and go: a new printing gets a row from its first priced day, with `null` before it; a product TCGplayer stops listing keeps `null` after its last day.

### Stability

- A month file changes only while its month is the latest one. Once a later day exists, every earlier day and every earlier month file is final and never changes again.
- The latest day can be replaced, never removed: if TCGCSV updates twice on one UTC date, the day holds the later update.
- Files are written deterministically: the same data gives byte-identical files. Each file is compact JSON with one price row per line; consumers must not depend on the line layout.

## For consumers of the history

- Fetch `history/index.json` at most once a day, with `If-None-Match`. Fetch a month file only when it is listed and you do not already have it, or it is the last listed month and `through` moved on.
- Finished months never change, so cache them for good.
- Never fall back to contacting TCGCSV directly.

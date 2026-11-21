# riftopedia-data — working rules

This is a **public** repository. It holds one scheduled job and the price file it publishes for the Riftopedia app. Read this file before doing any work here.

## Hard rules

- Nothing private goes in this repo: no personal names, email addresses, local file paths, tokens, or anything from the private `riftdex` app repository beyond what this job needs.
- No card images or card art, ever.
- Commits made by hand use the repository's configured no-reply identity. Do not change the git identity.
- Python 3 standard library only. No third-party packages, in the script or in the workflow.

## Being a good guest of TCGCSV

TCGCSV is a one-person hobby project. The job must:

- Check `https://tcgcsv.com/last-updated.txt` first and do nothing more if the data has not changed since the published file.
- Contact TCGCSV from at most a few runs a day and fetch each URL once per run, with a pause of at least 150 ms between requests and no parallel requests. The job is scheduled hourly so each day's update is published soon after it appears, but a run makes no request at all unless an update is due (23 hours after the last one), and once an update is overdue only four runs a day ask.
- Send a descriptive User-Agent that names this repository.
- Never retry in a tight loop. On any failure, stop, exit non-zero, and leave the previously published file untouched.

## Pull requests

- Work lands through stacked PRs made with the `gh stack` extension (`gh stack init`, `gh stack add`, `gh stack submit --auto --open`). Never commit work directly to `main`.
- Each PR does one thing and says in its description what changed and how it was verified.
- Only the project manager merges.

## Format changes

`docs/format.md` is the contract with the app. Changing the shape of `prices.json` means bumping its `version` and updating that document in the same PR.

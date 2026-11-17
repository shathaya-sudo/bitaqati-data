# Bitaqati data · بيانات بطاقاتي

Public catalog consumed by the Bitaqati iOS app (Qatar card-discount aggregator).

- `seed.json` — programs, merchants, branches and offers. Every offer carries its official `source` URL, `lastCheckedAt` and `verified`.
- `venues.json` — malls and destinations (centre, radius, aliases) used to group branches.
- `parts/testaahel.json` — the Testaahel (تستاهل، وزارة الدفاع) part, rebuilt from testaahel.qa by the scheduled job.
- `tools/` — the refresh pipeline (standard-library Python only).

The GitHub Actions workflow runs every two days: crawl → merge (pruning offers that vanished) → flag approximate locations → integrity check → commit. The app downloads `seed.json` and swaps it in when `generatedAt` is newer.

Offers are the issuers' public listings, reproduced for identification; discounts are honoured by the merchant under the issuer's terms, not by this project.

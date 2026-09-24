# News Engine

The news layer exists to do one thing well: stop the bot from buying into a shock. It is
free-only (no paid feed), fail-closed, and deliberately conservative about unverified
sources.

```
collectors → parser → deduper → credibility → impact → decay → correlation → state → G4
```

## 1. Sources

| Source | Tier | Credibility | Status |
|---|---|---|---|
| CFTC RSS (`cftc.gov/RSS/RSSGP/rssgp.xml`) | 1 PRIMARY_OFFICIAL | 1.00 | VERIFIED |
| SEC press releases RSS | 1 PRIMARY_OFFICIAL | 1.00 | VERIFIED |
| Federal Reserve `press_all.xml` | 1 PRIMARY_OFFICIAL | 1.00 | VERIFIED |
| CoinDesk RSS | 2 REPUTABLE_SECONDARY | 0.80 | VERIFIED |
| GDELT DOC API (JSON) | 2 REPUTABLE_SECONDARY | 0.80 | VERIFIED |
| Binance announcements, CoinDCX blog, The Block, Decrypt | — | — | **UNPROVEN → disabled** |

Unproven sources are shipped `enabled: false, status: UNPROVEN`. They are skipped at
collection time and reported in `sources_unverified` on every snapshot, so nothing is
silently trusted. A probe that proves one of them can flip it to VERIFIED with one config edit.

Multi-source confirmation adds +0.15 (two independent tiers) or +0.25 (three), capped at 1.00.

## 2. The credibility ceiling

**A single unverified rumour can never produce HIGH or CRITICAL.** This is enforced in code,
not documentation:

```
can_raise_high(tier, corroborating) = (tier <= 2) and (corroborating >= 1)
```

Social-discovery items (tier 4, credibility 0.30) are therefore capped at MEDIUM. A tier-2
source needs at least one *independent* corroborating source before its item can be graded
HIGH, and a critical grade also requires tier ≤ 2 with corroboration.

## 3. Impact model

```
impact = max(category_weight) × credibility × (0.5 + 0.5 × novelty)
         × (1.15 if market_wide else 1.0) × liquidity_sensitivity      → clipped to [0, 1]
```

* **category_weight** — `BLACK_SWAN 1.00`, `HACK/EXPLOIT/FED/FOMC 0.95`, `STABLECOIN 0.90`,
  `ETF/MACRO/SEC 0.85`, `REGULATION/GEOPOLITICAL 0.80`, `CFTC 0.75`, `EXCHANGE 0.70`,
  `DELISTING 0.65`, `LIQUIDATION/INSTITUTIONAL 0.60`, … `GENERAL 0.30`.
* **novelty** — 1 − max Jaccard similarity against items from the last 72 hours, so a
  re-reported story carries almost no weight.
* **liquidity_sensitivity** — 1.10 for market-wide items, 1.00 otherwise.
* **corroboration bonus** — applied inside the credibility term.

Severity: **CRITICAL** ≥ 0.80 (tier ≤ 2, corroborating ≥ 1) · **HIGH** ≥ 0.60 (tier ≤ 2) ·
**MEDIUM** ≥ 0.35 · else **LOW**.

## 4. Mandatory decay

```
decay(t) = impact × 0.5 ** (age_min / half_life_min)
```

Half-lives are per category: `BLACK_SWAN 480 min`, `FED/FOMC 240`, `ETF/SEC/STABLECOIN 180`,
`CFTC/EXCHANGE/MACRO 120`, `HACK/EXPLOIT 90`, `DEFAULT 60`. Items whose decayed impact falls
below the floor (0.05) stop steering signals entirely — a week-old headline cannot justify a
new entry, and an item expires after three half-lives.

## 5. Deduplication and corroboration

Exact repeats collapse onto an hour-bucketed hash. Near-duplicates collapse by token
Jaccard ≥ 0.82 over normalized headlines. The same cluster arriving from independent tiers is
what raises credibility; the same story from the same source never inflates it.

## 6. Correlation to the tradeable universe

`NewsCorrelationEngine` maps assets to configured pairs and flags *direction conflicts*: a
BEARISH market-wide item argues against a LONG candidate. G4 blocks on state; the correlation
engine is what lets the DANGER monitor tell you *which* pair an event touches and *why* a
signal is now contradicted.

## 7. State → G4

| Engine state | Meaning | G4 |
|---|---|---|
| `CLEAR` | ≥ `min_sources_healthy` sources responded, no blocking item | pass |
| `DEGRADED` | a HIGH item is live (decayed impact above the floor) | degrade confidence |
| `BLOCK` | fewer sources than required, **or** a CRITICAL item, **or** a blackout window | **block** |

The source-count check is what makes the layer fail-closed: if the feeds go dark, the bot
stops trading rather than trading blind.

## 8. Per-item record

Each item is journalled with: timestamp · source · tier · headline · url · entity · category ·
credibility · novelty · relevance · expected direction · confidence · corroborating sources ·
affected assets · impact · decayed impact · severity · half-life · expiry — enough to
reconstruct exactly what the bot knew when it decided.

```sql
select ts, source, severity, impact, headline from news order by ts desc limit 10;
```

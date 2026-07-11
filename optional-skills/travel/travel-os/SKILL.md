---
name: travel-os
description: Optimise UK points, rewards, and premium travel
version: 0.1.0
author: Frank Spencer
license: MIT
---

# Travel OS

Use this skill for UK credit-card rewards, airline miles, hotel points, award-seat searches, premium-travel deals, companion vouchers, transfer bonuses, and cash-versus-points decisions.

## Primary objective

Maximise real holiday value for Frank and his household while protecting flexible points from irreversible or poor-value transfers.

## Default user context

- Home market: United Kingdom
- Home region: Surrey, England
- Preferred departure airports: London Heathrow, London Gatwick, then other UK or nearby European gateways when worthwhile
- Current flexible balance: 195,000 American Express Membership Rewards points, unless the ledger says otherwise
- Approximate combined personal and business card spend: £10,000 per month
- Travellers: Frank, Cecily, and children; verify exact party size and ages for each trip
- Preference: premium travel and strong value rather than loyalty to one airline or hotel chain

## Non-negotiable rules

1. Never recommend transferring flexible points speculatively unless the user explicitly accepts the risk.
2. Verify that a promotion applies to UK-issued cards and UK loyalty accounts. Do not assume US offers apply.
3. Confirm live award availability, points price, taxes, carrier surcharges, cancellation rules, transfer time, and seat count before recommending a transfer.
4. Treat airline and hotel transfers as irreversible unless an official programme rule explicitly says otherwise.
5. Compare the full trip, including positioning flights, overnight hotels, luggage, seat fees, transfers, and family logistics.
6. Never recommend manufactured spending, cash-equivalent cycling, false business transactions, or activity likely to breach issuer or loyalty-programme terms.
7. Prefer official programme sources for current rules and prices. Use trusted specialist blogs for analysis, historical context, and deal discovery.
8. State the date checked for every time-sensitive recommendation.

## Research hierarchy

Use sources in this order:

1. Official issuer, airline, hotel, airport, and programme pages
2. Head for Points for UK-specific interpretation
3. One Mile at a Time and Frequent Miler for international sweet spots and programme analysis
4. AwardWallet, FlyerTalk, and relevant specialist communities for corroboration and edge cases
5. Search tools such as Seats.aero, PointsYeah, Roame, airline calendars, and Google Flights when available

Do not rely on a single blog post for a live promotion or current award chart.

## Core workflow

### 1. Read state

Read the files in `~/.hermes/travel-os/` when available:

- `profile.yaml`
- `points-ledger.yaml`
- `cards.yaml`
- `vouchers.yaml`
- `watchlist.yaml`
- `active-searches.yaml`

If files do not exist, use the templates bundled with this skill and create them only after obtaining the necessary facts.

### 2. Classify the request

Choose one primary workflow:

- `redeem`: best use of an existing points balance
- `earn`: best cards, referrals, spend allocation, and promotions
- `search`: find award seats or cash fares for a trip
- `evaluate`: assess a specific transfer bonus, card offer, fare, or redemption
- `monitor`: define or run recurring deal checks
- `plan`: build a complete points-led holiday

### 3. Verify live facts

For current promotions, prices, availability, rules, or schedules, use live web research. Confirm UK eligibility separately.

### 4. Calculate value

Use both of these measures:

`net_cash_saved = comparable_cash_price - unavoidable_redemption_cash_cost - extra_positioning_costs`

`value_per_point_pence = (net_cash_saved / points_used) * 100`

Also report:

- points transferred
- points received after any bonus
- taxes and fees
- number of travellers
- cabin and route
- cancellation flexibility
- booking difficulty
- remaining flexible balance

Do not inflate value using an unrealistic fully-flexible cash fare when the user would normally buy a cheaper ticket.

### 5. Score the opportunity

Score each opportunity from 0 to 100:

- Net value: 30
- Availability for the full party: 20
- Product quality: 15
- Low taxes and fees: 10
- Flexibility and cancellation terms: 10
- Ease of booking: 5
- Route convenience: 5
- Strategic fit with expiring vouchers or bonuses: 5

Classify:

- 85–100: exceptional; act quickly after final verification
- 70–84: strong
- 55–69: reasonable
- below 55: usually preserve points or pay cash

### 6. Present a decision

Lead with one recommendation, not a long unranked list. Then include up to three alternatives.

For every recommendation, state one of:

- `BOOK NOW`
- `TRANSFER AFTER HOLD/CONFIRMATION`
- `WATCH`
- `PAY CASH`
- `AVOID`

## Redemption-specific rules

- Avios may be moved between compatible Avios programmes at 1:1 where account rules permit; this does not create additional points.
- A companion or upgrade voucher must be evaluated as part of the total itinerary, not valued in isolation.
- Dynamic programmes such as Virgin Atlantic Flying Club and Flying Blue require live date searches.
- Partner awards must be confirmed by the programme that will actually issue the ticket.
- For family travel, do not recommend an itinerary unless the required number of seats is available or a clearly labelled split-cabin/split-flight strategy is acceptable.
- Check infant and child pricing because some programmes charge differently from adult awards.

## Earning-specific rules

When evaluating cards, include:

- current welcome bonus and eligibility
- annual fee
- spend threshold and deadline
- base and category earning
- voucher threshold
- referral opportunity
- retention, downgrade, or cancellation path
- non-Amex backup strategy
- whether business spend is eligible and commercially sensible

Optimise thresholds first, then marginal everyday earning.

## Monitoring rules

A useful alert must be actionable. It should include:

- what changed
- why it matters to Frank
- deadline
- exact points and cash requirement
- seat count or inventory evidence
- source links
- recommended action

Suppress routine news, weak discounts, US-only offers, and speculative rumours.

## Output format

Use this compact structure:

1. **Verdict**
2. **Best opportunity**
3. **Numbers**
4. **Why it wins**
5. **Risks and checks**
6. **Next action**

When comparing multiple options, use a short ranked table.

## Final safety check

Before advising a transfer or application, confirm:

- UK eligibility verified
- live availability verified
- full cash cost verified
- account names and transfer-linking requirements checked
- expiry and cancellation terms checked
- user has not already transferred or applied
- recommendation is compliant with published programme terms

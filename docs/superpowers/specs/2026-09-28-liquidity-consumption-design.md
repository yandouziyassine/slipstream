# Paper Fills That Consume Liquidity — Design

Date: 2026-09-28
Status: Approved design (approved in conversation)
Scope: Phase 2, sub-project 4 of `2026-09-26-low-latency-pipeline-design.md`.

## 1. Purpose

Today a paper fill leaves the book untouched. A child that walks three levels leaves them all in place, so the next child of the same parent order sees the same liquidity again. Reported costs are therefore optimistic, most of all for orders that are large next to the displayed depth. This is the largest accuracy gap in the project.

After this change, an order pays for the liquidity it has already taken. Its later children see the displayed size minus what it took, until the venue's feed shows that the level changed.

Paper trading only: the engine's books still mirror the venue feeds exactly. Consumption is a separate record per order, never a change to the book.

## 2. Decisions

| Question | Decision | Why |
|---|---|---|
| Whose view is reduced | Each parent order has its own consumption record | The four algorithms in `compare` run side by side on the same feed. They must not deplete each other's liquidity, or the comparison would depend on submission order. In reality only one of them would run. |
| When a record lapses | When the feed changes that level: a snapshot, or a delta that sets that price | The new quantity is the venue's current truth. Our paper orders never reached the venue, so the feed cannot show them; after a change the level is treated as fresh. |
| Reset cost | O(levels touched) per feed message; no scan of orders | Engine latency is a measured KPI. The single-writer loop must not do per-order work on every market message. |
| Counterfactual single-venue costs | Each venue-alone counterfactual keeps its own consumption record too | Routing gain compares the routed order with "the same order on venue v alone". Both sides must pay for their own depletion, or the comparison is not apples-to-apples. |
| Risk | Unchanged: `check_child` runs on the routed child's cost after consumption, before any fill | Hard limits apply to the real (worse) cost. |

## 3. Design

### 3.1 Level versions (the reset rule)

`OrderBook` stores `{qty, version}` per price. The book keeps one counter. Every level that a snapshot writes, and every level that a delta sets to a non-zero quantity, gets the next counter value. A delta that removes a level erases it, and truncation to the book depth erases levels too. A version therefore names one level at one price, as of its last feed change.

`OrderBook::versioned_liquidity_for(side)` returns the taker side best-first as `BookLevel{price, qty, version}`. `liquidity_for` is unchanged.

An order's record is keyed by version. When the feed touches a level, the level gets a new version, so every order's record for it simply stops matching: that is the reset. The update itself costs O(levels touched). Nothing walks the orders, and records lapse lazily.

### 3.2 Consumption record

`ConsumptionOverlay` (`consumption_overlay.{h,cpp}`): what one order has taken from one venue's book, as a vector of `{version, taken}` sorted by version.

- `remaining(levels)`: each level's displayed quantity minus what this order took at that version. Levels with nothing left are left out. The result is floored at zero: it is never negative. A remainder below 1e-9 of the displayed size is rounding noise from summed takes, so it counts as nothing left.
- `record(levels, taken)`: `taken[i]` is the quantity taken from the `i`-th level of `remaining(levels)`. The overlay is rebuilt from the current levels, so entries for levels that no longer exist, or that have a new version, are dropped here.
- `clear()`: releases the memory.

The overlay is keyed by version alone. Versions are unique per book across both sides, and each order trades only one side.

### 3.3 Engine

`ParentOrder` gains:
- `taken[v]`: what the routed order took from venue `v`.
- `alone_taken[v]`: what the counterfactual "same order on venue `v` alone" took from `v`.

Each child:
1. For every fresh venue, the order's liquidity is `taken[v].remaining(book)`, then the price collar.
2. The router splits the child exactly as before. Each `RouteLeg` now also reports `taken`: the quantity taken from each of that venue's levels, best-first. It is a prefix of the venue's levels, trimmed by the step floor.
3. `check_child` runs on the routed cost. If it fails, the order halts and nothing is recorded.
4. The fills are emitted and each leg is recorded in `taken[leg.venue]`.
5. For each venue still available, the same filled quantity is walked on `alone_taken[v].remaining(book)` without minimum sizes, as before. If `v` cannot fill it, `v` becomes unavailable for this order and `alone_taken[v]` is cleared. Otherwise its cost is added and the walk is recorded in `alone_taken[v]`.

The one-shot immediate cost at submit sweeps the displayed book once, so it has no history to consume and is unchanged.

### 3.4 Memory

An overlay only holds entries for levels in the current book side, so it has at most `book_depth` entries. A working order holds at most `2 × venues × book_depth` entries. When an order completes or halts, both overlay vectors are released. `Engine::consumption_entries()` reports the total so tests can check the bound and the release.

### 3.5 Complexity

- Feed message: O(levels touched), plus the existing map update. No per-order work.
- Child: building each venue's liquidity costs O(depth × log depth), because each level does a binary search of the overlay. Recording costs O(depth × log depth). Routing is unchanged.

## 4. What changes in results

- A multi-child order whose children meet the same level before the feed changes it now pays for deeper levels. For the same replay, costs go up or stay the same. They never go down, because the only effect is to remove liquidity the order already took.
- A single-slice order and the one-shot immediate cost are unchanged.
- Single-venue costs also rise, because each counterfactual depletes its own venue. The routed order spreads over venues and depletes each one less, so routing gain can grow. That growth is real: it is the benefit of not walking one book alone.
- A venue that cannot fill a child from what is left after its own counterfactual history now reports "unavailable" more often.
- Orders still never deplete each other, so `compare` stays a fair side-by-side comparison.

Measured on the replay fixtures (before → after, buy orders, cost in bps against the arrival mid):

| Fixture and order | Before | After | Why |
|---|---|---|---|
| `kraken_btcusd_replay`, 0.06 in 3 slices (the integration test) | 0.833 | 0.833 | Each child lands just after the feed refreshes or improves the level it needs |
| `kraken_btcusd_replay`, 0.09 in 6 slices | 0.833 | 1.111 | Children between refreshes now walk past what earlier children took |
| `two_venue_btcusd_replay`, single slice | 1.850 routed, Kraken 1.950, Coinbase 3.050 | unchanged | One child: nothing to consume yet |
| Schedules fixture (one snapshot, no refresh), 0.04 in 4 slices, each algorithm | TWAP 1.000, VWAP 1.250, POV 1.375, AC 1.176 | 2.250 for all four | The book never refreshes, so every schedule ends up walking the same 0.04 of displayed depth: the same cost as sending it at once |

## 5. Testing

Test-first in C++:
- Overlay unit tests: partial take, full take hides the level, a take larger than displayed floors at zero, a new version resets, taken quantities align with the visible levels, lapsed entries are dropped.
- Book: a delta renews only the versions it sets, and a snapshot renews all of them.
- Router: `RouteLeg::taken` per level, including the step floor.
- Engine: the second child sees reduced size; a delta at that price resets and a delta at another price does not; a snapshot resets; other orders are unaffected; multi-venue; counterfactual consistency (with one venue, the routed cost equals the venue-alone cost); the risk limit sees the consumed cost; completion and halt release the overlays; entries stay bounded across many refreshes.
- Replay integration expected values are updated only where the change is explained by consumption. Costs go up or stay the same.

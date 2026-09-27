# Product Marketing Context

*Last updated: 2026-09-27 (V1, drafted from the repo and planning conversations; the owner will correct it)*

## Product Overview
**One-liner:** Slipstream shows what it really costs to buy or sell crypto, including fees, and how much you'd save by splitting the order and sending each piece to the cheapest exchange.

**What it does:**
- Every hour it runs a small paper order against the live Kraken and Coinbase order books, using four execution strategies (TWAP, VWAP, POV, Almgren-Chriss).
- It routes each slice to whichever exchange is cheapest once fees are included.
- It publishes the measured all-in cost, the split between fees and slippage, the routing gain and the engine latency. No real orders are placed.

**Product category:** execution cost analysis, transaction cost analysis (TCA), smart order routing, crypto market microstructure research.

**Product type:** open-source project with a public research page. A public API and an MCP server for AI assistants come next.

**Business model:** free and open source (MIT). Portfolio and research project; no pricing.

## Target Audience
**Primary use case:** find out how much execution strategy, venue and fee tier really move the price you pay for crypto.

**Jobs to be done:**
- "Before I place a big order, tell me the cheapest way to do it."
- "Show me, with real data, whether splitting an order is worth it."
- "Let me judge this developer's engineering and finance skills from something live."

**Use cases:**
- A trader checks whether routing between two exchanges beats their main exchange.
- A quant hobbyist compares execution algorithms over weeks of data.
- A recruiter opens the page from a CV and sees a running system backed by real numbers.

## Personas
| Persona | Cares about | Challenge | Value we promise |
|---|---|---|---|
| Active crypto trader | Paying less on each trade | Can't see fees and slippage together; uses one exchange out of habit | Real all-in cost per strategy and exchange, updated hourly |
| Quant / algo hobbyist | Evidence and methodology | Execution research needs data and infrastructure | Open methodology, raw CSV, open-source engine to reproduce everything |
| Recruiter / hiring engineer | Real skills, not tutorials | Most portfolio projects are static toy apps | A live, tested, low-latency C++/Python system with measured results |

## Problems & Pain Points
**Core problem:** a large market order moves the price against you, and exchange fees are often bigger than the slippage. Most people see neither number clearly.

**Why alternatives fall short:**
- Exchange apps show only their own book and their own fee.
- Institutional TCA tools are expensive and closed.
- Comparison sites compare prices, not what it actually costs to execute an order.

**What it costs them:** tens of basis points per trade, which at 0.4–0.6% taker fees adds up to more than slippage on most orders.

**Emotional tension:** "Am I overpaying without knowing it?"

## Competitive Landscape
**Direct:** institutional TCA and smart-order-routing vendors. Closed, expensive, not built for individuals.

**Secondary:** exchange fee pages and aggregator price-comparison sites. They show price or fees, never both measured on a real order.

**Indirect:** "just use a market order on one exchange". Simple, but paying for it is invisible.

## Differentiation
**Key differentiators:**
- Measured on real live order books, hourly, not estimated or backtested only.
- Counts fees and slippage together (all-in cost).
- Shows the single-venue counterfactual for every order, so the routing gain is honest, including when it's zero.
- Open source, with a methodology page and raw data.

**Why that's better:** you see the true cost and the real alternatives on the same data at the same moment.

**Why customers choose us:** it's honest (it reports zero gain when there is none), transparent and free.

## Objections
| Objection | Response |
|---|---|
| "Paper trades aren't real." | The prices are real, live Kraken and Coinbase books. Only the fill is simulated, and the methodology states the one known simplification: paper fills don't consume liquidity. |
| "Your fees aren't mine." | The fees are published entry-tier rates. Your tier changes the numbers; the method is the same. |
| "Is this financial advice?" | No. It's research and education. The page says so clearly. |

**Anti-persona:** people looking for trading signals or price predictions. Slipstream measures execution cost, not direction.

## Switching Dynamics
**Push:** hidden costs; one-exchange habit; no data.

**Pull:** hard numbers, updated hourly, free.

**Habit:** "I always use exchange X."

**Anxiety:** "Is this data trustworthy?" A methodology page, open source and raw CSV answer it.

## Customer Language
**Words to use:** all-in cost, fees included, slippage, basis points (with a plain-English hint: 1 bps = 0.01%), paper trade, cheapest route, measured, hourly, open source.

**Words to avoid:**
- Anything that sounds like AI-startup hype: "unlock", "supercharge", "revolutionize", "seamless", "cutting-edge", "AI-powered", "game-changer", "leverage".
- Promises about profit or returns.

**Glossary:**
| Term | Meaning |
|---|---|
| bps | basis point, 0.01% |
| All-in cost | slippage plus fees, versus the market mid when the order started |
| Routing gain | best single exchange's cost minus the routed cost |
| TWAP / VWAP / POV / Almgren-Chriss | ways to split an order over time |

## Brand Voice
**Tone:** plain, precise, a little dry, honest about limits.

**Style:** short sentences, numbers first, first-person developer notes ("I run this every hour…"). Like a good engineering blog or an FT data chart, not a startup landing page.

**Personality:** rigorous, transparent, understated, curious.

## Proof Points
**Metrics (live):**
- The engine uses one writer thread and is tested under ThreadSanitizer.
- 231 C++ tests and 440 Python tests.
- Each market message is measured at p50 and p99 latency.
- Every fill respects the exchange's real minimum order size.

**Value themes:**
| Theme | Proof |
|---|---|
| Honest numbers | Reports a routing gain of 0 when one exchange is simply cheaper |
| Real data | Live Kraken and Coinbase books; exchange rules fetched from their APIs |
| Engineering quality | Sanitizer-tested C++20, fuzz and property tests, security reviews on every PR |

## Goals
**Business goal:** credibility, as a public, live portfolio piece and a useful free research page.

**Conversion action:** click through to GitHub (star or read the code), and later try the API or MCP server.

**Current metrics:** none yet (the page isn't live).

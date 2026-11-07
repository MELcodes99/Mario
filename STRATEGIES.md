# Mario strategy notes

What has worked in Solana memecoins, what Mario does with it, and what it refuses to do.
Researched November 2026. Every strategy below only *nominates* a token. Each nominee still has to clear
the security gate, score at least 85/100, pass the risk engine and pass transaction simulation.

## The base rates

- Only around **1–2% of Pump.fun launches graduate** to a real DEX pool. In weak markets the rate has dropped
  to **0.26%** (June 2026). After the BOOST launch change it rose to about **6.7%**.
- CoinGecko found that **about 80% of Pump.fun tokens stop trading on the day they launch or the next day.**
- Graduation does not mean survival. A token can graduate, spike, and still collapse within hours.
- Industry estimates put the share of losing memecoin trades at **70–85%**.

So Mario's default answer is "no". The directive's 80% win rate is a research target. Mario reports
expectancy (average result per trade) next to win rate, because a high win rate with large losses still loses money.

## Implemented

| # | Strategy | Evidence | How Mario runs it |
|---|---|---|---|
| 1 | **Smart-money confluence** (copy trading) | Copy trading is now the main way traders enter memecoins. Luo et al. (WWW'26) show that trader profitability is predictable, but bots farm naive copiers. Their copiers made about **3% per trade** after real costs. | You seed wallets (GMGN smart money, Birdeye top traders, Dune). Mario grades each wallet itself: a win means +25% within 6h, and wallets that flip within 2 minutes are marked as quick-flippers. It retires bad wallets automatically. A signal needs **at least 2 graded wallets within 30 minutes**. One wallet is never enough. |
| 2 | **Graduation retest** | The first minutes after migration are the most volatile, as early bonding-curve buyers sell. | Pump.fun graduations come from the PumpPortal stream. Mario waits at least 15 minutes, then buys a held retest at 70–97% of the post-migration high, with liquidity holding and buyers ahead of sellers. |
| 3 | **Volume breakout → retest** | Classic momentum: a breakout backed by volume, entered on consolidation rather than at the top. | Hourly volume must be 1.5–6x the 6h average, with price up more than 8% over 1h, flat over 5m, and within 12% of the recent high. |
| 4 | **Early narrative** | Memes move on culture and attention. Early, organic attention beats saturated attention. | The Anthropic analyst researches the token, with web search when enabled. It nominates only an *emerging or accelerating* narrative with organic attention and confidence of at least 60. DexScreener boosts are paid, so they are never treated as organic. |
| — | **Laddered exits** | A common pro habit is to take the initial stake out early. | Sell 25% at +15%, +30% and +50%, then trail the last 25% by 15% (10% when momentum fades). After the first take-profit the stop moves to breakeven, and stops only ever move up. |
| — | **Anti-FOMO** | Chasing vertical candles is how most retail traders become exit liquidity. | If price is up more than 35% in 15 minutes, Mario waits up to 90 minutes for a retest, then gives up. |

## Deliberately not implemented

| Strategy | Why not |
|---|---|
| Sniping launches in the first seconds | The directive keeps this disabled until it has its own validated record. More than 98% of launches never graduate, and sniping is a bot-latency arms race. |
| Buying the bonding curve when 50–70% full | Liquidity is below the $50k minimum and the pair is younger than 15 minutes, so it fails the hard rules. |
| Following KOL / influencer calls | KOLs often dump into their own followers. You can add KOL wallets as smart wallets, but they must earn a grade like any other wallet. |
| Chasing DexScreener trending or boosted lists | Those slots are paid. Mario uses them for discovery only, never as a buy signal. |
| Bundled launches, volume bots, wash trading, coordinated shilling | These are market manipulation, not strategies. Mario detects and rejects tokens that show these patterns. |

## Sources
- CoinGecko Research — average lifespan of Pump.fun tokens
- The Block — Pump.fun token graduation rate jumps after BOOST (July 2026)
- Crypto Briefing — Pump.fun graduation rate falls to 0.26% (June 2026)
- Solana Compass — Pump.fun launched 42,000 tokens in one day, fewer than 2% reach a DEX
- Luo, Feng, Xu, Liu — *Resisting Manipulative Bots in Meme Coin Copy Trading* (arXiv 2601.08641, WWW'26)
- Altrady — Pump.fun and Solana memecoin trading guide (May 2026)

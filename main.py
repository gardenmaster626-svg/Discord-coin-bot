import os
from dotenv import load_dotenv
import discord
from discord.ext import tasks
import requests
import asyncio
import time
alerted_contracts = set()
load_dotenv()

token = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
def get_tokens():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            print("Token profile request failed:", response.status_code)
            return []

        try:
            return response.json()
        except ValueError:
            print("Token profile API returned invalid JSON")
            return []

    except requests.RequestException as e:
        print("Token profile request error:", e)
        return []

def get_token_pairs(chain_id, token_address):
    url = f"https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"
    response = requests.get(url, timeout=10)
    return response.json()

def get_best_pair(pairs):
    if not pairs:
        return None

    return max(
        pairs,
        key=lambda p: (p.get("liquidity") or {}).get("usd") or 0
    )


def check_honeypot(token_address, chain_id):
    try:
        url = "https://api.honeypot.is/v2/IsHoneypot"

        response = requests.get(
            url,
            params={"address": token_address, "chainID": chain_id},
            timeout=10
        )

        if response.status_code != 200:
            return {
                "verified": False,
                "is_honeypot": True,
                "sell_tax": None,
                "risk_level": 100
            }

        data = response.json()

        simulation_success = data.get("simulationSuccess", False)
        honeypot_result = data.get("honeypotResult") or {}
        simulation_result = data.get("simulationResult") or {}
        summary = data.get("summary") or {}

        return {
            "verified": simulation_success is True,
            "is_honeypot": honeypot_result.get("isHoneypot", True),
            "sell_tax": simulation_result.get("sellTax"),
            "risk_level": summary.get("riskLevel", 100)
        }

    except Exception:
        return {
            "verified": False,
            "is_honeypot": True,
            "sell_tax": None,
            "risk_level": 100
        }
SECURITY_CHAIN_MAP = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "robinhood": "4663"
}

def get_bubble_clusters(network, token_address):
    try:
        api_key = os.getenv("INSIGHTX_API_KEY")

        if not api_key:
            print("InsightX API key not found")
            return None

        url = f"https://api.insightx.network/dex-metrics/v1/{network}/{token_address}/clusters"

        response = requests.get(
            url,
            headers={"X-API-Key": api_key},
            timeout=10
        )

        if response.status_code != 200:
            print("InsightX cluster check failed:", response.status_code)
            print(response.text)
            return None

        data = response.json()

        # API may return the cluster data directly or inside "data"
        return data.get("data", data)

    except Exception as e:
        print("InsightX cluster check error:", e)
        return None
def get_token_security(chain_id, token_address):
    try:
        url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

        response = requests.get(
            url,
            params={"contract_addresses": token_address},
            timeout=10
        )

        if response.status_code != 200:
            print("GoPlus security check failed:", response.status_code)
            print(response.text)
            return None

        data = response.json()
        result = data.get("result") or {}

        for address, token_data in result.items():
            if address.lower() == token_address.lower():
                return token_data

        return None

    except Exception as e:
        print("GoPlus security check error:", e)
        return None
def rug_risk_score(pair, honeypot, security_data):
    risk = 0

    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    if security_data:
        creator_percent = security_data.get("creator_percent")
        owner_percent = security_data.get("owner_percent")

        try:
            if creator_percent is not None and float(creator_percent) >= 10:
                risk += 15
        except (TypeError, ValueError):
            pass

        try:
            if owner_percent is not None and float(owner_percent) >= 10:
                risk += 15
        except (TypeError, ValueError):
            pass

        if str(security_data.get("hidden_owner")) == "1":
            risk += 20

        if str(security_data.get("can_take_back_ownership")) == "1":
            risk += 15

        if str(security_data.get("is_mintable")) == "1":
            risk += 15
    market_cap = pair.get("marketCap") or pair.get("fdv") or 0

    if liquidity < 10000:
        risk += 25
    elif liquidity < 25000:
        risk += 15

    if market_cap > 0 and liquidity / market_cap < 0.05:
        risk += 15

    if honeypot is None:
        risk += 20
    else:
        if not honeypot.get("verified"):
            risk += 30

        if honeypot.get("is_honeypot"):
            risk += 100

        sell_tax = honeypot.get("sell_tax")

        if sell_tax is not None:
            if sell_tax >= 20:
                risk += 40
            elif sell_tax >= 10:
                risk += 20

        risk_level = honeypot.get("risk_level")

        if risk_level is not None:
            risk += min(int(risk_level / 4), 25)

    top_holder_total = 0

    if security_data:
        holders = security_data.get("holders") or []

        for holder in holders[:10]:
            percent = holder.get("percent")

            try:
                top_holder_total += float(percent or 0) * 100
            except (TypeError, ValueError):
                pass

        if top_holder_total >= 70:
            risk += 40
        elif top_holder_total >= 50:
            risk += 25
        elif top_holder_total >= 30:
            risk += 10
    else:
        risk += 10
        # Holder concentration risk
    holders = security_data.get("holders") or []
    top_holder_percent = 0.0

    for holder in holders:
        try:
            percent = float(holder.get("percent", 0))

            # Convert whole-number percentages to decimals
            if percent > 1:
                percent = percent / 100

            top_holder_percent += percent

        except (TypeError, ValueError):
            pass

    if top_holder_percent > 0.60:
        risk += 30
    elif top_holder_percent > 0.40:
        risk += 20
    elif top_holder_percent > 0.20:
        risk += 10
    return min(risk, 100)
def score_pair(pair):
    score = 0

    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    volume_24h = (pair.get("volume") or {}).get("h24") or 0
    volume_1h = (pair.get("volume") or {}).get("h1") or 0
    volume_5m = (pair.get("volume") or {}).get("m5") or 0
    price_change_5m = (pair.get("priceChange") or {}).get("m5") or 0

    txns_5m = (pair.get("txns") or {}).get("m5") or {}
    buys_5m = txns_5m.get("buys") or 0
    sells_5m = txns_5m.get("sells") or 0

    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    pair_created_at = pair.get("pairCreatedAt") or 0

    age_hours = 0

    if pair_created_at:
        age_hours = (time.time() * 1000 - pair_created_at) / 3600000

    txns_1h = (pair.get("txns") or {}).get("h1") or {}
    buys_1h = txns_1h.get("buys") or 0
    sells_1h = txns_1h.get("sells") or 0

    if 50000 <= market_cap <= 5000000:
        score += 10

    if liquidity >= 50000:
        score += 20
    elif liquidity >= 25000:
        score += 15
    elif liquidity >= 10000:
        score += 8

    if volume_24h >= 250000:
        score += 15
    elif volume_24h >= 100000:
        score += 10
    elif volume_24h >= 25000:
        score += 5

    if volume_1h >= 20000:
        score += 15
    elif volume_1h >= 10000:
        score += 10
    elif volume_1h >= 3000:
        score += 5

    if volume_5m >= 5000:
        score += 5

    if buys_5m > sells_5m and buys_5m >= 5:
        score += 5

    if 1 <= price_change_5m <= 20:
        score += 5

    if buys_1h > sells_1h:
        score += 10

    if buys_1h >= sells_1h * 1.5 and buys_1h >= 10:
        score += 5

    if 1 <= age_hours <= 24:
        score += 5
    elif 24 < age_hours <= 72:
        score += 3

    return min(score, 100)
@tasks.loop(minutes=5)
async def scan_coins():
    print(f"Logged in as {client.user}")
    tokens = get_tokens()
    print(f"Found {len(tokens)} token profiles")
    ranked = []

    for token_info in tokens:
        chain_id = token_info.get("chainId")
        token_address = token_info.get("tokenAddress")

        if not chain_id or not token_address:
            continue

        pairs = get_token_pairs(chain_id, token_address)
        pair = get_best_pair(pairs)

        if pair:
            token_address = (pair.get("baseToken") or {}).get("address")
            chain_id = pair.get("chainId")

            honeypot = None
            cluster_data = None
            largest_cluster_pct = None
            cluster_risk = "⚪ Unavailable"

            supported_cluster_networks = {
                "sol", "ethereum", "eth", "base",
                "bsc", "monad", "xlayer", "abs"
            }

            cluster_network = chain_id

            if cluster_network == "ethereum":
                cluster_network = "eth"

            if cluster_network in supported_cluster_networks:
                cluster_data = get_bubble_clusters(
                    cluster_network,
                    token_address
                )

                if cluster_data:
                    clusters = cluster_data.get("clusters") or []

                    if clusters:
                        largest_cluster_pct = float(
                            clusters[0].get("pct", 0)
                        )

                        if largest_cluster_pct >= 60:
                            cluster_risk = "🔴 HIGH"
                        elif largest_cluster_pct >= 40:
                            cluster_risk = "🟠 CONCERNING"
                        elif largest_cluster_pct >= 20:
                            cluster_risk = "🟡 WATCH"
                        else:
                            cluster_risk = "🟢 LOW"
        honeypot = False

        if chain_id in ["ethereum", "bsc", "base"] and token_address:
            chain_map = {
                "ethereum": 1,
                "bsc": 56,
                "base": 8453
            }

            numeric_chain_id = chain_map.get(chain_id)

            if numeric_chain_id:
                honeypot = check_honeypot(token_address, numeric_chain_id)

        if honeypot:
            if not honeypot.get("verified"):
                continue

            if honeypot.get("is_honeypot"):
                continue

            if honeypot.get("risk_level", 100) >= 60:
                continue

            if honeypot.get("sell_tax") is not None:
                if honeypot["sell_tax"] >= 20:
                    continue
        security_chain_id = SECURITY_CHAIN_MAP.get(chain_id)
        security_data = None

        if security_chain_id and token_address:
            security_data = get_token_security(
                security_chain_id,
                token_address
            )
        if not security_data:
            print("BLOCKED: Security data unavailable")
            continue
        if security_data:
            if (
                str(security_data.get("is_honeypot", "0")) == "1"
                or str(security_data.get("cannot_buy", "0")) == "1"
                or str(security_data.get("is_blacklisted", "0")) == "1"
                or str(security_data.get("owner_change_balance", "0")) == "1"
                or str(security_data.get("is_open_source", "0")) != "1"
            ):
    print("BLOCKED: Security check failed")
    continue
                print("BLOCKED: Security check failed")
                continue
        if security_data:
            if str(security_data.get("is_honeypot")) == "1":
                continue

            sell_tax = security_data.get("sell_tax")
            transfer_tax = security_data.get("transfer_tax")

            # Reject if the API cannot provide a usable sellability result
            if sell_tax in (None, ""):
                continue

            if str(sell_tax) == "1":
                continue

            if str(transfer_tax) == "1":
                continue
        else:
            # No security data = cannot verify selling
            continue
        score = score_pair(pair)
        rug_risk = rug_risk_score(pair, honeypot, security_data)

        if honeypot is None:
            score = max(score - 15, 0)

        ranked.append((score, pair, honeypot, rug_risk, security_data))

    ranked.sort(key=lambda x: x[0], reverse=True)

    print(f"Scored {len(ranked)} coins")
    if not ranked:
        print("No coins passed security filters")
        return
    if ranked:
        top_score, top_pair, top_honeypot, top_rug_risk, top_security_data = ranked[0]

        print("TOP COIN:")
        print("Name:", (top_pair.get("baseToken") or {}).get("name"))
        print("Symbol:", (top_pair.get("baseToken") or {}).get("symbol"))
        print("CA:", (top_pair.get("baseToken") or {}).get("address"))
        print("Chain:", top_pair.get("chainId"))
        print("Price:", top_pair.get("priceUsd"))
        print("Liquidity:", (top_pair.get("liquidity") or {}).get("usd"))
        print("Market Cap:", top_pair.get("marketCap") or top_pair.get("fdv"))
        print("24H Volume:", (top_pair.get("volume") or {}).get("h24"))
        print("Score:", top_score)
        print("Rug Risk:", top_rug_risk)
        print("Security Data:", "AVAILABLE" if top_security_data else "UNAVAILABLE")
    if security_data:
        print("Honeypot:", security_data.get("is_honeypot"))
        print("Cannot Buy:", security_data.get("cannot_buy"))
        print("Blacklisted:", security_data.get("is_blacklisted"))
        print("Mintable:", security_data.get("is_mintable"))
        print("Hidden Owner:", security_data.get("hidden_owner"))
        print("Proxy:", security_data.get("is_proxy"))
        print("Owner %:", security_data.get("owner_percent"))
        print("Creator %:", security_data.get("creator_percent"))
        print("Buy Tax:", security_data.get("buy_tax"))
        print("Sell Tax:", security_data.get("sell_tax"))
    else:
        print("Security Data: UNAVAILABLE")

        if top_honeypot:
            print("Sell Check:", "PASSED" if top_honeypot.get("verified") else "FAILED")
        else:
            print("Sell Check: UNVERIFIED")

        contract = (top_pair.get("baseToken") or {}).get("address")
        base_token = top_pair.get("baseToken") or {}

        if top_score >= 75:

            channel = discord.utils.get(
                client.get_all_channels(),
                name="trade-signals"
            )

            if channel is None:
                print("ERROR: Could not find #trade-signals")

            if channel is not None and contract and contract not in alerted_contracts:
                base_token = top_pair.get("baseToken") or {}
                liquidity = (top_pair.get("liquidity") or {}).get("usd") or 0
                volume = (top_pair.get("volume") or {}).get("h24") or 0
                price = top_pair.get("priceUsd") or "N/A"
                market_cap = top_pair.get("marketCap") or top_pair.get("fdv") or 0

            txns = top_pair.get("txns") or {}
            h1_txns = txns.get("h1") or {}
            buys = h1_txns.get("buys", 0)
            sells = h1_txns.get("sells", 0)

            if buys > sells:
                trend = "🟢 Buying pressure"
            elif sells > buys:
                trend = "🔴 Selling pressure"
            else:
                trend = "🟡 Balanced"

            security = top_security_data or {}
            mintable = security.get("is_mintable")
            blacklisted = security.get("is_blacklisted")
            honeypot_status = security.get("is_honeypot")

            embed = discord.Embed(
                title=f"🪙 {base_token.get('name', 'Unknown')}",
                description=(
                    f"**{base_token.get('symbol', 'N/A')}**\n"
                    f"`{contract}`\n\n"
                    "⚠️ Automated market research only — not financial advice, "
                    "not a guaranteed buy/sell signal, and not a guarantee against a rug."
                ),
                color=0xF5B642
            )

            embed.add_field(name="💵 Price", value=f"${price}", inline=True)
            embed.add_field(
                name="💰 Market Cap",
                value=f"${market_cap:,.0f}" if isinstance(market_cap, (int, float)) else str(market_cap),
                inline=True
            )
            embed.add_field(
                name="💧 Liquidity",
                value=f"${liquidity:,.2f}" if isinstance(liquidity, (int, float)) else str(liquidity),
                inline=True
            )
            embed.add_field(
                name="📊 24H Volume",
                value=f"${volume:,.2f}" if isinstance(volume, (int, float)) else str(volume),
                inline=True
            )
            embed.add_field(name="🟢 1H Buys", value=str(buys), inline=True)
            embed.add_field(name="🔴 1H Sells", value=str(sells), inline=True)
            embed.add_field(name="📈 Trend", value=trend, inline=True)
            embed.add_field(name="🎯 Scanner Score", value=f"**{top_score}/100**", inline=True)
            embed.add_field(name="🛡️ Rug Risk", value=f"**{top_rug_risk}/100**", inline=True)
            if largest_cluster_pct is not None:
                cluster_text = f"{largest_cluster_pct:.1f}%"
            else:
                cluster_text = "Unavailable"

            embed.add_field(
                name="🫧 Bubble Map Cluster",
                value=f"{cluster_risk}\nLargest Cluster: **{cluster_text}**",
                inline=True
            )

            embed.add_field(
                name="🔐 Security",
                value=(
                    f"Honeypot: {'⚠️ Yes' if str(honeypot_status) == '1' else '✅ No'}\n"
                    f"Mintable: {'⚠️ Yes' if str(mintable) == '1' else '✅ No'}\n"
                    f"Blacklisted: {'⚠️ Yes' if str(blacklisted) == '1' else '✅ No'}"
                ),
                inline=False
            )

            embed.add_field(
                name="📋 Contract Address",
                value=f"`{contract}`",
                inline=False
            )

            embed.set_footer(text="Automated scanner • Research only")
            print("ABOUT TO SEND DISCORD ALERT")
            await channel.send(embed=embed)
            alerted_contracts.add(contract)
            print("Discord alert sent to #trade-signals")

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    if not scan_coins.is_running():
        scan_coins.start()


client.run(token)

client.run(token)
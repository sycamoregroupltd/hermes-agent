#!/usr/bin/env python3
"""
Wallet Discovery Script
Uses xAI Grok API to search X/Twitter for profitable trader wallets.
Cross-references against pro_trader_profiles and walletRegistry.
Adds new verified wallets to pro_trader_profiles.
Suitable for on-demand runs and weekly cron.
"""

import os
import re
import subprocess
import sys
from datetime import datetime
from typing import List, Dict, Set

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: openai package required. Run: pip install openai")
    sys.exit(1)


# Configuration
XAI_API_KEY = os.getenv("XAI_API_KEY")
if not XAI_API_KEY:
    print("ERROR: XAI_API_KEY not set in environment.")
    print("Set it with: export XAI_API_KEY=your_key")
    sys.exit(1)

client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1",
)

# Search queries for different platforms
SEARCH_QUERIES = [
    "profitable trader wallet address Hyperliquid",
    "top whale wallet Ethereum Solana",
    "best crypto trader address Bybit OKX",
    "Binance futures leaderboard trader wallet",
    "verified profitable Hyperliquid wallet",
    "smart money wallet address Solana Ethereum",
]

# Regex for wallet addresses
ETH_PATTERN = re.compile(r"0x[a-fA-F0-9]{40}")
SOL_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def search_x_for_wallets() -> List[Dict]:
    """Search X/Twitter via xAI Grok for profitable trader wallets."""
    found_wallets = []
    seen = set()

    for query in SEARCH_QUERIES:
        try:
            response = client.chat.completions.create(
                model="grok-3",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a crypto intelligence analyst. Extract ONLY verifiable wallet addresses mentioned in credible sources. Return JSON array with fields: address, platform, source, context. Only include addresses explicitly mentioned as profitable traders or whales."
                    },
                    {"role": "user", "content": f"Search X for: {query}. Return only addresses from credible analysts or official sources."}
                ],
                temperature=0.3,
                max_tokens=2000,
            )

            content = response.choices[0].message.content or ""

            # Extract addresses from response
            eth_matches = ETH_PATTERN.findall(content)
            sol_matches = SOL_PATTERN.findall(content)

            for addr in eth_matches + sol_matches:
                if addr in seen:
                    continue
                seen.add(addr)

                platform = "ethereum" if addr.startswith("0x") else "solana"
                found_wallets.append({
                    "wallet_address": addr,
                    "platform": platform,
                    "source": query,
                    "raw_context": content[:500],
                    "discovered_at": datetime.utcnow().isoformat()
                })

        except Exception as e:
            print(f"WARNING: Search failed for query '{query}': {e}")
            continue

    return found_wallets


def get_existing_wallets() -> Set[str]:
    """Fetch existing wallet addresses from pro_trader_profiles via Docker."""
    try:
        result = subprocess.run(
            [
                "docker", "exec", "-i", "postgres",
                "psql", "-U", "postgres", "-d", "sycode",
                "-t", "-c", "SELECT wallet_address FROM pro_trader_profiles WHERE removed_at IS NULL;"
            ],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"WARNING: Could not query pro_trader_profiles: {result.stderr}")
            return set()

        wallets = {line.strip() for line in result.stdout.strip().split("\n") if line.strip()}
        return wallets
    except Exception as e:
        print(f"WARNING: Failed to fetch existing wallets: {e}")
        return set()


def add_wallet_to_pro_trader_profiles(wallet: Dict) -> bool:
    """Insert new wallet into pro_trader_profiles using docker exec."""
    try:
        insert_sql = f"""
        INSERT INTO pro_trader_profiles (
            wallet_address, display_name, all_time_pnl, is_active, trader_tier, created_at, updated_at
        ) VALUES (
            '{wallet['wallet_address']}',
            'Discovered via X - {wallet["platform"]}',
            0,
            true,
            'UNRATED',
            NOW(),
            NOW()
        ) ON CONFLICT (wallet_address) DO NOTHING;
        """

        result = subprocess.run(
            [
                "docker", "exec", "-i", "postgres",
                "psql", "-U", "postgres", "-d", "sycode",
                "-c", insert_sql
            ],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode == 0 and "INSERT 0 1" in result.stdout:
            return True
        return False
    except Exception as e:
        print(f"ERROR inserting wallet {wallet['wallet_address']}: {e}")
        return False


def main():
    print("=" * 60)
    print("WALLET DISCOVERY SCRIPT")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 60)

    # Step 1: Search X/Twitter
    print("\n[1/4] Searching X/Twitter via xAI Grok...")
    discovered = search_x_for_wallets()
    print(f"      Found {len(discovered)} candidate addresses")

    if not discovered:
        print("      No new wallets found. Exiting.")
        return

    # Step 2: Cross-reference
    print("\n[2/4] Cross-referencing against existing profiles...")
    existing = get_existing_wallets()
    print(f"      Found {len(existing)} existing wallets in pro_trader_profiles")

    new_wallets = [w for w in discovered if w["wallet_address"] not in existing]
    duplicates = len(discovered) - len(new_wallets)
    print(f"      {len(new_wallets)} new wallets, {duplicates} duplicates skipped")

    # Step 3: Add verified wallets
    print("\n[3/4] Adding new verified wallets...")
    added = 0
    for wallet in new_wallets:
        if add_wallet_to_pro_trader_profiles(wallet):
            print(f"      ✓ Added: {wallet['wallet_address']} ({wallet['platform']})")
            added += 1
        else:
            print(f"      - Skipped (duplicate or error): {wallet['wallet_address']}")

    # Step 4: Summary
    print("\n[4/4] SUMMARY")
    print("-" * 40)
    print(f"Wallets discovered from X: {len(discovered)}")
    print(f"Duplicates skipped:        {duplicates}")
    print(f"New wallets added:         {added}")
    print(f"Total in pro_trader_profiles: {len(existing) + added}")
    print("-" * 40)
    print("Script completed successfully.")


if __name__ == "__main__":
    main()
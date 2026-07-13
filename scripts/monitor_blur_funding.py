
import ccxt
import os
import json
from datetime import datetime

def get_hyperliquid_funding_rate(symbol='BLUR/USDC:USDC'):
    try:
        exchange = ccxt.hyperliquid({
            'enableRateLimit': True,
        })
        
        # Hyperliquid often requires a specific market ID or symbol format
        # Let's try fetching markets first to get the correct symbol if needed
        markets = exchange.load_markets()
        
        # Find the correct market ID for BLUR/USDC:USDC
        market_id = None
        for k, v in markets.items():
            if v['symbol'] == symbol and v['settleId'] == 'USDC': # Assuming settleId is for USDC
                market_id = k
                break

        if not market_id:
            print(f"Error: Market for {symbol} not found on Hyperliquid.")
            return None

        # Fetch funding rate history - Hyperliquid might have a specific endpoint
        # ccxt's fetchFundingRate and fetchFundingRates might work
        
        # A direct approach for Hyperliquid using public API to get funding info
        # This might require some specific Hyperliquid API calls if ccxt's generic doesn't work well
        
        # For a quick check, let's try fetchFundingRate
        funding_rate_data = exchange.fetch_funding_rate(symbol)
        
        if funding_rate_data:
            # The 'info' field usually contains raw exchange response
            # Hyperliquid's API often provides 'fundingRate' directly in the market data or a specific endpoint
            
            # Assuming the funding_rate_data contains 'fundingRate' and 'symbol'
            hourly_funding_rate = funding_rate_data.get('fundingRate')
            
            print(f"[{datetime.utcnow().isoformat()}] Hyperliquid {symbol} Hourly Funding Rate: {hourly_funding_rate}")
            return hourly_funding_rate
        else:
            print(f"[{datetime.utcnow().isoformat()}] Could not fetch funding rate for {symbol} on Hyperliquid.")
            return None

    except ccxt.NetworkError as e:
        print(f"[{datetime.utcnow().isoformat()}] Network error: {e}")
    except ccxt.ExchangeError as e:
        print(f"[{datetime.utcnow().isoformat()}] Exchange error: {e}")
    except Exception as e:
        print(f"[{datetime.utcnow().isoformat()}] An unexpected error occurred: {e}")
    return None

if __name__ == '__main__':
    get_hyperliquid_funding_rate()

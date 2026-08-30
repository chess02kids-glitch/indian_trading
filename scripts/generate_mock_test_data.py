import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def create_dirs(file_path):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

def generate_eod_data(symbols, start_date, end_date):
    dates = pd.date_range(start_date, end_date, freq='B')
    for symbol in symbols:
        df = pd.DataFrame({'date': dates})
        df['symbol'] = symbol
        df['open'] = np.random.lognormal(mean=0, sigma=0.01, size=len(dates)).cumprod() * 100
        df['high'] = df['open'] * (1 + np.random.uniform(0, 0.02, size=len(dates)))
        df['low'] = df['open'] * (1 - np.random.uniform(0, 0.02, size=len(dates)))
        df['close'] = (df['high'] + df['low']) / 2 + np.random.normal(0, 1, size=len(dates))
        df['volume'] = np.random.randint(1000, 1000000, size=len(dates))
        df['series'] = 'EQ'
        df['source'] = 'mock'
        df['exchange'] = 'NSE'
        df['ingested_at'] = datetime.now()
        df['source_ts'] = datetime.now()
        df['adjustment_state'] = 'adjusted'
        
        # Save by year and month as per path
        df['year'] = df['date'].dt.year
        df['month'] = df['date'].dt.month
        
        for (year, month), group in df.groupby(['year', 'month']):
            path = f'data/raw/eod2_data/NSE/{symbol}/{year}/{month:02d}.parquet'
            create_dirs(path)
            group.drop(columns=['year', 'month']).to_parquet(path, index=False)

def generate_fundamentals(symbols, start_date, end_date):
    quarters = pd.date_range(start_date, end_date, freq='Q')
    records = []
    for symbol in symbols:
        for q in quarters:
            records.append({
                'date': q,
                'symbol': symbol,
                'roe': np.random.uniform(0.05, 0.25),
                'debt_to_equity': np.random.uniform(0, 2),
                'pe': np.random.uniform(10, 50),
                'pb': np.random.uniform(1, 10),
                'market_cap': np.random.uniform(1e9, 1e11),
                'sales': np.random.uniform(1e8, 1e10),
                'net_income': np.random.uniform(1e7, 1e9),
                'revenue_growth_yoy': np.random.uniform(-0.1, 0.3),
                'earnings_yoy': np.random.uniform(-0.2, 0.5),
                'roa': np.random.uniform(0.02, 0.15),
                'gross_margin': np.random.uniform(0.1, 0.6),
                'operating_margin': np.random.uniform(0.05, 0.4),
                'dividend_yield': np.random.uniform(0, 0.05),
                'sector': 'Technology',
                'industry': 'Software',
                'source': 'mock',
                'fetched_at': datetime.now()
            })
    df = pd.DataFrame(records)
    path = 'data/bundle/fundamentals_quarterly.parquet'
    create_dirs(path)
    df.to_parquet(path, index=False)
    
    # PIT
    pit_path = 'data/bundle/fundamentals_pit.csv'
    create_dirs(pit_path)
    # Simple mock PIT
    pit_records = [{'as_of_date': q, 'symbol': s, 'field': 'pe', 'value': np.random.uniform(10, 50)} 
                   for q in quarters for s in symbols]
    pd.DataFrame(pit_records).to_csv(pit_path, index=False)


def generate_pit_universe(symbols, start_date, end_date):
    for index_name, file_name in [('Nifty 50', 'nifty50-pit/nifty50.csv'), 
                                  ('Nifty 500', 'nifty500-pit/nifty500.csv'),
                                  ('Nifty 100', 'nifty100-pit/nifty100.csv')]:
        records = []
        for symbol in symbols:
            records.append({
                'symbol': symbol,
                'index_name': index_name,
                'valid_from': start_date.strftime('%Y-%m-%d'),
                'valid_to': '2099-12-31',
                'isin': f'INE{np.random.randint(100000000, 999999999)}',
                'sector': 'Technology',
                'exchange': 'NSE',
                'delisted': False
            })
        df = pd.DataFrame(records)
        path = f'data/universe/{file_name}'
        create_dirs(path)
        df.to_csv(path, index=False)

    # Reconstitution events
    recon_path = 'data/universe/reconstitution_events.csv'
    create_dirs(recon_path)
    pd.DataFrame([{
        'index_name': 'Nifty 50',
        'effective_date': '2023-03-31',
        'added': '["MOCK1"]',
        'removed': '["MOCK2"]',
        'reason': 'Regular reconstitution'
    }]).to_csv(recon_path, index=False)

def generate_indices_vix(start_date, end_date):
    dates = pd.date_range(start_date, end_date, freq='B')
    # NIFTY 50
    nifty = pd.DataFrame({'date': dates})
    nifty['index_name'] = 'NIFTY 50'
    nifty['open'] = np.random.lognormal(mean=0, sigma=0.01, size=len(dates)).cumprod() * 10000
    nifty['high'] = nifty['open'] * 1.01
    nifty['low'] = nifty['open'] * 0.99
    nifty['close'] = nifty['open'] * 1.005
    nifty['returns'] = nifty['close'].pct_change().fillna(0)
    
    path = 'data/market/indices/nifty50.parquet'
    create_dirs(path)
    nifty.to_parquet(path, index=False)
    
    # VIX
    vix = pd.DataFrame({'date': dates})
    vix['close'] = np.random.uniform(10, 30, size=len(dates))
    vix['high'] = vix['close'] * 1.1
    vix['low'] = vix['close'] * 0.9
    vix_path = 'data/market/india_vix.csv'
    create_dirs(vix_path)
    vix.to_csv(vix_path, index=False)
    
    # Breadth
    breadth = pd.DataFrame({'date': dates})
    breadth['advances'] = np.random.randint(100, 400, size=len(dates))
    breadth['declines'] = np.random.randint(100, 400, size=len(dates))
    breadth['unchanged'] = 500 - breadth['advances'] - breadth['declines']
    breadth['adv_decl_ratio'] = breadth['advances'] / breadth['declines']
    breadth['breadth_pct'] = breadth['advances'] / 500
    breadth_path = 'data/market/breadth.csv'
    create_dirs(breadth_path)
    breadth.to_csv(breadth_path, index=False)

def generate_intraday(symbols, date):
    # Only one day for intraday to save space
    times = pd.date_range(f"{date.strftime('%Y-%m-%d')} 09:15", f"{date.strftime('%Y-%m-%d')} 15:30", freq='5min')
    for symbol in symbols:
        df = pd.DataFrame({'date': times})
        df['symbol'] = symbol
        df['open'] = np.random.lognormal(mean=0, sigma=0.001, size=len(times)).cumprod() * 100
        df['high'] = df['open'] * 1.002
        df['low'] = df['open'] * 0.998
        df['close'] = df['open'] * 1.001
        df['volume'] = np.random.randint(100, 10000, size=len(times))
        
        path = f"data/intraday/{symbol}/{date.strftime('%Y-%m-%d')}.parquet"
        create_dirs(path)
        df.to_parquet(path, index=False)

def generate_borrow_data(symbols, start_date, end_date):
    dates = pd.date_range(start_date, end_date, freq='B')
    records = []
    for symbol in symbols:
        df = pd.DataFrame({'date': dates})
        df['symbol'] = symbol
        df['borrow_available'] = np.random.randint(0, 1000000, size=len(dates))
        df['borrow_fee_pct'] = np.random.uniform(0.01, 0.15, size=len(dates))
        df['shortable'] = True
        df['locates_available'] = True
        df['settle_date'] = dates + pd.Timedelta(days=1)
        records.append(df)
        
    df_all = pd.concat(records)
    path = 'data/market/borrow.csv'
    create_dirs(path)
    df_all.to_csv(path, index=False)

def generate_options(symbols, date):
    records = []
    for symbol in symbols:
        for strike in [90, 100, 110]:
            for typ in ['C', 'P']:
                records.append({
                    'symbol': symbol,
                    'expiry': date + timedelta(days=30),
                    'strike': strike,
                    'type': typ,
                    'open_interest': np.random.randint(100, 10000),
                    'volume': np.random.randint(10, 1000),
                    'bid': np.random.uniform(1, 5),
                    'ask': np.random.uniform(1.1, 5.5),
                    'last': np.random.uniform(1.05, 5.25),
                    'implied_vol': np.random.uniform(0.1, 0.5),
                    'delta': np.random.uniform(-1, 1),
                    'gamma': np.random.uniform(0, 0.1),
                    'vega': np.random.uniform(0, 1),
                    'theta': np.random.uniform(-1, 0),
                    'underlying_price': 100
                })
    df = pd.DataFrame(records)
    path = f"data/options/chains/{date.strftime('%Y-%m-%d')}/mock_chain.parquet"
    create_dirs(path)
    df.to_parquet(path, index=False)

def generate_sector_macro(symbols, start_date, end_date):
    # Sector
    sector = pd.DataFrame({
        'symbol': symbols,
        'sector': 'Technology',
        'industry': 'Software',
        'index_membership': 'NIFTY 50',
        'bse_isin': [f'INE{np.random.randint(1000,9999)}' for _ in symbols],
        'nse_ticker': symbols
    })
    path_sec = 'data/universe/sector_map.csv'
    create_dirs(path_sec)
    sector.to_csv(path_sec, index=False)
    
    # Macro
    dates = pd.date_range(start_date, end_date, freq='M')
    macro = pd.DataFrame({'date': dates})
    macro['repo_rate'] = 6.5
    macro['10y_gilt_yield'] = 7.1
    macro['inr_usd'] = 83.0
    macro['crude_price'] = 80.0
    macro['fii_dii_flow'] = np.random.uniform(-1000, 1000, size=len(dates))
    macro['cpi'] = 5.0
    macro['gdp_growth'] = 6.5
    path_mac = 'data/market/macro.csv'
    create_dirs(path_mac)
    macro.to_csv(path_mac, index=False)

def generate_corporate_actions(symbols):
    actions = pd.DataFrame({
        'symbol': symbols,
        'ex_date': [datetime(2023, 1, 1)] * len(symbols),
        'type': 'split',
        'ratio': '2:1',
        'description': 'Mock Split',
        'adjust_date': [datetime(2023, 1, 1)] * len(symbols)
    })
    path = 'data/corporate_actions.csv'
    create_dirs(path)
    actions.to_csv(path, index=False)

if __name__ == '__main__':
    symbols = ['MOCK1', 'MOCK2', 'MOCK3']
    start = datetime(2015, 1, 1)
    end = datetime(2026, 8, 30)
    
    print("Generating EOD Data...")
    generate_eod_data(symbols, start, end)
    
    print("Generating Fundamentals...")
    generate_fundamentals(symbols, start, end)
    
    print("Generating Universe/PIT...")
    generate_pit_universe(symbols, start, end)
    
    print("Generating Indices/VIX/Breadth...")
    generate_indices_vix(start, end)
    
    print("Generating Intraday...")
    generate_intraday(symbols, datetime(2026, 8, 29))
    
    print("Generating Borrow...")
    generate_borrow_data(symbols, start, end)
    
    print("Generating Options...")
    generate_options(symbols, datetime(2026, 8, 29))
    
    print("Generating Sector & Macro...")
    generate_sector_macro(symbols, start, end)
    
    print("Generating Corporate Actions...")
    generate_corporate_actions(symbols)
    
    print("Done!")

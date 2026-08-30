import os
import json
import glob
import pandas as pd
from collections import defaultdict

def build_pit():
    parsed_dir = 'data/membership/index_history/data/parsed'
    json_files = glob.glob(os.path.join(parsed_dir, '*.json'))
    
    # Store events: index_name -> date -> {'included': set(), 'excluded': set()}
    events_by_index = defaultdict(lambda: defaultdict(lambda: {'included': set(), 'excluded': set()}))
    
    # Read all events
    for file in json_files:
        with open(file, 'r') as f:
            data = json.load(f)
            
        date = data['effective_date']
        for event in data.get('events', []):
            idx_name = event['index_name']
            events_by_index[idx_name][date]['included'].update(event.get('included', []))
            events_by_index[idx_name][date]['excluded'].update(event.get('excluded', []))

    # We need to build a PIT table for Nifty 50, Nifty 100, Nifty 500
    target_indices = {
        'Nifty 50': 'nifty50',
        'Nifty 100': 'nifty100',
        'Nifty 500': 'nifty500'
    }
    
    # Write reconstitution events
    recon_records = []
    for idx_name in target_indices.keys():
        dates = sorted(events_by_index[idx_name].keys())
        for d in dates:
            inc = list(events_by_index[idx_name][d]['included'])
            exc = list(events_by_index[idx_name][d]['excluded'])
            if inc or exc:
                recon_records.append({
                    'index_name': idx_name,
                    'effective_date': d,
                    'added': json.dumps(inc),
                    'removed': json.dumps(exc),
                    'reason': 'Reconstitution'
                })
    
    os.makedirs('data/universe', exist_ok=True)
    pd.DataFrame(recon_records).to_csv('data/universe/reconstitution_events.csv', index=False)
    
    # We don't have the base composition from 2007, so we'll just reconstruct what we can 
    # based on the assumption that a stock is in the index from its first 'included' event 
    # until its next 'excluded' event. 
    # Note: To build a perfect PIT, we would need a base snapshot. 
    # But this is sufficient to create real rows.
    
    for idx_name, short_name in target_indices.items():
        # symbol -> list of (valid_from, valid_to)
        membership = defaultdict(list)
        current_members = {} # symbol -> start_date
        
        dates = sorted(events_by_index[idx_name].keys())
        for d in dates:
            ev = events_by_index[idx_name][d]
            
            for sym in ev['included']:
                if sym not in current_members:
                    current_members[sym] = d
                    
            for sym in ev['excluded']:
                if sym in current_members:
                    start_date = current_members.pop(sym)
                    membership[sym].append((start_date, d))
                else:
                    # Excluded but we never saw it included. We assume it was in since beginning of records
                    membership[sym].append(('2000-01-01', d))
                    
        # For remaining current members, they are valid until 2099
        for sym, start_date in current_members.items():
            membership[sym].append((start_date, '2099-12-31'))
            
        records = []
        for sym, spans in membership.items():
            for start, end in spans:
                records.append({
                    'symbol': sym,
                    'index_name': idx_name,
                    'valid_from': start,
                    'valid_to': end,
                    'isin': '',
                    'sector': '',
                    'exchange': 'NSE',
                    'delisted': False
                })
                
        df = pd.DataFrame(records)
        out_dir = f'data/universe/{short_name}-pit'
        os.makedirs(out_dir, exist_ok=True)
        df.to_csv(f'{out_dir}/{short_name}.csv', index=False)

if __name__ == "__main__":
    build_pit()
    print("PIT generation complete.")

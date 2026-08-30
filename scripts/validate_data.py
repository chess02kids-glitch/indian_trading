import json
import glob
import pandas as pd

def validate():
    with open('data/requirements.json', 'r') as f:
        reqs = json.load(f)
        
    all_passed = True
    
    for req in reqs['datasets']:
        print(f"Validating {req['name']}...")
        files = glob.glob(req['path_glob'])
        if not files:
            print(f"  [FAIL] No files found for {req['path_glob']}")
            all_passed = False
            continue
            
        total_rows = 0
        min_date_found = None
        
        for file in files:
            if file.endswith('.parquet'):
                df = pd.read_parquet(file)
            else:
                df = pd.read_csv(file)
                
            total_rows += len(df)
            
            # Check forbidden columns
            for col, forbidden_val in req.get('must_not_contain', {}).items():
                if col in df.columns:
                    if (df[col] == forbidden_val).any():
                        print(f"  [FAIL] File {file} contains forbidden value '{forbidden_val}' in column '{col}'")
                        all_passed = False
            
            # Check date range
            if 'min_date' in req and 'date' in df.columns:
                file_min_date = df['date'].min()
                if min_date_found is None or file_min_date < min_date_found:
                    min_date_found = file_min_date
                    
        # Verify min rows
        if 'min_rows' in req and total_rows < req['min_rows']:
            print(f"  [FAIL] Total rows {total_rows} is less than required {req['min_rows']}")
            all_passed = False
            
        # Verify min date
        if 'min_date' in req:
            min_req = pd.to_datetime(req['min_date'])
            if pd.api.types.is_datetime64_any_dtype(min_date_found):
                actual_min = min_date_found
            else:
                actual_min = pd.to_datetime(min_date_found)
                
            if actual_min > min_req:
                print(f"  [FAIL] Minimum date found is {actual_min}, required at least {min_req}")
                all_passed = False
                
        if all_passed:
            print(f"  [PASS] {req['name']} validated.")
            
    if not all_passed:
        print("Data validation failed. Mocks or incomplete data detected.")
        exit(1)
    else:
        print("All datasets passed provenance checks.")

if __name__ == "__main__":
    validate()

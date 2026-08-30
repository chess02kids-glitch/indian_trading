$ErrorActionPreference = "Continue"

Write-Host "--- Running test_real_data_pipeline.py ---"
python -m pytest tests/test_real_data_pipeline.py -v --tb=short

Write-Host "--- Running strategy and backtest tests ---"
python -m pytest tests/test_portfolio_backtest.py tests/test_backtest_determinism.py tests/test_research_engine.py tests/test_research_factors.py tests/test_research_workflow.py -v --tb=short

Write-Host "--- Running test_realtime.py ---"
python -m pytest tests/test_realtime.py -v --tb=short

Write-Host "--- Running full suite (minus RLS) ---"
python -m pytest tests/ --ignore=tests/test_rls_integration.py -q --tb=short

Write-Host "--- Running test_rls_integration.py ---"
$env:SUPABASE_URL="https://tpwuvqyposshgbaxirkx.supabase.co"
$env:SUPABASE_KEY="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRwd3V2cXlwb3NzaGdiYXhpcmt4Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4NzU3NjQ1NCwiZXhwIjoyMTAzMTUyNDU0fQ.DxaCqEmiz71f0Eltlt0y3dSdOi2tSa2iHNn61SGXPpE"
$env:DATABASE_URL="postgresql://postgres:N6bcqznuVxlJpZrb@db.tpwuvqyposshgbaxirkx.supabase.co:5432/postgres"
python -m pytest tests/test_rls_integration.py -v --tb=short

# Public API Catalog

## Data & Storage
- `DuckDBManager.execute(query)`: Run analytical queries.
- `StorageManager.save_historical_data(...)`: Save immutable Parquet partitions.

## Repositories (Supabase)
- `OrdersRepository.create_order(user_id, symbol, side, qty, price)`
- `OrdersRepository.update_status(order_id, status)`
- `ExecutionsRepository.save_execution(order_id, qty, price, exec_time, broker_id)`
- `APISessionsRepository.save_session(user_id, broker, access, refresh, expires)`

## Research & Portfolio
- `FactorStrategy.generate_signals(data) -> Signal`
- `ValidationEngine.run(prices, weights) -> pd.DataFrame`
- `combinatorial_purged_cv(index, n_groups, n_test_groups, embargo) -> List[CPCVWindow]`
- `EqualWeightConstructor.construct(signals) -> pd.DataFrame`

## Risk & Execution
- `KillSwitch.activate(reason)`
- `RiskEngine.evaluate(order_intent) -> bool`
- `BrokerAdapter.place_limit_order(symbol, side, qty, price)`
- `SessionManager.get_valid_session(broker) -> dict`

## Observability
- `AuthHealthMonitor.run_full_diagnostics() -> dict`
- `ReconciliationEngine.run_eod_reconciliation() -> bool`

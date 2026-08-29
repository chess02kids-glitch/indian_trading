"""Independent strategy-discovery research subsystem.

This package is deliberately separate from the production ``backtest`` and
``research`` packages. Its purpose is **broad discovery**: load local market
data, generate many hypothesis-driven signals, simulate them with a simple,
transparent target-weight engine, and produce an evidence file. Anything that
survives discovery is then re-tested with the stricter, more realistic
infrastructure in ``backtest`` / ``research``.

Nothing in here touches broker, execution, auth, or risk-kill code.
"""

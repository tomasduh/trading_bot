# Investigación de estrategias (2026-11-13)

Backtests en velas diarias que respaldan `STRATEGY_MODE=daily`.

    python scripts/research/fetch_daily.py   # baja historia diaria (Binance público + Alpaca)
    python scripts/research/explore.py       # 6 familias: parámetros elegidos hasta 2023, verificados 2024→
    python scripts/research/robust.py        # costos x2/x4, 1 día de retraso, desglose por símbolo
    python scripts/research/stops.py         # efecto de un stop de emergencia

Supuestos: señal al cierre del día, ejecución al open siguiente, solo largos,
costos 0.15%/lado en crypto y 0.05%/lado en stocks, efectivo al 0%.

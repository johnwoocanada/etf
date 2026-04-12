This is an python based UI app, showing real time gold ETF stock price: 
10 year yield
NUGT
JDST

live app deployed/hosted in https://huggingface.co/spaces/johnwoocloud/etf


Sample data
all_history = {
    "NUGT": {
        "records": [
            {"price": "199.25 - 199.84", "time": "15:40:23", "net_volume": 152, "olb": -0.42},
            {"price": "199.31 - 199.90", "time": "15:40:22", "net_volume": 151, "olb": -0.38},
            {"price": "199.40 - 199.95", "time": "15:40:21", "net_volume": 150, "olb": -0.35},
            {"price": "199.55 - 200.01", "time": "15:40:20", "net_volume": 149, "olb": -0.33},
        ],
        "trade_price": 199.55,
        "buy": 180,
        "sell": 28,
        "obi": -0.42
    },

    "JDST": {
        "records": [
            {"price": "9.12 - 9.15", "time": "15:40:23", "net_volume": -88, "olb": 0.12},
            {"price": "9.11 - 9.14", "time": "15:40:22", "net_volume": -85, "olb": 0.10},
            {"price": "9.10 - 9.13", "time": "15:40:21", "net_volume": -82, "olb": 0.08},
            {"price": "9.09 - 9.12", "time": "15:40:20", "net_volume": -80, "olb": 0.06},
        ],
        "trade_price": 9.12,
        "buy": 40,
        "sell": 128,
        "obi": 0.12
    }
}

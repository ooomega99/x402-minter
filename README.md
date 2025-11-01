# 🔥 X402 Parallel Minter

A Python-based **parallel X402 minter** built on top of the [X402 payment protocol](https://www.x402.org/).  
It supports concurrent minting across multiple accounts with automatic retries, logging, and structured result output.

---

## 🚀 Features

- ✅ **Concurrent minting** with `ThreadPoolExecutor`
- 🔁 **Automatic retry** with exponential backoff and jitter
- ⚙️ **Configurable network**, scheme, and version (e.g., Base)
- 🧩 **Structured logging** (per account suffix)
- 💾 **JSON result output** with timestamped file
- 🧱 **Graceful error handling** for each account

---

## 🧰 Requirements

Python **3.9+** recommended.

### Install dependencies:

```bash
pip install -r requirements.txt
```

## ⚙️ Configuration

Edit your private keys and mint URL in `main.py` or load from environment variables.

```python
PRIVATE_KEYS: list[str] = [
    # "0xabc...",
    # "0xdef...",
]

URL_MINT: str = "https://api.ping.observer/mint" # Example mint URL
```

## ▶️ Usage

```bash
python main.py
```

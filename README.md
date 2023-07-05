# ChatNut API
Simple API script to allow paid calls with `X-Cashu` header

X-Cashu – HTTP 402: Payment Required

# Install
- Install Python and Poetry environments
- Deploy a Cashu Mint, see examples [here](https://github.com/cashubtc/cashu) or run from [LNbits](https://github.com/lnbits/cashu)
- Edit `.env` file as needed

```sh
MINT_URL="https://<your cashu mint url>"
MODEL_URL="https://<your model API URL (local, runpod, etc...)>"
```

Install all dependencies

```
git clone <this_repo>
poetry install
```

# Server
```sh
poetry run uvicorn main:app
```
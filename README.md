
# pwthor API Proxy

FastAPI server deployable on Koyeb free tier.

## Flow

```
Startup  ->  direct fetch + proxy race in parallel
             first to return auth_token cookie wins
             cookie + working proxy cached 55 min

Request  ->  GET /<route>?<params>
             uses cached cookie + proxy
             calls pwthor.live/api/<route>?<params>
             returns raw JSON
```

## Deploy on Koyeb (no CLI needed)

1. Push this folder to a GitHub repo
2. Go to app.koyeb.com -> Create Service -> GitHub
3. Select repo -> Build type: **Dockerfile**
4. Port: **7860**
5. Deploy

Done. Your URL will be:
  https://<your-app>.koyeb.app/gettestjson?batchid=...

## Routes

| URL | Proxies to |
|-----|-----------|
| GET / | Health check |
| GET /gettestjson?... | pwthor.live/api/gettestjson?... |
| GET /anything?params | pwthor.live/api/anything?params |

## Local test

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# open http://localhost:8000
```

import asyncio
import logging
import httpx
from cashu.core.migrations import migrate_databases
from cashu.wallet import migrations
from cashu.wallet.helpers import deserialize_token_from_string
from cashu.wallet.wallet import Wallet
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BaseSettings
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware


class Settings(BaseSettings):
    MINT_URL: str
    MODEL_URL: str
    RUNPOD_API_KEY: str
    COST_PER_CALL: int = 1

    class Config:
        env_file = ".env"


settings = Settings()
logging.basicConfig(encoding="utf-8", level=logging.INFO)


# Create a request queue
request_queue = asyncio.Queue()

# Use a dictionary to store response items with request_id as the key
response_items = {}

# Create an asyncio.Event to signal the availability of response items
response_available = asyncio.Event()

wallet = None
mint_url = settings.MINT_URL
model_url = settings.MODEL_URL
api_key = settings.RUNPOD_API_KEY
cost = settings.COST_PER_CALL

headers = {
    "Authorization": f"{api_key}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}
client = httpx.AsyncClient(base_url=model_url, headers=headers, timeout=60)


class EcashHeaderMiddleware(BaseHTTPMiddleware):
    """
    Middleware that checks the HTTP headers for ecash
    """

    async def dispatch(self, request, call_next):
        # all requests to /cashu/* are not checked for ecash
        if request.url.path.startswith("/paid/"):
            # check whether valid ecash was provided in header
            token = request.headers.get("X-Cashu")
            logging.info(f"token: {token}")
            if not token:
                return JSONResponse(
                    {
                        "detail": "This endpoint requires a X-Cashu ecash header",
                    },
                    status_code=402,
                )
            tokenObj = await deserialize_token_from_string(token + "=")
            amount = tokenObj.get_amount()
            if amount < cost:
                return JSONResponse({"detail": "Invalid amount"}, status_code=402)
            proofs = tokenObj.get_proofs()

            try:
                await wallet.redeem(proofs)
            except Exception as e:
                return JSONResponse({"detail": str(e)}, status_code=402)

        response = await call_next(request)
        return response


# Create the FastAPI app
def create_app() -> FastAPI:
    middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        ),
        Middleware(EcashHeaderMiddleware),
    ]

    app = FastAPI(
        title="ChatNut API",
        description="API access with Cashu",
        license_info={
            "name": "MIT License",
            "url": "https://github.com/talvasconcelos/chatnut-api",
        },
        middleware=middleware,
    )

    return app


app = create_app()


class Prompt(BaseModel):
    prompt: str
    max_length: int = Query(160, ge=1)
    max_context_length: int = Query(4096, ge=2048)
    temperature: float = Query(0.8, ge=0.1)
    top_p: float = Query(0.94)
    singleline: bool = Query(False)


class RequestItem(BaseModel):
    request_id: str
    stream: bool = False


class ResponseItem(BaseModel):
    request_id: str
    response: str


@app.get("/ping")
async def pong():
    try:
        req = await client.get(f"{model_url}/health")
        res = req.json()
        logging.info(res)
        if req.status_code == 200:
            return {"status": "ok", "workers": res["workers"]}
        else:
            return {"status": "error", "result": "No model loaded!"}
    except Exception as e:
        return {"status": "error", "result": str(e)}


# For testing purposes only
# @app.post("/test/generate")
# async def generate_free(data: Prompt):
#     return await generate(data)


@app.post("/paid/generate")
async def generate(data: Prompt):
    payload = {
        "input": {
            "prompt": data.prompt,
            "max_new_tokens": data.max_length,
            "temperature": data.temperature,
            "top_k": 50,
            "top_p": 0.7,
            "repetition_penalty": 1.2,
            "batch_size": 8,
        }
    }
    try:
        req = await client.post(f"{model_url}/run", json=payload)
        if req.status_code == 200:
            res = req.json()
            request_id = res["id"]
            await request_queue.put(RequestItem(request_id=request_id))
            # Wait for the response to be available
            response_item = await wait_for_response(request_id)

            if response_item is None:
                raise HTTPException(
                    status_code=500,
                    detail="Error occurred while processing the request",
                )
            return JSONResponse(content=response_item.dict())

    except httpx.HTTPError as e:
        logging.error(f"HTTP error occurred (generate): {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    except Exception as e:
        logging.error(f"An error occurred (generate): {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def wait_for_response(request_id: str) -> ResponseItem:
    while True:
        # Check if the request ID is present in the response_items dictionary
        if request_id in response_items:
            response_item = response_items.pop(request_id)
            return response_item

        # Wait for the response_available event to be set
        await response_available.wait()
        response_available.clear()


async def process_requests():
    while True:
        # Dequeue a request item from the queue
        request_item = await request_queue.get()
        stream = request_item.stream or False
        previous_output = ""
        # Send the request to the external API
        try:
            while True:
                response = await client.get(f"/stream/{request_item.request_id}")
                if response.status_code == 200:
                    data = response.json()
                    if len(data["stream"]) > 0:
                        new_output = data["stream"][0]["output"]

                        if stream:
                            # output the stream to client
                            pass
                            # sys.stdout.write(new_output[len(previous_output):])
                            # sys.stdout.flush()
                        previous_output = new_output
                    if data.get("status") == "COMPLETED":
                        if not stream:
                            # Create a ResponseItem and add it to the list
                            response_item = ResponseItem(
                                request_id=request_item.request_id,
                                response=previous_output,
                            )
                            response_items[request_item.request_id] = response_item
                            # Set the response_available event
                            response_available.set()
                        break
                elif response.status_code >= 400:
                    pass
                # Sleep for 0.1 seconds between each request
                await asyncio.sleep(0.1 if stream else 1)
        except httpx.HTTPError as e:
            logging.error(f"HTTP error occurred (stream): {e}")
            # if the request fails, resume the loop and try again
            continue
        except Exception as e:
            # Handle error occurred during the request
            logging.error(f"An error occurred (stream): {e}")

        # Notify the queue that the task has been processed
        request_queue.task_done()


async def cancel_task(task_id: str):
    try:
        req = await client.get(f"{model_url}/cancel/{task_id}")
        res = req.json()
        return res
    except Exception as e:
        return {"status": "error", "result": str(e)}


@app.on_event("startup")
async def startup_event():
    global wallet

    # Start wallet and load the mint
    wallet = Wallet(url=mint_url, db="data/wallet")
    await init_wallet(wallet, load_proofs=False)
    await wallet.load_mint()

    # Start the background task to process requests
    asyncio.create_task(process_requests())


@app.on_event("shutdown")
async def shutdown_event():
    # Cancel the background task
    for task in asyncio.all_tasks():
        try:
            task.cancel()
        except Exception as exc:
            logging.warning(f"error while cancelling task: {str(exc)}")

    # Wait for the background task to be cancelled
    await asyncio.gather(*asyncio.all_tasks(), return_exceptions=True)


async def init_wallet(wallet: Wallet, load_proofs: bool = True):
    """Performs migrations and loads proofs from db."""
    await migrate_databases(wallet.db, migrations)
    if load_proofs:
        await wallet.load_proofs(reload=True)


"""
curl -X POST \
     --url http://localhost:8000/free/generate \
     --header 'accept: application/json' \
     --header 'content-type: application/json' \
     --data '{
        "prompt": "Who is Satoshi Nakamoto?",
        "max_length": 180,
        "temperature": 0.7
     }'
"""

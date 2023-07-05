import asyncio
import uuid

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

    class Config:
        env_file = ".env"

settings = Settings()


# Create a request queue
request_queue = asyncio.Queue()

wallet = None
mint_url = settings.MINT_URL
model_url = settings.MODEL_URL


class EcashHeaderMiddleware(BaseHTTPMiddleware):
    """
    Middleware that checks the HTTP headers for ecash
    """

    async def dispatch(self, request, call_next):
        # all requests to /cashu/* are not checked for ecash
        if request.url.path.startswith("/paid/"):
            # check whether valid ecash was provided in header
            token = request.headers.get("X-Cashu")
            if not token:
                # if LIGHTNING:
                #     payment_request, payment_hash = await ledger.request_mint(1000)
                # else:
                payment_request, payment_hash = "payment_request", "payment_hash"
                return JSONResponse(
                    {
                        "detail": "This endpoint requires a X-Cashu ecash header",
                        "pr": payment_request,
                        "hash": payment_hash,
                        "mint": "http://localhost:8000/cashu",
                    },
                    status_code=402,
                )
            tokenObj = await deserialize_token_from_string(token + "=")
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
    data: dict

class ResponseItem(BaseModel):
    request_id: str
    response: dict

@app.get("/ping")
async def pong():  
    async with httpx.AsyncClient(base_url=model_url) as client:
        try:
            req = await client.get("/api/v1/model")
            res = req.json()
            print(res)
            if res:
                return {"status": "ok", "result": res["result"]}
            else:
                return {"status": "error", "result": "No model loaded!"}
        except Exception as e:
            return {"status": "error", "result": str(e)}
    
    

        

@app.post("/paid/generate")
async def generate(data: Prompt):
    # Create a unique ID for the request
    request_id = str(uuid.uuid4())

    # Create a RequestItem and enqueue it
    request_item = RequestItem(request_id=request_id, data={**data.dict()})
    await request_queue.put(request_item)

    # Wait for the response to be available
    response_item = await wait_for_response(request_id)

    if response_item is None:
        raise HTTPException(status_code=500, detail="Error occurred while processing the request")

    return JSONResponse(content=response_item.dict())


async def wait_for_response(request_id: str) -> ResponseItem:
    # Wait for the response to be available
    while True:
        # Check if the request ID is present in the response items
        response_item = next((item for item in response_items if item.request_id == request_id), None)

        if response_item is not None:
            # Remove the response item from the list
            response_items.remove(response_item)
            return response_item

        await asyncio.sleep(0.1)  # Polling interval

async def process_requests():
    async with httpx.AsyncClient(base_url=model_url) as client:
        while True:
            # Dequeue a request item from the queue
            request_item = await request_queue.get()

            # Send the request to the external API
            try:
                response = await client.post("/api/v1/generate", timeout=180, json=request_item.data)
                response_data = response.json()

                # Create a ResponseItem and add it to the list
                response_item = ResponseItem(request_id=request_item.request_id, response=response_data)
                response_items.append(response_item)
            except Exception as e:
                # Handle error occurred during the request
                print(f"Error occurred while sending request: {e}")

            # Notify the queue that the task has been processed
            request_queue.task_done()


# List to hold response items
response_items = []


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
        task.cancel()

    # Wait until all tasks are cancelled
    await asyncio.gather(*asyncio.all_tasks(), return_exceptions=True)

async def init_wallet(wallet: Wallet, load_proofs: bool = True):
    """Performs migrations and loads proofs from db."""
    await migrate_databases(wallet.db, migrations)
    if load_proofs:
        await wallet.load_proofs(reload=True)
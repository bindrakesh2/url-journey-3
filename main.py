import asyncio
import httpx
import uvicorn
import logging
import socket
import ipaddress
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from urllib.parse import urlparse, urljoin
import time

# --- Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FastAPI App Initialization ---
app = FastAPI()

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Browser User-Agent ---
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
}

# --- Advanced Server Name Detection Logic ---
AKAMAI_IP_RANGES = ["23.192.0.0/11", "104.64.0.0/10", "184.24.0.0/13"]
ip_cache = {}

async def resolve_ip_async(hostname: str):
    if not hostname: return None
    if hostname in ip_cache: return ip_cache[hostname]
    try:
        ip = await asyncio.to_thread(socket.gethostbyname, hostname)
        ip_cache[hostname] = ip
        return ip
    except (socket.gaierror, TypeError): return None

def is_akamai_ip(ip: str) -> bool:
    if not ip: return False
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in AKAMAI_IP_RANGES:
            if addr in ipaddress.ip_network(cidr): return True
    except ValueError: pass
    return False

async def get_server_name_advanced(headers: dict, url: str) -> str:
    headers = {k.lower(): v for k, v in headers.items()}
    hostname = urlparse(url).hostname
    if hostname and ("bmw" in hostname.lower() or "mini" in hostname.lower()):
        if "cache-control" in headers:
            return "Apache (AEM)"
    server_value = headers.get("server", "").lower()
    if server_value:
        if "akamai" in server_value or "ghost" in server_value: return "Akamai"
        if "apache" in server_value: return "Apache (AEM)"
        return server_value.capitalize()
    
    server_timing = headers.get("server-timing", "")
    has_akamai_cache = "cdn-cache; desc=HIT" in server_timing or "cdn-cache; desc=MISS" in server_timing
    has_akamai_request_id = "x-akamai-request-id" in headers
    has_dispatcher = "x-dispatcher" in headers or "x-aem-instance" in headers
    has_aem_paths = any("/etc.clientlibs" in v for h, v in headers.items() if h in ["link", "baqend-tags"])
    
    ip = await resolve_ip_async(hostname)
    is_akamai = is_akamai_ip(ip)

    if has_akamai_cache or has_akamai_request_id or (server_timing and is_akamai):
        if has_aem_paths or has_dispatcher: return "Apache (AEM)"
        return "Akamai"
    
    if has_dispatcher or has_aem_paths: return "Apache (AEM)"
    if is_akamai: return "Akamai"
    
    return "Unknown"

# --- Core URL Analysis Logic ---
async def check_url_status(client: httpx.AsyncClient, url: str):
    start_time = time.time()
    redirect_chain = []
    current_url = url
    MAX_REDIRECTS = 15

    try:
        for _ in range(MAX_REDIRECTS):
            response = await client.get(current_url, follow_redirects=False, timeout=60.0)
            server_name = await get_server_name_advanced(response.headers, str(response.url))
            
            hop_info = {
                "url": str(response.url), 
                "status": response.status_code, 
                "serverName": server_name
            }
            redirect_chain.append(hop_info)

            if response.is_redirect:
                target_url = response.headers.get('location')
                if not target_url:
                    raise Exception("Redirect missing location header")
                
                next_url = urljoin(str(response.url), target_url)
                if next_url == current_url:
                    raise Exception("URL redirects to itself in a loop")
                current_url = next_url
            else:
                response.raise_for_status()
                break 
        
        if len(redirect_chain) >= MAX_REDIRECTS:
            raise Exception("Too many redirects")
        
        return {
            "url": url,
            "status": redirect_chain[0]['status'] if redirect_chain else None,
            "comment": "Redirect Chain" if len(redirect_chain) > 1 else "OK",
            "serverName": redirect_chain[0]['serverName'] if redirect_chain else "N/A",
            "redirectChain": redirect_chain
        }

    except Exception as e:
        comment = "An unexpected error occurred"
        if isinstance(e, httpx.TimeoutException):
            comment = "Request timed out after 60s"
        elif isinstance(e, httpx.HTTPStatusError):
            comment = f"HTTP Error: {e.response.status_code}"
            # Add the final error as a hop in the chain for visibility
            redirect_chain.append({"url": str(e.response.url), "status": e.response.status_code, "serverName": "N/A"})
        elif isinstance(e, httpx.RequestError):
            comment = "Request failed (Network/DNS error)"
        else:
            comment = str(e)
        
        return {
            "url": url,
            "status": redirect_chain[0]['status'] if redirect_chain else "Error",
            "comment": comment,
            "serverName": redirect_chain[0]['serverName'] if redirect_chain else "N/A",
            "redirectChain": redirect_chain
        }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        # This version receives plain text from the simple HTML textarea
        data = await websocket.receive_text()
        urls = data.splitlines()
        
        should_throttle = len(urls) > 50
        CONCURRENCY_LIMIT = 25
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def bound_check(url, client):
            async with semaphore:
                if should_throttle:
                    await asyncio.sleep(0.1)
                return await check_url_status(client, url)

        async with httpx.AsyncClient(http2=True, limits=httpx.Limits(max_connections=100), verify=False, headers=BROWSER_HEADERS) as client:
            tasks = []
            cleaned_urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))

            for url in cleaned_urls:
                if not url.startswith(('http://', 'https://')):
                    url = f'https://{url}'
                tasks.append(asyncio.create_task(bound_check(url, client)))

            for future in asyncio.as_completed(tasks):
                result = await future
                if websocket.client_state.name == 'CONNECTED':
                    await websocket.send_json(result)
        
        if websocket.client_state.name == 'CONNECTED':
            await websocket.send_json({"status": "done"})
            
    except WebSocketDisconnect:
        logger.info("Client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)
    finally:
        logger.info("Connection closed.")

# This serves the index.html file as the main page
@app.get("/")
async def read_index():
    return FileResponse('index.html')

@app.get("/test")
async def test():
    return {"status": "OK", "message": "Service operational"}

import asyncio
import logging
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Import the routers
from routers.notifications import router, set_connection_manager
from routers.email_notifications import router_email  # NEW: Import email router

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- WebSocket Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"✅ New WebSocket connection. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)  # Use discard to avoid KeyError
        logger.info(f"❌ WebSocket disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        if not self.active_connections:
            logger.debug("No active connections to broadcast to")
            return
        
        # Send to all connections and handle failures gracefully
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.error(f"Failed to send message to client: {e}")
                disconnected.add(connection)
        
        # Remove disconnected clients
        for connection in disconnected:
            self.disconnect(connection)
        
        logger.info(f"📡 Broadcasted message to {len(self.active_connections)} client(s)")

# --- Initialize Manager ---
manager = ConnectionManager()

# Inject the manager into the notifications router
set_connection_manager(manager)

# --- FastAPI App ---
app = FastAPI(
    title="Notification Service API",
    version="2.0.0",
    description="WebSocket and Email Notification Service for Bleu Bean Cafe"
)

# --- WebSocket Endpoint ---
@app.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive and handle client messages if needed
            data = await websocket.receive_text()
            logger.debug(f"Received message from client: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)

# --- Health Check Endpoint ---
@app.get("/health", tags=["Health"])
async def health_check():
    """Check if the service is running"""
    return {
        "status": "healthy",
        "service": "notification-service",
        "websocket_clients": len(manager.active_connections)
    }

# --- Include Routers ---
app.include_router(router)  # WebSocket notifications
app.include_router(router_email)  # NEW: Email notifications

# --- CORS Configuration ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bleu-pos-tau.vercel.app/",
        "https://bleu-ims-beta.vercel.app",
        ""
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Startup Event ---
@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Notification Service started successfully")
    logger.info("📡 WebSocket endpoint: ws://localhost:9004/ws/notifications")
    logger.info("📧 Email API endpoint: https://notificationservice-1jp5.onrender.com")

# --- Shutdown Event ---
@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Notification Service shutting down")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        port=9004,
        host="0.0.0.0",
        reload=True,
        log_level="info"
    )
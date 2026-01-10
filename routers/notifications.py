import logging
from typing import List

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

# --- Database Connection ---
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + '/../')
from database import get_db_connection

# --- Logging Configuration ---
logger = logging.getLogger(__name__)

# --- Pydantic Models ---
class NotificationBase(BaseModel):
    NotificationID: int
    SaleID: int | None
    Message: str
    CreatedAt: str
    IsRead: bool
    IsDone: bool

class CreateNotificationRequest(BaseModel):
    sale_id: int
    message: str

# --- Router ---
router = APIRouter(prefix="/notifications", tags=["Notifications"])

# This will be injected from main.py
manager = None

def set_connection_manager(conn_manager):
    """Set the WebSocket connection manager"""
    global manager
    manager = conn_manager

# --- API Endpoints ---
@router.post("/", status_code=status.HTTP_201_CREATED, summary="Create and broadcast a new notification")
async def create_notification(request: CreateNotificationRequest):
    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            sql = """
                INSERT INTO Notifications (SaleID, Message)
                OUTPUT INSERTED.NotificationID, INSERTED.SaleID, INSERTED.Message,
                       CONVERT(varchar, INSERTED.CreatedAt, 127), INSERTED.IsRead, INSERTED.IsDone
                VALUES (?, ?);
            """
            await cursor.execute(sql, request.sale_id, request.message)
            new_notif_row = await cursor.fetchone()
            await conn.commit()

            if not new_notif_row:
                raise HTTPException(status_code=500, detail="Failed to create and retrieve notification.")

            notification_data = {
                "NotificationID": new_notif_row[0], "SaleID": new_notif_row[1], "Message": new_notif_row[2],
                "CreatedAt": new_notif_row[3], "IsRead": bool(new_notif_row[4]), "IsDone": bool(new_notif_row[5]),
            }

            if manager:
                await manager.broadcast({"type": "new_notification", "payload": notification_data})
            
            return {"message": "Notification created successfully.", "data": notification_data}
    except Exception as e:
        logger.error(f"Error creating notification: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while creating notification.")
    finally:
        if conn: await conn.close()

@router.get("/", response_model=List[NotificationBase], summary="Get all active (not done) notifications")
async def get_notifications():
    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            sql = """
                SELECT NotificationID, SaleID, Message, 
                       CONVERT(varchar, CreatedAt, 127) as CreatedAt, 
                       IsRead, IsDone
                FROM Notifications 
                WHERE IsDone = 0 
                ORDER BY CreatedAt DESC;
            """
            await cursor.execute(sql)
            rows = await cursor.fetchall()
            
            notifications = []
            for row in rows:
                notification_dict = {
                    "NotificationID": row.NotificationID,
                    "SaleID": row.SaleID,
                    "Message": row.Message,
                    "CreatedAt": row.CreatedAt,
                    "IsRead": bool(row.IsRead),
                    "IsDone": bool(row.IsDone)
                }
                notifications.append(notification_dict)
            
            logger.info(f"Successfully fetched {len(notifications)} notifications")
            return notifications
    except Exception as e:
        logger.error(f"Error fetching notifications: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch notifications: {str(e)}")
    finally:
        if conn: await conn.close()

@router.patch("/{notif_id}/read", status_code=status.HTTP_200_OK, summary="Mark a single notification as read")
async def mark_as_read(notif_id: int):
    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT NotificationID, IsRead FROM Notifications WHERE NotificationID = ?", notif_id)
            existing = await cursor.fetchone()
            
            if not existing:
                raise HTTPException(status_code=404, detail="Notification not found.")
            
            await cursor.execute("UPDATE Notifications SET IsRead = 1 WHERE NotificationID = ?", notif_id)
            await conn.commit()
            
            if manager:
                await manager.broadcast({
                    "type": "notification_read", 
                    "payload": {"NotificationID": notif_id}
                })
            
            logger.info(f"Notification {notif_id} marked as read")
            return {"message": "Notification marked as read."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking notification as read: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to mark notification as read: {str(e)}")
    finally:
        if conn: await conn.close()

@router.patch("/{notif_id}/done", status_code=status.HTTP_200_OK, summary="Mark a notification as done")
async def mark_as_done(notif_id: int):
    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE Notifications SET IsDone = 1, IsRead = 1 WHERE NotificationID = ?", notif_id)
            await conn.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="Notification not found.")
            
            if manager:
                await manager.broadcast({"type": "notification_done", "payload": {"NotificationID": notif_id}})
            
            return {"message": "Notification marked as done."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error marking notification as done: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to mark notification as done: {str(e)}")
    finally:
        if conn: await conn.close()

@router.patch("/read-all", status_code=status.HTTP_200_OK, summary="Mark all notifications as read")
async def mark_all_as_read():
    conn = None
    try:
        conn = await get_db_connection()
        async with conn.cursor() as cursor:
            await cursor.execute("UPDATE Notifications SET IsRead = 1 WHERE IsRead = 0")
            await conn.commit()
            
            if manager:
                await manager.broadcast({"type": "notifications_read_all"})
            
            return {"message": "All notifications marked as read."}
    except Exception as e:
        logger.error(f"Error marking all as read: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to mark all as read: {str(e)}")
    finally:
        if conn: await conn.close()
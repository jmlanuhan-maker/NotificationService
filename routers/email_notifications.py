import logging
import smtplib
import os
import httpx
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, status, Header
from pydantic import BaseModel, EmailStr, ConfigDict
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Logging Configuration ---
logger = logging.getLogger(__name__)

# --- Email Configuration ---
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")
SENDER_NAME = os.getenv("SENDER_NAME", "Bleu Bean Cafe")
AUTH_API_BASE_URL = os.getenv("AUTH_API_BASE_URL", "https://authservices-npr8.onrender.com")
DELIVERY_FEE = 50.0  # Delivery fee constant

# --- Pydantic Models ---
class AddonItem(BaseModel):
    addon_id: int
    addon_name: str
    price: float

class OrderItem(BaseModel):
    name: str
    quantity: int
    price: float
    addons: List[AddonItem] = []
    promo_name: Optional[str] = None  # Promotion name if applied
    promo_type: Optional[str] = None  # percentage, fixed, bogo
    promo_value: Optional[float] = None  # Discount percentage or amount
    promo_discount: Optional[float] = None  # Actual discount amount in pesos

class EmailNotificationRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')  # Ignore extra fields from frontend
    
    customer_id: Optional[int] = None  # optional, can use customer_name as fallback
    customer_name: str
    customer_email: Optional[str] = None  # optional, direct email if available
    order_id: str
    order_type: str
    status: str
    items: List[OrderItem]
    total: float
    payment_method: str
    delivery_address: Optional[str] = None
    phone_number: Optional[str] = None
    reference_number: Optional[str] = None
    delivery_fee: Optional[float] = None  # Actual delivery fee from order (if null, will use order_type to determine)

# --- Router ---
router_email = APIRouter(prefix="/email", tags=["Email Notifications"])

# --- Helper Function to Fetch Email ---
async def fetch_user_email(user_id: Optional[int] = None, username: Optional[str] = None, auth_token: str = None) -> Optional[str]:
    """Fetch user email from auth service using either user_id or username"""
    if not auth_token:
        return None
    
    if not user_id and not username:
        return None
    
    try:
        async with httpx.AsyncClient() as client:
            if user_id:
                # Try fetching by user_id first
                response = await client.get(
                    f"{AUTH_API_BASE_URL}/users/email/by-id",
                    params={"user_id": user_id},
                    headers={"Authorization": f"Bearer {auth_token}"},
                    timeout=10.0
                )
            else:
                # Fallback to fetching by username
                response = await client.get(
                    f"{AUTH_API_BASE_URL}/users/email",
                    params={"username": username},
                    headers={"Authorization": f"Bearer {auth_token}"},
                    timeout=10.0
                )

            if response.status_code == 200:
                data = response.json()
                email = data.get("email")
                identifier = user_id if user_id else username
                logger.info(f"✅ Successfully fetched email for {identifier}: {email}")
                return email
            else:
                identifier = user_id if user_id else username
                logger.error(f"❌ Failed to fetch email for {identifier}: {response.status_code}")
                return None
                
    except httpx.TimeoutException:
        identifier = user_id if user_id else username
        logger.error(f"❌ Timeout fetching email for {identifier}")
        return None
    except Exception as e:
        identifier = user_id if user_id else username
        logger.error(f"❌ Error fetching email for {identifier}: {e}")
        return None

# --- Helper Function to Calculate Costs ---
def calculate_order_breakdown(data: EmailNotificationRequest):
    """Calculate subtotal, add-ons, delivery fee, discount, and final total"""
    subtotal = 0.0
    addons_total = 0.0
    discount_total = 0.0
    
    for item in data.items:
        # Calculate base item cost
        subtotal += item.price * item.quantity
        
        # Calculate add-ons cost
        if item.addons:
            for addon in item.addons:
                addons_total += addon.price
        
        # Calculate discount from promotions
        if item.promo_discount and item.promo_discount > 0:
            discount_total += item.promo_discount
    
    # Use provided delivery_fee or determine based on order_type
    is_delivery = data.order_type.lower() == "delivery"
    if data.delivery_fee is not None:
        # Use the delivery fee provided from the frontend
        delivery_fee = float(data.delivery_fee)
    else:
        # Fallback: Use the hardcoded constant if not provided
        delivery_fee = DELIVERY_FEE if is_delivery else 0.0
    
    # Calculate final total (including discounts)
    final_total = subtotal + addons_total + delivery_fee - discount_total
    
    return {
        "subtotal": subtotal,
        "addons_total": addons_total,
        "discount_total": discount_total,
        "delivery_fee": delivery_fee,
        "final_total": max(final_total, 0),  # Ensure final total doesn't go negative
        "is_delivery": is_delivery
    }

# --- Email Template Functions ---
def create_order_accepted_email(data: EmailNotificationRequest) -> str:
    """Generate HTML email for order acceptance"""
    
    # Calculate breakdown
    breakdown = calculate_order_breakdown(data)
    
    items_html = ""
    for item in data.items:
        addons_text = ""
        if item.addons:
            addon_names = [f"{addon.addon_name}" for addon in item.addons]
            addons_text = f"<br><small style='color: #666; padding-left: 20px;'>with: {', '.join(addon_names)}</small>"
        
        promo_text = ""
        if item.promo_name and item.promo_discount and item.promo_discount > 0:
            promo_text = f"<br><small style='color: #28a745; padding-left: 20px; font-weight: bold;'>✓ {item.promo_name}: -₱{item.promo_discount:.2f}</small>"
        
        items_html += f"""
        <tr>
            <td style='padding: 8px; border-bottom: 1px solid #eee;'>
                {item.name}{addons_text}{promo_text}
            </td>
            <td style='padding: 8px; border-bottom: 1px solid #eee; text-align: center;'>{item.quantity}</td>
            <td style='padding: 8px; border-bottom: 1px solid #eee; text-align: right;'>₱{item.price:.2f}</td>
        </tr>
        """
    
    # Only show delivery info if order type is "Delivery" and address exists
    delivery_info = ""
    if breakdown["is_delivery"] and data.delivery_address:
        delivery_info = f"""
        <div style='background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 20px;'>
            <h3 style='color: #4B929D; margin-top: 0;'>Delivery Information</h3>
            <p style='margin: 5px 0;'><strong>Address:</strong> {data.delivery_address}</p>
            <p style='margin: 5px 0;'><strong>Phone:</strong> {data.phone_number or 'N/A'}</p>
            <p style='margin: 5px 0;'><strong>Delivery Fee:</strong> ₱{breakdown['delivery_fee']:.2f}</p>
        </div>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #4B929D; color: white; padding: 30px 20px; text-align: center; border-radius: 5px 5px 0 0; }}
            .header h1 {{ margin: 0; font-size: 28px; }}
            .header p {{ margin: 5px 0 0 0; font-size: 14px; }}
            .content {{ background-color: #ffffff; padding: 30px; border: 1px solid #ddd; border-top: none; }}
            .order-status {{ background-color: #28a745; color: white; padding: 15px; text-align: center; border-radius: 5px; font-weight: bold; font-size: 18px; margin-bottom: 20px; }}
            .footer {{ background-color: #f8f9fa; padding: 20px; text-align: center; border-radius: 0 0 5px 5px; font-size: 12px; color: #666; border: 1px solid #ddd; border-top: none; }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            th {{ background-color: #4B929D; color: white; padding: 12px; text-align: left; }}
            .info-box {{ background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin-top: 20px; border-radius: 5px; }}
            .total-section {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 20px; }}
            .total-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #ddd; }}
            .total-row.final {{ border-top: 2px solid #4B929D; border-bottom: none; font-weight: bold; font-size: 18px; color: #4B929D; padding-top: 12px; margin-top: 8px; }}
        </style>
    </head>
    <body>
        <div class='container'>
            <div class='header'>
                <h1>BLEU BEAN CAFE</h1>
                <p>Order Confirmation</p>
            </div>
            <div class='content'>
                <div class='order-status'>
                    ✓ ORDER ACCEPTED
                </div>
                <p>Dear <strong>Ma'am/Sir</strong>,</p>
                <p>Great news! Your order has been accepted and is now being prepared by our team.</p>
                
                <h3 style='color: #4B929D; border-bottom: 2px solid #4B929D; padding-bottom: 5px;'>Order Details</h3>
                <p style='margin: 5px 0;'><strong>Order Type:</strong> {data.order_type}</p>
                <p style='margin: 5px 0;'><strong>Payment Method:</strong> {data.payment_method}</p>
                {f"<p style='margin: 5px 0;'><strong>Reference Number:</strong> {data.reference_number}</p>" if data.reference_number else ""}
                <p style='margin: 5px 0;'><strong>Date:</strong> {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
                
                <h3 style='color: #4B929D; border-bottom: 2px solid #4B929D; padding-bottom: 5px; margin-top: 25px;'>Order Items</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Item</th>
                            <th style='text-align: center; width: 80px;'>Qty</th>
                            <th style='text-align: right; width: 100px;'>Price</th>
                        </tr>
                    </thead>
                    <tbody>
                        {items_html}
                    </tbody>
                </table>
                
                <div class='total-section'>
                    <div class='total-row'>
                        <span>Subtotal:</span>
                        <span>₱{breakdown['subtotal']:.2f}</span>
                    </div>
                    {f"<div class='total-row'><span>Add-ons:</span><span>+ ₱{breakdown['addons_total']:.2f}</span></div>" if breakdown['addons_total'] > 0 else ""}
                    {f"<div class='total-row' style='color: #28a745;'><span>Discount:</span><span>- ₱{breakdown['discount_total']:.2f}</span></div>" if breakdown['discount_total'] > 0 else ""}
                    {f"<div class='total-row'><span>Delivery Fee:</span><span>+ ₱{breakdown['delivery_fee']:.2f}</span></div>" if breakdown['is_delivery'] else ""}
                    <div class='total-row final'>
                        <span>Total Amount:</span>
                        <span>₱{breakdown['final_total']:.2f}</span>
                    </div>
                </div>
                
                {delivery_info}
                
                <div class='info-box'>
                    <strong>📋 Next Steps:</strong>
                    <ul style='margin: 10px 0; padding-left: 20px;'>
                        <li>Your order is now being prepared</li>
                        <li>You'll receive another email when it's ready for {data.order_type.lower()}</li>
                        <li>Estimated preparation time: 15-30 minutes</li>
                    </ul>
                </div>
            </div>
            <div class='footer'>
                <p style='margin: 5px 0; font-weight: bold;'>Thank you for choosing Bleu Bean Cafe!</p>
                <p style='margin: 5px 0;'>Don Fabian St., Commonwealth, Quezon City, Philippines</p>
                <p style='margin: 5px 0;'>Phone: +63 961 687 2463</p>
                <p style='margin: 15px 0 5px 0; font-size: 11px;'>This is an automated email. Please do not reply.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def create_order_update_email(data: EmailNotificationRequest) -> str:
    """Generate HTML email for order status updates"""
    
    # Calculate breakdown
    breakdown = calculate_order_breakdown(data)
    
    status_colors = {
        'PREPARING': '#ffc107',
        'WAITING FOR PICK UP': '#17a2b8',
        'DELIVERING': '#007bff',
        'COMPLETED': '#28a745',
        'DELIVERED': '#28a745',
        'CANCELLED': '#dc3545',
        'READY FOR PICK UP': '#17a2b8'
    }
    
    status_messages = {
        'PREPARING': '👨‍🍳 Your order is being prepared by our team.',
        'WAITING FOR PICK UP': '✅ Your order is ready for pick up!',
        'READY FOR PICK UP': '✅ Your order is ready for pick up!',
        'DELIVERING': '🚗 Your order is on the way!',
        'COMPLETED': '✓ Your order has been completed. Thank you!',
        'DELIVERED': '✓ Your order has been delivered. Enjoy your meal!',
        'CANCELLED': '❌ Your order has been cancelled.'
    }
    
    status_instructions = {
        'WAITING FOR PICK UP': 'Please proceed to our store to pick up your order. Show this email to our staff.',
        'READY FOR PICK UP': 'Please proceed to our store to pick up your order. Show this email to our staff.',
        'DELIVERING': 'Our rider is on the way to your location. Please have your payment ready if you chose cash on delivery.',
        'COMPLETED': 'We hope you enjoyed your order! Please consider leaving us a review.',
        'DELIVERED': 'We hope you enjoyed your order! Please consider leaving us a review.',
        'CANCELLED': 'If you have any questions about this cancellation, please contact us.'
    }
    
    color = status_colors.get(data.status, '#4B929D')
    message = status_messages.get(data.status, 'Your order status has been updated.')
    instructions = status_instructions.get(data.status, '')
    
    # Build items summary for update email
    items_summary = ""
    for item in data.items:
        addons_text = ""
        if item.addons:
            addon_names = [f"{addon.addon_name}" for addon in item.addons]
            addons_text = f" (with: {', '.join(addon_names)})"
        
        promo_text = ""
        if item.promo_name and item.promo_discount and item.promo_discount > 0:
            promo_text = f" <strong style='color: #28a745;'>✓ {item.promo_name}: -₱{item.promo_discount:.2f}</strong>"
        
        items_summary += f"<li>{item.name}{addons_text}{promo_text} - Qty: {item.quantity}</li>"
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
            .header {{ background-color: #4B929D; color: white; padding: 30px 20px; text-align: center; border-radius: 5px 5px 0 0; }}
            .header h1 {{ margin: 0; font-size: 28px; }}
            .header p {{ margin: 5px 0 0 0; font-size: 14px; }}
            .content {{ background-color: #ffffff; padding: 30px; border: 1px solid #ddd; border-top: none; }}
            .order-status {{ background-color: {color}; color: white; padding: 15px; text-align: center; border-radius: 5px; font-weight: bold; font-size: 20px; margin-bottom: 20px; }}
            .footer {{ background-color: #f8f9fa; padding: 20px; text-align: center; border-radius: 0 0 5px 5px; font-size: 12px; color: #666; border: 1px solid #ddd; border-top: none; }}
            .info-box {{ background-color: #e7f3ff; border-left: 4px solid #007bff; padding: 15px; margin-top: 20px; border-radius: 5px; }}
            .message {{ font-size: 18px; color: #333; margin: 20px 0; padding: 15px; background-color: #f8f9fa; border-radius: 5px; }}
            .items-box {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin: 15px 0; }}
            .items-box ul {{ margin: 10px 0; padding-left: 20px; }}
            .items-box li {{ margin: 5px 0; color: #333; }}
            .total-section {{ background-color: #f8f9fa; padding: 15px; border-radius: 5px; margin-top: 15px; }}
            .total-row {{ display: flex; justify-content: space-between; padding: 5px 0; }}
            .total-row.final {{ font-weight: bold; font-size: 16px; color: #4B929D; border-top: 2px solid #4B929D; padding-top: 10px; margin-top: 5px; }}
        </style>
    </head>
    <body>
        <div class='container'>
            <div class='header'>
                <h1>BLEU BEAN CAFE</h1>
                <p>Order Status Update</p>
            </div>
            <div class='content'>
                <div class='order-status'>
                    {data.status}
                </div>
                <p>Dear <strong>Ma'am/Sir</strong>,</p>
                <div class='message'>
                    {message}
                </div>
                
                {f"<p style='margin: 15px 0;'>{instructions}</p>" if instructions else ""}
                
                <div class='items-box'>
                    <h3 style='color: #4B929D; margin-top: 0;'>Your Order</h3>
                    <ul>
                        {items_summary}
                    </ul>
                    <div class='total-section'>
                        <div class='total-row'>
                            <span>Subtotal:</span>
                            <span>₱{breakdown['subtotal']:.2f}</span>
                        </div>
                        {f"<div class='total-row'><span>Add-ons:</span><span>+ ₱{breakdown['addons_total']:.2f}</span></div>" if breakdown['addons_total'] > 0 else ""}
                        {f"<div class='total-row' style='color: #28a745;'><span>Discount:</span><span>- ₱{breakdown['discount_total']:.2f}</span></div>" if breakdown['discount_total'] > 0 else ""}
                        {f"<div class='total-row'><span>Delivery Fee:</span><span>+ ₱{breakdown['delivery_fee']:.2f}</span></div>" if breakdown['is_delivery'] else ""}
                        <div class='total-row final'>
                            <span>Total:</span>
                            <span>₱{breakdown['final_total']:.2f}</span>
                        </div>
                    </div>
                </div>
                
                <h3 style='color: #4B929D; border-bottom: 2px solid #4B929D; padding-bottom: 5px; margin-top: 25px;'>Order Information</h3>
                <p style='margin: 5px 0;'><strong>Order Type:</strong> {data.order_type}</p>
                <p style='margin: 5px 0;'><strong>Status:</strong> <span style='color: {color}; font-weight: bold;'>{data.status}</span></p>
                <p style='margin: 5px 0;'><strong>Updated:</strong> {datetime.now().strftime('%B %d, %Y %I:%M %p')}</p>
                
                <div class='info-box'>
                    <strong>📞 Need Help?</strong>
                    <p style='margin: 10px 0 0 0;'>Contact us at <strong>+63 961 687 2463</strong> or visit our store at Don Fabian St., Commonwealth, Quezon City.</p>
                </div>
            </div>
            <div class='footer'>
                <p style='margin: 5px 0; font-weight: bold;'>Thank you for choosing Bleu Bean Cafe!</p>
                <p style='margin: 5px 0;'>Don Fabian St., Commonwealth, Quezon City, Philippines</p>
                <p style='margin: 5px 0;'>Phone: +63 961 687 2463</p>
                <p style='margin: 15px 0 5px 0; font-size: 11px;'>This is an automated email. Please do not reply.</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# --- Email Sending Function ---
async def send_email(to_email: str, subject: str, html_content: str) -> bool:
    """Send email using SMTP"""
    try:
        if not SENDER_EMAIL or not SENDER_PASSWORD:
            logger.error("Email credentials not configured. Please set SENDER_EMAIL and SENDER_PASSWORD in .env")
            return False
            
        msg = MIMEMultipart('alternative')
        msg['From'] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        
        html_part = MIMEText(html_content, 'html')
        msg.attach(html_part)
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        
        logger.info(f"✅ Email sent successfully to {to_email} - Subject: {subject}")
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(f"❌ SMTP Authentication failed. Check your email credentials.")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"❌ SMTP error sending email to {to_email}: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Failed to send email to {to_email}: {e}", exc_info=True)
        return False

# --- API Endpoints ---
@router_email.post(
    "/send-order-accepted",
    status_code=status.HTTP_200_OK,
    summary="Send email when order is accepted"
)
async def send_order_accepted_email(request: EmailNotificationRequest, authorization: Optional[str] = Header(None)):
    """Send email notification when order is accepted (PENDING → PREPARING)"""
    try:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid authorization header"
            )
        
        auth_token = authorization.replace("Bearer ", "")
        
        logger.info(f"📧 Attempting to send order accepted email for order {request.order_id}")
        logger.info(f"   Delivery Fee from request: {request.delivery_fee}")
        logger.info(f"   Order Type: {request.order_type}")
        
        # Use customer_email if provided, otherwise fetch from auth service
        user_email = request.customer_email
        if not user_email:
            user_email = await fetch_user_email(
                user_id=request.customer_id,
                username=request.customer_name,
                auth_token=auth_token
            )

        
        if not user_email:
            logger.warning(f"⚠️ No email found for customer {request.customer_name}, skipping email notification")
            return {
                "success": False,
                "message": "No email address found for customer",
                "order_id": request.order_id
            }
        
        logger.info(f"📧 Sending to {user_email} for order {request.order_id}")
        
        html_content = create_order_accepted_email(request)
        success = await send_email(
            to_email=user_email,
            subject=f"✓ Order Accepted - Bleu Bean Cafe",
            html_content=html_content
        )
        
        if success:
            return {
                "success": True,
                "message": "Order acceptance email sent successfully",
                "email": user_email,
                "order_id": request.order_id
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send email. Check server logs."
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in send_order_accepted_email: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send email: {str(e)}"
        )

@router_email.post(
    "/send-order-update",
    status_code=status.HTTP_200_OK,
    summary="Send email when order status is updated"
)
async def send_order_update_email(request: EmailNotificationRequest, authorization: Optional[str] = Header(None)):
    """Send email notification when order status changes"""
    try:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid authorization header"
            )
        
        auth_token = authorization.replace("Bearer ", "")
        
        logger.info(f"📧 Attempting to send order update email for order {request.order_id} (Status: {request.status})")
        
        # Use customer_email if provided, otherwise fetch from auth service
        user_email = request.customer_email
        if not user_email:
            user_email = await fetch_user_email(
                user_id=request.customer_id,
                username=request.customer_name,
                auth_token=auth_token
            )
        
        if not user_email:
            logger.warning(f"⚠️ No email found for customer {request.customer_name}, skipping email notification")
            return {
                "success": False,
                "message": "No email address found for customer",
                "order_id": request.order_id,
                "status": request.status
            }
        
        logger.info(f"📧 Sending to {user_email} for order {request.order_id} (Status: {request.status})")
        
        html_content = create_order_update_email(request)
        success = await send_email(
            to_email=user_email,
            subject=f"Order Update: {request.status} - Bleu Bean Cafe",
            html_content=html_content
        )
        
        if success:
            return {
                "success": True,
                "message": "Order update email sent successfully",
                "email": user_email,
                "order_id": request.order_id,
                "status": request.status
            }
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send email. Check server logs."
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in send_order_update_email: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send email: {str(e)}"
        )

@router_email.get(
    "/test-connection",
    status_code=status.HTTP_200_OK,
    summary="Test email configuration"
)
async def test_email_connection():
    """Test if email is properly configured"""
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return {
            "configured": False,
            "message": "Email credentials not set. Please configure SENDER_EMAIL and SENDER_PASSWORD in .env file."
        }
    
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
        
        return {
            "configured": True,
            "message": "Email configuration is valid and connection successful",
            "smtp_server": SMTP_SERVER,
            "sender_email": SENDER_EMAIL
        }
    except Exception as e:
        return {
            "configured": False,
            "message": f"Email configuration error: {str(e)}",
            "smtp_server": SMTP_SERVER
        }

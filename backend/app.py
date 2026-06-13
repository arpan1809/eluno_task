import os
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from typing import Optional, List

from backend.db import get_db_conn, init_db, check_order_inventory
from backend.ai_engine import get_ai_predictions

# Create directories if not exists
os.makedirs("static/css", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Initialize database
init_db()

app = FastAPI(title="Eluno Eyewear AI-Powered OMS")

# System Status Endpoint
@app.get("/api/status")
def get_system_status():
    api_key = os.environ.get("GROQ_API_KEY")
    return {
        "server_status": "Online",
        "groq_configured": api_key is not None,
        "groq_model": "llama-3.3-70b-versatile" if api_key else "Fallback Simulation"
    }

# Mount static and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Pydantic Schemas for API validation
class OrderCreate(BaseModel):
    customer_name: str
    sph_right: float
    cyl_right: float
    axis_right: int
    sph_left: float
    cyl_left: float
    axis_left: int
    lens_type: str
    lens_index: float
    coating: str
    frame: str
    store_location: str

class StatusUpdate(BaseModel):
    status: str
    delay_reason: Optional[str] = None
    changed_by: str = "Operator"

class InventoryUpdate(BaseModel):
    lens_type: str
    lens_index: float
    coating: str
    sph: float
    cyl: float
    quantity: int

class InventoryCheck(BaseModel):
    lens_type: str
    lens_index: float
    coating: str
    sph_right: float
    cyl_right: float
    sph_left: float
    cyl_left: float

# HTML UI Routes
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html")

@app.get("/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request):
    return templates.TemplateResponse(request=request, name="inventory.html")

@app.get("/order-intake", response_class=HTMLResponse)
async def order_intake_page(request: Request):
    return templates.TemplateResponse(request=request, name="order_intake.html")

# API Routes

@app.get("/api/orders")
def get_orders(
    status: Optional[str] = Query(None),
    lens_type: Optional[str] = Query(None),
    store_location: Optional[str] = Query(None),
    breach_risk: Optional[str] = Query(None)
):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    query = "SELECT * FROM orders WHERE status != 'Delivered'"
    params = []
    
    if status:
        query += " AND status = ?"
        params.append(status)
    if lens_type:
        query += " AND lens_type = ?"
        params.append(lens_type)
    if store_location:
        query += " AND store_location = ?"
        params.append(store_location)
    if breach_risk:
        query += " AND predicted_breach_risk = ?"
        params.append(breach_risk)
        
    query += " ORDER BY created_at DESC"
    cursor.execute(query, params)
    active_orders = [dict(row) for row in cursor.fetchall()]
    
    # Also get Delivered orders for completed history reference in UI (limit 15)
    cursor.execute("SELECT * FROM orders WHERE status = 'Delivered' ORDER BY updated_at DESC LIMIT 15")
    delivered_orders = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return {
        "active": active_orders,
        "delivered": delivered_orders
    }

@app.get("/api/orders/{order_id}")
def get_order_detail(order_id: int):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order_row = cursor.fetchone()
    if not order_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
        
    order = dict(order_row)
    
    # Fetch audit logs
    cursor.execute("SELECT * FROM order_history WHERE order_id = ? ORDER BY changed_at DESC", (order_id,))
    history = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Calculate live elapsed and SLA remaining
    created_at = datetime.fromisoformat(order['created_at'])
    elapsed_hours = (datetime.now() - created_at).total_seconds() / 3600.0
    remaining_sla = order['sla_hours'] - elapsed_hours
    
    return {
        "order": order,
        "history": history,
        "elapsed_hours": round(elapsed_hours, 1),
        "remaining_sla_hours": round(remaining_sla, 1)
    }

@app.post("/api/orders")
def create_order(order_data: OrderCreate):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # Check inventory
    is_in_stock, rqty, lqty = check_order_inventory(
        cursor, order_data.lens_type, order_data.lens_index, order_data.coating,
        order_data.sph_right, order_data.cyl_right, order_data.sph_left, order_data.cyl_left
    )
    is_in_stock_val = 1 if is_in_stock else 0
    
    # Deduct stock if available
    if is_in_stock:
        cursor.execute("""
        UPDATE inventory SET quantity = quantity - 1 
        WHERE lens_type = ? AND lens_index = ? AND coating = ? AND sph = ? AND cyl = ?
        """, (order_data.lens_type, order_data.lens_index, order_data.coating, order_data.sph_right, order_data.cyl_right))
        cursor.execute("""
        UPDATE inventory SET quantity = quantity - 1 
        WHERE lens_type = ? AND lens_index = ? AND coating = ? AND sph = ? AND cyl = ?
        """, (order_data.lens_type, order_data.lens_index, order_data.coating, order_data.sph_left, order_data.cyl_left))
    
    # Assign order number
    cursor.execute("SELECT COUNT(*) FROM orders")
    order_count = cursor.fetchone()[0]
    order_number = f"ORD-2026-{1000 + order_count + 1:04d}"
    
    # Set SLA based on lens type
    sla_map = {'Single Vision': 72, 'Bifocal': 96, 'Progressive': 120}
    sla_hours = sla_map.get(order_data.lens_type, 72)
    
    now_str = datetime.now().isoformat()
    
    # Create preliminary order dict for AI analysis
    temp_order = {
        "order_number": order_number,
        "customer_name": order_data.customer_name,
        "sph_right": order_data.sph_right,
        "cyl_right": order_data.cyl_right,
        "axis_right": order_data.axis_right,
        "sph_left": order_data.sph_left,
        "cyl_left": order_data.cyl_left,
        "axis_left": order_data.axis_left,
        "lens_type": order_data.lens_type,
        "lens_index": order_data.lens_index,
        "coating": order_data.coating,
        "frame": order_data.frame,
        "store_location": order_data.store_location,
        "status": "Placed",
        "is_in_stock": is_in_stock_val,
        "qc_fail_count": 0,
        "sla_hours": sla_hours,
        "created_at": now_str
    }
    
    # Run AI predictions
    ai_predictions = get_ai_predictions(temp_order, [])
    
    cursor.execute("""
    INSERT INTO orders (
        order_number, customer_name, sph_right, cyl_right, axis_right, sph_left, cyl_left, axis_left,
        lens_type, lens_index, coating, frame, store_location, status, is_in_stock, qc_fail_count,
        sla_hours, delay_reason, predicted_completion_hours, predicted_breach_risk, ai_analysis,
        action_recommendation, alert_email_template, alert_whatsapp_template, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Placed', ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order_number, order_data.customer_name, order_data.sph_right, order_data.cyl_right, order_data.axis_right,
        order_data.sph_left, order_data.cyl_left, order_data.axis_left, order_data.lens_type, order_data.lens_index,
        order_data.coating, order_data.frame, order_data.store_location, is_in_stock_val, sla_hours,
        ai_predictions['predicted_completion_hours'], ai_predictions['breach_risk'], ai_predictions['ai_analysis'],
        ai_predictions['action_recommendation'], ai_predictions['alert_email_template'], ai_predictions['alert_whatsapp_template'],
        now_str, now_str
    ))
    
    order_id = cursor.lastrowid
    
    # Write history log
    cursor.execute("""
    INSERT INTO order_history (order_id, from_status, to_status, changed_at, changed_by, reason)
    VALUES (?, 'Intake', 'Placed', ?, 'system', 'Order registered successfully')
    """, (order_id, now_str))
    
    conn.commit()
    conn.close()
    
    return {"status": "success", "order_id": order_id, "order_number": order_number, "is_in_stock": is_in_stock}

@app.post("/api/orders/{order_id}/status")
def update_order_status(order_id: int, data: StatusUpdate):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order_row = cursor.fetchone()
    if not order_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
        
    order = dict(order_row)
    old_status = order['status']
    new_status = data.status
    
    # Check for QC Failure loopback (transitioning status from QC to Surfacing or Coating)
    qc_failed = 0
    qc_fail_count = order['qc_fail_count']
    if old_status == 'QC' and new_status in ['Surfacing', 'Coating']:
        qc_failed = 1
        qc_fail_count += 1
        
    now_str = datetime.now().isoformat()
    
    # Update order state in database
    cursor.execute("""
    UPDATE orders 
    SET status = ?, qc_fail_count = ?, delay_reason = ?, updated_at = ?
    WHERE id = ?
    """, (new_status, qc_fail_count, data.delay_reason, now_str, order_id))
    
    # Write event history log
    log_reason = data.delay_reason or ("QC Failure - Loopback to processing" if qc_failed else "Status advancement")
    cursor.execute("""
    INSERT INTO order_history (order_id, from_status, to_status, changed_at, changed_by, reason)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (order_id, old_status, new_status, now_str, data.changed_by, log_reason))
    
    # Fetch updated history for AI engine
    cursor.execute("SELECT * FROM order_history WHERE order_id = ? ORDER BY changed_at DESC", (order_id,))
    history_logs = [dict(h) for h in cursor.fetchall()]
    
    # Refresh order details object for AI prediction input
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    updated_order = dict(cursor.fetchone())
    
    # Re-calculate TAT prediction
    ai_predictions = get_ai_predictions(updated_order, history_logs)
    
    # Save predictions
    cursor.execute("""
    UPDATE orders 
    SET predicted_completion_hours = ?, predicted_breach_risk = ?, ai_analysis = ?,
        action_recommendation = ?, alert_email_template = ?, alert_whatsapp_template = ?
    WHERE id = ?
    """, (
        ai_predictions['predicted_completion_hours'], ai_predictions['breach_risk'], ai_predictions['ai_analysis'],
        ai_predictions['action_recommendation'], ai_predictions['alert_email_template'], ai_predictions['alert_whatsapp_template'],
        order_id
    ))
    
    conn.commit()
    conn.close()
    
    return {"status": "success", "new_status": new_status, "qc_fail_count": qc_fail_count}

@app.post("/api/orders/{order_id}/ai-refresh")
def force_ai_refresh(order_id: int):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order_row = cursor.fetchone()
    if not order_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
        
    order = dict(order_row)
    
    cursor.execute("SELECT * FROM order_history WHERE order_id = ? ORDER BY changed_at DESC", (order_id,))
    history_logs = [dict(h) for h in cursor.fetchall()]
    
    ai_predictions = get_ai_predictions(order, history_logs)
    
    cursor.execute("""
    UPDATE orders 
    SET predicted_completion_hours = ?, predicted_breach_risk = ?, ai_analysis = ?,
        action_recommendation = ?, alert_email_template = ?, alert_whatsapp_template = ?
    WHERE id = ?
    """, (
        ai_predictions['predicted_completion_hours'], ai_predictions['breach_risk'], ai_predictions['ai_analysis'],
        ai_predictions['action_recommendation'], ai_predictions['alert_email_template'], ai_predictions['alert_whatsapp_template'],
        order_id
    ))
    
    conn.commit()
    conn.close()
    return {"status": "success", "predictions": ai_predictions}

@app.post("/api/inventory/check")
def api_check_inventory(check: InventoryCheck):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    is_in_stock, rqty, lqty = check_order_inventory(
        cursor, check.lens_type, check.lens_index, check.coating,
        check.sph_right, check.cyl_right, check.sph_left, check.cyl_left
    )
    
    conn.close()
    return {
        "is_in_stock": is_in_stock,
        "right_lens_quantity": rqty,
        "left_lens_quantity": lqty
    }

@app.post("/api/inventory/update")
def api_update_inventory(data: InventoryUpdate):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # Upsert inventory quantity
    cursor.execute("""
    INSERT INTO inventory (lens_type, lens_index, coating, sph, cyl, quantity)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(lens_type, lens_index, coating, sph, cyl) 
    DO UPDATE SET quantity = ?
    """, (data.lens_type, data.lens_index, data.coating, data.sph, data.cyl, data.quantity, data.quantity))
    
    conn.commit()
    conn.close()
    return {"status": "success", "message": f"Updated stock for lens type {data.lens_type} ({data.sph}, {data.cyl}) to {data.quantity}"}

@app.get("/api/inventory/list")
def api_list_inventory(
    lens_type: Optional[str] = Query(None),
    coating: Optional[str] = Query(None),
    sph: Optional[float] = Query(None),
    cyl: Optional[float] = Query(None)
):
    conn = get_db_conn()
    cursor = conn.cursor()
    
    query = "SELECT * FROM inventory WHERE quantity > 0"
    params = []
    
    if lens_type:
        query += " AND lens_type = ?"
        params.append(lens_type)
    if coating:
        query += " AND coating = ?"
        params.append(coating)
    if sph is not None:
        query += " AND sph = ?"
        params.append(sph)
    if cyl is not None:
        query += " AND cyl = ?"
        params.append(cyl)
        
    query += " ORDER BY lens_type, lens_index, coating, sph, cyl LIMIT 200"
    cursor.execute(query, params)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    return {"inventory": items}

@app.get("/api/metrics")
def get_dashboard_metrics():
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # 1. Active orders
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status != 'Delivered'")
    active_count = cursor.fetchone()[0]
    
    # 2. Predicted SLA breaches
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status != 'Delivered' AND predicted_breach_risk = 'High'")
    breach_count = cursor.fetchone()[0]
    
    # 3. Stock availability rate (percentage of active orders that are in stock)
    cursor.execute("SELECT COUNT(*), SUM(is_in_stock) FROM orders WHERE status != 'Delivered'")
    active_row = cursor.fetchone()
    total_active = active_row[0] or 0
    in_stock_active = active_row[1] or 0
    stock_rate = round((in_stock_active / total_active * 100), 1) if total_active > 0 else 100.0
    
    # 4. Average historical processing time (Delivered orders)
    # We calculate actual duration = (updated_at - created_at)
    cursor.execute("""
    SELECT created_at, updated_at FROM orders 
    WHERE status = 'Delivered' AND order_number NOT LIKE 'ORD-HIST-%'
    """)
    rows = cursor.fetchall()
    
    # Fallback to historical seeds if not enough user orders yet
    if not rows:
        cursor.execute("SELECT created_at, updated_at FROM orders WHERE status = 'Delivered'")
        rows = cursor.fetchall()
        
    durations = []
    for r in rows:
        c_at = datetime.fromisoformat(r['created_at'])
        u_at = datetime.fromisoformat(r['updated_at'])
        durations.append((u_at - c_at).total_seconds() / 3600.0)
        
    avg_tat = round(sum(durations) / len(durations), 1) if durations else 0.0
    
    conn.close()
    return {
        "active_orders": active_count,
        "predicted_breaches": breach_count,
        "stock_rate_percent": stock_rate,
        "average_tat_hours": avg_tat
    }

if __name__ == "__main__":
    import uvicorn
    # Trigger database seed logic updates on load
    print("Pre-loading database predictions...")
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM orders WHERE status != 'Delivered' AND predicted_completion_hours IS NULL")
    uncalculated = cursor.fetchall()
    if uncalculated:
        print(f"Calculating TAT predictions for {len(uncalculated)} orders...")
        for row in uncalculated:
            oid = row['id']
            # Fetch history
            cursor.execute("SELECT * FROM order_history WHERE order_id = ? ORDER BY changed_at DESC", (oid,))
            history_logs = [dict(h) for h in cursor.fetchall()]
            # Fetch order
            cursor.execute("SELECT * FROM orders WHERE id = ?", (oid,))
            ord_data = dict(cursor.fetchone())
            ai_predictions = get_ai_predictions(ord_data, history_logs)
            cursor.execute("""
            UPDATE orders 
            SET predicted_completion_hours = ?, predicted_breach_risk = ?, ai_analysis = ?,
                action_recommendation = ?, alert_email_template = ?, alert_whatsapp_template = ?
            WHERE id = ?
            """, (
                ai_predictions['predicted_completion_hours'], ai_predictions['breach_risk'], ai_predictions['ai_analysis'],
                ai_predictions['action_recommendation'], ai_predictions['alert_email_template'], ai_predictions['alert_whatsapp_template'],
                oid
            ))
        conn.commit()
    conn.close()
    
    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, reload=True)

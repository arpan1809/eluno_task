import sqlite3
import sys
import os
from datetime import datetime

# Adjust path to import from backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.db import get_db_conn, init_db, check_order_inventory
from backend.ai_engine import calculate_rule_based_tat, get_ai_predictions

def test_inventory_check():
    print("\n--- Test 1: Lens Inventory Match Lookup ---")
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # Check a standard power (Single Vision, 1.50 Index, Anti-Glare, SPH 0.00, CYL 0.00)
    # This should be in stock (quantity > 0)
    is_in_stock, rqty, lqty = check_order_inventory(
        cursor, "Single Vision", 1.50, "Anti-Glare", 0.00, 0.00, 0.00, 0.00
    )
    print(f"Standard Lens Stock Check: in_stock={is_in_stock}, R_qty={rqty}, L_qty={lqty}")
    assert is_in_stock is True, "Standard power should be in stock"
    
    # Check a custom power (Progressive lens SPH 0.00, CYL -0.50, Anti-Glare, 1.50)
    # This should be out of stock (quantity = 0 or missing)
    is_in_stock, rqty, lqty = check_order_inventory(
        cursor, "Progressive", 1.50, "Anti-Glare", 4.00, -2.00, 4.00, -2.00
    )
    print(f"Complex Progressive Lens Stock Check: in_stock={is_in_stock}, R_qty={rqty}, L_qty={lqty}")
    assert is_in_stock is False, "Complex Progressive should be out of stock"
    
    conn.close()
    print("Test 1 Passed.")

def test_order_creation_and_stock_allocation():
    print("\n--- Test 2: Order Creation and Stock Allocation ---")
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # Query stock before allocation
    cursor.execute("""
    SELECT quantity FROM inventory 
    WHERE lens_type = 'Single Vision' AND lens_index = 1.50 AND coating = 'Anti-Glare' AND sph = 1.00 AND cyl = 0.00
    """)
    before_qty = cursor.fetchone()[0]
    print(f"Inventory stock before allocation: {before_qty}")
    
    # Simulate order placement
    from fastapi.testclient import TestClient
    from backend.app import app
    
    client = TestClient(app)
    
    response = client.post("/api/orders", json={
        "customer_name": "Test Customer",
        "sph_right": 1.00,
        "cyl_right": 0.00,
        "axis_right": 90,
        "sph_left": 1.00,
        "cyl_left": 0.00,
        "axis_left": 90,
        "lens_type": "Single Vision",
        "lens_index": 1.50,
        "coating": "Anti-Glare",
        "frame": "Test Aviator Frame",
        "store_location": "Uptown"
    })
    
    assert response.status_code == 200, "Order registration endpoint failed"
    res_data = response.json()
    print(f"Order created successfully: Number={res_data['order_number']}, is_in_stock={res_data['is_in_stock']}")
    
    # Query stock after allocation
    cursor.execute("""
    SELECT quantity FROM inventory 
    WHERE lens_type = 'Single Vision' AND lens_index = 1.50 AND coating = 'Anti-Glare' AND sph = 1.00 AND cyl = 0.00
    """)
    after_qty = cursor.fetchone()[0]
    print(f"Inventory stock after allocation: {after_qty}")
    assert before_qty - after_qty == 2, "Stock quantity did not decrement correctly (should decrement by 2 for bilateral matching prescription)"
    
    conn.close()
    print("Test 2 Passed.")

def test_status_change_and_qc_loop():
    print("\n--- Test 3: Status Transition and QC Loopback ---")
    from fastapi.testclient import TestClient
    from backend.app import app
    
    client = TestClient(app)
    
    # 1. Create a dummy order
    response = client.post("/api/orders", json={
        "customer_name": "QC Test Case",
        "sph_right": 0.00,
        "cyl_right": 0.00,
        "axis_right": 0,
        "sph_left": 0.00,
        "cyl_left": 0.00,
        "axis_left": 0,
        "lens_type": "Single Vision",
        "lens_index": 1.50,
        "coating": "Anti-Glare",
        "frame": "Classic Rimless",
        "store_location": "Downtown"
    })
    order_id = response.json()['order_id']
    
    # 2. Advance to QC stage
    res_adv = client.post(f"/api/orders/{order_id}/status", json={
        "status": "QC",
        "delay_reason": "Moving order to QC stage",
        "changed_by": "Operator"
    })
    assert res_adv.status_code == 200
    print("Advanced status to: QC")
    
    # 3. Simulate QC Failure (status goes back to Surfacing)
    res_fail = client.post(f"/api/orders/{order_id}/status", json={
        "status": "Surfacing",
        "delay_reason": "Failed cosmetic scratch inspection at QC stage.",
        "changed_by": "Glazing Inspector"
    })
    assert res_fail.status_code == 200
    fail_data = res_fail.json()
    print(f"QC fail processed. Returned status={fail_data['new_status']}, qc_fail_count={fail_data['qc_fail_count']}")
    
    assert fail_data['qc_fail_count'] == 1, "QC failure loop should increment count"
    assert fail_data['new_status'] == "Surfacing", "Status should roll back to Surfacing"
    
    # 4. Check that audit history contains this change
    res_detail = client.get(f"/api/orders/{order_id}")
    history = res_detail.json()['history']
    print(f"Audit log length: {len(history)} items")
    assert len(history) >= 3, "Audit logs should record placement, advancement, and failure loopback"
    
    # Verify delay reason is saved
    assert res_detail.json()['order']['delay_reason'] == "Failed cosmetic scratch inspection at QC stage."
    
    print("Test 3 Passed.")

def test_ai_prediction_fallback():
    print("\n--- Test 4: AI TAT & Breach Fallback Predictor ---")
    
    # Construct a simulated complex order
    now_str = datetime.now().isoformat()
    order = {
        "order_number": "ORD-TEST-999",
        "customer_name": "Richie Rich",
        "sph_right": 5.00,  # Complex (> 4.00)
        "cyl_right": -2.50, # Complex (> 2.00)
        "axis_right": 90,
        "sph_left": 5.00,
        "cyl_left": -2.50,
        "axis_left": 90,
        "lens_type": "Progressive",
        "lens_index": 1.67,
        "coating": "Blue Cut",
        "frame": "Designer Titanium",
        "store_location": "Downtown",
        "status": "Surfacing",
        "is_in_stock": 0,    # Out of stock
        "qc_fail_count": 1,  # QC Failure loopback delay
        "sla_hours": 120,    # Progressive SLA is 120h
        "created_at": now_str
    }
    
    # Call predictor (will run fallback since GROQ_API_KEY environment variable is not defined during test script runtime)
    predictions = get_ai_predictions(order, [])
    
    print(f"AI Output: predicted_completion={predictions['predicted_completion_hours']} hours remaining")
    print(f"AI Output: breach_risk={predictions['breach_risk']}")
    print(f"AI Output: analysis={predictions['ai_analysis']}")
    print(f"AI Output: recommendation={predictions['action_recommendation']}")
    
    assert predictions['breach_risk'] in ['Low', 'Medium', 'High']
    assert len(predictions['alert_email_template']) > 0
    assert len(predictions['alert_whatsapp_template']) > 0
    
    print("Test 4 Passed.")

if __name__ == "__main__":
    init_db()
    # Install fastapi test client dependency locally to enable testing
    # FastAPI test client uses standard httpx which is already installed in venv!
    try:
        test_inventory_check()
        test_order_creation_and_stock_allocation()
        test_status_change_and_qc_loop()
        test_ai_prediction_fallback()
        print("\n=== ALL TESTS COMPLETED SUCCESSFULLY! SYSTEM LOGIC OPERATIONAL. ===")
    except Exception as e:
        print(f"\n[ERROR] TEST RUN ENCOUNTERED FAILURE: {e}")
        sys.exit(1)

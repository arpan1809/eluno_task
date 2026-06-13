import sqlite3
import os
import random
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db")

def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_conn()
    cursor = conn.cursor()
    
    # 1. Orders Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT UNIQUE,
        customer_name TEXT NOT NULL,
        sph_right REAL NOT NULL,
        cyl_right REAL NOT NULL,
        axis_right INTEGER NOT NULL,
        sph_left REAL NOT NULL,
        cyl_left REAL NOT NULL,
        axis_left INTEGER NOT NULL,
        lens_type TEXT NOT NULL,
        lens_index REAL NOT NULL,
        coating TEXT NOT NULL,
        frame TEXT NOT NULL,
        store_location TEXT NOT NULL,
        status TEXT NOT NULL,
        is_in_stock INTEGER NOT NULL,
        qc_fail_count INTEGER DEFAULT 0,
        sla_hours INTEGER NOT NULL,
        delay_reason TEXT,
        predicted_completion_hours REAL,
        predicted_breach_risk TEXT,
        ai_analysis TEXT,
        action_recommendation TEXT,
        alert_email_template TEXT,
        alert_whatsapp_template TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    
    # 2. Inventory Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lens_type TEXT NOT NULL,
        lens_index REAL NOT NULL,
        coating TEXT NOT NULL,
        sph REAL NOT NULL,
        cyl REAL NOT NULL,
        quantity INTEGER NOT NULL,
        UNIQUE(lens_type, lens_index, coating, sph, cyl)
    )
    """)
    
    # 3. Order History Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS order_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        from_status TEXT NOT NULL,
        to_status TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        changed_by TEXT NOT NULL,
        reason TEXT,
        FOREIGN KEY (order_id) REFERENCES orders (id)
    )
    """)
    
    conn.commit()
    
    # Seed inventory if empty
    cursor.execute("SELECT COUNT(*) FROM inventory")
    if cursor.fetchone()[0] == 0:
        seed_inventory(cursor)
        conn.commit()

    # Seed orders if empty
    cursor.execute("SELECT COUNT(*) FROM orders")
    if cursor.fetchone()[0] == 0:
        seed_orders(cursor)
        conn.commit()
        
    conn.close()

def seed_inventory(cursor):
    """Seed inventory with common powers. SPH -4.00 to +4.00, CYL -2.00 to 0.00"""
    print("Seeding inventory stock...")
    
    # Lens Types: Single Vision, Bifocal, Progressive
    # Indexes: 1.50, 1.60, 1.67, 1.74
    # Coatings: Anti-Glare, Blue Cut, Photochromic, None
    
    # Standard values
    sph_values = [round(x * 0.25, 2) for x in range(-16, 17)] # -4.00 to +4.00
    cyl_values = [round(x * 0.25, 2) for x in range(-8, 1)]   # -2.00 to 0.00
    
    # Let's seed single vision stock
    # 1.50 and 1.60 index single vision lenses are highly stocked in house
    coatings = ['Anti-Glare', 'Blue Cut', 'Photochromic', 'None']
    
    inventory_items = []
    
    # Seed Single Vision, 1.50 and 1.60
    for idx in [1.50, 1.60]:
        for coating in coatings:
            for sph in sph_values:
                for cyl in cyl_values:
                    # High quantity for common, low for extremes
                    if abs(sph) <= 2.0 and abs(cyl) <= 1.0:
                        qty = random.randint(12, 25)
                    else:
                        qty = random.randint(3, 10)
                    inventory_items.append(('Single Vision', idx, coating, sph, cyl, qty))
                    
    # Seed Single Vision, 1.67 (fewer stock items)
    for coating in ['Anti-Glare', 'Blue Cut']:
        for sph in sph_values:
            for cyl in cyl_values:
                qty = random.randint(1, 4) if (abs(sph) <= 2.0) else 0
                inventory_items.append(('Single Vision', 1.67, coating, sph, cyl, qty))
                
    # Progressive and Bifocal lenses are mostly out of stock (made-to-order)
    # We will seed only a tiny subset in house, rest will be 0 quantity
    # (Bifocal 1.50 Anti-Glare has some stock)
    for sph in [0.0, -1.0, +1.0]:
        for cyl in [0.0, -0.50]:
            inventory_items.append(('Bifocal', 1.50, 'Anti-Glare', sph, cyl, 2))
            
    # Insert in batches
    cursor.executemany("""
    INSERT OR REPLACE INTO inventory (lens_type, lens_index, coating, sph, cyl, quantity)
    VALUES (?, ?, ?, ?, ?, ?)
    """, inventory_items)

def check_lens_stock(cursor, lens_type, lens_index, coating, sph, cyl):
    """Check stock for a single lens."""
    cursor.execute("""
    SELECT quantity FROM inventory 
    WHERE lens_type = ? AND lens_index = ? AND coating = ? AND sph = ? AND cyl = ?
    """, (lens_type, lens_index, coating, sph, cyl))
    row = cursor.fetchone()
    if row and row[0] > 0:
        return True, row[0]
    return False, 0

def check_order_inventory(cursor, lens_type, lens_index, coating, sph_right, cyl_right, sph_left, cyl_left):
    """Check if both lenses are in stock."""
    right_ok, r_qty = check_lens_stock(cursor, lens_type, lens_index, coating, sph_right, cyl_right)
    left_ok, l_qty = check_lens_stock(cursor, lens_type, lens_index, coating, sph_left, cyl_left)
    return (right_ok and left_ok), r_qty, l_qty

def seed_orders(cursor):
    """Seed historical and active orders."""
    print("Seeding historical and active orders...")
    
    locations = ['Downtown', 'Uptown', 'Westside', 'Eastside']
    lens_types = ['Single Vision', 'Bifocal', 'Progressive']
    indexes = {
        'Single Vision': [1.50, 1.60, 1.67, 1.74],
        'Bifocal': [1.50, 1.60],
        'Progressive': [1.50, 1.60, 1.67]
    }
    coatings = ['Anti-Glare', 'Blue Cut', 'Photochromic', 'None']
    frames = ['Titanium Round', 'Acetate Square', 'Classic Aviator', 'Rimless Rectangle', 'Cat-Eye Tortoise']
    
    # SLAs
    sla_map = {
        'Single Vision': 72, # 3 days
        'Bifocal': 96,        # 4 days
        'Progressive': 120   # 5 days
    }
    
    # 1. Historical Orders (100 completed orders over the last 30 days)
    # This allows baseline statistical calculation for stages
    now = datetime.now()
    historical_orders = []
    
    for i in range(1, 101):
        order_num = f"ORD-HIST-{2000 + i}"
        cust_name = f"Past Customer {i}"
        l_type = random.choice(lens_types)
        idx = random.choice(indexes[l_type])
        coat = random.choice(coatings)
        frame = random.choice(frames)
        loc = random.choice(locations)
        
        # Prescription
        sph_r = round(random.choice([x * 0.25 for x in range(-12, 13)]), 2)
        cyl_r = round(random.choice([x * 0.25 for x in range(-6, 1)]), 2)
        axis_r = random.choice([0, 90, 180])
        sph_l = round(random.choice([x * 0.25 for x in range(-12, 13)]), 2)
        cyl_l = round(random.choice([x * 0.25 for x in range(-6, 1)]), 2)
        axis_l = random.choice([0, 90, 180])
        
        # Inventory status at intake
        # For historical seed, we simulate stock status
        is_in_stock = 1 if (l_type == 'Single Vision' and idx in [1.50, 1.60] and abs(sph_r) <= 4.0 and abs(cyl_r) <= 2.0) else 0
        
        # QC Failures
        qc_fails = 0
        if random.random() < 0.12: # 12% failure rate in QC historically
            qc_fails = 1
            if random.random() < 0.2: # recursive fail
                qc_fails = 2
                
        # SLA
        sla = sla_map[l_type]
        
        # Calculate actual completion duration (hours)
        # Base: SV = 36h, Bifocal = 54h, Progressive = 72h
        base_h = 30 if l_type == 'Single Vision' else (50 if l_type == 'Bifocal' else 68)
        
        # Add-ons
        stock_delay = 0 if is_in_stock == 1 else random.randint(18, 30) # Out of stock delays surfacing
        complexity_delay = random.randint(4, 12) if (abs(sph_r) > 4.0 or abs(cyl_r) > 2.0) else 0
        qc_delay = qc_fails * random.randint(20, 32)
        random_delay = random.randint(-4, 8)
        
        actual_hours = base_h + stock_delay + complexity_delay + qc_delay + random_delay
        
        # Set timestamps
        order_days_ago = random.randint(5, 30)
        created_dt = now - timedelta(days=order_days_ago, hours=random.randint(0, 23))
        completed_dt = created_dt + timedelta(hours=actual_hours)
        
        # Determine breach
        breach = "Yes" if actual_hours > sla else "No"
        
        # Insert completed order
        cursor.execute("""
        INSERT INTO orders (
            order_number, customer_name, sph_right, cyl_right, axis_right, sph_left, cyl_left, axis_left,
            lens_type, lens_index, coating, frame, store_location, status, is_in_stock, qc_fail_count,
            sla_hours, delay_reason, predicted_completion_hours, predicted_breach_risk, ai_analysis,
            action_recommendation, alert_email_template, alert_whatsapp_template, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Delivered', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_num, cust_name, sph_r, cyl_r, axis_r, sph_l, cyl_l, axis_l,
            l_type, idx, coat, frame, loc, is_in_stock, qc_fails, sla,
            "Delivered successfully." if breach == "No" else "Delayed due to surfacing backlog & QC loop.",
            0.0, "Low", "Delivered.", "None", "", "",
            created_dt.isoformat(), completed_dt.isoformat()
        ))
        
    # 2. Active Orders (~60 active orders distributed across current stages)
    # Stages: Placed, Lab Intake, Surfacing, Coating, Glazing, QC, Dispatched
    stages = ['Placed', 'Lab Intake', 'Surfacing', 'Coating', 'Glazing', 'QC', 'Dispatched']
    
    first_names = ["Sophia", "Jackson", "Olivia", "Liam", "Emma", "Noah", "Ava", "Lucas", "Isabella", "Oliver", "Mia", "Ethan", "Amelia", "Aiden", "Harper", "Elijah"]
    last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson"]
    
    for i in range(1, 61):
        order_num = f"ORD-2026-{1000 + i:04d}"
        cust_name = f"{random.choice(first_names)} {random.choice(last_names)}"
        l_type = random.choice(lens_types)
        idx = random.choice(indexes[l_type])
        coat = random.choice(coatings)
        frame = random.choice(frames)
        loc = random.choice(locations)
        
        # Prescription
        sph_r = round(random.choice([x * 0.25 for x in range(-12, 13)]), 2)
        cyl_r = round(random.choice([x * 0.25 for x in range(-6, 1)]), 2)
        axis_r = random.choice([0, 90, 180])
        sph_l = round(random.choice([x * 0.25 for x in range(-12, 13)]), 2)
        cyl_l = round(random.choice([x * 0.25 for x in range(-6, 1)]), 2)
        axis_l = random.choice([0, 90, 180])
        
        # Determine stock level
        # Use database checker to see if seeded stock exists
        is_in_stock, rqty, lqty = check_order_inventory(cursor, l_type, idx, coat, sph_r, cyl_r, sph_l, cyl_l)
        is_in_stock_int = 1 if is_in_stock else 0
        
        # Deduct stock if available
        if is_in_stock:
            cursor.execute("""
            UPDATE inventory SET quantity = quantity - 1 
            WHERE lens_type = ? AND lens_index = ? AND coating = ? AND sph = ? AND cyl = ?
            """, (l_type, idx, coat, sph_r, cyl_r))
            cursor.execute("""
            UPDATE inventory SET quantity = quantity - 1 
            WHERE lens_type = ? AND lens_index = ? AND coating = ? AND sph = ? AND cyl = ?
            """, (l_type, idx, coat, sph_l, cyl_l))
            
        # Status placement distribution
        # We simulate aging based on the stage:
        # Placed: aged 0-3 hours
        # Lab Intake: aged 2-6 hours
        # Surfacing: aged 6-24 hours
        # Coating: aged 12-36 hours
        # Glazing: aged 24-48 hours
        # QC: aged 24-60 hours
        # Dispatched: aged 36-72 hours
        status = random.choice(stages)
        if status == 'Placed':
            age_hours = random.randint(0, 3)
        elif status == 'Lab Intake':
            age_hours = random.randint(2, 6)
        elif status == 'Surfacing':
            age_hours = random.randint(6, 24)
        elif status == 'Coating':
            age_hours = random.randint(12, 36)
        elif status == 'Glazing':
            age_hours = random.randint(24, 48)
        elif status == 'QC':
            age_hours = random.randint(24, 60)
        else: # Dispatched
            age_hours = random.randint(36, 72)
            
        created_dt = now - timedelta(hours=age_hours)
        
        # Failures
        qc_fails = 0
        delay_r = None
        if status in ['Surfacing', 'Coating', 'Glazing', 'QC'] and random.random() < 0.15:
            qc_fails = 1
            delay_r = "Failed cosmetic scratch inspection at QC. Re-routed to Surfacing."
            
        # SLA
        sla = sla_map[l_type]
        
        # Insert active order
        cursor.execute("""
        INSERT INTO orders (
            order_number, customer_name, sph_right, cyl_right, axis_right, sph_left, cyl_left, axis_left,
            lens_type, lens_index, coating, frame, store_location, status, is_in_stock, qc_fail_count,
            sla_hours, delay_reason, predicted_completion_hours, predicted_breach_risk, ai_analysis,
            action_recommendation, alert_email_template, alert_whatsapp_template, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
        """, (
            order_num, cust_name, sph_r, cyl_r, axis_r, sph_l, cyl_l, axis_l,
            l_type, idx, coat, frame, loc, status, is_in_stock_int, qc_fails,
            sla, delay_r, created_dt.isoformat(), created_dt.isoformat()
        ))
        
        order_id = cursor.lastrowid
        
        # Seed order history for active order
        # History tracks order movement
        histories = [('Placed', created_dt)]
        if age_hours > 3 and status != 'Placed':
            histories.append(('Lab Intake', created_dt + timedelta(hours=2)))
        if age_hours > 10 and status not in ['Placed', 'Lab Intake']:
            histories.append(('Surfacing', created_dt + timedelta(hours=6)))
        if age_hours > 24 and status not in ['Placed', 'Lab Intake', 'Surfacing']:
            histories.append(('Coating', created_dt + timedelta(hours=18)))
        if age_hours > 36 and status not in ['Placed', 'Lab Intake', 'Surfacing', 'Coating']:
            histories.append(('Glazing', created_dt + timedelta(hours=30)))
        if age_hours > 48 and status not in ['Placed', 'Lab Intake', 'Surfacing', 'Coating', 'Glazing']:
            histories.append(('QC', created_dt + timedelta(hours=42)))
        if age_hours > 60 and status == 'Dispatched':
            histories.append(('Dispatched', created_dt + timedelta(hours=54)))
            
        # Insert audit log lines
        for j in range(len(histories)):
            from_st = 'Intake' if j == 0 else histories[j-1][0]
            to_st = histories[j][0]
            ts = histories[j][1].isoformat()
            cursor.execute("""
            INSERT INTO order_history (order_id, from_status, to_status, changed_at, changed_by, reason)
            VALUES (?, ?, ?, ?, 'system_seeder', ?)
            """, (order_id, from_st, to_st, ts, "Initial seeding status setup"))
            
if __name__ == "__main__":
    init_db()
    print("Database seeded successfully.")

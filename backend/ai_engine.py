import os
import json
import httpx
from datetime import datetime

# Define standard durations (in hours) for each stage by lens type
STAGE_DURATIONS = {
    'Single Vision': {
        'Placed': 1,
        'Lab Intake': 2,
        'Surfacing': 12,
        'Coating': 10,
        'Glazing': 4,
        'QC': 2,
        'Dispatched': 12,
        'Delivered': 0
    },
    'Bifocal': {
        'Placed': 1,
        'Lab Intake': 2,
        'Surfacing': 18,
        'Coating': 12,
        'Glazing': 6,
        'QC': 3,
        'Dispatched': 12,
        'Delivered': 0
    },
    'Progressive': {
        'Placed': 1,
        'Lab Intake': 2,
        'Surfacing': 24,
        'Coating': 16,
        'Glazing': 8,
        'QC': 4,
        'Dispatched': 12,
        'Delivered': 0
    }
}

STAGES_ORDER = ['Placed', 'Lab Intake', 'Surfacing', 'Coating', 'Glazing', 'QC', 'Dispatched', 'Delivered']

def calculate_rule_based_tat(order):
    """
    Computes baseline expected remaining hours, total predicted hours, and breach risk.
    """
    status = order.get('status', 'Placed')
    lens_type = order.get('lens_type', 'Single Vision')
    is_in_stock = order.get('is_in_stock', 1)
    qc_fail_count = order.get('qc_fail_count', 0)
    sph_r = abs(order.get('sph_right', 0.0))
    cyl_r = abs(order.get('cyl_right', 0.0))
    sph_l = abs(order.get('sph_left', 0.0))
    cyl_l = abs(order.get('cyl_left', 0.0))
    
    created_at_str = order.get('created_at')
    created_at = datetime.fromisoformat(created_at_str)
    elapsed_hours = (datetime.now() - created_at).total_seconds() / 3600.0
    
    # Identify which index we are at in production
    try:
        current_idx = STAGES_ORDER.index(status)
    except ValueError:
        current_idx = 0
        
    remaining_stages = STAGES_ORDER[current_idx:-1] # exclude Delivered
    
    # Calculate baseline remaining duration
    type_durations = STAGE_DURATIONS.get(lens_type, STAGE_DURATIONS['Single Vision'])
    remaining_hours = sum(type_durations.get(s, 0) for s in remaining_stages)
    
    # Modifiers
    # 1. Stock status delay: If out of house (needs surfacing/ordering) and surfacing not done yet
    if is_in_stock == 0 and current_idx <= STAGES_ORDER.index('Surfacing'):
        remaining_hours += 24.0 # 24h delay to procure/custom block the lens
        
    # 2. Prescription complexity: SPH > 4 or CYL > 2 adds extra lab processing time
    is_complex = (sph_r > 4.0 or cyl_r > 2.0 or sph_l > 4.0 or cyl_l > 2.0)
    if is_complex and current_idx <= STAGES_ORDER.index('Surfacing'):
        remaining_hours += 8.0 # +8h surfacing complexity
        
    # 3. QC failures: loopback penalty
    if qc_fail_count > 0 and current_idx <= STAGES_ORDER.index('QC'):
        # QC failures push work back to Surfacing/Coating
        remaining_hours += qc_fail_count * 20.0
        
    predicted_total = elapsed_hours + remaining_hours
    sla = order.get('sla_hours', 72)
    
    # Determine risk category
    if status == 'Delivered':
        risk = 'Low'
        remaining_hours = 0.0
        predicted_total = elapsed_hours
    elif predicted_total > sla:
        risk = 'High'
    elif predicted_total > (sla * 0.85):
        risk = 'Medium'
    else:
        risk = 'Low'
        
    return {
        'remaining_hours': round(remaining_hours, 1),
        'predicted_total_hours': round(predicted_total, 1),
        'elapsed_hours': round(elapsed_hours, 1),
        'risk': risk,
        'is_complex': is_complex
    }

def get_ai_predictions(order, history_logs=[]):
    """
    Combines rule-based math and Groq API.
    If GROQ_API_KEY environment variable is defined, queries llama-3.3-70b.
    Otherwise, generates rule-based mock responses.
    """
    rule_results = calculate_rule_based_tat(order)
    
    api_key = os.environ.get("GROQ_API_KEY")
    
    if not api_key:
        # Generate structured fallback output
        return build_fallback_response(order, rule_results, history_logs)
        
    # Build prompt for Groq
    history_summary = "\n".join([
        f"- {log.get('changed_at')}: {log.get('from_status')} -> {log.get('to_status')} (By: {log.get('changed_by')}, Reason: {log.get('reason', 'N/A')})"
        for log in history_logs
    ])
    
    system_prompt = (
        "You are an expert AI Operations Analyst for 'Eluno Eyewear'. Your task is to analyze an active order's parameters, "
        "production stage, and history to output precise turnaround time (TAT) assessments, bottleneck analyses, and communication templates.\n"
        "You MUST return your response ONLY as a JSON object with these exact keys:\n"
        "{\n"
        "  \"predicted_completion_hours\": <float: estimated hours remaining from now to delivery>,\n"
        "  \"breach_risk\": <string: \"Low\", \"Medium\", or \"High\">,\n"
        "  \"ai_analysis\": <string: 2-3 sentence technical explanation of the risk, mentioning specific lens parameters or stages>,\n"
        "  \"action_recommendation\": <string: actionable lab directives to expedite or resolve delay>,\n"
        "  \"alert_email_template\": <string: customer-facing email updates explaining delays or dispatch status>,\n"
        "  \"alert_whatsapp_template\": <string: short team or customer WhatsApp alert (max 150 chars)>\n"
        "}"
    )
    
    user_prompt = f"""
    --- ORDER DETAILS ---
    Order Number: {order['order_number']}
    Customer: {order['customer_name']}
    Store Location: {order['store_location']}
    Lens Type: {order['lens_type']} (Index: {order['lens_index']}, Coating: {order['coating']})
    Prescription:
      OD (Right): SPH {order['sph_right']}, CYL {order['cyl_right']}, AXIS {order['axis_right']}
      OS (Left): SPH {order['sph_left']}, CYL {order['cyl_left']}, AXIS {order['axis_left']}
    Inventory Status: {"In-house stock matches" if order['is_in_stock'] == 1 else "Out-of-stock (Custom procurement/surfacing needed)"}
    QC Failures: {order['qc_fail_count']}
    
    --- FULFILLMENT STATS ---
    Current Status: {order['status']}
    Created At: {order['created_at']}
    SLA Allocation: {order['sla_hours']} hours
    Elapsed Time: {rule_results['elapsed_hours']} hours
    Mathematical Baseline Remaining: {rule_results['remaining_hours']} hours
    Mathematical Baseline Total: {rule_results['predicted_total_hours']} hours
    
    --- ORDER EVENT HISTORY ---
    {history_summary if history_logs else "No events logged yet."}
    
    Please refine the remaining duration, evaluate breach risk, and fill out the JSON schema.
    """
    
    try:
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2
        }
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        with httpx.Client(timeout=15.0) as client:
            response = client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
            ai_message = result['choices'][0]['message']['content']
            parsed_result = json.loads(ai_message)
            
            # Ensure keys exist
            return {
                'predicted_completion_hours': float(parsed_result.get('predicted_completion_hours', rule_results['remaining_hours'])),
                'breach_risk': parsed_result.get('breach_risk', rule_results['risk']),
                'ai_analysis': parsed_result.get('ai_analysis', "SLA monitoring active."),
                'action_recommendation': parsed_result.get('action_recommendation', "Proceed through normal workflow."),
                'alert_email_template': parsed_result.get('alert_email_template', ""),
                'alert_whatsapp_template': parsed_result.get('alert_whatsapp_template', "")
            }
            
    except Exception as e:
        print(f"Groq API call failed: {e}. Falling back to rule-based generation.")
        return build_fallback_response(order, rule_results, history_logs)

def build_fallback_response(order, rule_results, history_logs):
    """
    Generates plausible AI-like text response based on mathematical rules.
    """
    rem_h = rule_results['remaining_hours']
    total_h = rule_results['predicted_total_hours']
    risk = rule_results['risk']
    elapsed = rule_results['elapsed_hours']
    sla = order['sla_hours']
    
    # Formulate custom text based on order factors
    reasons = []
    if order['is_in_stock'] == 0:
        reasons.append("out-of-house lens blanks requiring custom procurement & blocking (+24h)")
    if rule_results['is_complex']:
        reasons.append("complex prescription cylinder/spherical values (+8h lab time)")
    if order['qc_fail_count'] > 0:
        reasons.append(f"QC fail count of {order['qc_fail_count']} requiring lab re-surfacing loop back")
        
    if reasons:
        factor_str = " and ".join(reasons)
        ai_analysis = (
            f"Baseline mathematical prediction shows order is at {risk} breach risk due to {factor_str}. "
            f"The order has elapsed {elapsed:.1f} hours out of the {sla} hours SLA."
        )
    else:
        ai_analysis = (
            f"Fulfillment is proceeding within standard SLA. Order is at {risk} breach risk. "
            f"Standard processing for a {order['lens_type']} lens at stage '{order['status']}'."
        )
        
    # Recommendations
    if risk == 'High':
        recom = "CRITICAL: Assign to senior technician for immediate glazing and surfacing bypass. Mark frame package as priority."
    elif risk == 'Medium':
        recom = "OPTIMIZATION: Monitor Surfacing/Coating transition closely. Expedite to Glazing within the next 4 hours."
    else:
        recom = "MAINTENANCE: Standard queue processing. Ensure routine QC checklist is followed."
        
    # Templates
    status_msg = f"is currently in the {order['status']} stage" if order['status'] != 'Delivered' else "has been delivered"
    
    email_temp = (
        f"Subject: Status Update for your Eluno Eyewear Order {order['order_number']}\n\n"
        f"Dear {order['customer_name']},\n\n"
        f"We are writing to update you on your order. Your premium {order['lens_type']} eyewear "
        f"{status_msg}. Our lab teams are working to ensure maximum precision. "
    )
    if risk == 'High':
        email_temp += "We are currently conducting advanced custom calibration which may cause a slight delay. We appreciate your patience."
    elif order['status'] == 'Dispatched':
        email_temp += "It has been handed to our courier and will arrive shortly!"
    else:
        email_temp += "We expect completion on schedule."
        
    email_temp += "\n\nWarm regards,\nEluno Customer Care"
    
    whatsapp_temp = f"Hi {order['customer_name']}, your Eluno Order {order['order_number']} is at the {order['status']} stage. "
    if risk == 'High':
        whatsapp_temp += "We're taking extra care for precision, delivery might take a bit longer."
    elif order['status'] == 'Dispatched':
        whatsapp_temp += "It's on its way! Track details on our dashboard."
    else:
        whatsapp_temp += "On track for SLA delivery."
        
    return {
        'predicted_completion_hours': rem_h,
        'breach_risk': risk,
        'ai_analysis': ai_analysis,
        'action_recommendation': recom,
        'alert_email_template': email_temp,
        'alert_whatsapp_template': whatsapp_temp[:150]
    }

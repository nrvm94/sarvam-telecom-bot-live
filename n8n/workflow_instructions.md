# Setting Up n8n Escalation Workflow

## Overview
This workflow triggers when the Airtel voice bot detects a complex customer issue.
It creates a support ticket, sends a WhatsApp notification, and logs the escalation back to FastAPI.

---

## Prerequisites
- n8n running at http://localhost:5678 (via Docker: `docker-compose up -d`)
- Mock server running at http://localhost:5000 (`python backend/mock_server.py`)
- FastAPI backend running at http://localhost:8000

---

## Step-by-Step Setup

### Step 1 — Open n8n
1. Start n8n: `docker-compose up -d`
2. Open browser → http://localhost:5678
3. If prompted for account setup, skip or use admin/admin

---

### Step 2 — Create New Workflow
1. Click **"+ New Workflow"** (top right)
2. Click the workflow title at the top and rename it: **"Airtel Support Escalation"**
3. Click **Save** (Ctrl+S)

---

### Step 3 — Add Webhook Trigger Node
1. Click the **"+"** button to add a node
2. Search for **"Webhook"** and click it
3. Configure:
   - **HTTP Method:** POST
   - **Path:** `escalation`
   - **Response Mode:** Last Node
   - **Authentication:** None
4. Click **Save**
5. Note the webhook URL shown: `http://localhost:5678/webhook/escalation`

---

### Step 4 — Add "Set Fields" Node (Extract Data)
1. Click **"+"** after the Webhook node
2. Search for **"Edit Fields"** (or "Set" in older versions)
3. Add these mappings:
   | Field Name    | Value                              |
   |---------------|------------------------------------|
   | call_id       | `{{ $json.call_id }}`              |
   | issue_type    | `{{ $json.issue_type }}`           |
   | user_query    | `{{ $json.user_query }}`           |
   | bot_response  | `{{ $json.bot_response }}`         |
   | timestamp     | `{{ $json.timestamp }}`            |
4. Click **Save**

---

### Step 5 — Add "Create Ticket" HTTP Request Node
1. Click **"+"** after the Set node
2. Search for **"HTTP Request"** and click it
3. Rename it: **"Create Ticket"**
4. Configure:
   - **Method:** POST
   - **URL:** `http://localhost:5000/mock/ticket`
   - **Body Content Type:** JSON
   - **Body:**
     ```json
     {
       "call_id": "{{ $json.call_id }}",
       "issue_type": "{{ $json.issue_type }}",
       "user_query": "{{ $json.user_query }}"
     }
     ```
5. Click **Save**
6. The response will contain `ticket_id` — note this for the next steps

---

### Step 6 — Add "Send WhatsApp" HTTP Request Node
1. Click **"+"** after Create Ticket
2. Add another **HTTP Request** node
3. Rename it: **"Send WhatsApp"**
4. Configure:
   - **Method:** POST
   - **URL:** `https://waba-sandbox.360dialog.io/v1/messages`
   - **Headers:**
     - `D360-API-KEY`: `<your DIALOG_360_API_KEY from .env>`
     - `Content-Type`: `application/json`
   - **Body:**
     ```json
     {
       "messaging_product": "whatsapp",
       "to": "919999999999",
       "type": "text",
       "text": {
         "body": "Your Airtel support ticket {{ $('Create Ticket').item.json.ticket_id }} has been created. Issue: {{ $('Set Fields').item.json.issue_type }}. Our agent will contact you within 2 hours."
       }
     }
     ```
5. Click **Save**

> **Note:** In sandbox mode, WhatsApp messages can only be sent to pre-approved test numbers.
> Replace `919999999999` with your verified sandbox phone number.

---

### Step 7 — Add "Callback to FastAPI" HTTP Request Node
1. Click **"+"** after Send WhatsApp
2. Add another **HTTP Request** node
3. Rename it: **"Callback to FastAPI"**
4. Configure:
   - **Method:** POST
   - **URL:** `http://localhost:8000/n8n/webhook`
   - **Body Content Type:** JSON
   - **Body:**
     ```json
     {
       "call_id": "{{ $('Set Fields').item.json.call_id }}",
       "ticket_id": "{{ $('Create Ticket').item.json.ticket_id }}",
       "status": "escalated"
     }
     ```
5. Click **Save**

---

### Step 8 — Connect All Nodes
Ensure the flow is:
```
[Webhook] → [Set Fields] → [Create Ticket] → [Send WhatsApp] → [Callback to FastAPI]
```
Drag arrows to connect if needed.

---

### Step 9 — Activate the Workflow
1. Click **Save** (Ctrl+S)
2. Toggle the **"Active"** switch in the top right to **ON**
3. The workflow is now live and listening for escalation events

---

### Step 10 — Verify the Webhook URL
1. Click on the Webhook node
2. Confirm the URL matches: `http://localhost:5678/webhook/escalation`
3. This must match the `N8N_WEBHOOK_URL` in your `.env` file

---

### Step 11 — Test the Workflow
Run this in a terminal to simulate an escalation:

```bash
curl -X POST http://localhost:5678/webhook/escalation \
  -H "Content-Type: application/json" \
  -d '{
    "call_id": "call_test123abc",
    "issue_type": "billing_dispute",
    "user_query": "I was charged wrong amount",
    "bot_response": "I understand your concern about the billing. Let me escalate this.",
    "timestamp": "2024-01-15T10:30:00Z"
  }'
```

**Expected result:**
1. n8n workflow executes all 4 nodes
2. Mock server creates ticket TKT-XXXXX
3. WhatsApp notification attempted (sandbox)
4. FastAPI /n8n/webhook called with ticket details
5. Supabase (or mock log) updated with escalation info

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Webhook not receiving | Check n8n is active, workflow is toggled ON |
| Mock ticket fails | Ensure `python backend/mock_server.py` is running on port 5000 |
| FastAPI callback fails | Ensure FastAPI is running on port 8000 |
| n8n can't reach localhost | Use `host.docker.internal` instead of `localhost` on Windows/Mac |

> **Windows Docker Note:** If n8n is running in Docker and cannot reach localhost:8000/5000,
> replace `localhost` with `host.docker.internal` in all HTTP Request node URLs.

# Testing Guide — Integration with OpenClaw

This guide explains how to test that the Facebook Groups Auto-Poster correctly responds to OpenClaw requests and delivers results via webhook callbacks.

## HTTP Flow

The expected request/response cycle is:

### 1. OpenClaw sends POST /post

OpenClaw sends a JSON request with posting parameters:

```http
POST http://localhost:5000/post HTTP/1.1
X-API-Key: your_api_key_here
Content-Type: application/json

{
  "text": "Mi primer anuncio de prueba",
  "image_path": "opcional",
  "accounts": ["elena"],
  "callback_url": "https://openclaw.local/webhook/job/123"
}
```

### 2. Server responds immediately with 202 Accepted

The API server responds with a job ID and accepts the request for background processing:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "status": "accepted",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "accounts": ["elena"],
  "text_preview": "Mi primer anuncio de prueba"
}
```

The job is now queued as "pending" in the database.

### 3. Server processes job in background daemon

The job transitions through states:
- **pending** → initial state when accepted
- **running** → processing accounts and publishing
- **done** → all accounts processed (some may have failed)
- **failed** → fatal error (account load failed, etc.)

### 4. Server delivers webhook callback

Once processing completes, the server POSTs results to the callback URL:

```http
POST https://openclaw.local/webhook/job/123 HTTP/1.1
Content-Type: application/json

{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "done",
  "finished_at": "2026-05-01T12:00:00.000000",
  "results": {
    "elena": {
      "111222333": true,
      "444555666": false
    }
  },
  "summary": {
    "total_groups": 2,
    "succeeded": 1,
    "failed": 1
  }
}
```

**Note:** The `finished_at` timestamp is in ISO 8601 format with microseconds. The `results` dict maps account names to group IDs, with boolean values indicating success/failure.

---

## Testing Options

### Option 1: Direct curl (Quick Test)

Start the server in one terminal:

```bash
cd facebook_auto_poster
python main.py
# Output: Running on http://0.0.0.0:5000
```

In another terminal, send a test POST request:

```bash
API_KEY=$(grep OPENCLAW_API_KEY .env | cut -d= -f2)

curl -X POST http://localhost:5000/post \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Texto de prueba desde curl",
    "accounts": ["elena"],
    "callback_url": "http://localhost:9999/callback"
  }'
```

Expected response (202 Accepted):

```json
{
  "status": "accepted",
  "job_id": "...",
  "accounts": ["elena"],
  "text_preview": "Texto de prueba desde curl"
}
```

**Note:** Without a webhook listener, the callback will fail silently (retry 3 times). To validate the callback, use Option 2.

---

### Option 2: Python Script with Local Webhook Listener

This script starts a local HTTP server to receive the webhook callback, then sends a POST /post request and waits for the callback.

Save as `test_openclaw_integration.py` in the project root:

```python
#!/usr/bin/env python3
"""
Test OpenClaw integration by simulating OpenClaw and listening for webhook callbacks.

Usage:
    python test_openclaw_integration.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add project to path for imports
sys.path.insert(0, str(Path(__file__).parent / "facebook_auto_poster"))

import requests
from dotenv import load_dotenv

# Load .env
ENV_PATH = Path(__file__).parent / "facebook_auto_poster" / ".env"
load_dotenv(ENV_PATH)

API_KEY = os.getenv("OPENCLAW_API_KEY", "")
SERVER_URL = "http://localhost:5000"
CALLBACK_PORT = 9999
CALLBACK_HOST = "127.0.0.1"

# Global state to track callback receipt
callback_received = asyncio.Event()
callback_payload = None


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for receiving webhook callbacks."""

    def do_POST(self):
        global callback_payload
        if self.path == "/callback":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            callback_payload = json.loads(body)

            # Send 200 OK
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

            # Signal that callback was received
            callback_received.set()
            print("\n✅ Webhook callback received!")
            print(json.dumps(callback_payload, indent=2))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging


def start_webhook_server():
    """Start the webhook listener in a background thread."""
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_integration():
    """Main test function."""
    print("=" * 60)
    print("  OpenClaw Integration Test")
    print("=" * 60)

    if not API_KEY:
        print("❌ OPENCLAW_API_KEY not found in .env")
        sys.exit(1)

    # Start webhook server
    print(f"\n[1] Starting webhook listener on {CALLBACK_HOST}:{CALLBACK_PORT}...")
    server = start_webhook_server()
    print(f"    ✓ Listening on http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback")

    # Send POST /post request
    print(f"\n[2] Sending POST /post to {SERVER_URL}...")
    callback_url = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"
    
    payload = {
        "text": f"Test message from integration test — {datetime.now().isoformat()}",
        "accounts": ["elena"],
        "callback_url": callback_url,
    }

    try:
        response = requests.post(
            f"{SERVER_URL}/post",
            json=payload,
            headers={"X-API-Key": API_KEY},
            timeout=5,
        )
        print(f"    Response: {response.status_code}")

        if response.status_code == 202:
            resp_json = response.json()
            job_id = resp_json.get("job_id")
            print(f"    ✓ Job accepted: {job_id}")
            print(f"    Payload: {json.dumps(resp_json, indent=6)}")
        else:
            print(f"    ❌ Unexpected status code {response.status_code}")
            print(f"    Body: {response.text}")
            sys.exit(1)

    except requests.exceptions.RequestException as e:
        print(f"    ❌ Request failed: {e}")
        print("    Is the server running? (python facebook_auto_poster/main.py)")
        sys.exit(1)

    # Wait for callback
    print(f"\n[3] Waiting for webhook callback (timeout: 60s)...")
    print("    Job is processing in background...")
    
    try:
        # Use asyncio to handle timeout
        import signal

        def timeout_handler(signum, frame):
            print("    ⏱️  Timeout waiting for callback (job may still be running)")
            sys.exit(1)

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(60)

        # Poll for callback
        while True:
            if callback_received.is_set():
                signal.alarm(0)  # Cancel timeout
                break
            asyncio.run(asyncio.sleep(0.1))

    except KeyboardInterrupt:
        print("    Interrupted by user")
        sys.exit(1)

    # Validate callback payload
    print(f"\n[4] Validating callback payload...")
    errors = []

    if not callback_payload:
        errors.append("Callback payload is empty")
    else:
        required_fields = ["job_id", "status", "finished_at", "results", "summary"]
        for field in required_fields:
            if field not in callback_payload:
                errors.append(f"Missing field: {field}")

        if "results" in callback_payload:
            if not isinstance(callback_payload["results"], dict):
                errors.append("'results' must be a dict mapping account names to group results")
            else:
                for account, groups in callback_payload["results"].items():
                    if not isinstance(groups, dict):
                        errors.append(f"Account '{account}' results must be a dict of group IDs → bool")
                    else:
                        for group_id, success in groups.items():
                            if not isinstance(success, bool):
                                errors.append(f"Group {group_id} result must be bool, got {type(success)}")

        if "summary" in callback_payload:
            summary = callback_payload["summary"]
            required_summary = ["total_groups", "succeeded", "failed"]
            for field in required_summary:
                if field not in summary:
                    errors.append(f"Missing summary field: {field}")
            
            if "total_groups" in summary and "succeeded" in summary and "failed" in summary:
                if summary["succeeded"] + summary["failed"] != summary["total_groups"]:
                    errors.append(
                        f"Summary math error: {summary['succeeded']} + {summary['failed']} "
                        f"!= {summary['total_groups']}"
                    )

    # Report results
    print("\n" + "=" * 60)
    if errors:
        print("  ❌ VALIDATION FAILED")
        print("=" * 60)
        for error in errors:
            print(f"  • {error}")
        sys.exit(1)
    else:
        print("  ✅ ALL CHECKS PASSED")
        print("=" * 60)
        print(f"  Job ID: {callback_payload.get('job_id')}")
        print(f"  Status: {callback_payload.get('status')}")
        print(f"  Summary: {callback_payload.get('summary')}")
        print("=" * 60)


if __name__ == "__main__":
    test_integration()
```

**Run the test:**

```bash
# Terminal 1: Start the server
python facebook_auto_poster/main.py

# Terminal 2: Run the integration test
python test_openclaw_integration.py
```

Expected output:

```
============================================================
  OpenClaw Integration Test
============================================================

[1] Starting webhook listener on 127.0.0.1:9999...
    ✓ Listening on http://127.0.0.1:9999/callback

[2] Sending POST /post to http://localhost:5000...
    Response: 202
    ✓ Job accepted: 550e8400-e29b-41d4-a716-446655440000
    Payload: {
      "status": "accepted",
      "job_id": "550e8400-e29b-41d4-a716-446655440000",
      "accounts": ["elena"],
      "text_preview": "Test message from..."
    }

[3] Waiting for webhook callback (timeout: 60s)...
    Job is processing in background...

✅ Webhook callback received!
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "done",
  "finished_at": "2026-05-01T12:00:15.123456",
  "results": {
    "elena": {
      "111222333": true,
      "444555666": false
    }
  },
  "summary": {
    "total_groups": 2,
    "succeeded": 1,
    "failed": 1
  }
}

[4] Validating callback payload...

============================================================
  ✅ ALL CHECKS PASSED
============================================================
  Job ID: 550e8400-e29b-41d4-a716-446655440000
  Status: done
  Summary: {'total_groups': 2, 'succeeded': 1, 'failed': 1}
============================================================
```

---

## Validation Checklist

After running either test option above, verify:

- [ ] POST /post returns **202 Accepted** (not 200, not 400)
- [ ] Response includes `job_id` (UUID format)
- [ ] Job appears in DB as "pending", then "running", then "done"
- [ ] Logs show login success: `[poster.elena] INFO Login exitoso`
- [ ] Logs show publish attempts: `[poster.elena] INFO Publicando en grupo 123456...`
- [ ] Logs show job completion: `[job_store] INFO Job ... marcado como done`
- [ ] Webhook callback is **received** (POST request to callback_url)
- [ ] Callback has correct **JSON structure** (job_id, status, finished_at, results, summary)
- [ ] `results[account][group_id]` is **boolean** (true = success, false = error)
- [ ] `summary.succeeded + summary.failed == summary.total_groups`
- [ ] Accounts and groups match the request (`accounts=["elena"]` and account's configured groups)

---

## Debugging

### Check Logs

View the most recent logs:

```bash
# All logs
tail -50 facebook_auto_poster/logs/main.log

# Per-account logs
tail -20 facebook_auto_poster/logs/elena.log

# Find job-specific entries
grep "550e8400" facebook_auto_poster/logs/*.log
```

### Check Database

Query job status and results:

```bash
sqlite3 facebook_auto_poster/jobs.db

# View all jobs
SELECT id, status, created_at FROM jobs ORDER BY created_at DESC LIMIT 10;

# View a specific job
SELECT id, status, created_at FROM jobs WHERE id = '550e8400-e29b-41d4-a716-446655440000';

# View job results
SELECT job_id, account, group_tag, success FROM job_results WHERE job_id = '550e8400-e29b-41d4-a716-446655440000';
```

### Check Network

If the webhook callback is not received, check:

1. **Callback URL is reachable** from the server's perspective
   ```bash
   curl -v http://localhost:9999/callback -X POST -H "Content-Type: application/json" -d '{}'
   ```

2. **Rate limiting**: If requests fail with 429, wait a bit or check `api_server.py` line ~350 for `_rate_limiter`

3. **Logs for webhook errors**:
   ```bash
   grep "webhook" facebook_auto_poster/logs/main.log
   grep "callback" facebook_auto_poster/logs/main.log
   ```

### Manual Test with sqlite3

Without running the full server, inspect the database directly:

```bash
# List all accounts
sqlite3 facebook_auto_poster/jobs.db \
  "SELECT name, email, is_active FROM accounts WHERE is_active = 1;"

# Check if a job exists
sqlite3 facebook_auto_poster/jobs.db \
  "SELECT id, status, text FROM jobs WHERE status='done' ORDER BY created_at DESC LIMIT 1;"
```

---

## Troubleshooting

| Symptom | Cause | Solution |
|---------|-------|----------|
| 401 Unauthorized | Wrong or missing X-API-Key | Check `OPENCLAW_API_KEY` in `.env` |
| 400 Bad Request | Invalid JSON or missing fields | Verify payload matches schema above |
| 429 Too Many Requests | Rate limiter triggered | Wait ~60 seconds before retrying |
| Callback not received | Callback URL is unreachable | Ensure OpenClaw webhook endpoint is accessible |
| Job stuck in "running" | Browser crashed or timeout | Check logs for exceptions; increase `implicit_wait` in config |
| Group not published | Group ID is wrong or account has no groups | Verify group IDs in DB: `SELECT groups FROM accounts` |

---

## Performance Notes

- The POST /post endpoint responds in **<100ms** (returns 202 immediately)
- Actual job processing happens in a **background daemon thread**
- Each account's browser session takes **5–10 seconds to login**
- Publishing to each group takes **30–60 seconds** (human-like delays)
- Full job with 2 accounts × 3 groups ≈ **5–10 minutes** total

Webhook callbacks are retried **3 times** if the first attempt fails (useful if OpenClaw server is temporarily down).

---

## Next Steps

1. **Verify your .env credentials** are correct (OPENCLAW_API_KEY, account emails/phones, Facebook password)
2. **Set up OpenClaw** to point its webhook URL to this server's callback endpoint
3. **Monitor logs** during the first few runs to ensure everything behaves as expected
4. **Tune timing parameters** in `config.py` if Facebook detects too much automation (adjust `wait_between_groups_*`, etc.)

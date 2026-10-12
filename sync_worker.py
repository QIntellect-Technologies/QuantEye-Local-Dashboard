"""
Phase 2.3 — Edge Sync Worker for Local-Dashboard
Reads pending events from sync_outbox_queue, authenticates Edge Node to Cloud,
and delivers idempotent batches with exponential backoff.
"""

import os
import sys
import json
import time
import math
import sqlite3
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple, Any

logger = logging.getLogger("quanteye.sync_worker")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s [SyncWorker] %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_integration.json")
CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".edge_credentials.json")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")

class EdgeCredentialManager:
    """Manages Edge Node ID and cryptographic secret securely."""
    @staticmethod
    def get_credentials() -> Dict[str, str]:
        if os.path.exists(CREDENTIALS_PATH):
            try:
                with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("edge_node_id") and data.get("edge_secret"):
                        return data
            except Exception as e:
                logger.warning("Could not read edge credentials: %s", e)
        # Default or fallback
        return {
            "edge_node_id": os.environ.get("EDGE_NODE_ID", "edge_demo_node_01"),
            "edge_secret": os.environ.get("EDGE_SECRET", "sec_demo_secret_99812")
        }

    @staticmethod
    def save_credentials(edge_node_id: str, edge_secret: str):
        try:
            with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
                json.dump({"edge_node_id": edge_node_id, "edge_secret": edge_secret}, f, indent=2)
        except Exception as e:
            logger.error("Failed to persist edge credentials: %s", e)

class EdgeSyncWorker:
    def __init__(self, db_path: str = DB_PATH, batch_size: int = 50, cloud_url: Optional[str] = None):
        self.db_path = db_path
        self.batch_size = batch_size
        self._custom_cloud_url = cloud_url
        self._cached_token: Optional[str] = None
        self._token_expiry: float = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def get_cloud_url(self) -> str:
        if self._custom_cloud_url:
            return self._custom_cloud_url.rstrip("/")
        if os.environ.get("MAIN_DASHBOARD_URL"):
            return os.environ.get("MAIN_DASHBOARD_URL").rstrip("/")
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    return str(cfg.get("main_dashboard_url") or "http://127.0.0.1:5100").rstrip("/")
            except Exception:
                pass
        return "http://127.0.0.1:5100"

    def authenticate_to_cloud(self) -> Optional[str]:
        """Obtain or refresh short-lived Edge JWT from cloud."""
        now = time.time()
        if self._cached_token and now < (self._token_expiry - 60):
            return self._cached_token

        creds = EdgeCredentialManager.get_credentials()
        url = f"{self.get_cloud_url()}/api/cloud/edge/auth"
        body = json.dumps({
            "edge_node_id": creds["edge_node_id"],
            "secret": creds["edge_secret"]
        }).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    self._cached_token = data.get("token")
                    expires_in = int(data.get("expires_in") or 3600)
                    self._token_expiry = now + expires_in
                    logger.info("Successfully obtained new Edge JWT from cloud.")
                    return self._cached_token
        except Exception as e:
            logger.warning("Edge authentication request failed: %s", e)
            return None

    def fetch_pending_outbox(self, limit: int = 50, ignore_backoff: bool = False) -> List[Dict[str, Any]]:
        """Fetch pending outbox records that are ready for attempt."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if ignore_backoff:
                cur.execute("""
                    SELECT id, event_id, organization_id, branch_id, local_instance_id,
                           event_type, payload_json, attempts
                    FROM sync_outbox_queue
                    WHERE status IN ('pending', 'failed')
                    ORDER BY id ASC
                    LIMIT ?;
                """, (limit,))
            else:
                cur.execute("""
                    SELECT id, event_id, organization_id, branch_id, local_instance_id,
                           event_type, payload_json, attempts
                    FROM sync_outbox_queue
                    WHERE status IN ('pending', 'failed')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= datetime('now', 'localtime'))
                    ORDER BY id ASC
                    LIMIT ?;
                """, (limit,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def mark_outbox_synced(self, record_ids: List[int]):
        """Mark successfully synced records."""
        if not record_ids:
            return
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            placeholders = ",".join(["?"] * len(record_ids))
            cur.execute(f"""
                UPDATE sync_outbox_queue
                SET status = 'synced',
                    synced_at = datetime('now', 'localtime'),
                    last_attempt_at = datetime('now', 'localtime'),
                    last_error = NULL
                WHERE id IN ({placeholders});
            """, record_ids)
            conn.commit()

    def mark_outbox_failed(self, record_ids: List[int], error_msg: str):
        """Mark failed attempts with exponential backoff."""
        if not record_ids:
            return
        now_dt = datetime.now()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            for rid in record_ids:
                cur.execute("SELECT attempts FROM sync_outbox_queue WHERE id = ?", (rid,))
                row = cur.fetchone()
                attempts = (row[0] if row else 0) + 1
                # Exponential backoff: 2^attempts seconds, capped at 300s (5m)
                delay_sec = min(int(math.pow(2, min(attempts, 8))), 300)
                next_at = (now_dt + timedelta(seconds=delay_sec)).strftime('%Y-%m-%d %H:%M:%S')

                cur.execute("""
                    UPDATE sync_outbox_queue
                    SET status = 'failed',
                        attempts = ?,
                        last_attempt_at = datetime('now', 'localtime'),
                        next_attempt_at = ?,
                        last_error = ?
                    WHERE id = ?;
                """, (attempts, next_at, error_msg[:500], rid))
            conn.commit()

    def sync_batch(self, batch_sequence: int = 1) -> Dict[str, Any]:
        """Process one batch of pending outbox events."""
        records = self.fetch_pending_outbox(self.batch_size)
        if not records:
            return {"status": "idle", "synced": 0}

        token = self.authenticate_to_cloud()
        record_ids = [r["id"] for r in records]

        if not token:
            self.mark_outbox_failed(record_ids, "Cloud offline: Could not authenticate edge node.")
            return {"status": "offline", "synced": 0, "error": "Could not authenticate"}

        events_payload = []
        for r in records:
            try:
                p = json.loads(r["payload_json"])
            except Exception:
                p = {}
            events_payload.append({
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "payload": p
            })

        url = f"{self.get_cloud_url()}/api/cloud/sync/batch"
        body = json.dumps({
            "batch_sequence": batch_sequence,
            "events": events_payload
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    self.mark_outbox_synced(record_ids)
                    logger.info("Synchronized batch of %d events to Cloud.", len(record_ids))
                    return {"status": "success", "synced": len(record_ids), "cloud_response": res_data}
                else:
                    err_msg = f"HTTP {resp.status}"
                    self.mark_outbox_failed(record_ids, err_msg)
                    return {"status": "error", "synced": 0, "error": err_msg}
        except urllib.error.HTTPError as he:
            err_msg = f"HTTP {he.code}: {he.read().decode('utf-8', errors='ignore')}"
            self.mark_outbox_failed(record_ids, err_msg)
            return {"status": "error", "synced": 0, "error": err_msg}
        except Exception as e:
            err_msg = f"Network error: {e}"
            self.mark_outbox_failed(record_ids, err_msg)
            return {"status": "offline", "synced": 0, "error": err_msg}

    def run_loop(self, poll_interval: float = 3.0):
        seq = 1
        while not self._stop_event.is_set():
            try:
                self.sync_batch(batch_sequence=seq)
                seq += 1
            except Exception as e:
                logger.error("Error in sync loop: %s", e)
            self._stop_event.wait(poll_interval)

    def start_background(self, poll_interval: float = 3.0):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run_loop, args=(poll_interval,), daemon=True)
        self._thread.start()
        logger.info("EdgeSyncWorker background thread started.")

    def stop_background(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            logger.info("EdgeSyncWorker background thread stopped.")

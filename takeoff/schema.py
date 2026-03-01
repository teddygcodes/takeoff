"""Takeoff database schema and persistence layer."""

import logging
import sqlite3
import json
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class TakeoffDB:
    """SQLite database for Takeoff job results and audit trails."""

    def __init__(self, db_path: str = "takeoff.db"):
        """Initialize database connection with WAL mode.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

        # Enable WAL mode (following Atlantis pattern)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Enforce foreign key constraints (SQLite disables them by default)
        self.conn.execute("PRAGMA foreign_keys=ON")

        self._create_tables()

    def _create_tables(self):
        """Create all Takeoff database tables."""

        # Job metadata
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS takeoff_jobs (
                job_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                drawing_name TEXT,
                total_pages INTEGER,
                snippet_count INTEGER,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                latency_ms INTEGER,
                cost_usd REAL
            )
        """)

        # Snippet metadata
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS snippets (
                snippet_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                page_number INTEGER,
                label TEXT NOT NULL,
                sub_label TEXT,
                bbox_json TEXT,
                image_path TEXT,
                FOREIGN KEY (job_id) REFERENCES takeoff_jobs(job_id)
            )
        """)

        # Extracted fixture schedule
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS fixture_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                type_tag TEXT NOT NULL,
                description TEXT,
                manufacturer TEXT,
                catalog_number TEXT,
                voltage TEXT,
                mounting TEXT,
                dimming TEXT,
                wattage REAL,
                notes TEXT,
                FOREIGN KEY (job_id) REFERENCES takeoff_jobs(job_id)
            )
        """)

        # Per-area fixture counts
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS fixture_counts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                type_tag TEXT NOT NULL,
                area TEXT NOT NULL,
                count INTEGER NOT NULL,
                confidence REAL,
                difficulty_code TEXT DEFAULT 'S',
                flags TEXT,
                FOREIGN KEY (job_id) REFERENCES takeoff_jobs(job_id)
            )
        """)

        # Adversarial log
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS adversarial_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                agent TEXT NOT NULL,
                attack_id TEXT,
                severity TEXT,
                category TEXT,
                description TEXT,
                resolution TEXT,
                final_verdict TEXT,
                FOREIGN KEY (job_id) REFERENCES takeoff_jobs(job_id)
            )
        """)

        # Final results
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                job_id TEXT PRIMARY KEY,
                grand_total INTEGER,
                confidence_score REAL,
                confidence_band TEXT,
                confidence_features TEXT,
                violations_json TEXT,
                flags_json TEXT,
                judge_verdict TEXT,
                approved_at REAL,
                full_result_json TEXT,
                FOREIGN KEY (job_id) REFERENCES takeoff_jobs(job_id)
            )
        """)
        # Migrate: add full_result_json if upgrading an existing database.
        # Only swallow "duplicate column name" — re-raise anything else (permissions, corruption).
        try:
            self.conn.execute("ALTER TABLE results ADD COLUMN full_result_json TEXT")
            self.conn.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

        # Create indexes
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_jobs_created
            ON takeoff_jobs(created_at)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_counts_job
            ON fixture_counts(job_id)
        """)

        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_adv_log_job
            ON adversarial_log(job_id)
        """)

        self.conn.commit()

    def create_job(
        self,
        job_id: str,
        mode: str,
        drawing_name: Optional[str] = None,
        total_pages: Optional[int] = None,
        snippet_count: Optional[int] = None
    ) -> None:
        """Create a new takeoff job record."""
        with self._lock:
            self.conn.execute("""
                INSERT INTO takeoff_jobs (job_id, created_at, drawing_name, total_pages, snippet_count, mode, status)
                VALUES (?, ?, ?, ?, ?, ?, 'running')
            """, (job_id, time.time(), drawing_name, total_pages, snippet_count, mode))
            self.conn.commit()

    def update_job_status(
        self,
        job_id: str,
        status: str,
        latency_ms: Optional[int] = None,
        cost_usd: Optional[float] = None
    ) -> None:
        """Update job status and metrics."""
        with self._lock:
            self.conn.execute("""
                UPDATE takeoff_jobs
                SET status = ?, latency_ms = ?, cost_usd = ?
                WHERE job_id = ?
            """, (status, latency_ms, cost_usd, job_id))
            self.conn.commit()

    def store_snippets(self, job_id: str, snippets: List[Dict]) -> None:
        """Store snippet metadata (not images)."""
        with self._lock:
            for s in snippets:
                snippet_id = s.get("id", "")
                if not snippet_id:
                    logger.warning("[DB] Snippet missing 'id' field for job %s — skipping", job_id)
                    continue
                self.conn.execute("""
                    INSERT OR REPLACE INTO snippets
                    (snippet_id, job_id, page_number, label, sub_label, bbox_json, image_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    snippet_id,
                    job_id,
                    s.get("page_number"),
                    s.get("label", ""),
                    s.get("sub_label"),
                    json.dumps(s.get("bbox")) if s.get("bbox") else None,
                    s.get("image_path")
                ))
            self.conn.commit()

    def store_fixture_schedule(self, job_id: str, schedule: Dict) -> None:
        """Store extracted fixture schedule entries."""
        with self._lock:
            fixtures = schedule.get("fixtures", {})
            for tag, info in fixtures.items():
                self.conn.execute("""
                    INSERT INTO fixture_schedule
                    (job_id, type_tag, description, manufacturer, catalog_number,
                     voltage, mounting, dimming, wattage, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id, tag,
                    info.get("description"),
                    info.get("manufacturer"),
                    info.get("catalog_number"),
                    info.get("voltage"),
                    info.get("mounting"),
                    info.get("dimming"),
                    info.get("wattage"),
                    info.get("notes")
                ))
            self.conn.commit()

    def store_fixture_counts(self, job_id: str, fixture_counts: List[Dict]) -> None:
        """Store per-area fixture counts."""
        with self._lock:
            for fc in fixture_counts:
                counts_by_area = fc.get("counts_by_area", {})
                for area, count in counts_by_area.items():
                    self.conn.execute("""
                        INSERT INTO fixture_counts
                        (job_id, type_tag, area, count, confidence, difficulty_code, flags)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        job_id,
                        fc.get("type_tag", ""),
                        area,
                        count,
                        fc.get("confidence"),
                        fc.get("difficulty_code") or fc.get("difficulty", "S"),
                        json.dumps(fc.get("flags", []), default=str)
                    ))
            self.conn.commit()

    def store_adversarial_log(
        self,
        job_id: str,
        attacks: List[Dict],
        reconciler_responses: List[Dict]
    ) -> None:
        """Store the full adversarial exchange log."""
        with self._lock:
            # Store attacks
            for attack in attacks:
                self.conn.execute("""
                    INSERT INTO adversarial_log
                    (job_id, agent, attack_id, severity, category, description, resolution, final_verdict)
                    VALUES (?, 'checker', ?, ?, ?, ?, NULL, NULL)
                """, (
                    job_id,
                    attack.get("attack_id"),
                    attack.get("severity"),
                    attack.get("category"),
                    attack.get("description")
                ))

            # Update with reconciler responses
            for resp in reconciler_responses:
                attack_id = resp.get("attack_id")
                verdict = resp.get("verdict")
                explanation = resp.get("explanation")
                cur = self.conn.execute("""
                    UPDATE adversarial_log
                    SET resolution = ?, final_verdict = ?
                    WHERE job_id = ? AND attack_id = ?
                """, (explanation, verdict, job_id, attack_id))
                if cur.rowcount == 0:
                    logger.warning(
                        "[DB] Reconciler response for attack_id '%s' found no matching row "
                        "in adversarial_log for job '%s' — response data not persisted",
                        attack_id, job_id
                    )

            self.conn.commit()

    def store_result(
        self,
        job_id: str,
        grand_total: int,
        confidence_score: float,
        confidence_band: str,
        confidence_features: str,
        violations: List[Dict],
        flags: List[str],
        judge_verdict: str,
        full_result: Optional[Dict] = None
    ) -> None:
        """Store final takeoff result."""
        with self._lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO results
                (job_id, grand_total, confidence_score, confidence_band, confidence_features,
                 violations_json, flags_json, judge_verdict, approved_at, full_result_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id,
                grand_total,
                confidence_score,
                confidence_band,
                confidence_features,
                json.dumps(violations, default=str),
                json.dumps(flags, default=str),
                judge_verdict,
                time.time() if judge_verdict == "PASS" else None,
                json.dumps(full_result, default=str) if full_result else None
            ))
            self.conn.commit()

    def store_job_results_atomic(
        self,
        job_id: str,
        fixture_counts: List[Dict],
        attacks: List[Dict],
        reconciler_responses: List[Dict],
        grand_total: int,
        confidence_score: float,
        confidence_band: str,
        confidence_features: str,
        violations: List[Dict],
        flags: List[str],
        judge_verdict: str,
        full_result: Optional[Dict] = None
    ) -> None:
        """Store fixture counts, adversarial log, and result in one atomic transaction.

        All three writes commit together or roll back together — prevents the DB from
        being left in a partial state if the process crashes mid-write.
        """
        with self._lock:
            cur = self.conn.cursor()
            try:
                cur.execute("BEGIN")
                # Fixture counts
                for fc in fixture_counts:
                    for area, count in fc.get("counts_by_area", {}).items():
                        cur.execute("""
                            INSERT INTO fixture_counts
                            (job_id, type_tag, area, count, confidence, difficulty_code, flags)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (
                            job_id,
                            fc.get("type_tag", ""),
                            area,
                            count,
                            fc.get("confidence"),
                            fc.get("difficulty_code") or fc.get("difficulty", "S"),
                            json.dumps(fc.get("flags", []), default=str)
                        ))

                # Adversarial log
                for attack in attacks:
                    cur.execute("""
                        INSERT INTO adversarial_log
                        (job_id, agent, attack_id, severity, category, description, resolution, final_verdict)
                        VALUES (?, 'checker', ?, ?, ?, ?, NULL, NULL)
                    """, (
                        job_id,
                        attack.get("attack_id"),
                        attack.get("severity"),
                        attack.get("category"),
                        attack.get("description")
                    ))
                for resp in reconciler_responses:
                    cur.execute("""
                        UPDATE adversarial_log
                        SET resolution = ?, final_verdict = ?
                        WHERE job_id = ? AND attack_id = ?
                    """, (resp.get("explanation"), resp.get("verdict"), job_id, resp.get("attack_id")))
                    if cur.rowcount == 0:
                        logger.warning(
                            "[DB] Reconciler response for attack_id '%s' found no matching row "
                            "in adversarial_log for job '%s' — response data not persisted",
                            resp.get("attack_id"), job_id
                        )

                # Result
                cur.execute("""
                    INSERT OR REPLACE INTO results
                    (job_id, grand_total, confidence_score, confidence_band, confidence_features,
                     violations_json, flags_json, judge_verdict, approved_at, full_result_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id,
                    grand_total,
                    confidence_score,
                    confidence_band,
                    confidence_features,
                    json.dumps(violations, default=str),
                    json.dumps(flags, default=str),
                    judge_verdict,
                    time.time() if judge_verdict == "PASS" else None,
                    json.dumps(full_result, default=str) if full_result else None
                ))

                cur.execute("COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass  # Already rolled back or BEGIN never succeeded
                raise

    def get_full_result(self, job_id: str) -> Optional[Dict]:
        """Retrieve the full formatted result for a completed job."""
        with self._lock:
            row = self.conn.execute(
                "SELECT full_result_json FROM results WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row and row["full_result_json"]:
                return json.loads(row["full_result_json"])
            return None

    def get_job(self, job_id: str) -> Optional[Dict]:
        """Retrieve a job record."""
        with self._lock:
            row = self.conn.execute("""
                SELECT j.*, r.grand_total, r.confidence_score, r.confidence_band,
                       r.violations_json, r.flags_json, r.judge_verdict, r.approved_at
                FROM takeoff_jobs j
                LEFT JOIN results r ON j.job_id = r.job_id
                WHERE j.job_id = ?
            """, (job_id,)).fetchone()
            return dict(row) if row else None

    def get_job_counts(self, job_id: str) -> List[Dict]:
        """Retrieve fixture counts for a job."""
        with self._lock:
            rows = self.conn.execute("""
                SELECT type_tag, area, count, confidence, difficulty_code, flags
                FROM fixture_counts
                WHERE job_id = ?
                ORDER BY type_tag, area
            """, (job_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_job_adversarial_log(self, job_id: str) -> List[Dict]:
        """Retrieve adversarial log for a job."""
        with self._lock:
            rows = self.conn.execute("""
                SELECT agent, attack_id, severity, category, description, resolution, final_verdict
                FROM adversarial_log
                WHERE job_id = ?
                ORDER BY id
            """, (job_id,)).fetchall()
            return [dict(r) for r in rows]

    def list_jobs(self, limit: int = 20) -> List[Dict]:
        """List recent takeoff jobs."""
        with self._lock:
            rows = self.conn.execute("""
                SELECT j.job_id, j.created_at, j.drawing_name, j.mode, j.status,
                       j.snippet_count, r.grand_total, r.confidence_band, r.judge_verdict
                FROM takeoff_jobs j
                LEFT JOIN results r ON j.job_id = r.job_id
                ORDER BY j.created_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def close(self):
        """Close database connection, waiting for any active write to finish."""
        with self._lock:
            self.conn.close()

"""Operational scan-delay alarm, independent of measurement freshness."""


class ScanUpdateAlarm:
    fresh_ns = 1_000_000_000
    confirm_ns = 3_000_000_000

    def __init__(self):
        self.ns = None
        self.at = None
        self.active = False
        self.recovering_since = None

    def ingest(self, acquired_ns, acquired_at, *, now_ns):
        if acquired_ns > now_ns or (self.ns is not None and acquired_ns <= self.ns):
            return
        # Detect a gap even if no status call occurred between scans.
        self.snapshot(now_ns=now_ns)
        if self.active:
            if self.ns is None or acquired_ns - self.ns > self.fresh_ns:
                self.recovering_since = None
            if now_ns - acquired_ns <= self.fresh_ns:
                if self.recovering_since is None:
                    self.recovering_since = acquired_ns
                elif acquired_ns - self.recovering_since >= self.fresh_ns:
                    self.active = False
                    self.recovering_since = None
        self.ns, self.at = acquired_ns, acquired_at

    def snapshot(self, *, now_ns):
        age = None if self.ns is None else now_ns - self.ns
        if age is None or age < 0:
            state, active, age = "UNKNOWN", None, None
            self.recovering_since = None
        else:
            if age > self.fresh_ns:
                self.recovering_since = None
            if age > self.confirm_ns:
                self.active = True
                state = "ACTIVE"
            elif self.active:
                state = "RECOVERING"
            else:
                state = "PENDING" if age > self.fresh_ns else "CLEAR"
            active = self.active
        code = {
            "PENDING": "SCAN_UPDATE_DELAY_PENDING",
            "ACTIVE": "SCAN_UPDATE_DELAY_CONFIRMED",
            "RECOVERING": "SCAN_UPDATE_RECOVERING",
        }.get(state)
        return {
            "state": state,
            "active": active,
            "scan_age_ms": None if age is None else age / 1_000_000,
            "last_valid_scan_at": self.at,
            "confirm_after_ms": 3000,
            "reason_codes": [] if code is None else [code],
        }

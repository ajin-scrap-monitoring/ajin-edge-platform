"""Conservative, continuously observed recovery of the Outbox loss alarm."""


class OutboxLossRecovery:
    recovery_seconds = 3600
    max_observation_gap = 3

    def __init__(self, previous_high_water=0):
        self.high_water = previous_high_water
        self.active = previous_high_water > 0
        self.last_count = None
        self.last_seen = None
        self.epoch = None
        self.healthy_since = None
        self.eligible = False

    def observe(self, count, *, healthy, epoch, now, evidence_missing=False):
        changed = self.last_count is None or count != self.last_count
        interrupted = (
            self.last_seen is None or not 0 <= now - self.last_seen <= self.max_observation_gap
        )
        restarted = epoch != self.epoch
        self.high_water = max(self.high_water, count)
        if changed or interrupted or restarted or evidence_missing:
            self.healthy_since = None
            self.active = self.high_water > 0
        self.eligible = healthy and not evidence_missing
        if not self.eligible:
            self.healthy_since = None
        elif self.healthy_since is None:
            self.healthy_since = now
        if (
            self.active
            and self.healthy_since is not None
            and now - self.healthy_since >= self.recovery_seconds
        ):
            self.active = False
        self.last_seen, self.last_count, self.epoch = now, count, epoch
        return self.active

    def interrupt(self):
        self.healthy_since = None
        self.last_seen = None
        self.eligible = False
        self.active = self.high_water > 0

    def snapshot(self, now):
        return {
            "active": self.active,
            "loss_high_water": self.high_water,
            "observed_loss_count": self.last_count,
            "eligible": self.eligible,
            "continuous_healthy_seconds": max(0, now - self.healthy_since)
            if self.healthy_since is not None
            else 0,
            "required_seconds": self.recovery_seconds,
        }

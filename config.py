# ── SLA thresholds (first reply, in minutes) ──────────────────────────────────
SLA_MINUTES = {
    "urgent": 30,
    "high":   60,
    "normal": 1440,   # 24 hours
    "low":    4320,   # 3 days
    "none":   1440,
}

# Warn when a ticket has this many minutes or fewer left before SLA breach.
WARNING_MINUTES = 4 * 60   # 4 hours

# Output folder for saved report files
OUTPUT_DIR = "output"

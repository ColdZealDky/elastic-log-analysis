#!/usr/bin/env python3
"""
Generate synthetic release logs for Elasticsearch analysis.

Outputs NDJSON (one JSON object per line):
- data/logs_v1_4_2.ndjson
- data/logs_v1_4_3.ndjson

Designed for "Release Readiness & Regression Tracker":
- v1.4.2 is baseline
- v1.4.3 introduces regressions (higher error rates, auth timeouts, db latency, new error signature)
"""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List


@dataclass
class ServiceProfile:
    name: str
    base_rps: float
    base_latency_ms: float
    latency_jitter_ms: float
    base_error_rate: float  # fraction (0.0 - 1.0)
    common_errors: List[str]


REGIONS = ["us-east", "us-west", "eu-west"]
ENVS = ["prod"]  # keep it simple; you can add "staging" later
LEVELS = ["INFO", "WARN", "ERROR"]
HOSTS = ["node-a", "node-b", "node-c", "node-d"]
TENANTS = ["tenant-a", "tenant-b", "tenant-c", "tenant-d", "tenant-e"]


SERVICES: Dict[str, ServiceProfile] = {
    "api-gateway": ServiceProfile(
        name="api-gateway",
        base_rps=60,
        base_latency_ms=45,
        latency_jitter_ms=18,
        base_error_rate=0.003,
        common_errors=["Upstream 502", "Request timeout", "Rate limit exceeded"],
    ),
    "auth": ServiceProfile(
        name="auth",
        base_rps=45,
        base_latency_ms=35,
        latency_jitter_ms=15,
        base_error_rate=0.004,
        common_errors=["Token validation failed", "JWT signature mismatch", "OIDC refresh failed"],
    ),
    "routing": ServiceProfile(
        name="routing",
        base_rps=25,
        base_latency_ms=75,
        latency_jitter_ms=35,
        base_error_rate=0.002,
        common_errors=["Graph lookup failed", "Route generation timeout"],
    ),
    "planner": ServiceProfile(
        name="planner",
        base_rps=18,
        base_latency_ms=95,
        latency_jitter_ms=40,
        base_error_rate=0.0025,
        common_errors=["Constraint solver failed", "Trajectory infeasible"],
    ),
    "perception": ServiceProfile(
        name="perception",
        base_rps=20,
        base_latency_ms=110,
        latency_jitter_ms=55,
        base_error_rate=0.002,
        common_errors=["Frame drop detected", "Model inference timeout"],
    ),
    "db": ServiceProfile(
        name="db",
        base_rps=55,
        base_latency_ms=28,
        latency_jitter_ms=12,
        base_error_rate=0.0015,
        common_errors=["Query latency exceeded threshold", "Connection pool exhausted"],
    ),
}


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def weighted_choice(items: List[str], weights: List[float]) -> str:
    return random.choices(items, weights=weights, k=1)[0]


def make_trace_id() -> str:
    # Not a real trace id format, but good enough for correlation
    return "".join(random.choice("0123456789abcdef") for _ in range(32))


def gen_latency_ms(profile: ServiceProfile, release: str) -> int:
    # Introduce release-specific regressions:
    # - v1.4.3: db latency drift + auth tail latency worse
    base = profile.base_latency_ms
    jitter = profile.latency_jitter_ms

    if release == "v1.4.3":
        if profile.name == "db":
            base *= 1.35  # db slower
            jitter *= 1.25
        if profile.name == "auth":
            base *= 1.20
            jitter *= 1.35

    latency = random.gauss(base, jitter)
    latency = clamp(latency, 5, 2000)
    return int(round(latency))


def gen_error_event(profile: ServiceProfile, release: str) -> str:
    # Introduce "new" signature in v1.4.3 for regression detection
    if release == "v1.4.3" and profile.name == "auth":
        # New integration-ish failure
        if random.random() < 0.35:
            return "OAuth callback timeout to identity provider"

    return random.choice(profile.common_errors)


def gen_error_rate(profile: ServiceProfile, release: str, region: str) -> float:
    rate = profile.base_error_rate

    # Introduce regressions in v1.4.3
    if release == "v1.4.3":
        if profile.name == "auth":
            rate *= 3.8
        if profile.name == "db":
            rate *= 2.2
        if profile.name == "api-gateway":
            rate *= 1.6

    # Add a slight regional wobble so analysis feels real
    if region == "eu-west":
        rate *= 1.15
    if region == "us-west":
        rate *= 1.05

    return clamp(rate, 0.0, 0.25)


def gen_rps(profile: ServiceProfile, release: str) -> float:
    # Slightly higher traffic for v1.4.3 to simulate growth
    rps = profile.base_rps * (1.06 if release == "v1.4.3" else 1.0)
    # Mild random drift
    rps *= random.uniform(0.9, 1.1)
    return max(1.0, rps)


def make_log(ts: datetime, profile: ServiceProfile, release: str, region: str) -> dict:
    trace_id = make_trace_id()
    request_id = trace_id[:16]
    host = random.choice(HOSTS)
    env = "prod"
    tenant = random.choice(TENANTS)

    latency_ms = gen_latency_ms(profile, release)
    error_rate = gen_error_rate(profile, release, region)

    # Decide level
    is_error = random.random() < error_rate
    is_warn = (not is_error) and (random.random() < error_rate * 1.8)

    if is_error:
        level = "ERROR"
        msg = gen_error_event(profile, release)
        status = weighted_choice(["500", "502", "503", "504"], [0.45, 0.18, 0.22, 0.15])
        outcome = "fail"
    elif is_warn:
        level = "WARN"
        msg = random.choice([
            "Latency approaching SLO threshold",
            "Transient upstream retry",
            "Degraded mode enabled",
            "Backpressure applied",
        ])
        status = "200"
        outcome = "degraded"
    else:
        level = "INFO"
        msg = "Request completed"
        status = "200"
        outcome = "ok"

    # Add a few regression-ish fields
    slo_ms = 200 if profile.name in ("planner", "perception") else 120
    slo_breached = latency_ms > slo_ms

    doc = {
        "@timestamp": iso(ts),
        "service": profile.name,
        "env": env,
        "region": region,
        "host": host,
        "release": release,
        "level": level,
        "message": msg,
        "http": {
            "status_code": int(status),
            "method": random.choice(["GET", "POST"]),
            "route": random.choice([
                "/v1/session",
                "/v1/route",
                "/v1/plan",
                "/v1/telemetry",
                "/v1/health",
            ]),
        },
        "latency_ms": latency_ms,
        "slo_ms": slo_ms,
        "slo_breached": bool(slo_breached),
        "outcome": outcome,
        "ids": {
            "trace_id": trace_id,
            "request_id": request_id,
            "tenant": tenant,
        },
    }

    # Sprinkle in a field useful for regression slices
    if profile.name == "db":
        doc["db"] = {"query_type": random.choice(["read", "write", "index"]), "pool": "main"}
    if profile.name == "auth":
        doc["auth"] = {"provider": random.choice(["internal", "external-idp"]), "token_type": random.choice(["access", "refresh"])}

    return doc


def generate_release_logs(
    release: str,
    start_utc: datetime,
    minutes: int,
    out_path: str,
    seed: int,
) -> int:
    random.seed(seed)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    count = 0
    ts = start_utc
    end = start_utc + timedelta(minutes=minutes)

    with open(out_path, "w", encoding="utf-8") as f:
        while ts < end:
            for region in REGIONS:
                # For each service, generate events proportional to its rps
                for profile in SERVICES.values():
                    rps = gen_rps(profile, release)
                    # Convert rps into per-second events; generate 1-second "bucket"
                    events = max(1, int(round(rps)))
                    for _ in range(events):
                        # Randomize within the second
                        jitter_ms = random.randint(0, 999)
                        event_ts = ts + timedelta(milliseconds=jitter_ms)
                        doc = make_log(event_ts, profile, release, region)
                        f.write(json.dumps(doc) + "\n")
                        count += 1

            ts += timedelta(seconds=1)

    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=10, help="Minutes of logs per release (default: 10)")
    ap.add_argument("--start", type=str, default="2026-01-01T12:00:00Z", help="Start timestamp UTC (ISO, Z)")
    ap.add_argument("--outdir", type=str, default="data", help="Output directory (default: data)")
    args = ap.parse_args()

    # Parse start
    s = args.start.replace("Z", "+00:00")
    start = datetime.fromisoformat(s).astimezone(timezone.utc)

    out_v142 = os.path.join(args.outdir, "logs_v1_4_2.ndjson")
    out_v143 = os.path.join(args.outdir, "logs_v1_4_3.ndjson")

    c1 = generate_release_logs("v1.4.2", start, args.minutes, out_v142, seed=142)
    c2 = generate_release_logs("v1.4.3", start + timedelta(hours=6), args.minutes, out_v143, seed=143)

    print(f"Wrote {c1:,} events to {out_v142}")
    print(f"Wrote {c2:,} events to {out_v143}")
    print("Done.")


if __name__ == "__main__":
    main()

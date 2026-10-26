"""Simple load test: many concurrent callers each run a scripted call; report latency and errors.

python loadtest/loadtest.py --base-url http://localhost:8000 --callers 20 --seconds 20
"""

import argparse
import asyncio
import statistics
import time

import httpx

SCRIPTS = [
    [
        "I need a flu shot",
        "Priya Sharma, July 23rd 1990",
        "any provider",
        "earliest available",
        "the first one",
        "yes",
        "no thanks",
    ],
    ["what time do you open", "where are you located", "no"],
    ["when is my next appointment", "James Carter, November 2nd 1972", "no"],
    [
        "book a physical",
        "Wei Zhang, May 17th 1995",
        "any",
        "next tuesday morning",
        "the second one",
        "no, change the time",
        "friday morning",
        "the first one",
        "yes",
        "no",
    ],
    ["I can't make it to my appointment", "cancel it", "Maria Gonzalez, March 14th 1985", "no", "goodbye"],
]


async def caller(
    client: httpx.AsyncClient, idx: int, deadline: float, lat: list[float], errors: list[str], calls: list[int]
) -> None:
    n = 0
    while time.perf_counter() < deadline:
        script = SCRIPTS[(idx + n) % len(SCRIPTS)]
        n += 1
        try:
            r = await client.post("/api/calls")
            r.raise_for_status()
            cid, seq = r.json()["call_id"], 1
            for line in script:
                t0 = time.perf_counter()
                resp = await client.post(f"/api/calls/{cid}/turn", json={"text": line, "seq": seq})
                lat.append(time.perf_counter() - t0)
                if resp.status_code == 409:  # the call ended early (e.g. handed off): expected, not an error
                    break
                resp.raise_for_status()
                seq += 1
                if resp.json()["ended"]:
                    break
            calls[0] += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc)[:120])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--callers", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=20)
    a = ap.parse_args()
    lat: list[float] = []
    errors: list[str] = []
    calls = [0]
    deadline = time.perf_counter() + a.seconds
    async with httpx.AsyncClient(
        base_url=a.base_url, timeout=30, limits=httpx.Limits(max_connections=a.callers * 2)
    ) as c:
        await asyncio.gather(*(caller(c, i, deadline, lat, errors, calls) for i in range(a.callers)))
    lat.sort()
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] * 1000  # noqa: E731
    print(f"callers={a.callers} seconds={a.seconds} calls_completed={calls[0]} turns={len(lat)} errors={len(errors)}")
    print(
        f"turn latency ms: mean={statistics.mean(lat) * 1000:.1f} p50={pct(0.5):.1f} p95={pct(0.95):.1f} p99={pct(0.99):.1f} max={lat[-1] * 1000:.1f}"
    )
    print(f"throughput: {len(lat) / a.seconds:.1f} turns/s")
    if errors:
        print("first errors:", errors[:3])


if __name__ == "__main__":
    asyncio.run(main())

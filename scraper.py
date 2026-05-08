"""Scraper for NockBlocks metrics using RPC API."""
import asyncio
import math
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from config import NOCKBLOCKS_API_KEY

# ASERT anchor block is consensus-fixed; cache across API client lifetimes.
_cached_asert_anchor: Optional[dict] = None

# Post-Aletheia emissions schedule (NOCK per block). See changelog/protocol/014-aletheia.md
_DECAY_REWARDS = (1536, 1024, 768, 512, 384, 256, 192, 128, 96)
_EMISSION_CAP_HEIGHT = 16_144_876


def block_reward_nock(height: int) -> int:
    """Consensus block subsidy in NOCK for the given height (Aletheia schedule)."""
    if height <= 0 or height > _EMISSION_CAP_HEIGHT:
        return 0
    if height <= 13_150:
        return 65_536
    if height <= 39_448:
        return 32_768
    if height <= 65_500:
        return 16_384
    if height <= 170_500:
        return 2_048
    if height <= 2_060_500:
        era_idx = (height - 170_501) // 210_000
        if 0 <= era_idx < len(_DECAY_REWARDS):
            return _DECAY_REWARDS[era_idx]
        return 64
    return 64


def _format_signed_duration(seconds: float) -> str:
    """Format |seconds| as e.g. '2d 5h' or '43m' or '12s'."""
    s = abs(int(round(seconds)))
    if s >= 86400:
        d, rem = divmod(s, 86400)
        h = rem // 3600
        return f"{d}d {h}h"
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h}h {m}m"
    if s >= 60:
        m, rem = divmod(s, 60)
        return f"{m}m {rem}s"
    return f"{s}s"


@dataclass
class MiningMetrics:
    """Container for Nockchain post-ASERT mining metrics."""

    difficulty: str
    proofrate: str
    proofrate_value: float  # Numeric value in MP/s
    avg_block_time: str
    cadence_ratio: str
    latest_block: str
    latest_height: int
    blocks_since_anchor: int
    time_since_anchor: str
    schedule_drift: str
    asert_target_factor: str
    block_reward_nock: int
    nock_per_minute: float

    def format_message(self, previous_proofrate: Optional[float] = None) -> str:
        """Format metrics as a readable Telegram message."""
        if previous_proofrate is None:
            trend = ""
        else:
            change = self.proofrate_value - previous_proofrate
            pct_change = (change / previous_proofrate * 100) if previous_proofrate > 0 else 0
            if pct_change > 5:
                trend = "⬆️⬆️⬆️🚀"
            elif pct_change > 0:
                trend = "⬆️↗"
            elif pct_change > -5:
                trend = "⬇️"
            else:
                trend = "‼️⬇️⬇️⬇️‼️"

        return f"""⛏️ <b>Nockchain Mining Metrics</b>

<b>📊 Network Stats</b>
├ Difficulty: <code>{self.difficulty}</code>
├ Proofrate (100 blocks): <code>{self.proofrate}</code> {trend}
├ Avg Block Time: <code>{self.avg_block_time}</code> <i>(target 2m 30s)</i>
├ Cadence: <code>{self.cadence_ratio}</code>
└ Latest Block: <code>{self.latest_block}</code>

🔗 <a href="https://nockblocks.com/metrics?tab=mining">View on NockBlocks</a>"""

    def format_asert_message(self) -> str:
        """ASERT difficulty-adjustment status (use with /asert)."""
        return f"""🎯 <b>ASERT Status</b>
<i>Block <code>{self.latest_block}</code></i>

├ Blocks since anchor: <code>{self.blocks_since_anchor:,}</code>
├ Time since anchor: <code>{self.time_since_anchor}</code>
├ Schedule drift: <code>{self.schedule_drift}</code>
├ Implied target factor: <code>{self.asert_target_factor}</code>
└ Half-life: <code>12h</code>

🔗 <a href="https://nockblocks.com/metrics?tab=mining">View on NockBlocks</a>"""

    def format_emissions_message(self) -> str:
        """Post-Aletheia emissions at current height (use with /emissions)."""
        return f"""💰 <b>Emissions</b>
<i>Block <code>{self.latest_block}</code></i>

├ Block reward: <code>{self.block_reward_nock:,} NOCK</code>
└ Issuance: <code>{self.nock_per_minute:,.1f} NOCK/min</code>

🔗 <a href="https://nockblocks.com/metrics?tab=mining">View on NockBlocks</a>"""


class NockBlocksAPI:
    """Client for NockBlocks JSON-RPC API."""

    BASE_URL = "https://nockblocks.com"
    RPC_V1_URL = f"{BASE_URL}/rpc/v1"

    ASERT_PHASE = 65_500
    ASERT_ANCHOR_HEIGHT = 65_499
    TARGET_BLOCK_TIME = 150  # seconds, post-Aletheia ideal
    ASERT_HALF_LIFE = 43_200  # 12h

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "NockchainMonitorBot/1.0",
            },
        )
        self._request_id = 0

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    def _next_id(self) -> int:
        """Get next request ID."""
        self._request_id += 1
        return self._request_id

    async def _rpc_call(self, method: str, params: list) -> Optional[dict]:
        """Make a JSON-RPC 2.0 call."""
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._next_id(),
            }
            response = await self.client.post(self.RPC_V1_URL, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                print(f"RPC error: {data['error']}")
                return None

            return data.get("result")
        except httpx.HTTPStatusError as e:
            print(f"HTTP error for {method}: {e.response.status_code}")
            return None
        except Exception as e:
            print(f"RPC call error for {method}: {e}")
            return None

    async def get_blocks_by_height(self, heights: list[int]) -> Optional[list[dict]]:
        """Get blocks by their heights."""
        return await self._rpc_call("getBlocksByHeight", [{"heights": heights}])

    async def get_tip(self) -> Optional[dict]:
        """Get the latest block (tip of the chain)."""
        return await self._rpc_call("getTip", [])

    async def get_blocks_by_timestamp_range(self, min_ts: int, max_ts: int) -> Optional[list[dict]]:
        """Get blocks within a timestamp range."""
        return await self._rpc_call(
            "getBlocksByTimestampRange", [{"minTimestamp": min_ts, "maxTimestamp": max_ts}]
        )

    async def get_transactions_by_block_height(self, height: int) -> Optional[list[dict]]:
        """Get transactions for a specific block height."""
        return await self._rpc_call("getTransactionsByBlockHeight", [{"height": height}])

    async def fetch_24h_volume(self) -> Optional[dict]:
        """Fetch 24-hour transaction volume."""
        import time

        now = int(time.time())
        day_ago = now - 86400

        blocks = await self.get_blocks_by_timestamp_range(day_ago, now)
        if not blocks:
            return None

        heights_with_txs = [b["height"] for b in blocks if b.get("txids")]

        total_volume = 0
        tx_count = 0

        for height in heights_with_txs:
            txs = await self.get_transactions_by_block_height(height)
            if not txs:
                continue

            for tx in txs:
                tx_count += 1
                for output in tx.get("outputs", []):
                    for seed in output.get("seeds", []):
                        if seed.get("isCoinbase", False):
                            continue
                        total_volume += seed.get("gift", 0)

        nock_volume = total_volume / 65_536

        return {
            "volume_nock": nock_volume,
            "tx_count": tx_count,
            "block_count": len(blocks),
        }

    async def fetch_metrics(self) -> Optional[MiningMetrics]:
        """Fetch mining metrics by analyzing recent blocks (post-ASERT)."""
        global _cached_asert_anchor
        try:
            latest_block = await self.get_tip()
            if not latest_block:
                print("Could not get chain tip")
                return None

            latest_height = latest_block.get("height", 0)
            if latest_height < self.ASERT_PHASE:
                print(f"Chain height {latest_height} is pre-ASERT; this build expects post-ASERT only")
                return None

            first_height = max(self.ASERT_PHASE, latest_height - 100)
            heights_to_fetch = sorted({first_height, self.ASERT_ANCHOR_HEIGHT})

            blocks_data = await self.get_blocks_by_height(heights_to_fetch)
            if not blocks_data:
                print("Could not fetch comparison blocks")
                return None

            blocks_by_height = {b["height"]: b for b in blocks_data if b}
            first_block = blocks_by_height.get(first_height)
            anchor_block = blocks_by_height.get(self.ASERT_ANCHOR_HEIGHT)

            if not first_block:
                print("Could not fetch 100-block comparison block")
                return None

            if anchor_block:
                _cached_asert_anchor = anchor_block
            elif _cached_asert_anchor:
                anchor_block = _cached_asert_anchor
            else:
                print("Could not fetch ASERT anchor block (height 65,499)")
                return None

            num_intervals = latest_height - first_height
            return self._calculate_metrics(
                first_block, latest_block, anchor_block, latest_height, num_intervals
            )

        except Exception as e:
            print(f"Error fetching metrics: {e}")
            import traceback

            traceback.print_exc()
            return None

    def _calculate_metrics(
        self,
        first_block: dict,
        latest_block: dict,
        anchor_block: dict,
        latest_height: int,
        num_intervals: int,
    ) -> MiningMetrics:
        """Calculate post-ASERT mining metrics from block data."""

        first_work = int(first_block.get("accumulatedWork", 0))
        latest_work = int(latest_block.get("accumulatedWork", 0))
        work_diff = latest_work - first_work

        if num_intervals > 0 and work_diff > 0:
            work_per_block = work_diff / num_intervals
            difficulty_exp = math.log2(work_per_block) if work_per_block > 0 else 0
            difficulty_str = f"2^{difficulty_exp:.1f}"
        else:
            difficulty_str = "N/A"
            work_per_block = 0

        latest_ts = int(latest_block.get("timestamp", 0))
        first_ts = int(first_block.get("timestamp", 0))
        time_diff_100 = latest_ts - first_ts
        avg_block_time_100 = (
            time_diff_100 / num_intervals if num_intervals > 0 and time_diff_100 > 0 else 0.0
        )

        if avg_block_time_100 > 0:
            minutes = int(avg_block_time_100 // 60)
            seconds = int(avg_block_time_100 % 60)
            avg_block_time_str = f"{minutes}m {seconds}s"
        else:
            avg_block_time_str = "N/A"

        if avg_block_time_100 > 0:
            cadence = self.TARGET_BLOCK_TIME / avg_block_time_100
            if cadence > 1.02:
                cadence_str = f"{cadence:.2f}× (faster)"
            elif cadence < 0.98:
                cadence_str = f"{cadence:.2f}× (slower)"
            else:
                cadence_str = f"~{cadence:.2f}× (on target)"
        else:
            cadence_str = "N/A"

        if work_per_block > 0 and avg_block_time_100 > 0:
            proofrate = work_per_block / avg_block_time_100
            proofrate_mps = proofrate / 1_000_000
        else:
            proofrate_mps = 0.0

        if proofrate_mps >= 1000:
            proofrate_str = f"{proofrate_mps / 1000:.2f} GP/s"
        elif proofrate_mps >= 1.0:
            proofrate_str = f"{proofrate_mps:.2f} MP/s"
        else:
            proofrate_str = f"{proofrate_mps * 1000:.2f} KP/s"

        # ASERT schedule drift uses the same time term as consensus exponent numerator:
        # time_diff - ideal * (height_diff - 1). We approximate anchor/parent min-ts with
        # raw block timestamps (RPC does not expose median-of-11).
        anchor_ts = int(anchor_block.get("timestamp", 0))
        blocks_since_anchor = latest_height - self.ASERT_ANCHOR_HEIGHT
        time_since_anchor_s = float(latest_ts - anchor_ts)

        if anchor_ts > 0 and latest_ts >= anchor_ts and blocks_since_anchor >= 1:
            expected_elapsed = self.TARGET_BLOCK_TIME * (blocks_since_anchor - 1)
            schedule_drift_s = time_since_anchor_s - expected_elapsed
            time_since_anchor_str = _format_signed_duration(time_since_anchor_s)
            if abs(schedule_drift_s) < 30:
                drift_str = "on schedule"
            elif schedule_drift_s > 0:
                drift_str = f"+{_format_signed_duration(schedule_drift_s)} (slow vs ideal)"
            else:
                drift_str = f"-{_format_signed_duration(schedule_drift_s)} (fast vs ideal)"

            exp_clamped = max(-30.0, min(30.0, schedule_drift_s / self.ASERT_HALF_LIFE))
            factor = 2.0**exp_clamped
            if abs(schedule_drift_s / self.ASERT_HALF_LIFE) > 30:
                factor_str = f"{factor:.2e}× (clamped display)"
            elif 0.99 <= factor <= 1.01:
                factor_str = "~1.00×"
            else:
                factor_str = f"{factor:.2f}×"
        else:
            time_since_anchor_str = "N/A"
            drift_str = "N/A"
            factor_str = "N/A"

        reward = block_reward_nock(latest_height)
        nock_per_min = reward * (60.0 / self.TARGET_BLOCK_TIME) if reward > 0 else 0.0

        return MiningMetrics(
            difficulty=difficulty_str,
            proofrate=proofrate_str,
            proofrate_value=proofrate_mps,
            avg_block_time=avg_block_time_str,
            cadence_ratio=cadence_str,
            latest_block=str(latest_height),
            latest_height=latest_height,
            blocks_since_anchor=blocks_since_anchor,
            time_since_anchor=time_since_anchor_str,
            schedule_drift=drift_str,
            asert_target_factor=factor_str,
            block_reward_nock=reward,
            nock_per_minute=nock_per_min,
        )


async def get_metrics() -> Optional[MiningMetrics]:
    """Get metrics using the NockBlocks API."""
    if not NOCKBLOCKS_API_KEY:
        print("Warning: NOCKBLOCKS_API_KEY not set")
        return None

    api = NockBlocksAPI(NOCKBLOCKS_API_KEY)
    try:
        return await api.fetch_metrics()
    finally:
        await api.close()


async def get_tip() -> Optional[dict]:
    """Get the latest block (chain tip)."""
    if not NOCKBLOCKS_API_KEY:
        print("Warning: NOCKBLOCKS_API_KEY not set")
        return None

    api = NockBlocksAPI(NOCKBLOCKS_API_KEY)
    try:
        return await api.get_tip()
    finally:
        await api.close()


async def get_24h_volume() -> Optional[dict]:
    """Get 24-hour transaction volume."""
    if not NOCKBLOCKS_API_KEY:
        print("Warning: NOCKBLOCKS_API_KEY not set")
        return None

    api = NockBlocksAPI(NOCKBLOCKS_API_KEY)
    try:
        return await api.fetch_24h_volume()
    finally:
        await api.close()


if __name__ == "__main__":
    async def test():
        print("Fetching Nockchain metrics...")
        metrics = await get_metrics()
        if metrics:
            msg = metrics.format_message()
            for tag in ["<b>", "</b>", "<code>", "</code>", "</a>", "<i>", "</i>"]:
                msg = msg.replace(tag, "")
            msg = re.sub(r"<a[^>]*>", "", msg)
            print(msg)
        else:
            print("Failed to fetch metrics")

    asyncio.run(test())

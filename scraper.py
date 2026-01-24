"""Scraper for NockBlocks metrics using RPC API."""
import asyncio
import re
import math
from dataclasses import dataclass
from typing import Optional

import httpx

from config import NOCKBLOCKS_API_KEY


@dataclass
class MiningMetrics:
    """Container for Nockchain mining metrics."""
    difficulty: str
    proofrate: str
    proofrate_value: float  # Numeric value in MP/s
    epoch_progress: str
    epoch_percentage: float
    blocks_to_adj: str
    est_time_to_adj: str
    avg_block_time: str
    next_adj_ratio: str
    latest_block: str
    
    def format_message(self, previous_proofrate: Optional[float] = None) -> str:
        """Format metrics as a readable Telegram message.
        
        Args:
            previous_proofrate: Previous proofrate value to compare against for trend.
        """
        # Determine trend emoji based on change from previous
        if previous_proofrate is None:
            trend = ""  # No previous data to compare
        else:
            change = self.proofrate_value - previous_proofrate
            pct_change = (change / previous_proofrate * 100) if previous_proofrate > 0 else 0
            
            if pct_change > 5:
                trend = "⬆️⬆️⬆️🚀"  # Up significantly
            elif pct_change > 0:
                trend = "⬆️↗"   # Up slightly
            elif pct_change > -5:
                trend = "⬇️"   # Down slightly
            else:
                trend = "‼️⬇️⬇️⬇️‼️"  # Down significantly
            
        return f"""⛏️ <b>Nockchain Mining Metrics</b>

<b>📊 Network Stats</b>
├ Difficulty: <code>{self.difficulty}</code>
├ Proofrate: <code>{self.proofrate}</code> {trend}
├ Avg Block Time: <code>{self.avg_block_time}</code>
└ Latest Block: <code>{self.latest_block}</code>

<b>📈 Epoch Progress</b>
├ Progress: <code>{self.epoch_progress}</code>
├ Blocks to Adj: <code>{self.blocks_to_adj}</code>
├ Est. Time to Adj: <code>{self.est_time_to_adj}</code>
└ Next Adj Ratio: <code>{self.next_adj_ratio}</code>

🔗 <a href="https://nockblocks.com/metrics?tab=mining">View on NockBlocks</a>"""


class NockBlocksAPI:
    """Client for NockBlocks JSON-RPC API."""
    
    BASE_URL = "https://nockblocks.com"
    RPC_V1_URL = f"{BASE_URL}/rpc/v1"
    
    # Nockchain constants
    BLOCKS_PER_EPOCH = 2016
    TARGET_BLOCK_TIME = 600  # 10 minutes in seconds
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "NockchainMonitorBot/1.0",
            }
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
                "id": self._next_id()
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
        return await self._rpc_call("getBlocksByTimestampRange", [{"minTimestamp": min_ts, "maxTimestamp": max_ts}])
    
    async def get_transactions_by_block_height(self, height: int) -> Optional[list[dict]]:
        """Get transactions for a specific block height."""
        return await self._rpc_call("getTransactionsByBlockHeight", [{"height": height}])
    
    async def fetch_24h_volume(self) -> Optional[dict]:
        """Fetch 24-hour transaction volume."""
        import time
        
        now = int(time.time())
        day_ago = now - 86400
        
        # Get blocks from last 24h
        blocks = await self.get_blocks_by_timestamp_range(day_ago, now)
        if not blocks:
            return None
        
        # Get heights of blocks with transactions
        heights_with_txs = [b['height'] for b in blocks if b.get('txids')]
        
        total_volume = 0
        tx_count = 0
        
        # Fetch transactions for each block
        for height in heights_with_txs:
            txs = await self.get_transactions_by_block_height(height)
            if not txs:
                continue
            
            for tx in txs:
                tx_count += 1
                for output in tx.get('outputs', []):
                    for seed in output.get('seeds', []):
                        # Skip coinbase outputs
                        if seed.get('isCoinbase', False):
                            continue
                        total_volume += seed.get('gift', 0)
        
        # Convert nicks to NOCK (1 NOCK = 2^16 = 65,536 nicks)
        nock_volume = total_volume / 65_536
        
        return {
            'volume_nock': nock_volume,
            'tx_count': tx_count,
            'block_count': len(blocks),
        }
    
    async def fetch_metrics(self) -> Optional[MiningMetrics]:
        """Fetch mining metrics by analyzing recent blocks."""
        try:
            # Get latest block using getTip
            latest_block = await self.get_tip()
            if not latest_block:
                print("Could not get chain tip")
                return None
            
            latest_height = latest_block.get("height", 0)
            if latest_height == 0:
                print("Invalid latest block height")
                return None
            
            # Fetch block from 100 blocks ago for comparison
            first_height = max(1, latest_height - 100)
            blocks_data = await self.get_blocks_by_height([first_height])
            
            if not blocks_data or len(blocks_data) < 1 or not blocks_data[0]:
                print("Could not fetch comparison block")
                return None
            
            first_block = blocks_data[0]
            
            # Number of block intervals between first and latest
            num_intervals = latest_height - first_height
            
            # Calculate metrics
            return self._calculate_metrics(first_block, latest_block, latest_height, num_intervals)
            
        except Exception as e:
            print(f"Error fetching metrics: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _calculate_metrics(
        self, 
        first_block: dict, 
        latest_block: dict, 
        latest_height: int,
        num_intervals: int
    ) -> MiningMetrics:
        """Calculate mining metrics from block data."""
        
        # Get timestamps and accumulated work
        first_ts = first_block.get("timestamp", 0)
        latest_ts = latest_block.get("timestamp", 0)
        first_work = int(first_block.get("accumulatedWork", 0))
        latest_work = int(latest_block.get("accumulatedWork", 0))
        
        # Calculate time and work differences
        time_diff = latest_ts - first_ts  # seconds
        work_diff = latest_work - first_work
        
        # Calculate average block time (time_diff / number of block intervals)
        if time_diff > 0 and num_intervals > 0:
            avg_block_time_seconds = time_diff / num_intervals
        else:
            avg_block_time_seconds = 0
        
        # Format average block time
        if avg_block_time_seconds > 0:
            minutes = int(avg_block_time_seconds // 60)
            seconds = int(avg_block_time_seconds % 60)
            avg_block_time_str = f"{minutes}m {seconds}s"
        else:
            avg_block_time_str = "N/A"
        
        # Calculate difficulty from work per block
        # Difficulty = average work per block
        if num_intervals > 0 and work_diff > 0:
            work_per_block = work_diff / num_intervals
            difficulty_exp = math.log2(work_per_block) if work_per_block > 0 else 0
            difficulty_str = f"2^{difficulty_exp:.1f}"
        else:
            difficulty_str = "N/A"
            difficulty_exp = 0
        
        # Calculate proofrate (work per second)
        if time_diff > 0 and work_diff > 0:
            proofrate = work_diff / time_diff  # proofs per second
            proofrate_mps = proofrate / 1_000_000  # MP/s
        else:
            proofrate_mps = 0.0
        
        # Format proofrate
        if proofrate_mps >= 1000:
            proofrate_str = f"{proofrate_mps / 1000:.2f} GP/s"
        elif proofrate_mps >= 0.01:
            proofrate_str = f"{proofrate_mps:.2f} MP/s"
        else:
            proofrate_str = f"{proofrate_mps * 1000:.2f} KP/s"
        
        # Calculate epoch progress
        epoch_counter = latest_block.get("epochCounter", 0)
        epoch_block = epoch_counter % self.BLOCKS_PER_EPOCH
        if epoch_block == 0 and epoch_counter > 0:
            epoch_block = self.BLOCKS_PER_EPOCH
        epoch_percentage = (epoch_block / self.BLOCKS_PER_EPOCH) * 100
        epoch_progress_str = f"{epoch_block}/{self.BLOCKS_PER_EPOCH} ({epoch_percentage:.1f}%)"
        
        # Blocks to difficulty adjustment
        blocks_to_adj = self.BLOCKS_PER_EPOCH - epoch_block
        
        # Estimated time to adjustment
        if avg_block_time_seconds > 0:
            est_seconds = blocks_to_adj * avg_block_time_seconds
            est_days = int(est_seconds // 86400)
            est_hours = int((est_seconds % 86400) // 3600)
            est_time_str = f"{est_days}d {est_hours}h"
        else:
            est_time_str = "N/A"
        
        # Next adjustment ratio (target_time / actual_avg_time)
        if avg_block_time_seconds > 0:
            next_adj_ratio = self.TARGET_BLOCK_TIME / avg_block_time_seconds
            next_adj_str = f"{next_adj_ratio:.3f}x"
        else:
            next_adj_str = "N/A"
        
        return MiningMetrics(
            difficulty=difficulty_str,
            proofrate=proofrate_str,
            proofrate_value=proofrate_mps,
            epoch_progress=epoch_progress_str,
            epoch_percentage=epoch_percentage,
            blocks_to_adj=str(blocks_to_adj),
            est_time_to_adj=est_time_str,
            avg_block_time=avg_block_time_str,
            next_adj_ratio=next_adj_str,
            latest_block=str(latest_height),
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


# Test
if __name__ == "__main__":
    async def test():
        print("Fetching Nockchain metrics...")
        metrics = await get_metrics()
        if metrics:
            # Clean HTML tags for console output
            msg = metrics.format_message()
            for tag in ["<b>", "</b>", "<code>", "</code>", "</a>"]:
                msg = msg.replace(tag, "")
            msg = re.sub(r'<a[^>]*>', '', msg)
            print(msg)
        else:
            print("Failed to fetch metrics")
    
    asyncio.run(test())

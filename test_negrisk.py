#!/usr/bin/env python3
"""
Negrisk Arbitrage Test
=======================

Simple test script to verify neg-risk arbitrage detection works.

Tests:
- Event discovery from Gamma API
- BBA tracking via WebSocket
- Opportunity detection
- No execution (read-only)
"""

import asyncio
import logging
import sys
from datetime import datetime

from core.negrisk.models import NegriskConfig
from core.negrisk.registry import NegriskRegistry
from core.negrisk.bba_tracker import BBATracker
from core.negrisk.detector import NegriskDetector


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def test_negrisk():
    """Test neg-risk arbitrage detection."""

    print("=" * 80)
    print("NEGRISK ARBITRAGE TEST")
    print("=" * 80)
    print()

    # Create config
    config = NegriskConfig(
        min_net_edge=0.020,           # 2.0% minimum net edge (relaxed for testing)
        min_outcomes=3,               # At least 3 outcomes
        max_legs=15,                  # Max 15 outcomes per bundle
        staleness_ttl_ms=60000.0,     # 60 second staleness (realistic for prediction markets)
        taker_fee_bps=150,            # 1.5% taker fee
        gas_per_leg=0.05,             # $0.05 gas per leg
        min_liquidity_per_outcome=50.0,  # $50 min (relaxed for testing)
        min_event_volume_24h=5000.0,     # $5k min volume (relaxed for testing)
        max_position_per_event=500.0,
        skip_augmented_placeholders=True,
    )

    print(f"Config: min_net_edge={config.min_net_edge*100:.1f}%, "
          f"min_liquidity=${config.min_liquidity_per_outcome:.0f}, "
          f"min_volume=${config.min_event_volume_24h:.0f}")
    print()

    # Initialize components
    registry = NegriskRegistry(config)
    detector = NegriskDetector(config)

    print("Starting registry...")
    await registry.start()

    # Wait for initial events
    print("Waiting 5 seconds for event discovery...")
    await asyncio.sleep(5)

    # Show registry stats
    reg_stats = registry.get_stats()
    print()
    print("REGISTRY STATS:")
    print(f"  Events discovered: {reg_stats['events_tracked']}")
    print(f"  Tradeable events:  {len(registry.get_tradeable_events())}")
    print(f"  Total tokens:      {len(registry.get_all_token_ids())}")
    print()

    # Get all events
    all_events = registry.get_all_events()
    if all_events:
        print("SAMPLE EVENTS:")
        for i, event in enumerate(all_events[:5]):
            print(f"  {i+1}. {event.title[:70]}")
            print(f"      Outcomes: {event.outcome_count}, Volume 24h: ${event.volume_24h:,.0f}")
        print()

    # Start BBA tracker
    print("Starting BBA tracker (WebSocket)...")

    def on_price_update(event_id: str, token_id: str):
        """Callback for price updates."""
        pass  # Just track internally

    tracker = BBATracker(
        registry=registry,
        config=config,
        on_price_update=on_price_update,
    )
    await tracker.start()

    # Wait for WebSocket to connect
    print("Waiting 5 seconds for WebSocket connection...")
    await asyncio.sleep(5)

    # Seed initial BBA data for top events
    print("Seeding BBA data for top 30 events...")
    all_events = registry.get_all_events()
    sorted_events = sorted(all_events, key=lambda e: e.volume_24h, reverse=True)
    top_events = sorted_events[:30]

    for i, event in enumerate(top_events):
        await tracker.fetch_all_prices(event)
        if (i + 1) % 10 == 0:
            print(f"  Seeded {i + 1}/{len(top_events)} events...")

    print("BBA seeding complete. Waiting 5 more seconds for WebSocket data...")
    await asyncio.sleep(5)

    # Show tracker stats
    tracker_stats = tracker.get_stats()
    print()
    print("PRICE TRACKING STATS:")
    print(f"  WS messages received: {tracker_stats.get('ws_messages', 0)}")
    print(f"  CLOB fetches:         {tracker_stats.get('clob_fetches', 0)}")
    print(f"  Sequence gaps:        {tracker_stats.get('sequence_gaps', 0)}")
    print(f"  Tokens tracked:       {tracker_stats.get('tokens_tracked', 0)}")
    print()

    # Scan for opportunities
    print("Scanning for opportunities...")
    tradeable_events = registry.get_tradeable_events()
    print(f"Checking {len(tradeable_events)} tradeable events...")
    print()

    opportunities = detector.detect_opportunities(tradeable_events)

    # Show detector stats
    det_stats = detector.get_stats_dict()
    print("DETECTION STATS:")
    print(f"  Opportunities detected: {det_stats['opportunities_detected']}")
    print(f"  Best edge seen:         {det_stats['best_edge_seen']:.4f} ({det_stats['best_edge_seen']*100:.2f}%)")
    if det_stats['best_edge_event']:
        print(f"  Best event:             {det_stats['best_edge_event'][:70]}")
    print()
    print(f"  Stale data rejections:  {det_stats['stale_data_rejections']}")
    print(f"  Liquidity rejections:   {det_stats['liquidity_rejections']}")
    print()

    # Show opportunities
    if opportunities:
        print(f"FOUND {len(opportunities)} OPPORTUNITIES:")
        print()
        for i, opp in enumerate(opportunities):
            print(f"  {i+1}. {opp.event.title[:70]}")
            print(f"      Sum of Asks:  {opp.sum_of_asks:.4f}")
            print(f"      Gross Edge:   {opp.gross_edge:.4f} ({opp.gross_edge*100:.2f}%)")
            print(f"      Net Edge:     {opp.net_edge:.4f} ({opp.net_edge*100:.2f}%)")
            print(f"      Legs:         {opp.num_legs}")
            print(f"      Size:         {opp.suggested_size:.2f} shares")
            print(f"      Total Cost:   ${opp.total_cost:.2f}")
            print(f"      Est. Profit:  ${opp.expected_profit:.2f}")
            print()

            # Show legs
            print(f"      Legs:")
            for j, leg in enumerate(opp.legs):
                print(f"        {j+1}. {leg['outcome_name'][:40]:<40} @ ${leg['price']:.4f}")
            print()
    else:
        print("No opportunities found at current edge threshold.")
        print()
        print("Try relaxing parameters:")
        print("  - Lower min_net_edge (currently 2.0%)")
        print("  - Lower min_liquidity_per_outcome")
        print("  - Lower min_event_volume_24h")
        print()

    # Show some events with prices
    print("SAMPLE EVENTS WITH PRICES:")
    sample_events = tradeable_events[:3]
    for event in sample_events:
        print(f"\n  Event: {event.title[:70]}")
        print(f"  Outcomes: {event.outcome_count}, Sum of Asks: {event.sum_of_asks}")
        for outcome in event.active_outcomes:
            ask = outcome.bba.best_ask
            if ask:
                print(f"    - {outcome.name[:40]:<40} Ask: ${ask:.4f}")
    print()

    # Cleanup
    print("Stopping tracker...")
    await tracker.stop()

    print("Stopping registry...")
    await registry.stop()

    print()
    print("=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    try:
        asyncio.run(test_negrisk())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)

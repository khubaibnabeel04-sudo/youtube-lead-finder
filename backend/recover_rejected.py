"""
One-time recovery script: re-checks all previously rejected channels
against the fixed word-boundary negative_hit filter.
Channels that now pass all filters get added back to channels_found.

Run this while the backend is stopped:
  cd backend && python recover_rejected.py

This reuses main.run_filters() directly (skip_rejected_check=True) so the
recovery pass always matches whatever the live validation pipeline does —
no separate copy of the filter logic to keep in sync.
"""
import asyncio
import httpx
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

# Import all the pieces we need from main
from main import (
    state, sheet_dedup, save_state, load_state,
    get_full_meta, keys, run_filters, MIN_NICHE_SCORE,
)


async def recover():
    print("=" * 60)
    print("  Rejected Channel Recovery Script")
    print("  Re-checks ALL rejected UC... IDs against fixed word-boundary filters")
    print("  Skips: previously_rejected check, already_in_sheets, in_blocklist")
    print("=" * 60)

    load_state()

    try:
        result = sheet_dedup.refresh()
        print(f"  Sheets loaded: {result.get('ids', 0)} known IDs, "
              f"{result.get('names', 0)} names, {result.get('urls', 0)} URLs")
    except Exception as e:
        print(f"  WARNING: Could not load Google Sheets: {e}")
        print("  Continuing with state.json data only (no dedup against sheets)")

    rejected_ids = [x for x in state.rejected if isinstance(x, str) and x.startswith("UC")]

    # Remove IDs already present in active lists
    existing = set()
    for ch in state.channels_found + state.accepted + state.borderline:
        existing.add(ch["id"])

    to_check = [cid for cid in rejected_ids if cid not in existing]

    print(f"\n  Total rejected items:         {len(state.rejected)}")
    print(f"  UC channel IDs in rejected:  {len(rejected_ids)}")
    print(f"  Already in active lists:     {len(existing)}")
    print(f"  To re-check:                 {len(to_check)}")
    print()

    if not to_check:
        print("  Nothing to re-check. Exiting.")
        return

    recovered = 0
    failed = 0
    quota_ok = True

    async with httpx.AsyncClient() as client:
        for i, cid in enumerate(to_check):
            if not quota_ok:
                break

            if keys.available_keys() == 0:
                print(f"\n  [QUOTA] All API keys exhausted at {i}/{len(to_check)}. Stopping.")
                quota_ok = False
                break

            if sheet_dedup.is_known(channel_id=cid):
                failed += 1
                continue

            try:
                meta = await get_full_meta(client, cid)
            except Exception as e:
                print(f"  [{i+1}/{len(to_check)}] SKIP {cid[:20]}... fetch error: {str(e)[:50]}")
                failed += 1
                await asyncio.sleep(0.5)
                continue

            if not meta:
                print(f"  [{i+1}/{len(to_check)}] SKIP {cid[:20]}... meta empty)")
                failed += 1
                continue

            # Same pipeline as live validation, just skipping "previously_rejected"
            result, reason, score = run_filters(meta, skip_rejected_check=True)

            if result in ("pass", "borderline"):
                channel_data = {
                    "id":          meta["channel_id"],
                    "name":        meta["channel_name"],
                    "url":         meta["channel_url"],
                    "subscribers": meta["subscriber_count"],
                    "description": meta["description"][:250] + ("..." if len(meta["description"]) > 250 else ""),
                    "uploadDate":  meta["last_upload_date"],
                    "thumbnail":   meta.get("thumbnail"),
                    "source":      "recovered",
                    "niche_score": score,
                    "timestamp":   datetime.now().isoformat(),
                    "selected":    False,
                }

                bucket = state.borderline if result == "borderline" else state.channels_found
                bucket.append(channel_data)
                state.all_seen.add(cid)
                state.rejected.discard(cid)
                state.rejected.discard(meta["channel_name"].lower())

                recovered += 1
                print(f"  [{i+1}/{len(to_check)}] RECOVERED {meta['channel_name'][:35]:35s} "
                      f"({meta['subscriber_count']:,} subs) -> {result} (score={score})")
            else:
                failed += 1

            if (i + 1) % 50 == 0:
                print(f"  ... progress: {i+1}/{len(to_check)} checked, "
                      f"{recovered} recovered, {failed} still failing")

            await asyncio.sleep(0.3)

    save_state()

    print(f"\n  {'='*50}")
    print(f"  Recovery complete!")
    print(f"  Checked:   {len(to_check)}")
    print(f"  Recovered: {recovered}")
    print(f"  Still rejected: {failed}")
    print(f"  {'='*50}")


if __name__ == "__main__":
    asyncio.run(recover())

"""
One-time cleanup: cross-checks channels that were added by recover_rejected.py
(source == "recovered") against the Google Sheet, now that the service-account
key is available. Removes any that are already present in the sheet (moves them
back to rejected instead of just deleting, so they don't get re-recovered later).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from main import state, sheet_dedup, save_state, load_state


def main():
    load_state()

    result = sheet_dedup.refresh()
    if result.get("error"):
        print(f"ERROR loading sheet: {result['error']}")
        return
    print(f"Sheet loaded: {result['ids']} IDs, {result['names']} names, {result['urls']} URLs")

    removed = []

    for bucket_name in ("channels_found", "borderline"):
        bucket = getattr(state, bucket_name)
        keep = []
        for ch in bucket:
            if ch.get("source") != "recovered":
                keep.append(ch)
                continue
            if sheet_dedup.is_known(channel_id=ch["id"], name=ch["name"].lower(), url=ch.get("url", "")):
                removed.append((bucket_name, ch))
            else:
                keep.append(ch)
        setattr(state, bucket_name, keep)

    for _, ch in removed:
        state.rejected.add(ch["id"])
        state.rejected.add(ch["name"].lower())

    save_state()

    print(f"\nChecked recovered channels against sheet.")
    print(f"Duplicates removed: {len(removed)}")
    for bucket_name, ch in removed:
        print(f"  [{bucket_name}] {ch['name']} ({ch['id']})")

    remaining_cf = len([c for c in state.channels_found if c.get('source') == 'recovered'])
    remaining_bl = len([c for c in state.borderline if c.get('source') == 'recovered'])
    print(f"\nRecovered channels remaining after dedup: {remaining_cf + remaining_bl} "
          f"({remaining_cf} in channels_found, {remaining_bl} in borderline)")

    rejected_ids = [x for x in state.rejected if isinstance(x, str) and x.startswith("UC")]
    existing = set()
    for ch in state.channels_found + state.accepted + state.borderline:
        existing.add(ch["id"])
    to_check = [cid for cid in rejected_ids if cid not in existing]
    print(f"\nRemaining rejected channels still needing a recovery check: {len(to_check)}")


if __name__ == "__main__":
    main()

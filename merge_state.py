"""Merge two state.json files (ours + origin) without losing postings.

Used by the workflow's Save-state step when a push is rejected as
non-fast-forward (another run pushed first at 1-minute cadence).
A textual git merge of JSON almost always conflicts — both runs
append to the same arrays — so the merge is semantic:

- "seen", "seen_urls", "ipo_posted": ordered union.
- "archive": one entry per filing (keyed by PDF basename, the
  same urlkey watcher.filing_url_key computes); when both sides
  hold the same filing, a real card beats a "Results Filed"
  placeholder, otherwise the first entry wins. A merged archive
  can therefore never cause a repost: every posted filing stays
  recorded exactly once.
- "digest": merged recursively. Lists union by JSON form
  (text_posts union by entry key). Scalar flags: a value on
  either side beats a missing value (a flag set by the other run
  must survive); when both sides set the same flag, the
  lexicographically larger value wins, which is correct for the
  ISO dates and hour keys every flag uses.
- Anything else: ours wins when present, else origin's.

Usage: python merge_state.py ours.json origin.json > merged.json
Never invents data: every value in the output came from one of
the two inputs.
"""

import json
import sys


def _urlkey(filing):
    if not filing:
        return ""
    return (filing.split("?")[0].rstrip("/").rsplit("/", 1)[-1]).lower()


def _merge_lists(key, ours, origin):
    if key in ("seen", "seen_urls", "ipo_posted"):
        out = list(ours)
        for v in origin:
            if v not in out:
                out.append(v)
        return out
    if key == "archive":
        by_key = {}
        order = []
        for e in ours + origin:
            k = _urlkey(e.get("filing") or "") or json.dumps(e, sort_keys=True)
            if k not in by_key:
                by_key[k] = e
                order.append(k)
            elif (by_key[k].get("r") == "Results Filed"
                    and e.get("r") != "Results Filed"):
                by_key[k] = e  # a card beats a placeholder
        return [by_key[k] for k in order]
    if key == "text_posts":
        by_key = {}
        order = []
        for e in ours + origin:
            k = e.get("key") or json.dumps(e, sort_keys=True)
            if k not in by_key:
                by_key[k] = e
                order.append(k)
        return [by_key[k] for k in order]
    out = list(ours)
    for v in origin:
        if v not in out:
            out.append(v)
    return out


def merge(ours, origin):
    if isinstance(ours, dict) and isinstance(origin, dict):
        out = {}
        for k in set(ours) | set(origin):
            if k in ours and k in origin:
                out[k] = merge_values(k, ours[k], origin[k])
            elif k in ours:
                out[k] = ours[k]
            else:
                out[k] = origin[k]
        return out
    if isinstance(ours, list) and isinstance(origin, list):
        return _merge_lists("", ours, origin)
    return ours if ours is not None else origin


def merge_values(key, ours, origin):
    if isinstance(ours, dict) and isinstance(origin, dict):
        return merge(ours, origin)
    if isinstance(ours, list) and isinstance(origin, list):
        return _merge_lists(key, ours, origin)
    if ours is None:
        return origin
    if origin is None:
        return ours
    if isinstance(ours, str) and isinstance(origin, str):
        return max(ours, origin)
    return ours


def main():
    with open(sys.argv[1]) as fh:
        ours = json.load(fh)
    with open(sys.argv[2]) as fh:
        origin = json.load(fh)
    json.dump(merge(ours, origin), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

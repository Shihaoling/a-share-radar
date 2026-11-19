"""Build the public sector directory from saved SXData index_basic and rt_idx_k JSONL replies."""
import argparse
from datetime import date
import json
import math
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from sector_descriptions import checked_intro, exchange_intro, ths_intro


EXCHANGES = {"SSE": "SH", "SZSE": "SZ"}
KINDS = {"行业指数": "industry", "主题指数": "theme"}
TARGET = Path(__file__).resolve().parents[1] / "web/data/sector-catalog.json"


def build_catalog(paths, day):
    definitions = {}
    snapshots = {}
    directory_pages = {market: {} for market in EXCHANGES}
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line in source:
                reply = json.loads(line)
                if reply["status"] != "ok" or reply["received_at"][:10] != day:
                    continue
                params = reply["params"]
                if reply["api"] == "index_basic" and params.get("market") in EXCHANGES:
                    if params.keys() - {"market", "limit", "offset"}:
                        continue
                    market = params["market"]
                    rows = reply["rows"]
                    directory_pages[market][int(params.get("offset", 0))] = (
                        len(rows), int(params.get("limit", 8000)))
                    for row in rows:
                        if row["market"] == market and row["ts_code"].endswith("." + EXCHANGES[market]):
                            definitions[row["ts_code"]] = row
                elif reply["api"] == "rt_idx_k" and params in ({"ts_code": "*.SH"}, {"ts_code": "*.SZ"}):
                    exchange = params["ts_code"][-2:]
                    samples = snapshots.setdefault(exchange, {})
                    for row in reply["rows"]:
                        code = row["ts_code"]
                        stamp = row.get("trade_time")
                        prices = (row.get("close"), row.get("pre_close"))
                        if (re.fullmatch(r"\d{6}\." + exchange, code)
                                and isinstance(stamp, str) and stamp[:10] == day
                                and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in prices)
                                and stamp >= samples.get(code, {}).get("trade_time", "")):
                            samples[code] = row

    items = []
    for market, exchange in EXCHANGES.items():
        pages = directory_pages[market]
        offset = 0
        while offset in pages:
            count, limit = pages[offset]
            if count < limit and limit > 0:
                break
            if count == 0 or count != limit:
                raise ValueError(f"Invalid {market} catalog page at {offset}")
            offset += count
        else:
            raise ValueError(f"Incomplete {market} catalog, missing offset {offset}")
        matched = 0
        for code, sample in snapshots.get(exchange, {}).items():
            definition = definitions.get(code, {})
            kind = KINDS.get(definition.get("category"))
            if not kind:
                continue
            name = definition["name"].strip()
            fullname = definition["fullname"].strip()
            quote_name = sample["name"].strip()
            if not name or not fullname or not quote_name:
                raise ValueError(f"Missing source name for {code}")
            item = dict(code=code, name=name, fullname=fullname, quote_name=quote_name, kind=kind)
            if "芯片" in fullname or "半导体" in fullname:
                item["keywords"] = ["芯片", "半导体", "集成电路"]
            elif "人工智能" in fullname:
                item["keywords"] = ["人工智能", "AI"]
            item["intro"] = checked_intro(exchange_intro(item))
            items.append(item)
            matched += 1
        if not matched:
            raise ValueError(f"No same-day sector quotes for {market}")
    return {"schema_version": 1, "as_of": day, "items": sorted(items, key=lambda item: item["code"])}


def build_research_catalog(paths, day):
    ths, sw = {}, {}
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line in source:
                reply = json.loads(line)
                if reply["status"] != "ok" or reply["received_at"][:10] != day:
                    continue
                if reply["api"] == "ths_index" and reply["params"] == {"exchange": "A"}:
                    for row in reply["rows"]:
                        code, name, kind = row["ts_code"], row["name"].strip(), row["type"]
                        if row["exchange"] != "A" or not re.fullmatch(r"\d{6}R?\.TI", code) or kind not in ("I", "N", "TH"):
                            continue
                        item = dict(code=code, name=name, kind=kind, intro=checked_intro(ths_intro(name, kind)))
                        count = row.get("count")
                        if type(count) in (int, float) and math.isfinite(count) and count >= 0 and int(count) == count:
                            item["member_count"] = int(count)
                        ths[code] = item
                elif reply["api"] == "index_classify" and reply["params"] == {"src": "SW2021", "level": "L1"}:
                    for row in reply["rows"]:
                        code, name = row["index_code"], row["industry_name"].strip()
                        if row["src"] == "SW2021" and row["level"] == "L1" and re.fullmatch(r"\d{6}\.SI", code):
                            sw[code] = dict(code=code, name=name, kind="L1", intro=checked_intro(ths_intro(name, "I")))
    if not sw or {item["kind"] for item in ths.values()} != {"I", "N", "TH"}:
        raise ValueError("Missing SW L1 or A-share THS industry, concept or theme catalog")
    return {"schema_version": 1, "as_of": day, "items": [ths[k] for k in sorted(ths)],
            "sw_items": [sw[k] for k in sorted(sw)]}


def write_catalog(catalog, output):
    # Keep each index on one line so that renames and membership changes have a short diff.
    groups = []
    for field in ("items", "sw_items"):
        if field in catalog:
            rows = ",\n".join("    " + json.dumps(item, ensure_ascii=False) for item in catalog[field])
            groups.append(f'  "{field}": [\n{rows}\n  ]')
    with NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False) as handle:
        handle.write('{\n  "schema_version": 1,\n  "as_of": "' + catalog["as_of"] + '",\n')
        handle.write(",\n".join(groups) + "\n}\n")
    temporary = Path(handle.name)
    temporary.chmod(0o644)
    temporary.replace(output)
    print(f"{len(catalog['items'])} symbols -> {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replies", type=Path, nargs="+", help="Saved JSONL replies from the catalog audit")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="Shanghai trading date")
    parser.add_argument("--output", type=Path, default=TARGET)
    parser.add_argument("--ths-output", type=Path, help="Also build the THS and SW reference directory")
    args = parser.parse_args()
    day = args.date.isoformat()
    catalog = build_catalog(args.replies, day)
    if args.ths_output:
        research = build_research_catalog(args.replies, day)
        write_catalog(research, args.ths_output)
    write_catalog(catalog, args.output)


if __name__ == "__main__":
    main()

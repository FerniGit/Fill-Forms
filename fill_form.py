#!/usr/bin/env python3

import json
import sys
import os
import re
import subprocess
from datetime import datetime
import tkinter as tk
from tkinter import filedialog

# ======================================================================
#   JSON HELPERS (with sanitization)
# ======================================================================

# Matches interleaved log-framework lines that the server injects mid-JSON.
# Example:
#   2025-12-11 20:42:10.908 shq-app-shipperws/auto-deploy-app DEBUG 1 --- [...] com.shq.ws: ...
_INTERLEAVED_LOG_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} '
)


def strip_interleaved_log_noise(lines):
    """
    Pre-process a list of lines to remove interleaved log-framework noise.

    ShipperHQ logs can inject timestamped debug lines (and their multi-line
    Java toString() continuations) right in the middle of a JSON payload.
    This function strips those noise lines and, when the noise causes JSON
    truncation (open braces never closed), auto-closes them so the result
    can be parsed as valid JSON.

    Returns a new list of cleaned lines.
    """
    noise_mode = False
    cleaned = []
    # Stack tracks open JSON braces/brackets: '{' or '['
    stack = []

    for line in lines:
        stripped = line.lstrip()

        # Detect timestamp-prefixed log lines
        if _INTERLEAVED_LOG_RE.match(stripped):
            noise_mode = True
            continue

        if noise_mode:
            if not stripped:
                # Empty line inside a noise block — skip
                continue
            if stripped.startswith('"'):
                # A JSON key line — noise is over, JSON content resumes
                noise_mode = False
                cleaned.append(line)
                for ch in line:
                    if ch == '{':
                        stack.append('{')
                    elif ch == '[':
                        stack.append('[')
                    elif ch == '}' and stack and stack[-1] == '{':
                        stack.pop()
                    elif ch == ']' and stack and stack[-1] == '[':
                        stack.pop()
            # else: Java toString continuation or ambiguous — skip
            continue

        # Normal (non-noise) line
        cleaned.append(line)
        for ch in line:
            if ch == '{':
                stack.append('{')
            elif ch == '[':
                stack.append('[')
            elif ch == '}' and stack and stack[-1] == '{':
                stack.pop()
            elif ch == ']' and stack and stack[-1] == '[':
                stack.pop()

    # If noise stripping left unclosed braces/brackets, auto-close them
    # so the JSON is still parseable (the truncated fields will just be empty).
    if stack:
        # Remove trailing comma on the last real line if present
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            last = cleaned[-1].rstrip()
            if last.endswith(','):
                cleaned[-1] = last[:-1]

        while stack:
            item = stack.pop()
            cleaned.append('}' if item == '{' else ']')

    return cleaned

def sanitize_json_block(block: str) -> str:
    """
    Sanitizes JSON without breaking formatting.
    - Removes ANSI sequences
    - Removes illegal control chars
    - Escapes newline or carriage return *inside* string literals
    """

    # Remove ANSI escape sequences
    block = re.sub(r'\x1B[@-_][0-?]*[ -/]*[@-~]', '', block)

    result = []
    in_string = False
    escape_next = False

    for ch in block:
        if escape_next:
            # Just append escaped character literally
            result.append(ch)
            escape_next = False
            continue

        if ch == '\\':
            result.append(ch)
            escape_next = True
            continue

        # Detect string toggle
        if ch == '"':
            result.append(ch)
            in_string = not in_string
            continue

        if in_string:
            # Inside a string → escape forbidden chars
            if ch == '\n':
                result.append('\\n')
                continue
            if ch == '\r':
                result.append('\\r')
                continue
            if ord(ch) < 32:
                # Any other control char becomes a space
                result.append(' ')
                continue

        else:
            # Outside string → remove CR entirely, keep newlines
            if ch == '\r':
                continue
            if ord(ch) < 32 and ch not in ['\n', '\t']:
                continue

        # Normal char
        result.append(ch)

    return ''.join(result)


def collect_json_from_line_list(lines, start_index):
    """
    JSON collector:
    - Pre-process lines to strip interleaved log-framework noise.
    - Scan forward until the FIRST line beginning with '{' (ignoring leading spaces).
    - Perform brace-counting until the JSON object is complete.
    - Sanitize control characters.
    - Return parsed dict or None.
    """
    # Pre-clean lines from start_index onward to strip interleaved noise
    cleaned_lines = strip_interleaved_log_noise(lines[start_index:])

    n = len(cleaned_lines)
    found_start = None
    first_line = None

    for j in range(n):
        stripped = cleaned_lines[j].lstrip()
        if stripped.startswith("{"):
            found_start = j
            first_line = stripped
            break

    if found_start is None:
        return None

    brace_count = 0
    collected = []
    started = False

    for j in range(found_start, n):
        if j == found_start:
            line = first_line
        else:
            line = cleaned_lines[j]

        collected.append(line)
        brace_count += line.count("{")
        brace_count -= line.count("}")

        if "{" in line:
            started = True

        if started and brace_count == 0:
            raw_block = "\n".join(collected)
            sanitized = sanitize_json_block(raw_block)
            try:
                return json.loads(sanitized)
            except Exception as e:
                # Helpful debug, but non-fatal (caller will try other markers)
                print("JSON parse error:", e)
                print("Faulty sanitized block:")
                print(sanitized[:400])
                return None

    return None


def extract_json_after_marker(lines, marker, require_keys=None, first=True):
    """
    Find JSON immediately following a line containing `marker`.
    Handles both:
      line: ".... marker: { ... }"
      line: ".... marker:" + next line "{"
    If require_keys is given, JSON must contain all of those top-level keys.
    `first=True` => return first match, otherwise last match.
    """
    indices = []
    for i, line in enumerate(lines):
        if marker in line:
            indices.append(i)

    if not indices:
        return None

    if not first:
        indices = reversed(indices)

    for i in indices:
        line = lines[i]
        if "{" in line:
            # JSON starts on same line after the first '{'
            brace_part = line[line.index("{") :]
            block_lines = [brace_part] + lines[i + 1 :]
            obj = collect_json_from_line_list(block_lines, 0)
        else:
            obj = collect_json_from_line_list(lines, i + 1)

        if obj is None:
            continue

        if require_keys:
            if not all(k in obj for k in require_keys):
                continue

        return obj

    return None


def json_contains_carrier_groups(obj) -> bool:
    """Check if JSON contains 'carrierGroups' anywhere."""
    if isinstance(obj, dict):
        if "carrierGroups" in obj:
            return True
        return any(json_contains_carrier_groups(v) for v in obj.values())
    if isinstance(obj, list):
        return any(json_contains_carrier_groups(v) for v in obj)
    return False


# ======================================================================
#   LOG PARSING (REQUEST / RESPONSE)
# ======================================================================

_LOG_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)')


def extract_log_timestamp(lines):
    """Return the earliest timestamp from a [NO SESSION FOUND] line, or None."""
    for line in lines:
        if "[NO SESSION FOUND]" in line:
            m = _LOG_TS_RE.match(line)
            if m:
                return m.group(1)
    return None


def parse_log_file(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    lines = text.splitlines()

    # -------------------------------
    # REQUEST: strict priority order
    # -------------------------------

    request_json = None

    # 1) Shopify
    if request_json is None:
        request_json = extract_json_after_marker(
            lines,
            "Converted shopify request to",
            require_keys=["cart", "destination"],
            first=True,
        )

    # 2) BigCommerce
    if request_json is None:
        request_json = extract_json_after_marker(
            lines,
            "Converted bigcommerce request to",
            require_keys=["cart", "destination"],
            first=True,
        )

    # 3) Magento 2 GraphQL
    if request_json is None:
        request_json = extract_json_after_marker(
            lines,
            "REST Request after GraphQL Unmarshalling",
            require_keys=["cart"],
            first=True,
        )

    # 4) Magento fallback [REQUEST]:
    if request_json is None:
        request_json = extract_json_after_marker(
            lines,
            "[REQUEST]:",
            require_keys=["cart"],
            first=True,
        )

    # 5) Generic converted request
    if request_json is None:
        for i, line in enumerate(lines):
            if (
                "Converted" in line
                and "request to" in line
                and "shopify" not in line.lower()
                and "bigcommerce" not in line.lower()
            ):
                obj = None
                if "{" in line:
                    brace_part = line[line.index("{") :]
                    block_lines = [brace_part] + lines[i + 1 :]
                    obj = collect_json_from_line_list(block_lines, 0)
                else:
                    obj = collect_json_from_line_list(lines, i + 1)
                if obj and "cart" in obj and "destination" in obj:
                    request_json = obj
                    break

    if request_json is None:
        raise ValueError("Could not extract request JSON from this log.")

    # -------------------------------
    # RESPONSE: strict priority order
    # -------------------------------

    response_json = None

    # 1) Shopify adapted response
    if response_json is None:
        response_json = extract_json_after_marker(
            lines,
            "Adapting the following rate response to shopify response",
            first=False,
        )
        if response_json and not json_contains_carrier_groups(response_json):
            response_json = None

    # 2) BigCommerce adapted response
    if response_json is None:
        response_json = extract_json_after_marker(
            lines,
            "Adapting the following rate response to bigcommerce response",
            first=False,
        )
        if response_json and not json_contains_carrier_groups(response_json):
            response_json = None

    # 3) Magento 2 GraphQL response
    if response_json is None:
        response_json = extract_json_after_marker(
            lines,
            "REST Response before GraphQL Marshalling",
            first=True,
        )
        if response_json and not json_contains_carrier_groups(response_json):
            response_json = None

    # 4) Magento fallback [RESPONSE]:
    if response_json is None:
        response_json = extract_json_after_marker(
            lines,
            "[RESPONSE]:",
            first=True,
        )
        if response_json and not json_contains_carrier_groups(response_json):
            response_json = None

    # 5) Generic adapted response
    if response_json is None:
        for i, line in enumerate(lines):
            if "Adapting the following rate response to" in line:
                if "shopify response" in line.lower() or "bigcommerce response" in line.lower():
                    continue
                obj = None
                if "{" in line:
                    brace_part = line[line.index("{") :]
                    block_lines = [brace_part] + lines[i + 1 :]
                    obj = collect_json_from_line_list(block_lines, 0)
                else:
                    obj = collect_json_from_line_list(lines, i + 1)
                if obj and json_contains_carrier_groups(obj):
                    response_json = obj
                    break

    if response_json is None:
        raise ValueError("Could not extract response JSON from this log.")

    # -------------------------------
    # PLATFORM IDENTIFICATION
    # -------------------------------
    platform = "Unknown"
    site = request_json.get("siteDetails")
    if isinstance(site, dict):
        platform = site.get("ecommerceCart") or "Unknown"

    result = extract_fields(request_json, response_json)
    result["Platform"] = platform
    result["LogTimestamp"] = extract_log_timestamp(lines)
    return result


# ======================================================================
#   FIELD EXTRACTION
# ======================================================================

def extract_fields(request_json, response_json):
    # Ship From
    try:
        ship_from = response_json["carrierGroups"][0]["carrierGroupDetail"]["originAddress"]
    except Exception:
        ship_from = {}

    # Ship To
    try:
        ship_to = request_json["destination"]
    except Exception:
        ship_to = {}

    # Cart
    cart_items = []

    def _extract_item_attrs(item):
        """Pull dimensions, shipping group, and packing rules from an item's attributes."""
        length = width = height = None
        shipping_group = "N/A"
        dim_group = "N/A"
        for attr in item.get("attributes", []):
            name = (attr.get("name") or "").lower()
            vals = attr.get("values")
            val = attr.get("value")
            if vals and isinstance(vals, list):
                val = ", ".join(str(v) for v in vals if v is not None) or val

            if name == "shipperhq_shipping_group":
                shipping_group = val or "N/A"
            elif name == "shipperhq_dim_group":
                dim_group = val or "N/A"
            elif name in ("ship_length", "shipperhq_length", "length"):
                length = val
            elif name in ("ship_width", "shipperhq_width", "width"):
                width = val
            elif name in ("ship_height", "shipperhq_height", "height"):
                height = val

        dimensions = f"{length}x{width}x{height}" if length and width and height else "N/A"
        return shipping_group, dim_group, dimensions

    try:
        for item in request_json.get("cart", {}).get("items", []):
            shipping_group, dim_group, dimensions = _extract_item_attrs(item)

            # Magento configurable/bundle items carry child items[].
            # Check children for attributes that may be missing on the parent.
            children = item.get("items") or []
            if children and isinstance(children, list):
                for child in children:
                    sg, dg, dims = _extract_item_attrs(child)
                    if shipping_group == "N/A" and sg != "N/A":
                        shipping_group = sg
                    if dim_group == "N/A" and dg != "N/A":
                        dim_group = dg
                    if dimensions == "N/A" and dims != "N/A":
                        dimensions = dims

            item_type = item.get("type", "")
            sku = item.get("sku")
            if item_type in ("configurable", "bundle") and sku:
                sku = f"{sku} ({item_type})"

            cart_items.append({
                "product": sku,
                "qty": item.get("qty"),
                "weight": item.get("weight"),
                "value": item.get("rowTotal"),
                "shipping_group": shipping_group,
                "dim_group": dim_group,
                "dimensions": dimensions,
            })
    except Exception:
        pass

    # ============================
    # METHODS + PER-CARRIER PACKING
    # ============================

    methods = []

    def pick_shipments(*sources):
        """Return the first non-empty shipments list from the provided dict sources."""
        for src in sources:
            if not isinstance(src, dict):
                continue
            ships = src.get("shipments")
            if isinstance(ships, list) and ships:
                return ships
        return []

    try:
        for group in response_json.get("carrierGroups", []):
            if not isinstance(group, dict):
                continue

            for carrier in group.get("carrierRates", []):
                if not isinstance(carrier, dict):
                    continue

                is_shared = (carrier.get("carrierType") or "").lower() == "shqshared"

                carrier_name = (
                    carrier.get("carrierName")
                    or carrier.get("carrierTitle")
                    or carrier.get("carrierCode")
                    or "Unknown Carrier"
                )
                carrier_code = carrier.get("carrierCode") or "N/A"
                carrier_type = carrier.get("carrierType") or "N/A"

                # Build method entries
                rates = carrier.get("rates") or []
                for r in rates:
                    if not isinstance(r, dict):
                        continue

                    # For shared carriers, each rate carries its own real
                    # carrier identity (carrierCode, carrierType, carrierTitle).
                    if is_shared:
                        rate_carrier_name = (
                            r.get("carrierTitle")
                            or r.get("carrierCode")
                            or carrier_name
                        )
                        rate_carrier_code = r.get("carrierCode") or carrier_code
                        rate_carrier_type = r.get("carrierType") or carrier_type
                    else:
                        rate_carrier_name = carrier_name
                        rate_carrier_code = carrier_code
                        rate_carrier_type = carrier_type

                    # Extract address type
                    address_type = None
                    options = (r.get("selectedOptions") or {}).get("options") or []
                    if isinstance(options, list) and options:
                        address_type = options[0].get("value")

                    # For shared carriers, prefer packing from rateBreakdownList
                    # which contains the actual per-carrier shipment details.
                    shipments = []
                    if is_shared:
                        for bd in r.get("rateBreakdownList") or []:
                            if isinstance(bd, dict):
                                shipments = pick_shipments(bd)
                                if shipments:
                                    break
                    if not shipments:
                        shipments = pick_shipments(r, carrier)

                    methods.append({
                        "carrier": rate_carrier_name,
                        "carrier_code": rate_carrier_code,
                        "carrier_type": rate_carrier_type,
                        "service_name": r.get("name"),
                        "base": r.get("origShippingPrice", r.get("totalCharges")),
                        "final": r.get("shippingPrice"),
                        "handling": r.get("handlingFee"),
                        "negotiated": r.get("negotiatedRate", False),
                        "address_type": address_type,
                        "flat_rules": r.get("flatRulesApplied", []),
                        "change_rules": r.get("changeRulesApplied", []),

                        # Packing per carrier (all shipments, not just first)
                        "packing": shipments,
                        "blocked_reasons": [],
                        "error_message": "",
                    })
                # If no rates, capture blocking/prevent rules for visibility (BigCommerce case)
                if not rates:
                    prevent = carrier.get("preventRulesApplied") or []
                    err = carrier.get("error") or {}
                    err_msg = (
                        err.get("internalErrorMessage")
                        or err.get("externalErrorMessage")
                        or ""
                    )
                    if prevent or err_msg:
                        shipments = pick_shipments(carrier)
                        methods.append({
                            "carrier": carrier_name,
                            "carrier_code": carrier_code,
                            "carrier_type": carrier_type,
                            "service_name": carrier.get("carrierTitle") or carrier_name,
                            "base": None,
                            "final": None,
                            "handling": None,
                            "negotiated": carrier.get("negotiatedRate", False),
                            "address_type": None,
                            "flat_rules": [],
                            "change_rules": [],
                            "packing": shipments,
                            "blocked_reasons": prevent,
                            "error_message": err_msg,
                        })
    except Exception as e:
        print("Carrier extraction error:", e)

    # No more "global packing"
    return {
        "Ship From": ship_from,
        "Ship To": ship_to,
        "Cart": cart_items,
        "Methods": methods,
    }


# ======================================================================
#   REPORT GENERATION
# ======================================================================

def build_table(headers, rows):
    if not rows:
        return "(no data)"

    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))

    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    separator = "-+-".join("-" * w for w in widths)
    lines = [header_line, separator]

    for r in rows:
        lines.append(" | ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))

    return "\n".join(lines)


def generate_report(summary):
    sf = summary["Ship From"]
    st = summary["Ship To"]

    ship_from_line = f"{sf.get('street','')}, {sf.get('city','')}, {sf.get('region','')} {sf.get('zipcode','')}, {sf.get('country','')}"
    ship_to_line = f"{st.get('street','')}, {st.get('city','')}, {st.get('region','')} {st.get('zipcode','')}, {st.get('country','')}"

    # Cart
    cart_rows = []
    for item in summary["Cart"]:
        cart_rows.append([
            item.get("product", "N/A"),
            item.get("qty", "N/A"),
            item.get("weight", "N/A"),
            item.get("dimensions", "N/A"),
            item.get("value", "N/A"),
            item.get("shipping_group", "N/A"),
            item.get("dim_group", "N/A"),
        ])
    cart_table = build_table(
        ["Product", "Qty", "Weight", "Dimensions", "Value", "Shipping Groups", "Packing Rules (shipperhq_dim_group)"],
        cart_rows,
    )

    # Methods grouped by carrier, packing displayed once per carrier
    carriers = {}
    for m in summary["Methods"]:
        name = m.get("carrier", "Unknown Carrier")
        entry = carriers.setdefault(name, {"packing": [], "methods": [], "carrier_code": "N/A", "carrier_type": "N/A"})
        packing_list = m.get("packing") or []
        if not entry["packing"] and packing_list:
            entry["packing"] = packing_list
        if entry["carrier_code"] == "N/A" and m.get("carrier_code", "N/A") != "N/A":
            entry["carrier_code"] = m["carrier_code"]
        if entry["carrier_type"] == "N/A" and m.get("carrier_type", "N/A") != "N/A":
            entry["carrier_type"] = m["carrier_type"]
        entry["methods"].append(m)

    method_sections = []
    for carrier_name, data in carriers.items():
        carrier_methods = []
        for m in data["methods"]:
            base_val = m.get("base")
            final_val = m.get("final")
            handling_val = m.get("handling")
            addr = m.get("address_type")
            blocked = m.get("blocked_reasons") or []
            err_msg = m.get("error_message") or ""

            carrier_methods.append("\n".join([
                f"Method: {carrier_name} – {m['service_name']}",
                f"Base Rate: ${base_val}" if base_val not in (None, "") else "Base Rate: N/A",
                f"Final Price Shown to Customer: ${final_val}" if final_val not in (None, "") else "Final Price Shown to Customer: N/A",
                f"Handling Fee: ${handling_val}" if handling_val not in (None, "") else "Handling Fee: N/A",
                f"Address Type: {addr if addr else 'N/A'}",
                f"Rate Type: {'Negotiated' if m['negotiated'] else 'List'}",
                f"Flat Rules Applied: {', '.join(m['flat_rules']) if m['flat_rules'] else 'None'}",
                f"Change Rules Applied: {', '.join(m['change_rules']) if m['change_rules'] else 'None'}",
                *(["Prevent/Block Reasons: " + "; ".join(blocked)] if blocked else []),
                *(["Carrier Error: " + err_msg] if err_msg else []),
            ]))

        packings = data["packing"] if isinstance(data.get("packing"), list) else []
        packing_lines = ["Packing:"]
        if packings:
            for idx, pack in enumerate(packings, 1):
                dims = None
                if pack.get("length") is not None and pack.get("width") is not None and pack.get("height") is not None:
                    dims = f"{pack.get('length')}x{pack.get('width')}x{pack.get('height')}"

                items = []
                for bi in pack.get("boxedItems") or []:
                    sku = bi.get("sku") or bi.get("itemId") or "item"
                    qty = bi.get("qtyPacked")
                    weight_packed = bi.get("weightPacked")
                    item_bits = [sku]
                    if qty not in (None, ""):
                        item_bits.append(f"x{qty}")
                    if weight_packed not in (None, ""):
                        item_bits.append(f"{weight_packed} lb")
                    items.append(" ".join(item_bits))

                packing_lines.append(f"  Box {idx}:")
                packing_lines.append(f"    Name: {pack.get('name', 'N/A')}")
                packing_lines.append(f"    Weight: {pack.get('weight', 'N/A')}")
                packing_lines.append(f"    Dimensions: {dims if dims else 'N/A'}")
                if pack.get("freightClass"):
                    packing_lines.append(f"    Freight Class: {pack.get('freightClass')}")
                packing_lines.append("    Items: " + ("; ".join(items) if items else "N/A"))
        else:
            packing_lines.extend([
                "  Box: N/A",
                "  Weight: N/A",
                "  Dimensions: N/A",
            ])

        method_sections.append("\n\n".join([
            f"Carrier: {carrier_name}  (code: {data['carrier_code']}, type: {data['carrier_type']})",
            "\n\n".join(carrier_methods),
            "\n".join(packing_lines),
        ]))

    methods_section = "\n\n".join(method_sections) if method_sections else "No carrier services returned."

    platform = summary.get("Platform", "Unknown")
    log_ts = summary.get("LogTimestamp") or "N/A"

    return f"""
=== Request Timestamp ===
{log_ts}

=== Platform ===
{platform}

=== Ship From ===
{ship_from_line}

=== Ship To ===
{ship_to_line}

=== Cart Contents ===
{cart_table}

=== Carrier & Service Returned (Per Method) ===
{methods_section}
""".strip()


# ======================================================================
#   FILE PICKER
# ======================================================================

_SHQ_ID_RE = re.compile(r"SHQ_\d{8}_\d{4}_shipperws_\d+_\d+")


def pick_file():
    root = tk.Tk()
    root.withdraw()
    return filedialog.askopenfilename(
        title="Select a log file  –OR–  unique_SHQ_IDs.txt for batch mode",
        filetypes=[("Log files", "*.log"), ("ID / Text files", "*.txt"), ("All files", "*.*")]
    )


def file_is_shq_ids(path):
    """Return True if the file looks like a list of SHQ IDs rather than a log."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4096)
        ids = _SHQ_ID_RE.findall(head)
        # If the first non-empty lines are all SHQ IDs, treat as IDs file
        lines = [l.strip() for l in head.splitlines() if l.strip()]
        if not lines:
            return False
        sample = lines[:min(5, len(lines))]
        return all(_SHQ_ID_RE.fullmatch(l) for l in sample)
    except Exception:
        return False


def find_latest_shipperws_log(base_dir, token):
    """Find the newest shipperws log containing the token; prefer *.shipperws.log."""
    logs_root = os.path.join(base_dir, "logs")
    preferred = []
    fallback = []

    for root, _, files in os.walk(logs_root):
        for name in files:
            if token not in name or "shipperws" not in name or not name.endswith(".log"):
                continue
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if name.endswith(".shipperws.log"):
                preferred.append((mtime, path))
            else:
                fallback.append((mtime, path))

    for bucket in (preferred, fallback):
        if bucket:
            bucket.sort(key=lambda t: t[0], reverse=True)
            return bucket[0][1]
    return None


# ======================================================================
#   SINGLE-ID PROCESSING HELPER
# ======================================================================

def process_one(tid, script_dir):
    """
    Fetch logs for a single SHQ transaction ID, parse, and return
    (report_str, log_timestamp) or (None, None) on failure.
    """
    findlog_path = os.path.join(script_dir, "findlog.sh")
    if not os.path.isfile(findlog_path):
        print(f"Cannot find findlog.sh at {findlog_path}")
        return None, None

    print(f"\nFetching logs via findlog.sh for {tid} ...")
    try:
        subprocess.run([findlog_path, tid, "-x"], cwd=script_dir, check=True)
    except subprocess.CalledProcessError as e:
        print(f"findlog.sh failed with code {e.returncode} for {tid}")
        return None, None

    log_path = find_latest_shipperws_log(script_dir, tid)
    if not log_path:
        print(f"No shipperws log found for token '{tid}' under logs/.")
        return None, None

    print(f"Using latest shipperws log: {log_path}")
    print(f"Processing: {log_path}")

    try:
        summary = parse_log_file(log_path)
        return generate_report(summary), summary.get("LogTimestamp")
    except Exception as e:
        print(f"\nERROR processing {tid}: {e}")
        return None, None


def run_batch(ids, script_dir, ids_source_path):
    """Process a list of SHQ IDs and write all reports into one combined file."""
    entries = []  # list of (sort_key, section_text)
    ok = fail = 0
    for shq_id in ids:
        report, log_ts = process_one(shq_id, script_dir)
        display_ts = log_ts or "N/A"
        sort_key = log_ts or "9999"  # entries without a timestamp sort last
        if report is not None:
            sections_text = f"{'=' * 60}\nID: {shq_id}  |  Timestamp: {display_ts}\n{'=' * 60}\n{report}"
            ok += 1
        else:
            sections_text = f"{'=' * 60}\nID: {shq_id}  |  Timestamp: {display_ts}\n{'=' * 60}\nERROR: could not generate report for this ID."
            fail += 1
        entries.append((sort_key, sections_text))

    # Sort from earliest to oldest
    entries.sort(key=lambda e: e[0])
    combined = "\n\n".join(text for _, text in entries)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(os.path.dirname(os.path.abspath(ids_source_path)), f"batch_rate_analysis_{ts}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(combined)

    print(f"\nBatch complete: {ok} succeeded, {fail} failed.")
    print(f"Combined report saved to: {out_path}")
    subprocess.Popen(["open", out_path])
    return ok, fail


# ======================================================================
#   MAIN
# ======================================================================

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))

    log_path = None

    if len(sys.argv) > 1:
        arg = sys.argv[1]

        # ── Batch mode: arg is the unique_SHQ_IDs.txt file ──────────────────
        ids_file = os.path.join(script_dir, "unique_SHQ_IDs.txt")
        if os.path.abspath(arg) == os.path.abspath(ids_file) or arg == "unique_SHQ_IDs.txt":
            with open(ids_file, "r") as f:
                ids = [line.strip() for line in f if line.strip()]

            if not ids:
                print(f"No IDs found in {ids_file}.")
                sys.exit(1)

            print(f"Batch mode: processing {len(ids)} IDs from {ids_file}")
            ok, fail = run_batch(ids, script_dir, ids_file)
            sys.exit(0 if fail == 0 else 1)

        # ── Direct log file path provided ────────────────────────────────────
        elif os.path.isfile(arg):
            log_path = arg

        # ── Single SHQ transaction ID ─────────────────────────────────────────
        else:
            report, _ = process_one(arg, script_dir)
            if report is None:
                sys.exit(1)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = os.path.join(script_dir, f"rate_analysis_{ts}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"Saved to: {out_path}")
            subprocess.Popen(["open", out_path])
            sys.exit(0)

    else:
        # ── No args → show file picker (log file OR IDs txt) ─────────────────
        print("Please select a log file or unique_SHQ_IDs.txt for batch mode...")
        picked = pick_file()

        if not picked:
            print("No file selected.")
            sys.exit(0)

        if file_is_shq_ids(picked):
            # Treat as batch IDs file
            with open(picked, "r", encoding="utf-8", errors="ignore") as f:
                ids = [l.strip() for l in f if _SHQ_ID_RE.fullmatch(l.strip())]

            if not ids:
                print(f"No SHQ IDs found in {picked}.")
                sys.exit(1)

            print(f"Batch mode: processing {len(ids)} IDs from {picked}")
            ok, fail = run_batch(ids, script_dir, picked)
            sys.exit(0 if fail == 0 else 1)

        log_path = picked

    if not log_path:
        print("No file selected.")
        sys.exit(0)

    print(f"Processing: {log_path}")

    try:
        summary = parse_log_file(log_path)
        report = generate_report(summary)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(
            os.path.dirname(log_path),
            f"rate_analysis_{ts}.txt"
        )
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)

        print(f"Saved to: {out_path}")
        subprocess.Popen(["open", out_path])

    except Exception as e:
        print("\nERROR:", e)
        sys.exit(1)

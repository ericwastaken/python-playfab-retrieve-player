import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import click
import requests
import yaml
from jsonpath_ng.ext import parse as jsonpath_parse


@dataclass
class Config:
    playfab_api_endpoint: str
    request_body_template: Dict[str, Any]
    output_layout: Dict[str, List[Dict[str, Union[str, bool]]]]
    output_format: str  # one of: json, yaml, csv, ndjson




def _log(msg: str, verbose: bool = False, level: str = "INFO") -> None:
    """Minimal logger: prints to stderr. If level == DEBUG, only prints when verbose."""
    if level == "DEBUG" and not verbose:
        return
    sys.stderr.write(f"[{level}] {msg}\n")
    sys.stderr.flush()


def load_config(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    endpoint = data.get("playfab_api_endpoint")
    if not endpoint:
        raise click.ClickException("Config missing required 'playfab_api_endpoint'.")

    # Require new 'request_body' block (no legacy support)
    request_body = data.get("request_body")
    if not isinstance(request_body, dict):
        raise click.ClickException("Config missing required 'request_body' mapping.")

    # Normalize single-item lists to dicts for keys that should be objects
    def _normalize_obj(v: Any) -> Any:
        if isinstance(v, list) and len(v) == 1 and isinstance(v[0], dict):
            return v[0]
        return v

    rb_norm = dict(request_body)

    # Require new 'output' block (no legacy support)
    output_block = data.get("output")
    if not isinstance(output_block, dict):
        raise click.ClickException("Config missing required 'output' block with 'outputFormat' and 'layout'.")
    output_format = str(output_block.get("outputFormat") or "json").strip().lower()
    output_layout = output_block.get("layout") or {}

    if not isinstance(output_layout, dict):
        raise click.ClickException("Output layout must be a mapping of output fields to extraction rules.")

    # Normalize to: field -> list of {source, path, json_parse?}
    norm_layout: Dict[str, List[Dict[str, Union[str, bool]]]] = {}
    for out_field, rules in output_layout.items():
        if rules is None:
            norm_layout[out_field] = []
            continue
        if isinstance(rules, dict):
            rules_list = [rules]
        elif isinstance(rules, list):
            rules_list = rules
        else:
            raise click.ClickException("Each layout value must be a rule mapping or a list of rule mappings.")
        cleaned: List[Dict[str, Union[str, bool]]] = []
        for r in rules_list:
            if not isinstance(r, dict):
                raise click.ClickException("Each rule in layout must be a mapping with 'source' and 'path'.")
            src = r.get("source")
            pth = r.get("path")
            jp = r.get("json_parse", False)
            if src not in {"inputcsv", "response"}:
                raise click.ClickException("Rule 'source' must be either 'inputcsv' or 'response'.")
            if not isinstance(pth, str) or not pth:
                raise click.ClickException("Rule 'path' must be a non-empty string.")
            if jp not in (True, False):
                # allow missing/None treated as False, otherwise enforce boolean
                jp = bool(jp)
            rec: Dict[str, Union[str, bool]] = {"source": src, "path": pth}
            if jp:
                rec["json_parse"] = True
            cleaned.append(rec)
        norm_layout[out_field] = cleaned

    # Validate output format
    allowed_formats = {"json", "yaml", "csv", "ndjson"}
    if output_format not in allowed_formats:
        raise click.ClickException(f"Unsupported outputFormat '{output_format}'. Must be one of: {', '.join(sorted(allowed_formats))}.")

    return Config(
        playfab_api_endpoint=endpoint.rstrip("/"),
        request_body_template=rb_norm,
        output_layout=norm_layout,
        output_format=output_format,
    )


def read_input_rows(csv_path: str) -> List[Dict[str, Optional[str]]]:
    """Read the entire CSV as strings and return a list of row dicts.
    No specific column is required; any column names can be referenced via '$.<columnName>' in the request_body template.
    """
    try:
        import pandas as pd
    except Exception as e:
        raise click.ClickException(f"pandas is required to parse CSV: {e}")
    try:
        df = pd.read_csv(csv_path, dtype=str, engine="python")
    except Exception as e:
        raise click.ClickException(f"Failed to read CSV: {e}")
    if df is None or df.empty:
        raise click.ClickException("Input CSV appears to be empty.")
    # strip quotes/whitespace for all string cells
    df = df.map(lambda v: v.strip().strip('"') if isinstance(v, str) else v)
    rows: List[Dict[str, Optional[str]]] = [dict(r) for r in df.to_dict(orient="records")]
    return rows


def login_with_custom_id(endpoint: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    url = f"{endpoint}/Client/LoginWithCustomID"
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        status = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return status, body
    except requests.RequestException as e:
        return 0, {"error": str(e)}


def _extract_from_response(body: Dict[str, Any], path: str) -> Any:
    """Extract a value from a dict using jsonpath-ng.
    Be forgiving about paths that don't start with '$' by retrying with '$.' prefix.
    Also tolerate configs that include an extra top-level 'response' segment.
    Returns None when nothing is found or the path is invalid.
    """
    def _find(p: str, search_body: Dict[str, Any]) -> List[Any]:
        try:
            expr = jsonpath_parse(p)
            return [m.value for m in expr.find(search_body)]
        except Exception:
            return []

    # First attempt with the provided path as-is
    matches = _find(path, body)

    # If not found and path doesn't look like a rooted JSONPath, try with '$.' prefix
    if not matches and path and not path.lstrip().startswith(("$", "@")):
        prefixed = f"$.{path}"
        matches = _find(prefixed, body)

    # If still not found, handle the common case where configs use '$.response.' while the API body is not wrapped
    if not matches and path:
        # If the API body actually has a top-level 'response', try searching inside it
        if isinstance(body, dict) and "response" in body and isinstance(body["response"], dict):
            matches = _find(path, body["response"]) or matches
        # If the path starts with '$.response.', try removing that segment and re-searching against the top body
        prefix = "$.response."
        if not matches and path.startswith(prefix):
            trimmed = "$." + path[len(prefix):]
            matches = _find(trimmed, body)

    if not matches:
        return None
    # If single value, return scalar; if multiple, return list
    return matches[0] if len(matches) == 1 else matches


def _build_output_record(layout: Dict[str, List[Dict[str, Union[str, bool]]]], row: Dict[str, Any], response: Dict[str, Any], verbose: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field, rules in layout.items():
        value: Any = None
        for rule in rules:
            src = rule.get("source")
            pth = rule.get("path")
            if src == "inputcsv":
                value = row.get(pth)
            elif src == "response":
                value = _extract_from_response(response, pth)
                if (value is None or value == ""):
                    _log(f"No match for JSONPath '{pth}' when extracting field '{field}'. If your API body isn't wrapped under 'response', try removing that segment from the path.", verbose, level="WARNING")
                # Optionally JSON-parse the extracted value(s)
                if value is not None and rule.get("json_parse"):
                    def _try_parse(v: Any) -> Any:
                        if isinstance(v, str):
                            try:
                                return json.loads(v)
                            except Exception:
                                if verbose:
                                    _log(f"Failed to json-parse value for field '{field}' from path '{pth}'. Keeping original string.", verbose, level="WARNING")
                                return v
                        return v
                    if isinstance(value, list):
                        value = [_try_parse(v) for v in value]
                    else:
                        value = _try_parse(value)
            if value is not None and value != "":
                break
        out[field] = value
    return out


def _resolve_jsonpath_for_row(expr: str, row: Dict[str, Any]) -> Any:
    """Resolve a very small subset of JSONPath against a row dict.
    Supports expressions like '$.customId'. Returns None if no match.
    """
    try:
        if not isinstance(expr, str):
            return expr
        if expr.startswith("$."):
            key = expr[2:]
            return row.get(key)
        return expr
    except Exception:
        return expr


def build_request_payload(template: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """Build the LoginWithCustomID payload strictly from the request_body template and a CSV row.
    - Replace any string values that look like JSONPath (e.g., '$.artifactId') with row values.
    - Do NOT inject TitleId or otherwise modify the structure beyond substitutions.
    """
    def _walk(val: Any) -> Any:
        if isinstance(val, dict):
            return {k: _walk(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_walk(v) for v in val]
        if isinstance(val, str):
            return _resolve_jsonpath_for_row(val, row)
        return val

    built = _walk(template)

    # Keep the structure exactly as in the template
    payload: Dict[str, Any] = dict(built)

    return payload


def _find_unresolved_placeholders(obj: Any, path: str = "$") -> List[str]:
    """Return list of JSON-like paths within obj where string values still start with '$.'"""
    unresolved: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            unresolved.extend(_find_unresolved_placeholders(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            unresolved.extend(_find_unresolved_placeholders(v, f"{path}[{idx}]"))
    elif isinstance(obj, str):
        if obj.startswith("$."):
            unresolved.append(path)
    return unresolved


def _ensure_custom_id_present(payload: Dict[str, Any]) -> Optional[str]:
    """Ensure payload contains a non-empty 'CustomId'. Return its string value if present, else None."""
    cid = payload.get("CustomId")
    if cid is None:
        return None
    cid_str = str(cid).strip()
    return cid_str or None


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """PlayFab retrieve CLI.

    Available commands:
    - with-custom-id: Call Client LoginWithCustomID for each row in a CSV.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        # Provide a friendly hint and non-zero exit to indicate a command is required
        raise click.ClickException("No command specified. Please run: playfab-retrieve with-custom-id --help")


@main.command("with-custom-id", help="Call PlayFab Client LoginWithCustomID for each row in the CSV. Column names are mapped via '$.<columnName>' in request_body; no fixed 'customId' column is required.")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to YAML config file.")
@click.option("--input", "input_csv", required=True, type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to CSV file with arbitrary columns.")
@click.option("--output", "output_path", required=True, type=click.Path(dir_okay=False, writable=True), help="Path to write results. Format determined by config 'output.outputFormat' (json|yaml|ndjson|csv).")
@click.option("--verbose", is_flag=True, default=False, help="Enable detailed logging; request payloads and response bodies will be printed.")
def cmd_with_custom_id(config_path: str, input_csv: str, output_path: str, verbose: bool) -> None:
    """
    Retrieve PlayFab player information via LoginWithCustomID for each customId in the CSV.
    Output file format and fields are determined by the 'output' block in the YAML config.
    """
    cfg = load_config(config_path)

    rows = read_input_rows(input_csv)
    _log(f"Loaded {len(rows)} input rows from {input_csv}", verbose, level="DEBUG")

    # VERIFICATION STEP
    # Build an example payload from the first row to show the user what will be sent
    sample_row = rows[0]
    sample_payload = build_request_payload(cfg.request_body_template, sample_row)
    unresolved = _find_unresolved_placeholders(sample_payload)
    resolved_cid = _ensure_custom_id_present(sample_payload)
    if unresolved:
        raise click.ClickException(
            "Some placeholders in request_body could not be resolved from the CSV for the first row: "
            + ", ".join(unresolved)
        )
    if not resolved_cid:
        raise click.ClickException(
            "Config 'request_body' must contain 'CustomId' that resolves to a non-empty value from the CSV."
        )
    summary = {
        "endpoint": f"{cfg.playfab_api_endpoint}/Client/LoginWithCustomID",
        "totalRequests": len(rows),
        "resolvedCustomIdExample": resolved_cid,
        "payloadExample": sample_payload,
    }
    sys.stderr.write("=== VERIFICATION ===\n")
    sys.stderr.write("About to make the following requests based on your CONFIG:\n")
    sys.stderr.write(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))
    sys.stderr.write("====================\n")
    sys.stderr.flush()
    if not click.confirm("Proceed with the requests?", default=False):
        raise click.ClickException("Aborted by user.")

    # Fetch all records
    records = []
    for i, row in enumerate(rows, start=1):
        payload = build_request_payload(cfg.request_body_template, row)
        unresolved_row = _find_unresolved_placeholders(payload)
        if unresolved_row:
            raise click.ClickException(
                f"Row {i}: some placeholders in request_body could not be resolved from the CSV: "
                + ", ".join(unresolved_row)
            )
        aid = _ensure_custom_id_present(payload)
        if not aid:
            raise click.ClickException(
                f"Row {i}: 'CustomId' from request_body did not resolve to a non-empty value."
            )
        _log(f"[{i}/{len(rows)}] Requesting LoginWithCustomID for customId='{aid}'", verbose)
        # In verbose mode, show the exact payload as it will be sent (same serialization as the request)
        data_str = json.dumps(payload)
        _log(f"[{i}/{len(rows)}] Payload to be sent: {data_str}", verbose, level="DEBUG")
        status, body = login_with_custom_id(cfg.playfab_api_endpoint, payload)
        _log(f"[{i}/{len(rows)}] Received status {status}", verbose)
        if verbose:
            try:
                pretty = json.dumps(body, ensure_ascii=False, indent=2)
            except Exception:
                pretty = str(body)
            if 200 <= int(status) < 300:
                _log(f"[{i}/{len(rows)}] Response body:", verbose, level="DEBUG")
            else:
                _log(f"Non-2xx response for customId='{aid}'. Full response body follows:", verbose, level="ERROR")
            sys.stderr.write(pretty + "\n")
            sys.stderr.flush()
        record = _build_output_record(cfg.output_layout, row, body, verbose=verbose)
        records.append(record)

    # Write output in the requested format
    fmt = cfg.output_format
    try:
        if fmt == "ndjson":
            with open(output_path, "w", encoding="utf-8", newline="\n") as out_f:
                for rec in records:
                    out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        elif fmt == "json":
            with open(output_path, "w", encoding="utf-8") as out_f:
                json.dump(records, out_f, ensure_ascii=False, indent=2)
        elif fmt == "yaml":
            with open(output_path, "w", encoding="utf-8") as out_f:
                yaml.safe_dump(records, out_f, allow_unicode=True, sort_keys=False)
        elif fmt == "csv":
            import csv

            def _flatten(prefix: str, value: Any, out: Dict[str, Any]) -> None:
                key_prefix = prefix + "." if prefix else ""
                if isinstance(value, dict):
                    for k, v in value.items():
                        _flatten(key_prefix + str(k), v, out)
                elif isinstance(value, list):
                    for idx, item in enumerate(value):
                        if isinstance(item, (dict, list)):
                            _flatten(f"{key_prefix}{idx}", item, out)
                        else:
                            out[f"{key_prefix}{idx}"] = item
                else:
                    out[prefix] = value

            flat_records: List[Dict[str, Any]] = []
            header_set: List[str] = []
            seen = set()
            for rec in records:
                flat: Dict[str, Any] = {}
                for k, v in rec.items():
                    if isinstance(v, (dict, list)):
                        _flatten(k, v, flat)
                    else:
                        flat[k] = v
                flat_records.append(flat)
                for col in flat.keys():
                    if col not in seen:
                        seen.add(col)
                        header_set.append(col)

            with open(output_path, "w", encoding="utf-8", newline="") as out_f:
                writer = csv.DictWriter(out_f, fieldnames=header_set)
                writer.writeheader()
                for row in flat_records:
                    writer.writerow({h: row.get(h, "") for h in header_set})
        else:
            raise click.ClickException(f"Unhandled output format: {fmt}")
        _log(f"Wrote {len(records)} records to {output_path} as {fmt}", verbose)
    except OSError as e:
        raise click.ClickException(f"Failed to write output file: {e}")


if __name__ == "__main__":
    main()

import json
import sys
import time
import signal
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import click
import requests
import yaml
import re
from jsonpath_ng.ext import parse as jsonpath_parse


@dataclass
class Config:
    playfab_api_endpoint: str
    request_body_template: Dict[str, Any]
    output_layout: Dict[str, List[Dict[str, Union[str, bool]]]]
    output_format: str  # one of: json, yaml, csv, ndjson
    request_header_template: Dict[str, Any]
    concurrent_requests: int = 1
    delay_s_after_requests: float = 0.0




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

    # Optional concurrency control (new: flow_control.concurrent_requests; legacy: concurrent_requests)
    conc_source = "flow_control.concurrent_requests" if isinstance(data.get("flow_control"), dict) and "concurrent_requests" in data.get("flow_control", {}) else "concurrent_requests"
    if conc_source == "flow_control.concurrent_requests":
        conc = data.get("flow_control", {}).get("concurrent_requests", 1)
    else:
        conc = data.get("concurrent_requests", 1)
    try:
        conc = int(conc)
    except Exception:
        raise click.ClickException("Config 'flow_control.concurrent_requests' (or legacy 'concurrent_requests') must be an integer >= 1.")
    if conc < 1:
        raise click.ClickException("Config 'flow_control.concurrent_requests' (or legacy 'concurrent_requests') must be >= 1.")

    # Optional per-request delay (new: flow_control.delay_s_after_requests)
    flow = data.get("flow_control") or {}
    delay = flow.get("delay_s_after_requests", 0)
    try:
        delay = float(delay)
    except Exception:
        raise click.ClickException("Config 'flow_control.delay_s_after_requests' must be a number >= 0.")
    if delay < 0:
        raise click.ClickException("Config 'flow_control.delay_s_after_requests' must be >= 0.")

    # Optional 'request_header' block to allow custom headers (e.g., X-SecretKey)
    request_header = data.get("request_header") or {}
    if request_header is None:
        request_header = {}
    if not isinstance(request_header, dict):
        raise click.ClickException("Config 'request_header' must be a mapping when provided.")

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

    # Normalize to: field -> list of {source, path, json_parse?, quote?}
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
            qt = r.get("quote", False)
            if src not in {"inputcsv", "response"}:
                raise click.ClickException("Rule 'source' must be either 'inputcsv' or 'response'.")
            if not isinstance(pth, str) or not pth:
                raise click.ClickException("Rule 'path' must be a non-empty string.")
            if jp not in (True, False):
                # allow missing/None treated as False, otherwise enforce boolean
                jp = bool(jp)
            if qt not in (True, False):
                qt = bool(qt)
            rec: Dict[str, Union[str, bool]] = {"source": src, "path": pth}
            if jp:
                rec["json_parse"] = True
            if qt:
                rec["quote"] = True
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
        request_header_template=request_header,
        concurrent_requests=conc,
        delay_s_after_requests=delay,
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
    """Extract a value from a dict using jsonpath-ng, with extensions.
    Extensions supported:
      - json_parse(<jsonpath>)<suffix>: resolve <jsonpath> first against the response,
        JSON-decode the resulting string(s), then apply an optional JSONPath <suffix>
        (like .field, [0], [*].field) against the parsed object(s).
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

    # Handle custom json_parse(...)<suffix> syntax
    if isinstance(path, str):
        s = path.strip()
        if s.startswith("json_parse("):
            # Extract inner jsonpath and optional suffix after the matching ')'
            open_idx = s.find("(")
            close_idx = s.rfind(")")
            inner = s[open_idx + 1 : close_idx].strip() if close_idx > open_idx else s[open_idx + 1 :].strip()
            suffix = s[close_idx + 1 :].strip() if close_idx != -1 else ""

            # Resolve inner path using tolerant logic by delegating to this function
            inner_val = _extract_from_response(body, inner)
            inner_vals: List[Any]
            if inner_val is None:
                inner_vals = []
            elif isinstance(inner_val, list):
                inner_vals = inner_val
            else:
                inner_vals = [inner_val]

            # JSON-decode each value if it's a string; keep dict/list as-is
            parsed_vals: List[Any] = []
            for v in inner_vals:
                if isinstance(v, str):
                    try:
                        parsed_vals.append(json.loads(v))
                    except Exception:
                        # If not valid JSON, keep original string
                        parsed_vals.append(v)
                else:
                    parsed_vals.append(v)

            # If no suffix, return parsed (first if single)
            if not suffix:
                if not parsed_vals:
                    return None
                return parsed_vals[0] if len(parsed_vals) == 1 else parsed_vals

            # Apply suffix as a JSONPath against each parsed value
            # If suffix doesn't start with '$' or '@', prepend '$'
            suffix_path = suffix if suffix.lstrip().startswith(("$", "@")) else f"${suffix}"
            out_vals: List[Any] = []
            for obj in parsed_vals:
                try:
                    expr = jsonpath_parse(suffix_path)
                    out_vals.extend([m.value for m in expr.find(obj)])
                except Exception:
                    # If suffix jsonpath invalid for this obj, ignore
                    continue
            if not out_vals:
                return None
            return out_vals[0] if len(out_vals) == 1 else out_vals

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


def _apply_secrets_placeholders(val: Any, secrets: Dict[str, str]) -> Any:
    """Replace secrets placeholders with values from the provided secrets dict.
    Supported forms:
      - String sentinel: "{$secrets: KEY_NAME}"
      - Mapping sentinel: {"$secrets": "KEY_NAME"} (common when using YAML inline mapping syntax)
      - Embedded in strings: e.g., "Bearer { $secrets: TOKEN }" (supports multiple occurrences)
    Recurses into dicts and lists.
    """
    def _resolve_str(s: str) -> Any:
        # Replace any occurrences like { $secrets: KEY } within the string.
        # KEY allows word-ish characters including dash, underscore, dot.
        pattern = re.compile(r"\{\s*\$secrets\s*:\s*([A-Za-z0-9_.-]+)\s*\}")
        def repl(m: re.Match) -> str:
            key = m.group(1)
            return secrets.get(key, "")
        return pattern.sub(repl, s)

    # Mapping sentinel: {"$secrets": "KEY"} → secrets[KEY]
    if isinstance(val, dict):
        if len(val) == 1 and "$secrets" in val:
            key_name = val.get("$secrets")
            if isinstance(key_name, str):
                return secrets.get(key_name)
            # If not a string, fall through to recursive processing
        return {k: _apply_secrets_placeholders(v, secrets) for k, v in val.items()}

    if isinstance(val, list):
        return [_apply_secrets_placeholders(v, secrets) for v in val]

    if isinstance(val, str):
        return _resolve_str(val)

    return val


def _contains_secrets_placeholder(val: Any) -> bool:
    """Detect if the given value (possibly nested) contains a secrets placeholder.
    Matches any of:
      - Mapping sentinel: {"$secrets": "KEY"}
      - String containing pattern like "{ $secrets: KEY }" (anywhere in string)
    Recurses into dicts/lists.
    """
    if isinstance(val, dict):
        if "$secrets" in val and len(val) == 1 and isinstance(val.get("$secrets"), (str, bytes)):
            return True
        return any(_contains_secrets_placeholder(v) for v in val.values())
    if isinstance(val, list):
        return any(_contains_secrets_placeholder(v) for v in val)
    if isinstance(val, str):
        return re.search(r"\{\s*\$secrets\s*:\s*([A-Za-z0-9_.-]+)\s*\}", val) is not None
    return False


def load_secrets_file(path: Optional[str]) -> Dict[str, str]:
    """Load secrets from a simple KEY VALUE PAIRS file.
    Supports lines in formats:
      KEY VALUE
      KEY=VALUE
    Ignores empty lines and comments starting with '#'.
    """
    secrets: Dict[str, str] = {}
    if not path:
        return secrets
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" in s:
                    k, v = s.split("=", 1)
                    secrets[k.strip()] = v.strip()
                else:
                    parts = s.split(None, 1)
                    if len(parts) == 2:
                        secrets[parts[0].strip()] = parts[1].strip()
    except OSError as e:
        raise click.ClickException(f"Failed to read secrets file: {e}")
    return secrets


def merge_headers(base: Dict[str, str], extra: Dict[str, Any]) -> Dict[str, str]:
    out = dict(base)
    for k, v in (extra or {}).items():
        if v is None:
            continue
        out[str(k)] = str(v)
    return out


def build_request_payload(template: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """Build the request payload from the template and a CSV row.
    - Replace any string values that look like JSONPath (e.g., '$.artifactId') with row values.
    - Keep the structure exactly as provided.
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
    payload: Dict[str, Any] = dict(built)
    return payload


def build_request_payload_with_secrets(template: Dict[str, Any], row: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, Any]:
    # First apply secrets, then resolve JSONPath against row
    templ = _apply_secrets_placeholders(template, secrets) if secrets else template
    return build_request_payload(templ, row)


def _ensure_playfab_id_present(payload: Dict[str, Any]) -> Optional[str]:
    pid = payload.get("PlayFabId")
    if pid is None:
        return None
    pid_str = str(pid).strip()
    return pid_str or None


def server_get_player_combined_info(endpoint: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    url = f"{endpoint}/Server/GetPlayerCombinedInfo"
    base_headers = {"Content-Type": "application/json"}
    all_headers = merge_headers(base_headers, headers or {})
    try:
        resp = requests.post(url, headers=all_headers, data=json.dumps(payload), timeout=30)
        status = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return status, body
    except requests.RequestException as e:
        return 0, {"error": str(e)}


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
        raise click.ClickException("No command specified. Please run: playfab-retrieve-player with-custom-id --help")


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

    # Determine output mode
    fmt = cfg.output_format

    # Helper to flatten nested dict/list values when writing CSV
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

    # Prepare writers for progressive output
    csv_writer = None
    csv_header = None
    out_file_handle = None

    try:
        if fmt == "ndjson":
            out_file_handle = open(output_path, "w", encoding="utf-8", newline="\n")
        elif fmt == "csv":
            out_file_handle = open(output_path, "w", encoding="utf-8", newline="")
            # We will derive header from layout keys; nested objects will be flattened under these roots
            csv_header = list(cfg.output_layout.keys())
            # Build a map of fields that must be quoted in CSV rows
            field_quote_map = {k: any((isinstance(r, dict) and r.get("quote") is True) for r in (cfg.output_layout.get(k) or [])) for k in csv_header}
            # Helpers to encode CSV cells and lines
            def _csv_needs_quote(s: str) -> bool:
                return ("," in s) or ("\"" in s) or ("\n" in s) or ("\r" in s) or s.startswith(" ") or s.endswith(" ")
            def _csv_encode_cell(val: Any, force_quote: bool = False) -> str:
                s = "" if val is None else str(val)
                if force_quote or _csv_needs_quote(s):
                    return "\"" + s.replace("\"", "\"\"") + "\""
                return s
            # Always quote headers
            out_file_handle.write(",".join("\"" + h.replace("\"", "\"\"") + "\"" for h in csv_header) + "\n")
        elif fmt in ("json", "yaml"):
            # For JSON/YAML we still collect in memory, but requests will still be concurrent
            pass
        else:
            raise click.ClickException(f"Unhandled output format: {fmt}")

        # Concurrency setup with graceful Ctrl+C and per-request grouped logs
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

        interrupted = False

        def _sigint_handler(signum, frame):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                _log("CTRL+C detected: stopping new submissions and waiting for in-flight requests to finish...", verbose, level="WARNING")

        old_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, _sigint_handler)
        except Exception:
            # On some platforms (e.g., Windows in certain contexts) setting signals may fail; ignore
            old_sigint = None

        def task_do(i: int, row: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str]:
            logs: List[str] = []

            def log_line(message: str, level: str = "INFO") -> None:
                if level == "DEBUG" and not verbose:
                    return
                logs.append(f"[{level}] [{i}/{len(rows)}] {message}")

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
            log_line(f"Requested LoginWithCustomID for customId='{aid}'")
            data_str = json.dumps(payload)
            log_line(f"Payload to be sent: {data_str}", level="DEBUG")
            status, body = login_with_custom_id(cfg.playfab_api_endpoint, payload)
            log_line(f"Received status {status}")
            if verbose:
                try:
                    pretty = json.dumps(body, ensure_ascii=False, indent=2)
                except Exception:
                    pretty = str(body)
                if 200 <= int(status) < 300:
                    log_line("Response body:", level="DEBUG")
                else:
                    logs.append("[ERROR] " + f"Non-2xx response for customId='{aid}'. Full response body follows:")
                logs.append(pretty)
            # Apply per-request delay, if configured
            if cfg.delay_s_after_requests and cfg.delay_s_after_requests > 0:
                log_line(f"Delaying {cfg.delay_s_after_requests} seconds after request as per flow_control.delay_s_after_requests", level="DEBUG")
                time.sleep(cfg.delay_s_after_requests)
            record = _build_output_record(cfg.output_layout, row, body, verbose=verbose)
            return i, record, "\n".join(logs)

        # Execute tasks concurrently and write progressively as they finish
        collected_for_buffer_formats: List[Dict[str, Any]] = []
        pending = set()
        processed_count = 0
        try:
            with ThreadPoolExecutor(max_workers=cfg.concurrent_requests) as executor:
                total = len(rows)
                next_i = 1

                def submit_one(idx: int) -> None:
                    nonlocal pending
                    fut = executor.submit(task_do, idx, rows[idx - 1])
                    pending.add(fut)

                # Prime initial submissions
                while next_i <= total and len(pending) < cfg.concurrent_requests and not interrupted:
                    submit_one(next_i)
                    next_i += 1

                # Process as tasks complete; submit next unless interrupted
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            i, rec, logbuf = fut.result()
                        except Exception as e:
                            _log(f"Task failed: {e}", verbose, level="ERROR")
                            continue
                        # Print grouped logs for this request atomically
                        if logbuf:
                            sys.stderr.write(logbuf + "\n")
                            sys.stderr.flush()
                        # Progressive output writing
                        if fmt == "ndjson":
                            out_file_handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            out_file_handle.flush()
                        elif fmt == "csv":
                            row_out: Dict[str, Any] = {}
                            for k in csv_header:
                                v = rec.get(k)
                                if isinstance(v, (dict, list)):
                                    flat: Dict[str, Any] = {}
                                    _flatten(k, v, flat)
                                    subkeys = [sk for sk in flat.keys() if sk.startswith(k + ".")]
                                    if subkeys:
                                        row_out[k] = json.dumps({sk[len(k)+1:]: flat[sk] for sk in subkeys}, ensure_ascii=False)
                                    else:
                                        row_out[k] = json.dumps(v, ensure_ascii=False)
                                else:
                                    row_out[k] = v
                            line = ",".join(_csv_encode_cell(row_out.get(k), field_quote_map.get(k, False)) for k in csv_header)
                            out_file_handle.write(line + "\n")
                            out_file_handle.flush()
                        else:
                            collected_for_buffer_formats.append(rec)
                        processed_count += 1
                    # Top up submissions
                    while next_i <= total and len(pending) < cfg.concurrent_requests and not interrupted:
                        submit_one(next_i)
                        next_i += 1
        except KeyboardInterrupt:
            interrupted = True
            # Attempt to cancel anything not yet running
            try:
                for fut in list(pending):
                    if not fut.running() and not fut.done():
                        fut.cancel()
            except Exception:
                pass
            in_flight = sum(1 for f in pending if not f.cancelled())
            _log(f"CTRL+C received. Waiting for {in_flight} in-flight request(s) to finish...", verbose, level="WARNING")
            # Wait for running ones to finish and output their results
            try:
                done, still = wait(pending, return_when=None)
                for fut in done:
                    if fut.cancelled():
                        continue
                    try:
                        i, rec, logbuf = fut.result()
                    except Exception as e:
                        _log(f"Task failed during shutdown: {e}", verbose, level="ERROR")
                        continue
                    if logbuf:
                        sys.stderr.write(logbuf + "\n")
                        sys.stderr.flush()
                    if fmt == "ndjson":
                        out_file_handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out_file_handle.flush()
                    elif fmt == "csv":
                        row_out: Dict[str, Any] = {}
                        for k in csv_header:
                            v = rec.get(k)
                            if isinstance(v, (dict, list)):
                                flat: Dict[str, Any] = {}
                                _flatten(k, v, flat)
                                subkeys = [sk for sk in flat.keys() if sk.startswith(k + ".")]
                                if subkeys:
                                    row_out[k] = json.dumps({sk[len(k)+1:]: flat[sk] for sk in subkeys}, ensure_ascii=False)
                                else:
                                    row_out[k] = json.dumps(v, ensure_ascii=False)
                            else:
                                row_out[k] = v
                        line = ",".join(_csv_encode_cell(row_out.get(k), field_quote_map.get(k, False)) for k in csv_header)
                        out_file_handle.write(line + "\n")
                        out_file_handle.flush()
                    else:
                        collected_for_buffer_formats.append(rec)
                    processed_count += 1
            except Exception:
                pass
        finally:
            # Restore original SIGINT handler if we changed it
            try:
                if old_sigint is not None:
                    signal.signal(signal.SIGINT, old_sigint)
            except Exception:
                pass

        # Finalize buffered formats

        # Finalize buffered formats
        if fmt == "json":
            with open(output_path, "w", encoding="utf-8") as out_f:
                json.dump(collected_for_buffer_formats, out_f, ensure_ascii=False, indent=2)
        elif fmt == "yaml":
            with open(output_path, "w", encoding="utf-8") as out_f:
                yaml.safe_dump(collected_for_buffer_formats, out_f, allow_unicode=True, sort_keys=False)

        _log(f"Processed {processed_count} of {len(rows)} requests with concurrency={cfg.concurrent_requests}. Output written to {output_path} as {fmt}.", verbose)

    except OSError as e:
        raise click.ClickException(f"Failed to write output file: {e}")
    finally:
        try:
            if out_file_handle is not None:
                out_file_handle.close()
        except Exception:
            pass


@main.command("with-playfab-id", help="Call PlayFab Server GetPlayerCombinedInfo for each row. Requires request_body.PlayFabId to resolve from CSV.")
@click.option("--config", "config_path", required=True, type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to YAML config file (e.g., retrieve-config-example-playfabid.yml).")
@click.option("--input", "input_csv", required=True, type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to CSV file with arbitrary columns.")
@click.option("--output", "output_path", required=True, type=click.Path(dir_okay=False, writable=True), help="Path to write results. Format determined by config 'output.outputFormat' (json|yaml|ndjson|csv).")
@click.option("--secrets", "secrets_path", required=False, type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to a KEY VALUE PAIRS file to resolve '{$secrets: KEY}' placeholders.")
@click.option("--verbose", is_flag=True, default=False, help="Enable detailed logging; request payloads and response bodies will be printed.")
def cmd_with_playfab_id(config_path: str, input_csv: str, output_path: str, secrets_path: Optional[str], verbose: bool) -> None:
    cfg = load_config(config_path)
    secrets = load_secrets_file(secrets_path)

    rows = read_input_rows(input_csv)
    _log(f"Loaded {len(rows)} input rows from {input_csv}", verbose, level="DEBUG")

    # Prepare headers by substituting secrets (headers are not row-dependent)
    headers_template = cfg.request_header_template or {}
    headers_resolved_any = _apply_secrets_placeholders(headers_template, secrets) if secrets else headers_template
    # Remove None or empty-string headers
    request_headers: Dict[str, str] = {str(k): str(v) for k, v in (headers_resolved_any or {}).items() if v not in (None, "")}

    # VERIFICATION STEP using first row
    sample_row = rows[0]
    sample_payload = build_request_payload_with_secrets(cfg.request_body_template, sample_row, secrets)
    unresolved = _find_unresolved_placeholders(sample_payload)
    resolved_pid = _ensure_playfab_id_present(sample_payload)
    if unresolved:
        raise click.ClickException(
            "Some placeholders in request_body could not be resolved from the CSV for the first row: "
            + ", ".join(unresolved)
        )
    if not resolved_pid:
        raise click.ClickException(
            "Config 'request_body' must contain 'PlayFabId' that resolves to a non-empty value from the CSV."
        )
    # Build redacted headers for verification (do not print secrets)
    redacted_headers: Dict[str, Any] = {}
    for hk, hv in (request_headers or {}).items():
        templ_val = (headers_template or {}).get(hk)
        if _contains_secrets_placeholder(templ_val):
            redacted_headers[hk] = "**REDACTED**"
        else:
            redacted_headers[hk] = hv

    summary = {
        "endpoint": f"{cfg.playfab_api_endpoint}/Server/GetPlayerCombinedInfo",
        "totalRequests": len(rows),
        "resolvedPlayFabIdExample": resolved_pid,
        "payloadExample": sample_payload,
        "requestHeadersExample": redacted_headers,
    }
    sys.stderr.write("=== VERIFICATION ===\n")
    sys.stderr.write("About to make the following requests based on your CONFIG:\n")
    sys.stderr.write(yaml.safe_dump(summary, allow_unicode=True, sort_keys=False))
    sys.stderr.write("====================\n")
    sys.stderr.flush()
    if not click.confirm("Proceed with the requests?", default=False):
        raise click.ClickException("Aborted by user.")

    fmt = cfg.output_format

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

    csv_writer = None
    csv_header = None
    out_file_handle = None

    try:
        if fmt == "ndjson":
            out_file_handle = open(output_path, "w", encoding="utf-8", newline="\n")
        elif fmt == "csv":
            out_file_handle = open(output_path, "w", encoding="utf-8", newline="")
            csv_header = list(cfg.output_layout.keys())
            field_quote_map = {k: any((isinstance(r, dict) and r.get("quote") is True) for r in (cfg.output_layout.get(k) or [])) for k in csv_header}
            def _csv_needs_quote(s: str) -> bool:
                return ("," in s) or ("\"" in s) or ("\n" in s) or ("\r" in s) or s.startswith(" ") or s.endswith(" ")
            def _csv_encode_cell(val: Any, force_quote: bool = False) -> str:
                s = "" if val is None else str(val)
                if force_quote or _csv_needs_quote(s):
                    return "\"" + s.replace("\"", "\"\"") + "\""
                return s
            out_file_handle.write(",".join("\"" + h.replace("\"", "\"\"") + "\"" for h in csv_header) + "\n")
        elif fmt in ("json", "yaml"):
            pass
        else:
            raise click.ClickException(f"Unhandled output format: {fmt}")

        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

        interrupted = False

        def _sigint_handler(signum, frame):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                _log("CTRL+C detected: stopping new submissions and waiting for in-flight requests to finish...", verbose, level="WARNING")

        old_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, _sigint_handler)
        except Exception:
            old_sigint = None

        def task_do(i: int, row: Dict[str, Any]) -> Tuple[int, Dict[str, Any], str]:
            logs: List[str] = []
            def log_line(message: str, level: str = "INFO") -> None:
                if level == "DEBUG" and not verbose:
                    return
                logs.append(f"[{level}] [{i}/{len(rows)}] {message}")

            payload = build_request_payload_with_secrets(cfg.request_body_template, row, secrets)
            unresolved_row = _find_unresolved_placeholders(payload)
            if unresolved_row:
                raise click.ClickException(
                    f"Row {i}: some placeholders in request_body could not be resolved from the CSV: "
                    + ", ".join(unresolved_row)
                )
            pid = _ensure_playfab_id_present(payload)
            if not pid:
                raise click.ClickException(
                    f"Row {i}: 'PlayFabId' from request_body did not resolve to a non-empty value."
                )
            log_line(f"Requested GetPlayerCombinedInfo for PlayFabId='{pid}'")
            data_str = json.dumps(payload)
            log_line(f"Payload to be sent: {data_str}", level="DEBUG")
            status, body = server_get_player_combined_info(cfg.playfab_api_endpoint, request_headers, payload)
            log_line(f"Received status {status}")
            if verbose:
                try:
                    pretty = json.dumps(body, ensure_ascii=False, indent=2)
                except Exception:
                    pretty = str(body)
                if 200 <= int(status) < 300:
                    log_line("Response body:", level="DEBUG")
                else:
                    logs.append("[ERROR] " + f"Non-2xx response for PlayFabId='{pid}'. Full response body follows:")
                logs.append(pretty)
            if cfg.delay_s_after_requests and cfg.delay_s_after_requests > 0:
                log_line(f"Delaying {cfg.delay_s_after_requests} seconds after request as per flow_control.delay_s_after_requests", level="DEBUG")
                time.sleep(cfg.delay_s_after_requests)
            record = _build_output_record(cfg.output_layout, row, body, verbose=verbose)
            return i, record, "\n".join(logs)

        collected_for_buffer_formats: List[Dict[str, Any]] = []
        pending = set()
        processed_count = 0
        try:
            with ThreadPoolExecutor(max_workers=cfg.concurrent_requests) as executor:
                total = len(rows)
                next_i = 1
                def submit_one(idx: int) -> None:
                    nonlocal pending
                    fut = executor.submit(task_do, idx, rows[idx - 1])
                    pending.add(fut)
                while next_i <= total and len(pending) < cfg.concurrent_requests and not interrupted:
                    submit_one(next_i)
                    next_i += 1
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            i, rec, logbuf = fut.result()
                        except Exception as e:
                            _log(f"Task failed: {e}", verbose, level="ERROR")
                            continue
                        if logbuf:
                            sys.stderr.write(logbuf + "\n")
                            sys.stderr.flush()
                        if fmt == "ndjson":
                            out_file_handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            out_file_handle.flush()
                        elif fmt == "csv":
                            row_out: Dict[str, Any] = {}
                            for k in csv_header:
                                v = rec.get(k)
                                if isinstance(v, (dict, list)):
                                    flat: Dict[str, Any] = {}
                                    _flatten(k, v, flat)
                                    subkeys = [sk for sk in flat.keys() if sk.startswith(k + ".")]
                                    if subkeys:
                                        row_out[k] = json.dumps({sk[len(k)+1:]: flat[sk] for sk in subkeys}, ensure_ascii=False)
                                    else:
                                        row_out[k] = json.dumps(v, ensure_ascii=False)
                                else:
                                    row_out[k] = v
                            line = ",".join(_csv_encode_cell(row_out.get(k), field_quote_map.get(k, False)) for k in csv_header)
                            out_file_handle.write(line + "\n")
                            out_file_handle.flush()
                        else:
                            collected_for_buffer_formats.append(rec)
                        processed_count += 1
                    while next_i <= total and len(pending) < cfg.concurrent_requests and not interrupted:
                        submit_one(next_i)
                        next_i += 1
        except KeyboardInterrupt:
            interrupted = True
            try:
                for fut in list(pending):
                    if not fut.running() and not fut.done():
                        fut.cancel()
            except Exception:
                pass
            in_flight = sum(1 for f in pending if not f.cancelled())
            _log(f"CTRL+C received. Waiting for {in_flight} in-flight request(s) to finish...", verbose, level="WARNING")
            try:
                done, still = wait(pending, return_when=None)
                for fut in done:
                    if fut.cancelled():
                        continue
                    try:
                        i, rec, logbuf = fut.result()
                    except Exception as e:
                        _log(f"Task failed during shutdown: {e}", verbose, level="ERROR")
                        continue
                    if logbuf:
                        sys.stderr.write(logbuf + "\n")
                        sys.stderr.flush()
                    if fmt == "ndjson":
                        out_file_handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        out_file_handle.flush()
                    elif fmt == "csv":
                        row_out: Dict[str, Any] = {}
                        for k in csv_header:
                            v = rec.get(k)
                            if isinstance(v, (dict, list)):
                                flat: Dict[str, Any] = {}
                                _flatten(k, v, flat)
                                subkeys = [sk for sk in flat.keys() if sk.startswith(k + ".")]
                                if subkeys:
                                    row_out[k] = json.dumps({sk[len(k)+1:]: flat[sk] for sk in subkeys}, ensure_ascii=False)
                                else:
                                    row_out[k] = json.dumps(v, ensure_ascii=False)
                            else:
                                row_out[k] = v
                        line = ",".join(_csv_encode_cell(row_out.get(k), field_quote_map.get(k, False)) for k in csv_header)
                        out_file_handle.write(line + "\n")
                        out_file_handle.flush()
                    else:
                        collected_for_buffer_formats.append(rec)
                    processed_count += 1
            except Exception:
                pass
        finally:
            try:
                if old_sigint is not None:
                    signal.signal(signal.SIGINT, old_sigint)
            except Exception:
                pass

        if fmt == "json":
            with open(output_path, "w", encoding="utf-8") as out_f:
                json.dump(collected_for_buffer_formats, out_f, ensure_ascii=False, indent=2)
        elif fmt == "yaml":
            with open(output_path, "w", encoding="utf-8") as out_f:
                yaml.safe_dump(collected_for_buffer_formats, out_f, allow_unicode=True, sort_keys=False)

        _log(f"Processed {processed_count} of {len(rows)} requests with concurrency={cfg.concurrent_requests}. Output written to {output_path} as {fmt}.", verbose)

    except OSError as e:
        raise click.ClickException(f"Failed to write output file: {e}")
    finally:
        try:
            if out_file_handle is not None:
                out_file_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()

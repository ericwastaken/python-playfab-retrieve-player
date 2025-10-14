# playfab-retrieve-player

CLI to retrieve PlayFab player information for a list of custom IDs provided in a CSV. The tool lets you:
- Build the exact request body you want to send to PlayFab (including InfoRequestParameters) using a YAML config.
- Substitute data from the input CSV into the request body (e.g., set CustomId from a CSV column).
- Extract fields from PlayFab responses using JSONPath expressions into a structured output in JSON, YAML, NDJSON, or CSV.

This README covers both regular usage and developer notes. It references the examples included in this repository:
- data/input-example.csv
- data/retrieve-config-example.yml

Useful PlayFab documentation:
- LoginWithCustomID REST reference: https://learn.microsoft.com/en-us/rest/api/playfab/client/authentication/login-with-custom-id?view=playfab-rest
- InfoRequestParameters on the same page: https://learn.microsoft.com/en-us/rest/api/playfab/client/authentication/login-with-custom-id?view=playfab-rest#inforequestparameters
- Player profile and data overview: https://learn.microsoft.com/en-us/gaming/playfab/api-references/data-types/player-profile
- Player data (User Data): https://learn.microsoft.com/en-us/gaming/playfab/features/data/playerdata/


## Quick start (users)

1) Install (venv will be created)
- Use the provided bootstrap script to automatically create .venv and install the package:
  - Regular install:
    - `python3 bootstrap.py`
  - Editable (dev) install:
    - `python3 bootstrap.py --editable`
- After install, you can run the CLI without activating the venv:
  - `./playfab-retrieve-player --help`
- If you prefer to manage venvs yourself, you can still do:
  - `python -m venv .venv && .venv/bin/python -m pip install -U pip && .venv/bin/pip install -e .`

2) Prepare your input CSV
- Use data/input-example.csv as a template. The CSV can have any header columns you prefer; there is no strict requirement 
  for a column named customId. Columns can be referenced in the request body using the $.<column-name> notation. Example:
  ```text
  "customId","batchTag"
  "YOUR-PLAYFAB-CUSTOM-ID-001","alpha"
  "YOUR-PLAYFAB-CUSTOM-ID-002","beta"
  ```
  
3) Prepare your config YAML
- Copy data/retrieve-config-example.yml to a new file (e.g., data/retrieve-config.yml) and edit it. Details of each 
  field are described below in Config reference.

4) Run the tool
- Example command:
  - `playfab-retrieve-player with-custom-id --config data/retrieve-config.yml --input data/input.csv --output out.csv`
- On start, the tool prints a verification summary showing the endpoint, total requests, a resolvedCustomIdExample 
  (taken from the first row after substitutions), and a payload example. You must confirm to proceed.
- Use --verbose to print each request payload and the full response bodies to stderr.


## CLI usage

After installation, the CLI entry point is available as a command group:
- `playfab-retrieve-player --help` (shows available commands)
- `playfab-retrieve-player with-custom-id --help` (shows options for this command)

Commands:
See the [Commands section below](#commands) for details.
- with-custom-id
- with-playfab-id

options:
- --config PATH (required)
  - Path to a YAML config file. See Config reference below. Must contain playfab_api_endpoint, request_body, and output.
- --input PATH (required)
  - Path to a CSV file with arbitrary header columns. Values are substituted into request_body via '$.<columnName>' placeholders.
- --output PATH (required)
  - File path to write results. The output format is determined by output.outputFormat in the config.
- --verbose (flag)
  - Enables detailed logging. Shows serialized request payloads and response bodies.
  - Note: JSONPath extraction warnings are always printed to stderr, regardless of --verbose.

Exit behavior and errors:
- The tool raises human-friendly errors if the CSV is unreadable, if placeholders in request_body cannot be resolved
  from the CSV, if the request_body lacks a resolvable CustomId, or if output cannot be written.
- Network errors are reported with a status of 0 and an error message in the response body for that row.

Verification step shows:
- endpoint
- totalRequests
- resolvedPlayFabIdExample
- payloadExample
- requestHeadersExample (any header values originating from secrets are displayed as **REDACTED**)

Secrets file format and usage for this command:
- Provide with --secrets PATH. Supported line formats:
    - KEY VALUE
    - KEY=VALUE
- Comments (#...) and empty lines are ignored.
- Reference secrets in config via:
    - Mapping sentinel (entire value is a secret): `X-SecretKey: { $secrets: SERVER_SECRET_KEY }`
    - Inline string (embedded): `Authorization: "Bearer { $secrets: CLOUD_PROD_AUTH_TOKEN }"`
- Secrets placeholders are supported in request_header and request_body for this command; values are redacted in verification output but used when sending requests.


## Config reference

The config file is a YAML document with these top-level sections:
- playfab_api_endpoint: string
- request_body: mapping
- output: mapping
- flow_control: mapping (optional) — controls concurrency and pacing of requests

## Understanding the response and building outputs

- PlayFab successful responses are 200 OK with a body whose data property includes InfoResultPayload when InfoRequestParameters are used.
- Typical shapes relevant to this tool:
  - $.data.InfoResultPayload.PlayerProfile.DisplayName
  - $.data.InfoResultPayload.UserData["<Key>"].Value
- Use JSONPath in your layout rules to target the exact data you want. If you see warnings like "No match for JSONPath ...", 
  double-check your path and make sure you’re not accidentally including an extra response wrapper.
- CSV output flattening:
  - When outputFormat is csv and a field resolves to an object or list, the tool flattens it into multiple columns 
    using dot and numeric indices, e.g. profile.name -> profile.name, list[0] -> list.0.
- NDJSON output:
  - Each record is written on its own line as JSON.

Tip: Run with --verbose to print full response bodies to stderr; then craft/validate your JSONPath expressions interactively.

## How to analyze the raw PlayFab response

When you’re deciding which JSONPath expressions to use in output.layout, it helps to see the exact raw response from 
PlayFab for your request.

Follow these steps:

1) Run a small sample with verbose logging
- Use one or a few rows from your CSV and add --verbose. Example:
  ```bash
  playfab-retrieve-player with-custom-id \
    --config data/retrieve-config-example.yml \
    --input data/input-example.csv \
    --output out.csv \
    --verbose
  ```
- The tool prints grouped logs for each request to stderr (not to the output file). You will see the serialized payload 
  and then the full JSON response body.

2) Find the root of the data you need
- For LoginWithCustomID, when InfoRequestParameters are used, most interesting data appears under:
  - $.data.InfoResultPayload
- Common examples you’ll see in the verbose response:
  - $.data.InfoResultPayload.PlayerProfile.DisplayName
  - $.data.InfoResultPayload.UserData["SomeKey"].Value

3) Craft JSONPath expressions for your layout
- Start paths at the root ($). Do not add an extra wrapper like $.response unless your actual response shows it (the CLI 
  passes the parsed PlayFab JSON directly to the JSONPath evaluator).
- Keys that contain dots or special characters must use bracket notation:
  - $.data.InfoResultPayload.UserData["SomeKey"].Value
- Arrays: use numeric indexes as needed (e.g., $.data.InfoResultPayload.SomeList[0].Id).
- If a value you’re extracting is itself a JSON-encoded string (common in UserData), set json_parse: true so it becomes 
  a structured object.
  ```yaml
  output:
    outputFormat: csv
    layout:
      walletId:
        - source: response
          path: $.data.InfoResultPayload.UserData["devhub.aptosWalletId"].Value
          json_parse: true
  ```

4) Validate and iterate
- After editing your config, run again with --verbose and confirm that your fields extract correctly. If you see warnings 
  like "No match for JSONPath ...", copy a small portion of the response shown in stderr and double-check your JSONPath 
  against that structure.
- Tip: You can paste a single response object into an online JSONPath tester to trial expressions before adding them to 
  your config.

## Examples

- Using the included examples:
  ```bash
  playfab-retrieve-player with-custom-id \
    --config data/retrieve-config-example.yml \
    --input data/input-example.csv \
    --output out.csv \
    --verbose
  ```

- Change layout to write JSON instead of CSV:
  - In your config set outputFormat: json, then:
  - `playfab-retrieve-player with-custom-id --config data/retrieve-config.yml --input data/input.csv --output out.json`


## Commands Reference

### with-custom-id

Calls PlayFab Client LoginWithCustomID for each row in your CSV.

- Endpoint used: <playfab_api_endpoint>/Client/LoginWithCustomID
- Required in config.request_body:
  - CustomId: must resolve from your input CSV using the $.<column> syntax.
  - InfoRequestParameters: optional. Mirrors the REST API parameters on the Client endpoint.
- For shared behavior across all commands (config structure, verification, output layout and JSONPath, json_parse options, flow_control, logging), see All Commands.
- Example run:
  ```bash
  playfab-retrieve-player with-custom-id \
    --config data/retrieve-config-example.yml \
    --input data/input-example.csv \
    --output out.csv \
    --verbose
  ```

Example (see data/retrieve-config-example.yml):

```yaml
playfab_api_endpoint: https://XXXXXX.playfabapi.com

flow_control:
  concurrent_requests: 10
  delay_s_after_requests: 2
    
request_body:
  # Matching RequestBody in https://learn.microsoft.com/en-us/rest/api/playfab/client/authentication/login-with-custom-id?view=playfab-rest
  TitleId: XXXXXX
  CustomId: $.customId
  CreateAccount: false
  # You can reference ANY input CSV column via $.<column-name>. Example: pass a batch tag from CSV
  CustomTags:
    batch: $.batchTag
  # Matching https://learn.microsoft.com/en-us/rest/api/playfab/client/authentication/login-with-custom-id?view=playfab-rest#inforequestparameters
  InfoRequestParameters:
    GetPlayerProfile: true
    ProfileConstraints:
      ShowDisplayName: true
    GetUserData: true
    UserDataKeys:
      - "your-user-data-key-name"
      - "your-user-data-key-name-2"

output:
  # outputFormat: yaml, json, ndjson, csv
  outputFormat: csv
  # The file layout. In the case of CSV, all nesting is flattened.
  layout:
    customId:
      - source: inputcsv
        path: customId
    displayName:
      - source: response
        path: $.data.InfoResultPayload.PlayerProfile.DisplayName
    someCustomKey:
      - source: response
        path: $.data.InfoResultPayload.UserData["your-user-data-key-name"].Value
        json_parse: true
    someCustomKey2:
      - source: response
        path: $.data.InfoResultPayload.UserData["your-user-data-key-name-2"].Value
        json_parse: true
```


### with-playfab-id

Calls PlayFab Server GetPlayerCombinedInfo for each row in your CSV. Use this when you already have PlayFab player IDs and want to gather combined info in bulk.

- Endpoint used: <playfab_api_endpoint>/Server/GetPlayerCombinedInfo
- Required in config.request_body:
  - PlayFabId: must resolve from your input CSV using the $.<column> syntax.
  - InfoRequestParameters: optional but recommended. Mirrors the REST API parameters.
- Optional request headers via config.request_header (e.g., X-SecretKey). You can supply secrets via --secrets and reference them in the config.
- For shared behavior across all commands (config structure, verification, output layout and JSONPath, json_parse options, flow_control, logging), see All Commands.

Example config (see also data/retrieve-config-example-playfabid.yml):

```yaml
a: &endpoint https://XXXXXX.playfabapi.com

playfab_api_endpoint: *endpoint

flow_control:
  concurrent_requests: 10
  delay_s_after_requests: 2

request_header:
  # The PlayFab Server secret key supplied as a header; resolved from secrets file
  X-SecretKey: { $secrets: SERVER_SECRET_KEY }
  # Or embed inside a string:
  Authorization: "Bearer { $secrets: CLOUD_PROD_AUTH_TOKEN }"

request_body:
  # Matching RequestBody in https://learn.microsoft.com/en-us/rest/api/playfab/server/player-data-management/get-player-combined-info?view=playfab-rest
  PlayFabId: $.playFabId
  # You can reference ANY input CSV column via $.<column-name>.
  CustomTags:
    batch: $.batchTag
  # Matching https://learn.microsoft.com/en-us/rest/api/playfab/server/player-data-management/get-player-combined-info?view=playfab-rest#getplayercombinedinforequestparams
  InfoRequestParameters:
    GetPlayerProfile: true
    ProfileConstraints:
      ShowDisplayName: true
    GetUserData: true
    UserDataKeys:
      - "your-user-data-key-name"
      - "your-user-data-key-name-2"

output:
  outputFormat: csv
  layout:
    playFabId:
      - source: inputcsv
        path: playFabId
    displayName:
      - source: response
        path: $.response.data.InfoResultPayload.PlayerProfile.DisplayName
    someCustomKey:
      - source: response
        path: $.response.data.InfoResultPayload.UserData["your-user-data-key-name"].Value
        json_parse: true
```

Run:

```bash
playfab-retrieve-player with-playfab-id \
  --config data/retrieve-config-example-playfabid.yml \
  --input data/your-input.csv \
  --output out.csv \
  --secrets data/pfan-secrets.env \
  --verbose
```

## All Commands

The following concepts and behaviors are common to all sub-commands:

- Configuration file structure
  - Top-level keys: playfab_api_endpoint, request_body, output, optional flow_control.
  - request_body can reference any CSV column using the $.<columnName> notation.
- Verification step before execution
  - The CLI prints a summary with the endpoint, totalRequests, an example of the key identifier resolved from the first row, and a payloadExample.
  - You must confirm to proceed.
- Output formats and layout
  - output.outputFormat: json, yaml, ndjson, or csv.
  - output.layout: define fields from either the input CSV (source: inputcsv) or the API response (source: response) using JSONPath.
  - For csv, objects/lists are serialized into the cell; nested data from multiple rules can be flattened as needed.
- JSONPath extraction and json_parse
  - Use paths like $.data.InfoResultPayload.PlayerProfile.DisplayName or $.data.InfoResultPayload.UserData["Key"].Value.
  - The CLI attempts to be forgiving if you accidentally include a leading $.response.
  - Parsing JSON-encoded strings (common in UserData.Value):
    - Option A — json_parse: true on a rule
      - Use when you want the entire extracted value parsed as JSON (no further selection in the same rule).
      - Example:
        ```yaml
        layout:
          walletId:
            - source: response
              path: $.response.data.InfoResultPayload.UserData["devhub.aptosWalletId"].Value
              json_parse: true
        ```
    - Option B — Extended path wrapper: json_parse(INNER_JSONPATH)SUFFIX
      - Use when you need to parse and then immediately select a nested piece in a single rule.
      - INNER_JSONPATH is evaluated against the full response; if it resolves to a JSON string, it is decoded.
      - SUFFIX is an optional JSONPath applied to the parsed object (e.g., [1].id, .field, [*].id).
      - Examples:
        ```yaml
        layout:
          artifactId:
            - source: response
              path: json_parse($.response.data.InfoResultPayload.UserData["artifacts"].Value)[1].id
          allIds:
            - source: response
              path: json_parse($.response.data.InfoResultPayload.UserData["artifacts"].Value)[*].id
          lastItem:
            - source: response
              path: json_parse($.response.data.InfoResultPayload.UserData["artifacts"].Value)[-1]
        ```
  - Behavior notes:
    - If a JSONPath finds multiple values, the CLI returns a list; if exactly one, a scalar; if none, null and a warning (when --verbose).
    - For CSV output, objects/lists are JSON-serialized into the cell.
- Concurrency and pacing (flow_control)
  - concurrent_requests controls parallelism; delay_s_after_requests adds a per-request pause by each worker.
- Logging
  - --verbose prints each request payload and the full response bodies to stderr, grouped per row.

Notes:
- Secrets and request_header processing apply only to with-playfab-id, which supports --secrets and header templating.


## Developer guide

- Requirements
    - Python 3.8+
    - pip
- Setup (recommended):
  ```bash
  python -m venv .venv
  source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
  python -m pip install -U pip
  python -m pip install -e .
  python -m pip install -r requirements.txt
  ```
- Local run:
    - `playfab-retrieve-player with-custom-id --config data/retrieve-config-example.yml --input data/input-example.csv --output out.csv --verbose`
- Code layout
    - playfab_retrieve/cli.py contains the CLI and all logic for config loading, CSV reading, HTTP requests, extraction,
      and output writing.
    

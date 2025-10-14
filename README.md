# python-playfab-retrieve

CLI to retrieve PlayFab player information by calling the Client LoginWithCustomID endpoint for a list of custom IDs 
provided in a CSV. The tool lets you:
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

1) Install
- It’s recommended to use a virtual environment first.
- Install the package from the repo root:
  - Editable (dev):
    - `python -m pip install -e .`
    - See [Developer Guide](#developer-guide) below for more details.
  - Regular:
    - `pip install .`

2) Prepare your input CSV
- Use data/input-example.csv as a template. The CSV must have a header column named customId. You can add any number 
  of additional columns; they can be referenced in the request body using the $.<column-name> notation. Example:
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
  - `playfab-retrieve with-custom-id --config data/retrieve-config.yml --input data/input.csv --output out.csv`
- On start, the tool prints a verification summary showing the endpoint, total requests, first customId, and a payload 
  example. You must confirm to proceed.
- Use --verbose to print each request payload and the full response bodies to stderr.


## CLI usage

After installation, the CLI entry point is available as a command group:
- `playfab-retrieve --help` (shows available commands)
- `playfab-retrieve with-custom-id --help` (shows options for this command)

Commands:
- with-custom-id
  - Calls PlayFab Client LoginWithCustomID for each row in the CSV.
  - Requires at least a customId column in the input CSV.

with-custom-id options:
- --config PATH (required)
  - Path to a YAML config file. See Config reference below. Must contain playfab_api_endpoint, request_body, and output.
- --input PATH (required)
  - Path to a CSV file with a header column customId. Rows with empty customId are skipped; if none remain, the run aborts.
- --output PATH (required)
  - File path to write results. The output format is determined by output.outputFormat in the config.
- --verbose (flag)
  - Enables detailed logging. Shows serialized request payloads and response bodies.
  - Note: JSONPath extraction warnings are always printed to stderr, regardless of --verbose.

Exit behavior and errors:
- The tool raises human-friendly errors if the CSV is unreadable/missing customId, if the config is invalid, or if 
  output cannot be written.
- Network errors are reported with a status of 0 and an error message in the response body for that row.


## Config reference (data/retrieve-config-example.yml)

The config file is a YAML document with three top-level sections:
- playfab_api_endpoint: string
- request_body: mapping
- output: mapping

Example (see data/retrieve-config-example.yml):

```yaml
playfab_api_endpoint: https://XXXXXX.playfabapi.com

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

Notes and details:
- playfab_api_endpoint
  - Your title’s endpoint root, e.g., https://<titleId>.playfabapi.com. The tool appends /Client/LoginWithCustomID when making requests.
- request_body
  - This is sent as the JSON body to LoginWithCustomID. See PlayFab docs: https://learn.microsoft.com/en-us/rest/api/playfab/client/authentication/login-with-custom-id?view=playfab-rest
  - Values that look like JSONPath (e.g., strings starting with $. ) are substituted from the current CSV row before 
    sending. For example, CustomId: $.customId reads the customId column value for each row and populates CustomId.
  - You can pass InfoRequestParameters to ask PlayFab to include additional info in the response (e.g., GetPlayerProfile, 
    GetUserData with specific UserDataKeys). See the InfoRequestParameters section: https://learn.microsoft.com/en-us/rest/api/playfab/client/authentication/login-with-custom-id?view=playfab-rest#inforequestparameters
  - The tool does not add or remove fields beyond performing these substitutions; it sends exactly what you put in request_body.
- output
  - outputFormat: one of json, yaml, ndjson, csv.
  - layout: a mapping of output field names to one or more extraction rules. Each rule is a mapping with:
    - source: inputcsv or response
      - inputcsv means copy a value from the input CSV row using the provided path as the column name.
      - response means extract from the PlayFab response body using a JSONPath.
    - path: string
      - If source is inputcsv: the CSV column name.
      - If source is response: a JSONPath expression. These are evaluated over the full response JSON. If you used 
        InfoRequestParameters, the relevant data is typically under $.data.InfoResultPayload. Examples:
        - $.data.InfoResultPayload.PlayerProfile.DisplayName
        - $.data.InfoResultPayload.UserData["Key"].Value
    - json_parse: boolean (optional)
      - If true and the extracted value is a string containing JSON, the tool will parse it into a structured object 
        before writing to output (useful when your UserData values are JSON-encoded strings).


## Understanding the response and building outputs

- PlayFab successful responses for LoginWithCustomID are 200 OK with a body whose data property includes InfoResultPayload 
  when InfoRequestParameters are used.
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


## Examples

- Using the included examples:
  ```bash
  playfab-retrieve with-custom-id \
    --config data/retrieve-config-example.yml \
    --input data/input-example.csv \
    --output out.csv \
    --verbose
  ```

- Change layout to write JSON instead of CSV:
  - In your config set outputFormat: json, then:
  - `playfab-retrieve with-custom-id --config data/retrieve-config.yml --input data/input.csv --output out.json`


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
  - `playfab-retrieve with-custom-id --config data/retrieve-config-example.yml --input data/input-example.csv --output out.csv --verbose`
- Code layout
  - playfab_retrieve/cli.py contains the CLI and all logic for config loading, CSV reading, HTTP requests, extraction, 
    and output writing.
    

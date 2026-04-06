# luma

A CLI tool that queries and browses Luma events across categories and calendars.

## Setup

- Clone the repo, `cd` into it
- `make setup` to install dependencies
- Add venv to PATH (printed by `make setup`)
- Recreate from scratch: `make clean && make setup`

## LLM configuration

Configure your LLM provider in `~/.luma/config.toml`:

```toml
[llm]
provider = "anthropic"

[llm.anthropic]
api_key = "sk-ant-..."
model = "claude-sonnet-4-20250514"

[llm.ollama]
host = "http://localhost:11434"
model = "llama3.1"
```

Override provider per-command:

```shell
luma --provider ollama "events this week"
```

## Usage

Fetch events from all sources:

```shell
luma refresh
```

Query cached events:

```shell
# Default view — next 14 days, top 100, sorted by date
luma

# Narrow window and limit
luma --days 7 --top 50

# Sort by popularity
luma --sort guest --min-guest 100

# Search and filter by weekday
luma --search 'AI' --day Tue,Thu

# Time-of-day filter
luma --min-time 18 --max-time 21
```

Free-text query (routed through the agent):

```shell
# Ask a question
luma "what events are happening this week?"

# Combine with filter flags
luma --days 7 "AI meetups"
```

Interactive chat:

```shell
luma chat
```

Run tests:

```shell
make test
```

## Evals

**Running evals**

- `make eval-list` -- list all available datasets
- `make eval-smoke` -- one tagged case per dataset, fast sanity check
- `make eval SET=query_command/date_parsing` -- single dataset
- `make eval-all` -- all datasets sequentially
- `make eval-all TAG=nature:edge_case` -- filter cases by metadata tag across all datasets
- `make eval SET=query_command/smoke PROVIDER=ollama` -- run evals with a specific provider
- `make eval VERBOSE=1` or `make eval-all VERBOSE=1` -- show per-assertion pass/fail reasons

**Baselines**

Each dataset has a committed `<dataset>.baseline.json` that lives next to the dataset file. On every `make eval` run the runner loads it automatically and prints a diff table. To save a new baseline after you are satisfied with results:

```shell
make save-baseline SET=query_command/date_parsing  # single dataset
make save-baseline-all                              # all datasets
```

**Iteration workflow**

1. `make eval-smoke` — confirm nothing is broken
2. `make eval SET=<dataset>` — inspect the affected capability in detail
3. Make a change (prompt, tool, params)
4. `make eval SET=<dataset>` — review diff against baseline
5. Repeat 3–4 until satisfied
6. `make save-baseline SET=<dataset>` — record new baseline
7. Commit the change and the updated `.baseline.json` together

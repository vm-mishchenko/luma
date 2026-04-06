# luma

A CLI tool to query and browse Luma events.

## Install

- Clone and `cd` into the repo
- Run `make setup` and put the venv on your PATH (the command prints where it is)
- Nuclear option: `make clean && make setup`

## Usage

Pull fresh events into the local cache:

```shell
luma refresh
```

Browse what is cached (default is about the next two weeks, top 100, sorted by date):

```shell
luma
luma next-week --min-guest 100
luma --days 7 --top 50
luma --sort guest --min-guest 100
luma --search 'AI' --day Tue,Thu
luma --min-time 18 --max-time 21
```

Free-text questions go through the agent (same flags still apply):

```shell
luma "what events are happening this week?"
luma --days 7 "AI meetups"
```

Interactive chat:

```shell
luma chat
```

Everything else (flags, subcommands): `luma --help`.

## Configuration

Edit `~/.luma/config.toml` for defaults.

### LLM

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

### One-off provider

```shell
luma --provider ollama "events this week"
```

## Development

### Tests

```shell
make test
```

### Evals

- `make eval-list` — list datasets
- `make eval-smoke` — one tagged case per dataset
- `make eval SET=query_command/date_parsing` — single dataset
- `make eval-all` — all datasets, in order
- `make eval-all TAG=nature:edge_case` — filter cases by tag
- `make eval SET=query_command/smoke PROVIDER=ollama` — force a provider
- `make eval VERBOSE=1` or `make eval-all VERBOSE=1` — show each assertion

Each dataset can ship a committed `<dataset>.baseline.json` next to the dataset file. `make eval` loads it and prints a diff table.

```shell
make save-baseline SET=query_command/date_parsing
make save-baseline-all
```

Typical loop: `make eval-smoke`, then `make eval SET=<dataset>`, change things, rerun eval, run `make save-baseline SET=<dataset>` when happy, commit the code and the updated `.baseline.json` together.

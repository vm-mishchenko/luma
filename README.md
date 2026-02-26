# luma

A CLI tool that queries and browses Luma events across categories and calendars.

## Setup

- Clone the repo, `cd` into it
- `make setup` to install dependencies
- Add venv to PATH (printed by `make setup`)
- Recreate from scratch: `make clean && make setup`

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

Manage seen events:

```shell
# Mark displayed events as seen
luma --discard

# Show all including previously seen (grayed out)
luma --all

# Clear seen list
luma --reset
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

Run agent evaluations using `pydantic-evals` (installed via `make setup`):

```shell
# List available eval sets
make eval

# Run a specific eval set
make eval SET=smoke

# Verbose output (per-evaluator detail)
make eval SET=smoke VERBOSE=1
```

Add a new eval set by creating a Python file in `evals/` that defines a `dataset` variable.

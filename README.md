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

VENV := .venv
BIN  := $(VENV)/bin

.PHONY: setup clean test run eval

setup:
	python3 -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"
	@echo ""
	@echo "Add to ~/.zshrc"
	@echo '  export PATH="$(CURDIR)/$(BIN):$$PATH"'
	@echo "Reload shell:"
	@echo "  source ~/.zshrc"

clean:
	rm -rf $(VENV) src/*.egg-info build dist

test:
	$(BIN)/pytest tests/

run:
	$(BIN)/luma

eval:
ifndef SET
	$(BIN)/python -m evals.runner --list
else
	$(BIN)/python -m evals.runner --set $(SET) $(if $(VERBOSE),--verbose)
endif

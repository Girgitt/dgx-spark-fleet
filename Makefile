.PHONY: test sources
sources:
	git submodule update --init --recursive

test:
	python3 -m unittest discover -s tests -v
	python3 -m py_compile fleet.py scripts/smoke-openai.py

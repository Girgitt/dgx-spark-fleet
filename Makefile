.PHONY: bootstrap test sources

bootstrap:
	./scripts/bootstrap.sh

sources:
	git submodule update --init --recursive

test:
	./scripts/python.sh -m unittest discover -s tests -v
	./scripts/python.sh -m py_compile fleet.py fleetctl.py scripts/smoke-openai.py recipes/artifacts/*.py
